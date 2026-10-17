// guest_frame.c —— 客户机侧：接收宿主发来的 dmabuf fd + 几何信息，
//   mmap 出解码后的 NV12 帧，做统计并写出 Y 平面的 PGM（供人工确认画面）。
// 编译：zig cc -target aarch64-linux-musl -O2 -static -o guest_frame guest_frame.c
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <unistd.h>

typedef struct {
    char magic[4];
    uint32_t version;
    uint32_t flags;
    char fourcc[4];
    uint32_t w, h, stride, size;
} hdr_t;

static int recv_hdr_fd(int sock, hdr_t *h) {
    struct msghdr msg;
    struct iovec io;
    char ctrl[CMSG_SPACE(sizeof(int))];
    memset(&msg, 0, sizeof(msg));
    memset(ctrl, 0, sizeof(ctrl));
    io.iov_base = h;
    io.iov_len = sizeof(*h);
    msg.msg_iov = &io;
    msg.msg_iovlen = 1;
    msg.msg_control = ctrl;
    msg.msg_controllen = sizeof(ctrl);

    ssize_t n = recvmsg(sock, &msg, MSG_WAITALL);
    if (n < (ssize_t)sizeof(*h)) {
        fprintf(stderr, "[guest] recvmsg 返回 %zd (errno=%s)\n", n, strerror(errno));
        return -1;
    }
    struct cmsghdr *cm = CMSG_FIRSTHDR(&msg);
    if (!cm || cm->cmsg_level != SOL_SOCKET || cm->cmsg_type != SCM_RIGHTS) {
        fprintf(stderr, "[guest] 没收到 SCM_RIGHTS\n");
        return -1;
    }
    int fd = -1;
    memcpy(&fd, CMSG_DATA(cm), sizeof(int));
    return fd;
}

int main(int argc, char **argv) {
    const char *path = argc > 1 ? argv[1] : "/var/home/armada/Downloads/hwb.sock";
    const char *out = argc > 2 ? argv[2] : "/var/home/armada/Downloads/frame.pgm";
    setvbuf(stdout, NULL, _IONBF, 0);

    int s = socket(AF_UNIX, SOCK_STREAM, 0);
    struct sockaddr_un a;
    memset(&a, 0, sizeof(a));
    a.sun_family = AF_UNIX;
    strncpy(a.sun_path, path, sizeof(a.sun_path) - 1);
    if (connect(s, (struct sockaddr *)&a, sizeof(a)) < 0) {
        printf("[guest] connect 失败: %s\n", strerror(errno));
        return 1;
    }
    printf("[guest] 已连接宿主 daemon，等待解码结果…\n");
    write(s, "guest-ready\n", 12);

    hdr_t h;
    memset(&h, 0, sizeof(h));
    int fd = recv_hdr_fd(s, &h);
    if (fd < 0)
        return 2;
    printf("[guest] 收到 dmabuf: %.4s ver=%u fmt=%.4s %ux%u stride=%u size=%u\n",
           h.magic, h.version, h.fourcc, h.w, h.h, h.stride, h.size);

    size_t len = h.size;
    unsigned char *p = mmap(NULL, len, PROT_READ, MAP_SHARED, fd, 0);
    if (p == MAP_FAILED) {
        printf("[guest] mmap 失败: %s\n", strerror(errno));
        return 3;
    }
    printf("[guest] mmap 成功 %zu 字节\n", len);

    unsigned char *y = p;
    long long sum = 0;
    int mn = 255, mx = 0;
    for (uint32_t r = 0; r < h.h; r++) {
        for (uint32_t c = 0; c < h.w; c++) {
            int v = y[(size_t)r * h.stride + c];
            sum += v;
            if (v < mn) mn = v;
            if (v > mx) mx = v;
        }
    }
    printf("[guest] Y 平面: min=%d max=%d mean=%.1f\n", mn, mx,
           (double)sum / ((double)h.w * h.h));

    FILE *f = fopen(out, "wb");
    if (!f) {
        printf("[guest] 打不开 %s: %s\n", out, strerror(errno));
        return 4;
    }
    fprintf(f, "P5\n%u %u\n255\n", h.w, h.h);
    for (uint32_t r = 0; r < h.h; r++)
        fwrite(y + (size_t)r * h.stride, 1, h.w, f);
    fclose(f);
    printf("[guest] 已写出 %s\n", out);

    // 采样几行，肉眼判断画面是否有结构（纯色/全黑说明没解出真画面）
    for (int r = 0; r < 3; r++) {
        uint32_t row = h.h / 4 * (r + 1);
        printf("[guest] 第 %u 行采样: ", row);
        for (int c = 0; c < 8; c++)
            printf("%3d ", y[(size_t)row * h.stride + (size_t)(h.w / 8) * c]);
        printf("\n");
    }
    printf("[guest] OK\n");
    return 0;
}
