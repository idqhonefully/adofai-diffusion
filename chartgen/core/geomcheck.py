# -*- coding: utf-8 -*-
"""**几何体检**：给一份谱面打分 —— 它是"一条路"，还是"一把碎渣"？

    from core.geomcheck import metrics
    m = metrics(chart)      # chart 只要有 floors（Floor.travel / .x / .y）

为什么需要它（2026-10 的教训）：ASGORE 60s 那一版**时序校验 0.0us 全过**，
交付出去用户一看截图：「为什么碎成这样了」。因为
  · `core.verify` 只证**按键时刻**对得上；
  · 谁也没看**砖块在平面上长什么样**。
⇒ 时序校验**不能**代替几何体检。生成完一份谱，两个都要看。

判据（都是**相对**量，可跨谱比较；实测基线见 `docs/60` §15）：

| 指标 | 一条正常的路 | 碎渣 |
|---|---|---|
| `overlap` 非相邻重叠对 | 个位数~几十 | **上千** |
| `fold_frac` 回折率（\\|Δ角\\|≥135°） | ≈0 | 20%~60% |
| `hairpin_frac` 发卡弯（travel<60 或 >300） | 0~5% | 25%+ |
| `area_per_tile` 每格占地（格²） | 几十~几百 | **< 5** |

★ 只看单项会被误导：ASGORE 原谱的 `fold_frac` 也有 24.6%（它本来就是难谱），
但它 `area_per_tile` 是 97、`overlap` 只有 537/5268 格 ⇒ 那是**有结构**的折返。
**碎渣的签名是"高重叠 + 每格地皮极小"**，不是"折返多"。
"""
from __future__ import annotations

import math

__all__ = ["metrics", "verdict", "OVL_RADIUS", "report_lines"]

#: 判定"两格在同一位置"的距离（单位：格；与 `core.track_fx.LOOP_RADIUS` 同源）
OVL_RADIUS = 0.35
#: 重叠只在 ±这么宽的窗口里两两比（O(n·W)，够用）
_WIN = 48


def metrics(ch, *, win: int = _WIN, radius: float = OVL_RADIUS) -> dict:
    """→ 一份几何体检报告（纯数据）。"""
    from .path import Path

    fs = ch.floors
    n = len(fs)
    if n == 0:
        return {"n": 0}
    pth = Path.from_floors(fs, [bool(getattr(f, "twirl", False)) for f in fs],
                           radius=1.0)
    pos = [pth.position_of(i) for i in range(n)]
    tv = [float(getattr(f, "travel", 180.0)) for f in fs]
    turns = [None] + [180.0 - t for t in tv[1:]]      # Δ角 = 180 − travel（带符号要另算）

    n_straight = sum(1 for t in tv if abs(t - 180.0) < 1e-6)
    n_hairpin = sum(1 for t in tv if t < 60.0 or t > 300.0)
    dv = [abs(x) for x in turns[1:] if x is not None]
    n_fold = sum(1 for x in dv if x >= 135.0)
    xs = [p[0] for p in pos]
    ys = [p[1] for p in pos]
    w = max(xs) - min(xs)
    h = max(ys) - min(ys)
    ov = 0
    for i in range(n):
        for j in range(i + 2, min(n, i + win + 1)):
            if math.hypot(pos[i][0] - pos[j][0], pos[i][1] - pos[j][1]) < radius:
                ov += 1
    return {
        "n": n,
        "straight_frac": n_straight / n,
        "hairpin_frac": n_hairpin / n,
        "fold_frac": (n_fold / len(dv)) if dv else 0.0,
        "mean_abs_turn": (sum(dv) / len(dv)) if dv else 0.0,
        "overlap": ov,
        "overlap_per_tile": ov / n,
        "bbox_w": w,
        "bbox_h": h,
        "area_per_tile": (max(w, 1e-9) * max(h, 1e-9)) / n,
        "compactness": n / max(math.hypot(w, h), 1e-9),
    }


#: 判定"碎渣"的阈值（保守：宁可不报，也不误伤本来就难的谱）
BAD_OVERLAP_PER_TILE = 0.35      # 每格超过这么多对重叠
BAD_AREA_PER_TILE = 8.0          # 每格占地小于这么多格²


def verdict(m: dict) -> tuple[bool, str]:
    """→ (看着像一条路吗, 人话)。**不知道就说不知道**（n 太小不判）。"""
    n = int(m.get("n") or 0)
    if n < 32:
        return True, f"格数太少（{n}）不判"
    bad = []
    if m["overlap_per_tile"] > BAD_OVERLAP_PER_TILE:
        bad.append(f"每格重叠 {m['overlap_per_tile']:.2f} 对（>{BAD_OVERLAP_PER_TILE}）")
    if m["area_per_tile"] < BAD_AREA_PER_TILE:
        bad.append(f"每格占地只有 {m['area_per_tile']:.1f} 格²（<{BAD_AREA_PER_TILE}）")
    if bad:
        return False, "★ **像一把碎渣**：" + "；".join(bad)
    return True, (f"像一条路（重叠 {m['overlap_per_tile']:.2f} 对/格 · "
                  f"每格占地 {m['area_per_tile']:.1f} 格² · "
                  f"回折 {m['fold_frac']:.1%}）")


def report_lines(m: dict, *, prefix: str = "   ") -> list[str]:
    ok, why = verdict(m)
    return [
        f"{prefix}几何体检: {'✓' if ok else '✗'} {why}",
        f"{prefix}  直线 {m['straight_frac']:.1%} · 发卡弯 {m['hairpin_frac']:.1%} · "
        f"回折 {m['fold_frac']:.1%} · 平均|Δ角| {m['mean_abs_turn']:.1f}°",
        f"{prefix}  重叠 {m['overlap']} 对 · bbox {m['bbox_w']:.1f}×{m['bbox_h']:.1f} 格 · "
        f"每格 {m['area_per_tile']:.1f} 格²",
    ]
