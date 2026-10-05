/**
 * ADO 谱面桥 —— BDG 侧插件（**renderer.js**，只读，不改宿主）。
 *
 * 装法：把整个 `bridge_plugin/` 目录拷进
 *   `<userData>/plugins/`（宿主的 设置 → 打开插件目录）
 *   或开发模式下的 `<项目根>/plugins/`，然后重启 / 重载编辑器。
 *
 * 它干什么：
 *   · 连我们 sidecar 的 `ws://127.0.0.1:PORT/ws?token=…`（`hello` 握手）；
 *   · 工程一变（**去抖 300ms**）就推一次全量 `api.project.snapshot()`；
 *   · 顺带推 `selection` / `playhead` / `audio`（`api.system.audioPath()`）；
 *   · 面板里显示 ●已连接 / 已同步 N 轨 / 最后一条错误。
 *
 * 它**不**干什么（v0.4 口径，见 docs/37 §5）：
 *   · **不写回**、不改用户工程、不进撤销栈 —— 桥只做「读」。
 *     （宿主 `api.project.edit.moveMarker` 没暴露 `force`，写回必被吸附，见 docs/35 §6.2。）
 *   · 不碰宿主源码；只是一个插件目录。
 *
 * 为什么能直连：宿主 `src/renderer/index.html` 的 CSP 是
 *   `connect-src 'self' ws:` ⇒ `ws:` 是放行的。
 */
