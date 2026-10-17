#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ff_latency_probe.py —— 量测宿主 ffmpeg 解码管线的「首帧需要多少 AU」。

逐 AU 喂入，每喂一个就非阻塞看 stdout 有没有整帧出来，记录出第一帧时已喂了多少 AU
（≈ 解码器内部缓冲深度）以及墙钟时间。解码参数必须放在 -i 之前（放后面就变成输出编码器）。

用法：python3 ff_latency_probe.py <h264> [模式名]
模式：base / threads1 / lowdelay / ld_t1 / ld_t1_noprobe
"""
import os
import select
import subprocess
import sys
import threading
import time

FRAME = 1280 * 720 * 3 // 2

MODES = {
    "base":        ["-c:v", "h264_v4l2m2m"],
    "threads1":    ["-c:v", "h264_v4l2m2m", "-threads", "1"],
    "lowdelay":    ["-c:v", "h264_v4l2m2m", "-flags", "low_delay"],
    "ld_t1":       ["-c:v", "h264_v4l2m2m", "-flags", "low_delay", "-threads", "1"],
    "sw":          ["-c:v", "h264"],
    "sw_nb":       ["-c:v", "h264", "-fflags", "nobuffer"],
    "sw_pa":       ["-c:v", "h264", "-probesize", "32", "-analyzeduration", "0"],
    "sw_nb_pa":    ["-c:v", "h264", "-fflags", "nobuffer", "-probesize", "32",
                    "-analyzeduration", "0"],
    "hw_pa_ld":    ["-c:v", "h264_v4l2m2m", "-probesize", "32", "-analyzeduration", "0",
                    "-flags", "low_delay"],
    "hw_pa_ld_t1": ["-c:v", "h264_v4l2m2m", "-probesize", "32", "-analyzeduration", "0",
                    "-flags", "low_delay", "-threads", "1"],
    "sw_direct":   ["-c:v", "h264", "-avioflags", "direct"],
    "sw_pa_direct": ["-c:v", "h264", "-probesize", "32", "-analyzeduration", "0",
                     "-avioflags", "direct"],
    "sw_pa_ld":    ["-c:v", "h264", "-probesize", "32", "-analyzeduration", "0",
                    "-flags", "low_delay"],
    "sw_pa_ld_direct": ["-c:v", "h264", "-probesize", "32", "-analyzeduration", "0",
                        "-flags", "low_delay", "-avioflags", "direct"],
    "sw_ld_nb_pa": ["-c:v", "h264", "-flags", "low_delay", "-fflags", "nobuffer",
                    "-probesize", "32", "-analyzeduration", "0"],
    "ld_t1_np":    ["-c:v", "h264_v4l2m2m", "-flags", "low_delay", "-threads", "1",
                    "-analyzeduration", "0", "-probesize", "32"],
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
    stream = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "base"
    pace = float(sys.argv[3]) if len(sys.argv) > 3 else 0.005   # 每个 AU 之间的间隔（秒）
    data = open(stream, "rb").read()
    aus = split_aus(data)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"] + MODES[mode] + \
          ["-f", "h264", "-i", "pipe:0", "-fps_mode", "passthrough",
           "-f", "rawvideo", "-pix_fmt", "nv12", "-"]
    print("[probe:%s] %s" % (mode, " ".join(cmd)))

    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, bufsize=0)

    got = []
    frames = [0]
    first = [None]
    t0 = time.time()
    stop = threading.Event()

    def drain():
        buf = bytearray()
        fd = p.stdout.fileno()
        while True:
            try:
                chunk = os.read(fd, 1 << 20)   # 阻塞读，有多少拿多少，不做轮询
            except (OSError, ValueError):
                break
            if not chunk:
                break
            buf += chunk
            while len(buf) >= FRAME:
                buf = buf[FRAME:]
                frames[0] += 1
                if first[0] is None:
                    first[0] = (fed_now[0], (time.time() - t0) * 1000)
                    print("[probe:%s] 第 1 帧：已喂 %d AU，用时 %.0f ms" %
                          (mode, fed_now[0], first[0][1]), flush=True)

    fed_now = [0]
    th = threading.Thread(target=drain, daemon=True)
    th.start()
    for i, start in enumerate(aus):
        end = aus[i + 1] if i + 1 < len(aus) else len(data)
        try:
            p.stdin.write(data[start:end])
            p.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            print("[probe:%s] 写入 ffmpeg 失败（%s）：%s" %
                  (mode, e, p.stderr.read(400).decode("utf-8", "replace").strip()))
            stop.set()
            return 1
        fed_now[0] = i + 1
        if first[0] is not None:
            break
        time.sleep(pace)
    if first[0] is None:
        print("[probe:%s] 喂完 %d AU 仍无整帧（解码器要 EOF 才吐帧）" % (mode, fed_now[0]),
              flush=True)
    stop.set()
    p.kill()
    return 0


if __name__ == "__main__":
    sys.exit(main())
