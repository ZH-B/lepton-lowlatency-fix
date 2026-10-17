// guest_hwbench.c —— 客户机侧解码基准（走 App 同一条 MediaCodec 路径）
//
// 目的：公平对比「宿主硬解桥」与「内置软解」的
//   · 首帧延迟（喂完第 1 个 AU 到拿到第 1 帧）
//   · 稳态流水线延迟（第 N 个 AU 入队 → 第 N 帧出队，FIFO 配对）
//   · 吞吐（60 帧总耗时 / fps）
//   · 输出像素（可选 dump 一帧 NV12 供比对）
//
// 做法：先把输入喂满（输入缓冲一还回来就立刻再喂），过程中用 0 超时轮询输出，
// 不阻塞喂数据；时间戳都记在本地。soft/hard 的切换：桥不在就自动回退软解
// （所以「杀掉宿主桥」= 软解基线，「桥在」= 硬解），无需改代码。
//
// 编译（见 component/build.sh 同款工具链）：
//   zig cc -target aarch64-linux-android -D__ANDROID_API__=29 -nostdinc \
//     -isystem $SYS/usr/include -isystem $SYS/usr/include/aarch64-linux-android \
//     -O2 -fPIE -c -o /tmp/bench.o guest_hwbench.c
//   zig ld.lld -m aarch64linux -pie --dynamic-linker /system/bin/linker64 -o bench \
//     $D/crtbegin_dynamic.o /tmp/bench.o -L$D -lmediandk -llog -lc -lm -ldl $D/crtend_android.o
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
#include <time.h>
#include <unistd.h>

static double now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec * 1000.0 + ts.tv_nsec / 1e6;
}

static uint8_t *read_file(const char *p, size_t *len) {
    FILE *f = fopen(p, "rb");
    if (!f) { printf("打不开 %s: %s\n", p, strerror(errno)); return NULL; }
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *b = malloc(n);
    if (fread(b, 1, n, f) != (size_t)n) { free(b); fclose(f); return NULL; }
    fclose(f);
    *len = (size_t)n;
    return b;
}

static int split_aus(const uint8_t *d, size_t n, size_t **outs) {
    size_t cap = 256, cnt = 0;
    size_t *pos = malloc(cap * sizeof(size_t));
    for (size_t i = 0; i + 4 < n;) {
        size_t sc, p;
        if (!memcmp(d + i, "\x00\x00\x00\x01", 4)) { sc = 4; p = i; }
        else if (!memcmp(d + i, "\x00\x00\x01", 3)) { sc = 3; p = i; }
        else { i++; continue; }
        if (p + sc < n && (d[p + sc] & 0x1f) == 9) {
            if (cnt == cap) { cap *= 2; pos = realloc(pos, cap * sizeof(size_t)); }
            pos[cnt++] = p;
        }
        i = p + sc + 1;
    }
    if (cnt < 2) { free(pos); *outs = NULL; return 0; }
    *outs = pos;
    return (int)cnt;
}

