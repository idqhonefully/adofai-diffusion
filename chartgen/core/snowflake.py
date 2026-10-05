"""魔法阵（雪花）生成器。

**几何**（从 R lv16 / FALLENERA 反解，见 `tools/snowflake_anatomy.py`）

    一朵雪花 = N 条**闭合花瓣**，都从同一个中心点出发、又回到同一个中心点。

    一条花瓣（2·m 格）：
        outbound   m 条 heading： h0, h0+t1, h0+t1+t2, …     合计转角 T_out
        back       把 outbound 每条 heading +180°，**顺序不变**
      ⇒ back 的向量和恰好 = −(outbound 的向量和) ⇒ 花瓣**必闭合**（任意 m、任意分摊都成立）

    相邻两条花瓣之间 heading 要连续，给出唯一约束：

        360/N = 180 + T_out        ⇒  T_out = 360/N − 180        （恒为负）

**内圈 / 外圈**（游戏口径，`scrFloor.cs:985`）

    num = GetAngleMoved(entry, exit, !isCCW) ∈ [0, 2π)
    flag2 = num < π
    SetIconSprite(flag2 ? sprIconSwirlRed : sprIconSwirlBlue);
    //            红 = 小圈 / 内圈           蓝 = 大圈 / 外圈

    落到我们的口径就是：

        travel = 180 + turn
        travel < 180  ⇔  turn < 0   →  内圈（行星切角，走短线）
        travel > 180  ⇔  turn > 0   →  外圈（行星绕一大圈）

    R lv16（唯一被认可的参考）实测 `tools/_inner_outer.py`：
        内圈 61.1%   外圈 **0.0%**   直线 38.9%      travel 只有 {180, 135, 90}
    ⇒ **内圈 ⇔ 转角全部 ≤ 0**。

    ★ 所以**不做镜像**。`mirrored=True` 是把整朵转角反号 ⇒ 全部 travel 变成 360−T ⇒ 100% 外圈。
      镜像与「走内圈」在几何上互斥，见 `ALLOW_MIRROR`。

    ★ 内外圈和**包围盒无关**。包围盒只由 heading 序列（格子坐标）决定，
      而内外圈只由逐格转角（行星扫过的弧）决定，两者互相独立。

**形状**（用户口径：转角分布完全随便）

    只有两条硬约束：① 出去那半段转角之和 = T_out  ② 回来 = 出去 +180°、同序。
    剩下怎么分摊都合法，所以形状是自由的 —— `out_turns()` 就是那个分摊。

    实现在 `unit` 度的**整数网格**上做整数分割：和**精确**等于 T_out，
    没有余数补偿、不会漏出正转角（也就不会意外变外圈）。

**速度**（用户口径：绝对匀速）

    每格 dt 必须相同 ⇒ speed ∝ 该格转角。不自己算倍率，直接解「BPM 除数」：

        k = 180 · r / travel          （r = 该格占几拍）

    这样每格 dt 精确等于音乐给的间隔，等间隔段落自然全等。
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

#: 允许的旋转阶数
N_CHOICES: tuple[int, ...] = (6, 8, 10, 12)

#: 一条花瓣最少几步（2 格 = 一去一回，退化成直线，不要）
MIN_ARMS = 2

#: 分摊转角用的基础网格（度）
TURN_UNIT = 3.0

#: 单步转角上限（度）—— travel ≥ 60°，避免发卡弯
TURN_MAX = 120.0

#: 是否允许镜像整朵。
#: **必须 False**：镜像 = 转角全体反号 ⇒ travel 全部 > 180 ⇒ 100% 外圈。
#: 用户口径「优先走内圈」，R lv16 也是 0% 外圈。
ALLOW_MIRROR = False

#: 花瓣形状（把 T_out 分摊到 m−1 步上的方式）。形状**不固定**，都合法。
SHAPES: tuple[str, ...] = ("uniform", "front", "back", "step", "zigzag", "random")

#: 确定性形状（剔掉需要随机数的 "random"）—— 确定性路径只会用这些。
SHAPES_DETERMINISTIC: tuple[str, ...] = tuple(s for s in SHAPES if s != "random")


def beat_angle(n_rot: int, p_interval: int, *, inner: bool = True) -> float:
    """开源项目 `star_calculator.js` 的基准 travel（用来对标我们的形状）。

    那边的公式（`pCount` = 对称阶数 = 我们的 `n_rot`，`pInterval` = 每臂步数）：

        beatAngle = 180 + (180 − 180/(pCount/2)) / pInterval      # 外圈
        pReverse  ⇒ beatAngle = 360 − beatAngle                    # 内圈

    记 `pInterval = q`，内圈那支展开：

        inner = 360 − [180 + (180 − 360/n_rot)/q]
              = 180 + (360/n_rot − 180)/q

    而本项目 `shape="uniform"` 把 `turn_out = 360/n_rot − 180` 均匀分摊到
    `q = arms − 1` 个槽 ⇒ 每步 travel = `180 + turn_out/q` —— **完全一致**。

    ★ 所以对标关系是 **`pInterval = arms − 1`**（可分的转向槽数，不是 `arms`）。
      例：`n_rot=6, arms=3` ⇒ `q=2` ⇒ inner = 180 + (−120)/2 = **120°**
      （与实测 `travels() = [180,120,120,…]` 一致），外圈则是 240°。
    """
    outer = 180.0 + (180.0 - 360.0 / float(n_rot)) / float(p_interval)
    return (360.0 - outer) if inner else outer



def _dist_unit(total: float, q: int) -> float:
    """挑分摊用的角度网格：在能整除 `total` 的前提下尽量粗，但每槽至少 ~2 格。"""
    n = abs(total)
    for u in (15.0, 5.0, 3.0, 1.0):
        if abs(total / u - round(total / u)) < 1e-9 and n / u >= 2 * max(1, q):
            return u
    for u in (15.0, 5.0, 3.0, 1.0):
        if abs(total / u - round(total / u)) < 1e-9:
            return u
    return TURN_UNIT


def _repair(parts: list[int], n: int, cap: int) -> None:
    """就地修：让 `0 ≤ parts[i] ≤ cap` 且 `sum(parts) == n`。"""
    q = len(parts)
    for i in range(q):
        parts[i] = max(0, min(cap, parts[i]))
    diff = n - sum(parts)
    if diff > 0:
        i = 0
        guard = 0
        while diff and guard < 1000 * q + 1000:
            guard += 1
            for _ in range(q):
                if diff == 0:
                    break
                if parts[i % q] < cap:
                    parts[i % q] += 1
                    diff -= 1
                i += 1
    elif diff < 0:
        i = 0
        guard = 0
        while diff and guard < 1000 * q + 1000:
            guard += 1
            for _ in range(q):
                if diff == 0:
                    break
                if parts[i % q] > 0:
                    parts[i % q] -= 1
                    diff += 1
                i += 1


def _split(n_units: int, q: int, shape: str, rng: random.Random, cap: int) -> list[int]:
    """把 `n_units` 个「格」分给 `q` 个槽，返回长度 q 的非负整数表（和精确 = n）。

    `cap` = 单槽上限（格数）。形状只是**起手式**，最后统一走 `_repair` 保证合法。
    """
    if q <= 0:
        return []
    if q == 1:
        return [n_units]
    cap = max(cap, math.ceil(n_units / q))
    if shape == "front":
        raw = [n_units] + [0] * (q - 1)
    elif shape == "back":
        raw = [0] * (q - 1) + [n_units]
    elif shape == "step":
        half = n_units // 2
        raw = [half] + [0] * max(0, q - 2) + [n_units - half]
    elif shape == "zigzag":
        raw = []
        for i in range(q):
            raw.append(cap if i % 2 == 0 else 0)
    elif shape == "random":
        # 只挑 k 个槽真正受力，剩下的留 0（直线）—— 这才是"花瓣"而不是"圆"
        k = rng.randint(1, min(q, max(1, n_units)))
        slots = rng.sample(range(q), k)
        raw = [0] * q
        left = n_units
        for j, s in enumerate(slots):
            if j == k - 1:
                take = left
            else:
                take = rng.randint(1, max(1, left - (k - j - 1)))
            raw[s] = take
            left -= take
    else:  # uniform
        base, rem = divmod(n_units, q)
        raw = [base + (1 if i < rem else 0) for i in range(q)]
    _repair(raw, n_units, cap)
    return raw


@dataclass(frozen=True)
class Snowflake:
    """一朵雪花的参数。

    `arms` = 一条花瓣走几步（整朵 = 2·arms·n_rot 格）。
    `shape` / `seed` 决定**花瓣长什么样**，约束只有那两条硬约束。
    """

    n_rot: int
    arms: int
    mirrored: bool = False        # ★ 见 ALLOW_MIRROR：True 会整朵变外圈，默认永不开
    shape: str = "uniform"
    seed: int = 0

    @property
    def petal_tiles(self) -> int:
        return 2 * self.arms

    @property
    def tiles(self) -> int:
        return self.n_rot * self.petal_tiles

    @property
    def turn_out(self) -> float:
        """一条花瓣「出去」那半段的总转角（度）。恒为负。"""
        return 360.0 / self.n_rot - 180.0

    @property
    def delta_rot(self) -> float:
        """每换一条花瓣，整个人朝转多少度。恒 = 360/N。"""
        return 360.0 / self.n_rot

    def unit(self) -> float:
        return _dist_unit(self.turn_out, max(1, self.arms - 1))

    def out_turns(self) -> list[float]:
        """把 `turn_out` 分摊到 `arms−1` 步上的**转角序列** —— 形状就是它。

        全部 ≤ 0，和**精确** = `turn_out`（在 `unit` 的整数网格上做整数分割）。
        """
        q = self.arms - 1
        if q <= 0:
            return []
        u = self.unit()
        n = int(round(abs(self.turn_out) / u))
        cap = max(1, int(math.floor(TURN_MAX / u + 1e-9)))
        rng = random.Random((self.seed * 7919) ^ (self.arms * 131) ^ (self.n_rot * 17) ^ 0x5F3A)
        parts = _split(n, q, self.shape, rng, cap)
        vals = [-p * u for p in parts]
        # 数值上把最后一项补齐（u 是浮点，累计可能有 1e-13 级误差）
        err = self.turn_out - sum(vals)
        if abs(err) > 1e-9:
            vals[-1] += err
        return vals

    def out_headings(self) -> list[float]:
        """出去那 m 步各自的 heading（相对整朵第一条 heading = 0）。"""
        hs: list[float] = []
        h = 0.0
        for t in self.out_turns():
            hs.append(h)
            h += t
        if self.arms > 0:
            hs.append(h)                      # 最后一步之后的 heading（= T_out）
        return hs[:self.arms] if self.arms else []

    def turns(self) -> list[float]:
        """整朵雪花逐格的**转角**（度），已归一化到 (−180, 180]。

        第 0 格恒为 0（进雪花那一格走直线）。全部 ≤ 0 ⇔ 全部内圈。
        """
        out: list[float] = []
        prev = 0.0
        for t in range(self.n_rot):
            base = t * self.delta_rot
            outs = [base + h for h in self.out_headings()]
            heads = outs + [h + 180.0 for h in outs]
            for h in heads:
                if not out:                       # 整朵第 0 格：接上外部 heading，转角 0
                    out.append(0.0)
                else:
                    out.append(((h - prev + 180.0) % 360.0) - 180.0)
                prev = h
        return out

    def travels(self, heading_in: float = 90.0) -> list[float]:
        """整朵雪花逐格的 `travel`（度）。

        `heading_in` = 进雪花前那一格的 heading。
        `mirrored=False` 时 turn ≤ 0 ⇒ travel ≤ 180 ⇒ **全程内圈**。
        """
        return [180.0 + (t if not self.mirrored else -t) for t in self.turns()]

    def headings(self, heading_in: float = 90.0) -> list[float]:
        """整朵雪花**每格走出去时**的 heading（长度 = tiles）。"""
        hs: list[float] = []
        h = heading_in
        for t in self.turns():
            hs.append(h % 360.0)
            h = (h + (t if not self.mirrored else -t)) % 360.0
        return hs

    def positions(self, heading_in: float = 90.0,
                  start: tuple[float, float] = (0.0, 0.0)) -> list[tuple[float, float]]:
        """整朵雪花的格子坐标（长度 = tiles + 1，末尾应回到 start）。"""
        pts = [start]
        x, y = start
        for h in self.headings(heading_in):
            x += math.cos(math.radians(h))
            y += math.sin(math.radians(h))
            pts.append((x, y))
        return pts

    def inner_frac(self) -> float:
        """内圈格占比（travel < 180）。目标 = 1.0。"""
        tv = self.travels()
        return sum(1 for t in tv if t < 180.0 - 1e-9) / len(tv) if tv else 0.0

    def describe(self) -> str:
        return (f"{self.n_rot} 重 × {self.arms} 步（{self.tiles} 格）"
                f"　{self.shape}　{'镜像' if self.mirrored else '正向'}"
                f"　内圈 {self.inner_frac()*100:.0f}%")


def bbox_of(s: Snowflake) -> tuple[float, float, float]:
    """(宽, 高, 面积)。★ 这**不是**内外圈指标，只用来避免花瓣张得太开。"""
    pts = s.positions()
    w = max(p[0] for p in pts) - min(p[0] for p in pts)
    h = max(p[1] for p in pts) - min(p[1] for p in pts)
    return w, h, w * h


def richness(s: Snowflake) -> tuple:
    """「花不花」的打分（越大越好）：不同的转角档数 → 受力步数 → 总转角。"""
    ts = [abs(t) for t in s.turns() if abs(t) > 1e-9]
    if not ts:
        return (0, 0, 0.0)
    distinct = len({round(t, 6) for t in ts})
    return (distinct, len(ts), sum(ts))


def close_err(s: Snowflake) -> float:
    """闭合误差（应 ≤ 1e-9）。"""
    pts = s.positions()
    return math.dist(pts[0], pts[-1])


def outer_tiles(s: Snowflake) -> int:
    return sum(1 for t in s.travels() if t > 180.0 + 1e-9)


def candidate_grid(length: int, *, n_choices: tuple[int, ...] = N_CHOICES,
                   min_arms: int = MIN_ARMS) -> list[tuple[int, int, int]]:
    """枚举所有塞得进 `length` 的 `(tiles, n_rot, arms)`，**确定性顺序**。"""
    grid: list[tuple[int, int, int]] = []
    for n in n_choices:
        for arms in range(min_arms, length // (2 * n) + 1):
            tiles = 2 * n * arms
            if tiles <= length:
                grid.append((tiles, n, arms))
    return grid


def _pool_of(grid: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    """取「格数最大」那一档；同格数优先偶数 N（偶数 N 天然带中心对称）。"""
    if not grid:
        return []
    best = max(g[0] for g in grid)
    pool = [g for g in grid if g[0] == best]
    evens = [g for g in pool if g[1] % 2 == 0]
    return sorted(evens or pool)


def _valid(s: Snowflake, travel_min: float = 0.0) -> bool:
    """硬约束：必须内圈、必须闭合、别要退化的直线花瓣、不许低于最小角度。"""
    if outer_tiles(s) != 0:
        return False
    if not any(abs(x) > 1e-9 for x in s.out_turns()):
        return False
    if travel_min > 0 and min(s.travels()) < float(travel_min) - 1e-9:
        return False
    return close_err(s) <= 1e-6


def plan(length: int, *, n_choices: tuple[int, ...] = N_CHOICES,
         min_arms: int = MIN_ARMS, rng: random.Random | None = None,
         trials: int = 32, compact: bool = True,
         allow_mirror: bool = ALLOW_MIRROR,
         deterministic: bool = True,
         shapes: tuple[str, ...] | None = None,
         travel_min: float = 0.0) -> Snowflake | None:
    """给一段 `length` 格，挑一朵塞得下的雪花。

    - **N 随便**：搜「格数最大但不超过 length」的那几组；同格数优先偶数 N
    - **必须内圈**：硬筛掉任何 travel > 180 的候选（`allow_mirror=False` 时天然满足）
    - **越花越好**：合法候选里挑 `richness` 最大；同分时包围盒小的优先
    - `travel_min > 0`：整朵里任何一格的 travel 低于它就不要（用户口径的「最小角度」）
    - `shapes`：限定形状集合（`None` = 自动，用 `SHAPES_DETERMINISTIC`；
      传 `("random",)` 且 `deterministic=False` 才走随机采样）

    ★ `deterministic=True`（默认，v0.2 项4）：**穷举**所有 (N, arms, shape) 候选并
      取最优 —— 不掷骰子。所以同一份输入每次都给同一朵，而且**与调用顺序无关**
      （旧写法共用一个 RNG，同一段长度在不同调用次序下会得到不同雪花）。

    `deterministic=False` 保留旧的随机采样路径（`trials` 次），供 A/B 对比。
    """
    grid = candidate_grid(length, n_choices=n_choices, min_arms=min_arms)
    pool = _pool_of(grid)
    if not pool:
        return None

    mins = float(travel_min or 0.0)
    key = (lambda s: richness(s) + (-bbox_of(s)[2],)) if compact else richness

    if deterministic:
        best: Snowflake | None = None
        best_key = None
        shape_list = tuple(shapes) if shapes else SHAPES_DETERMINISTIC
        if "random" in shape_list:                  # 确定性路径不支持随机形状
            shape_list = tuple(s for s in shape_list if s != "random") or \
                SHAPES_DETERMINISTIC
        for _tiles, n, arms in pool:
            for shp in shape_list:
                s = Snowflake(n_rot=n, arms=arms, mirrored=False,
                              shape=shp, seed=0)
                if not _valid(s, mins):
                    continue
                k = key(s)
                if best_key is None or k > best_key:
                    best_key, best = k, s
        return best

    # ---- 旧路径：随机采样 ----
    r = rng or random.Random()
    shape_list = tuple(shapes) if shapes else SHAPES
    cands: list[Snowflake] = []
    for _ in range(max(1, trials)):
        _t, n, arms = r.choice(pool)
        s = Snowflake(n_rot=n, arms=arms,
                      mirrored=allow_mirror and r.random() < 0.5,
                      shape=r.choice(shape_list),
                      seed=r.randrange(1 << 30))
        if _valid(s, mins):
            cands.append(s)
    if not cands:
        return None
    return max(cands, key=key)


def should_use(length: int, min_tiles: int, full_tiles: float, seed: int) -> bool:
    """用户口径：10 格以内不用雪花，之后**权重递增直到 100%**。

    用确定性伪随机（按段起点做种子），所以同一份输入每次结果一样。
    """
    if length < min_tiles:
        return False
    if full_tiles <= min_tiles or length >= full_tiles:
        return True
    p = (length - min_tiles) / (full_tiles - min_tiles)
    h = (seed * 2654435761) % 1000003
    return (h / 1000003.0) < p
