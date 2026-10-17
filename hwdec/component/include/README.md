# 第三方头文件

`openmax/` 下的 OMX IL 头文件来自 **Khronos Group 的 OpenMAX IL 规范**（AOSP 随 Android 分发的
版本，Apache-2.0）。这里只作编译 `shim_avcdec.cpp` 之用，未做修改。

媒体框架（`android::SoftOMXComponent`）只用了**前向声明**，因此不需要 AOSP 的 stagefright 头文件。
