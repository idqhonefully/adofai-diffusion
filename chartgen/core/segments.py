# -*- coding: utf-8 -*-
"""分段采音（`docs/34` §3 方案 C 的落地）—— **源无关**的音轨角色时间轴。

## 它解决什么（对应 `docs/34` §2 的 P3 / P4）

老「区间」的做法是：**整段丢掉**全局 onset → 放进区间自己采的那一批 →
按 `merge_ms` 去重且**只留最早**（`sidecar/session.py::apply_regions`）。
而区间只能改「主轨」一维，次级 / 双押仍是全局的（P4）。

本模块换成**按时刻查角色**：

    [0,   at0)  → 全局（② 的三条列表）
    [at0, at1)  → seg0 显式指定的角色（**没指定的维度继承全局**）
    [at1, end)  → seg1 …

于是：

* **P3 消解**：老算法的病根是**先聚类、后切割** —— `build_onsets` 在整条轨上
  把互相 ≤`merge_ms` 的音聚成一簇（簇头是簇内最早的音），**然后**才按边界筛掉
  簇外的音；落在边界外侧的那个「簇头」被筛走时，簇里的音就**整簇消失**，而
  边界只是一个像素换算出来的数字。本模块改成**先切割、后聚类**：先按片决定
  「哪些音进池」，再把各片的池拼起来**全曲只聚一次类** ⇒ 边界**不参与聚类**。
  实测（`tools/_seg_probe.py`，20000 组随机输入）：老做法 **555 组（2.8%）**
  与真值不同，且**双向**出错（既吃音、也凭空多音、还会把音挪到别处）。
  另修掉一个 off-by-one：老尾巴去重用 `<`、片内聚类用 `<=`。
* **P4 消解**：三个角色都是**时间的函数**，且每一维都能**单独**覆盖
  （`None` = 继承，`[]` = 显式关掉 —— 这两件事必须能分开表达，否则
  「这段只留节拍器」和「这段别动次级」会写成同一个数据）；
* **P6 开区间**：最后一段天然延伸到曲末，用户不必填「结尾毫秒 = 曲子长度」；
* **P7 可保存**：数据就是一份可序列化的编排表（`to_json`）。

## 两种语义（用户口径，可切换）

    模式 "from"（默认）           模式 "until"
    ─────────────────────        ─────────────────────
    [0,   at0) 全局              [0,   at0) seg0
    [at0, at1) seg0              [at0, at1) seg1
    [at1, end) seg1              [at1, end) 全局

`from` = 点说的是「**从这点起**用这几条轨」；`until` = 「**到这点为止**」。
两者互为镜像。默认 `from`：`docs/34` P6 说真实编辑绝大多数是
「从第 N 小节开始，往后都用这几条轨」。

★ **两种模式都不改「边界落在哪」以外的东西** —— `from` 与 `until` 对同一组
  分段给出的**段数相同**，只是哪一段吃哪条规则不同。所以在 UI 上换模式是
  零成本的（不需要重新画块，只需要重算归属）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .onsets import build_onsets, notes_fill_gaps

# ============================================================ 模式
MODE_FROM = "from"
MODE_UNTIL = "until"
MODES = (MODE_FROM, MODE_UNTIL)
DEFAULT_MODE = MODE_FROM

# ============================================================ 角色
ROLE_MAIN = "main"
ROLE_SUB = "sub"
ROLE_DP = "dp"
ROLE_OFF = "off"
ROLES = (ROLE_MAIN, ROLE_SUB, ROLE_DP, ROLE_OFF)
# 可以「随时间改」的三个角色（`off` 不是角色，是**指令**：把这条轨从三个角色里摘掉）
ROLE_DIMS = (ROLE_MAIN, ROLE_SUB, ROLE_DP)

# ============================================================ 线格式键（前端 ⇄ 后端）
K_AT = "at_ms"
K_LABEL = "label"
K_MAIN = "main"
K_SUB = "sub"
K_DP = "dp"
# ★ 语义模式的键**必须带命名空间**：`state` 是前端一整个大对象，
#   用裸 `"mode"` 会跟别的字段撞车（实测撞过一次，`until` 被静默忽略）。
K_SEG_MODE = "segment_mode"
K_T0 = "start_ms"
K_T1 = "end_ms"
K_SRC = "src"
K_N = "n"
K_N_CROSS = "n_cross"
K_TRACK = "track"
K_ROLE = "role"
K_INDEX = "index"
K_TRACKS = "tracks"
K_SUMMARY = "summary"

# 这一段从哪儿来（给 UI 上色 / 报告用）
SRC_SEG = "segment"          # 用户画的分段
SRC_GLOBAL = "global"        # 继承全局 ② 的选择（没被任何分段覆盖）

DEFAULT_GAP_MS = 600.0


# ============================================================ 数据
@dataclass
class Segment:
    """一条「从 `at_ms` 起 / 到 `at_ms` 止」的角色规则。

    ★ 三个维度各自的 `None` 与 `[]` **语义不同**，不能混淆：

        None  →  **继承**全局 ② 里该维度的选择（用户没管这一维）
        []    →  **显式关掉**该维度（这一段不用这个角色）
        [1,3] →  显式指定
    """

    at_ms: float
    label: str = ""
    main: list | None = None
    sub: list | None = None
    dp: list | None = None
    index: int = 0

    # ----------------------------------------------------------
    def has_override(self) -> bool:
        return any(getattr(self, r) is not None for r in ROLE_DIMS)

    def dims(self) -> list:
        return [r for r in ROLE_DIMS if getattr(self, r) is not None]

    def to_json(self) -> dict:
        d = {K_AT: round(float(self.at_ms), 6), K_LABEL: self.label,
             K_INDEX: self.index}
        for r in ROLE_DIMS:
            v = getattr(self, r)
            d[r] = None if v is None else [int(x) for x in v]
        return d


def _clean_tracks(v, n_tracks: int, has_notes) -> list | None:
    """轨号列表 → 去重排序 + 越界/空轨剔除。`None` 原样传下去（= 继承）。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        v = [v]
    out = []
    for x in v or []:
        try:
            i = int(x)
        except (TypeError, ValueError):
            continue
        if 0 <= i < n_tracks and (has_notes is None or bool(has_notes(i))):
            out.append(i)
    return sorted(set(out))


