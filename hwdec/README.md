# Lepton 硬件解码桥（Armada OS / qcom-iris）

> 前提：只对**客户机里只有软件解码器**的 Lepton/Waydroid 容器有意义；
> 需要宿主是 Qualcomm 平台（这里用的是 `qcom-iris` 的 `/dev/video0`）、有 python3，
> 以及 Zig + NDK sysroot 来编译客户机组件（见 §三）。
> 相关：本仓库主线的「低延迟崩溃修复」见 [../README.md](../README.md)。

让 Lepton 客户机里的应用（UU 远程等）在解码时**用上宿主的硬件解码器**，
而不是在客户机里跑软解。做法是替换客户机的 AVC 解码组件，把码流转发给宿主解，
再把解出来的帧灌回 OMX 输出缓冲。

- 目标设备：AYN Odin 3（aarch64，Armada OS / Fedora-bootc，内核 7.2.6）
- 宿主硬解引擎：`/dev/video0` = `qcom-iris`（`iris_driver`，V4L2 stateful M2M）
- 客户机：Lepton（Waydroid 派生）容器 `lepton-steamlaunch-*` / `lepton-dev`

---

## 一、最终实测结果

测量口径：客户机内 `guest_latbench`（走 App 同一条 MediaCodec/OMX 路径），按码流真实帧率喂 AU，
端到端延迟 = **AU 入队 → 对应帧出队**（FIFO 配对；取帧用 2ms 小步轮询，消除「异步帧要等下一轮
才被发现」的偏差）。每格是「端到端平均延迟 / 客户机 `media.codec` 进程 CPU」。

| 场景 | 硬解桥 | 内置软解 | 延迟 | CPU |
|---|---|---|---|---|
| 720p30（60 帧） | **33.6 ms** / 190 ms | 101.9 ms / 1030 ms | **3.0× 更低** | **−82%** |
| 1080p60（300 帧） | **16.9 ms** / 790 ms | 51.9 ms / 6090 ms | **3.1× 更低** | **−87%** |
| 4K30（120 帧） | **33.8 ms** / 1530 ms | 101.2 ms / 4820 ms | **3.0× 更低** | **−68%** |

其它已验证项：

| 项 | 结果 |
|---|---|
| 解码帧数 | 三种场景**全帧通过**（60/60、300/300、120/120），帧率跟得上码流（29.4 / 57.9 / 29.4 fps） |
| 输出像素 | 与软解**逐字节一致**（Y/U/V 三平面差异全 0，PNG md5 相同） |
| low-latency | `configure -> OK`，组件直接接受 `OMX_IndexConfigLowLatency`，不再报 -1010 |
| 通路判定 | logcat：`数据通路 = 宿主硬解` / `输出端口: … colorFormat=0x13 -> I420` |
| 退化行为 | 桥不在时自动回退软解；断开后**下一个 AU 会自动重连**（不会永久退化） |
| 资源 | 跑完 3 个场景后宿主桥：`/dev/video0` fd = **0**、线程 = 3（无泄漏） |

> **结论**：硬解桥现在**同时赢延迟和 CPU** —— 端到端延迟约为内置软解的 **1/3**（33.6 ms ≈ 720p30 的
> 一帧间隔，1080p60 甚至只有 16.9 ms），客户机解码 CPU 降 68%~87%，输出与软解逐字节一致。
> 达到这个结果的关键是**不走 ffmpeg**：ffmpeg 的 raw h264 parser 必须等到下一帧起始码才肯吐当前帧
> （多 1 帧），rawvideo muxer + AVIO 再叠一层；换成自写 V4L2 客户端后宿主侧 AU→帧只剩 ~7 ms。
> 对 UU 远程这类实时流，这 33 ms 基本就是「解码器本身的一帧延迟」，已经到硬件能给的极限。

证据（可复现）：
- `evidence/hwdec-hw-720p.png`、`evidence/hwdec-sw-720p.png` —— 硬解/软解同一帧（PNG md5 相同）
- `evidence/hwdec-frame-testsrc2-1280x720.png` —— 端到端取帧
- 测试码流：`test_aud.h264`(720p30) / `test_1080p60.h264` / `test_4k30.h264`，都用 AUD 切帧
  （`-bsf:v h264_metadata=aud=insert`）

---

## 一之二、实机验证（UU 远程真机，2026-10-17）

