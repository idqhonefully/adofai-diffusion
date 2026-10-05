# -*- coding: utf-8 -*-
"""容错取值器（`docs/36` §4）。

**全部不抛异常**：失败 → 返回默认值 **并记进 report**
（哪条概念、用的默认是什么、原值长什么样）。
外部数据用异常控制流是反模式。

本文件不得出现任何字段名字面量 —— 路径一律由调用方从 `aliases` 传进来。
"""

from __future__ import annotations

from . import aliases as al


class _Missing:
    __slots__ = ()

    def __repr__(self):    # pragma: no cover
        return "<MISSING>"

    def __bool__(self):
        return False


MISSING = _Missing()


# ---------------------------------------------------------------- 取路径
def _pluck(obj, path):
    """按点号路径取值；任一层缺失 → MISSING。"""
    cur = obj
    for seg in path.split("."):
        if isinstance(cur, dict):
            if seg not in cur:
                return MISSING
            cur = cur[seg]
        elif isinstance(cur, (list, tuple)):
            try:
                cur = cur[int(seg)]
            except (ValueError, IndexError):
                return MISSING
        else:
            return MISSING
    return cur


def locate(obj, paths, default=MISSING):
    """按候选路径取值 ⇒ `(值, 命中的路径)`；都没命中 ⇒ `(default, None)`。"""
    for p in paths:
        v = _pluck(obj, p)
        if v is not MISSING:
            return v, p
    return default, None


# `pick` 是 `docs/36` §4 里定的名字。
pick = locate


def take(obj, paths, default, rep=None, concept=""):
    """取值 + **记报告**。命中 ⇒ `rep.hit`；没命中 ⇒ `rep.default`。

    这是 `parse.py` 里唯一该用的取值入口。
    """
    v, where = locate(obj, paths, MISSING)
    if where is None:
        if rep is not None and concept:
            rep.default(concept, default)
        return default
    if rep is not None and concept:
        rep.hit(concept, where)
    return v


# ---------------------------------------------------------------- 形状归一
def one_or_many(x):
    """单对象 → [x]。上游常在版本间把「一个」和「多个」改来改去。"""
    if x is None:
        return []
    if isinstance(x, (list, tuple)):
        return list(x)
    return [x]


def is_map(x):
    return isinstance(x, dict)


# ---------------------------------------------------------------- 标量
def as_float(x, default=0.0) -> float:
    if isinstance(x, bool):
        return float(x)
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        s = x.strip()
        if not s:
            return default
        try:
            return float(s)
        except ValueError:
            return default
    if isinstance(x, dict):
        for v in x.values():          # {ms: 123} 之类
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
        return default
    return default


def as_int(x, default=0) -> int:
    f = as_float(x, None) if not isinstance(x, (int, float)) else x
    if f is None:
        return default
    try:
        return int(f)
    except (TypeError, ValueError):
        return default


def as_str(x, default="") -> str:
    if x is None:
        return default
    if isinstance(x, str):
        return x
    if isinstance(x, (int, float, bool)):
        return str(x)
    return default


def as_bool(x, default=False) -> bool:
    if isinstance(x, bool):
        return x
    if isinstance(x, (int, float)):
        return bool(x)
    if isinstance(x, str):
        s = x.strip().lower()
        if s in al.BOOL_TRUE_WORDS:
            return True
        if s in al.BOOL_FALSE_WORDS:
            return False
    return default


def as_beat(x, default=0.0) -> float:
    """数字直接用；也认 "17.25" / "17:2"（小节:拍）/ {bar,beat}。

    ★ 绝不 `round`（`docs/36` §11.3：实测 beat 是「用平铺 map 编码的毫秒」，
      866/866 都不在吸附网格上）。
    """
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return float(x)
    if isinstance(x, str):
        s = x.strip()
        if ":" in s:
            a, _, b = s.partition(":")
            return as_float(a, 0.0) * al.BAR_BEATS + as_float(b, 0.0)
        return as_float(s, default)
    if isinstance(x, dict):
        bar = as_float(x.get(al.SUB_BAR, 0.0), 0.0)
        beat = as_float(x.get(al.SUB_BEAT, 0.0), 0.0)
        if bar or beat:
            return bar * al.BAR_BEATS + beat
    return default


def as_time_ms(x, default=0.0) -> float:
    """数字；或 {ms:…}；或 "00:01.234" / "1.234s"。"""
    if isinstance(x, (int, float)) and not isinstance(x, bool):
        return float(x)
    if isinstance(x, dict):
        for k, v in x.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return float(v)
        return default
    if isinstance(x, str):
        s = x.strip()
        if s.endswith(al.UNIT_S) and not s.endswith(al.UNIT_MS):
            return as_float(s[:-1], default) * 1000.0
        if ":" in s:
            parts = s.split(":")
            tot = 0.0
            for p in parts:
                tot = tot * 60.0 + as_float(p, 0.0)
            return tot * 1000.0
        return as_float(s, default)
    return default