def normalize(raw, mode: str = DEFAULT_MODE, total_ms: float = 0.0,
              n_tracks: int = 0, has_notes=None) -> list:
    """规范化分段列表：夹范围 / 清洗轨号 / 同 `at` **后写的赢** / 按时刻排序。

    `total_ms <= 0` 时不做上界裁剪（调用方拿不到曲长时不该把 `at` 掐成 0）。
    """
    out: list = []
    seen: dict = {}
    for i, rg in enumerate(raw or []):
        if not isinstance(rg, dict):
            continue
        try:
            at = float(rg.get(K_AT, 0.0))
        except (TypeError, ValueError):
            continue
        if at != at or at in (float("inf"), float("-inf")):      # NaN / ±inf
            continue
        at = max(0.0, at)
        if total_ms > 0:
            at = min(at, float(total_ms))
        seg = Segment(
            at_ms=at,
            label=str(rg.get(K_LABEL) or ""),
            main=_clean_tracks(rg.get(K_MAIN), n_tracks, has_notes),
            sub=_clean_tracks(rg.get(K_SUB), n_tracks, has_notes),
            dp=_clean_tracks(rg.get(K_DP), n_tracks, has_notes),
            index=i,
        )
        if not seg.has_override():
            continue              # 一个维度都没管 ⇒ 这条规则是空操作
        key = round(at, 6)
        if key in seen:
            out[seen[key]] = seg   # ★ 后写的赢（与老区间一致）
        else:
            seen[key] = len(out)
            out.append(seg)
    out.sort(key=lambda s: s.at_ms)
    for j, s in enumerate(out):
        s.index = j
    return out


def mode_of(st) -> str:
    m = str((st or {}).get(K_SEG_MODE) or DEFAULT_MODE)
    return m if m in MODES else DEFAULT_MODE


