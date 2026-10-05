# -*- coding: utf-8 -*-
"""角度双押插入（项3，v0.2 定稿）。

机制
----
把目标格 `travel = T` 拆成一对 `[θ, T−θ]`（**薄格在前**）：总 travel 不变 ⇒
**零时长偏移**；薄格比原格早 θ/180 个本地拍按下 ⇒ 两下落在同一判定窗口 = 双押。

★ **θ 不是固定的 15°/30°**，而是按 Δ 预算反解（`docs/33`）：

    Δ = (θ/180) · (60000 / **该格自己的 bpm**)      ← 双押两下的间隔（ms）
    θ ≤ Δ预算 · 180 · bpm / 60000

用户口径 2026-10：「**力求所有 bpm 下的双押产生的偏移时值降低到 25ms 以内**」
⇒ `SKEW_MAX_MS = 25.0`，`choose_thin()` 在候选阶梯里挑满足预算的最大一档。
**必须用该格自己的 bpm**（慢速档 `k` 会把 Δ 放大 k 倍），不能用整谱 `base_bpm`。
`angleData` 是任意浮点、**没有 15° 限制**（`docs/02`），所以低 bpm 下 θ 可以小于 15°。
`θ` 按有效 BPM 选：`< 300 → 15°`，`≥ 300 → 30°`（语料实测口径）。

两种等价编码（`docs/16` §4.2）
------------------------------
| 编码 | 写入 angleData 的 raw travel | Twirl |
|---|---|---|
| **正常** | `[θ, T−θ]` | 无 |
| **反向** | `[360−θ, 360−(T−θ)]` | **薄格上加一个** |

两者**游戏实测的有效 travel 完全相同**（时序一模一样），只差几何：

- 反向写法每加一个缺口就翻转一次 parity ⇒ 相邻缺口的 180° 折返**方向相反**
  ⇒ 轨道逐段朝另一侧铺开。
- 正常写法 parity 不变 ⇒ 每个缺口都朝同一侧折返 ⇒ 高密度时轨道**原路重合**
  （实测：最近距 0.000，宽度塌成 1 格）。

**⇒ 默认用反向写法**（用户口径：高密度下重叠率只有正常的 5%~8%，更适合读）。
唯一的例外：薄格正好落在 SetSpeed 格上（社区硬规则「Twirl 不能与 SetSpeed 同格」），
此时自动退回正常写法。

模型口径
--------
`Floor.travel` = **有效 travel**（游戏实测的那个，也用于计时）。
反向写法的「补角 angleData」**不需要额外字段**：只要把 Twirl 放在薄格上，
`core/path.py` 的 `turn = 180 − travel`（ccw=True）就会把 angleData 写成补角
（`a_i − a_{i−1} = travel − 180`），正是「写 360−t + parity 翻」的编码。
所以 `times_from_chart` 与 angleData 自动一致，时序不变。

护栏分两层（`docs/31` §5.1「b」）
-------------------------------
- **硬保护**（永不拆）：开局站位 / 尾层 / 模板段 / 三连音引擎段 / 魔法阵雪花格。
- **软保护**（默认可拆）：自然闭合段的填充格 —— 它只是「把剩下的时间铺满」的
  配平量，拆一格使 `Σtravel` 不变 ⇒ **总时长一分不差、闭合性不变**。
  以前软保护也被当成硬保护，于是满直线谱（全是自然填充格）上
  `owned == dp_onsets`，**双押一个都插不进去还不报错**。
- 带 `SetSpeed` 的格不拆：参考谱 A 的注释「调速一定要放在平着的格子上面」。
- `snap_ms > 0` 时允许在 ±`window` 格内换位，且只换到音头差 ≤ `snap_ms` 的格；
  默认 0（宁可少插，绝不挪时间）。
"""
from __future__ import annotations

import bisect
from dataclasses import replace

from .solve import Chart, Floor

#: 判据速度：有效 BPM（= 该格自己的 bpm）< 阈值 → 薄角 15°，否则 30°
#: ★ 2026-10 起这套「固定两档」**只在 `SKEW_MAX_MS <= 0`（关掉预算）时**才用，
#:   默认走下面的 Δ 预算（见 `choose_thin`）。
CBPM_THRESHOLD = 300.0
THIN_LOW = 15.0
THIN_HIGH = 30.0

#: ★★ 用户口径 2026-10：「**力求所有 bpm 下的双押产生的偏移时值降低到 25ms 以内**」。
#:
#: 偏移是什么（`docs/33`）：
#:
#:     Δ = θ · m,   m = (60000 / bpm) / 180 = 每 1° 的毫秒数
#:       = (θ / 180) · (60000 / bpm)
#:
#: 它是**双押这一对内部**两下的间隔（薄格 entry 与剩格 entry）。一格只有一个
#: entry，所以 Δ ≠ 0 是几何必然；能不能算「同时按下」只看 Δ 是否在判定窗口内。
#:
#: 反解预算：**想让 Δ ≤ S ⇒ θ ≤ S · 180 · bpm / 60000**。
#:
#: ⚠ 必须用**这一格自己的 bpm**（`Floor.bpm = base_bpm / k`），不是整谱的
#: `base_bpm` —— 慢速档上的同一格，Δ 会被放大 k 倍。
SKEW_MAX_MS = 25.0

