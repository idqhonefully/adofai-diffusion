"""BDG ⇄ 我们 的**协议层**（`docs/35` §3.5 / `docs/37` §5）。

**传输无关**：这里只做「一条文本进 → 若干文本出」，所以能脱离 socket 单测。
`sidecar/ws.py` 提供帧，`server.py` 的 `/ws` 把两者接起来。

设计口径（沿用本项目一贯的「不许静默」）：

  · 每个写操作都要回 `ack`（成功 / 失败都回）；
  · 不认识的 `type` **不是**静默忽略，而是 `ack{ok:false}` + 记一条 warn；
  · 一次性的 `token` **在升级握手之前**就校验（同机任何程序都可能连上来）；
  · `project` 进来走**同一个 `core.bdg.parse()`**（`docs/36` §9），
    解析报告（命中别名 / 默认 / 丢弃 / 猜测）原样回给对端面板 —— 让他也看得见。
"""

from __future__ import annotations

import json
import os
import time

from core import bdg
from core import segments as seg_mod

from . import ws as WS

PROTO_V = 1
ACCEPTED = [1]                 # 我们接受的协议版本区间（超出 = warn，不拒）
PLUGIN_MIN = "0.1.0"
MAX_WARNS = 12


def _new_run_id() -> str:
    """批次号：时间 + 4 位随机（写进每个点的 attrs，用于区分上一批残留）。"""
    return time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()

# 入站类型（BDG → 我们）
IN_HELLO = "hello"
IN_PROJECT = "project"
IN_SELECTION = "selection"
IN_PLAYHEAD = "playhead"
IN_AUDIO = "audio"
IN_PULL = "pull"
IN_PING = "ping"
IN_PONG = "pong"
IN_ACK = "ack"
# ★ 收回：编辑器里那些**带时值数据的轨道**（`docs/45`）。
#   与 `project` 的区别：这是用户**主动**按「返回数据到谱面生成器」时发的，
#   只带我们的轨道（不是全量快照），是「把音轨搬回来」这件事本身。
IN_TRACKS = "tracks"
# ★ 网格探测：插件拿到一份**裸时间戳**（比如从文件导入）时，先问我们
#   「砖长/相位/分母该是多少」，再把点摆到宿主的格上（`docs/45` §7）。
#   为什么要问我们：格子的数学在 `core/denoise`（Python）这边，插件是 JS，
#   两边各写一份必然分叉 —— 于是让插件**只负责读文件与画点**。
IN_GRID = "grid"

# 出站类型（我们 → BDG）
OUT_ACCEPTED = "accepted"
OUT_FAILED = "failed"
OUT_PONG = "pong"
OUT_PULL = "pull"
OUT_IMPORT = "import"
OUT_ACK = "ack"


