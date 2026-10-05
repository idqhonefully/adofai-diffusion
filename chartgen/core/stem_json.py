# -*- coding: utf-8 -*-
"""**时间戳 JSON（DEMUCS 分轨 · 多路音头）** 当生成源。

规格原文：`docs/refs/时间戳JSON字段说明.md`（合作方 `adofai_diffusion` 的输出）；
接入方案：`docs/56-时间戳JSON兼容方案.md`。

用户口径（2026-10）：

    「这是一个新的常用格式，我希望我们的**桥接器、生成逻辑**可以基于它做一些兼容，
      **就像是现在兼容纯时间戳那样**。」

那份 JSON 一句话：DEMUCS 把歌拆成 4~6 条分轨，每轨各跑一个检测器（神经网络踩点 /
频谱通量），`stems` 字典里**每一路 = 一串秒数**。

    JSON ──read_stem_json──► [Stem…] ──to_midi_like──► MidiFile（**一路一轨**）
                                                          │
                            「选轨 → 采音 → 求解/直拟合 → 桥」**一行都不用改**

★ 四个口径（与 `core/ts_source.py` 同一套，只是多轨）：

1. `onsets_sec`（秒 → 我们内部一律**毫秒**×1000）是**唯一真源**；
   `onsets_frame` 只做**交叉校验**（`sec ≈ frame×hop_ms/1000`），不当真源；
2. **一路一 Track**（不像纯时间戳塌成一条）→ 用户能像 MIDI 那样分别勾选
   「哪路当主轨 / 次轨 / 双押轨」。每点一个 Note，常量与纯时间戳共用；
   **不用 channel 9 冒充鼓**（那会让 `is_drum_only()` 与音高过滤自动改写行为分叉），
   鼓的身份靠**轨道名 + method** 表达；
3. **建议角色**照 BDG 那套（`role_hints` → `info["stem_roles"]` → 三条列表带
   `suggest`）—— 角色只是**建议**，勾选权永远在用户；
4. **解析不静默**：版本、长度不符、sec/frame 偏差、未排序、重复、越界、空路、
   `has_vocals` 与键不一致 …… 全部记进 `report` 并上屏。
"""
from __future__ import annotations

import json
import os

from . import denoise as dn_mod
from . import ts_source as ts_mod
from .midi import MidiFile, Note, Track

__all__ = ["STEM_EXTS", "STEM_LABEL", "Stem", "looks_like_stem_json",
           "looks_like_stem_json_file", "read_stem_json", "to_midi_like",
           "role_hints", "role_notes", "default_checked", "report_text",
           "resolve_audio"]

#: 靠**内容嗅探**认它（后缀复用 `.json` —— 与纯时间戳同一后缀，见 `docs/56` §4.2）
STEM_EXTS = (".json",)

# ------------------------------------------------------------------ 字段名
#: 规格 `docs/refs/时间戳JSON字段说明.md` §二 / §四 的字面量。**只在这里出现**
#: （与 `core/bdg/aliases.py` 的纪律同理：上游改格式 → 只改这一段）。
KEY_VERSION = "version"
KEY_AUDIO = "source_audio"
KEY_DURATION = "duration_sec"
KEY_SAMPLE_RATE = "sample_rate"
KEY_HOP = "hop_ms"
KEY_SEP = "separation_model"
KEY_HAS_VOCALS = "has_vocals"
KEY_BPM_HINT = "bpm_hint"
KEY_STEMS = "stems"
STEM_SEC = "onsets_sec"
STEM_FRAME = "onsets_frame"
STEM_SOURCE = "source"
STEM_METHOD = "method"
STEM_MODEL = "model"
STEM_VOCALS = "vocals"
METHOD_ONSETNET = "onsetnet"
METHOD_FLUX = "spectral_flux"

#: schema 闸：只认 1；其它**警告但继续**（不许静默）
VERSION_OK = 1

#: 认得出的路名 → 中文（认不出的键**用键名本身，不丢**）
STEM_LABEL = {"melody": "旋律", "vocals": "人声", "drums": "鼓",
              "bass": "贝斯", "guitar": "吉他", "piano": "钢琴"}

#: 来源标注（前端的「来源」徽标 / 文件信息用）
SRC_LABEL = "时间戳 JSON（DEMUCS 分轨）"