#: 薄角候选阶梯（从大到小）。既有语料实证过的 60 / 45 / 30 / 22.5 / 15 / 9，
#: 也有往下细分的那几档 —— 低 bpm 时只有它们才能把 Δ 压进预算。
#: （`docs/02`：`angleData` 是任意浮点，**没有 15° 限制**；真正会出事的只有
#:   `angleMoved <= 1e-6`（≈ travel ≈ 0）那把整圈掉头的特判，远低于这里的下限。）
THIN_LADDER = (60.0, 45.0, 30.0, 22.5, 15.0, 12.0, 9.0, 7.5, 6.0,
               4.5, 3.0, 2.0, 1.5, 1.0, 0.75, 0.5)
#: 薄角下限：再小就没有可读性了。低于它时**预算做不到**，会如实报 `skew_over`。
#: 0.5° 对应 Δ ≤ 25ms 的局部 bpm 下限 ≈ 6.7（`solve` 的 `bpm_min=80` + 最大 `k=8`
#: ⇒ 实际最低局部 bpm ≈ 10，够用）。
THIN_MIN = 0.5
#: 薄角上限（语料里最大的成对角度是 60°）
THIN_MAX = 60.0

#: ★ 反向写法的**原始** travel 是 `360 − θ`。项目自己的口径是「travel 落在
#:   [15°, 345°] 才不是回头方块」，而反向薄格要满足它就需要 `θ ≥ 15°`。
#:   低于这个角度一律退回**正常写法**（`[θ, T−θ]` 直写，没有补角），
#:   免得写出一个原始值 359.25° 的薄格。
REVERSE_MIN_THETA = 15.0

#: 拆完后「剩余格」的 travel 不能低于它（否则成回头方块）。
#: ★ 薄格不受这条约束 —— 它是机制需要的形状（`rules` 对 `dp_pairs` 豁免），
#:   而且**必须**能低于 15°，否则低 bpm 下 Δ 压不进预算。
MIN_TRAVEL = 15.0


MODE_REVERSE = "reverse"      # 补角 + 薄格 Twirl（默认；高密度不重合）
MODE_NORMAL = "normal"        # 直接用 [θ, T−θ]，无 Twirl


def theta_for_skew(bpm: float, skew_max_ms: float = SKEW_MAX_MS) -> float:
    """反解预算：Δ ≤ `skew_max_ms` 允许的最大薄角（度）。

        θ_max = S · 180 · bpm / 60000
    """
    b = float(bpm)
    if b <= 0:
        return THIN_MAX
    return float(skew_max_ms) * 180.0 * b / 60000.0


def skew_ms(theta: float, bpm: float) -> float:
    """这一对双押两下的间隔（ms）。`Δ = (θ/180)·(60000/bpm)`。"""
    b = float(bpm)
    if b <= 0:
        return 0.0
    return (float(theta) / 180.0) * (60000.0 / b)


# ============================================================ ★★ 固定角度表
#: 用户 2026-10 **定死**：「双押**不是**按照**毫秒均匀计算**的，就是**一个在不同 bpm
#: 窗口下均匀使用的多种规定写法**。**不能自定义角度**」。
#:
#: ⇒ 薄角**按该格自己的 bpm 查表**（`(窗口下界, θ)`，从大到小），**不再按 Δ 反解**。
#:   ★ 旧口径（2026-10「力求所有 bpm 下双押的偏移 ≤ 25ms」）**作废**；
#:   `choose_thin`（Δ 预算反解）**只在「使用固定双押角度」关掉时**才用 —— 保留不删。
#:
#: 写法结构（`docs/31` §2.2.2 的统一公式 `[θ, 180·W − θ]`，`W` = 占几拍）：
#:
#: | 写法     | W   | 两格         | 什么时候用                    |
#: | -------- | --- | ------------ | ----------------------------- |
#: | 双押交错 | 1   | `[30, 150]`  | 满直线下的一般写法（每 4 拍一个） |
#: | 斜向单元 | 1   | `[30, 150]`×2| **cbpm ≥ 360**（连续双押手段） |
#: | 双押塔   | 2   | `[30, 330]`  | **cbpm < 360**（「塔是 360 以下」）|
#: | 强弱     | 0.5 | `[30, 60]`   | 需要连续高速押                 |
FIXED_THETA_TABLE = ((840.0, 90.0), (300.0, 30.0), (0.0, 15.0))

#: 「cbpm ≥ 840 用 90°」的**例外**：连续双押 ≥ 这个长度 ⇒ 仍然回 30°（用户口径）
FIXED_RUN_MAX = 4
FIXED_THETA_RUN = 30.0


def fixed_theta(bpm: float, run: int = 1) -> float:
    """按 **bpm 窗口**取规定写法的薄角（`docs/47` §5.7 / `docs/31` §2.2.2）。

    `run` = 该双押所在**连续双押段**的长度（落在相邻格上的连成一串）：
    `cbpm ≥ 840` 且 `run ≥ FIXED_RUN_MAX` ⇒ 回 30°。
    """
    th = FIXED_THETA_TABLE[-1][1]
    for lo, t in FIXED_THETA_TABLE:
        if float(bpm) >= lo:
            th = t
            break
    if th >= 90.0 and int(run) >= FIXED_RUN_MAX:
        return FIXED_THETA_RUN
    return th


