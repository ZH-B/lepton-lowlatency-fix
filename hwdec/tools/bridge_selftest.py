#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bridge_selftest.py —— 在宿主机上假扮客户机组件，直接验证硬解桥能出帧。

用法：python3 bridge_selftest.py <h264文件> [--sock ...] [--aus N]
"""
import argparse
import socket
import struct
import sys
import threading
import time

HELLO_MAGIC = 0x48454C4F
SIZE_MAGIC = 0x53495A45
AU_MAGIC = 0x41554431
EOS_MAGIC = 0x454F5331
FRAME_MAGIC = 0x46524D31
HDR = struct.Struct("<II")
FRAME_HDR = struct.Struct("<III")


def split_aus(d):
    pos = []
    i = 0
    n = len(d)
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


def recv_exact(s, n):
    b = bytearray()
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c:
            return None
        b += c
    return bytes(b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stream")
    ap.add_argument("--sock", default="/var/home/armada/Downloads/hwdec.sock")
    ap.add_argument("--size", type=int, default=1280 * 720 * 3 // 2)
    ap.add_argument("--w", type=int, default=1280)
    ap.add_argument("--h", type=int, default=720)
    ap.add_argument("--aus", type=int, default=0, help="只喂前 N 个 AU（0=全部）")
    ap.add_argument("--pace", type=float, default=0.0, help="每个 AU 的间隔（秒）")
    a = ap.parse_args()

    data = open(a.stream, "rb").read()
    aus = split_aus(data)
    total = len(aus)
    if a.aus:
        aus = aus[:a.aus]
    print("[selftest] 码流 %d 字节，%d 个 AU（本次喂 %d）" % (len(data), total, len(aus)))

    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(15.0)
    s.connect(a.sock)
    s.sendall(HDR.pack(HELLO_MAGIC, 1))
    s.sendall(HDR.pack(SIZE_MAGIC, a.size) + struct.pack("<II", a.w, a.h))
    print("[selftest] 已连接并告知帧大小 %d" % a.size)

    t0 = time.time()
    frames = [0]
    lats = []
    first_ms = [None]
    reader_done = threading.Event()

    def reader():
        try:
            while True:
                h = recv_exact(s, FRAME_HDR.size)
                if h is None:
                    break
                magic, ln, flags = FRAME_HDR.unpack(h)
                if magic != FRAME_MAGIC:
                    print("[selftest] 协议错误 magic=%#x" % magic)
                    break
                body = recv_exact(s, ln) if ln else b""
                if body is None:
                    break
                lat = None
                if frames[0] < len(t_send):
                    lat = (time.time() - t_send[frames[0]]) * 1000
                    lats.append(lat)
                frames[0] += 1
                if first_ms[0] is None:
                    first_ms[0] = (time.time() - t0) * 1000
                    print("[selftest] 第 1 帧：%d 字节，flags=%d，%.0f ms" % (ln, flags, first_ms[0]))
                if flags & 1:
                    print("[selftest] 收到 EOS 帧")
                    break
        finally:
            reader_done.set()

    th = threading.Thread(target=reader, daemon=True)
    th.start()

    t_send = []
    for i, start in enumerate(aus):
        end = aus[i + 1] if i + 1 < total else len(data)
        payload = data[start:end]
        s.sendall(HDR.pack(AU_MAGIC, len(payload)) + struct.pack("<i", i * 33333) + payload)
        t_send.append(time.time())
        if a.pace:
            time.sleep(a.pace)
    print("[selftest] 已喂入 %d AU（%.0f ms），发 EOS 让宿主 flush 尾部帧" %
          (len(aus), (time.time() - t0) * 1000))
    s.sendall(HDR.pack(EOS_MAGIC, 0))

    deadline = time.time() + 20
    while frames[0] < len(aus) and not reader_done.is_set() and time.time() < deadline:
        time.sleep(0.05)
    dt = time.time() - t0
    print("[selftest] 结果：喂 %d AU，收 %d 帧，用时 %.0f ms（%.1f fps）" %
          (len(aus), frames[0], dt * 1000, frames[0] / dt if dt else 0))
    if lats:
        print("[selftest] 宿主侧单帧延迟（AU 送到帧回来）：平均 %.1f ms，最大 %.1f ms" %
              (sum(lats) / len(lats), max(lats)))
    s.close()
    return 0 if frames[0] else 1


if __name__ == "__main__":
    sys.exit(main())
