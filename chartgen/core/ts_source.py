# -*- coding: utf-8 -*-
"""**毫秒时间戳**当生成源（`docs/45` §7）。

用户口径（2026-10）：

    midi  ──► 多条音轨 ──► 采音 ──► BDG（保留多音轨线）
    毫秒时间戳 ──► BDG ──► 回到我们的工具

第二条路的**第一段**（时间戳 → 我们能算的东西）就是本模块：把一份裸时间戳
做成 `MidiFile`（**一条轨、每个时间戳一个 Note**）——

    时间戳文件 ──read_ts──► [ms…] ──to_midi_like──► MidiFile
                                                      │
                        「选轨 → 采音 → 求解/直拟合 → 桥」**一行都不用改**

★ 三个口径：

1. **砖长/网格先算出来**（`core.denoise.plan`）并写进 `tempo_map[0]` 与
   `mf.grid_fit` —— 于是界面上的文件信息、去噪报告、桥接锚都有依据；
2. 时间戳**不许被 `merge_ms` 吃掉**：`load()` 会给这条来源
   `default_merge_ms = 0`（30ms 的默认合并会把密集处的音悄悄并掉）；
3. 解析**不静默**：跳过的行、识别到的时刻格式、取的是哪一列，全部记进 `report`。
"""
from __future__ import annotations

import json
import os
import re

from . import denoise as dn_mod
from .midi import MidiFile, Note, Track

__all__ = ["TS_EXTS", "PPQN", "read_ts", "looks_like_ts", "to_midi_like",
           "grid_info", "report_text", "clean_points"]

#: 我们认的时间戳后缀（`.json` 也认 —— 但只在里面真能读出数的时候才当它是时间戳）
TS_EXTS = (".txt", ".csv", ".tsv", ".ms", ".ts", ".json", ".log")
PPQN = 480                 # 与 MIDI 兜底一致
DEF_PITCH = 60
DEF_VELOCITY = 100
DEF_CHANNEL = 0            # 非 9 ⇒ 显示成「旋律」而不是「架子鼓」
TAIL_BEATS = 4.0           # 收尾余量：最后一个音之后留 4 拍（谱面/预览要尾巴）

_TS_RE = re.compile(r"^(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$")     # mm:ss.xxx / hh:mm:ss.xxx
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")
_CLEAN_RE = re.compile(r"[\[\]{}\"']")
_COMMENT = ("#", "//", ";")

#: ★★ 结构化文件的**指纹键**（2026-10 · 用户给了一份「时间戳 JSON（DEMUCS 分轨）」）。
#:   出现其中任一 ⇒ 这份 JSON 是**别的工具的工程/数据文件**（BDG 工程 `app`+`markers`、
#:   分轨时间戳 `stems`、插件快照 `tracks`…），**不是**「一串时间戳」。
#:   为什么要有它：「顶层所有数字」那条兜底在这种文件上必然吃**垃圾** —— 实测把
#:   stem-JSON 读成 `[1.0, 5.805, 202.378, 22050.0]`（version / hop_ms /
#:   duration_sec / sample_rate），界面上看起来还像成功了。
_STRUCT_KEYS = ("stems", "app", "markers", "markerTracks", "tracks", "lanes",
                "bpmPoints", "tempoPoints", "points", "notes", "loop",
                "project", "version", "formatVersion")


