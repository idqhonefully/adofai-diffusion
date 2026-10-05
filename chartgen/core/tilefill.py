# -*- coding: utf-8 -*-
"""**补格**：按 bpm 网格，在相邻采音点之间补「合成 onset」（`docs/71` §9 的第 3 条手段）。

## 为什么需要它

从 MIDI 还原一张**人类写的**谱面时，两边按键数会差出一大截 —— 实测
（Flower Rocket，`docs/71`）：

| 边 | 按键数 | 每拍 |
|---|---|---|
| 参考谱（人写的） | **2543** | 3.31 |
| 我们（一条 onset 一格） | **1742** | 2.27 |

人类打谱**不会**让一格去跨半个拍：他们会在间隔里按 bpm 网格再铺几格。
所以「一格 = 一个 onset」这条**硬性约束**必须能松开 —— 这就是本模块。

## 关键设计：与 `core/xkbase.py` 同一条路 —— **注入合成 onset，不改 `solve()`**

`xkbase` 已经证明这条架构最省（`docs/47` §3）：往 `onsets` 里塞
`Onset(..., synth=True)`，下游求解 / 写盘 / 反解校验**一行都不用改**。
本模块只做**更保守**的一件事：

* **一个真实 onset 都不丢**（`xkbase` 是区间内全丢换成骨架砖）；
* 只在「相邻两个真实 onset 之间**离得够远**」时才补；
* 补的点落在 `t = a + k·(砖长/N)` 上 —— **锚在间隔起点**，不是全局网格，
  所以不会因为全曲 bpm 微漂而跟参考谱错开。

## 参数口径

`div` 是「一个**砖长**切几份」（砖长由 `core.denoise.plan` 从采音结果自己测出来，
那就是「采bpm」）。`min_steps` 是「间隔至少要有几份才值得补」——太小会把
八分音符也切碎（实测噪声），太大会漏掉人写的补格。

## 不许静默

砖长、bpm、切几份、看了多少个间隔、补了几个、**因为上限被截掉几个**，全部报出来；
砖长测不出来（`GridPlan.ok=False`）⇒ **一格都不补**并把原因写进 `why`，
**不猜一个砖长硬上**。
"""
from __future__ import annotations

from bisect import bisect_left as _bisect
from dataclasses import dataclass

from . import denoise as dn
from .onsets import Onset

#: 合成 onset 的占位力度/音高（与 `xkbase` 对齐：只是占位，下游别当音乐信息看）
SYNTH_VELOCITY = 100
SYNTH_PITCH = 60

#: `div` 的合法档：一个砖长切 2 / 4 / 8 份（= 1/4、1/8、1/16 拍，砖长取半拍时）
DIV_CHOICES = (2, 4, 6, 8)

#: 默认：砖长切 4 份、间隔至少 3 份才补。
#: ★ 实测（Flower Rocket，`docs/71` §11）这一档把「谱面↔谱面 一对一」从
#:   **60.4% → 69.2%**，且**不会**把覆盖打下去（82.0% → 84.0%）。
DEFAULT_DIV = 4
DEFAULT_MIN_STEPS = 3

#: 补出来的 onset 数上限（0 = 不限）。防手滑把 4747 个音补成 10 万个。
DEFAULT_MAX_ADD = 20000


@dataclass
class FillMeta:
    """补格这一步的账（UI / 报告直接贴上屏）。"""

    on: bool = False
    why: str = ""
    period_ms: float = 0.0
    bpm: float = 0.0
    div: int = 0
    min_steps: int = 0
    max_per_gap: int = 0
    min_merged: int = 1
    per_chord: bool = False
    step_ms: float = 0.0
    n_in: int = 0
    n_added: int = 0
    n_gaps: int = 0
    n_gaps_filled: int = 0
    n_capped: int = 0
    n_collide: int = 0
    n_skip_single: int = 0
    hint_from: str = ""
    max_add: int = 0

    def to_dict(self) -> dict:
        d = {k: (round(v, 6) if isinstance(v, float) else v)
             for k, v in self.__dict__.items()}
        d["text"] = self.report_text()
        return d

    def report_text(self) -> str:
        if not self.on:
            return "补格：关（{}）".format(self.why or "没开")
        if not self.n_added:
            return ("补格：砖长 {:.3f}ms（bpm {:.1f}）· 切 1/{} · "
                    "{:.3f}ms 一份 ⇒ **一个都没补**"
                    "（{} 个间隔都短于 {} 份）"
                    .format(self.period_ms, self.bpm, self.div, self.step_ms,
                            self.n_gaps, self.min_steps))
        out = ["补格：砖长 {:.3f}ms（bpm {:.1f}）· 切 1/{} · {:.3f}ms 一份"
               .format(self.period_ms, self.bpm, self.div, self.step_ms),
               "  · 每个够长的间隔最多补 {} 个{}"
               .format(self.max_per_gap if self.max_per_gap else "**不限（铺满）**",
                       "（**按和弦大小摊开**：n 个音摊成 n 格）"
                       if self.per_chord else ""),
               "  · 和弦门：间隔起点至少要 {} 个同时音（实测人写的谱"
               "单音起头只有 18% 会补、和弦起头 44~68%）".format(self.min_merged),
               "  · {} → {} 个采音点（**补了 {} 个**，锚在间隔起点、真实 onset 一个没丢）"
               .format(self.n_in, self.n_in + self.n_added, self.n_added),
               "  · 看了 {} 个间隔，其中 {} 个够长（≥ {} 份 = {:.1f}ms）被补，"
               "{} 个被和弦门挡掉"
               .format(self.n_gaps, self.n_gaps_filled, self.min_steps,
                       self.min_steps * self.step_ms, self.n_skip_single)]
        if self.n_collide:
            out.append("  · 有 {} 个补点**撞在已有采音点上**（≤ 1µs）⇒ 跳过"
                       "（不挤成 0ms 双押）".format(self.n_collide))
        if self.n_capped:
            out.append("  · ★ 撞到上限 {} ⇒ **截掉了 {} 个补点**"
                       "（想全补就把上限调大）".format(self.max_add, self.n_capped))
        if self.hint_from:
            out.append("  · 砖长依据：{}".format(self.hint_from))
        return "\n".join(out)


