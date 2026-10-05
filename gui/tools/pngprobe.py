#!/usr/bin/env python313
# -*- coding: utf-8 -*-
"""像素探针：零依赖解 PNG（zlib + unfilter）读色，无需 Pillow。

为什么需要它：模型读不了 PNG，但能读数字。外观类改动（材质 / 标题栏过渡 / 配色）
必须靠真实像素值下判断，不能对着一张缩略图猜。

用法：
  python313/python.exe gui/tools/pngprobe.py <png> 100,20 60,60      # 直接列坐标（旧用法保留）
  python313/python.exe gui/tools/pngprobe.py <png> --size
  python313/python.exe gui/tools/pngprobe.py <png> --px 100,20
  python313/python.exe gui/tools/pngprobe.py <png> --avg 0,0,300,42   # 区域均色
  python313/python.exe gui/tools/pngprobe.py <png> --col 60 --rows 0-90    # 某列逐行（找横向分界）
  python313/python.exe gui/tools/pngprobe.py <png> --row 20 --cols 0-90    # 某行逐列（找纵向分界）
  python313/python.exe gui/tools/pngprobe.py <png> --scanrows 0-120        # 逐行中位色 + Δ（自动找台阶）
  python313/python.exe gui/tools/pngprobe.py <png> --bands 0-300           # 连续同色行合并成带
  python313/python.exe gui/tools/pngprobe.py <png> --ascii 20,20,48,48     # 裁切区渲染成字符画（"看"图形轮廓）
                                                             # 🔴 后两个数是**宽和高**（x0,y0,W,H），
                                                             #    不是右下角坐标！当成 x1,y1 会裁到画面外
                                                             #    ⇒ 整片空白，看着像"这块没画东西"。
                                                             #    脚本会在越界时告警。
"""
import sys, zlib, struct


def load_png(path):
    data = open(path, 'rb').read()
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise SystemExit('不是 PNG 文件: %s' % path)
    pos, idat, plte, trns = 8, b'', None, None
    w = h = bd = ct = None
    while pos < len(data):
        ln = struct.unpack('>I', data[pos:pos + 4])[0]
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        if typ == b'IHDR':
            w, h, bd, ct, _comp, _filt, inter = struct.unpack('>IIBBBBB', body)
            if bd != 8:
                raise SystemExit('只支持 8-bit（这张 %d-bit）' % bd)
            if inter:
                raise SystemExit('不支持隔行扫描')
        elif typ == b'IDAT':
            idat += body
        elif typ == b'PLTE':
            plte = body
        elif typ == b'tRNS':
            trns = body
        elif typ == b'IEND':
            break
        pos += 12 + ln

    raw = zlib.decompress(idat)
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ct)
    if ch is None:
        raise SystemExit('未知 color type %d' % ct)
    stride = w * ch
    out, prev, p = bytearray(h * stride), bytearray(stride), 0
    for y in range(h):
        f = raw[p]; p += 1
        line = bytearray(raw[p:p + stride]); p += stride
        if f == 1:
            for x in range(ch, stride):
                line[x] = (line[x] + line[x - ch]) & 255
        elif f == 2:
            for x in range(stride):
                line[x] = (line[x] + prev[x]) & 255
        elif f == 3:
            for x in range(stride):
                a = line[x - ch] if x >= ch else 0
                line[x] = (line[x] + ((a + prev[x]) >> 1)) & 255
        elif f == 4:
            for x in range(stride):
                a = line[x - ch] if x >= ch else 0
                b = prev[x]
                c = prev[x - ch] if x >= ch else 0
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[x] = (line[x] + pr) & 255
        out[y * stride:(y + 1) * stride] = line
        prev = line

    def px(x, y):
        o = (y * w + x) * ch
        if ct == 6:
            return (out[o], out[o + 1], out[o + 2], out[o + 3])
        if ct == 2:
            return (out[o], out[o + 1], out[o + 2], 255)
        if ct == 4:
            return (out[o], out[o], out[o], out[o + 1])
        if ct == 0:
            return (out[o], out[o], out[o], 255)
        i = out[o] * 3                      # ct == 3 调色板
        a = trns[out[o]] if trns and out[o] < len(trns) else 255
        return (plte[i], plte[i + 1], plte[i + 2], a)

    return w, h, px


def hexs(c):
    return '#%02X%02X%02X' % c[:3]