不是夹具，是**真的 UU 远程会话**（`com.netease.uuremote` 在 `lepton-steamlaunch-2893933989` 里串流）：

| 项 | 实测 |
|---|---|
| 解码器 | `OMX.google.hevc.decoder` → 我们的 shim → 宿主 `/dev/video0`（日志 `codec=HEVC`、`数据通路 = 宿主硬解`、`输出端口: 1920x1088 stride=1920 colorFormat=0x13`） |
| 码流 | HEVC **1080p30**（输出缓冲 3133440 字节 = 1920×1088 NV12） |
| 帧率/丢帧 | 稳定 **30 fps**（8 秒窗口 240 帧），桥侧 5000+ 帧中**仅丢 2 帧** |
| 客户机 `media.codec` CPU | **26%** 单核（软解时代实测 ~79% 单核） |
| 客户机 UU 进程 CPU | 62% 单核（UI/WebRTC/网络，与解码无关） |
| 宿主桥进程 CPU | 13% 单核（搬帧；真正的解码在硬件/固件里，不计入 CPU） |
| 4K | 桥日志里出现过一次 `3840x2160` 的 HEVC 会话（创建后 1.4 s 关闭、0 帧），说明 UU 已经能发起 4K 解码配置 |

结论：**UU 远程在真机上已经跑在宿主硬件解码上**，1080p30 稳定、几乎不丢帧，客户机解码 CPU 降到约 1/3。

## 一之三、真机故障定位（2026-10-17 晚）：解码 1:1，卡在「应用不渲染」

### 走到的最终状态

| 项 | 结论 |
|---|---|
| 宿主硬解 + 客户机交付 | **通**：真机会话桥侧 `喂入 2323 AU → 出 2287 帧`，坏帧过滤命中 34 帧（空帧），丢 3 帧 |
| 客户机解码 CPU | 真机测过 79% → 26%（1080p30 HEVC） |
| 输出像素 | 与软解逐字节一致（夹具验证） |
| **应用渲染** | **不通**：UU 一直停在「正在传输画面」，ACodec 无报错、帧已交付但不显示 |

### 已修的真实缺陷（保留在代码里）

1. **capture 队列必须比 output 流先就绪**（1080p 下反序会 `REQBUFS` EBUSY →
   `qcom-iris: session error 0x4000003`，会话再也起不来）。
2. **客户端 Flush 不能新建会话**（否则丢几何参数 → `喂入 0 AU` 死循环）；Flush 不动 V4L2 队列。
3. **帧参数要在 `EmptyThisBuffer` 就上报**（UU 是先送码流、后给输出缓冲），桥侧在无几何时缓存 AU。
4. **坏帧必须过滤**：驱动标记 `V4L2_BUF_FLAG_ERROR` 的失败帧、Y 平面几乎全 0 的空帧。
   真机上 2323 AU 里命中 34 个空帧——这就是「偶发绿屏」的来源。
5. 断线先快速重连并丢帧（5 秒宽限），超时才退软解；proactive 重连线程。
6. `mmap` 必须 `trackfd=False`（否则每个会话 dup 一次设备 fd）。
7. 会话 `close()` 先放设备再 join 线程（`cannot join current thread` 会漏设备 fd）。

### 试过但**不可用/无收益**的

| 尝试 | 结果 |
|---|---|
| 输出端口声明 **NV12**（`hwdec.conf: port_format=nv12`） | **有害**：UU 启动即崩、连设备列表都进不去（应用启动时做解码能力探测）。**不要再试。** |
| 输出端口 `slice=flat`（sliceHeight 报成帧高） | 未验证到（被 NV12 的崩溃打断） |
| 补发 `OMX_EventPortSettingsChanged` | 技术上可行（`data1` 必须是端口号 1，否则 ACodec `CHECK_EQ` 崩溃），但夹具延迟 33.7ms → **270ms**，且真机 03:11 那次没有它也能出画面 → 判定不是缺失环节 |
| 4K（3840×2160） | 真机上 UU 配置出 4K 解码器后，**它自己的 GLES 渲染器句柄为 0**（`StreamerSurfaceView: current native gles renderer handle is zero`）且 0 AU 送入 → 黑屏。**未证实**是 4K 本身的问题（宿主/客户机夹具 4K30 都是 120/120 帧）。 |

### 关键事实：它曾经在真机上可用