# ============================================================ 砖长（采bpm）
def detect_period(onsets: list, hint_ms: float | None = None,
                  offset_ms: float | None = None) -> tuple[float, float, str, str]:
    """从采音结果自己测**砖长** ⇒ `(period_ms, bpm, 依据, 失败原因)`。

    直接复用 `core.denoise.plan` —— 它就是项目里「从 onset 间隔直方图测速度」的
    那条路（间隔对数直方图众数 → 周期精修 → 取整齐值 → 置信度）。
    **不另造一套**：另造一套就等于有两个「采bpm」会说不同的话。

    测不出来（`ok=False`）⇒ 返回 `(0, 0, "", 原因)`，调用方**一格都不补**。
    """
    ms = [float(x.t_ms if hasattr(x, "t_ms") else x) for x in onsets]
    if len(ms) < 3:
        return 0.0, 0.0, "", "采音点太少（{} 个）测不出砖长".format(len(ms))
    try:
        g = dn.plan(ms, hint=hint_ms, offset_ms=offset_ms)
    except Exception as exc:                                     # noqa: BLE001
        return 0.0, 0.0, "", "测速抛了 {}：{}".format(type(exc).__name__, exc)
    if not getattr(g, "ok", False):
        return 0.0, 0.0, "", "网格不成立：{}".format(
            getattr(g, "reason", "") or "没给原因")
    p = float(getattr(g, "period_ms", 0.0) or 0.0)
    if p <= 0:
        return 0.0, 0.0, "", "测出来的砖长是 {}".format(p)
    notes = (getattr(g, "notes", None) or [])
    return p, float(getattr(g, "bpm", 0.0) or 0.0), (notes[0] if notes else ""), ""


