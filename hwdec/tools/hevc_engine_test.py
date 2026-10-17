#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hevc_engine_test.py —— 验证 hwdec_v4l2 的 HEVC 路径（PIX_HEVC + DRC + 按可见尺寸打包）。

HEVC 的 AUD 是 NAL type 35（H.264 是 9），所以这里单独切帧。
用法：python3 hevc_engine_test.py <hevc 文件> [--w 1280] [--h 720]
"""
import argparse
import sys
import time

import hwdec_v4l2


def split_aus_hevc(d):
    """按 HEVC AUD(35) 切 access unit；没有 AUD 就整段一包。"""
    auds, i, n = [], 0, len(d)
    while i + 5 < n:
        if d[i:i + 4] == b"\x00\x00\x00\x01":
            sc, p = 4, i
        elif d[i:i + 3] == b"\x00\x00\x01":
            sc, p = 3, i
        else:
            i += 1
            continue
        if p + sc + 1 < n:
            nal = (d[p + sc] >> 1) & 0x3F          # HEVC: 6bit type 在第 2..7 位
            if nal == 35:
                auds.append(p)
        i = p + sc + 1
    if len(auds) > 1:
        return [d[auds[k]:(auds[k + 1] if k + 1 < len(auds) else n)] for k in range(len(auds))]
    return [d]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("stream")
    ap.add_argument("--w", type=int, default=1280)
    ap.add_argument("--h", type=int, default=720)
    ap.add_argument("--pace", type=float, default=0.033)
    ap.add_argument("--aus", type=int, default=60)
    a = ap.parse_args()

    data = open(a.stream, "rb").read()
    aus = split_aus_hevc(data)[:a.aus]
    print("[hevc] %s：%d AU（本次喂 %d）" % (a.stream, len(split_aus_hevc(data)), len(aus)))
    if len(aus) < 2:
        print("[hevc] 码流里没有 HEVC AUD，切不出 AU")
        return 1

    dec = hwdec_v4l2.V4l2Decoder(codec=2, verbose=True)
    dec.open(a.w, a.h)

    t0 = time.time()
    t_feed = []
    frames = 0
    for i, au in enumerate(aus):
        deadline = time.time() + 2.0
        while not dec.feed(au):
            time.sleep(0.001)
            if time.time() > deadline:
                print("[hevc] 喂第 %d 个 AU 超时" % i)
                return 3
        t_feed.append(time.time())
        for _ in range(4):
            got = dec.poll(0.02)
            if got:
                frames += len(got)
                if frames <= 3:
                    print("[hevc] 第 %d 帧 %d 字节（%.0f ms）" %
                          (frames, len(got[-1]), (time.time() - t0) * 1000))
        dec.events()
        if a.pace:
            time.sleep(a.pace)
    for _ in range(20):
        got = dec.poll(0.05)
        if not got:
            break
        frames += len(got)
    print("[hevc] 结果：喂 %d AU / 出 %d 帧 / 用时 %.0f ms / %s" %
          (len(aus), frames, (time.time() - t0) * 1000, dec.summary()))
    dec.close()
    return 0 if frames else 1


if __name__ == "__main__":
    sys.exit(main())