2026-10-17 03:11–03:19 的 UU 会话（1080p30 HEVC、I420、sliceHeight=1088、无 NV12/PSC）
**确实出过画面**（11507 帧、30fps），用户反馈「硬编/硬解 H265、各清晰度可用、偶发绿屏」。
之后为修「正在传输画面」的改动让渲染交接失效，至今没定位到具体是哪一处
（代码没有版本库，无法直接回到那个构建）。**下一步应当按改动逐项二分**（每次只回退一处）：
先回退「pts 不清零」→ 再回退「提前上报几何」→ 再回退「坏帧过滤」，用真机 UU 验证渲染。

### 最终结论（2026-10-18，实测）

| 项 | 结论 |
|---|---|
| **I420 声明（原样、不改端口格式）** | ✅ **可用**：1080p 与「原画」（用户实测 4K 档）都能出画面；桥侧 1:1 回帧、0 丢帧 |
| **声明 NV12（port_format=nv12）** | ❌ **不可用且有破坏性**：UU 启动即崩（连设备列表都加载不到）——应用启动时会做解码能力探测，被"假 NV12"打崩。**不要再试** |
| 4K 渲染 | ✅ 在 I420 路径下可用（之前 4K 黑屏是 NV12 实验造成的，不是 4K 本身） |
| 偶发绿屏 | 桥会丢弃驱动标记 `ERROR` 的失败帧与疑似空帧，实测 `帧长度不符 0 次`；绿屏是否完全消失待长期观察 |
| 客户机解码 CPU | 软解 ~79% 单核 → 硬解 ~26% 单核（1080p）；端到端延迟 ~100ms → **33.7ms** |

### 当前设备状态（本次收尾时）

* overlay 里是 **AVC/HEVC 的 I420 版 shim**（无 `hwdec.conf`），宿主桥手动运行中；
* 桥未做成开机自启：**桥不在时 shim 自动回退原版软解**，所以不会影响日常使用，只是没有硬解；
* 低延迟补丁（`system/lib64/libstagefright.so`）保留 —— 那是本仓库的主线成果。

### 当前设备状态（已回滚，可用）

overlay 与原版一致（`libstagefright_soft_avcdec.so` = `acdbc164`、`_hevcdec.so` = `563bd907`），
bridge 未运行，`hwdec.conf` 已删，低延迟补丁仍在。UU 走原版软解，正常可用。

## 二、架构

```
┌─ Lepton 客户机（Android 容器）──────────────┐        ┌─ Armada 宿主 ─────────────────────┐
│ App (UU 远程)                                │        │                                   │
│   ↓ MediaCodec / ACodec                      │        │  hwdec_bridge.py（常驻）          │
│   ↓ OMX                                      │        │    ├─ hwdec_v4l2.py（自写 V4L2）  │
│ media.codec 进程                             │        │    │    └─ /dev/video0 (qcom-iris)│
│   └─ /vendor/lib64/libstagefright_            │        │    └─ 帧队列 → sender 线程         │
│        soft_avcdec.so  ← 我们的 shim         │  unix  │                                   │
│        ├─ EmptyThisBuffer：压缩帧 ───────────┼─socket─┼─→ AU → ffmpeg stdin               │
│        ├─ FillThisBuffer：登记空闲输出缓冲    │        │                                   │
│        ├─ 收到 NV12 帧 → 转 I420 → 灌进缓冲   │←───────┼── FRAME（12B 头 + NV12 数据）   │
│        └─ FillBufferDone 回调给 ACodec       │        │                                   │
│ 原库改名：libsf_avcdec_orig.so（软解回退用）  │        │                                   │
└──────────────────────────────────────────────┘        └───────────────────────────────────┘
```

- **宿主解码后端**（`--decoder` 可选）：
  - `v4l2`（默认）：`hwdec_v4l2.py` 自己驱动 `/dev/video0`，按 AUD 切好 AU 直接 QBUF，
    省掉 ffmpeg 的 parser/muxer 两层（实测宿主侧 AU→帧 ~7 ms，ffmpeg 是 168 ms）。
  - `ffmpeg`：旧的 `h264_v4l2m2m` 管线，留作对照/兜底。
