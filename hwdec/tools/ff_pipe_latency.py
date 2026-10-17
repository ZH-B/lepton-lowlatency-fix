#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ff_pipe_latency.py —— 直接量测「ffmpeg 解码管线」自身的稳态延迟（不含 socket/客户机）。

做法：一个发送线程按 pace 把 AU 写进 ffmpeg stdin；另一个线程连续读 stdout 整帧；
      frame_i 到达时刻 − AU_i 送入时刻（FIFO 配对）= 该 AU 的解码管线延迟。
      首帧延迟另记（含解码器/固件冷启动）。

用法：python3 ff_pipe_latency.py <h264> [--pace 0.033] [--preset 名字] [--ffextra "..."]
"""
import argparse
import subprocess
import sys
import threading
import time

FRAME_W, FRAME_H = 1280, 720
FRAME = FRAME_W * FRAME_H * 3 // 2

PRESETS = {
    "base":            [],
    "buf2":            ["-num_output_buffers", "2", "-num_capture_buffers", "2"],
    "buf4_8":          ["-num_output_buffers", "4", "-num_capture_buffers", "8"],
    "buf1_4":          ["-num_output_buffers", "1", "-num_capture_buffers", "4"],
    "threads1":        ["-threads", "1"],
    "lowdelay_only":   [],
}


def split_aus(d):
    pos, i, n = [], 0, len(d)
    while i + 4 < n:
        if d[i:i + 4] == b"\x00\x00\x00\x01":
            sc, p = 4, i
        elif d[i:i + 3] == b"\x00\x00\x01":
            sc, p = 3, i
        else:
            i += 1
            continue
        if p + sc < n and (d[p + sc] & 0x1f) == 9:
            pos.append(p)
        i = p + sc + 1
    return pos


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stream")
    ap.add_argument("--pace", type=float, default=0.033)
    ap.add_argument("--preset", default="base")
    ap.add_argument("--ffextra", default="")
    ap.add_argument("--aus", type=int, default=90)
    ap.add_argument("--dec", default="h264_v4l2m2m", help="h264_v4l2m2m=硬解，h264=软解")
    ap.add_argument("--w", type=int, default=1280)
    ap.add_argument("--h", type=int, default=720)
    a = ap.parse_args()

    global FRAME
    FRAME = a.w * a.h * 3 // 2
    data = open(a.stream, "rb").read()
    aus = split_aus(data)
    total = len(aus)
    n = min(a.aus, total)

    extra = list(PRESETS.get(a.preset, []))
    if a.ffextra:
        extra += a.ffextra.split()
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"] + extra + [
        "-probesize", "32", "-analyzeduration", "0", "-flags", "low_delay",
        "-c:v", a.dec, "-f", "h264", "-i", "pipe:0",
        "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "nv12", "-"]
    print("[pipe:%s/%s] %s" % (a.preset, a.dec, " ".join(extra) or "(默认)"))
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, bufsize=0)

    t_send = []
    t0 = time.time()
    lats = []
    first = [None]
    stop = threading.Event()
    lock = threading.Lock()

    def sender():
        for i in range(n):
            start = aus[i]
            end = aus[i + 1] if i + 1 < total else len(data)
            try:
                p.stdin.write(data[start:end])
                p.stdin.flush()
            except (OSError, ValueError):
                break
            t_send.append(time.time())
            time.sleep(a.pace)
        try:
            p.stdin.close()
        except (OSError, ValueError):
            pass

    def reader():
        buf = bytearray()
        fd = p.stdout.fileno()
        while not stop.is_set():
            try:
                chunk = p.stdout.read(1 << 20)
            except (OSError, ValueError):
                break
            if not chunk:
                break
            buf += chunk
            while len(buf) >= FRAME:
                t = time.time()
                buf = buf[FRAME:]
                with lock:
                    idx = len(lats)
                    if idx < len(t_send):
                        lats.append((t - t_send[idx]) * 1000)
                    else:
                        lats.append(None)
                    fed_now = len(t_send)
                    firstframe = first[0] is None
                    if firstframe:
                        first[0] = (t - t0) * 1000
                if firstframe:
                    print("[pipe:%s] 首帧 %.0f ms（已喂 %d AU）" %
                          (a.preset, first[0], fed_now), flush=True)

    ts = threading.Thread(target=sender, daemon=True)
    tr = threading.Thread(target=reader, daemon=True)
    tr.start()
    ts.start()
    ts.join(timeout=n * a.pace + 15)
    time.sleep(1.0)
    stop.set()

    p.kill()
    vals = [v for v in lats if v is not None]
    print("[pipe:%s] 喂 %d AU / 出 %d 帧；稳态单帧延迟：平均 %.1f ms，最大 %.1f ms，最小 %.1f ms" %
          (a.preset, len(t_send), len(lats), sum(vals) / len(vals) if vals else -1,
           max(vals) if vals else -1, min(vals) if vals else -1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
