#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v4l2dec_bench.py —— **不经 ffmpeg** 的手写 V4L2 有状态解码，直接量延迟。

动机：ffmpeg 的 `h264_v4l2m2m` 管线固定比进程内软解多 1~2 帧。其中一帧来自 raw h264
demuxer 的 parser（必须等到**下一帧**的起始码才肯吐当前帧），另一帧来自 rawvideo muxer + AVIO。
本脚本按 AUD 自己切好完整 access unit，直接 QBUF 一个 AU，DQBUF 到帧立刻拿走，
理论上能把这两层省掉。

ioctl 顺序照抄 ffmpeg libavcodec/v4l2_m2m（这是能驱动 qcom-iris 的关键）：
    S_FMT(OUTPUT,H264) → REQBUFS(OUTPUT) → 写第 1 个 AU + QBUF → STREAMON(OUTPUT)
    → G_FMT(CAPTURE) → S_FMT(CAPTURE,NV12) → REQBUFS(CAPTURE) → QBUF 全部 → STREAMON(CAPTURE)
    → 循环：QBUF 下一个 AU / DQBUF capture 取帧

用法：python3 v4l2dec_bench.py <h264> [--pace 0.033] [--aus 200] [--w 1280] [--h 720]
                             [--n-out 4] [--n-cap 4] [--verbose]
