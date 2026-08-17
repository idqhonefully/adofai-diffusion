"""train_onset.py — 训练踩点神经网络 OnsetNet

监督信号：train/ 里每首歌的 adofai 经 chart_repr.adofai_to_dense 产出的
C0 onset 热图（= 用户谱面里每个 tile 的真实时间点，落在 mel 帧网格上）。
模型学"这首歌哪里该踩点"，替代旧的信号检测器。

用法（在 venv 里，已含 CUDA torch）：
  venv/Scripts/python.exe app/training/train_onset.py
可加 --epochs 80 --chunk 512 --stride 256
"""
from __future__ import annotations
import os, sys, glob, json, argparse
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.utils.data as td

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "training"))
os.environ.setdefault("MKL_THREADING_LAYER", "sequential")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("KMP_AFFINITY", "disabled")
torch.set_num_threads(1)

import paths
import librosa
from adofai_parse import load_adofai
from chart_repr import adofai_to_dense, SR, HOP
from onset_net import OnsetNet, normalize_mel
from demucs_mel import demucs_mel, HOP_MS_ONSET, HOP_ONSET, STEMS, release_sep, _cache_path

N_MELS = 128
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"


def _mel(y):
    # 仅作兜底/调试用（单通道）。正式训练走 demucs_mel（多通道 + hop128）。
    mel = librosa.feature.melspectrogram(y=y, sr=SR, n_fft=2048, hop_length=HOP_ONSET, n_mels=N_MELS)
    return np.log1p(mel).astype(np.float32)      # (128, T) @ hop128


def _pairs(train_dir):
    pairs = []
    for d in glob.glob(train_dir + "/*/"):
        og = sorted(glob.glob(d + "*.ogg") + glob.glob(d + "*.mp3"))
        ad = sorted(glob.glob(d + "*.adofai"))
        if og and ad:
            pairs.append((og[0], ad[0]))
    return pairs


def build_samples(train_dir, chunk=512, stride=256):
    """返回 (mels:list[(128,chunk)], targets:list[(1,chunk)], pos_weight)。"""
    mels, targets = [], []
    np_sum = 0.0
    np_count = 0
    pairs = _pairs(train_dir)
    total = len(pairs)
    for idx, (ogg, adf) in enumerate(pairs):
        try:
            lvl = load_adofai(adf)
            if not isinstance(lvl, dict):
                print(f"[demucs] 跳过 {idx+1}/{total} (非合法谱面): {os.path.basename(ogg)}")
                continue
            # 6 通道 Demucs 分离 mel @ hop128（首跑分离并缓存，之后走缓存秒读）
            _cp = _cache_path(ogg)
            if _cp and os.path.exists(_cp):
                print(f"[demucs] 缓存命中 {idx+1}/{total}: {os.path.basename(ogg)}", flush=True)
            else:
                print(f"[demucs] 分离 {idx+1}/{total}: {os.path.basename(ogg)}", flush=True)
            mel = demucs_mel(ogg, device=DEVICE)   # (C=6, 128, T) @ hop128
            T = mel.shape[2]                        # 时间轴在最后一维
            # 真值 onset 热图也在 hop128 网格上，与 mel 逐帧对齐
            dense = adofai_to_dense(lvl, T, hop_ms=HOP_MS_ONSET)  # (3, T) @ hop128
            if dense.sum() == 0:
                continue
            onset = dense[0:1]                 # (1, T) 真值热图
            for s in range(0, max(1, T - chunk) + 1, stride):
                if s + chunk <= T:
                    mels.append(mel[:, :, s:s + chunk])   # (6,128,chunk) 时间轴在最后一维
                    targets.append(onset[:, s:s + chunk])  # (1,chunk) 时间轴在最后一维
                    np_sum += float(onset[:, s:s + chunk].sum())
                    np_count += chunk
        except Exception as e:
            print(f"[warn] skip {os.path.basename(ogg)}: {e}")
            continue
    release_sep()   # 分离完成, 立即释放 Demucs 模型(占 ~1.2G 显存), 避免后续训练 OOM
    if np_count:
        frac_pos = np_sum / np_count
        pos_weight = min(100.0, max(1.0, (1.0 - frac_pos) / max(frac_pos, 1e-4)))
        print(f"[data] 样本数={len(mels)}  正类占比≈{frac_pos:.4f}  pos_weight={pos_weight:.1f}")
    else:
        pos_weight = 1.0
        print(f"[data] 样本数={len(mels)}  (无正类样本, pos_weight=1.0)")
    return mels, targets, pos_weight


class OnsetDataset(td.Dataset):
    def __init__(self, mels, targets):
        self.mels = mels
        self.targets = targets

    def __len__(self):
        return len(self.mels)

    def __getitem__(self, i):
        mel = torch.from_numpy(self.mels[i].astype("float32"))   # (6,128,chunk) 多通道(6=Demucs)
        tgt = torch.from_numpy(self.targets[i].astype("float32"))      # (1,chunk)
        return normalize_mel(mel), tgt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train_dir", default=os.environ.get("ADOFAI_TRAIN_DIR",
                        paths.TRAIN_DIR_DEFAULT))
    ap.add_argument("--epochs", type=int, default=80)
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--chunk", type=int, default=1024)
    ap.add_argument("--stride", type=int, default=512)
    ap.add_argument("--out", default=str(paths.CHECKPOINTS_DIR / paths.CKPT_ONSET))
    a = ap.parse_args()

    print(f"[cfg] device={DEVICE}  train_dir={a.train_dir}")
    mels, targets, pos_weight = build_samples(a.train_dir, a.chunk, a.stride)
    if not mels:
        print("[err] 没有可用训练样本"); return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()   # 再清一次, 确保 Demucs 显存彻底释放
    ds = OnsetDataset(mels, targets)
    dl = td.DataLoader(ds, batch_size=a.batch, shuffle=True, num_workers=0, drop_last=False)

    model = OnsetNet(n_mels=N_MELS, in_channels=len(STEMS)).to(DEVICE)
    n_params = sum(p.numel() for p in model.parameters())
    print(f"[model] OnsetNet 参数={n_params:,}  in_channels={len(STEMS)} (Demucs 多通道)")
    criterion = nn.BCEWithLogitsLoss(pos_weight=torch.tensor([pos_weight], device=DEVICE))
    opt = torch.optim.Adam(model.parameters(), lr=a.lr)

    import time as _time
    _t0 = _time.time()
    for ep in range(1, a.epochs + 1):
        model.train()
        tot = 0.0
        nb = len(dl)
        for bi, (mel, tgt) in enumerate(dl, 1):
            mel, tgt = mel.to(DEVICE), tgt.to(DEVICE)
            opt.zero_grad()
            logits = model(mel)                  # (B, chunk)
            loss = criterion(logits, tgt.squeeze(1))
            loss.backward()
            opt.step()
            tot += float(loss.item()) * mel.size(0)
            # 每 batch 实时进度(强制 flush, 避免被缓冲吞掉)
            print(f"  [epoch {ep:03d}/{a.epochs}] batch {bi}/{nb} loss={loss.item():.4f}", flush=True)
        avg = tot / len(ds)
        _el = _time.time() - _t0
        print(f"[epoch {ep:03d}/{a.epochs}] loss={avg:.4f}  已用 {_el/60:.1f}min", flush=True)
        if ep % 20 == 0 or ep == a.epochs:
            torch.save(model.state_dict(), a.out)
            print(f"  -> 已保存 {a.out}", flush=True)

    torch.save(model.state_dict(), a.out)
    print(f"[done] 训练完成，权重保存至 {a.out}", flush=True)


if __name__ == "__main__":
    main()