int main(int argc, char **argv) {
    const char *path = argc > 1 ? argv[1] : "/var/home/armada/Downloads/test_aud.h264";
    const char *dump = argc > 2 ? argv[2] : NULL;   // 非空则把第一帧写成 NV12 原始文件
    int use_ll = (argc > 3) ? atoi(argv[3]) : 1;
    long pace_us = (argc > 4) ? atol(argv[4]) : 0;   // 每个 AU 的间隔（微秒），模拟实时码流
    setvbuf(stdout, NULL, _IONBF, 0);

    size_t len = 0;
    uint8_t *data = read_file(path, &len);
    if (!data) return 1;
    size_t *aus = NULL;
    int n_aus = split_aus(data, len, &aus);
    printf("[bench] 码流 %zu 字节，%d 个 AU\n", len, n_aus);

    AMediaCodec *codec = AMediaCodec_createDecoderByType("video/avc");
    if (!codec) { printf("[bench] 没有 video/avc 解码器\n"); return 2; }
    AMediaFormat *fmt = AMediaFormat_new();
    AMediaFormat_setString(fmt, AMEDIAFORMAT_KEY_MIME, "video/avc");
    AMediaFormat_setInt32(fmt, AMEDIAFORMAT_KEY_WIDTH, 1280);
    AMediaFormat_setInt32(fmt, AMEDIAFORMAT_KEY_HEIGHT, 720);
    if (use_ll) AMediaFormat_setInt32(fmt, "low-latency", 1);
    media_status_t st = AMediaCodec_configure(codec, fmt, NULL, NULL, 0);
    if (st != AMEDIA_OK) { printf("[bench] configure -> %d\n", st); return 3; }
    AMediaCodec_start(codec);

    double *t_feed = calloc(n_aus, sizeof(double));
    double t0 = now_ms();
    int fed = 0, got = 0, dumped = 0;
    double first_frame_ms = -1, lat_sum = 0, lat_max = 0, last_frame_ms = 0;
    int lat_n = 0;

    while (got < n_aus) {
        int did = 0;
        // 喂一个 AU（输入缓冲没空就等 2ms）
        if (fed < n_aus) {
            ssize_t bi = AMediaCodec_dequeueInputBuffer(codec, 2000);
            if (bi >= 0) {
                size_t cap = 0;
                uint8_t *buf = AMediaCodec_getInputBuffer(codec, bi, &cap);
                size_t start = aus[fed];
                size_t end = (fed + 1 < n_aus) ? aus[fed + 1] : len;
                size_t n = end - start;
                if (n > cap) n = cap;
                if (buf) {
                    memcpy(buf, data + start, n);
                    AMediaCodec_queueInputBuffer(codec, bi, 0, n, fed * 33333, 0);
                    t_feed[fed] = now_ms();
                    fed++;
                    did = 1;
                    if (pace_us > 0) usleep(pace_us);
                } else {
                    AMediaCodec_queueInputBuffer(codec, bi, 0, 0, 0, 0);
                }
            }
        }
        // 把所有已经出来的帧都收掉
        for (;;) {
            AMediaCodecBufferInfo info;
            ssize_t oi = AMediaCodec_dequeueOutputBuffer(codec, &info, 2000);
            if (oi == AMEDIACODEC_INFO_TRY_AGAIN_LATER) break;
            if (oi < 0) break;
            double t = now_ms();
            if (got == 0) first_frame_ms = t - t0;
            if (got < fed) {
                double lat = t - t_feed[got];
                lat_sum += lat;
                if (lat > lat_max) lat_max = lat;
                lat_n++;
            }
            if (!dumped && dump) {
                size_t cap = 0;
                uint8_t *ob = AMediaCodec_getOutputBuffer(codec, oi, &cap);
                if (ob && info.size > 0) {
                    FILE *f = fopen(dump, "wb");
                    if (f) { fwrite(ob + info.offset, 1, info.size, f); fclose(f); }
                    printf("[bench] 已 dump 第 1 帧 %d 字节 -> %s\n", info.size, dump);
                }
                dumped = 1;
            }
            got++;
            last_frame_ms = t - t0;
            AMediaCodec_releaseOutputBuffer(codec, oi, false);
            did = 1;
        }
        if (!did) usleep(500);   // 既没喂进也没收到：让出 CPU
        if (now_ms() - t0 > 30000) {
            printf("[bench] 30s 超时：已喂 %d AU，拿到 %d 帧\n", fed, got);
            break;
        }
    }

    double dt = now_ms() - t0;
    printf("[bench] 喂入 %d AU / 解出 %d 帧\n", fed, got);
    printf("[bench] 总耗时 %.1f ms（%.1f fps），首帧 %.1f ms，末帧 %.1f ms\n",
           dt, got * 1000.0 / (dt > 0 ? dt : 1), first_frame_ms, last_frame_ms);
    if (lat_n)
        printf("[bench] 流水线延迟（FIFO 配对，%d 个样本）：平均 %.1f ms，最大 %.1f ms\n",
               lat_n, lat_sum / lat_n, lat_max);
    struct rusage ru;
    getrusage(RUSAGE_SELF, &ru);
    printf("[bench] 本进程 CPU：user %.0f ms / sys %.0f ms\n",
           ru.ru_utime.tv_sec * 1000.0 + ru.ru_utime.tv_usec / 1000.0,
           ru.ru_stime.tv_sec * 1000.0 + ru.ru_stime.tv_usec / 1000.0);

    double ts = now_ms();
    AMediaCodec_stop(codec);
    printf("[bench] stop() 用时 %.1f ms\n", now_ms() - ts);
    ts = now_ms();
    AMediaCodec_delete(codec);
    printf("[bench] delete() 用时 %.1f ms\n", now_ms() - ts);
    AMediaFormat_delete(fmt);
    free(aus); free(data); free(t_feed);
    return got > 0 ? 0 : 4;
}
