#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hwdec_v4l2.py —— 手写 V4L2 有状态解码引擎（不依赖 ffmpeg）。

为什么不用 ffmpeg：实测 `ffmpeg -c:v h264_v4l2m2m` 的管线固定多 1~2 帧 ——
raw h264 demuxer 的 parser 必须等到**下一帧**的起始码才肯吐当前帧（1 帧），
rawvideo muxer + AVIO 再叠一层。本引擎按 AUD 自己切好完整 access unit 直接 QBUF，
DQBUF 到帧立刻交给调用者：720p30 实测稳态单帧延迟 **33.7 ms（= 1 帧）**，
ffmpeg 同条件是 102 ms。

驱动要点（qcom-iris，都是实测踩出来的）：
  * ioctl 顺序必须照 ffmpeg：S_FMT(OUTPUT) → REQBUFS(OUTPUT) → 写第 1 个 AU + QBUF
    → STREAMON(OUTPUT) → G_FMT(CAPTURE) → S_FMT(CAPTURE) → REQBUFS(CAPTURE)
    → QBUF 全部 → STREAMON(CAPTURE)；
  * 第一帧之后驱动会回一个 **bytesused=0 且带 LAST**（常常还带 ERROR）的 capture 缓冲，
    这是**动态分辨率变化（DRC）**：必须 STREAMOFF(capture) → REQBUFS(capture,0)
    → 重新 G_FMT/S_FMT/REQBUFS/QBUF/STREAMON，然后继续喂 —— 不做这一步就**一帧都出不来**
    （之前的原型就是卡在这里）；
  * 之后再发 `V4L2_DEC_CMD_START` 会返回 EBUSY，但**不影响出帧**（有新输入驱动就继续解）；
  * capture 的 geometry 是驱动给的：1280x720 码流得到 **1280x736**（高度按 16 对齐），
    stride=1280，所以对外发帧时要按「可见 w×h」重新拼（Y 和 UV 分两次拷）。
