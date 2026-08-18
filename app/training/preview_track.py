"""preview_track.py — 把选中音轨分离出来导出成可播放音频，供网页试听。

用法: python preview_track.py --audio <wav> --track <name> --out <outpath>
输出到 stdout 的 JSON（唯一输出，便于父进程解析）:
  {"ok": true, "duration_s": 12.34, "path": "..."}
  {"ok": false, "error": "..."}

必须在 venv 里跑（需要 torch + demucs）。分离复用 demucs_mel._get_sep。
"""
from __future__ import annotations
import os, sys, json, argparse
import numpy as np

ROOT = os.path.dirname(os.path.abspath(__file__))          # app/training/
APP = os.path.dirname(ROOT)                                # app/
for p in (APP, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

import torch
import librosa
from device_util import get_safe_device

SR = 22050
VALID = ("all", "drums", "bass", "other", "vocals", "accomp")


def _separate_stem(audio_path, track):
    """返回 (stem_22050_mono float32, sr)。逻辑与 demucs_mel.demucs_mel 一致。"""
    device = get_safe_device()   # 老显卡 CUDA 假阳性，先探测再决定
    from demucs_mel import _get_sep

    if track == "all" or track not in VALID:
        y, _ = librosa.load(audio_path, sr=SR, mono=True)
        return y.astype(np.float32), SR

    # Demucs 要求 44100 立体声输入
    y44, _ = librosa.load(audio_path, sr=44100, mono=False)
    if y44.ndim == 1:
        y44 = y44[None]
    if y44.shape[0] == 1:
        y44 = np.repeat(y44, 2, axis=0)
    y44 = y44.astype(np.float32)

    y_full, _ = librosa.load(audio_path, sr=SR, mono=True)
    L = len(y_full)

    model, apply_model = _get_sep(device)
    x = torch.from_numpy(y44).float().to(device)
    with torch.no_grad():
        sources = apply_model(model, x[None], device=device, progress=False)[0]  # (nsrc,2,N)
    names = list(model.sources)  # ['drums','bass','other','vocals']

    def get_stem(nm):
        i = names.index(nm)
        s = sources[i].mean(0).cpu().numpy().astype(np.float32)
        s = librosa.resample(s, orig_sr=44100, target_sr=SR)
        if len(s) > L:
            s = s[:L]
        elif len(s) < L:
            s = np.pad(s, (0, L - len(s)))
        return s

    if track == "accomp":
        voc = get_stem("vocals")
        accomp = np.clip(y_full - voc, -1.0, 1.0)
        return accomp.astype(np.float32), SR
    return get_stem(track), SR


def _write_wav(path, y, sr):
    import wave
    y = np.asarray(y, dtype=np.float32)
    y = np.clip(y, -1.0, 1.0)
    data = (y * 32767.0).astype("<i2").tobytes()
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--track", default="all")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    try:
        y, sr = _separate_stem(a.audio, a.track)
        _write_wav(a.out, y, sr)
        dur = round(len(y) / sr, 2)
        print(json.dumps({"ok": True, "duration_s": dur, "path": a.out}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


if __name__ == "__main__":
    main()
