# verify_clip.py —— 量「设置齿轮」墨迹的纵向跨度：静止 vs 旋转中
# 判据：裁区内每行找与背景角像素色差>阈值的墨迹像素，记首/末墨迹行 → 纵向跨度。
# 若修复生效（overflow:hidden），旋转中跨度应与静止相近（都≈齿轮本身尺寸），不再撑到对角线。
import sys, os
sys.path.insert(0, r'<REPO>\gui\tools')
from pngprobe import load_png

CX0, CY0, W, H = 2, 492, 48, 48   # 与 spinclip_probe 一致的裁区
TH = 26                            # 色差阈值（0-255 单通道）

def span(png):
    w, h, px = load_png(png)
    x0, y0 = CX0, CY0
    bg = px(x0, y0)[:3]            # 背景角像素
    first = last = -1
    rows = []
    for ry in range(H):
        yy = y0 + ry
        hit = 0
        for rx in range(W):
            xx = x0 + rx
            c = px(xx, yy)[:3]
            d = max(abs(c[i] - bg[i]) for i in range(3))
            if d > TH:
                hit += 1
        rows.append(hit)
        if hit:
            if first < 0: first = ry
            last = ry
    span_px = (last - first + 1) if first >= 0 else 0
    mid = first + span_px // 2
    return dict(bg=bg, first=first, last=last, span=span_px, midRow=mid,
                topPct=round(100*first/H), botPct=round(100*last/H))

rest = span(r'<REPO>\output\.tmp\spinclip_rest.png')
mid  = span(r'<REPO>\output\.tmp\spinclip_mid.png')
print('裁区 48x48 (y 492..540)，图标盒 22px (y 505..527)')
print('背景角像素色:', rest['bg'])
print()
print('静止(rest) : 首墨迹行 %2d  末墨迹行 %2d  纵向跨度 %2d px  中行 %2d' % (rest['first'], rest['last'], rest['span'], rest['midRow']))
print('旋转(mid)  : 首墨迹行 %2d  末墨迹行 %2d  纵向跨度 %2d px  中行 %2d' % (mid['first'],  mid['last'],  mid['span'],  mid['midRow']))
print()
print('判定：旋转中跨度(%d) vs 静止跨度(%d)；图标盒 22px' % (mid['span'], rest['span']))
if mid['span'] <= 26:   # 旋转中墨迹被关在 22px 盒内(留 4px 余量)，不再撑到对角线/44px
    print('  => 齿轮被静态裁框关在 22px 内，旋转不再撑大  PASS ✓（修前实测 44px）')
else:
    print('  => 旋转中仍明显变大  FAIL ✗')
