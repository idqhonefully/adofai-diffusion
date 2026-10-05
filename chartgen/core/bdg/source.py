# -*- coding: utf-8 -*-
"""把 `.bdg` / 插件快照当**生成源**（`docs/38` §9）。

思路：**把它做成一种 `MidiFile`** ——
于是「选轨 → 采音 → 求解」那条早已跑通的路**一行都不用改**：

    .bdg ──parse──► Project ──to_midi_like──► MidiFile（每条 BDG 轨 = 一条音轨）
                                                   │
                          用户在**现有的**「主轨多选 / 次级轨 / 双押轨」UI 里勾
                                                   ▼
                              onsets.build_onsets_multi(...) ──► solve()

★ 角色**不由我们猜**（用户 2026-10：「展示为音轨，让用户自己选择它做什么」）：
  `role_hints()` 只给**建议**（从 `type` 冒号后的 localId 来，插件角色轨就是干这个的），
  真正的角色是用户在三条列表里勾出来的。
"""

from __future__ import annotations

from ..midi import MidiFile, Note, Track
from . import aliases as al

PPQN = 960
DEF_PITCH = 60          # BDG 的点没有音高（它只有拍位）⇒ 统一给 C4，只为让上游摘要能显示
DEF_VELOCITY = 100
DEF_CHANNEL = 0         # ★ 非 9 ⇒ `is_drum_only()` 为 False，显示成「旋律」而不是「架子鼓」


def _ticks(ms: float, bpm: float, ppqn: int) -> int:
    """按**平铺**基准 BPM 把毫秒折成 tick（我们自己的速度表另行计算，不采他的）。"""
    if bpm <= 0:
        bpm = 120.0
    return int(round(ms * ppqn * bpm / 60000.0))


def to_midi_like(project, ppqn: int = PPQN) -> MidiFile:
    """`Project` → `MidiFile`（每条 BDG 轨 → 一条 `Track`）。

    · 拍位用**他自己的时间锚**换算成毫秒（`time_ms`，只读，实测逐点一致）；
    · 音长没有信息 ⇒ 零长（`t_off_ms = t_on_ms`），**不假装**有 dur；
    · `bpm0` 取他的 `baseBpm`；他的**变速不写进 tempo_map**（音乐内容以我们为准，
      他的变速另走 `speed_hints()` 只做参考/报告）。
    """
    base = float(project.base_bpm or 120.0)
    mf = MidiFile(path=getattr(project, "audio_name", "") or "",
                  format=0, ppqn=ppqn,
                  tracks=[], tempo_map=[(0, base)], time_sig=[(0, 4, 4)])
    for i, t in enumerate(project.tracks):
        trk = Track(index=i, name=t.name or ("trk%d" % i))
        for pt in project.points:
            if pt.track_id != t.id:
                continue
            ms = float(pt.time_ms)
            trk.notes.append(Note(track=i, channel=DEF_CHANNEL, pitch=DEF_PITCH,
                                  velocity=DEF_VELOCITY,
                                  t_on_ms=ms, t_off_ms=ms,
                                  t_on_tick=_ticks(ms, base, ppqn),
                                  t_off_tick=_ticks(ms, base, ppqn)))
        trk.notes.sort(key=lambda n: n.t_on_ms)
        mf.tracks.append(trk)
    mf.length_ms = max((n.t_on_ms for t in mf.tracks for n in t.notes), default=0.0)
    return mf


def role_hints(project) -> dict:
    """`{track_index: 建议角色}` —— **只是建议**，由用户在三条列表里最终决定。

    口径与 `parse.role_for_type` 对齐，只有一处故意不同：
    **没有 `type` 的普通踩点轨留空**（它可能是主/次/双押，只有用户知道）。
    """
    from .parse import role_for_type
    out = {}
    for i, t in enumerate(project.tracks):
        if not t.type:
            role = ""                  # 普通踩点轨：留空，让用户勾
        else:
            role = role_for_type(t.type)
        if t.hidden:
            role = al.ROLE_OFF         # 宿主的 hidden = 白送的「别导」
        if role:
            out[i] = role
    return out


