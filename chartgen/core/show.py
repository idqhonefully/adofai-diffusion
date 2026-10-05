# -*- coding: utf-8 -*-
"""演出调度（⑤d）：**入场 / 离场 / 反向 QE / 轨道底噪**。

依据：`docs/62-演出招式库-实现清单.md`（**唯一依据**，含招式表、硬规则、自检清单）。
招式排练/验收谱的生成器是同源的 `tools/make_show_demo.py` —— **改招式表要两边同步**。

## 定稿模型（用户 2026-09 口径，逐字）

> 「让**用户自己选择预设的入场和出场效果**。允许**添加分段**，并且在当前生成的谱面里面
> **使用当前生成的谱面格子数定义**。否则，都只使用**预设的入场+出场**。特殊的，
> **三连音的写法中，三连音被特殊标出**。**不选择就自动用 QE 的写法**。」

⇒ 逐格判定的三条优先级（**高 → 低**）：

    ① 用户添加的分段（用**生成后谱面的格号** [lo, hi] 定义）
    ② 自动三连音段（`Floor.engine` 的连续 run，**自动标出**，默认走 QE）
    ③ 全局预设（`in_move` / `out_move`）

段内字段留空 = 跟随全局预设；填 `"none"` = 这一侧不上。

## 时序零影响

全部是 `MoveTrack` / `RecolorTrack` / `AnimateTrack`，**只写 `actions`**，
绝不碰 `angleData / bpm / travel / Twirl / settings` ⇒ 与 ⑤b 上色、⑤c 算法轨道调度同级。
（唯一例外：`required_beats_ahead` 让调用方把 `settings.beatsAhead` **抬高** —— 见下。）

## 单位口径（踩过，别改）

* `duration` = **拍**；`angleOffset` = **度**，`180 = 1 拍`（`−1440` = 提前 8 拍）。
* `startTile[n]` 用 `"ThisTile"` ⇒ 目标格 = `floor + n`；**入场写在前方 `lead` 格**。
* `MoveTrack` 的目标是**绝对坐标**（`startPos + positionOffset`），不是增量 ⇒
  「先甩出去再拉回来」必须两条（初态 + 回位）。
* ★ `beatsAhead` 必须覆盖入场的起跑点：`beatsAhead ≥ lead + margin`。
  `lead = 10` 时是 **12 拍**，而 `settings.beatsAhead` 默认只有 **8** ⇒
  **方块会在动画开始之后才出现**。`plan()` 把 `required_beats_ahead` 报出来，
  由 `writer` 取 `max(现值, required)`。
"""
from __future__ import annotations

from dataclasses import dataclass, field

# ============================================================ 招式表
#: 离场 4 招：写在 `f` 格，**踹走 `f−1`**（`span[-1,ThisTile]`，`angleOffset 0`）。
#: 依据 HQ 取证（`docs/62 §4.5C`）：真实谱的"甩走"前瞻中位就是 **`−1`**（2842/3938 次）。
#: `出B` 的 duration 已按用户「4拍子」改成 4 拍（原 1 拍在 HQ 里是少数派）。
MOVES_OUT: dict[str, dict] = {
    "出A": dict(dur=4.0, ease="InBack",  pos=(0, -2),   rot=-25, scale=(0, 0),   op=0.0),
    "出B": dict(dur=4.0, ease="OutQuad", pos=(0, 0),    rot=0,   scale=(20, 20), op=0.0),
    "出C": dict(dur=4.0, ease="InSine",  pos=(0, -1.5), rot=15,  scale=(95, 95), op=0.0),
    "出D": dict(dur=4.0, ease="InExpo",  pos=(-1, 6),   rot=30,  scale=(90, 90), op=0.0),
}

#: 入场 2 招：**初态 + 回位**两条写在**同一格**，目标 = `floor + lead`。
#: `lead` 由 `ShowParams.lead` 给（定稿 **10** 格；HQ 取证中位 +13）。
MOVES_IN: dict[str, dict] = {
    "入A": dict(dur=2.0, ease="InOutBack",
                init=dict(pos=(6, 4),  rot=90,  scale=(0, 0),   op=0.0)),
    "入B": dict(dur=2.0, ease="OutBack",
                init=dict(pos=(0, 2),  rot=90,  scale=(0, 0),   op=0.0)),
}

