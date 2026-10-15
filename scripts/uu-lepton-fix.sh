#!/usr/bin/env bash
# uu-lepton-fix.sh —— 在 Armada OS 的 Lepton 客户机里修掉
# 「网易 UU 远程一开串流就崩」的平台侧病灶。
#
# 原理：客户机 libstagefright.so 里的 ACodec::setLowLatency() 被改成直接返回 OK。
#       于是 UU 远程 SDK 请求的低延迟解码不再让 MediaCodec::configure 失败，
#       也就不会踩到失败复位路径里的那个空指针（SIGSEGV / CodecLooper）。
#       客户机里只有软件解码器，实际就是按普通延迟模式解码。
#
# 落地方式：把打了补丁的 .so 放进 Lepton 派生工具的
#   ~/.local/share/lepton-armada/tool/images/rootfs_overlay/system/lib64/libstagefright.so
#   Lepton 启动时会把 overlay 里的每个文件 bind-mount 覆盖到客户机同名路径。
#   原件（Steam 的 Lepton depot）一个字节都不动，随时可回滚。
#
# 用法：
#   bash uu-lepton-fix.sh status     # 看当前状态
#   bash uu-lepton-fix.sh apply      # 拉原件 -> 打补丁 -> 部署 -> 校验
#   bash uu-lepton-fix.sh rollback   # 拿掉 overlay 文件（恢复原样）
#   ODIN=192.168.28.109 bash uu-lepton-fix.sh status    # 换 IP
set -euo pipefail

ODIN="${ODIN:-192.168.28.112}"
SSHUSER="${SSHUSER:-armada}"
SSHPASS="${SSHPASS:-armada}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE="$HERE/cache"
PATCHER="$HERE/patch-lowlatency.py"

ROOTFS_LIB='~/.local/share/Steam/steamapps/common/Lepton/images/rootfs/system/lib64/libstagefright.so'
TOOL_LIB='~/.local/share/lepton-armada/tool/images/rootfs_overlay/system/lib64/libstagefright.so'

ssh_o=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=10)
ssh_armada() { sshpass -p "$SSHPASS" ssh "${ssh_o[@]}" "$SSHUSER@$ODIN" "$@"; }
scp_from()   { sshpass -p "$SSHPASS" scp "${ssh_o[@]}" "$SSHUSER@$ODIN:$1" "$2"; }
scp_to()     { sshpass -p "$SSHPASS" scp "${ssh_o[@]}" "$1" "$SSHUSER@$ODIN:$2"; }

say()  { printf '\033[32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33m  !\033[0m %s\n' "$*"; }
die()  { printf '\033[31m错误:\033[0m %s\n' "$*" >&2; exit 1; }

check_online() {
    ssh_armada 'echo online' >/dev/null 2>&1 || die "连不上 $ODIN：设备可能休眠/掉 WiFi（Odin 3 的 ath12k 老毛病）。先唤醒再试。"
}

cmd_status() {
    check_online
    say "设备上的原件 md5"
    ssh_armada "md5sum $ROOTFS_LIB 2>/dev/null || echo '  找不到原件'"
    say "overlay 里的补丁文件"
    ssh_armada "md5sum $TOOL_LIB 2>/dev/null || echo '  尚未部署'"
    say "运行中的容器里实际生效的文件"
    ssh_armada 'T=~/.local/share/lepton-armada/tool/lepton; for c in $($T ps 2>/dev/null | awk "{print \$1}" | head -3); do
        printf "  %-32s " "$c"; podman exec "$c" md5sum /system/lib64/libstagefright.so 2>/dev/null || echo "（读不到）";
    done'
}

cmd_apply() {
    check_online
    mkdir -p "$CACHE"
    local orig="$CACHE/libstagefright.orig.so" patched="$CACHE/libstagefright.patched.so"

    say "1/4 拉取客户机原件"
    scp_from "$ROOTFS_LIB" "$orig"
    local omd5; omd5=$(md5sum "$orig" | awk '{print $1}')
    echo "    原件 md5 = $omd5"

    say "2/4 打补丁（ACodec::setLowLatency -> return OK）"
    python3 "$PATCHER" "$orig" "$patched"
    local pmd5; pmd5=$(md5sum "$patched" | awk '{print $1}')
    echo "    补丁 md5 = $pmd5"

    say "3/4 上传并放进 overlay"
    scp_to "$patched" "/tmp/libstagefright.patched.so"
    ssh_armada "set -e; mkdir -p \$(dirname $TOOL_LIB); cp /tmp/libstagefright.patched.so $TOOL_LIB; md5sum $TOOL_LIB"

    say "4/4 校验"
    local got; got=$(ssh_armada "md5sum $TOOL_LIB | awk '{print \$1}'")
    [[ "$got" == "$pmd5" ]] || die "部署后 md5 不一致：$got != $pmd5"
    echo "    overlay 文件已就位（$got）"
    warn "要让改动生效，必须从 Steam 重新启动该应用（容器重建时才会重新挂载 overlay）"
    cat <<EOF

补丁就绪。回滚：bash $0 rollback
以后 Lepton 升级导致派生工具重建时，重跑一次 apply 即可。
EOF
}

cmd_rollback() {
    check_online
    say "删除 overlay 里的补丁文件"
    ssh_armada "rm -f $TOOL_LIB; ls -la $TOOL_LIB 2>/dev/null || echo '  已删除（恢复使用 Lepton 原件）'"
    cat <<EOF

已回滚。重启应用后客户机使用 Lepton 原件（UU 远程会重新出现原来的崩溃）。
EOF
}

case "${1:-status}" in
    status)   cmd_status ;;
    apply)    cmd_apply ;;
    rollback) cmd_rollback ;;
    *)        die "未知命令：$1（可用：status / apply / rollback）" ;;
esac