# ------------------------------------------------------------------ 建议角色
#: ★ 建议角色表（`docs/56` §6.1 的**保守**口径）：一次只让一个变量动。
#:   只是**建议** —— 用户可以在三条列表里勾成任何组合。
ROLE_BY_KEY = {
    "melody": "main",     # 主轨：取并集
    "vocals": "main",     # 也是主轨，但**默认不勾**（只标注）
    "drums": "dp",        # 双押轨
    "bass": "sub",        # 次轨：只插空
    "guitar": "sub",
    "piano": "",          # 赠品通道，不表态
}
#: 每路的**人话注释**（跟着建议一起上屏，免得用户猜我们为什么这么建议）
NOTE_BY_KEY = {
    "melody": "主旋律 · 默认勾这一路",
    "vocals": "建议主轨 · 默认不勾（想要人声自己勾）",
    "drums": "建议双押轨 · 默认不勾",
    "bass": "建议次轨（只插空）· 默认不勾",
    "guitar": "建议次轨（只插空）· 默认不勾",
    "piano": "赠品通道 · 供选",
}

#: ★ 首屏**默认勾**的只有主旋律（vocals 也是 main 建议，但默认不勾）。
DEFAULT_MAIN_KEYS = ("melody",)
DEFAULT_SUB_KEYS: tuple = ()
DEFAULT_DP_KEYS: tuple = ()


#                                                                 小工具
def _as_num(v):
    """`int/float`（或数字字符串）→ `float`；布尔与 NaN 不算数。"""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
        return f if f == f else None
    if isinstance(v, str):
        try:
            f = float(v.strip())
        except Exception:                                       # noqa: BLE001
            return None
        return f if f == f else None
    return None


def _as_list(v):
    return list(v) if isinstance(v, list) else []


def looks_like_stem_json(raw: str) -> bool:
    """顶层 dict **且有 `stems` 字典**（`version` 可选）→ 是「时间戳 JSON」。

    空 `stems` 也算认（那样会明确报「每一路都是空的」，**不会**退化成垃圾谱）。
    """
    s = (raw or "").lstrip()
    if s[:1] != "{":
        return False
    try:
        obj = json.loads(raw)
    except Exception:                                           # noqa: BLE001
        return False
    return isinstance(obj, dict) and isinstance(obj.get(KEY_STEMS), dict)


def looks_like_stem_json_file(path: str) -> bool:
    """按内容嗅探一份文件（读不了 / 不是这个格式 → `False`，不抛）。"""
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            return looks_like_stem_json(fh.read())
    except OSError:
        return False


# ------------------------------------------------------------------ 一路
class Stem:
    """一路音头（= 一条轨的料）。

    `ms` 是**唯一真源**（`onsets_sec` × 1000，过了一遍 `core.ts_source` 的清洗：
    排序 / 同刻合并 / 丢 NaN 与越界）。
    """

    __slots__ = ("key", "label", "source", "method", "model", "ms",
                 "n_raw", "n_sec", "n_frame", "n_unsorted", "n_frame_bad",
                 "n_dup", "n_bad", "frame_gap_ms")

    def __init__(self, key: str, source: str = "", method: str = "",
                 model: str = ""):
        self.key = key
        self.label = STEM_LABEL.get(key, key)       # ★ 认不出的键用键名本身，不丢
        self.source = source or ""
        self.method = method or ""
        self.model = model or ""
        self.ms: list[float] = []
        self.n_raw = 0            # `onsets_sec` 原样给了几个
        self.n_sec = 0
        self.n_frame = 0
        self.n_unsorted = 0       # 相邻逆序次数
        self.n_frame_bad = 0      # sec 与 frame×hop 差 > 1 帧的个数
        self.n_dup = 0            # 同刻合并掉的
        self.n_bad = 0            # 丢弃的 NaN / 越界 / 非法
        self.frame_gap_ms = 0.0   # 最大偏差（ms）

    @property
    def n(self) -> int:
        return len(self.ms)

    def info(self) -> dict:
        """给 UI / 报告的一行摘要。"""
        return {
            "key": self.key, "label": self.label,
            "source": self.source, "method": self.method, "model": self.model,
            "n": self.n, "n_raw": self.n_raw, "n_sec": self.n_sec,
            "n_frame": self.n_frame, "n_unsorted": self.n_unsorted,
            "n_dup": self.n_dup, "n_bad": self.n_bad,
            "n_frame_mismatch": self.n_frame_bad,
            "frame_gap_ms": round(self.frame_gap_ms, 4),
            "ms_lo": round(self.ms[0], 3) if self.ms else 0.0,
            "ms_hi": round(self.ms[-1], 3) if self.ms else 0.0,
            "role": ROLE_BY_KEY.get(self.key, ""),
            "note": NOTE_BY_KEY.get(self.key, ""),
            "empty": not self.ms,
        }