#: 反向 QE：① 总藏一条罩整段 ② 拉回**一条罩 `g` 格**，逐组推进（`docs/62 §1.3`）。
REV_QE: dict[str, dict] = {
    "逐格": dict(lead=4, dur=3.0, ease="OutElastic", group=1),
    "组2": dict(lead=4, dur=3.0, ease="OutElastic", group=2),
    "组3": dict(lead=4, dur=3.0, ease="OutElastic", group=3),
    "组4": dict(lead=4, dur=3.0, ease="OutElastic", group=4),
}
#: 总藏 / 拉回 的初态与终态（两档共用）
QE_HIDE = dict(pos=(0, -6), rot=0, scale=(0, 0), op=0.0)
QE_SHOW = dict(pos=(0, 0), rot=0, scale=(100, 100), op=100.0)

#: 三连音段默认用哪一档（用户：「三个三个轨道地出来」⇒ `g = 3`）
TRIPLET_QE = "组3"

NONE = "none"                     # 段内字段填这个 = 该侧不上
INIT_ANGLE = -1440.0              # 初态提前 8 拍（度）
ANIM_AHEAD_MIN = 8.0              # `settings.beatsAhead` 的现有默认（writer.build_json）

#: `MoveTrack` 的字段白名单（`docs/62 §5.1`）—— 多一个字段都是 bug
MV_KEYS = frozenset((
    "floor", "eventType", "startTile", "endTile", "gapLength", "duration",
    "positionOffset", "rotationOffset", "scale", "opacity", "angleOffset",
    "ease", "eventTag",
))
#: `AnimateTrack` 的字段白名单（`docs/62 §5.7`）
AN_KEYS = frozenset((
    "floor", "eventType", "trackAnimation", "beatsAhead",
    "trackDisappearAnimation", "beatsBehind",
))


# ============================================================ 数据
@dataclass
class ShowSegment:
    """一段覆盖（用**生成后谱面的格号**定义，闭区间）。

    `in_move` / `out_move` 留空 = **跟随全局预设**；填 `"none"` = 该侧不上。
    `qe` 非空 = 这一段走**反向 QE**（`in/out` 就都不用了）。
    """
    lo: int
    hi: int
    in_move: str = ""
    out_move: str = ""
    qe: str = ""
    why: str = ""                 # "user" | "triplet"（自动标出的三连音段）

    def covers(self, i: int) -> bool:
        return self.lo <= i <= self.hi


@dataclass
class ShowParams:
    """⑤d 的全部旋钮（与 `sidecar/schema.py` 的键一一对应）。"""
    enabled: bool = True
    out_move: str = "出A"          # 全局预设：出场
    in_move: str = "入A"           # 全局预设：入场
    lead: int = 10                 # 入场前瞻（格）—— 定稿 10
    margin: float = 4.0            # 入场余量（拍）⇒ `angleOffset = -180*margin`
    segments: tuple = ()           # (ShowSegment, ...) 用户分段
    triplet: str = "qe"            # "qe" | "inherit" | "none"
    qe_g: str = TRIPLET_QE         # 三连音段的 QE 档
    collide_floors: tuple = ()     # 与 ⑤b/⑤c 同格就避让（省得演出打架）

    def to_dict(self) -> dict:
        return {
            "enabled": bool(self.enabled),
            "out_move": str(self.out_move),
            "in_move": str(self.in_move),
            "lead": int(self.lead),
            "margin": float(self.margin),
            "segments": [{"lo": int(s.lo), "hi": int(s.hi),
                          "in_move": s.in_move, "out_move": s.out_move,
                          "qe": s.qe, "why": s.why} for s in self.segments],
            "triplet": str(self.triplet),
            "qe_g": str(self.qe_g),
            "collide_floors": sorted(int(x) for x in self.collide_floors),
        }


