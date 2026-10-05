# -*- coding: utf-8 -*-
"""能力探测（caps）+「字段形状指纹」（`docs/36` §5 / §8）。

**走哪条路以 `caps` 为准，`version` 只写进报告。**

`tools/_bdg_probe.py <file.bdg>` 打印指纹 —— **收新 fixture 时先跑它**。
"""

from __future__ import annotations

from . import aliases as al
from . import coerce
from .coerce import MISSING


def _lst(obj, paths):
    v, where = coerce.locate(obj, paths, MISSING)
    if where is None:
        return [], None
    return coerce.one_or_many(v), where


def caps(raw) -> dict:
    """能力探测：决定走哪条解析路径。"""
    if not isinstance(raw, dict):
        return {"n_tracks": 0, al.K_N_POINTS: 0, "n_bpm": 0, "n_notes": 0,
                "has_loop": False, "has_typed": False, "has_attrs": False,
                "has_time": False, "has_shape": False}
    tracks, _ = _lst(raw, al.ROOT_TRACKS)
    points, _ = _lst(raw, al.ROOT_MARKERS)
    bpm, _ = _lst(raw, al.ROOT_BPM_POINTS)
    notes, _ = _lst(raw, al.ROOT_NOTES)
    return {
        "n_tracks": len(tracks),
        al.K_N_POINTS: len(points),
        "n_bpm": len(bpm),
        "n_notes": len(notes),
        "has_typed": any(coerce.locate(t, al.TRACK_TYPE, MISSING)[1] for t in tracks
                         if isinstance(t, dict)),
        "has_loop": any(coerce.locate(m, al.MARK_LOOP, MISSING)[1] for m in points
                        if isinstance(m, dict)),
        "has_attrs": any(coerce.locate(m, al.MARK_ATTRS, MISSING)[1] for m in points
                         if isinstance(m, dict)),
        "has_time": any(coerce.locate(m, al.MARK_TIME, MISSING)[1] for m in points
                        if isinstance(m, dict)),
        "has_shape": bool(coerce.locate(raw, al.ROOT_MARKERS, MISSING)[1]
                          or coerce.locate(raw, al.ROOT_TRACKS, MISSING)[1]),
    }


def _kind(v) -> str:
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list[{}]".format(len(v))
    if isinstance(v, dict):
        return "dict"
    if v is None:
        return "null"
    return type(v).__name__


def _shape_rows(seq, top=8):
    """一个列表里「元素键组合」的分布。"""
    from collections import Counter
    rows = Counter()
    for it in seq:
        if isinstance(it, dict):
            rows[tuple(sorted(it.keys()))] += 1
        else:
            rows[("«" + _kind(it) + "»",)] += 1
    out = []
    for keys, cnt in rows.most_common(top):
        out.append({"keys": list(keys), "cnt": cnt})
    return out, len(rows)


def fingerprint(raw) -> dict:
    """字段形状指纹：顶层键 / 每个列表的元素键集合 / 值样例。"""
    if not isinstance(raw, dict):
        return {"top": {}, "shapes": {}, "samples": {}}
    top = {k: _kind(v) for k, v in raw.items()}
    shapes = {}
    samples = {}
    for key, v in raw.items():
        if isinstance(v, list) and v:
            rows, distinct = _shape_rows(v)
            shapes[key] = {"distinct": distinct, "rows": rows}
            picked = []
            seen = set()
            for it in v:
                if not isinstance(it, dict):
                    continue
                sig = tuple(sorted(it.keys()))
                if sig in seen:
                    continue
                seen.add(sig)
                picked.append(it)
                if len(picked) >= 6:
                    break
            samples[key] = picked
    return {"top": top, "shapes": shapes, "samples": samples, "caps": caps(raw)}


def report_text(raw) -> str:
    fp = fingerprint(raw)
    out = []
    out.append("[顶层] " + "  ".join("{}:{}".format(k, v) for k, v in fp["top"].items()))
    for key, sh in fp["shapes"].items():
        out.append("[形状] {} —— 元素键组合 {} 种".format(key, sh["distinct"]))
        for r in sh["rows"]:
            out.append("        {:>5} × {}".format(r["cnt"], "{" + ", ".join(r["keys"]) + "}"))
    for key, ss in fp["samples"].items():
        out.append("[样例] " + key)
        for s in ss:
            txt = repr(s)
            out.append("        " + (txt if len(txt) < 220 else txt[:217] + "…"))
    out.append("[能力] " + "  ".join("{}={}".format(k, v) for k, v in fp["caps"].items()))
    return "\n".join(out)
