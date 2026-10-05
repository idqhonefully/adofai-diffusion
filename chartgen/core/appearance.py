"""⑥ 算法轨道调度：**皮肤 + 涟漪环 + 半径调度**（`docs/60` 第一轮落地）。

用户口径（2026-10，`docs/60` §11）：
  · 驱动信号 = **谱面结构**（换手押 / 图形 / 密度），**不用** onset/音乐 ⇒ 不依赖采音质量
  · 皮肤 = **Neon 打底**（`settings` 层，0 条 action 就能摆脱"编辑器默认方块"）
  · 顺带做 `ScaleRadius`（轨道半径呼吸）；`Hide` **不做**（归"雪花"那个功能）
  · actions **不设总量上限**，但必须有"每格并行度 + 写了几条 + 跳过几条/为什么"的记账
  · ★ **⑤b（换手押上色）优先级更高**：同格不许打架

三条硬约束：
  1. **绝不碰** `angleData` / `bpm` / `travel` / `Twirl` / `pause_beats` ⇒ 时序零影响
  2. 只产出事件（`meta["appearance_events"]`）与 settings 覆盖（`meta["appearance_settings"]`）
  3. **不许静默**：任何跳过都要有原因，且进 `skipped_why` / `warnings`

## 为什么 `ScaleRadius` 能做到"改轨道观感但不改判定"

反编译实证（`scnGame.cs:1165` / `:636` / `:1250`）：
```
radiusScale = scale / 100                     # 从该格起继承（和 bpm 一样的持续状态）
zero += vec(entryangle) * (-(radiusScale-1)) * tileSize
floor.transform.position = floor.startPos + zero      # 只改**显示**坐标
```
⇒ 它把每块砖沿自己的行进方向推开 `(1-scale/100)` 格 ≈ **把整张谱的几何按出发点缩放**；
`startPos`（逻辑位置）不动 ⇒ **时序与判定完全不变**；星球位置取自 `floors[i].transform.position`
（`scnGame.cs:2241-2245`）⇒ 星球跟着走。
用户口径：**密集 → 摊开（125，见下），稀疏 → 100**。

## 涟漪环（`docs/60` §4.3 的配方，来自 Hello2025 一格 171 条的实测套路）

```
floor = 触发格 ; startTile=[-n] ; endTile=[+n] ; gapLength = 2n-1 ; angleOffset = 30n
```
`gapLength` 是**步长**（`TimelineManager.ts:177` `for (i = start; i <= end; i += 1 + gapLength)`），
所以步长 `2n` ⇒ 恰好只刷 ±n 两个点；`angleOffset` 单位是「180 = 1 拍」⇒ `30` = 每环晚 1/6 拍。
⚠ `n=0` 时 `2n-1 = -1` ⇒ 步长 0 ⇒ **游戏里死循环**，所以取 `max(0, 2n-1)`。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .track_fx import beat_positions

# --------------------------------------------------------------------- 常量

#: 官方皮肤（`TrackStyle`）。用户 2026-10：「先用 Neon 打底」。
SKINS: tuple[str, ...] = ("Standard", "Basic", "Neon", "NeonLight", "Minimal", "Gems")
DEFAULT_SKIN = "Neon"

#: 涟漪环每环延迟（度；180 = 1 拍）⇒ 30 = 1/6 拍。语料实测值。
RIPPLE_STEP_DEG = 30.0
#: 最多几环（`Hello2025` 实测上场峰值 85 环 / 同格 171 条；这里取保守默认）
RIPPLE_RINGS = 16
#: 同格并行度硬上限（防护，不是"预算"）—— 超了就截断并记账
MAX_PER_FLOOR = 128

#: 半径档（`ScaleRadius` = **行星距离**）。
#:
#: ★★ 用户 2026-10 定：**这个事件的最高值是 125**。
#:   > 「看看『行星距离』是哪个逻辑的事件，这个事件的**阈值并没有被正确设置**。
#:   >   这个事件**最高的阈值应该为 125**」
#:
#: ⇒ 从 250 收到 **125**，并且 `RADIUS_MAX` 是**硬上限**（越界会夹住并记账，不静默）。
#: 语料参考（`docs/60` §9，社区语料 530 份共 1517 条）：`150`×588 · `250`×462 · `100`×272 ·
#: `180`×74 —— **那是别人的用法**；用户口径是本项目的上限，冲突时**以用户口径为准**。
RADIUS_QUIET = 100
RADIUS_DENSE = 125
#: `ScaleRadius` 的**硬上限**（= 参考谱里「行星距离」能到的最远档）
RADIUS_MAX = 125
#: 硬下限（0 会把轨道缩成一个点）
RADIUS_MIN = 25

#: 密度窗口（**秒**）与分档阈值（单位：**音/秒**）。
#: ★★ 2026-10 实测纠正：第一版用「格/拍」（拍轴 = 基准 BPM 的拍）——**同一首音乐，
#:   基准 BPM 标成 122.5 还是 980，密度的数值差了 8 倍**（`beat_positions` 只吃
#:   `travel/speed_k`，不吃 `base_bpm`；我们这条流水线的 auto_bpm 会因密集音判到
#:   122.5×8 = 980）⇒ 阈值不再是音乐意义上的常量，实测在 ASGORE 60s 上做出了
#:   **4 格闪一下**（125 → 100 只隔 4 格）。
#:   改成 **音/秒**（从 onset 表直接数，纯实时、与 BPM 标注无关）——"手每秒要动几下"
#:   才是玩家真正感觉到的"密度"。
DENSITY_WINDOW_S = 2.0
DENSE_FPS = 8.0          # ≥ 8 音/秒 算"密"
QUIET_FPS = 5.0          # ≤ 5 音/秒 才算回"疏"（迟滞防抖）
#: 进入"密"之后至少维持多久 / 至少几格，才允许回落（★ 只有"拍"是不够的：
#: 980 BPM 下 4 拍 = 4 格，实测就闪了这一下）
RADIUS_MIN_SEC = 1.0
RADIUS_MIN_TILES = 8

#: `RecolorTrack` 字段**逐字对齐语料**（18 字段组合里的 17 个必填；`active` 可不写）
_RECOLOR_ORDER = ("floor", "eventType", "startTile", "endTile", "gapLength", "duration",
                  "trackColorType", "trackColor", "secondaryTrackColor",
                  "trackColorAnimDuration", "trackColorPulse", "trackPulseLength",
                  "trackStyle", "trackGlowIntensity", "angleOffset", "ease", "eventTag")

#: 图形段的来源（`Floor` 上的互斥标记，见 `core/solve.py:362-371`）
FIGURE_FLAGS: tuple[str, ...] = ("snowflake", "template", "natural", "engine")
_FIGURE_CN = {"snowflake": "雪花", "template": "模板", "natural": "自然闭合", "engine": "三连音"}


# --------------------------------------------------------------------- 皮肤

@dataclass
class Skin:
    """settings 层的轨道外观（L0 打底）。"""
    style: str = DEFAULT_SKIN
    color_type: str = "Glow"
    color: str = "ffffff"
    secondary: str = "ffffff"
    pulse: str = "Forward"
    pulse_len: int = 10
    glow: int = 100
    anim: float = 2.0

    def to_settings(self) -> dict:
        """覆盖 `writer.SETTINGS_TEMPLATE` 里对应的键（**只写这些**，别的一律不动）。"""
        return {
            "trackStyle": str(self.style),
            "trackColorType": str(self.color_type),
            "trackColor": str(self.color),
            "secondaryTrackColor": str(self.secondary),
            "trackColorPulse": str(self.pulse),
            "trackPulseLength": int(self.pulse_len),
            "trackGlowIntensity": int(self.glow),
            "trackColorAnimDuration": float(self.anim),
        }

    def _body(self, *, color: str, secondary: str, color_type: str | None = None,
              pulse: str | None = None, glow: int | None = None) -> dict:
        return {
            "trackColorType": str(color_type or self.color_type),
            "trackColor": str(color),
            "secondaryTrackColor": str(secondary),
            "trackColorAnimDuration": float(self.anim),
            "trackColorPulse": str(pulse if pulse is not None else self.pulse),
            "trackPulseLength": int(self.pulse_len),
            "trackStyle": str(self.style),
            "trackGlowIntensity": int(self.glow if glow is None else glow),
        }

    def recolor(self, floor: int, *, start: int, end: int, gap: int, duration: float,
                ease: str, angle_offset: float, color: str | None = None,
                secondary: str | None = None, color_type: str | None = None,
                pulse: str | None = None, glow: int | None = None) -> dict:
        """一条 `RecolorTrack`（字段顺序与语料一致）。"""
        ev = {"floor": int(floor), "eventType": "RecolorTrack",
              "startTile": [int(start), "ThisTile"], "endTile": [int(end), "ThisTile"],
              "gapLength": int(gap), "duration": float(duration)}
        ev.update(self._body(color=color or self.color,
                             secondary=secondary or self.secondary,
                             color_type=color_type, pulse=pulse, glow=glow))
        ev["angleOffset"] = float(angle_offset)
        ev["ease"] = str(ease)
        ev["eventTag"] = ""
        return {k: ev[k] for k in _RECOLOR_ORDER}


# --------------------------------------------------------------------- 方案

@dataclass
class Ripple:
    """一个涟漪环组（触发格 + 实际环数 + 为什么只给这么多环）。"""
    floor: int
    rings: int
    kind: str = ""
    why: str = ""


@dataclass
class RadiusSpan:
    """一段半径档（`floor` = 切换点，`scale` = 档位值，`fps` = 切换时的密度 音/秒）。"""
    floor: int
    scale: int
    fps: float

@dataclass
class AppearancePlan:
    """算法轨道调度方案（纯数据，可 JSON 化）。"""
    events: list[dict] = field(default_factory=list)
    settings: dict = field(default_factory=dict)
    ripples: list[Ripple] = field(default_factory=list)
    radius_spans: list[RadiusSpan] = field(default_factory=list)
    figure_spans: list[tuple[int, int]] = field(default_factory=list)
    n_collide: int = 0                       # 因与 ⑤b 同格而跳过的触发点
    n_capped: int = 0                        # 因同格并行度上限被截断的环
    n_overlap_occ: int = 0                   # 涟漪会扫过的 ⑤b 格数（记账，不跳过）
    skipped_why: list[str] = field(default_factory=list)
    enabled: bool = True
    skin: str = DEFAULT_SKIN
    ripple_on: bool = True
    radius_on: bool = True
    ripple_step: float = RIPPLE_STEP_DEG
    ripple_rings: int = RIPPLE_RINGS
    radius_quiet: int = RADIUS_QUIET
    radius_dense: int = RADIUS_DENSE
    dense_fps: float = DENSE_FPS
    quiet_fps: float = QUIET_FPS
    density_window: float = DENSITY_WINDOW_S

    # ------------------------------------------------------------- 查询
    @property
    def n_events(self) -> int:
        return len(self.events)

    @property
    def floors(self) -> list[int]:
        """事件落在哪些格上（前端段带用；同一格可能多条 ⇒ 去重后排序）。"""
        return sorted({int(e["floor"]) for e in self.events})

    @property
    def max_per_floor(self) -> int:
        c: dict[int, int] = {}
        for e in self.events:
            f = int(e["floor"])
            c[f] = c.get(f, 0) + 1
        return max(c.values()) if c else 0

    def to_dict(self) -> dict:
        return {
            "enabled": bool(self.enabled),
            "skin": str(self.skin),
            "settings": dict(self.settings),
            "ripple_on": bool(self.ripple_on),
            "radius_on": bool(self.radius_on),
            "n_events": self.n_events,
            "floors": self.floors,
            "n_figures": len(self.figure_spans),
            "figure_spans": [[int(a), int(b)] for a, b in self.figure_spans],
            "ripples": [{"floor": int(r.floor), "rings": int(r.rings),
                         "kind": r.kind, "why": r.why} for r in self.ripples],
            "n_ripple_events": sum(2 * (r.rings + 1) for r in self.ripples),
            "radius_spans": [{"floor": int(s.floor), "scale": int(s.scale),
                              "fps": round(float(s.fps), 3)} for s in self.radius_spans],
            "n_collide": int(self.n_collide),
            "n_capped": int(self.n_capped),
            "n_overlap_occupied": int(self.n_overlap_occ),
            "ripple_step": float(self.ripple_step),
            "ripple_rings": int(self.ripple_rings),
            "radius_quiet": int(self.radius_quiet),
            "radius_dense": int(self.radius_dense),
            "dense_fps": float(self.dense_fps),
            "quiet_fps": float(self.quiet_fps),
            "density_window": float(self.density_window),
            "max_per_floor": int(self.max_per_floor),
            "skipped_why": list(self.skipped_why),
        }

    def _skip_txt(self) -> str:
        out = []
        if self.n_collide:
            out.append(f"与换手押同格、避让 {self.n_collide} 个触发点")
        if self.n_capped:
            out.append(f"同格并行度上限 {MAX_PER_FLOOR} ⇒ 截断 {self.n_capped} 环")
        if self.n_overlap_occ:
            out.append(f"涟漪会短暂扫过 {self.n_overlap_occ} 个换手押格（刻意的时间差效果，不跳过）")
        return "；" + "，".join(out) if out else ""

    def report_text(self) -> str:
        if not self.enabled:
            return ""
        bits = [f"算法轨道调度：{self.skin} 皮肤"]
        if self.ripple_on:
            bits.append(f"涟漪环 {len(self.ripples)} 处 / {2 * sum(r.rings + 1 for r in self.ripples)} 条")
        if self.radius_on:
            bits.append(f"半径切换 {len(self.radius_spans)} 处")
        return "；".join(bits) + self._skip_txt()


# --------------------------------------------------------------------- 结构信号

def figure_spans(ch, *, sources: tuple[str, ...] = FIGURE_FLAGS,                 min_tiles: int = 2) -> list[tuple[int, int]]:
    """**图形段**（连续 True 的 run）→ [(起格, 止格)]（闭区间）。

    图形标记就是 `Floor` 上那四个互斥布尔（`core/solve.py:362-371`）：
    `snowflake`（雪花/魔法阵）/ `template`（模板）/ `natural`（自然闭合）/ `engine`（三连音）。
    用户口径：涟漪环打在**图形起点**（`docs/60` §12.2）。
    """
    src = tuple(sources) or FIGURE_FLAGS
    fs = ch.floors
    out: list[tuple[int, int]] = []
    i = 0
    n = len(fs)
    while i < n:
        if any(bool(getattr(fs[i], s, False)) for s in src):
            j = i
            while j + 1 < n and any(bool(getattr(fs[j + 1], s, False)) for s in src):
                j += 1
            if j - i + 1 >= int(min_tiles):
                out.append((i, j))
            i = j + 1
        else:
            i += 1
    return out


def figure_kind(ch, i: int, *, sources: tuple[str, ...] = FIGURE_FLAGS) -> str:
    """这一格属于哪种图形（人话，用于报告）。"""
    f = ch.floors[i] if 0 <= i < len(ch.floors) else None
    if f is None:
        return ""
    names = [_FIGURE_CN.get(s, s) for s in (sources or FIGURE_FLAGS)
             if bool(getattr(f, s, False))]
    return "+".join(names)


def density_fps(ch, *, window_s: float = DENSITY_WINDOW_S) -> list[float]:
    """逐格的**密度**（单位：**音/秒**；窗口 ±`window_s/2` 秒）。

    口径：用 `track_fx.beat_positions` 的拍轴换算成**秒**
    （`秒 = 拍 × 60 / base_bpm`）再数窗口内的音数，`(k−1)/秒跨`（**边到边**）。

    ★ 为什么不用「格/拍」：拍轴只吃 `travel/speed_k`，不吃 `base_bpm`；
      我们这条流水线会把同一首音乐标成很不一样的基准 BPM（ASGORE 实测 122.5 → 980），
      「格/拍」于是差了 8 倍 ⇒ 阈值失去意义、实测闪出 4 格。
      **音/秒是纯实时量**：与 BPM 标注无关、与曲子无关，"手每秒动几下"就是它。
    """
    bp = beat_positions(ch)
    n = len(bp)
    if n == 0:
        return []
    base = float(getattr(ch, "base_bpm", 0.0) or 0.0)
    if base <= 1e-9:
        base = 180.0
    sec = [b * 60.0 / base for b in bp]                  # 拍 → 秒
    half = float(window_s) / 2.0
    out: list[float] = []
    lo = 0
    hi = 0
    for i in range(n):
        while lo < i and sec[i] - sec[lo] > half:
            lo += 1
        if hi < i:
            hi = i
        while hi + 1 < n and sec[hi + 1] - sec[i] <= half:
            hi += 1
        span = sec[hi] - sec[lo]
        k = hi - lo + 1
        # span = 0 只可能出现在"整段同一时刻"的退化段；退回"格数"当代理值。
        out.append(((k - 1) / span) if span > 1e-9 else float(k))
    return out


def radius_switch_points(ch, *, dense_fps: float = DENSE_FPS, quiet_fps: float = QUIET_FPS,
                         window_s: float = DENSITY_WINDOW_S,
                         min_sec: float = RADIUS_MIN_SEC,
                         min_tiles: int = RADIUS_MIN_TILES,
                         quiet: int = RADIUS_QUIET,
                         dense: int = RADIUS_DENSE) -> list[RadiusSpan]:
    """密度（音/秒）→ 半径档的**切换点**（只返回切换处，持续状态靠"继承"）。

    状态机（迟滞 + **双**最短驻留，防抖）：
      · 初始 quiet（= 默认半径，**不写事件**）
      · `fps >= dense_fps` ⇒ 切 dense（写一条 `scale=dense`）
      · `fps <= quiet_fps` 且已驻留 **≥min_sec 秒 且 ≥min_tiles 格** ⇒ 切回 quiet

    ★★ 两个档都会先夹进 `[RADIUS_MIN, RADIUS_MAX]`（用户 2026-10：
      「行星距离**最高的阈值应该为 125**」）。**夹了就在 `radius_clamped` 里记账**，
      由上层打进报告 —— 不许静默。
    """
    dense = max(RADIUS_MIN, min(RADIUS_MAX, int(dense)))
    quiet = max(RADIUS_MIN, min(RADIUS_MAX, int(quiet)))
    fps = density_fps(ch, window_s=window_s)
    bp = beat_positions(ch)
    base = float(getattr(ch, "base_bpm", 0.0) or 0.0) or 180.0
    sec = [b * 60.0 / base for b in bp]
    out: list[RadiusSpan] = []
    state = "quiet"
    at_sec = -1e9
    at_i = -10 ** 9
    for i, d in enumerate(fps):
        if state == "quiet":
            if d >= dense_fps:
                state = "dense"
                at_sec = sec[i]
                at_i = i
                out.append(RadiusSpan(floor=i, scale=int(dense), fps=float(d)))
        else:
            if (d <= quiet_fps and (sec[i] - at_sec) >= float(min_sec)
                    and (i - at_i) >= int(min_tiles)):
                state = "quiet"
                at_sec = sec[i]
                at_i = i
                out.append(RadiusSpan(floor=i, scale=int(quiet), fps=float(d)))
    return out


# --------------------------------------------------------------------- 主入口

def plan(ch, *, enabled: bool = True,
         skin_style: str = DEFAULT_SKIN, skin_color: str = "ffffff",
         skin_color_type: str = "Glow", skin_pulse: str = "Forward",
         skin_pulse_len: int = 10, skin_glow: int = 100,
         ripple: bool = True, ripple_rings: int = RIPPLE_RINGS,
         ripple_step: float = RIPPLE_STEP_DEG,
         ripple_sources: tuple[str, ...] = FIGURE_FLAGS,
         radius: bool = True, radius_quiet: int = RADIUS_QUIET,
         radius_dense: int = RADIUS_DENSE,
         dense_fps: float = DENSE_FPS, quiet_fps: float = QUIET_FPS,
         density_window: float = DENSITY_WINDOW_S,
         min_sec: float = RADIUS_MIN_SEC, min_tiles: int = RADIUS_MIN_TILES,
         occupied: set[int] | frozenset[int] | None = None,
         max_per_floor: int = MAX_PER_FLOOR) -> AppearancePlan:
    """算好整份"算法轨道调度"方案（**只读 chart，产出纯数据**）。

    `occupied` = **⑤b 已占用的格**（换手押格）—— 用户口径「⑤b 优先」：
      · 触发点与 ⑤b **同格** ⇒ 跳过该触发点（避免同格 `ColorTrack`/`RecolorTrack` 打架），记账
      · 涟漪**扫过** ⑤b 格 ⇒ 不跳过（那是刻意的"能量扫过"效果），但记数进报告
    """
    pl = AppearancePlan(
        enabled=bool(enabled), skin=str(skin_style),
        ripple_on=bool(ripple), radius_on=bool(radius),
        ripple_step=float(ripple_step), ripple_rings=int(ripple_rings),
        radius_quiet=int(radius_quiet), radius_dense=int(radius_dense),
        dense_fps=float(dense_fps), quiet_fps=float(quiet_fps),
        density_window=float(density_window))
    sk = Skin(style=str(skin_style), color=str(skin_color),
              color_type=str(skin_color_type), pulse=str(skin_pulse),
              pulse_len=int(skin_pulse_len), glow=int(skin_glow))
    if not pl.enabled:
        # ★ 关掉 ⇒ `settings` 留空 ⇒ 导出与"没有这个功能"时**逐字节一致**
        #   （用户口径：关就是真关，不许偷偷改皮肤）。
        return pl
    pl.settings = sk.to_settings()

    occ = set(int(i) for i in (occupied or ()))
    n = len(ch.floors)
    per: dict[int, int] = {}

    def put(ev: dict, *, tag: str) -> bool:
        """压入一条事件；同格并行度超上限 ⇒ 拒绝并记账。"""
        f = int(ev["floor"])
        if f < 0 or f >= n:
            pl.skipped_why.append(f"{tag}：floor {f} 越界（共 {n} 层），整条丢弃")
            return False
        if per.get(f, 0) >= int(max_per_floor):
            pl.n_capped += 1
            return False
        pl.events.append(ev)
        per[f] = per.get(f, 0) + 1
        return True

    # ---------------------------------------------------------- 涟漪环
    if pl.ripple_on:
        spans = figure_spans(ch, sources=tuple(ripple_sources))
        pl.figure_spans = spans
        if not spans:
            pl.skipped_why.append("涟漪环：没有找到图形段（雪花/模板/自然/三连音 四个标记全空）")
        for a, b in spans:
            kind = figure_kind(ch, a, sources=tuple(ripple_sources))
            if a in occ:
                pl.n_collide += 1
                pl.skipped_why.append(
                    f"涟漪环：格 {a}（{kind}起点）与换手押上色同格 ⇒ 避让（⑤b 优先）")
                continue
            # 环数上限：① 设置值 ② 不越过谱首/谱尾（负下标会被游戏忽略，`TimelineManager.ts:178`）
            cap = min(int(ripple_rings), int(a), int(n - 1 - a))
            why = ""
            if cap < int(ripple_rings):
                why = f"受谱面边界限制（触发格 {a}，共 {n} 层）"
            if cap < 0:
                cap = 0
            rp = Ripple(floor=int(a), rings=int(cap), kind=kind, why=why)
            wrote = 0
            for k in range(cap + 1):
                gap = max(0, 2 * k - 1)               # ★ n=0 时必须是 0，否则步长 0 死循环
                ao = float(ripple_step) * k
                flash = sk.recolor(a, start=-k, end=k, gap=gap, duration=0.0,
                                   ease="Linear", angle_offset=ao,
                                   color="ffffff", secondary="ffffff",
                                   color_type="Single", pulse="None", glow=100)
                back = sk.recolor(a, start=-k, end=k, gap=gap, duration=2.0,
                                  ease="OutCubic", angle_offset=ao)
                ok1 = put(flash, tag="涟漪环")
                ok2 = put(back, tag="涟漪环")
                if ok1 and ok2:
                    wrote += 1
                else:
                    break
            rp.rings = wrote - 1                 # -1 ⇒ 一条都没写进去（上限/越界）
            if wrote == 0:
                pl.skipped_why.append(
                    f"涟漪环：格 {a}（{kind}起点）连中心环都没写进去（同格并行度上限？）")
            pl.ripples.append(rp)
        if occ:
            seen: set[int] = set()
            for r in pl.ripples:
                for k in range(r.rings + 1):
                    for t in (r.floor - k, r.floor + k):
                        if t in occ and 0 <= t < n:
                            seen.add(t)
            pl.n_overlap_occ = len(seen)

    # ------------------------------------------------------ 半径调度
    if pl.radius_on:
        pl.radius_spans = radius_switch_points(
            ch, dense_fps=dense_fps, quiet_fps=quiet_fps, window_s=density_window,
            min_sec=min_sec, min_tiles=min_tiles,
            quiet=int(radius_quiet), dense=int(radius_dense))
        # ★★ 用户 2026-10：「行星距离」**最高的阈值应该为 125** —— 夹了就要记账
        for _nm, _v in (("密集半径", radius_dense), ("稀疏半径", radius_quiet)):
            if int(_v) > RADIUS_MAX or int(_v) < RADIUS_MIN:
                _got = max(RADIUS_MIN, min(RADIUS_MAX, int(_v)))
                pl.skipped_why.append(
                    "半径调度：%s %d 超出 [%d, %d] ⇒ **夹到 %d**（用户口径：行星距离最高 %d）"
                    % (_nm, int(_v), RADIUS_MIN, RADIUS_MAX, _got, RADIUS_MAX))
        for s in pl.radius_spans:
            if not put({"floor": int(s.floor), "eventType": "ScaleRadius",
                        "scale": int(s.scale)}, tag="半径调度"):
                break
        if not pl.radius_spans:
            pl.skipped_why.append("半径调度：全谱密度没跨过密/疏阈值 ⇒ 0 次切换（默认半径 100 不变）")

    pl.events.sort(key=lambda e: (int(e["floor"]), str(e["eventType"])))
    return pl


def apply(ch, **kw) -> int:
    """算好挂到 `ch.meta`（`appearance_events` / `appearance_settings`），返回事件条数。"""
    pl = plan(ch, **kw)
    ch.meta["appearance_events"] = list(pl.events)
    ch.meta["appearance_settings"] = dict(pl.settings)
    ch.meta["appearance_n"] = len(pl.events)
    ch.meta["appearance_report"] = pl.to_dict()
    return len(pl.events)


__all__ = ["Skin", "AppearancePlan", "Ripple", "RadiusSpan", "SKINS", "DEFAULT_SKIN",
           "FIGURE_FLAGS", "plan", "apply", "figure_spans", "figure_kind",
           "density_fps", "radius_switch_points",
           "RIPPLE_STEP_DEG", "RIPPLE_RINGS", "MAX_PER_FLOOR",
           "RADIUS_QUIET", "RADIUS_DENSE", "DENSE_FPS", "QUIET_FPS",
           "DENSITY_WINDOW_S", "RADIUS_MIN_SEC", "RADIUS_MIN_TILES"]
