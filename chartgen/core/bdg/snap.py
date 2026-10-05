# -*- coding: utf-8 -*-
"""写回**吸附**：宿主 `store.snapped()` 的对等实现 + 写回审计。

★★ 本文件按**宿主源码**逐行对齐（`vendor/beat_data_generator/src/renderer/src/store.ts`）：

```ts
315  const round = (b: number): number => Math.round(b * 1e6) / 1e6;
317  function snapped(b: number): number {
318    return store.ui.snapEnabled ? snapBeat(b, store.ui.snapDiv) : round(b);
319  }
...
736  export function moveMarker(id: string, rawBeat: number, force = false): boolean {
744    const beat = Math.max(0, force ? round(rawBeat) : snapped(rawBeat));
```
`tempo.ts:109`：
```ts
109  export function snapBeat(beat: number, div: number): number {
110    if (div <= 1) return Math.round(beat);
111    const step = 1 / div;
112    return Math.round(beat / step) * step;
```

⇒ **三种写入的精度完全不同，别混为一谈**（我原来第一次就混了）：

| 情形 | 公式 | 精度 / 损失 |
|---|---|---|
| 吸附开，`div=1` | `Math.round(beat)` | **整拍** ⇒ 最坏 0.5 拍 |
| 吸附开，`div=N` | 吸到 1/N 网格 | 1/4 档最坏 0.1186 拍 ≈ 37ms |
| **吸附关** | `round(b)` = `Math.round(b*1e6)/1e6` | **1e-6 拍 ≈ 0.0003ms ⇒ 无损** |
| `moveMarker(..., force=true)` | `round(b)` | **无损**（但插件 API 没暴露 `force`） |

⇒ 结论：**写回无损是可行的** —— 要么让用户在 BDG 里**关掉吸附**，
要么他给 `api.project.edit.moveMarker` 加一个可选 `force`（向后兼容）。
本节不猜路径，只把「会被吃掉多少」算清楚。
"""

from __future__ import annotations

import math

from . import aliases as al

# 宿主时间轴给用户选的吸附档位（1/1 … 1/16 拍）
SNAP_DIVS = (1, 2, 4, 6, 8, 12, 16)
BUDGET_MS = 25.0                     # 与 `core/dp_angle.py` 的 SKEW_MAX_MS 同口径
FALLBACK_MS_PER_BEAT = 500.0         # 没有 tempo 时的兜底换算（120bpm）


def js_round(x: float) -> float:
    """JS 的 `Math.round`（半数向 +∞），Python 的 `round` 是银行家舍入，**不能直接用**。"""
    return float(math.floor(x + 0.5))


def round_prec(beat: float, digits: int = al.PRECISION_DIGITS) -> float:
    """宿主 `round()` —— 关吸附路径，精度 `1e-6` 拍。"""
    f = 10.0 ** digits
    return js_round(beat * f) / f


def snap_beat(beat: float, div: int) -> float:
    """宿主 `snapBeat()`；`div <= 1` = **整拍**。"""
    if not div or div <= 1:
        return js_round(beat)
    step = 1.0 / float(div)
    return js_round(beat / step) * step


def snapped(beat: float, div: int = 4, snap_enabled: bool = True) -> float:
    """宿主 `snapped()`。"""
    if snap_enabled:
        return snap_beat(beat, div)
    return round_prec(beat)


def _drift_ms(tempo, beat: float, beat2: float) -> float:
    if tempo is not None:
        try:
            return abs(tempo.time_of_beat(beat2) - tempo.time_of_beat(beat))
        except Exception:                                   # noqa: BLE001
            pass
    return abs(beat2 - beat) * FALLBACK_MS_PER_BEAT


def _row(points, tempo, div, snap_enabled, budget_ms):
    moved = 0
    over = 0
    worst_b = 0.0
    worst_ms = 0.0
    for p in points:
        b2 = snapped(p.beat, div, snap_enabled)
        d = abs(b2 - p.beat)
        if d <= 1e-12:
            continue
        moved += 1
        dm = _drift_ms(tempo, p.beat, b2)
        if dm > worst_ms:
            worst_ms, worst_b = dm, d
        if dm > budget_ms:
            over += 1
    return {"div": div, "snap_on": snap_enabled, "n_moved": moved,
            "total": len(points), "worst_beat": worst_b,
            "worst_ms": worst_ms, "over": over}


def audit(points, tempo=None, divs=SNAP_DIVS, budget_ms: float = BUDGET_MS) -> dict:
    """逐档审计 + **关吸附（无损）**对照行。"""
    pts = list(points)
    rows = [_row(pts, tempo, d, True, budget_ms) for d in divs]
    no_snap = _row(pts, tempo, 4, False, budget_ms)
    return {"rows": rows, "no_snap": no_snap, "budget_ms": budget_ms, "total": len(pts)}


def plan(points, tempo=None, div: int = 4, divs=SNAP_DIVS,
         budget_ms: float = BUDGET_MS):
    """`(结论, 依据)` —— 在**用户当前那一档** `div` 上判定写回路径。

    结论三态：

      · `al.SNAP_PLAN_SAFE`     —— 点本来就在网格上，直接写
      · `al.SNAP_PLAN_OFF`      —— 这一档**有超预算**的点 ⇒ **必须关吸附**（或 `force`）
      · `al.SNAP_PLAN_READBACK` —— 会挪但不超预算 ⇒ 可吸附，**仍必须回读逐点比对**
    """
    rep = audit(points, tempo, divs, budget_ms)
    at = next((r for r in rep["rows"] if r["div"] == div), rep["rows"][-1])
    if at["n_moved"] == 0:
        verdict = al.SNAP_PLAN_SAFE
    elif at["over"] > 0:
        verdict = al.SNAP_PLAN_OFF
    else:
        verdict = al.SNAP_PLAN_READBACK
    out = dict(rep)
    out["div"] = div
    out[al.K_AT] = at
    out["way"] = verdict
    return verdict, out


def report_text(points, tempo=None, div: int = 4, divs=SNAP_DIVS,
                budget_ms: float = BUDGET_MS) -> str:
    way, rep = plan(points, tempo, div, divs, budget_ms)
    out = ["[写回吸附审计] 点 {} 个 · 预算 {:.0f}ms · 当前档 1/{} · 结论 {}".format(
        rep["total"], budget_ms, div, way)]
    for r in rep["rows"]:
        mark = " ←当前" if r["div"] == div else ""
        out.append("    吸附开 1/{:<2d} 挪 {:>4d}/{} 点  最坏 {:.4f} 拍 / {:>8.2f} ms  "
                   "超预算 {:>4d}{}".format(r["div"], r["n_moved"], r["total"],
                                            r["worst_beat"], r["worst_ms"],
                                            r["over"], mark))
    no_snap = rep["no_snap"]
    out.append("    ★ 吸附**关**（store.ts:315 `round(b*1e6)/1e6`）挪 {} 点  "
               "最坏 {:.4f} 拍 / {:.5f} ms ⇒ **实践上无损**".format(
                   no_snap["n_moved"], no_snap["worst_beat"], no_snap["worst_ms"]))
    out.append("    （`div=1` 是整拍吸附 `Math.round(beat)`，最坏 0.5 拍 ≈ 156ms —— "
               "**不是「关吸附」**）")
    return "\n".join(out)
