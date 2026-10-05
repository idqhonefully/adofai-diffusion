# -*- coding: utf-8 -*-
"""raw dict → 规范化模型（`docs/36` §6）。

**版本适配器**：已知版本走对应读法；未知版本 **不拒绝** ——
形状还在就尽力按标准读法解析（+ 报警），形状整个变了才进 generic 兜底。

**禁止**在本文件散落 `if version == N:` —— 加版本 = 加一个适配器 / 给
`aliases.py` 加候选路径。

**禁止**出现任何字段名字面量：取值一律 `coerce.take(obj, al.XXX, …)`。
"""

from __future__ import annotations

from . import aliases as al
from . import coerce, model, probe, tempo
from . import expand as expand_mod
from .coerce import MISSING
from .report import ParseReport

# ---------------------------------------------------------------- 版本适配器
_ADAPTERS = {}          # version → reader（下方注册）


def _register(ver):
    def deco(fn):
        _ADAPTERS[ver] = fn
        return fn
    return deco


# ---------------------------------------------------------------- 嵌套拆包
def _unwrap(raw, rep):
    """上游可能把工程多包一层（`{project:{…}}`）⇒ 找出来并记账。"""
    if (coerce.locate(raw, al.ROOT_MARKERS, MISSING)[1]
            or coerce.locate(raw, al.ROOT_TRACKS, MISSING)[1]):
        return raw, ()
    queue = [(raw, ())]
    n = 0
    while queue and n < 64:
        obj, path = queue.pop(0)
        n += 1
        for k, v in obj.items():
            if not isinstance(v, dict):
                continue
            if (coerce.locate(v, al.ROOT_MARKERS, MISSING)[1]
                    or coerce.locate(v, al.ROOT_TRACKS, MISSING)[1]):
                rep.guess("工程多包了一层", list(path + (k,)))
                return v, path + (k,)
            if len(path) < 3:
                queue.append((v, path + (k,)))
    return None, ()


# ---------------------------------------------------------------- generic 兜底
def _harvest_generic(raw, rep):
    """形态整个变了：递归找「带拍位键的对象列表」，能救多少救多少。**强警告**。"""
    points = []

    def walk(o, depth):
        if depth > 5:
            return
        if isinstance(o, list):
            for it in o:
                if isinstance(it, dict) and coerce.locate(it, al.MARK_BEAT, MISSING)[1]:
                    points.append(it)
                else:
                    walk(it, depth + 1)
        elif isinstance(o, dict):
            for v in o.values():
                walk(v, depth + 1)

    walk(raw, 0)
    rep.warn("形态无法识别 ⇒ generic 兜底：只捞到 {} 个带拍位的对象，"
             "轨道/变速信息缺失，**不建议写回**".format(len(points)))
    return points


def role_from_lane_name(name: str) -> str:
    """★ **内置轨**按**轨名**认角色（`docs/42`）。

    为什么需要：我们投过去的是内置踩点轨（`type = "beat"`），而
    `role_for_type("beat")` 一律给 `main` ⇒ 所有泳道都会被认成主轨。
    而我们的泳道名字就是 `ADO·主轨 trk0` / `ADO·双押轨` 这种，名字里带着角色。

    ★ 这也顺带保住了「**用户把点拖到另一条泳道 = 改角色**」这个信号
      （拖过去 ⇒ 轨名变了 ⇒ 角色变了）。若改用 `attrs.adbRole` 来判，
      那个信号反而会**丢**（attrs 是合并写入的，拖轨不会清掉旧标记）。

    认不出 ⇒ 返回 `""`（调用方退回按 type 认）。
    """
    s = (name or "").strip()
    if not s:
        return ""
    for role, label in al.ROLE_NAME.items():
        if s == label or s.startswith(label + " "):
            return role
    # ★★ 显式泳道的名字（`ADO·三押轨` ⇒ 角色 **dp**）。漏了这一步的后果是真机实测过的：
    #   三押轨的 67 个点被对账判成「删除」，而且会被并进**主轨**采音。
    for lane, label in al.LANE_NAME.items():
        if s == label or s.startswith(label + " "):
            return al.LANE_ROLE.get(lane, "")
    return ""


