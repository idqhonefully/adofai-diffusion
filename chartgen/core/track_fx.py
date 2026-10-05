"""PositionTrack：给**重叠的闭合图形**加"这是第几圈"的视觉提示。

用户口径：**单个格子错开**最直观 —— 一眼能数出循环了几遍，而且不费眼。
（`MoveTrack` 整段错开反而看不出圈数，所以不用。）

做法 —— **完全不碰 angleData / bpm / travel / Twirl，时序零影响**：

  ① 找「同位置」的格子：非相邻（间隔 ≥2）、距离 < `radius` 格
  ② 同一组内按出现次序，第 k 次出现（k≥1）错开 `k·step` 格
  ③ 写成 `PositionTrack`（只动渲染，几何与判定都不变）

★ 魔法阵（雪花）排除在外：它**故意**原路折返，本身就是设计，不需要提示。

v0.2（项2）：坐标与朝向不再从 `f.x/f.y/angle` 自己算，改为统一走 `core/path.py`
的 `Path`（几何唯一真源）；错位方向升级成**方向调度** —— 在若干候选方向里，
选「错位之后离周围其它方块最远」的那个，避免"错开一下结果又撞上别的块"。

    候选方向 = 组中心径向  ⊕  ±45° / ±90° / 反向
    打分     = 错位点到窗口内其它方块的最小距离（越大越好）
    tie-break = 优先径向（保持"由中心向外"的直觉读法）
"""
from __future__ import annotations

import math

from .path import Path

LOOP_RADIUS = 0.35      # 判定「同位置」的距离，单位：格
LOOP_STEP = 0.22        # 每多一圈错开多少格
LOOP_RECENT = 32        # 只在这么大的窗口里找重合
LOOP_MAX = 8            # 最多提示到第几圈（再多也数不清了）
LOOP_MIN_BEATS = 8.0    # ★ 只有**间隔超过这么多拍**的重合才错开

#: 方向调度的候选偏转角（度）。0 = 径向（保持旧读法），其余用来避让。
CANDIDATE_ANGLES: tuple[float, ...] = (0.0, 45.0, -45.0, 90.0, -90.0, 180.0)


def path_of(ch) -> Path:
    """从 `Chart` 拿一条 `Path`（几何唯一真源）。

    不做持久缓存 —— 雪花覆盖 / 双押插入都会改几何，缓存一旦失效就会算错，
    而构造一条 Path 只是 O(n)，很便宜。
    """
    return Path.from_floors(
        ch.floors,
        [bool(getattr(f, "twirl", False)) for f in ch.floors],
        radius=1.0)


def _unit_of(ch) -> float:
    """一格在坐标系里的长度（= 2·planar_radius）。从相邻格距反推，别写死。"""
    pts = [(f.x, f.y) for f in ch.floors[:6]]
    ds = [math.hypot(pts[i + 1][0] - pts[i][0], pts[i + 1][1] - pts[i][1])
          for i in range(len(pts) - 1)]
    ds = [d for d in ds if d > 1e-9]
    return (sum(ds) / len(ds)) if ds else 2.0


def beat_positions(ch) -> list[float]:
    """每层到达时刻，单位：基准 BPM 的拍。

    `f.bpm = base / k`，所以该层在基准拍上的时长
      = (travel/180 + pause_beats) × (base / bpm) = (travel/180 + pause_beats) × k
    """
    out = [0.0]
    t = 0.0
    for f in ch.floors[:-1]:
        t += (f.travel / 180.0 + getattr(f, "pause_beats", 0.0)) * (f.speed_k or 1.0)
        out.append(t)
    return out


def find_loop_groups(ch, *, radius: float = LOOP_RADIUS,
                     recent: int = LOOP_RECENT,
                     min_beats: float = LOOP_MIN_BEATS) -> list[list[int]]:
    """把「落在同一位置的格子」聚成组，返回 [[floor, floor, ...], ...]（长度 ≥2）。

    ★ `min_beats`：只有**时间上隔得够远**（> min_beats 拍）的重合才收。
      因为轨道出现动画会提前 `beatsAhead`（默认 8）拍播放，8 拍以内的重合
      玩家本来就靠"出现先后"分得清，不需要再错位 —— 错位了反而多余。
    """
    fs = ch.floors
    pth = path_of(ch)
    pts = [pth.position_of(i) for i in range(len(fs))]
    U = _unit_of(ch)
    lim = radius * U
    bp = beat_positions(ch)
    used: set[int] = set()
    groups: list[list[int]] = []
    for j in range(len(pts)):
        if j in used or getattr(fs[j], "snowflake", False):
            continue
        g = [j]
        for i in range(max(0, j - recent), j - 1):
            if i in used or getattr(fs[i], "snowflake", False):
                continue
            if abs(bp[j] - bp[i]) <= min_beats:
                continue
            if math.hypot(pts[j][0] - pts[i][0], pts[j][1] - pts[i][1]) < lim:
                g.append(i)
                used.add(i)
        if len(g) > 1:
            used.add(j)
            groups.append(sorted(g))
    return groups


