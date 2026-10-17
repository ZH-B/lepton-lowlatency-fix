# Lepton 硬解桥 —— 进展与交接（STATUS）

> 目标：让 Lepton 客户机里的应用（UU 远程等）用上宿主 qcom-iris 的**硬件解码**。
> 更新：2026-10-17
>
> **当前状态：M2/M4/M5 全部完成 —— 硬解通路打通，并且两头都赢。**
>
> 最终实测（详见 [`README.md`](README.md) §一，那是最终口径）：
>
> | 场景 | 硬解桥 | 内置软解 | 延迟 | CPU |
> |---|---|---|---|---|
> | 720p30 | 33.6 ms / 190 ms | 101.9 ms / 1030 ms | **3.0× 更低** | **−82%** |
> | 1080p60 | 16.9 ms / 790 ms | 51.9 ms / 6090 ms | **3.1× 更低** | **−87%** |
> | 4K30 | 33.8 ms / 1530 ms | 101.2 ms / 4820 ms | **3.0× 更低** | **−68%** |
>
> 全帧通过、输出与软解**逐字节一致**、桥不在时自动回退软解（断开后还能自动重连）。
>
> **关键一步：不用 ffmpeg。** ffmpeg 的 raw h264 parser 必须等到下一帧起始码才肯吐当前帧
> （固定多 1 帧），rawvideo muxer + AVIO 再叠一层 —— 实测 ffmpeg 管线 102 ms / 3 帧，
> 同机软解只要 2.3 帧，所以「零成本传输」也救不回来。改成 `hwdec_v4l2.py` 自己按 AUD 切帧
> 直接 QBUF / DQBUF 之后，宿主侧 AU→帧掉到 ~7 ms，客户机端到端就只剩「一帧」的量级。

## 一、已验证的事实（都可复现）

| 项 | 结论 | 证据/命令 |
|---|---|---|
| 宿主硬解引擎 | ✅ `/dev/video0` = `iris_driver` "Iris Decoder"，M2M_MPLANE；支持 H264/HEVC/VP9/AV1 → NV12/P010/Q08C/Q10C | `python3 probe_v4l2.py /dev/video0` |
| 宿主硬解可用性 | ✅ ffmpeg 自带 `h264_v4l2m2m` 直接驱动它；1080p30 5s：硬解 utime **0.006s** vs 软解 **0.481s** | `ffmpeg -c:v h264_v4l2m2m -i x.h264 -f null -` |
| 缓冲接口 | ✅ `EXPBUF` 能导出解码输出的 **dmabuf fd**；`REQBUFS mem=DMABUF` 也接受导入外部 dmabuf | `probe_v4l2.py` 第 4/5 节 |
| 跨容器 fd 传递 | ✅ 客户机（Android 容器内）连宿主 unix socket，**收到宿主 memfd 并读到内容** | `host_sock.py` + `guest_test` |
| **端到端取帧** | ✅ ffmpeg 硬解 → NV12 写入 memfd 环形缓冲 → 客户机 mmap → 画面正确（testsrc2 图案） | `hwdec_host.py` + `guest_shm`，证据图 `evidence/hwdec-frame-testsrc2-1280x720.png` |
| 客户机解码夹具 | ✅ NDK AMediaCodec 按类型解码，**low-latency=1 时 configure OK**（反证我们的 libstagefright 补丁在生效）；软解基线 720p：60 帧 / 168 ms（356 fps） | `guest_decode_test`（见 §三 编译法） |
| **拦截点** | ✅ 客户机 `OMX.google.h264.decoder` 由 `vendor/lib64/libstagefright_soft_avcdec.so` 实现，入口是 **C++ 修饰名** `_Z22createSoftOMXComponentPKcPK16OMX_CALLBACKTYPEPvPP17OMX_COMPONENTTYPE` | 见 §二 |
| **组件注入已验证** | ✅ 自研 shim 顶替该 .so 后，解码服务**加载并调用**了它（logcat `ArmadaHwDec: shim 生效`），转发给改名的原库后解码行为不变（330 fps） | 见 §二 |

## 二、组件注入（已完成的部分）

部署（overlay，全部可回滚）：

