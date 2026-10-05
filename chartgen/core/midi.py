"""零依赖 MIDI 解析器（Python 3.14 友好，不依赖 mido）。

支持：format 0/1/2、running status、SMPTE 与 PPQ 两种 division、
      tempo map（多段）、SysEx 跳过、meta 轨名（GBK/UTF-8/Shift-JIS 容错）、
      note-on(vel=0) 当 note-off。

产出：MidiFile
  .format .ppqn .tracks[...]
  track.name_guess / track.events / track.notes
  note = Note(track, channel, pitch, velocity, t_on_ms, t_off_ms, t_on_tick, t_off_tick)
  file.tempo_map = [(t_ms, bpm), ...]  file.duration_ms
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field


# ----------------------------------------------------------------- helpers
def _vlq(buf: bytes, i: int) -> tuple[int, int]:
    v = 0
    while True:
        if i >= len(buf):
            raise ValueError("VLQ overrun")
        c = buf[i]
        i += 1
        v = (v << 7) | (c & 0x7F)
        if not (c & 0x80):
            return v, i


_DECODERS = ("utf-8", "gbk", "shift_jis", "big5", "latin-1")


def _decode_text(b: bytes) -> str:
    b = b.rstrip(b"\x00")
    for enc in _DECODERS:
        try:
            return b.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return b.decode("latin-1", "replace")


@dataclass
class Note:
    track: int
    channel: int
    pitch: int
    velocity: int
    t_on_ms: float
    t_off_ms: float
    t_on_tick: int
    t_off_tick: int

    @property
    def dur_ms(self) -> float:
        return max(0.0, self.t_off_ms - self.t_on_ms)

    @property
    def name(self) -> str:
        names = ("C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B")
        return f"{names[self.pitch % 12]}{self.pitch // 12 - 1}"


@dataclass
class Track:
    index: int
    name: str = ""
    program: dict[int, int] = field(default_factory=dict)   # channel -> program
    notes: list[Note] = field(default_factory=list)
    raw_event_count: int = 0

    @property
    def channels(self) -> list[int]:
        return sorted({n.channel for n in self.notes})

    def is_drum_only(self) -> bool:
        return bool(self.notes) and all(n.channel == 9 for n in self.notes)


@dataclass
class MidiFile:
    path: str = ""
    format: int = 1
    ppqn: int = 480
    smpte: tuple[int, int] | None = None
    tracks: list[Track] = field(default_factory=list)
    tempo_map: list[tuple[int, float]] = field(default_factory=list)   # (tick, bpm)
    time_sig: list[tuple[int, int, int]] = field(default_factory=list)  # (tick, num, den)
    length_ms: float = 0.0

    # ---------------------------------------------------------------- util
    @property
    def bpm0(self) -> float:
        return self.tempo_map[0][1] if self.tempo_map else 120.0

    def ticks_to_ms(self, tick: int) -> float:
        """按 tempo map 把 tick 换算成毫秒。"""
        if self.smpte is not None:
            fps, tpf = self.smpte
            return tick / (fps * tpf) * 1000.0
        us = 0.0
        prev_tick = 0
        bpm = 120.0
        for t, b in self.tempo_map:
            if tick <= prev_tick:
                break
            if t >= tick:
                seg = tick - prev_tick
                us += seg * (60_000_000.0 / bpm) / self.ppqn
                prev_tick = tick
                bpm = b
                break
            seg = t - prev_tick
            us += seg * (60_000_000.0 / bpm) / self.ppqn
            prev_tick = t
            bpm = b
        else:
            seg = tick - prev_tick
            us += seg * (60_000_000.0 / bpm) / self.ppqn
        return us / 1000.0

    def ms_to_tick(self, ms: float) -> int:
        """ms -> tick（tempo map 分段精确求，用于量化网格落地）。"""
        if self.smpte is not None:
            fps, tpf = self.smpte
            return int(round(ms / 1000.0 * fps * tpf))
        prev_ms = 0.0
        prev_tick = 0
        bpm = 120.0
        for t, b in self.tempo_map:
            seg_end_ms = prev_ms + (t - prev_tick) * (60_000.0 / bpm) / self.ppqn
            if ms < seg_end_ms:
                return prev_tick + int(round((ms - prev_ms) * self.ppqn * bpm / 60_000.0))
            prev_ms, prev_tick, bpm = seg_end_ms, t, b
        return prev_tick + int(round((ms - prev_ms) * self.ppqn * bpm / 60_000.0))

    def stats(self) -> str:
        tot = sum(len(t.notes) for t in self.tracks)
        return (f"format={self.format} ppqn={self.ppqn} tracks={len(self.tracks)} "
                f"notes={tot} bpm0={self.bpm0:g} len={self.length_ms/1000:.1f}s")


# ----------------------------------------------------------------- parser
def load(path: str) -> MidiFile:
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:4] != b"MThd":
        raise ValueError("不是 MIDI 文件（缺 MThd）")

    hlen = struct.unpack(">I", data[4:8])[0]
    fmt, ntrk, div = struct.unpack(">HHH", data[8:14])
    mid = MidiFile(path=path, format=fmt)
    if div & 0x8000:
        fps = 256 - ((div >> 8) & 0xFF)
        mid.smpte = (fps, div & 0xFF)
        mid.ppqn = 480
    else:
        mid.ppqn = div or 480

    i = 8 + hlen
    tempo_pending: list[tuple[int, float]] = []
    max_tick = 0

    for ti in range(ntrk):
        if data[i:i + 4] != b"MTrk":
            break
        ln = struct.unpack(">I", data[i + 4:i + 8])[0]
        body = data[i + 8:i + 8 + ln]
        i += 8 + ln

        trk = Track(index=ti)
        pending: dict[tuple[int, int], list[tuple[int, int]]] = {}
        j = 0
        tick = 0
        running: int | None = None
        while j < len(body):
            dt, j = _vlq(body, j)
            tick += dt
            if j >= len(body):
                break
            st = body[j]
            if st < 0x80:
                if running is None:
                    j += 1
                    continue
                st = running
            else:
                j += 1
                if st < 0xF0:
                    running = st

            if st == 0xFF:
                if j >= len(body):
                    break
                mt = body[j]; j += 1
                ln2, j = _vlq(body, j)
                payload = body[j:j + ln2]; j += ln2
                trk.raw_event_count += 1
                if mt == 0x51 and ln2 == 3:
                    us = (payload[0] << 16) | (payload[1] << 8) | payload[2]
                    bpm = 60_000_000.0 / us if us else 120.0
                    tempo_pending.append((tick, bpm))
                elif mt == 0x03:
                    trk.name = trk.name or _decode_text(payload)
                elif mt == 0x58 and ln2 >= 2:
                    den = 1 << payload[1]
                    mid.time_sig.append((tick, payload[0], den))
                elif mt == 0x2F:
                    break
            elif st in (0xF0, 0xF7):
                ln2, j = _vlq(body, j)
                j += ln2
            else:
                hi, ch = st & 0xF0, st & 0x0F
                if hi in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                    if j + 1 >= len(body):
                        break
                    d1, d2 = body[j], body[j + 1]; j += 2
                    if hi == 0x90 and d2 > 0:
                        pending.setdefault((ch, d1), []).append((tick, d2))
                    elif hi == 0x80 or (hi == 0x90 and d2 == 0):
                        q = pending.get((ch, d1))
                        if q:
                            t_on, vel = q.pop(0)
                            trk.notes.append(Note(ti, ch, d1, vel, 0.0, 0.0, t_on, tick))
                elif hi in (0xC0, 0xD0):
                    if j >= len(body):
                        break
                    v = body[j]; j += 1
                    if hi == 0xC0:
                        trk.program[ch] = v
        # 悬挂的 note-on：用曲末收尾
        for (ch, pitch), q in pending.items():
            for t_on, vel in q:
                trk.notes.append(Note(ti, ch, pitch, vel, 0.0, 0.0, t_on, tick))
        max_tick = max(max_tick, tick)
        mid.tracks.append(trk)

    # tempo map（去重排序；空则 120）
    tempo_pending.sort(key=lambda x: x[0])
    dedup: list[tuple[int, float]] = []
    for t, b in tempo_pending:
        if dedup and dedup[-1][0] == t:
            dedup[-1] = (t, b)
        else:
            dedup.append((t, b))
    mid.tempo_map = dedup or [(0, 120.0)]

    # 时间换算
    for trk in mid.tracks:
        for n in trk.notes:
            n.t_on_ms = mid.ticks_to_ms(n.t_on_tick)
            n.t_off_ms = mid.ticks_to_ms(n.t_off_tick)
        trk.notes.sort(key=lambda n: (n.t_on_ms, n.pitch))
    mid.length_ms = mid.ticks_to_ms(max_tick)
    return mid
