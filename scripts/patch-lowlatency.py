#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""patch-lowlatency.py —— 把 Android 客户机 libstagefright.so 里的
ACodec::setLowLatency(int32_t) 补成「直接返回 OK」。

背景
----
网易 UU 远程的串流 SDK（libstreamer.so）会给 H.264 解码器设 MediaFormat
键 "low-latency"=1。Android 的 ACodec 收到后会向 OMX 组件下发
OMX_IndexConfigLowLatency(0x6f800006)，而**软件解码器**（OMX.google.*）
必然拒绝，于是 ACodec::configureCodec 返回错误，MediaCodec 在复位路径里
空指针崩溃（Fatal signal 11 in tid CodecLooper）——整个 Lepton 容器随之退出。

Lepton 客户机里只有软件解码器（没有 c2/Codec2 组件、也没有硬件解码器），
所以只能让平台侧「接受」这个设置。把 setLowLatency 的头两条指令改成
    mov w0, #0     ; return OK
    ret
即可：configure 不再失败，mIsLowLatency 保持 false（= 普通延迟模式）。

适用
----
Armada OS / Lepton rootfs v2.8.11（libstagefright.so md5
262f0793b7ef3bf401a9c896934e3963）。换版本时用 --auto 让脚本自己找函数。

用法
----
    python3 patch-lowlatency.py 原始.so 输出.so          # 用已知偏移（校验字节）
    python3 patch-lowlatency.py 原始.so 输出.so --auto   # 自动定位（推荐换版本时）
