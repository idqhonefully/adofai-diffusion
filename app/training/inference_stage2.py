"""inference_stage2.py — 128 全链路生成: 音频 -> OnsetNet(踩点) + VAE/扩散(加事件) -> .adofai

所有网格统一为 HOP=128 (≈5.805ms/帧):
  - 踩点 : Demucs 6 通道 mel @128 -> OnsetNet -> onset 帧(128 网格)
  - 扩散 : 单通道 full-mix mel @128 -> VAE/扩散 -> 稠密谱面(128 网格)
  - 落谱 : dense_to_adofai(onset_frames=踩点帧, hop_ms=5.805)
整条链路同一网格, 无需 512<->128 换算(这就是和 512 版本最大的干净之处)。
"""
from __future__ import annotations
import os, sys, json, argparse
from pathlib import Path
import numpy as np
import torch
torch.set_num_threads(1)  # 避免后台/子进程多线程段错误

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "training"))
os.environ.setdefault("MKL_THREADING_LAYER", "sequential")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("KMP_AFFINITY", "disabled")

import paths
import librosa
from dataset import CHUNK, N_MELS, _mel
from vae import ChartVAE
from diffusion import DDPM
from chart_repr import dense_to_adofai, SR, HOP, HOP_MS
from onset_net import OnsetNet, predict_onset_frames
from demucs_mel import demucs_mel, HOP_ONSET, STEMS

CKPT = str(paths.CHECKPOINTS_DIR)
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"


# 信号检测器(频谱通量峰值)已移除：踩点固定走 OnsetNet 模型，不再回退。


_ONSET_NET = None
_VAE = None
_DDPM = None


def _load_onset_net():
    """加载踩点神经网络（本路径固定 6 通道 Demucs onsets）。权重缺失/加载失败直接报错，不回退信号检测器。"""
    global _ONSET_NET
    if _ONSET_NET is not None:
        return _ONSET_NET
    p = os.path.join(CKPT, "onset_net.pt")
    if not os.path.exists(p):
        raise RuntimeError(f"[infer] 未找到踩点模型权重 {p}，无法生成（请先训练 onset 模型）")
    try:
        m = OnsetNet(n_mels=N_MELS, in_channels=len(STEMS)).to(DEVICE).eval()
        m.load_state_dict(torch.load(p, map_location=DEVICE))
    except Exception as e:
        raise RuntimeError(f"[infer] 踩点模型加载失败({e})，无法生成")
    _ONSET_NET = m
    print(f"[infer] 已加载踩点模型 onset_net.pt（模型踩点, in_channels={len(STEMS)}）")
    return _ONSET_NET


def _load_vae():
    """VAE 常驻缓存（in-process 模式多次推理不重复加载权重）。"""
    global _VAE
    if _VAE is None:
        vae = ChartVAE().to(DEVICE).eval()
        vae.load_state_dict(torch.load(os.path.join(CKPT, paths.CKPT_VAE), map_location=DEVICE))
        _VAE = vae
    return _VAE


def _load_ddpm():
    """DDPM 常驻缓存（in-process 模式多次推理不重复加载权重）。"""
    global _DDPM
    if _DDPM is None:
        ddpm = DDPM().to(DEVICE).eval()
        ddpm.load_state_dict(torch.load(os.path.join(CKPT, paths.CKPT_DDPM), map_location=DEVICE))
        _DDPM = ddpm
    return _DDPM


def _detect_onsets(mel_onset):
    """踩点检测：固定用神经网络（Demucs 6 通道 mel 喂入，真正模型踩点）。
    输入 mel_onset: (C,128,T128) @ hop128。输出 (frames:list[int], prob:np.ndarray @128)。
    """
    net = _load_onset_net()
    frames, prob = predict_onset_frames(net, mel_onset, device=DEVICE)
    return frames, prob