# ------------------------------------------------------------------ 读
def read_stem_json(path: str):
    """读一份「时间戳 JSON」→ `(stems, report)`。

    `report["meta"]` 是给前端的紧凑头（版本 / 原曲 / 时长 / 采样率 / hop /
    分离模型 / has_vocals / bpm_hint / 每一路的摘要行）；
    `report["warnings"]` 是**人话**的取舍账（一条都不许静默吞掉）。
    """
    if not path or not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        raw = fh.read()
    try:
        obj = json.loads(raw)
    except Exception as exc:                                    # noqa: BLE001
        raise ValueError("时间戳 JSON 解析失败：{}".format(exc))
    if not isinstance(obj, dict) or not isinstance(obj.get(KEY_STEMS), dict):
        raise ValueError("这份 JSON 里没有 `stems` —— 不是「时间戳 JSON」")

    warn: list[str] = []
    notes: list[str] = []

    # ---- 顶层
    version = obj.get(KEY_VERSION)
    version_ok = (version == VERSION_OK)
    if not version_ok:
        warn.append("★ `version` = {}（我们认 {}）—— 仍然尝试解析，"
                    "但 schema 可能变了".format(version, VERSION_OK))
    hop = _as_num(obj.get(KEY_HOP))
    if not hop or hop <= 0:
        warn.append("★ `hop_ms` 缺失/不合法（{}）→ **不做** sec↔frame 交叉校验"
                    .format(obj.get(KEY_HOP)))
        hop = 0.0
    dur = _as_num(obj.get(KEY_DURATION))
    if dur is None or dur <= 0:
        warn.append("· `duration_sec` 缺失/不合法 → 尾部长度只用「末点 + 4 拍」")
        dur = 0.0
    sr = _as_num(obj.get(KEY_SAMPLE_RATE))
    bpm_hint = _as_num(obj.get(KEY_BPM_HINT)) or 0.0
    has_voc = obj.get(KEY_HAS_VOCALS)
    has_voc_note = None if has_voc is None else bool(has_voc)
    if has_voc is not None and not isinstance(has_voc, bool):
        warn.append("· `has_vocals` 不是布尔（{}）—— 按真假理解".format(repr(has_voc)))

    # ---- 每一路
    stems: list[Stem] = []
    for key, sub in obj[KEY_STEMS].items():
        if not isinstance(sub, dict):
            warn.append("★ 路 `{}` 不是对象（{}）→ 跳过这一路".format(
                key, type(sub).__name__))
            continue
        st = Stem(str(key), _str(sub.get(STEM_SOURCE)), _str(sub.get(STEM_METHOD)),
                  _str(sub.get(STEM_MODEL)))
        for fld, v in ((STEM_SOURCE, sub.get(STEM_SOURCE)),
                       (STEM_METHOD, sub.get(STEM_METHOD))):
            if not v:
                warn.append("· 路 `{}` 缺 `{}`（诊断信息不全，点照常采）"
                            .format(key, fld))
        sec = _as_list(sub.get(STEM_SEC))
        frame = _as_list(sub.get(STEM_FRAME))
        st.n_sec, st.n_frame = len(sec), len(frame)
        st.n_raw = len(sec)

        if not sec and frame:
            # ★ 只给了帧 → 用帧换算（**报出来**，不当成「没有数据」）
            if hop:
                warn.append("· 路 `{}` 只有 `onsets_frame`、没有 `onsets_sec` → "
                            "按 hop_ms 换算成时间戳".format(key))
                sec = [f * hop / 1000.0 for f in frame]
                st.n_raw = len(sec)
            else:
                warn.append("★ 路 `{}` 只有 `onsets_frame` 而 `hop_ms` 不合法 → "
                            "这一路读不出来".format(key))

        if not sec:
            warn.append("· 路 `{}`（{}）的 `onsets_sec` 是空的 → **不建轨**"
                        .format(key, st.label))
            stems.append(st)
            continue

        if not frame:
            if hop:
                notes.append("· 路 `{}` 没有 `onsets_frame`（只信 sec）".format(key))

        # 未排序 / 类型垃圾
        ms_raw = []
        for v in sec:
            f = _as_num(v)
            ms_raw.append(float("nan") if f is None else f * 1000.0)
        for a, b in zip(ms_raw, ms_raw[1:]):
            if b < a:
                st.n_unsorted += 1
        n_junk = sum(1 for v in sec if _as_num(v) is None)

        # sec ↔ frame 交叉校验（只诊断，不改真源）
        if hop:
            n_cmp = min(len(sec), len(frame))
            for i in range(n_cmp):
                s_ms = _as_num(sec[i])
                f_ms = _as_num(frame[i])
                if s_ms is None or f_ms is None:
                    continue
                gap = abs(s_ms * 1000.0 - f_ms * hop)
                if gap > hop:                       # > 1 帧
                    st.n_frame_bad += 1
                    st.frame_gap_ms = max(st.frame_gap_ms, gap)
            if len(sec) != len(frame):
                warn.append("★ 路 `{}`：`onsets_sec` {} 个而 `onsets_frame` {} 个"
                            "（不等长）—— **只信 sec**".format(
                                key, len(sec), len(frame)))
            if st.n_frame_bad:
                warn.append("★ 路 `{}`：{} 个点的 sec 与 frame×hop_ms 差超过 1 帧"
                            "（最大 {:.3f}ms）—— 只信 sec".format(
                                key, st.n_frame_bad, st.frame_gap_ms))

        stat: dict = {}
        st.ms = ts_mod.clean_points(ms_raw, stat)
        st.n_dup = int(stat.get("n_dup") or 0)
        st.n_bad = int(stat.get("n_bad") or 0)
        _bits = []
        if st.n_unsorted:
            _bits.append("未排序，已排序（{} 处逆序）".format(st.n_unsorted))
        if st.n_dup:
            _bits.append("同刻合并 {}".format(st.n_dup))
        if st.n_bad:
            _bits.append("丢弃 NaN/越界/非法 {}".format(st.n_bad))
        if n_junk:
            _bits.append("非数字项 {}".format(n_junk))
        if _bits:
            warn.append("· 路 `{}`（{}）：{}".format(key, st.label, " / ".join(_bits)))
        if not st.ms:
            warn.append("★ 路 `{}`（{}）清洗后一个点都不剩 → **不建轨**"
                        .format(key, st.label))
        stems.append(st)

    # ---- has_vocals 与键的一致性
    keys = set(obj[KEY_STEMS].keys())
    if has_voc_note is True and STEM_VOCALS not in keys:
        warn.append("★ `has_vocals=true` 却没有 `vocals` 这一路（继续，人声当没有）")
    elif has_voc_note is False and STEM_VOCALS in keys:
        warn.append("★ `has_vocals=false` 却有 `vocals` 这一路 —— 按**存在**为准")

    live = [s for s in stems if s.ms]
    if not live:
        warn.append("★ 这份 JSON 的**每一路都是空的**（或全被清洗掉）—— 一个点都没有")

    total = sum(s.n for s in live)
    meta = {
        "src": SRC_LABEL,
        "path": path,
        "name": os.path.splitext(os.path.basename(path))[0],
        "version": version, "version_ok": version_ok,
        "source_audio": _str(obj.get(KEY_AUDIO)),
        "duration_ms": round(dur * 1000.0, 3),
        "sample_rate": int(sr) if sr else 0,
        "hop_ms": round(hop, 6),
        "separation_model": _str(obj.get(KEY_SEP)),
        "has_vocals": has_voc_note,
        "bpm_hint": bpm_hint,
        "n_stems": len(stems), "n_live": len(live), "n_points": total,
        "stems": [s.info() for s in stems],
    }
    rep = {
        "src": SRC_LABEL,
        "path": path,
        "n_stems": len(stems), "n_live": len(live), "n_kept": total,
        "n_raw": sum(s.n_raw for s in stems),
        "ms_lo": round(min((s.ms[0] for s in live), default=0.0), 3),
        "ms_hi": round(max((s.ms[-1] for s in live), default=0.0), 3),
        "warnings": warn, "notes": notes, "meta": meta,
    }
    return stems, rep


