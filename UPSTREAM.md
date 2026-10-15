# Upstream report notes

This repo ships a **workaround**. The underlying defects are upstream; below is a ready-to-use
write-up if you want to report them.

---

## A. Valve Lepton / Waydroid (guest image)

**Title**: Android apps that request low-latency video decoding crash inside Lepton guests

**Environment**

- Host: Armada OS (AYN Odin 3, aarch64), Lepton v2.8.14 (rootfs v2.8.11) and v3.0.5 (rootfs v3.0.3)
- Guest: Android 12-ish rootfs shipped by the Lepton depot (`~/.local/share/Steam/steamapps/common/Lepton`)
- App: NetEase UU Remote (Android client, `com.netease.uuremote`)

**Summary**

The guest exposes **only software video decoders** (`OMX.google.*`; `vendor/etc/media_codecs_google_video.xml`).
There are no hardware/vendor codecs, and no Codec2 components either (`/apex` has no
`com.android.media.swcodec`). In addition, the Lepton launcher sets `debug.stagefright.ccodec=0`,
which forces the legacy ACodec/OMX path.

As a result, when an app puts `low-latency=1` into its `MediaFormat`, `ACodec::setLowLatency()` issues
`OMX_IndexConfigLowLatency` (0x6f800006) — an OEM extension that only hardware decoders implement —
the software decoder rejects it (`err -1010`), and `MediaCodec.configure()` fails.

**Please consider**

1. Shipping (or documenting how to enable) codecs that accept `OMX_IndexConfigLowLatency`, or
2. Making `ACodec::setLowLatency()` on pure software components non-fatal when nothing else can serve
   the request (so `configure()` does not fail), or
3. Documenting that apps requesting low-latency decoding are unsupported in this guest.

**Observed fallout**: apps that do not handle the failed `configure()` can take down the whole
Lepton container.

---

## B. NetEase UU Remote (Android client / `libstreamer.so`)

**Title**: Crash when `MediaCodec.configure()` fails (low-latency request) on devices without hardware decoders

**Summary**

`libstreamer.so` sets `MediaFormat` key `low-latency` while selecting/benchmarking a decoder at session
startup. On devices where only software decoders exist, `configure()` fails. The failure is not handled:
the client keeps operating on the same codec from other threads, and the platform's
failure/reset path dereferences null — the process dies with `SIGSEGV` in `CodecLooper`.

**Expected behaviour**: catch the `configure()` failure (or check `KEY_LOW_LATENCY` support first) and
fall back to a non-low-latency configuration.

**Log excerpt (guest logcat)**

```
E ACodec  : decoder can not set low-latency to 1 (err -1010)
E ACodec  : [OMX.google.h264.decoder] configureCodec returning error -1010
E ACodec  : Unable to instantiate a decoder for type 'video/avc' with err 0xfffffffe.
E MediaCodec: Codec reported err 0xfffffffe, actionCode 0, while in state 3
E MediaCodec: configure failed with err 0xfffffffe, resetting...
F libc    : Fatal signal 11 (SIGSEGV), code 1 (SEGV_MAPERR), fault addr 0x0 in tid 1305 (CodecLooper)
```

---

## C. AOSP media framework

**Title**: Null dereference in `CodecLooper` after a failed `MediaCodec::configure()`

The failure path (`configureCodec` error → `MediaCodec` reset/error handling) can race with the app
touching the same `MediaCodec` instance, ending in a null dereference on the `CodecLooper` thread.
Reproducible with `KEY_LOW_LATENCY` on a software OMX H.264 decoder
(`frameworks/av/media/libstagefright/ACodec.cpp`, `setLowLatency()`).

---

## Workaround in this repo

Patch the guest's `libstagefright.so` so `ACodec::setLowLatency()` returns `OK` immediately —
`configure()` then succeeds and the decoder runs in normal (non-low-latency) mode.
See `README.md`.