- **协议**（unix socket，`/var/home/armada/Downloads/hwdec.sock`，两侧共享目录）
  - 客户机 → 宿主：`HELLO(8B)` → `SIZE(frame_size,w,h, 16B)` → 若干 `AU(12B头+压缩数据)` → `EOS(8B)` / `FLUSH(8B)`
  - 宿主 → 客户机：若干 `FRAME(12B头+NV12)`；`flags=1` 表示 EOS
- **回退**：桥连不上 / 发送失败 → 该组件实例整体走原库软解，App 行为不变。
  所以「桥没启动」= 现状软解，不会把用户的 UU 远程弄坏。
- **背压**：桥侧帧队列有界（满了丢最旧帧，低延迟直播里这是正确行为），
  客户机侧输出缓冲不够时也丢旧帧，任何一侧慢都不会把另一侧堵死。

---

## 三、部署

```bash
cd lepton-fix/hwdec

# 1) 编译 shim（用 Zig + NDK sysroot，见 component/build.sh）
bash component/build.sh

# 2) 部署到 Lepton rootfs overlay（原子替换 + 自动重建容器）
bash deploy-shim.sh                     # 默认 192.168.28.112，可用 RESTART=0 跳过重建

# 3) 在宿主启动桥（保持这个 SSH 会话 / 用 systemd 或 nohup 常驻）
ssh armada@192.168.28.112 'python3 -u ~/Downloads/hwdec_bridge.py --verbose'
# 或本机直接跑： python3 hwdec_bridge.py --verbose
```

部署后 overlay 目录内容：

```
~/.local/share/lepton-armada/tool/images/rootfs_overlay/vendor/lib64/
├── libsf_avcdec_orig.so            ← 原 libstagefright_soft_avcdec.so 改名保留（shim dlopen 它）
└── libstagefright_soft_avcdec.so   ← 我们的 shim（权限必须 755）
```

**回滚**：

```bash
rm ~/.local/share/lepton-armada/tool/images/rootfs_overlay/vendor/lib64/libstagefright_soft_avcdec.so \
   ~/.local/share/lepton-armada/tool/images/rootfs_overlay/vendor/lib64/libsf_avcdec_orig.so
# 然后重建容器（lepton kill dev && lepton start dev）
# ⚠ 不要动 system/lib64/libstagefright.so —— 那是「低延迟补丁」，与本桥无关
```

只停硬解、保留 shim（不改任何文件）：**杀掉宿主桥进程**即可，新起的解码组件会自动回退软解。

---

## 四、验证

```bash
# 客户机侧基准（走 App 同一条 MediaCodec 路径）
zig cc ... -o guest_latbench guest_latbench.c     # 编译命令见 component/build.sh 同款
T=~/.local/share/lepton-armada/tool/lepton
$T exec dev sh -c "cp /var/home/armada/Downloads/guest_latbench /data/local/tmp/lb; chmod 755 /data/local/tmp/lb;
  /data/local/tmp/lb /var/home/armada/Downloads/test_aud.h264 1 33333 /var/home/armada/Downloads/out.i420"
#   ↑ low-latency=1   ↑ 33ms 实时节奏    ↑ dump 第 1 帧

# 宿主侧协议自测（不经过客户机，验证桥本身）
python3 bridge_selftest.py /var/home/armada/Downloads/test_aud.h264 --pace 0.033

# 观察点
$T exec dev logcat -s ArmadaHwDec:*     # shim 日志：端口格式、AU/帧计数、回退原因
```

判定硬解生效：logcat 出现

```
ArmadaHwDec: 已连上硬解桥 /var/home/armada/Downloads/hwdec.sock（fd=6）
ArmadaHwDec: 数据通路 = 宿主硬解
ArmadaHwDec: 输出端口: 1280x720 stride=1280 sliceHeight=720 colorFormat=0x13 -> I420（做 NV12→I420 转换）
ArmadaHwDec: 硬解帧 #1 -> 输出缓冲 1382400 字节 pts=0
```

---

## 五、踩过的坑（都已在代码里修掉）

**宿主 ffmpeg**

| 现象 | 原因 / 解法 |
|---|---|
| 60 帧只出来 3 帧（`drop=58`） | 不加 `-fps_mode passthrough` 时 rawvideo 输出按 25fps CFR 去重 → 必须加 |
| 首帧要等约 52 个 AU（≈1.6MB / 1.8s） | ffmpeg 默认输入探测缓冲 → 加 `-probesize 32 -analyzeduration 0` |
| 首帧仍要等约 11 个 AU | 解码器重排缓冲 → 再加 `-flags low_delay`（首帧降到 3~4 个 AU） |
| 一帧都不出（要 EOF 才吐） | **不要**加 `-fflags nobuffer`（管道输入下会彻底不出帧） |
| 忘了 `-c:v h264_v4l2m2m` | 就变成软解，白忙 |