def _str(v) -> str:
    return "" if v is None else str(v)


# ------------------------------------------------------------------ 建议角色
def role_hints(stems) -> dict:
    """`{轨下标: 建议角色}` —— **只是建议**（勾选权在用户）。

    下标就是 `to_midi_like()` 里那条轨的 `index`（= **有料的路**的顺序）。
    """
    return {i: ROLE_BY_KEY.get(s.key, "") for i, s in enumerate(stems)
            if ROLE_BY_KEY.get(s.key, "")}


def role_notes(stems) -> dict:
    """`{轨下标: 人话注释}`（认不出的路给空串，不硬编）。"""
    return {i: NOTE_BY_KEY.get(s.key, "") for i, s in enumerate(stems)
            if NOTE_BY_KEY.get(s.key, "")}


def default_checked(stems) -> tuple[list[int], list[int], list[int]]:
    """首屏**默认勾选**（`docs/56` §6.1 的保守口径）：
    只勾 `melody` 当主轨；次轨/双押轨**一条都不默认勾**（建议已标出，自己勾）。

    返回 `(mains, subs, dps)`；`melody` 不在时退回「有料的第一路」。
    """
    live = [(i, s) for i, s in enumerate(stems) if s.ms]
    mains = [i for i, s in live if s.key in DEFAULT_MAIN_KEYS]
    subs = [i for i, s in live if s.key in DEFAULT_SUB_KEYS]
    dps = [i for i, s in live if s.key in DEFAULT_DP_KEYS]
    if not mains and live:
        mains = [live[0][0]]
    return mains, subs, dps