# ============================================================ 分段 → 时间片
def spans(segs: list, mode: str, total_ms: float) -> list:
    """`[(t0, t1, seg_or_None)]` —— 半开、连续、**铺满 `[0, total)`**。

    返回的每一片都自带「它该用哪条规则」（`None` = 用全局 ② 的选择）。
    """
    segs = sorted(segs, key=lambda s: s.at_ms)
    total_ms = max(0.0, float(total_ms))
    bounds = [0.0] + [float(s.at_ms) for s in segs] + [total_ms]
    out = []
    for i in range(len(bounds) - 1):
        t0, t1 = bounds[i], bounds[i + 1]
        if t1 <= t0:
            continue
        if mode == MODE_UNTIL:
            seg = segs[i] if i < len(segs) else None
        else:
            seg = segs[i - 1] if i >= 1 else None
        out.append((t0, t1, seg))
    return out


def resolve(seg, role: str, glob) -> list:
    """查一维的角色：分段显式给了就用它的（`[]` 也是"给了"），否则继承全局。"""
    v = None if seg is None else getattr(seg, role, None)
    return [int(x) for x in glob] if v is None else [int(x) for x in v]


def _slice(notes: list, t0: float, t1: float) -> list:
    return [n for n in notes if t0 <= n.t_on_ms < t1]


# ============================================================ 采音
def sample(tracks: list, segs: list, mode: str, total_ms: float,
           glob_main, glob_sub, p_on, gap_ms: float = DEFAULT_GAP_MS,
           glob_dp=()) -> tuple:
    """★ 按时间片采音。**边界只决定「哪些音进池，全曲只聚一次类」**（P3 消解）。

    两趟，职责分得很干净：

    1. **分片建池**：每片按自己的角色算出「这一段该有哪些音」。主轨那几条全采；
       次级轨只往主轨的空隙里补（与全局 ② 同一套语义）。**这一步会按片切音**。
    2. **全曲聚类**：把各片的池**拼在一起**，只跑**一次** `build_onsets`
       —— 于是边界**根本不参与聚类**，它只影响「谁在池里」。

    这就是老 `apply_regions` 的病因解药：它是**先聚类、后切割**（簇头落在边界外
    就被整簇筛走）。实测 20000 组随机输入里，老做法有 **2.8%（555 组）**与
    「先切割、后聚类」的真值不同，而且**双向**出错（既吃音，也凭空多音）。
    """
    pools = []                     # [(t0, t1, seg, notes)]
    for j, (t0, t1, seg) in enumerate(spans(segs, mode, total_ms)):
        main = resolve(seg, ROLE_MAIN, glob_main)
        sub = resolve(seg, ROLE_SUB, glob_sub)
        dp = resolve(seg, ROLE_DP, glob_dp)
        prim = [n for i in main for n in _slice(tracks[i].notes, t0, t1)]
        rest = [i for i in sub if i not in set(main)]
        fill = [n for i in rest for n in _slice(tracks[i].notes, t0, t1)]
        if prim and fill:
            notes = notes_fill_gaps(prim, fill, float(gap_ms))
        else:
            notes = prim or fill
        pools.append((t0, t1, seg, notes))

    # ---- 第 2 趟：全曲**一次**聚类（`build_onsets` 的簇头是簇内最早的音）
    allnotes = [n for (_a, _b, _s, ns) in pools for n in ns]
    ons = build_onsets(allnotes, p_on)

    rows = []
    for j, (t0, t1, seg) in enumerate((p[0], p[1], p[2]) for p in pools):
        rows.append({
            K_INDEX: j, K_T0: round(t0, 6), K_T1: round(t1, 6),
            K_LABEL: (seg.label if seg is not None and seg.label else
                      (f"分段{j + 1}" if seg is not None else "全局")),
            K_SRC: SRC_SEG if seg is not None else SRC_GLOBAL,
            K_MAIN: resolve(seg, ROLE_MAIN, glob_main),
            K_SUB: resolve(seg, ROLE_SUB, glob_sub),
            K_DP: resolve(seg, ROLE_DP, glob_dp),
            K_N: 0, K_N_CROSS: 0,
        })

    # ---- 归片：片是半开且互不重叠的，onset 时刻落在哪片就是哪片
    #      （簇头在最早的那片 ⇒ 归属 = 簇头时刻所在的片）
    bounds = [(p[0], p[1]) for p in pools]
    for o in ons:
        for j, (t0, t1) in enumerate(bounds):
            if t0 <= o.t_ms < t1:
                rows[j][K_N] += 1
                break

    # ---- ★ 不许静默：簇跨了边界就报出来（跨片簇 = 边界处两次按键并成一次）
    keep = _passes(allnotes, p_on)
    n_cross = 0
    for o in ons:
        hi = o.t_ms + max(0.0, float(p_on.merge_ms))
        js = {_span_at(t, bounds) for t in
              (n.t_on_ms for n in keep if o.t_ms <= n.t_on_ms <= hi)}
        js.discard(-1)
        if len(js) > 1:
            n_cross += 1
    if rows and n_cross:
        rows[-1][K_N_CROSS] = n_cross
    return ons, rows


