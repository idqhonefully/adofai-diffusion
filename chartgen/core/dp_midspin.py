# -*- coding: utf-8 -*-
"""中旋双押插入：在已经求好的 `Chart` 上，按**另一条音轨**的音头插 [X, 999]。

为什么能插
----------
游戏里（`scrLevelMaker`）：

    angleData == 999  ⇒ exitangle = entryangle（不改方向）且 midSpin = true
    CalculateFloorEntryTimes 里 midSpin 的 travel = 0（瞬发）

于是 `[X, 999]` 插在第 f 格前面时：

    X   落第 f 格的**原位与原时刻**（它继承前一格的 travel）
    999 与第 f 格**同一瞬间**按下            ⇒ 双押
    第 f 格的 travel 从 `travel_f` 变成 `travel_f − s`

恒等式 `travel_X + travel_Y' ≡ travel_f (mod 360)` 保证**时间**不缠绕时零净偏移
（要求 `0 < s < travel_f`）。**s 可调**是双轨模式的关键：双押落点 =
`t_f + T(s)`，所以能把双押摆到底部两格之间的**任意**位置 —— 第二条轨的音头
一般都不在底部网格上。

Twirl 奇偶
----------
`_apply` 里 `if flips[i]: ccw = not ccw`，转角公式随之变成 `180 − travel`。
所以 pivot 要分两种情况：

    ccw = False : a_X = (a_{f-1} − 180 − s) mod 360
    ccw = True  : a_X = (a_{f-1} + 180 + s) mod 360

几何上是纯局部改动：X 与原第 f 格坐标**完全重合**，Y 是那个折返尖角，
而第 f 格之后的坐标与朝向**一个都没变**。所以只需要改插入的那两格 +
第 f 格的 travel，不用重排整张谱。
"""
from __future__ import annotations

import bisect
import math
from dataclasses import replace

from .solve import Chart, Floor

#: s 的下限（度）。s→0 会让 X 的 travel 退化，游戏按特判给 2 拍，所以兜一下
S_MIN = 0.25
#: 被 Pause 挤到同一格的多个双押，彼此至少错开这么多毫秒（否则等于同一瞬间）
SEP_MS = 30.0
#: 原格的 travel 被双押吃掉后不能低于这个值，否则就成回头方块了
MIN_TRAVEL = 15.0
MIDSPIN = 999.0


def norm360(x: float) -> float:
    v = x % 360.0
    return 0.0 if abs(v) < 1e-9 else v


def norm180(x: float) -> float:
    return (x + 180.0) % 360.0 - 180.0


def pivot_for(a_prev: float, s: float, ccw: bool) -> float:
    """插入格 X 的 angleData。"""
    return norm360((a_prev + 180.0 + s) if ccw
                   else (a_prev - 180.0 - s))


def twirl_parity(floors) -> list[bool]:
    """逐层的 ccw 状态（含该层自己的 Twirl），与 `_apply` 同序。"""
    out, ccw = [], False
    for f in floors:
        if f.twirl:
            ccw = not ccw
        out.append(ccw)
    return out


