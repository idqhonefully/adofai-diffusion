# verify_clip_final.py —— 量 45°+给定 scale 下，裁框「开/关」两版的墨迹最远点离中心距离
# hidden < visible ⇒ 齿轮被裁框切掉了一部分（用户看到的"变形/切掉"）
# hidden ≈ visible ⇒ 没被切
import glob, os, struct, zlib
OUT=r'<REPO>\output\.tmp\spinclipv'
def read_png(fp):
    d=open(fp,'rb').read(); assert d[:8]==b'\x89PNG\r\n\x1a\n'
    pos=8; W=H=ct=None; idat=b''
    while pos<len(d):
        ln=struct.unpack('>I',d[pos:pos+4])[0]; typ=d[pos+4:pos+8]; data=d[pos+8:pos+8+ln]
        if typ==b'IHDR': W,H,_,ct=struct.unpack('>IIBB',data[:10])
        elif typ==b'IDAT': idat+=data
        elif typ==b'IEND': break
        pos+=12+ln
    raw=zlib.decompress(idat); ch=4 if ct==6 else 3; stride=W*ch; px=bytearray(); prev=bytearray(stride); p=0
    for y in range(H):
        f=raw[p]; p+=1; line=bytearray(raw[p:p+stride]); p+=stride
        for x in range(stride):
            a=line[x-ch] if x>=ch else 0; b=prev[x]; c=prev[x-ch] if x>=ch else 0
            if f==1: line[x]=(line[x]+a)&255
            elif f==2: line[x]=(line[x]+b)&255
            elif f==3: line[x]=(line[x]+((a+b)>>1))&255
            elif f==4:
                pp=a+b-c; pa=abs(pp-a); pb=abs(pp-b); pc=abs(pp-c)
                pr=a if(pa<=pb and pa<=pc) else(b if pb<=pc else c); line[x]=(line[x]+pr)&255
        px+=line; prev=line
    return W,H,ch,px
def bg(px,W,H,ch):
    from collections import Counter; c=Counter()
    for y in range(0,H,4):
        for x in range(0,W,4):
            i=(y*W+x)*ch; c[(px[i],px[i+1],px[i+2])]+=1
    return c.most_common(1)[0][0]
def maxext(fp,cx,cy):
    W,H,ch,px=read_png(fp); b=bg(px,W,H,ch); mx=0; cnt=0
    for y in range(H):
        for x in range(W):
            i=(y*W+x)*ch
            if abs(px[i]-b[0])+abs(px[i+1]-b[1])+abs(px[i+2]-b[2])>60:
                e=max(abs(x-cx),abs(y-cy)); mx=max(mx,e); cnt+=1
    return mx,cnt
meta=eval(open(os.path.join(OUT,'meta.json')).read()); clip=meta['clip']
SCALE=clip['scale']; cx=cy=(clip['width']/2)*SCALE
print(f"裁框中心像素({cx},{cy})，半边=11CSSpx={11*SCALE}px")
print(f"{'图':16s} {'maxext(px)':>10s} {'=CSS':>7s} {'判定'}")
for name in ['s1_hidden','s1_visible','s88_hidden','s88_visible']:
    fp=os.path.join(OUT,name+'.png'); me,cnt=maxext(fp,cx,cy); css=me/SCALE
    flag=''
    if 'hidden' in name:
        other=os.path.join(OUT,name.replace('hidden','visible')+'.png')
        meV,cntV=maxext(other,cx,cy)
        flag='  ✗被切' if me<meV-2 else '  ✓未切'
    print(f"{name:16s} {me:10.1f} {css:7.2f} {cnt:6d}{flag}")
print("\n=== 结论 ===")
_,c1v=maxext(os.path.join(OUT,'s1_visible.png'),cx,cy)
_,c1h=maxext(os.path.join(OUT,'s1_hidden.png'),cx,cy)
_,c88v=maxext(os.path.join(OUT,'s88_visible.png'),cx,cy)
_,c88h=maxext(os.path.join(OUT,'s88_hidden.png'),cx,cy)
print(f"修前 scale1 @45°: visible={c1v/SCALE:.2f}CSS  hidden={c1h/SCALE:.2f}CSS  -> {'被切' if c1h<c1v-2 else '未切'}")
print(f"修后 scale.88 @45°: visible={c88v/SCALE:.2f}CSS  hidden={c88h/SCALE:.2f}CSS  -> {'被切' if c88h<c88v-2 else '未切（PASS）'}")