"""
import struct
import sys

from elftools.elf.elffile import ELFFile

STR = b"decoder can not set low-latency to %d (err %d)"
MOV_W0_0 = 0x52800000          # mov w0, #0
RET = 0xD65F03C0               # ret
BTI_C = 0xD503245F             # bti c
PACIASP = 0xD503233F           # paciasp

# rootfs v2.8.11 上人工确认过的位置与原始字节
KNOWN = {"vaddr": 0xA0C08, "orig": bytes.fromhex("ff8301d1fd7b02a9")}


def sections(path):
    """返回 (text节数据, text基址, rodata节, 已加载段列表)；读完后关闭文件。"""
    with open(path, "rb") as f:
        elf = ELFFile(f)
        text = elf.get_section_by_name(".text")
        rodata = elf.get_section_by_name(".rodata")
        if text is None or rodata is None:
            raise SystemExit("不是预期的 ELF：缺少 .text/.rodata")
        tb = text.data()
        base = text["sh_addr"]
        rd = (rodata.data(), rodata["sh_addr"])
        segs = [(s["p_offset"], s["p_vaddr"], s["p_filesz"])
                for s in elf.iter_segments() if s["p_type"] == "PT_LOAD"]
    return tb, base, rd, segs


def vaddr_to_off(segs, va):
    for off, vaddr, filesz in segs:
        if vaddr <= va < vaddr + filesz:
            return off + (va - vaddr)
    return None


def find_string_vaddr(rodata):
    data, addr = rodata
    off = data.find(STR)
    if off < 0:
        raise SystemExit("定位失败：.rodata 里找不到 low-latency 错误字符串")
    return addr + off


def refs_to(tb, base, target):
    """扫描 ADRP+ADD 组合，返回引用 target 地址的指令地址列表。"""
    out = []
    n = len(tb)
    for i in range(0, n - 8, 4):
        w = struct.unpack_from("<I", tb, i)[0]
        if (w & 0x9F000000) != 0x90000000:      # ADRP
            continue
        rd = w & 0x1F
        imm = (((w >> 5) & 0x7FFFF) << 2) | ((w >> 29) & 0x3)
        if imm & (1 << 20):
            imm -= 1 << 21
        page = ((base + i) & ~0xFFF) + (imm << 12)
        for j in range(i + 4, min(i + 160, n - 4), 4):
            w2 = struct.unpack_from("<I", tb, j)[0]
            if (w2 & 0xFF800000) != 0x91000000:  # ADD (imm), 64-bit
                continue
            rn, rd2, imm12 = (w2 >> 5) & 0x1F, w2 & 0x1F, (w2 >> 10) & 0xFFF
            if rn == rd and page + imm12 == target:
                out.append(base + i)
    return out


def is_sp_sub(w):
    return (w & 0xFF800000) == 0xD1000000 and (w & 0x1F) == 31 and ((w >> 5) & 0x1F) == 31


def is_stp_fp_lr(w):
    # stp x29, x30, [sp, #-N]!
    return w in (0xA9BF7BFD, 0xA9BE7BFD, 0xA9BD7BFD)


def find_func_start(tb, base, ref_addr):
    """从引用点往回找函数入口：入口通常是 sub sp,sp,#N / stp x29,x30,[sp,#-N]!
    （或前面还有 bti c / paciasp），且其前一条指令是上一条函数的收尾。"""
    i = ref_addr - base
    for _ in range(i // 4):
        w = struct.unpack_from("<I", tb, i)[0]
        if is_sp_sub(w) or is_stp_fp_lr(w):
            prev = struct.unpack_from("<I", tb, i - 4)[0] if i >= 4 else 0
            prev_is_end = prev in (RET, 0xD4200000) or (prev & 0xFC000000) in (0x14000000, 0x94000000)
            if prev_is_end:
                start = base + i
                w0 = struct.unpack_from("<I", tb, i)[0]
                if w0 in (BTI_C, PACIASP):
                    start += 4
                return start
        i -= 4
    raise SystemExit("定位失败：没能从引用点回溯到函数入口")


def main():
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    src, dst = sys.argv[1], sys.argv[2]
    auto = "--auto" in sys.argv

    tb, base, rodata, segs = sections(src)
    sv = find_string_vaddr(rodata)
    hits = refs_to(tb, base, sv)
    if not hits:
        raise SystemExit("定位失败：没有代码引用 low-latency 错误字符串")

    print("字符串 vaddr = %#x，引用点 = %s" % (sv, ", ".join(hex(h) for h in hits)))

    targets = []
    if auto:
        for h in hits:
            targets.append(find_func_start(tb, base, h))
    else:
        targets = [KNOWN["vaddr"]]

    data = bytearray(open(src, "rb").read())
    changed = 0
    for va in sorted(set(targets)):
        off = vaddr_to_off(segs, va)
        if off is None:
            raise SystemExit("定位失败：vaddr %#x 不在任何 PT_LOAD 里" % va)

        cur = bytes(data[off:off + 8])
        if cur == struct.pack("<II", MOV_W0_0, RET):
            print("  %#x 已经是补丁后的样子，跳过" % va)
            continue
        if not auto and cur != KNOWN["orig"]:
            raise SystemExit(
                "字节不匹配：%#x 处是 %s，期望 %s。"
                "（Lepton 版本可能变了，试试 --auto）"
                % (va, cur.hex(), KNOWN["orig"].hex()))
        if auto:
            # 自动模式下只接受「像函数入口」的位置
            w = struct.unpack_from("<I", tb, va - base)[0]
            if not (is_sp_sub(w) or is_stp_fp_lr(w)):
                raise SystemExit("自动定位到的 %#x 不像函数入口（%#x），请人工检查" % (va, w))
        data[off:off + 8] = struct.pack("<II", MOV_W0_0, RET)
        changed += 1
        print("  已打补丁 @ %#x（文件偏移 %#x）：%s -> %s"
              % (va, off, cur.hex(), bytes(data[off:off + 8]).hex()))

    if not changed:
        print("没有任何改动。")
    open(dst, "wb").write(bytes(data))

    a, b = open(src, "rb").read(), open(dst, "rb").read()
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    print("输出 %s：差异字节 %d 个（%s）"
          % (dst, len(diff), hex(min(diff)) + ".." + hex(max(diff)) if diff else "无"))


if __name__ == "__main__":
    main()
