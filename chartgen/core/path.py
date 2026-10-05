# -*- coding: utf-8 -*-
"""几何底座：把「逐格 travel + Twirl 翻转」走成一条**路径**（朝向 / 坐标 / 转角）。

这是全项目**几何的唯一真源**。`core/solve.py::_apply()` 以前在这里内联算
`turn / heading / angle / x,y`，现在改成构造一个 `Path` 再写回 `Floor` ——
所以路径的算法只有这一处，回正（项1）、方向调度/轨道偏移（项2）、
时间戳模板（项4）、双押（项3）都从这里取数，不会各算一套。

口径（与游戏反编译、README 一致，改动前先看这里）
------------------------------------------------

    初始 heading = 90°          # 让第 0 格（travel=180）的 angleData[0] == 0
    ccw = False                 # Twirl 命中则翻转

    turn_i    = (180 − travel_i)   若 ccw     否则 (travel_i + 180)   # 归一化到 (−180, 180]
    heading  += turn_i
    angle_i   = (90 − heading_i) mod 360                                # 写进 angleData
    p_{i+1}   = p_i + R2 · u(heading_i)                                 # R2 = 2·planar_radius

要点：
- `heading_i` 是**第 i 格走出去**的朝向（= exitangle），位置推进用的是它。
- Twirl **不影响坐标**：它只改「绕这一格往哪边走」（方向符号 + 计时），
  终点仍是下一格坐标，位置完全由各格 heading 决定。
- `travel < 180 ⇔ turn < 0`（内圈 / 行星切角）；`travel > 180 ⇔ turn > 0`（外圈）。

回正 / 偏移用到的查询（项1、项2）
--------------------------------
- `heading_of(i)` / `deviation(i)`  → 第 i 格朝向相对最近正交主轴偏了多少度（「斜不斜」）
- `is_axis_aligned(i)`              → 是否贴着 0/90/180/270（正交）
- `position_of(i)` / `offset_of(i)` → 第 i 格绝对坐标 / 相对前一格的位移
- `diagonal_runs(...)`              → 找「一长段斜轨」（heading 持续偏主轴）
- `recent_centroid(i,k)`            → 最近 k 格局部质心（铺开 / 回正决策）
- `min_distance_recent(i,k)`        → 与最近 k 格的最小距离（重叠体检）
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

#: 正交主轴（度）。回正的目标就是让 heading 贴到这几个方向之一。
AXES: tuple[float, ...] = (0.0, 90.0, 180.0, 270.0, 360.0)

#: 「贴轴」容差（度）。heading 累计浮点漂移可达 1e-4 级，真实角度是 7.5° 的倍数，
#: 所以 1e-2 度以内一律算正交（既滤掉漂移，又远小于任何真实偏角）。
AXIS_EPS = 1e-2

#: 第 0 格（开局站位）恒为直线（travel = 180，angleData[0] = 0）。
FIRST_TRAVEL = 180.0

#: 默认初始朝向。90° → 第 0 格 angleData 正好是 0。
DEFAULT_HEADING0 = 90.0


def norm360(x: float) -> float:
    """归一化到 [0, 360)。"""
    v = x % 360.0
    return 0.0 if abs(v - 360.0) < 1e-12 else v


def norm180(x: float) -> float:
    """归一化到 (−180, 180]。"""
    return (x + 180.0) % 360.0 - 180.0


def turn_of(travel: float, ccw: bool) -> float:
    """单格转弯角（度），已归一化到 (−180, 180]。唯一真源的公式。"""
    t = (180.0 - travel) if ccw else (travel + 180.0)
    return norm180(t)


def angle_of(heading: float) -> float:
    """heading → angleData，按 `_apply` 的老口径收尾（0 而不是 360，6 位小数）。"""
    a = round((90.0 - (heading % 360.0)) % 360.0, 6)
    return 0.0 if abs(a - 360.0) < 1e-6 or abs(a) < 1e-6 else a


@dataclass
class Path:
    """一条谱面的几何路径。

    `points` 长度 = 格数 + 1（最后一个是尾层站位）；
    `headings` / `turns` / `angles` / `ccw` 长度 = 格数。
    """

    points: list[tuple[float, float]] = field(default_factory=list)
    headings: list[float] = field(default_factory=list)
    turns: list[float] = field(default_factory=list)
    angles: list[float] = field(default_factory=list)
    #: 该格**是否有 Twirl 事件**（= flips[i] and allow_twirl），写回 `Floor.twirl`
    twirl: list[bool] = field(default_factory=list)
    #: 走到该格时的**累积奇偶状态**（Twirl 翻转后的 ccw），决定 turn 的符号
    ccw: list[bool] = field(default_factory=list)
    radius: float = 1.0

    # ---------------- 构造 ----------------
    @classmethod
    def from_travels(
        cls,
        travels: list[float],
        *,
        flips: list[bool] | None = None,
        radius: float = 1.0,
        heading0: float = DEFAULT_HEADING0,
        allow_twirl: bool = True,
    ) -> "Path":
        """从逐格 `travel`（+ 可选 Twirl 翻转位）走出一条 Path。

        `flips[i]` 为真且 `allow_twirl` 时才翻转 ccw —— 与 `_apply` 同一口径。
        """
        n = len(travels)
        fl = list(flips) if flips is not None else [False] * n
        if len(fl) < n:
            fl += [False] * (n - len(fl))

        R2 = 2.0 * radius
        pts: list[tuple[float, float]] = [(0.0, 0.0)]
        headings: list[float] = []
        turns: list[float] = []
        angles: list[float] = []
        twirls: list[bool] = []
        ccws: list[bool] = []

        heading = float(heading0)
        ccw = False
        for i in range(n):
            has_twirl = bool(fl[i] and allow_twirl)
            if has_twirl:
                ccw = not ccw
            tv = travels[i]
            turn = turn_of(tv, ccw)
            heading = norm180(heading + turn)
            headings.append(heading % 360.0)
            turns.append(turn)
            angles.append(angle_of(heading))
            twirls.append(has_twirl)
            ccws.append(ccw)
            x, y = pts[-1]
            hx = math.cos(math.radians(heading))
            hy = math.sin(math.radians(heading))
            pts.append((x + R2 * hx, y + R2 * hy))

        return cls(points=pts, headings=headings, turns=turns,
                   angles=angles, twirl=twirls, ccw=ccws,
                   radius=float(radius))

    @classmethod
    def from_floors(cls, floors, flips, *, radius: float = 1.0,
                    allow_twirl: bool = True) -> "Path":
        """直接从 `Floor` 列表构造（读 `travel` = **有效 travel**）。

        ★ 反向写法的双押缺口**不需要**额外字段：只要喂**有效 travel** + 该格 Twirl，
          `turn = 180 − travel`（ccw=True）会自动把 angleData 写成补角
          （`a_i − a_{i−1} = travel − 180 = 180 − (360−travel)`）——
          正是「写入 360−t、parity 翻」的编码，见 `core/dp_angle`。
        """
        return cls.from_travels([f.travel for f in floors], flips=flips,
                                radius=radius, allow_twirl=allow_twirl)

    # ---------------- 基本量 ----------------
    @property
    def n(self) -> int:
        """格数。"""
        return len(self.headings)

    @property
    def start(self) -> tuple[float, float]:
        return self.points[0] if self.points else (0.0, 0.0)

    @property
    def end(self) -> tuple[float, float]:
        return self.points[-1] if self.points else (0.0, 0.0)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        """(minx, miny, maxx, maxy)。"""
        if not self.points:
            return (0.0, 0.0, 0.0, 0.0)
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return (min(xs), min(ys), max(xs), max(ys))

    @property
    def size(self) -> tuple[float, float]:
        """(宽, 高)。"""
        x0, y0, x1, y1 = self.bbox
        return (x1 - x0, y1 - y0)

    @property
    def center(self) -> tuple[float, float]:
        x0, y0, x1, y1 = self.bbox
        return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)

    # ---------------- 逐格查询 ----------------
    def position_of(self, i: int) -> tuple[float, float]:
        """第 i 格的**绝对全局坐标**。i == n 时是尾层站位。"""
        if not self.points:
            return (0.0, 0.0)
        return self.points[max(0, min(i, len(self.points) - 1))]

    def offset_of(self, i: int) -> tuple[float, float]:
        """第 i 格 → 第 i+1 格的**相对位移** (Δx, Δy)。"""
        if i < 0 or i + 1 >= len(self.points):
            return (0.0, 0.0)
        a, b = self.points[i], self.points[i + 1]
        return (b[0] - a[0], b[1] - a[1])

    def relative_of(self, i: int, origin: int = 0) -> tuple[float, float]:
        """第 i 格相对第 `origin` 格的位置。"""
        a = self.position_of(origin)
        b = self.position_of(i)
        return (b[0] - a[0], b[1] - a[1])

    def heading_of(self, i: int) -> float:
        """第 i 格走出去的朝向（度，[0, 360)）。"""
        if not self.headings:
            return 0.0
        return self.headings[max(0, min(i, len(self.headings) - 1))]

    # ---------------- 回正（项1）用 ----------------
    def deviation(self, i: int) -> float:
        """第 i 格朝向离**最近正交主轴**的角度差（度，0~45）。0 = 正交。

        ★ 抗浮点漂移：长谱上 heading 累计误差可达 1e-4 级（如 359.9996° 其实
          就是 0°）。真实角度永远是 7.5° 的倍数，所以把 `AXIS_EPS` 以内的
          偏差直接判成 0，避免把「正交」误报成「斜」。
        """
        h = self.heading_of(i)
        d = min(abs(norm180(h - a)) for a in AXES)
        d = min(d, 90.0 - d) if d > 45.0 else d
        return 0.0 if d <= AXIS_EPS else d

    def is_axis_aligned(self, i: int, tol: float = AXIS_EPS) -> bool:
        """第 i 格是否贴着正交主轴。"""
        return self.deviation(i) <= tol

    def diagonal_runs(self, theta: float = 20.0, min_run: int = 8,
                      start: int = 1) -> list[tuple[int, int]]:
        """找出「斜轨」游程：连续 `deviation > theta` 且长度 ≥ `min_run` 的段。

        返回 [(起始格, 格数), ...]。`start=1` 跳过开局站位（它恒为直线）。
        """
        out: list[tuple[int, int]] = []
        i = max(0, int(start))
        n = self.n
        while i < n:
            if self.deviation(i) > theta:
                j = i
                while j < n and self.deviation(j) > theta:
                    j += 1
                if j - i >= min_run:
                    out.append((i, j - i))
                i = j
            else:
                i += 1
        return out

    # ---------------- 方向调度 / 偏移（项2）用 ----------------
    def recent_centroid(self, i: int, k: int) -> tuple[float, float]:
        """第 [i−k, i] 格的局部质心（决定「往哪边铺」/回正方向时用）。"""
        lo = max(0, i - max(1, int(k)))
        pts = self.points[lo:i + 1] or [(0.0, 0.0)]
        return (sum(p[0] for p in pts) / len(pts),
                sum(p[1] for p in pts) / len(pts))

    def min_distance_recent(self, i: int, recent: int = 24,
                            exclude_adjacent: int = 1) -> float:
        """第 i 格与之前 `recent` 格中**非相邻**格的最小距离。"""
        if i <= 0 or not self.points:
            return float("inf")
        p = self.position_of(i)
        lo = max(0, i - max(1, int(recent)))
        dmin = float("inf")
        for j in range(lo, max(0, i - max(0, int(exclude_adjacent)))):
            q = self.points[j]
            d = math.hypot(p[0] - q[0], p[1] - q[1])
            if d < dmin:
                dmin = d
        return dmin

    def distances_to_recent(self, i: int, recent: int = 24,
                            exclude_adjacent: int = 1) -> list[float]:
        """第 i 格到之前若干格的距离列表（重叠直方图 / PositionTrack 决策用）。"""
        if i <= 0 or not self.points:
            return []
        p = self.position_of(i)
        lo = max(0, i - max(1, int(recent)))
        out = []
        for j in range(lo, max(0, i - max(0, int(exclude_adjacent)))):
            q = self.points[j]
            out.append(math.hypot(p[0] - q[0], p[1] - q[1]))
        return out

    # ---------------- 落回 Floor ----------------
    def commit_to(self, floors, write_twirl: bool = True) -> None:
        """把路径结果写回 `Floor`（turn / heading / angle / x / y，可选 twirl）。

        与 `_apply` 的老行为逐字对齐，包括 angle 的 6 位小数与 0/360 收尾。
        `write_twirl=True` 时写的是**该格是否有 Twirl 事件**（`Path.twirl`），
        与 `ccw`（累积奇偶）是两回事。
        """
        for i, f in enumerate(floors):
            if i >= self.n:
                break
            f.turn = self.turns[i]
            if write_twirl:
                f.twirl = bool(self.twirl[i])
            f.heading = self.headings[i]
            f.angle = self.angles[i]
            f.x, f.y = self.points[i]


def build_path(floors, flips=None, *, radius: float = 1.0,
               allow_twirl: bool = True, heading0: float = DEFAULT_HEADING0) -> Path:
    """便捷入口：从 `Floor` 列表构造一条 `Path`。

    `_apply()` 与项2 的轨道偏移都用它，保证「算法一处」。
    读 `travel`（= 有效 travel）；反向双押缺口靠**该格 Twirl** 自动写成补角。
    """
    n = len(floors)
    fl = list(flips) if flips is not None else [False] * n
    return Path.from_travels([f.travel for f in floors], flips=fl,
                             radius=radius, heading0=heading0,
                             allow_twirl=allow_twirl)
