#!/usr/bin/env python3
# 复现 UU 的真实节奏：30fps 喂 AU + 每 flush_interval 秒 FLUSH 一次，统计每秒出帧数
import socket, struct, sys, threading, time
HELLO=0x48454C4F; CODEC=0x434F4443; SIZE=0x53495A45; AU=0x41554431; FLUSH=0x464C5331; FRAME=0x46524D31
HDR=struct.Struct("<II"); FH=struct.Struct("<III")
def split_hevc(d):
    pos=[]; i=0; n=len(d)
    while i+5<n:
        if d[i:i+4]==b"\x00\x00\x00\x01": sc,p=4,i
        elif d[i:i+3]==b"\x00\x00\x01": sc,p=3,i
        else: i+=1; continue
        if p+sc+1<n and ((d[p+sc]>>1)&0x3f)==35: pos.append(p)
        i=p+sc+1
    return [d[pos[k]:(pos[k+1] if k+1<len(pos) else n)] for k in range(len(pos))]
stream=sys.argv[1]; W=int(sys.argv[2]); H=int(sys.argv[3])
dur=float(sys.argv[4]) if len(sys.argv)>4 else 30
fi=float(sys.argv[5]) if len(sys.argv)>5 else 3.5
aus=split_hevc(open(stream,'rb').read())
print(f"[repro] {len(aus)} AU，{W}x{H}，喂 {dur}s，每 {fi}s Flush")
s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM); s.settimeout(10)
s.connect("/var/home/armada/Downloads/hwdec.sock")
s.sendall(HDR.pack(HELLO,2)+HDR.pack(CODEC,2)+HDR.pack(SIZE,W*H*3//2)+struct.pack("<II",W,H))
counts=[]; stop=threading.Event()
def rd():
    while not stop.is_set():
        try: h=s.recv(FH.size)
        except OSError: return
        if len(h)<FH.size: return
        m,l,f=FH.unpack(h)
        if m!=FRAME: return
        n=0
        while n<l:
            c=s.recv(min(1<<20,l-n))
            if not c: return
            n+=len(c)
        counts.append(time.time())
threading.Thread(target=rd,daemon=True).start()
t0=time.time(); fed=0; nflush=0; next_flush=t0+fi
while time.time()-t0 < dur:
    i=fed%len(aus)
    try: s.sendall(HDR.pack(AU,len(aus[i]))+struct.pack("<i",fed*33333)+aus[i])
    except OSError as e:
        print("[repro] 发送失败:",e); break
    fed+=1
    if time.time()>=next_flush:
        s.sendall(HDR.pack(FLUSH,0)); nflush+=1; next_flush=time.time()+fi
    time.sleep(1/30)
time.sleep(0.5); stop.set()
el=time.time()-t0
per=[sum(1 for t in counts if k<=t-t0<k+1) for k in range(int(el))]
print(f"[repro] 喂 {fed} AU / 收 {len(counts)} 帧 / {el:.1f}s / Flush {nflush} 次 → 出帧率 {len(counts)/el:.1f} fps（期望≈30）")
print("[repro] 每秒出帧:", per)
s.close()
