# -*- coding: utf-8 -*-
"""镜头调度：**国士无双式「呼吸」**（`docs/70` 是规格，本文是实现）。

用户 2026-10：

> 「**去正式接线**，镜头调度中，**已经被验证的国士无双式写法允许先接线**，
>   标记为「**（new）镜头调度**」，**目前只给出一个选项「呼吸」**。
>   **其他方案等待完全成熟后接入**。」

⇒ 本文只实现 **漂移（位置 + 旋转） + 呼吸（缩放）** 两层；
`docs/70` §6/§7 的**聚焦**模块（折弯循环段 / 结尾聚焦球）**不在这里** —— 它属于
「其他方案」，等成熟。

## 三层公式（`docs/70` §4 §5）

```
漂移·位置   P(u) = p₀ + [ Σᵢ Aˣᵢ·sin(2πu/Pˣᵢ + φˣᵢ),  Σⱼ Aʸⱼ·sin(2πu/Pʸⱼ + φʸⱼ) ]
漂移·旋转   R(u) =       Σₘ Aʳₘ·sin(2πu/Pʳₘ + φʳₘ)
呼吸·缩放   Z(u,k) = [ Z_m + A_z·sin(2πu/P_z + φ_z) ] · ( 1 + ε·(−1)^k )
```

网格：每 `S = 8` 拍一条、每条 `D = 16` 拍（`D = 2S` ⇒ **任意时刻都有补间在跑 ⇒ 永不静止**），
`ease = InOutSine`（零过冲），`relativeTo = "Player"`。

★★ **两处最容易做错的地方**（`docs/70` §3，实测踩过）：

| 量 | 采样点 | 为什么 |
|---|---|---|
| 漂移 / 呼吸 | 补间**中点** `u + D·τ/2000` | 一条补间代表它"走到一半时"的位置，链起来才平滑 |
| （聚焦权重） | **事件自己的格号** | 用中点会**提前 `D/2` 生效** |

## 跨谱面复用（`docs/70` §12）

公式里的 `Z_m = 220 / A_z = 45` 是**在 z₀ = 250 的谱面上标定的**，
所以本实现按 `zoom × z₀/250` 换算 ⇒ 保的是「相对基准位的观感」，不是绝对值。
位置（单位 = 格）**原样**、时值（单位 = 拍）**原样**。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

#: 漂移：每个轴 **两组正弦**（单周期会看出「摆动」，双周期才是「漂」）
DRIFT_X = ((13.0, 1.00, 0.00), (7.3, 0.50, 1.70))
DRIFT_Y = ((11.0, 0.80, 0.90), (6.1, 0.40, 2.40))
DRIFT_R = ((17.0, 4.0, 0.40), (9.0, 2.0, 2.10))

#: 标定基准（`docs/70` §12：这套数是在 z₀ = 250 的谱面上量出来的）
ZOOM_REF = 250.0

#: 只允许零过冲的 ease（用户：「少用甩尾运镜」）
SAFE_EASE = ("Linear", "InSine", "OutSine", "InOutSine",
             "InQuad", "OutQuad", "InOutQuad",
             "InCubic", "OutCubic", "InOutCubic",
             "InQuart", "OutQuart", "InOutQuart")

#: `mode` 的取值
MODE_OFF = 0
MODE_BREATH = 1
MODE_NAMES = {MODE_OFF: "关", MODE_BREATH: "呼吸"}


@dataclass
class CameraParams:
    """「（new）镜头调度」的生成时参数。**默认全关**（向后兼容）。"""

    mode: int = MODE_OFF
    # ---- 网格（`docs/70` §2）
    step_beats: float = 8.0        # S：每几拍一条
    dur_beats: float = 16.0        # D：每条时长（拍）
    # ---- 漂移（§4）
    drift_pos: bool = True
    drift_rot: bool = True
    pos_gain: float = 1.0          # 位置漂移总幅度倍率
    rot_gain: float = 1.0          # 旋转漂移总幅度倍率
    # ---- 呼吸（§5）
    breath: bool = True
    zoom_mid: float = 220.0        # Z_m（z₀=250 标定）
    zoom_amp: float = 45.0         # A_z：慢基线摆幅
    zoom_period_s: float = 15.0    # P_z：慢基线周期
    zoom_phase: float = 0.7        # φ_z
    breath_eps: float = 0.11       # ε：逐条交替的快呼吸
    # ---- 区间（格号；`to_floor = 0` ⇒ 到结尾）
    from_floor: int = 0
    to_floor: int = 0
    # ---- 兜底
    max_events: int = 4000         # 上限；撞了**报出来**

    def describe(self) -> str:
        if self.mode == MODE_OFF:
            return "镜头调度：关"
        return ("镜头调度：%s · 网格 %g 拍/条 · 时长 %g 拍 · 漂移 位置%s/旋转%s · "
                "呼吸 Z=%g±%g(P=%gs, 相位 %g) · ε=%g"
                % (MODE_NAMES.get(self.mode, self.mode), self.step_beats,
                   self.dur_beats, "开" if self.drift_pos else "关",
                   "开" if self.drift_rot else "关",
                   self.zoom_mid, self.zoom_amp, self.zoom_period_s,
                   self.zoom_phase, self.breath_eps))


@dataclass
class CameraMeta:
    ok: bool = True
    why: str = ""
    mode: int = MODE_OFF
    n_events: int = 0
    n_floor: int = 0
    f0: int = 0
    f1: int = 0
    span_s: float = 0.0
    max_gap_beats: float = 0.0     # 相邻两条之间最多隔几拍（≤ S 才叫「永不静止」）
    max_still_s: float = 0.0       # 位置完全静止的最长秒数（目标 0）
    zoom_lo: float = 0.0
    zoom_hi: float = 0.0
    pos_pp: float = 0.0            # 位置峰峰（格）
    rot_pp: float = 0.0            # 旋转峰峰（度）
    n_capped: int = 0
    text: str = ""

    def to_dict(self) -> dict:
        d = {k: (round(v, 6) if isinstance(v, float) else v)
             for k, v in self.__dict__.items()}
        d["text"] = self.report_text()
        return d

    def report_text(self) -> str:
        if self.mode == MODE_OFF or not self.n_events:
            return "镜头调度：关（%s）" % (self.why or "没开")
        out = ["镜头调度：%s ⇒ %d 条 MoveCamera · 覆盖格 %d~%d（%.1f s）"
               % (MODE_NAMES.get(self.mode, self.mode), self.n_events,
                  self.f0, self.f1, self.span_s),
               "  · 网格：%d 条之间最多隔 %.2f 拍（≤ 步长才叫「永不静止」）"
               % (self.n_events, self.max_gap_beats),
               "  · **位置完全静止最长 %.2f s**（目标 0）· 漂移峰峰 位置 %.2f 格 / 旋转 %.1f°"
               % (self.max_still_s, self.pos_pp, self.rot_pp),
               "  · 缩放 %g ~ %g（永不为 0 / null）" % (self.zoom_lo, self.zoom_hi)]
        if self.n_capped:
            out.append("  · ★ 撞到上限 ⇒ **截掉了 %d 条**" % self.n_capped)
        if not self.ok:
            out.append("  · ⚠ " + self.why)
        return "\n".join(out)


# ============================================================ 公式
def _wave(u: float, spec) -> float:
    """`Σ A·sin(2πu/P + φ)`。"""
    return sum(a * math.sin(2.0 * math.pi * u / p + ph) for p, a, ph in spec)


def drift_pos(u: float, gain: float = 1.0) -> tuple[float, float]:
    """漂移·位置（§4）。"""
    return (_wave(u, DRIFT_X) * gain, _wave(u, DRIFT_Y) * gain)


def drift_rot(u: float, gain: float = 1.0) -> float:
    """漂移·旋转（§4）。"""
    return _wave(u, DRIFT_R) * gain


def breath_zoom(u: float, k: int, p: CameraParams, zoom0: float) -> float:
    """呼吸·缩放（§5）。`zoom0` = 谱面基准 zoom（换算用 `docs/70` §12）。"""
    slow = p.zoom_mid + p.zoom_amp * math.sin(
        2.0 * math.pi * u / max(1e-9, p.zoom_period_s) + p.zoom_phase)
    z = slow * (1.0 + p.breath_eps * (1.0 if k % 2 == 0 else -1.0))
    return z * (zoom0 / ZOOM_REF)


# ============================================================ 落格
def plan(times_ms: list[float], params: CameraParams, *,
         base_bpm: float, pose: tuple[float, float, float],
         zoom0: float = ZOOM_REF) -> tuple[list, CameraMeta]:
    """按 `docs/70` §2 的网格铺一套「呼吸」镜头。

    `times_ms`：每格的**到达时刻**（`core/solve.times_from_chart` 那一套）。
    `pose`：谱面基准位 `(px, py, rot)`（`settings.position` / `settings.rotation`）。
    `zoom0`：谱面基准 `settings.zoom` —— **呼吸的缩放要按 `× z₀/250` 换算**（§12）。
    """
    meta = CameraMeta(mode=params.mode)
    if params.mode == MODE_OFF:
        meta.why = "mode=0（关）"
        return [], meta
    if params.mode != MODE_BREATH:
        meta.ok, meta.why = False, "mode=%r 还没有实现（目前只有「呼吸」）" % params.mode
        return [], meta
    if base_bpm <= 0 or len(times_ms) < 2:
        meta.ok, meta.why = False, "没有谱面时间轴（base_bpm=%g, %d 格）" % (
            base_bpm, len(times_ms))
        return [], meta

    tau = 60000.0 / float(base_bpm)
    S = max(0.5, float(params.step_beats))
    D = max(0.5, float(params.dur_beats))
    n = len(times_ms)
    f0 = max(0, min(int(params.from_floor), n - 1))
    f1 = n - 1 if int(params.to_floor) <= 0 else max(f0, min(int(params.to_floor), n - 1))
    t0 = float(times_ms[f0])
    px, py, rot0 = float(pose[0]), float(pose[1]), float(pose[2])
    z0 = float(zoom0) if zoom0 and float(zoom0) > 0 else ZOOM_REF

    events: list[dict] = []
    k = 0
    half_ms = D * tau / 2.0                      # ★ 中点偏移（§3）
    while True:
        t_k = t0 + k * S * tau
        if t_k > float(times_ms[f1]) + 1e-9:
            break
        if len(events) >= max(1, int(params.max_events)):
            meta.n_capped += 1
            k += 1
            continue
        # 落格：`max{ f : T[f] ≤ t_k }`
        lo, hi = f0, f1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if float(times_ms[mid]) <= t_k + 1e-9:
                lo = mid
            else:
                hi = mid - 1
        f_k = lo
        u = (t_k - t0) / 1000.0
        um = u + half_ms / 1000.0                # ★ 补间中点
        dx, dy = drift_pos(um, params.pos_gain) if params.drift_pos else (0.0, 0.0)
        dr = drift_rot(um, params.rot_gain) if params.drift_rot else 0.0
        z = breath_zoom(um, k, params, z0) if params.breath else z0
        events.append({
            "floor": int(f_k),
            "eventType": "MoveCamera",
            "duration": D,
            "relativeTo": "Player",
            "position": [round(px + dx, 6), round(py + dy, 6)],
            "rotation": round(rot0 + dr, 6),
            "zoom": round(z, 6),
            "angleOffset": 0.0,
            "ease": "InOutSine",
            "eventTag": "gs_breath",
        })
        k += 1

    meta.n_events = len(events)
    meta.f0, meta.f1 = f0, f1
    meta.n_floor = f1 - f0 + 1
    if events:
        meta.span_s = (float(times_ms[f1]) - t0) / 1000.0
        zooms = [e["zoom"] for e in events]
        meta.zoom_lo, meta.zoom_hi = min(zooms), max(zooms)
        fl = [e["floor"] for e in events]
        if len(fl) > 1:
            beats = [0.0]
            for a, b in zip(fl, fl[1:]):
                beats.append((float(times_ms[b]) - float(times_ms[a])) / tau)
            meta.max_gap_beats = max(beats) if beats else 0.0
        pos = [e["position"] for e in events]
        meta.pos_pp = max(max(p[0] for p in pos) - min(p[0] for p in pos),
                          max(p[1] for p in pos) - min(p[1] for p in pos))
        rots = [e["rotation"] for e in events]
        meta.rot_pp = max(rots) - min(rots)
        meta.max_still_s = longest_still(events, times_ms)
    if meta.n_capped:
        meta.ok = False
        meta.why = "撞到上限 %d ⇒ 截掉 %d 条" % (params.max_events, meta.n_capped)
    return events, meta


# ============================================================ 自检
def longest_still(events: list, times_ms: list[float]) -> float:
    """位置**完全静止**的最长秒数（`docs/70` §9 不变量 4，目标 0）。

    「静止」= 相邻两条事件的 `position` 逐字相同 ⇒ 这中间没有任何位移。
    """
    if len(events) < 2:
        return 0.0
    worst, run = 0.0, 0.0
    for a, b in zip(events, events[1:]):
        t_a, t_b = float(times_ms[a["floor"]]), float(times_ms[b["floor"]])
        if a["position"] == b["position"]:
            run += (t_b - t_a) / 1000.0
            worst = max(worst, run)
        else:
            run = 0.0
    return worst


def check_events(events: list, times_ms: list[float]) -> list[str]:
    """`docs/70` §9 的六条不变量。返回**违规说明**（空 = 全过）。"""
    bad: list[str] = []
    for i, e in enumerate(events):
        if e.get("eventType") != "MoveCamera":
            bad.append("#%d 不是 MoveCamera" % i)
        if e.get("zoom") in (None, 0, 0.0):
            bad.append("#%d zoom 是 %r（**永不为 null/0**）" % (i, e.get("zoom")))
        if e.get("position") is None or len(e["position"]) != 2 \
                or any(v is None for v in e["position"]):
            bad.append("#%d position 不完整" % i)
        if e.get("rotation") is None:
            bad.append("#%d rotation 是 None" % i)
        if float(e.get("duration") or 0) <= 0:
            bad.append("#%d duration=%r（**零时长 = 硬切**）" % (i, e.get("duration")))
        if e.get("ease") not in SAFE_EASE:
            bad.append("#%d ease=%r 有过冲风险" % (i, e.get("ease")))
    fl = [e["floor"] for e in events]
    if any(b <= a for a, b in zip(fl, fl[1:])):
        bad.append("落格不是严格递增")
    return bad
