"""onset 序列的**自洽子网格拟合**：周期 + 相位 + 置信度。

## 为什么需要它（docs/17）

线上流程是：librosa 估 BPM → 把 onset 吸附到 `beat/grid` 网格。
问题在于 **librosa 的 tempo 估计有 0.1%~0.3% 的相对误差**，而网格是拿估计值
算出来的：周期差 ε，网格相对音乐就以 `ε·t` 线性漂移。
63.75s 的曲子 ε=0.154% 就漂 98ms —— 于是前半段吸附把偏置纠回来、
后半段吸附把 onset **挪到错误格子上**，这就是「靠后位置随机偏移」。

本模块不再无条件相信估出来的周期，而是**用 onset 自己**把周期和相位精修到自洽：

  ① 周期：在 `p_hint` 附近搜索使**相位集中度 R**（`|Σexp(2πi·t/p)|/n`）最大的 p。
  ② 相位：R 取到最大后，`φ = arg(Σexp)`，即 onset 相对网格的公共相位。
  ③ 置信度：R 本身。R 低 ⇒ 这个网格跟音乐没关系 ⇒ 交给调用方**别吸**。

R 是对周期/相位都"自洽"的度量，且对"漏检/多检"不敏感（漏掉几个 onset 只让 R
略微下降），正好适合拿来做安全阀。

## 相位为什么要拟合

旧实现默认网格原点 = 音频 t=0，即"音乐从第 0 秒整拍开始"。真实 OGG 开头
通常是静音/气声，这个假设根本不成立；加上检波器的系统性提前量，
网格和音乐之间会差一个**整曲常量相位**。拟合相位把这一项解出来，且
**不改变任何两个 onset 的相对时间**（Δticks 不受 φ 影响，见 `snap_lattice`）。

## 单位说明

`period` 是**网格步长**（ms），不是拍长：`period = beat / grid`。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

__all__ = ["GridFit", "concentration", "phase_at", "refine_period",
           "fit_grid", "snap_lattice", "period_of"]

# 周期搜索范围（相对 p_hint 的 ±比例）。
# ★ 只做「精修」不做「换倍频」：octave 问题是另一个维度（docs/17 F2），
#   在这里搜大范围会把网格锁到错误的倍频上，反而更糟。
SPAN = 0.02
# 相对周期的搜索分辨率（两阶段：粗 → 细）
RES_COARSE = 2.0e-5
RES_FINE = 1.0e-6


@dataclass
class GridFit:
    """一次网格拟合的结果。"""
    period: float          # 精修后的网格步长（ms）
    phase: float           # 网格相位 φ ∈ [0, period)
    conf: float            # 集中度 R ∈ [0,1]，越高越说明"这是真网格"
    rms: float             # 拟合后残差 rms（ms）
    vmax: float            # 拟合后 |残差| 最大值（ms）
    hit: float             # 落在容差内的 onset 比例
    n: int
    period_hint: float
    ok: bool = False       # 是否通过置信度门槛（不通过 ⇒ 调用方不应吸附）
    reason: str = ""

    @property
    def bpm(self) -> float:
        return self.bpm_for(1)

    def bpm_for(self, grid: int) -> float:
        """按 `grid` 反推 BPM：beat = period·grid。"""
        g = max(1, int(grid))
        return 60_000.0 / max(1e-9, self.period * g)

    def describe(self) -> str:
        tag = "OK" if self.ok else f"skip({self.reason})"
        return (f"p={self.period:.4f}ms φ={self.phase:+.2f} R={self.conf:.3f} "
                f"rms={self.rms:.2f} max={self.vmax:.2f} n={self.n} "
                f"[{tag}] hint={self.period_hint:.4f}")


def _R_curve(t: np.ndarray, ps: np.ndarray) -> np.ndarray:
    """向量化的集中度曲线：R(p) = |mean(exp(2πi·t/p))|。"""
    out = np.empty(len(ps), dtype=np.float64)
    t = np.asarray(t, dtype=np.float64)
    # 分块，避免 n×m 复数矩阵过大
    blk = max(1, int(2_000_000 // max(1, len(t))))
    for a in range(0, len(ps), blk):
        q = ps[a:a + blk]
        ang = (2.0 * np.pi / q)[:, None] * t[None, :]
        c = np.cos(ang).mean(axis=1)
        s = np.sin(ang).mean(axis=1)
        out[a:a + blk] = np.hypot(c, s)
    return out


def concentration(t, period: float) -> tuple[float, float, np.ndarray]:
    """固定周期 → (R, φ, 残差 ms)。残差 = 到最近格点的有符号距离，∈(−p/2, p/2]。"""
    t = np.asarray(t, dtype=float)
    if t.size == 0:
        return 0.0, 0.0, np.zeros(0)
    ang = 2.0 * np.pi * (t / period)
    c, s = float(np.cos(ang).mean()), float(np.sin(ang).mean())
    R = float(np.hypot(c, s))
    phi = float((np.arctan2(s, c) / (2.0 * np.pi)) % 1.0 * period)
    d = np.mod(t - phi + 0.5 * period, period) - 0.5 * period
    return R, phi, d


def phase_at(t, period: float) -> tuple[float, float]:
    """固定周期只解相位 → (R, φ)。"""
    R, phi, _ = concentration(t, period)
    return R, phi


def period_of(bpm: float, grid: int) -> float:
    """BPM + 网格数 → 网格步长（ms）。"""
    return 60_000.0 / max(1e-9, float(bpm) * max(1, int(grid)))


def refine_period(t, p_hint: float, *, span: float = SPAN,
                  n_coarse: int | None = None) -> tuple[float, float, float]:
    """在 `p_hint(1±span)` 上找让 R 最大的周期（两阶段精修）。

    返回 `(period, R, φ)`。数据太少（< 3 个 onset）时原样返回 hint。
    """
    t = np.asarray(t, dtype=float)
    if t.size < 3 or p_hint <= 0:
        R, phi, _ = concentration(t, max(1e-9, p_hint))
        return float(p_hint), R, phi

    if n_coarse is None:
        n_coarse = int(min(1201, max(65, 2.0 * span / RES_COARSE + 1)))
    ps = p_hint * (1.0 + np.linspace(-span, span, n_coarse))
    R = _R_curve(t, ps)
    k = int(np.argmax(R))

    # 细搜：在粗搜最优点 ±1 个粗格内加密
    d = (ps[1] - ps[0]) if n_coarse > 1 else p_hint * RES_COARSE
    lo, hi = max(1e-6, ps[k] - d), ps[k] + d
    n_fine = 401
    ps2 = np.linspace(lo, hi, n_fine)
    R2 = _R_curve(t, ps2)
    k2 = int(np.argmax(R2))
    # 抛物线插值（亚格点）
    if 0 < k2 < n_fine - 1:
        y0, y1, y2 = R2[k2 - 1], R2[k2], R2[k2 + 1]
        den = (y0 - 2.0 * y1 + y2)
        if abs(den) > 1e-12:
            u = 0.5 * (y0 - y2) / den
            if abs(u) <= 1.0:
                p_best = ps2[k2] + u * (ps2[1] - ps2[0])
                # 用插值点重算（R 单调时够用；不单调则退回格点）
                _R, phi, _d = concentration(t, p_best)
                if _R >= R2[k2] - 1e-12:
                    return float(p_best), float(_R), phi
    R_best, phi, _ = concentration(t, ps2[k2])
    return float(ps2[k2]), float(R_best), phi


def fit_grid(t, p_hint: float, *, span: float = SPAN, min_conf: float = 0.30,
             max_res_frac: float = 0.25, refine: bool = True,
             max_frac_dev: float | None = None, min_n: int = 6,
             min_z: float = 2.5) -> GridFit:
    """把 onset 拟合到自洽网格。

    门槛（任一不过 ⇒ `ok=False`，调用方应当**放弃吸附**）：
      - `n ≥ min_n`
      - `R ≥ min_conf` 且 `R·√n ≥ min_z`
        （Rayleigh 检验：n 很小时 R 会虚高 —— 两个点总能"完美对齐"）
      - 残差中位数 `≤ max_res_frac · period`
      - 周期相对 hint 的偏差 `≤ max_frac_dev`（防细搜跑飞）

    ★ 门槛的哲学：**宁可不吸，也不要吸到错误的格子上**。
      不吸只是保留检波器的常量偏置（可标定），吸错则是随位置的漂移。
    """
    t = np.asarray(t, dtype=float)
    n = int(t.size)
    if n == 0:
        return GridFit(p_hint, 0.0, 0.0, 0.0, 0.0, 0.0, 0, p_hint, False, "empty")

    if refine:
        period, R, phi = refine_period(t, p_hint, span=span)
    else:
        period = float(p_hint)
        R, phi = phase_at(t, period)

    # concentration 返回的残差已经是相对拟合相位 φ 的
    _, _, d = concentration(t, period)
    rms = float(np.sqrt((d ** 2).mean()))
    vmax = float(np.abs(d).max())
    med = float(np.median(np.abs(d)))
    hit = float(np.mean(np.abs(d) <= 0.25 * period))

    z = float(R * np.sqrt(max(1, n)))
    ok, why = True, ""
    if n < min_n:
        ok, why = False, f"n={n}<{min_n}"
    elif R < min_conf:
        ok, why = False, f"R={R:.3f}<{min_conf:g}"
    elif z < min_z:
        ok, why = False, f"z={z:.2f}<{min_z:g}"
    elif med > max_res_frac * period:
        ok, why = False, f"med={med:.1f}ms>{max_res_frac * period:.1f}ms"
    if ok and max_frac_dev is not None and p_hint > 0:
        dev = abs(period / p_hint - 1.0)
        if dev > max_frac_dev:
            ok, why = False, f"dev={dev * 100:.2f}%>{max_frac_dev * 100:.2f}%"
    return GridFit(period=float(period), phase=float(phi), conf=float(R),
                   rms=rms, vmax=vmax, hit=hit, n=n,
                   period_hint=float(p_hint), ok=ok, reason=why)


def snap_lattice(t, period: float, phase: float, *,
                 tol_frac: float = 0.5,
                 max_move_frac: float = 0.35,
                 min_gap_ms: float = 0.0) -> np.ndarray:
    """吸附到 `{φ + k·period}` 格点；单次挪动上限 `min(tol_frac,max_move_frac)·period`。

    ★ **Δtick 不受 φ 影响**：`tick = round((φ+k·p)·PPQN/(p·grid))`
      = `k·PPQN/grid + round(φ·PPQN/(p·grid))`，
      而 `PPQN/grid` 对 `grid | 480` 是整数 ⇒ 任意两个 onset 的 tick 差
      仍然是干净的 `Δk·PPQN/grid`（见 `core/audio_onsets.load_as_midi`）。
    """
    t = np.asarray(t, dtype=float)
    if t.size == 0 or period <= 0:
        return t
    cap = max(0.0, min(float(tol_frac), float(max_move_frac))) * period
    target = np.rint((t - phase) / period) * period + phase
    move = np.abs(target - t)
    out = np.where(move <= cap, target, t)
    out = np.unique(np.round(out, 6))
    if min_gap_ms > 0 and len(out) > 1:
        keep = [0]
        for i in range(1, len(out)):
            if out[i] - out[keep[-1]] >= min_gap_ms:
                keep.append(i)
        out = out[keep]
    return out
