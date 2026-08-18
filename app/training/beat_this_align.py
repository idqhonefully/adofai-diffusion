"""beat_this_align.py — 用 SOTA 节拍追踪器 Beat This! 估计 BPM/downbeat，并提供节拍相位特征

Beat This! (ISMIR 2024, JKU) 是当下 beat/downbeat tracking 的 SOTA：输入音频 -> 输出
beats + downbeats（每拍 / 每下拍的时间戳，单位秒）。本项目用它在两条路上增强 OnsetNet：

  (A) 校正谱面 BPM / 强拍对齐（【无需重训】，立即生效）
      - estimate_bpm(beats) 给出歌曲真实 BPM，替换推理时写死的 base_bpm(默认120)，
        彻底解决"谱面 BPM 经常是错的、整谱相对音乐错位"的老问题。
      - downbeats 用于把首个 onset 吸附到最近的强拍（可选 snap_offset_to_downbeat）。

  (B) 作为 OnsetNet 的节拍网格条件（【需要重训】，代码已铺好，等用户点头）
      - build_beat_phase(beats, downbeats, T) 产出 (3,128,T) 的节拍相位特征
        (sin(2πφ), cos(2πφ), downbeat_gate)，拼到 Demucs 6 通道后面 -> in_channels=9。
        让模型显式知道"现在在拍子的哪个相位 / 是不是强拍"，弱起、切分、密集音头更稳。

依赖：已在 portable/venv 装好 beat_this（torch 2.11 已满足 >=2.0）。
音频读取走 librosa（Audio2Beats 直接吃 tensor），无需 ffmpeg，兼容 .ogg。
"""
from __future__ import annotations
import numpy as np
from pathlib import Path

from demucs_mel import HOP_ONSET, HOP_MS_ONSET, DEVICE as _DEVICE

# 用 8MB small 模型：便携友好、精度够用。final0(78MB) 更准但占空间。
CHECKPOINT = "small0"
# 节拍相位特征通道数（sin, cos, downbeat_gate）
BEAT_COND_CH = 3

_f2b = None


def _get_a2b(device):
    """懒加载 Audio2Beats（进程内只建一次）。"""
    global _f2b
    if _f2b is None:
        from beat_this.inference import Audio2Beats
        _f2b = Audio2Beats(checkpoint_path=CHECKPOINT, device=device, dbn=False)
    return _f2b


def estimate_beats(audio_path, device=None, sr=22050):
    """返回 (beats, downbeats) 两个 np.ndarray[float64, 秒]。

    用 librosa 读音频（兼容 .ogg，无需 ffmpeg）后交给 Beat This! 的 Audio2Beats。
    """
    import librosa
    device = device or "cpu"  # 一次性推理走 CPU 即可，避免抢 GPU 显存
    y, _ = librosa.load(audio_path, sr=sr, mono=True)
    a2b = _get_a2b(device)
    beats, downbeats = a2b(y.astype(np.float32), sr)
    return np.asarray(beats, dtype=np.float64), np.asarray(downbeats, dtype=np.float64)


def estimate_bpm(beats, min_bpm=60.0, max_bpm=240.0):
    """从中拍间隔(IBI)中位数估计 BPM，并做八度校正到 [min_bpm, max_bpm]。

    Beat This! 不用 DBN，偶尔会落到半速/倍速（如真实 158 估成 79）。这里只做基本八度
    折回；若仍不满意，可后续开 dbn=True（需装 madmom）进一步稳定。
    """
    if len(beats) < 2:
        return 120.0
    ibi = np.diff(beats)
    ibi = ibi[ibi > 0]
    if len(ibi) == 0:
        return 120.0
    bpm = 60.0 / float(np.median(ibi))
    while bpm < min_bpm:
        bpm *= 2.0
    while bpm > max_bpm:
        bpm /= 2.0
    return round(float(bpm), 2)


def snap_offset_to_downbeat(onset_frames, downbeats, hop_ms=HOP_MS_ONSET):
    """把首个 onset 帧吸附到最近的 downbeat，返回 offset(ms, int)。

    注意：默认【不】启用。ADOFAI 通常让 tile0 落在首个 onset（可玩性最佳）。
    仅当首个 onset 与某 downbeat 极近时才用此函数做微调；否则保持 onset 对齐。
    """
    if len(onset_frames) == 0 or len(downbeats) == 0:
        return 0
    first_sec = onset_frames[0] * hop_ms / 1000.0
    d = downbeats[np.argmin(np.abs(downbeats - first_sec))]
    return int(round(d * 1000.0 - first_sec * 1000.0))


def build_beat_phase(beats, downbeats, T, hop_ms=HOP_MS_ONSET):
    """构建节拍相位特征 (BEAT_COND_CH, 128, T)，拼到 Demucs mel 后作 OnsetNet 条件。

    通道布局：
      ch0 = sin(2πφ)   φ∈[0,1) 为当前帧在「相邻两拍之间」的相位
      ch1 = cos(2πφ)
      ch2 = downbeat_gate  在 downbeat 时刻附近的高斯脉冲(标识强拍)
    特征沿频率轴恒定（全局时间信号），故先算 (3,T) 再广播成 (3,128,T)。
    """
    t_sec = np.arange(T, dtype=np.float64) * hop_ms / 1000.0
    phase = np.zeros(T, dtype=np.float64)
    if len(beats) >= 2:
        idx = np.searchsorted(beats, t_sec, side="right") - 1
        idx = np.clip(idx, 0, len(beats) - 2)
        b0 = beats[idx]
        b1 = beats[idx + 1]
        phase = np.clip((t_sec - b0) / np.maximum(b1 - b0, 1e-6), 0.0, 1.0)
    sin = np.sin(2.0 * np.pi * phase).astype(np.float32)
    cos = np.cos(2.0 * np.pi * phase).astype(np.float32)

    db_gate = np.zeros(T, dtype=np.float32)
    if len(downbeats) >= 1:
        sigma = max(1, int(round(0.04 / (hop_ms / 1000.0))))  # ~40ms 宽
        for db in downbeats:
            fr = int(round(db * 1000.0 / hop_ms))
            if 0 <= fr < T:
                lo = max(0, fr - 3 * sigma)
                hi = min(T, fr + 3 * sigma + 1)
                d = np.arange(lo, hi) - fr
                db_gate[lo:hi] = np.maximum(
                    db_gate[lo:hi],
                    np.exp(-(d ** 2) / (2.0 * sigma * sigma)).astype(np.float32),
                )
    feat = np.stack([sin, cos, db_gate], axis=0)          # (3, T)
    feat3d = np.broadcast_to(feat[:, None, :], (BEAT_COND_CH, 128, T)).copy()  # (3,128,T)
    return feat3d


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        b, db = estimate_beats(sys.argv[1])
        print(f"beats={len(b)} downbeats={len(db)} bpm={estimate_bpm(b):.2f}")
