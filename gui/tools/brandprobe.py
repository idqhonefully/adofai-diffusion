#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""brandprobe.py —— 判定侧栏品牌格里画的到底是「老 CSS 假 logo」还是「真应用图标」。

为什么需要它：两者在深色背景上都很暗，字符画（按亮度 ramp）会把它们都糊成一片
`. - =`，肉眼分辨不出。但两者的**色相分布**完全不同：

  · 老假 logo = 蓝色渐变底 + 白色字母「A」  ⇒ 只有蓝系 + 灰白，**一个暖色像素都没有**
  · 真应用图标 = 深蓝底 + 暖橙「漩涡」      ⇒ 蓝系之外必然有一撮 R 明显大于 B 的暖色

所以判据是「暖色像素占比」，不是亮度。顺带报主色聚类，便于人工再核一眼。

用法：python gui/tools/brandprobe.py <png> <x0,y0,x1,y1>
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__))))
import pngprobe as pp  # noqa: E402


def main():
    path = sys.argv[1]
    x0, y0, x1, y1 = [int(v) for v in sys.argv[2].split(',')]
    w, h, px = pp.load_png(path)
    x1 = min(x1, w)
    y1 = min(y1, h)

    n = warm = blue = gray = opaque = 0
    maxgap = -999
    maxgap_px = None
    buckets = {}
    for y in range(y0, y1):
        for x in range(x0, x1):
            r, g, b, a = px(x, y)
            if a < 40:
                continue
            opaque += 1
            n += 1
            gap = r - b
            if gap > maxgap:
                maxgap, maxgap_px = gap, (x, y, (r, g, b))
            if gap >= 20:
                warm += 1
            elif b - r >= 20:
                blue += 1
            else:
                gray += 1
            key = (r >> 5, g >> 5, b >> 5)
            buckets[key] = buckets.get(key, 0) + 1

    print('%s  区域 (%d,%d)-(%d,%d)  不透明像素 %d' % (os.path.basename(path), x0, y0, x1, y1, opaque))
    if not n:
        print('  ⚠ 区域全透明 —— 裁错地方了')
        return
    print('  暖色(R-B>=20) %5d  %5.1f%%' % (warm, 100.0 * warm / n))
    print('  蓝系(B-R>=20) %5d  %5.1f%%' % (blue, 100.0 * blue / n))
    print('  中性(灰/白)  %5d  %5.1f%%' % (gray, 100.0 * gray / n))
    print('  最暖像素 R-B=%+d @ (%d,%d) rgb%s' % (maxgap, maxgap_px[0], maxgap_px[1], maxgap_px[2]))
    top = sorted(buckets.items(), key=lambda kv: -kv[1])[:6]
    print('  主色簇（RGB 高位分箱，top6）：')
    for (r, g, b), c in top:
        print('    #%02X%02X%02X×32  %5d  %5.1f%%' % (r * 32 + 16, g * 32 + 16, b * 32 + 16, c,
                                                     100.0 * c / n))
    verdict = ('✅ 有暖色 ⇒ 是真应用图标（漩涡/星云那张）'
               if warm >= 0.02 * n else
               '❌ 零暖色 ⇒ 还是老 CSS 假 logo（蓝底白 A）')
    print('  判定：' + verdict)


if __name__ == '__main__':
    main()
