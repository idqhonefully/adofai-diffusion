# -*- coding: utf-8 -*-
"""解析报告（`docs/36` §1 铁律 5：**一切假设都要能看见**）。

它要回答四个问题：

  1. **命中了哪些别名** —— `hits`：概念 → 实际命中的候选路径；
  2. **哪里用了默认** —— `defaults`：概念 / 用了什么 / 原值缺成什么样；
  3. **丢了什么**     —— `dropped`：跳过的点、丢弃的轨、无法理解的东西；
  4. **哪里是猜的**   —— `guesses`：兜底解析、多包一层、时间锚点假设。

外加 `warns` / `errs` 两条流水。**不许静默**是本项目一贯口径
（双押 `lost` 原因、Δ 预算超支，现在是这里）。
"""

from __future__ import annotations


class ParseReport:
    """一次解析的全部「看得见」信息。"""

    def __init__(self, source: str = ""):
        self.source = source
        self.ok = True
        self.parser = ""
        self.fmt_version = 0
        self.nested_at = ()          # 多包一层时的路径（元组，如 ("project",)）
        # 实际命中的**上游键名**，写回时照抄（绝不硬写 "tracks"）
        self.key_tracks = None
        self.key_markers = None
        self.key_bpm = None
        self.key_notes = None
        self.hits: dict = {}
        self.defaults: list = []
        self._seen_defaults = set()
        self.dropped: list = []
        self.guesses: list = []
        self.warns: list = []
        self.errs: list = []
        self.stats: dict = {}

    # ------------------------------------------------------------ 记录
    def hit(self, concept: str, where: str) -> None:
        if concept and where:
            self.hits[concept] = where

    def default(self, concept: str, used, note: str = "") -> None:
        # 同一个概念反复用同一个默认值（如 300 个普通点都没有 attrs）只记一次
        sig = (concept, repr(used), note)
        if sig in self._seen_defaults:
            return
        self._seen_defaults.add(sig)
        self.defaults.append({"concept": concept, "used": used, "note": note})

    def drop(self, what: str, detail=None) -> None:
        self.dropped.append({"what": what, "detail": detail})

    def guess(self, what: str, detail=None) -> None:
        self.guesses.append({"what": what, "detail": detail})

    def warn(self, msg: str) -> None:
        self.warns.append(msg)

    def error(self, msg: str) -> None:
        self.ok = False
        self.errs.append(msg)

    def count(self, **kw) -> None:
        self.stats.update(kw)

    # ------------------------------------------------------------ 输出
    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "src": self.source,
            "parser": self.parser,
            "fmt": self.fmt_version,
            "nested_at": list(self.nested_at),
            "hits": dict(self.hits),
            "defaults": list(self.defaults),
            "dropped": list(self.dropped),
            "guesses": list(self.guesses),
            "warns": list(self.warns),
            "errs": list(self.errs),
            "stats": dict(self.stats),
        }

    def summary(self) -> str:
        """一行给状态栏用。"""
        bits = [f"parser={self.parser or '?'}"]
        if self.fmt_version:
            bits.append(f"fmt={self.fmt_version}")
        bits.append(f"别名命中={len(self.hits)}")
        if self.defaults:
            bits.append(f"默认={len(self.defaults)}")
        if self.dropped:
            bits.append(f"丢弃={len(self.dropped)}")
        if self.guesses:
            bits.append(f"猜测={len(self.guesses)}")
        if self.warns:
            bits.append(f"警告={len(self.warns)}")
        if self.errs:
            bits.append(f"错误={len(self.errs)}")
        return " · ".join(bits)

    def pretty(self) -> str:
        """多行，给 `tools/_bdg_probe.py` 用。"""
        out = [f"[解析报告] {self.summary()}"]
        if self.hits:
            out.append("  命中别名:")
            for k in sorted(self.hits):
                out.append(f"    {k:16s} ← {self.hits[k]}")
        for tag, rows in (("用了默认", self.defaults),
                          ("丢弃", self.dropped),
                          ("猜测", self.guesses)):
            for r in rows:
                out.append(f"  [{tag}] {r}")
        for w in self.warns:
            out.append(f"  [警告] {w}")
        for e in self.errs:
            out.append(f"  [错误] {e}")
        if self.stats:
            out.append("  计数: " + " ".join(f"{k}={v}" for k, v in self.stats.items()))
        return "\n".join(out)
