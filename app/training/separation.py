"""separation.py — Demucs 音源分离的公共实现。

原来 demucs_mel / separate_all / preview_track 三处各写了一遍
「加载 htdemucs -> 44100 立体声 -> apply_model -> 重采样对齐 -> accomp」，
行为容易漂移。这里收拢为一份，三处共用。

输出约定：各 stem 均为 22050Hz 单声道 float32，长度与原始音频一致（L）。
"""
from __future__ import annotations

import os
import numpy as np
import torch
import librosa
from pathlib import Path

SR = 22050                 # 项目统一采样率
SR_DEMUCS = 44100          # Demucs 要求的输入采样率

# 六通道组合（按乐器分离 + 混合/伴奏）
STEMS = ["drums", "bass", "other", "vocals", "full", "accomp"]

_SEP = None  # (model, apply_model) 常驻缓存


def _demucs_cached():
    """检查 htdemucs 权重是否已缓存（避免离线模式下首次下载失败）。"""
    cache_dir = Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    if not cache_dir.exists():
        return False
    # htdemucs 缓存目录特征
    for d in cache_dir.glob("models--*htdemucs*"):
        if d.is_dir() and any(d.rglob("*.bin")) or any(d.rglob("*.safetensors")):
            return True
    return False


def _get_sep(device):
    """加载 htdemucs 并常驻（模块级缓存，多次调用不重复加载）。
    
    仅当权重已缓存时设置 HF_HUB_OFFLINE=1；否则允许联网下载。
    """
    global _SEP
    if _SEP is None:
        # 首次加载前决定是否离线
        if _demucs_cached():
            os.environ.setdefault("HF_HUB_OFFLINE", "1")
        else:
            os.environ.pop("HF_HUB_OFFLINE", None)
            os.environ.pop("TRANSFORMERS_OFFLINE", None)
        
        from demucs.pretrained import get_model
        from demucs.apply import apply_model
        m = get_model("htdemucs")
        m.to(device).eval()
        _SEP = (m, apply_model)
    return _SEP


def release_sep():
    """释放 Demucs 模型占用的显存（训练前调用，避免后续 OOM）。"""
    global _SEP
    _SEP = None
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def load_stereo_44k(audio_path):
    """任意音频 -> 44100Hz 双声道 float32 numpy (2, N)。"""
    y44, _ = librosa.load(audio_path, sr=SR_DEMUCS, mono=False)
    if y44.ndim == 1:
        y44 = y44[None]
    if y44.shape[0] == 1:
        y44 = np.repeat(y44, 2, axis=0)
    return y44.astype(np.float32)


def separate_stems(audio_path, device=None):
    """分离音频为各 stem（22050Hz 单声道，长度对齐 L）。

    返回 dict[str, np.ndarray]：
      drums / bass / other / vocals 由 Demucs 给出，
      full = 原始混合，accomp = full - vocals（去人声伴奏）。
    """
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    y_full, _ = librosa.load(audio_path, sr=SR, mono=True)
    L = len(y_full)

    y44 = load_stereo_44k(audio_path)
    model, apply_model = _get_sep(device)
    x = torch.from_numpy(y44).float().to(device)
    with torch.no_grad():
        sources = apply_model(model, x[None], device=device, progress=False)[0]  # (nsrc,2,N)
    names = list(model.sources)  # ['drums','bass','other','vocals']

    stems = {}
    for nm in ("drums", "bass", "other", "vocals"):
        i = names.index(nm)
        s = sources[i].mean(0).cpu().numpy().astype(np.float32)
        s = librosa.resample(s, orig_sr=SR_DEMUCS, target_sr=SR)
        if len(s) > L:
            s = s[:L]
        elif len(s) < L:
            s = np.pad(s, (0, L - len(s)))
        stems[nm] = s
    stems["full"] = y_full.astype(np.float32)
    stems["accomp"] = np.clip(y_full - stems["vocals"], -1.0, 1.0).astype(np.float32)
    return stems


def write_wav(path, y, sr=SR):
    """写 16bit 单声道 wav（跨平台，标准库 wave）。"""
    import wave
    y = np.asarray(y, dtype=np.float32)
    y = np.clip(y, -1.0, 1.0)
    data = (y * 32767.0).astype("<i2").tobytes()
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data)
