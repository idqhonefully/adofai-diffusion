"""规则层：生成完自检，把「不合格」的地方点出来。

这些是用户明确定下的硬规则（不是我的猜测）：

1. **Twirl 和 SetSpeed 不能在同一格。** 社区一律判不合格。
2. **速度档只用 2 的幂**（×2 / ×4 / ×8 及其倒数）。方便解谱、读谱。
   —— 本文件里的档位 `k` 是 **BPM 除数**：该层 BPM = base_bpm / k，行星速度 = 1/k。
       k = 2  → 行星慢一半（×1/2）
       k = 1/2 → 行星快一倍（×2）
3. **不出现回头方块**：travel ∈ [15°, 345°]。
   （游戏 scrLevelMaker：angleMoved ≤ 1e-6 或 ≥ 2π 会被强制成 2 拍）
4. **第 0 层（开局站位）travel 必须 = 180°**，即 angleData[0] = 0。
   真谱 95.6% 如此。
5. **不出现中旋**（angleData 里没有 999）。

重叠（遮挡）**不在这里查** —— 用户明确说「没必要查那么仔细，避免一下就行」。
`path_overlap.py` 只做提示，不做判定。
"""
from __future__ import annotations

import math

from .solve import Chart, SPEED_POW2, TRAVEL_HARD_MIN

FIRST_TRAVEL = 180.0


def _is_pow2_ratio(k: float, tol: float = 1e-6) -> bool:
    if k <= 0:
        return False
    e = math.log2(k)
    return abs(e - round(e)) < tol