class Bridge:
    """一个 sidecar 进程一份；同一时刻**只服务一条**桥连接（够用且简单）。"""

    def __init__(self, app=None, token: str = ""):
        self.app = app
        self.token = token or WS.new_token()
        self.conn = None
        self.authed = False
        self.peer: dict = {}
        self.seq = 0
        self.seq_in = 0
        self.last_project = None          # core.bdg.model.Project
        self.last_rep = None              # core.bdg.ParseReport
        self.selection = None
        self.playhead = None
        self.audio = None
        self.sent: list = []              # 我们投送出去的 onsets（往来对账用）
        self.run = ""                     # 本批批次号
        self.sent_src = ""
        self.adopted = None               # 最近一次收回的对账结果
        self.import_ack = None            # 最近一次投射的回执（含吸附偏移量）
        self.back = None                  # ★ 最近一次「返回数据到谱面生成器」的载荷
        self.back_lanes: list = []
        self.warns: list = []
        self.should_close = False
        self.stats = {"sessions": 0, "rejected": 0, "msgs_in": 0, "msgs_out": 0,
                      "projects": 0, "unknown": 0, "imports": 0, "adopts": 0,
                      "backs": 0, "grids": 0}

    # ------------------------------------------------------------ 信封
    def envelope(self, kind: str, payload: dict | None = None, seq: int | None = None) -> dict:
        self.seq += 1
        msg = {"v": PROTO_V, "type": kind, "seq": self.seq if seq is None else seq,
               "ts": round(time.time() * 1000.0, 1)}
        if payload:
            msg.update(payload)
        return msg

    def dumps(self, msg: dict) -> str:
        return json.dumps(msg, ensure_ascii=False, allow_nan=False)

    def ack(self, ok: bool, error: str = "", **kw) -> str:
        p = {"ok": ok}
        if error:
            p["error"] = error
        p.update(kw)
        return self.dumps(self.envelope(OUT_ACK, p))

    def warn(self, msg: str) -> None:
        self.warns.append(msg)
        if len(self.warns) > MAX_WARNS * 4:
            del self.warns[:MAX_WARNS]

    # ------------------------------------------------------------ 对外发
    def pull_msg(self) -> str:
        """请求一次全量快照。"""
        return self.dumps(self.envelope(OUT_PULL, {}))

    def import_msg(self, tracks=None, bpm_points=None, twirl_beats=None,
                   notes=None) -> str:
        """把我们算好的东西写成他的轨（第 3/4 项落地时用）。"""
        return self.dumps(self.envelope(OUT_IMPORT, {
            "tracks": tracks or [], "bpm_points": bpm_points or [],
            "twirl_beats": twirl_beats or [], "notes": notes or [],
        }))

    # ------------------------------------------------------------ 入站
    def handle_text(self, text: str) -> list:
        self.stats["msgs_in"] += 1
        try:
            msg = json.loads(text)
        except Exception as exc:                              # noqa: BLE001
            return [self.ack(False, f"JSON 解析失败：{exc}")]
        if not isinstance(msg, dict):
            return [self.ack(False, "消息必须是对象")]
        kind = msg.get("type") or ""
        if kind == IN_PING:
            return [self.dumps(self.envelope(OUT_PONG, {}))]
        if kind == IN_HELLO:
            return [self._hello(msg)]
        if not self.authed:
            self.should_close = kind != IN_PING
            return [self.dumps(self.envelope(OUT_FAILED, {"error": "先握手（hello）"}))]
        handler = getattr(self, "_on_" + kind, None)
        if handler is None:
            self.stats["unknown"] += 1
            self.warn(f"不认识的 type={kind!r}（不静默忽略）")
            return [self.ack(False, f"未知 type {kind!r}")]
        # ★ 处理器返回**一条**文本；入口统一成列表，免得调用方遍历字符串
        return [handler(msg)]

    # ---- hello
    def _hello(self, msg) -> str:
        self.peer = {
            "plugin": str(msg.get("plugin") or ""),
            "bdg": str(msg.get("bdg") or msg.get("version") or ""),
            "caps": msg.get("caps") if isinstance(msg.get("caps"), dict) else {},
            "proto": msg.get("v") or PROTO_V,
            "plugin_min": PLUGIN_MIN,
        }
        self.authed = True
        self.stats["sessions"] += 1
        try:
            peer_v = int(msg.get("v") or PROTO_V)
        except (TypeError, ValueError):
            peer_v = PROTO_V
        if peer_v not in ACCEPTED:
            self.warn(f"对端协议版本 {peer_v} 不在我们接受的 {ACCEPTED} 里 "
                      "⇒ 按最低版本对话（不拒）")
        self._push("handshake")
        return self.dumps(self.envelope(OUT_ACCEPTED, {
            "ok": True, "accepted": ACCEPTED, "v": PROTO_V,
            "root": getattr(self.app, "root", ""),
            "plugin_min": PLUGIN_MIN,
            "caps": self.peer.get("caps") or {},
        }))

    # ---- project（全量快照）★ 走同一个软解析
    def _on_project(self, msg) -> str:
        raw = msg.get("project") if isinstance(msg.get("project"), dict) else None
        if raw is None:
            self.stats["unknown"] += 1
            return self.ack(False, "project 消息缺少 project 对象")
        p, rep = bdg.parse(raw, source="ws")
        self.last_project, self.last_rep = p, rep
        self.stats["projects"] += 1
        for w in (rep.warns + rep.errs):
            self.warn(w)
        self._push("project")
        ok_emit, why = bdg.emit.can_emit(rep)
        # ★ 快照本来就不该写回（can_emit=False 是**正常**的）⇒ ok 只看「解出来有没有东西」
        ok = rep.ok and (len(p.points) > 0 or len(p.tracks) > 0)
        err = "" if ok else ("；".join(rep.errs[:3]) or "快照里没有可用的轨/点")
        return self.ack(
            ok, err,
            n_tracks=len(p.tracks),
            n_points=len(p.points),
            n_bpm_events=len(p.bpm_events),
            dup_beats=rep.stats.get("n_dup_beats", 0),
            loops=rep.stats.get("n_loops", 0),
            parser=rep.parser,
            fmt=rep.fmt_version,
            can_emit=ok_emit,
            parse=rep.summary(),
            warn_list=rep.warns[:MAX_WARNS],
            guess_list=[g.get("what") for g in rep.guesses][:MAX_WARNS],
        )

    # ---- 收回：带时值数据的轨道（`docs/45`）★ 插件面板上那个按钮
    def _on_tracks(self, msg) -> str:
        """★「返回数据到谱面生成器」：把编辑器里的轨道整条搬回我们的工程。

        用户口径（2026-10）：「我们的工具使用的音轨**就是 BDG 里面带时值数据的
        音轨**」⇒ 收回来之后，我们界面的音轨列表**就是这些轨**，重建也直接用它们
        （`Session.restore_tracks`，不再从 MIDI 采音）。
        """
        lanes = bdg.roundtrip.back_lanes(msg)
        if not lanes:
            self.stats["unknown"] += 1
            return self.ack(False, "tracks 消息里没有可用的轨（每轨要有 points[].ms）")
        self.back = dict(msg)
        self.back_lanes = lanes
        self.stats["backs"] += 1
        app = self.app
        sess = getattr(app, "session", None) if app is not None else None
        meta, text, info = None, "", None
        if sess is not None:
            try:
                out = sess.restore_tracks(dict(msg))
                meta, text = out.get("meta"), out.get("text")
            except Exception as exc:                            # noqa: BLE001
                self.warn(f"收回落地失败：{exc}")
                return self.ack(False, f"收回落地失败：{exc}",
                                n_tracks=len(lanes))
        else:
            self.warn("收到 tracks 但没有 session 可落库")
        self._push("tracks")
        return self.ack(True, run=msg.get(bdg.aliases.K_RUN),
                        n_tracks=len(lanes),
                        n_points=msg.get(bdg.aliases.K_N_POINTS),
                        n_added=msg.get(bdg.aliases.K_N_ADDED),
                        n_dup=(meta or {}).get("n_dup", 0),
                        n_onsets=(meta or {}).get("n_onsets", 0),
                        report=text)

    # ---- 网格探测：给插件算「这份时间戳该用什么 bpm/相位/分母」（`docs/45` §7）
    def _on_grid(self, msg) -> str:
        """`{type:"grid", times:[ms…], hint?, div?}` ⇒ `{bpm, offsetMs, div, step, phase}`。

        ★ 只算，不落库、不动我们的会话状态（纯函数 + 明确回执）。
        ★ 键名是 **`times`** 不是 `ts`：插件那条信封已经占用了 `ts`（发送时刻），
          用 `ts` 装毫秒数组会被信封覆盖掉（在插件单测里被抓到过一次）。
        """
        raw = msg.get("times")
        if raw is None:
            raw = msg.get("ts")                 # 宽容一点：老/手写的载荷
        if not isinstance(raw, list) or len(raw) < 2:
            return self.ack(False, "grid 消息要带 times（毫秒数组，至少 2 个点）",
                            kind="grid", grid=None)
        try:
            ts = [float(x) for x in raw]
        except (TypeError, ValueError):
            return self.ack(False, "ts 里有非数字", kind="grid", grid=None)
        hint = msg.get("hint")
        div = msg.get("div")
        try:
            from core import ts_source as _ts
            info = _ts.grid_info(ts, hint=(float(hint) if hint else None),
                                 div=(int(div) if div else None))
        except Exception as exc:                                # noqa: BLE001
            return self.ack(False, f"网格算不动：{exc}", kind="grid", grid=None)
        self.stats["grids"] = self.stats.get("grids", 0) + 1
        if not info.get("ok"):
            return self.ack(False, "网格没通过：{}".format(info.get("reason") or "?"),
                            kind="grid", grid=info, n=len(ts))
        return self.ack(True, kind="grid", grid=info, n=len(ts),
                        bpm=info.get("bpm"), offsetMs=info.get("phase_ms"),
                        div=info.get("div"), step=info.get("step_ms"),
                        period=info.get("period_ms"),
                        notes=(info.get("notes") or [])[:4])

    # ---- 其余入站
    def _on_selection(self, msg) -> str:
        self.selection = {"kind": msg.get("kind"),
                          "marker_ids": list(msg.get("markerIds") or []),
                          "id": msg.get("id")}
        self._push("selection")
        return self.ack(True, n=len(self.selection["marker_ids"]))

    def _on_playhead(self, msg) -> str:
        self.playhead = msg.get("beat")
        return self.ack(True)

    def _on_audio(self, msg) -> str:
        self.audio = {"path": msg.get("path") or "",
                      "name": msg.get("name") or "",
                      "md5": msg.get("md5") or ""}
        self._push("audio")
        if not self.audio["path"]:
            return self.ack(False, "audio 消息没有 path")
        return self.ack(True, path=self.audio["path"])

    def _on_pull(self, msg) -> str:
        # 对端问我们要东西（一般是我们的导入指令）——目前先确认收到
        return self.ack(True, want=IN_PROJECT)

    def _on_pong(self, msg) -> str:
        return self.ack(True)

    def _on_ack(self, msg) -> str:
        if not msg.get("ok"):
            self.warn(f"对端回执失败：{msg.get('error')}")
        # ★ 投射回执：带偏移量就要**存下来并报出来**（docs/38 §3.2）
        if msg.get("run") and ("n_off_grid" in msg or "drift_max_ms" in msg):
            # ★★ 2026-10 修：这里以前是**硬编码 7 个键的白名单** —— 插件多报的
            #   `anchor` / `via_ms` / `n_tracks` / `n_lanes` / `n_cross_track` /
            #   `n_lane_collision` 全被**静默丢掉**，于是 app 那边
            #   「★ 已采用我们的时序锚」「已投送 N 条泳道」从来不会出现，
            #   反而永远误报「⚠ 这一批**没有带时序锚**」（实测：锚其实生效了，
            #   宿主 baseBpm 确实被改成我们给的值）。
            #   回执是我们自己插件发的小定长报告，**原样收下**，别再筛。
            self.import_ack = dict(msg)
            if msg.get("n_off_grid"):
                self.warn("投射有 {} 个点被 BDG 挪动，最大 {:.3f} ms{}".format(
                    msg.get("n_off_grid"), float(msg.get("drift_max_ms") or 0.0),
                    "（超 Δ 预算，请关掉「节拍网格」吸附再投）"
                    if msg.get("drift_over") else ""))
            if msg.get("n_lane_collision"):
                self.warn("★ 有 {} 条泳道塌到同一条轨（分轨失败）—— "
                          "这批点可能互相清掉了，别信这次对账".format(
                              msg.get("n_lane_collision")))
                self._push("import_drift")
        return self.ack(True)

    # ------------------------------------------------------------ 状态
    def _push(self, what: str) -> None:
        if self.app is not None and getattr(self.app, "broker", None) is not None:
            try:
                self.app.broker.push("bridge", what=what, state=self.state())
            except Exception:                                   # noqa: BLE001
                pass

    def summary(self) -> dict:
        """最近一次快照的**人可读**结构（给面板 / 调试 / 调试 / 分段采音用）。"""
        p = self.last_project
        if p is None:
            return {"tracks": [], "points": 0, "by_role": {}}
        rows = []
        for t in p.tracks:
            pts = sorted([x for x in p.points if x.track_id == t.id],
                         key=lambda y: y.beat)
            rows.append({
                "name": t.name, "type": t.type, "role": t.role, "hidden": t.hidden,
                "n": len(pts), "loop_parents": sum(1 for x in pts if x.loop is not None),
                "beats": [round(x.beat, 4) for x in pts][:16],
            })
        by_role: dict = {}
        for r in rows:
            by_role.setdefault(r["role"] or "?", []).append(
                {"name": r["name"], "n": r["n"], "beats": r["beats"]})
        return {"name": p.name, "base_bpm": p.base_bpm, "offset_ms": p.offset_ms,
                "parser": (self.last_rep.parser if self.last_rep else ""),
                "tracks": rows, "points": len(p.points), "by_role": by_role}

    # ------------------------------------------------------------ 投射 / 收回
    def role_points(self) -> list:
        """★ **角色轨上的全部点** → `[{at_ms, role, track, beat}]`（`docs/38` §10）。

        这是「分段采音」的自动填充入口：用户在 BDG 里往**角色轨**上摆点，
        每个点就是一句「从这一刻起，这条轨是什么角色」。语义与
        `core.segments.from_roles` 的 `from`（从这点起）口径一致。

        `track` 用 **BDG 侧轨下标** —— `core.bdg.source.to_midi_like` 是按
        `enumerate(project.tracks)` 生成音轨的，所以两边下标一一对应。

        ★ 只认 `parse.role_track_role()`（= `<pluginId>:<main|sub|dp|off>`）：
          **内置踩点轨**与**宿主 bpm/twirl 控制轨**都不算 —— 前者会让每条
          普通轨凭空生成分段，后者上面是变速点、不是角色指令。
        """
        from core.bdg.parse import role_track_role
        p, tm = self.last_project, self.tempo()
        if p is None or tm is None:
            return []
        out = []
        for i, t in enumerate(p.tracks):
            if t.hidden:
                continue
            role = role_track_role(t.type)
            if role not in seg_mod.ROLES:
                continue
            for x in p.points:
                if x.track_id != t.id:
                    continue
                out.append({"at_ms": round(tm.time_of_beat(float(x.beat)), 6),
                            "role": role, "track": i, "beat": round(float(x.beat), 6)})
        out.sort(key=lambda e: (e["at_ms"], e["track"]))
        return out

    def segments_payload(self, label_prefix: str = "分段") -> dict:
        """给 UI 的「从角色轨生成分段」结果（`docs/38` §10）。"""
        pts = self.role_points()
        if not pts:
            return {"ok": False,
                    "error": "对端还没有任何**角色轨**上的点（在 BDG 里用我们的"
                             "「主轨/次轨/双押轨/关轨」轨类型摆几个点）"}
        segs = seg_mod.from_roles(pts, label_prefix=label_prefix)
        rows = [{
            seg_mod.K_INDEX: s.index, seg_mod.K_T0: s.at_ms, seg_mod.K_T1: 0.0,
            seg_mod.K_SRC: seg_mod.SRC_SEG, seg_mod.K_LABEL: s.label,
            seg_mod.K_MAIN: s.main or [], seg_mod.K_SUB: s.sub or [],
            seg_mod.K_DP: s.dp or [], seg_mod.K_N: 0, seg_mod.K_N_CROSS: 0,
        } for s in segs]
        return {"ok": True, "mode": seg_mod.MODE_FROM,
                "segments": seg_mod.to_json(segs), "n": len(segs),
                "n_points": len(pts),
                "report": seg_mod.report_text(rows, seg_mod.MODE_FROM)}

    def tempo(self):
        """用**他**的锚做换算器（`docs/35` §3.4：beat⇄ms 以 BDG 为准，只读）。"""
        p = self.last_project
        return p.tempo if p is not None else None

    def _can_send(self):
        return self.conn is not None and not self.conn.closed

    def push_import(self, onsets, src: str = "session", clear: bool = True,
                    names=None, base_bpm: float = 0.0,
                    offset_ms: float = 0.0) -> dict:
        """★「投射到编辑器」：我们的 onsets（毫秒）→ 发**毫秒**+**时序锚**过去。

        `docs/41` §4：载荷里既带 `beat`（旧版插件回退用）也带 `ms`，并带上
        `baseBpm`/`offsetMs`。**换算拍位的活交给插件** —— 它会把锚先设好，
        再用宿主**当前**的模型算，省掉「谁先设锚」的顺序坑。
        """
        tm = self.tempo()
        if tm is None:
            return {"ok": False, "error": "还没收到对端快照，拿不到 beat⇄ms 的锚"}
        if not self._can_send():
            return {"ok": False, "error": "桥没连上（先在 BDG 面板里点连接）"}

        sent = []
        for i, o in enumerate(onsets):
            lane = ""
            if isinstance(o, dict):
                ms = o.get("ms")
                if ms is None:
                    ms = o.get("t_ms")
                role = str(o.get("role") or bdg.aliases.ROLE_MAIN)
                idx = o.get("idx")
                idx = int(idx) if idx is not None else i
                tr = tuple(o.get("src_tracks") or ())
                # ★★ 显式泳道（三押 `dp:3`）。以前这里丢掉了 ⇒ 三押点全挤进双押轨。
                lane = str(o.get(bdg.aliases.K_LANE) or "")
            else:
                ms = getattr(o, "t_ms", None)
                if ms is None:
                    ms = float(o)
                role = str(getattr(o, "role", "") or bdg.aliases.ROLE_MAIN)
                idx = int(getattr(o, "idx", i))
                tr = tuple(getattr(o, "src_tracks", ()) or ())
                lane = str(getattr(o, bdg.aliases.K_LANE, "") or "")
            ms = float(ms)
            # ★ `docs/41`：带上毫秒（插件在锚设好之后自己换算拍位）
            # ★ `docs/42`：带上**源轨**，由 `build_import` 按 `<role>:<源轨>` 分泳道
            sent.append(bdg.roundtrip.Sent(idx, tm.beat_of_time(ms), role, src, ms,
                                           tuple(int(x) for x in tr), lane))

        run = _new_run_id()
        payload = bdg.roundtrip.build_import(sent, run, names=names, clear=clear,
                                            base_bpm=base_bpm, offset_ms=offset_ms)
        self.sent, self.run, self.sent_src = sent, run, src
        self.adopted = None
        self.conn.send_text(self.dumps(self.envelope(OUT_IMPORT, payload)))
        self.stats["imports"] += 1
        self._push("import")
        return {"ok": True, "run": run, "n": len(sent), "note": payload,
                "base_bpm": float(base_bpm or 0.0), "offset_ms": float(offset_ms or 0.0),
                "beats": [round(s.beat, 4) for s in sent][:16]}

    def diff(self) -> dict | None:
        """把「我们投送的」和「收回来的」对一次账（`docs/38` §4）。"""
        if not self.sent or self.last_project is None:
            return None
        return bdg.roundtrip.compare(self.sent, self.last_project, self.run, self.tempo())

    def adopt(self) -> dict:
        """★「收回编辑器改动」：对账 → 把**我们单位（毫秒）**的新进度交回去。"""
        d = self.diff()
        if d is None:
            return {"ok": False, "error": "没有可对账的投送（先点投射）"}
        tm = self.tempo()
        for o in d["onsets"]:
            o["ms"] = round(tm.time_of_beat(o["beat"]), 6) if tm else 0.0
        self.adopted = d
        self.stats["adopts"] += 1
        self._push("adopt")
        return {"ok": True, "run": self.run, "diff": d,
                "onsets": d["onsets"], "report": bdg.roundtrip.report_text(d)}

    def state(self) -> dict:
        p = self.last_project
        # ★★ 2026-10：对账**只在「点过收回」之后才报**（用 `self.adopted`，不是现算）。
        #   为什么：`diff()` 是「我们投送的 vs 现在工程里的」。投射刚做完时，插件
        #   还在逐轨写、宿主在不停地推快照 ⇒ `last_project` 是**写到一半的那一份**，
        #   于是面板会显示「留下 1 · 移动 327 · 删除 128」这种**自相矛盾**的对账
        #   （明明刚投完、一个点都没改）。`push_import()` 会把 `self.adopted` 清成
        #   None ⇒ 投完不再显示；点一次「收回改动」才有。
        #   （这个问题是**事件订阅修好之后**才显出来的：以前投射期间根本收不到快照。）
        d = self.adopted
        return {
            "connected": self.conn is not None,
            "authed": self.authed,
            "peer": dict(self.peer),
            "projects": self.stats["projects"],
            "n_tracks": len(p.tracks) if p else 0,
            "n_points": len(p.points) if p else 0,
            "n_bpm_events": len(p.bpm_events) if p else 0,
            "parser": (self.last_rep.parser if self.last_rep else ""),
            "last_warn": self.warns[-1] if self.warns else "",
            "run": self.run,
            "sent_n": len(self.sent),
            "import_ack": dict(self.import_ack) if self.import_ack else None,
            # ★ 收回的轨道项目（`docs/45`）：面板/状态栏要看得到
            "back": ({"run": (self.back or {}).get(bdg.aliases.K_RUN, ""),
                      "n_lanes": len(self.back_lanes),
                      "n_points": sum(x.get(bdg.aliases.K_N_POINTS, 0)
                                      for x in self.back_lanes),
                      "lanes": [{"name": x.get(bdg.aliases.K_NAME, ""),
                                 "role": x.get(bdg.aliases.K_ROLE, ""),
                                 "src_track": x.get(bdg.aliases.K_SRC_TRACK),
                                 "n": x.get(bdg.aliases.K_N_POINTS, 0),
                                 "ms_lo": x.get(bdg.aliases.K_MS_LO, 0.0),
                                 "ms_hi": x.get(bdg.aliases.K_MS_HI, 0.0)}
                                for x in self.back_lanes]}
                     if self.back else None),
            "diff": ({"counts": d["counts"], "n_added": d["n_added"],
                      "n_stale": d["n_stale"], "drift_max_ms": d["drift_max_ms"],
                      "drift_over": d["drift_over"], "snap_suspect": d["snap_suspect"],
                      "snap_div": d["snap_div"]} if d else None),
            "stats": dict(self.stats),
        }

    def url(self, host: str = "127.0.0.1", port: int = 0) -> str:
        return f"ws://{host}:{port}/ws?token={self.token}"

    # ------------------------------------------------------------ 连接循环
    def serve(self, conn) -> None:
        """握手已完成（且 token 已验过）⇒ 收消息、回消息，直到对端走。"""
        self.conn = conn
        self.authed = False
        self.should_close = False
        try:
            while not self.should_close:
                text = conn.recv_text()
                if text is None:
                    break
                for reply in self.handle_text(text):
                    conn.send_text(reply)
                    self.stats["msgs_out"] += 1
        except WS.WsError as exc:
            self.warn(str(exc))
        except OSError:
            pass
        finally:
            self.conn = None
            self.authed = False
            self._push("closed")
