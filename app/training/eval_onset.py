"""eval_onset.py — 诚实评测踩点质量（模型 vs 真值 vs 信号检测器）

真值 = 用户 train/ 里 adofai 的 tile 时间点（经 adofai_to_dense C0 热图 >0.5 的帧）。
对比：
  - OnsetNet 模型踩点
  - 旧信号检测器（detect_onset_frames）
指标：在 ±tol 帧容差内的 命中率 / 精确率 / 召回 / F1 / 平均时间误差(ms)。
"""
from __future__ import annotations
import os, sys, glob, argparse
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "training"))

import paths
import librosa
from adofai_parse import load_adofai
from chart_repr import adofai_to_dense, SR, HOP, HOP_MS
from onset_net import OnsetNet, normalize_mel, predict_onset_frames
from demucs_mel import demucs_mel, HOP_MS_ONSET, HOP_ONSET, STEMS


def _mel(y):
    mel = librosa.feature.melspectrogram(y=y, sr=SR, n_fft=2048, hop_length=HOP, n_mels=128)
    return np.log1p(mel).astype(np.float32)


def _signal_onsets(mel):
    mag = mel.mean(0)
    flux = np.maximum(0.0, np.diff(mag))
    flux = np.convolve(flux, np.ones(3) / 3.0, mode="same")
    peak = float(flux.max())
    if peak < 1e-6:
        return []
    thr = 0.18 * peak
    frames, last = [], -10
    for f in range(1, len(flux) - 1):
        if flux[f] >= flux[f - 1] and flux[f] > flux[f + 1] and flux[f] > thr:
            if f - last >= 2:
                frames.append(f); last = f
    return frames


def _gt_frames(adf, T, hop_ms=HOP_MS_ONSET):
    lvl = load_adofai(adf)
    dense = adofai_to_dense(lvl, T, hop_ms=hop_ms)
    return np.where(dense[0] > 0.5)[0].tolist()


def _f1(pred, gt, tol, hop_ms=HOP_MS_ONSET):
    if not gt:
        return 0.0, 0.0, 0.0, 0.0, 0.0
    gt = np.array(sorted(gt))
    pred = np.array(sorted(pred)) if pred else np.array([])
    matched_gt = set()
    tp = 0
    errs = []
    for p in pred:
        d = np.abs(gt - p)
        j = int(np.argmin(d))
        if d[j] <= tol and j not in matched_gt:
            tp += 1
            matched_gt.add(j)
            errs.append(float(d[j]) * hop_ms)
    fp = len(pred) - tp
    fn = len(gt) - tp
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    mean_err = float(np.mean(errs)) if errs else 0.0
    return prec, rec, f1, float(tp), mean_err


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train_dir", default=os.environ.get("ADOFAI_TRAIN_DIR", paths.TRAIN_DIR_DEFAULT))
    ap.add_argument("--n", type=int, default=6, help="评测前 N 首")
    ap.add_argument("--tol", type=int, default=4, help="容差帧数(±, hop128网格≈±23ms)")
    ap.add_argument("--ckpt", default=str(paths.CHECKPOINTS_DIR / paths.CKPT_ONSET))
    a = ap.parse_args()

    pairs = []
    for d in glob.glob(a.train_dir + "/*/"):
        og = sorted(glob.glob(d + "*.ogg") + glob.glob(d + "*.mp3"))
        ad = sorted(glob.glob(d + "*.adofai"))
        if og and ad:
            pairs.append((og[0], ad[0]))
    pairs = pairs[:a.n]

    net = None
    if os.path.exists(a.ckpt):
        net = OnsetNet(n_mels=128, in_channels=len(STEMS)).eval()
        net.load_state_dict(torch.load(a.ckpt, map_location="cpu"))
        print(f"[eval] 已加载踩点模型 {a.ckpt} (in_channels={len(STEMS)}, Demucs 6 通道)\n")
    else:
        print(f"[eval] 未找到 {a.ckpt}，仅评测信号检测器\n")

    print(f"{'歌曲':<26} {'GT':>4} | {'模型P/R/F1':>16} {'err':>7} | {'信号P/R/F1':>16}")
    mP = mR = mF = mE = sP = sR = sF = 0.0
    nn = 0
    for ogg, adf in pairs:
        name = Path(ogg).parent.name
        # Demucs 6 通道 mel @ hop128 —— 模型与信号检测器都用它(公平对比)
        mel6 = demucs_mel(ogg)                  # (6,128,T128)
        T128 = mel6.shape[2]                     # 时间轴在最后一维
        gt = _gt_frames(adf, T128)              # 真值 @ hop128
        sig = _signal_onsets(mel6[4])           # 信号检测器跑在 full 通道 @ hop128
        if net is not None:
            mpred, _ = predict_onset_frames(net, mel6, device="cpu")
            pP, pR, pF, _, pE = _f1(mpred, gt, a.tol)
            mP += pP; mR += pR; mF += pF; mE += pE; nn += 1
            mp = f"{pP:.2f}/{pR:.2f}/{pF:.2f}"
        else:
            mp = "  -  "
        sP_, sR_, sF_, _, sE_ = _f1(sig, gt, a.tol)
        sP += sP_; sR += sR_; sF += sF_
        print(f"{name[:25]:<26} {len(gt):>4} | {mp:>16} {pE if net else 0:>7.1f} | "
              f"{sP_:.2f}/{sR_:.2f}/{sF_:.2f}")
    if nn:
        print(f"\n[均值] 模型(Demucs6ch): P={mP/nn:.3f} R={mR/nn:.3f} F1={mF/nn:.3f} err={mE/nn:.1f}ms | "
              f"信号: P={sP/nn:.3f} R={sR/nn:.3f} F1={sF/nn:.3f}")


if __name__ == "__main__":
    main()