"""
import argparse
import os
import select
import sys
import time

import v4l2m2m as V


def split_aus(d):
    """按 AUD(type 9) 切 access unit；没有 AUD 就退化成整段一包。"""
    starts, i, n = [], 0, len(d)
    while i + 4 < n:
        if d[i:i + 4] == b"\x00\x00\x00\x01":
            sc, p = 4, i
        elif d[i:i + 3] == b"\x00\x00\x01":
            sc, p = 3, i
        else:
            i += 1
            continue
        if p + sc < n:
            starts.append((p, d[p + sc] & 0x1F))
        i = p + sc + 1
    auds = [p for p, t in starts if t == 9]
    if len(auds) > 1:
        return [d[auds[k]:(auds[k + 1] if k + 1 < len(auds) else n)] for k in range(len(auds))]
    return [d]


class HandDec(object):
    def __init__(self, dev, n_out, n_cap, verbose=False):
        self.d = V.Decoder(dev=dev, n_output=n_out, n_capture=n_cap)
        self.n_out, self.n_cap = n_out, n_cap
        self.verbose = verbose
        self.free_out = []
        self.pending_out = set()
        self.frames = []          # (t_recv, index, sequence, bytesused, flags)
        self.cap_ready = False
        self.out_streaming = False
        self.log_n = 0
        self.drc_done = 0
        self.last_handled = False

    def log(self, msg):
        if self.verbose:
            print("    [dec] %s" % msg, flush=True)

    # ---- 启动：完全照 ffmpeg 的顺序 ----
    def open(self, w, h):
        self.d.open_output(V.PIX_H264, w, h, n=self.n_out)
        self.d.subscribe(V.EVENT_SOURCE_CHANGE)
        self.d.subscribe(V.EVENT_EOS)
        self.free_out = list(range(len(self.d.out)))
        self.log("output 队列就绪，空闲缓冲 %s" % self.free_out)

    def feed(self, au):
        """把一个完整 AU 写进 output 缓冲并 QBUF。返回是否成功。"""
        if not self.free_out:
            return False
        idx = self.free_out.pop(0)
        mm = self.d.out[idx]["mmap"]
        mm.seek(0)
        mm.write(au)
        try:
            self.d.qbuf(V.BUF_OUTPUT_MPLANE, idx, len(au), V.MEMORY_MMAP, self.d.out_planes)
        except V.V4L2Error as e:
            self.free_out.insert(0, idx)
            raise
        self.pending_out.add(idx)
        return True

    def start_capture(self):
        """第 1 个 AU 入队后：STREAMON(output) → G_FMT/S_FMT/REQBUFS/QBUF/STREAMON(capture)。"""
        if not self.out_streaming:
            self.d.stream(True, V.BUF_OUTPUT_MPLANE)
            self.out_streaming = True
            self.log("STREAMON output")
        self.d.open_capture(start=True)
        self.cap_ready = True

    def handle_drc(self, why):
        """动态分辨率变化（DRC）：按 V4L2 规范重建 capture 队列并 START 恢复。
        顺序：STREAMOFF(capture) → REQBUFS(0) → G_FMT/S_FMT/REQBUFS/QBUF/STREAMON → DEC_CMD_START。"""
        self.drc_done += 1
        print("    [dec] === DRC #%d（%s）===" % (self.drc_done, why), flush=True)
        try:
            self.d.stream(False, V.BUF_CAPTURE_MPLANE)
            self.log("STREAMOFF capture")
        except V.V4L2Error as e:
            self.log("STREAMOFF 失败: %s" % e)
        try:
            self.d.reqbufs(V.BUF_CAPTURE_MPLANE, 0, V.MEMORY_MMAP)
            self.log("REQBUFS(capture,0) 释放旧缓冲")
        except V.V4L2Error as e:
            self.log("REQBUFS(0) 失败: %s" % e)
        for d in self.d.cap.values():
            if d.get("fd") is not None:
                try:
                    os.close(d["fd"])
                except OSError:
                    pass
        self.d.cap = {}
        self.d.open_capture(start=True, n=self.n_cap)
        try:
            self.d.decoder_cmd(V.DEC_CMD_START)
            print("    [dec] DECODER_CMD_START 成功（恢复解码）", flush=True)
        except V.V4L2Error as e:
            print("    [dec] DECODER_CMD_START 被拒: %s" % e, flush=True)
        self.cap_ready = True

    def reclaim_out(self):
        for _ in range(self.n_out):
            try:
                b, _p = self.d.dqbuf(V.BUF_OUTPUT_MPLANE, self.d.out_planes)
            except V.V4L2Error:
                break
            self.pending_out.discard(b.index)
            self.free_out.append(b.index)
            self.log("回收 output #%d（bytesused=%d flags=%#x）" % (b.index, b.m.planes[0].bytesused, b.flags))

    def pump_capture(self, verbose_frames=6):
        """收所有已完成的 capture 帧，返回本次新增的帧列表。"""
        got = []
        if not self.cap_ready:
            return got
        for _ in range(self.n_cap):
            try:
                b, planes = self.d.dqbuf(V.BUF_CAPTURE_MPLANE, self.d.cap_planes)
            except V.V4L2Error:
                break
            used, flags = planes[0].bytesused, b.flags
            t = time.time()
            if flags & V.V4L2_BUF_FLAG_LAST:
                self.log("capture LAST（bytesused=%d flags=%#x seq=%d）" % (used, flags, b.sequence))
                self.d.qbuf(V.BUF_CAPTURE_MPLANE, b.index, None, V.MEMORY_MMAP,
                            self.d.cap_planes)
                self.handle_drc("capture LAST flags=%#x" % flags)
                continue
            if used == 0:
                self.log("capture 空帧（flags=%#x seq=%d）→ 归还" % (flags, b.sequence))
                self.d.qbuf(V.BUF_CAPTURE_MPLANE, b.index, None, V.MEMORY_MMAP,
                            self.d.cap_planes)
                continue
            self.frames.append((t, b.index, b.sequence, used, flags))
            got.append(self.frames[-1])
            if len(self.frames) <= verbose_frames:
                print("    [dec] 帧 #%d idx=%d seq=%d %d 字节 flags=%#x" %
                      (len(self.frames), b.index, b.sequence, used, flags), flush=True)
            self.d.qbuf(V.BUF_CAPTURE_MPLANE, b.index, None, V.MEMORY_MMAP, self.d.cap_planes)
        return got

    def events(self):
        out = []
        for _ in range(4):
            ev = self.d.poll_event(0)
            if ev is None:
                break
            out.append(ev)
            self.log("事件 type=%d changes=%#x" %
                     (ev.type, ev.src_change if ev.type == V.EVENT_SOURCE_CHANGE else 0))
            if ev.type == V.EVENT_SOURCE_CHANGE and ev.src_change != 0:
                print("    [dec] SOURCE_CHANGE changes=%#x → 走 DRC 重建" % ev.src_change,
                      flush=True)
                self.handle_drc("source change")
        return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stream")
    ap.add_argument("--dev", default="/dev/video0")
    ap.add_argument("--pace", type=float, default=0.033)
    ap.add_argument("--aus", type=int, default=200)
    ap.add_argument("--w", type=int, default=1280)
    ap.add_argument("--h", type=int, default=720)
    ap.add_argument("--n-out", type=int, default=4)
    ap.add_argument("--n-cap", type=int, default=4)
    ap.add_argument("--feed-ahead", type=int, default=4)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    data = open(a.stream, "rb").read()
    aus = split_aus(data)
    n = min(a.aus, len(aus))
    print("[v4l2dec] %s: %d AU（本次喂 %d，节奏 %.0f ms）" % (a.stream, len(aus), n, a.pace * 1000))

    dec = HandDec(a.dev, a.n_out, a.n_cap, verbose=a.verbose)
    dec.open(a.w, a.h)

    t0 = time.time()
    t_feed = []
    started = False
    stop = False
    i = 0
    while i < n or not stop:
        # 喂：保持不超过 feed_ahead 个在路上
        if i < n and len(dec.pending_out) < a.feed_ahead:
            try:
                if dec.feed(aus[i]):
                    t_feed.append(time.time())
                    i += 1
                    if not started:
                        dec.start_capture()
                        started = True
            except V.V4L2Error as e:
                print("[v4l2dec] QBUF 失败: %s" % e)
                break
        else:
            time.sleep(0.0005)

        dec.reclaim_out()
        dec.pump_capture()
        dec.events()

        if i >= n and not dec.pending_out:
            break
        if time.time() - t0 > 30:
            print("[v4l2dec] 30s 超时：喂 %d，收 %d" % (i, len(dec.frames)))
            break
        if a.pace and i < n:
            # 按节奏喂：等下一个 AU 的时间点
            target = t0 + (i) * a.pace
            now = time.time()
            if now < target:
                time.sleep(min(target - now, a.pace))

    dec.reclaim_out()
    for _ in range(20):
        if not dec.pump_capture():
            break
        time.sleep(0.01)

    lats, idx = [], 0
    for (t, bi, seq, used, flags) in dec.frames:
        if idx < len(t_feed):
            lats.append((t - t_feed[idx]) * 1000)
        idx += 1
    print("[v4l2dec] 喂 %d AU / 出 %d 帧 / 用时 %.0f ms" %
          (i, len(dec.frames), (time.time() - t0) * 1000))
    if lats:
        print("[v4l2dec] 稳态单帧延迟：平均 %.1f ms，最大 %.1f ms，最小 %.1f ms" %
              (sum(lats) / len(lats), max(lats), min(lats)))
    dec.d.close()
    return 0 if dec.frames else 1


if __name__ == "__main__":
    sys.exit(main())