"""
import mmap
import os
import select
import threading
import time

import v4l2m2m as V


FRAME_FLAG_REPEAT = 2


def split_aus(d):
    """按 AUD(type 9) 切 access unit；没有 AUD 就整段一包。"""
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


class V4l2Decoder(object):
    """有状态解码器：喂完整 AU，取 NV12 帧（按可见 w×h 紧排）。"""

    def __init__(self, dev="/dev/video0", n_out=4, n_cap=4, verbose=False, codec=1):
        self.codec = V.PIX_HEVC if codec == 2 else V.PIX_H264
        self.d = V.Decoder(dev=dev, codec=self.codec, n_output=n_out, n_capture=n_cap)
        self.n_out, self.n_cap = n_out, n_cap
        self.verbose = verbose
        self.free_out = []
        self.pending_out = set()
        self.cap_ready = False
        self.out_streaming = False
        self.drc_count = 0
        # 驱动给的 capture geometry（DRC 后更新）
        self.cap_w = self.cap_h = self.cap_stride = 0
        self.aus_in = 0
        self.frames_out = 0
        self.err_frames = 0        # 驱动标记 ERROR 的解码失败帧（内容未定义，绝不能发出去）
        self.blank_frames = 0      # 疑似空帧（Y 平面几乎全 0）
        self.closed = False
        # 引擎里的 ioctl 有状态：喂数据的是「转发线程」，收帧/DRC 重建是「取帧线程」，
        # 两边同时动 capture 队列会互相踩（实测 4K 时 QBUF capture 报 EINVAL）。
        self.lock = threading.RLock()

    def log(self, msg):
        if self.verbose:
            print("    [v4l2] %s" % msg, flush=True)

    # ---------- 启动 ----------
    def open(self, w, h):
        self.d.open_output(self.codec, w, h, n=self.n_out)
        self.d.subscribe(V.EVENT_SOURCE_CHANGE)
        self.d.subscribe(V.EVENT_EOS)
        self.free_out = list(range(len(self.d.out)))
        self.want_w, self.want_h = w, h
        self.log("output 就绪：%d 缓冲，空闲 %s" % (len(self.d.out), self.free_out))

    def _start_capture(self):
        """建 capture 队列。
        顺序很重要：**先建 capture 队列并 STREAMON，最后才 STREAMON(output)**。
        反过来（先开 output 再 REQBUFS capture）在 1080p 上会让驱动抢先处理第一帧、
        进入 source change，于是 REQBUFS 直接 EBUSY（实测，720p 恰好侥幸能过）。
        """
        try:
            self.d.open_capture(start=False, n=self.n_cap)
        except V.V4L2Error as e:
            # 驱动偶尔在刚 STREAMON 时返回 EBUSY，重试几次
            for _ in range(5):
                time.sleep(0.02)
                try:
                    self.d.open_capture(start=False, n=self.n_cap)
                    e = None
                    break
                except V.V4L2Error as e2:
                    e = e2
            if e is not None:
                raise e
        self._map_capture()
        info = self.d.g_fmt(V.BUF_CAPTURE_MPLANE)
        self.cap_w, self.cap_h = info["width"], info["height"]
        self.cap_stride = info["stride"][0] or info["width"]
        self.d.stream(True, V.BUF_CAPTURE_MPLANE)
        if not self.out_streaming:
            self.d.stream(True, V.BUF_OUTPUT_MPLANE)
            self.out_streaming = True
            self.log("STREAMON(output)（capture 已就绪）")
        self.cap_ready = True

    def _map_capture(self):
        """把 capture 缓冲 mmap 进本进程（open_capture 只记了 offset/dmabuf fd）。"""
        for i, info in self.d.cap.items():
            if info.get("mmap") is None:
                info["mmap"] = mmap.mmap(self.d.fd, info["len"], mmap.MAP_SHARED,
                                         mmap.PROT_READ | mmap.PROT_WRITE,
                                         offset=info["offset"], trackfd=False)

    def _release_capture(self):
        """释放 capture 队列：必须先 munmap，否则 vb2 的 REQBUFS(0) 返回 EBUSY。"""
        try:
            self.d.stream(False, V.BUF_CAPTURE_MPLANE)
        except V.V4L2Error:
            pass
        for d in self.d.cap.values():
            mm = d.get("mmap")
            if mm is not None:
                try:
                    mm.close()
                except Exception:
                    pass
                d["mmap"] = None
            if d.get("fd") is not None:
                try:
                    os.close(d["fd"])
                except OSError:
                    pass
                d["fd"] = None
        try:
            self.d.reqbufs(V.BUF_CAPTURE_MPLANE, 0, V.MEMORY_MMAP)
        except V.V4L2Error as e:
            self.log("REQBUFS(capture,0) 失败: %s" % e)
        self.d.cap = {}
        self.cap_ready = False

    def _handle_drc(self, why):
        self.drc_count += 1
        self.log("DRC #%d（%s）：重建 capture 队列" % (self.drc_count, why))
        # 顺序必须和之前实测能出帧的一致：STREAMOFF → REQBUFS(0)（驱动态会拒绝，忽略）
        # → 关掉旧 dmabuf fd → 重建（REQBUFS(n) 会复用已有队列）。加 munmap 反而会让
        # 驱动在 REQBUFS 上一直 EBUSY，所以这里不 munmap（旧 mmap 交给 GC）。
        try:
            self.d.stream(False, V.BUF_CAPTURE_MPLANE)
        except V.V4L2Error:
            pass
        try:
            self.d.reqbufs(V.BUF_CAPTURE_MPLANE, 0, V.MEMORY_MMAP)
        except V.V4L2Error as e:
            self.log("REQBUFS(capture,0) 被拒（正常，继续）: %s" % e)
        for d in self.d.cap.values():
            if d.get("fd") is not None:
                try:
                    os.close(d["fd"])
                except OSError:
                    pass
                d["fd"] = None
        self.d.cap = {}
        self._start_capture()
        try:
            self.d.decoder_cmd(V.DEC_CMD_START)
            self.log("DECODER_CMD_START 成功")
        except V.V4L2Error as e:
            # 首次 IPSC 时 EBUSY 是正常的，驱动靠新输入继续
            self.log("DECODER_CMD_START 被拒（%s），继续喂即可" % e)

    # ---------- 数据面 ----------
    def feed(self, au):
        """把完整 AU 写进空闲 output 缓冲并 QBUF。没有空闲缓冲返回 False。"""
        with self.lock:
            return self._feed_locked(au)

    def _feed_locked(self, au):
        if self.closed or not self.free_out:
            return False
        idx = self.free_out.pop(0)
        mm = self.d.out[idx]["mmap"]
        mm.seek(0)
        mm.write(au)
        try:
            self.d.qbuf(V.BUF_OUTPUT_MPLANE, idx, len(au), V.MEMORY_MMAP, self.d.out_planes)
        except V.V4L2Error:
            self.free_out.insert(0, idx)
            raise
        self.pending_out.add(idx)
        self.aus_in += 1
        if not self.cap_ready:
            self._start_capture()
        return True

    def _reclaim_out(self):
        for _ in range(self.n_out):
            try:
                b, _p = self.d.dqbuf(V.BUF_OUTPUT_MPLANE, self.d.out_planes)
            except V.V4L2Error:
                break
            self.pending_out.discard(b.index)
            self.free_out.append(b.index)

    def _pack(self, mm, idx):
        """把驱动缓冲里的 NV12 按可见 w×h 紧排成 bytes（Y 与 UV 分两次拷）。"""
        w, h = self.want_w, self.want_h
        stride = self.cap_stride
        off_uv = stride * self.cap_h
        mm.seek(0)
        y = mm.read(stride * h)
        mm.seek(off_uv)
        uv = mm.read(stride * (h // 2))
        if stride == w:
            return y[:w * h] + uv[:w * (h // 2)]
        # stride 比宽度大：逐行去掉填充
        out = bytearray(w * h + w * (h // 2))
        for r in range(h):
            out[r * w:(r + 1) * w] = y[r * stride:r * stride + w]
        base = w * h
        for r in range(h // 2):
            src = r * stride
            out[base + r * w:base + (r + 1) * w] = uv[src:src + w]
        return bytes(out)

    def poll(self, timeout=0.05):
        """等可读 → 收帧。select 在锁外等（否则会把喂数据那一侧堵住 50ms）。"""
        if not self.cap_ready:
            with self.lock:
                self._reclaim_out()
            return []
        try:
            r, _, _ = select.select([self.d.fd], [], [], timeout)
        except (OSError, ValueError):
            return []
        if not r:
            with self.lock:
                self._reclaim_out()
            return []
        with self.lock:
            return self._poll_locked(0)

    def _poll_locked(self, timeout):
        out = []
        if self.closed:
            return out
        self._reclaim_out()
        if not self.cap_ready:
            return out
        for _ in range(self.n_cap):
            try:
                b, planes = self.d.dqbuf(V.BUF_CAPTURE_MPLANE, self.d.cap_planes)
            except V.V4L2Error:
                break
            used, flags = planes[0].bytesused, b.flags
            if flags & V.V4L2_BUF_FLAG_LAST:
                self.log("capture LAST（bytesused=%d flags=%#x）" % (used, flags))
                self.d.qbuf(V.BUF_CAPTURE_MPLANE, b.index, None, V.MEMORY_MMAP,
                            self.d.cap_planes)
                self._handle_drc("capture LAST")
                continue
            if flags & V.V4L2_BUF_FLAG_ERROR:
                # 解码失败帧：内容未定义（渲染就是绿屏）。不丢弃，而是告诉客户机
                # 「重复上一帧」——上层看到的是轻微冻结，而不是绿屏。
                self.err_frames += 1
                if self.err_frames <= 5 or self.err_frames % 100 == 0:
                    print("    [v4l2] 解码失败帧 #%d（flags=%#x used=%d）→ 让客户机重复上一帧" %
                          (self.err_frames, flags, used), flush=True)
                self.d.qbuf(V.BUF_CAPTURE_MPLANE, b.index, None, V.MEMORY_MMAP,
                            self.d.cap_planes)
                out.append((b"", FRAME_FLAG_REPEAT))
                continue
            if used == 0:
                self.d.qbuf(V.BUF_CAPTURE_MPLANE, b.index, None, V.MEMORY_MMAP,
                            self.d.cap_planes)
                continue
            info = self.d.cap.get(b.index)
            if info is None or info.get("mmap") is None:
                self.d.qbuf(V.BUF_CAPTURE_MPLANE, b.index, None, V.MEMORY_MMAP,
                            self.d.cap_planes)
                continue
            frame = self._pack(info["mmap"], b.index)
            # 空帧哨兵：Y 平面全 0 的帧渲染出来就是绿屏 → 同样让客户机重复上一帧
            step = max(1, len(frame) // 4096)
            if not any(frame[0:min(len(frame), 921600):step]):
                self.blank_frames += 1
                if self.blank_frames <= 5 or self.blank_frames % 100 == 0:
                    print("    [v4l2] 空帧 #%d（%d 字节）→ 让客户机重复上一帧" %
                          (self.blank_frames, len(frame)), flush=True)
                self.d.qbuf(V.BUF_CAPTURE_MPLANE, b.index, None, V.MEMORY_MMAP,
                            self.d.cap_planes)
                out.append((b"", FRAME_FLAG_REPEAT))
                continue
            out.append((frame, 0))
            self.frames_out += 1
            self.d.qbuf(V.BUF_CAPTURE_MPLANE, b.index, None, V.MEMORY_MMAP,
                        self.d.cap_planes)
        return out

    def events(self):
        with self.lock:
            return self._events_locked()

    def _events_locked(self):
        if self.closed:
            return
        for _ in range(4):
            ev = self.d.poll_event(0)
            if ev is None:
                break
            if ev.type == V.EVENT_SOURCE_CHANGE and ev.src_change != 0:
                self.log("SOURCE_CHANGE changes=%#x" % ev.src_change)
                self._handle_drc("source change")

    def end_input(self, drain_timeout=1.0):
        """EOS：让驱动把剩下的帧吐出来（DRAIN 不一定被接受，尽力而为）。"""
        with self.lock:
            return self._end_input_locked(drain_timeout)

    def _end_input_locked(self, drain_timeout):
        deadline = time.time() + drain_timeout
        try:
            self.d.decoder_cmd(V.DEC_CMD_STOP)
            self.log("DECODER_CMD_STOP（drain）已发")
        except V.V4L2Error as e:
            self.log("DECODER_CMD_STOP 被拒: %s" % e)
        frames = []
        while time.time() < deadline:
            got = self.poll(0.05)
            frames.extend(got)
            if not got and not self.pending_out:
                break
        return frames

    def flush(self):
        """客户端 Flush：丢掉在路上的数据。"""
        with self.lock:
            return self._flush_locked()

    def _flush_locked(self):
        """客户端 Flush。
        踩过的坑：不要拆 V4L2 队列重建 —— STREAMOFF+REQBUFS(0)+重建之后，qcom-iris
        会一直在 SOURCE_CHANGE/LAST 之间重建，再也不出帧（实测）。
        硬件解码器自己能处理「跳帧/换 GOP」，下一个 IDR 就会恢复，所以这里只记一笔，
        客户端侧的排队数据由桥（V4l2Session.flush_now）清掉。
        """
        self.flush_count = getattr(self, "flush_count", 0) + 1
        self.log("flush #%d（保持 V4L2 队列不动，等下一个 IDR）" % self.flush_count)

    def _flush_locked_old(self):
        try:
            self.d.stream(False, V.BUF_OUTPUT_MPLANE)
        except V.V4L2Error:
            pass
        self.out_streaming = False

    def close(self):
        self.closed = True
        try:
            self._release_capture()
        except Exception:
            pass
        try:
            self.d.stream(False, V.BUF_OUTPUT_MPLANE)
        except Exception:
            pass
        for d in self.d.out.values():          # 输出缓冲的 mmap 也要收
            mm = d.get("mmap")
            if mm is not None:
                try:
                    mm.close()
                except Exception:
                    pass
                d["mmap"] = None
        self.d.close()

    def summary(self):
        return ("喂入 %d AU / 出 %d 帧 / DRC %d 次 / 跳过坏帧 %d（错误 %d + 空帧 %d）" %
                (self.aus_in, self.frames_out, self.drc_count, self.err_frames + self.blank_frames,
                 self.err_frames, self.blank_frames))
