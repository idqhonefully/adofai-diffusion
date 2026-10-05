"""预览音频：把 MIDI（选定轨）渲染成 WAV，并在每个 onset 上叠一个「采音咔哒」。

没有现成的 `.sf2` 音源（本项目不自带任何音源素材），所以用 numpy 简易合成音色：
  钢琴轨 -> 正弦+少量三次谐波 + 快起缓落的 ADSR
  低音轨 -> 正弦 + 稍长的衰减
鼓轨不做合成，直接给噪声打击。
目的不是好听，而是**能让耳朵判断采音对不对**（音头位置是否和咔哒重合）。
"""
from __future__ import annotations

import math
import os
import wave

import numpy as np

from .midi import MidiFile
from .onsets import Onset

SR = 44100


def _adsr(n: int, a: float, d: float, s: float, r: float, sr: int = SR) -> np.ndarray:
    env = np.zeros(n, dtype=np.float32)
    ai = min(int(a * sr), n)
    di = min(int(d * sr), max(0, n - ai))
    ri = min(int(r * sr), max(0, n - ai - di))
    si = max(0, n - ai - di - ri)
    p = 0
    if ai:
        env[p:p + ai] = np.linspace(0.0, 1.0, ai, dtype=np.float32); p += ai
    if di:
        env[p:p + di] = np.linspace(1.0, s, di, dtype=np.float32); p += di
    if si:
        env[p:p + si] = s; p += si
    if ri:
        env[p:p + ri] = np.linspace(s, 0.0, ri, dtype=np.float32)
    return env


def _tone(freq: float, dur: float, gain: float, sr: int = SR) -> np.ndarray:
    n = max(1, int(dur * sr))
    t = np.arange(n, dtype=np.float32) / sr
    w = (np.sin(2 * math.pi * freq * t)
         + 0.30 * np.sin(2 * math.pi * freq * 2 * t)
         + 0.12 * np.sin(2 * math.pi * freq * 3 * t))
    env = _adsr(n, 0.004, min(0.35, dur * 0.5), 0.25, min(0.25, dur * 0.5))
    return (w * env * gain).astype(np.float32)


def _click(sr: int = SR) -> np.ndarray:
    """采音咔哒：极短的宽带脉冲（音头位置 = onset 精确时刻）。"""
    n = int(0.010 * sr)
    t = np.arange(n, dtype=np.float32) / sr
    rng = np.random.default_rng(12345)
    noise = rng.standard_normal(n).astype(np.float32)
    body = np.sin(2 * math.pi * 2600.0 * t) * 0.7 + noise * 0.5
    env = np.exp(-t * 420.0)
    return (body * env * 0.55).astype(np.float32)


def _midi_to_freq(p: int) -> float:
    return 440.0 * (2.0 ** ((p - 69) / 12.0))


def render(midi: MidiFile, out_wav: str, *,
           track_index: int = 0,
           track_indexes: list[int] | None = None,
           onsets: list[Onset] | None = None,
           click: bool = True,
           click_gain: float = 0.55,
           tone_gain: float = 0.28,
           max_seconds: float = 0.0,
           lead_ms: float = 0.0,
           sr: int = SR) -> str:
    """渲染选定音轨 + （可选）onset 咔哒 到 WAV。

    `track_indexes` 给多条轨（多音轨采音时用）；不给就退回 `track_index`。
    lead_ms：在音频最前面补的静音长度。用来给 ADOFAI 的**倒计时 + 开局站位层**
    腾出时间（公式见 core.solve.total_lead_ms）。
    """
    trks = ([midi.tracks[i] for i in track_indexes] if track_indexes
            else [midi.tracks[track_index]])
    total_ms = midi.length_ms if max_seconds <= 0 else min(midi.length_ms, max_seconds * 1000.0)
    lead_n = int(max(0.0, lead_ms) / 1000.0 * sr)
    buf = np.zeros(lead_n + int(total_ms / 1000.0 * sr) + sr, dtype=np.float32)
    off = lead_n

    is_drum = all(t.is_drum_only() for t in trks)
    notes = [n for t in trks for n in t.notes]
    for n in notes:
        i0 = off + int(n.t_on_ms / 1000.0 * sr)
        if i0 >= len(buf):
            continue
        if is_drum:
            seg = _click(sr) * (n.velocity / 127.0) * tone_gain * 3.0
        else:
            dur = max(0.06, min(2.5, n.dur_ms / 1000.0))
            seg = _tone(_midi_to_freq(n.pitch), dur, tone_gain * (0.35 + n.velocity / 127.0))
        i1 = min(len(buf), i0 + len(seg))
        if i1 <= i0:
            continue
        buf[i0:i1] += seg[:i1 - i0]

    if click and onsets:
        c = _click(sr) * click_gain
        for o in onsets:
            i0 = off + int(o.t_ms / 1000.0 * sr)
            i1 = min(len(buf), i0 + len(c))
            if i1 <= i0:
                continue
            buf[i0:i1] += c[:i1 - i0]

    peak = float(np.max(np.abs(buf))) if len(buf) else 1.0
    if peak > 1e-9:
        buf = buf / peak * 0.88

    os.makedirs(os.path.dirname(os.path.abspath(out_wav)), exist_ok=True)
    pcm = (buf * 32767.0).astype("<i2")
    with wave.open(out_wav, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return out_wav
