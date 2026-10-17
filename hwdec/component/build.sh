#!/usr/bin/env bash
# build.sh —— 在手机容器里构建客户机组件 shim（bionic/aarch64），并打印部署方式。
#
# 依赖：Zig（aarch64-linux）+ NDK sysroot（见 README 或下面的路径）
#   /root/opt/zig                                  zig 0.15.2
#   /root/opt/ndk/sysroot                          NDK r27c 的 sysroot（已扁平化 API 29 的库）
set -euo pipefail

ZIG="${ZIG:-/root/opt/zig/zig}"
SYS="${SYS:-/root/opt/ndk/sysroot}"
API="${API:-29}"
LIBDIR="$SYS/usr/lib/aarch64-linux-android/$API"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CODEC="${CODEC:-avc}"          # avc | hevc —— 两个变体同一份源码，只换原库路径与产物名
case "$CODEC" in
  avc)  OUT="${OUT:-/tmp/libstagefright_soft_avcdec.so}"; ORIGLIB="/vendor/lib64/libsf_avcdec_orig.so" ;;
  hevc) OUT="${OUT:-/tmp/libstagefright_soft_hevcdec.so}"; ORIGLIB="/vendor/lib64/libsf_hevcdec_orig.so" ;;
  *) echo "CODEC 只能是 avc 或 hevc" >&2; exit 1 ;;
esac
SONAME="$(basename "$OUT")"

[[ -x "$ZIG" ]] || { echo "找不到 zig：$ZIG" >&2; exit 1; }
[[ -d "$LIBDIR" ]] || { echo "找不到 NDK 库目录：$LIBDIR" >&2; exit 1; }

echo "==> 1/2 编译 shim_avcdec.cpp（bionic aarch64, PIE/共享库）"
"$ZIG" c++ -target aarch64-linux-android -D__ANDROID_API__="$API" \
    -nostdinc -nostdinc++ \
    -isystem "$SYS/usr/include" \
    -isystem "$SYS/usr/include/aarch64-linux-android" \
    -isystem "$SYS/usr/include/c++/v1" \
    -I "$HERE/include" -I "$HERE/include/openmax" \
    -fPIC -O2 -Wall -DORIG_LIB="\"$ORIGLIB\"" -c -o /tmp/shim_$CODEC.o "$HERE/shim_avcdec.cpp"

echo "==> 2/2 链接共享库 -> $OUT"
"$ZIG" ld.lld -m aarch64linux -shared \
    -soname "$SONAME" \
    -o "$OUT" /tmp/shim_$CODEC.o \
    -L"$LIBDIR" -llog -ldl -lc

python3 - "$OUT" <<'PY'
import sys
from elftools.elf.elffile import ELFFile
p = sys.argv[1]
f = open(p, "rb"); e = ELFFile(f)
want = "_Z22createSoftOMXComponentPKcPK16OMX_CALLBACKTYPEPvPP17OMX_COMPONENTTYPE"
syms = [s.name for s in e.get_section_by_name(".dynsym").iter_symbols()]
ok = want in syms
print("    导出入口:", want if ok else [s for s in syms if "createSoftOMX" in s])
f.close()
sys.exit(0 if ok else 1)
PY

cat <<EOF

完成：$OUT

部署（Armada 宿主机，需要能 SSH）：
  R=~/.local/share/Steam/steamapps/common/Lepton/images/rootfs
  O=~/.local/share/lepton-armada/tool/images/rootfs_overlay/vendor/lib64
  mkdir -p \$O
  # 原库改名保留（shim 会 dlopen 它）
  cp -f \$R/vendor/lib64/libstagefright_soft_avcdec.so \$O/libsf_avcdec_orig.so
  scp $OUT armada@<设备IP>:\$O/libstagefright_soft_avcdec.so
  chmod 755 \$O/*.so          # 关键：权限不足会导致解码服务 dlopen 失败
  # 重启 Lepton 容器（overlay 只在容器创建时挂载）后生效
EOF