def _passes(notes: list, p_on) -> list:
    """复刻 `build_onsets` 的**过滤**那一步（只为跨片诊断，不复制聚类逻辑）。"""
    return [n for n in notes
            if n.velocity >= p_on.min_velocity
            and p_on.pitch_lo <= n.pitch <= p_on.pitch_hi]


def _span_at(t: float, bounds: list) -> int:
    for j, (t0, t1) in enumerate(bounds):
        if t0 <= t < t1:
            return j
    return -1


def dp_marks(tracks: list, segs: list, mode: str, total_ms: float,
             glob_dp, p_on, per_span: bool = False) -> list:
    """双押落点 —— **带轨号**：`[(t_ms, track), …]`（`docs/48` §6.1 的前置坑）。

    ★ 三押的判据是「**同一时刻有 2 条多押轨都有音**」⇒ **轨号必须留着**，
      否则数不出「同时有几条轨」（同 `docs/42` `Onset.src_tracks` 那次的教训）。

    `per_span=False`（默认）走老路径：全局双押轨整条采。
    `per_span=True`：只有确实有分段显式改了双押轨时才按片采（否则分段的
    `dp: []` 就成了空话）。
    """
    if not per_span:
        out: list = []
        for i in glob_dp:
            out.extend((o.t_ms, int(i))
                       for o in build_onsets(tracks[i].notes, p_on))
        return sorted(out)
    out = []
    for (t0, t1, seg) in spans(segs, mode, total_ms):
        for i in resolve(seg, ROLE_DP, glob_dp):
            out.extend((o.t_ms, int(i)) for o in build_onsets(tracks[i].notes, p_on)
                       if t0 <= o.t_ms < t1)
    return sorted(out)


def dp_times(tracks: list, segs: list, mode: str, total_ms: float,
             glob_dp, p_on, per_span: bool = False) -> list:
    """双押落点时刻（**不带轨号**）。

    ★ 就是 `dp_marks(…)[0]` 那一列 —— 保持**逐字节不变**的老口径
      （`sorted` 后丢掉轨号与「按 `(t, 轨)` 排序再丢轨号」等价）。
    """
    return [t for (t, _tr) in dp_marks(tracks, segs, mode, total_ms,
                                       glob_dp, p_on, per_span=per_span)]



def any_override(segs: list, role: str) -> bool:
    return any(getattr(s, role, None) is not None for s in (segs or []))


