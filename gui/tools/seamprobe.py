#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""seamprobe.py —— 找"竖线"：在若干行上扫描每一列，只报**色值发生变化**的列。

为什么需要它：外观排查里"凭空多出来一根竖线"这类问题，靠 --row 逐列打印
会刷出上千行、看不出哪一列是"异类"。这里改成只报变化点，并标出该列与其
左邻的亮度差 Δ —— 一条"画上去的线"的特征是 Δ 很小但夹在两侧同色之间
（即 A→B→A 的孤立窄带），而两个面板的天然分界是 A→B 之后一直是 B。

用法：
  python gui/tools/seamprobe.py <png> 700,300,500          # 指定若干行，逐列报变化点
  python gui/tools/seamprobe.py <png> --lines              # ★ 跨行统计：哪些列"几乎每行都变"
                                                        #   真竖线会在这里排前几名
  python gui/tools/seamprobe.py <png> --lines 0-130        # 只在给定列区间里统计
  python gui/tools/seamprobe.py <png> --inkband 28,120,60,560   # ★ 按行找墨迹带（定位一列小图标各在哪）
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
import pngprobe as pp  # noqa: E402


def lum(c):
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def scan_lines(path, x0, x1, y0, y1, thr):
    """跨行统计"边缘列"：一列在同一 y 区间内有多行出现明显色差，就说明画面里
    这个 x 位置长期存在一条竖直边界。返回 [(命中行数, x, 该列最大 Δ), ...] 降序。"""
    w, h, px = pp.load_png(path)
    if x1 > w:
        x1 = w
    if y1 > h:
        y1 = h
    rows = list(range(y0, y1, 4))
    hits = {}
    for y in rows:
        prev = px(x0, y)
        for x in range(x0 + 1, x1):
            c = px(x, y)
            if c != prev:
                d = abs(lum(c) - lum(prev))
                if d >= thr:
                    hits.setdefault(x, [0, 0])
                    hits[x][0] += 1
                    hits[x][1] = max(hits[x][1], d)
                prev = c
    top = sorted(((v[0], k, v[1]) for k, v in hits.items()), reverse=True)
    print('%s  %dx%d  采样 %d 行  阈值 Δ>=%d' % (os.path.basename(path), w, h, len(rows), thr))
    print('  命中最多的 24 列（横跨多行的竖直边界）：')
    for n, x, d in top[:24]:
        print('    x=%-5d 命中 %4d/%d 行   最大Δ=%d' % (x, n, len(rows), round(d)))
    return top


def ink_bands(path, x0, y0, x1, y1, thr):
    """在给定矩形里按行找"有墨"的带：一行里只要有一个像素亮度 >= thr 就算有墨，
    连续的墨行合并成一条带并报出它的 y 范围与最亮值。
    用途：定位一列小图标各在哪一段（比 --col 逐行刷几百行好读得多）。"""
    w, h, px = pp.load_png(path)
    x1 = min(x1, w)
    y1 = min(y1, h)
    bands = []
    cur = None
    for y in range(y0, y1):
        best = 0
        bx = None
        for x in range(x0, x1):
            l = lum(px(x, y))
            if l > best:
                best, bx = l, x
        if best >= thr:
            if cur is None:
                cur = {'y0': y, 'y1': y, 'max': best, 'bx': bx}
            else:
                cur['y1'] = y
                if best > cur['max']:
                    cur['max'], cur['bx'] = best, bx
        elif cur is not None:
            bands.append(cur)
            cur = None
    if cur is not None:
        bands.append(cur)
    print('%s  框 (%d,%d)-(%d,%d)  阈值亮度>=%d' % (os.path.basename(path), x0, y0, x1, y1, thr))
    if not bands:
        print('  ⚠ 这一块里一点墨都没有')
    for b in bands:
        print('  y=%d..%d  (高 %d)  最亮 %d @ x=%d' %
              (b['y0'], b['y1'], b['y1'] - b['y0'] + 1, round(b['max']), b['bx']))


def main():
    path = sys.argv[1]
    argv = sys.argv[2:]
    if argv and argv[0] == '--inkband':
        x0, y0, x1, y1 = [int(v) for v in argv[1].split(',')]
        thr = int(argv[2]) if len(argv) > 2 else 45
        ink_bands(path, x0, y0, x1, y1, thr)
        return
    if argv and argv[0] == '--lines':
        span = argv[1] if len(argv) > 1 else None
        if span and ':' not in span:
            x0, x1 = int(span.split('-')[0]), int(span.split('-')[1])
        else:
            x0, x1 = 0, 10 ** 9
        scan_lines(path, x0, x1, 0, 10 ** 9, 6)
        return

    rowspec = argv[0] if argv else '700'
    x0 = int(argv[1]) if len(argv) > 1 else 0
    x1 = int(argv[2]) if len(argv) > 2 else None
    w, h, px = pp.load_png(path)
    if x1 is None or x1 > w:
        x1 = w

    if ':' in rowspec:
        a, b, st = (list(map(int, rowspec.split(':'))) + [1])[:3]
        rows = list(range(a, b, st))
    else:
        rows = [int(v) for v in rowspec.split(',')]

    print('%s  %dx%d' % (os.path.basename(path), w, h))
    for y in rows:
        if y >= h:
            continue
        prev = px(x0, y)
        out = ['x=%d %s' % (x0, pp.hexs(prev))]
        for x in range(x0 + 1, x1):
            c = px(x, y)
            if c != prev:
                d = lum(c) - lum(prev)
                out.append('x=%d #%02X%02X%02X(Δ%+d)' % (x, c[0], c[1], c[2], round(d)))
                prev = c
        print('  y=%d: %s' % (y, '  '.join(out)))


if __name__ == '__main__':
    main()

