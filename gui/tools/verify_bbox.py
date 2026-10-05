# verify_bbox.py —— 量「设置齿轮」在 24 个角度下的墨迹包围盒
#
# 输入：spinbboxprobe.js 产出的 output/.tmp/spinbbox/bbox_{hidden,visible}_XXX.png + meta.json
#   · 每张是 CDP 按 clip+scale=8 出的局部放大图：窗口 40x40 CSS px，以图标盒(22px)中心为中心
#   · meta.json 给出窗口在页面里的左上角 + 盒子的 rect ⇒ 可把像素坐标换算回 CSS px
# 输出：每个角度的墨迹 bbox（相对 22px 盒的四条边），以及**越界量**（墨迹伸出盒外的 CSS px）
#
# 判读：
#   · hidden 轮：越界 > 0 = 被裁框切掉（切了多少）
#   · visible 轮：若各角度 bbox 明显变化 ⇒ 墨迹没居中，绕盒心旋转时在"扫圈"（视觉忽大忽小/偏移）
#                 若各角度 bbox 几乎不变 ⇒ 齿轮本身是圆的，旋转不会改变轮廓 ⇒ 根本不需要裁框
import json, os, sys
sys.path.insert(0, r'<REPO>\gui\tools')
from pngprobe import load_png

D = r'<REPO>\output\.tmp\spinbbox'
meta = json.load(open(os.path.join(D, 'meta.json'), encoding='utf-8'))
clip = meta['clip']; box = meta['meta']['box']; S = clip['scale']

# 盒子的四条边在"窗口局部 CSS 坐标"里的位置
bx0 = box['x'] - clip['x']; by0 = box['y'] - clip['y']
bx1 = bx0 + box['w'];        by1 = by0 + box['h']

def corners(w, h, px):
    return [px(1, 1)[:3], px(w - 2, 1)[:3], px(1, h - 2)[:3], px(w - 2, h - 2)[:3]]

def ink_bbox(png, th=40):
    """返回墨迹 bbox（CSS px，含小数），以**窗口左上角**为原点"""
    w, h, px = load_png(png)
    cs = corners(w, h, px)
    bg = max(set(cs), key=cs.count)          # 四角出现最多的颜色 = 背景
    xs, ys, n = [], [], 0
    for y in range(h):
        for x in range(w):
            c = px(x, y)[:3]
            if max(abs(c[i] - bg[i]) for i in range(3)) > th:
                xs.append(x); ys.append(y); n += 1
    if not xs:
        return dict(n=0, bg=bg)
    return dict(n=n, bg=bg,
                l=min(xs)/S, r=(max(xs)+1)/S, t=min(ys)/S, b=(max(ys)+1)/S)

print('窗口 %.0fx%.0f CSS px（scale %d）起点 (%.0f,%.0f)；22px 盒在窗口内 x %.0f..%.0f, y %.0f..%.0f'
      % (clip['width'], clip['height'], S, clip['x'], clip['y'], bx0, bx1, by0, by1))
print('盒子中心在窗口内 = (%.1f, %.1f)' % ((bx0+bx1)/2, (by0+by1)/2))
print()

def over(bb):
    """墨迹越出 22px 盒的量：左/上为负=越界，右/下同理；统一以"伸出多少 px"表示"""
    return (max(0, bx0 - bb['l']), max(0, by0 - bb['t']),
            max(0, bb['r'] - bx1), max(0, bb['b'] - by1))

def report(round_name):
    rows = []
    for a in meta['angles']:
        f = os.path.join(D, 'bbox_%s_%03d.png' % (round_name, a))
        bb = ink_bbox(f)
        if not bb['n']:
            rows.append((a, None, bb)); continue
        rows.append((a, bb, bb))
    print('===== 轮次 %s（裁框 overflow:%s）=====' % (round_name, round_name))
    print('角度 |  墨迹 bbox（窗口内 CSS px）        | 宽 x 高 | 越出 22px 盒 左/上/右/下')
    spans = []
    for a, bb, _ in rows:
        if bb['n'] == 0:
            print('%4d | (无墨迹)' % a); continue
        w_ = bb['r'] - bb['l']; h_ = bb['b'] - bb['t']
        o = over(bb)
        spans.append((w_, h_))
        flag = '  ← 被切' if max(o) > 0.6 else ''
        print('%4d | %6.1f..%-6.1f  %6.1f..%-6.1f | %5.1f x %5.1f | %.1f / %.1f / %.1f / %.1f%s'
              % (a, bb['l'], bb['r'], bb['t'], bb['b'], w_, h_, o[0], o[1], o[2], o[3], flag))
    if spans:
        ws = [s[0] for s in spans]; hs = [s[1] for s in spans]
        print('  → 跨度范围：宽 %.1f~%.1f px，高 %.1f~%.1f px（波动 %.1f / %.1f px）'
              % (min(ws), max(ws), min(hs), max(hs), max(ws)-min(ws), max(hs)-min(hs)))
    print()

report('hidden')
report('visible')
