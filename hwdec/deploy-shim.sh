#!/usr/bin/env bash
# deploy-shim.sh —— 把编译好的 OMX 代理组件（AVC 和/或 HEVC）原子部署到 Lepton rootfs overlay。
#
# 用法：
#   bash deploy-shim.sh                 # 部署 AVC + HEVC 两个变体（默认）
#   CODEC=avc bash deploy-shim.sh       # 只部署 AVC
#   CODEC=hevc bash deploy-shim.sh      # 只部署 HEVC
#   RESTART=0 bash deploy-shim.sh       # 不重建容器（只换文件，等下次重建才生效）
#
# 为什么必须原子替换（先传临时文件再 mv）：
#   media.codec 会 mmap 这些 .so。就地覆盖（truncate+write）会改掉正在运行进程的映射，
#   GOT/PLT 页读到新文件里的 0 → 下一次调用 SIGSEGV（pc=0x0），整个 OMX 服务崩掉。
# overlay 是整目录 bind mount 进容器，但挂载点是**逐文件**的：新 inode 要重建容器才可见。
set -euo pipefail

CODEC="${CODEC:-all}"
ODIN="${ODIN:-192.168.28.112}"
SSHUSER="${SSHUSER:-armada}"
SSHPASS="${SSHPASS:-armada}"

case "$CODEC" in
  all)  LIST="avc hevc" ;;
  avc)  LIST="avc" ;;
  hevc) LIST="hevc" ;;
  *) echo "CODEC 只能是 all/avc/hevc" >&2; exit 1 ;;
esac

SSH=(sshpass -p "$SSHPASS" ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$SSHUSER@$ODIN")
SCP=(sshpass -p "$SSHPASS" scp -o StrictHostKeyChecking=no)

for c in $LIST; do
  SO="/tmp/libstagefright_soft_${c}dec.so"
  [[ -f "$SO" ]] || { echo "缺少 $SO（先跑 CODEC=$c bash component/build.sh）" >&2; exit 1; }
  "${SCP[@]}" "$SO" "$SSHUSER@$ODIN:~/.local/share/lepton-armada/tool/images/rootfs_overlay/vendor/lib64/.new-${c}dec.so"
  echo "已上传 $c"
done

"${SSH[@]}" "
set -e
O=\$HOME/.local/share/lepton-armada/tool/images/rootfs_overlay/vendor/lib64
R=\$HOME/.local/share/Steam/steamapps/common/Lepton/images/rootfs
mkdir -p \$O
for c in $LIST; do
  n=libstagefright_soft_\${c}dec.so
  o=libsf_\${c}dec_orig.so
  [ -f \$O/\$o ] || cp -f \$R/vendor/lib64/\$n \$O/\$o
  chmod 755 \$O/\$o
  mv -f \$O/.new-\${c}dec.so \$O/\$n
  chmod 755 \$O/\$n
  echo \"== \$n\"
  md5sum \$O/\$n \$O/\$o
done
"

if [[ "${RESTART:-1}" == "1" ]]; then
  echo "==> 重建 lepton dev 容器（逐文件 bind mount，必须重建才看得到新 inode）"
  "${SSH[@]}" 'T=$HOME/.local/share/lepton-armada/tool/lepton; $T kill dev >/dev/null 2>&1; sleep 1; nohup $T start dev >/tmp/lepton-dev-start.log 2>&1 & sleep 42; podman ps --format "{{.Names}} {{.Status}}" | grep dev; $T exec dev sh -c "md5sum /vendor/lib64/libstagefright_soft_avcdec.so /vendor/lib64/libstagefright_soft_hevcdec.so"'
fi

cat <<'EOF'

提示：
  * 只替换 AVC/HEVC 的软解实现（OMX.google.h264/hevc.decoder），其它组件不动；
  * 宿主桥会按组件的 HELLO 自动选择 H.264/HEVC；
  * UU 远程等应用要**重启**后才会创建新解码组件（旧实例还在旧实现上）；
  * 回滚：删 overlay 里对应 codec 的 libstagefright_soft_*dec.so 与 libsf_*dec_orig.so 再重建容器。
    不要动 system/lib64/libstagefright.so（那是低延迟补丁）。
EOF
