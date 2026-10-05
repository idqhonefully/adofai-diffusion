"""双射校验：生成器写出的 .adofai -> 用第三方 parser 反解 -> 跟 MIDI onset 对时间。

锚点说明
--------
vendored parser 的 `getRotateAngle()` 会把「第一个实体层」的角行程强行置 0
（它自己的约定："时间轴从第一个按键开始计时"，我们不动它）。
因此 parser 的累计时间 `cum[j]` 满足：

    cum[j] = Σ_{k=1..j} t(T_k)
    onset[j+1] - onset[1] = Σ_{k=1..j} t(T_k)

所以对照时用 **onset[j+1] - onset[1]  <->  cum[j]**，两边锚点一致、无歧义。
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from vendor.adofai_timemodel import ADOAngle, ADOLevelData   # noqa: E402


@dataclass
class VerifyResult:
    ok: bool
    n_checked: int
    max_err_ms: float
    mean_err_ms: float
    p99_err_ms: float
    first_bad: tuple[int, float, float] | None
    note: str = ""

    def summary(self) -> str:
        flag = "OK" if self.ok else "FAIL"
        s = (f"[{flag}] 对照 {self.n_checked} 层  最大误差 {self.max_err_ms*1000:.1f} us  "
             f"平均 {self.mean_err_ms*1000:.1f} us  p99 {self.p99_err_ms*1000:.1f} us")
        if not self.ok and self.first_bad:
            i, got, exp = self.first_bad
            s += f"  首个超差: floor#{i} got={got:.3f}ms exp={exp:.3f}ms"
        if self.note:
            s += f"  ({self.note})"
        return s


def load_chart(path: str):
    ald = ADOLevelData.new(path)
    ald.decode()
    return ADOAngle(ald)


def parse_times(path: str) -> tuple[list[float], ADOAngle]:
    a = load_chart(path)
    a.getRotateAngle()
    a.getAbsBeatList()
    iv = a.getPressIntervalList()
    cum, t = [], 0.0
    for x in iv:
        t += float(x or 0.0)
        cum.append(t)
    return cum, a


def verify_file(path: str, onset_times_ms: list[float], tol_ms: float = 1.0,
                lead_floors: int = 1) -> VerifyResult:
    """用 parser 反解 .adofai，与 onset 时间对照。

    lead_floors：谱面最前面有几层是「开局站位」（不消耗按键）。
    这批生成器固定为 1（见 core.solve 里"前置引导层"的注释）。

    锚点：parser 会把第 0 层的角行程强制置 0，而生成器的第 0 层正是引导层
    （travel 180 = 应被置 0 的那一层），所以两边天然对齐：
        parser_cum[j] = Σ_{k=1..j} T_k          (j≥1)，cum[0] = 0
        第 k 层(k≥1) 的角长时间 = Δt_{k−1}
        =>  Σ_{k=1..j} T_k = Σ_{i=0..j−1} Δt_i = onset[j] − onset[0]
    """
    cum, a = parse_times(path)
    n = min(len(cum), max(0, len(onset_times_ms) - lead_floors + 1))
    if n <= 0:
        return VerifyResult(False, 0, 0.0, 0.0, 0.0, None, "层数不足")

    errs: list[float] = []
    first_bad = None
    for j in range(n):
        got = cum[j]
        exp = onset_times_ms[j + lead_floors - 1] - onset_times_ms[0]
        e = abs(got - exp)
        errs.append(e)
        if first_bad is None and e > tol_ms:
            first_bad = (j, got, exp)
    errs_sorted = sorted(errs)
    p99 = errs_sorted[min(len(errs_sorted) - 1, int(len(errs_sorted) * 0.99))]
    return VerifyResult(
        ok=(first_bad is None),
        n_checked=n,
        max_err_ms=max(errs),
        mean_err_ms=sum(errs) / len(errs),
        p99_err_ms=p99,
        first_bad=first_bad,
    )


import bisect


def verify_press_subset(path: str, press_times_ms: list[float],
                        tol_ms: float = 5.0) -> VerifyResult:
    """**按键集合**校验：模型给的每个按键时刻都能在 parser 的按键序列里找到。

    为什么需要它（`docs/18` 风险 7）：插入双押层（中旋的 `999` 格）之后
    「层 ↔ onset」**不再 1:1**，`verify_file` 的逐层对照就不适用了 ——
    实测会把一张正确的谱报成「最大误差 999.8ms」的假失败。

    但**按键时刻本身**仍然可信：实测（`doublepress_demo_120`，插了 22 个中旋层）
    184/184 个按键都能在 parser 序列里找到，最大偏差 **1.3889ms 且是常量**
    （来自中旋那一对的编码方式：两下之间固定差一点）。

    判据：每个按键在其邻域（±3 个 press）内的最近距离 ≤ `tol_ms`，
    且 parser 序列单调不减。
    """
    cum, _a = parse_times(path)
    press = [float(x) for x in press_times_ms]
    if not cum or not press:
        return VerifyResult(False, 0, 0.0, 0.0, 0.0, None, "层数不足")
    mono = all(b >= a - 1e-9 for a, b in zip(cum, cum[1:]))
    base = press[0]
    errs: list[float] = []
    first_bad = None
    for t in press:
        x = t - base
        k = bisect.bisect_left(cum, x)
        best = 1e9
        for j in range(max(0, k - 3), min(len(cum), k + 4)):
            best = min(best, abs(cum[j] - x))
        errs.append(best)
        if first_bad is None and best > tol_ms:
            first_bad = (len(errs) - 1, best, x)
    errs_sorted = sorted(errs)
    p99 = errs_sorted[min(len(errs_sorted) - 1, int(len(errs_sorted) * 0.99))]
    return VerifyResult(
        ok=(first_bad is None and mono),
        n_checked=len(errs),
        max_err_ms=max(errs),
        mean_err_ms=sum(errs) / len(errs),
        p99_err_ms=p99,
        first_bad=first_bad,
        note="" if mono else "parser 按键序列非单调",
    )


def corpus_smoke(root: str, limit: int = 0) -> tuple[int, int, list[tuple[str, str]]]:
    """拿真实语料做加载烟测（回归基线）。返回 (ok, total, fails)。"""
    import glob
    files = sorted(glob.glob(os.path.join(root, "**", "*.adofai"), recursive=True))
    if limit:
        files = files[:limit]
    fails: list[tuple[str, str]] = []
    ok = 0
    for f in files:
        try:
            parse_times(f)
            ok += 1
        except Exception as e:      # noqa: BLE001
            fails.append((f, f"{type(e).__name__}: {e}"))
    return ok, len(files), fails
