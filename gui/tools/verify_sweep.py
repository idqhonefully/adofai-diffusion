# verify_sweep.py —— 量每个字号下齿轮墨迹相对裁框中心的"方盒最大外延"
#
# 裁框 = 22px 正方形，中心 = 裁框几何中心。墨迹被切的判据：
#   只要任意墨迹像素的 clip-局部坐标 |dx|>11 或 |dy|>11（即超出 22px 方盒）→ 该角度下被切。
# 旋转 45° 时外延最大，所以重点看 a45_visible 的 maxext；若 maxext <= 10.5（留 0.5 余量）则安全。
import glob, os, struct, zlib, sys

OUT = r'<REPO>\output\.tmp\gearsweep'

def read_png(fp):
    d = open(fp, 'rb').read()
    assert d[:8] == b'\x89PNG\r\n\x1a\n'
    pos = 8; W=H=bd=ct=None; idat=b''
    while pos < len(d):
        ln = struct.unpack('>I', d[pos:pos+4])[0]; typ = d[pos+4:pos+8]
        data = d[pos+8:pos+8+ln]
        if typ == b'IHDR':
            W,H,bd,ct = struct.unpack('>IIBB', data[:10])
        elif typ == b'IDAT':
            idat += data
        elif typ == b'IEND':
            break
        pos += 12 + ln
    raw = zlib.decompress(idat)
    ch = 4 if ct == 6 else (3 if ct == 2 else 1)
    stride = W*ch
    # 去滤镜（只处理常见的 0/1/2/3/4）
    px = bytearray(); prev = bytearray(stride)
    p = 0
    for y in range(H):
        f = raw[p]; p+=1
        line = bytearray(raw[p:p+stride]); p+=stride
        for x in range(stride):
            a = line[x-ch] if x>=ch else 0
            b = prev[x]
            c = prev[x-ch] if x>=ch else 0
            if f==1: line[x]=(line[x]+a)&255
            elif f==2: line[x]=(line[x]+b)&255
            elif f==3: line[x]=(line[x]+((a+b)>>1))&255
            elif f==4:
                pp=a+b-c; pa=abs(pp-a); pb=abs(pp-b); pc=abs(pp-c)
                pr = a if (pa<=pb and pa<=pc) else (b if pb<=pc else c)
                line[x]=(line[x]+pr)&255
        px += line; prev = line
    return W,H,ch,px

def bg_color(px, W, H, ch):
    from collections import Counter
    c = Counter()
    for y in range(0, H, 3):
        for x in range(0, W, 3):
            i = (y*W+x)*ch
            c[(px[i],px[i+1],px[i+2])] += 1
    return c.most_common(1)[0][0]

def ink_extent(fp, cx, cy):
    W,H,ch,px = read_png(fp)
    bg = bg_color(px, W, H, ch)
    maxext = 0.0; cnt = 0
    for y in range(H):
        for x in range(W):
            i = (y*W+x)*ch
            r,g,b = px[i],px[i+1],px[i+2]
            d = abs(r-bg[0])+abs(g-bg[1])+abs(b-bg[2])
            if d > 60:
                dx = abs(x - cx); dy = abs(y - cy)
                e = max(dx, dy)
                if e > maxext: maxext = e
                cnt += 1
    return maxext, cnt

def main():
    meta = {}
    try:
        meta = eval(open(os.path.join(OUT,'meta.json')).read())
    except: pass
    clip = meta.get('clip')
    # 裁框中心在局部图里的像素位置 = WIN/2 * scale（局部窗口以裁框中心为中点）
    SCALE = clip['scale'] if clip else 6
    WIN = clip['width'] if clip else 44
    cx = cy = (WIN/2)*SCALE
    print(f"局部图 {WIN}x{WIN} @scale{SCALE}，裁框中心像素 ≈ ({cx},{cy})，裁框半边=11CSSpx={11*SCALE}px")
    print(f"{'file':28s} {'maxext(px)':>10s} {'=CSS':>7s} {'inkpx':>7s}")
    rows = []
    for fp in sorted(glob.glob(os.path.join(OUT,'sz*_visible.png'))):
        name = os.path.basename(fp)
        me, cnt = ink_extent(fp, cx, cy)
        css = me/SCALE
        rows.append((name, me, css, cnt))
        flag = '  ✗超裁框' if css > 10.5 else ''
        print(f"{name:28s} {me:10.1f} {css:7.2f} {cnt:7d}{flag}")
    print("\n=== 每个字号在 45° 的方盒最大外延(CSS px)，对照裁框半边 11px ===")
    for size in [12,13,14,15,16]:
        for r in rows:
            if r[0]==f'sz{size}_a45_visible.png':
                css = r[2]
                verdict = '安全(≤10.5)' if css<=10.5 else '会被切'
                print(f"  字号 {size}px @45°: maxext={css:.2f}CSSpx  {verdict}")

if __name__ == '__main__':
    main()