**客户机 shim（`component/shim_avcdec.cpp`）**

1. 入口符号必须是**全局命名空间**的 C++ 修饰名
   `_Z22createSoftOMXComponentPKcPK16OMX_CALLBACKTYPEPvPP17OMX_COMPONENTTYPE`
   （不能 `extern "C"`、不能放进 `namespace android`），否则插件 `dlsym` 找不到。
2. **回调必须原样回传创建时给的 `appData`**：OMXNodeInstance 用它找回自己，传 `nullptr`
   会导致客户端拿不到 `EmptyBufferDone`（现象：App 只喂了 8 个 AU 就再也喂不动）。
3. **不能在持有实例锁时调用客户端回调**：客户端可能同步回到 `FillThisBuffer`，
   自己把自己锁死（现象：出 3~4 帧后整条链全睡死）。先解锁、再回调。
4. **`Flush` / `StateSet(Idle)` 必须把扣在手里的输出缓冲还给客户端**，
   否则 `MediaCodec_stop()` 永久挂起（现象：测试进程被 timeout 杀掉、rc=124）。
5. **组件对象地址会被复用**：同一个 `OMX_HANDLETYPE` 再次出现就是旧组件已销毁，
   必须在注册新实例时回收旧实例（否则新解码器被路由到旧连接上，日志里表现为 AU 计数接着涨）。
6. **OMX 输出缓冲的格式是 I420（`colorFormat=0x13`），不是 NV12**：
   宿主给的是 NV12，必须自己拆成 I420（Y 逐行拷、UV 拆两个平面），否则 App 颜色全错。
   实测转完以后与软解输出**逐字节一致**。
7. 输出缓冲是**普通内存**（`nAllocLen=1382400`，非 native handle），所以 v1 是 memcpy 而非零拷贝。
8. `overlay` 文件权限必须 **755**（否则解码服务 dlopen 失败，`video/avc` 会从解码器列表里消失）。

**自写 V4L2 解码器（`hwdec_v4l2.py`，本轮新增）**

11. **必须处理 DRC（动态分辨率变化）**：第一帧之后驱动会回一个 `bytesused=0` 且带
    `V4L2_BUF_FLAG_LAST` 的 capture 缓冲（常常还带 ERROR），此时必须
    `STREAMOFF(capture)` → `REQBUFS(capture,0)` → `G_FMT/S_FMT/REQBUFS/QBUF/STREAMON`，
    然后继续喂。**不做这一步一帧都出不来** —— 之前两个原型就是卡在这里。
12. `V4L2_DEC_CMD_START` 在首次 IPSC 会返回 **EBUSY**，但**不影响出帧**（有新输入驱动就继续解），
    不要把它当失败。
13. capture 的 geometry 是驱动给的：720p 码流得到 **1280x736**（高度 16 对齐），4K 得到 3840x2176；
    发给客户机前要按可见 w×h 重新拼（Y 和 UV 分两次拷）。
14. **Python `mmap` 默认 `trackfd=True` 会 dup 一次设备 fd**：会话关掉后要等 GC 才释放，
    表现为 `ls /proc/<pid>/fd | grep video0` 一直涨。**必须 `trackfd=False`**。
15. 解码引擎不是线程安全的：喂数据（转发线程）和收帧/DRC 重建（取帧线程）会同时动 capture 队列
    （实测 4K 时 `QBUF capture` 报 EINVAL）→ 加锁；但 **`select` 必须放在锁外**，
    否则会把喂数据那一侧堵住一个 timeout。
16. 会话 `close()` **务必先放设备再 join 线程**：从取帧线程里 join 自己会抛
    `RuntimeError: cannot join current thread`，异常打断了后面的 `dec.close()` → 设备 fd 泄漏，
    而且残留实例会占住硬件解码器，害得后面所有会话都拿不到缓冲。

**部署**

