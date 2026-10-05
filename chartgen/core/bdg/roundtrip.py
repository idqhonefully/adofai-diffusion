# -*- coding: utf-8 -*-
"""往返对账：我们投送的 onset ↔ 编辑器里回来的点（`docs/38`）。

纯逻辑、零依赖（不碰 IO / 网络），所以能离线单测。

    sent（我们推过去的）  +  project（收回来的快照）  →  四类编辑 + 新进度

**为什么必须对账而不是信它**：宿主的 `addMarker`/`moveMarker` 都走 `snapped()`，
吸附开着就会挪点；插件 API 又没暴露 `force`（`api.ts:315`）⇒ 我们只能
**读回来自己比**，并把偏移**如实报出来**（不许静默）。

★ 本文件的 JSON 键一律走 `aliases.K_*`（它们与上游字段同名，见 `aliases.py` 的说明）。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import aliases as al
from . import coerce, snap
from .coerce import MISSING

BUDGET_MS = 25.0          # 与 `core/dp_angle.SKEW_MAX_MS` / `snap.BUDGET_MS` 同口径
EPS_BEAT = 1e-9           # beat 视为「没动」的阈值（宿主 round() 精度是 1e-6）


@dataclass
class Sent:
    """我们投送的一个音。

    ★ `ms` 是**我们这边**的时间（毫秒）。现在它随载荷一起过去，
    由插件在「锚设好之后」换算成拍位（`docs/41` §4）。
    """
    idx: int
    beat: float
    role: str
    src: str = ""
    ms: float | None = None
    # ★ 这个音来自哪几条**源轨**（`docs/42` 分泳道用）。空 = 不知道（比如收回来的点）。
    src_tracks: tuple = ()
    # ★★ 显式泳道（2026-10 · 三押）：空 = 按 `<role>:<源轨>` 自动算。
    #   用来把**押数 ≥3** 的双押点分到 `dp:3`（角色仍是 `dp`，收回那边不受影响）。
    lane: str = ""


ORDER_ROLE = {al.ROLE_MAIN: 0, al.ROLE_SUB: 1, al.ROLE_DP: 2, al.ROLE_OFF: 3}


def lane_of(role: str, src_tracks, lane: str = "") -> str:
    """★ 一个 onset 该进哪条「泳道」（`docs/42`）。

    泳道键是 **`<role>:<源轨>`**；**双押**（`dp`）不给源轨维 —— 它本身就是
    「单开的那个轨道」（用户口径：「采多轨道的双押时，把双押单开一个轨道」）。

    源轨取**簇里第一条**（`src_tracks` 升序 ⇒ 就是最靠前那条）。跨轨簇
    （一次按键好几条轨同时响）会归到最早那条，**但全部源轨都记进 attrs**
    （`adbTracks`）并在载荷里报 `n_cross_track` —— 不许静默。

    ★ `lane` 非空则**直接用它**（2026-10 · 三押：`dp:` 与 `dp:3` 两条泳道同角色）。
    """
    if lane:
        return str(lane)
    st = tuple(int(x) for x in (src_tracks or ()) if int(x) >= 0)
    if role == al.ROLE_DP:
        return "%s:" % al.ROLE_DP
    if not st:
        return "%s:" % role
    return "%s:%d" % (role, st[0])


def _lane_of(o) -> str:
    if isinstance(o, dict):
        return str(o.get(al.K_LANE) or "")
    # ★ 属性名与载荷键名**同一个字面量**（`al.K_LANE`）—— 本文件禁止硬写字段名，
    #   所以这里也不能写 `"lane"`（`test_bdg_parse` 的守卫会抓）。
    return str(getattr(o, al.K_LANE, "") or "")


def _role_of(o) -> str:
    if isinstance(o, dict):
        return str(o.get(al.K_ROLE) or al.ROLE_MAIN)
    return str(getattr(o, al.K_ROLE, "") or al.ROLE_MAIN)


def _tracks_of(o) -> tuple:
    if isinstance(o, dict):
        return tuple(o.get(al.K_SRC_TRACKS) or ())
    return tuple(getattr(o, al.K_SRC_TRACKS, ()) or ())


def split_lanes(sent) -> dict:
    """`Sent` 列表 → `{泳道: [items]}`。"""
    out: dict = {}
    for o in sent:
        out.setdefault(lane_of(_role_of(o), _tracks_of(o), _lane_of(o)), []).append(o)
    return out


def lane_names(sent) -> dict:
    """泳道 → BDG 轨名。

    ★ 同一角色下**只有一个源轨**时**不加后缀**（`ADO·主轨`）⇒ 单轨工程的布局
      与老版本**逐字节不变**；真的有多个源轨时才分成 `ADO·主轨 trk0` / `trk1`…
    """
    lanes = split_lanes(sent)
    per_role: dict = {}
    for items in lanes.values():
        role = _role_of(items[0])
        for t in _tracks_of(items[0]):
            per_role.setdefault(role, set()).add(t)
    out = {}
    for lane, items in lanes.items():
        # ★ 显式泳道名优先（`dp:3` → 「ADO·三押轨」）
        if lane in al.LANE_NAME:
            out[lane] = al.LANE_NAME[lane]
            continue
        role = _role_of(items[0])
        base = al.ROLE_NAME.get(role, role)
        tr = _tracks_of(items[0])
        if role == al.ROLE_DP or not tr or len(per_role.get(role) or ()) <= 1:
            out[lane] = base
        else:
            out[lane] = "%s trk%d" % (base, tr[0])
    return out


@dataclass
class Edit:
    idx: object            # 新增的点没有 idx（None）
    status: str
    beat_in: float = 0.0
    beat_out: float = 0.0
    role_in: str = ""
    role_out: str = ""
    drift_ms: float = 0.0
    point: object = None
    stale_run: bool = False

    @property
    def moved(self) -> bool:
        return self.status == al.EDIT_MOVED

    def to_dict(self) -> dict:
        return {al.K_IDX: self.idx, al.K_STATUS: self.status,
                "beat_in": round(self.beat_in, 6), "beat_out": round(self.beat_out, 6),
                "role_in": self.role_in, "role_out": self.role_out,
                "drift_ms": round(self.drift_ms, 4)}


# ---------------------------------------------------------------- 写标记
def tag_attrs(idx, run: str, role: str, src: str = "", extra=None) -> dict:
    """要写进 `marker.attrs` 的对账标记（宿主那边是**合并**写入，不会冲掉别的字段）。"""
    out = {al.SYNC_IDX[0]: int(idx), al.SYNC_RUN[0]: str(run),
           al.SYNC_ROLE[0]: str(role)}
    if src:
        out[al.SYNC_SRC[0]] = str(src)
    if extra:
        out.update(extra)
    return out


def read_tag(point) -> dict:
    """从回来的点里读对账标记；没有 ⇒ 字段给默认值。"""
    a = point.attrs if isinstance(point.attrs, dict) else {}
    idx, _ = coerce.locate(a, al.SYNC_IDX, MISSING)
    run, _ = coerce.locate(a, al.SYNC_RUN, MISSING)
    role, _ = coerce.locate(a, al.SYNC_ROLE, MISSING)
    return {al.K_IDX: idx if idx is not MISSING else None,
            al.K_RUN: run if run is not MISSING else "",
            al.K_ROLE: role if role is not MISSING else ""}


def build_import(onsets, run: str, names=None, clear: bool = True,
                 base_bpm: float = 0.0, offset_ms: float = 0.0) -> dict:
    """把「我们的 onsets」打成 `import` 载荷。

    `onsets` 可以是 `Sent`、dict（`{idx,beat,role,src,ms}`）或任何有 `.beat/.role` 的对象。

    ★★ 载荷里多两样（`docs/41`）：
      · `baseBpm` / `offsetMs` —— **我们谱的时序锚**。以前只推拍位、锚留宿主的，
        用户就得自己猜（issue 2）；
      · 每个点的 **`ms`** —— 让**插件**在「锚已经设好之后」用宿主的模型换算拍位，
        省掉「谁先设锚」的顺序坑（issue 2/3 的共同根子）。
      `beat` 仍然发（旧版插件当回退用）。
    """
    names = names or {}
    lanes = split_lanes(onsets)
    auto_names = lane_names(onsets)
    n_cross = 0
    out_tracks = []
    for lane in sorted(lanes, key=lambda k: (ORDER_ROLE.get(k.split(":")[0], 9), k)):
        items = lanes[lane]
        role = _role_of(items[0])
        rows = []
        for j, o in enumerate(items):
            if isinstance(o, dict):
                beat = float(o.get(al.K_BEAT) or 0.0)
                idx = int(o[al.K_IDX]) if o.get(al.K_IDX) is not None else j
                src = str(o.get("src") or "")
                ms = o.get(al.K_MS)
            else:
                beat = float(getattr(o, al.K_BEAT, 0.0))
                idx = int(getattr(o, al.K_IDX, j))
                src = str(getattr(o, "src", "") or "")
                ms = getattr(o, al.K_MS, None)
            tr = _tracks_of(o)
            extra = {}
            if tr:
                extra[al.SYNC_TRACK[0]] = int(tr[0])
                if len(tr) > 1:
                    extra[al.SYNC_TRACKS[0]] = [int(x) for x in tr]
                    n_cross += 1
            row = {al.K_IDX: idx, al.K_BEAT: beat,
                   al.K_ATTRS: tag_attrs(idx, run, role, src, extra)}
            if ms is not None:
                row[al.K_MS] = round(float(ms), 6)
            rows.append(row)
        out_tracks.append({al.K_ROLE: role,
                           al.K_LANE: lane,
                           al.K_NAME: names.get(lane) or names.get(role)
                           or auto_names.get(lane, role),
                           al.K_CLEAR: bool(clear),
                           al.K_ONSETS: rows})
    out = {al.K_RUN: run, al.K_TRACKS: out_tracks,
           "n_onsets": sum(len(t[al.K_ONSETS]) for t in out_tracks),
           al.K_N_LANES: len(out_tracks)}
    if n_cross:
        # ★ 不许静默：跨轨簇（一次按键是好几条轨同时响）被归到**最早那条源轨**的泳道
        out[al.K_N_CROSS_TRACK] = n_cross
    if base_bpm:
        out[al.K_BASE_BPM] = float(base_bpm)
    if offset_ms or base_bpm:
        out[al.K_OFFSET_MS] = float(offset_ms)
    return out


# ---------------------------------------------------------------- 对账
def _drift_ms(tempo, beat_in: float, beat_out: float, point=None) -> float:
    """漂移 = **拍位**移动了多少毫秒（用我们的 tempo 表换算）。

    ★ 不要拿点自带的 `timeMs` 当基准：那是**当前拍位**的时间戳，
      而我们要量的是「现在的拍位」相对「我们推过去的拍位」差了多少。
    """
    if tempo is not None:
        try:
            return abs(tempo.time_of_beat(beat_out) - tempo.time_of_beat(beat_in))
        except Exception:                                   # noqa: BLE001
            pass
    return abs(beat_out - beat_in) * snap.FALLBACK_MS_PER_BEAT


def _snap_suspect(beats, divs=snap.SNAP_DIVS):
    """动过的 beat **全都落在同一档网格上** ⇒ 偏移就是吸附干的。"""
    if not beats:
        return None
    for d in divs:
        if all(abs(b * d - round(b * d)) < 1e-6 for b in beats):
            return d
    return None


def compare(sent, project, run: str = "", tempo=None,
            budget_ms: float = BUDGET_MS) -> dict:
    """`sent`（Sent 列表）⇄ `project`（收回来的解析结果）⇒ 对账。"""
    sent = list(sent)
    role_tracks = {}
    for t in (getattr(project, al.K_TRACKS, None) or []):
        role_tracks[t.id] = t.role
    by_idx: dict = {}
    added: list = []
    stale: list = []
    for p in (getattr(project, al.K_POINTS, None) or []):
        tg = read_tag(p)
        role_here = role_tracks.get(p.track_id, "")
        if tg[al.K_IDX] is None:
            if role_here:                       # 用户自己加的点（在角色轨上）
                added.append(Edit(None, al.EDIT_ADDED, 0.0, p.beat, "",
                                  role_here, 0.0, p))
            continue
        if run and tg[al.K_RUN] and tg[al.K_RUN] != run:
            stale.append(Edit(tg[al.K_IDX], al.EDIT_STALE, 0.0, p.beat, tg[al.K_ROLE],
                              role_here, 0.0, p, True))
            continue
        by_idx.setdefault(int(tg[al.K_IDX]), []).append(p)

    edits: list = []
    moved_beats: list = []
    drift_max = 0.0
    drift_over = 0

    def _take(idx, role):
        """从 `idx` 的候选里挑一个（★ 同一 idx 可能有多条）。

        ★★ 2026-10 · 真机踩到：**双押点的 idx 与主轨点的 idx 共用 onset 下标**
          （双押 = 同一下同时按下的第二个轨），所以一个 idx 常常有 2 条点。
          以前 `by_idx` 是**一对一**的（`setdefault`）⇒ 撞车的那个直接被挤掉，
          对账把它们全判成「**删除**」：实测 544 点里 216 个多押点报 删除，
          而谱面其实一个都没丢（收回后 544 onset 不变）。
          按**角色**挑：同角色的优先；都没同角色就拿第一条（留给下面判 role_changed）。
        """
        cands = by_idx.get(idx)
        if not cands:
            return None
        for k, c in enumerate(cands):
            if role_tracks.get(c.track_id, "") in ("", role):
                return cands.pop(k)
        return cands.pop(0)

    for s in sent:
        p = _take(s.idx, s.role)
        if p is None:
            edits.append(Edit(s.idx, al.EDIT_DELETED, s.beat, 0.0, s.role, "", 0.0, None))
            continue
        role_here = role_tracks.get(p.track_id, s.role)
        dms = _drift_ms(tempo, s.beat, p.beat, p)
        moved = abs(p.beat - s.beat) > EPS_BEAT
        changed = bool(role_here) and role_here != s.role
        if moved:
            status = al.EDIT_MOVED
            moved_beats.append(p.beat)
            if dms > budget_ms:
                drift_over += 1
        elif changed:
            status = al.EDIT_ROLE
        else:
            status = al.EDIT_KEPT
        drift_max = max(drift_max, dms)
        edits.append(Edit(s.idx, status, s.beat, p.beat, s.role, role_here, dms, p))

    # 没被任何 sent 认领的点（含同 idx 撞车里剩下的那些）都算残留
    orphan = []
    for k, cands in by_idx.items():
        for v in cands:
            orphan.append(Edit(k, al.EDIT_STALE, 0.0, v.beat, "",
                               role_tracks.get(v.track_id, ""), 0.0, v, True))

    counts = {st: sum(1 for e in edits if e.status == st)
              for st in (al.EDIT_KEPT, al.EDIT_MOVED, al.EDIT_ROLE, al.EDIT_DELETED)}
    div = _snap_suspect(moved_beats)
    return {
        al.K_RUN: run,
        "n_sent": len(sent),
        "n_back": len(getattr(project, al.K_POINTS, None) or []),
        al.K_COUNTS: counts,
        al.K_N_ADDED: len(added),
        "n_stale": len(stale) + len(orphan),
        "drift_max_ms": round(drift_max, 4),
        "drift_over": drift_over,
        "budget_ms": budget_ms,
        "snap_suspect": bool(div),
        "snap_div": div,
        al.K_EDITS: [e.to_dict() for e in edits],
        al.K_ADDED: [e.to_dict() for e in added],
        al.K_STALE: [e.to_dict() for e in (stale + orphan)],
        al.K_ONSETS: onsets_out(sent, edits, added),
    }


def onsets_out(sent, edits, added) -> list:
    """★ 收回来的**新进度**：删除的丢掉、移动的用新拍位、新增的补上（idx=None）。

    ★ **带上源轨**（`src_tracks`）—— 以前只带 idx/beat/role/status，
      `adopt_onsets` 造 Onset 时又只有 time/velocity/pitch ⇒ **源轨信息在收回这一步
      被吃掉**，于是「收回改动」之后再投射，5 条泳道会塌回一条 `main:`
      （实测过的真实不一致）。现在原样带过去。

    ★★ 2026-10：键必须是 **`(idx, 角色)`**，不能只用 `idx`。
      双押点与主轨点**共用 onset 下标**，只按 idx 存 ⇒ 两条行互相覆盖，
      于是主轨那一行会**拿到双押的拍位**；撞车行拍位相同，再被下游合并 ⇒
      真机实测：投 544 点、收回重建只剩 **112 个 onset**（看着像「收回丢了一大半」）。
    """
    alive: dict = {}
    for e in edits:
        if e.status == al.EDIT_DELETED:
            continue
        alive.setdefault((e.idx, e.role_in), e)
    out = []
    for s in sent:
        e = alive.get((s.idx, s.role))
        if e is None:
            continue
        row = {al.K_IDX: s.idx, al.K_BEAT: e.beat_out,
               al.K_ROLE: e.role_out or s.role,
               al.K_STATUS: e.status, "drift_ms": round(e.drift_ms, 4)}
        tr = tuple(getattr(s, al.K_SRC_TRACKS, ()) or ())
        if tr:
            row[al.K_SRC_TRACKS] = [int(x) for x in tr]
        out.append(row)
    for i, e in enumerate(added):
        out.append({al.K_IDX: None, al.K_BEAT: e.beat_out, al.K_ROLE: e.role_out,
                    al.K_STATUS: al.EDIT_ADDED, "drift_ms": 0.0, "new": i})
    out.sort(key=lambda x: x[al.K_BEAT])
    return out


def report_text(diff: dict) -> str:
    c = diff.get(al.K_COUNTS, {})
    out = ["[往返对账] 投送 {} 点 → 回来 {} 点".format(diff.get("n_sent"), diff.get("n_back"))]
    out.append("    留下 {} · 移动 {} · 换角色 {} · 删除 {} · 新增 {} · 残留 {}".format(
        c.get(al.EDIT_KEPT, 0), c.get(al.EDIT_MOVED, 0), c.get(al.EDIT_ROLE, 0),
        c.get(al.EDIT_DELETED, 0), diff.get(al.K_N_ADDED, 0), diff.get("n_stale", 0)))
    if diff.get("drift_max_ms"):
        out.append("    最大偏移 {:.3f} ms · 超预算({:.0f}ms) {} 个".format(
            diff["drift_max_ms"], diff.get("budget_ms", BUDGET_MS), diff.get("drift_over", 0)))
    if diff.get("snap_suspect"):
        out.append("    ★ 偏移全都落在 1/{} 网格上 ⇒ **是 BDG 的吸附干的**，"
                   "请在顶栏关掉「吸附」再投射".format(diff.get("snap_div")))
    elif diff.get("drift_max_ms", 0) > 0.01:
        out.append("    偏移不像吸附（未对齐网格）⇒ 是人手动拖的，尊重它")
    else:
        out.append("    逐点无损 ✓")
    return "\n".join(out)


# ==================================================================== 收回
# ★ 收回（BDG → 我们，`docs/45`）：把编辑器里**带时值数据的轨道**整条搬回来，
#   让「我们的音轨」就是那些轨（用户口径）。这一半是**纯函数**，离线可测。
def _pt_tracks(point) -> tuple:
    """**点对象**的源轨：先看 `adbTrack`（单数），再看跨轨簇的 `adbTracks`。

    ⚠ `_tracks_of` 是给 `Sent`/dict 用的（值直接挂在对象上），点对象的标记
      住在 `point.attrs` 里 —— 两者不能混用（踩过一次：全部归到 `main:` 空源轨）。
    """
    a = point.attrs if isinstance(getattr(point, al.K_ATTRS, None), dict) else {}
    one, got1 = coerce.locate(a, al.SYNC_TRACK, MISSING)
    if got1 is not MISSING and one is not None:
        try:
            return (int(one),)
        except (TypeError, ValueError):
            pass
    many, got2 = coerce.locate(a, al.SYNC_TRACKS, MISSING)
    if got2 is not MISSING and many is not None:
        try:
            return tuple(int(x) for x in coerce.one_or_many(many))
        except (TypeError, ValueError):
            return ()
    return ()


def _ms_of(point, tempo, offset_ms: float = 0.0) -> float | None:
    """点的毫秒：**原文自带时间戳就用它**（快照解析会带），否则用 tempo 换算。"""
    t = float(point.time_ms or 0.0)
    if t:
        return float(t)
    if tempo is not None:
        try:
            return float(tempo.time_of_beat(float(point.beat)))
        except Exception:                                   # noqa: BLE001
            return None
    return None


def collect_back(project, run: str = "", tempo=None, offset_ms: float = 0.0) -> dict:
    """★ 把编辑器快照里**带我们标记的轨道**整条收回来（`docs/45`）。

    只认「有 `adbIdx` / `adbRole` 标记的轨」—— 用户自己的轨不搬（那是他的素材，
    不是我们的谱）。轨上**用户新加的点**（没有标记）也收，并记 `n_added`。

    返回的载荷结构与插件发来的一致 ⇒ 两边走同一个下游（`sidecar.session.restore_tracks`）。
    """
    pts = list(getattr(project, al.K_POINTS, None) or [])
    tmap = {t.id: t for t in (getattr(project, al.K_TRACKS, None) or [])}
    # ---- ① 先认「哪些轨是我们的」，并给每条轨定角色/源轨（按标记多数派）----
    votes: dict = {}                    # track_id → {(role, src): n}
    n_added = n_skipped = 0
    for p in pts:
        tg = read_tag(p)
        if tg[al.K_IDX] is None and not tg[al.K_ROLE]:
            continue
        tr = _pt_tracks(p)
        key = (tg[al.K_ROLE] or al.ROLE_MAIN, (tr[0] if tr else None))
        votes.setdefault(p.track_id, {})[key] = \
            votes.setdefault(p.track_id, {}).get(key, 0) + 1
    # ---- ② 按**轨**整条收（一条 BDG 轨 = 我们的一条音轨项目）----
    by_lane: dict = {}
    for p in pts:
        vv = votes.get(p.track_id)
        if not vv:
            continue
        tg = read_tag(p)
        trk = tmap.get(p.track_id)
        # 角色/源轨取该轨上的**多数派**（用户把点拖到别处时跟着多数走，但不静默）
        best = max(vv.items(), key=lambda kv: (kv[1], str(kv[0])))
        role, src0 = best[0]
        tr = _pt_tracks(p)
        if not tr and src0 is not None:
            tr = (src0,)
        ms = _ms_of(p, tempo, offset_ms)
        if ms is None:
            n_skipped += 1
            continue
        if tg[al.K_IDX] is None:
            n_added += 1
        key = lane_of(role, tr if tr else ((src0,) if src0 is not None else ()))
        row = {al.K_IDX: int(tg[al.K_IDX]) if tg[al.K_IDX] is not None else None,
               al.K_BEAT: round(float(p.beat), 9), al.K_MS: round(float(ms), 6),
               al.K_SRC_TRACKS: [int(x) for x in tr]}
        by_lane.setdefault(key, {al.K_ROLE: role, al.K_SRC_TRACK: (tr[0] if tr else src0),
                                 al.K_NAME: (trk.name if trk else ""),
                                 al.K_POINTS: []})
        by_lane[key][al.K_POINTS].append(row)
    tracks = []
    for key in sorted(by_lane, key=lambda k: (ORDER_ROLE.get(k.split(":")[0], 9), k)):
        ln = by_lane[key]
        ln[al.K_POINTS].sort(key=lambda q: q[al.K_MS])
        tracks.append({al.K_NAME: ln[al.K_NAME], al.K_ROLE: ln[al.K_ROLE],
                       al.K_LANE: key, al.K_SRC_TRACK: ln[al.K_SRC_TRACK],
                       al.K_POINTS: ln[al.K_POINTS]})
    return {al.K_RUN: run, al.K_TRACKS: tracks,
            al.K_N_POINTS: sum(len(t[al.K_POINTS]) for t in tracks),
            al.K_N_ADDED: n_added, al.K_N_SKIPPED: n_skipped,
            al.K_ANCHOR: {al.K_BASE_BPM: float(getattr(project, "base_bpm", 0.0) or 0.0),
                          al.K_OFFSET_MS: float(getattr(project, "offset_ms", 0.0) or 0.0)}}


def back_lanes(payload: dict) -> list:
    """收回载荷 → 规范化泳道列表（纯函数，不碰 IO）。

    每条 = `{name, role, src_track, lane, points:[{idx,beat,ms,src_tracks}], n, ms_lo, ms_hi}`。
    """
    out = []
    for t in (payload.get(al.K_TRACKS) or []):
        if not isinstance(t, dict):
            continue
        pts = []
        for p in (t.get(al.K_POINTS) or t.get(al.K_ONSETS) or []):
            if not isinstance(p, dict) or p.get(al.K_MS) is None:
                continue
            idx = p.get(al.K_IDX)
            pts.append({al.K_IDX: int(idx) if idx is not None else None,
                        al.K_BEAT: float(p.get(al.K_BEAT) or 0.0),
                        al.K_MS: float(p[al.K_MS]),
                        al.K_SRC_TRACKS: [int(x) for x in
                                          (p.get(al.K_SRC_TRACKS) or ())]})
        pts.sort(key=lambda q: q[al.K_MS])
        role = str(t.get(al.K_ROLE) or al.ROLE_MAIN)
        src = t.get(al.K_SRC_TRACK)
        out.append({al.K_NAME: str(t.get(al.K_NAME) or ""), al.K_ROLE: role,
                    al.K_SRC_TRACK: int(src) if src is not None else None,
                    al.K_LANE: str(t.get(al.K_LANE) or lane_of(role, [])),
                    al.K_POINTS: pts, al.K_N_POINTS: len(pts),
                    al.K_MS_LO: round(pts[0][al.K_MS], 3) if pts else 0.0,
                    al.K_MS_HI: round(pts[-1][al.K_MS], 3) if pts else 0.0})
    return out


def back_text(payload: dict, lanes=None) -> str:
    """收回载荷的人话报告。"""
    lanes = lanes if lanes is not None else back_lanes(payload)
    n_pts = sum(l[al.K_N_POINTS] for l in lanes)
    out = ["[收回] {} 轨 / {} 点（新加 {}）".format(
        len(lanes), n_pts, payload.get(al.K_N_ADDED, 0))]
    for l in lanes[:12]:
        out.append("    · {:22s} {:<5s} {:>4d} 点  {:.1f}~{:.1f}ms".format(
            (l[al.K_NAME] or "?")[:22], l[al.K_ROLE], l[al.K_N_POINTS],
            l[al.K_MS_LO], l[al.K_MS_HI]))
    if len(lanes) > 12:
        out.append("    … 还有 {} 轨".format(len(lanes) - 12))
    return "\n".join(out)