def check_chart(ch: Chart) -> list[dict]:
    """返回违规清单：[{level, code, floor, msg}, ...]。空 = 全过。"""
    out: list[dict] = []
    if not ch.floors:
        return [{"level": "error", "code": "empty", "floor": None, "msg": "谱面为空"}]

    speeds = dict(ch.set_speed_floors)          # floor -> bpm
    twirls = {i for i, f in enumerate(ch.floors) if f.twirl}

    # ① Twirl 与 SetSpeed 同格
    for i in sorted(twirls & set(speeds)):
        out.append({"level": "error", "code": "twirl_on_setspeed", "floor": i,
                    "msg": f"第 {i} 格同时有 Twirl 和 SetSpeed —— 社区判定不合格"})

    # ② 速度档必须都是 2 的幂
    #   **魔法阵（雪花）豁免**：绝对匀速要求 speed ∝ 该格转角，档位必然不是 2 的幂。
    #   这是用户明确给的例外（「只要最后的图形+速度匀速即可」）。
    sno = {i for i, f in enumerate(ch.floors) if getattr(f, "snowflake", False)}
    normal = [f for i, f in enumerate(ch.floors) if i not in sno]
    for f in normal:
        k = f.speed_k
        if not _is_pow2_ratio(k):
            out.append({"level": "error", "code": "speed_not_pow2", "floor": None,
                        "msg": f"速度档 {k:.6g} 不是 2 的幂（行星速度 {1.0/k:.3g}×）"})
            break
    kset = {f.speed_k for f in normal}
    for k in sorted(kset):
        if not _is_pow2_ratio(k):
            out.append({"level": "error", "code": "speed_not_pow2", "floor": None,
                        "msg": f"出现非 2 的幂档位 k={k:.6g}"})
    if sno:
        out.append({"level": "info", "code": "snowflake_speed", "floor": None,
                    "msg": f"魔法阵段 {len(sno)} 格的速度档不受 2 的幂约束（绝对匀速需要）"})

    # ③ 回头方块 / travel 越界
    #   ★ 例外：**专门为双押插入**的折返格与 midspin 格（见 core.dp_midspin），
    #     以及角度双押的「薄格」（travel = θ = 15°/30°，见 core.dp_angle）。
    #     它们是双押机制本身要的形状，不是手滑画出来的回头方块。
    dp_pole = set()
    for (_ix, _iy, _ib) in (ch.meta.get("dp_pairs") or []):
        dp_pole.update((_ix, _iy))
    for i, f in enumerate(ch.floors):
        if i in dp_pole:
            continue
        # ★ 2026-10：带 1e-9 容差 —— 阶梯 (`core/ladder.rung_ok`) 放行的上界是
        #   `345 + 1e-9`，而 `travel = 180·r/k` 是浮点算的，实测会给出
        #   `345.0000000000035` 这种值：阶梯说合法、这里却按「回头方块」报 error
        #   （实测 1 格，把状态栏染成违规）。①口径与 ③b/③c 统一。
        if f.travel < 15.0 - 1e-9 or f.travel > 345.0 + 1e-9:
            out.append({"level": "error", "code": "turnaround", "floor": i,
                        "msg": f"第 {i} 格 travel={f.travel:.2f}° → 回头方块"})
            break

    # ③b 最小角度（用户口径 2026-10）：非双押格不许低于 `chart.meta["travel_min"]`
    tmin = float(ch.meta.get("travel_min") or 0.0)
    if tmin > 0:
        lows = [i for i, f in enumerate(ch.floors)
                if i not in dp_pole and f.travel < tmin - 1e-9]
        if lows:
            i0 = lows[0]
            out.append({"level": "error", "code": "travel_below_min", "floor": i0,
                        "msg": (f"{len(lows)} 格低于最小角度 {tmin:g}°"
                                f"（最先：第 {i0} 格 travel={ch.floors[i0].travel:.2f}°）")})

    # ③c 最大夹角（用户口径 2026-10 第二版）：非双押格不许高于 `chart.meta["travel_max"]`
    #   `0` = 没设上界（老口径）⇒ 这条不报。
    tmax = float(ch.meta.get("travel_max") or 0.0)
    if tmax > 0:
        highs = [i for i, f in enumerate(ch.floors)
                 if i not in dp_pole and f.travel > tmax + 1e-9]
        if highs:
            i0 = highs[0]
            out.append({"level": "error", "code": "travel_above_max", "floor": i0,
                        "msg": (f"{len(highs)} 格高于最大夹角 {tmax:g}°"
                                f"（最先：第 {i0} 格 travel={ch.floors[i0].travel:.2f}°）")})

    # ④ 第 0 层必须直线
    if abs(ch.floors[0].travel - FIRST_TRAVEL) > 1e-6:
        out.append({"level": "error", "code": "first_not_straight", "floor": 0,
                    "msg": f"第 0 层 travel={ch.floors[0].travel:g}°，应为 180°"})
    if abs(ch.floors[0].angle) > 1e-6 and abs(ch.floors[0].angle - 360.0) > 1e-6:
        out.append({"level": "error", "code": "first_angle", "floor": 0,
                    "msg": f"angleData[0]={ch.floors[0].angle:g}，应为 0"})

    # ⑤ 中旋
    #   ★ 例外：**专门为双押插入**的中旋是合法的（本生成器只在这一处用 999）。
    #     判据：该格在 `ch.meta["dp_pairs"]` 里登记过（见 core.dp_midspin.apply）。
    dp_floors = {iy for (_ix, iy, _ib) in (ch.meta.get("dp_pairs") or [])}
    stray = [i for i, f in enumerate(ch.floors)
             if abs(f.angle - 999.0) < 1e-6 and i not in dp_floors]
    if stray:
        out.append({"level": "error", "code": "midspin", "floor": stray[0],
                    "msg": f"出现非双押的中旋 999（第 {stray[0]} 格起 "
                           f"共 {len(stray)} 格）；本生成器只在双押处用中旋"})
    elif dp_floors:
        out.append({"level": "info", "code": "midspin_dp",
                    "msg": f"{len(dp_floors)} 格中旋（全部为双押插入）"})
    return out


def summary(violations: list[dict]) -> str:
    """只有 level == 'error' 才算不合格；info 只是提示。"""
    errs = [v for v in violations if v.get("level") != "info"]
    infos = [v for v in violations if v.get("level") == "info"]
    if not errs:
        base = "规则检查：全部通过"
        return base + (f"（{infos[0]['msg']}）" if infos else "")
    lines = [f"规则检查：{len(errs)} 条不合格"]
    for v in errs[:8]:
        lines.append(f"   · [{v['code']}] {v['msg']}")
    if len(errs) > 8:
        lines.append(f"   … 还有 {len(errs) - 8} 条")
    for v in infos[:2]:
        lines.append(f"   · [{v['code']}] {v['msg']}")
    return "\n".join(lines)
