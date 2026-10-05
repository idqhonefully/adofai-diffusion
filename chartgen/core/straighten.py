# -*- coding: utf-8 -*-
"""角度回正（项1）：在**不动时序**的前提下，让轨道尽量少长时间停在斜向。

问题
----
生成器给出的谱面里，长串 `travel=180°`（直线）会**保持当前朝向**一直走。
如果此时朝向恰好是 45°/135°（斜向），就会拉出一条**很长的斜轨** —— 这正是
Automaton_Waltz 实测到的「最长 234 格斜轨」。

机制：只改 Twirl 符号，零时序代价
--------------------------------
对任一格：

    ccw = False : turn = norm180(travel + 180)
    ccw = True  : turn = −norm180(travel + 180)      ← 只是符号相反

`travel`（= 时长分母）完全不变，所以 **Twirl 只改朝向、不改时间**。
而「当前是否 ccw」是一个**奇偶状态**，在第 i 格放一个 Twirl 就把它翻转。
于是：

- 每格 turn 的**符号**可以自由选（想翻就放 Twirl）
- 放 Twirl 的**代价** = 谱面上多一个旋转图标（社区口径：能省则省）

这正好落成一个 DP：

    状态 = (量化后的 heading, 当前奇偶)
    转移 = 本格翻 / 不翻
    代价 = deviation(heading)   +   λ × [本格翻]
    目标 = 最小化「偏轴停留 + Twirl 图标数」

受保护的地方（模板段 / 三连音引擎段 / 自然闭合段 / 雪花段）**不允许改**，
SetSpeed 格**不允许放 Twirl**（社区硬规则），这两类都由调用方传进来钉死。

DP 只负责**规划** flip 序列；真正的 heading / 坐标永远由 `core/path.py` 精确重算。
"""
from __future__ import annotations

from dataclasses import dataclass

from .path import (
    AXES,
    AXIS_EPS,
    DEFAULT_HEADING0,
    Path,
    norm180,
)

#: heading 量化网格（度）。DP 状态用它分桶，避免浮点导致状态爆炸。
DEFAULT_GRID = 0.5


def _deviation(h: float) -> float:
    """朝向 h 离最近正交主轴的角度差（与 `Path.deviation` 同口径，抗浮点漂移）。"""
    d = min(abs(norm180(h - a)) for a in AXES)
    d = min(d, 90.0 - d) if d > 45.0 else d
    return 0.0 if d <= AXIS_EPS else d


def _base_turn(travel: float) -> float:
    """ccw=False 时的转弯角（ccw=True 就是它的相反数）。"""
    return norm180(travel + 180.0)


@dataclass
class Plan:
    """回正规划的结果。"""

    flips: list[bool]
    changed: int = 0              # 相对输入翻了多少位
    cost_before: float = 0.0
    cost_after: float = 0.0
    offaxis_before: float = 0.0   # Σ deviation（度·格）
    offaxis_after: float = 0.0
    twirls_before: int = 0
    twirls_after: int = 0
    skipped: str = ""             # 跳过原因（没跑 DP 时）

    @property
    def gain(self) -> float:
        return self.cost_before - self.cost_after


def _cost_of(floors, flips, radius, allow_twirl) -> tuple[float, float, int]:
    """给定 flip 序列的 (Σdeviation, 精确偏轴量, Twirl 数)。"""
    pth = Path.from_floors(floors, flips, radius=radius, allow_twirl=allow_twirl)
    off = sum(pth.deviation(i) for i in range(pth.n))
    n_tw = sum(1 for v in flips if v)
    return (off, off, n_tw)