9. overlay 是**逐文件 bind mount**：改完必须**重建容器**新 inode 才可见。
10. **绝对不要就地覆盖正在被 mmap 的 .so**（`scp` 直接覆盖同一 inode）：
    正在运行的 `media.codec` 的映射会被改写，GOT 里读到新文件的 0 → 下一次调用
    `SIGSEGV, pc=0x0`，整个 OMX 服务崩掉、解码器列表变空。→ 用 `deploy-shim.sh`（临时文件 + `mv`）。

---

## 六、延迟拆解（为什么必须干掉 ffmpeg）

用 `ff_pipe_latency.py`（发送线程按真实帧率喂 AU + 读线程连续收帧，**不含**跨容器传输）
把三种解码方式单独量出来，看的是「固定的帧数」：

| 解码方式 | 720p30 | 1080p60 | 结论 |
|---|---|---|---|
| `ffmpeg -c:v h264_v4l2m2m` | 102.2 ms（≈3.1 帧） | 73.0 ms（≈4.4 帧） | parser 等下一帧 + muxer/AVIO 两层 |
| 同机 ffmpeg **软解**对照 | 75.4 ms（≈2.3 帧） | 40.5 ms（≈2.4 帧） | 进程内解码没有传输层 |
| **自写 V4L2（`hwdec_v4l2.py`）** | **~7 ms**（AU→帧，含 socket 往返） | ~7 ms | 只剩解码器本身的一帧延迟 |

所以本轮的路线是：**不用 ffmpeg，自己按 AUD 切帧直接 QBUF / DQBUF**。
调 ffmpeg 参数（`-num_output_buffers`、`-num_capture_buffers`、`-threads`）全部无效，
队列深度也不是瓶颈（客户机待投递队列 4→1、桥帧队列 4→2 后延迟无变化）。

剩下的可选优化（都不影响当前结论）：

1. **共享内存环形缓冲（memfd + `SCM_RIGHTS`）替代 socket 传帧**：省掉客户机侧一次拷贝，
   预期再降几毫秒；`hwdec_host.py` + `guest_shm.c` 已把 fd 传递与 mmap 读帧验证过。
2. **真零拷贝**：宿主 capture 缓冲已经用 `EXPBUF` 拿到 dmabuf fd（每帧 4 个），
   可以直接把 fd 传给客户机、包成 native handle 交给 OMX，省掉宿主打包 + 客户机转换两次拷贝。
   需要客户机输出端口声明 native buffer（当前是普通内存，`nAllocLen=1382400`）。
3. **HEVC/VP9/AV1**：把 shim 换到对应组件（`libstagefright_soft_hevcdec.so` 等）、
   `--decoder v4l2` 里换 `PIX_HEVC` 即可；客户机软解 HEVC 更慢，那一档收益更大。

## 七、文件清单

| 文件 | 作用 |
|---|---|
| `component/shim_avcdec.cpp` | 客户机侧替换组件（硬解代理 + 自动回退 + NV12→I420） |
| `component/build.sh` | 用 Zig + NDK sysroot 编译成 bionic aarch64 共享库 |
| `deploy-shim.sh` | 原子部署到 overlay + 重建容器 |
| `hwdec_bridge.py` | 宿主侧常驻桥（多连接、帧队列 + sender 线程、EOS/FLUSH、会话/设备 fd 上限与回收） |
| `hwdec_v4l2.py` | **自写 V4L2 有状态解码引擎**（DRC 握手、按可见 w×h 打包、线程安全） |
| `v4l2dec_bench.py` | 自写解码器的独立延迟基准（不经 socket/客户机） |
| `guest_latbench.c` | 客户机侧延迟/CPU 基准（沿用已验证可靠的喂收节奏） |
| `guest_hwbench.c` | 另一种（激进喂入）客户机侧基准，用于压力/丢帧观察 |
| `bridge_selftest.py` | 宿主侧协议自测（不经过客户机） |
| `ff_latency_probe.py` | ffmpeg 参数对首帧缓冲深度影响的探针（本文档 §五 的依据） |
| `probe_v4l2.py` / `v4l2m2m.py` / `host_decode.py` | V4L2 能力探查 / ctypes M2M 层 / 自写解码器原型 |
| `hwdec_host.py` | v1 原型：memfd 环形缓冲 + `SCM_RIGHTS`（零拷贝路线的验证） |
| `STATUS.md` | 更详细的事实清单与上手笔记 |
| `evidence/` | 硬解/软解同帧 PNG 证据 |
