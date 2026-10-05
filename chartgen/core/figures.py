"""统一的「图形候选生成 + 单一评分器」。

原来自然段和三连音引擎是**两条平行路径**，各自 greedy 抢格：
自然段先跑（长窗口优先），引擎只能捡剩下的。结果：
  ① 谁先跑谁赢，跟好坏无关；
  ② 两条路径各有一套判据，候选之间没法比较；
  ③ 「不自重叠」压根没有落脚点。

现在合成一条：同一个窗口上两个生成器各出候选，用**同一个 scorer** 比。

评分顺序（用户口径，越靠前越硬）：
  ① 不产生 SetSpeed（沿用前一段的速度档）  ←「滥用速度会让手感非常差」
  ② Y > 0（向上绘制）                      ← 社区不约而同（五月雪版堕落时代）
     ★ 2026-10 用户：「算法会贪心地倾向于原地打转，建议**增加原地惩罚**」
       ⇒ 这一项从「0/1 二值闸」升级成**分级净位移**（`-prog`）：
       净位移 ≥ 0 的仍然全部优于 < 0 的，但「绕一圈原地打转」（净位移 0）
       从此打不过「同一朵花但整体往上挪」。见 `score()`。
  ③ 不自重叠（非相邻格 ≥ 1.0R）            ←「更优先让他们不自己重叠」
  ④ 闭合 Σtravel ≡ 180n (mod 360)          ← **硬过滤**，见下
  ⑤ Twirl 少                               ← 零成本，但叠多了瞎眼
  ⑥ 更长、travel 更接近 prefer_travel       ← 平手时的收尾

④ 是硬过滤而不是打分项：README 的两条不变量（Σtravel = 180×拍数 且 Σ转角 ≡ 0
mod 360）不满足就会**逐格漂角**，最后漂成斜线 —— 那正是我们要根除的问题。
生成阶段就只吐闭合的图形，所以这一项永远不会出现在比较里。

几何推进用的是 `core.solve._apply` 的**同一套公式**，不是复制品：
    ccw = not ccw（若本格有 Twirl）
    turn = (180 − travel) if ccw else (travel + 180)
    heading += turn
    p += unit(heading)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from fractions import Fraction

from .triplet_engine import Run, is_triplet, plan_run

TRAVEL_MIN = 45.0
TRAVEL_MAX = 180.0
# ★ 量纲要对：一格 = 2·planar_radius = 2.0 个单位。
#   原来写 1.75 —— 那只能捞到「几乎完全重合」，等于把「不自重叠」这条评分项废掉了。
#   实测 FallenEra 排掉雪花后：<0.5 有 412 对、<1.0 有 517 对、<2.0 有 853 对。
#   1.0 = 半格，是「两格压在一起」的判据。
OVERLAP_R = 1.0
RECENT = 24


def nice_travel(r: float) -> float:
    """r（拍）→ k=1 时的 travel = 180·r。

    ★ 先吸附到最简有理数：`rs` 是毫秒折出来的，180.00018 之类噪声累计起来
      比容差大几十倍，判据会整片落空。

    ★ 2026-10 加**记忆化**：`nat_candidates` 对每个 n 都重算一遍窗口里的
      `nice_travel`（O(hi²) 次），而 `Fraction.limit_denominator` 是纯 Python、
      相当贵 —— 实测占 Automaton_Waltz 求解的 **~1.8s/2.9s（cProfile）**。
      这个函数是纯函数 ⇒ 直接按 `round(r, 6)` 缓存，语义不变。
    """
    _k = round(float(r), 6)
    _v = _NICE_CACHE.get(_k)
    if _v is None:
        _v = float(Fraction(180) * Fraction(_k).limit_denominator(192))
        _NICE_CACHE[_k] = _v
    return _v


#: `nice_travel` 的记忆表（纯函数缓存）
_NICE_CACHE: dict = {}


# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Figure:
    """一段图形：逐格的 travel / 速度档 / Twirl。"""
    start: int
    n: int
    travels: tuple[float, ...]
    ks: tuple[float, ...]
    twirls: tuple[bool, ...]
    kind: str                       # "nat" | "eng"
    run: Run | None = None          # 引擎候选才带（落盘时要 s_sum / flips）

    @property
    def travel0(self) -> float:
        return self.travels[0]


@dataclass
class Walk:
    """边扫边走的几何状态。公式与 `_apply` 逐字一致。"""
    R2: float = 240.0
    allow_twirl: bool = True
    heading: float = 90.0
    ccw: bool = False
    x: float = 0.0
    y: float = 0.0
    pts: list[tuple[float, float]] = field(default_factory=list)

    def step(self, travel: float, twirl: bool) -> None:
        if twirl and self.allow_twirl:
            self.ccw = not self.ccw
        turn = (180.0 - travel) if self.ccw else (travel + 180.0)
        turn = ((turn + 180.0) % 360.0) - 180.0
        h = ((self.heading + turn) + 180.0) % 360.0 - 180.0
        self.heading = h
        self.x += self.R2 * math.cos(math.radians(h))
        self.y += self.R2 * math.sin(math.radians(h))
        self.pts.append((self.x, self.y))


# ---------------------------------------------------------------------------
def probe(fig: Figure, w: Walk, want_ov: bool = True) -> tuple[float, int]:
    """把图形在**当前状态**下预演一遍，返回 (净 Y 位移, 非相邻过近对数)。

    不修改 `w`。

    ★ `want_ov=False`：**不算重叠**（省掉 `O(n×RECENT)` 次 hypot）。
      这是给「两段式比较」用的（见 `_prefix` / `choose`）——
      评分元组里 `ov` 排在后面，前缀就打平的候选才需要它。
    """
    heading, ccw, x, y = w.heading, w.ccw, w.x, w.y
    recent = list(w.pts[-RECENT:])
    ov = 0
    for t, tw in zip(fig.travels, fig.twirls):
        if tw and w.allow_twirl:
            ccw = not ccw
        turn = (180.0 - t) if ccw else (t + 180.0)
        turn = ((turn + 180.0) % 360.0) - 180.0
        heading = ((heading + turn) + 180.0) % 360.0 - 180.0
        x += w.R2 * math.cos(math.radians(heading))
        y += w.R2 * math.sin(math.radians(heading))
        if want_ov:
            # recent[:-1] = 排除紧邻的前一个点（相邻格本来就近）
            for px, py in recent[:-1]:
                if math.hypot(x - px, y - py) < OVERLAP_R:
                    ov += 1
            recent.append((x, y))
            if len(recent) > RECENT + 1:
                recent.pop(0)
    return y - w.y, ov


def is_closed(fig: "Figure") -> bool:
    """这段图形是不是**闭合**的（Σtravel ≡ 180n (mod 360)）。

    ★ 用户 2026-10：「调整闭合图形使用策略 —— 让程序更倾向于或者更不倾向于使用闭合图形」。
      闭合 = 行星走完这一段之后**回到主轴方向**，观感上是「一个收得住的花」；
      不闭合则会把 heading 带偏，靠后面的格慢慢扭回来。
    """
    return _closed([float(t) for t in fig.travels], int(fig.n))


def prefix(fig: Figure, w: Walk, *, prev_k: float, next_k: float | None,
           closed_bias: int = 0, inplace_waste: bool = True,
           dy: float | None = None) -> tuple:
    """评分元组的**前半段**（便宜的那半：不算重叠）。

    `score()` = `prefix()` + `(ov, tw, -n, |travel0 − prefer|)` —— **逐位一致**。
    拆开的理由：`ov` 要跑 `O(格数×24)` 次 hypot，是唯一的重活；
    而元组比较是**字典序**，前缀分出胜负的候选**根本不需要**算 `ov`。
    2026-10 加「DP 对照项」后候选数翻倍，实测求解因此从 0.32s 涨到 1.14s
    （Automaton_Waltz）—— 两段式比较把这一份开销还回去（见 `choose`）。

    `dy`：调用方已经算好的「净 Y 位移」（见 `dp_walk_dy`）—— 传进来就不用再走一遍。
    """
    # ① SetSpeed：本段内部 + 进/出各一次
    changes = 0
    cur = float(prev_k)
    for k in fig.ks:
        if abs(k - cur) > 1e-9:
            changes += 1
            cur = float(k)
    if next_k is not None and abs(float(next_k) - cur) > 1e-9:
        changes += 1
    # ② 几何：能走直线的格就走直线。
    #    这是 DP 的设计意图，也是项目的红色底线（直线层 > 45%）。
    #    ★ 必须排在「省一次 SetSpeed」之后但在 Y>0 之前：用户口径是
    #      「优先对图形，实在对不上了再变速」，但**直线本身也是图形**；
    #      为了省一个速度事件把直线格扭成弧线，是拿手感换数字。
    ns = sum(1 for t in fig.travels if abs(t - 180.0) < 1e-6)
    # ②b 闭合图形偏好（用户 2026-10 新增）
    closed_term: tuple = ()
    if closed_bias > 0:
        closed_term = (0 if is_closed(fig) else 1,)
    elif closed_bias < 0:
        closed_term = (1 if is_closed(fig) else 0,)
    # ③ 净位移（原地惩罚）—— 只算终点，不算重叠
    if dy is None:
        dy, _ = probe(fig, w, want_ov=False)
    # ★★ 原地惩罚（用户 2026-10：「算法会贪心地倾向于**原地打转**，建议增加原地惩罚，
    #   让算法更倾向于**向规划的方向**铺设轨道」）。
    #   `probe` 给的 `dy` 是整段走完的**净 Y 位移**；除以「每格正好走一格」=`n·R2`
    #   就是「每格的平均前进量」`prog`（1 = 一路向上 / 0 = **原地打转** / 负 = 往回走）。
    #
    #   ★ 根因就在上面的 `changes`：**SetSpeed 数是第一位的判据**，而
    #     「原地绕 90° 小方块」（一串 travel=90/270）恰好**一次速度都不用换** ⇒
    #     哪怕它一格都没往前走，也总能打赢「换两次速度、一路走直线」的写法。
    #     实测 Automaton_Waltz 就是被这样铺出 100+ 格的 90° 方阵（原地打转 12%）。
    #
    #   处罚口径：**每 3 格白转 ≈ 一个 SetSpeed 的代价** ——
    #     与 `_dp_tiers` 里「1 格直线 = 1.0、1 次换档 = `speed_switch_penalty` = 3.0」
    #     同一套标定：`waste = (1 − prog) × 格数 ÷ 3`（四舍五入）。
    #     ⇒ 12 格原地打转 = 4 分，**输给**「2 次 SetSpeed + 一路直线」的 2 分；
    #        而 3 格以内的小回环（= 1 分）仍然可以靠省一次 SetSpeed 换回来。
    _step = max(1, int(fig.n)) * float(w.R2 or 1.0)
    prog = dy / _step if _step else 0.0
    prog = max(-1.0, min(1.0, float(prog)))
    waste = int(round(max(0.0, 1.0 - prog) * max(1, int(fig.n)) / 3.0))
    if fig.kind != "nat" or not inplace_waste:
        # · 引擎（三连音）是**结构性**的：形状由 `plan_run` 定死，没得挑
        #   ⇒ 不能因为它「前进得少」就把它罚到打不过自然段（那会毁掉三连音谱面）。
        # · `inplace_waste=False`（`SolveParams`，2026-10 加）= **总开关**，
        #   关掉就回到「只看 0/1 的 Y>0」那条老口径（A/B 与冻结哈希用）。
        waste = 0
    return (changes + waste, -ns) + closed_term + (-round(prog, 4),)


def tail(fig: Figure, w: Walk, *, prefer_travel: float) -> tuple:
    """评分元组的**后半段**（唯一的重活：`ov`；只有前缀打平时才算）。"""
    _dy, ov = probe(fig, w, want_ov=True)
    tw = sum(1 for t in fig.twirls if t)
    return (ov, tw, -fig.n, abs(fig.travel0 - prefer_travel))


def score(fig: Figure, w: Walk, *, prev_k: float, next_k: float | None,
          prefer_travel: float, closed_bias: int = 0,
          inplace_waste: bool = True) -> tuple:
    """越小越好。**= `prefix()` + `tail()`**，顺序见模块 docstring。

    `closed_bias`（用户 2026-10）：**闭合图形使用策略** —— 0 = 不干预（默认）；
    > 0 = 更倾向闭合；< 0 = 更不倾向闭合。
    插在「直线格数」之后、「Y>0 / 不自重叠」之前：它是一条**实打实的偏好**，
    但**不许盖过**「少换档」和「走直线」这两条既有红线。
    """
    pre = prefix(fig, w, prev_k=prev_k, next_k=next_k,
                 closed_bias=closed_bias, inplace_waste=inplace_waste)
    return pre + tail(fig, w, prefer_travel=prefer_travel)


def _closed(tv: list[float], n: int) -> bool:
    """Σtravel ≡ 180n (mod 360) —— README 的第一条不变量。"""
    d = math.fmod(sum(tv) - 180.0 * n, 360.0)
    return abs(d) < 1e-6 or abs(abs(d) - 360.0) < 1e-6


# ---------------------------------------------------------------------------
def nat_candidates(i: int, n: int, rs, ks, p, tail_k: float) -> list[Figure]:
    """自然闭合段：`travel = 180·rs/k`，零 Twirl，靠 Σtravel 自己复位。

    k 优先沿用 DP 在这一段选好的档（不为对音另开速度），全不闭合才退回 k=1。
    k 是 2 的幂 ⇒ 二进制精确，除法不引入误差。

    ★ 下界取 `max(natural_travel_min, travel_min)`：后者是用户口径的「最小角度」，
      自然段也不能绕过它。
    """
    lo = max(float(p.natural_travel_min), float(getattr(p, "travel_min", 0.0)))
    # ★ 2026-10：上界也要吃用户口径的「最大夹角」—— 以前只吃 `natural_travel_max`
    #   （默认 180），所以 `travel_max=150` 这种窗口下自然段照样吐 180°。
    #   `travel_max=0`（默认）时 `min()` 取回原值 ⇒ 老路径逐字节不变。
    hi_tv = float(p.natural_travel_max)
    _tmax = float(getattr(p, "travel_max", 0.0) or 0.0)
    if _tmax > 0:
        hi_tv = min(hi_tv, _tmax)
    base = [nice_travel(rs[i + j]) for j in range(n)]
    order: list[float] = []
    if p.natural_reuse_speed:
        for j in range(n):
            kk = float(ks[i + j]) if (i + j) < len(ks) else float(tail_k)
            if kk not in order:
                order.append(kk)
    if 1.0 not in order:
        order.append(1.0)

    out: list[Figure] = []
    seen: set[tuple] = set()
    for k in order:
        tv = [b / k for b in base]
        if any(t < lo or t > hi_tv for t in tv):
            continue
        if not _closed(tv, n):
            continue
        key = tuple(round(t, 6) for t in tv)
        if key in seen:
            continue
        seen.add(key)
        out.append(Figure(i, n, tuple(tv), (k,) * n, (False,) * n, "nat"))
    return out


def eng_candidates(i: int, n: int, rs, p, prefer_travel: float) -> list[Figure]:
    """三连音引擎：等值连打，恒定 travel + 块状 Twirl 掩码（S=+ 段 / − 段）。"""
    if n < p.triplet_min_tiles:
        return []
    r0 = rs[i]
    if not is_triplet(r0):
        return []
    if any(abs(rs[i + j] - r0) > p.triplet_tol for j in range(n)):
        return []
    run = plan_run(r0, n, prefer_travel=prefer_travel)
    if run is None or run.n != n:
        return []
    # ★ 最小角度：引擎的 travel 是恒定值，低于用户口径就整段放弃（交回 DP）
    if run.travel < float(getattr(p, "travel_min", 0.0)) - 1e-9:
        return []
    # ★ 2026-10：最大夹角同理（triplet 引擎的 travel 不受 `natural_travel_max` 管）
    _tmax = float(getattr(p, "travel_max", 0.0) or 0.0)
    if _tmax > 0 and run.travel > _tmax + 1e-9:
        return []
    return [Figure(i, n, (run.travel,) * n, (run.speed_k,) * n,
                   tuple(run.flips), "eng", run)]


def dp_candidate(i: int, n: int, rs, ks, p, tail_k: float) -> Figure:
    """「**DP 本来会怎么铺**」——把逐格档位组成的写法也当一个候选。

    ★★ 2026-10 用户：「算法会贪心地倾向于**原地打转**，建议增加原地惩罚，
    让算法可以更倾向于**向规划的方向铺设轨道**」。

    ★ 为什么光在「闭合图形」内部比不够：`nat_candidates` 的每一段**只用一个 k**
    （uniform-k），于是音乐里一混长短音，它就只能靠 90°/270° 去凑闭合 ——
    而 DP 是**逐格**选档的（r 是长音就 k 大、短音就 k 小），能把同一段铺成
    「该直的直、该短的短」。实测 Automaton_Waltz @651 就是这种混排：
    DP 自己给的是 `180/180/90/180/180/90…`，闭合段给的是一串 90°。

    这个候选**不保证闭合**（DP 路径本来就不保证逐段闭合，那是 `straighten` 的活），
    所以它**只用来比较**：赢了也只是让 `choose` 返回 `None`（= 交回 DP 自己铺），
    **不会**破坏「生成阶段只吐闭合图形」这条硬不变量。

    `kind="dp"` 与 `eng` 一样**不吃原地惩罚**（它是逐格最优，不是"选形状"）。
    """
    kk = [float(ks[i + j]) if (i + j) < len(ks) else float(tail_k)
          for j in range(n)]
    tv = []
    for j in range(n):
        t = 180.0 * float(rs[i + j]) / kk[j]
        tv.append(180.0 if abs(t - 180.0) < 1e-6 else t)
    return Figure(i, n, tuple(tv), tuple(kk), (False,) * n, "dp")


def dp_walk_dy(i: int, hi: int, rs, ks, tail_k: float, w: Walk) -> list[float]:
    """给 `dp_candidate` 用：**走一次** `hi` 格，返回「窗口长度 n 的净 Y 位移」表。

    `dy[n - 1]` = 长度 n 的 DP 走位的净位移。

    ★ 为什么要单独做：`dp_candidate` 对每个 n（2..24）都要算一次 `probe`，
      而它们其实是**同一串逐格走位的前缀** ⇒ 23 次走位可以合成 1 次。
      实测这一步把 Automaton_Waltz 的求解从 0.96s 拉回（见 `choose` 的注释）。
    """
    heading, ccw, x, y = w.heading, w.ccw, w.x, w.y
    out: list[float] = []
    n_all = len(rs)
    for j in range(max(0, int(hi))):
        k = float(ks[i + j]) if (i + j) < len(ks) else float(tail_k)
        t = 180.0 * float(rs[i + j]) / k if (i + j) < n_all else 180.0
        if abs(t - 180.0) < 1e-6:
            t = 180.0
        # ⚠ dp 候选**没有 Twirl**（`dp_candidate` 给的是全 False）⇒ 这里**不许**翻 ccw。
        #   （写错一次就被 golden 冻结哈希抓住：整段 heading 被镜像，dy 全错。）
        turn = (180.0 - t) if ccw else (t + 180.0)
        turn = ((turn + 180.0) % 360.0) - 180.0
        heading = ((heading + turn) + 180.0) % 360.0 - 180.0
        x += w.R2 * math.cos(math.radians(heading))
        y += w.R2 * math.sin(math.radians(heading))
        out.append(y - w.y)
    return out


def choose(i: int, rs, ks, p, w: Walk, *, blocked, tail_k: float,
           prefer_travel: float, prev_k: float,
           closed_bias: int | None = None, dp_mode: str = "none") -> Figure | None:
    """从 i 出发，挑一个最好的图形；挑不出来返回 None（交回 DP）。

    ★ 关于「闭合图形使用策略」（用户 2026-10）：
      **`nat_candidates` 产出的候选本来就全都是闭合的**（`_closed` 是硬过滤），
      所以「闭合 / 不闭合」在 `score` 里**分不出胜负** —— 真正决定
      「用不用闭合图形」的是下面那道**硬闸**：

          闭合段要变速就必须够长（短段变速 = 白花两个 SetSpeed 换一个跟 DP 没差的图形）

      于是这个偏好就落在**闸门的松紧**上：
          更倾向闭合 ⇒ 闸门放宽（更多闭合段被接受）
          更不倾向   ⇒ 闸门收紧（只有明显划算的闭合段才用）

    ★★ 2026-10 用户：「算法会贪心地倾向于原地打转…让算法更倾向于**向规划的方向**
      铺设轨道」⇒ 从这一版起，「**DP 自己的逐格铺法**」(`dp_candidate`) 也进候选：
      它更前进/更省事就让它赢。

      `dp_mode` 决定「它赢了怎么办」（两条调用方的语义不同，**不能混**）：
        · `"none"`（默认，`core/solve.py` 用）⇒ 返回 `None` = **交回 DP 逐格铺**。
          这正是老路径的定义，也保证「图形段只吐闭合图形」这条硬不变量不被破坏。
        · `"figure"`（`core/ladder.py` 用）⇒ **把这个 dp 候选当图形返回**。
          为什么不能也返回 None：阶梯里 `None` 的意思是「**往下走一级**」
          （第 2 级用 `k_prev` 硬画），于是直线率会从 47% 掉到 19% ——
          实测（FallenEra）就是这么掉的。阶梯是**逐格 Decision**，
          直接照抄 dp 候选的逐格 travel/k 恰好等价于「第 1 级用 DP 的答案」。
    """
    if closed_bias is None:
        closed_bias = int(getattr(p, "closed_figure_bias", 0) or 0)
    base_min = int(p.natural_speed_min_tiles)
    if closed_bias > 0:
        nat_min_tiles = max(2, base_min // 2)
    elif closed_bias < 0:
        nat_min_tiles = base_min * 2
    else:
        nat_min_tiles = base_min
    max_n = int(p.natural_max_tiles)
    hi = min(max_n, len(rs) - i)
    waste_on = bool(getattr(p, "inplace_waste", True))
    best: Figure | None = None
    best_pre: tuple | None = None          # 便宜的前缀（赢家暂时只按它挑）
    best_s: tuple | None = None            # 全量元组（只有前缀打平时才补算）
    dp_wins = False
    # ★ DP 对照项的净位移表：**一次走位**算出所有 n 的 `dy`（见 `dp_walk_dy`）
    dp_want = (str(dp_mode) != "off")
    dp_dy = dp_walk_dy(i, hi, rs, ks, tail_k, w) if dp_want else []
    for n in range(hi, 1, -1):
        if any((i + j) in blocked for j in range(n)):
            continue
        cands: list[Figure] = []
        if p.use_natural_spans:
            cands += nat_candidates(i, n, rs, ks, p, tail_k)
        if p.use_triplet_engine:
            cands += eng_candidates(i, n, rs, p, prefer_travel)
        # ★ 原地惩罚的对照项：DP 自己会怎么铺这一段（见 `dp_candidate`）。
        #   ★★ **无条件加**（哪怕这一窗一条 nat/eng 都没有）—— 这就是冻结哈希那一版的
        #   行为：dp 候选在某個 n 上赢 ⇒ `dp_wins` ⇒ 直接交回 DP。
        #   我一度想「没有 nat 候选就短路」（省一点评分），结果**被 golden 抓住**：
        #   那一窗的 dp 候选本来能赢、能决定「这一段交回 DP」，短路掉就变成
        #   让别的 n 上的 nat 图形胜出 ⇒ 产物变了。
        #   （性能由 `dp_walk_dy` 的一次走位解决，不靠短路。）
        if dp_want and n - 1 < len(dp_dy):
            cands.append(dp_candidate(i, n, rs, ks, p, tail_k))
        if not cands:
            # `dp_mode="off"`（阶梯保留老行为）且这一窗没有真候选 ⇒ 交回调用方
            continue
        nk = float(ks[i + n]) if (i + n) < len(ks) else float(tail_k)
        for c in cands:
            # ★★ 两段式比较（等价于原来的全量元组比较，但省掉绝大部分 `ov` 计算）：
            #   先比前缀；前缀打平才去算 `tail`（O(格数×24) 次 hypot 的重活）。
            pre = prefix(c, w, prev_k=prev_k, next_k=nk,
                         closed_bias=closed_bias, inplace_waste=waste_on,
                         dy=(dp_dy[n - 1] if c.kind == "dp" else None))
            # 硬闸：自然段**要变速就得够长**。短段变速 = 白花两个 SetSpeed
            # 换一个跟 DP 没差的图形（用户口径：只有雪花豁免速度限制）。
            # ★ 「闭合图形使用策略」就是调这道闸的松紧（见 docstring）。
            #   `pre[0]` = 换档数 + 原地惩罚（= 老 `s[0]`）
            if c.kind == "nat" and pre[0] > 0 and c.n < nat_min_tiles:
                continue
            if best_pre is None or pre < best_pre:
                best_pre, best, best_s = pre, c, None
                dp_wins = (c.kind == "dp")
            elif pre == best_pre:
                if best_s is None:
                    best_s = best_pre + tail(best, w, prefer_travel=prefer_travel)
                s = pre + tail(c, w, prefer_travel=prefer_travel)
                if s < best_s:
                    best_s, best = s, c
                    dp_wins = (c.kind == "dp")
    # 收尾：赢家是**按前缀**挑出来的 ⇒ 补算它的后缀（保持与 `score()` 逐位一致）
    if best is not None and best_s is None:
        best_s = best_pre + tail(best, w, prefer_travel=prefer_travel)
    # ★★ 「DP 自己铺更前进」⇒ 按调用方的语义落地（见 docstring 的 `dp_mode`）。
    if dp_wins:
        if str(dp_mode) == "figure" and best is not None and best.kind == "dp":
            return best                       # 阶梯：照抄 DP 的逐格 travel/k
        return None                           # 老路径：交回 DP 逐格铺
    return best
