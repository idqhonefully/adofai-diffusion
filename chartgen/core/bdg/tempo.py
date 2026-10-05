# -*- coding: utf-8 -*-
"""宿主 tempo map 的**只读**对等实现（`docs/35` §3.4）。

对齐 `beat_data_generator/src/renderer/src/tempo.ts` 的 `buildTempoMap`：

  · 只取 beat > 0 的变速点，按 beat 排序；
  · 分段线性：段内 `ms/beat = 60000 / bpm`；
  · mode `abs` → 该点起**绝对** BPM；mode `mult` → **当前 BPM × value**；
  · BPM 夹在 [20, 999]；
  · `timeOfBeat` / `beatOfTime` 段内互逆。

★★ 铁律：**这张表只读**。改 `baseBpm` / `offsetMs` / `bpmPoints`
   = 把上游工程里所有 marker 的 `timeMs` 集体平移、与音频脱钩（`docs/36` §11.4）。

★ 关于 `offsetMs` 的锚点（`time_of_beat(0)` 是 0 还是 `offsetMs`）：
  实测样本无法反证（两种都落在合理时长内），所以 `parse` 会把它记进
  `rep.guess("time_anchor")`，**看得见**。
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from . import aliases as al

BPM_MIN = 20.0
BPM_MAX = 999.0

_BEAT_MS = 60000.0


def _clamp_bpm(x: float) -> float:
    if x < BPM_MIN:
        return BPM_MIN
    if x > BPM_MAX:
        return BPM_MAX
    return float(x)


@dataclass
class Segment:
    beat_start: float = 0.0
    beat_end: float = 0.0
    bpm: float = 0.0
    time_start_ms: float = 0.0
    time_end_ms: float = 0.0


@dataclass
class TempoMap:
    base_bpm: float = 0.0
    offset_ms: float = 0.0
    include_offset: bool = True
    segments: list = field(default_factory=list)
    _starts: list = field(default_factory=list)     # 段起点拍（bisect 用）
    ignored: int = 0                                # 被跳过的非法变速点

    # ------------------------------------------------------------ 查询
    def _seg_at_beat(self, beat: float) -> Segment:
        if not self.segments:
            return Segment(0.0, 0.0, _clamp_bpm(self.base_bpm or BPM_MIN), 0.0, 0.0)
        i = bisect.bisect_right(self._starts, beat) - 1
        if i < 0:
            i = 0
        return self.segments[i]

    def bpm_at_beat(self, beat: float) -> float:
        return self._seg_at_beat(beat).bpm

    def raw_time_of_beat(self, beat: float) -> float:
        """不含 offset 的纯积分时间。"""
        s = self._seg_at_beat(beat)
        return s.time_start_ms + (beat - s.beat_start) * (_BEAT_MS / s.bpm)

    def time_of_beat(self, beat: float) -> float:
        t = self.raw_time_of_beat(beat)
        return t + (self.offset_ms if self.include_offset else 0.0)

    def beat_of_time(self, ms: float) -> float:
        t = ms - (self.offset_ms if self.include_offset else 0.0)
        if not self.segments:
            return t / (_BEAT_MS / _clamp_bpm(self.base_bpm or BPM_MIN))
        # 找段：最后一段是开放的
        i = 0
        for j, s in enumerate(self.segments):
            if t >= s.time_start_ms:
                i = j
        s = self.segments[i]
        return s.beat_start + (t - s.time_start_ms) / (_BEAT_MS / s.bpm)

    def bpm_at_time(self, ms: float) -> float:
        return self.bpm_at_beat(self.beat_of_time(ms))

    def duration_ms(self) -> float:
        if not self.segments:
            return 0.0
        return self.segments[-1].time_end_ms - self.segments[-1].time_start_ms


def build(base_bpm: float, offset_ms: float, bpm_points, include_offset: bool = True,
          rep=None) -> TempoMap:
    """用宿主的三个锚建表。`bpm_points` 是 `model.BpmPoint` 或 `(beat,mode,value)`。"""
    tm = TempoMap(base_bpm=float(base_bpm or 0.0), offset_ms=float(offset_ms or 0.0),
                  include_offset=include_offset)

    edges = [(0.0, _clamp_bpm(base_bpm or BPM_MIN))]
    cur = edges[0][1]

    norm = []
    for bp in bpm_points or []:
        if isinstance(bp, (tuple, list)):
            beat, mode, value = bp[0], bp[1], bp[2]
        else:
            beat, mode, value = bp.beat, bp.mode, bp.value
        norm.append((float(beat), str(mode or ""), float(value or 0.0)))
    norm.sort(key=lambda x: x[0])

    for beat, mode, value in norm:
        if beat <= 0.0 or beat <= edges[-1][0]:
            tm.ignored += 1               # 上游也是「只取 beat > 0」
            continue
        if mode == al.MODE_ABS:
            nxt = _clamp_bpm(value)
        else:
            nxt = _clamp_bpm(cur * (value if value else 1.0))
        edges.append((beat, nxt))
        cur = nxt

    # 累计时间
    t = 0.0
    segs = []
    for i, (bs, bpm) in enumerate(edges):
        nxt_beat = edges[i + 1][0] if i + 1 < len(edges) else None
        if nxt_beat is None:
            nxt_beat = bs                      # 末段开放，长度 0 占位
        span = (nxt_beat - bs) * (_BEAT_MS / bpm)
        segs.append(Segment(bs, nxt_beat, bpm, t, t + span))
        t += span
    tm.segments = segs
    tm._starts = [s.beat_start for s in segs]

    if rep is not None and tm.ignored:
        rep.warn(f"tempo: 跳过 {tm.ignored} 个非法/重复变速点（宿主同样只取 beat>0）")
    return tm