def role_for_type(track_type: str) -> str:
    """`type` → 角色。

    ★ 只认冒号后面的 **localId**（`<pluginId>:<localId>`，宿主给插件 id 加前缀，
      见 `plugin-api.d.ts` 的 `TrackTypeSchema.id` 与 example-basic 的
      `api.id + ":flip"`）⇒ 我们换插件 id 也不失效。
    """
    t = (track_type or "").strip()
    if not t:
        return al.ROLE_MAIN                       # 缺省 = 内置音砖轨
    if al.TYPE_SEP in t:
        local = t.rsplit(al.TYPE_SEP, 1)[1]
        if local in al.ROLE_BY_LOCAL:
            return al.ROLE_BY_LOCAL[local]
        return al.ROLE_OFF                        # 别人的插件轨：当控制轨，不当音
    return al.ROLE_BY_TYPE.get(t, al.ROLE_MAIN)


def role_track_role(track_type: str) -> str:
    """★ 只认**我们插件**的角色轨：`<pluginId>:<main|sub|dp|off>`（否则 `""`）。

    与 `role_for_type` 的三处**故意不同**（分段采音的自动填充必须用这个）：

    * 内置轨（无 `type` / `"beat"`）→ `""`。项目里每条普通踩点轨都是内置轨，
      而 `role_for_type` 把内置轨当**主音轨**（对采音是对的），
      但拿它当「从这儿起这条轨是什么角色」的指令，会**凭空多出成批分段**；
    * 宿主自带 ADOFAI 导出插件的控制轨（`bpm` / `twirl`）→ `""`。
      `role_for_type` 把它们当 `off`（不采音，正确），但它上面的点是**变速**，
      不是角色指令；
    * 别人的插件轨 → `""`。
    """
    t = (track_type or "").strip()
    if al.TYPE_SEP not in t:
        return ""
    local = t.rsplit(al.TYPE_SEP, 1)[1]
    return local if local in al.ROLE_TRACK_LOCALS else ""