def save_png(path, w, h, rows):
    """写一张 8-bit RGB 真彩 PNG（零依赖）。每行用 filter 0（None）。
    只用来导出裁切图给人看，不需要支持调色板/透明。"""
    raw = bytearray()
    for line in rows:
        raw.append(0)
        raw += line
    def chunk(typ, body):
        return (struct.pack('>I', len(body)) + typ + body +
                struct.pack('>I', zlib.crc32(typ + body) & 0xffffffff))
    ihdr = struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)
    with open(path, 'wb') as f:
        f.write(b'\x89PNG\r\n\x1a\n')
        f.write(chunk(b'IHDR', ihdr))
        f.write(chunk(b'IDAT', zlib.compress(bytes(raw), 9)))
        f.write(chunk(b'IEND', b''))


def rng(s):
    a, _, b = s.partition('-')
    return int(a), (int(b) if b else int(a))


def mid_row(px, w, y, n=60):
    cols = sorted((px(x, y) for x in range(0, w, max(1, w // n))),
                  key=lambda c: c[0] + c[1] + c[2])
    return cols[len(cols) // 2]


def main():
    if len(sys.argv) < 3:
        print(__doc__); return
    path = sys.argv[1]
    w, h, px = load_png(path)
    args = sys.argv[2:]

    def arg(name, default=None):
        for i, a in enumerate(args):
            if a == name and i + 1 < len(args):
                return args[i + 1]
            if a.startswith(name + '='):
                return a.split('=', 1)[1]
        return default

    print('%s  %dx%d' % (path.replace('\\', '/').split('/')[-1], w, h))
    if '--size' in args or not args:
        return
    if '--px' in args:
        x, y = map(int, arg('--px').split(','))
        c = px(x, y); print('  (%d,%d) = %s rgba%s' % (x, y, hexs(c), c))
    if '--avg' in args:
        x, y, ww, hh = map(int, arg('--avg').split(','))
        r = g = b = n = 0
        for yy in range(y, min(y + hh, h)):
            for xx in range(x, min(x + ww, w)):
                c = px(xx, yy); r += c[0]; g += c[1]; b += c[2]; n += 1
        if n:
            print('  区域 (%d,%d)+%dx%d 均色 = %s' % (x, y, ww, hh, hexs((r // n, g // n, b // n))))
    if '--col' in args:
        x = int(arg('--col')); y0, y1 = rng(arg('--rows', '0-%d' % (h - 1)))
        print('  列 x=%d y=%d..%d：' % (x, y0, y1))
        prev = None
        for yy in range(y0, min(y1 + 1, h)):
            c = px(x, yy)
            d = ''
            if prev is not None:
                dd = max(abs(c[i] - prev[i]) for i in range(3))
                if dd: d = '  ← Δ%d' % dd
            print('    y=%4d  %s%s' % (yy, hexs(c), d)); prev = c
    if '--row' in args:
        y = int(arg('--row')); x0, x1 = rng(arg('--cols', '0-%d' % (w - 1)))
        print('  行 y=%d x=%d..%d：' % (y, x0, x1))
        prev = None
        for xx in range(x0, min(x1 + 1, w)):
            c = px(xx, y)
            d = ''
            if prev is not None:
                dd = max(abs(c[i] - prev[i]) for i in range(3))
                if dd: d = '  ← Δ%d' % dd
            print('    x=%4d  %s%s' % (xx, hexs(c), d)); prev = c
    if '--scanrows' in args:
        y0, y1 = rng(arg('--scanrows', '0-%d' % (h - 1)))
        print('  逐行中位色 y=%d..%d：' % (y0, y1))
        prev = None
        for yy in range(y0, min(y1 + 1, h)):
            mid = mid_row(px, w, yy)
            d = ''
            if prev is not None:
                dd = max(abs(mid[i] - prev[i]) for i in range(3))
                if dd: d = '   Δ=%d' % dd
            print('    y=%4d  %s%s' % (yy, hexs(mid), d)); prev = mid
    if '--bands' in args:
        y0, y1 = rng(arg('--bands', '0-%d' % (h - 1)))
        print('  分带（连续同色行合并）y=%d..%d：' % (y0, y1))
        cur, start = None, y0
        for yy in range(y0, min(y1 + 1, h)):
            mid = mid_row(px, w, yy)
            if cur is None:
                cur, start = mid, yy
            elif max(abs(mid[i] - cur[i]) for i in range(3)) > 2:
                print('    y %4d..%-4d  %3d 行  %s' % (start, yy - 1, yy - start, hexs(cur)))
                cur, start = mid, yy
        print('    y %4d..%-4d  %3d 行  %s' % (start, y1, y1 - start + 1, hexs(cur)))
    if '--crop' in args:
        # 把裁切区**导出成一张新的 PNG**（可选最近邻放大）。
        # 为什么需要它：本探针只会打数字和字符画，而"让主人肉眼核对某个小细节"必须给图 ——
        # 整窗截图缩到聊天窗口里，88px 宽的侧栏细节等于没有。
        # 裁出那一小块再放大，才看得出"图标是齿轮还是太阳"。
        # 用最近邻而不是平滑：看图标笔法时糊掉的插值反而更难判断。
        x0, y0, ww, hh = map(int, arg('--crop').split(','))
        out = arg('-o')
        if not out:
            raise SystemExit('--crop 需要配 -o <输出.png>')
        scale = max(1, int(arg('--scale', '1')))
        if x0 >= w or y0 >= h or x0 + ww <= 0 or y0 + hh <= 0:
            raise SystemExit('裁切区 (%d,%d)+%dx%d 完全在画面(%dx%d)之外 —— '
                             '后两个参数是**宽和高**，不是右下角坐标。' % (x0, y0, ww, hh, w, h))
        pw, ph = ww * scale, hh * scale
        rows = []
        for gy in range(hh):
            sy = y0 + gy
            line = bytearray()
            if 0 <= sy < h:
                for gx in range(ww):
                    sx = x0 + gx
                    r, g, b, _a = px(sx, sy) if 0 <= sx < w else (14, 17, 22, 255)
                    line += bytes((r, g, b)) * scale
            else:
                line = bytes((14, 17, 22)) * pw
            for _ in range(scale):
                rows.append(line)
        save_png(out, pw, ph, rows)
        print('  已裁出 %s   裁切区 (%d,%d)+%dx%d  放大 %d×  →  %dx%d'
              % (out, x0, y0, ww, hh, scale, pw, ph))
    if '--ascii' in args:
        # 把裁切区渲染成字符画。模型读不了 PNG，但能读这个 —— 用来"看"图标轮廓/线条位置。
        # 亮度映射：空间 " .:-=+*#%@"（越亮越密）。行用两个字符，补偿字符高宽比。
        x0, y0, ww, hh = map(int, arg('--ascii').split(','))
        # 🔴 越界必须喊一声。踩过的坑：把后两个数当成右下角坐标（x1,y1）传进来，
        #    于是 ww/hh 变成几百，整块裁到画面外，每一格都取不到像素 ⇒ 输出**整片空白**，
        #    看上去就像"这个位置压根没画东西"，会把人带进完全错误的排查方向。
        if x0 >= w or y0 >= h or x0 + ww <= 0 or y0 + hh <= 0:
            print('  ⚠ --ascii 的裁切区 (%d,%d)+%dx%d 完全在画面(%dx%d)之外 —— '
                  '后两个参数是**宽和高**，不是右下角坐标。' % (x0, y0, ww, hh, w, h))
        elif x0 + ww > w or y0 + hh > h:
            print('  ⚠ --ascii 的裁切区 (%d,%d)+%dx%d 超出了画面(%dx%d)，超出部分按空白处理。'
                  % (x0, y0, ww, hh, w, h))
        cols = int(arg('--cols', '0')) or min(60, ww)
        rows = int(arg('--rows', '0')) or max(1, round(cols * (hh / ww) * 0.5))
        ramp = ' .:-=+*#%@'
        print('  ASCII (%d,%d)+%dx%d → %d×%d（亮=密）' % (x0, y0, ww, hh, cols, rows))
        for r in range(rows):
            line = ''
            for c in range(cols):
                xs = x0 + c * ww // cols
                xe = max(xs + 1, x0 + (c + 1) * ww // cols)
                ys = y0 + r * hh // rows
                ye = max(ys + 1, y0 + (r + 1) * hh // rows)
                tot = n = 0
                for yy in range(ys, min(ye, h)):
                    for xx in range(xs, min(xe, w)):
                        cc = px(xx, yy)
                        tot += (cc[0] * 299 + cc[1] * 587 + cc[2] * 114) // 1000
                        n += 1
                v = tot // n if n else 0
                line += ramp[min(len(ramp) - 1, v * len(ramp) // 256)]
            print('  |' + line + '|')
    # 裸坐标（旧用法）—— 跳过选项名和它后面紧跟的那个值
    skip = set()
    for i, a in enumerate(args):
        if a.startswith('--') and '=' not in a and i + 1 < len(args):
            skip.add(i + 1)
    for i, a in enumerate(args):
        if a.startswith('--') or i in skip:
            continue
        if ',' in a and a.count(',') == 1:
            x, y = map(int, a.split(','))
            c = px(x, y)
            print('  (%d,%d) = %s  rgba%s' % (x, y, hexs(c), c))


if __name__ == '__main__':
    main()
