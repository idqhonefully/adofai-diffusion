# -*- coding: utf-8 -*-
"""规范化中间模型（`docs/36` §9）。

**下游只认这里，永不认 `.bdg` 形状。**

每个对象都揣着自己的**原文子集**（`raw`）⇒ `emit` 只替换被改动的子树，
未知字段/未知轨道原样回去（铁律 4）。

本文件不得出现任何字段名字面量（dataclass 的字段名是标识符，不是字符串）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LoopSpec:
    """一个主点的循环展开规格（`docs/36` §11.2 语义已实测确认）。"""
    interval: float = 1.0          # 单位 = 拍
    count: int = 0                 # 不含第 0 次 ⇒ 共 count+1 个点
    exclude: tuple = ()            # ★ 存的就是 k 本身（k ∈ [1, count]）
    raw: dict = field(default_factory=dict)

    def ks(self):
        """要生成的 k 序列：1..count 去掉 exclude。"""
        ex = set(self.exclude)
        return [k for k in range(1, self.count + 1) if k not in ex]


@dataclass
class Point:
    """一个拍点。"""
    id: str = ""
    track_id: str = ""
    beat: float = 0.0
    time_ms: float = 0.0
    attrs: dict = field(default_factory=dict)
    loop: LoopSpec | None = None
    parent_id: str | None = None
    idx: int = 0                   # 在源数组里的下标（保序/溯源用）
    src_time: bool = False         # 原文是否自带时间戳（而非我们换算的）
    synth: bool = False            # ★ 由 expand 补出来的（原文里没有）
    raw: dict = field(default_factory=dict)

    @property
    def is_loop_child(self) -> bool:
        return self.parent_id is not None

    @property
    def is_loop_parent(self) -> bool:
        return self.loop is not None


@dataclass
class Track:
    id: str = ""
    name: str = ""
    type: str = ""                 # ★ 角色载体
    role: str = ""
    hidden: bool = False
    locked: bool = False
    color: str = ""
    idx: int = 0
    raw: dict = field(default_factory=dict)


@dataclass
class BpmPoint:
    beat: float = 0.0
    mode: str = ""
    value: float = 0.0
    idx: int = 0
    raw: dict = field(default_factory=dict)


@dataclass
class BpmEvent:
    """类型化 BPM 轨上的变速点（★ 真正的变速在这里，`docs/36` §11.4）。"""
    beat: float = 0.0
    speed_type: str = ""
    value: float = 0.0
    track_id: str = ""
    point_id: str = ""


@dataclass
class Project:
    app: str = ""
    fmt_version: int = 0
    name: str = ""
    base_bpm: float = 0.0
    offset_ms: float = 0.0
    audio_name: str = ""
    audio_md5: str = ""
    bpm_locked: bool = False
    tracks: list = field(default_factory=list)
    points: list = field(default_factory=list)     # ★ 源顺序（passthrough 要）
    bpm_points: list = field(default_factory=list)
    bpm_events: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    tempo: object = None                            # TempoMap
    rep: object = None
    raw: dict = field(default_factory=dict)

    # ------------------------------------------------------------ 便捷
    def track_of(self, point) -> Track | None:
        for t in self.tracks:
            if t.id == point.track_id:
                return t
        return None

    def role_of(self, point) -> str:
        t = self.track_of(point)
        return t.role if t else ""

    def points_of(self, track_id: str):
        return [p for p in self.points if p.track_id == track_id]

    def sorted_points(self):
        """按拍排序（★ 不依赖文件顺序，`docs/36` §11.5）。"""
        return sorted(self.points, key=lambda p: (p.beat, p.idx))

    def by_role(self, role: str):
        """只取某个角色的点（角色值从 `aliases` 传进来，本层不认识字面量）。"""
        return [p for p in self.points if self.role_of(p) == role]

    def dedup(self):
        """按 (beat, track_id) 去重，返回被吃掉的个数。

        ⚠ 只在**下游 onset 判定**时用（同拍多点 = 双押/和弦，是常态），
          绝不在解析/写回里用（实测 232/866 是重复 beat）。
        """
        seen = set()
        kept = []
        for p in self.points:
            sig = (round(p.beat, 9), p.track_id)
            if sig in seen:
                continue
            seen.add(sig)
            kept.append(p)
        return kept, len(self.points) - len(kept)
