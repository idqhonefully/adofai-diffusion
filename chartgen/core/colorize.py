"""换手押上色：`ColorTrack` + `justThisTile` 事件生成（`docs/59`）。

用户口径（2026-10，逐轮定死）：
  1. **只改轨道颜色**，而且**只有换手押给换色**；颜色 = 固定的**黑底白边霓虹**
     （`trackColorType=Glow` / 主 `000000` / 副 `ffffff` / `trackStyle=Neon`）；
  2. **换手押判据**：`X` = 双押组、`O` = 普通格，满足 **`OOX` 循环**（周期 3 个事件）的结构；
     连续 **≥2 个周期**才算；**不看 Twirl**；
  3. **只在换的那个格子染色**（记号串 `XOODOOXOODOO`：`D` = 染色的那个双押）
     ⇒ **每 2 个双押染 1 个、从链内第 2 个起**；
  4. **要「设置」不要「重新设置」** —— 那格**一出现就是霓虹**（能起提示作用），
     而不是「走过之后才变色」。

★★ 事件选型（2026-10 用户两次进游戏实测后定的，**别再退回 `RecolorTrack`**）：

| 事件 | 游戏里的名字 | 语义 | 为什么不用/用 |
|---|---|---|---|
| `RecolorTrack` | **重新设置轨道颜色** | **运行时**：玩家时间越过事件所在格，才把 `startTile..endTile` 范围翻色（`vendor/Re_ADOJAS/.../TileColorManager.ts:542-569`） | ✗ 用户实测「**走过后才展示**，起不到提示作用」；而且只能靠「提前发事件」硬凑 |
| **`ColorTrack`** | **设置轨道颜色** | **建关时逐格预演**，直接把颜色写进那一格的组件（`scnGame.cs:379-461`、`:653`） | ✓ 那格**一出生就是该颜色** |

而「**仅作用于当前方块**」在 `ColorTrack` 里是**一个字段**（`scnGame.cs:447-461`）：

```csharp
if (!output6)            // output6 = justThisTile 取出来的值
{
    color1 = tempColor1; // ← 只有【不】justThisTile 才把这次的颜色写回「全局当前状态」
    style  = tempStyle;  //    做了 justThisTile ⇒ **只改这一格，下一格自动恢复原色**
    ...
}
```
（编辑器自己的「单格粘贴」也正是这么干的：`scnEditor.PasteTrackColorSingleTile()`，
要额外在 id+1 补一条恢复事件 —— 有了 `justThisTile` 就**不用**补了。）

★ 硬约束：
  · **只写 `actions`**，绝不碰 `angleData / bpm / travel / Twirl / settings` ⇒ 时序零影响；
  · 关掉开关时 **一条事件都不写**（导出与旧版逐字节相同）；
  · **一格一条、绝不堆叠**（用户拿编辑器截图指出过：几百条压在 floor 0 会变成「1/500」）；
  · **不许静默**：识别了几组、跳过了几组、为什么不算换手押，全部进报告。

★ 双押组从哪来（两条路径，见 `docs/59` §2.1.2）：
  · **我们自己的谱面**：`ch.meta["dp_pairs"]` 是权威记录 ——
    `core.dp_angle.apply` 写的是 `[薄格 θ, 剩余格 原travel−θ]`，回填 `(薄格, 薄格, 剩余格)`**每块薄格一条**；
  · **外来谱面**：几何判据 `travel[i] + travel[i+1] == 180`（用户样例上零假阳性），
    ⚠ `(90°, 90°)` 与普通直角折角无法区分 ⇒ 只接受 `min(travel) < 90°`，其余**逐条报出来**。
"""
from __future__ import annotations

from dataclasses import dataclass, field

#: 双押组的两种编码（与 `core.dp_angle` 的 `dp_kind` 对应）
KIND_ANGLE = "angle"
KIND_MIDSPIN = "midspin"
KIND_NONE = "none"

#: 上色范围
SPAN_THIN = "thin"      # 只染薄格 1 格（用户定稿）
SPAN_GROUP = "group"    # 整个双押组（薄格 + 剩余格 ⇒ 两条事件）

SPAN_LABELS = (("只染薄格（1 格）", SPAN_THIN), ("整个双押组（2 格）", SPAN_GROUP))

#: 几何判据里「薄」的定义：min(travel) 必须严格小于它（排除 90/90 与普通直角折角）
GEOM_THIN_LT = 90.0
_EPS = 1e-6

#: 每隔几个双押染一个（`1` = 全染）
#: ★ 用户 2026-10：「换手的意义在于：**只在换的那个格子**进行染色」，并给出记号串
#:   `XOODOOXOODOO`（X=双押 / O=普通格 / **D=染色的那个双押**）⇒ **每 2 个双押染 1 个**。
COLOR_EVERY = 2
#: 从链内**第几个**双押开始染（1 起算）。用户记号串是 `X O O D O O …` ⇒ 从**第 2 个**开始。
COLOR_PHASE = 2

#: 事件字段集 **逐字对齐真实语料**（`ColorTrack`，见 `out/_colortrack.txt` 与 `docs/59` §1.1）。
#: ⚠ 少字段会像 `PositionTrack` 那次一样让 `LevelEvent.Decode` 取到 null ⇒ 游戏 NRE。
#: 语料实测的最常见组合（含 `justThisTile`）；`floorIconOutlines` 是可选的，不写。
EVENT_KEYS = ("floor", "eventType", "trackColorType", "trackColor", "secondaryTrackColor",
              "trackColorAnimDuration", "trackColorPulse", "trackPulseLength", "trackStyle",
              "trackTexture", "trackTextureScale", "trackGlowIntensity", "justThisTile")

DEFAULTS = dict(color_type="Glow", color="000000", secondary="ffffff",
                style="Neon", anim=2, pulse="None", pulse_len=10, glow=100,
                texture="", texture_scale=1)


@dataclass
class Group:
    """一个双押组：`thin` = 薄格下标，`rest` = 剩余格下标，`press` = 押数。"""
    thin: list[int]
    rest: int
    press: int

    @property
    def first(self) -> int:
        return self.thin[0]

    @property
    def last(self) -> int:
        return max(self.rest, self.thin[-1])


@dataclass
class ColorPlan:
    """上色方案（纯数据，可 JSON 化）。"""
    events: list[dict] = field(default_factory=list)
    #: 被染色的格（与 `events` 一一对应）
    _tiles: list[int] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    chains: list[list[int]] = field(default_factory=list)   # 每条链里的组下标
    source: str = KIND_NONE                                  # dp_pairs / geometry
    n_skipped_press: int = 0        # 押数 ≠ 2（三押/四押）→ 本轮不处理
    n_skipped_ambig: int = 0        # 几何判据里 (90,90) 之类认不出的候选
    skipped_why: list[str] = field(default_factory=list)
    gap_tiles: int = 2
    min_cycles: int = 2
    span: str = SPAN_THIN
    color_every: int = COLOR_EVERY   # 每隔几个双押染一个（1 = 全染）
    color_phase: int = COLOR_PHASE   # 从链内第几个双押开始染（1 起算）
    enabled: bool = True

    # ---------------------------------------------------------------- 查询
    @property
    def n_events(self) -> int:
        return len(self.events)

    @property
    def tiles(self) -> list[int]:
        """**被染色的那些格**（= 换手押格）。"""
        return [int(t) for t in self._tiles]

    @property
    def floors(self) -> list[int]:
        """`ColorTrack` 是**一格一条**、事件就在那一格上 ⇒ 与 `tiles` 相同（前端段带/计数用它）。"""
        return self.tiles

    #: 兼容旧名字（前端/单测读过它）
    handswitch_floors = floors

    @property
    def max_per_floor(self) -> int:
        """同一格上最多压了几条事件 —— **必须是 1**（用户拿编辑器截图指出的堆叠问题）。"""
        c: dict[int, int] = {}
        for e in self.events:
            f = int(e["floor"])
            c[f] = c.get(f, 0) + 1
        return max(c.values()) if c else 0

    def to_dict(self) -> dict:
        return {
            "enabled": bool(self.enabled),
            "source": self.source,
            "n_groups": len(self.groups),
            "n_chains": len(self.chains),
            "n_events": len(self.events),
            "floors": self.floors,                 # = 换手押格（段带用）
            "event_type": "ColorTrack",
            "just_this_tile": True,
            "max_per_floor": int(self.max_per_floor),
            "gap_tiles": int(self.gap_tiles),
            "min_cycles": int(self.min_cycles),
            "color_every": int(self.color_every),
            "color_phase": int(self.color_phase),
            "span": self.span,
            "n_skipped_press": int(self.n_skipped_press),
            "n_skipped_ambig": int(self.n_skipped_ambig),
            "skipped_why": list(self.skipped_why),
        }

    def _skip_txt(self) -> str:
        out = []
        if self.n_skipped_press:
            out.append(f"押数≠2 的组 {self.n_skipped_press} 个已跳过")
        if self.n_skipped_ambig:
            out.append(f"认不出的 (90,90) 候选 {self.n_skipped_ambig} 个已跳过")
        return "；" + "，".join(out) if out else ""

    def every_text(self) -> str:
        """染色间隔的人话（**不许静默**：用户给的定义是 `XOODOOXOODOO`）。"""
        every = int(self.color_every)
        if every <= 1:
            return "每个双押都染"
        return f"每 {every} 个双押染 1 个（从链内第 {int(self.color_phase)} 个开始）"

    def report_text(self) -> str:
        """一句人话（界面/日志用）——**不许静默**。"""
        if not self.enabled:
            return "换手押上色：已关闭（一条 ColorTrack 都不写）"
        if not self.groups:
            why = self.skipped_why[0] if self.skipped_why else "没有双押组"
            return f"换手押上色：0 处（{why}）" + self._skip_txt()
        base = (f"换手押上色 {self.n_events} 处"
                f"（双押组 {len(self.groups)} 个 → 链 {len(self.chains)} 条；"
                f"来源 {self.source}；判据 间隔{self.gap_tiles}格·≥{self.min_cycles}周期；"
                f"{self.every_text()}；ColorTrack+justThisTile（一格一条、"
                f"一出现就是霓虹））")
        return base + self._skip_txt()


# -------------------------------------------------------------------- 双押组
def _groups_from_meta(ch) -> list[Group] | None:
    """从 `ch.meta["dp_pairs"]` 取**权威**双押组；没有就返回 None（交给几何判据）。"""
    pairs = (getattr(ch, "meta", {}) or {}).get("dp_pairs") or []
    if not pairs:
        return None
    by_rest: dict[int, list[int]] = {}
    for tup in pairs:
        try:
            ix, _iy, ib = (int(tup[0]), int(tup[1]), int(tup[2]))
        except Exception:      # noqa: BLE001
            continue
        by_rest.setdefault(ib, []).append(ix)
    out = []
    for rest, thins in sorted(by_rest.items()):
        thins = sorted(thins)
        out.append(Group(thin=thins, rest=rest, press=len(thins) + 1))
    out.sort(key=lambda g: g.first)
    return out or None


def _groups_geometric(ch, rep: "ColorPlan") -> list[Group]:
    """几何判据：相邻两格 travel 之和 = 180，且**薄的一格严格 < 90°**。"""
    fl = ch.floors
    tv = [float(getattr(f, "travel", 180.0)) for f in fl]
    out: list[Group] = []
    i = 0
    while i + 1 < len(tv):
        a, b = tv[i], tv[i + 1]
        straight = 180.0
        if a < straight - _EPS and b < straight - _EPS and abs(a + b - straight) < _EPS:
            thin = i if a < b else i + 1
            rest = i + 1 if thin == i else i
            if min(a, b) < GEOM_THIN_LT - _EPS:
                out.append(Group(thin=[thin], rest=rest, press=2))
            else:
                rep.n_skipped_ambig += 1
                rep.skipped_why.append(
                    f"第 {i}~{i + 1} 格 travel=({a:g},{b:g}) 恰好各 90° ⇒ "
                    "无法与普通直角折角区分，按『不认』处理")
            i += 2
        else:
            i += 1
    return out


def find_chains(groups: list[Group], gap_tiles: int, min_cycles: int) -> list[list[int]]:
    """按 `OOX` 结构分链：相邻两组的「间隔普通格数」必须恰好 = `gap_tiles`。

    `gap` 定义：`下一组的首格 − 上一组的末格 − 1`。链长（组数）≥ `min_cycles` 才算换手押段。
    """
    chains: list[list[int]] = []
    cur: list[int] = []
    for k, g in enumerate(groups):
        if not cur:
            cur = [k]
            continue
        prev = groups[cur[-1]]
        gap = g.first - prev.last - 1
        if gap == int(gap_tiles):
            cur.append(k)
        else:
            if len(cur) >= int(min_cycles):
                chains.append(cur)
            cur = [k]
    if len(cur) >= int(min_cycles):
        chains.append(cur)
    return chains


# -------------------------------------------------------------------- 事件
def make_event(floor: int, cfg: dict | None = None) -> dict:
    """造一条 **`ColorTrack` + `justThisTile:true`**（字段集逐字对齐语料）。

    `justThisTile: true` = **仅作用于当前方块**（`scnGame.cs:447-461`）：
    只给这一格上色、**不写回全局当前颜色** ⇒ 下一格自动恢复，不需要补恢复事件。
    """
    c = dict(DEFAULTS)
    if cfg:
        c.update(cfg)
    return {
        "floor": int(floor),
        "eventType": "ColorTrack",
        "trackColorType": str(c["color_type"]),
        "trackColor": str(c["color"]),
        "secondaryTrackColor": str(c["secondary"]),
        "trackColorAnimDuration": c["anim"],
        "trackColorPulse": str(c["pulse"]),
        "trackPulseLength": c["pulse_len"],
        "trackStyle": str(c["style"]),
        "trackTexture": str(c["texture"]),
        "trackTextureScale": c["texture_scale"],
        "trackGlowIntensity": c["glow"],
        "justThisTile": True,
    }


def plan(ch, *, enabled: bool = True, gap_tiles: int = 2, min_cycles: int = 2,
         span: str = SPAN_THIN, color_every: int = COLOR_EVERY,
         color_phase: int = COLOR_PHASE, cfg: dict | None = None) -> ColorPlan:
    """算一份换手押上色方案。**纯函数**，不改 `ch` 任何东西。"""
    rep = ColorPlan(gap_tiles=int(gap_tiles), min_cycles=int(min_cycles),
                    span=str(span), color_every=int(color_every),
                    color_phase=int(color_phase), enabled=bool(enabled))
    if not enabled:
        return rep
    if ch is None or not getattr(ch, "floors", None):
        rep.skipped_why.append("还没有谱面")
        return rep

    groups = _groups_from_meta(ch)
    if groups is None:
        rep.source = "geometry"
        groups = _groups_geometric(ch, rep)
    else:
        rep.source = "dp_pairs"

    keep: list[Group] = []
    for g in groups:
        if g.press != 2:
            rep.n_skipped_press += 1
            rep.skipped_why.append(
                f"第 {g.first} 格的双押组押数 = {g.press} ⇒ 本轮只处理双押（三押/四押按『跳过』记帐）")
            continue
        keep.append(g)
    rep.groups = keep

    if not keep:
        if not rep.skipped_why:
            rep.skipped_why.append("全曲没有双押组")
        return rep

    rep.chains = find_chains(keep, gap_tiles, min_cycles)
    if not rep.chains:
        rep.skipped_why.append(
            f"没有任何连续 ≥{int(min_cycles)} 个、间隔恰好 {int(gap_tiles)} 格的 `OOX` 链 ⇒ 不算换手押")
        return rep

    # ★★ 「**只在换的那个格子**染色」（用户记号串 `XOODOOXOODOO`）：
    #   一条链里每 `color_every` 个双押染 1 个，从链内第 `color_phase` 个起算（1 起算）。
    every = max(1, int(color_every))
    phase = max(1, int(color_phase))
    tiles: list[int] = []
    for ch_ix in rep.chains:
        for pos, k in enumerate(ch_ix):          # pos：链内第几个双押（0 起算）
            if every > 1 and ((pos - (phase - 1)) % every) != 0:
                continue
            tiles.append(int(keep[k].first))
    tiles = sorted(set(tiles))
    rep._tiles = list(tiles)
    if not tiles:
        rep.skipped_why.append(
            f"链里每个双押链都不足 {phase} 个双押（相位 = 第 {phase} 个）⇒ 一条都没染；"
            "把「从链内第几个开始」改成 1，或把「每几个双押染一个」改成 1")
        return rep

    n_fl = len(getattr(ch, "floors", []) or [])
    by_first = {int(g.first): g for g in keep}
    for t in tiles:
        if t < 0 or t >= n_fl:
            rep.skipped_why.append(f"换手押格 {t} 超出谱面 {n_fl} 格 ⇒ 这一条没写")
            continue
        rep.events.append(make_event(t, cfg=cfg))
        # 范围档：整个双押组 ⇒ 再给剩余格补一条（仍是 `justThisTile`，一格一条）
        if str(span) == SPAN_GROUP:
            g = by_first.get(t)
            if g is not None and int(g.rest) != t and 0 <= int(g.rest) < n_fl:
                rep.events.append(make_event(int(g.rest), cfg=cfg))
    rep.events.sort(key=lambda e: int(e["floor"]))
    return rep


def describe(rep: ColorPlan) -> str:
    return rep.report_text()
