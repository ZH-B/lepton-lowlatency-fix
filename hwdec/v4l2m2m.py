#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v4l2m2m.py —— 极简 V4L2 mem2mem（M2M_MPLANE）解码器封装。
只用 ctypes，不依赖编译器和内核头文件；结构体尺寸/ioctl 号在启动时自检。

已在 Armada OS + qcom-iris（/dev/video0, "Iris Decoder"）上验证：
  - 有状态 M2M：output=H264/HEVC/VP9/AV1，capture=NV12/P010/Q08C/Q10C
  - EXPBUF 可取解码输出的 dmabuf fd；REQBUFS(DMABUF) 亦可导入外部缓冲
"""
import ctypes
import fcntl
import mmap
import os
import struct

# ---------------------------------------------------------------- ioctl 宏
_IOC_NRBITS, _IOC_TYPEBITS, _IOC_SIZEBITS, _IOC_DIRBITS = 8, 8, 14, 2
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_NONE, _IOC_WRITE, _IOC_READ = 0, 1, 2


def _IOC(d, t, nr, size):
    return ((d << _IOC_DIRSHIFT) | (ord(t) << _IOC_TYPESHIFT) |
            (nr << _IOC_NRSHIFT) | (size << _IOC_SIZESHIFT))


def _IOR(t, nr, size):
    return _IOC(_IOC_READ, t, nr, size)


def _IOWR(t, nr, size):
    return _IOC(_IOC_READ | _IOC_WRITE, t, nr, size)


def _IOW(t, nr, size):
    return _IOC(_IOC_WRITE, t, nr, size)


def fourcc(s):
    return (ord(s[0]) | (ord(s[1]) << 8) | (ord(s[2]) << 16) | (ord(s[3]) << 24))


def fcc2s(v):
    b = bytes([v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF])
    return "".join(chr(c) if 32 <= c < 127 else "." for c in b)


u8, u16, u32 = ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint32


class v4l2_capability(ctypes.Structure):
    _fields_ = [("driver", ctypes.c_char * 16), ("card", ctypes.c_char * 32),
                ("bus_info", ctypes.c_char * 32), ("version", u32),
                ("capabilities", u32), ("device_caps", u32), ("reserved", u32 * 3)]


class v4l2_fmtdesc(ctypes.Structure):
    _fields_ = [("index", u32), ("type", u32), ("flags", u32),
                ("description", ctypes.c_char * 32), ("pixelformat", u32),
                ("mbus_code", u32), ("reserved", u32 * 3)]


class v4l2_plane_pix_format(ctypes.Structure):
    _fields_ = [("sizeimage", u32), ("bytesperline", u32), ("reserved", u16 * 6)]


class v4l2_pix_format_mplane(ctypes.Structure):
    _fields_ = [("width", u32), ("height", u32), ("pixelformat", u32), ("field", u32),
                ("colorspace", u32), ("plane_fmt", v4l2_plane_pix_format * 8),
                ("num_planes", u8), ("flags", u8), ("ycbcr_enc", u8),
                ("quantization", u8), ("xfer_func", u8), ("reserved", u8 * 7)]


class _FmtUnion(ctypes.Union):
    _fields_ = [("pix_mp", v4l2_pix_format_mplane), ("raw", u8 * 200),
                ("_align", ctypes.c_void_p)]


class v4l2_format(ctypes.Structure):
    _fields_ = [("type", u32), ("fmt", _FmtUnion)]


class v4l2_requestbuffers(ctypes.Structure):
    _fields_ = [("count", u32), ("type", u32), ("memory", u32), ("capabilities", u32),
                ("reserved", u32 * 1)]


class v4l2_plane(ctypes.Structure):
    class _M(ctypes.Union):
        _fields_ = [("mem_offset", u32), ("userptr", ctypes.c_ulong), ("fd", ctypes.c_int)]
    _fields_ = [("bytesused", u32), ("length", u32), ("m", _M),
                ("data_offset", u32), ("reserved", u32 * 11)]


class timeval(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class v4l2_timecode(ctypes.Structure):
    _fields_ = [("type", u32), ("flags", u32), ("frames", u8), ("seconds", u8),
                ("minutes", u8), ("hours", u8), ("userbits", u8 * 4)]


class _BufM(ctypes.Union):
    _fields_ = [("offset", u32), ("userptr", ctypes.c_ulong),
                ("planes", ctypes.POINTER(v4l2_plane)), ("fd", ctypes.c_int)]


class v4l2_buffer(ctypes.Structure):
    _fields_ = [("index", u32), ("type", u32), ("bytesused", u32), ("flags", u32),
                ("field", u32), ("timestamp", timeval), ("timecode", v4l2_timecode),
                ("sequence", u32), ("memory", u32), ("m", _BufM),
                ("length", u32), ("reserved2", u32), ("request_fd", ctypes.c_int),
                ("reserved", u32)]


class v4l2_exportbuffer(ctypes.Structure):
    _fields_ = [("type", u32), ("index", u32), ("plane", u32), ("flags", u32),
                ("fd", ctypes.c_int), ("reserved", u32 * 11)]


class v4l2_decoder_cmd(ctypes.Structure):
    """VIDIOC_DECODER_CMD（DRC/drain 后用 V4L2_DEC_CMD_START 恢复解码）。
    union 里有 u32 raw[16]，sizeof == 72 —— 别按 16 算 ioctl 号。"""

    class _U(ctypes.Union):
        class _Stop(ctypes.Structure):
            _fields_ = [("pts", ctypes.c_uint64)]

        class _Start(ctypes.Structure):
            _fields_ = [("speed", ctypes.c_int32), ("format", u32)]

        class _Raw(ctypes.Structure):
            _fields_ = [("data", u32 * 16)]

        _fields_ = [("stop", _Stop), ("start", _Start), ("raw", _Raw)]

    _fields_ = [("cmd", u32), ("flags", u32), ("u", _U)]


class v4l2_event_subscription(ctypes.Structure):
    _fields_ = [("type", u32), ("id", u32), ("flags", u32), ("reserved", u32 * 5)]


class timespec64(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


class _EvUnion(ctypes.Union):
    _fields_ = [("data", u8 * 64), ("_align", ctypes.c_longlong)]


class v4l2_event(ctypes.Structure):
    _fields_ = [("type", u32), ("pending", u32), ("u", _EvUnion), ("sequence", u32),
                ("_pad1", u32), ("timestamp", timespec64), ("id", u32),
                ("reserved", u32 * 8)]

    @property
    def src_change(self):
        """V4L2_EVENT_SOURCE_CHANGE 的 changes 位（union 起始处的 u32）。"""
        return ctypes.cast(ctypes.byref(self.u), ctypes.POINTER(u32))[0]


VIDIOC_QUERYCAP = _IOR("V", 0, ctypes.sizeof(v4l2_capability))
VIDIOC_ENUM_FMT = _IOWR("V", 2, ctypes.sizeof(v4l2_fmtdesc))
VIDIOC_G_FMT = _IOWR("V", 4, ctypes.sizeof(v4l2_format))
VIDIOC_S_FMT = _IOWR("V", 5, ctypes.sizeof(v4l2_format))
VIDIOC_REQBUFS = _IOWR("V", 8, ctypes.sizeof(v4l2_requestbuffers))
VIDIOC_QUERYBUF = _IOWR("V", 9, ctypes.sizeof(v4l2_buffer))
VIDIOC_QBUF = _IOWR("V", 15, ctypes.sizeof(v4l2_buffer))
VIDIOC_DQBUF = _IOWR("V", 17, ctypes.sizeof(v4l2_buffer))
VIDIOC_EXPBUF = _IOWR("V", 16, ctypes.sizeof(v4l2_exportbuffer))
VIDIOC_STREAMON = _IOW("V", 18, ctypes.sizeof(ctypes.c_int))
VIDIOC_STREAMOFF = _IOW("V", 19, ctypes.sizeof(ctypes.c_int))
VIDIOC_DQEVENT = _IOR("V", 89, ctypes.sizeof(v4l2_event))
VIDIOC_SUBSCRIBE_EVENT = _IOW("V", 90, ctypes.sizeof(v4l2_event_subscription))
VIDIOC_DECODER_CMD = _IOWR("V", 96, ctypes.sizeof(v4l2_decoder_cmd))
VIDIOC_UNSUBSCRIBE_EVENT = _IOW("V", 91, ctypes.sizeof(v4l2_event_subscription))

DEC_CMD_START = 0
DEC_CMD_STOP = 1
DEC_CMD_PAUSE = 2
DEC_CMD_RESUME = 3

EVENT_EOS = 2
EVENT_SOURCE_CHANGE = 5
SRC_CHANGE_RESOLUTION = 0x0001

V4L2_BUF_FLAG_LAST = 0x00100000

BUF_CAPTURE_MPLANE = 9
BUF_OUTPUT_MPLANE = 10
MEMORY_MMAP = 1
MEMORY_DMABUF = 4

PIX_H264 = fourcc("H264")
PIX_HEVC = fourcc("HEVC")
PIX_NV12 = fourcc("NV12")
PIX_NV12M = fourcc("NM12")

FIELD_NONE = 1
V4L2_BUF_FLAG_DONE = 0x00000004
V4L2_BUF_FLAG_ERROR = 0x00000040
V4L2_BUF_FLAG_LAST = 0x00100000


def check_abi():
    exp = {ctypes.sizeof(v4l2_capability): 104, ctypes.sizeof(v4l2_fmtdesc): 64,
           ctypes.sizeof(v4l2_format): 208, ctypes.sizeof(v4l2_requestbuffers): 20,
           ctypes.sizeof(v4l2_buffer): 88, ctypes.sizeof(v4l2_plane): 64,
           ctypes.sizeof(v4l2_exportbuffer): 64}
    bad = [k for k in exp if k != exp[k]]
    return (not bad), exp


class V4L2Error(RuntimeError):
    pass


class Decoder:
    """有状态 M2M 解码器。type 均为 MPLANE。"""

    def __init__(self, dev="/dev/video0", codec=PIX_H264, width=1280, height=720,
                 n_capture=4, n_output=4):
        self.dev = dev
        self.width, self.height = width, height
        self.codec = codec
        self.n_capture, self.n_output = n_capture, n_output
        self.fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
        self.cap = {}          # index -> dict(len, offset, fd)
        self.out = {}          # index -> dict(len, offset, mmap)
        self.cap_planes = 1
        self.out_planes = 1
        self.cap_stride = 0
        self.cap_size = 0

    # --- 基础 ioctl ---
    def _ioctl(self, req, arg, what):
        try:
            return fcntl.ioctl(self.fd, req, arg)
        except OSError as e:
            raise V4L2Error("%s: %s" % (what, e)) from None

    def querycap(self):
        c = v4l2_capability()
        self._ioctl(VIDIOC_QUERYCAP, c, "QUERYCAP")
        return c.driver.decode(), c.card.decode(), c.capabilities, c.device_caps

    def enum_formats(self, buf_type):
        out, i = [], 0
        while i < 32:
            d = v4l2_fmtdesc()
            d.index, d.type = i, buf_type
            try:
                fcntl.ioctl(self.fd, VIDIOC_ENUM_FMT, d)
            except OSError:
                break
            out.append(fcc2s(d.pixelformat))
            i += 1
        return out

    def set_fmt(self, buf_type, pixfmt, w, h, num_planes=1):
        f = v4l2_format()
        f.type = buf_type
        f.fmt.pix_mp.width, f.fmt.pix_mp.height = w, h
        f.fmt.pix_mp.pixelformat = pixfmt
        f.fmt.pix_mp.field = FIELD_NONE
        f.fmt.pix_mp.num_planes = num_planes
        self._ioctl(VIDIOC_S_FMT, f, "S_FMT %s" % fcc2s(pixfmt))
        pm = f.fmt.pix_mp
        return {"pixfmt": fcc2s(pm.pixelformat), "width": pm.width, "height": pm.height,
                "planes": pm.num_planes,
                "sizeimage": [pm.plane_fmt[i].sizeimage for i in range(pm.num_planes)],
                "stride": [pm.plane_fmt[i].bytesperline for i in range(pm.num_planes)]}

    def reqbufs(self, buf_type, count, memory):
        rb = v4l2_requestbuffers()
        rb.count, rb.type, rb.memory = count, buf_type, memory
        self._ioctl(VIDIOC_REQBUFS, rb, "REQBUFS")
        return rb.count

    def querybuf(self, buf_type, index, memory, nplanes):
        planes = (v4l2_plane * max(1, nplanes))()
        b = v4l2_buffer()
        b.type, b.index, b.memory, b.length = buf_type, index, memory, max(1, nplanes)
        b.m.planes = ctypes.cast(planes, ctypes.POINTER(v4l2_plane))
        self._ioctl(VIDIOC_QUERYBUF, b, "QUERYBUF %d" % index)
        return b, planes

    def exportbuf(self, buf_type, index, plane=0):
        eb = v4l2_exportbuffer()
        eb.type, eb.index, eb.plane = buf_type, index, plane
        self._ioctl(VIDIOC_EXPBUF, eb, "EXPBUF %d" % index)
        return eb.fd

    def qbuf(self, buf_type, index, bytesused=None, memory=MEMORY_MMAP, nplanes=1):
        planes = (v4l2_plane * max(1, nplanes))()
        if bytesused is not None:
            planes[0].bytesused = bytesused
        b = v4l2_buffer()
        b.type, b.index, b.memory, b.length = buf_type, index, memory, max(1, nplanes)
        b.m.planes = ctypes.cast(planes, ctypes.POINTER(v4l2_plane))
        self._ioctl(VIDIOC_QBUF, b, "QBUF %d" % index)
        return b

    def dqbuf(self, buf_type, nplanes=1):
        planes = (v4l2_plane * max(1, nplanes))()
        b = v4l2_buffer()
        b.type, b.memory, b.length = buf_type, MEMORY_MMAP, max(1, nplanes)
        b.m.planes = ctypes.cast(planes, ctypes.POINTER(v4l2_plane))
        self._ioctl(VIDIOC_DQBUF, b, "DQBUF")
        return b, planes

    def stream(self, on, buf_type):
        t = ctypes.c_int(buf_type)
        self._ioctl(VIDIOC_STREAMON if on else VIDIOC_STREAMOFF, t, "STREAMON/OFF")

    # --- 组合动作 ---
    def setup(self, codec=None, width=None, height=None, capture_fmt=PIX_NV12):
        """设定 output/capture 格式，申请 MMAP 缓冲并导出 capture 的 dmabuf。"""
        codec = codec or self.codec
        width = width or self.width
        height = height or self.height
        cap = self.set_fmt(BUF_OUTPUT_MPLANE, codec, width, height, 1)
        print("  output: %s %dx%d planes=%d" % (cap["pixfmt"], cap["width"], cap["height"], cap["planes"]))
        self.out_planes = cap["planes"]
        c = self.set_fmt(BUF_CAPTURE_MPLANE, capture_fmt, width, height, 1)
        print("  capture: %s %dx%d planes=%d sizeimage=%s stride=%s" %
              (c["pixfmt"], c["width"], c["height"], c["planes"], c["sizeimage"], c["stride"]))
        self.cap_planes = c["planes"]
        self.cap_size = c["sizeimage"][0]
        self.cap_stride = c["stride"][0]

        n = self.reqbufs(BUF_CAPTURE_MPLANE, self.n_capture, MEMORY_MMAP)
        for i in range(n):
            b, planes = self.querybuf(BUF_CAPTURE_MPLANE, i, MEMORY_MMAP, self.cap_planes)
            info = {"len": planes[0].length, "offset": planes[0].m.mem_offset}
            try:
                info["fd"] = self.exportbuf(BUF_CAPTURE_MPLANE, i, 0)
            except V4L2Error as e:
                info["fd"] = None
                print("  EXPBUF 失败: %s" % e)
            self.cap[i] = info
        print("  capture 缓冲 %d 个，dmabuf fd=%s" %
              (n, [self.cap[i]["fd"] for i in range(n)]))

        m = self.reqbufs(BUF_OUTPUT_MPLANE, self.n_output, MEMORY_MMAP)
        for i in range(m):
            b, planes = self.querybuf(BUF_OUTPUT_MPLANE, i, MEMORY_MMAP, self.out_planes)
            mm = mmap.mmap(self.fd, planes[0].length, mmap.MAP_SHARED,
                           mmap.PROT_READ | mmap.PROT_WRITE,
                           offset=planes[0].m.mem_offset, trackfd=False)
            self.out[i] = {"len": planes[0].length, "mmap": mm}
        print("  output 缓冲 %d 个（每个 %d 字节）" % (m, self.out[0]["len"]))
        return self

    # --- 有状态解码器的标准时序（capture 必须等 SOURCE_CHANGE 之后再建）---
    def subscribe(self, ev_type):
        s = v4l2_event_subscription()
        s.type = ev_type
        self._ioctl(VIDIOC_SUBSCRIBE_EVENT, s, "SUBSCRIBE_EVENT %d" % ev_type)

    def poll_event(self, timeout):
        import select
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return None
        ev = v4l2_event()
        try:
            self._ioctl(VIDIOC_DQEVENT, ev, "DQEVENT")
        except V4L2Error:
            # fd 可读不一定是有事件（可能是缓冲状态变化），无事件时返回 None
            return None
        return ev

    def decoder_cmd(self, cmd, speed=0, fmt=0):
        """发 VIDIOC_DECODER_CMD；DRC/drain 之后必须用 START 恢复会话。"""
        c = v4l2_decoder_cmd()
        c.cmd, c.flags = cmd, 0
        if cmd == DEC_CMD_START:
            c.u.start.speed, c.u.start.format = speed, fmt
        self._ioctl(VIDIOC_DECODER_CMD, c, "DECODER_CMD %d" % cmd)
        return c

    def reopen_capture(self, capture_fmt=PIX_NV12, n=4):
        """SOURCE_CHANGE 后按新分辨率重建 capture 队列（含新的 dmabuf fd）。"""
        try:
            self.stream(False, BUF_CAPTURE_MPLANE)
        except V4L2Error:
            pass
        try:
            self.reqbufs(BUF_CAPTURE_MPLANE, 0, MEMORY_MMAP)
        except V4L2Error:
            pass
        for d in self.cap.values():
            if d.get("fd") is not None:
                try:
                    os.close(d["fd"])
                except OSError:
                    pass
        self.cap = {}
        return self.open_capture(capture_fmt, start=True, n=n)

    def g_fmt(self, buf_type):
        f = v4l2_format()
        f.type = buf_type
        self._ioctl(VIDIOC_G_FMT, f, "G_FMT")
        pm = f.fmt.pix_mp
        return {"pixfmt": fcc2s(pm.pixelformat), "width": pm.width, "height": pm.height,
                "planes": pm.num_planes,
                "sizeimage": [pm.plane_fmt[i].sizeimage for i in range(pm.num_planes)],
                "stride": [pm.plane_fmt[i].bytesperline for i in range(pm.num_planes)]}

    def open_output(self, codec=None, width=None, height=None, n=4):
        info = self.set_fmt(BUF_OUTPUT_MPLANE, codec or self.codec,
                            width or self.width, height or self.height, 1)
        self.out_planes = info["planes"]
        m = self.reqbufs(BUF_OUTPUT_MPLANE, n, MEMORY_MMAP)
        for i in range(m):
            _b, planes = self.querybuf(BUF_OUTPUT_MPLANE, i, MEMORY_MMAP, self.out_planes)
            mm = mmap.mmap(self.fd, planes[0].length, mmap.MAP_SHARED,
                           mmap.PROT_READ | mmap.PROT_WRITE,
                           offset=planes[0].m.mem_offset, trackfd=False)
            self.out[i] = {"len": planes[0].length, "mmap": mm}
        print("  output: %s %dx%d planes=%d, %d 缓冲 x %d 字节" %
              (info["pixfmt"], info["width"], info["height"], info["planes"], m,
               self.out[0]["len"]))
        return info

    def open_capture(self, capture_fmt=PIX_NV12, start=True, n=4):
        sug = self.g_fmt(BUF_CAPTURE_MPLANE)
        w = sug["width"] or self.width
        h = sug["height"] or self.height
        info = self.set_fmt(BUF_CAPTURE_MPLANE, capture_fmt, w, h, 1)
        self.cap_planes = info["planes"]
        self.cap_size = info["sizeimage"][0]
        self.cap_stride = info["stride"][0]
        self.cap_width, self.cap_height = info["width"], info["height"]
        cnt = self.reqbufs(BUF_CAPTURE_MPLANE, n, MEMORY_MMAP)
        for i in range(cnt):
            _b, planes = self.querybuf(BUF_CAPTURE_MPLANE, i, MEMORY_MMAP, self.cap_planes)
            d = {"len": planes[0].length, "offset": planes[0].m.mem_offset, "fd": None}
            try:
                d["fd"] = self.exportbuf(BUF_CAPTURE_MPLANE, i, 0)
            except V4L2Error as e:
                print("  EXPBUF 失败: %s" % e)
            self.cap[i] = d
        print("  capture: %s %dx%d stride=%d size=%d, %d 缓冲, dmabuf fds=%s" %
              (info["pixfmt"], info["width"], info["height"], self.cap_stride,
               self.cap_size, cnt, [self.cap[i]["fd"] for i in range(cnt)]))
        # 关键：capture 缓冲必须全部 QBUF，驱动才有地方写解码结果
        for i in range(cnt):
            self.qbuf(BUF_CAPTURE_MPLANE, i, None, MEMORY_MMAP, self.cap_planes)
        print("  已把 %d 个 capture 缓冲全部 QBUF" % cnt)
        if start:
            self.stream(True, BUF_CAPTURE_MPLANE)
        return info

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass
