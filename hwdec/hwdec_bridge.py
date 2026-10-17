#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""hwdec_bridge.py —— 宿主侧硬解桥（常驻、多连接版）

客户机组件（shim）连上这个 socket 后：
  guest → host：HELLO(8B) → SIZE(frame_size, 8B) → 若干 AU（12B 头 + 压缩数据）
                → EOS(8B，要求 flush 并回送结束标志) / FLUSH(8B，重启解码器)
  host  → guest：若干 FRAME（12B 头 + NV12 数据），flags=1 表示 EOS
宿主用 ffmpeg 的 v4l2m2m 驱动 /dev/video0（qcom-iris）解码；每个连接一个 ffmpeg 进程，
帧按 FIFO 顺序回发，客户机自己用输入 pts 队列配时间戳（v1 不处理 B 帧重排的 pts 偏移）。
连接断开后继续监听，供下次使用。

用法：python3 hwdec_bridge.py [--sock ...] [--verbose]
"""
import argparse
import collections
import os
import socket
import struct
import subprocess
import sys
import threading
import time

import hwdec_v4l2

HELLO_MAGIC = 0x48454C4F     # "HELO"
SIZE_MAGIC = 0x53495A45      # "SIZE"
AU_MAGIC = 0x41554431        # "AUD1"
EOS_MAGIC = 0x454F5331       # "EOS1"
FLUSH_MAGIC = 0x464C5331     # "FLS1"
FRAME_MAGIC = 0x46524D31     # "FRM1"
CODEC_MAGIC = 0x434F4443     # "CODC"：解码器类型 1=H.264 2=HEVC
FRAME_FLAG_EOS = 1

HDR = struct.Struct("<II")            # magic, value
AU_HDR = struct.Struct("<III")        # magic, len, pts(低 32 位)
FRAME_HDR = struct.Struct("<III")     # magic, len, flags

ARGS = None
STOP = threading.Event()

# 活着的会话（用于封顶与空闲回收）：客户机进程被杀时 socket 由 media.codec 持有，
# 不会自动断开，所以必须靠「上限 + 空闲超时」兜底，否则解码器实例/设备 fd 会一直堆着。
LIVE_SESSIONS = []
ALL_DECODERS = []        # 所有活着的解码器引擎（用于回收孤儿，保证设备 fd 不无限增长）
SESSION_LOCK = threading.Lock()


def register_session(sess):
    evicted = []
    with SESSION_LOCK:
        LIVE_SESSIONS.append(sess)
        while len(LIVE_SESSIONS) > ARGS.max_sessions:
            evicted.append(LIVE_SESSIONS.pop(0))
    for e in evicted:
        log("会话数超过 %d，关闭最旧会话（%s）" % (ARGS.max_sessions, e.summary()))
        e.close()


def unregister_session(sess):
    with SESSION_LOCK:
        if sess in LIVE_SESSIONS:
            LIVE_SESSIONS.remove(sess)


def idle_watchdog():
    while not STOP.is_set():
        time.sleep(5)
        now = time.time()
        idle = []
        with SESSION_LOCK:
            for s in list(LIVE_SESSIONS):
                if now - getattr(s, "last_active", now) > ARGS.idle_timeout:
                    idle.append(s)
                    LIVE_SESSIONS.remove(s)
            # 孤儿解码器：会话已经不活着了，但引擎还占着 /dev/video0
            live_decs = set(id(x.dec) for x in LIVE_SESSIONS if not x.stop.is_set())
            orphans = [d for d in ALL_DECODERS if id(d) not in live_decs]
            ALL_DECODERS[:] = [d for d in ALL_DECODERS if id(d) in live_decs]
        for s in idle:
            log("会话空闲超过 %ds，回收（%s）" % (ARGS.idle_timeout, s.summary()))
            s.close()
        for d in orphans:
            log("回收孤儿解码器（%s）" % (d.summary() if hasattr(d, "summary") else "?"))
            try:
                d.close()
            except Exception:
                pass


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def log(msg):
    print("[bridge %s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def ffmpeg_cmd():
    # 这些参数是实测调出来的（见 ff_latency_probe.py）：
    #   * 必须 -c:v h264_v4l2m2m：走宿主 qcom-iris 硬解；
    #   * 必须 -fps_mode passthrough：否则 rawvideo 输出按 25fps CFR 去重，60 帧只剩 3 帧；
    #   * 必须 -probesize 32 -analyzeduration 0：ffmpeg 默认要缓冲约 1.6MB(≈52 帧) 才吐第一帧；
    #   * 必须 -flags low_delay：再去掉解码器重排缓冲，首帧从 11 AU 降到 3~4 AU；
    #   * 千万不要 -fflags nobuffer：管道输入下一帧都不出（要 EOF 才吐）。
    return [ARGS.ffmpeg, "-hide_banner", "-loglevel", "error"] + \
           (ARGS.ffextra.split() if ARGS.ffextra else []) + \
           ["-probesize", "32", "-analyzeduration", "0",
            "-flags", "low_delay",
            "-c:v", "h264_v4l2m2m", "-f", "h264", "-i", "pipe:0",
            "-fps_mode", "passthrough",
            "-f", "rawvideo", "-pix_fmt", "nv12", "-"]


class FfSession(object):
    """一次 ffmpeg 解码会话：把 AU 喂进去，把 NV12 帧取出来回发。

    线程结构（关键：解码管线永远不被客户端拖住）：
        pump   线程：读 ffmpeg stdout 的整帧 → 放进有界队列（满了丢最旧帧）
        sender 线程：从队列取帧 → sendall 给客户机（可以阻塞，只阻塞它自己）
    这样即使客户机一时不读 socket，ffmpeg 也不会被堵住，最多丢帧 ——
    对低延迟直播来说，丢旧帧本来就是正确行为。
    """

    MAX_QUEUE = 2

    def __init__(self, conn, tag, frame_size_ref, lock, gen, codec=1):
        self.codec = codec
        self.conn = conn
        self.tag = tag
        self.frame_size_ref = frame_size_ref
        self.lock = lock
        self.gen = gen
        self.ff = None
        self.stop = threading.Event()
        self.pump = None
        self.sender = None
        self.sent = 0
        self.dropped = 0
        self.aus = 0
        self.t0 = time.time()
        self.frames = collections.deque(maxlen=self.MAX_QUEUE)
        self.cv = threading.Condition()

    def start(self):
        cmd = ffmpeg_cmd()
        log("%s [解码器#%d] 启动 ffmpeg" % (self.tag, self.gen))
        self.ff = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, bufsize=0)
        self.sender = threading.Thread(target=self._send_loop, daemon=True)
        self.sender.start()
        self.pump = threading.Thread(target=self._pump, daemon=True)
        self.pump.start()

    def _pump(self):
        """读整帧 → 入队。绝不因为客户端慢而阻塞（队列满就丢最旧）。"""
        while not self.stop.is_set():
            with self.lock:
                fs = self.frame_size_ref[0]
            if fs == 0:
                time.sleep(0.002)
                continue
            buf = bytearray()
            while len(buf) < fs:
                try:
                    chunk = self.ff.stdout.read(fs - len(buf))
                except (OSError, ValueError):
                    chunk = b""
                if not chunk:
                    if not self.stop.is_set():
                        log("%s [解码器#%d] ffmpeg 输出结束" % (self.tag, self.gen))
                    self.stop.set()
                    with self.cv:
                        self.cv.notify_all()
                    return
                buf += chunk
            with self.cv:
                if len(self.frames) == self.frames.maxlen:
                    self.frames.popleft()
                    self.dropped += 1
                self.frames.append(bytes(buf))
                self.cv.notify_all()

    def _send_loop(self):
        while True:
            with self.cv:
                while not self.frames and not self.stop.is_set():
                    self.cv.wait(0.2)
                if not self.frames:
                    if self.stop.is_set():
                        return
                    continue
                payload, flags = self.frames.popleft()
            try:
                self.conn.sendall(FRAME_HDR.pack(FRAME_MAGIC, len(payload), flags) + payload)
            except OSError as e:
                log("%s [解码器#%d] 回发失败: %s" % (self.tag, self.gen, e))
                self.stop.set()
                return
            self.sent += 1
            if ARGS.verbose and self.sent % 30 == 1:
                log("%s [解码器#%d] 已回发 %d 帧（丢 %d）" %
                    (self.tag, self.gen, self.sent, self.dropped))

    def feed(self, payload):
        view = memoryview(payload)
        while view:
            n = self.ff.stdin.write(view)
            if not n:
                raise OSError("ffmpeg stdin 写入 0 字节")
            view = view[n:]
        self.ff.stdin.flush()
        self.aus += 1
        if ARGS.verbose and self.aus % 30 == 1:
            log("%s [解码器#%d] 已喂 %d 个 AU" % (self.tag, self.gen, self.aus))

    def end_input(self):
        """关掉 stdin 让 ffmpeg flush 出剩余帧；等 pump 把剩下的帧读完。"""
        try:
            self.ff.stdin.close()
        except (OSError, ValueError):
            pass
        if self.pump:
            self.pump.join(timeout=5.0)

    def close(self):
        """务必先放设备再收线程：join 自己会抛 RuntimeError，把设备 fd 漏掉（实测踩过，
        漏掉之后残留实例还会把硬件解码器占住，导致后面所有会话都拿不到缓冲）。"""
        self.stop.set()
        try:
            self.dec.close()
        except Exception:
            pass
        with self.cv:
            self.cv.notify_all()
        with SESSION_LOCK:
            if self.dec in ALL_DECODERS:
                ALL_DECODERS.remove(self.dec)
        me = threading.current_thread()
        for t in (self.pump, self.sender):
            if t and t is not me:
                try:
                    t.join(timeout=2.0)
                except Exception:
                    pass

    def summary(self):
        return "%s / 回发 %d 帧 / 丢 %d 帧 / 用时 %.1fs" % (
            self.dec.summary(), self.sent, self.dropped, time.time() - self.t0)


class V4l2Session(object):
    """用自写 V4L2 解码器（hwdec_v4l2）的会话：延迟比 ffmpeg 低 2~3 帧。

    实测（720p30）：宿主侧 AU → 帧回来只有 ~7ms，客户机端到端 33.6ms（= 1 帧）。
    """

    MAX_QUEUE = 2

    def __init__(self, conn, tag, frame_size_ref, lock, gen, dev="/dev/video0",
                 n_out=4, n_cap=4, verbose=False, codec=1):
        self.conn = conn
        self.tag = tag
        self.frame_size_ref = frame_size_ref
        self.lock = lock
        self.gen = gen
        self.dec = hwdec_v4l2.V4l2Decoder(dev=dev, n_out=n_out, n_cap=n_cap,
                                         verbose=verbose, codec=codec)
        self.stop = threading.Event()
        self.pump = None
        self.sender = None
        self.sent = 0
        self.dropped = 0
        self.aus = 0
        self.geo = None
        self.t0 = time.time()
        self.last_active = time.time()
        self.frames = collections.deque(maxlen=self.MAX_QUEUE)
        self.cv = threading.Condition()
        self.opened = False
        self.pending_aus = []       # 还没拿到几何参数时先攒着（客户端可能先送码流）
        stale = []
        with SESSION_LOCK:
            ALL_DECODERS.append(self.dec)
            # 设备 fd 硬上限：新会话建立时，把最旧的引擎关掉。
            # （会话册记偶尔会漏注销，这里是最后一道闸，保证 /dev/video0 的 fd 不增长。）
            while len(ALL_DECODERS) > ARGS.max_sessions:
                stale.append(ALL_DECODERS.pop(0))
        for d in stale:
            log("解码器超出上限 %d，关闭最旧实例" % ARGS.max_sessions)
            try:
                d.close()
            except Exception:
                pass

    def set_geometry(self, w, h):
        self.geo = (w, h)
        if not self.opened and w and h:
            self.dec.open(w, h)
            self.opened = True
            log("%s [v4l2#%d] 已按 %dx%d 打开 %s" %
                (self.tag, self.gen, w, h, self.dec.d.dev))
            # 把等几何参数期间攒下的 AU 补喂进去
            for au in self.pending_aus:
                try:
                    self.dec.feed(au)
                except Exception as e:
                    log("%s [v4l2#%d] 补喂失败: %s" % (self.tag, self.gen, e))
                    break
            if self.pending_aus:
                log("%s [v4l2#%d] 已补喂 %d 个缓存 AU" %
                    (self.tag, self.gen, len(self.pending_aus)))
            self.pending_aus = []

    def start(self):
        self.sender = threading.Thread(target=self._send_loop, daemon=True)
        self.sender.start()
        self.pump = threading.Thread(target=self._pump, daemon=True)
        self.pump.start()

    def _pump(self):
        while not self.stop.is_set():
            if not self.opened:
                time.sleep(0.002)
                continue
            try:
                got = self.dec.poll(0.05)
                self.dec.events()
            except Exception as e:      # 驱动异常不能让桥整个死掉，但必须把设备彻底放开
                if self.stop.is_set() or getattr(e, "errno", None) == 9:
                    self.stop.set()     # 别的线程已经关掉会话了，安静退出
                    break
                log("%s [v4l2#%d] 解码异常（关闭会话）: %s" % (self.tag, self.gen, e))
                unregister_session(self)
                self.close()
                break
            if got:
                self.last_active = time.time()
            for item in got:
                with self.cv:
                    if len(self.frames) == self.frames.maxlen:
                        self.frames.popleft()
                        self.dropped += 1
                    self.frames.append(item)     # (payload, flags)
                    self.cv.notify_all()
        # 线程退出时必须把设备放开（否则设备 fd 会随着会话数慢慢堆着）
        try:
            self.dec.close()
        except Exception:
            pass

    def _send_loop(self):
        while True:
            with self.cv:
                while not self.frames and not self.stop.is_set():
                    self.cv.wait(0.2)
                if not self.frames:
                    if self.stop.is_set():
                        return
                    continue
                payload, flags = self.frames.popleft()
            try:
                self.conn.sendall(FRAME_HDR.pack(FRAME_MAGIC, len(payload), flags) + payload)
            except OSError as e:
                log("%s [v4l2#%d] 回发失败: %s" % (self.tag, self.gen, e))
                self.stop.set()
                return
            self.sent += 1
            if ARGS.verbose and self.sent % 30 == 1:
                log("%s [v4l2#%d] 已回发 %d 帧（丢 %d）" %
                    (self.tag, self.gen, self.sent, self.dropped))

    def feed(self, payload):
        if not self.opened:
            # 还不知道分辨率：先缓存（上限 60 个），不要因此判定连接失败
            if len(self.pending_aus) < 60:
                self.pending_aus.append(payload)
            else:
                self.pending_aus.pop(0)
                self.pending_aus.append(payload)
            self.aus += 1
            return
        deadline = time.time() + 0.5
        while not self.stop.is_set():
            if self.dec.feed(payload):
                self.aus += 1
                self.last_active = time.time()
                if ARGS.verbose and self.aus % 30 == 1:
                    log("%s [v4l2#%d] 已喂 %d AU" % (self.tag, self.gen, self.aus))
                return
            time.sleep(0.001)
            if time.time() > deadline:
                raise OSError("output 缓冲一直不空闲")
        raise OSError("会话已停止")

    def flush_now(self):
        # 只清宿主侧还没发出去的帧；V4L2 队列保持不动（拆了驱动会卡在 DRC 重建里）
        with self.cv:
            self.frames.clear()
        if not self.opened:
            self.pending_aus = []
            return
        try:
            self.dec.flush()
        except Exception as e:
            log("%s [v4l2#%d] flush 异常: %s" % (self.tag, self.gen, e))

    def end_input(self):
        if not self.opened:
            return
        for item in self.dec.end_input():
            with self.cv:
                if len(self.frames) == self.frames.maxlen:
                    self.frames.popleft()
                    self.dropped += 1
                self.frames.append(item)
                self.cv.notify_all()

    def close(self):
        """务必先放设备再收线程：join 自己会抛 RuntimeError，把设备 fd 漏掉（实测踩过；
        漏掉之后残留实例还会占住硬件解码器，导致后面所有会话都拿不到缓冲）。"""
        self.stop.set()
        try:
            self.dec.close()
        except Exception:
            pass
        with self.cv:
            self.cv.notify_all()
        with SESSION_LOCK:
            if self.dec in ALL_DECODERS:
                ALL_DECODERS.remove(self.dec)
        me = threading.current_thread()
        for t in (self.pump, self.sender):
            if t and t is not me:
                try:
                    t.join(timeout=2.0)
                except Exception:
                    pass

    def summary(self):
        return "%s / 回发 %d 帧 / 丢 %d 帧 / 用时 %.1fs" % (
            self.dec.summary(), self.sent, self.dropped, time.time() - self.t0)


def make_session(conn, tag, frame_size_ref, lock, gen, codec=1):
    if ARGS.decoder == "ffmpeg":
        return FfSession(conn, tag, frame_size_ref, lock, gen, codec=codec)
    return V4l2Session(conn, tag, frame_size_ref, lock, gen, verbose=ARGS.verbose,
                       codec=codec)


def handle(conn, peer_id):
    tag = "#%d" % peer_id
    hdr = recv_exact(conn, HDR.size)
    if hdr is None:
        log("%s HELLO 读取失败" % tag)
        return
    magic, ver = HDR.unpack(hdr)
    log("%s 客户机已连接 HELLO magic=%#x ver=%d" % (tag, magic, ver))
    codec_id = 1
    if ver >= 2:
        ch = recv_exact(conn, HDR.size)
        if ch is not None:
            cmagic, cval = HDR.unpack(ch)
            if cmagic == CODEC_MAGIC:
                codec_id = cval
                log("%s 解码类型 = %s" % (tag, "HEVC" if cval == 2 else "H.264"))

    frame_size = [0]
    lock = threading.Lock()
    gen = [0]
    cur = [None]

    def new_session():
        gen[0] += 1
        s = make_session(conn, tag, frame_size, lock, gen[0], codec=codec_id)
        s.start()
        register_session(s)
        cur[0] = s
        return s

    new_session()
    try:
        while not STOP.is_set():
            h = recv_exact(conn, HDR.size)
            if h is None:
                break
            magic, value = HDR.unpack(h)
            if magic == SIZE_MAGIC:
                extra = recv_exact(conn, 8)      # width, height（v2 协议）
                w = h = 0
                if extra is not None:
                    w, h = struct.unpack("<II", extra)
                with lock:
                    frame_size[0] = value
                cur_sess = cur[0]
                if cur_sess is not None and hasattr(cur_sess, "set_geometry"):
                    cur_sess.set_geometry(w, h)
                log("%s 帧大小 = %d 字节（%dx%d）" % (tag, value, w, h))
                continue
            if magic == FLUSH_MAGIC:
                sess = cur[0]
                if sess is not None and hasattr(sess, "flush_now"):
                    # v4l2 后端：复用同一个解码器（几何参数还在），只重置 V4L2 队列；
                    # 换新会话会丢掉 SIZE，新会话永远打不开 → 客户端一直卡在等画面。
                    sess.flush_now()
                    log("%s Flush：保留解码器，仅重置队列（%s）" % (tag, sess.summary()))
                else:
                    old = cur[0]
                    if old:
                        unregister_session(old)
                        old.close()
                        log("%s Flush：%s" % (tag, old.summary()))
                    new_session()
                    with lock:
                        frame_size[0] = 0
                continue
            if magic == EOS_MAGIC:
                s = cur[0]
                log("%s EOS：等待 flush…" % tag)
                if s:
                    s.end_input()
                    log("%s EOS 完成：%s" % (tag, s.summary()))
                try:
                    conn.sendall(FRAME_HDR.pack(FRAME_MAGIC, 0, FRAME_FLAG_EOS))
                except OSError:
                    break
                continue
            if magic != AU_MAGIC:
                log("%s 未知消息 magic=%#x" % (tag, magic))
                continue
            length = value
            rest = recv_exact(conn, 8)          # pts（64 位）
            if rest is None:
                break
            pts = struct.unpack("<q", rest)[0]
            payload = recv_exact(conn, length) if length else b""
            if payload is None:
                break
            s = cur[0]
            if s is None:
                continue
            try:
                s.feed(payload)
            except (OSError, ValueError) as e:
                log("%s 写入 ffmpeg 失败: %s" % (tag, e))
                break
    finally:
        s = cur[0]
        if s:
            unregister_session(s)
            s.close()
            log("%s 收尾：%s" % (tag, s.summary()))
        try:
            conn.close()
        except OSError:
            pass


def main():
    global ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--sock", default="/var/home/armada/Downloads/hwdec.sock")
    ap.add_argument("--ffmpeg", default="ffmpeg")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--ffextra", default="", help="附加到 ffmpeg 解码参数前的额外参数")
    ap.add_argument("--max-sessions", type=int, default=3,
                    help="同时允许的解码会话数（超出时关最旧的）")
    ap.add_argument("--idle-timeout", type=float, default=20.0,
                    help="会话空闲多少秒后回收（客户机进程被杀时 socket 不会自己断）")
    ap.add_argument("--decoder", default="v4l2", choices=["v4l2", "ffmpeg"],
                    help="v4l2=自写解码器（低延迟，默认）；ffmpeg=旧的 h264_v4l2m2m 管线")
    ARGS = ap.parse_args()

    if os.path.exists(ARGS.sock):
        os.unlink(ARGS.sock)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(ARGS.sock)
    os.chmod(ARGS.sock, 0o777)
    srv.listen(8)
    log("监听 %s，等客户机组件连接…" % ARGS.sock)

    threading.Thread(target=idle_watchdog, daemon=True).start()

    n = 0
    while not STOP.is_set():
        try:
            conn, _ = srv.accept()
        except OSError:
            break
        n += 1
        threading.Thread(target=handle, args=(conn, n), daemon=True).start()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