def looks_like_ts(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in TS_EXTS


def _from_json(raw: str):
    """JSON：只认「像时间」的键 / 顶层数组里的数。

    返回 `(ts, 说明, 拒绝原因)`：

      · `ts` 非空    ⇒ 认下来了，`说明` 写清用的是哪个键；
      · `ts is None` 且 `why` 为空 ⇒ **不是 JSON**（调用方可以按文本再试）；
      · `ts is None` 且 `why` 非空 ⇒ 是 JSON，但**读不出时间戳** —— `why` 必须说清
        是什么文件（结构化指纹 / 没有可用键），**不许静默退化成垃圾**。
    """
    keys = ("t_ms", "ms", "time_ms", "times_ms", "timestamp", "timestamps",
            "time", "times", "t", "onsets", "beats_ms", "points")
    try:
        obj = json.loads(raw)
    except Exception:                                           # noqa: BLE001
        return None, "", ""                                     # 不是 JSON

    def nums(v):
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return [float(v)]
        if isinstance(v, list):
            out = []
            for x in v:
                if isinstance(x, (int, float)) and not isinstance(x, bool):
                    out.append(float(x))
                elif isinstance(x, dict):
                    for k in keys:
                        if k in x:
                            out.extend(nums(x[k]))
                            break
            return out
        return []

    vals: list = []
    if isinstance(obj, list):
        vals = nums(obj)
    elif isinstance(obj, dict):
        for k in keys:
            if k in obj:
                vals = nums(obj[k])
                break
        if not vals:
            # ★ 兜底（「字典里所有数字」）**收紧**：只在没有结构化指纹键时才算数。
            hits = [k for k in _STRUCT_KEYS if k in obj]
            if hits:
                return None, "", ("这份 JSON 是**结构化文件**（有 {}）——"
                                  "不是「一串时间戳」").format("/".join(hits[:3]))
            for k, v in obj.items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    vals.append(float(v))
    if not vals:
        return None, "", "JSON 里没有可用的时间戳键（认：{}）".format(
            "/".join(keys[:5]))
    return vals, "JSON（键 {}/…）".format("/".join(keys[:5])), ""


def read_ts(path: str) -> tuple[list[float], dict]:
    """读一份时间戳文件 → `([ms…], 报告)`。

    支持：**一行一个毫秒数**（我们见过的那种）；CSV（取**每行最后一列数字**）；
    `mm:ss.xxx` / `hh:mm:ss.xxx`；JSON 数组或 `{"timestamps": [...]}`。

    ★ 行的取舍一律记进报告（`skipped` / `fmts`），不静默。
    ★★ 认得出是 JSON 但**读不出时间戳**时（结构化文件）⇒ **直接拒绝**，
       `report["json_reject"]` 带上原因 —— 绝不退化成「把每行里的数字当时刻」。
    """
    if not path or not os.path.exists(path):
        raise FileNotFoundError(path)
    with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
        raw = fh.read()

    rep = {"path": path, "n_lines": 0, "n_kept": 0, "n_skipped": 0,
           "fmts": {}, "skipped": [], "src": ""}
    ts: list[float] = []

    stripped = raw.lstrip()
    if stripped[:1] in ("[", "{"):
        got, src, why = _from_json(raw)
        if got is not None:
            ts = got
            rep["src"] = src
            rep["fmts"]["json"] = len(ts)
            rep["n_kept"] = len(ts)
            ts = _finish(ts, rep)
            return ts, rep
        if why:
            rep["json_reject"] = why
            rep["src"] = "JSON（已拒绝）"
            rep["n_lines"] = len(raw.splitlines())
            return [], rep

    for ln, line in enumerate(raw.splitlines(), 1):
        s = line.strip()
        rep["n_lines"] += 1
        if not s or s.startswith(_COMMENT):
            rep["n_skipped"] += 1
            continue
        m = _TS_RE.match(s)
        if m:
            h, mm, ss = m.group(1), m.group(2), m.group(3)
            sec = (int(h) * 3600 if h else 0) + int(mm) * 60 + float(ss)
            ts.append(sec * 1000.0)
            rep["fmts"]["clock"] = rep["fmts"].get("clock", 0) + 1
            continue
        clean = _CLEAN_RE.sub(" ", s)
        nums = _NUM_RE.findall(clean)
        if not nums:
            rep["n_skipped"] += 1
            if len(rep["skipped"]) < 8:
                rep["skipped"].append({"line": ln, "text": s[:40]})
            continue
        # ★ 取**最后一个**数：常见的导出是 `序号,毫秒` / `标签 毫秒`
        ts.append(float(nums[-1]))
        rep["fmts"]["num"] = rep["fmts"].get("num", 0) + 1
    ts = _finish(ts, rep)
    rep["src"] = "文本（每行取最后一个数）"
    return ts, rep


def _finish(ts: list[float], rep: dict) -> list[float]:
    """排序 / 去重（同刻合并并计数）/ 丢负数与 NaN。

    ★★ 2026-10 修了一个**真 bug**：原来先 `sorted()` 再丢 NaN —— 而含 NaN 的序列
    **没有全序**（NaN 的任何比较都是 False），Timsort 会给出「局部有序」的乱序结果：
    实测 `[1000, 1000, -500, NaN, NaN, 2000, NaN, 3000]` 出来是
    `[2000.0, 3000.0, 1000.0]` —— 时间戳**没排序**，下游「去重合并在后一个点上」
    会整片错位（时间戳 JSON 的清洗正好会喂进 NaN）。
    ⇒ 现在**先过滤 NaN/越界，再排序**。
    """
    out: list[float] = []
    n_dup = n_bad = 0
    clean: list[float] = []
    for x in ts:
        t = float(x)
        if not (t == t) or t < 0 or t > 6 * 3600 * 1000:           # NaN / 越界
            n_bad += 1
            continue
        clean.append(t)
    for t in sorted(clean):
        if out and abs(t - out[-1]) < 1e-9:
            n_dup += 1
            continue
        out.append(t)
    rep["n_kept"] = len(out)
    rep["n_dup"] = n_dup
    rep["n_bad"] = n_bad
    if out:
        rep["ms_lo"] = round(out[0], 3)
        rep["ms_hi"] = round(out[-1], 3)
    return out


def clean_points(ts, rep: dict | None = None) -> list[float]:
    """`_finish` 的**公开入口** —— 别的来源（如 `core.stem_json`）也要用
    **同一套**清洗规则（排序 / 同刻合并 / 丢 NaN 与越界），不许各写一份。
    """
    return _finish([float(x) for x in ts], rep if rep is not None else {})


def grid_info(ts, *, hint: float | None = None, div: int | None = None,
              offset_ms: float | None = None) -> dict:
    """这份时间戳的**网格规划**（= 该给 BDG 的 `baseBpm`/`offsetMs`/分母）。

    它就是 `core.denoise.plan()` 的薄封装 —— 与去噪、直拟合、桥接锚**同一个真源**。
    """
    g = dn_mod.plan(ts, hint=hint, div=div, offset_ms=offset_ms)
    d = g.to_dict()
    d["ok"] = bool(g.ok)
    d["reason"] = g.reason
    return d


def to_midi_like(ts, bpm: float = 0.0, *, ppqn: int = PPQN, name: str = "",
                 hint: float | None = None, div: int | None = None) -> MidiFile:
    """`[ms…]` → `MidiFile`（**一条轨**，每个时间戳一个 Note）。

    `bpm=0` ⇒ 用 `core.denoise` 估出来的砖长（算不出来就退回 120）。
    """
    t = [float(x) for x in ts]
    if len(t) < 2:
        raise ValueError("时间戳少于 2 个，成不了谱")
    g = dn_mod.plan(t, hint=hint, div=div)
    base = float(bpm or (g.bpm if g.ok else 0.0) or 120.0)
    beat_ms = 60_000.0 / base
    trk = Track(index=0, name=name or "时间戳")
    for x in t:
        tk = int(round(x * ppqn * base / 60_000.0))
        trk.notes.append(Note(track=0, channel=DEF_CHANNEL, pitch=DEF_PITCH,
                              velocity=DEF_VELOCITY, t_on_ms=x, t_off_ms=x,
                              t_on_tick=tk, t_off_tick=tk))
    mf = MidiFile(path="", format=0, ppqn=ppqn, tracks=[trk],
                  tempo_map=[(0, base)], time_sig=[(0, 4, 4)])
    mf.length_ms = t[-1] + TAIL_BEATS * beat_ms
    # ★ 这三个「非标准字段」是给 `sidecar.session.load()` 的接口（与 MIDI 源同一套 getattr）
    mf.is_ts = True                     # type: ignore[attr-defined]
    mf.grid_fit = g                    # type: ignore[attr-defined]
    mf.ts_plan = g.to_dict()           # type: ignore[attr-defined]
    mf.ts_meta = {"n": len(t), "bpm": base, "beat_ms": beat_ms,
                  "ms_lo": t[0], "ms_hi": t[-1]}   # type: ignore[attr-defined]
    return mf


def report_text(rep: dict, plan: dict | None = None) -> str:
    """人话报告（上屏用）。"""
    out = ["[时间戳来源] {} 个点（{:.1f}~{:.1f}ms）· {} 行 · 跳过 {} 行 · 同刻合并 {}".format(
        rep.get("n_kept", 0), rep.get("ms_lo", 0.0), rep.get("ms_hi", 0.0),
        rep.get("n_lines", 0), rep.get("n_skipped", 0), rep.get("n_dup", 0))]
    if rep.get("src"):
        out.append("    解析：{}".format(rep["src"]))
    if rep.get("json_reject"):
        out.append("    ★ 拒绝：{}".format(rep["json_reject"]))
    if plan:
        if plan.get("ok"):
            out.append("    网格：砖长 {:.4f}ms（bpm {:.3f}）· 相位 {:+.3f}ms · "
                       "分母 1/{} · 格 {:.4f}ms".format(
                           plan.get("period_ms", 0.0), plan.get("bpm", 0.0),
                           plan.get("phase_ms", 0.0), plan.get("div", 0),
                           plan.get("step_ms", 0.0)))
        else:
            out.append("    ★ 网格没通过：{}（去噪会跳过，直拟合仍可用）".format(
                plan.get("reason") or "?"))
    for s in (rep.get("skipped") or [])[:3]:
        out.append("    跳过第 {} 行：{}".format(s["line"], s["text"]))
    return "\n".join(out)