@dataclass
class ShowPlan:
    """演出方案（纯数据，可 JSON 化；不改 `Chart`）。"""
    events: list[dict] = field(default_factory=list)
    segments: list[ShowSegment] = field(default_factory=list)
    params: ShowParams = field(default_factory=ShowParams)
    required_beats_ahead: float = ANIM_AHEAD_MIN
    n_out: int = 0
    n_in: int = 0
    n_qe: int = 0
    n_collide: int = 0                       # 因与 ⑤b/⑤c 同格而跳过的格
    n_clamped: int = 0                       # 入场提前量不够、被压到 floor 0 的格
    skipped_why: list[str] = field(default_factory=list)

    @property
    def n_events(self) -> int:
        return len(self.events)

    @property
    def floors(self) -> list[int]:
        return sorted({int(e["floor"]) for e in self.events})

    def to_dict(self) -> dict:
        return {
            **self.params.to_dict(),
            "n_events": self.n_events,
            "n_out": int(self.n_out),
            "n_in": int(self.n_in),
            "n_qe": int(self.n_qe),
            "n_collide": int(self.n_collide),
            "n_clamped": int(self.n_clamped),
            "required_beats_ahead": float(self.required_beats_ahead),
            "floors": self.floors,
            "segments": [{"lo": int(s.lo), "hi": int(s.hi), "in_move": s.in_move,
                          "out_move": s.out_move, "qe": s.qe, "why": s.why}
                         for s in self.segments],
            "skipped_why": list(self.skipped_why),
        }

    def report_text(self) -> str:
        if not self.params.enabled:
            return ""
        if not self.events:
            return "演出：未启用或没有可写的格"
        bits = [f"演出：离场 {self.n_out} 条 · 入场 {self.n_in} 条"]
        if self.n_qe:
            bits.append(f"反向 QE {self.n_qe} 条")
        bits.append(f"beatsAhead ≥ {self.required_beats_ahead:g} 拍")
        why: list[str] = []
        if self.n_collide:
            why.append(f"与上色/轨道调度同格、避让 {self.n_collide} 格")
        if self.n_clamped:
            why.append(f"入场提前量不足、压到首格 {self.n_clamped} 格")
        if self.skipped_why:
            why.append(f"跳过 {len(self.skipped_why)} 处")
        return "；".join(bits) + ("；" + "，".join(why) if why else "")


# ============================================================ 小工具
def _num(v):
    """整数就写整数，否则 4 位小数（去掉浮点噪声，与 `writer._num` 同口径）。"""
    if v is None:
        return None
    f = float(v)
    return int(f) if abs(f - round(f)) < 1e-9 else round(f, 4)


def move_track(floor: int, s0: int, s1: int, *, dur: float, ease: str = "Linear",
               pos=None, rot=None, scale=None, op=None, angle: float = 0.0,
               gap: int = 0, tag: str = "") -> dict:
    """一条 `MoveTrack`（`ThisTile` 相对锚点）；**只写要生效的字段**（§3.1）。"""
    a: dict = {"floor": int(floor), "eventType": "MoveTrack",
               "startTile": [int(s0), "ThisTile"], "endTile": [int(s1), "ThisTile"],
               "gapLength": int(gap), "duration": _num(dur)}
    if pos is not None:
        a["positionOffset"] = [_num(pos[0]), _num(pos[1])]
    if rot is not None:
        a["rotationOffset"] = _num(rot)
    if scale is not None:
        a["scale"] = [_num(scale[0]), _num(scale[1])]
    if op is not None:
        a["opacity"] = _num(op)
    a["angleOffset"] = _num(angle)
    a["ease"] = ease
    a["eventTag"] = tag
    return a


def animate_track(floor: int, *, anim: str = "Fade", ahead: float = 8.0,
                  vanish: str = "Fade", behind: float = 4.0) -> dict:
    return {"floor": int(floor), "eventType": "AnimateTrack",
            "trackAnimation": anim, "beatsAhead": _num(ahead),
            "trackDisappearAnimation": vanish, "beatsBehind": _num(behind)}


def triplet_spans(ch, *, min_tiles: int = 1) -> list[tuple[int, int]]:
    """自动标出的三连音段 = `Floor.engine == True` 的连续 run（闭区间）。

    `engine` 是求解侧打的段标签（`core/solve.py` 的 `_apply_engine`），
    比重新按 `travel` 等分**准得多**（`docs/62 §4.3` D4）。
    """
    fs = getattr(ch, "floors", [])
    out: list[tuple[int, int]] = []
    i = 0
    n = len(fs)
    while i < n:
        if bool(getattr(fs[i], "engine", False)):
            j = i
            while j + 1 < n and bool(getattr(fs[j + 1], "engine", False)):
                j += 1
            if j - i + 1 >= int(min_tiles):
                out.append((i, j))
            i = j + 1
        else:
            i += 1
    return out