# ============================================================ 角色点 → 分段
def from_roles(events: list, label_prefix: str = "分段") -> list:
    """把「角色轨上的点」编译成 `from` 模式的段落（`docs/38` §10 的自动填充）。

    `events` = `[{at_ms, role, track}]`（`role` ∈ ROLES）。语义：**从这点起**
    这条轨是这个角色（累计快照 —— 后面没再出现的轨保持上一次的角色）。

    ★ 只有**到这一刻为止确实出现过**的维度才写进段落（其余留 `None` = 继承全局）：
      用户只在「主轨」角色轨上打了点，那 `sub/dp` 就该照旧走全局 ②，
      而不是被悄悄清空。注意是**累计**的 —— 2000ms 才第一次出现 `sub` 时，
      1000ms 那条规则里 `sub` 仍是 `None`（那时用户还没管过次级）。
    ★ `off` 是**指令**：`track` 给了就把这条轨从三个角色里摘掉；没给就三档全清。
    """
    ev = []
    for e in events or []:
        if not isinstance(e, dict):
            continue
        try:
            t = float(e.get(K_AT))
        except (TypeError, ValueError):
            continue
        r = str(e.get(K_ROLE) or "")
        if r not in ROLES:
            continue
        trk = e.get(K_TRACK)
        try:
            trk = None if trk is None else int(trk)
        except (TypeError, ValueError):
            trk = None
        ev.append((t, len(ev), r, trk))
    ev.sort(key=lambda x: (x[0], x[1]))
    cur = {r: [] for r in ROLE_DIMS}
    seen: set = set()
    out: list = []
    for (t, _seq, role, trk) in ev:
        nxt = {r: [x for x in cur[r] if x != trk] for r in ROLE_DIMS}
        if role != ROLE_OFF:
            if trk is not None and trk not in nxt[role]:
                nxt[role].append(trk)
            seen.add(role)
        if nxt == cur and out:
            continue                    # 净效果没变 ⇒ 不产生空段落
        cur = nxt
        out.append(Segment(at_ms=t, label=f"{label_prefix}{len(out) + 1}",
                           **{r: (sorted(cur[r]) if r in seen else None)
                              for r in ROLE_DIMS}))
    for j, s in enumerate(out):
        s.index = j
    return out


def role_events(rows: list) -> list:
    """把 `bridge.summary()["tracks"]` 那种行 → `from_roles` 要的 `events`。

    只认**有角色、且角色在 ROLES 里**的轨；一条轨的每个拍位都产生一个事件
    （每个事件的 `at_ms` 由调用方换算好填进行里 —— 本模块不碰 beat⇄ms）。
    """
    out = []
    for r in rows or []:
        role = str(r.get(K_ROLE) or "")
        if role not in ROLES:
            continue
        idx = r.get(K_INDEX)
        for e in r.get("events") or []:
            out.append({K_AT: e.get(K_AT), K_ROLE: role, K_TRACK: idx})
    return out


# ============================================================ 报告
def to_json(segs: list) -> list:
    return [s.to_json() for s in (segs or [])]


def report_text(rows: list, mode: str = DEFAULT_MODE) -> str:
    if not rows:
        return "分段：无（全曲走全局 ② 的选择）"
    head = ("分段：%d 段 · 模式「%s」"
            % (len(rows), "从这点起" if mode == MODE_FROM else "到这点为止"))
    lines = [head]
    for r in rows:
        lines.append(
            "  [%d] %8.3f~%8.3fms  %-6s  主%s 次%s 双押%s  → %d 点%s"
            % (r[K_INDEX], r[K_T0], r[K_T1], r[K_SRC],
               r[K_MAIN] or "—", r[K_SUB] or "—", r[K_DP] or "—", r[K_N],
               ("（%d 处簇跨边界）" % r[K_N_CROSS]) if r.get(K_N_CROSS) else ""))
    return "\n".join(lines)


def summary(rows: list) -> dict:
    return {
        K_N: len(rows or []),
        "n_seg": sum(1 for r in (rows or []) if r[K_SRC] == SRC_SEG),
        "n_global": sum(1 for r in (rows or []) if r[K_SRC] == SRC_GLOBAL),
        "n_onsets": sum(r[K_N] for r in (rows or [])),
        K_N_CROSS: sum(r.get(K_N_CROSS, 0) for r in (rows or [])),
        K_TRACKS: sorted({i for r in (rows or []) for i in
                          (r[K_MAIN] + r[K_SUB] + r[K_DP])}),
    }
