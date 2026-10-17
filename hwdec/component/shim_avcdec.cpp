// shim_avcdec.cpp —— 顶替 /vendor/lib64/libstagefright_soft_{avc,hevc}dec.so 的硬解代理组件。
//
// 本版 = 「真机上出过画面」那版的行为（阶段 A）：
//   * 帧参数（SIZE）只在 FillThisBuffer 里上报
//   * Flush 清本地队列 + pts 队列，通知宿主重启解码器，并把扣着的输出缓冲还给客户端
//   * 断开后直接转发原库软解，下一个 AU 再尝试重连
//   * 不发 OMX_EventPortSettingsChanged，无主动重连线程
// 唯一新增：宿主标记为坏帧（绿屏来源）时，**重发上一帧**给上层。
//
// 入口符号必须是全局命名空间的 C++ 修饰名（SoftOMXPlugin 用 dlsym 找）。
#include <android/log.h>
#include <dlfcn.h>
#include <errno.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <unistd.h>

#include <openmax/OMX_Core.h>
#include <openmax/OMX_Component.h>

#define LOG_TAG "ArmadaHwDec"
#define LOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)
#define LOGW(...) __android_log_print(ANDROID_LOG_WARN, LOG_TAG, __VA_ARGS__)
#define LOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)

#ifndef ORIG_LIB
#define ORIG_LIB "/vendor/lib64/libsf_avcdec_orig.so"
#endif
#define BRIDGE_SOCK "/var/home/armada/Downloads/hwdec.sock"
#define MANGLED_CREATE \
    "_Z22createSoftOMXComponentPKcPK16OMX_CALLBACKTYPEPvPP17OMX_COMPONENTTYPE"

#define HELLO_MAGIC 0x48454C4F
#define CODEC_MAGIC 0x434F4443
#define SIZE_MAGIC 0x53495A45
#define AU_MAGIC 0x41554431
#define EOS_MAGIC 0x454F5331
#define FLUSH_MAGIC 0x464C5331
#define FRAME_MAGIC 0x46524D31
#define FRAME_FLAG_EOS 1u
#define FRAME_FLAG_REPEAT 2u

#define OMX_INDEX_CONFIG_LOW_LATENCY ((OMX_INDEXTYPE)0x6F800006)

#define MAX_FREE_BUFS 24
#define MAX_PENDING_FRAMES 1
#define MAX_PTS_FIFO 128
#define MAX_BRIDGES 8
#define RECONNECT_INTERVAL_MS 1000

namespace android {
class SoftOMXComponent;
}

typedef android::SoftOMXComponent *(*CreateFn)(const char *name,
                                               const OMX_CALLBACKTYPE *callbacks,
                                               OMX_PTR appData,
                                               OMX_COMPONENTTYPE **component);

static CreateFn load_orig() {
    static CreateFn fn = nullptr;
    static bool tried = false;
    if (!tried) {
        tried = true;
        void *h = dlopen(ORIG_LIB, RTLD_NOW | RTLD_LOCAL);
        if (h == nullptr) {
            LOGE("dlopen(%s) 失败: %s", ORIG_LIB, dlerror());
            return nullptr;
        }
        fn = reinterpret_cast<CreateFn>(dlsym(h, MANGLED_CREATE));
        if (fn == nullptr)
            LOGE("dlsym 失败: %s", dlerror());
    }
    return fn;
}

namespace {

struct Frame {
    uint32_t len;
    uint32_t flags;
    uint8_t *data;
};

struct Bridge {
    OMX_HANDLETYPE handle = nullptr;
    OMX_PTR appData = nullptr;
    OMX_COMPONENTTYPE orig{ };
    OMX_CALLBACKTYPE appCb{ };
    bool haveAppCb = false;

    pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
    pthread_t reader = 0;
    bool readerStarted = false;
    volatile bool dead = false;

    int fd = -1;
    bool connected = false;
    bool everConnected = false;
    bool finished = false;
    int64_t lastTryMs = 0;
    uint32_t frameSize = 0;
    bool sizeSent = false;

    bool fmtQueried = false;
    bool planarOut = false;
    uint32_t fmtWidth = 0, fmtHeight = 0, fmtStride = 0, fmtSliceHeight = 0, fmtColor = 0;

    uint32_t codec = 1;

    OMX_BUFFERHEADERTYPE *freeBufs[MAX_FREE_BUFS] = { };
    int nFree = 0;
    Frame pending[MAX_PENDING_FRAMES];
    int nPending = 0;
    bool eosPending = false;