def plan(ch: Chart, targets_ms, *, s_min: float = S_MIN,
         travel_min: float = MIN_TRAVEL,
         report: dict | None = None) -> dict[int, list[float]]:
    """把目标时刻（`times_from_chart` 口径的相对毫秒）翻译成 {格下标: [s, ...]}。

    ★ 不允许插在 floor 0 前面 —— 那会顶掉 `angleData[0]`，破坏
      「开局那一格一定是直线 / angleData[0] == 0」这条硬规则。

    `travel_min` = 用户口径的「最小角度」：原格 travel 被双押吃掉后必须
    仍 ≥ `max(MIN_TRAVEL, travel_min)`，否则这个落点放弃（计入 dropped）。

    `report` 回填统计：
        dropped   落在谱面外、或同一格挤不下
        pause     目标落进某格的 **Pause 区间** —— 那段时间不属于 travel，
                  插不进去，改挂到「travel 末端」或「下一格开头」里更近的那个，
                  落点会偏（偏移量见 `pause_ms` 的最大值）
        pause_ms  这些被迫偏移的目标的最大偏移（ms）
    """
    from .solve import times_from_chart
    n = len(ch.floors)
    if n < 3:
        return {}
    t = times_from_chart(ch)
    tv = [f.travel for f in ch.floors]
    mpd = [(60000.0 / max(1e-9, f.bpm)) / 180.0 for f in ch.floors]
    keep = max(MIN_TRAVEL, float(travel_min or 0.0))

    assign: dict[int, list[float]] = {}
    used: dict[int, float] = {}
    dropped = pause = 0
    worst = 0.0
    for T in sorted(float(x) for x in targets_ms):
        k = bisect.bisect_right(t, T) - 1
        if k > n - 2:
            dropped += 1                 # 落在最后一格里：插了会破坏零净偏移
            continue
        k = max(1, k)                    # 第一格之前也不行（会顶掉 angleData[0]）
        # 该格能给出多少 travel 给双押：
        #   ① 至少留 s_min 给原格自己（否则 X 的 travel 退化）
        #   ② 原格 travel 不能被压到 15° 以下 —— 那就变成回头方块了
        limit = max(s_min, min(tv[k] - s_min, tv[k] - keep))
        S = (T - t[k]) / mpd[k]
        reanch = False
        if S > limit:
            # ── 目标落在 Pause 区间（那段时间不属于任何 travel）。两个锚点取更近的：
            #    A) 压回本格 travel 末端        → 落点 t_k + travel_k
            #    B) 挂到下一格开头（s = s_min） → 落点 t_{k+1} + s_min
            pause += 1
            e_a = T - (t[k] + tv[k] * mpd[k])
            if k + 1 <= n - 2:
                e_b = (t[k + 1] + s_min * mpd[k + 1]) - T
                if abs(e_b) < abs(e_a):
                    k, S, reanch = k + 1, s_min, True
                    limit = max(s_min, min(tv[k] - s_min, tv[k] - keep))
                    worst = max(worst, abs(e_b))
                else:
                    S = max(s_min, limit)
                    worst = max(worst, abs(e_a))
            else:
                S = max(s_min, limit)
                worst = max(worst, abs(e_a))
        prev = used.get(k)
        if prev is None:
            S = max(S, s_min)
        else:
            # 同一格多个目标必须严格递增；被 Pause 挤过来的那些按时间间隔错开，
            # 否则它们会全叠在同一个 s 上被互相挤掉。
            need = max(s_min, SEP_MS / mpd[k]) if reanch else s_min
            if S < prev + need:
                S = prev + need
        if S > limit:
            dropped += 1
            continue
        assign.setdefault(k, []).append(S)
        used[k] = S

    out: dict[int, list[float]] = {}
    for k, vs in assign.items():
        prev, ss = 0.0, []
        for S in vs:                      # T 递增 ⇒ vs 已递增
            ss.append(S - prev)
            prev = S
        if ss:
            out[k] = ss
    if report is not None:
        report.update(dropped=dropped, pause=pause, pause_ms=worst)
    return out