def describe_tracks(project, midi=None) -> list:
    """给 UI 的轨道清单（**现有那三条列表直接用这个**）。

    `midi` 传 `to_midi_like()` 的结果时，会顺便带上 `track_summary` 摘要。
    """
    hints = role_hints(project)
    rows = []
    for i, t in enumerate(project.tracks):
        pts = [p for p in project.points if p.track_id == t.id]
        row = {
            al.K_INDEX: i,
            al.K_NAME: t.name or ("trk%d" % i),
            al.K_TYPE: t.type,
            al.K_HIDDEN: bool(t.hidden),
            al.K_SUGGEST: hints.get(i, ""),
            al.K_NOTES: len(pts),
            al.K_MS_LO: round(min((p.time_ms for p in pts), default=0.0), 3),
            al.K_MS_HI: round(max((p.time_ms for p in pts), default=0.0), 3),
            al.K_LOOP: sum(1 for p in pts if p.loop is not None),
            al.K_CHILD: sum(1 for p in pts if p.parent_id),
        }
        if midi is not None and i < len(midi.tracks):
            row[al.K_SUMMARY] = _summary(midi.tracks[i])
            row[al.K_DRUM] = midi.tracks[i].is_drum_only()
        rows.append(row)
    return rows


def _summary(trk) -> str:
    try:
        from ..onsets import track_summary
        return track_summary(trk)
    except Exception:                                       # noqa: BLE001
        return ""


def speed_hints(project) -> list:
    """他**自己的**变速（宿主 BPM 点 + 插件 BPM 轨的 attrs）⇒ `[(ms, bpm_kind, value)]`。

    ★ 只作参考：我们的 SetSpeed 要落在 `travel=180` 的平格上、2 的幂档，
      他的 `multiplier 1.7` 这种**照抄不了**。所以这一步只**报**，不采。
    """
    out = []
    tm = project.tempo
    for b in project.bpm_points:
        out.append({al.K_AT: al.ROOT_BPM_POINTS[0], al.K_MS: round(tm.time_of_beat(b.beat), 3),
                    al.K_BEAT: round(b.beat, 6), al.K_MODE: b.mode, al.K_VALUE: b.value})
    for e in project.bpm_events:
        out.append({al.K_AT: al.K_PLUGIN_TRACK, al.K_MS: round(tm.time_of_beat(e.beat), 3),
                    al.K_BEAT: round(e.beat, 6), al.K_MODE: e.speed_type,
                    al.K_VALUE: e.value})
    out.sort(key=lambda x: x[al.K_MS])
    return out


def ladder_fit(project) -> dict:
    """「他的变速能不能用我们的合法档复现？」—— `docs/38` §9.3 的第一笔账。

    我们的档是 2 的幂（`solve.SPEED_POW2`），所以：
    倍率是 2 的幂 ⇒ 能精确复现；否则给最近的合法档 + 偏差百分比。
    """
    import math
    try:
        from ..solve import SPEED_POW2
    except Exception:                                       # noqa: BLE001
        SPEED_POW2 = [0.5, 0.75, 1.0, 1.5, 2.0]
    rows = []
    for h in speed_hints(project):
        if str(h[al.K_MODE]) not in (al.SPEED_TYPE_MULT, al.MODE_MULT):
            rows.append(dict(h, fit="abs 模式：不是倍率，另行处理", dev_pct=None))
            continue
        v = float(h[al.K_VALUE] or 1.0)
        best = min(SPEED_POW2, key=lambda s: abs(math.log(s / v)) if v > 0 else 1e9)
        dev = abs(best / v - 1.0) * 100.0 if v else 0.0
        rows.append(dict(h, fit=("精确" if abs(best - v) < 1e-9 else "近似"),
                         nearest=best, dev_pct=round(dev, 3)))
    return {"rows": rows,
            "n_exact": sum(1 for r in rows if r.get("fit") == "精确"),
            "n_approx": sum(1 for r in rows if r.get("fit") == "近似")}
