"""preview_track.py — 把选中音轨分离出来导出成可播放音频，供网页试听。

用法: python preview_track.py --audio <wav> --track <name> --out <outpath>
输出到 stdout 的 JSON（唯一输出，便于父进程解析）:
  {"ok": true, "duration_s": 12.34, "path": "..."}
  {"ok": false, "error": "..."}

必须在 torch 环境里跑（需要 demucs）。分离复用 separation.separate_stems。

核心逻辑在 do_preview()：子进程模式（CLI）与 web_server 的 in-process
模式共用，避免两套实现漂移。
"""
from __future__ import annotations
import os, sys, json, argparse
import numpy as np
import librosa

ROOT = os.path.dirname(os.path.abspath(__file__))          # app/training/
APP = os.path.dirname(ROOT)                                # app/
for p in (APP, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from separation import SR, separate_stems, write_wav

VALID = ("all", "drums", "bass", "other", "vocals", "accomp")


def do_preview(audio_path, track, out_path):
    """分离选中音轨并导出 wav。返回 {"ok": true, "duration_s":..., "path":...} 或 {"ok": false, "error":...}。"""
    try:
        if track == "all" or track not in VALID:
            y, _ = librosa.load(audio_path, sr=SR, mono=True)
            y = y.astype(np.float32)
        else:
            stems = separate_stems(audio_path)
            y = stems[track]
        write_wav(out_path, y, SR)
        return {"ok": True, "duration_s": round(len(y) / SR, 2), "path": out_path}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--track", default="all")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    print(json.dumps(do_preview(a.audio, a.track, a.out)))


if __name__ == "__main__":
    main()