def apply(ch: Chart, insert_plan: dict[int, list[float]], *,
          planar_radius: float = 1.0) -> int:
    """就地插入。返回插入的**对数**。

    顺带在 `ch.meta` 里记两样东西（预览/调试要用）：
        `dp_pairs`    = [(折返格下标, 999 下标, 原格新下标), ...]
        `dp_old2new`  = {原格下标: 新下标}
    """
    plan_ = {k: v for k, v in (insert_plan or {}).items() if v}
    if not plan_:
        ch.meta["dp_pairs"] = []
        ch.meta["dp_old2new"] = {i: i for i in range(len(ch.floors))}
        return 0
    floors = ch.floors
    n = len(floors)
    par = twirl_parity(floors)
    # 相邻格中心距 = 2 × planar_radius（与 solve._apply 的 R2 同尺度）
    step = 2.0 * planar_radius

    new: list[Floor] = []
    old2new: dict[int, int] = {}
    pairs: list[tuple[int, int, int]] = []
    base_travel: dict[int, float] = {}
    n_pairs = 0
    for i in range(n):
        ss = plan_.get(i)
        if ss:
            c = par[i - 1] if i > 0 else False
            f0 = floors[i]
            h = norm360(90.0 - (floors[i - 1].angle if i > 0 else 0.0))
            x, y = f0.x, f0.y                # 先从原第 f 格位置起步
            for s in ss:
                turn_x = norm180((180.0 - s) if c else (s + 180.0))
                h_x = norm360(h + turn_x)
                a_x = norm360(90.0 - h_x)
                i_x = len(new)
                new.append(Floor(travel=float(s), bpm=f0.bpm, twirl=False,
                                 turn=turn_x, heading=h_x, angle=a_x,
                                 speed_k=f0.speed_k, pause_beats=0.0,
                                 x=x, y=y, snowflake=False, snowflake_id=-1))
                x += step * math.cos(math.radians(h_x))
                y += step * math.sin(math.radians(h_x))
                turn_y = 180.0
                h_y = norm360(h_x + turn_y)
                i_y = len(new)
                new.append(Floor(travel=0.0, bpm=f0.bpm, twirl=False,
                                 turn=turn_y, heading=h_y, angle=MIDSPIN,
                                 speed_k=f0.speed_k, pause_beats=0.0,
                                 x=x, y=y, snowflake=False, snowflake_id=-1))
                x += step * math.cos(math.radians(h_y))
                y += step * math.sin(math.radians(h_y))
                h = h_y
                n_pairs += 1
                pairs.append((i_x, i_y, -1))     # 第三个稍后回填
            old2new[i] = len(new)
            for j in range(len(ss)):
                a, b, _ = pairs[-len(ss) + j]
                pairs[-len(ss) + j] = (a, b, len(new))
            # ★ 主音 onset 压在哪一格上 = **第一个折返格 X**（`pairs[-len(ss)][0]`）。
            #   以前指向「被吃掉 Σs 的原格」，那一格的 entry 是 `t_k + Σs·mpd`
            #   ⇒ 每个主音都**偏晚** Σs·mpd（实测最多 +1.8ms，不累积但确实偏）。
            #   `docs/16` 的那套 angleOffset / Pause 回正**不管这个**（它是映射问题，
            #   不是时长问题）；指到 X 就是精确 0.000ms
            #   （见 `tools/_dp_offset_audit.py`，三首样本全部归零）。
            old2new[i] = pairs[-len(ss)][0]
            # 原格的 travel 会被吃掉 Σs（≈0.25°/处）。记下原值，
            # 统计直线率/音值分布时用它还原 —— 否则插几百个双押之后
            # 「直线率」会被 179.75° 这种无关紧要的扰动拉垮。
            base_travel[len(new)] = f0.travel
            new.append(replace(f0, travel=f0.travel - sum(ss)))
        else:
            old2new[i] = len(new)
            new.append(floors[i])
    ch.floors = new
    ch.meta["dp_pairs"] = pairs
    ch.meta["dp_old2new"] = old2new
    ch.meta["dp_base_travel"] = base_travel

    # meta 里按层号索引的数据要跟着平移
    pt = ch.meta.get("pos_tracks")
    if pt:
        ch.meta["pos_tracks"] = [(old2new.get(int(fi), fi), dx, dy)
                                 for fi, dx, dy in pt]
    return n_pairs


def stats(ch: Chart) -> dict:
    """自检用：统计中旋双押的结构与时序。"""
    from .solve import times_from_chart
    t = times_from_chart(ch)
    floors = ch.floors
    n999 = sum(1 for f in floors if abs(f.angle - MIDSPIN) < 1e-9)
    pairs = 0
    ok_sim = ok_pos = 0
    for i, f in enumerate(floors):
        if abs(f.angle - MIDSPIN) >= 1e-9 or i + 1 >= len(floors):
            continue
        pairs += 1
        if abs(t[i + 1] - t[i]) < 0.05:
            ok_sim += 1
        # 折返格（i-1）与 999 的下一格坐标重合
        if i >= 1 and math.dist((floors[i - 1].x, floors[i - 1].y),
                                (floors[i + 1].x, floors[i + 1].y)) < 1e-6:
            ok_pos += 1
    return dict(n999=n999, pairs=pairs, ok_sim=ok_sim, ok_pos=ok_pos,
                total_ms=t[-1] if t else 0.0, floors=len(floors))