def _clearance(px: float, py: float, pts, lo: int, hi: int,
               skip: set[int]) -> float:
    """错位点 (px,py) 到 [lo,hi) 窗口内**其它**方块的最小距离（越大越好）。"""
    dmin = float("inf")
    for idx in range(lo, hi):
        if idx in skip:
            continue
        x, y = pts[idx]
        d = math.hypot(px - x, py - y)
        if d < dmin:
            dmin = d
    return dmin


def _candidates(radial: tuple[float, float], fallback: tuple[float, float]):
    """按 `CANDIDATE_ANGLES` 旋转径向单位向量，给出候选方向（首个 = 径向本身）。"""
    rx, ry = radial
    if math.hypot(rx, ry) < 1e-9:
        rx, ry = fallback
    out = []
    for a in CANDIDATE_ANGLES:
        r = math.radians(a)
        ca, sa = math.cos(r), math.sin(r)
        out.append((rx * ca - ry * sa, rx * sa + ry * ca))
    return out


def plan(ch, *, step: float = LOOP_STEP, radius: float = LOOP_RADIUS,
         recent: int = LOOP_RECENT, max_loops: int = LOOP_MAX,
         min_beats: float = LOOP_MIN_BEATS, schedule: bool = False):
    """→ [(floor, dx, dy), ...]，偏移量单位 = 格（ADOFAI 的 positionOffset 口径）。

    `schedule=True`：对每个错位点做方向调度（在候选方向里选净空最大的）。
    `schedule=False`（默认）：纯径向（由组中心向外）。

    ⚠ 方向调度**当前默认关闭**：三首样本实测无净收益（循环图形彼此孤立、
      没有可避让的邻居；净空 min/avg 完全一致）。它是为将来 magic 形状 /
      高密度重叠谱面留的开关，别默认开。
    """
    fs = ch.floors
    pth = path_of(ch)
    pts = [pth.position_of(i) for i in range(len(fs))]
    out: list[tuple[int, float, float]] = []
    for g in find_loop_groups(ch, radius=radius, recent=recent,
                              min_beats=min_beats):
        gs = set(g)
        cx = sum(pts[i][0] for i in g) / len(g)
        cy = sum(pts[i][1] for i in g) / len(g)
        for k, i in enumerate(g[1:], start=1):        # 第 1 个原地不动
            if k > max_loops:
                break
            x, y = pts[i]
            lo = max(0, i - recent)
            hi = min(len(pts), i + recent + 1)
            dx, dy = x - cx, y - cy
            n = math.hypot(dx, dy)
            if n < 1e-6:                              # 正好压在中心 → 用朝向法线
                h = math.radians(pth.heading_of(i))
                dx, dy, n = -math.sin(h), math.cos(h), 1.0
            radial = (dx / n, dy / n)

            best_dir, best_score = radial, -1.0
            if schedule:
                # ★ 评分=错位点到**除自己外所有格**的最小距离（含同组其它圈）——
                #   "既不要撞别人，也不要和同组的圈叠在一起"。
                fallback = (-math.sin(math.radians(pth.heading_of(i))),
                            math.cos(math.radians(pth.heading_of(i))))
                for cand in _candidates(radial, fallback):
                    px = x + cand[0] * step * k
                    py = y + cand[1] * step * k
                    sc = _clearance(px, py, pts, lo, hi, skip={i})
                    if sc > best_score + 1e-9:
                        best_score, best_dir = sc, cand
            out.append((i, best_dir[0] * step * k, best_dir[1] * step * k))
    out.sort()
    return out


def apply(ch, *, step: float = LOOP_STEP, radius: float = LOOP_RADIUS,
          recent: int = LOOP_RECENT, max_loops: int = LOOP_MAX,
          min_beats: float = LOOP_MIN_BEATS, schedule: bool = False) -> int:
    """算好偏移挂到 `ch.meta["pos_tracks"]`，返回事件条数。"""
    rows = plan(ch, step=step, radius=radius, recent=recent,
                max_loops=max_loops, min_beats=min_beats, schedule=schedule)
    ch.meta["pos_tracks"] = [(int(i), float(dx), float(dy)) for i, dx, dy in rows]
    ch.meta["pos_track_n"] = len(rows)
    return len(rows)


__all__ = ["find_loop_groups", "plan", "apply", "beat_positions", "path_of",
           "CANDIDATE_ANGLES", "LOOP_STEP", "LOOP_RADIUS", "LOOP_MIN_BEATS"]
