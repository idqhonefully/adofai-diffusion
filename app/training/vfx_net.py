"""
vfx_net.py — 视觉特效神经网络 (VFXNet)

架构（复用 OnsetNet 已验证骨架）：
  输入 (B, 10, 128, T)：
     6 通道 Demucs 梅尔（音乐音色/分乐器） + 4 通道方块节奏辅助（aux 沿频率维广播）
  2D 卷积（仅频率维池化，时间分辨率 T 不变）
     → 双层双向 GRU（沿时间建模节奏/段落上下文）
     → 共享特征 (B, T, 2H)
    四路多任务头：
     - event_head : (B,T,V) 每帧每类视觉事件对数几率（独立 sigmoid = 多标签）
     - param_head : (B,T,V,3) 每类事件关键参数（duration/强度/旋转），仅在该类事件处监督
     - filt_head  : (B,T,F) 滤镜类别（仅 SetFilter/SetFilterAdvanced 事件处监督）
     - easing_head: (B,T,V,E) 每类事件每帧缓动类别（所有事件处监督，默认 Linear）

为什么帧级多标签（与 OnsetNet 一致）：
  人类谱与生成谱方块不同，但时间轴一致。视觉事件按 floor→tile 时间→帧对齐，
  与梅尔天然同帧。推理时把预测事件吸附到生成方块即可。

输入约定：aux 的 4 通道 (4,T) 在 forward 内广播成 (4,128,T) 再与 mel 拼接，
  所以调用方直接传 mel(6,128,T) + aux(4,T) 即可（见 make_input）。
"""
from __future__ import annotations
import torch
import torch.nn as nn


class VFXNet(nn.Module):
    def __init__(self, n_mels: int = 128, hidden: int = 128, freq_ch: int = 64,
                 mel_ch: int = 6, aux_ch: int = 4, n_events: int = 19,
                 n_filters: int = 120, n_easings: int = 41, param_dim: int = 3):
        super().__init__()
        in_channels = mel_ch + aux_ch
        self.n_events = n_events
        self.n_filters = n_filters
        self.n_easings = n_easings
        self.param_dim = param_dim
        self.conv = nn.Sequential(
            nn.Conv2d(in_channels, 16, (3, 3), padding=(1, 1)), nn.ReLU(),
            nn.Conv2d(16, 32, (3, 3), padding=(1, 1)), nn.ReLU(),
            nn.Conv2d(32, freq_ch, (3, 1), padding=(1, 0)), nn.ReLU(),
            nn.AdaptiveAvgPool2d((1, None)),   # (B, freq_ch, 1, T)
        )
        self.gru = nn.GRU(freq_ch, hidden, num_layers=2, batch_first=True,
                          bidirectional=True, dropout=0.2)
        self.event_head = nn.Linear(2 * hidden, n_events)
        self.param_head = nn.Linear(2 * hidden, n_events * param_dim)
        self.filt_head = nn.Linear(2 * hidden, n_filters)
        self.easing_head = nn.Linear(2 * hidden, n_events * n_easings)
        self.disable_head = nn.Linear(2 * hidden, 1)   # SetFilter 的 disableOthers 二分类

    def forward(self, mel, aux):
        """mel: (B, mel_ch, 128, T)  log-mel；aux: (B, aux_ch, T) 节奏辅助。
        返回 (event_logits (B,T,V), param (B,T,V,P), filt_logits (B,T,F), ease_logits (B,T,V,E))。"""
        B, _, _, T = mel.shape
        aux_e = aux.unsqueeze(2).expand(-1, -1, mel.shape[2], -1)  # (B,aux_ch,128,T)
        x = torch.cat([mel, aux_e], dim=1)                          # (B,10,128,T)
        h = self.conv(x)                       # (B, freq_ch, 1, T)
        h = h.squeeze(2).permute(0, 2, 1)      # (B, T, freq_ch)
        feat, _ = self.gru(h)                  # (B, T, 2H)
        ev = self.event_head(feat)             # (B, T, V)
        pa = self.param_head(feat).view(B, T, self.n_events, self.param_dim)
        fl = self.filt_head(feat)              # (B, T, F)
        ez = self.easing_head(feat).view(B, T, self.n_events, self.n_easings)
        dis = self.disable_head(feat).squeeze(-1)     # (B, T) 对数几率
        return ev, pa, fl, ez, dis


def make_input(mel_np, aux_np, device="cpu"):
    """mel_np (6,128,T) float32, aux_np (4,T) float32 -> 归一化后的模型输入张量。
    逐样本对 mel 标准化（与 OnsetNet 同思路）。"""
    import numpy as np
    mel = torch.from_numpy(mel_np.astype("float32"))[None]              # (1,6,128,T)
    aux = torch.from_numpy(aux_np.astype("float32"))[None]             # (1,4,T)
    # mel 逐样本标准化（仅 mel 通道）
    mean = mel.mean(dim=(-2, -1), keepdim=True)
    std = mel.std(dim=(-2, -1), keepdim=True)
    mel = (mel - mean) / (std + 1e-6)
    return mel.to(device), aux.to(device)


def predict_vfx(model, mel_np, aux_np, device="cpu", event_thr=0.35):
        """推理：返回 (event_prob (V,T), param (V,3,T), filt_prob (F,T),
        ease_prob (V,E,T), disable_prob (T,))。"""
        import numpy as np
        model = model.to(device).eval()
        with torch.no_grad():
            mel_t, aux_t = make_input(mel_np, aux_np, device)
            ev, pa, fl, ez, dis = model(mel_t, aux_t)
            ev = torch.sigmoid(ev)[0].cpu().numpy().astype("float32")      # (T,V)
            pa = pa[0].cpu().numpy().astype("float32")                    # (T,V,P)
            fl = torch.softmax(fl, dim=-1)[0].cpu().numpy().astype("float32")  # (T,F)
            ez = torch.softmax(ez, dim=-1)[0].cpu().numpy().astype("float32")  # (T,V,E)
            dis = torch.sigmoid(dis)[0].cpu().numpy().astype("float32")    # (T,)
        # 转置成 (V,T) / (V,3,T) / (V,E,T) 便于按事件索引
        return ev.T, np.transpose(pa, (1, 2, 0)), fl.T, np.transpose(ez, (1, 2, 0)), dis
