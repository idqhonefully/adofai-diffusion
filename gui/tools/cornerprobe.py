#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""cornerprobe —— 量**圆角半径**（零依赖，复用 pngprobe 的 PNG 解码）。

为什么需要它：主人指着截图说"这个框框的角度和窗口的角度不一样"，这种话
**眼睛和标量阈值都判不了**——半径差 4px 肉眼只能说"别扭"，说不出差多少；
而"取一圈采样点算方差"那种做法在这里也没意义（圆角里全是背景色）。
唯一能说清的是**弧线本身的形状**：把某个角上的边界逐行描出来，
按圆的方程反解半径 R，并用两条独立通路交叉验（横向跨度 vs 纵向跨度）。

用法：
  cornerprobe.py IMG --runs Y            # 打印第 Y 行的色run（先看清有几条边）
  cornerprobe.py IMG --corner br --roi X,Y,W,H --in R,G,B --out R,G,B
                                         # 描出该 ROI 里右下角的内侧边界并反解 R

⚠ 一律传 `D:/...` 形式的绝对路径（MSYS 会把 /d/... 弄丢）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pngprobe import load_png, hexs          # noqa: E402


def runs(px, w, y, minlen=3):
    """把一行压成色 run（相邻像素差 <= 2 视为同色）⇒ 一眼看出有几条竖边。"""
    out, cur, n = [], None, 0
    for x in range(w):
        c = px(x, y)[:3]
        if cur is None or max(abs(a - b) for a, b in zip(c, cur)) <= 2:
            cur = c if cur is None else cur
            n += 1
        else:
            out.append((cur, n))
            cur, n = c, 1
    out.append((cur, n))
    merged = []
    for c, n in out:                          # 吃掉零碎的抗锯齿点
        if merged and n < minlen:
            merged[-1] = (merged[-1][0], merged[-1][1] + n)
        else:
            merged.append((c, n))
    return merged


def d2(c, ref):
    return sum((a - b) ** 2 for a, b in zip(c[:3], ref[:3]))


def edge_x(px, w, h, y, ref_in, ref_out, x0, x1):
    """在 [x0,x1] 里找"离内侧色更近"的最后一个 x（右边界）。找不到返回 None。"""
    last = None
    for x in range(x0, min(x1, w - 1) + 1):
        c = px(x, y)[:3]
        if d2(c, ref_in) <= d2(c, ref_out):
            last = x
    return last


