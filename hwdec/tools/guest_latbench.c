// guest_latbench.c —— 客户机侧解码延迟基准（沿用 guest_decode_test 已验证可用的喂/收节奏）。
//
// 用法：guest_latbench <stream.h264> [low_latency=1] [pace_us=0] [dump.nv12]
//   pace_us>0 时按该间隔喂 AU（33333 = 30fps 实时码流），量的是真实端到端解码延迟。
// 切换软/硬解：宿主桥在 → 硬解；宿主桥不在 → shim 自动回退软解。
//   用 NDK AMediaCodec 按类型创建 video/avc 解码器 → 喂入 Annex-B 码流（按 AUD/AU）→ 收帧。
//   用于：① 量测当前软解基线；② 验证替换后的解码组件是否被选中且能出帧。
// 编译（手机容器）：
//   zig cc -target aarch64-linux-android29 --sysroot=/root/opt/ndk/sysroot \
//          -O2 -o guest_decode_test guest_decode_test.c -lmediandk -llog
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <media/NdkMediaCodec.h>
#include <media/NdkMediaFormat.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static uint8_t *read_file(const char *p, size_t *len) {
    FILE *f = fopen(p, "rb");
    if (!f) {
        printf("打不开 %s: %s\n", p, strerror(errno));
        return NULL;
    }
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *b = malloc(n);
    if (fread(b, 1, n, f) != (size_t)n) {
        free(b);
        fclose(f);
        return NULL;
    }
    fclose(f);
    *len = (size_t)n;
    return b;
}

// 按 H.264 起始码找 NAL；有 AUD(type 9) 时以 AUD 为界切 access unit
static int is_hevc;
static int split_aus(const uint8_t *d, size_t n, size_t **outs) {
    size_t cap = 256, cnt = 0;
    size_t *pos = malloc(cap * sizeof(size_t));
    size_t i = 0;
    while (i + 4 < n) {
        size_t sc = 0, p = 0;
        if (!memcmp(d + i, "\x00\x00\x00\x01", 4)) { sc = 4; p = i; }
        else if (!memcmp(d + i, "\x00\x00\x01", 3)) { sc = 3; p = i; }
        else { i++; continue; }
        int aud = 0;
        if (p + sc < n) {
            aud = is_hevc ? (((d[p + sc] >> 1) & 0x3f) == 35) : ((d[p + sc] & 0x1f) == 9);
        }
        if (p + sc < n && aud) {
            if (cnt == cap) { cap *= 2; pos = realloc(pos, cap * sizeof(size_t)); }
            pos[cnt++] = p;
        }
        i = p + sc + 1;
    }
    if (cnt < 2) {
        free(pos);
        *outs = NULL;
        return 0;
    }
    *outs = pos;
    return (int)cnt;
}

static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

