# -*- coding: utf-8 -*-
"""`core.bdg` —— BDG（`BUGJI/beat_data_generator`）工程 ⇄ 我们的规范化模型。

对外只暴露四件事：

    parse(raw) / load_file(path)   → (Project, ParseReport)
    emit.build(project)            → raw dict（passthrough）或 None
    ParseReport                    → 命中的别名 / 默认 / 丢弃 / 猜测
    probe.fingerprint(raw)         → 字段形状指纹（收 fixture 时先跑）

设计见 `docs/36`（软解析）/ `docs/35`（桥）。

★ 我们依赖的是「**概念**」（拍 / 轨 / 环 / 变速 / 属性），而不是「字段」。
  概念的名字可以换（改 `aliases.py` 一张表），概念本身不能。
"""

from __future__ import annotations

from . import (aliases, coerce, emit, expand, model, probe, roundtrip, snap,
               source, tempo)
from .emit import build, dump_file, dumps
from .model import BpmEvent, BpmPoint, LoopSpec, Point, Project, Track
from .parse import load_file, load_text, parse
from .report import ParseReport

__all__ = [
    "aliases", "coerce", "emit", "expand", "model", "probe", "roundtrip",
    "snap", "source", "tempo",
    "parse", "load_text", "load_file", "build", "dumps", "dump_file",
    "ParseReport", "Project", "Track", "Point", "LoopSpec",
    "BpmPoint", "BpmEvent",
]