/// <reference path="plugin-api.d.ts" />
(function () {
  "use strict";

  var LS_KEY = "adoc.bridge.url";
  var DEBOUNCE_MS = 300; // 宿主 project 事件是微任务级的，拖拽时会狂发 ⇒ 必须去抖
  var PLAYHEAD_MS = 120;
  var SELECTION_MS = 80;
  var RECONNECT_MS = 3000;
  var MAX_LOG = 8;

  /* ------------------------------------------------------------ 会话 */
  function Session(api, onState) {
    this.api = api;
    this.ws = null;
    this.url = "";
    this.connected = false;
    this.accepted = false;
    this.seq = 0;
    this.sent = 0;
    this.pushed = 0;
    this.lastError = "";
    this.logs = [];
    this._timer = null;
    this._playTimer = null;
    this._selTimer = null;
    this._reconnect = null;
    this._closeByUs = false;
    this.onState = onState || function () {};
  }

  Session.prototype.setUrl = function (url) {
    this.url = String(url || "").trim();
    try {
      window.localStorage.setItem(LS_KEY, this.url);
    } catch (e) {
      /* localStorage 可能被禁，无所谓 */
    }
  };

  Session.prototype.say = function (msg) {
    this.lastError = msg;
    this.logs.unshift(new Date().toLocaleTimeString() + "  " + msg);
    if (this.logs.length > MAX_LOG) this.logs.length = MAX_LOG;
    this.api.log("[bridge]", msg);
    this.onState();
  };

  Session.prototype.connect = function () {
    var self = this;
    if (!this.url) {
      this.say("先填 ws:// 地址（我们界面里的连接串）");
      return;
    }
    if (this.ws) {
      try {
        this.ws.close();
      } catch (e) {
        /* ignore */
      }
    }
    this._closeByUs = false;
    var ws;
    try {
      ws = new WebSocket(this.url);
    } catch (e) {
      this.say("WebSocket 建不起来：" + e.message);
      this._scheduleReconnect();
      return;
    }
    this.ws = ws;

    ws.onopen = function () {
      self.connected = true;
      self.accepted = false;
      self.send({ type: "hello", v: 1, plugin: "0.1.0", bdg: "unknown", caps: caps() });
      self.say("已连接，等 accepted…");
      self.pushProject();
      self.pushAudio();
    };

    ws.onmessage = function (ev) {
      var msg = null;
      try {
        msg = JSON.parse(ev.data);
      } catch (e) {
        self.say("收到非 JSON：" + String(ev.data).slice(0, 80));
        return;
      }
      self.handle(msg);
    };

    ws.onerror = function () {
      self.say("连接出错（对端没起？端口/token 对不上？）");
    };

    ws.onclose = function () {
      self.connected = false;
      self.accepted = false;
      if (!self._closeByUs) {
        self.say("断开，3s 后重连");
        self._scheduleReconnect();
      } else {
        self.say("已断开");
      }
    };
  };

  Session.prototype._scheduleReconnect = function () {
    var self = this;
    if (this._reconnect) return;
    this._reconnect = setTimeout(function () {
      self._reconnect = null;
      self.connect();
    }, RECONNECT_MS);
  };

  Session.prototype.disconnect = function () {
    this._closeByUs = true;
    if (this._reconnect) {
      clearTimeout(this._reconnect);
      this._reconnect = null;
    }
    if (this.ws) {
      try {
        this.ws.close();
      } catch (e) {
        /* ignore */
      }
    }
    this.ws = null;
    this.connected = false;
    this.say("手动断开");
  };

  Session.prototype.send = function (obj) {
    if (!this.ws || this.ws.readyState !== 1) return false;
    this.seq += 1;
    obj.v = 1;
    obj.seq = this.seq;
    obj.ts = Date.now();
    try {
      this.ws.send(JSON.stringify(obj));
      this.sent += 1;
      return true;
    } catch (e) {
      this.say("发送失败：" + e.message);
      return false;
    }
  };

  /** 回执（每次写操作都回，不许静默 —— `docs/38` §3.2）
   *  注意：这里的键名 `adbIdx/adbRun/adbRole` 必须和
   *  `core/bdg/aliases.py` 里的 `SYNC_IDX/SYNC_RUN/SYNC_ROLE` 对齐。 */
  Session.prototype.ack = function (ok, error, run, extra) {
    var m = { type: "ack", ok: !!ok, run: run || "" };
    if (error) m.error = error;
    if (extra) for (var k in extra) if (extra[k] !== undefined) m[k] = extra[k];
    this.send(m);
    return false;
  };

  Session.prototype.handle = function (msg) {
    var t = msg && msg.type;
    if (t === "import") {
      this.applyImport(msg);
      return;
    }
    if (t === "accepted") {
      this.accepted = true;
      this.say("握手成功（对端接受 v" + (msg.accepted || []).join("/") + "）");
      return;
    }
    if (t === "failed") {
      this.accepted = false;
      this.say("对端拒绝：" + (msg.error || "未知"));
      return;
    }
    if (t === "ack") {
      // ★ 网格探测的回执（`docs/45` §7）：先喂给等着的那个 Promise
      if (msg.kind === "grid" && this._gridWait) {
        var res = this._gridWait;
        this._gridWait = null;
        if (this._gridTimer) { clearTimeout(this._gridTimer); this._gridTimer = null; }
        if (!msg.ok) this.say("我们那边说网格不成立：" + (msg.error || ""));
        else this.say("网格探测回来了：bpm " + (msg.bpm || 0).toFixed(3)
          + " · 相位 " + (msg.offsetMs || 0).toFixed(3) + "ms · 1/" + msg.div);
        res(msg.ok ? msg.grid : null);
        return;
      }
      if (msg.ok) {
        this.pushed += 1;
        // ★ 收回的回执（`docs/45`）：报告收了几轨/几点
        if (msg.n_tracks !== undefined) {
          this.say(
            "已返回 " + msg.n_tracks + " 轨 / " + (msg.n_points || 0) + " 点" +
              "（我们那边落成 " + (msg.n_onsets || 0) + " 个 onset" +
              (msg.n_added ? " · 新加 " + msg.n_added : "") +
              (msg.n_dup ? " · 同刻合并 " + msg.n_dup : "") + "）",
          );
          this.lastBack = msg;
          return;
        }
        if (msg.n_points !== undefined) {
          this.say(
            "已同步 " +
              msg.n_tracks +
              " 轨 / " +
              msg.n_points +
              " 点" +
              (msg.n_bpm_events ? " / 变速 " + msg.n_bpm_events : "") +
              (msg.dup_beats ? "（重复拍 " + msg.dup_beats + "）" : ""),
          );
        }
      } else {
        this.say("对端说不行：" + (msg.error || "未说明"));
      }
      return;
    }
    if (t === "pull") {
      this.pushProject();
      return;
    }
    if (t === "pong") return;
    this.say("不认识的回执 type=" + t);
  };

  /* ------------------------------------------------------------ 投射：import */
  // ★ 收到 import 就把它变成编辑器的轨与点（docs/38 §3.1）
  //
  // ★★ 改了三件事（用户拿真谱试出来的，见 docs/41）：
  //   1. **用内置踩点轨**（`addTrack`）—— 不是插件轨（`addTypedTrack`）。
  //      插件轨类型没有「继承内置踩点轨」的字段（plugin-api.d.ts 的
  //      TrackTypeSchema 只有 id/trackName/pointName/color/fields），
  //      所以那种轨**永远不是踩点轨**，用户的编辑器工具用不上。
  //      角色改由「哪条轨 + attrs.adbRole」认。
  //   2. **把我们谱的时序锚交过去**（`setBaseBpm` + `setOffset`）。
  //      以前只推拍位，宿主就留在它自己那套 bpm/offset 上，用户只能猜。
  //      ★ 锚先设、**再**算拍位 —— 不然算出来的拍位是旧模型的。
  //   3. **载荷里发毫秒**（`ms`），由这里用**宿主当前模型**换算成 beat。
  //      谁先谁后的坑就没了（我们的谱一发，锚就是我们的）。
  Session.prototype.applyImport = function (msg) {
    var self = this;
    var api = this.api;
    var run = msg.run || "";
    var wanted = msg.tracks || [];
    var report = { run: run, n_placed: 0, n_cleared: 0, n_off_grid: 0,
                   drift_max_ms: 0, drift_over: 0, n_skipped: 0,
                   n_tracks: 0, anchor: null, via_ms: 0,
                   // ★ 泳道数 / 跨轨簇数是**对端算好发过来的**（roundtrip.build_import），
                   //   以前没往回收，app 那行「已投送 N 条泳道」就永远不显示。
                   n_lanes: Number(msg.n_lanes || 0),
                   n_cross_track: Number(msg.n_cross_track || 0) };
    var budgetMs = 25;                    // 与 Δ 预算同口径

    // 先记下每轨「我们投送的 beat」，等加完再回读比对（吸附会挪点）
    var want = [];                        // {id, beat, ms, trackId}
    var usedTrack = {};                   // trackId → lane（查「两条泳道塌成一条」）

    var edit = api.project.edit;

    // ---- ① 先把时序锚设成我们的（documented：锚必须早于拍位换算）----
    var anchor = null;
    try {
      if (typeof msg.baseBpm === "number" && isFinite(msg.baseBpm) && msg.baseBpm > 0) {
        edit.setBaseBpm(msg.baseBpm);
        anchor = { baseBpm: msg.baseBpm };
      }
      if (typeof msg.offsetMs === "number" && isFinite(msg.offsetMs)) {
        edit.setOffset(msg.offsetMs);
        anchor = anchor || {};
        anchor.offsetMs = msg.offsetMs;
      }
      report.anchor = anchor;
      if (anchor) {
        this.say("已采用对端的时序锚：bpm=" + msg.baseBpm + " offset=" + msg.offsetMs + "ms");
      }
    } catch (e) {
      this.say("设时序锚失败（继续，点可能对不上）：" + e.message);
    }

    // ---- ② 按泳道找/建**内置**轨（名字还是 ADO·主轨 trk0 这种）----
    //   ★★ 真机踩过的坑：宿主把内置轨的 `type` 报成 **`"beat"`**（不是空串！）。
    //     第一版写成 `if (!t.type && ...)` ⇒ 永远匹配不上 ⇒ **每次投射都新建一条轨**，
    //     `clear` 也就永远清不掉上一批（实测：推两次变成两条 ADO·主轨 + 24 个点）。
    //     所以这里两种都认：空串 / `"beat"`。
    //
    //   ★★ 第二个坑（`docs/42`）：泳道是 `<role>:<源轨>`，**不能按 role 兜底找轨** ——
    //     那样 4 条主轨泳道会全塌进同一条 `trk0`，然后每条的 `clear` 把上一条刚放的
    //     清掉（实测 631 点只落 83、548 被清又被拒）。兜底必须按 **(role, 源轨)**。
    var isBuiltin = function (t) { return !t.type || t.type === "beat"; };
    var laneParse = function (lane, role) {
      // 载荷里的 `lane` 形如 `main:2` / `dp:`（dp 没有源轨维）
      var s = String(lane || "");
      var i = s.indexOf(":");
      if (i < 0) return { role: role, track: null };
      var tr = s.slice(i + 1);
      return { role: s.slice(0, i), track: tr === "" ? null : parseInt(tr, 10) };
    };
    var findTrack = function (name, role, srcTrack) {
      var snap = api.project.snapshot();
      var byName = null, byMark = null;
      for (var i = 0; i < snap.tracks.length; i++) {
        var t = snap.tracks[i];
        if (!isBuiltin(t)) continue;
        if (!byName && t.name === name) byName = t;
        if (!byMark) {
          // 兜底：用户把轨改名了，但轨上还有**同一泳道**的标记 ⇒ 还是那条轨
          for (var k = 0; k < snap.markers.length; k++) {
            var m = snap.markers[k];
            var a = m.attrs || {};
            if (m.trackId !== t.id || a.adbRole !== role) continue;
            var at = a.adbTrack;
            var same = (srcTrack === null || srcTrack === undefined)
              ? (at === undefined || at === null)
              : (at === srcTrack);
            if (same) { byMark = t; break; }
          }
        }
      }
      return byName || byMark;
    };

    try {
      edit.batch(function () {
        for (var i = 0; i < wanted.length; i++) {          var spec = wanted[i];
          var role = spec.role;
          var name = spec.name || role;
          var lp = laneParse(spec.lane, role);
          var track = findTrack(name, role, lp.track);
          if (track && usedTrack[track.id] && usedTrack[track.id] !== spec.lane) {
            // ★★ 两条泳道指向同一条轨 ⇒ 一定是找轨逻辑崩了。**不许静默**：
            //   以前这里会塌成一条轨，然后 clear 互相清（实测 631 点只落 83）。
            report.n_lane_collision = (report.n_lane_collision || 0) + 1;
            this.say("⚠ 泳道 " + spec.lane + " 与 " + usedTrack[track.id]
                     + " 指向同一条轨 —— 分轨失败，别再往下推");
            track = null;
          }
          if (!track) {
            var tid = edit.addTrack({ name: name });
            if (!tid) {
              report.n_skipped += (spec.onsets || []).length;
              continue;
            }
            track = { id: tid, name: name };
            report.n_tracks++;
          }
          usedTrack[track.id] = spec.lane || name;
          if (spec.clear) {
            // ★ 只清「我们上一批」的点（带 adbIdx 的），用户手加的点一个不碰
            var old = api.project.snapshot().markers;
            for (var k = 0; k < old.length; k++) {
              var m = old[k];
              if (m.trackId === track.id && m.attrs && m.attrs.adbIdx !== undefined) {
                edit.removeMarker(m.id);
                report.n_cleared++;
              }
            }
          }
          var onsets = spec.onsets || [];
          for (var q = 0; q < onsets.length; q++) {
            var o = onsets[q];
            // ★ 有 ms 就用**宿主当前模型**换算（锚已经设好了）；没有就退回旧载荷的 beat
            var beat = o.beat;
            if (typeof o.ms === "number" && isFinite(o.ms)) {
              beat = api.project.beatOfTime(o.ms);
              report.via_ms++;
            }
            var id2 = edit.addMarker({ trackId: track.id, beat: beat });
            if (!id2) {
              report.n_skipped++;
              continue;
            }
            edit.setMarkerAttrs(id2, o.attrs || {});
            want.push({ id: id2, beat: beat, ms: o.ms, trackId: track.id });
          }
        }
      });
    } catch (e) {
      this.say("投射失败：" + e.message);
      return this.ack(false, "投射失败：" + e.message, run, report);
    }

    // 回读对账：addMarker 走 snapped()，吸附开着就会挪点 ⇒ 如实报出来
    var tm = api.project.snapshot();
    var byId = {};
    for (var a = 0; a < tm.markers.length; a++) byId[tm.markers[a].id] = tm.markers[a];
    for (var b = 0; b < want.length; b++) {
      var got = byId[want[b].id];
      if (!got) {
        report.n_skipped++;
        continue;
      }
      report.n_placed++;
      // ★ 判「动了没有」用宿主自己的精度（它内部就是 round 到 1e-6）。
      //   真机教训：`> 1e-9` 会把纯浮点误差也算成「被挪」，实测 12 个整拍点
      //   报了 5 个「被挪」而 `drift_max_ms = 0` —— 自相矛盾、吓人。
      var d = Math.abs(got.beat - want[b].beat);
      if (d > 1e-6) {
        report.n_off_grid++;
        // ★ 用**时间**量漂移（用户在意的是「对不上音」，不是拍位差）
        var ms;
        if (typeof want[b].ms === "number" && isFinite(want[b].ms)) {
          ms = Math.abs(api.project.timeOfBeat(got.beat) - want[b].ms);
        } else {
          ms = Math.abs(api.project.timeOfBeat(got.beat) - api.project.timeOfBeat(want[b].beat));
        }
        if (ms > report.drift_max_ms) report.drift_max_ms = ms;
        if (ms > budgetMs) report.drift_over++;
      }
    }
    report.drift_max_ms = Math.round(report.drift_max_ms * 1000) / 1000;
    this.lastImport = report;
    this.say(
      "投射完成：" + report.n_placed + " 点" +
        (report.n_cleared ? "（清掉上批 " + report.n_cleared + "）" : "") +
        (report.n_off_grid
          ? " · ★ " + report.n_off_grid + " 点被挪，最大 " + report.drift_max_ms + "ms" +
            (report.drift_over ? "（超预算 " + report.drift_over + "）" : "") +
            " —— 请在顶栏关掉「吸附」重投"
          : " · 逐点无损 ✓"),
    );
    // ★★ 2026-10 修：以前这里**只回传 7 个字段**，把 `anchor` / `via_ms` /
    //   `n_tracks` / `n_lanes` / `n_cross_track` / `n_lane_collision` 全丢了。
    //   后果：app 面板永远误报「⚠ 这一批没有带时序锚」（实测锚其实生效了 ——
    //   宿主 baseBpm 确实被我们改掉了），泳道那行也永远不显示。
    //   回执是定长小报告 ⇒ **整份回传**，让对端自己决定怎么显示。
    return this.ack(true, "", null, report);
  };

  /* ------------------------------------------------------------ 收回：轨道项目 */
  // ★★ 「返回数据到谱面生成器」（`docs/45`，用户 2026-10 口径）：
  //   「我们的工具使用的音轨**就是** BDG 里面带时值数据的音轨」。
  //
  //   所以这里按 **BDG 的轨** 分组（一条轨 = 我们那边的一条音轨项目），
  //   只要那条轨上有「我们投送过的标记」（attrs.adbIdx / adbRole）。
  //   用户自己新建的轨**不搬** —— 那是他的素材，不是我们的谱。
  //   轨上**用户新加的点**（没有标记）照搬，并单独计数（n_added）。
  var ROLE_KEY = "adbRole";     // 与 core/bdg/aliases.py 的 SYNC_ROLE 对齐
  var IDX_KEY = "adbIdx";       // SYNC_IDX（身份）
  var RUN_KEY = "adbRun";       // SYNC_RUN（批次）
  var TRACK_KEY = "adbTrack";   // SYNC_TRACK（单数：源轨号）
  var TRACKS_KEY = "adbTracks"; // SYNC_TRACKS（跨轨簇：全部源轨号）

  Session.prototype.collectBack = function () {
    var snap = this.api.project.snapshot();
    var names = {};
    var i, m, a, k;
    for (i = 0; i < snap.tracks.length; i++) names[snap.tracks[i].id] = snap.tracks[i].name;

    var ours = {};                            // 哪些轨上有我们的标记
    var runs = {};                            // 我们的批次号 → 出现次数
    for (i = 0; i < snap.markers.length; i++) {
      m = snap.markers[i];
      a = m.attrs || {};
      if (a[IDX_KEY] === undefined && a[ROLE_KEY] === undefined) continue;
      ours[m.trackId] = true;
      if (a[RUN_KEY]) runs[a[RUN_KEY]] = (runs[a[RUN_KEY]] || 0) + 1;
    }
    var run = "", best = -1;
    for (k in runs) if (runs[k] > best) { best = runs[k]; run = k; }

    var byTrack = {}, order = [];
    var nPts = 0, nAdded = 0, nSkip = 0, nNoTrack = 0;
    for (i = 0; i < snap.markers.length; i++) {
      m = snap.markers[i];
      if (!ours[m.trackId]) continue;
      a = m.attrs || {};
      var ms = m.timeMs;
      if (typeof ms !== "number" || !isFinite(ms)) {
        try { ms = this.api.project.timeOfBeat(m.beat); } catch (e) { ms = null; }
      }
      if (typeof ms !== "number" || !isFinite(ms)) { nSkip++; continue; }
      var idx = (a[IDX_KEY] !== undefined && a[IDX_KEY] !== null) ? a[IDX_KEY] : null;
      if (idx === null) nAdded++;
      var tr = a[TRACKS_KEY] || (a[TRACK_KEY] !== undefined ? [a[TRACK_KEY]] : []);
      var lane = byTrack[m.trackId];
      if (!lane) {
        lane = { name: names[m.trackId] || "", role: a[ROLE_KEY] || "main",
                 src_track: tr.length ? tr[0] : null, lane: "", points: [],
                 _roles: {}, _tracks: {} };
        byTrack[m.trackId] = lane;
        order.push(m.trackId);
      }
      // 用户可能把点从别的角色轨拖过来 ⇒ 记一下多用了几种角色（不许静默）
      lane._roles[a[ROLE_KEY] || "main"] = (lane._roles[a[ROLE_KEY] || "main"] || 0) + 1;
      lane.points.push({ idx: idx, beat: m.beat, ms: ms, src_tracks: tr });
      nPts++;
    }
    var tracks = [];
    for (i = 0; i < order.length; i++) {
      var lt = byTrack[order[i]];
      // 角色取**多数**（拖过来的少数派点会跟着走，但会报出来）
      var rk = null, rv = -1;
      for (k in lt._roles) if (lt._roles[k] > rv) { rv = lt._roles[k]; rk = k; }
      var mixed = Object.keys(lt._roles).length > 1;
      var st = (lt.role === "dp")
        ? null
        : ((rk === "dp") ? null : (lt.src_track === null ? null : lt.src_track));
      lt.points.sort(function (x, y) { return x.ms - y.ms; });
      tracks.push({ name: lt.name, role: lt.role, src_track: st,
                    lane: lt.role + ":" + ((st === null || st === undefined) ? "" : st),
                    points: lt.points, n: lt.points.length, mixed_role: mixed });
    }
    // 按角色排序：主/次/双押/关（和投送时一致，方便对数）
    var ORD = { main: 0, sub: 1, dp: 2, off: 3 };
    tracks.sort(function (x, y) {
      var a1 = ORD[x.role] === undefined ? 9 : ORD[x.role];
      var b1 = ORD[y.role] === undefined ? 9 : ORD[y.role];
      return a1 !== b1 ? a1 - b1 : String(x.name) < String(y.name) ? -1 : 1;
    });
    if (!tracks.length) {
      this.say("这条工程里没有我们投送过的轨道（先在我们那边点「投射」，或先用桥连上）");
      return null;
    }
    var mixedN = 0;
    for (i = 0; i < tracks.length; i++) if (tracks[i].mixed_role) mixedN++;
    if (mixedN) this.say("⚠ " + mixedN + " 条轨上的点来自不止一个角色（按多数派归类）");
    return { type: "tracks", run: run,
             anchor: { baseBpm: snap.baseBpm, offsetMs: snap.offsetMs },
             tracks: tracks, n_points: nPts, n_added: nAdded, n_skipped: nSkip };
  };

  /** 「返回数据到谱面生成器」：把当前工程的轨道整条发回我们那边。 */
  Session.prototype.sendBack = function () {
    if (!this.connected) {
      this.say("没连上（先点「连接」，我们那边要有 sidecar 在跑）");
      return false;
    }
    var p = this.collectBack();
    if (!p) return false;
    var ok = this.send(p);
    this.say(ok ? ("正在返回 " + p.tracks.length + " 轨 / " + p.n_points + " 点…")
                : "返回失败（socket 断了）");
    return ok;
  };

  /* ------------------------------------------------------------ 导入时间戳 */
  // ★★ 「毫秒时间戳 → BDG」（`docs/45` §7，用户那条箭头的前半段）。
  //
  //   为什么格子要问我们（Python 侧）：吸附的数学在 `core.denoise`，JS 各写一份
  //   必然分叉 ⇒ 插件**只负责读文件与画点**，`{type:"grid"}` 问一次拿
  //   `bpm / offsetMs / div`，然后：
  //     ① 先把宿主锚设成我们的（锚必须早于拍位）；
  //     ② 每个时间戳 → `beatOfTime(ms)` → addMarker；
  //     ③ 打上 `adb*` 标记 ⇒ 之后按「返回数据到谱面生成器」能整条收回；
  //     ④ 回读比对：被吸附挪了就如实报（并告诉用户把分母设成 1/div）。
  var TS_EXT = ["txt", "csv", "tsv", "ms", "log", "json"];

  /** 把一份文本解析成毫秒数组（规则与 `core/ts_source.py` 对齐）。 */
  function parseTimestamps(text) {
    var out = [];
    var s = String(text || "").replace(/^\uFEFF/, "");
    var t = s.trim();
    if (t.charAt(0) === "[" || t.charAt(0) === "{") {
      try {
        var obj = JSON.parse(t);
        var pick = function (v) {
          if (typeof v === "number") out.push(v);
          else if (v && v.length) for (var i = 0; i < v.length; i++) pick(v[i]);
        };
        var keys = ["t_ms", "ms", "time_ms", "timestamp", "timestamps", "time",
                    "times", "t", "onsets"];
        if (Array.isArray(obj)) pick(obj);
        else for (var k = 0; k < keys.length; k++) {
          if (obj && obj[keys[k]] !== undefined) { pick(obj[keys[k]]); break; }
        }
        if (out.length) return out;
      } catch (e) { /* 不是 JSON ⇒ 当文本走 */ }
      out = [];
    }
    var lines = s.split(/\r?\n/);
    for (var j = 0; j < lines.length; j++) {
      var line = lines[j].trim();
      if (!line || line.charAt(0) === "#" || line.slice(0, 2) === "//") continue;
      var clk = line.match(/^(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$/);
      if (clk) {
        var sec = (clk[1] ? parseInt(clk[1], 10) * 3600 : 0)
          + parseInt(clk[2], 10) * 60 + parseFloat(clk[3]);
        out.push(sec * 1000);
        continue;
      }
      var nums = line.replace(/[\[\]{}"']/g, " ").match(/-?\d+(?:\.\d+)?/g);
      if (nums && nums.length) out.push(parseFloat(nums[nums.length - 1]));
    }
    return out;
  }

  /** 问我们的 sidecar 要网格（`bpm / 相位 / 分母`）；连不上/超时就返回 null。 */
  Session.prototype.askGrid = function (times) {
    var self = this;
    return new Promise(function (resolve) {
      if (!self.connected) { resolve(null); return; }
      self._gridWait = resolve;
      self._gridTimer = setTimeout(function () {
        self._gridWait = null;
        self._gridTimer = null;
        self.say("网格探测超时（4s）⇒ 按宿主当前锚硬摆，可能会被吸附挪动");
        resolve(null);
      }, 4000);
      // ★★ 键名必须叫 `times`：`Session.send()` 会给**信封**打上
      //   `{v, seq, ts}`，用 `ts` 装毫秒数组会被 `ts: Date.now()` 直接冲掉
      //   （踩过：`gm.ts.length` 成了 undefined，测试抓出来的）。
      if (!self.send({ type: "grid", times: times })) {
        clearTimeout(self._gridTimer);
        self._gridTimer = null;
        self._gridWait = null;
        resolve(null);
      }
    });
  };

  /** 导入一份毫秒时间戳文件 → 变成宿主的一条轨（并打上我们的标记）。 */
  Session.prototype.importTimestamps = async function () {
    var self = this;
    var api = this.api;
    if (!api.system || !api.system.pickFile) {
      this.say("这个宿主版本没有 pickFile，导不了文件");
      return;
    }
    var path = await api.system.pickFile({
      title: "选一份毫秒时间戳（一行一个数）",
      filters: [{ name: "时间戳", extensions: TS_EXT }],
    });
    if (!path) return;
    var r = await api.system.readText(path);
    if (!r || r.canceled || typeof r.content !== "string") {
      this.say("读不到文件：" + path);
      return;
    }
    var ts = parseTimestamps(r.content);
    if (ts.length < 2) {
      this.say("这份文件里没读够时间戳（" + ts.length + " 个）——"
        + "支持一行一个毫秒数 / CSV（取每行最后一个数）/ mm:ss.xxx / JSON 数组");
      return;
    }
    ts.sort(function (x, y) { return x - y; });
    var uniq = [];
    for (var i = 0; i < ts.length; i++) {
      if (!uniq.length || Math.abs(ts[i] - uniq[uniq.length - 1]) > 1e-9) uniq.push(ts[i]);
    }
    this.say("读到 " + uniq.length + " 个时间戳，正在问我们的网格…");

    var grid = await this.askGrid(uniq);
    var snap = api.project.snapshot();
    var bpm = (grid && grid.bpm) || snap.baseBpm || 120;
    var phase = (grid && grid.phase_ms !== undefined) ? grid.phase_ms
      : (snap.offsetMs || 0);
    var div = (grid && grid.div) || 4;
    var edit = api.project.edit;
    var run = "TS-" + Date.now();
    var name = "ADO·时间戳";
    var report = { n: uniq.length, n_placed: 0, n_off_grid: 0, drift_max_ms: 0,
                   grid: grid, bpm: bpm, offsetMs: phase, div: div, path: path };
    var want = [];
    var tid = null;
    try {
      edit.batch(function () {
        if (grid) {                       // ★ 锚必须早于拍位
          edit.setBaseBpm(bpm);
          edit.setOffset(phase);
        }
        tid = edit.addTrack({ name: name });
        if (!tid) return;
        for (var k = 0; k < uniq.length; k++) {
          var beat = api.project.beatOfTime(uniq[k]);
          var id = edit.addMarker({ trackId: tid, beat: beat });
          if (!id) continue;
          edit.setMarkerAttrs(id, { adbIdx: k, adbRun: run, adbRole: "main",
                                    adbTrack: 0, adbSrc: "ts" });
          want.push({ id: id, beat: beat, ms: uniq[k] });
        }
      });
    } catch (e) {
      this.say("导入失败：" + e.message);
      return;
    }
    var tm = api.project.snapshot();
    var byId = {};
    for (var a = 0; a < tm.markers.length; a++) byId[tm.markers[a].id] = tm.markers[a];
    for (var b = 0; b < want.length; b++) {
      var got = byId[want[b].id];
      if (!got) continue;
      report.n_placed++;
      var d = Math.abs(got.beat - want[b].beat);
      if (d > 1e-6) {
        report.n_off_grid++;
        var ms = Math.abs(api.project.timeOfBeat(got.beat) - want[b].ms);
        if (ms > report.drift_max_ms) report.drift_max_ms = ms;
      }
    }
    report.drift_max_ms = Math.round(report.drift_max_ms * 1000) / 1000;
    this.lastImport = Object.assign({}, report, { ts_import: true });
    var g = grid
      ? ("网格：砖长 " + (grid.period_ms || 0).toFixed(3) + "ms（bpm "
         + (grid.bpm || 0).toFixed(3) + "）· 相位 " + (grid.phase_ms || 0).toFixed(3)
         + "ms · 分母 1/" + div)
      : "★ 没连上我们 ⇒ 用宿主当前锚硬摆（可能会被吸附挪动）";
    this.say("导入完成：" + report.n_placed + "/" + uniq.length + " 点落轨「" + name + "」\n" + g
      + (report.n_off_grid
        ? ("\n★ " + report.n_off_grid + " 点被吸附挪动，最大 "
           + report.drift_max_ms + "ms ⇒ 请把顶栏吸附分母设为 1/" + div + " 再导一次")
        : "\n逐点无损 ✓"));
    if (grid && grid.div) {
      this.say("提示：宿主顶栏「吸附」建议设成 1/" + div + "（格 "
        + (grid.step_ms || 0).toFixed(3) + "ms），与我们的格一致");
    }
    this.pushProjectNow();
    return report;
  };

  /* ------------------------------------------------------------ 推送 */
  Session.prototype.pushProject = function () {
    var self = this;
    if (!this.connected) return;
    if (this._timer) return; // 去抖：合并成一次
    this._timer = setTimeout(function () {
      self._timer = null;
      try {
        self.send({ type: "project", project: self.api.project.snapshot() });
      } catch (e) {
        self.say("快照取不到：" + e.message);
      }
    }, DEBOUNCE_MS);
  };

  Session.prototype.pushProjectNow = function () {
    var self = this;
    if (this._timer) {
      clearTimeout(this._timer);
      this._timer = null;
    }
    if (!this.connected) return;
    try {
      this.send({ type: "project", project: this.api.project.snapshot() });
    } catch (e) {
      self.say("快照取不到：" + e.message);
    }
  };

  Session.prototype.pushSelection = function () {
    var self = this;
    if (!this.connected || this._selTimer) return;
    this._selTimer = setTimeout(function () {
      self._selTimer = null;
      if (!self.connected) return;
      var s = self.api.selection.current();
      self.send({ type: "selection", kind: s.kind, id: s.id, markerIds: s.markerIds });
    }, SELECTION_MS);
  };

  Session.prototype.pushPlayhead = function () {
    var self = this;
    if (!this.connected || this._playTimer) return;
    this._playTimer = setTimeout(function () {
      self._playTimer = null;
      if (!self.connected) return;
      self.send({ type: "playhead", beat: self.api.project.beatOfTime(self.api.player.positionMs()) });
    }, PLAYHEAD_MS);
  };

  Session.prototype.pushAudio = function () {
    if (!this.connected) return;
    var path = this.api.system.audioPath();
    var s = this.api.project.snapshot();
    this.send({ type: "audio", path: path, name: s.audioName, md5: s.audioMd5 });
  };

  function caps() {
    return { loop: true, typedTracks: true, attrs: true, snapshot: true };
  }

  /* ------------------------------------------------------------ 入口 */
  window.__bdgPluginRegister(function activate(api) {
    var offs = [];
    var session = new Session(api, function () {
      render();
    });

    try {
      session.url = window.localStorage.getItem(LS_KEY) || "";
    } catch (e) {
      session.url = "";
    }

    // ★ 记住过地址就**自动重连**。
    //   以前每次宿主重载/重启都要人去面板点一下「连接」——
    //   而「重载宿主」恰恰是**开发中最常做的事**（改了 plugin 就得重载，
    //   见 README：Vite 不会热更新 root 之外的 junction 路径）。
    if (session.url) {
      setTimeout(function () {
        try {
          session.say("记得上次的地址，自动重连…");
          session.connect();
        } catch (e) { /* 连不上就算了，面板里还能手点 */ }
      }, 300);
    }

    // ★★ 事件订阅挂**会话**，不挂面板（2026-10 · BDG 0.2.8 升级实测）。
    //   以前这三行写在下面 registerPanel 的 mount() 里 —— 于是**面板没被打开过**
    //   就一个事件都不订阅：我们在 sidecar 里的工程快照永远停在握手那一份，
    //   收回时会把「宿主里明明有的点」全判成 deleted。
    //   真机复现：BDG 0.2.8 + 全新重载、没按 Alt+Shift+B ⇒ 投 7 点后
    //   `/api/bridge?full=1` 仍报 n_points=0、n_tracks=1；按一下 Alt+Shift+B
    //   立刻变成 n_points=4、n_tracks=3。
    //   面板只是「显示」，订阅得活到插件被卸载（宿主 finalize 会统一退订）。
    offs.push(api.events.on("project", function () {
      session.pushProject();
    }));
    offs.push(api.events.on("selection", function () {
      session.pushSelection();
    }));
    offs.push(api.events.on("playhead", function () {
      session.pushPlayhead();
    }));

    var host = null;

    function el(tag, cls, text) {
      var n = document.createElement(tag);
      if (cls) n.className = cls;
      if (text !== undefined) n.textContent = text;
      return n;
    }

    var statusEl = null;
    var statsEl = null;
    var logEl = null;
    var inputEl = null;
    var btnEl = null;

    function render() {
      if (!statusEl) return;
      var dot = session.connected ? (session.accepted ? "● 已连接" : "◐ 握手…") : "○ 未连接";
      statusEl.textContent = dot;
      statusEl.style.color = session.connected ? (session.accepted ? "#22c55e" : "#eab308") : "#9ca3af";
      statsEl.textContent =
        "发送 " + session.sent + " 条 · 已同步 " + session.pushed + " 次" +
        (session.lastError ? "\n最后：" + session.lastError : "");
      logEl.textContent = session.logs.join("\n");
      if (btnEl) btnEl.textContent = session.connected ? "断开" : "连接";
    }

    var panel = api.ui.registerPanel({
      id: "bridge",
      title: { zh: "ADO 谱面桥", en: "ADO Bridge" },
      mount: function mount(root) {
        root.textContent = "";

        var wrap = el("div");
        wrap.style.cssText = "font:12px/1.6 ui-monospace,Consolas,monospace;padding:8px;min-width:320px";
        root.appendChild(wrap);

        var head = el("div", null, "ADO 谱面桥 v0.1.0（只读）");
        head.style.cssText = "font-weight:600;margin-bottom:6px";
        wrap.appendChild(head);

        statusEl = el("div");
        statusEl.style.marginBottom = "6px";
        wrap.appendChild(statusEl);

        inputEl = el("input");
        inputEl.type = "text";
        inputEl.placeholder = "ws://127.0.0.1:8765/ws?token=…";
        inputEl.value = session.url;
        inputEl.style.cssText =
          "width:100%;box-sizing:border-box;background:#111827;color:#e5e7eb;" +
          "border:1px solid #374151;border-radius:4px;padding:4px 6px";
        inputEl.addEventListener("change", function () {
          session.setUrl(inputEl.value);
          render();
        });
        wrap.appendChild(inputEl);

        var row = el("div");
        row.style.cssText = "display:flex;gap:6px;margin:6px 0";
        wrap.appendChild(row);

        btnEl = el("button", null, "连接");
        btnEl.style.cssText = "flex:1;padding:4px 8px;cursor:pointer";
        btnEl.addEventListener("click", function () {
          session.setUrl(inputEl.value);
          if (session.connected) session.disconnect();
          else session.connect();
          render();
        });
        row.appendChild(btnEl);

        var btnPush = el("button", null, "立即推一次");
        btnPush.style.cssText = "flex:1;padding:4px 8px;cursor:pointer";
        btnPush.addEventListener("click", function () {
          session.pushProjectNow();
        });
        row.appendChild(btnPush);

        // ★★ 用户 2026-10：「我需要工程可以回到我们的工具里面（bdg插件层 ui
        //    『返回数据到谱面生成器』）」—— 这一颗就是那个按钮。
        var row2 = el("div");
        row2.style.cssText = "display:flex;gap:6px;margin:0 0 6px";
        wrap.appendChild(row2);

        var btnBack = el("button", null, "⤴ 返回数据到谱面生成器");
        btnBack.title = "把这条工程里「我们投送过的轨道」整条发回谱面生成器"
                      + "（那边会把这些轨当作它的音轨）";
        btnBack.style.cssText =
          "flex:1;padding:5px 8px;cursor:pointer;font-weight:600;" +
          "background:#1d4ed8;color:#fff;border:1px solid #1e40af;border-radius:4px";
        btnBack.addEventListener("click", function () {
          session.sendBack();
        });
        row2.appendChild(btnBack);

        // ★★ 用户那条箭头的前半段：「毫秒时间戳 → BDG」（docs/45 §7）
        var row3 = el("div");
        row3.style.cssText = "display:flex;gap:6px;margin:0 0 6px";
        wrap.appendChild(row3);

        var btnTs = el("button", null, "⇣ 导入时间戳文件…");
        btnTs.title = "读一份毫秒时间戳（一行一个数）→ 问我们的网格 → 摆成一条轨"
                    + "（打好我们的标记，之后可按「返回数据」整条收回）";
        btnTs.style.cssText = "flex:1;padding:4px 8px;cursor:pointer";
        btnTs.addEventListener("click", function () {
          session.importTimestamps();
        });
        row3.appendChild(btnTs);

        statsEl = el("div");
        statsEl.style.cssText = "white-space:pre-wrap;color:#9ca3af;margin-bottom:6px";
        wrap.appendChild(statsEl);

        logEl = el("pre");
        logEl.style.cssText =
          "white-space:pre-wrap;max-height:120px;overflow:auto;margin:0;color:#6b7280";
        wrap.appendChild(logEl);

        render();

        // 事件订阅已在 activate 里挂好（见上面 ★★）—— 挂这里就只在面板被打开时生效。
        // 面板的 unmount 只负责面板自己的事。

        return function unmount() {
          /* 面板卸载不动会话订阅：宿主 dispose 插件时会统一退订 */
        };
      },
    });

    api.ui.registerAction({
      label: { zh: "ADO 桥：显示/隐藏", en: "ADO Bridge: toggle panel" },
      run: function () {
        panel.toggle();
      },
    });

    api.ui.registerShortcut({
      id: "adoc-toggle",
      label: { zh: "ADO 桥：显示/隐藏", en: "ADO Bridge: toggle panel" },
      combo: "Alt+Shift+B",
      run: function () {
        panel.toggle();
      },
    });

    api.ui.registerAction({
      label: { zh: "ADO 桥：重连并推一次", en: "ADO Bridge: reconnect & push" },
      run: function () {
        session.setUrl(inputEl ? inputEl.value : session.url);
        session.connect();
      },
    });

    // ★★ 「返回数据到谱面生成器」：菜单里也放一份（面板没开时也能点）
    api.ui.registerAction({
      label: { zh: "ADO 桥：返回数据到谱面生成器",
               en: "ADO Bridge: send tracks back" },
      run: function () {
        session.sendBack();
      },
    });
    // ★★ 「导入时间戳」：也进菜单 + 注册成宿主的**导入器**（文件菜单里能直接选）
    api.ui.registerAction({
      label: { zh: "ADO 桥：导入毫秒时间戳文件…",
               en: "ADO Bridge: import timestamps" },
      run: function () {
        session.importTimestamps();
      },
    });
    if (api.ui.registerImporter) {
      api.ui.registerImporter({
        label: { zh: "ADO 时间戳（毫秒）…", en: "ADO timestamps (ms)…" },
        run: function () {
          session.importTimestamps();
        },
      });
    }
    api.ui.registerShortcut({
      id: "adoc-back",
      label: { zh: "ADO 桥：返回数据到谱面生成器",
               en: "ADO Bridge: send tracks back" },
      combo: "Alt+Shift+R",
      run: function () {
        session.sendBack();
      },
    });

    // 类型化轨 = 角色（docs/35 §3.6）：轨道级唯一的自由字段是 type，
    // 宿主会把插件 id 前缀加上，最终 type = "dev.adocharter.bdg-bridge:main"
    var roles = [
      { local: "main", zh: "主轨", en: "Main", color: "#38bdf8" },
      { local: "sub", zh: "次轨", en: "Sub", color: "#34d399" },
      { local: "dp", zh: "双押轨", en: "Double press", color: "#f472b6" },
      { local: "off", zh: "关轨", en: "Off", color: "#6b7280" },
    ];
    for (var i = 0; i < roles.length; i++) {
      var r = roles[i];
      var res = api.trackTypes.register({
        id: r.local,
        trackName: { zh: r.zh, en: r.en },
        pointName: { zh: r.zh + "点", en: r.en + " point" },
        color: r.color,
        fields: [],
      });
      if (!res || !res.ok) api.log("[bridge] 角色轨注册失败", r.local, res && res.reason);
    }

    api.log("[bridge] 已加载；面板 Alt+Shift+B；地址存在 localStorage." + LS_KEY);

    // ★ 调试钩子：面板里点不出来时（或要脚本化验证时）可以从开发者工具驱动它。
    //   例：__adocBridge.session.pushProjectNow()
    //       __adocBridge.api.project.edit.addMarker({trackId:"…", beat:12})
    window.__adocBridge = { api: api, session: session, panel: panel };

    return function dispose() {
      session.disconnect();
      while (offs.length) {
        var f = offs.pop();
        try {
          f();
        } catch (e) {
          /* ignore */
        }
      }
    };
  });

  // 供 Node 侧单测（tools/_bdg_plugin_test.js）取用，浏览器里无副作用
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { Session: Session, DEBOUNCE_MS: DEBOUNCE_MS,
                       parseTimestamps: parseTimestamps };
  }
})();
