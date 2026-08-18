"""
shape_model.py — 「摆形状」模型：学习人工谱面的路径走向(每格左/右转)。

设计(2026-08-13 晚, 用户要求"再添加一个模型让他学习摆形状, 学 traindata")：
  - 输入：每格(方块)的特征向量 (F=8)
        [0] onset_prob      该格 OnsetNet 踩点概率(帧级)
        [1] sin(2π·拍相位)  音乐节拍相位(0~1)
        [2] cos(2π·拍相位)
        [3] 小节相位        (拍时间 / 4拍) mod 1
        [4] drums 能量(归一) Demucs 鼓点能量(窗口均值)
        [5] bass  能量(归一) Demucs 贝斯能量
        [6] other 能量(归一) 其余 stem 能量
        [7] 局部间隔(归一)   到下一格的拍数 /4
    —— 训练与推理用完全相同的特征(都来自 OnsetNet 概率 + Demucs 能量 + BPM)，
       故 train/inference 特征分布一致。
  - 输出：每格一个 logit；>=0 -> 往左(d2=+1)，<0 -> 往右(d2=-1)。
    chart_repr.plan_path_twirl 用 turn_sign 接管左右(几何自交仍一票否决兜底)。
  - 标签：人工谱 angleData 的逐格转向符号(shortest(angleData[i]-angleData[i-1]) 的符号)。
    （注：绝对左右符号约定若实测相反，翻转 SIGN_FLIP 即可，不影响学习结构。）

模型：2 层双向 GRU + 线性头。序列模型(前后文重要：螺旋/连续同向拐成图案而非逐格乱拐)。
"""
from __future__ import annotations
import numpy as np
import torch
import torch.nn as nn


class ShapeModel(nn.Module):
    def __init__(self, feat_dim: int = 8, hidden: int = 64):
        super().__init__()
        self.gru = nn.GRU(feat_dim, hidden, num_layers=2, batch_first=True,
                          bidirectional=True, dropout=0.2)
        self.head = nn.Linear(hidden * 2, 1)

    def forward(self, x):
        # x: (B, T, F) -> (B, T, 1)
        out, _ = self.gru(x)
        return self.head(out)


# 若实测发现模型学的左右与期望相反，置 True（单次全局翻转，不影响训练结构）。
SIGN_FLIP = False


def extract_tile_features(tile_frames, onset_prob, stem_energy, bpm, hop_ms, n_stems=6):
    """为每格提取 8 维特征。

    tile_frames : list[int] 每格帧下标(与谱面 kept 对齐)
    onset_prob  : (Tf,) OnsetNet 概率
    stem_energy : (n_stems, T) 各 stem 每帧能量(已 abs 均值)；None 时能量置 0
    """
    beat = 60.0 / float(bpm) if bpm and bpm > 0 else 0.5
    hms = hop_ms / 1000.0
    Tf = len(onset_prob)
    Ts = stem_energy.shape[1] if stem_energy is not None else 0
    feats = []
    n = len(tile_frames)
    for i, f in enumerate(tile_frames):
        f = max(0, min(Tf - 1, int(f)))
        op = float(onset_prob[f]) if Tf > 0 else 0.0
        a = max(0, f - 6)
        b = min(Ts - 1, f + 6) if Ts > 0 else f

        def _e(s):
            if stem_energy is None or s >= stem_energy.shape[0] or Ts == 0 or b < a:
                return 0.0
            return float(np.mean(stem_energy[s, a:b + 1]))

        drums = _e(0)
        bass = _e(1) if n_stems > 1 else 0.0
        other = float(np.mean([_e(s) for s in range(2, n_stems)])) if n_stems > 2 else 0.0
        t_sec = f * hms
        phase = (t_sec / beat) % 1.0
        bar = (t_sec / (4.0 * beat)) % 1.0
        nxt = tile_frames[i + 1] if i + 1 < n else f
        interval = max(0.0, (nxt - f) * hms) / beat
        interval = min(interval, 4.0) / 4.0
        feats.append([op, np.sin(2 * np.pi * phase), np.cos(2 * np.pi * phase),
                      bar, drums, bass, other, interval])
    arr = np.array(feats, dtype=np.float32)
    # 能量列做 z-score 归一(按列)，避免不同歌响度尺度漂移
    for c in (4, 5, 6):
        col = arr[:, c]
        if col.size and col.std() > 1e-8:
            arr[:, c] = (col - col.mean()) / (col.std() + 1e-6)
    return arr


def predict_turn_sign(model, feats, device):
    """feats:(N,F) -> (N,) +1/-1。"""
    xt = torch.from_numpy(np.asarray(feats, dtype=np.float32))
    if xt.dim() == 2:
        xt = xt.unsqueeze(0)
    xt = xt.to(device)
    model = model.to(device).eval()
    with torch.no_grad():
        logit = model(xt)[0].cpu().numpy().reshape(-1)
    # 运行时去偏(2026-08-15)：模型 logit 常系统性偏正(=> 大量往左拐成螺旋)。
    # 减去中位数后再按 0 判符号，使左右分布趋近均衡，打断持续同向螺旋。
    # 对称操作，偏左/偏右都治；只搬移判决点，不破坏序列前后文结构。
    med = float(np.median(logit))
    logit = logit - med
    sign = np.where(logit >= 0, 1.0, -1.0).astype(np.float32)
    if SIGN_FLIP:
        sign = -sign
    return sign
