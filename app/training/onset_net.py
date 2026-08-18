"""onset_net.py — 踩点神经网络 (OnsetNet)

架构：2D 卷积(在 频率×时间 上提特征, 仅对频率维池化以保留时间分辨率)
      + 双向 GRU(沿时间建模节奏上下文) + 线性头 -> 每帧 onset 概率。

输入：log-mel 谱 (B, C, 128, T)，C=in_channels（单通道=1；Demucs 多分离=6）
输出：每帧 onset 对数几率 (B, T)，sigmoid 后∈[0,1]，峰值处=音头。

这是用户心里的"双模型"里负责【踩点】的那个模型；
另一个模型(VAE+扩散)负责【加事件】(Twirl/转角)。
训练目标 = 用户 adofai 经 chart_repr.adofai_to_dense 产出的 C0 onset 热图
(即用户谱面里每个 tile 的真实时间点)，由此学"这首歌哪里该踩点"。
"""
from __future__ import annotations
import torch
import torch.nn as nn


class OnsetNet(nn.Module):
    def __init__(self, n_mels: int = 128, hidden: int = 128, freq_ch: int = 64,
                 in_channels: int = 1):
        super().__init__()
        # 2D 卷积：在 (频率, 时间) 平面提特征。
        # 关键：只用 (freq,1) 卷积 + 对频率维 AdaptiveAvgPool -> 时间分辨率 T 不变，
        # 保证输出帧与输入 mel 帧一一对应（踩点要精确到帧）。
        # in_channels: 输入通道数(默认1=单通道mel；Demucs 方案用 6=多乐器分离通道)。
        self.in_channels = in_channels
        self.conv = nn.Sequential(
            nn.Conv2d(in_channels, 16, (3, 3), padding=(1, 1)), nn.ReLU(),
            nn.Conv2d(16, 32, (3, 3), padding=(1, 1)), nn.ReLU(),
            nn.Conv2d(32, freq_ch, (3, 1), padding=(1, 0)), nn.ReLU(),
            nn.AdaptiveAvgPool2d((1, None)),   # (B, freq_ch, 1, T)
        )
        self.gru = nn.GRU(freq_ch, hidden, num_layers=2, batch_first=True,
                          bidirectional=True, dropout=0.2)
        self.head = nn.Linear(2 * hidden, 1)

    def forward(self, x):
        """x: (B, 1, 128, T) -> (B, T) 每帧 onset 对数几率。"""
        h = self.conv(x)                 # (B, freq_ch, 1, T)
        h = h.squeeze(2).permute(0, 2, 1)  # (B, T, freq_ch)
        out, _ = self.gru(h)             # (B, T, 2*hidden)
        return self.head(out).squeeze(-1)  # (B, T)


def normalize_mel(mel: torch.Tensor) -> torch.Tensor:
    """逐样本标准化 log-mel，使尺度稳定（训练/推理共用）。"""
    mean = mel.mean(dim=(-2, -1), keepdim=True)
    std = mel.std(dim=(-2, -1), keepdim=True)
    return (mel - mean) / (std + 1e-6)


@torch.no_grad()
def predict_onset_frames(model, mel_np, thr: float = 0.3, min_dist: int = 2,
                         thr_local: float = 0.3, local_win: int = 64,
                         device: str = "cpu"):
    """用训练好的 OnsetNet 从多通道 log-mel 预测 onset 帧下标列表。

    输入 mel_np 形状:
      - (128, T)       单通道 -> 视作 (1,128,T)
      - (C, 128, T)    多通道(如 Demucs 6 通道) -> 视作 (C,128,T)
    流程：标准化 -> 送入模型 -> sigmoid 得概率 -> 自适应峰值挑选。
    返回 (frames:list[int], prob_np:(T,) )。帧下标在输入 mel 的 T 网格上。
    这是真正"模型踩点"：格子位置完全由神经网络给出，不再是信号检测器。
    """
    import numpy as np
    if mel_np.ndim == 2:
        x = torch.from_numpy(mel_np.astype("float32"))[None, None]      # (1,1,128,T)
    else:
        x = torch.from_numpy(mel_np.astype("float32"))[None]            # (1,C,128,T)
    x = normalize_mel(x).to(device)
    model = model.to(device).eval()
    logits = model(x)[0]                     # (T,)
    prob = torch.sigmoid(logits).cpu().numpy().astype("float32")
    T = prob.shape[0]
    # 峰值提取：全局相对阈值保最强段，额外叠加【滑动窗口局部相对阈值】，
    # 避免中段弱奏被全局最强段(peak)压制成 0.3×peak 而整段被吞（"中段跳踩好多"的根因）。
    peak = float(prob.max())
    if peak < 1e-6:
        return [], prob
    th_global = max(thr * peak, 0.04)
    win = max(8, int(local_win))
    frames, last = [], -10 ** 9
    for f in range(1, T - 1):
        if prob[f] >= prob[f - 1] and prob[f] > prob[f + 1]:
            # 局部窗口峰值 -> 局部自适应阈值（静音段仍被 0.04 下限拦住）
            lo, hi = max(0, f - win), min(T, f + win + 1)
            win_peak = float(prob[lo:hi].max())
            th_local = max(thr_local * win_peak, 0.04)
            # 取全局/局部较松者：全局峰值全保留 + 局部显著峰补回中段漏检
            if prob[f] > th_global or prob[f] > th_local:
                if f - last < min_dist:
                    continue
                frames.append(int(f))
                last = f
    return frames, prob