    uint8_t *lastFrame = nullptr;    // 上一帧（用于坏帧重发）
    uint32_t lastLen = 0;
    uint32_t repeats = 0;

    int64_t ptsFifo[MAX_PTS_FIFO] = { };
    int ptsHead = 0, ptsTail = 0, ptsCount = 0;

    uint64_t ausSent = 0, framesRecv = 0, framesOut = 0;
    uint32_t sizeMismatch = 0;
    int64_t createSeq = 0;
    uint32_t cfgLowLatency = 0;

    bool hwMode = false;
    bool modeDecided = false;

    static int64_t now_ms() {
        struct timeval tv;
        gettimeofday(&tv, nullptr);
        return (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
    }

    void ptsPush(int64_t pts) {
        if (ptsCount < MAX_PTS_FIFO) {
            ptsFifo[ptsTail] = pts;
            ptsTail = (ptsTail + 1) % MAX_PTS_FIFO;
            ptsCount++;
        }
    }

    int64_t ptsPop() {
        int64_t v = 0;
        if (ptsCount > 0) {
            v = ptsFifo[ptsHead];
            ptsHead = (ptsHead + 1) % MAX_PTS_FIFO;
            ptsCount--;
        }
        return v;
    }

    void resetQueuesLocked() {
        nFree = 0;
        for (int i = 0; i < nPending; i++)
            free(pending[i].data);
        nPending = 0;
        eosPending = false;
        ptsHead = ptsTail = ptsCount = 0;
    }
};

static Bridge *g_bridges[MAX_BRIDGES] = { };
static pthread_mutex_t g_regLock = PTHREAD_MUTEX_INITIALIZER;
static Bridge *g_last = nullptr;
static int64_t g_seq = 0;

static void bridge_shutdown(Bridge *b);
static void session_reset(Bridge *b);
static bool ensure_hw(Bridge *b);

Bridge *bridge_find(OMX_HANDLETYPE h) {
    pthread_mutex_lock(&g_regLock);
    Bridge *r = nullptr;
    for (int i = 0; i < MAX_BRIDGES; i++)
        if (g_bridges[i] && g_bridges[i]->handle == h) {
            r = g_bridges[i];
            break;
        }
    if (r == nullptr)
        r = g_last;
    pthread_mutex_unlock(&g_regLock);
    return r;
}

bool bridge_register(Bridge *b) {
    Bridge *evicted = nullptr;
    pthread_mutex_lock(&g_regLock);
    b->createSeq = ++g_seq;
    for (int i = 0; i < MAX_BRIDGES; i++) {
        if (g_bridges[i] && g_bridges[i]->handle == b->handle) {
            evicted = g_bridges[i];
            g_bridges[i] = nullptr;
            LOGI("handle %p 被复用，回收旧实例", (void *)b->handle);
            break;
        }
    }
    int slot = -1, oldest = 0;
    for (int i = 0; i < MAX_BRIDGES; i++) {
        if (g_bridges[i] == nullptr) {
            slot = i;
            break;
        }
        if (g_bridges[i]->createSeq < g_bridges[oldest]->createSeq)
            oldest = i;
    }
    if (slot >= 0)
        g_bridges[slot] = b;
    else {
        if (evicted == nullptr)
            evicted = g_bridges[oldest];
        g_bridges[oldest] = b;
    }
    g_last = b;
    pthread_mutex_unlock(&g_regLock);
    if (evicted) {
        bridge_shutdown(evicted);
        delete evicted;
    }
    return true;
}

void bridge_unregister(Bridge *b) {
    pthread_mutex_lock(&g_regLock);
    for (int i = 0; i < MAX_BRIDGES; i++)
        if (g_bridges[i] == b)
            g_bridges[i] = nullptr;
    if (g_last == b)
        g_last = nullptr;
    pthread_mutex_unlock(&g_regLock);
}

bool write_all(int fd, const void *buf, size_t n) {
    const uint8_t *p = (const uint8_t *)buf;
    while (n > 0) {
        ssize_t w = send(fd, p, n, MSG_NOSIGNAL);
        if (w <= 0) {
            if (w < 0 && errno == EINTR)
                continue;
            return false;
        }
        p += w;
        n -= (size_t)w;
    }
    return true;
}

bool read_all(int fd, void *buf, size_t n) {
    uint8_t *p = (uint8_t *)buf;
    while (n > 0) {
        ssize_t r = recv(fd, p, n, 0);
        if (r <= 0) {
            if (r < 0 && errno == EINTR)
                continue;
            return false;
        }
        p += r;
        n -= (size_t)r;
    }
    return true;
}

void nv12_to_i420(const uint8_t *src, uint8_t *dst, uint32_t w, uint32_t h,
                  uint32_t stride, uint32_t sliceHeight) {
    const uint8_t *sy = src;
    const uint8_t *suv = src + (size_t)w * h;
    uint8_t *dy = dst;
    uint8_t *du = dst + (size_t)stride * sliceHeight;
    uint8_t *dv = du + (size_t)(stride / 2) * (sliceHeight / 2);
    for (uint32_t y = 0; y < h; y++)
        memcpy(dy + (size_t)y * stride, sy + (size_t)y * w, w);
    uint32_t cw = w / 2, ch = h / 2;
    for (uint32_t y = 0; y < ch; y++) {
        const uint8_t *s = suv + (size_t)y * w;
        uint8_t *u = du + (size_t)y * (stride / 2);
        uint8_t *v = dv + (size_t)y * (stride / 2);
        for (uint32_t x = 0; x < cw; x++) {
            u[x] = s[2 * x];
            v[x] = s[2 * x + 1];
        }
    }
}

// 调用者持 b->lock；不在这里回调客户端
void deliver_locked(Bridge *b, Frame &f, OMX_BUFFERHEADERTYPE **ready, int *nReady) {
    if (b->nFree == 0) {
        if (b->nPending == MAX_PENDING_FRAMES) {
            free(b->pending[0].data);
            memmove(&b->pending[0], &b->pending[1], sizeof(Frame) * (MAX_PENDING_FRAMES - 1));
            b->nPending--;
        }
        b->pending[b->nPending++] = f;
        f.data = nullptr;
        return;
    }
    OMX_BUFFERHEADERTYPE *buf = b->freeBufs[--b->nFree];

    // 坏帧（宿主标记 REPEAT）：把上一帧再送一次 → 轻微冻结，而不是绿屏
    if (f.data == nullptr && (f.flags & FRAME_FLAG_REPEAT)) {
        uint32_t n = 0;
        if (b->lastFrame && b->lastLen && buf->pBuffer && b->lastLen <= buf->nAllocLen) {
            memcpy(buf->pBuffer + buf->nOffset, b->lastFrame, b->lastLen);
            n = b->lastLen;
        }
        buf->nFilledLen = n;
        buf->nOffset = 0;
        buf->nFlags = 0;
        buf->nTimeStamp = (OMX_TICKS)b->ptsPop();
        b->repeats++;
        if (b->repeats <= 3 || b->repeats % 60 == 0)
            LOGI("坏帧 → 重发上一帧 #%u（%u 字节）", (unsigned)b->repeats, (unsigned)n);
        if (nReady && *nReady < MAX_FREE_BUFS)
            ready[(*nReady)++] = buf;
        return;
    }

    uint32_t n = f.len;
    if (b->frameSize && n != b->frameSize) {
        b->sizeMismatch++;
        if (b->sizeMismatch <= 5 || b->sizeMismatch % 100 == 0)
            LOGW("帧长度不符：%u != %u（按坏帧处理）", (unsigned)n, (unsigned)b->frameSize);
        free(f.data);
        f.data = nullptr;
        buf->nFilledLen = 0;
        buf->nOffset = 0;
        buf->nFlags = 0;
        buf->nTimeStamp = (OMX_TICKS)b->ptsPop();
        if (nReady && *nReady < MAX_FREE_BUFS)
            ready[(*nReady)++] = buf;
        return;
    }
    if (n > buf->nAllocLen)
        n = buf->nAllocLen;
    if (buf->pBuffer && f.data) {
        if (b->planarOut && b->fmtWidth && b->fmtHeight) {
            nv12_to_i420(f.data, buf->pBuffer + buf->nOffset, b->fmtWidth, b->fmtHeight,
                         b->fmtStride ? b->fmtStride : b->fmtWidth,
                         b->fmtSliceHeight ? b->fmtSliceHeight : b->fmtHeight);
        } else {
            memcpy(buf->pBuffer + buf->nOffset, f.data, n);
        }
    }
    buf->nFilledLen = n;
    buf->nOffset = 0;
    buf->nFlags = f.flags;
    buf->nTimeStamp = (OMX_TICKS)b->ptsPop();
    // 存一份「上一帧」（按组件声明的格式存，坏帧时原样重发）
    if (n > 0) {
        uint32_t keep = (b->frameSize ? b->frameSize : n);
        if (b->lastLen < keep) {
            uint8_t *p = (uint8_t *)realloc(b->lastFrame, keep);
            if (p) {
                b->lastFrame = p;
                b->lastLen = keep;
            }
        }
        if (b->lastFrame && b->lastLen >= keep && buf->pBuffer)
            memcpy(b->lastFrame, buf->pBuffer, keep);
    }
    free(f.data);
    f.data = nullptr;
    b->framesOut++;
    if (b->framesOut <= 3 || b->framesOut % 120 == 0)
        LOGI("硬解帧 #%llu -> 输出缓冲 %u 字节 pts=%lld", (unsigned long long)b->framesOut,
             (unsigned)n, (long long)buf->nTimeStamp);
    if (nReady && *nReady < MAX_FREE_BUFS)
        ready[(*nReady)++] = buf;
    else if (b->haveAppCb && b->appCb.FillBufferDone)
        b->appCb.FillBufferDone(b->handle, b->appData, buf);
}

void *reader_thread(void *arg) {
    Bridge *b = (Bridge *)arg;
    for (;;) {
        uint32_t hdr[3];
        if (!read_all(b->fd, hdr, sizeof(hdr))) {
            if (!b->dead) {
                LOGE("桥断开（已收 %llu 帧）", (unsigned long long)b->framesRecv);
                session_reset(b);
            }
            return nullptr;
        }
        if (hdr[0] != FRAME_MAGIC || hdr[1] > 32u * 1024 * 1024) {
            LOGE("桥协议错误 magic=%#x len=%u", hdr[0], hdr[1]);
            session_reset(b);
            return nullptr;
        }
        Frame f;
        f.len = hdr[1];
        f.flags = hdr[2];
        f.data = nullptr;
        if (f.len > 0) {
            f.data = (uint8_t *)malloc(f.len);
            if (f.data == nullptr || !read_all(b->fd, f.data, f.len)) {
                free(f.data);
                session_reset(b);
                return nullptr;
            }
        }

        OMX_BUFFERHEADERTYPE *ready[MAX_FREE_BUFS];
        int nReady = 0;
        pthread_mutex_lock(&b->lock);
        b->framesRecv++;
        if (f.data || (f.flags & FRAME_FLAG_REPEAT)) {
            deliver_locked(b, f, ready, &nReady);
        } else if (f.flags & FRAME_FLAG_EOS) {
            if (b->nFree > 0) {
                OMX_BUFFERHEADERTYPE *buf = b->freeBufs[--b->nFree];
                buf->nFilledLen = 0;
                buf->nOffset = 0;
                buf->nFlags = OMX_BUFFERFLAG_EOS;
                buf->nTimeStamp = (OMX_TICKS)b->ptsPop();
                LOGI("回送 EOS 输出缓冲");
                if (nReady < MAX_FREE_BUFS)
                    ready[nReady++] = buf;
            } else {
                b->eosPending = true;
            }
        }
        while (b->nPending > 0 && b->nFree > 0) {
            Frame nf = b->pending[0];
            memmove(&b->pending[0], &b->pending[1], sizeof(Frame) * (b->nPending - 1));
            b->nPending--;
            deliver_locked(b, nf, ready, &nReady);
        }
        pthread_mutex_unlock(&b->lock);

        for (int i = 0; i < nReady; i++) {
            if (b->haveAppCb && b->appCb.FillBufferDone)
                b->appCb.FillBufferDone(b->handle, b->appData, ready[i]);
        }
    }
}

void session_reset(Bridge *b) {
    pthread_mutex_lock(&b->lock);
    int fd = b->fd;
    b->fd = -1;
    b->connected = false;
    b->hwMode = false;
    b->modeDecided = false;
    b->finished = false;
    b->sizeSent = false;
    b->frameSize = 0;
    b->fmtQueried = false;
    b->lastTryMs = 0;
    b->resetQueuesLocked();
    b->ausSent = 0;
    b->framesRecv = 0;
    pthread_mutex_unlock(&b->lock);
    if (fd >= 0) {
        shutdown(fd, SHUT_RDWR);
        close(fd);
    }
}

bool bridge_connect(Bridge *b) {
    int64_t now = Bridge::now_ms();
    if (b->connected)
        return true;
    if (b->finished)
        return false;
    if (now - b->lastTryMs < RECONNECT_INTERVAL_MS)
        return false;
    b->lastTryMs = now;

    int fd = socket(AF_UNIX, SOCK_STREAM, 0);
    if (fd < 0)
        return false;
    struct sockaddr_un a;
    memset(&a, 0, sizeof(a));
    a.sun_family = AF_UNIX;
    strncpy(a.sun_path, BRIDGE_SOCK, sizeof(a.sun_path) - 1);
    if (connect(fd, (struct sockaddr *)&a, sizeof(a)) < 0) {
        if (!b->everConnected)
            LOGI("连接硬解桥失败（先用软解，稍后重试）: %s", strerror(errno));
        close(fd);
        return false;
    }
    uint32_t hello[2] = { HELLO_MAGIC, 2 };
    uint32_t codec[2] = { CODEC_MAGIC, b->codec };
    if (!write_all(fd, hello, sizeof(hello)) || !write_all(fd, codec, sizeof(codec))) {
        close(fd);
        return false;
    }
    b->fd = fd;
    b->connected = true;
    b->everConnected = true;
    b->sizeSent = false;
    pthread_create(&b->reader, nullptr, reader_thread, b);
    b->readerStarted = true;
    LOGI("已连上硬解桥 %s（fd=%d，codec=%s）", BRIDGE_SOCK, fd,
         b->codec == 2 ? "HEVC" : "AVC");
    return true;
}

void bridge_shutdown(Bridge *b) {
    pthread_mutex_lock(&b->lock);
    b->dead = true;
    int fd = b->fd;
    b->fd = -1;
    b->connected = false;
    b->resetQueuesLocked();
    if (b->lastFrame) {
        free(b->lastFrame);
        b->lastFrame = nullptr;
        b->lastLen = 0;
    }
    pthread_mutex_unlock(&b->lock);
    if (fd >= 0) {
        shutdown(fd, SHUT_RDWR);
        if (b->readerStarted)
            pthread_join(b->reader, nullptr);
        close(fd);
        b->readerStarted = false;
    }
}

bool ensure_hw(Bridge *b) {
    if (b->hwMode && b->connected)
        return true;
    if (b->finished)
        return false;
    if (b->modeDecided && !b->hwMode && (b->ausSent > 0 || b->framesRecv > 0 || b->nFree > 0))
        return false;
    bool ok = bridge_connect(b);
    if (!b->modeDecided || (ok && !b->hwMode)) {
        LOGI("数据通路 = %s", ok ? "宿主硬解" : "内置软解");
        b->modeDecided = true;
        b->hwMode = ok;
    }
    return b->hwMode && b->connected;
}

// 查询输出端口格式；帧参数只在这里（Fill 时）上报 —— 与阶段 A 一致
void ensure_geometry(Bridge *b, OMX_HANDLETYPE h, uint32_t allocLen) {
    pthread_mutex_lock(&b->lock);
    if (!b->fmtQueried) {
        b->fmtQueried = true;
        OMX_PARAM_PORTDEFINITIONTYPE def;
        memset(&def, 0, sizeof(def));
        def.nSize = sizeof(def);
        def.nPortIndex = 1;
        if (b->orig.GetParameter &&
            b->orig.GetParameter(h, OMX_IndexParamPortDefinition, &def) == OMX_ErrorNone) {
            b->fmtWidth = def.format.video.nFrameWidth;
            b->fmtHeight = def.format.video.nFrameHeight;
            b->fmtStride = def.format.video.nStride;
            b->fmtSliceHeight = def.format.video.nSliceHeight;
            b->fmtColor = (uint32_t)def.format.video.eColorFormat;
            b->planarOut = (def.format.video.eColorFormat == OMX_COLOR_FormatYUV420Planar) ||
                           (def.format.video.eColorFormat == OMX_COLOR_FormatYUV420PackedPlanar);
            LOGI("输出端口: %ux%u stride=%u sliceHeight=%u colorFormat=%#x -> %s",
                 (unsigned)b->fmtWidth, (unsigned)b->fmtHeight, (unsigned)b->fmtStride,
                 (unsigned)b->fmtSliceHeight, (unsigned)b->fmtColor,
                 b->planarOut ? "I420（做 NV12→I420 转换）" : "按原样拷贝");
        } else {
            LOGW("查询输出端口定义失败，按 I420 处理");
            b->planarOut = true;
        }
    }
    if (b->frameSize == 0 && allocLen) {
        b->frameSize = allocLen;
        LOGI("输出缓冲 %u 字节 => 告知宿主帧大小", (unsigned)allocLen);
    }
    if (!b->sizeSent && b->frameSize && b->fmtWidth && b->connected) {
        uint32_t msg[4] = { SIZE_MAGIC, b->frameSize, b->fmtWidth, b->fmtHeight };
        b->sizeSent = write_all(b->fd, msg, sizeof(msg));
        if (b->sizeSent)
            LOGI("已告知宿主帧参数：%u 字节 %ux%u", (unsigned)b->frameSize,
                 (unsigned)b->fmtWidth, (unsigned)b->fmtHeight);
    }
    pthread_mutex_unlock(&b->lock);
}

OMX_ERRORTYPE cb_EventHandler(OMX_HANDLETYPE h, OMX_PTR app, OMX_EVENTTYPE e, OMX_U32 d1,
                              OMX_U32 d2, OMX_PTR p) {
    Bridge *b = bridge_find(h);
    if (b && b->haveAppCb && b->appCb.EventHandler)
        return b->appCb.EventHandler(h, app, e, d1, d2, p);
    return OMX_ErrorNone;
}

OMX_ERRORTYPE cb_EmptyBufferDone(OMX_HANDLETYPE h, OMX_PTR app, OMX_BUFFERHEADERTYPE *buf) {
    Bridge *b = bridge_find(h);
    if (b && b->haveAppCb && b->appCb.EmptyBufferDone)
        return b->appCb.EmptyBufferDone(h, app, buf);
    return OMX_ErrorNone;
}

OMX_ERRORTYPE cb_FillBufferDone(OMX_HANDLETYPE h, OMX_PTR app, OMX_BUFFERHEADERTYPE *buf) {
    Bridge *b = bridge_find(h);
    if (b && b->haveAppCb && b->appCb.FillBufferDone)
        return b->appCb.FillBufferDone(h, app, buf);
    return OMX_ErrorNone;
}

static OMX_CALLBACKTYPE g_ours = { cb_EventHandler, cb_EmptyBufferDone, cb_FillBufferDone };

OMX_ERRORTYPE my_EmptyThisBuffer(OMX_HANDLETYPE h, OMX_BUFFERHEADERTYPE *buf) {
    Bridge *b = bridge_find(h);
    if (b == nullptr)
        return OMX_ErrorUndefined;
    if (!ensure_hw(b))
        return b->orig.EmptyThisBuffer ? b->orig.EmptyThisBuffer(h, buf) : OMX_ErrorUndefined;

    uint32_t len = buf->nFilledLen;
    bool eos = (buf->nFlags & OMX_BUFFERFLAG_EOS) != 0;
    bool ok = true;
    pthread_mutex_lock(&b->lock);
    if (eos) {
        uint32_t msg[2] = { EOS_MAGIC, 0 };
        ok = write_all(b->fd, msg, sizeof(msg));
        if (ok) {
            b->finished = true;
            LOGI("已通知宿主 EOS");
        }
    } else if (len > 0) {
        // pts 用 64 位（UU 的 pts 是 ns，会话超过 ~2 秒就超过 int32 了）
        int64_t pts = (int64_t)buf->nTimeStamp;
        uint32_t msg[4] = { AU_MAGIC, len, (uint32_t)(pts & 0xFFFFFFFF),
                            (uint32_t)((uint64_t)pts >> 32) };
        ok = write_all(b->fd, msg, sizeof(msg)) &&
             write_all(b->fd, buf->pBuffer + buf->nOffset, len);
        if (ok) {
            b->ptsPush(pts);
            b->ausSent++;
            if (b->ausSent <= 3 || b->ausSent % 120 == 0)
                LOGI("已送宿主 AU #%llu: %u 字节 pts=%lld",
                     (unsigned long long)b->ausSent, (unsigned)len, (long long)pts);
        }
    }
    pthread_mutex_unlock(&b->lock);

    if (!ok) {
        LOGE("发送失败，桥断开（本次回退软解）");
        session_reset(b);
        return b->orig.EmptyThisBuffer ? b->orig.EmptyThisBuffer(h, buf) : OMX_ErrorUndefined;
    }
    if (b->haveAppCb && b->appCb.EmptyBufferDone)
        b->appCb.EmptyBufferDone(h, b->appData, buf);
    return OMX_ErrorNone;
}

OMX_ERRORTYPE my_FillThisBuffer(OMX_HANDLETYPE h, OMX_BUFFERHEADERTYPE *buf) {
    Bridge *b = bridge_find(h);
    if (b == nullptr)
        return OMX_ErrorUndefined;
    if (!ensure_hw(b))
        return b->orig.FillThisBuffer ? b->orig.FillThisBuffer(h, buf) : OMX_ErrorUndefined;

    ensure_geometry(b, h, buf->nAllocLen);

    pthread_mutex_lock(&b->lock);
    if (b->nFree < MAX_FREE_BUFS)
        b->freeBufs[b->nFree++] = buf;
    if (b->eosPending && b->nFree > 0) {
        OMX_BUFFERHEADERTYPE *eb = b->freeBufs[--b->nFree];
        eb->nFilledLen = 0;
        eb->nOffset = 0;
        eb->nFlags = OMX_BUFFERFLAG_EOS;
        eb->nTimeStamp = (OMX_TICKS)b->ptsPop();
        b->eosPending = false;
        pthread_mutex_unlock(&b->lock);
        if (b->haveAppCb && b->appCb.FillBufferDone)
            b->appCb.FillBufferDone(h, b->appData, eb);
        return OMX_ErrorNone;
    }
    pthread_mutex_unlock(&b->lock);
    return OMX_ErrorNone;
}

OMX_ERRORTYPE my_SetCallbacks(OMX_HANDLETYPE h, OMX_CALLBACKTYPE *cb, OMX_PTR app) {
    Bridge *b = bridge_find(h);
    if (b && cb) {
        pthread_mutex_lock(&b->lock);
        b->appCb = *cb;
        b->haveAppCb = true;
        pthread_mutex_unlock(&b->lock);
    }
    if (b == nullptr || b->orig.SetCallbacks == nullptr)
        return OMX_ErrorNone;
    return b->orig.SetCallbacks(h, &g_ours, app);
}

OMX_ERRORTYPE my_SetConfig(OMX_HANDLETYPE h, OMX_INDEXTYPE idx, OMX_PTR p) {
    Bridge *b = bridge_find(h);
    if (idx == OMX_INDEX_CONFIG_LOW_LATENCY) {
        if (b)
            b->cfgLowLatency = 1;
        LOGI("低延迟请求 -> 直接返回成功（由宿主硬解承接）");
        return OMX_ErrorNone;
    }
    if (b == nullptr || b->orig.SetConfig == nullptr)
        return OMX_ErrorUnsupportedIndex;
    return b->orig.SetConfig(h, idx, p);
}

OMX_ERRORTYPE my_GetConfig(OMX_HANDLETYPE h, OMX_INDEXTYPE idx, OMX_PTR p) {
    Bridge *b = bridge_find(h);
    if (idx == OMX_INDEX_CONFIG_LOW_LATENCY) {
        if (p)
            ((OMX_CONFIG_BOOLEANTYPE *)p)->bEnabled = OMX_TRUE;
        return OMX_ErrorNone;
    }
    if (b == nullptr || b->orig.GetConfig == nullptr)
        return OMX_ErrorUnsupportedIndex;
    return b->orig.GetConfig(h, idx, p);
}

static void drop_buf_locked(Bridge *b, OMX_BUFFERHEADERTYPE *buf) {
    for (int i = 0; i < b->nFree; i++)
        if (b->freeBufs[i] == buf) {
            memmove(&b->freeBufs[i], &b->freeBufs[i + 1],
                    sizeof(b->freeBufs[0]) * (b->nFree - i - 1));
            b->nFree--;
            break;
        }
}

OMX_ERRORTYPE my_FreeBuffer(OMX_HANDLETYPE h, OMX_U32 port, OMX_BUFFERHEADERTYPE *buf) {
    Bridge *b = bridge_find(h);
    if (b && buf && b->modeDecided && b->hwMode) {
        pthread_mutex_lock(&b->lock);
        drop_buf_locked(b, buf);
        pthread_mutex_unlock(&b->lock);
    }
    if (b == nullptr || b->orig.FreeBuffer == nullptr)
        return OMX_ErrorNone;
    return b->orig.FreeBuffer(h, port, buf);
}

void return_output_buffers(Bridge *b) {
    OMX_BUFFERHEADERTYPE *ret[MAX_FREE_BUFS];
    int n = 0;
    pthread_mutex_lock(&b->lock);
    for (int i = 0; i < b->nFree && n < MAX_FREE_BUFS; i++)
        ret[n++] = b->freeBufs[i];
    b->nFree = 0;
    b->eosPending = false;
    pthread_mutex_unlock(&b->lock);
    for (int i = 0; i < n; i++) {
        ret[i]->nFilledLen = 0;
        ret[i]->nOffset = 0;
        ret[i]->nFlags = 0;
        if (b->haveAppCb && b->appCb.FillBufferDone)
            b->appCb.FillBufferDone(b->handle, b->appData, ret[i]);
    }
    if (n)
        LOGI("已归还 %d 个输出缓冲", n);
}

OMX_ERRORTYPE my_SendCommand(OMX_HANDLETYPE h, OMX_COMMANDTYPE cmd, OMX_U32 p1, OMX_PTR p2) {
    Bridge *b = bridge_find(h);
    if (b == nullptr)
        return OMX_ErrorNone;

    bool flush = (cmd == OMX_CommandFlush) && (p1 == 1 || p1 == OMX_ALL);
    if (flush) {
        bool notify = false;
        pthread_mutex_lock(&b->lock);
        for (int i = 0; i < b->nPending; i++)
            free(b->pending[i].data);
        b->nPending = 0;
        b->ptsHead = b->ptsTail = b->ptsCount = 0;
        b->eosPending = false;
        if (b->connected && b->hwMode && !b->finished) {
            uint32_t msg[2] = { FLUSH_MAGIC, 0 };
            notify = write_all(b->fd, msg, sizeof(msg));
        }
        pthread_mutex_unlock(&b->lock);
        LOGI("Flush(port %u)：清本地队列%s", (unsigned)p1,
             notify ? "，已让宿主重启解码器" : "");
    }

    OMX_ERRORTYPE r = b->orig.SendCommand
                          ? b->orig.SendCommand(h, cmd, p1, p2) : OMX_ErrorNone;

    if (flush)
        return_output_buffers(b);
    if (cmd == OMX_CommandStateSet && (p1 == OMX_StateIdle || p1 == OMX_StateLoaded) &&
        b->modeDecided && b->hwMode) {
        return_output_buffers(b);
    }
    return r;
}

}  // namespace

android::SoftOMXComponent *createSoftOMXComponent(const char *name,
                                                  const OMX_CALLBACKTYPE *callbacks,
                                                  OMX_PTR appData,
                                                  OMX_COMPONENTTYPE **component) {
    LOGI("shim 生效：createSoftOMXComponent(name=%s)", name ? name : "(null)");
    CreateFn fn = load_orig();
    if (fn == nullptr)
        return nullptr;

    Bridge *b = new Bridge();
    if (callbacks) {
        b->appCb = *callbacks;
        b->haveAppCb = true;
    }
    android::SoftOMXComponent *c = fn(name, &g_ours, appData, component);
    if (c == nullptr || component == nullptr || *component == nullptr) {
        LOGE("原库创建组件失败（%s 是否存在？）", ORIG_LIB);
        delete b;
        return c;
    }
    b->handle = (OMX_HANDLETYPE)*component;
    b->appData = appData;
    b->codec = (name && strstr(name, "hevc")) ? 2 : 1;
    b->orig = **component;
    if (!bridge_register(b)) {
        LOGE("桥实例表已满，本实例走软解");
        delete b;
        return c;
    }
    if (b->orig.EmptyThisBuffer)
        (*component)->EmptyThisBuffer = my_EmptyThisBuffer;
    if (b->orig.FillThisBuffer)
        (*component)->FillThisBuffer = my_FillThisBuffer;
    if (b->orig.SetCallbacks)
        (*component)->SetCallbacks = my_SetCallbacks;
    if (b->orig.SetConfig)
        (*component)->SetConfig = my_SetConfig;
    if (b->orig.GetConfig)
        (*component)->GetConfig = my_GetConfig;
    if (b->orig.SendCommand)
        (*component)->SendCommand = my_SendCommand;
    if (b->orig.FreeBuffer)
        (*component)->FreeBuffer = my_FreeBuffer;

    LOGI("组件 %p 已包装（实例 #%lld，codec=%s）", (void *)*component, (long long)b->createSeq,
         b->codec == 2 ? "HEVC" : "AVC");
    return c;
}