# ---------------------------------------------------------------- 标准读法
def _read_standard(proj_raw, rep, tempo_offset=True):
    p = model.Project(raw=proj_raw)
    p.app = coerce.as_str(coerce.take(proj_raw, al.ROOT_APP, "", rep, "应用标识"))
    p.fmt_version = coerce.as_int(coerce.take(proj_raw, al.ROOT_VERSION, 0, rep, "格式版本"), 0)
    p.name = coerce.as_str(coerce.take(proj_raw, al.ROOT_NAME, "", rep, "工程名"))
    p.base_bpm = coerce.as_float(coerce.take(proj_raw, al.ROOT_BASE_BPM, 0.0, rep, "基准BPM"), 0.0)
    p.offset_ms = coerce.as_float(coerce.take(proj_raw, al.ROOT_OFFSET, 0.0, rep, "音频偏移"), 0.0)
    p.audio_name = coerce.as_str(coerce.take(proj_raw, al.ROOT_AUDIO_NAME, "", rep, "音频文件名"))
    p.audio_md5 = coerce.as_str(coerce.take(proj_raw, al.ROOT_AUDIO_MD5, "", rep, "音频指纹"))
    p.bpm_locked = coerce.as_bool(coerce.take(proj_raw, al.ROOT_BPM_LOCKED, False, rep, "BPM锁定"))

    # ------------------------------------------------------------ 轨
    tracks_raw, kw = coerce.locate(proj_raw, al.ROOT_TRACKS, [])
    rep.key_tracks = kw
    if kw is None:
        rep.default("轨道列表", [])
    tracks_raw = coerce.one_or_many(tracks_raw)
    unknown_types = {}
    seen_ids = set()
    for i, t in enumerate(tracks_raw):
        if not isinstance(t, dict):
            rep.drop("非对象的轨", i)
            continue
        tr = model.Track(idx=i, raw=t)
        tr.id = coerce.as_str(coerce.take(t, al.TRACK_ID, "", rep, "轨id"))
        tr.name = coerce.as_str(coerce.take(t, al.TRACK_NAME, "", rep, "轨名"))
        tr.type = coerce.as_str(coerce.take(t, al.TRACK_TYPE, "", rep, "轨类型"))
        tr.hidden = coerce.as_bool(coerce.take(t, al.TRACK_HIDDEN, False, rep, "轨隐藏"))
        tr.locked = coerce.as_bool(coerce.take(t, al.TRACK_LOCKED, False, rep, "轨锁定"))
        tr.color = coerce.as_str(coerce.take(t, al.TRACK_COLOR, "", rep, "轨颜色"))
        if not tr.id:
            rep.drop("无 id 的轨（无法挂点）", {"下标": i, "轨名": tr.name})
        elif tr.id in seen_ids:
            rep.warn("轨 id 重复：{} ⇒ 后一条的点会与前者混淆".format(tr.id))
        seen_ids.add(tr.id)
        # ★ 角色怎么定（`docs/42`）：
        #   我们现在往宿主里投的是**内置踩点轨**（`type = "beat"`），而
        #   `role_for_type("beat")` 一律给 `main` ⇒ **所有泳道都会被认成主轨**，
        #   双押轨的点会被误判、用户把点拖到别的泳道也看不出来。
        #   所以：**内置轨先按轨名认角色**（我们的泳道名字就是 `ADO·主轨 trk0`
        #   这种），认不出再退回按 type 认。带插件 type 的轨不受影响。
        lane_role = ""
        if not tr.type or tr.type == al.TYPE_BUILTIN:
            lane_role = role_from_lane_name(tr.name)
        tr.role = lane_role or role_for_type(tr.type)
        if tr.role == al.ROLE_OFF and tr.type:
            unknown_types[tr.type] = unknown_types.get(tr.type, 0) + 1
        p.tracks.append(tr)
    if unknown_types:
        rep.guess("未知轨类型按「非音轨」处理"
                  "（角色写在 type 的 localId 上：…:main / :sub / :dp / :off）",
                  unknown_types)

    # ------------------------------------------------------------ 变速点
    bp_raw, kw2 = coerce.locate(proj_raw, al.ROOT_BPM_POINTS, [])
    rep.key_bpm = kw2
    if kw2 is None:
        rep.default("变速点列表", [])
    for i, b in enumerate(coerce.one_or_many(bp_raw)):
        if not isinstance(b, dict):
            rep.drop("非对象的变速点", i)
            continue
        bp = model.BpmPoint(idx=i, raw=b)
        bp.beat = coerce.as_beat(coerce.take(b, al.BPM_BEAT, 0.0, rep, "变速点拍位"), 0.0)
        bp.mode = coerce.as_str(coerce.take(b, al.BPM_MODE, al.MODE_MULT, rep, "变速模式"))
        bp.value = coerce.as_float(coerce.take(b, al.BPM_VALUE, 0.0, rep, "变速值"), 0.0)
        p.bpm_points.append(bp)

    # ------------------------------------------------------------ tempo（只读换算器）
    p.tempo = tempo.build(p.base_bpm, p.offset_ms, p.bpm_points,
                          include_offset=tempo_offset, rep=rep)
    rep.guess("时间锚点：time_of_beat(0) = offsetMs",
              "实测样本无法反证；若对端为 0 基，请把 include_offset 置 False")

    # ------------------------------------------------------------ 点
    marks_raw, kw3 = coerce.locate(proj_raw, al.ROOT_MARKERS, [])
    rep.key_markers = kw3
    if kw3 is None:
        rep.default("拍点列表", [])
    marks_raw = coerce.one_or_many(marks_raw)
    bpm_track_ids = {t.id for t in p.tracks
                     if t.type.endswith(al.TYPE_SEP + al.PLUGIN_LOCAL_BPM)}
    drift_n = 0
    drift_max = 0.0

    for i, m in enumerate(marks_raw):
        if not isinstance(m, dict):
            rep.drop("非对象的点", i)
            continue
        pt = model.Point(idx=i, raw=m)
        pt.id = coerce.as_str(coerce.take(m, al.MARK_ID, "", rep, "点id"))
        pt.track_id = coerce.as_str(coerce.take(m, al.MARK_TRACK, "", rep, "所属轨"))
        pt.beat = coerce.as_beat(coerce.take(m, al.MARK_BEAT, 0.0, rep, "拍位"), 0.0)

        attrs, aw = coerce.locate(m, al.MARK_ATTRS, MISSING)
        if aw is not None:
            rep.hit("点属性", aw)
            pt.attrs = attrs if isinstance(attrs, dict) else {}
        else:
            pt.attrs = {}

        lp, lw = coerce.locate(m, al.MARK_LOOP, MISSING)
        if lw is not None:
            rep.hit("循环规格", lw)
            if isinstance(lp, dict):
                pt.loop = model.LoopSpec(
                    interval=coerce.as_float(
                        coerce.take(lp, al.LOOP_INTERVAL, 1.0, rep, "循环间隔"), 1.0),
                    count=coerce.as_int(
                        coerce.take(lp, al.LOOP_COUNT, 0, rep, "循环次数"), 0),
                    exclude=tuple(coerce.as_int(x, 0) for x in coerce.one_or_many(
                        coerce.take(lp, al.LOOP_EXCLUDE, [], rep, "循环排除"))),
                    raw=lp)
            else:
                rep.drop("循环规格不是对象", {"point": pt.id, "got": type(lp).__name__})

        par, pw = coerce.locate(m, al.MARK_PARENT, MISSING)
        if pw is not None:
            rep.hit("母点id", pw)
            pt.parent_id = coerce.as_str(par, "") or None

        # 时间：★ 优先用原文自带的（上游将来可能真落一个），否则用只读 map 换算
        tg, tw = coerce.locate(m, al.MARK_TIME, MISSING)
        if tw is not None:
            rep.hit("点时间戳", tw)
            pt.time_ms = coerce.as_time_ms(tg, 0.0)
            pt.src_time = True
            ref = p.tempo.time_of_beat(pt.beat)
            d = abs(pt.time_ms - ref)
            if d > 0.5:
                drift_n += 1
                drift_max = max(drift_max, d)
        else:
            pt.time_ms = p.tempo.time_of_beat(pt.beat)

        if pt.track_id in bpm_track_ids:
            st = coerce.as_str(coerce.take(pt.attrs, al.ATTR_SPEED_TYPE, "", rep, "插件变速类型"))
            if st:
                p.bpm_events.append(model.BpmEvent(
                    beat=pt.beat, speed_type=st,
                    value=coerce.as_float(
                        coerce.take(pt.attrs, al.ATTR_SPEED_VALUE, 0.0, rep, "插件变速值"), 0.0),
                    track_id=pt.track_id, point_id=pt.id))

        p.points.append(pt)

    if drift_n:
        rep.warn("{} 个点自带的 timeMs 与本地算出的时间不符（最大 {:.1f} ms）"
                 "⇒ 已优先采用原文值".format(drift_n, drift_max))

    # ------------------------------------------------------------ 计数
    beats = [x.beat for x in p.points]
    n_dup = len(beats) - len(set(round(b, 9) for b in beats))
    rep.count(n_tracks=len(p.tracks), n_points=len(p.points),
              n_bpm=len(p.bpm_points), n_bpm_events=len(p.bpm_events),
              n_dup_beats=n_dup,
              n_empty_tracks=sum(1 for t in p.tracks if not p.points_of(t.id)))
    return p