def resolve_segments(ch, params: ShowParams, *, n: int) -> list[ShowSegment]:
    """用户分段 + 自动三连音段 → **按优先级排好**的段列表（高优先级在前）。

    `triplet` 三种语义（`ShowParams.triplet`）：
      · `"qe"`（默认）  三连音段**自动标出**，整段走反向 QE（用户：「不选择就自动用 QE」）
      · `"inherit"`     不特别处理 ⇒ 三连音段也跟随全局预设
      · `"none"`        三连音段**整段不上演出**
    """
    segs: list[ShowSegment] = []
    for s in params.segments:
        lo, hi = int(s.lo), int(s.hi)
        if lo > hi:
            lo, hi = hi, lo
        segs.append(ShowSegment(lo=lo, hi=hi, in_move=s.in_move, out_move=s.out_move,
                                qe=s.qe, why="user"))
    mode = str(params.triplet)
    if mode in ("qe", "none"):
        for lo, hi in triplet_spans(ch):
            if mode == "qe":
                segs.append(ShowSegment(lo=lo, hi=hi, qe=(params.qe_g or TRIPLET_QE),
                                        why="triplet"))
            else:
                segs.append(ShowSegment(lo=lo, hi=hi, in_move=NONE, out_move=NONE,
                                        why="triplet"))
    return segs


def _decide(segs: list[ShowSegment], i: int, params: ShowParams,
            n: int) -> tuple[str, str, str, str]:
    """这一格用哪一侧的招 → `(out_move, in_move, qe, why)`。

    优先级：**用户段 > 三连音段 > 全局预设**（同优先级里"后来者优先"，
    并且在报告里记一笔重叠，不许静默）。
    """
    for s in segs:                                   # segs 已是"高优先级在前"
        if s.covers(i):
            if s.qe:
                return ("", "", s.qe, s.why)
            return (s.out_move or params.out_move,
                    s.in_move or params.in_move, "", s.why)
    return (params.out_move, params.in_move, "", "default")


# ============================================================ 主入口
def plan(ch, params: ShowParams | None = None, **kw) -> ShowPlan:
    """算出演出方案（**纯函数**，不改 `ch`）。

    调用方（`sidecar.session` 步骤 8d）把 `plan.events` 放进 `meta["show_events"]`，
    并把 `required_beats_ahead` 交给 `writer` 抬 `settings.beatsAhead`。
    """
    if params is None:
        params = ShowParams(**kw)
    elif kw:
        raise TypeError("plan(): 要么给 params，要么给关键字参数，别混用")
    n = len(getattr(ch, "floors", []))
    pl = ShowPlan(params=params)
    if not params.enabled or n <= 0:
        pl.skipped_why.append("⑤d 关掉或空谱面 ⇒ 一条事件都不写")
        return pl

    # ---- 参数校验（错了就报出来，不静默兜底）
    if params.out_move not in MOVES_OUT and params.out_move not in ("", NONE):
        pl.skipped_why.append(f"未知出场招 {params.out_move!r} ⇒ 出场整侧关闭")
        params.out_move = NONE
    if params.in_move not in MOVES_IN and params.in_move not in ("", NONE):
        pl.skipped_why.append(f"未知入场招 {params.in_move!r} ⇒ 入场整侧关闭")
        params.in_move = NONE
    pl.params = params

    segs = resolve_segments(ch, params, n=n)
    pl.segments = segs
    collide = {int(x) for x in params.collide_floors}
    lead = max(1, int(params.lead))
    # 入场提前量（拍）= lead + margin − 动画时长；两条事件里"起跑点"最早的是初态
    in_dur = max((MOVES_IN[m]["dur"] for m in MOVES_IN), default=0.0) \
        if params.in_move in MOVES_IN else 0.0
    pl.required_beats_ahead = max(ANIM_AHEAD_MIN, float(lead) + float(params.margin))

    seen_qe: set[tuple[int, int]] = set()
    for i in range(n):
        out_m, in_m, qe, why = _decide(segs, i, params, n)
        if i in collide:
            pl.n_collide += 1
            continue

        # ---------- ① 出场：踩到第 i 格时踹走第 i−1 格
        if out_m and out_m not in ("", NONE):
            t = i - 1
            if t < 0:
                pl.skipped_why.append(f"格 {i} 的出场目标 {t} < 0 ⇒ 跳过")
            else:
                m = MOVES_OUT[out_m]
                pl.events.append(move_track(
                    i, -1, -1, dur=m["dur"], ease=m["ease"], pos=m["pos"],
                    rot=m["rot"], scale=m["scale"], op=m["op"], angle=0.0,
                    tag=out_m))
                pl.n_out += 1

        # ---------- ② 入场：初态 + 回位两条写在**目标格前方 lead 格**
        if in_m and in_m not in ("", NONE):
            m = MOVES_IN[in_m]
            t = i                                   # 目标格
            f = t - lead                            # 触发格
            span = lead
            init_a, ret_a = INIT_ANGLE, -180.0 * float(params.margin)
            if f < 0:
                # ★ 提前量不够（谱面开头的 `lead` 格）⇒ 压到首格，并把两条的
                #   `angleOffset` 归零：**否则 tween 会落在"负时间"上**
                #   （事件时刻 = floor 0 的时刻 − margin/8 拍 < 0）⇒ 游戏里多半根本没生效，
                #   方块会直接以常态出现、白播一次动画。
                f, span = 0, t
                init_a = ret_a = 0.0
                pl.n_clamped += 1
            pl.events.append(move_track(
                f, span, span, dur=0.0, angle=init_a, ease="Linear",
                tag="in_init", **m["init"]))
            pl.events.append(move_track(
                f, span, span, dur=m["dur"], angle=ret_a, ease=m["ease"],
                pos=(0, 0), rot=0, scale=(100, 100), op=100, tag="in_ret"))
            pl.n_in += 1

        # ---------- ③ 反向 QE：整段行为，**每段只做一次**（在段首收口）
        if qe and qe not in ("", NONE):
            key = (i, i)
            # 找到这一格所属的 QE 段，只在**段首**展开整段
            seg = next((s for s in segs if s.covers(i) and s.qe == qe), None)
            if seg is None or i != seg.lo:
                continue
            if (seg.lo, seg.hi) in seen_qe:
                continue
            seen_qe.add((seg.lo, seg.hi))
            _emit_qe(pl, seg, qe, collide)

    pl.events.sort(key=lambda a: (int(a["floor"]), _order(a["eventType"]),
                                  a.get("angleOffset") or 0))
    return pl


