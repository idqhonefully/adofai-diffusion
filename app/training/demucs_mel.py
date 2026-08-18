"""demucs_mel.py — 用 Demucs 神经网络音源分离替代老掉牙的 HPSS（6 通道 + hop=128）

为什么换掉 HPSS：
  HPSS 的 percussive/harmonic 二分法对钢琴曲是灾难——钢琴音头(attack)软、延音长，
  percussive 通道根本抓不住 onset，踩点直接"瞎"。竞品用 HPSS 是技术债。
  Demucs (Meta) 把音乐按乐器拆成 drums/bass/other/vocals 四个 stem，
  钢琴干净落进 `other`，音头比 HPSS 锐利得多。

输出：多通道 log-mel (C, 128, T)，默认 C=6：
  ch0 = drums   （打击/节奏骨架）
  ch1 = bass    （低音线）
  ch2 = other   （旋律/钢琴主体 —— 钢琴曲最关键通道）
  ch3 = vocals  （人声）
  ch4 = full    （原始混合，整体能量上下文）
  ch5 = accomp  （full - vocals，纯伴奏，去掉人声onset干扰）
比竞品 HPSS 三通道(percussive/harmonic/full)强得多：分离是按乐器学的，且多一倍信息。

时间分辨率：mel 用 hop=128（≈5.8ms/帧，是 512 的 4 倍细），踩点模型能更准地咬住音头。
  —— 本 128 全链路版本里，扩散生成(VAE+扩散)也跑在 HOP=128 网格上，
     因此踩点(128)与落谱(128)同一网格，无需任何换算。

对齐保证：所有 stem 重采样到 SR=22050 并裁剪/补齐到与原始音频相同长度，
因此每条通道 mel 帧数 T 完全一致，能和 chart_repr.adofai_to_dense(hop_ms=5.805) 的真值逐帧对齐。

磁盘缓存：Demucs 分离首跑慢（GPU 几秒/首），结果缓存为 npy，
训练/推理重复调用同名音频不再重分离。
"""
from __future__ import annotations
import os, sys, hashlib
from pathlib import Path
import numpy as np
import torch
import librosa

ROOT = Path(__file__).resolve().parents[1]  # app/
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from chart_repr import SR, HOP  # 22050, 128(本路径全局, 踩点+扩散同网格)
from device_util import get_safe_device

N_MELS = 128
N_FFT = 2048
# 踩点模型专用细网格
HOP_ONSET = 128
HOP_MS_ONSET = 1000.0 * HOP_ONSET / SR          # ≈ 5.805 ms

# 六通道组合（按乐器分离 + 混合/伴奏）
STEMS = ["drums", "bass", "other", "vocals", "full", "accomp"]
# 老显卡(如 GTX 10 系) torch.cuda.is_available() 会假阳性，必须用一次真实内核探测
DEVICE = get_safe_device()
CACHE_DIR = os.path.join(ROOT.parent, "data", "demucs_cache")

_SEP = None  # (model, apply_model)


def _get_sep(device):
    global _SEP
    if _SEP is None:
        from demucs.pretrained import get_model
        from demucs.apply import apply_model
        m = get_model("htdemucs")
        m.to(device).eval()
        _SEP = (m, apply_model)
    return _SEP


def release_sep():
    """分离全部完成后释放 Demucs 模型占用的显存，避免后续训练/推理 OOM。

    笔记本 GPU(如 RTX 4070 8G) 上，htdemucs(~1.2G)若一直驻留显存，
    紧接着的 OnsetNet 训练在反向传播时就会 CUDA 显存不足。
    """
    global _SEP
    _SEP = None
    if get_safe_device() == "cuda":
        torch.cuda.empty_cache()


def _cache_path(audio_path):
    try:
        st = os.stat(audio_path)
        h = hashlib.sha1(
            f"{os.path.abspath(audio_path)}|{st.st_size}|{st.st_mtime:.3f}|C6H128".encode()
        ).hexdigest()[:16]
        return os.path.join(CACHE_DIR, h + ".npy")
    except Exception:
        return None


def _one_mel(y):
    """单声道波形 -> log-mel (128, T)，用 HOP_ONSET 细网格。"""
    mel = librosa.feature.melspectrogram(y=y, sr=SR, n_fft=N_FFT, hop_length=HOP_ONSET, n_mels=N_MELS)
    return np.log1p(mel).astype(np.float32)


def demucs_mel(audio_path, device=None, fallback=True):
    """返回 (C, 128, T) float32 的多通道 log-mel（C=len(STEMS)=6），hop=128。

    device: 分离用设备（默认自动）。
    fallback: 若 Demucs 失败，退化为把单通道 mel 复制 C 份（保证管线不崩，但失去分离收益）。
    """
    device = device or DEVICE
    cp = _cache_path(audio_path)
    if cp and os.path.exists(cp):
        return np.load(cp)

    try:
        # 原始混合 @ 22050 单声道（作为 full/accomp 通道 + 长度基准 L）
        y_full, _ = librosa.load(audio_path, sr=SR, mono=True)
        L = len(y_full)

        # 分离输入 @ 44100 立体声（Demucs 要求）
        y44, _ = librosa.load(audio_path, sr=44100, mono=False)
        if y44.ndim == 1:
            y44 = y44[None]
        if y44.shape[0] == 1:
            y44 = np.repeat(y44, 2, axis=0)  # (2, N44)
        y44 = y44.astype(np.float32)

        model, apply_model = _get_sep(device)
        x = torch.from_numpy(y44).float().to(device)
        with torch.no_grad():
            sources = apply_model(model, x[None], device=device, progress=False)[0]  # (nsrc,2,N)
        names = list(model.sources)  # ['drums','bass','other','vocals']

        # 先把需要的 stem 重采样到 22050 并对齐到 L
        stem220 = {}
        for nm in ("drums", "bass", "other", "vocals"):
            i = names.index(nm)
            s = sources[i].mean(0).cpu().numpy().astype(np.float32)  # (N,)
            s = librosa.resample(s, orig_sr=44100, target_sr=SR)     # -> 22050
            if len(s) > L:
                s = s[:L]
            elif len(s) < L:
                s = np.pad(s, (0, L - len(s)))
            stem220[nm] = s

        accomp = y_full - stem220["vocals"]           # 纯伴奏（去人声）
        accomp = np.clip(accomp, -1.0, 1.0)

        chans = []
        for nm in STEMS:
            if nm == "full":
                s = y_full
            elif nm == "accomp":
                s = accomp
            else:
                s = stem220[nm]
            chans.append(_one_mel(s))

        mel = np.stack(chans, axis=0)  # (6, 128, T)
    except Exception as e:
        if not fallback:
            raise
        y_full, _ = librosa.load(audio_path, sr=SR, mono=True)
        single = _one_mel(y_full)
        mel = np.stack([single] * len(STEMS), axis=0)
        print(f"[demucs] 分离失败({e})，退化为单通道复制")

    if cp:
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            np.save(cp, mel)
        except Exception:
            pass
    return mel


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) > 1:
        m = demucs_mel(_sys.argv[1])
        print("demucs_mel ->", m.shape, m.dtype)
