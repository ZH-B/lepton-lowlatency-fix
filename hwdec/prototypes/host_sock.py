#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""host_sock.py —— 宿主侧验证：
   在共享目录里建 unix socket，等客户机连上来，回复一句话，
   然后通过 SCM_RIGHTS 把一个 memfd（内容 "hello-from-host-memfd"）传过去。
   跑在 Armada 宿主机上（python3 自带）。
"""
import os
import socket
import struct
import sys
import time

PATH = sys.argv[1] if len(sys.argv) > 1 else "/var/home/armada/Downloads/hwb.sock"

if os.path.exists(PATH):
    os.unlink(PATH)

srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
srv.bind(PATH)
os.chmod(PATH, 0o777)          # 客户机里是 root/其它 uid，放开权限
srv.listen(4)
print("[host] 监听 %s（模式 %o）" % (PATH, 0o777))
print("[host] 等待客户机连接…（Ctrl-C 退出）", flush=True)

srv.settimeout(120)
try:
    conn, _ = srv.accept()
except socket.timeout:
    print("[host] 等待超时，没人连上来")
    sys.exit(1)

print("[host] 客户机已连接")
data = conn.recv(256)
print("[host] 收到: %r" % data)
conn.sendall(b"hello-from-host\n")

# SCM_RIGHTS 传一个 memfd
fd = os.memfd_create("hwb-test", 0)
os.write(fd, b"hello-from-host-memfd")
os.lseek(fd, 0, os.SEEK_SET)
fds = array = struct.pack("i", fd)
conn.sendmsg([b"\x00"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array)])
print("[host] 已发送 memfd (fd=%d) via SCM_RIGHTS" % fd)

time.sleep(1)
conn.close()
srv.close()
os.unlink(PATH)
print("[host] 完成")
