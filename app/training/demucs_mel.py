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

分离本身的加载/推理/对齐逻辑统一在 separation.py（本文件与 separate_all /
preview_track 共用），本模块负责：把分离结果转成多通道 log-mel + 磁盘缓存。

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

import paths
from chart_repr import SR, HOP  # 22050, 128(本路径全局, 踩点+扩散同网格)
from separation import STEMS, _get_sep, separate_stems, release_sep

N_MELS = 128
N_FFT = 2048
# 踩点模型专用细网格
HOP_ONSET = 128
HOP_MS_ONSET = 1000.0 * HOP_ONSET / SR          # ≈ 5.805 ms

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
CACHE_DIR = str(paths.DEMUCS_CACHE_DIR)


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
        stems = separate_stems(audio_path, device=device)   # dict {name: mono22050 (L,)}
        chans = []
        for nm in STEMS:
            chans.append(_one_mel(stems[nm]))
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
