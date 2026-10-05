# -*- coding: utf-8 -*-
"""xk base 的**区间接线**：区间内注入骨架、区间外原路（定稿 = `docs/47` §3）。

用户口径（`docs/47` §1 第 12/13 条 + §3）：

    双窗口：区间用**显式 `[start, end]`**；**只有区间内采bpm**；**区间外走原路径**；
    每段可以独立选：采的音轨、**xk base（N）**。

## 关键设计：把骨架当成「合成 onset」注入，**不改 `solve()` 的架构**

```
区间内：真实 onset **全部丢掉**（采bpm 开 ⇒ ② 主轨失效，`docs/47` §1 第 5 条）
        改注入「**每块砖一个**」的合成 onset：t = φ + k·砖长，`Onset.synth=True`
区间内：**多押轨的 onset 不进骨架** —— 它只负责标「哪些砖出双押」（`dp_hit_idx`）
区间外：真实 onset **原样保留** ⇒ 走原路径（主轨采音 + 常规求解）
```

为什么这样最省：注入之后 `r = Δt / 砖长 ≡ 1.0`，再令 `base_bpm = cbpm`
⇒ `travel = 180·r/k` 取 `k=1` 就是 **180° 直线**、时长**逐位等于砖长** ——
「**绝对能对上拍**」是**构造出来的**，不是拟合出来的。

## 不许静默

* 区间内**被丢掉的真实 onset 数**、**注入的砖数**、**区间外保留的数**，全部报出来；
* 区间**重叠** ⇒ 抛（猜哪个赢都是错）；
* 起止**倒了** ⇒ 自动排先后并**说出来**；
* 八度校验（`bigline.octave_note`）的话原文带上去。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import bigline as BL
from .bigline import XkError                            # noqa: F401  （对外统一用这个）
from .onsets import Onset

#: 状态里的键名（前端 ⇄ 后端）
K_RANGES = "xk_ranges"
K_START = "start_ms"
K_END = "end_ms"
K_N = "xk_base"
K_TRACKS = "tracks"
K_LABEL = "label"

#: 合成 onset 的「假」力度/音高：只是占位，下游别拿它当音乐信息看
SYNTH_VELOCITY = 100
SYNTH_PITCH = 60

#: 「落在区间内」的容差（ms）
TOL_MS = 1e-6


# ============================================================ 区间
@dataclass
class XkRange:
    """一段「采bpm」区间：`[start_ms, end_ms]` 内铺骨架，**每段自带 N**。"""

    start_ms: float
    end_ms: float
    n: int
    tracks: tuple = ()
    label: str = ""

    def __post_init__(self):
        if self.end_ms < self.start_ms:
            self.start_ms, self.end_ms = self.end_ms, self.start_ms
        if not (self.end_ms > self.start_ms):
            raise XkError("区间的起止相同（{:.3f}ms）⇒ 空区间没有意义".format(self.start_ms))
        self.n = BL.check_n(self.n)
        if self.n == 0:
            raise XkError("区间内 N=0 没有意义（区间=采bpm，N 必须是 {}）".format(list(BL.N_ON)))
        self.tracks = tuple(int(x) for x in (self.tracks or ()))

    @property
    def span_ms(self) -> float:
        return self.end_ms - self.start_ms

    def contains(self, t_ms: float, tol: float = TOL_MS) -> bool:
        return self.start_ms - tol <= float(t_ms) <= self.end_ms + tol

    def to_dict(self) -> dict:
        return {K_START: round(self.start_ms, 6), K_END: round(self.end_ms, 6),
                K_N: self.n, K_TRACKS: list(self.tracks), K_LABEL: self.label}

    def describe(self, line: "BL.BigLine | None" = None) -> str:
        s = "区间 {:.3f}~{:.3f}ms（{:.0f}ms）· {}k".format(
            self.start_ms, self.end_ms, self.span_ms, self.n)
        if line is not None and line.ok and line.n == self.n:
            ks = line.tiles_within(self.start_ms, self.end_ms)
            if ks:
                s += " · 格子 {}~{} · {} 块砖".format(ks[0] + 1, ks[-1] + 1, len(ks))
            else:
                s += " · **这段里一块砖都没有**（格子与区间没对上）"
        if self.tracks:
            s += " · 多押轨 {}".format(list(self.tracks))
        if self.label:
            s += " · {}".format(self.label)
        return s


def normalize_ranges(raw) -> list:
    """用户填的区间 → `XkRange` 列表。

    **重排先后**（并留痕）、**拒绝重叠**、**拒绝非法 N**、**拒绝空区间** —— 都抛 `XkError`。
    """
    if raw is None:
        return []
    if not isinstance(raw, (list, tuple)):
        raise XkError("{} 应该是列表（拿到 {!r}）".format(K_RANGES, type(raw).__name__))
    out = []
    for i, item in enumerate(raw):
        if isinstance(item, XkRange):
            r = item
        elif isinstance(item, dict):
            try:
                r = XkRange(
                    start_ms=float(item.get(K_START, item.get("start"))),
                    end_ms=float(item.get(K_END, item.get("end"))),
                    n=item.get(K_N, item.get("n", 0)),
                    tracks=item.get(K_TRACKS) or (),
                    label=str(item.get(K_LABEL) or ""),
                )
            except (TypeError, ValueError):
                raise XkError("第 {} 段区间的起止不是数：{!r}".format(i + 1, item)) from None
        else:
            raise XkError("第 {} 段区间不是字典：{!r}".format(i + 1, item))
        out.append(r)
    # 重叠检查（**猜哪个赢都是错** ⇒ 直接抛）
    ordered = sorted(out, key=lambda r: r.start_ms)
    for a, b in zip(ordered, ordered[1:]):
        if b.start_ms < a.end_ms - TOL_MS:
            raise XkError(
                "区间重叠：{:.3f}~{:.3f} 与 {:.3f}~{:.3f} —— "
                "请先合并（重叠时猜哪个赢都是错）".format(
                    a.start_ms, a.end_ms, b.start_ms, b.end_ms))
    return ordered


# ============================================================ 报告
@dataclass
class XkMeta:
    ok: bool = True
    why: str = ""
    line: dict = field(default_factory=dict)
    rows: list = field(default_factory=list)
    n_synth: int = 0
    n_dropped: int = 0
    n_outside: int = 0
    n_total: int = 0
    #: 两段区间**首尾相接**时，交界处那块砖两边都算 ⇒ 去重后剩下的重复数（**报出来**）
    n_dup: int = 0
    octave: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "why": self.why, "line": self.line,
                "rows": self.rows, "n_synth": self.n_synth,
                "n_dropped": self.n_dropped, "n_outside": self.n_outside,
                "n_total": self.n_total, "n_dup": self.n_dup,
                "octave": self.octave,
                # ★ 给人看的一整段（UI 直接贴上屏，**不许静默**）
                "text": self.report_text()}

    def report_text(self) -> str:
        if not self.ok:
            return "采bpm：关（{}）—— 全部走原路径".format(self.why)
        if not self.rows:
            return "采bpm：{:.0f} 个采音点全部走原路径（没有框区间）".format(self.n_total)
        out = ["采bpm：{} 段区间 ⇒ 注入骨架 {} 块砖".format(len(self.rows), self.n_synth)]
        for row in self.rows:
            out.append("  · " + row["text"])
        out.append("  · 区间内**被取代**的真实 onset {} 个（采bpm 开 ⇒ 主轨在该段失效）"
                   .format(self.n_dropped))
        out.append("  · 区间外**原样保留** {} 个 ⇒ 走原路径（主轨采音 + 常规求解）"
                   .format(self.n_outside))
        if self.n_dup:
            out.append("  · 两段**首尾相接**，交界处 {} 块砖重合 ⇒ 已去重（只留一块）"
                       .format(self.n_dup))
        if self.octave:
            out.append("  · " + self.octave)
        return "\n".join(out)


# ============================================================ 注入
def synth_onsets(line: "BL.BigLine", rng: XkRange) -> list:
    """一块区间内的骨架砖 → 合成 onset 列表（**每砖一个**）。"""
    if not line.ok:
        raise XkError("没有骨架（xk base 关）却要注入：{}".format(line.why))
    if int(line.n) != int(rng.n):
        raise XkError("骨架是 {}k，而这段区间要 {}k —— 每段的 N 各自成一条骨架，"
                      "调用方要按 N 分组".format(line.n, rng.n))
    return [Onset(t_ms=line.ms_of(k), velocity=SYNTH_VELOCITY, pitch=SYNTH_PITCH,
                  n_merged=1, pitches=(), tick=0, src_tracks=(), synth=True)
            for k in line.tiles_within(rng.start_ms, rng.end_ms)]


def inject(onsets, lines, ranges, *, octave: str = "") -> tuple:
    """把区间内的真实 onset 换成骨架砖，区间外原样 —— 返回 `(new_onsets, XkMeta)`。

    `lines`：`{N: BigLine}`（每段区间自带 N；同一 N 共用一条骨架）。
    """
    rs = normalize_ranges(ranges)
    meta = XkMeta(octave=octave)
    meta.n_total = len(onsets)
    if not lines:
        meta.ok, meta.why = False, "xk base 关（没有骨架）"
        return list(onsets), meta
    if not rs:
        meta.why = "没有框区间 ⇒ 全部走原路径"
        meta.n_outside = len(onsets)
        meta.line = {n: ln.to_dict() for n, ln in lines.items()}
        return list(onsets), meta

    keep = []
    owner = [0] * len(rs)          # 每个被取代的 onset 记到**第一段**（首尾相接时不重复计数）
    for o in onsets:
        hit = None
        for i, r in enumerate(rs):
            if r.contains(o.t_ms):
                hit = i
                break
        if hit is None:
            keep.append(o)
        else:
            owner[hit] += 1
    dropped = sum(owner)

    synth, seen, n_dup = [], set(), 0
    for i, r in enumerate(rs):
        ln = lines.get(int(r.n))
        if ln is None or not ln.ok:
            raise XkError("这段区间要 {}k，但没有对应的骨架（N={} 的骨架{}）".format(
                r.n, r.n, "是关的" if ln is not None else "没建"))
        got = []
        for o in synth_onsets(ln, r):
            key = round(o.t_ms, 6)
            if key in seen:        # ★ 首尾相接的交界砖：**只留一块**（并报数，不许静默）
                n_dup += 1
                continue
            seen.add(key)
            got.append(o)
        synth.extend(got)
        meta.rows.append({
            "text": r.describe(ln),
            "start_ms": round(r.start_ms, 6), "end_ms": round(r.end_ms, 6),
            "n": r.n, "tiles": len(got),
            "cbpm": round(ln.cbpm, 6), "period_ms": round(ln.period_ms, 6),
            "dropped": owner[i],
        })

    new = sorted(keep + synth, key=lambda o: o.t_ms)
    meta.n_synth, meta.n_dropped, meta.n_outside = len(synth), dropped, len(keep)
    meta.n_dup = n_dup
    meta.line = {n: ln.to_dict() for n, ln in lines.items()}
    return new, meta


def strip_synth(onsets) -> list:
    """只留真实 onset（去掉骨架砖）—— 需要「按音乐看」的地方用。"""
    return [o for o in onsets if not getattr(o, "synth", False)]


def synth_flags(onsets) -> list:
    """`[bool, …]`：哪些是骨架砖（给前端上色 / 给报告计数）。"""
    return [bool(getattr(o, "synth", False)) for o in onsets]


def dp_marks(onsets, dp_t, *, tol_ms: float = 45.0) -> tuple:
    """多押轨的时刻 → 「**哪些骨架砖**出双押」的下标。

    与 `sidecar/session.rebuild()` 里那段（原来是给真实 onset 用的）**同一套判据**，
    只是这里的 `onsets` 已经是注入完骨架的那一批 ⇒ 多押落点自然吸附到**砖**上。
    返回 `(hit_idx, n_missed)`：`n_missed` = 多押轨里没落到任何砖上的（**报出来**）。
    """
    ts = [o.t_ms for o in onsets]
    hit, j = [], 0
    for T in sorted(dp_t):
        while j < len(ts) and ts[j] < T - tol_ms:
            j += 1
        k = j
        best, bd = None, None
        while k < len(ts) and ts[k] <= T + tol_ms:
            d = abs(ts[k] - T)
            if bd is None or d < bd:
                best, bd = k, d
            k += 1
        if best is None:
            continue
        if best not in hit:
            hit.append(best)
    n_missed = len(dp_t) - len(hit)
    return sorted(hit), max(0, n_missed)
