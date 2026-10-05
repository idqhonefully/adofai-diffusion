#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Merge several per-stem MIDI files into one multi-track Format-1 MIDI.

Each stem is transcribed from an isolated audio stem (BS-RoFormer separation),
so every note in a stem belongs to that instrument. We re-emit all note
events of a stem onto a dedicated MIDI channel with the correct General MIDI
program number, producing a clean, instrument-pure, correctly-labeled score.

Usage:
    python merge_stems.py <spec.json>

spec.json:
    {"out": "combined.mid", "ticks": 480,
     "stems": [{"label":"Piano","ch":0,"prog":0,"drums":false,"mid":"piano.mid"}, ...]}
"""
import sys
import json
import mido


def main():
    if len(sys.argv) < 2:
        print("usage: merge_stems.py <spec.json>", file=sys.stderr)
        sys.exit(2)
    with open(sys.argv[1], "r", encoding="utf-8") as f:
        spec = json.load(f)

    out = spec["out"]
    ticks = int(spec.get("ticks", 480))
    stems = spec.get("stems", [])

    mid = mido.MidiFile(type=1, ticks_per_beat=ticks)

    # detect tempo from the first stem that carries a set_tempo meta
    tempo = 500000  # 120 BPM default
    for s in stems:
        try:
            m = mido.MidiFile(s["mid"])
        except Exception:
            continue
        for tr in m.tracks:
            for msg in tr:
                if msg.type == "set_tempo":
                    tempo = msg.tempo
                    break
            else:
                continue
            break
        if tempo != 500000:
            break

    cond = mido.MidiTrack()
    cond.append(mido.MetaMessage("track_name", name="Conductor", time=0))
    cond.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))
    cond.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(cond)

    for s in stems:
        src = mido.MidiFile(s["mid"])
        merged = mido.merge_tracks(src.tracks)
        ch = int(s["ch"])
        tr = mido.MidiTrack()
        tr.append(mido.MetaMessage("track_name", name=s["label"], time=0))
        if not s.get("drums"):
            tr.append(mido.Message("program_change", channel=ch,
                                    program=int(s["prog"]), time=0))
        for msg in merged:
            if msg.type in ("note_on", "note_off"):
                tr.append(msg.copy(channel=ch))
        tr.append(mido.MetaMessage("end_of_track", time=0))
        mid.tracks.append(tr)

    mid.save(out)
    print(f"MERGED_OK {out} ({len(mid.tracks)} tracks)")


if __name__ == "__main__":
    main()
