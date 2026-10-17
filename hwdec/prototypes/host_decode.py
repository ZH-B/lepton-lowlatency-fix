#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""host_decode.py —— 宿主侧硬解守护（原型）

严格照搬 ffmpeg libavcodec/v4l2_m2m 的解码时序（实测它能驱动 qcom-iris）：
    init:      S_FMT output(H264) + REQBUFS output；capture 只 S_FMT 不申请缓冲
    每包循环:  QBUF output（一个 AU）→ STREAMON output（首次）
              → G_FMT capture（此刻驱动才给出真实格式）
              → REQBUFS capture + EXPBUF 出 dmabuf + QBUF 全部 + STREAMON capture
              → DQBUF capture 收帧
    收尾:      把"最新一帧"所在的 capture 缓冲留着不归还，其 dmabuf fd 通过
              SCM_RIGHTS 发给客户机。

用法：python3 host_decode.py <stream.h264> [--w 1280] [--h 720] [--sock ...]
"""
import argparse
import os
import socket
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import v4l2m2m as V  # noqa: E402

MAGIC = b"HWDB"


def split_aus(data):
    """按 NAL 起始码切片，优先用 AUD(type 9) 切 access unit。"""
    starts, i, n = [], 0, len(data)
    while i < n - 4:
        if data[i:i + 4] == b"\x00\x00\x00\x01":
            sc, pos = 4, i
        elif data[i:i + 3] == b"\x00\x00\x01":
            sc, pos = 3, i
        else:
            i += 1
            continue
        if pos + sc < n:
            starts.append((pos, data[pos + sc] & 0x1F))
        i = pos + sc + 1
    auds = [p for p, t in starts if t == 9]
    if len(auds) > 1:
        return [data[auds[k]:(auds[k + 1] if k + 1 < len(auds) else n)]
                for k in range(len(auds))]
    return [data]


def decode(dec, aus, feed_ahead=4, deadline=40.0):
    """ffmpeg 顺序：先喂包并开 output 流，再建 capture 队列。返回 (held, frames)。"""
    dec.open_output()                       # S_FMT output + REQBUFS output + mmap
    n_out = len(dec.out)
    # ffmpeg 也会先给 capture 设一个格式（但不申请缓冲）
    dec.set_fmt(V.BUF_CAPTURE_MPLANE, V.PIX_NV12, dec.width, dec.height, 1)
    dec.subscribe(V.EVENT_SOURCE_CHANGE)
    dec.subscribe(V.EVENT_EOS)

    out_free = list(range(n_out))
    pending_out = set()
    to_feed = list(aus)
    frames, held = [], None
    cap_ready = False
    out_streaming = False
    queued_any = False
    drc_pending = False
    drained = False
    saw_last = False
    fed = 0
    t0 = time.time()

    while time.time() - t0 < deadline:
        progressed = False

        # 1) 喂包：capture 未就绪时只喂 1 个（ffmpeg 也是这样，喂完就建 capture）
        limit = feed_ahead if cap_ready else 1
        while to_feed and out_free and len(pending_out) < limit:
            idx = out_free.pop(0)
            au = to_feed.pop(0)
            mm = dec.out[idx]["mmap"]
            mm.seek(0)
            mm.write(au)
            dec.qbuf(V.BUF_OUTPUT_MPLANE, idx, len(au), V.MEMORY_MMAP, dec.out_planes)
            pending_out.add(idx)
            queued_any = True
            fed += 1
            progressed = True

        # 2) 第一个包入队后：STREAMON output → 建 capture（G_FMT → REQBUFS → EXPBUF → QBUF → STREAMON）
        if queued_any and not cap_ready:
            if not out_streaming:
                dec.stream(True, V.BUF_OUTPUT_MPLANE)
                out_streaming = True
                print("  [ffmpeg 序] STREAMON output")
            print("  [ffmpeg 序] G_FMT capture（驱动此刻给出真实格式）")
            dec.open_capture(start=True)    # G_FMT + REQBUFS + EXPBUF + QBUF + STREAMON
            cap_ready = True
            progressed = True

        # 3) 回收 output 缓冲
        for _ in range(n_out):
            try:
                b, _p = dec.dqbuf(V.BUF_OUTPUT_MPLANE, dec.out_planes)
            except V.V4L2Error:
                break
            pending_out.discard(b.index)
            out_free.append(b.index)
            progressed = True

        # 4) 收帧
        if cap_ready:
            for _ in range(len(dec.cap)):
                try:
                    b, planes = dec.dqbuf(V.BUF_CAPTURE_MPLANE, dec.cap_planes)
                except V.V4L2Error:
                    break
                progressed = True
                used, flags = planes[0].bytesused, b.flags
                if flags & V.V4L2_BUF_FLAG_LAST:
                    saw_last = True
                    print("  capture LAST 帧 (bytesused=%d flags=%#x)" % (used, flags))
                    if drc_pending:
                        print("  → DRC：重建 capture 队列")
                        dec.reopen_capture()
                        try:
                            dec.decoder_cmd(V.DEC_CMD_START)
                            print("    DECODER_CMD_START 成功")
                        except V.V4L2Error as e:
                            print("    DECODER_CMD_START 被拒（首次 IPSC 属正常）: %s" % e)
                        drc_pending = False
                    continue          # LAST 帧属于旧缓冲集，不归还
                if used > 0 and not (flags & V.V4L2_BUF_FLAG_ERROR):
                    meta = (b.index, b.sequence, used, flags)
                    frames.append(meta)
                    if held is not None:
                        dec.qbuf(V.BUF_CAPTURE_MPLANE, held[0], None, V.MEMORY_MMAP,
                                 dec.cap_planes)
                    held = meta
                else:
                    dec.qbuf(V.BUF_CAPTURE_MPLANE, b.index, None, V.MEMORY_MMAP,
                             dec.cap_planes)

        # 5) 事件
        if cap_ready and not to_feed and not pending_out and not drained:
            try:
                dec.decoder_cmd(V.DEC_CMD_STOP)
                print("  [时序] 已发 DECODER_CMD_STOP（drain）")
            except V.V4L2Error as e:
                print("  DECODER_CMD_STOP 被拒: %s" % e)
            drained = True
            progressed = True

        ev = dec.poll_event(0.02)
        if ev is not None:
            if ev.type == V.EVENT_SOURCE_CHANGE:
                print("  事件 SOURCE_CHANGE changes=%#x（已解 %d 帧，已喂 %d）" %
                      (ev.src_change, len(frames), fed))
                # 规范要求：分辨率变化时停 capture、重建队列再继续
                drc_pending = True
                progressed = True
            elif ev.type == V.EVENT_EOS:
                print("  事件 EOS（已解 %d 帧，已喂 %d，to_feed=%d pending=%d）" %
                      (len(frames), fed, len(to_feed), len(pending_out)))
                progressed = True

        if fed and fed % 20 == 0 and fed != getattr(dec, '_lastlog', -1):
            dec._lastlog = fed
            print("  ... 已喂 %d，pending=%d，出帧 %d" % (fed, len(pending_out), len(frames)))

        if not progressed:
            time.sleep(0.002)

        # 全部喂完、输入队列空、且已拿到帧 → 提前收工
        if held is not None and drained and not to_feed and not pending_out:
            if saw_last or time.time() - t0 > 2.0:
                break

    if out_streaming:
        dec.stream(False, V.BUF_OUTPUT_MPLANE)
    if cap_ready:
        dec.stream(False, V.BUF_CAPTURE_MPLANE)
    return held, frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stream")
    ap.add_argument("--dev", default="/dev/video0")
    ap.add_argument("--w", type=int, default=1280)
    ap.add_argument("--h", type=int, default=720)
    ap.add_argument("--sock", default="/var/home/armada/Downloads/hwb.sock")
    a = ap.parse_args()

    ok, sizes = V.check_abi()
    print("ABI 自检:", "ok" if ok else sizes)

    if os.path.exists(a.sock):
        os.unlink(a.sock)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(a.sock)
    os.chmod(a.sock, 0o777)
    srv.listen(2)
    srv.settimeout(120)
    print("[host] 监听 %s，等客户机连接…" % a.sock, flush=True)
    try:
        conn, _ = srv.accept()
    except socket.timeout:
        print("[host] 等待超时")
        return 1
    print("[host] 客户机已连接:", conn.recv(64))

    data = open(a.stream, "rb").read()
    aus = split_aus(data)
    print("[host] 码流 %d 字节 → %d 个 access unit" % (len(data), len(aus)))

    dec = V.Decoder(a.dev, V.PIX_H264, a.w, a.h)
    print("[host] %s: %s" % (a.dev, dec.querycap()[:2]))
    try:
        t0 = time.time()
        held, frames = decode(dec, aus)
        print("[host] 解出 %d 帧，耗时 %.2fs" % (len(frames), time.time() - t0))
        if held is None:
            print("[host] 没有可用帧")
            return 1
        print("[host] 保留帧: buf#%d seq=%s bytesused=%s flags=%#x" %
              (held[0], held[1], held[2], held[3]))
        buf = dec.cap[held[0]]
        hdr = struct.pack("<4sII4sIIII", MAGIC, 1, 0, b"NV12", dec.cap_width,
                          dec.cap_height, dec.cap_stride, buf["len"])
        conn.sendmsg([hdr], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", buf["fd"]))])
        print("[host] 已发送 dmabuf fd=%d（%dx%d stride=%d size=%d）" %
              (buf["fd"], dec.cap_width, dec.cap_height, dec.cap_stride, buf["len"]))
        time.sleep(0.5)
    finally:
        dec.close()
        conn.close()
        srv.close()
        if os.path.exists(a.sock):
            os.unlink(a.sock)
    print("[host] 完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
