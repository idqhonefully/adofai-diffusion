# -*- coding: utf-8 -*-
"""去噪 / 吸附：把**带抖动的** onset 时间戳吸到一个和弦网格上（`docs/44`）。

## 为什么需要它

外部来源（强化学习生成的踩点、手扒、第三方工具导出）的时间戳有**随机抖动**。
抖动本身不致命，致命的是它让「一砖一个音」的直拟合失去意义：
每个 Δt 都是个奇怪的实数 ⇒ `travel = 180·Δt/C/k` 落不到任何整齐的角度上。

**去噪 = 先把点吸到一个格子上**，之后：

  · Δt 全是 `m·step`（m 整数）⇒ `travel = 180·(m/div)/k` 是**整齐的有理数**；
  · 直拟合**逐点精确**（不是"接近"）；
  · 写回 BDG 时也不会再被它的吸附挪（点本来就在格上）。

## 网格的定义（与 BDG **逐字一致**）

宿主 `store.ts:315` / `tempo.ts:109`：

```ts
const round = (b) => Math.round(b * 1e6) / 1e6;          // 关吸附：无损
function snapBeat(beat, div) {                            // 开吸附：
  if (div <= 1) return Math.round(beat);                  //   div=1 是**整拍**
  const step = 1 / div;
  return Math.round(beat / step) * step;                  //   否则吸到 1/div 拍
}
```

而 `time = offsetMs + beat · (60000/baseBpm)` ⇒ **格子在毫秒轴上是**

    t = offset + k · (period / div)          k ∈ ℤ
    period = 60000 / baseBpm                 ← 一拍多少毫秒（"砖长"）

★ 两处必须记住：

  1. `Math.round` 是**半数向 +∞**（JS 语义），Python 的 `round` 是银行家舍入
     —— 所以这里借 `core.bdg.snap.js_round`，保证与宿主**逐点同结果**。
  2. **相位就是我们的 `offset`**：BDG 的格子锚在 `offsetMs` 上，我们在
     `.adofai` 里的 `settings.offset` 也是它。⇒ 相位不能单独调（`docs/44` §风险）。

## 抖动能不能被吸收

只要 `抖动 < step/2` 就一定能吸到对格上。抖动是**相对量**（固定 ms），
而这个判据与音符长度无关 ⇒ 密集段（小 step）才是风险点。
`safe_div()` 就是干这个的：**取能让「最小间隔 > step」的最细分母**，
既细到能吸收抖动，又不细到两个音撞进同一格（撞了宿主 `addMarker` 返回
`null` = **静默丢音**，`store.ts:669`）。

## 默认口径（用户 2026-10：「直接按照合适的来」）

  · **默认全吸**（`radius=None`）—— 与 BDG 的吸附行为一致，去噪就是要"全吸"；
  · **分母自动推导** `safe_div()`，上限 `MAX_DIV`，并在报告里说明依据；
  · 给 `radius` 才变成"只吸离格 <r 的、其余保留并报出"（保真档，手动用）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .bdg.snap import js_round
from . import gridfit as gf

__all__ = ["DIVS", "MAX_DIV", "DEFAULT_DIV", "GridPlan", "intervals", "min_gap",
           "tile_period", "safe_div", "plan", "denoise", "report_text",
           "MODE_ALL", "MODE_RADIUS"]

#: 候选分母。宿主顶栏的档位就是这几个（`metrics.ts:10` `SNAP_DIVISIONS`），
#: 用它们能保证「我们算的格」= 「用户在 BDG 里能选到的格」。
DIVS = (1, 2, 4, 8, 16, 32)
#: 自动分母的上限：再细就纯属把抖动也当信号了，而且 BDG 顶栏也选不到。
MAX_DIV = 32
#: 宿主默认档（`metrics.ts` `DEFAULT_DIV`）。
DEFAULT_DIV = 4

#: 抖动 < step/2 才吸得对（理论上界）；**格只要不粗于最小间隔**就不会撞格
#: （撞格的充要条件是 `最小间隔 < 格`）。
GAP_MARGIN = 1.0
#: 解释度门槛：候选周期能解释多少比例的间隔（按整数倍判定）
EXPLAIN_MIN = 0.85
EXPLAIN_TOL = 0.25          # 间隔相对候选格步长的容差

MODE_ALL = "all"            # 全吸（与 BDG 一致）
MODE_RADIUS = "radius"      # 带半径：只吸离格近的


@dataclass
class GridPlan:
    """一次网格规划的结果（周期 + 相位 + 分母 + 置信度）。"""
    ok: bool
    reason: str = ""
    period_ms: float = 0.0      # 一拍 = 60000/base_bpm
    phase_ms: float = 0.0       # 格子相位（= offset）
    step_ms: float = 0.0        # 格步长 = period/div
    div: int = DEFAULT_DIV
    conf: float = 0.0           # **格级**集中度 R（在 `step` 上量的：格子立不立得住）
    conf_beat: float = 0.0      # 拍级集中度 R（音在不在整拍上；只当情报）
    rms_ms: float = 0.0
    vmax_ms: float = 0.0
    n: int = 0
    n_hit: float = 0.0          # 落在 ±step/2 内的点比例
    gap_ms: float = 0.0         # 最小间隔（自动分母的依据）
    from_hint: bool = False
    notes: list = field(default_factory=list)

    @property
    def bpm(self) -> float:
        return 60_000.0 / self.period_ms if self.period_ms > 0 else 0.0

    def to_dict(self) -> dict:
        return {"ok": self.ok, "reason": self.reason,
                "period_ms": round(self.period_ms, 6),
                "bpm": round(self.bpm, 6),
                "phase_ms": round(self.phase_ms, 6),
                "step_ms": round(self.step_ms, 6),
                "div": int(self.div), "conf": round(self.conf, 4),
                "conf_beat": round(self.conf_beat, 4),
                "rms_ms": round(self.rms_ms, 4), "vmax_ms": round(self.vmax_ms, 4),
                "n": self.n, "n_hit": round(self.n_hit, 4),
                "gap_ms": round(self.gap_ms, 4),
                "from_hint": bool(self.from_hint), "notes": list(self.notes)}

    def describe(self) -> str:
        if not self.ok:
            return "网格未通过({})".format(self.reason or "?")
        return ("砖长 {:.4f}ms(=bpm {:.3f}) 相位 {:+.3f}ms 分母 1/{} 格 {:.4f}ms "
                "R={:.3f} rms={:.2f}ms".format(self.period_ms, self.bpm,
                                               self.phase_ms, self.div, self.step_ms,
                                               self.conf, self.rms_ms))


# ---------------------------------------------------------------- 基础统计
def intervals(ts, min_ms: float = 0.0):
    return [b - a for a, b in zip(ts, ts[1:]) if (b - a) > min_ms]


def min_gap(ts) -> float:
    """最小间隔（撞格风险就是它说了算）。少于 2 个点 ⇒ 0。"""
    iv = intervals(ts)
    return float(min(iv)) if iv else 0.0


def _step_loss(t, period: float, div: int):
    """候选周期 → `(平均平方残差[格²], 相位[ms])`。

    ★ 为什么不用 `core.gridfit.refine_period`（那是**拍级**集中度 R）：
      抖动存在时 R 的极大点会被"把拍对齐"这种宽平山顶骗走 —— 实测
      `period 148.99 / 相位 92.3 / R 0.78`（真值 150/76.9），于是**每个点都吸错格**。
      这里直接量「点离最近格多远」（就是我们接下来要挪的量），抖动的贡献是
      `Var(jitter)` 这个常数底座，真实周期才让它降到最小 ⇒ 判据与目标一致。
    """
    step = period / float(div)
    if step <= 0:
        return float("inf"), 0.0
    ang = 2.0 * np.pi * (t / step)
    c, s = float(np.cos(ang).mean()), float(np.sin(ang).mean())
    phi = float((np.arctan2(s, c) / (2.0 * np.pi)) % 1.0) * step
    r = (t - phi) / step
    r = r - np.rint(r)
    return float((r ** 2).mean()), phi


def refine_step(t, p_hint: float, div: int, *, span: float = 0.02, n: int = 61,
                rounds: int = 3):
    """在 `p_hint(1±span)` 上最小化「离最近格的距离」，三段收敛到 ~1e-6。

    返回 `(period, phase, loss)`；数据太少时原样返回 hint。
    """
    t = np.asarray(t, dtype=float)
    if t.size < 3 or p_hint <= 0:
        _l, phi = _step_loss(t, max(1e-9, p_hint), max(1, div))
        return float(p_hint), phi, _l
    p = float(p_hint)
    lo, hi = float(span), float(span)
    best_phi = 0.0
    for _r in range(max(1, rounds)):
        ps = p * (1.0 + np.linspace(-lo, hi, n))
        bl, bp, bphi = None, p, 0.0
        for q in ps:
            l, phi = _step_loss(t, float(q), div)
            if bl is None or l < bl:
                bl, bp, bphi = l, float(q), phi
        p, best_phi = bp, bphi
        lo = hi = span * (0.05 ** (_r + 1))       # 2% → 0.1% → 0.005%
    return p, best_phi, bl


def nice_candidates(period: float):
    """「整齐值」候选：BPM 是整数 / 半整数，或周期是 0.1ms / 0.01ms 的整数倍。

    ★ 为什么需要它：短样本 + 抖动时**周期本身是不可辨识的** —— 0.06% 的周期误差
      在 2 秒里只造成 1.2ms 漂移，而抖动有 ±5ms，任何方法都分不出来。
      但真值几乎总是"人写的整齐值"（实测那份 RL 数据就是正好 400 BPM）。
      所以只在**不吃亏**的时候才吸（见 `snap_nice_period`）。
    """
    out = []
    bpm = 60_000.0 / period if period > 0 else 0.0
    for q in (1.0, 0.5, 0.25):
        b = round(bpm / q) * q
        if b > 0:
            out.append(60_000.0 / b)
    for s in (1.0, 0.1, 0.01):
        out.append(round(period / s) * s)
    # ★ 按「人味」从粗到细排序（先整数 BPM，最后 0.01ms），去重保留首次出现
    seen, ordered = set(), []
    for p in out:
        k = round(p, 9)
        if p > 0 and k not in seen:
            seen.add(k)
            ordered.append(p)
    return ordered


def snap_nice_period(t, period: float, div: int, *, tol: float = 0.0015,
                     slack: float = 1.25):
    """把周期吸到「整齐值」——**但只在不会让格内残差变差**的时候。

    判据（这是关键，不然会把长样本上已精修好的周期搞坏）：
    整齐值的格内残差 `≤ 精修值的残差 × slack` 才接受。
      · 长样本：精修已经到 1e-6，整齐值差一点 ⇒ 残差明显变大 ⇒ **拒绝**；
      · 短样本：两者都在抖动底座上 ⇒ 接受整齐值（那才是真值）。

    返回 `(period, phase, 说明)`。
    """
    if period <= 0 or div < 1:
        return period, 0.0, ""
    arr = np.asarray(t, dtype=float)
    l_ref, phi_ref = _step_loss(arr, period, div)
    best = None
    for cand in nice_candidates(period):
        if abs(cand / period - 1.0) > tol:
            continue
        l, phi = _step_loss(arr, cand, div)
        if l <= l_ref * slack + 1e-12:
            best = (cand, phi, l)          # ★ 取**第一个**合格的 = 最"人味"的
            break
    if best is None:
        return period, phi_ref, ""
    cand, phi, l = best
    return cand, phi, ("周期取整齐值 {:.6f}ms（{:.4f} bpm；格内残差 "
                       "{:.4f}→{:.4f} 格，没变差才吸）".format(
                           cand, 60_000.0 / cand, float(np.sqrt(l_ref)),
                           float(np.sqrt(l))))


def _explain(iv, period: float, tol: float = EXPLAIN_TOL) -> float:
    """候选周期 `period` 能解释多少比例的间隔。

    ★ 判据用 **`period/2` 的整数倍**，不是 `period` 的整数倍：
      半砖/四分之一砖的音（16 分、8 分）是**合法细分**，不该把「砖长」否掉。
      踩过一次（`tests/test_ts_source.py::C_grid`）：8 个间隔里有 1 个半砖时，
      按 `period` 整数倍只解释 71% < 门槛 ⇒ 砖长被拽到一半（bpm 400 → 800）。
    """
    if not iv or period <= 0:
        return 0.0
    q = period / 2.0
    ok = 0
    for d in iv:
        m = max(1, int(js_round(d / q)))
        if abs(d - m * q) <= tol * period:
            ok += 1
    return ok / float(len(iv))


def tile_period(ts, hint: float | None = None, *, bin_oct: float = 0.05,
                min_n: int = 8):
    """估「砖长」（一拍多少毫秒 = 60000/base_bpm）。

    ★ 为什么不是「最小间隔」：半砖/四分之一砖的音（16 分音符）会把最小间隔
      压到 1/2、1/4 砖 —— 那不是砖长，是细分。所以这里取**间隔的对数直方图
      众数**（砖长必然是出现最多的那一档），再往下试 1/2、1/3 倍频
      （全是 16 分的曲子，众数会落在 2× 砖上）。

    返回 `(period_ms, 依据说明)`。`hint` 给了就原样返回（用户说了算）。
    """
    if hint and hint > 0:
        return float(hint), "hint"
    iv = [d for d in intervals(ts) if d > 0.5]
    if len(iv) < min_n:
        med = float(np.median(iv)) if iv else 0.0
        return med, "样本太少(n={})，取中位间隔".format(len(iv))
    L = np.log2(np.asarray(iv, dtype=float))
    lo, hi = float(L.min()), float(L.max())
    nb = max(1, int(round((hi - lo) / bin_oct)) + 1)
    h, edges = np.histogram(L, bins=nb, range=(lo, hi + 1e-9))
    k = int(np.argmax(h))
    p = float(2.0 ** ((edges[k] + edges[k + 1]) / 2.0))
    # ★ 众数 = 出现最多的间隔 = **砖长**。先认它（解释度够就到此为止）。
    #
    #   反面教材（实测）：原来写成「只要 p/2 解释度更高 5% 就往下走」，
    #   于是 15 个间隔里有 1 个半砖（70ms）就把砖长从 151 拽到 75.6 ⇒
    #   bpm 从 400 变 801。**倍频是另一个维度**，不该被一个离群间隔决定。
    if _explain(iv, p) >= EXPLAIN_MIN:
        return p, "间隔对数直方图众数（n={}）".format(len(iv))
    # 众数解释不了（比如整曲全是三连音/16 分，众数落在细分上）⇒ 才往下试细的
    for m in (2.0, 3.0):
        cand = p / m
        if _explain(iv, cand) >= EXPLAIN_MIN:
            return cand, ("众数 {:.3f}ms 解释不了整体 ⇒ 取它的 1/{:.0f}"
                          .format(p, m))
    return p, "间隔对数直方图众数（n={}，解释度偏低）".format(len(iv))


def safe_div(period_ms: float, gap_ms: float, *, divs=DIVS, max_div: int = MAX_DIV,
             margin: float = GAP_MARGIN):
    """自动挑分母：**最细但仍不撞格**的那一档。

    判据：`period/div ≤ gap`（撞格的充要条件是 `gap < step`）。
      · 太粗 ⇒ 抖动吸收不掉（会吸到错格，比不吸更糟）；
      · 太细 ⇒ 两个音撞进同一格 ⇒ 宿主 `addMarker` 返回 null ⇒ **静默丢音**。

    ★ 为什么"最细但安全"而不是"最粗"：见 `tests/test_denoise.py::G_real` ——
      真数据上分母 1/1 会丢 9 个音、1/16 起直线结构崩掉，1/4 正好。
      但**过细也有代价**（抖动会被切成 ±1 格）⇒ 到上限时明确报出来。

    返回 `(div, 说明)`。
    """
    cands = [d for d in divs if d <= max_div and d >= 1]
    if not cands:
        cands = [1]
    limit = max(1e-9, float(gap_ms)) * margin * (1.0 + 1e-6)   # +ε：周期是浮点数
    for d in cands:
        step = period_ms / d
        if gap_ms <= 0 or step <= limit:
            why = ("最小间隔 {:.3f}ms > 格 {:.3f}ms ⇒ 不撞格"
                   if gap_ms > 0 else "没有两个点可比（按最细分母）")
            return int(d), why.format(gap_ms, step) if gap_ms > 0 else why
    d = cands[-1]
    return int(d), ("★ 分母已到上限 1/{}（格 {:.3f}ms）仍 ≥ 最小间隔 {:.3f}ms "
                    "⇒ 可能有音撞进同一格被丢（`addMarker` 静默返回 null）"
                    .format(d, period_ms / d, gap_ms))


# ---------------------------------------------------------------- 规划
def plan(ts, *, hint: float | None = None, offset_ms: float | None = None,
         div: int | None = None, divs=DIVS, max_div: int = MAX_DIV,
         min_conf: float = 0.30, min_n: int = 6, refine: bool = True) -> GridPlan:
    """给一串时间戳规划网格（不动作，只算）。

    `hint` = 已知砖长（比如来自我们的谱面 BPM），`offset_ms` = 已知相位
    （我们的 `offset`；给了就**不拟合相位**，只报它差多少）。
    """
    t = np.asarray(sorted(float(x) for x in ts), dtype=float)
    n = int(t.size)
    g = GridPlan(ok=False, n=n)
    if n < 2:
        g.reason = "点太少(n={})".format(n)
        return g
    g.gap_ms = min_gap(t)
    p, why = tile_period(t, hint)
    if p <= 0:
        g.reason = "估不出砖长"
        return g
    g.from_hint = bool(hint and hint > 0)
    g.notes.append("砖长依据：{}".format(why))

    if refine and not g.from_hint and n >= min_n:
        # ★ 先在默认档上精修周期（判据 = 离最近格的距离，见 `_step_loss`），
        #   再由安全分母重算一次（目标档位变了，最优点会略微移动）。
        period, fit_phase, loss = refine_step(t, p, int(div or DEFAULT_DIV))
        d0, _why0 = ((int(div), "") if div else safe_div(period, g.gap_ms,
                                                         divs=divs, max_div=max_div))
        if d0 != int(div or DEFAULT_DIV):
            period, fit_phase, loss = refine_step(t, period, d0)
        ok_fit, fit_why = True, ""
        g.notes.append("周期精修：格内残差 rms {:.4f} 格（≈{:.3f}ms）".format(
            float(np.sqrt(loss)), float(np.sqrt(loss)) * period / d0))
        period, fit_phase, why_nice = snap_nice_period(t, period, d0)
        g.notes.append("周期取整齐值：{}".format(why_nice) if why_nice
                       else "周期离整齐值较远（或吸了会让残差变差）⇒ 保留精修值")
    else:
        period = float(p)
        _R, fit_phase, _d = gf.concentration(t, p)
        ok_fit, fit_why = True, ""

    g.period_ms = float(period)
    g.phase_ms = float(offset_ms) if offset_ms is not None else float(fit_phase)
    g.div, why_div = (int(div), "用户指定") if div else safe_div(
        g.period_ms, g.gap_ms, divs=divs, max_div=max_div)
    g.step_ms = g.period_ms / g.div
    g.notes.append("分母依据：1/{} —— {}".format(g.div, why_div))

    # ★★ 置信度必须量在**格**上，不是量在拍上：
    #   拍级集中度 `R_beat` 只反映「音是不是都落在整拍上」—— 一首 8 分/16 分
    #   为主的曲子 `R_beat` 会接近 0，于是网格被误判成「跟音乐没关系」。
    #   踩过一次（`tests/test_ts_source.py::C_grid`：真值 150ms 的格被判 R=0.05）。
    #   所以：**格级 R** 当门槛（配 Rayleigh z），**拍级 R** 只当情报报出去。
    conf, _phi_s, _ds = gf.concentration(t, max(1e-9, g.step_ms))
    conf_beat, _phi_b, dd = gf.concentration(t, max(1e-9, g.period_ms))
    g.conf, g.conf_beat = float(conf), float(conf_beat)
    g.rms_ms = float(np.sqrt((dd ** 2).mean()))
    g.vmax_ms = float(np.abs(dd).max())
    z = float(conf) * float(np.sqrt(max(1, n)))
    g.notes.append("置信度：格级 R={:.3f}（Rayleigh z={:.2f}）· 拍级 R={:.3f}"
                   .format(conf, z, conf_beat))
    if conf_beat < 0.30:
        g.notes.append("★ 拍级集中度低（音不都在整拍上）⇒ 网格是照 1/{} 格定的，"
                       "这本身不是错误".format(g.div))
    if offset_ms is not None:
        # 相位由我们定（= 我们的 offset）⇒ 报出它相对拟合相位的偏差
        # ★ 相位只在**模一格**的意义下有区别：差一格 = 同一个网格，别拿它吓人。
        dphi = (fit_phase - float(offset_ms)) / g.step_ms
        dphi -= round(dphi)
        if abs(dphi) > 0.02:
            g.notes.append("相位：我们的 offset {:.3f}ms 与拟合相位 {:.3f}ms 差 "
                           "{:.3f} 格 ≈ {:.3f}ms（要精确吸格就得让 offset 对上它）"
                           .format(float(offset_ms), fit_phase, dphi,
                                   dphi * g.step_ms))

    # 「吸得干不干净」：相对**我们的相位**的残差（不是拟合相位 —— 两者可能差半格）
    res = np.mod(t - g.phase_ms + 0.5 * g.step_ms, g.step_ms) - 0.5 * g.step_ms
    g.n_hit = float(np.mean(np.abs(res) <= 0.25 * g.step_ms))
    g.notes.append("格级残差 rms={:.3f}ms max={:.3f}ms（≤ 半格 {:.3f}ms 才吸得对，"
                   "否则会吸到邻格）；落在 1/4 格内 {:.0%}".format(
                       float(np.sqrt((res ** 2).mean())), float(np.abs(res).max()),
                       0.5 * g.step_ms, g.n_hit))

    if n < min_n:
        g.ok, g.reason = False, "点太少(n={})".format(n)
    elif not ok_fit and not g.from_hint:
        g.ok, g.reason = False, fit_why
    elif (not g.from_hint and conf < min_conf and z < 2.5):
        g.ok, g.reason = False, (
            "网格不成立：格级 R={:.3f} < {:.2f} 且 Rayleigh z={:.2f} < 2.5"
            "（这些点凑不出格子）".format(conf, min_conf, z))
    else:
        g.ok = True
    return g


# ---------------------------------------------------------------- 吸附
def denoise(ts, g: GridPlan | None = None, *, div: int | None = None,
            radius: float | None = None, offset_ms: float | None = None,
            hint: float | None = None, keep: str = "first",
            divs=DIVS, max_div: int = MAX_DIV, **kw) -> dict:
    """把 `ts` 吸到网格上（`docs/44` 的主函数）。

    `radius=None`（默认）⇒ **全吸**，与 BDG 的吸附行为一致；
    给了 `radius` ⇒ 只吸 `|Δt| ≤ radius` 的点，其余**原样保留并报出来**
    （这是 BDG 没有的能力：它的吸附是全有全无）。

    `keep="first"` ⇒ 撞格时保留**先到的那个**（与我们在 BDG 里逐个
    `addMarker` 的先后一致），撞掉的记进 `dropped` —— ★ 不许静默。

    返回的 `beats` 是**宿主的拍位**（`k/div`，基点 0 = 相位）：带着它去写
    `.adofai` / 交给 BDG，两边都是整数格，不会再被挪。
    """
    t = [float(x) for x in ts]
    order = sorted(range(len(t)), key=lambda i: t[i])
    g = g or plan(t, hint=hint, offset_ms=offset_ms, div=div, divs=divs,
                  max_div=max_div, **kw)
    if not g.ok:
        return {"ok": False, "error": g.reason or "网格不成立", "grid": g.to_dict(),
                "ts": t, "beats": [], "moves_ms": [], "dropped": [],
                "n": len(t), "n_moved": 0, "n_kept": len(t), "n_dropped": 0,
                "mode": MODE_ALL, "report": {}, "text": report_text({}, g)}

    step, phase, div = g.step_ms, g.phase_ms, g.div
    out: list[float] = []
    beats: list[float] = []
    moves: list[float] = []
    ks: list[int] = []
    kept_idx: list[int] = []
    dropped: list[dict] = []
    n_moved = n_kept_orig = n_out = 0
    for i in order:
        ti = t[i]
        k = int(js_round((ti - phase) / step))
        sn = phase + k * step
        mv = sn - ti
        if radius is not None and abs(mv) > float(radius):
            n_out += 1
            sn, mv, k = ti, 0.0, None
        if k is None:
            out.append(sn)
            beats.append((sn - phase) / g.period_ms)
            moves.append(mv)
            ks.append(-1)
            kept_idx.append(i)
            continue
        if out and abs(sn - out[-1]) < 1e-9:
            # ★ 撞格：宿主 `addMarker` 在同拍会返回 null（静默丢音）⇒ 我们自己丢，
            #   并**记下来**，绝不让它悄悄消失。
            dropped.append({"i": i, "t_ms": ti, "at_ms": sn,
                            "kept_i": kept_idx[-1], "move_ms": mv})
            continue
        out.append(sn)
        beats.append(k / float(div))
        moves.append(mv)
        ks.append(k)
        kept_idx.append(i)
        if abs(mv) > 1e-9:
            n_moved += 1
        else:
            n_kept_orig += 1

    rep = _stats(moves, step, div, dropped)
    res = {"ok": True, "grid": g.to_dict(), "ts": out, "beats": beats,
           "moves_ms": [round(m, 6) for m in moves],
           "ks": ks, "src": kept_idx, "dropped": dropped,
           "n": len(t), "n_out": len(out),
           "n_moved": n_moved, "n_kept": n_kept_orig, "n_dropped": len(dropped),
           "n_out_of_radius": n_out,
           "mode": MODE_ALL if radius is None else MODE_RADIUS,
           "radius_ms": radius, "report": rep}
    res["text"] = report_text(res, g)
    return res


def _stats(moves, step, div, dropped) -> dict:
    a = np.abs(np.asarray([m for m in moves if m != 0.0], dtype=float)) \
        if any(m != 0.0 for m in moves) else np.zeros(0)
    if a.size:
        med = float(np.median(a))
        p90 = float(np.percentile(a, 90))
        mx = float(a.max())
    else:
        med = p90 = mx = 0.0
    return {"n": len(moves), "n_moved": int((a.size)), "n_dropped": len(dropped),
            "step_ms": round(step, 6), "div": int(div),
            "move_median_ms": round(med, 4), "move_p90_ms": round(p90, 4),
            "move_max_ms": round(mx, 4),
            "frac_of_step": round(med / step, 4) if step > 0 else 0.0,
            "frac_over_quarter": round(float(np.mean(a > 0.25 * step)), 4)
            if a.size else 0.0,
            "worst": sorted(
                [{"i": i, "move_ms": round(m, 4)}
                 for i, m in enumerate(moves) if abs(m) == mx], key=lambda x: x["i"])
            if a.size else []}


def report_text(res: dict, g: GridPlan | None = None) -> str:
    """人话报告（上屏走 `warning_list`，不许静默）。"""
    if not res or not res.get("ok"):
        return "[去噪] 没做：{}".format((res or {}).get("error", "?"))
    g = g or GridPlan(ok=True, period_ms=res["grid"]["period_ms"],
                      phase_ms=res["grid"]["phase_ms"], div=res["grid"]["div"],
                      step_ms=res["grid"]["step_ms"])
    r = res.get("report") or {}
    out = ["[去噪/吸附] {} 个点 → {} 个（丢 {} 撞格）".format(
        res["n"], res["n_out"], res.get("n_dropped", 0)),
        "    砖长 {:.4f}ms（bpm {:.3f}）· 相位 {:+.3f}ms · 分母 1/{} · 格 {:.4f}ms".format(
            g.period_ms, g.bpm, g.phase_ms, g.div, g.step_ms),
        "    挪动：中位 {:.3f}ms · p90 {:.3f}ms · max {:.3f}ms（{:.0%} 格）".format(
            r.get("move_median_ms", 0.0), r.get("move_p90_ms", 0.0),
            r.get("move_max_ms", 0.0), r.get("frac_of_step", 0.0))]
    if res.get("mode") == MODE_RADIUS:
        out.append("    半径档：{} 个点离格 > {:.3f}ms ⇒ **原样保留**".format(
            res.get("n_out_of_radius", 0), res.get("radius_ms") or 0.0))
    for n in (g.notes or []):
        out.append("    · " + n)
    if res.get("n_dropped"):
        worst = res["dropped"][:3]
        out.append("    ★ 撞格被丢（宿主会静默返回 null）：{} 个 {}".format(
            res["n_dropped"],
            "、".join("t={:.3f}→{:.3f}".format(d["t_ms"], d["at_ms"]) for d in worst)))
    return "\n".join(out)