# ------------------------------------------------------------------ 造 MidiFile
def _main_stem(live) -> "Stem":
    """算网格的**那一路**：优先 `melody`，没有就用点最多的（`docs/56` §6.2-A）。"""
    for s in live:
        if s.key == "melody":
            return s
    return max(live, key=lambda s: s.n)


def to_midi_like(stems, bpm: float = 0.0, *, name: str = "",
                 hint: float | None = None, div: int | None = None,
                 duration_ms: float = 0.0, meta: dict | None = None) -> MidiFile:
    """`[Stem…]` → `MidiFile`（**一路一 Track**，每点一个 Note）。

    `bpm=0` → 用 `core.denoise` 在**主路**（melody）上估出来的砖长（算不出来退回 120）。
    """
    live = [s for s in stems if s.ms]
    if not live:
        raise ValueError("这份时间戳 JSON 里每一路都是空的，一个音头都没有")
    total = sum(s.n for s in live)
    if total < 2:
        raise ValueError("总共只有 {} 个音头（少于 2），成不了谱".format(total))

    main = _main_stem(live)
    g = dn_mod.plan(main.ms, hint=hint, div=div)
    base = float(bpm or (g.bpm if g.ok else 0.0) or 120.0)
    beat_ms = 60_000.0 / base
    ppqn = ts_mod.PPQN

    tracks = []
    for i, s in enumerate(live):
        trk = Track(index=i, name="{}·{}".format(s.label, s.key))
        for x in s.ms:
            tk = int(round(x * ppqn * base / 60_000.0))
            trk.notes.append(Note(track=i, channel=ts_mod.DEF_CHANNEL,
                                  pitch=ts_mod.DEF_PITCH,
                                  velocity=ts_mod.DEF_VELOCITY,
                                  t_on_ms=x, t_off_ms=x,
                                  t_on_tick=tk, t_off_tick=tk))
        tracks.append(trk)

    mf = MidiFile(path="", format=1, ppqn=ppqn, tracks=tracks,
                  tempo_map=[(0, base)], time_sig=[(0, 4, 4)])
    last = max(s.ms[-1] for s in live)
    mf.length_ms = max(float(duration_ms or 0.0),
                       last + ts_mod.TAIL_BEATS * beat_ms)
    # ★ 与纯时间戳**同一套**非标准字段（`sidecar.session.load()` 走 getattr）
    mf.is_ts = True                      # type: ignore[attr-defined]
    mf.grid_fit = g                      # type: ignore[attr-defined]
    mf.ts_plan = g.to_dict()             # type: ignore[attr-defined]
    mf.ts_meta = {                       # type: ignore[attr-defined]
        "n": total, "n_lines": 0, "n_kept": total, "n_skipped": 0,
        "n_dup": 0, "src": SRC_LABEL,
        "ms_lo": round(min(s.ms[0] for s in live), 3),
        "ms_hi": round(last, 3),
    }
    # ★ 多出来的三个（只有这条路才有）
    mf.is_stem_json = True               # type: ignore[attr-defined]
    mf.stem_hints = role_hints(live)     # type: ignore[attr-defined]
    mf.stem_notes = role_notes(live)     # type: ignore[attr-defined]
    mf.stem_meta = dict(meta or {})      # type: ignore[attr-defined]
    mf.stem_main_track = live.index(main)  # type: ignore[attr-defined]
    mf.stem_live = [s.key for s in live]   # type: ignore[attr-defined]
    # ★ 网格是按**哪一路**算的（`docs/56` §6.2-A）—— 报告里要说出来
    mf.stem_meta["main_key"] = main.key    # type: ignore[attr-defined]
    return mf