# ============================================================ ★★ 三押角度组合
#: 用户 2026-10 定死：「**三押只出现在两条多押轨这一时间都有音的情况**」
#: ⇒ **押数 = 1 + 同时有音的 dp 轨数**（`docs/48` §3.1）。
#:
#: 几何沿用统一公式 `[θ…, 180·W − Σθ]` 的「**最后一块吃余量**」，`W ≡ 1`
#: （用户：「理论上没错」）：
#:
#:     双押 W=1:  [θ,       180 − θ]
#:     三押 W=1:  [θ₁, θ₂,  180 − θ₁ − θ₂]      ← 三块，**替掉一个普通格**
#:
#: 组合表（`docs/48` §4）—— **只入有据可查的两组**：
#:
#: | 窗口              | 组合        | 第三块 | 出处                                  |
#: | ----------------- | ----------- | ------ | ------------------------------------- |
#: | cbpm ≥ 1000       | `30 · 60`   | 90     | fixture `three_press_interact` 实测   |
#: | 低位（边界**待定**）| `15 · 15`   | 150    | 语料 `15·15`(678) + 用户「15 度三连」 |
#:
#: ★ **`90°·90°`（语料 1411 处）不入表**：W=1 下第三块 = `180 − 180 = 0` ⇒ **不闭合**。
#: ★ `30·60` 是 `θ,2θ`，而 `15·15` 是 `θ,θ` —— 用户：「不确定是不是巧合」⇒ **不当规矩**，
#:   按表硬写（`docs/48` §4）。
FIXED_TRIPLE_HI_BPM = 1000.0
FIXED_TRIPLE_HI = (30.0, 60.0)
FIXED_TRIPLE_LOW = (15.0, 15.0)


def fixed_theta_list(bpm: float, n_blocks: int = 1, run: int = 1) -> tuple:
    """按**薄块数**给出这一格的薄角序列 `(θ₁[, θ₂])`（剩余块吃余量，调用方自己算）。

    `n_blocks = 押数 − 1`（双押 ⇒ 1，三押 ⇒ 2）：

    - `n_blocks == 1` ⇒ 双押 `(θ,)` —— 查 `fixed_theta` 的规定写法表
    - `n_blocks == 2` ⇒ 三押 `(θ₁, θ₂)` —— 查 §上面的组合表
    - `n_blocks >= 3` ⇒ **四押 / 5押 = 不做**（`docs/48` §5）：这里退回**双押**，
      由 `plan` 记账 `extra_press` + 上屏（**不许静默**）
    """
    nb = max(1, int(n_blocks))
    if nb == 2:
        return FIXED_TRIPLE_HI if float(bpm) >= FIXED_TRIPLE_HI_BPM \
            else FIXED_TRIPLE_LOW
    return (fixed_theta(bpm, run),)


def choose_thin(bpm: float, skew_max_ms: float = SKEW_MAX_MS) -> float:
    """按 **Δ 预算**选薄角，在候选阶梯里挑**最大的、且 Δ 不超预算**的那个。

    - `skew_max_ms > 0`（默认 25ms）：`θ ≤ θ_max(bpm)` 里挑最大的一档
    - `skew_max_ms <= 0`：退回老的固定两档（`< 300 → 15°`，`≥ 300 → 30°`），
      方便 A/B 与老基线比对
    - 若连阶梯里最小的一档（1°）都超预算（局部 bpm < 13.3），返回 `THIN_MIN`
      并**放弃预算** —— 调用方会把这种情况计进 `report["skew_over"]`，不静默
    """
    b = float(bpm)
    if float(skew_max_ms or 0.0) <= 0.0:
        return THIN_HIGH if b >= CBPM_THRESHOLD else THIN_LOW
    cap = min(theta_for_skew(b, skew_max_ms), THIN_MAX)
    fit = [t for t in THIN_LADDER if t <= cap + 1e-9]
    return max(fit) if fit else THIN_MIN


# ============================================================ ★★ 押数（三押判据）
def group_marks(marks, tol_ms: float = 45.0) -> list:
    """`marks = [(t_ms, track), …]` ⇒ `[(代表时刻, **同时有音的不同轨数**), …]`。

    判据（用户 2026-10 定死）：**押数 = 1 + 同时有音的 dp 轨数**。

    聚类口径（`docs/48` §3.1）：
    - 按时刻排序后**单链**：前一个与本个差 ≤ `tol_ms` 就同组（`dp_tol` 的职责
      在 `docs/47` §5.7 已收窄成「配对」，这里就是配对）
    - 组内**按轨去重** ⇒ ★ **一条 dp 轨连续响两下 ≠ 三押**（那是两个双押）

    ⇒ 返回值里的数 = `押数 − 1`（0 = 普通单押，1 = 双押，2 = 三押…）。
    """
    out: list = []
    cur_t: float | None = None
    cur_tr: set = set()
    prev = None
    for t, tr in sorted((float(a), int(b)) for a, b in marks):
        if prev is None or (t - prev) > float(tol_ms):
            if prev is not None:
                out.append((cur_t, len(cur_tr)))
            cur_t, cur_tr = t, set()
        cur_tr.add(tr)
        prev = t
    if prev is not None:
        out.append((cur_t, len(cur_tr)))
    return out