```
~/.local/share/lepton-armada/tool/images/rootfs_overlay/vendor/lib64/
├── libsf_avcdec_orig.so               ← 原 libstagefright_soft_avcdec.so 改名保留
└── libstagefright_soft_avcdec.so      ← 我们的 shim（必须先 chmod 755！）
```

- 源码：`component/shim_avcdec.cpp`；构建：`bash component/build.sh`（Zig + NDK sysroot → bionic aarch64 .so）
- **踩过的坑**：
  1. 入口符号必须是**全局命名空间**的 C++ 函数（不能 `extern "C"`、不能放进 `namespace android`），否则修饰名对不上，插件 `dlsym` 失败；
  2. overlay 文件权限必须是 755（root/armada 之外的服务进程要能读，否则 dlopen 失败，且**解码器会从列表里消失**）；
  3. 改完必须**重建容器**（overlay 只在容器创建时挂载）。

回滚：
```bash
rm ~/.local/share/lepton-armada/tool/images/rootfs_overlay/vendor/lib64/libstagefright_soft_avcdec.so \
   ~/.local/share/lepton-armada/tool/images/rootfs_overlay/vendor/lib64/libsf_avcdec_orig.so
# 注意：system/lib64/libstagefright.so 是「低延迟补丁」，不要一起删
```

## 三、命令速查

```bash
# 客户机解码夹具（NDK MediaCodec，和 App 同一条路径）
SYS=/root/opt/ndk/sysroot; D=$SYS/usr/lib/aarch64-linux-android/29
/root/opt/zig/zig cc -target aarch64-linux-android -D__ANDROID_API__=29 -nostdinc \
  -isystem $SYS/usr/include -isystem $SYS/usr/include/aarch64-linux-android \
  -O2 -fPIE -c -o /tmp/gdt.o guest_decode_test.c
/root/opt/zig/zig ld.lld -m aarch64linux -pie --dynamic-linker /system/bin/linker64 \
  -o /tmp/guest_decode_test $D/crtbegin_dynamic.o /tmp/gdt.o \
  -L$D -lmediandk -llog -lc -lm -ldl $D/crtend_android.o

# 在客户机里跑（越权可读 /var/home/armada/Downloads 共享目录）
T=~/.local/share/lepton-armada/tool/lepton
$T exec dev sh -c "cp /var/home/armada/Downloads/guest_decode_test /data/local/tmp/ && \
  chmod 755 /data/local/tmp/guest_decode_test && \
  /data/local/tmp/guest_decode_test /var/home/armada/Downloads/test_aud.h264 1"
```

## 四、下一步（M2 收尾 → M4/M5）

1. **在 shim 里做真·卸载**：包装 `OMX_COMPONENTTYPE`
   - 拦 `EmptyThisBuffer`：把压缩帧交给宿主桥（unix socket + memfd 环形缓冲，见 `hwdec_host.py` 协议）；
   - 拦输出：拿到宿主硬解帧后写进 OMX 输出缓冲（native handle → `GraphicBufferMapper::lock`），再回调 `FillBufferDone`；
   - `SetConfig/GetConfig`：`OMX_IndexConfigLowLatency` 返回成功；`GetParameter` 里的能力项按需支持。
   - 建议先用 shim 记录 ACodec 的完整调用序列（日志），再照着实现。
2. **验证**：用 `guest_decode_test` 对比「软解基线 356 fps / 首帧 46 ms」与「硬解桥」的 CPU 占用与延迟；可用 PGM 导出核对像素。
3. **M3**：确认 `media_codecs.xml` / `ccodec` 无需改动（走 OMX 路径即可，因为替换的是组件实现本身）。
4. **M5**：UU 远程实测（远控会话），并写入部署/回滚文档。

### 已知未解

- **自写 V4L2 有状态解码**（零拷贝路线）卡在 `qcom_iris` 的 DRC/DRAIN 握手：驱动会回 LAST 空帧 + EOS，
  而首次 source change（FIRST_IPSC）按驱动逻辑不允许 `V4L2_DEC_CMD_START`（返回 EBUSY）。
  ffmpeg 的 `v4l2_m2m` 能正确处理，所以 v1 走 ffmpeg；零拷贝留待后续（dmabuf 导出与 fd 传递已分别验证 ✓）。