int main(int argc, char **argv) {
    const char *path = argc > 1 ? argv[1] : "/var/home/armada/Downloads/test_aud.h264";
    int use_ll = (argc > 2) ? atoi(argv[2]) : 1;      // 是否设置 low-latency
    long pace_us = (argc > 3) ? atol(argv[3]) : 0;    // 喂 AU 的间隔（微秒）
    const char *dump = (argc > 4) ? argv[4] : NULL;   // 非空则写第 1 帧 NV12
    int vw = (argc > 5) ? atoi(argv[5]) : 1280;       // 声明分辨率（必须与码流一致）
    int vh = (argc > 6) ? atoi(argv[6]) : 720;
    const char *mime = (argc > 7) ? argv[7] : "video/avc";   // video/avc 或 video/hevc
    setvbuf(stdout, NULL, _IONBF, 0);

    size_t len = 0;
    uint8_t *data = read_file(path, &len);
    if (!data) return 1;
    printf("[test] 码流 %zu 字节\n", len);

    is_hevc = (strstr(mime, "hevc") != NULL);
    size_t *aus = NULL;
    int n_aus = split_aus(data, len, &aus);
    printf("[test] mime=%s，切成 %d 个 access unit\n", mime, n_aus);

    AMediaCodec *codec = AMediaCodec_createDecoderByType(mime);
    if (!codec) {
        printf("[test] createDecoderByType 失败（没有 video/avc 解码器？）\n");
        return 2;
    }
    AMediaFormat *fmt = AMediaFormat_new();
    AMediaFormat_setString(fmt, AMEDIAFORMAT_KEY_MIME, mime);
    AMediaFormat_setInt32(fmt, AMEDIAFORMAT_KEY_WIDTH, vw);
    AMediaFormat_setInt32(fmt, AMEDIAFORMAT_KEY_HEIGHT, vh);
    if (use_ll) {
        AMediaFormat_setInt32(fmt, "low-latency", 1);
        printf("[test] 已设置 low-latency=1\n");
    }
    media_status_t st = AMediaCodec_configure(codec, fmt, NULL, NULL, 0);
    printf("[test] configure -> %d (%s)\n", st, st == AMEDIA_OK ? "OK" : "失败");
    if (st != AMEDIA_OK) return 3;
    st = AMediaCodec_start(codec);
    printf("[test] start -> %d\n", st);

    double *t_feed = calloc(n_aus, sizeof(double));
    double t0 = now_ms();
    int in_flight = 0, got = 0, fed = 0, eos_sent = 0;
    size_t timeout_us = 20000;
    double first_ms = -1, lat_sum = 0, lat_max = 0;
    int lat_n = 0, dumped = 0;

    while (got < n_aus && (now_ms() - t0) < 20000) {
        // 喂输入
        if (fed < n_aus) {
            ssize_t bi = AMediaCodec_dequeueInputBuffer(codec, 2000);
            if (bi >= 0) {
                size_t cap = 0;
                uint8_t *buf = AMediaCodec_getInputBuffer(codec, bi, &cap);
                size_t start = aus[fed];
                size_t end = (fed + 1 < n_aus) ? aus[fed + 1] : len;
                size_t n = end - start;
                if (n > cap) n = cap;
                memcpy(buf, data + start, n);
                AMediaCodec_queueInputBuffer(codec, bi, 0, n, fed * 33333, 0);
                t_feed[fed] = now_ms();
                in_flight++;
                fed++;
                if (pace_us > 0) usleep(pace_us);
            }
        } else if (!eos_sent) {
            ssize_t bi = AMediaCodec_dequeueInputBuffer(codec, 2000);
            if (bi >= 0) {
                AMediaCodec_queueInputBuffer(codec, bi, 0, 0, 0,
                                             AMEDIACODEC_BUFFER_FLAG_END_OF_STREAM);
                eos_sent = 1;
            }
        }
        // 收帧：用小步长轮询把「帧到达」到「被发现」的间隔压到 2ms 以内，
        // 否则异步到达的帧会等到下一轮循环才被取走（对硬解路径不公平）。
        AMediaCodecBufferInfo info;
        ssize_t oi;
        {
            double deadline = now_ms() + timeout_us / 1000.0;
            for (;;) {
                oi = AMediaCodec_dequeueOutputBuffer(codec, &info, 2000);
                if (oi >= 0 || now_ms() >= deadline) break;
            }
        }
        if (oi >= 0) {
            if (info.flags & AMEDIACODEC_BUFFER_FLAG_END_OF_STREAM) {
                printf("[test] 收到 EOS（共 %d 帧）\n", got);
                break;
            }
            double tnow = now_ms();
            if (got == 0) {
                first_ms = tnow - t0;
                printf("[lat] 第 1 帧: %d 字节, pts=%lld（首帧延迟 %.1f ms）\n",
                       info.size, (long long)info.presentationTimeUs, first_ms);
            }
            if (got < fed) {
                double lat = tnow - t_feed[got];
                lat_sum += lat;
                if (lat > lat_max) lat_max = lat;
                lat_n++;
                if (got < 3 || got % 20 == 0)
                    printf("[lat] 帧 #%d 端到端 %.1f ms（喂于 t=%.1f ms）\n",
                           got, lat, t_feed[got] - t0);
            }
            if (!dumped && dump) {
                size_t cap = 0;
                uint8_t *ob = AMediaCodec_getOutputBuffer(codec, oi, &cap);
                if (ob && info.size > 0) {
                    FILE *f = fopen(dump, "wb");
                    if (f) { fwrite(ob + info.offset, 1, info.size, f); fclose(f); }
                    printf("[lat] 已 dump 第 1 帧 %d 字节 -> %s\n", info.size, dump);
                }
                dumped = 1;
            }
            got++;
            AMediaCodec_releaseOutputBuffer(codec, oi, false);
        }
    }
    double dt = now_ms() - t0;
    printf("[lat] 结果: 喂入 %d AU，解出 %d 帧，用时 %.1f ms（%.1f fps）\n",
           fed, got, dt, got * 1000.0 / (dt > 0 ? dt : 1));
    if (lat_n)
        printf("[lat] 端到端延迟（FIFO 配对 %d 个样本）: 平均 %.1f ms，最大 %.1f ms\n",
               lat_n, lat_sum / lat_n, lat_max);
    struct rusage ru;
    getrusage(RUSAGE_SELF, &ru);
    printf("[lat] 本进程 CPU: user %.0f ms / sys %.0f ms\n",
           ru.ru_utime.tv_sec * 1000.0 + ru.ru_utime.tv_usec / 1000.0,
           ru.ru_stime.tv_sec * 1000.0 + ru.ru_stime.tv_usec / 1000.0);
    AMediaCodec_stop(codec);
    AMediaCodec_delete(codec);
    AMediaFormat_delete(fmt);
    free(aus);
    free(data);
    free(t_feed);
    return got > 0 ? 0 : 4;
}