# ------------------------------------------------------------------ 原曲
def resolve_audio(name: str, json_path: str) -> tuple[str, list[str]]:
    """`source_audio` → **真实存在**的音频路径（`docs/56` §6.4）。

    解析顺序：JSON 同目录 → JSON 的上一级 → 原样（绝对路径 / 当前目录）。
    返回 `(找到的路径 or "", 试过的候选)`——**找不到不报错**，只是不绑；
    试过哪些要上屏（不许静默）。
    """
    name = (name or "").strip()
    if not name:
        return "", []
    base = os.path.dirname(os.path.abspath(json_path))
    cands = [os.path.join(base, name),
             os.path.join(os.path.dirname(base), name),
             name]
    seen = set()
    tried = []
    for c in cands:
        k = os.path.normcase(os.path.abspath(c))
        if k in seen:
            continue
        seen.add(k)
        tried.append(c)
        if os.path.isfile(c):
            return c, tried
    return "", tried


# ------------------------------------------------------------------ 人话报告
def report_text(rep: dict, plan: dict | None = None) -> str:
    """上屏用的人话报告（`Session.warnings` / 文件信息都吃它）。"""
    meta = rep.get("meta") or {}
    out = ["[{}] {} 路 / 共 {} 个音头（{:.1f}~{:.1f}ms）· 原样 {} 个 · {}".format(
        SRC_LABEL, rep.get("n_live", 0), rep.get("n_kept", 0),
        rep.get("ms_lo", 0.0), rep.get("ms_hi", 0.0), rep.get("n_raw", 0),
        ("separation_model=" + meta.get("separation_model", ""))
        if meta.get("separation_model") else "无 separation_model")]
    out.append("    hop {:.4f}ms · 采样率 {} · duration {:.3f}s · bpm_hint {}".format(
        meta.get("hop_ms", 0.0), meta.get("sample_rate", 0),
        (meta.get("duration_ms", 0.0) or 0.0) / 1000.0,
        meta.get("bpm_hint") or "（无）"))
    if not meta.get("version_ok", True):
        out.append("    ★ version = {}（我们认 {}）".format(
            meta.get("version"), VERSION_OK))
    for row in meta.get("stems", []):
        out.append("    · {:<7}{:<5}{:<15}{}{}　{} 点　{:.1f}~{:.1f}ms".format(
            row.get("key", ""), row.get("label", ""),
            row.get("method", "") or "（无 method）",
            ("　" + row.get("model", "")) if row.get("model") else "",
            "　[空]　" if row.get("empty") else "",
            row.get("n", 0), row.get("ms_lo", 0.0), row.get("ms_hi", 0.0)))
    if plan:
        if plan.get("ok"):
            out.append("    网格（按 {} 算）：砖长 {:.4f}ms（bpm {:.3f}）· 相位 {:+.3f}ms"
                       " · 分母 1/{}".format(
                           (meta.get("main_key") or "主路"),
                           plan.get("period_ms", 0.0), plan.get("bpm", 0.0),
                           plan.get("phase_ms", 0.0), plan.get("div", 0)))
        else:
            out.append("    ★ 网格没通过：{}（去噪会跳过，直拟合仍可用）".format(
                plan.get("reason") or "?"))
    for w in (rep.get("warnings") or []):
        out.append("    " + w)
    return "\n".join(out)