def press_of(targets, groups, tol_ms: float = 45.0) -> tuple:
    """把 `group_marks` 的分组落到**目标时刻**上。

    ⇒ `(presses, n_unmatched)`：

        `presses[i]` = 第 i 个目标的**押数**（1 = 单/双押，2 = 三押，≥3 = 四押+）
        `n_unmatched` = 没能落到任何目标上的分组数（**要报出来**，不许静默）

    一个目标若被多个分组认领，取**最大的**押数（并集语义）。
    """
    ts = [float(x) for x in targets]
    presses = [1] * len(ts)
    unmatched = 0
    for (t, n_tr) in groups:
        best, bd = None, None
        for i, T in enumerate(ts):
            d = abs(T - float(t))
            if d <= float(tol_ms) and (bd is None or d < bd):
                best, bd = i, d
        if best is None:
            unmatched += 1
            continue
        presses[best] = max(presses[best], 1 + int(n_tr))
    return presses, unmatched


def _hard_owned(floors, i: int) -> bool:
    """第 i 格是否被**硬保护**（永远不许拆）。

    硬 = 开局站位 / 尾层 / 手写模板段 / 三连音引擎段 / 魔法阵雪花格。
    这些格的 travel 是别人算好的**结论**（模板节奏、雪花角度、引擎 BPM 除数），
    拆了就把那个图形拆坏。
    """
    if i <= 0 or i >= len(floors) - 1:
        return True
    f = floors[i]
    return bool(getattr(f, "snowflake", False) or getattr(f, "template", False)
                or getattr(f, "engine", False))


def _soft_owned(floors, i: int) -> bool:
    """第 i 格是否只被**软保护**（自然闭合段的填充格）。

    自然段只是「把剩下的时间铺满」的**贪心填充**（`core/solve.py` 的 `nat_hit`），
    它的 travel 不是某个图形的结论，而是一个**配平量**：

        Σtravel 守恒 ⇒ **总时长一分不差**（拆一格成 `[θ, T−θ]` 之和还是 T）
        逐格 bpm 不变 ⇒ **SetSpeed 位置不变**（除非本来就在这格，见下）

    所以拆它只会让那一段多一个弯，不会改变任何时间点 —— 这是「静默丢音」
    最划算的落脚点。默认允许（`allow_soft=True`）。
    """
    return bool(getattr(floors[i], "natural", False))


def _owned(floors, i: int) -> bool:
    """第 i 格是否被某条图形逻辑拥有（硬保护 ∪ 软保护）。"""
    return _hard_owned(floors, i) or _soft_owned(floors, i)


def _reserved(floors, i: int) -> bool:
    """第 i 格是否是求解前替双押**预留的槽位**（`SolveParams.dp_reserve`，见 `docs/31` §5.2）。

    预留的就是要拿来拆的：档已钉死（自己不出 SetSpeed）、图形也已让路。
    """
    return bool(getattr(floors[i], "dp_reserved", False))


def _setspeed_here(floors, i: int) -> bool:
    """第 i 格是否本来就带 SetSpeed（= bpm 与前一格不同）。

    参考谱 A 的注释是一条**硬规则**：「如果需要调速，那么一定要放在平着的格子
    上面，否则会判定为不合规」。拆一格会让它从 `180` 变成 `θ`（薄格在前），
    于是 SetSpeed 就落在斜格上了 ⇒ **这一格不能拆**，只能换位或丢。
    """
    if i <= 0 or i >= len(floors):
        return False
    return abs(float(floors[i].bpm) - float(floors[i - 1].bpm)) > 1e-9


def _candidates(k0: int, window: int):
    """落脚点搜索次序：原格 → +1 → −1 → +2 → −2 …（先近后远）。"""
    yield k0, 0
    for d in range(1, max(0, int(window)) + 1):
        yield k0 + d, d
        yield k0 - d, -d


