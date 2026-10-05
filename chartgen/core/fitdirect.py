# -*- coding: utf-8 -*-
"""**直拟合**：一砖一个音，`travel = 180·(Δt/拍)/k`（`docs/43` §代价表 / `docs/44`）。

## 和 `solve()` 的关系

`core.solve.solve()` 是**最优化**：它把 Δt 当成"音值"，去挑参考音值/速度档/
模板/三连音/雪花，目标是"像人写的谱"。代价是**它会改时序**（量化到音值网格、
用 Pause 填长休止）—— 对**外部来源的抖动时间戳**，那点改动会累积成几十上百毫秒
的漂移（`tools/_ext_ts_go.py` 实测 261s 上漂 155ms）。

直拟合反过来：**时序优先，几何服从**。

  · 每个 onset = 一层（不够就让长间隔拆成几层，见下）；
  · `travel = 180·r/k`，`r = Δt/一拍`，`k` ∈ `SPEED_TIERS`（2 的幂）；
  · ⇒ 每层时长 `= (travel/180)·(60000/(base/k)) = r·60000/base` **恒等于 Δt**
    （`times_from_chart` 是对这个模型的逐字实现）⇒ **时序逐点精确**，零漂移；
  · `k` 只在"保持上一档会让 travel 跑出合法区间"时才换 ⇒ 速度事件少、可读。

配合 `core.denoise`：先把抖动吸到格子，`r` 就是 `m/div` 这种整数格
⇒ `travel` 是整齐的有理数，"逐点精确"从"数学上精确"变成"读起来也精确"。

## 代价（必须说清楚，不许静默）

直线率 = `r/k ≈ 1`（整数格距为 2 的幂）的比例。实测 Flower_Dance 去噪后
**89.6%**；天然谱面（`solve()` 那条路）靠模板/三连音能把直线率抬到 39~42%
—— 两条路各有取舍，用户在界面上选。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import solve as solve_mod
from .solve import (FIRST_TRAVEL, PAUSE_MIN_BEATS, SPEED_TIERS, TRAVEL_HARD_MAX,
                    TRAVEL_HARD_MIN, TRAVEL_SOFT_MAX, TRAVEL_SOFT_MIN, Chart,
                    Floor, SolveParams, _is_straight, _plan_twirls,
                    auto_base_bpm, times_from_chart)

__all__ = ["FitReport", "pick_tiers", "build", "report_text", "MODE",
           "LADDER_DEG", "ladder_unit_ms", "snap_to_ladder"]

MODE = "direct"

#: 一层最多转多少度（游戏硬上限 358°，工程上留余量 ⇒ 用 SOFT 上限分格）
_SPLIT_MAX = 340.0
#: 「保持上一档」的 travel 容许区间（超出就重新挑档）
_KEEP = (TRAVEL_SOFT_MIN, TRAVEL_SOFT_MAX)

# ================================================================ 15° 阶梯
# 用户 2026-10：「加一个**使用激进的拟合策略**，其效果为：拟合的时候**最小角度为 15°**，
#               也就是**只允许 15 30 45 60 75 90……往后**」。
#
# ★ 一个必须写下来的硬事实：**角度不是独立可选的**。一层的角度由
#     `travel = 180 · (该层时值 / 基准砖长) / k`      （k = 速度档，2 的幂）
#   唯一决定。所以「只出现 15° 的整数倍」**等价于**把**时值**吸附到
#     `该层时值 = 基准砖长 · k · j / 12`（j 为正整数，travel = 15j）
#   这一串格子上 —— 也就是说：**要动时值**（这正是「拟合容差」该管的事）。
#   容差 = 允许的最大挪动量（ms）；挪不动的**原样保留并报出来**（不许静默）。
LADDER_DEG = 15.0                 # 阶梯步长（用户口径：15 30 45 60 …）
LADDER_MIN_DEG = 15.0             # 下界：太小观感差，且 `rung_ok` 的硬下界也是 15
LADDER_MAX_DEG = 345.0            # 上界：≈360 会被游戏特判成回头方块，取 345

#: 一层最多占多少个 15° 单位（345/15=23；拆层时每片也受它限制）
_LADDER_UNITS_MAX = 22


def ladder_unit_ms(tile_ms: float, k: float) -> float:
    """**一个 15° 单位**对应的时值（ms）：`基准砖长 · k / 12`。

    （`travel = 180·d/(tile·k) = 15` ⇒ `d = tile·k/12`。）
    """
    return float(tile_ms) * float(k) / 12.0


def _ladder_fit(want: float, unit: float, j_min: int = 1,
                j_max: int = 0) -> tuple[float, int] | None:
    """把「需要多少毫秒」吸到最近的 `unit` 整数倍上 ⇒ `(时值, 单位数 j)`。

    `j_min` / `j_max` = 允许的单位数区间（**用户的角度上下界**：
    `travel = 15·j` ⇒ `j ∈ [ceil(min/15), floor(max/15)]`）；`j_max <= 0` = 不设上界。
    `unit <= 0` / 区间为空 ⇒ `None`（调用方负责原样保留 + 计数）。
    """
    if unit <= 0 or want <= 0:
        return None
    j0 = int(round(want / unit))
    hi = int(j_max) if int(j_max or 0) > 0 else j0 + 1
    best = None
    for j in (j0 - 1, j0, j0 + 1):
        if j < max(1, int(j_min or 1)) or j > hi:
            continue
        d = j * unit
        cost = abs(d - want)
        if best is None or cost < best[0]:
            best = (cost, d, j)
    if best is None:
        return None
    return best[1], best[2]


def _ladder_j_range(travel_min: float, travel_max: float) -> tuple[int, int]:
    """用户角度口径 → 阶梯单位数区间 `j ∈ [j_min, j_max]`（`travel = 15·j`）。"""
    j_min = max(1, int(math.ceil(max(0.0, float(travel_min or 0.0)) / LADDER_DEG - 1e-9)))
    if float(travel_max or 0.0) > 0:
        j_max = int(math.floor(float(travel_max) / LADDER_DEG + 1e-9))
    else:
        j_max = int(LADDER_MAX_DEG // LADDER_DEG)          # 345/15 = 23
    return j_min, max(j_min, j_max)


def snap_to_ladder(ons, ks, *, tile_ms: float, tol_ms: float,
                   travel_min: float = 0.0, travel_max: float = 0.0):
    """把 onset 时刻吸到**能让角度落在 15° 整数倍上**的格子（挪动量 ≤ `tol_ms`）。

    `ks` = 每个**间隔**的速度档（`len(ons)-1` 个，DP 已经挑好）。

    `travel_min` / `travel_max` = 用户的角度口径（`travel = 15·j`）：吸附只在
    `j ∈ [ceil(min/15), floor(max/15)]` 里挑 —— 否则吸附本身会把角度**推出**
    用户给的窗口（26 帧的实测：高 bpm 段 j=1 ⇒ 15°，正是用户不要的）。

    ★ 为什么是逐点**重新锚定**而不是吸「间隔」：吸间隔会让误差**累积**
    （每步错 5ms，一千个音就漂 5 秒）。这里每一步都拿**原始时刻**当锚：
        `新时刻 = 上一新时刻 + (15° 的整数倍时值)`，且该步误差 ≤ tol
    ⇒ 每个音自己的误差 ≤ tol，**不累积**。

    返回 `(新时刻列表, 账)`；账里有挪了几个、挪不动的几个、挪动中位/max。
    """
    t = [float(o.t_ms) for o in ons]
    n = len(t)
    out = [t[0]]
    moves: list[float] = []
    n_raw = 0
    prev = t[0]
    j_min, j_max = _ladder_j_range(travel_min, travel_max)
    for i in range(n - 1):
        k = float(ks[i]) if i < len(ks) else 1.0
        want = t[i + 1] - prev                 # 要落回**原始时刻**所需的时值
        got = _ladder_fit(want, ladder_unit_ms(tile_ms, k),
                          j_min=j_min, j_max=j_max)
        if got is None or abs(got[0] - want) > float(tol_ms) + 1e-9:
            n_raw += 1                          # 挪不动 ⇒ 原样保留
            nxt = t[i + 1]
        else:
            nxt = prev + got[0]
        out.append(nxt)
        moves.append(nxt - t[i + 1])
        prev = nxt
    ms = sorted(abs(m) for m in moves if abs(m) > 1e-9)
    rep = {
        "n": n, "n_moved": sum(1 for m in moves if abs(m) > 1e-9),
        "n_raw": n_raw,
        "move_median_ms": round(ms[len(ms) // 2], 6) if ms else 0.0,
        "move_max_ms": round(ms[-1], 6) if ms else 0.0,
        "move_sum_ms": round(sum(moves), 6),
    }
    return out, rep


def _split_units(j: int, n: int) -> list[int]:
    """把 `j` 个 15° 单位分成 `n` 片（每片 ≥1、尽量均匀）—— 拆层也留在阶梯上。

    旧口径按 `tv/340` 等分 ⇒ 例如 `tv=345` 拆成两片各 **172.5°**（不在 15° 的整数倍上）。
    这里按**整数单位**分：345/15=23 单位 ⇒ `[12, 11]` = 180° + 165°（都在阶梯上）。
    """
    n = max(1, int(n))
    base, rem = divmod(int(j), n)
    parts = [base + 1] * rem + [base] * (n - rem)
    return [max(1, p) for p in parts]


def _clone_onset(o, t_ms: float):
    """复制一个 onset 只改时刻（**不改调用方的对象**：吸附是拟合的局部决定）。"""
    from dataclasses import replace as _replace
    return _replace(o, t_ms=float(t_ms))



@dataclass
class FitReport:
    ok: bool
    mode: str = MODE
    base_bpm: float = 0.0
    period_ms: float = 0.0
    n_onsets: int = 0
    n_floors: int = 0
    n_fill: int = 0              # 长间隔拆出来的**填充层**（不是 onset）
    n_pause: int = 0             # 长休止用「1 直线格 + Pause」代替拆层
    n_setspeed: int = 0
    n_straight: int = 0
    n_switched: int = 0          # k 变档次数
    err_median_ms: float = 0.0
    err_max_ms: float = 0.0
    err_p90_ms: float = 0.0
    travel_min: float = 180.0
    travel_max: float = 180.0
    n_hairpin: int = 0           # 发卡弯：travel < 60 或 > 300（最显眼的那种「怪格子」）
    k_used: tuple = ()
    clamped: int = 0
    # ★ 用了哪些用户参数（`docs/46`）：以前这一栏全是常量，用户设了也没用
    tier_mode: str = "dp"        # dp = 复用 solve 的选档 DP（默认）/ sticky = 旧策略
    travel_min_setting: float = 0.0   # 「最小角度」（SolveParams.travel_min）
    speed_min_run: int = 1            # 一档至少连续几层
    # ★ 15° 阶梯（「使用激进的拟合策略」· 用户 2026-10）—— 默认关，关着逐字节是老口径
    ladder_on: bool = False
    ladder_tol_ms: float = 0.0        # 「拟合容差」= 允许的最大挪动量（ms）
    ladder_iters: int = 0             # 吸附↔选档 迭代了几轮（通常 1~2）
    n_ladder_moved: int = 0           # 被修正到 15° 阶梯上的音
    n_ladder_raw: int = 0             # 挪不动 ⇒ **原样保留**的音（不许静默）
    ladder_move_median_ms: float = 0.0
    ladder_move_max_ms: float = 0.0
    ladder_move_sum_ms: float = 0.0
    n_ladder_bad: int = 0             # 最终产物里**非双押格**不在 15° 整数倍上的层数

    @property
    def straight_frac(self) -> float:
        n = max(1, self.n_onsets - 1)
        return self.n_straight / float(n)

    @property
    def ladder_frac(self) -> float:
        """非双押格里「角度落在 15° 整数倍上」的比例（1.0 = 全都在阶梯上）。"""
        n = max(1, int(self.n_floors) - 2)
        return max(0.0, (n - int(self.n_ladder_bad)) / float(n))

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["straight_frac"] = round(self.straight_frac, 4)
        d["ladder_frac"] = round(self.ladder_frac, 4)
        d["k_used"] = [float(x) for x in self.k_used]
        return d

    def describe(self) -> str:
        s = ("直拟合 bpm {:.4f}（砖长 {:.4f}ms）· {} onset → {} 层{} · "
             "直线 {}/{} = {:.1%} · 换档 {}{} · 时序误差 中位 {:.4f}ms / max {:.4f}ms"
             ).format(self.base_bpm, self.period_ms, self.n_onsets, self.n_floors,
                      "（含填充 {} / 休止 {}）".format(self.n_fill, self.n_pause)
                      if (self.n_fill or self.n_pause) else "",
                      self.n_straight, max(0, self.n_onsets - 1),
                      self.straight_frac, self.n_switched,
                      "（发卡弯 {}）".format(self.n_hairpin) if self.n_hairpin else "",
                      self.err_median_ms, self.err_max_ms)
        if self.ladder_on:
            s += (" · ★ 15° 阶梯（容差 {:.1f}ms）：修正 {} 音（挪 中位 {:.2f}/max {:.2f}ms）"
                  "· 挪不动原样 {} · 非阶梯格 {}".format(
                      self.ladder_tol_ms, self.n_ladder_moved,
                      self.ladder_move_median_ms, self.ladder_move_max_ms,
                      self.n_ladder_raw, self.n_ladder_bad))
        return s


def pick_tiers_dp(rs, p: SolveParams, *, tiers=None):
    """★ 用**用户那套参数**挑速度档（`docs/46`）。

    以前这里是「粘住上一档，粘不住才重挑」的一句话 —— 结果有两个坏毛病：

      1. **16 分音全成发卡弯**：`r=0.25` 时上一档 `k=1` 给出 `travel=45°`，
         它落在「合法」区间里 ⇒ 粘住不动。可人类会用**加速档**把它铺直
         （`k=1/4` ⇒ `travel=180°`）。这就是「拟合之后出现很多很奇怪的轨道」。
      2. **完全不看用户参数**：`travel_min`（最小角度）、`straight_weight`（直线优先）、
         `speed_switch_penalty`（换档代价）、`speed_min_run`（一档至少几层）
         一个都没用，全被硬编码常量代替。

    现在直接复用 `solve` 那套 DP（`_dp_tiers` + `_merge_short_runs`）——
    **同一条谱、同一套参数，两条路选档的逻辑是一致的**。

    ★ 换档代价这里**故意**传 `None`：`_dp_tiers` 的兜底就是「换档代价 = 直线优先度」，
      于是界面上「直线优先」那一档（少 3.0 / 平衡 1.5 / 多 0.7）**真的**控制
      「更直 vs 更少 SetSpeed」这个取舍。若照搬 `SolveParams.speed_switch_penalty`
      （默认写死 3.0），「直线优先」在直拟合里就是**死参数**——正是用户问的那件事。
      （`solve()` 那条路不动：它的梯子搜索另有 `straight_weight` 的用法，已标定过。）
    """
    ts = tuple(tiers or p.speed_tiers or SPEED_TIERS)
    tv_min = float(getattr(p, "travel_min", TRAVEL_SOFT_MIN) or TRAVEL_SOFT_MIN)
    # ★ 最大夹角（用户口径：≤ 270°）——`0` = 老口径（软上界 340）
    tv_max = float(getattr(p, "travel_max", 0.0) or 0.0)
    travels, ks, _frac, _ev = solve_mod._dp_tiers(
        list(rs), ts, float(p.straight_weight), float(p.speed_penalty),
        None, tv_min, travel_max=tv_max,
        slow_speed_penalty=float(getattr(p, "slow_speed_penalty", 0.0) or 0.0))
    if int(getattr(p, "speed_min_run", 1) or 1) > 1:
        # ★ `keep_straight=True`：并档**不许把直线格变少**。
        #   不加这一条的话，`speed_min_run=6` 会把「直线优先」整个吃掉
        #   （实测 w=3.0/1.5/0.7/0.3 全变成 83~83.6%，参数形同虚设）。
        ks = solve_mod._merge_short_runs(list(ks), list(rs),
                                         int(p.speed_min_run), tv_min,
                                         keep_straight=True, travel_max=tv_max)
        travels = [180.0 * r / k for r, k in zip(rs, ks)]
    return ks, travels


def pick_tiers(rs, tiers=SPEED_TIERS, *, prev: float = 1.0,
               keep=_KEEP, hard=(TRAVEL_HARD_MIN, TRAVEL_HARD_MAX)) -> list[float]:
    """**旧策略**（粘住上一档）—— 留作对照 / `tier_mode="sticky"`。

    策略（确定性，无随机）：**尽量保持上一档**；只有保持会让 `travel` 跑出
    `keep` 区间时才改挑「让 travel 最接近 180」的合法档。

    ★ 默认已经**不用**它了：见 `pick_tiers_dp` 的注释（16 分会变发卡弯 +
      用户参数全被绕开）。它还在，是因为「保真优先、尽量不动速度档」也是一种取向，
      而且 `docs/45` 的代价对比表要用它。
    """
    ts = sorted(set(float(t) for t in tiers))
    out = []
    for r in rs:
        tv = 180.0 * r / prev
        if keep[0] <= tv <= keep[1]:
            out.append(prev)
            continue
        best, bestc = None, None
        for k in ts:
            t2 = 180.0 * r / k
            if t2 < hard[0] or t2 > hard[1]:
                continue
            # 先要"直"（离 180 近），再要"档位省"（离上一档近）
            cost = (abs(t2 - 180.0), abs(k - prev), abs(k))
            if bestc is None or cost < bestc:
                best, bestc = k, cost
        if best is None:                      # 没有合法档：取最接近的（会被 clamp 掉）
            best = min(ts, key=lambda k: (abs(180.0 * r / k - 180.0), abs(k - prev)))
        out.append(best)
    return out


def build(onsets, p: SolveParams | None = None, *, base_bpm: float = 0.0,
          tiers=SPEED_TIERS, keep=_KEEP, tier_mode: str = "dp",
          use_pause: bool | None = None,
          angle_ladder: bool = False, angle_tol_ms: float = 0.0
          ) -> tuple[Chart, FitReport]:
    """把 onsets 直拟合成功率谱面 `(Chart, FitReport)`。

    `tier_mode`：
      · `"dp"`（默认）—— 用 `solve` 那套 DP + 用户参数（最小角度 / 直线优先 /
        换档代价 / 一档最少层数），见 `pick_tiers_dp`；
      · `"sticky"` —— 旧的「粘住上一档」（对照用）。
    `use_pause`：长休止用**一个直线格 + Pause 事件**表示，而不是硬拆成好几个大角度格
      （默认跟随 `p.use_pause`；阈值 `p.pause_min_beats`）。

    ★ 2026-10「使用激进的拟合策略」（`angle_ladder=True`）：把每一层都吸到
      **15° 整数倍**的角度上（= 把时值吸到「基准砖长·k/12」的格子上），
      挪动量**每个音都不超过 `angle_tol_ms`**（=「拟合容差」），挪不动的原样保留。
      只走这条路（`fit_mode=direct`）；默认 `False` ⇒ 老口径逐字节不变。
    """
    p = p or SolveParams()
    ons = sorted(onsets, key=lambda o: o.t_ms)
    if len(ons) < 2:
        raise ValueError("直拟合至少要 2 个 onset")
    bb = float(base_bpm or p.base_bpm or 0.0)
    if bb <= 0:
        bb = float(auto_base_bpm(ons, p.target_travel))
    if bb <= 0:
        raise ValueError("定不出 base_bpm")
    period = 60_000.0 / bb

    def _rs_of(o):
        return [(o[i].t_ms - o[i - 1].t_ms) / period for i in range(1, len(o))]

    def _ks_of(rs_):
        if tier_mode == "sticky":
            return pick_tiers(rs_, tiers, prev=1.0, keep=keep)
        return pick_tiers_dp(rs_, p, tiers=tiers)[0]

    # ---- 15° 阶梯：吸附 ↔ 选档 迭代（吸附会轻微改时值 ⇒ 档位可能跟着变）----
    ladder_rep: dict | None = None
    ladder_iters = 0
    ons_orig = ons
    if angle_ladder and len(ons) >= 2:
        cur = ons
        for _it in range(3):
            ladder_iters = _it + 1
            ks_ = _ks_of(_rs_of(cur))
            ts_new, _r_l = snap_to_ladder(cur, ks_, tile_ms=period,
                                          tol_ms=float(angle_tol_ms or 0.0),
                                          travel_min=float(getattr(p, "travel_min", 0.0) or 0.0),
                                          travel_max=float(getattr(p, "travel_max", 0.0) or 0.0))
            if all(abs(a.t_ms - b) < 1e-9 for a, b in zip(cur, ts_new)):
                break                                  # 已经在阶梯上 ⇒ 收敛
            cur = [_clone_onset(o, t) for o, t in zip(cur, ts_new)]
        ons = cur

    rs = _rs_of(ons)
    tier_mode = str(tier_mode or "dp").lower()
    ks = _ks_of(rs)
    # ★ 阶梯的账：**最终 vs 原始**（逐轮累加才是用户看到的「挪了多少」）；
    #   「挪不动」按**最终档位**再判一次 —— 与产物一致，不许自说自话。
    if angle_ladder:
        tol_f = float(angle_tol_ms or 0.0)
        moves = [b.t_ms - a.t_ms for a, b in zip(ons_orig, ons)]
        ms_abs = sorted(abs(m) for m in moves if abs(m) > 1e-9)
        n_bad = 0
        for i in range(len(ons) - 1):
            want = ons[i + 1].t_ms - ons[i].t_ms
            got = _ladder_fit(want, ladder_unit_ms(period, ks[i]))
            if got is None or abs(got[0] - want) > tol_f + 1e-9:
                n_bad += 1
        ladder_rep = {
            "n": len(ons), "n_moved": len(ms_abs), "n_raw": n_bad,
            "move_median_ms": round(ms_abs[len(ms_abs) // 2], 6) if ms_abs else 0.0,
            "move_max_ms": round(ms_abs[-1], 6) if ms_abs else 0.0,
            "move_sum_ms": round(sum(moves), 6),
        }
    do_pause = bool(p.use_pause) if use_pause is None else bool(use_pause)
    pause_min = float(getattr(p, "pause_min_beats", 0.0) or 0.0)

    # ---- 每个间隔 → 这一段的层：正常 1 层；长休止「1 层 + Pause」；再不行才拆层 ----
    plan: list[list[tuple[float, float, float]]] = []   # [(r, k, pause_beats), …]
    n_fill = n_pause = 0
    tier_list = sorted(set(float(t) for t in (tiers or p.speed_tiers or SPEED_TIERS)))
    pause_floor = max(PAUSE_MIN_BEATS, pause_min)
    for i, (r, k) in enumerate(zip(rs, ks)):
        # ★ 长休止用「**一个直线格 + Pause 事件**」表示（`docs/24` 的等待拍），
        #   而不是硬拆成好几个大角度格 —— 后者正是「看起来一堆怪格子」的来源。
        #   时长照样精确：duration = (travel/180 + pb)·60000·k/base，
        #   取 travel = 180 ⇒ pb = r/k − 1。
        if do_pause and r >= pause_floor:
            k_cap = r / (1.0 + pause_floor)          # 让 pb ≥ pause_floor
            pick = None
            if k <= k_cap:
                pick = k                              # 沿用本层的档：不额外产生 SetSpeed
            else:
                cands = [t for t in tier_list if t <= k_cap]
                if cands:
                    pick = cands[-1]
                elif k <= r:                          # 退一步：pb ≥ 0 就行
                    pick = k
            if pick:
                pb = r / pick - 1.0
                if pb >= PAUSE_MIN_BEATS - 1e-9:
                    plan.append([(pick, pick, pb)])
                    n_pause += 1
                    continue
        tv = 180.0 * r / k
        units = 12.0 * r / k                  # 这一格值多少个 15°（阶梯模式下是整数）
        if angle_ladder and abs(units - round(units)) < 1e-6 and round(units) >= 1:
            # ★ 阶梯模式：拆层也按**整数个 15°** 拆（否则会拆出 172.5° 这种非阶梯角）
            j = int(round(units))
            n = max(1, -(-j // _LADDER_UNITS_MAX))
            if n > 1:
                n_fill += n - 1
            parts = _split_units(j, n)
            plan.append([(r * (p_ / float(j)), k, 0.0) for p_ in parts])
            continue
        n = 1
        if tv > _SPLIT_MAX:
            n = int(tv // _SPLIT_MAX) + 1
        if n > 1:
            n_fill += n - 1
        plan.append([(r / n, k, 0.0)] * n)

    floors: list[Floor] = [Floor(travel=FIRST_TRAVEL, bpm=bb, twirl=False,
                                 turn=0.0, heading=0.0, angle=0.0, speed_k=1.0)]
    entry: list[int] = []                          # 第 j 个 onset 的层下标
    for i, seg in enumerate(plan):
        entry.append(len(floors))
        for r, k, pb in seg:
            floors.append(Floor(travel=180.0 * r / k, bpm=bb / k, twirl=False,
                                turn=0.0, heading=0.0, angle=0.0, speed_k=k,
                                pause_beats=pb))
    tail_k = ks[-1]
    floors.append(Floor(travel=FIRST_TRAVEL, bpm=bb / tail_k, twirl=False,
                        turn=0.0, heading=0.0, angle=0.0, speed_k=tail_k))
    # ★ 最后一个 onset 落在**尾层**上（区间只有 n−1 个，循环只给出前 n−1 个）
    entry.append(len(floors) - 1)

    ch = Chart(base_bpm=bb)
    ch.floors = floors
    ch.first_onset_ms = ons[0].t_ms
    # ★ `_plan_twirls` 内部就调 `_finish`（→ `_apply` → 写回 heading/angle/twirl），
    #   所以这里**不能再调一次** `_finish`（那会拿旧的 flips 又跑一遍挪位）。
    _plan_twirls(floors, p)

    # ---- 自检：按游戏模型反算 entryTime，逐点比 ----
    tt = times_from_chart(ch)
    base = tt[entry[0]]
    errs = [abs((tt[entry[j]] - base) - (ons[j].t_ms - ons[0].t_ms))
            for j in range(len(ons))]
    es = sorted(errs)
    rep = FitReport(ok=True, base_bpm=bb, period_ms=period, n_onsets=len(ons),
                    n_floors=len(floors), n_fill=n_fill)
    rep.n_setspeed = sum(1 for i in range(1, len(floors))
                         if abs(floors[i].bpm - floors[i - 1].bpm) > 1e-9)
    rep.n_straight = sum(1 for f in floors[1:-1] if _is_straight(f.travel))
    rep.n_switched = sum(1 for a, b in zip(ks, ks[1:]) if a != b)
    rep.n_pause = n_pause
    rep.tier_mode = tier_mode
    rep.travel_min_setting = round(float(getattr(p, "travel_min", 0.0) or 0.0), 3)
    rep.speed_min_run = int(getattr(p, "speed_min_run", 1) or 1)
    rep.err_median_ms = round(es[len(es) // 2], 6)
    rep.err_p90_ms = round(es[int(0.9 * (len(es) - 1))], 6)
    rep.err_max_ms = round(es[-1], 6)
    rep.travel_min = round(min((f.travel for f in floors[1:-1]), default=180.0), 6)
    rep.travel_max = round(max((f.travel for f in floors[1:-1]), default=180.0), 6)
    rep.n_hairpin = sum(1 for f in floors[1:-1] if f.travel < 60.0 or f.travel > 300.0)
    rep.k_used = tuple(sorted({float(k) for k in ks}))

    # ★ 15° 阶梯的账（`angle_ladder`）：挪了多少 / 挪不动多少 / 最终还有几层不在阶梯上
    if angle_ladder:
        rep.ladder_on = True
        rep.ladder_tol_ms = round(float(angle_tol_ms or 0.0), 6)
        rep.ladder_iters = ladder_iters
        if ladder_rep:
            rep.n_ladder_moved = int(ladder_rep.get("n_moved") or 0)
            rep.n_ladder_raw = int(ladder_rep.get("n_raw") or 0)
            rep.ladder_move_median_ms = float(ladder_rep.get("move_median_ms") or 0.0)
            rep.ladder_move_max_ms = float(ladder_rep.get("move_max_ms") or 0.0)
            rep.ladder_move_sum_ms = float(ladder_rep.get("move_sum_ms") or 0.0)
        # 最终产物里，**非双押格**（双押/中旋折返是机制需要的形状）的角度
        # 是否都落在 15° 的整数倍上。留一格都不许：数出来并上屏。
        rep.n_ladder_bad = sum(
            1 for f in floors[1:-1]
            if abs(f.travel / LADDER_DEG - round(f.travel / LADDER_DEG)) > 1e-6)

    # ★ 轨道位置偏移（PositionTrack）：与 `solve()` 同一条路（`docs/24 §6`）。
    #   以前直拟合完全没接 —— 界面上开着也没用。
    try:
        if bool(getattr(p, "use_position_track", False)):
            from . import track_fx as _tfx
            _tfx.apply(ch, step=float(getattr(p, "pos_track_step", 0.22)),
                       min_beats=float(getattr(p, "pos_track_min_beats", 8.0)))
        else:
            ch.meta["pos_tracks"] = []
            ch.meta["pos_track_n"] = 0
    except Exception as exc:                                    # noqa: BLE001
        ch.meta["pos_tracks"] = []
        ch.meta["pos_track_n"] = 0
        ch.meta["pos_track_err"] = str(exc)

    ch.meta.update({
        "n_onsets": len(ons), "n_floors": len(floors), "n_lead": 1,
        # ★ onset ↔ 层 的对应表：直拟合会为长间隔拆出**填充层**，
        #   所以「第 i 个 onset 在第 i+1 层」这个老假设**不成立**。
        #   下游（`sidecar.session.rebuild` 的 hit_times / 双押落点）必须用这张表。
        "onset_floors": list(entry),
        "lead_ms": FIRST_TRAVEL / 180.0 * period,
        "duration_ms": ons[-1].t_ms - ons[0].t_ms,
        "fit_mode": MODE, "fit": rep.to_dict(),
        "straight_frac": rep.straight_frac,
        "event_frac": rep.n_setspeed / float(max(1, len(floors))),
        "quant_err_ms": rep.err_median_ms,
        "tick_err_ms": 0.0, "tpl_hits": 0, "tpl_covered": 0,
        "nat_count": 0, "nat_tiles": 0, "eng_count": 0, "eng_tiles": 0,
        "snow_count": 0, "snow_tiles": 0,
        "min_travel_seen": rep.travel_min,
        "travel_min": float(getattr(p, "travel_min", 0.0) or 0.0),
        "travel_max": float(getattr(p, "travel_max", 0.0) or 0.0),
        "clamped": 0,
    })
    return ch, rep


def report_text(rep: FitReport, plan=None) -> str:
    out = ["[直拟合] " + rep.describe()]
    out.append("    选档：{}（最小角度 {}°、一档至少 {} 层、直线优先/换档代价都是"
               "④ 里的值）".format(
                   "复用 solve 的 DP" if rep.tier_mode == "dp" else "旧策略（粘住上一档）",
                   rep.travel_min_setting, rep.speed_min_run))
    if rep.n_fill:
        out.append("    ★ 长间隔拆了 {} 个填充层（travel 上限 {:.0f}°），"
                   "总时长不变".format(rep.n_fill, _SPLIT_MAX))
    if rep.n_pause:
        out.append("    长休止 {} 处用「1 个直线格 + Pause 事件」代替拆层"
                   "（时长同样精确）".format(rep.n_pause))
    if rep.ladder_on:
        out.append("    ★ 15° 阶梯（激进拟合）：容差 {:.2f}ms · 修正 {} 音"
                   "（挪 中位 {:.2f} / max {:.2f}ms，合计 {:+.2f}ms）".format(
                       rep.ladder_tol_ms, rep.n_ladder_moved,
                       rep.ladder_move_median_ms, rep.ladder_move_max_ms,
                       rep.ladder_move_sum_ms))
        if rep.n_ladder_raw:
            out.append("    ⚠ 有 {} 个音离 15° 阶梯 > {:.2f}ms（容差不够）⇒ "
                       "**原样保留**，没有硬掰".format(rep.n_ladder_raw,
                                                      rep.ladder_tol_ms))
        if rep.n_ladder_bad:
            out.append("    ⚠ 最终还有 {} 层不在 15° 的整数倍上（多半是双押/中旋"
                       "折返格，或长休止的 Pause 格）".format(rep.n_ladder_bad))
        else:
            out.append("    非双押格的角度**全部**落在 15 30 45 60 75 90…… 上 ✔")
    if rep.err_max_ms > 0.05:
        if rep.ladder_on:
            out.append("    时序误差 max {:.3f}ms = 容差内的**主动修正**"
                       "（「拟合容差」就是干这个的；调小容差就挪得少）"
                       .format(rep.err_max_ms))
        else:
            out.append("    ★ 时序误差 max {:.3f}ms —— 直拟合本该是 0；"
                       "说明输入没去噪（先跑 `core.denoise`）".format(rep.err_max_ms))
    if rep.straight_frac < 0.5:
        out.append("    直线率 {:.1%}（低）：想去噪后重算，或换回 `solve()` 那套"
                   "（它靠模板/三连音抬直线率，但会改时序）".format(
                       rep.straight_frac))
    # ★ 直拟合**不用**哪些参数：说清楚，免得用户以为设了没生效
    out.append("    （直拟合不用：模板 / 三连音引擎 / 自然闭合 / 雪花 / 音值量化 ——"
               "那几样都会**重排时间**，与「时序优先」冲突；"
               "要它们请切回「最优化」）")
    if plan is not None:
        out.append("    " + str(plan))
    return "\n".join(out)
