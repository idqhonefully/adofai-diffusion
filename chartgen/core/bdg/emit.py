# -*- coding: utf-8 -*-
"""写回（passthrough，`docs/36` §7）。

三条硬规矩：

1. **原文全留** —— `deepcopy(raw)` 起手，只替换 `tracks` / `markers` / 变速点
   这三个子树（用**解析时命中的键名**，绝不硬写 `"tracks"`）；
   `notes` / 颜色 / `locked` / 未知顶层键 —— 一个都不动。
2. **补出来的循环子点不写回**（`synth=True`）—— 上游自己会重建子点，
   写回去只会制造重复。
3. **兜底模式（generic / broken）禁止写回**，只读，并明说理由
   （宁可不写，也不要写出一份让上游读不懂的工程）。

本文件不得出现任何字段名字面量。
"""

from __future__ import annotations

import copy
import json

from . import aliases as al

INDENT = 2


def can_emit(rep) -> tuple:
    """`(能不能写回, 不能的理由)`。"""
    if rep is None:
        return True, ""
    if rep.parser == al.PARSER_GENERIC:
        return False, "generic 兜底解出来的工程不完整，写回会破坏它"
    if rep.parser == al.PARSER_BROKEN:
        return False, "原始数据本身就读不通（坏 JSON / 不是工程）"
    if rep.parser == al.PARSER_SNAPSHOT:
        return False, "插件快照（有 timeMs、无版本号）不是工程文件，写回请走 .bdg / import 消息"
    if not rep.key_markers and not rep.key_tracks:
        return False, "解析时没有命中任何已知键名，无从写回"
    return True, ""


def build(project, rep=None) -> dict | None:
    """规范化模型 → raw dict（**未知字段原样保留**）。不能写回 ⇒ `None`。"""
    rep = rep or project.rep
    ok, why = can_emit(rep)
    if not ok:
        return None

    out = copy.deepcopy(project.raw) if project.raw else {}
    target = out
    for k in (rep.nested_at or ()):
        nxt = target.get(k)
        if not isinstance(nxt, dict):
            nxt = {}
            target[k] = nxt
        target = nxt

    if rep.key_tracks:
        target[rep.key_tracks] = [copy.deepcopy(t.raw) for t in project.tracks]
    if rep.key_markers:
        target[rep.key_markers] = [copy.deepcopy(p.raw) for p in project.points
                                   if not p.synth]
    if rep.key_bpm:
        target[rep.key_bpm] = [copy.deepcopy(b.raw) for b in project.bpm_points]
    return out


def dumps(obj, indent: int = INDENT) -> str:
    """★ 与宿主的 `JSON.stringify(p, null, 2)` **逐字节一致**（实测已验证）。"""
    return json.dumps(obj, ensure_ascii=False, indent=indent)


def dump_file(obj, path: str) -> None:
    with open(path, "wb") as f:
        f.write(dumps(obj).encode("utf-8"))