def plan(ch: Chart, targets_ms, *, theta: float | None = None,
         mode: str = MODE_REVERSE, exclude: set[int] | None = None,
         travel_min: float = MIN_TRAVEL,
         report: dict | None = None,
         allow_soft: bool = True,
         snap_ms: float = 0.0,
         window: int = 8,
         skew_max_ms: float = SKEW_MAX_MS,
         use_fixed: bool = True,
         presses=None,
         three_press: bool = True,
         skip_press: bool = False) -> dict[int, tuple[tuple, str]]:
    """把目标时刻翻译成 `{格下标: ((θ…), 编码)}`（`docs/48` §6.2 第 3 步）。

    - **`use_fixed=True`（默认）**：薄角按**规定写法表**取（`fixed_theta`，看该格自己的
      bpm + 连续双押段长）⇒ **不看 `theta` / `skew_max_ms`**（用户 2026-10 定死：
      「不能自定义角度」「不是按毫秒均匀计算的」）
    - `use_fixed=False`：老口径 —— `theta` 给了就用它，否则按 Δ 预算反解
      （`choose_thin`，2026-10「Δ ≤ 25ms」，**已作废但保留**供 A/B 与回归）
    - `skew_max_ms`：预算（ms）。`<= 0` = 关掉预算，退回固定两档 15°/30°
    - `presses`：与 `targets_ms` **平行**的**押数**列表（`1 + 同时有音的 dp 轨数`，
      见 `press_of`）。省略 ⇒ 全按双押（**老路径逐字节不变**）
    - `three_press=False`：**关掉三押**（一律按双押拆 ⇒ 与今天逐字节相同）
    - `skip_press=True`（用户 2026-10：「为三押添加开关，**可以不一定生成双押**」）：
      押数 ≥ 3 的那一组**连双押也不插**（整组跳过，记账 `press_skipped`）——
      与 `three_press=False` 的区别：后者仍按双押插一格，前者那一格**根本不出现**
    - `mode=reverse`（默认）：首选反向写法；薄格撞 SetSpeed 时自动退正常写法
    - `mode=normal`：一律正常写法
    - **不拆硬保护格**（模板 / 引擎 / 雪花 / 开局站位 / 尾层）；
      **默认可拆软保护格**（自然闭合段的填充格，见 `_soft_owned`）——
      这是 `docs/31` §5.1 的「b」：**双押不许静默丢**
    - **带 SetSpeed 的格照拆**（★ 2026-10 用户完整规则）：「（当双押使用变速）调速必须
      **不和旋转重叠**，必须位于**双押的第一格**」⇒ 拆完 SetSpeed 正好落在**第一块薄格**上，
      而这一格**不许叠 Twirl**（用正常写法）。只有「SetSpeed **且** 自带 Twirl」才让位。
    - `snap_ms > 0`：原格不能拆时，在 ±`window` 格内找一个**音头时刻差
      ≤ `snap_ms`** 的可拆格顶上（换位）；默认 0 = 只认原格，绝不挪时间
    - 拆完剩余 `travel − Σθ` 必须 ≥ `max(MIN_TRAVEL, travel_min)`（**最后一块吃余量**）
    - 带 SetSpeed 的格不拆（「调速要放在平格上」）—— 三押**整格**一起不拆
    - 同一格多个目标只取第一个（其余计入 `report["dup"]`）

    ★ 押数（`docs/48` §3）：`3` ⇒ 三押，拆 **2** 块薄格 + 1 块余量；
    `≥ 4` ⇒ 四押/5押 **不做**，**降级成双押**（1 块）并记账 `extra_press`（不许静默）；
    `use_fixed=False`（老口径没有押数概念）⇒ 三押**不做**，记账 `three_legacy`。

    `report` 里的账（**一条都不许省**，状态栏要照抄）：
        hits / lost            成 / 丢
        moved / moved_ms_max   换过位的个数 / 最大挪动毫秒
        owned                  被硬保护挡住（模板 / 引擎 / 雪花 / 站位 / 尾层）
        setspeed               因「调速 + 自带 Twirl 无处安放」让位（★ 已不再是主因）
        setspeed_used          ★ **调速落在双押第一格**的点数（2026-10 用户规则，赚回来的）
        dropped                剩余 travel 不够 / 被显式排除
        twirl_busy             原格自带 Twirl 而被改成反向写法的个数
        fallback_normal        θ < 15° 时反向写法会写出 >345° 的原始 travel，已退正常写法
        dup                    同一格抢两次
        out_of_range           目标落在开局/尾层外面
        soft_used              借用软保护（自然段）格的个数
        reserved_used          落在求解前预留槽位上的个数（`dp_reserve`，a）
        theta_min / theta_max  本次用到的薄角范围
        skew_max/avg_ms        Δ 的最大 / 平均值（ms）
        skew_over              **Δ 超预算的个数**（局部 bpm 太低，连 1° 都压不住）
    """
    from .solve import times_from_chart
    n = len(ch.floors)
    if n < 3:
        return {}
    # ★ 押数：与 `targets_ms` 平行的列表（缺省 ⇒ 全 1 = 老路径）
    _pn = list(presses) if presses is not None else []
    tg = sorted((float(T), int(_pn[i]) if i < len(_pn) else 1)
                for i, T in enumerate(targets_ms))
    _tv = [T for T, _np in tg]
    t = times_from_chart(ch)
    fixed = float(theta) if theta is not None else None
    limit = max(MIN_TRAVEL, float(travel_min or 0.0))
    budget = float(skew_max_ms or 0.0)
    excl = set(exclude or ())
    snap = max(0.0, float(snap_ms or 0.0))
    # ★★ 「连续双押段长」——只有**固定角度表**下才用（`≥840 且 run≥4 ⇒ 回 30°`）。
    #   先用目标时刻估一次「落在第几格」，相邻（k 差 1）的连成一串，再从**段尾**回填，
    #   这样段内每个目标都知道整段有多长。
    runs: list[int] = [1] * len(tg)
    if use_fixed and tg:
        _k0 = [bisect.bisect_right(t, T) - 1 for T in _tv]
        for i in range(1, len(tg)):
            runs[i] = runs[i - 1] + 1 if _k0[i] == _k0[i - 1] + 1 else 1
        for i in range(len(tg) - 2, -1, -1):
            if _k0[i + 1] == _k0[i] + 1:
                runs[i] = runs[i + 1]

    out: dict[int, tuple[tuple, str]] = {}
    dropped = owned = notime = dup = 0
    moved = setspeed = soft_used = twirl_moved = reserved_used = 0
    fallback_normal = 0
    moved_ms_max = 0.0
    skews: list[float] = []
    skew_over = 0
    theta_cnt: dict = {}
    # ★ 押数账（`docs/48` §5：**四押不做但必须报**）
    triple_used = extra_press = three_legacy = 0
    # ★ 2026-10 用户开关：「三押」三档里的第 3 档 = 整组跳过（连双押也不插）
    three_off = press_skipped = 0
    # ★ 调速账（2026-10 用户完整规则）：
    #   `setspeed_used`  = **调速落在双押第一格** 的点数（这条规则要求的就是它）
    #   `setspeed`       = 「调速 + 自带 Twirl」仍无处安放而让位的点数（另记账，不静默）
    setspeed_used = 0

    for _ti, (T, n_press) in enumerate(tg):
        # ★ 薄块数 = 押数 − 1（押数 2 = 双押 ⇒ 1 块；押数 3 = 三押 ⇒ 2 块）
        n_blk = max(1, int(n_press) - 1)
        degrade = False
        # ★★ 2026-10 用户第 3 档：**跳过**（押数 ≥ 3 的那一组连双押也不插，整组丢掉）。
        #   必须排在「不拆」之前 —— 否则先被 `n_blk = 1` 降级成双押，就变成第 2 档了。
        if skip_press and int(n_press) >= 3:
            press_skipped += 1
            continue
        if not use_fixed:
            # 老口径（没有押数概念）⇒ 一律按双押拆（见 §6.3），单独记账
            if n_blk >= 2:
                three_legacy += 1
            n_blk = 1
        elif not three_press:
            # 用户把「三押」开关打到「不拆」⇒ 照双押插一格，单独记账
            if n_blk >= 2:
                three_off += 1
            n_blk = 1
        elif n_blk >= 3:
            # 四押 / 5押：**不做** ⇒ 降级成双押，另记账（不许静默）
            degrade = True
            n_blk = 1

        k0 = bisect.bisect_right(t, T) - 1
        if k0 < 1 or k0 > n - 2:
            notime += 1
            continue
        if k0 in excl:
            dropped += 1
            continue

        pick = None
        why = ""
        for k, d in _candidates(k0, window):
            if k < 1 or k > n - 2 or k in excl:
                continue
            if _hard_owned(ch.floors, k) or \
                    (not allow_soft and _soft_owned(ch.floors, k)):
                why = why or ("owned" if d == 0 else why)
                continue
            if k in out:
                why = why or ("dup" if d == 0 else why)
                continue
            # ★★ 2026-10 用户给完整规则后的**改变**：
            #   「（当双押使用变速）调速必须**不和旋转重叠**，必须位于**双押的第一格**」
            #   ⇒ 带 SetSpeed 的格**不再一律让位**！照拆，只是**这一格不许叠 Twirl**
            #   （退正常写法）—— 拆完 SetSpeed 自然落在**第一块薄格**上，正好满足规则。
            #   ★ 只有「SetSpeed **且** 自带 Twirl」才真的无处安放 ⇒ 才让位（另记账）。
            #   （以前把它读成「调速必须落在 180 平格上」这种**全局**规矩，白丢了一堆点：
            #     实测 FallenEra/Automaton_Waltz/MemoryLocked 三个样本
            #     `setspeed` 让位 12/52/4 处，**其中带 Twirl 的 0 处**。）
            if _setspeed_here(ch.floors, k) and \
                    bool(getattr(ch.floors[k], "twirl", False)):
                why = why or ("setspeed" if d == 0 else why)
                continue
            # ★ 薄角按**这一格自己的 bpm** 算（慢速档上 Δ 会被放大 k 倍）
            if use_fixed:
                # ★★ 规定写法表：**不自定义角度、不按毫秒反解**（用户 2026-10 定死）
                ths = fixed_theta_list(float(ch.floors[k].bpm), n_blk, runs[_ti])
            else:
                ths = (fixed if fixed is not None else choose_thin(
                    float(ch.floors[k].bpm), budget),)
            if ch.floors[k].travel - sum(ths) < limit - 1e-9:
                why = why or ("travel_short" if d == 0 else why)
                continue
            err = abs(float(t[k]) - T) if k < len(t) else float("inf")
            if d and err > snap:
                continue
            pick = (k, d, err, ths)
            break

        if pick is None:
            # 分类记账：优先报「原格为什么不行」，用户才知道该改哪里
            why = why or "dropped"
            if why == "owned":
                owned += 1
            elif why == "setspeed":
                setspeed += 1
            elif why == "dup":
                dup += 1
            else:
                dropped += 1
            continue

        k, d, err, ths = pick
        thr = float(ths[0])
        # ★★ 「SetSpeed 落在双押第一格」（2026-10 用户规则）：
        #   拆完之后 SetSpeed 事件自动落在**第一块薄格**上（bpm 与上一格不同 ⇒ 就是它），
        #   而规则的另一半「**不和旋转重叠**」⇒ 这一格**不许有 Twirl** ⇒ 一律正常写法。
        sp_here = _setspeed_here(ch.floors, k)
        # ★ 原格自带 Twirl（solve 排的）：把 Twirl 搬到**薄格**上即可 ——
        #   奇偶照样翻一次、有效 travel 照样是 [θ…, 余量]、总时长不变，
        #   所以不必因此丢音（`twirl_busy` 从「丢弃原因」降级成「改成反向写法」）。
        #   ★ 三押：Twirl **只放第一块薄格**（与双押一致，`docs/48` §6.3 / §8-4）；
        #   放两块会把 parity 翻回来 ⇒ 变成另一种几何。
        need_tw = bool(getattr(ch.floors[k], "twirl", False))
        if sp_here:
            enc = MODE_NORMAL              # ★ 调速格：不叠 Twirl（规则要求）
            setspeed_used += 1
        else:
            enc = MODE_REVERSE if (mode == MODE_REVERSE or need_tw) else MODE_NORMAL
            # ★ θ < 15° 时反向写法的**原始** travel（360−θ）会 > 345°，落在项目
            #   自己的「回头方块」区间里 ⇒ 退回正常写法（见 REVERSE_MIN_THETA）。
            if enc == MODE_REVERSE and thr < REVERSE_MIN_THETA - 1e-9 and not need_tw:
                enc = MODE_NORMAL
                fallback_normal += 1
        out[k] = (tuple(float(x) for x in ths), enc)
        if degrade:
            extra_press += 1
        elif len(ths) >= 2:
            triple_used += 1
        if need_tw:
            twirl_moved += 1
        if d:
            moved += 1
            moved_ms_max = max(moved_ms_max, err)
        if _soft_owned(ch.floors, k):
            soft_used += 1
        if _reserved(ch.floors, k):
            reserved_used += 1
        # Δ 预算体检：**只在「没有用固定表」且没给自定义 θ」时**才算预算超没超
        # （固定表下 Δ 只是**情报**，用户 2026-10 定死「不按毫秒计算」）
        sk = skew_ms(thr, float(ch.floors[k].bpm))
        skews.append(sk)
        for _x in ths:                     # ★ 三押：**每一块薄格**都记一笔 θ
            theta_cnt[float(_x)] = theta_cnt.get(float(_x), 0) + 1
        if (not use_fixed) and budget > 0 and fixed is None and sk > budget + 1e-9:
            skew_over += 1

    if report is not None:
        report.update(hits=len(out), lost=max(0, len(set(tg)) - len(out)),
                      dropped=dropped, owned=owned, out_of_range=notime,
                      dup=dup, twirl_busy=twirl_moved, moved=moved,
                      moved_ms_max=moved_ms_max, setspeed=setspeed,
                      # ★ 调速账：落在双押第一格的（赚回来的）/ 仍让位的
                      setspeed_used=setspeed_used,
                      soft_used=soft_used, reserved_used=reserved_used,
                      fallback_normal=fallback_normal,
                      # ★ 押数账（三押 / 跳过的四押 / 老口径未拆 / 开关关掉 / 整组跳过）
                      triple_used=triple_used, extra_press=extra_press,
                      three_legacy=three_legacy, three_off=three_off,
                      press_skipped=press_skipped,
                      theta_src=("fixed" if use_fixed else "budget"),
                      theta_counts={float(k): int(v)
                                    for k, v in sorted(theta_cnt.items())},
                      theta_min=min((min(v[0]) for v in out.values()), default=0.0),
                      theta_max=max((max(v[0]) for v in out.values()), default=0.0),
                      skew_max_ms=max(skews, default=0.0),
                      skew_avg_ms=(sum(skews) / len(skews)) if skews else 0.0,
                      skew_over=skew_over)
    return out