def _emit_qe(pl: ShowPlan, seg: ShowSegment, qe: str, collide: set) -> None:
    """反向 QE：① 总藏罩整段 ② 拉回**一条罩 `g` 格**，逐组推进。

    ★ 偏移量**恒为 `lead`**（第 k 组写在 `段首 + k − lead` 格上）。写成 `lead + k`
    会每前进一格多偏一格 ⇒ **只命中一半方块，另一半永远看不见**（`docs/62 §1.3`）。
    """
    m = REV_QE.get(qe) or REV_QE[TRIPLET_QE]
    lead = int(m["lead"])
    lo, hi = seg.lo, seg.hi
    body = hi - lo + 1
    g = max(1, int(m["group"]))

    # ① 总藏：写在段首−lead（不够就压到 0），罩住整段
    f0 = lo - lead
    span0 = lead
    if f0 < 0:
        f0, span0 = 0, lo
    pl.events.append(move_track(
        f0, span0, span0 + body - 1, dur=0.0, angle=INIT_ANGLE, ease="Linear",
        pos=QE_HIDE["pos"], rot=QE_HIDE["rot"], scale=QE_HIDE["scale"],
        op=QE_HIDE["op"], tag="qe_hide"))
    pl.n_qe += 1

    k = 0
    while k < body:
        size = min(g, body - k)
        f = lo + k - lead
        if f < 0:
            f = 0
        pl.events.append(move_track(
            f, lead, lead + size - 1, dur=m["dur"], ease=m["ease"],
            pos=QE_SHOW["pos"], rot=QE_SHOW["rot"], scale=QE_SHOW["scale"],
            op=QE_SHOW["op"], angle=0.0, tag="qe_pull"))
        pl.n_qe += 1
        k += g


def _order(et: str) -> int:
    return {"SetSpeed": 0, "Pause": 0, "Twirl": 0, "AnimateTrack": 1,
            "MoveTrack": 2, "MoveCamera": 3, "Flash": 4, "ColorTrack": 5,
            "RecolorTrack": 6, "EditorComment": 9}.get(et, 7)