def plan_straighten(
    floors,
    flips: list[bool],
    *,
    radius: float = 1.0,
    allow_twirl: bool = True,
    pinned: set[int] | None = None,
    forbidden: set[int] | None = None,
    theta: float = 20.0,
    min_run: int = 8,
    flip_penalty: float = 180.0,
    grid: float = DEFAULT_GRID,
    min_gain: float = 1.0,
) -> Plan:
    """规划回正后的 flip 序列（不改 travel，时序零影响）。

    参数
    ----
    - `pinned`：这些格的 flip **钉死**（模板段 / 引擎段 / 自然段 / 雪花段）。
    - `forbidden`：这些格**不许**放 Twirl（SetSpeed 格）。
    - `theta` / `min_run`：只有存在「连续 ≥ min_run 格 deviation > theta」的斜轨时才动手
      （干净谱面直接原样返回，避免无谓改动）。
    - `flip_penalty`（λ）：一个 Twirl 图标抵多少「度·格」的偏轴。越大越省 Twirl。
    - `grid`：heading 量化网格。`min_gain`：收益小于它就不采纳。
    """
    n = len(floors)
    fl = [bool(x) for x in flips]
    if n == 0 or not allow_twirl:
        return Plan(fl, skipped="disabled")

    pinned = set(pinned or ())
    forbidden = set(forbidden or ())

    # —— 先看有没有值得回正的长斜轨；没有就原样返回 ——
    pth0 = Path.from_floors(floors, fl, radius=radius, allow_twirl=allow_twirl)
    runs = pth0.diagonal_runs(theta=theta, min_run=min_run, start=1)
    if not runs:
        c, off, ntw = _cost_of(floors, fl, radius, allow_twirl)
        return Plan(fl, cost_before=c, cost_after=c, offaxis_before=off,
                    offaxis_after=off, twirls_before=ntw, twirls_after=ntw,
                    skipped="no-diagonal-run")

    base_turn = [_base_turn(f.travel) for f in floors]
    cost_before, off_before, tw_before = _cost_of(floors, fl, radius, allow_twirl)
    cost_before += flip_penalty * 0.0        # 口径：λ 只加在 DP 内，基线只比 off+λ·tw

    # —— DP ——
    # 状态: (hq, parity) -> (cost, h, prev_key, flip)
    # parity: False = ccw 关（turn = base），True = ccw 开（turn = −base）
    def hq(h: float) -> tuple[float, int]:
        return (round(h / grid), 0)

    h0 = DEFAULT_HEADING0
    start = (hq(h0), False)
    cur: dict[tuple, tuple[float, float, tuple | None, bool]] = {
        start: (0.0, h0, None, False)
    }
    # 逐格保留回溯
    hist: list[dict] = []

    for i in range(n):
        nxt: dict[tuple, tuple[float, float, tuple | None, bool]] = {}
        for key, (cst, h, _prev, _fl) in cur.items():
            par = key[1]
            # 可选 flip
            if i in pinned:
                opts = (fl[i],)
            elif i in forbidden:
                opts = (False,)
            else:
                opts = (False, True)
            for f in opts:
                par2 = (par ^ f) if allow_twirl else par
                turn = base_turn[i] if not par2 else -base_turn[i]
                h2 = norm180(h + turn)
                c2 = cst + _deviation(h2) + (flip_penalty if f else 0.0)
                k2 = (hq(h2), par2)
                old = nxt.get(k2)
                if old is None or c2 < old[0] - 1e-9:
                    nxt[k2] = (c2, h2, key, f)
        hist.append(nxt)
        cur = nxt
        if not cur:                       # 理论上不会发生
            return Plan(fl, skipped="dp-empty")

    # —— 回溯最优路径 ——
    best_key = min(cur.items(), key=lambda kv: kv[1][0])[0]
    out = list(fl)
    key = best_key
    for i in range(n - 1, -1, -1):
        cst, h, prev_key, f = hist[i][key]
        out[i] = bool(f)
        key = prev_key

    pth2 = Path.from_floors(floors, out, radius=radius, allow_twirl=allow_twirl)
    off_after = sum(pth2.deviation(i) for i in range(pth2.n))
    tw_after = sum(1 for v in out if v)
    cost_after = off_after + flip_penalty * tw_after
    cost_before_full = off_before + flip_penalty * tw_before

    if cost_after >= cost_before_full - min_gain:
        return Plan(fl, cost_before=cost_before_full, cost_after=cost_before_full,
                    offaxis_before=off_before, offaxis_after=off_before,
                    twirls_before=tw_before, twirls_after=tw_before,
                    skipped="no-gain")

    # 不许因为回正让 Twirl 暴涨（超过这个倍数就放弃）
    if tw_before > 0 and tw_after > tw_before * 2 + 2:
        return Plan(fl, cost_before=cost_before_full, cost_after=cost_before_full,
                    offaxis_before=off_before, offaxis_after=off_before,
                    twirls_before=tw_before, twirls_after=tw_before,
                    skipped="too-many-twirls")

    changed = sum(1 for a, b in zip(fl, out) if a != b)
    return Plan(out, changed=changed, cost_before=cost_before_full,
                cost_after=cost_after, offaxis_before=off_before,
                offaxis_after=off_after, twirls_before=tw_before,
                twirls_after=tw_after)
