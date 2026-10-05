# measure_shot.py —— 在**主人的截图**上量「设置齿轮」的实际墨迹
#
# 为什么：headless 探针（spinbboxprobe）量出齿轮墨迹 19~20px、完整落在 22px 盒内、
# 24 个角度都不越界 —— 也就是说在探针环境下"切掉"根本没复现。既然主人看得见切，
# 就必须先在**他那张图**上把真相量出来，再去找两个环境的差异。
#
# 标尺：选中指示条 #nav-ind 是写死的 22px 高 × 3px 宽（CSS），拿它当比例尺，
#       就能反推这张截图是多少设备px/CSSpx，进而判断齿轮墨迹是不是被切短了。
#
# 判据（分类而非阈值挑边）：
#   · 强调黄（齿轮 & 指示条）：R 明显 > B，且够亮
#   · 文字白（"设置"两个汉字）：R≈G≈B 且亮
import sys
sys.path.insert(0, r'<REPO>\gui\tools')
from pngprobe import load_png

P = sys.argv[1] if len(sys.argv) > 1 else r'~\.workbuddy\clipboard-images\clipboard-2026-09-22T06-31-39-032Z-c479322a.png'
w, h, px = load_png(P)
print('%s  %dx%d' % (P.replace('\\', '/').split('/')[-1], w, h))

def is_yellow(c):
    r, g, b = c[:3]
    return r > 120 and r - b > 60 and g > 80

def is_white(c):
    r, g, b = c[:3]
    return r > 190 and abs(r - g) < 30 and abs(g - b) < 40 and b > 150

def bbox(pred):
    xs, ys, n = [], [], 0
    for y in range(h):
        for x in range(w):
            if pred(px(x, y)):
                xs.append(x); ys.append(y); n += 1
    if not n: return None
    return dict(n=n, x0=min(xs), x1=max(xs), y0=min(ys), y1=max(ys),
                w=max(xs)-min(xs)+1, h=max(ys)-min(ys)+1)

yel = bbox(is_yellow)
wht = bbox(is_white)
print('\n--- 黄色像素（齿轮 + 指示条 + 可能的强调文字）---')
print(yel)
print('--- 白色像素（"设置"文字）---')
print(wht)

# 逐列统计黄色像素的纵向范围，用来把"3px 宽的指示条"与"齿轮"分开
cols = {}
for y in range(h):
    for x in range(w):
        if is_yellow(px(x, y)):
            cols.setdefault(x, []).append(y)
print('\n--- 黄色像素逐列（x: 数量 / y范围）---')
for x in sorted(cols):
    ys = cols[x]
    print('  x=%3d  n=%3d  y=%3d..%-3d' % (x, len(ys), min(ys), max(ys)))