def measure(px, w, h, roi, corner, ref_in, ref_out, step=1):
    x0, y0, rw, rh = roi
    x1, y1 = x0 + rw, y0 + rh
    prof = []
    for y in range(y0, min(y1, h - 1) + 1, step):
        e = edge_x(px, w, h, y, ref_in, ref_out, x0, x1)
        if e is not None:
            prof.append((y, e))
    if len(prof) < 4:
        return None
    ex = max(e for _y, e in prof)                     # 直边处的右边界
    straight = [y for y, e in prof if e >= ex - 1]
    if not straight:
        return None
    if corner in ("br", "tr"):
        y_flat = max(straight) if corner == "br" else min(straight)
        ye = prof[-1][0] if corner == "br" else prof[0][0]
        y_corner = max(y for y, _e in prof) if corner == "br" else min(y for y, _e in prof)
    else:
        return None
    x_at_corner = dict(prof)[y_corner]
    r_horiz = ex - x_at_corner                      # 横向跨度
    r_vert = abs(y_corner - y_flat)                 # 纵向跨度
    # 逐行反解：以 (ex-r, y_corner-r) 为圆心，看 r 取多少时整条弧都贴合
    best, best_err = None, 1e9
    for r in range(2, 120):
        cx, cy = ex - r, y_corner - r
        err, cnt = 0.0, 0
        for y, e in prof:
            dy = y - cy
            if dy < 0 or dy > r:
                continue
            want = cx + (r * r - dy * dy) ** 0.5
            err += abs(want - e)
            cnt += 1
        if cnt >= max(4, r // 2):
            err /= cnt
            if err < best_err:
                best, best_err = r, err
    return {"ex": ex, "y_corner": y_corner, "y_flat": y_flat,
            "r_horiz": r_horiz, "r_vert": r_vert,
            "r_fit": best, "fit_err": round(best_err, 2), "n": len(prof)}


def edges(px, w, h, ys, xr, minjump=5):
    """逐行找**最大色阶跳变**的位置。

    用于"底色本身是渐变"的场合（材质底下的壁纸是渐变的，取固定参考色会失效，
    而这条 4% 白的台阶在任何底色上都还在，只是绝对值跟着飘）。
    ⇒ 找局部跳变比找"离哪个参考色近"稳得多。
    """
    out = []
    for y in ys:
        best = (0, 0)
        for x in range(xr[0], min(xr[1], w - 2)):
            a, b = px(x, y)[:3], px(x + 1, y)[:3]
            j = abs(sum(a) - sum(b))
            if j > best[1]:
                best = (x, j)
        if best[1] >= minjump:
            out.append((y, best[0], best[1]))
    return out


def vedges(px, w, h, xs, yr, minjump=5):
    """按列找**纵向**最大色阶跳变（水平边的位置）。

    和 edges() 是一对：edges 走行找竖边、vedges 走列找横边。
    量"某个盒子有多高"、"圆角的直边从哪一行开始"都要用它。
    """
    out = []
    for x in xs:
        best = (0, 0)
        for y in range(yr[0], min(yr[1], h - 2)):
            a, b = px(x, y)[:3], px(x, y + 1)[:3]
            j = abs(sum(a) - sum(b))
            if j > best[1]:
                best = (y, j)
        if best[1] >= minjump:
            out.append((x, best[0], best[1]))
    return out


def vprofile(px, w, h, x, yr, minjump=4):
    """某一列的**全部**跳变（不只最大的那个）。

    为什么需要：盒子与底色之间往往只差 4%（十几阶梯），而文字与底色差 300+。
    只报最大值 ⇒ 永远只能看到文字，看不到盒子的边。
    """
    rows = []
    for y in range(yr[0], min(yr[1], h - 2)):
        a, b = px(x, y)[:3], px(x, y + 1)[:3]
        j = abs(sum(a) - sum(b))
        if j >= minjump:
            rows.append((y, j, hexs(a), hexs(b)))
    return rows


def inkbands(px, w, h, yr, thr, mincount, xr=None):
    """按行统计"亮像素"个数，把连续的亮行合并成**墨迹带**。

    用途：一眼看出"这段文字到底占哪几行"、"它有没有跑到盒子外面"。
    盒子边框只有 4~7% 白（十几阶梯），用阈值一卡就分开了 —— 只有文字会亮过阈值。
    """
    x0, x1 = xr or (0, w - 1)
    bands, cur = [], None
    for y in range(yr[0], min(yr[1], h - 1)):
        n = 0
        for x in range(x0, min(x1, w - 1) + 1):
            c = px(x, y)[:3]
            if (c[0] + c[1] + c[2]) / 3.0 >= thr:
                n += 1
        if n >= mincount:
            cur = [y, y, n] if cur is None else [cur[0], y, max(cur[2], n)]
        elif cur is not None:
            bands.append(cur)
            cur = None
    if cur is not None:
        bands.append(cur)
    return bands


def main():
    a = sys.argv[1:]
    if not a:
        raise SystemExit(__doc__)
    img = a[0]
    w, h, px = load_png(img)
    if "--ink" in a:
        thr, minc = [int(v) for v in a[a.index("--ink") + 1].split(",")]
        yr = [int(v) for v in a[a.index("--yr") + 1].split(",")]
        xr = [int(v) for v in a[a.index("--xr") + 1].split(",")] if "--xr" in a else None
        print("%s  %dx%d   墨迹带（阈值 %d，最少 %d px/行，xr=%s）" % (
            os.path.basename(img), w, h, thr, minc, xr))
        for y0, y1, n in inkbands(px, w, h, yr, thr, minc, xr):
            print("   y %3d..%-3d  高 %2d   最宽 %d px" % (y0, y1, y1 - y0 + 1, n))
        return
    if "--vprofile" in a:
        x = int(a[a.index("--vprofile") + 1])
        yr = [int(v) for v in a[a.index("--yr") + 1].split(",")]
        mj = int(a[a.index("--minjump") + 1]) if "--minjump" in a else 4
        print("%s  %dx%d   x=%d 的纵向剖面（minjump=%d）" % (
            os.path.basename(img), w, h, x, mj))
        for y, j, ca, cb in vprofile(px, w, h, x, yr, mj):
            print("   y=%3d  %s → %s   跳变 %d" % (y, ca, cb, j))
        return
    if "--vedge" in a:
        xs = [int(v) for v in a[a.index("--vedge") + 1].split(",")]
        yr = [int(v) for v in a[a.index("--yr") + 1].split(",")]
        mj = int(a[a.index("--minjump") + 1]) if "--minjump" in a else 5
        print("%s  %dx%d   逐列最大纵跳变（xs=%s, yr=%s, minjump=%d）" % (
            os.path.basename(img), w, h, xs, yr, mj))
        for x, y, j in vedges(px, w, h, xs, yr, mj):
            print("   x=%4d  边界 y=%3d   跳变 %2d" % (x, y, j))
        return
    if "--edge" in a:
        ys = range(*[int(v) for v in a[a.index("--edge") + 1].split(":")])
        xr = [int(v) for v in a[a.index("--xr") + 1].split(",")]
        mj = int(a[a.index("--minjump") + 1]) if "--minjump" in a else 5
        print("%s  %dx%d   逐行最大跳变（xr=%s, minjump=%d）" % (
            os.path.basename(img), w, h, xr, mj))
        prev = None
        for y, x, j in edges(px, w, h, ys, xr, mj):
            d = "" if prev is None else "  Δx=%+d" % (x - prev)
            prev = x
            print("   y=%3d  边界 x=%3d   跳变 %2d%s" % (y, x, j, d))
        return
    if "--runs" in a:
        for y in [int(v) for v in a[a.index("--runs") + 1].split(",")]:
            print("── y=%d ──" % y)
            acc, x = [], 0
            for c, n in runs(px, w, y):
                print("   x %4d..%-4d  %-9s  len %d" % (x, x + n - 1, hexs(c), n))
                x += n
        return

    def rgb(flag):
        return tuple(int(v) for v in a[a.index(flag) + 1].split(","))

    roi = [int(v) for v in a[a.index("--roi") + 1].split(",")]
    corner = a[a.index("--corner") + 1] if "--corner" in a else "br"
    ref_in, ref_out = rgb("--in"), rgb("--out")
    r = measure(px, w, h, roi, corner, ref_in, ref_out)
    print("%s  %dx%d" % (os.path.basename(img), w, h))
    if not r:
        print("  ⚠ 采样点不足，什么也没量到（ROI 或两个参考色给错了？）")
        return
    print("  直边右界 x=%d，角底行 y=%d（直边纵向从 y=%d 起）"
          % (r["ex"], r["y_corner"], r["y_flat"]))
    print("  横向跨度 = %d px   纵向跨度 = %d px   ← 两者应当接近（是圆才相等）"
          % (r["r_horiz"], r["r_vert"]))
    print("  圆弧反解 R = %s px（拟合误差 %s，用 %d 行）"
          % (r["r_fit"], r["fit_err"], r["n"]))


if __name__ == "__main__":
    main()