_register(2)(_read_standard)


# ---------------------------------------------------------------- 入口
def parse(raw, source: str = "", tempo_offset: bool = True,
          do_expand: bool = True, unwrap: bool = True, expand_loops: bool = True):
    """外部数据 → `(Project, ParseReport)`。**永不抛异常。**"""
    rep = ParseReport(source)
    if not isinstance(raw, dict):
        rep.parser = al.PARSER_BROKEN
        rep.error("对端数据不是对象（{}）⇒ 不是工程".format(type(raw).__name__))
        return model.Project(rep=rep, raw={}), rep

    proj_raw = raw
    if unwrap:
        proj_raw, nest = _unwrap(raw, rep)
        rep.nested_at = nest
        if proj_raw is None:
            rep.parser = al.PARSER_GENERIC
            rep.guess("generic 兜底", "找不到轨道/拍点列表")
            pts = _harvest_generic(raw, rep)
            p = model.Project(rep=rep, raw=raw)
            p.points = [model.Point(idx=i, raw=m) for i, m in enumerate(pts)]
            return p, rep

    ver_raw, ver_where = coerce.locate(proj_raw, al.ROOT_VERSION, MISSING)
    ver = coerce.as_int(ver_raw, 0)
    rep.fmt_version = ver
    if ver_where is not None:
        rep.hit("格式版本", ver_where)
    reader = _ADAPTERS.get(ver) if ver_where is not None else None
    if reader is not None:
        rep.parser = al.PARSER_NESTED if rep.nested_at else al.PARSER_STANDARD
    elif ver_where is None:
        # ★ 没有版本号 = 插件的 `api.project.snapshot()`（`plugin-api.d.ts`）。
        #   按 §5 的教条：**用形状决定走哪条路，不看版本** ⇒ 标准读法 + 不报警。
        rep.parser = al.PARSER_NESTED if rep.nested_at else al.PARSER_SNAPSHOT
        rep.guess("无版本号 ⇒ 按插件快照形状解析（点自带 timeMs）", al.PARSER_SNAPSHOT)
        reader = _read_standard
    else:
        shape = probe.caps(proj_raw)["has_shape"]
        if not shape:
            rep.parser = al.PARSER_GENERIC
            rep.guess("generic 兜底", "未知版本且无已知形状")
            pts = _harvest_generic(proj_raw, rep)
            p = model.Project(rep=rep, raw=raw)
            p.points = [model.Point(idx=i, raw=m) for i, m in enumerate(pts)]
            return p, rep
        rep.warn("未知格式版本 {} ⇒ 按标准读法尽力解析（请核对）".format(ver))
        rep.guess("未知版本按标准读法", ver)
        reader = _read_standard
        rep.parser = al.PARSER_NESTED if rep.nested_at else al.PARSER_STANDARD

    p = reader(proj_raw, rep, tempo_offset)
    p.raw = raw                       # ★ 原文（外层）——emit 的 passthrough 基准
    p.fmt_version = ver or p.fmt_version
    if rep.nested_at:
        rep.guess("工程包在外层键下", list(rep.nested_at))

    if do_expand and expand_loops:
        expand_mod.expand(p, rep, synth_only=True)
    return p, rep


def load_text(text: str, source: str = "", **kw):
    """JSON 文本 → `(Project, ParseReport)`；截断/坏 JSON **优雅失败**。"""
    import json
    try:
        raw = json.loads(text)
    except Exception as e:                       # noqa: BLE001
        rep = ParseReport(source)
        rep.parser = al.PARSER_BROKEN
        rep.error("JSON 解析失败：{}".format(e))
        return model.Project(rep=rep, raw={}), rep
    return parse(raw, source=source, **kw)


def load_file(path: str, **kw):
    """`*.bdg` 文件 → `(Project, ParseReport)`。"""
    with open(path, "rb") as f:
        data = f.read()
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    return load_text(data.decode("utf-8", errors="replace"), source=path, **kw)
