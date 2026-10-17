// guest_test.c —— 客户机侧验证：
//   1) 能否连到宿主在共享目录里建的 unix socket
//   2) 能否通过 SCM_RIGHTS 收到宿主传来的 fd（先用 memfd 验证，之后换成 dmabuf）
// 编译（在手机容器里）：zig cc -target aarch64-linux-android -O2 -static -o guest_test guest_test.c
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <sys/utsname.h>
#include <unistd.h>

static int recv_fd(int sock) {
    struct msghdr msg = {0};
    struct iovec io;
    char buf[8] = {0};
    io.iov_base = buf;
    io.iov_len = sizeof(buf);
    msg.msg_iov = &io;
    msg.msg_iovlen = 1;
    char ctrl[CMSG_SPACE(sizeof(int))];
    msg.msg_control = ctrl;
    msg.msg_controllen = sizeof(ctrl);

    ssize_t n = recvmsg(sock, &msg, 0);
    if (n < 0) {
        perror("recvmsg");
        return -1;
    }
    struct cmsghdr *cm = CMSG_FIRSTHDR(&msg);
    if (!cm || cm->cmsg_level != SOL_SOCKET || cm->cmsg_type != SCM_RIGHTS) {
        fprintf(stderr, "[guest] 没收到 SCM_RIGHTS（cmsg=%p)\n", (void *)cm);
        return -1;
    }
    int fd = -1;
    memcpy(&fd, CMSG_DATA(cm), sizeof(int));
    return fd;
}

int main(int argc, char **argv) {
    const char *path = argc > 1 ? argv[1] : "/var/home/armada/Downloads/hwb.sock";
    setvbuf(stdout, NULL, _IONBF, 0);

    struct utsname u;
    uname(&u);
    printf("[guest] uid=%d gid=%d 内核=%s %s\n", getuid(), getgid(), u.sysname, u.release);

    struct stat st;
    if (stat(path, &st) == 0)
        printf("[guest] socket 路径可见: %s (mode=%o)\n", path, st.st_mode & 07777);
    else
        printf("[guest] 注意: stat(%s) 失败: %s\n", path, strerror(errno));

    int s = socket(AF_UNIX, SOCK_STREAM, 0);
    if (s < 0) {
        perror("socket");
        return 1;
    }
    struct sockaddr_un a;
    memset(&a, 0, sizeof(a));
    a.sun_family = AF_UNIX;
    strncpy(a.sun_path, path, sizeof(a.sun_path) - 1);

    if (connect(s, (struct sockaddr *)&a, sizeof(a)) < 0) {
        printf("[guest] connect 失败: %s\n", strerror(errno));
        return 1;
    }
    printf("[guest] 已连上宿主 daemon\n");

    const char *hello = "hello-from-guest\n";
    write(s, hello, strlen(hello));

    char buf[256];
    memset(buf, 0, sizeof(buf));
    ssize_t n = recv(s, buf, sizeof(buf) - 1, 0);
    printf("[guest] 宿主回复: %.*s", n > 0 ? (int)n : 0, buf);

    int fd = recv_fd(s);
    if (fd < 0) {
        printf("[guest] 未收到 fd\n");
        return 2;
    }
    char linkp[64], target[256];
    memset(target, 0, sizeof(target));
    snprintf(linkp, sizeof(linkp), "/proc/self/fd/%d", fd);
    ssize_t k = readlink(linkp, target, sizeof(target) - 1);
    printf("[guest] 收到 fd=%d -> %s\n", fd, k > 0 ? target : "?");

    // 如果是 memfd，读一下内容验证 fd 真的可用
    char rb[128];
    memset(rb, 0, sizeof(rb));
    if (lseek(fd, 0, SEEK_SET) >= 0) {
        ssize_t r = read(fd, rb, sizeof(rb) - 1);
        if (r > 0)
            printf("[guest] fd 内容(%zd 字节): %.*s\n", r, (int)r, rb);
    }
    printf("[guest] OK\n");
    return 0;
}