def apply(ch: Chart, insert_plan: dict[int, tuple], *,
          planar_radius: float = 1.0,
          skew_max_ms: float = SKEW_MAX_MS) -> int:
    """就地拆分。返回插入的**薄格数**。

    `insert_plan` 的值是 `(θ 序列, 编码)`（`docs/48` §6.2 第 5 步）：

        双押 ⇒ `((30.0,), "reverse")`          ⇒ 2 块：`[θ, 余量]`
        三押 ⇒ `((30.0, 60.0), "reverse")`     ⇒ 3 块：`[θ₁, θ₂, 余量]`

    ★ 薄块**连着**写、最后一块吃余量 ⇒ `Σtravel` 不变 ⇒ **零时长偏移**。
    ★ 反向写法的 Twirl **只放第一块薄格**（`docs/48` §6.3 / §8-4）—— 放两块会把
      parity 翻回来，变成另一种几何。

    `skew_max_ms` 只写进 `ch.meta`，供 `stats()` 做 Δ 预算体检（不改几何）。

    回填 `ch.meta`：
        `dp_kind`        = "angle"
        `dp_pairs`       = [(薄格下标, 薄格下标, 剩余格下标), ...]（**每块薄格一条**；
                           三押 ⇒ 同一格 2 条，剩余格下标相同 ⇒ 老消费方零改动）
        `dp_old2new`     = {原格下标: **第一块**薄格新下标}（原 onset 压在薄格上）
        `dp_base_travel` = {剩余格新下标: 原 travel}（统计时还原）
        `dp_theta`       = {薄格下标: θ}
        `dp_enc`         = {薄格下标: "reverse"/"normal"}
        `dp_skew_max_ms` = 本次的 Δ 预算（ms）
    """
    ch.meta["dp_skew_max_ms"] = float(skew_max_ms or 0.0)
    from .path import Path

    plan_ = {k: v for k, v in (insert_plan or {}).items() if v}
    if not plan_:
        ch.meta["dp_kind"] = "angle"
        ch.meta["dp_pairs"] = []
        ch.meta["dp_old2new"] = {i: i for i in range(len(ch.floors))}
        ch.meta["dp_base_travel"] = {}
        ch.meta["dp_theta"] = {}
        ch.meta["dp_enc"] = {}
        return 0

    floors = ch.floors
    n = len(floors)
    new: list[Floor] = []
    old2new: dict[int, int] = {}
    pairs: list[tuple[int, int, int]] = []
    base_travel: dict[int, float] = {}
    theta_of: dict[int, float] = {}
    enc_of: dict[int, str] = {}
    n_thin = 0

    for i in range(n):
        f0 = floors[i]
        item = plan_.get(i)
        if not item:
            old2new[i] = len(new)
            new.append(replace(f0))
            continue
        ths, enc = item
        ths = tuple(float(x) for x in ths) or (float(MIN_TRAVEL),)
        rest = float(f0.travel) - sum(ths)
        tw = (enc == MODE_REVERSE)       # 反向写法：**第一块**薄格放 Twirl（写补角）

        first = None
        idx_thin: list[int] = []
        for j, th in enumerate(ths):
            it = len(new)
            new.append(Floor(travel=float(th), bpm=f0.bpm,
                             twirl=bool(tw and j == 0),
                             turn=0.0, heading=0.0, angle=0.0,
                             speed_k=f0.speed_k, pause_beats=0.0,
                             snowflake=False, snowflake_id=-1,
                             angle_offset=0.0))
            theta_of[it] = float(th)
            enc_of[it] = enc
            n_thin += 1
            idx_thin.append(it)
            if first is None:
                first = it

        i_rest = len(new)
        new.append(replace(f0, travel=rest, twirl=False))
        old2new[i] = first               # 原 onset 压在**第一块**薄格上
        base_travel[i_rest] = float(f0.travel)
        for it in idx_thin:
            pairs.append((it, it, i_rest))

    # ★ 几何统一重算（按 raw_travel + parity）——保证 angleData 自洽
    flips = [bool(getattr(f, "twirl", False)) for f in new]
    path = Path.from_floors(new, flips, radius=planar_radius, allow_twirl=True)
    path.commit_to(new, write_twirl=True)

    ch.floors = new
    ch.meta["dp_kind"] = "angle"
    ch.meta["dp_pairs"] = pairs
    ch.meta["dp_old2new"] = old2new
    ch.meta["dp_base_travel"] = base_travel
    ch.meta["dp_theta"] = theta_of
    ch.meta["dp_enc"] = enc_of

    pt = ch.meta.get("pos_tracks")
    if pt:
        ch.meta["pos_tracks"] = [(old2new.get(int(fi), fi), dx, dy)
                                 for fi, dx, dy in pt]
    return n_thin