# ============================================================ 自检（docs/62 §5）
def check(pl: ShowPlan, ch) -> list[str]:
    """`docs/62 §5` 的断言 → 违规字符串列表（空 = 全绿）。

    1 字段白名单 · 2 范围不越界 · 3 `gapLength ≥ 0` · 4 不变量 ·
    5 **QE 覆盖完整性**（每格恰好命中一次）· 6 按 floor 有序 · 7 同格顺序。
    """
    bad: list[str] = []
    n = len(getattr(ch, "floors", []))
    for a in pl.events:
        et = a.get("eventType")
        if et == "MoveTrack":
            if not set(a) <= MV_KEYS:
                bad.append(f"floor {a.get('floor')} MoveTrack 多出字段 "
                           f"{sorted(set(a) - MV_KEYS)}")
            f = int(a["floor"])
            lo = f + int(a["startTile"][0])
            hi = f + int(a["endTile"][0])
            if lo > hi:
                bad.append(f"floor {f} startTile > endTile")
                continue
            if not (0 <= lo and hi < n):
                bad.append(f"floor {f} 作用范围越界 {lo}..{hi}（共 {n} 格）")
            if not (0 <= f < n):
                bad.append(f"floor {f} 触发格越界（共 {n} 格）")
            if int(a.get("gapLength", 0)) < 0:
                bad.append(f"floor {f} gapLength<0（步长变 0 ⇒ 死循环）")
            dur = float(a.get("duration") or 0)
            if lo > f:
                pass                                   # 入场：只碰未来 ✓
            elif hi <= f:
                pass                                   # 离场：不碰未来 ✓
            elif dur > 0:
                bad.append(f"floor {f} 带动画的事件跨过玩家：{lo}..{hi} 含 {f}")
        elif et == "AnimateTrack":
            if not set(a) <= AN_KEYS:
                bad.append(f"floor {a.get('floor')} AnimateTrack 多出字段 "
                           f"{sorted(set(a) - AN_KEYS)}")
        else:
            bad.append(f"演出事件里出现非法类型 {et!r}")

    fl = [int(a["floor"]) for a in pl.events]
    if fl != sorted(fl):
        bad.append("事件没有按 floor 有序")
    if pl.required_beats_ahead < pl.params.lead:
        bad.append(f"beatsAhead {pl.required_beats_ahead:g} < lead {pl.params.lead}"
                   f" ⇒ 方块会在动画之后才出现")

    # ★ 完整性：每个 QE 段的每一格必须被「拉回」命中**恰好一次**
    for seg in pl.segments:
        if not seg.qe or seg.qe in ("", NONE):
            continue
        hits: list[int] = []
        for a in pl.events:
            if a.get("eventTag") != "qe_pull":
                continue
            f = int(a["floor"])
            hi = f + int(a["endTile"][0])
            for t in range(f + int(a["startTile"][0]), hi + 1):
                if seg.lo <= t <= seg.hi:
                    hits.append(t)
        for t in range(seg.lo, seg.hi + 1):
            c = hits.count(t)
            if c == 0:
                bad.append(f"QE 段 {seg.lo}..{seg.hi}：格 {t} 没有任何『拉回』命中"
                           f"（会永远看不见）")
                break
            if c > 1:
                bad.append(f"QE 段 {seg.lo}..{seg.hi}：格 {t} 被『拉回』命中 {c} 次"
                           f"（会互相打断）")
                break

    # 同格顺序：入场初态(−1440) → 入场回位(−margin*180) → 演出动作(0)
    per: dict[int, list[float]] = {}
    for a in pl.events:
        if a.get("eventType") == "MoveTrack":
            per.setdefault(int(a["floor"]), []).append(float(a.get("angleOffset") or 0.0))
    for f, angs in sorted(per.items()):
        if angs != sorted(angs):
            bad.append(f"floor {f} 同格 MoveTrack 顺序错：angleOffset {angs}"
                       f"（应为递增：先入场后离场）")
    return bad


__all__ = ["MOVES_OUT", "MOVES_IN", "REV_QE", "TRIPLET_QE", "NONE",
           "ShowSegment", "ShowParams", "ShowPlan",
           "plan", "check", "triplet_spans", "resolve_segments",
           "move_track", "animate_track", "MV_KEYS", "AN_KEYS"]
