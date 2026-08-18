"""inference_stage2.py — ADOFAI Diffusion 生成: 音频 -> OnsetNet(踩点) + VAE/扩散(加事件) -> .adofai

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
os.environ.setdefault("HF_HUB_OFFLINE", "1")  # Demucs 权重已缓存, 离线加载跳过网络重试

import librosa
from dataset import CHUNK, N_MELS, _mel
from vae import ChartVAE
from diffusion import DDPM
from chart_repr import dense_to_adofai, SR, HOP, HOP_MS
from onset_net import OnsetNet, predict_onset_frames
from demucs_mel import demucs_mel, HOP_ONSET, STEMS
from beat_this_align import (estimate_beats, estimate_bpm, build_beat_phase, BEAT_COND_CH)
from device_util import get_safe_device

CKPT = os.environ.get("ADOFAI_DATA_DIR", str(ROOT / "data")) + "/checkpoints"
# 老显卡(如 GTX 10 系) torch.cuda.is_available() 会假阳性，必须用一次真实内核探测
DEVICE = get_safe_device()


# 信号检测器(频谱通量峰值)已移除：踩点固定走 OnsetNet 模型，不再回退。


_ONSET_NETS = {}  # 按 beat_grid 分缓存：False->标准6通道, True->9通道(含节拍网格)


def _load_onset_net(beat_grid=False):
    """加载踩点神经网络。权重缺失/加载失败直接报错，不回退信号检测器。

    beat_grid=True 时加载含节拍相位条件的版本(onset_net_beatgrid.pt, in_channels=9)，
    该权重需重训得到（见 train_onset.py --beat_grid）；缺失即报错，不静默回退。
    """
    if beat_grid in _ONSET_NETS:
        return _ONSET_NETS[beat_grid]
    if beat_grid:
        p = os.path.join(CKPT, "onset_net_beatgrid.pt")
        in_ch = len(STEMS) + BEAT_COND_CH
        tag = "节拍网格(beatgrid)"
    else:
        p = os.path.join(CKPT, "onset_net.pt")
        in_ch = len(STEMS)
        tag = "标准"
    if not os.path.exists(p):
        raise RuntimeError(f"[infer] 未找到{tag}踩点模型权重 {p}（beatgrid 需先重训 onset）")
    try:
        m = OnsetNet(n_mels=N_MELS, in_channels=in_ch).to(DEVICE).eval()
        m.load_state_dict(torch.load(p, map_location=DEVICE))
    except Exception as e:
        raise RuntimeError(f"[infer] {tag}踩点模型加载失败({e})，无法生成")
    _ONSET_NETS[beat_grid] = m
    print(f"[infer] 已加载{tag}踩点模型 {os.path.basename(p)} (模型踩点, in_channels={in_ch})")
    return _ONSET_NETS[beat_grid]


def _detect_onsets(mel_onset, beat_grid=False):
    """踩点检测：固定用神经网络踩点。
    输入 mel_onset: (C,128,T128) @ hop128（beat_grid 时为 9 通道，否则 6 通道）。
    输出 (frames:list[int], prob:np.ndarray @128)。
    """
    net = _load_onset_net(beat_grid)
    frames, prob = predict_onset_frames(net, mel_onset, device=DEVICE)
    return frames, prob


@torch.no_grad()
def generate(audio, out_path, base_bpm=120.0, steps=50, guidance=2.5,
             onset_track="all", vfx=False, intensity=0.5,
             auto_bpm=False, beat_grid=False):
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
    # —— SOTA 节拍对齐 (Beat This!) ——
    # 一次推理拿到 beats/downbeats，Phase A 用于 BPM、Phase B 用于节拍相位条件。
    _bt_beats, _bt_down = None, None
    if auto_bpm or beat_grid:
        try:
            _bt_beats, _bt_down = estimate_beats(audio, device="cpu")
        except Exception as e:
            print(f"[beat_this] 推理失败({e})，跳过节拍对齐")
    # Phase A：用 Beat This! 估计真实 BPM，替换写死的 base_bpm（无需重训，立即生效）。
    if auto_bpm:
        if _bt_beats is not None and len(_bt_beats) >= 2:
            est = estimate_bpm(_bt_beats)
            print(f"[beat_this] 估计 BPM={est}（原 base_bpm={base_bpm}），已采用")
            base_bpm = est
        else:
            print("[beat_this] 未检出足够 beats，沿用 base_bpm")
    # Phase B：把节拍相位条件(3通道)拼到 mel 后 -> 9 通道，喂 beatgrid 模型（需重训）。
    if beat_grid:
        if _bt_beats is not None:
            bg = build_beat_phase(_bt_beats, _bt_down, mel_onset.shape[2])  # (3,128,T)
            mel_onset = np.concatenate([mel_onset, bg], axis=0)            # (9,128,T)
            print(f"[beat_this] 已拼节拍相位条件 -> in_channels={mel_onset.shape[0]}")
        else:
            print("[beat_this] 无 beats 可构建节拍条件，回退纯 mel")
    onsets, onset_prob = _detect_onsets(mel_onset, beat_grid=beat_grid)
    print(f"[infer] audio {Path(audio).name} T={T} frames, 模型(Demucs-OnsetNet) 踩点 onsets={len(onsets)}")

    vae = ChartVAE().to(DEVICE).eval()
    vae.load_state_dict(torch.load(os.path.join(CKPT, "vae.pt"), map_location=DEVICE))
    ddpm = DDPM().to(DEVICE).eval()
    ddpm.load_state_dict(torch.load(os.path.join(CKPT, "ddpm.pt"), map_location=DEVICE))

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

    # 摆形状模型(可选): 权重存在则加载, 让模型接管左右转向; 无权重退回几何贪心(当前行为)
    shape_model = None
    shape_ckpt = os.path.join(CKPT, "shape_model.pt")
    if os.path.exists(shape_ckpt):
        try:
            from shape_model import ShapeModel
            sd = torch.load(shape_ckpt, map_location=DEVICE)
            shape_model = ShapeModel()
            shape_model.load_state_dict(sd, strict=True)
            shape_model = shape_model.to(DEVICE).eval()
            print(f"[shape] 已加载摆形状模型 {shape_ckpt}")
        except Exception as e:
            print(f"[shape] 加载失败, 退回几何贪心: {e}")
            shape_model = None
    # 各 stem 能量必须来自 Demucs 6 通道分离 mel（(6,128,T128)），与 onset_prob 同网格。
    # 之前误用单通道全混合 mel 的 np.mean(axis=1) -> (128,) 一维，导致
    # extract_tile_features 里 stem_energy.shape[1] 索引越界、模型接管失败退回几何贪心。
    stem_energy = np.mean(np.abs(mel_onset), axis=1).astype(np.float32)  # (6, T128)
    level = dense_to_adofai(dense, global_bpm=float(base_bpm), hop_ms=HOP_MS,
                            song=Path(audio).name, onset_frames=onset_frames,
                            twirl_desire=dense[2],
                            shape_model=shape_model, onset_prob=onset_prob,
                            stem_energy=stem_energy, device=DEVICE)
    if level is None:
        print("[infer] dense_to_adofai 返回 None (onset 不足)")
        return None

    # —— 第三阶段：注入视觉特效（VFXNet 帧级预测吸附到方块）——
    n_vfx = 0
    if vfx:
        try:
            from apply_vfx import apply_vfx as _apply_vfx
            before = len(level.get("actions", []))
            level = _apply_vfx(level, audio, intensity=float(intensity), device=DEVICE)
            n_vfx = len(level.get("actions", [])) - before
            print(f"[infer] VFX 注入完成：新增 {n_vfx} 个视觉特效动作")
        except Exception as e:
            print(f"[infer] VFX 注入失败（已跳过，谱面仍可用）：{e}")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(level, ensure_ascii=False, indent=1), encoding="utf-8-sig")
    n_twirl = sum(1 for a in level.get("actions", [])
                  if isinstance(a, dict) and a.get("eventType") == "Twirl")
    print(f"[infer] wrote {out_path} | tiles={len(level['angleData'])} "
          f"| twirl={n_twirl} | vfx={n_vfx} | bpm={base_bpm}")
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
    ap.add_argument("--vfx", action="store_true",
                    help="生成时加入视觉特效（VFXNet 帧级预测吸附到方块）")
    ap.add_argument("--intensity", type=float, default=0.5,
                    help="视觉特效强度 0..1（越大越密、幅度越狠）")
    ap.add_argument("--auto_bpm", action="store_true",
                    help="用 Beat This! 估计真实 BPM 替换 --bpm（默认写死值）")
    ap.add_argument("--beat_grid", action="store_true",
                    help="把节拍相位条件拼入 OnsetNet（需先 --beat_grid 重训 onset_net_beatgrid.pt）")
    a = ap.parse_args()
    generate(a.audio, a.out, a.bpm, a.steps, a.guidance, a.track,
             vfx=a.vfx, intensity=a.intensity, auto_bpm=a.auto_bpm, beat_grid=a.beat_grid)


if __name__ == "__main__":
    main()