# ============================================================ 补格
def fill(onsets: list, *, div: int = DEFAULT_DIV,
         min_steps: int = DEFAULT_MIN_STEPS,
         max_per_gap: int = 1,
         min_merged: int = 1,
         per_chord: bool = False,
         period_ms: float | None = None,
         offset_ms: float | None = None,
         max_add: int = DEFAULT_MAX_ADD) -> tuple[list, FillMeta]:
    """在相邻采音点之间按 `砖长/div` 补合成 onset。**真实 onset 一个都不动。**

    返回 `(new_onsets, FillMeta)`。`div <= 0` ⇒ 原样返回（**逐字节不变**）。

    `max_per_gap`：**每个间隔最多补几个**（在 `t = a + k·step` 上从 k=1 起取）。
    * `1`（默认）= 只在间隔里补**一个**点（= `a + 砖长/div`）；
    * `0` = 不限（把整个间隔铺满）。

    ★ 为什么默认是 **1** 而不是铺满（实测，Flower Rocket）：「铺满 1/8 拍网格」
    会补出 5509 个点、覆盖 96.1% 但**命中只有 41%** ⇒ 一对一反而掉到 41.1%；
    「每个够长的间隔只补一个」是 69.2% —— 因为人写的谱**不是**处处铺满，
    只在部分间隔里插一格。

    ## ★★ `min_merged` / `per_chord`：**和弦摊开**（实测最有效的那个门）

    用户 2026-10：「通过**双押轨道**（用分段采音辅助的）进一步加强」。

    在 ADOFAI 里，**同时响的几个音没法一次按完**，所以人手会把和弦**摊开**
    成连续的几格。实测（Flower Rocket，1740 个间隔）：

    | 间隔起点的和弦大小 | 间隔个数 | 被参考谱补过点的比例 |
    |---|---|---|
    | 1（单音） | 605 | **18.0%** |
    | 2 | 370 | **43.8%** |
    | 3 | 139 | **68.3%** |
    | 4 | 318 | **57.5%** |

    ⇒ `min_merged=2` 只补和弦起头的间隔（把 18% 那批噪声挡掉）；
    `per_chord=True` 再把上限设成 `和弦大小 − 1`（n 个音摊成 n 格）。

    ★ 补点只落在**开区间** `(a, b)` 内、且离已有 onset > 1µs ⇒ 不会挤成 0ms 双押。
    """
    meta = FillMeta(n_in=len(onsets), div=int(div), min_steps=int(min_steps),
                    max_per_gap=int(max_per_gap), min_merged=int(min_merged),
                    per_chord=bool(per_chord))
    if int(div) <= 0:
        meta.why = "div=0 ⇒ 关"
        return list(onsets), meta
    if int(div) not in DIV_CHOICES:
        meta.why = "div={} 不在 {} 里 ⇒ **不补**（不猜）".format(div, list(DIV_CHOICES))
        return list(onsets), meta
    if len(onsets) < 2:
        meta.why = "采音点少于 2 个"
        return list(onsets), meta

    if period_ms and period_ms > 0:
        p, bpm, why = float(period_ms), 60000.0 / float(period_ms), "调用方指定"
    else:
        p, bpm, why, bad = detect_period(onsets, offset_ms=offset_ms)
        if p <= 0:
            meta.why = bad
            return list(onsets), meta
    meta.on = True
    meta.period_ms, meta.bpm, meta.hint_from = p, bpm, why
    step = p / float(meta.div)
    meta.step_ms = step
    need = step * float(meta.min_steps)

    sx = sorted(onsets, key=lambda o: float(o.t_ms))
    times = [float(o.t_ms) for o in sx]
    out = list(sx)
    meta.max_add = int(max_add)
    for a_o, b_o in zip(sx, sx[1:]):
        a, b = float(a_o.t_ms), float(b_o.t_ms)
        meta.n_gaps += 1
        if b - a < need:
            continue
        nmerge = int(getattr(a_o, "n_merged", 1) or 1)
        if nmerge < meta.min_merged:
            meta.n_skip_single += 1                               # 单音起头 ⇒ 不补
            continue
        cap = max_per_gap
        if meta.per_chord:
            cap = min(nmerge - 1 if nmerge > 1 else 0, max_per_gap)
            if cap <= 0:
                meta.n_skip_single += 1
                continue
        k = 1
        added_here = 0
        capped_here = 0
        while True:
            t = a + k * step
            k += 1
            if t >= b - 1e-6:
                break
            # ★★ 每个间隔的配额要**同时**算上「补成功的」和「被上限截掉的」——
            #   只算成功的会死循环：被截掉时 `added_here` 不动 ⇒ 配额永远不触发
            #   ⇒ 会把整个间隔的每个网格点都记一遍 `n_capped`（实测报出 98 个，真值 14）。
            if cap > 0 and added_here + capped_here >= cap:
                break
            i = _bisect(times, t)
            if any(abs(t - times[j]) <= 1e-6
                   for j in (i - 1, i) if 0 <= j < len(times)):
                meta.n_collide += 1                                # 撞上真实 onset ⇒ 不补
                continue
            if max_add > 0 and meta.n_added + added_here >= max_add:
                meta.n_capped += 1
                capped_here += 1
                continue
            out.append(Onset(t_ms=t, velocity=SYNTH_VELOCITY, pitch=SYNTH_PITCH,
                             n_merged=1, pitches=(), tick=0, src_tracks=(),
                             synth=True))
            added_here += 1
        if added_here:
            meta.n_gaps_filled += 1
        meta.n_added += added_here
    out.sort(key=lambda o: float(o.t_ms))
    return out, meta


def dedup(onsets: list, tol_ms: float = 1e-6) -> tuple[list, int]:
    """去掉挤在同一时刻的采音点（保**真实**的、丢**合成**的）。返回 `(列表, 丢了几个)`。

    为什么需要：补点撞上真实 onset 时会造出一个 0ms 的「双押」—— 那不是音乐，
    是网格对齐的副产物。
    """
    out: list = []
    n_drop = 0
    for o in sorted(onsets, key=lambda x: (float(x.t_ms), bool(getattr(x, "synth", False)))):
        if out and float(o.t_ms) - float(out[-1].t_ms) <= tol_ms:
            n_drop += 1
            continue
        out.append(o)
    return out, n_drop
