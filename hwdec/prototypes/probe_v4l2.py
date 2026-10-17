#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_v4l2.py —— 探测 V4L2 硬件解码器（如 qcom-iris）的能力，重点是：
  1) 设备能力/是否 M2M、支持哪些输入输出格式
  2) 能否给解码输出缓冲做 EXPBUF（导出 dmabuf fd）  ← 跨容器零拷贝的前提
  3) 能否用 DMABUF 内存类型导入外部缓冲（在 guest 分配、宿主直接写入） ← 更理想的前提

用法（在 Armada 宿主机上跑，需要 python3）：
    python3 probe_v4l2.py [/dev/video0]
"""
import ctypes
import fcntl
import os
import sys

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


def fourcc(s):
    return (ord(s[0]) | (ord(s[1]) << 8) | (ord(s[2]) << 16) | (ord(s[3]) << 24))


def fcc2s(v):
    s = bytes([v & 0xff, (v >> 8) & 0xff, (v >> 16) & 0xff, (v >> 24) & 0xff])
    return "".join(chr(c) if 32 <= c < 127 else "." for c in s)


# ---------------------------------------------------------------- 内核结构
u8, u16, u32, u64 = (ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint32, ctypes.c_uint64)


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
    # _align 让 union 8 字节对齐，从而 sizeof(v4l2_format) == 208（内核 ABI）
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


# ---------------------------------------------------------------- ioctl 号
VIDIOC_QUERYCAP = _IOR("V", 0, ctypes.sizeof(v4l2_capability))
VIDIOC_ENUM_FMT = _IOWR("V", 2, ctypes.sizeof(v4l2_fmtdesc))
VIDIOC_G_FMT = _IOWR("V", 4, ctypes.sizeof(v4l2_format))
VIDIOC_S_FMT = _IOWR("V", 5, ctypes.sizeof(v4l2_format))
VIDIOC_REQBUFS = _IOWR("V", 8, ctypes.sizeof(v4l2_requestbuffers))
VIDIOC_QUERYBUF = _IOWR("V", 9, ctypes.sizeof(v4l2_buffer))
VIDIOC_EXPBUF = _IOWR("V", 16, ctypes.sizeof(v4l2_exportbuffer))
VIDIOC_QBUF = _IOWR("V", 15, ctypes.sizeof(v4l2_buffer))
VIDIOC_STREAMON = _IOC(_IOC_WRITE, "V", 18, ctypes.sizeof(ctypes.c_int))

V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE = 9
V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE = 10
V4L2_MEMORY_MMAP = 1
V4L2_MEMORY_DMABUF = 4
V4L2_PIX_FMT_H264 = fourcc("H264")
V4L2_PIX_FMT_HEVC = fourcc("HEVC")
V4L2_PIX_FMT_NV12M = fourcc("NM12")
V4L2_PIX_FMT_NV12 = fourcc("NV12")
V4L2_PIX_FMT_YUV420 = fourcc("YU12")

CAP_VIDEO_M2M_MPLANE = 0x00001000
CAP_STREAMING = 0x04000000


def banner(t):
    print("\n== %s" % t)


def check_abi():
    exp = {"capability": (ctypes.sizeof(v4l2_capability), 104),
           "fmtdesc": (ctypes.sizeof(v4l2_fmtdesc), 64),
           "format": (ctypes.sizeof(v4l2_format), 208),
           "requestbuffers": (ctypes.sizeof(v4l2_requestbuffers), 20),
           "buffer": (ctypes.sizeof(v4l2_buffer), 88),
           "plane": (ctypes.sizeof(v4l2_plane), 64),
           "exportbuffer": (ctypes.sizeof(v4l2_exportbuffer), 64)}
    ok = True
    for k, (got, want) in exp.items():
        flag = "ok" if got == want else "!! 不匹配"
        if got != want:
            ok = False
        print("  sizeof(%-15s) = %3d (期望 %3d) %s" % (k, got, want, flag))
    # 交叉验证已知 ioctl 常量
    known = {"QUERYCAP": (VIDIOC_QUERYCAP, 0x80685600), "S_FMT": (VIDIOC_S_FMT, 0xC0D05605),
             "REQBUFS": (VIDIOC_REQBUFS, 0xC0145608), "QUERYBUF": (VIDIOC_QUERYBUF, 0xC0585609)}
    for k, (got, want) in known.items():
        flag = "ok" if got == want else "!! 不匹配"
        if got != want:
            ok = False
        print("  %-10s = %#010x (期望 %#010x) %s" % (k, got, want, flag))
    return ok


def ioctl(fd, req, arg, what):
    try:
        return fcntl.ioctl(fd, req, arg)
    except OSError as e:
        print("  [%s] ioctl 失败: %s" % (what, e))
        return None


def enum_formats(fd, buf_type, label):
    i = 0
    found = []
    while True:
        d = v4l2_fmtdesc()
        d.index = i
        d.type = buf_type
        try:
            fcntl.ioctl(fd, VIDIOC_ENUM_FMT, d)
        except OSError:
            break
        found.append((fcc2s(d.pixelformat), d.description.decode("utf-8", "replace")))
        i += 1
        if i > 32:
            break
    print("  %s 支持格式 (%d): %s" % (label, len(found),
                                    ", ".join("%s(%s)" % (f, d) for f, d in found) or "无"))
    return found


def set_fmt_mplane(fd, buf_type, pixfmt, w, h, num_planes):
    f = v4l2_format()
    f.type = buf_type
    f.fmt.pix_mp.width = w
    f.fmt.pix_mp.height = h
    f.fmt.pix_mp.pixelformat = pixfmt
    f.fmt.pix_mp.field = 1  # V4L2_FIELD_NONE
    f.fmt.pix_mp.num_planes = num_planes
    r = ioctl(fd, VIDIOC_S_FMT, f,
              "S_FMT %s %s %dx%d planes=%d" % (fcc2s(pixfmt),
                                               "capture" if buf_type == V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE else "output",
                                               w, h, num_planes))
    if r is None:
        return None
    print("  S_FMT %s -> %s %dx%d planes=%d sizeimage=%s" %
          (fcc2s(pixfmt), fcc2s(f.fmt.pix_mp.pixelformat), f.fmt.pix_mp.width,
           f.fmt.pix_mp.height, f.fmt.pix_mp.num_planes,
           [f.fmt.pix_mp.plane_fmt[i].sizeimage for i in range(f.fmt.pix_mp.num_planes)]))
    return f


def reqbufs(fd, buf_type, count, memory):
    rb = v4l2_requestbuffers()
    rb.count, rb.type, rb.memory = count, buf_type, memory
    r = ioctl(fd, VIDIOC_REQBUFS, rb, "REQBUFS type=%d mem=%d count=%d" % (buf_type, memory, count))
    if r is None:
        return None
    print("  REQBUFS mem=%s -> count=%d" % ({1: "MMAP", 4: "DMABUF"}.get(memory, memory), rb.count))
    return rb


def querybuf(fd, buf_type, index, nplanes):
    planes = (v4l2_plane * nplanes)()
    b = v4l2_buffer()
    b.type, b.index, b.memory = buf_type, index, V4L2_MEMORY_MMAP
    b.length = nplanes
    b.m.planes = ctypes.cast(planes, ctypes.POINTER(v4l2_plane))
    if ioctl(fd, VIDIOC_QUERYBUF, b, "QUERYBUF idx=%d" % index) is None:
        return None, None
    info = [(planes[i].length, planes[i].m.mem_offset) for i in range(nplanes)]
    print("  QUERYBUF idx=%d -> planes(len,off)=%s" % (index, info))
    return b, planes


def expbuf(fd, buf_type, index, plane):
    eb = v4l2_exportbuffer()
    eb.type, eb.index, eb.plane = buf_type, index, plane
    if ioctl(fd, VIDIOC_EXPBUF, eb, "EXPBUF idx=%d plane=%d" % (index, plane)) is None:
        return None
    try:
        target = os.readlink("/proc/self/fd/%d" % eb.fd)
    except OSError:
        target = "?"
    print("  EXPBUF idx=%d plane=%d -> fd=%d (%s)" % (index, plane, eb.fd, target))
    return eb.fd


def main():
    dev = sys.argv[1] if len(sys.argv) > 1 else "/dev/video0"
    print("=== ABI 自检")
    if not check_abi():
        print("!! 结构体大小与内核 ABI 不符，后续 ioctl 可能 ENOTTY —— 先修表")
        return 2

    fd = os.open(dev, os.O_RDWR | os.O_NONBLOCK)
    try:
        banner("1) QUERYCAP %s" % dev)
        cap = v4l2_capability()
        if ioctl(fd, VIDIOC_QUERYCAP, cap, "QUERYCAP") is not None:
            print("  driver=%s card=%s bus=%s" % (cap.driver.decode(), cap.card.decode(),
                                                  cap.bus_info.decode()))
            print("  capabilities=%#x device_caps=%#x  M2M_MPLANE=%s STREAMING=%s" %
                  (cap.capabilities, cap.device_caps,
                   bool(cap.capabilities & CAP_VIDEO_M2M_MPLANE),
                   bool(cap.capabilities & CAP_STREAMING)))

        banner("2) 支持的格式")
        enum_formats(fd, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE, "OUTPUT(码流输入)")
        enum_formats(fd, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE, "CAPTURE(帧输出)")

        banner("3) 设定格式 OUTPUT=H264 1920x1080 / CAPTURE=NV12M 1920x1080")
        if set_fmt_mplane(fd, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE, V4L2_PIX_FMT_H264, 1920, 1080, 1) is None:
            print("  用 HEVC 重试…")
            set_fmt_mplane(fd, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE, V4L2_PIX_FMT_HEVC, 1920, 1080, 1)
        cf = set_fmt_mplane(fd, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE, V4L2_PIX_FMT_NV12M, 1920, 1080, 2)
        nplanes = cf.fmt.pix_mp.num_planes if cf else 2

        banner("4) MMAP 申请输出缓冲 + EXPBUF 导出 dmabuf")
        rb = reqbufs(fd, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE, 4, V4L2_MEMORY_MMAP)
        if rb and rb.count:
            b, _ = querybuf(fd, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE, 0, nplanes)
            if b:
                expbuf(fd, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE, 0, 0)

        banner("5) 试探 DMABUF 内存类型（guest 分配缓冲、宿主直接写入）")
        reqbufs(fd, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE, 0, V4L2_MEMORY_MMAP)  # 释放
        rb2 = reqbufs(fd, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE, 2, V4L2_MEMORY_DMABUF)
        print("  => DMABUF 模式%s" % ("可用" if (rb2 and rb2.count) else "不可用/需外部 fd"))
        if rb2:
            reqbufs(fd, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE, 0, V4L2_MEMORY_DMABUF)
    finally:
        os.close(fd)
    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
