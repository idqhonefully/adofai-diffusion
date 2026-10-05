import sys, os, json
sys.path.insert(0, "<REPO>/chartgen")
from sidecar.session import Session

JSON = "<REPO>/audio-sep/test_mix_timestamps.json"
OUT = "<REPO>/audio-sep/test_export"

s = Session()
r = s.load(JSON)
print("load ok     =", r.get("ok"))
print("bpm0        =", s.midi.bpm0, "| tempo_map =", s.midi.tempo_map)
print("n tracks    =", len(s.midi.tracks))
print("melody pts  =", len(s.midi.tracks[0].notes) if s.midi.tracks else 0)

# 勾选 melody 轨(0),其余是空轨不参与采音
st = {
    "tracks_checked": [0],
    "dp_checked": [],
    "sub_checked": [],
    "song": "test_mix", "artist": "", "author": "osn1",
    "difficulty": 1, "countdown_ticks": 4,
    "separate_countdown": False, "auto_offset": True,
    "merge_ms": 30, "min_velocity": 1, "pitch_lo": 0, "pitch_hi": 127,
}
rb = s.rebuild(st)
print("\nrebuild ok  =", rb.get("ok"), "| msg =", rb.get("msg"))
print("chart       =", "YES" if s.chart else "NONE",
      "| floors =", len(s.chart.floors) if s.chart else None)

os.makedirs(OUT, exist_ok=True)
ex = s.export(st, OUT)
print("\nexport ok   =", ex.get("ok"))
print("dir         =", ex.get("dir"))
print("verify_ok   =", ex.get("verify_ok"))
print("msg         =\n" + ex.get("msg", ""))

# 列出导出产物
if ex.get("ok"):
    for root, _, files in os.walk(ex["dir"]):
        for f in files:
            print("  ->", os.path.join(root, f))
