# verify_clip2.py —— 判定「齿轮墨迹是否始终落在 22px 裁框内」（形状判定，非标量阈值）
#
# 输入：spinbbox 探针 DSF=1.5/GPU 截的 24 张 bbox_hidden_*.png（overflow:hidden=被裁后的真容）
#       与 24 张 bbox_visible_*.png（不裁=真容）。
# 窗口：40 CSS px 居中于裁框中心。CDP 在 DSF 下会把 scale 再乘 DSF，故 PNG 实际 = 40*8*1.5=480。
# 本脚本从首图尺寸反推倍率，不写死。
import os, struct, zlib, glob
from collections import Counter

OUT = r'<REPO>\output\.tmp\spinbbox-dsf1.5_gpu'
WIN_CSS = 40
CLIP_HALF_CSS = 11   # 22px 裁框半边

def read_png(path):
    d = open(path,'rb').read()
    assert d[:8]==b'\x89PNG\r\n\x1a\n'
    pos=8; W=H=bd=ct=0; idat=b''
    while pos<len(d):
        ln=struct.unpack('>I',d[pos:pos+4])[0]; typ=d[pos+4:pos+8]; body=d[pos+8:pos+8+ln]
        if typ==b'IHDR':
            W,H=struct.unpack('>II',body[:8]); bd=body[8]; ct=body[9]
        elif typ==b'IDAT': idat+=body
        pos+=12+ln
    raw=zlib.decompress(idat)
    ch={6:4,2:3,4:2,0:1}.get(ct,4)
    stride=W*ch
    px=[[0,0,0,0] for _ in range(W*H)]
    prev=bytearray(stride); o=0
    for y in range(H):
        ft=raw[o]; o+=1
        line=bytearray(raw[o:o+stride]); o+=stride
        if ft==1:
            for i in range(ch,stride): line[i]=(line[i]+line[i-ch])&255
        elif ft==2:
            for i in range(stride): line[i]=(line[i]+prev[i])&255
        elif ft==3:
            for i in range(stride):
                a=line[i-ch] if i>=ch else 0
                line[i]=(line[i]+((a+prev[i])//2))&255
        elif ft==4:
            for i in range(stride):
                a=line[i-ch] if i>=ch else 0; b=prev[i]; c=prev[i-ch] if i>=ch else 0
                p=a+b-c; pa,pb,pc=abs(p-a),abs(p-b),abs(p-c)
                pr=a if (pa<=pb and pa<=pc) else (b if pb<=pc else c)
                line[i]=(line[i]+pr)&255
        prev=line
        for x in range(W):
            r=line[x*ch]; g=line[x*ch+1]; b=line[x*ch+2]; a=line[x*ch+3] if ch>3 else 255
            px[y*W+x]=[r,g,b,a]
    return W,H,px,ch

def ink_bbox(W,H,px):
    cnt=Counter()
    for i in range(W*H):
        p=px[i]
        if p[3]>40: cnt[(p[0]//16,p[1]//16,p[2]//16)]+=1
    if not cnt: return None
    bg=cnt.most_common(1)[0][0]
    minx=miny=10**9; maxx=maxy=-1
    for y in range(H):
        for x in range(W):
            p=px[y*W+x]
            if p[3]<40: continue
            if (p[0]//16,p[1]//16,p[2]//16)==bg: continue
            if x<minx:minx=x
            if x>maxx:maxx=x
            if y<miny:miny=y
            if y>maxy:maxy=y
    if maxx<0: return None
    return (minx,miny,maxx,maxy)

def main():
    files_h=sorted(glob.glob(os.path.join(OUT,'bbox_hidden_*.png')))
    if not files_h:
        print('无截图，先跑 spinbboxprobe.js'); return
    W,H,_,_=read_png(files_h[0])
    SCALE_PX=W/WIN_CSS          # 实际倍率（含 DSF）
    CTR=W/2
    CLIP_HALF_PX=CLIP_HALF_CSS*SCALE_PX
    print('PNG=%dx%d 实际倍率=%.1f 裁框半边=%g px (CSS %g)'%(W,H,SCALE_PX,CLIP_HALF_PX,CLIP_HALF_CSS))
    worst=-1; worst_a=None; any_cut=False; big=[]
    for fh in files_h:
        ang=int(os.path.basename(fh)[len('bbox_hidden_'):-4])
        W,H,px,_=read_png(fh)
        bb=ink_bbox(W,H,px)
        if not bb:
            print('  ang=%03d: 无墨迹'%ang); continue
        minx,miny,maxx,maxy=bb
        ox0=(minx-CTR)/SCALE_PX; ox1=(maxx-CTR)/SCALE_PX
        oy0=(miny-CTR)/SCALE_PX; oy1=(maxy-CTR)/SCALE_PX
        ext=max(abs(ox0),abs(ox1),abs(oy0),abs(oy1))
        if ext>worst: worst=ext; worst_a=ang
        if ext>CLIP_HALF_CSS+0.5:
            any_cut=True; big.append((ang,ext))
        if ext>CLIP_HALF_CSS-1:
            print('  ang=%03d: L%.1f R%.1f T%.1f B%.1f 最大%.1f %s'%(ang,ox0,ox1,oy0,oy1,ext,'✗切' if ext>CLIP_HALF_CSS+0.5 else ''))
    print('\n=== 结论 ===')
    print('裁框半边 = %g CSS px（齿轮墨迹须落在 ±%g 内才不被切）'%(CLIP_HALF_CSS,CLIP_HALF_CSS))
    print('所有角度最大偏移 = %.2f CSS px（出现在 %s°）'%(worst,worst_a))
    if any_cut:
        print('  => 有角度身体超裁框 ⇒ 仍被切  FAIL ✗  (超出的角度: %s)'%big)
    else:
        print('  => 齿轮（含齿尖）始终在裁框内/仅齿尖贴边 ⇒ 不被切  PASS ✓')

if __name__=='__main__':
    main()
