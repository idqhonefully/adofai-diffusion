# -*- coding: utf-8 -*-
"""双押偏移累计与回正（项3 配套）。

用户口径
--------
> 偏移时值容忍它累计，总计超过 **20ms** 之后，找一个 SetSpeed 事件或暂停节拍事件
> 计算后**回正所有偏移**。SetSpeed 事件有 `angleOffset` 参数。

游戏时间模型（已核对两处：反编译源码 + 本项目 `vendor/adofai_timemodel`）
--------------------------------------------------------------------------
**① 每格的时长 = 沿角度轴积分**

    dt = ∫ (dAngle/180) · (60000 / seg_bpm)          [ms]

没有速度事件时整格一段，于是 `dt = (travel/180)·(60000/bpm)` —— 与
`core/solve.times_from_chart` 现有口径一致。

**② `SetSpeed` 的 `angleOffset` 是「沿本格再走多少度才生效」**

反编译 `scnGame.cs:1204-1247`：把该格的 travel `A` 按 `angleOffset` 切成两段，
各自用自己的 BPM，再把该格总时长换算成一个等效 speed。
`vendor/adofai_timemodel/angle.py:465` 更直接：

    abs_angle = cum_angle[floor] + angleOffset      # 事件生效的绝对角度
    # 然后按绝对角度切段：seg 内 bpm 恒定

所以「在本格第 φ 度处变速」= 把本格切成 `[0, φ]`（**旧速**）与 `[φ, A]`（新速）：

    dt(φ) = (φ/180)·(60000/b_prev) + ((A−φ)/180)·(60000/b_new)

**修正量：把已有 SetSpeed 的 angleOffset 从小往大挪 φ**

    Δ(φ) = dt(φ) − dt(0) = (φ/180)·60000·(1/b_prev − 1/b_new)          [ms]

★ 这才是「回正」要用的量：**只改这一格自己的时长，后面每一格都不动**
（它们本来就都在 `b_new` 上）。反解：

    φ = Δ · 180 / (60000 · (1/b_prev − 1/b_new))        ∈ [0, travel]

取值范围与符号由 `b_prev / b_new` 决定：

    提速（b_new > b_prev）⇒ Δ ≥ 0，只能**加长**本格（上限 (A/180)·60000·(1/b_prev−1/b_new)）
    减速（b_new < b_prev）⇒ Δ ≤ 0，只能**缩短**本格

（另一条路是在没有事件的格上**新插**一对 SetSpeed：本格 `[φ,A]` 用新速、下一格再
恢复旧速，于是只有本格时长变化。代价是多两个速度事件，所以默认不用。）

**③ `Pause` 的 `duration` 是「额外几拍」**

反编译 `scrFloor.cs:417`：

    entryTimeAfterExtraBeats = entryTime + crotchetAtStart·extraBeats/speed

`crotchetAtStart = 60/base_bpm`、`speed = bpm/base_bpm` ⇒ 额外时长 = `60000·d/bpm` ms。
所以对**任意实数** `d`：

    d = Δ · bpm / 60000                                        [拍]

Pause 不改速度，所以**不破坏「速度档只用 2 的幂」**，也不受符号限制；代价是
多一个事件，而且 `d` 是分数拍（游戏接受，但可读性差）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: 用户口径：累计偏移超过这个就回正
DEFAULT_TOL_MS = 20.0

#: 回正手段
MODE_SET_SPEED = "set_speed"
MODE_PAUSE = "pause"
MODE_AUTO = "auto"


def flat_duration_ms(travel: float, bpm: float) -> float:
    """整格同一速度时的时长（ms）。"""
    return (float(travel) / 180.0) * (60000.0 / float(bpm))


def split_duration_ms(travel: float, phi: float,
                      bpm_prev: float, bpm_new: float) -> float:
    """本格在第 `phi` 度处变速后的时长（ms）。"""
    A = float(travel)
    phi = max(0.0, min(float(phi), A))
    return (phi / 180.0) * (60000.0 / float(bpm_prev)) + \
        ((A - phi) / 180.0) * (60000.0 / float(bpm_new))


def delta_ms(travel: float, phi: float, bpm_prev: float, bpm_new: float) -> float:
    """把已有 SetSpeed 的 `angleOffset` 挪到 `phi` 带来的时长**修正量**（ms）。

    `phi=0` 即「事件在本格起点生效」（默认写法）；本格被推迟 φ 度才变速，
    于是前 φ 度仍按旧速走 ⇒ 相对默认写法，本格时长改变：

        Δ(φ) = (φ/180)·60000·(1/b_prev − 1/b_new)
    """
    return float(phi) / 180.0 * 60000.0 * \
        (1.0 / float(bpm_prev) - 1.0 / float(bpm_new))


def solve_angle_offset(travel: float, bpm_prev: float, bpm_new: float,
                       want_delta_ms: float) -> float:
    """反解 `angleOffset` φ：让本格时长恰好改变 `want_delta_ms`。

    结果可能落在 `[0, travel]` 之外 —— 由调用方判断可行性。
    """
    d = 1.0 / float(bpm_prev) - 1.0 / float(bpm_new)
    if abs(d) < 1e-15:
        raise ValueError("bpm_new == bpm_prev，该格没有 SetSpeed，无法用 angleOffset 修正")
    return float(want_delta_ms) * 180.0 / (60000.0 * d)


def defer_capacity_ms(travel: float, bpm_prev: float, bpm_new: float) -> float:
    """本格 angleOffset 能提供的**最大**修正量（ms，带符号）。

    提速 ⇒ 正值（最多加长这么多）；减速 ⇒ 负值（最多缩短这么多）。
    """
    return delta_ms(travel, travel, bpm_prev, bpm_new)


def pause_beats_for(want_delta_ms: float, bpm: float) -> float:
    """反解 Pause 的 `duration`（拍，可为负）让该格时长改变 `want_delta_ms`。"""
    return float(want_delta_ms) * float(bpm) / 60000.0


@dataclass
class Correction:
    """一次回正。"""

    floor: int
    kind: str                     # set_speed / pause
    delta_ms: float               # 该事件带来的时长修正（ms）
    offset_before_ms: float       # 回正前累计偏移
    offset_after_ms: float        # 回正后累计偏移（应为 ~0）
    angle_offset: float = 0.0     # kind == set_speed
    bpm: float = 0.0              # kind == set_speed：新 BPM
    pause_beats: float = 0.0      # kind == pause


@dataclass
class OffsetPlan:
    """一整张谱面的偏移回正方案。"""

    corrections: list[Correction] = field(default_factory=list)
    peak_ms: float = 0.0          # 回正前的最大累计偏移
    residual_ms: float = 0.0      # 末尾残余偏移
    dropped: int = 0              # 找不到可行回正点的次数

    def describe(self) -> str:
        return (f"回正 {len(self.corrections)} 处 · 峰值 {self.peak_ms:.1f}ms · "
                f"残余 {self.residual_ms:.1f}ms · 放弃 {self.dropped}")


def plan_corrections(floors, deltas_ms, *, base_bpm: float,
                     tol_ms: float = DEFAULT_TOL_MS,
                     mode: str = MODE_AUTO,
                     max_corrections: int = 100000,
                     report: dict | None = None) -> OffsetPlan:
    """给定每格引入的偏移 `deltas_ms`（ms，累加量），在超阈值后安排回正。

    参数
    ----
    - `floors`：`Floor` 序列（只读 `travel` / `bpm`）
    - `deltas_ms[i]`：第 i 格**新引入**的时值偏移（例如每次轨道双押 +1/12 拍）
    - `mode`：`set_speed` / `pause` / `auto`（auto 先试 set_speed，不可行退 pause）
    - `tol_ms`：累计偏移超过它才动手

    口径：累计量按格推进；一旦 `|acc| >= tol_ms`，就在**当前格**放一个回正事件
    把 `acc` 打回 0（然后继续累计）。
    """
    n = len(floors)
    out = OffsetPlan()
    if n == 0 or not deltas_ms:
        return out

    acc = 0.0
    prev_bpm = float(base_bpm)
    for i in range(n):
        f = floors[i]
        acc += float(deltas_ms[i] if i < len(deltas_ms) else 0.0)
        if abs(acc) >= float(tol_ms) and len(out.corrections) < max_corrections:
            corr = _correct(floors, i, prev_bpm, acc, mode=mode)
            if corr is not None:
                out.corrections.append(corr)
                acc = corr.offset_after_ms
                out.peak_ms = max(out.peak_ms, abs(corr.offset_before_ms))
            else:
                out.dropped += 1
        prev_bpm = float(f.bpm)
    out.residual_ms = acc
    if report is not None:
        report.update(corrections=len(out.corrections), peak_ms=out.peak_ms,
                      residual_ms=out.residual_ms, dropped=out.dropped)
    return out


def _correct(floors, i: int, prev_bpm: float, acc: float, *,
             mode: str) -> Correction | None:
    """在第 i 格安排一次把 `acc` 打回 0 的回正。"""
    f = floors[i]
    A = float(f.travel)
    b_prev = float(prev_bpm) or 1.0
    b_now = float(f.bpm)
    want = -float(acc)                     # 本格时长需要改变的量（ms）

    if mode in (MODE_SET_SPEED, MODE_AUTO):
        # 只有「本格本来就有 SetSpeed」才能用 angleOffset 修（零额外事件）
        if abs(b_now - b_prev) > 1e-12:
            phi = solve_angle_offset(A, b_prev, b_now, want)
            if -1e-9 <= phi <= A + 1e-9:
                phi = max(0.0, min(phi, A))
                got = delta_ms(A, phi, b_prev, b_now)
                if abs(got - want) < 1e-6:      # 数值上确实修上了
                    return Correction(floor=i, kind=MODE_SET_SPEED,
                                      angle_offset=phi, bpm=b_now,
                                      delta_ms=got, offset_before_ms=acc,
                                      offset_after_ms=acc + got)
        if mode == MODE_SET_SPEED:
            return None

    # Pause：不改速度，任意实数拍数都能修（含负拍）
    if mode in (MODE_PAUSE, MODE_AUTO):
        d = pause_beats_for(want, b_now)
        got = d * 60000.0 / b_now
        return Correction(floor=i, kind=MODE_PAUSE, pause_beats=d,
                          delta_ms=got, offset_before_ms=acc,
                          offset_after_ms=acc + got)
    return None

    # Pause：不改速度，任意实数拍数都能修
    if mode in (MODE_PAUSE, MODE_AUTO):
        d = pause_beats_for(want, b_now)
        got = d * 60000.0 / b_now
        return Correction(floor=i, kind=MODE_PAUSE, pause_beats=d,
                          delta_ms=got, offset_before_ms=acc,
                          offset_after_ms=acc + got)
    return None