def stats(ch: Chart) -> dict:
    """自检：薄格数 / 编码分布 / 剩余格 travel / 与原格的时序关系 / **Δ 预算体检**。

    ★ 薄格的 travel **按机制豁免 `MIN_TRAVEL`** —— 它就是要小（低 bpm 下必须
      小到能把 Δ 压进预算），所以 `bad_travel` 只检查**剩余格**（按剩余格**去重**，
      否则三押的那一格会被重复卡两次）。
    ★ `press_*` = **押数分布**（`docs/48`）：按「同一剩余格绑了几块薄格」还原 ——
      `1 块 ⇒ 押数 2（双押）`，`2 块 ⇒ 押数 3（三押）`。
    """
    from .solve import times_from_chart
    t = times_from_chart(ch)
    th = ch.meta.get("dp_theta") or {}
    enc = ch.meta.get("dp_enc") or {}
    pairs = ch.meta.get("dp_pairs") or []
    bad = 0
    sep: list[float] = []
    gap: list[float] = []          # ★ 相邻薄块的**块时长**（= 相邻两下的间隔）
    skews: list[float] = []
    budget = float(ch.meta.get("dp_skew_max_ms") or 0.0)
    over = 0
    by_rest: dict[int, list[int]] = {}
    checked_rest: set[int] = set()
    for (ix, _iy, ib) in pairs:
        if ix >= len(ch.floors) or ib >= len(ch.floors):
            continue
        by_rest.setdefault(ib, []).append(ix)
        if ib not in checked_rest:                        # 只卡**剩余格**（去重）
            checked_rest.add(ib)
            if ch.floors[ib].travel < MIN_TRAVEL - 1e-9:
                bad += 1
        if ib < len(t) and ix < len(t):
            sep.append(t[ib] - t[ix])
        if ix + 1 < len(t):
            gap.append(t[ix + 1] - t[ix])                 # 本薄块自己的时长
        sk = skew_ms(th.get(ix, 0.0), float(ch.floors[ix].bpm))
        skews.append(sk)
        if budget > 0 and sk > budget + 1e-9:
            over += 1
    press = [len(v) + 1 for v in by_rest.values()]        # 押数 = 薄块数 + 1
    hist: dict = {}
    for p in press:
        hist[int(p)] = hist.get(int(p), 0) + 1
    return dict(kind="angle", thin=len(th),
                reverse=sum(1 for v in enc.values() if v == MODE_REVERSE),
                normal=sum(1 for v in enc.values() if v == MODE_NORMAL),
                pairs=len(pairs), bad_travel=bad,
                sep_ms_min=min(sep) if sep else 0.0,
                sep_ms_max=max(sep) if sep else 0.0,
                gap_ms_min=min(gap) if gap else 0.0,
                gap_ms_max=max(gap) if gap else 0.0,
                triple=sum(1 for p in press if p == 3),
                press_hist=hist,
                skew_ms_max=max(skews, default=0.0),
                skew_ms_avg=(sum(skews) / len(skews)) if skews else 0.0,
                skew_budget_ms=budget, skew_over=over,
                floors=len(ch.floors))
