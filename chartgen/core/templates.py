"""节奏型模板：加载 `patterns/templates.json`，按「音值序列」匹配。

模板的身份 = **音值序列**（`travel/180`，单位四分音符拍数）。
匹配到了就直接用模板的 `travel` + `Twirl`，没匹配上的交给原 DP 兜底。

这也对应 ADOFAI 的物理：`travel = 180° × 音值`（speed 1），
所以模板里存的是角度，匹配时看的是音值。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(ROOT, "patterns", "templates.json")
#: ★ 迭代 2.2：「折弯循环」排版变体（**不进主库**，由 `SolveParams.stair_first` 打开）
STAIR_PATH = os.path.join(ROOT, "patterns", "templates_stair.json")


@dataclass
class Template:
    id: str
    name: str
    notes: list[float]
    travel: list[float]
    twirl: list[bool] = field(default_factory=list)
    pos: list[dict] = field(default_factory=list)
    beats: float = 0.0
    tags: str = ""
    src: str = ""
    rounds: int = 1
    #: ★ 迭代 2.2：**求解器权重**。同分（覆盖率相同）时大的赢；
    #:   贪心 `match()` 里它也是**第一排序键** ⇒ 先被尝试。
    #:   缺省 `0.0` ⇒ 不含这条字段的模板**顺序与老版本完全一致**。
    weight: float = 0.0

    @property
    def n(self) -> int:
        return len(self.notes)

    def describe(self) -> str:
        tv = " · ".join(f"{x:g}°" for x in self.travel)
        return f"{self.name} [{tv}]"


_cache: list[Template] | None = None


def _read(p: str) -> list[Template]:
    with open(p, encoding="utf-8") as fh:
        d = json.load(fh)
    out = []
    for t in d.get("templates", []):
        out.append(Template(
            id=t["id"], name=t.get("name", t["id"]),
            notes=[float(x) for x in t["notes"]],
            travel=[float(x) for x in t["travel"]],
            twirl=[bool(x) for x in t.get("twirl", [])],
            pos=list(t.get("pos", [])),
            beats=float(t.get("beats", sum(t["notes"]))),
            tags=t.get("tags", ""), src=t.get("src", ""),
            rounds=int(t.get("rounds", 1)),
            weight=float(t.get("weight", 0.0)),
        ))
    return out


def load(path: str | None = None, *, refresh: bool = False,
         extra: str | None = None) -> list[Template]:
    """读模板库。`extra` 是**追加**的一份（迭代 2.2 的「折弯循环」变体走这里）。

    ★ 只在 `path is None and extra is None` 时才用缓存 —— 带 `extra` 的调用
      每次都重读，避免把变体缓存进默认库。
    """
    global _cache
    if extra is not None:
        return _read(path or DEFAULT_PATH) + _read(extra)
    if _cache is not None and path is None and not refresh:
        return _cache
    out = _read(path or DEFAULT_PATH)
    if path is None:
        _cache = out
    return out


def match(rs: list[float], tpls: list[Template], tol: float = 0.006,
          *, timing: list[float] | None = None, beat_ms: float = 500.0,
          timing_tol_ms: float = 0.5, accept=None) -> list[tuple[int, Template, int]]:
    """在 rs（每格的音值，单位拍）上做贪心匹配。

    - **最长优先**：先试音值序列最长的模板
    - **轮数叠加**：同一个模板能接着重复就重复（用户口径：轮可以多次叠加）
    - **时序闸**：模板会把这一格的时长锁成 `音值 × 拍`，
      所以只在实际 Δt 离它 ≤ `timing_tol_ms` 时才用 —— 否则宁可不套模板，
      也不要把时间锁歪（量化没洗干净的地方会差几十毫秒）。

    `rs` 用**量化后**的音值（判定节奏型身份）；`timing` 给**原始**音值（判定时间对不对）。
    返回 [(起始 onset 下标, 模板, 轮数), ...]，区间互不重叠。
    """
    if not tpls or not rs:
        return []
    # ★ 第一排序键 = `weight`（迭代 2.2「求解器第一权重」）；
    #   权重全是 0（老库）时退化成原来的 `(-n, id)` ⇒ **逐字节不变**。
    order = sorted(tpls, key=lambda t: (-t.weight, -t.n, t.id))
    n = len(rs)
    tol_b = timing_tol_ms / beat_ms if beat_ms > 0 else 1.0

    def fits(i: int, t: Template) -> bool:
        L = t.n
        if i + L > n:
            return False
        for j in range(L):
            if abs(rs[i + j] - t.notes[j]) > tol:
                return False
            if timing is not None and abs(timing[i + j] - t.notes[j]) > tol_b:
                return False
        return True

    hits: list[tuple[int, Template, int]] = []
    i = 0
    while i < n:
        best = None
        best_len = 0
        for t in order:
            if not fits(i, t):
                continue
            reps = 1
            while fits(i + reps * t.n, t):        # 轮数叠加
                reps += 1
            span = reps * t.n
            if span > best_len:
                best, best_len = (t, reps), span
        if best is not None:
            t, reps = best
            if accept is None or accept(i, t, reps):
                hits.append((i, t, reps))
                i += best_len
            else:
                i += 1
        else:
            i += 1
    return hits


def match_dp(rs: list[float], tpls: list[Template], tol: float = 0.006,
             *, timing: list[float] | None = None, beat_ms: float = 500.0,
             timing_tol_ms: float = 0.5, accept=None,
             max_reps: int = 16) -> list[tuple[int, Template, int]]:
    """**时间戳窗口 DP 对位**（v0.2 项5）：全局最优覆盖，替代贪心最长优先。

    贪心的毛病：在 `i` 处看到「最长能匹配 5 格」就吃掉 5 格，但可能因此错过
    后面几个「能各吃 4 格」的窗口 —— 总覆盖反而低。DP 直接解：

        dp[j] = 覆盖前 j 格能得到的最优 (被覆盖格数, −模板次数)
        转移 ① 跳过第 j 格：dp[j+1] ← dp[j]
             ② 在 j 处放模板 t（可叠 reps 轮）：dp[j+span] ← dp[j] + span

    目标：**被覆盖格数最多**；同分时**模板次数更少**（更长、更整的段优先）。
    约束与贪心完全一致：音值容差 `tol`、时序闸 `timing_tol_ms`、
    `accept(...)` 屏障（Pause / 引擎 / 长段 / 只修非直线）。
    """
    n = len(rs)
    if not tpls or n == 0:
        return []
    tol_b = timing_tol_ms / beat_ms if beat_ms > 0 else 1.0

    def fits(i: int, t: Template) -> bool:
        L = t.n
        if i + L > n:
            return False
        for j in range(L):
            if abs(rs[i + j] - t.notes[j]) > tol:
                return False
            if timing is not None and abs(timing[i + j] - t.notes[j]) > tol_b:
                return False
        return True

    # 预先算好每个位置、每个模板能叠多少轮（含 1 轮；不可行为 0）
    # ★ 候选按 `weight` 从大到小排 ⇒ DP 在**同分**时保留先放进来的那一个
    #   （下面用的是严格 `>`）⇒ 权重高的模板赢平局。权重全 0 时顺序与老版本一致。
    _torder = sorted(tpls, key=lambda t: (-t.weight, -t.n, t.id))
    reach: list[list[tuple[int, Template, int, int]]] = [[] for _ in range(n)]
    for i in range(n):
        for t in _torder:
            if not fits(i, t):
                continue
            reps = 1
            while reps < max_reps and fits(i + reps * t.n, t):
                reps += 1
            for r in range(1, reps + 1):
                span = r * t.n
                if accept is None or accept(i, t, r):
                    reach[i].append((span, t, r, i + span))

    # dp[j] = (covered, -placements, prev_j, choice)
    NEG = (-1, 0, None, None)
    dp: list[tuple] = [NEG] * (n + 1)
    best = list(dp)
    best[0] = (0, 0, None, None)
    for j in range(n):
        cur = best[j]
        if cur[0] < 0:
            continue
        # ① 跳过
        cand = (cur[0], cur[1], j, None)
        if cand[:2] > best[j + 1][:2]:
            best[j + 1] = cand
        # ② 放模板
        for span, t, r, nxt in reach[j]:
            if nxt > n:
                continue
            c = (cur[0] + span, cur[1] - 1, j, (t, r, span))
            if c[:2] > best[nxt][:2]:
                best[nxt] = c
    # 回溯
    out: list[tuple[int, Template, int]] = []
    j = n
    while j is not None and j > 0:
        cov, _neg, prev, choice = best[j]
        if choice is None:
            j = prev if prev is not None else None
            continue
        t, r, span = choice
        out.append((prev, t, r))
        j = prev
    out.reverse()
    return out