@torch.no_grad()
def generate(audio, out_path, base_bpm=120.0, steps=50, guidance=2.5, onset_track="all"):
    y, _ = librosa.load(audio, sr=SR, mono=True)
    mel = _mel(y)                       # (128, T) 单通道 —— 扩散条件 mc 用(@128)
    T = mel.shape[1]
    # 踩点专用：Demucs 6 通道分离 mel @ hop128，模型踩点（与扩散同网格，无换算）
    mel_onset = demucs_mel(audio, device=DEVICE)   # (6, 128, T128)
    # —— 选音轨采音：把未选中通道置零，只留选中轨喂 OnsetNet（6 通道维度不变，避开重训）——
    if onset_track and onset_track != "all":
        if onset_track in STEMS:
            idx = STEMS.index(onset_track)
            single = mel_onset[idx]  # (128, T) 选中轨
            # 关键：把选中轨复制填充到全部 6 通道，而非置零其他通道。
            # OnsetNet 训练时 6 通道始终同时有内容，若置零会触发"静音"分布导致踩点塌成 0；
            # 复制填充保持"多通道均非零"的分布，模型只从该轨的 onset 特征踩点。
            mel_onset = np.stack([single] * len(STEMS), axis=0)
            print(f"[infer] 采音音轨={onset_track}（已复制填充至全部 6 通道供 OnsetNet 踩点）")
        else:
            print(f"[infer] 未知音轨 '{onset_track}'，回退全部混合踩点")
    onsets, onset_prob = _detect_onsets(mel_onset)
    print(f"[infer] audio {Path(audio).name} T={T} frames, 模型(Demucs-OnsetNet) 踩点 onsets={len(onsets)}")

    vae = _load_vae()
    ddpm = _load_ddpm()

    # 逐块处理（每块 CHUNK 帧 @128）
    dense_chunks = []
    for s in range(0, T, CHUNK):
        if s + CHUNK > T:
            break
        mc = torch.from_numpy(mel[:, s:s + CHUNK])[None].to(DEVICE)
        # onset 包络 = 该块内音头条件（模型概率平滑）
        oe_np = np.zeros((1, 1, CHUNK), np.float32)
        for f in range(s, min(s + CHUNK, len(onset_prob))):
            oe_np[0, 0, f - s] = float(onset_prob[f])
        oe = torch.from_numpy(oe_np).to(DEVICE)
        z = ddpm.sample(mc, oe, steps=steps, guidance=guidance, device=DEVICE)  # (1,16,chunk/16)
        rec = vae.decode(z)               # (1,3,chunk)
        rec = rec[0].cpu().numpy()
        rec[0] = 1.0 / (1.0 + np.exp(-rec[0]))     # sigmoid onset
        rec[2] = 1.0 / (1.0 + np.exp(-rec[2]))     # sigmoid twirl
        dense_chunks.append(rec)
    if not dense_chunks:
        print("[infer] 音频过短, 无法生成"); return None
    dense = np.concatenate(dense_chunks, axis=1)    # (3, T')
    # 仅保留与原始 onset 帧一致的范围
    Tf = dense.shape[1]
    onset_frames = [f for f in onsets if f < Tf]

    level = dense_to_adofai(dense, global_bpm=float(base_bpm), hop_ms=HOP_MS,
                            song=Path(audio).name, onset_frames=onset_frames,
                            twirl_desire=dense[2])
    if level is None:
        print("[infer] dense_to_adofai 返回 None (onset 不足)")
        return None
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(level, ensure_ascii=False, indent=1), encoding="utf-8-sig")
    print(f"[infer] wrote {out_path} | tiles={len(level['angleData'])} "
          f"| twirl={len(level['actions'])} | bpm={base_bpm}")
    return level


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--bpm", type=float, default=120.0)
    ap.add_argument("--steps", type=int, default=50)
    ap.add_argument("--guidance", type=float, default=2.5)
    ap.add_argument("--track", default="all",
                    help="采音音轨: all/drums/bass/other/vocals/accomp（选某轨则只盯该轨踩点）")
    a = ap.parse_args()
    generate(a.audio, a.out, a.bpm, a.steps, a.guidance, a.track)


if __name__ == "__main__":
    main()
