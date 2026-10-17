# lepton-lowlatency-fix

在 **Valve Lepton / Waydroid 这类「客户机只有软件解码器」的 Android 容器**里，
修复因应用请求「低延迟解码」而导致的崩溃。

典型场景：**Armada OS（如 AYN Odin 3）上用 Lepton 运行网易 UU 远程**，
一开远控就崩（Steam 显示「游戏崩溃」），或一直卡在「初始化连接」。

- 原理：客户机 `libstagefright.so` 的 `ACodec::setLowLatency()` 改 8 字节为「直接返回成功」，
  让 `MediaCodec.configure()` 不再失败（详见 [原理摘要](#7-原理摘要)）。
- 落地：放进 Lepton 的 overlay 目录，容器启动时 bind-mount 覆盖客户机同名文件。
  **Steam / Lepton 原件一字节不动**，一条命令可回滚。
- 适用范围：**任何**因设置 `MediaFormat` 的 `low-latency` 而在软解客户机里崩溃的 Android 应用
  （不只是 UU 远程）。

## 另外一个成果：硬件解码桥（可选，独立目录）

如果目标是**让客户机里只有软解的应用用上宿主的硬件解码器**（降低 CPU/发热、降低解码延迟），
本仓库还附带一套完整的实现，见 **[`hwdec/`](hwdec/README.md)**：

- 客户机侧：顶替 `libstagefright_soft_{avc,hevc}dec.so` 的 OMX 代理组件（`component/shim_avcdec.cpp`，
  一份源码编 AVC/HEVC 两个变体），把压缩帧转给宿主、把宿主解出来的帧按 **I420** 灌回 OMX 缓冲；
- 宿主侧：`hwdec_bridge.py` + `hwdec_v4l2.py`（**自写 V4L2 有状态解码器**，不走 ffmpeg，
  这正是不走高延迟 ffmpeg 管线的关键）+ `v4l2m2m.py`（ctypes V4L2 M2M 层）；
- 夹具/工具：`hwdec/tools/`（客户机延迟基准、UU 节奏复现、宿主管线延迟探针等）。

实测（Qualcomm + `qcom-iris`，1080p30 HEVC，客户机内）：

| 指标 | 内置软解 | 硬件解码桥 |
|---|---|---|
| 端到端延迟（AU 入队→帧出队） | 101.9 ms | **33.7 ms** |
| 客户机 `media.codec` CPU | ~79% 单核 | **~26% 单核** |
| 输出像素 | 基准 | **逐字节一致** |
| 帧率/丢帧 | 30fps | 30fps，桥侧 1:1、0 丢帧 |

需要注意的三点（细节在 [`hwdec/README.md`](hwdec/README.md)）：

1. **不要声明 NV12**：把输出端口格式改成 NV12 会让 UU 这类应用**启动即崩**（实测）；
   必须保持组件原本声明的 I420，自己把宿主的 NV12 拆成 I420。
2. 1080p 与「原画（4K）」在 I420 路径下都实测可用；4K 黑屏的锅在 NV12 实验，不在 4K。
3. 桥需要手动/自启运行；**桥不在时 shim 自动回退原版软解**，所以装了也不影响日常可用性。

---

> 仓库不附带修改过的系统库文件：补丁由脚本在你自己的设备上、基于设备上的原始文件现场生成
> （更安全，也不受 Lepton 版本变化影响）。

---

## 0. 快速开始

```bash
git clone git@github.com:ZH-B/lepton-lowlatency-fix.git
cd lepton-lowlatency-fix
export ODIN=192.168.28.112          # 你的 Armada 设备 IP

bash scripts/uu-lepton-fix.sh status
bash scripts/uu-lepton-fix.sh apply
# 然后从 Steam 重新启动该应用（必须重建容器才会挂载 overlay）
```

---

## 1. 前置条件

**本机**（Linux / WSL，需 python3）：

```bash
sudo apt-get install -y python3-pyelftools sshpass        # Debian/Ubuntu
```

**设备**：Armada OS 已联网；SSH 可登录（默认用户 `armada` / 密码 `armada`，
可用环境变量 `SSHUSER` / `SSHPASS` 覆盖）。

**定位设备 IP**（DHCP 会变）：

```bash
for i in $(seq 1 254); do
  (timeout 1 bash -c "exec 3<>/dev/tcp/192.168.28.$i/22" 2>/dev/null && echo "OPEN 192.168.28.$i") &
done; wait
export ODIN=<扫到的 IP>
```

---

## 2. 修复（三步）

### 2.1 查看当前状态

```bash
bash scripts/uu-lepton-fix.sh status
```

输出三处信息：设备上 Lepton **原件** md5、**overlay** 里的补丁文件（未部署会提示）、
以及运行中容器里**实际生效**的文件 md5。首次修复通常看到：

```
原件 md5   262f0793b7ef3bf401a9c896934e3963
overlay    尚未部署
```

### 2.2 一键修复

```bash
bash scripts/uu-lepton-fix.sh apply
```

脚本：拉原件 → 打补丁（**校验原始字节，不匹配即报错中止**）→ 上传 → 写入 overlay → 比对 md5。
成功时最后一行形如：

```
    overlay 文件已就位（22f702cf8651d6e5ac89117f821e40b2）
```

### 2.3 让改动生效

**从 Steam 重新启动该应用**（overlay 只在容器创建时挂载，重启应用=重建容器）。
若已有会话在跑，先退出再启动。

---

## 3. 验收

界面：应用能正常进入主界面并建立会话，不再闪退、不再卡「初始化连接」。

日志（在设备上执行）：

```bash
cd ~/.local/share/lepton/steamvr/logs
L=$(ls -t lepton-steamlaunch-*.log | head -1)
sed "s/\x1b\[[0-9;]*m//g" "$L" | grep -aE "low-latency|Fatal signal|set native gles renderer" | tail -20
```

通过标准：

- ✅ **没有** `decoder can not set low-latency`、**没有** `Fatal signal`；
- ✅ 出现 `I StreamerSurfaceView: set native gles renderer <非 0 数字>`。

> 日志开头可能出现 `current native gles renderer handle is zero`——那是视图创建过程中的瞬时状态，
> 只要随后出现 `set native gles renderer <非 0>` 即正常。

---

## 4. 常见情况处理

| 情况 | 处理办法 |
|---|---|
| **连不上设备** | 设备休眠/掉 WiFi（Odin 3 的 ath12k 老毛病）。先唤醒；必要时在设备桌面执行 `armada-wifi-debug.sh --fix`，再回 §1 重扫 IP |
| **Armada / Lepton 更新后补丁失效** | 更新会重建派生工具、清掉 overlay → 重跑 §2.2 |
| **`apply` 报「字节不匹配」** | 库文件随版本变了，用 `--auto` 重新定位（见下） |
| **部署后仍崩溃** | ① 确认确实从 Steam 重启过应用；② `status` 看运行容器内 md5 是否已是 `22f702cf…`；③ 抓 §3 日志看是否换了别的失败点 |
| **想不动线上会话做验证** | 起独立 dev 容器（与 Steam 启动共用同一份 overlay）：见下 |

`apply` 报字节不匹配时：

```bash
scp armada@$ODIN:~/.local/share/Steam/steamapps/common/Lepton/images/rootfs/system/lib64/libstagefright.so /tmp/orig.so
python3 scripts/patch-lowlatency.py /tmp/orig.so /tmp/patched.so --auto
# 成功会打印：已打补丁 @ 0x??????（文件偏移 ...）：xxxxxxxx -> 00008052c0035fd6
scp /tmp/patched.so armada@$ODIN:/tmp/libstagefright.patched.so
sshpass -p armada ssh armada@$ODIN '
  T=~/.local/share/lepton-armada/tool/images/rootfs_overlay/system/lib64
  mkdir -p "$T"; cp /tmp/libstagefright.patched.so "$T/libstagefright.so"
  md5sum "$T/libstagefright.so"'
# 然后从 Steam 重启应用
```

不动线上会话的验证方式：

```bash
sshpass -p armada ssh armada@$ODIN '
  T=~/.local/share/lepton-armada/tool/lepton
  nohup $T start dev >/dev/null 2>&1 &
  sleep 45
  $T exec dev sh -c "md5sum /system/lib64/libstagefright.so"'
# 期望：22f702cf8651d6e5ac89117f821e40b2  /system/lib64/libstagefright.so
```

---

## 5. 回滚

```bash
bash scripts/uu-lepton-fix.sh rollback
```

删除 overlay 内的补丁文件，重启应用后恢复使用 Lepton 原件（原先的崩溃行为也会恢复，属预期）。

---

## 6. 背景：为什么会崩

崩溃日志（`~/.local/share/lepton/steamvr/logs/lepton-steamlaunch-<appid>.log`）：

```
E ACodec  : decoder can not set low-latency to 1 (err -1010)
E ACodec  : [OMX.google.h264.decoder] configureCodec returning error -1010
E ACodec  : Unable to instantiate a decoder for type 'video/avc' with err 0xfffffffe.
E MediaCodec: configure failed with err 0xfffffffe, resetting...
F libc    : Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x0 in tid 1305 (CodecLooper)
```

1. 应用（如 UU 远程的 `libstreamer.so`）给 `MediaFormat` 设了 `low-latency=1`；
2. 客户机里只有软件解码器（`OMX.google.*`），而 `ACodec::setLowLatency()` 下发的
   `OMX_IndexConfigLowLatency` 只有支持它的**硬件**解码器才会接受 → 必然 `err -1010`
   → `configureCodec` 失败；
3. `MediaCodec.configure()` 失败后走复位路径，`CodecLooper` 线程空指针崩溃
   （应用此时还在并发操作同一 codec），容器随之退出。

---

## 7. 原理摘要

`libstagefright.so` 里 `ACodec::setLowLatency(int32_t)` 的头两条指令改成：

```
原始   : ff 83 01 d1  fd 7b 02 a9   (sub sp, sp, #0x60 ; stp x29, x30, [sp, #0x20])
补丁后 : 00 00 80 52  c0 03 5f d6   (mov w0, #0 ; ret)  ← 直接返回 OK
```

- 定位方式：在 `.rodata` 找字符串 `"decoder can not set low-latency to %d (err %d)"`，
  再扫 `.text` 中引用它的 `ADRP+ADD` 指令对，回溯到函数入口（`--auto` 即此逻辑）。
- 效果：`configure()` 不再失败；`mIsLowLatency` 保持 `false`，按**普通延迟模式**解码。
- **软解 / 硬解**：补丁**不改变解码器选择**。客户机里可用的 AVC 解码器只有软件解码器
  （`OMX.google.h264.decoder`），没有硬件解码器，所以**修复前后都是软件解码**；
  区别只是修复前 `configure()` 直接失败并崩溃，修复后能正常解码。
  代价是 1080p 高帧率可能吃力，可在应用内调低画质/帧率。

### 版本与校验

| 时间 | Lepton / rootfs | `libstagefright.so` md5 | 补丁 |
|---|---|---|---|
| 2026-10-07 | v2.8.14 / v2.8.11 | `262f0793b7ef3bf401a9c896934e3963` | 函数 @ `0xa0c08`，补丁后 md5 `22f702cf8651d6e5ac89117f821e40b2` |
| 2026-10-12 | v3.0.5 / v3.0.3 | `262f0793b7ef3bf401a9c896934e3963`（同一文件） | 同上，通用 |

> 换 Lepton 版本时先比原件 md5：**相同 → 直接 `apply`**；不同 → 用 `--auto` 重新定位。

---

## 8. 文件

| 路径 | 说明 |
|---|---|
| `scripts/uu-lepton-fix.sh` | `status` / `apply` / `rollback` |
| `scripts/patch-lowlatency.py` | 补丁器：`patch-lowlatency.py <输入.so> <输出.so> [--auto]` |

依赖：`python3-pyelftools`、`sshpass`（`capstone` 仅人工核对反汇编时需要）。

---

## 9. 上游问题

这个补丁是**绕过**，不是根治。真正的上游问题与反馈建议见 [`UPSTREAM.md`](UPSTREAM.md)：
Lepton 客户机不提供硬件解码器（且默认 `debug.stagefright.ccodec=0` 强制走 OMX），
以及应用在 `MediaCodec.configure()` 失败时未做处理并并发操作 codec。

## License

MIT，见 [`LICENSE`](LICENSE)。
