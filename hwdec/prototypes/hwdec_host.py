#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hwdec_host.py —— 宿主侧硬件解码桥（v1）

路径：ffmpeg(-c:v h264_v4l2m2m) → /dev/video0 (qcom-iris) 硬解
      → NV12 帧写入 memfd 环形缓冲 → 通过 SCM_RIGHTS 把 memfd 交给客户机 mmap

为什么用 ffmpeg 而不是自己走 V4L2：ffmpeg 的 libavcodec/v4l2_m2m 已正确处理
qcom_iris 的有状态 DRC/DRAIN 握手（自查时发现需要 DECODER_CMD_START 等），
自己实现成本高；解码仍在硬件里完成（实测用户态 CPU 比软解低约 80 倍）。

客户机侧协议（shm_hdr，64 字节，小端）：
    magic[4]="HWDB", version, w, h, stride, slot_size, n_slots, mmap_size,
    write_index(u64), frames(u64), pad[16]
    环形槽位从偏移 4096 开始，第 i 帧写入 (i % n_slots)。

用法：python3 hwdec_host.py <stream.h264> [--w 1280] [--h 720] [--frames 3] [--sock ...]
"""
import argparse
import mmap
import os
import socket
import struct
import subprocess
import sys
import time

HDR = struct.Struct("<4sIIIIIIIQQ16s")
HDR_SIZE = HDR.size            # 64
DATA_OFF = 4096                # 槽位起始偏移
MAGIC = b"HWDB"


def read_exact(f, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            return bytes(buf)
        buf += chunk
    return bytes(buf)


def y_stats(buf, w, h):
    y = buf[:w * h]
    return min(y), max(y), sum(y) / float(w * h)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stream")
    ap.add_argument("--w", type=int, default=1280)
    ap.add_argument("--h", type=int, default=720)
    ap.add_argument("--n-slots", type=int, default=4)
    ap.add_argument("--frames", type=int, default=3, help="写几帧后停住等客户机读")
    ap.add_argument("--sock", default="/var/home/armada/Downloads/hwb.sock")
    a = ap.parse_args()

    w, h, n_slots = a.w, a.h, a.n_slots
    slot_size = w * h * 3 // 2
    mmap_size = DATA_OFF + slot_size * n_slots

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

    fd = os.memfd_create("hwdec-shm", 0)
    os.ftruncate(fd, mmap_size)
    shm = mmap.mmap(fd, mmap_size, mmap.MAP_SHARED,
                    mmap.PROT_READ | mmap.PROT_WRITE)
    hdr = HDR.pack(MAGIC, 1, w, h, w, slot_size, n_slots, mmap_size, 0, 0, b"")
    shm[0:HDR_SIZE] = hdr
    conn.sendmsg([hdr], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack("i", fd))])
    print("[host] 已把 memfd(fd=%d, %d 字节, %d 槽) 交给客户机" % (fd, mmap_size, n_slots))

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
           "-c:v", "h264_v4l2m2m", "-i", a.stream,
           "-f", "rawvideo", "-pix_fmt", "nv12", "-"]
    print("[host] 启动: %s" % " ".join(cmd[:8]), flush=True)
    t0 = time.time()
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=0)
    n = 0
    try:
        while n < a.frames:
            buf = read_exact(p.stdout, slot_size)
            if len(buf) < slot_size:
                print("[host] ffmpeg 只给到 %d 字节（需要 %d）" % (len(buf), slot_size))
                break
            off = DATA_OFF + (n % n_slots) * slot_size
            shm[off:off + slot_size] = buf
            n += 1
            shm[0:HDR_SIZE] = HDR.pack(MAGIC, 1, w, h, w, slot_size, n_slots,
                                       mmap_size, n, n, b"")
            if n == 1:
                mn, mx, mean = y_stats(buf, w, h)
                print("[host] 第 1 帧 Y: min=%d max=%d mean=%.1f（%.2fs）" %
                      (mn, mx, mean, time.time() - t0))
    finally:
        p.terminate()
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
    print("[host] 已写入 %d 帧，等客户机读取…" % n)
    time.sleep(3)
    conn.close()
    srv.close()
    if os.path.exists(a.sock):
        os.unlink(a.sock)
    print("[host] 完成")
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main())
