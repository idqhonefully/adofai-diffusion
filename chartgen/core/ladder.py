# -*- coding: utf-8 -*-
"""对音阶梯 —— `docs/25` 的实施（`SolveParams.aggressive_pick`）。

**这是与老路径并列的第二条落点决策路径，不是替换。**
`aggressive_pick=False` 时本模块一行都不会被执行（`solve()` 直接走老路径）。

用户口径（`docs/25` §0）：

    1 需要对音时，先找能不能用已有图形定义
    2 找不到时 从当前旋转角度的180°范围内，寻找一个能对上音的角度
    3 再找不到时 添加旋转事件，从新的180°寻找能对上音的角度
    4 再找不到时 先变速，然后1 2 3
    5 在找不到时 使用暂停节拍
    但是，如果4产生的角度 低于最小角度 使用5

—— 一句话：**逐音降级**，前一级全失败才允许动下一级。

## 动手前必须认账的数学（`docs/25` §1.2，已用 tools/_ladder_verify.py 验过）

    travel = 180·r/k              被 (r, k) 唯一决定 —— 换档才有新时长
    Twirl(parity) **不改变时长**，只把写法在 +(180−T) / −(180−T) 之间切换
    所以：第 2/3 级扩的是**几何候选**，第 4/5 级扩的才是**时间候选**

## 级的硬序（`docs/25` §4）

**级号小的永远优先**。评分只在同一级内部比。别把级号和评分塞进同一个 tuple ——
否则第 4 级的「直线」会盖掉第 2 级的「非直线」，阶梯就白写了。

## 定稿的口径（`docs/25` §9.0，用户 2026-10 拍板）

| # | 定稿 |
|---|---|
| Q1 | 「当前旋转角度」= 当前朝向 `heading`（只影响措辞） |
| Q2 | 「180° 范围」= **内圈那一半（T ≤ 180）** |
| Q3 | 第 4 级的补丁 = **短路**：`T < travel_min` 立刻进第 5 级，不再试别的档 |
| Q4 | 变速后重跑图形 = **只重跑当前格** |
| Q5 | 老 DP 保留，降级为「第 4 级的档位建议器」（本模块用 `ctx.ks` 喂 level 1） |
| Q6 | `twirl_mode` 只在第 3 级内部生效；**激进路径不做后置 `_plan_twirls`** |
| Q7 | 雪花算第 1 级的图形（由 `solve_aggressive` 以「锁定段」注入） |
| Q8 | 最小角度对**所有级**生效；**豁免只有 双押、雪花** |
| Q9 | 先纯贪心，不做回看 / 回退 |
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

#: 与 `core.solve` 保持一致（不 import 以避免循环依赖）
PAUSE_MIN_BEATS = 0.25
FIRST_TRAVEL = 180.0

#: ★ 用户 2026-10：**速度偏低时优先绕外圈，速度较快时优先绕内圈**，分界 = cbpm 400。
#: 「绕外圈」= `T > 180`（行星多绕一圈），慢速时观感更舒展；快速时外圈会糊成一片。
LADDER_OUTER_CBPM = 400.0


def outer_pref(p, base_bpm: float) -> bool:
    """当前这首谱应该**优先绕外圈**吗？（`docs/25` §7 的 `ladder_outer_mode`）

        auto  （默认）：cbpm < `ladder_outer_cbpm`（默认 400）→ 优先外圈
        always        ：总是优先外圈
        never         ：总是优先内圈

    注意它是**偏好**而不是禁止：第 4 级先按偏好试一遍，都不行再放开外圈兜一次
    （见 `plan()`），所以不会因为偏好把整格逼进第 5 级。
    """
    mode = str(getattr(p, "ladder_outer_mode", "auto") or "auto").strip().lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    thr = float(getattr(p, "ladder_outer_cbpm", LADDER_OUTER_CBPM) or LADDER_OUTER_CBPM)
    return float(base_bpm) < thr


def _half_rank(t: float, prefer_outer: bool) -> int:
    """§2 第 4 级 ④ 的 tie-break：优先落在**偏好**的那一半（0 好 1 差）。"""
    inner = t <= 180.0 + 1e-9
    return 0 if (inner != bool(prefer_outer)) else 1


# ---------------------------------------------------------------------------
# 判定基元
# ---------------------------------------------------------------------------
@dataclass
class Decision:
    """一格（或一段）的落点决策。"""
    kind: str                 # figure / single / twirl / retier / pause
    travel: float
    k: float
    twirl: bool
    pause_beats: float = 0.0
    span: int = 1             # 这一段覆盖几格（level 1 的图形 > 1）
    src: str = ""             # 图形来源：tpl / nat / eng / snow / 空

    @property
    def bpm_k(self) -> float:
        return float(self.k)


def travel_of(r: float, k: float) -> float:
    """`docs/25` §1.2(A)：travel 被 (r, k) 唯一决定。"""
    return 180.0 * float(r) / float(k)


def is_straight(t: float, tol: float = 1e-6) -> bool:
    return abs(t - 180.0) <= tol


def travel_hi(p) -> float:
    """本格允许的 travel **上界**（度）。

    ★★ 2026-10 修的 bug：这里以前**写死 345**，`p.travel_max`（UI「最大夹角」，
    用户口径「最大夹角不能超过 270°」）**整个阶梯一次都没看过** ——
    实测（`out/_grin/grin.mid`，`travel_max=270`）阶梯照吐 `338.25 / 331.5 / 345.0`
    共 10 格，而且 `solve_aggressive` 连 `meta["travel_max"]` 都没写
    ⇒ `core.rules` 的 ③c 上界检查**也看不见**（`tmax=0` 当没设界）
    ⇒ 「违规 0」却满是超窗的格子。老 DP 路径（`_dp_tiers(..., travel_max=)`）
    与直拟合（`fitdirect._ladder_j_range`）都认它，只有这条阶梯漏了。
    """
    hi = 345.0
    tmax = float(getattr(p, "travel_max", 0.0) or 0.0)
    if tmax > 0:
        hi = min(hi, tmax)
    # 上界低于硬下限时按硬下限走：否则**所有候选全被判非法**，
    # 整首曲子会一路掉进第 5 级（Pause），比「忽略这个荒唐值」更坏。
    return max(15.0, hi)


def rung_ok(t: float, p, *, exempt: bool = False) -> bool:
    """第 2/3/4 级共用的放行闸（`docs/25` §2）。

    · `15 ≤ T ≤ min(345, travel_max)` —— 不是回头方块（游戏对 angleMoved≈0/2π
      特判成 2 拍），也不许超过用户口径的**最大夹角**
    · `T ≥ travel_min` —— **Q8：所有级都卡**；`exempt` 给双押 / 雪花开口子
      ★ 注意 `exempt` **只豁免「最小角度」**：上界是用户的硬口径，
        而且 `core.rules` 的 ③c 只对双押格开口子（雪花段照查），
        所以这里必须照样卡住上界，否则又会变成「checker 报违规」。
    """
    if not (15.0 - 1e-9 <= t <= travel_hi(p) + 1e-9):
        return False
    if exempt:
        return True
    return t >= float(getattr(p, "travel_min", 0.0)) - 1e-9


def tier_order(r: float, p, k_prev: float, prefer_outer: bool = False) -> list[float]:
    """第 4 级的档位尝试顺序（`docs/25` §2 第 4 级 ①~④）—— **必须钉死**。

        ① 先试能给出直线（T ≡ 180）的最小 |log2 k| 档
        ② 再按 |log2(k / k_prev)| 从小到大（越少换档越好）
        ③ 同 |log2| 时优先 k > k_prev（提速档：把长音压回合法区间）
        ④ 同分时优先落在**偏好**的那一半（内圈 / 外圈，见 `outer_pref`）

    返回的列表**不含** `k_prev`（那已经在第 2/3 级试过了）。
    `p.ladder_tier_order` 可以改变主序：`switch` = ②做主序，`inner` = ④做主序。
    """
    mode = str(getattr(p, "ladder_tier_order", "straight") or "straight")
    tiers = [float(k) for k in p.speed_tiers if abs(float(k) - k_prev) > 1e-12]

    def key(k: float):
        t = travel_of(r, k)
        straight = 0 if is_straight(t) else 1
        dlog = abs(math.log2(k / k_prev)) if k_prev > 0 else 0.0
        faster = 0 if k > k_prev else 1
        half = _half_rank(t, prefer_outer)
        if mode == "switch":
            return (dlog, straight, faster, half)
        if mode == "inner":
            return (half, straight, dlog, faster)
        return (straight, dlog, faster, half)           # 默认：保直线优先

    return sorted(tiers, key=key)


def try_single(r: float, k: float, *, flip: bool, p,
               allow_outer: bool, exempt: bool = False) -> Decision | None:
    """第 2 级（`flip=False`）/ 第 3 级（`flip=True`）的单格判定。

    `flip=True` 就是「加一个旋转事件」—— 按 `docs/25` §1.2(B/D)，
    它**不改时长**，只把出射朝向关于当前朝向镜像（写法从 `+(180−T)` 换成 `−(180−T)`）。
    """
    if flip and not (getattr(p, "allow_twirl", False) and getattr(p, "emit_twirl", False)):
        return None                                     # 用户明确关掉 Twirl 时第 3 级整级失效
    t = travel_of(r, k)
    if not rung_ok(t, p, exempt=exempt):
        return None
    if not allow_outer and t > 180.0 + 1e-9:
        return None                                     # Q2：180° 范围 = 内圈那一半
    return Decision("twirl" if flip else "single", t, float(k), bool(flip))


def pause_decision(r: float, k_prev: float) -> Decision:
    """第 5 级：暂停节拍（兜底）。规则与老路径**完全一致**（`docs/24` §2）。

        travel = 180（直线，1 拍）
        pause = r/k' − 1        k' = 沿用前一格的档
        若 r/k' − 1 < PAUSE_MIN_BEATS ⇒ 退回 k' = 1，pause = r − 1

    `PAUSE_MIN_BEATS` 那条短路是老路径踩过的坑：不退回 k=1 的话
    `pb = r/k − 1` 会被吃成 0，等待就**悄悄变回「缓速爬过去」**了。
    """
    k = float(k_prev)
    if not (k <= r + 1e-9 and r / k - 1.0 >= PAUSE_MIN_BEATS):
        k = 1.0
    return Decision("pause", FIRST_TRAVEL, k, False,
                    pause_beats=max(0.0, r / k - 1.0))


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
@dataclass
class Ctx:
    """阶梯跑一遍需要的全部输入。由 `core.solve._solve_aggressive` 组装。"""
    rs: list[float]
    p: object
    ks: list[float]                 # 老 DP 的档位建议（Q5：只当 level 1 的输入）
    tail_k: float = 1.0
    prefer_travel: float = 120.0
    blocked: set[int] = field(default_factory=set)      # 长休止屏障（模板/图形不许跨）
    locked: dict = field(default_factory=dict)          # onset → Decision（模板 / 雪花）
    locked_len: dict = field(default_factory=dict)      # onset → 这一段几格
    exempt: set[int] = field(default_factory=set)       # Q8 豁免格（双押 / 雪花）
    walk: object = None                                 # core.figures.Walk
    allow_figures: bool = True
    #: 谱面基准 BPM —— 决定「优先绕外圈还是内圈」（`outer_pref`）
    base_bpm: float = 0.0
    #: 显式覆盖绕圈偏好；`None` = 按 `outer_pref(p, base_bpm)` 自动算
    prefer_outer: bool | None = None


@dataclass
class Plan:
    decisions: list[Decision]
    rungs: list[int]                # 每一格是第几级答的（0 = 没答，不该出现）
    stats: dict = field(default_factory=dict)


def _emit(decisions, rungs, i: int, d: Decision, rung: int) -> None:
    decisions[i] = d
    rungs[i] = rung


def plan(ctx: Ctx) -> Plan:
    """逐音降级，返回每一格的决策。

    ⚠ **纯贪心**（Q9-A）：不回看、不回退。每一格的决定只依赖此刻的
    `k_prev` 与 Walk 状态；这也是「级是硬序」能成立的前提。
    """
    from . import figures as _fig

    rs, p = ctx.rs, ctx.p
    n = len(rs)
    decisions: list[Decision] = [None] * n           # type: ignore[list-item]
    rungs = [0] * n
    k_prev = 1.0                                     # 第 0 层是开局站位，档 = 1
    # ★ 用户 2026-10：速度偏低优先绕外圈（cbpm < 400），速度较快优先绕内圈。
    po = (ctx.prefer_outer if ctx.prefer_outer is not None
          else outer_pref(p, ctx.base_bpm))

    i = 0
    while i < n:
        # ── 第 1 级（锁定段）：模板 / 雪花 —— 整段照抄，A/B 的基准 -----------------
        if i in ctx.locked:
            L = int(ctx.locked_len.get(i, 1) or 1)
            for j in range(L):
                if i + j >= n:
                    break
                d = _locked_at(ctx, i, j)
                _emit(decisions, rungs, i + j, d, 1)
                if ctx.walk is not None:
                    ctx.walk.step(d.travel, d.twirl)
            k_prev = _last_k(ctx, i, L) or k_prev
            i += L
            continue

        # ── 第 1 级（图形）：自然闭合段 / 三连音引擎 -------------------------------
        # Q4-A：这里只对**当前格**重跑，不向前看、不回退。
        fig = None
        if ctx.allow_figures and ctx.walk is not None:
            # ★ 原地惩罚的对照项（DP 候选）在阶梯里**关掉**（`dp_mode="off"`）：
            #   实测（三个样本）它在第 1 级几乎每次都赢 ⇒ 阶梯 100% 退化成
            #   `_dp_tiers` 那条路，`aggressive_pick` 这个开关等于失效。
            #   阶梯是你 2026-10 亲手定的**纯贪心硬序**（`docs/25` §9.0 Q9：
            #   「先纯贪心，不做回看/回退」），原地惩罚只作用在**默认路径**
            #   （`core/solve.py` 走 `dp_mode="none"`：dp 候选赢了就交回 DP）。
            fig = _fig.choose(i, rs, ctx.ks, p, ctx.walk, blocked=ctx.blocked,
                              tail_k=ctx.tail_k,
                              prefer_travel=ctx.prefer_travel, prev_k=k_prev,
                              dp_mode="off")
        if fig is not None:
            for j in range(fig.n):
                d = Decision("figure", float(fig.travels[j]), float(fig.ks[j]),
                             bool(fig.twirls[j]), 0.0, 1, str(fig.kind))
                _emit(decisions, rungs, i + j, d, 1)
                if ctx.walk is not None:
                    ctx.walk.step(d.travel, d.twirl)
            k_prev = float(fig.ks[-1])
            i += fig.n
            continue

        r = rs[i]
        exempt = i in ctx.exempt

        # ★ 长休止：按既有口径（`pause_min_beats`）**直接进第 5 级**。
        #   为什么必须在第 2/3/4 级**之前**拦：
        #   2 拍的休止不算「对不上音」，它在第 4 级会被一个 `T = 180` 的慢速档
        #   干净地「吸收」掉（`k=2`，直线、时长正确、无违规）—— 但那正是
        #   用户 item ① 明确否掉的「**缓速爬过去**」。实测不加这一步时
        #   Automaton 的 Pause 从 321 掉到 **0**、直线率也掉到 41%。
        #   所以长等待走暂停节拍，几何上仍是 `travel = 180` 的直线，只是
        #   多出来的时间由 Pause 补，而不是靠降速。
        if getattr(p, "use_pause", True) and r > float(p.pause_min_beats):
            d = pause_decision(r, k_prev)
            if d.pause_beats > 1e-9:
                _emit(decisions, rungs, i, d, 5)
                if ctx.walk is not None:
                    ctx.walk.step(d.travel, d.twirl)
                k_prev = d.k
                i += 1
                continue

        # ── 第 2 级：当前档、当前 parity ----------------------------------------
        d = try_single(r, k_prev, flip=False, p=p, allow_outer=po, exempt=exempt)
        if d is not None:
            _emit(decisions, rungs, i, d, 2)
            if ctx.walk is not None:
                ctx.walk.step(d.travel, d.twirl)
            i += 1
            continue

        # ── 第 3 级：加一个 Twirl，从镜像的那一半找 ------------------------------
        d = try_single(r, k_prev, flip=True, p=p, allow_outer=po, exempt=exempt)
        if d is not None:
            _emit(decisions, rungs, i, d, 3)
            if ctx.walk is not None:
                ctx.walk.step(d.travel, d.twirl)
            i += 1
            continue

        # ── 第 4 级：先变速，然后重跑 1 / 2 / 3 ---------------------------------
        got = None
        got_fig = None
        # ★ 绕圈偏好在这里**只影响排序**（`tier_order` 的 ④ tie-break），
        #   不做硬过滤 —— `T = 180`（直线）本来就落在内圈那一半，
        #   若拿「优先外圈」把它过滤掉，慢速谱的直线率会直接崩
        #   （实测 MemoryLocked 从 47.2% 掉到 **44.1%**，破了 45% 红线）。
        #   偏好是「同分时先选哪一半」，不是「只许哪一半」。
        for k in tier_order(r, p, k_prev, prefer_outer=po):
            t = travel_of(r, k)
            # ★ 用户补丁（Q3）：第 4 级**产生**的角度低于最小角度就不许用。
            #   ⚠ 实测校正（2026-10）：这里**不能 `break`**。
            #   `tier_order` 把「能给出直线」的档排在第一位，而 `T = 180·r/k`
            #   在 `r` 很小时（四分音符 r=0.25）这个首选档是 k=0.25，
            #   `T = 180` 完全合法；`break` 会在**别的候选之前**就短路掉，
            #   把这一格推给第 5 级，而第 5 级写 1 拍直线 + 0 暂停 ⇒
            #   实测 Automaton 第 9 层凭空多 **121.622ms**（时序直接 FAIL）。
            #   所以按「跳过这个档、继续试」实现（`continue`）：
            #   只有**所有档都给不出 ≥ travel_min 的角度**时才落第 5 级 ——
            #   这与「4 产生的角度低于最小角度就使用 5」等价，且不会自伤。
            #   ★ 上下界**两条**都由下面的 `rung_ok` 统一把关（上界 =
            #     `min(345, p.travel_max)`，见 `travel_hi`）；这里这一行只保留
            #     老写法（下界的 `continue` 语义与它完全一致，属于**冗余但无害**）。
            if t < float(getattr(p, "travel_min", 0.0)) - 1e-9 and not exempt:
                continue
            if not rung_ok(t, p, exempt=exempt):
                continue
            # 「然后 1」：用这个新档重扫一遍图形（只扫当前格 —— Q4-A）
            if ctx.allow_figures and ctx.walk is not None:
                ksf = [float(k)] * n
                # ★ 第 4 级**不加 dp 对照项**（`dp_mode` 保持默认 `"none"`）：
                #   这一级的语义是「先变速，然后只重跑当前格」，`ksf` 已经是
                #   一整条恒定 k；让它按 dp 候选**提交一整段**会绕过这一级的
                #   `rung_ok` / `travel_min` 逐档体检（那些检查在下面按 k 做）。
                #   dp 候选在这里赢了也只是 `None` = 「这一级没有图形」⇒ 安全。
                f2 = _fig.choose(i, rs, ksf, p, ctx.walk, blocked=ctx.blocked,
                                 tail_k=float(k),
                                 prefer_travel=ctx.prefer_travel, prev_k=k_prev)
                if f2 is not None:
                    got_fig = f2
                    got = Decision("figure", float(f2.travels[0]),
                                   float(f2.ks[0]), bool(f2.twirls[0]),
                                   0.0, int(f2.n), "retier:" + str(f2.kind))
                    break
            # 再跑 2 / 3（两半都允许；偏好已经体现在 `tier_order` 的顺序里）
            d = try_single(r, k, flip=False, p=p, allow_outer=True, exempt=exempt)
            if d is None:
                d = try_single(r, k, flip=True, p=p, allow_outer=True, exempt=exempt)
            if d is not None:
                d.kind = "retier"
                got = d
                break

        if got is not None and got_fig is not None:
            # 变速解锁的图形：整段照抄（span 由图形决定）
            span = min(int(got_fig.n), n - i)
            for j in range(span):
                dd = Decision("figure", float(got_fig.travels[j]),
                              float(got_fig.ks[j]), bool(got_fig.twirls[j]),
                              0.0, 1, "retier:" + str(got_fig.kind))
                _emit(decisions, rungs, i + j, dd, 4)
                if ctx.walk is not None:
                    ctx.walk.step(dd.travel, dd.twirl)
            k_prev = float(got_fig.ks[span - 1])
            i += span
            continue

        if got is not None:
            got.span = 1
            _emit(decisions, rungs, i, got, 4)
            if ctx.walk is not None:
                ctx.walk.step(got.travel, got.twirl)
            k_prev = got.k
            i += 1
            continue

        # ── 第 5 级：暂停节拍（兜底） ------------------------------------------
        # ⚠ 老路径里 `use_pause=False` 是**彻底不用** Pause；这里必须尊重它，
        #   否则关掉 Pause 的用户会被阶梯偷偷塞回来。
        #   关掉时退到「第 4 级里最接近的一个档」，还是不行就只能用 k_prev 硬画。
        if not getattr(p, "use_pause", True):
            d = _no_pause_fallback(r, p, k_prev, exempt=exempt)
        else:
            d = pause_decision(r, k_prev)
            if d.pause_beats <= 1e-9:
                # ★ 实测校正（2026-10）：`r < 1 拍`时根本没有「等待」可放。
                #   第 5 级的形状是「travel = 180（1 拍直线）+ Pause 补余下的时间」，
                #   而 `pause = r/k − 1` 在 `r < 1` 时恒为负 ⇒ 被 `PAUSE_MIN_BEATS`
                #   那条短路吃成 0 ⇒ 只剩一条 1 拍直线，这一格时长变成 `r` 的
                #   整数倍（实测 Automaton 第 9 层多 **121.622ms**，`verify_file` FAIL）。
                #   ⇒ 没有等待可放时，退回「不许暂停」的兜底（老老实实按 r 画）。
                d = _no_pause_fallback(r, p, k_prev, exempt=exempt)
        _emit(decisions, rungs, i, d, 5)
        if ctx.walk is not None:
            ctx.walk.step(d.travel, d.twirl)
        k_prev = d.k
        i += 1

    st = {
        "rung_counts": {q: sum(1 for x in rungs if x == q) for q in (1, 2, 3, 4, 5)},
        "n": n,
        "n_twirl_decided": sum(1 for d in decisions if d is not None and d.twirl),
    }
    return Plan(decisions, rungs, st)


def _no_pause_fallback(r: float, p, k_prev: float, *, exempt: bool) -> Decision:
    """`use_pause=False` 时的兜底：不许用 Pause，那就退到「最接近合法」的档。

    优先保留 `k_prev`（少换档），把 travel 硬画出来 —— 和老路径「关掉 Pause 就
    让行星减速爬过去」的选择一致（用户当时明确不要缓速，但那是开着 Pause 时；
    关掉 Pause 就是另一回事了）。
    """
    d = try_single(r, k_prev, flip=False, p=p, allow_outer=True, exempt=True)
    if d is not None:
        return d
    for k in tier_order(r, p, k_prev):
        d = try_single(r, k, flip=False, p=p, allow_outer=True, exempt=True)
        if d is not None:
            d.kind = "retier"
            return d
    # 真的没有合法档：用 k_prev 直接画（travel 可能越界，交给 rules 报出来）
    return Decision("retier", travel_of(r, k_prev), float(k_prev), False)


def _locked_at(ctx: Ctx, i0: int, j: int) -> Decision:
    """锁定段（模板 / 雪花）里第 j 格的决策。"""
    d = ctx.locked.get(i0 + j)
    if d is not None:
        return d
    head = ctx.locked.get(i0)
    if head is None:
        raise KeyError(f"锁定段缺决策: onset {i0}+{j}")
    return Decision("figure", float(head.travel), float(head.k),
                    bool(head.twirl), 0.0, 1, str(head.src))


def _last_k(ctx: Ctx, i0: int, L: int) -> float | None:
    for j in range(L - 1, -1, -1):
        d = ctx.locked.get(i0 + j)
        if d is not None:
            return float(d.k)
    return None


__all__ = ["Decision", "Ctx", "Plan", "plan", "tier_order", "try_single",
           "travel_of", "pause_decision", "rung_ok", "is_straight", "outer_pref",
           "solve_aggressive", "PAUSE_MIN_BEATS", "FIRST_TRAVEL",
           "LADDER_OUTER_CBPM"]


# ---------------------------------------------------------------------------
# 与 `core/solve.solve()` 的接驳：把决策变成 Chart
# ---------------------------------------------------------------------------
def solve_aggressive(onsets, p, prep: dict, ks: list[float], *, base_bpm: float):
    """激进路径的入口 —— 由 `core/solve.solve()` 在 `p.aggressive_pick` 时调用。

    `prep` 与 `ks` 都是 `solve()` 已经算好的（**两条路径共用同一套基准**，
    见 `solve._prepare_rhythm` 的说明）。`ks` 是**老 DP 的档位建议**，
    在本路径里只喂给第 1 级的图形扫描（`docs/25` §9.0 Q5）。

    ⚠ 本函数是 `solve()` 主体的一段**平行实现**：模板匹配 / 雪花扫描 / 建层 /
    写 meta 都照着老路径的对应段落重写了一遍。这是刻意的（`docs/25` §5.3.2：
    「两条路径的代码不许相互调用」），代价是这部分要**成对维护**。
    `tests/golden/off_hashes.json` 保证改动本函数**不影响**老路径。
    """
    from . import solve as S
    from . import figures as _fig

    rs = prep["rs"]
    rq = prep["rq"]
    n_on = prep["n_on"]
    mbpm = prep["mbpm"]
    tail_k = float(ks[len(ks) // 2]) if len(ks) else 1.0
    prefer_travel = 60.0 if base_bpm >= 1440.0 else 120.0

    ch = S.Chart(base_bpm=float(base_bpm))
    ch.base_bpm = float(base_bpm)

    # 激进路径自己的参数副本：
    #  · **不做后置 `_plan_twirls`**（Q6：Twirl 由第 3 级逐音决定）
    #  · **回正照跑**（2026-10 修）：以前这里写死 `p2.straighten = False`，
    #    理由是「全局回正会与逐音 Twirl 打架」。但结果是**激进路径下回正彻底死掉**
    #    （用户：「回正逻辑好像有一点死了」）。回正**只改 Twirl 符号、不动 travel**，
    #    时序零影响；它跑在 `_finish` 里、在模板/自然/引擎/雪花段之后，
    #    那些段仍然被 `_protected_floors` 钉死。所以放它跑，冲突交给 `min_gain` 兜。
    import copy as _copy
    p2 = _copy.copy(p)

    # ---------------- 长休止：先当屏障标出来（第 5 级会把它变成 Pause） -------------
    rests: set[int] = set()
    if getattr(p, "use_pause", True):
        for _i, _r in enumerate(rs):
            if _r > float(p.pause_min_beats):
                rests.add(_i)

    # ---------------- 第 1 级（锁定段 A）：手写节奏型模板 -------------------------
    # 与 `solve()` 的 2.5 段同口径，但只生产「锁定决策」，不参与逐音竞争。
    hit_at: dict = {}
    spans: list = []
    span_meta: list = []
    tpl_flips: list = []
    tpl_reps = 0
    if getattr(p, "use_templates", True) and n_on > 1:
        from .templates import load as _load_tpl, match as _match_tpl
        from .templates import match_dp as _match_tpl_dp
        from .templates import STAIR_PATH as _STAIR

        dp_tv = [180.0 * rs[i] / (ks[i] if i < len(ks) else tail_k)
                 for i in range(len(rs))]
        pass_of = [0] * len(rs)
        pass_ms: list[float] = []
        _i = 0
        _dts = [onsets[i + 1].t_ms - onsets[i].t_ms for i in range(n_on - 1)]
        while _i < len(_dts):
            _j = _i
            while _j + 1 < len(_dts) and _dts[_j] <= p.template_rest_ms:
                _j += 1
            pass_ms.append(sum(_dts[_i:_j + 1]))
            for _x in range(_i, _j + 1):
                pass_of[_x] = len(pass_ms) - 1
            _i = _j + 1

        def _accept(s, t, reps):
            L = t.n * reps
            if any((s + j) in rests for j in range(L)):
                return False
            if p.template_max_span_s > 0 and not S._tpl_is_triplet(t) and \
                    pass_ms[pass_of[s]] > p.template_max_span_s * 1000.0:
                return False
            if not p.template_only_nonstraight:
                return True
            return any(not is_straight(x) for x in dp_tv[s:s + L])

        _matcher = _match_tpl_dp if getattr(p, "template_dp", True) else _match_tpl
        # ★ 迭代 2.2：`stair_first` 把「折弯循环」变体作为**第一权重**加进来
        _tpls = _load_tpl(extra=_STAIR) if getattr(p, "stair_first", False) \
            else _load_tpl()
        for s, t, reps in _matcher(rq, _tpls, p.template_tol,
                                   timing=rs, beat_ms=60000.0 / base_bpm,
                                   timing_tol_ms=p.template_timing_tol_ms,
                                   accept=_accept):
            spans.append((s + 1, t))
            span_meta.append((s, t.n * reps))
            tpl_flips.append((s, t.n * reps, t))
            tpl_reps += reps
            for j in range(t.n * reps):
                hit_at[s + j] = (j % t.n, t)

    # ---------------- 第 1 级（锁定段 B）：魔法阵（雪花） -------------------------
    # Q7-A：雪花算第 1 级的图形，沿用 docs/24 §4 的全部门槛与**两遍式**落地逻辑
    # （真正的覆盖仍交给 `_finish` → `_apply_snowflakes`，避免重复实现那套时序修正）。
    snow_spans: list = []
    snow_tiles = 0
    if getattr(p, "use_snowflake", False) and n_on > 2:
        import random as _random
        from .snowflake import plan as _snow_plan, should_use as _snow_use
        from .snowflake import SHAPES_DETERMINISTIC, SHAPES as _ALL_SHAPES   # noqa: F401
        from .snowflake import MIN_ARMS as _MIN_ARMS
        _srng = _random.Random(int(p.snowflake_seed) if p.snowflake_seed else 20240213)
        tol = max(0.5, float(p.snowflake_uniform_tol_ms))
        _shape = str(getattr(p, "snowflake_shape", "auto") or "auto").strip().lower()
        if _shape in ("", "auto", "自动"):
            _shapes = None
        elif _shape == "random":
            _shapes = _ALL_SHAPES
        elif _shape in _ALL_SHAPES:
            _shapes = (_shape,)
        else:
            _shapes = None
        _det = not bool(getattr(p, "snowflake_random", False))
        _dts = [onsets[i + 1].t_ms - onsets[i].t_ms for i in range(n_on - 1)]
        _i = 0
        while _i < len(_dts):
            _j = _i
            while (_j + 1 < len(_dts) and abs(_dts[_j + 1] - _dts[_i]) <= tol
                   and (_j + 1) not in rests):
                _j += 1
            L = _j - _i + 1
            spec = (_snow_plan(
                L, n_choices=tuple(p.snowflake_n_rot),
                min_arms=int(getattr(p, "snowflake_min_arms", _MIN_ARMS) or _MIN_ARMS),
                rng=_srng,
                trials=int(getattr(p, "snowflake_trials", 32) or 32),
                compact=bool(getattr(p, "snowflake_compact", True)),
                deterministic=_det, shapes=_shapes,
                travel_min=float(p.travel_min),
            ) if L >= p.snowflake_min_tiles else None)
            if spec is not None and _snow_use(L, p.snowflake_min_tiles,
                                              p.snowflake_full_tiles, _i):
                snow_spans.append((_i + 1, spec))
                snow_tiles += int(spec.tiles)
            _i = _j + 1
    p2.snowflake_spans = snow_spans
    p2.snowflake_rs = rs
    p2.snowflake_shape_used = sorted({s.shape for _s0, s in snow_spans}) or []

    # ---------------- 组装锁定决策 + 屏障 ---------------------------------------
    snow_onsets: set[int] = set()
    for _s0, _spec in snow_spans:
        for _j in range(int(_spec.tiles)):
            snow_onsets.add(_s0 - 1 + _j)
    locked: dict = {}
    for _i, (_j, _t) in hit_at.items():
        locked[_i] = Decision("figure", float(_t.travel[_j]), 1.0,
                              bool(_t.twirl[_j]) if _t.twirl else False,
                              0.0, 1, "tpl")
    # 雪花段先放**占位**决策：真正的内容由 `_apply_snowflakes` 覆盖
    # （它要按绝对匀速解 k，且带 docs/24 §4 的两遍式时序修正）。
    for _i in snow_onsets:
        if 0 <= _i < n_on and _i not in locked:
            locked[_i] = Decision("figure", 180.0, 1.0, False, 0.0, 1, "snow")

    blocked = set(locked) | rests
    walk = _fig.Walk(R2=2.0 * p.planar_radius, allow_twirl=bool(p.allow_twirl))
    # Q8：豁免只有「双押、雪花」。双押格此刻还不知道（在 solve 之后才插），
    # 所以这里只豁免雪花；双押由 `dp_*` 在阶梯之后插入，不经过本模块。
    ctx = Ctx(rs=rs, p=p, ks=ks, tail_k=tail_k, prefer_travel=prefer_travel,
              blocked=blocked, locked=locked, locked_len={k: 1 for k in locked},
              exempt=set(snow_onsets), walk=walk, allow_figures=True,
              base_bpm=float(base_bpm))
    pl = plan(ctx)

    # ---------------- 决策 → 层 ------------------------------------------------
    travels = [S.FIRST_TRAVEL]
    ks_out = [1.0]
    pauses = [0.0]
    flips_of_floor = [False] * (n_on + 2)
    marks = [""] * (n_on + 1)
    for _i, d in enumerate(pl.decisions):
        travels.append(float(d.travel))
        ks_out.append(float(d.k))
        pauses.append(float(d.pause_beats))
        if d.twirl:
            flips_of_floor[_i + 1] = True
        marks[_i + 1] = str(d.src or "")
    travels.append(S.FIRST_TRAVEL)
    ks_out.append(ks_out[-1])
    pauses.append(0.0)

    floors: list[S.Floor] = []
    for _i, _tv in enumerate(travels):
        k = ks_out[_i] if _i < len(ks_out) else 1.0
        floors.append(S.Floor(travel=float(_tv), bpm=float(base_bpm) / k,
                              twirl=False, turn=0.0, heading=0.0, angle=0.0,
                              speed_k=k, pause_beats=float(pauses[_i])))

    # 标记所有权（角度双押插入时不许拆这些格）
    for _i in range(1, len(floors)):
        src = marks[_i] if _i < len(marks) else ""
        if src == "tpl":
            floors[_i].template = True
        elif src == "nat":
            floors[_i].natural = True
        elif src == "eng":
            floors[_i].engine = True

    # ---------------- 落地 ------------------------------------------------------
    p2.template_spans = spans
    p2.template_flips = tpl_flips
    p2.engine_spans = []            # 引擎已作为第 1 级图形逐格写入，不再整段改 flip
    p2.natural_spans = []           # 同上
    p2.template_all_rounds = bool(getattr(p, "template_all_rounds", True))
    S._finish(floors, flips_of_floor, p2)

    ch.floors = floors
    if getattr(p, "use_position_track", False):
        from . import track_fx as _tfx
        _tfx.apply(ch, step=float(getattr(p, "pos_track_step", 0.22)),
                   min_beats=float(getattr(p, "pos_track_min_beats", 8.0)))
    else:
        ch.meta["pos_tracks"] = []
        ch.meta["pos_track_n"] = 0

    ch.first_onset_ms = onsets[0].t_ms
    ch.meta.update({
        "n_onsets": n_on,
        "n_floors": len(floors),
        "n_lead": 1,
        "lead_ms": S.FIRST_TRAVEL / 180.0 * (60000.0 / max(1e-9, base_bpm)),
        "duration_ms": onsets[-1].t_ms - onsets[0].t_ms,
        "ref_beats": prep.get("ref_beats", 1.0),
        "straight_frac": ch.straight_frac,
        "event_frac": 0.0,
        "tick_err_ms": prep.get("tick_err", 0.0),
        "clamped": 0,
        "quant_err_ms": prep.get("quant_err", 0.0),
        "interval": S.interval_stats(onsets),
        "tpl_hits": len(spans),
        "tpl_rounds": tpl_reps,
        "tpl_covered": len(hit_at),
        "tpl_names": [t.name for _, t in spans],
        "tpl_spans": span_meta,
        "tri_runs": 0,
        "nat_count": sum(1 for m in marks if m == "nat"),
        "nat_tiles": sum(1 for m in marks if m == "nat"),
        "eng_count": sum(1 for m in marks if m == "eng"),
        "eng_tiles": sum(1 for m in marks if m == "eng"),
        "eng_spans": [],
        "snow_count": len(snow_spans),
        "snow_tiles": snow_tiles,
        "snow_spans": [(int(s0), s.n_rot, s.arms, s.tiles) for s0, s in snow_spans],
        "snow_shapes": sorted({s.shape for _s0, s in snow_spans}),
        "twirl_moved_snow": int(getattr(p2, "twirl_moved_by_snow", 0) or 0),
        "travel_min": float(p.travel_min),
        # ★★ 2026-10（上面的 bug）：以前这里**只有 min**，`core.rules` 的 ③c
        #   「最大夹角」读的是 `meta["travel_max"]`，读不到就当「没设上界」
        #   ⇒ 阶梯一路超窗而状态栏写「违规 0」。补上它，检查器才看得见。
        "travel_max": float(getattr(p, "travel_max", 0.0) or 0.0),
        "min_travel_seen": (min((f.travel for f in floors[1:-1]), default=180.0)
                            if len(floors) > 2 else 180.0),
        "straighten": dict(getattr(p2, "straighten_meta", {}) or {}),
        # ★ 阶梯自己的读数（老路径没有）
        "aggressive_pick": True,
        "ladder_rung_counts": dict(pl.stats["rung_counts"]),
        "ladder_n_twirl": int(pl.stats["n_twirl_decided"]),
    })
    return ch

