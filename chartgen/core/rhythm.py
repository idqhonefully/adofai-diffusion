"""节奏层：从 MIDI 里取出「它是几分音符」，并量化到音值网格。

=============== 「MIDI 里含它是几分音吗？」===============
**不含。** 但**可以精确算出来**，而且比从毫秒反推更准：

  MIDI 存的是 tick（delta time）+ 文件头的 division。
  division 的值就是 PPQ（每四分音符多少 tick，样本里都是 480）。
  于是：  **拍数 = Δtick / PPQ**     ← 与 tempo 无关，精确

  拍数 → 音值：1 拍 = 四分音符，0.5 = 八分，0.25 = 十六分，
                1/3 ≈ 三连八分（1/12 音符），1.5 = 附点四分 …
  这是**记谱概念**，MIDI 不直接存，但由 tick 完全确定。

v1 我只用了 t_ms（毫秒）来算角度，毫秒里混进了 tempo 和浮点误差，
而且没有任何"这是几分音"的概念 —— 现在补上。
"""
from __future__ import annotations

import collections
from dataclasses import dataclass

#: 允许的音值词汇表（单位：四分音符拍数）
NOTE_VALUES: tuple[float, ...] = (
    1 / 32, 1 / 24, 1 / 16, 1 / 12, 1 / 8, 1 / 6, 3 / 16,
    1 / 4, 1 / 3, 3 / 8, 1 / 2, 2 / 3, 3 / 4,
    1.0, 4 / 3, 3 / 2, 2.0, 8 / 3, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0,
)

#: 音值 → 人话
_NAMES = {
    1 / 32: "1/128", 1 / 24: "1/96", 1 / 16: "1/64", 1 / 12: "1/48三连",
    1 / 8: "1/32", 1 / 6: "1/24三连", 3 / 16: "1/32附点",
    1 / 4: "1/16", 1 / 3: "1/12三连", 3 / 8: "1/16附点",
    1 / 2: "1/8", 2 / 3: "1/6三连", 3 / 4: "1/8附点",
    1.0: "1/4", 4 / 3: "1/6三连", 3 / 2: "1/4附点",
    2.0: "1/2", 8 / 3: "1/2三连", 3.0: "附点二分", 4.0: "全音符",
    6.0: "附点全", 8.0: "2全", 12.0: "3全", 16.0: "4全",
}


def name_of(beats: float) -> str:
    q = quantize(beats)
    base = _NAMES.get(round(q, 6), "")
    if not base:
        # 用 1/4 音符的分数表示（社区习惯）
        base = f"{beats:g}拍"
    return f"{base}({q:g}拍)"


def quantize(beats: float, vocab: tuple[float, ...] = NOTE_VALUES) -> float:
    """把拍数吸附到最近的音值。"""
    best = vocab[0]
    bd = abs(beats - best)
    for v in vocab[1:]:
        d = abs(beats - v)
        if d < bd:
            bd, best = d, v
    return best


@dataclass
class RhythmStats:
    beats: list[float]            # 每个间隔的原始拍数
    quant: list[float]            # 量化后的拍数
    ppqn: int
    mode_beats: float             # 出现最多的音值（用来定基准 BPM）
    mode_count: int
    err_beats: list[float]        # 量化误差（拍）

    def hist(self, top: int = 8) -> list[tuple[str, float, int, float]]:
        c = collections.Counter(round(v, 6) for v in self.quant)
        return [(name_of(v), v, n, n / max(1, len(self.quant)) * 100)
                for v, n in c.most_common(top)]

    def max_err_ms(self, beat_ms: float) -> float:
        return max((abs(e) for e in self.err_beats), default=0.0) * beat_ms


def extract(onsets, ppqn: int, fallback_beat_ms: float = 500.0) -> RhythmStats:
    """从 onset 序列取「几分音符」。

    优先用 tick（精确、与 tempo 无关）；tick 缺失时退回毫秒折算。
    """
    n = len(onsets)
    beats: list[float] = []
    for i in range(n - 1):
        a, b = onsets[i], onsets[i + 1]
        da = getattr(a, "tick", 0)
        db = getattr(b, "tick", 0)
        if db > da and ppqn > 0:
            beats.append((db - da) / ppqn)
        else:
            beats.append((b.t_ms - a.t_ms) / max(1e-9, fallback_beat_ms))
    quant = [quantize(b) for b in beats]
    err = [q - b for q, b in zip(quant, beats)]
    c = collections.Counter(round(v, 6) for v in quant)
    mode_v, mode_n = c.most_common(1)[0] if c else (1.0, 0)
    return RhythmStats(beats=beats, quant=quant, ppqn=ppqn,
                       mode_beats=mode_v, mode_count=mode_n, err_beats=err)


def base_bpm_for(midi_bpm: float, mode_beats: float, target_travel: float) -> float:
    """把「最常出现的音值」映射到 target_travel 所需的基准 BPM。

    推导：travel = Δt × bpm / (1000/3)，而 Δt = mode_beats × (60000/midi_bpm)
          => bpm = target_travel × 180 × mode_beats / midi_bpm … 反解：
    """
    if mode_beats <= 1e-9 or midi_bpm <= 0:
        return midi_bpm
    return midi_bpm * target_travel / (180.0 * mode_beats)
