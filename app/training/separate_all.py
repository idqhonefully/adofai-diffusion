"""separate_all.py — 一次分离全部音轨并导出可播放 wav，供网页「第二步」列出试听。

用法: python separate_all.py --audio <wav> --outdir <dir> --prefix <uid>
输出到 stdout 的 JSON（唯一输出）:
  {"ok": true, "stems": [{"name","label","file","duration_s"}, ...]}
  {"ok": false, "error": "..."}

必须在 venv 里跑（需要 torch + demucs）。分离只跑一次（htdemucs），
再合成 full(原) / accomp(原-人声)。
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

SR = 22050
# 第二步要列出的「分解音轨」（不含原音频；原音频由前端用上传文件直接播放）
STEMS_OUT = [
    ("drums", "鼓点 drums"),
    ("bass", "贝斯 bass"),
    ("other", "旋律 / 钢琴 other"),
    ("vocals", "人声 vocals"),
    ("accomp", "伴奏（去人声）accomp"),
]


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
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--prefix", required=True)
    a = ap.parse_args()
    try:
        y_full, _ = librosa.load(a.audio, sr=SR, mono=True)
        L = len(y_full)

        from demucs_mel import _get_sep
        # Demucs 要求 44100 立体声输入
        y44, _ = librosa.load(a.audio, sr=44100, mono=False)
        if y44.ndim == 1:
            y44 = y44[None]
        if y44.shape[0] == 1:
            y44 = np.repeat(y44, 2, axis=0)
        y44 = y44.astype(np.float32)

        # 优先 GPU；若显卡架构不被打包的 PyTorch 支持
        # （CUDA error: no kernel image is available for the device），自动退回 CPU。
        # 退回后分离变慢但可用，避免老显卡用户直接报错。
        device = "cuda" if torch.cuda.is_available() else "cpu"
        try:
            model, apply_model = _get_sep(device)
            x = torch.from_numpy(y44).float().to(device)
            with torch.no_grad():
                sources = apply_model(model, x[None], device=device, progress=False)[0]  # (nsrc,2,N)
        except Exception as e:
            if device != "cuda":
                raise
            sys.stderr.write(f"[separate] GPU 分离失败({e})，退回 CPU\n")
            sys.stderr.flush()
            device = "cpu"
            model, apply_model = _get_sep(device)
            x = torch.from_numpy(y44).float().to(device)
            with torch.no_grad():
                sources = apply_model(model, x[None], device=device, progress=False)[0]
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

        stems = {}
        for nm in ("drums", "bass", "other", "vocals"):
            stems[nm] = get_stem(nm)
        # accomp = 原音频 - 人声
        stems["accomp"] = np.clip(y_full - stems["vocals"], -1.0, 1.0)

        out = []
        for key, label in STEMS_OUT:
            y = stems[key]
            fname = f"{a.prefix}_{key}.wav"
            fpath = os.path.join(a.outdir, fname)
            _write_wav(fpath, y, SR)
            out.append({
                "name": key, "label": label, "file": fname,
                "duration_s": round(len(y) / SR, 2),
            })
        print(json.dumps({"ok": True, "stems": out}))
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))


if __name__ == "__main__":
    main()
