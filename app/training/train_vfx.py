"""
train_vfx.py — VFXNet 训练（重写版：配合稀疏 / 精选标签）

数据：D:/ADOFAI_AI_Mug/vision/best（用户精选谱面）
目标缓存：extract_vfx.py 产出的 data/vfx_cache/*.npz（multi/params/filt/aux）

设计哲学（与旧版的根本区别）
----------------------------
旧版多标签 + 高 pos_weight 补偿，让模型"能激活的全激活"，推理铺满、杂乱。
新版（配合 extract_vfx 的稀疏标签）：
  - 标签已经每帧最多 2 个正事件（_sparsify），模型学的是"每刻只挑最重要的 1~2 个"。
  - pos_weight 不再用 cap=100 的强补偿（那会逼模型多激活），改为 cap=20：
    标签已稀疏，弱补偿即可，重点是让模型敢在真正该放处激活、其余保持 0。
  - 监督 filt 头：extract 里 SetFilter 类已存真实滤镜索引（受控白名单），
    CrossEntropy(ignore_index=-1) 只在 SetFilter 事件处监督 -> 模型学会"放什么滤镜"。
  - 监督 easing 头：extract 里每事件每帧已存缓动索引（默认 Linear=0），
    CrossEntropy 在事件激活处监督 -> 模型学会"这格用哪种缓动"，生成端不再写死。
  - aux 只在 Twirl 重音处给提示（见 extract_vfx），模型不会见块就放。

输入 = 6 通道 Demucs 梅尔 + 4 通道方块节奏辅助（VFXNet.forward 内拼接）。
多任务损失：
      event : BCEWithLogits + 逐类 pos_weight（cap=20，弱补偿）
      param : MSE，仅在该类事件处(mask)监督（pa 头精度有限，幅度由推理端兜底）
      filt  : CrossEntropy(ignore_index=-1)，仅 SetFilter 事件处监督
      ease  : CrossEntropy，事件激活处监督（含 Linear 默认）
设备：get_safe_device()（老显卡自动退回 CPU）。

用法：
  python train_vfx.py --data D:/ADOFAI_AI_Mug/vision/best --cache data/vfx_cache --epochs 60
（注意：本脚本只写训练逻辑。运行前需先用 extract_vfx.py 重新生成 vfx_cache。）
"""
from __future__ import annotations
import os, sys, json, argparse, glob, re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "training"))

import numpy as np
import torch
import torch.nn as nn
import torch.utils.data as td

from extract_vfx import (VFX_EVENTS, _pick_chart, _audio_of, collect_filter_names,
                         EVENT_INDEX, V, P)
from demucs_mel import demucs_mel, SR, HOP
from device_util import get_safe_device
from vfx_net import VFXNet
# 参数向量语义（[mag, spatial, rotation]）现由 effects_schema 统一定义，
# 训练目标里的 params 即由其 extract_params 产出（MoveCamera 的 zoom/position/
# rotation 等真实物理量）。此处断言维度一致，避免标签与模型错位。
from effects_schema import (P as SCHEMA_P, build_action as _schema_build,
                             FILTER_TYPES, EASING, N_EASINGS)

CHUNK = 4096          # 必须被 16 整除（若将来接 VAE 下采样）；此处仅切块
STRIDE = 3072
N_FILTERS = len(FILTER_TYPES)   # 受控滤镜白名单长度（120）

# pos_weight 上限：旧版 100 太激进（逼模型多激活）。标签已稀疏，弱补偿即可。
POS_WEIGHT_CAP = 20.0


def _norm_mel(mel):
    """mel (C,128,T) 逐样本标准化。"""
    m = mel.mean(axis=(-2, -1), keepdims=True)
    s = mel.std(axis=(-2, -1), keepdims=True)
    return (mel - m) / (s + 1e-6)


class VFXDataset(td.Dataset):
    def __init__(self, best_dir, cache_dir, seq_len=2048, stride=1536,
                 device="cpu", rebuild_mel=False):
        self.seq_len = seq_len
        self.stride = stride
        self.items = []      # (mel_path, tgt_path, Tmin)
        self.samples = []    # (idx, start)
        best_dir = str(best_dir); cache_dir = str(cache_dir)
        os.makedirs(cache_dir, exist_ok=True)
        _, fidx = collect_filter_names(best_dir, N_FILTERS)
        self.fidx = fidx
        folders = sorted(glob.glob(os.path.join(best_dir, "*")))
        folders = [d for d in folders if os.path.isdir(d)]
        for d in folders:
            adf = _pick_chart(d); aud = _audio_of(d)
            song = os.path.basename(d)
            multi_path = os.path.join(cache_dir, song + ".multi.npy")
            if not (adf and aud and os.path.exists(multi_path)):
                continue
            mel_path = os.path.join(cache_dir, song + ".mel.npy")
            if rebuild_mel or not os.path.exists(mel_path):
                try:
                    mel6 = demucs_mel(aud, device=device)   # (6,128,Tm)
                except Exception as e:
                    print(f"  [skip] {song} demucs 失败: {e}")
                    continue
                np.save(mel_path, mel6.astype(np.float32))
            # 读 target / mel 时间长度，按较小者对齐，保证 mel 与 target 帧一致
            try:
                Tt = int(np.load(multi_path, mmap_mode="r").shape[1])
                Tm = int(np.load(mel_path, mmap_mode="r").shape[2])
            except Exception:
                continue
            Tmin = min(Tm, Tt)
            if Tmin < 16:
                continue
            self.items.append((mel_path, multi_path, Tmin))
        for i, (_, _, Tmin) in enumerate(self.items):
            if Tmin <= self.seq_len:
                self.samples.append((i, 0))
            else:
                for s in range(0, Tmin - self.seq_len + 1, self.stride):
                    self.samples.append((i, s))
        print(f"[vfx-ds] 载入 {len(self.items)} 首，{len(self.samples)} 样本块")

    def __len__(self):
        return len(self.samples)

    def __getitem__(self, idx):
        i, s = self.samples[idx]
        mel_path, _, _ = self.items[i]
        mel = np.load(mel_path, mmap_mode="r")             # (6,128,Tm) 未压缩 .npy（OS 页缓存）
        base = mel_path[:-len(".mel.npy")]                 # .../<song>
        multi = np.load(base + ".multi.npy", mmap_mode="r")
        params = np.load(base + ".params.npy", mmap_mode="r")
        filt = np.load(base + ".filt.npy", mmap_mode="r")
        ease = np.load(base + ".ease.npy", mmap_mode="r")
        dis = np.load(base + ".dis.npy", mmap_mode="r")
        aux = np.load(base + ".aux.npy", mmap_mode="r")
        L = self.seq_len
        length = min(s + L, mel.shape[2], multi.shape[1]) - s
        # 固定长度 buffer（不足部分 pad 0），保证 batch 内时间维一致可 stack
        mel_c = np.zeros((6, 128, L), np.float32)
        aux_c = np.zeros((4, L), np.float32)
        multi_c = np.zeros((V, L), np.float32)
        params_c = np.zeros((V, P, L), np.float32)
        filt_c = np.zeros((L,), np.int64)
        ease_c = np.zeros((V, L), np.int64)
        dis_c = np.zeros((L,), np.int64)
        if length > 0:
            mel_c[:, :, :length] = np.asarray(mel[:, :, s:s + length], dtype=np.float32)
            aux_c[:, :length] = np.asarray(aux[:, s:s + length], dtype=np.float32)
            multi_c[:, :length] = np.asarray(multi[:, s:s + length], dtype=np.float32)
            params_c[:, :, :length] = np.asarray(params[:, :, s:s + length], dtype=np.float32)
            fi = EVENT_INDEX["SetFilter"]; fa = EVENT_INDEX["SetFilterAdvanced"]
            ff = np.asarray(filt[fi, s:s + length], dtype=np.int64)
            ff2 = np.asarray(filt[fa, s:s + length], dtype=np.int64)
            filt_c[:length] = np.where(ff >= 0, ff, ff2)
            dd = np.asarray(dis[fi, s:s + length], dtype=np.int64)
            dd2 = np.asarray(dis[fa, s:s + length], dtype=np.int64)
            dis_c[:length] = np.maximum(dd, dd2)   # 任一滤镜类要求 disable 即 disable
            ease_c[:, :length] = np.asarray(ease[:, s:s + length], dtype=np.int64)
        mel_c = _norm_mel(mel_c)
        return (torch.from_numpy(mel_c), torch.from_numpy(aux_c),
                torch.from_numpy(multi_c), torch.from_numpy(params_c),
                torch.from_numpy(filt_c), torch.from_numpy(ease_c),
                torch.from_numpy(dis_c))


def compute_pos_weight(dataset):
    """遍历统计每类事件正样本占比，返回逐类 pos_weight（cap 20）。"""
    pos = np.zeros(V, np.float64)
    tot = 0
    for _, multi_path, _ in dataset.items:
        multi = np.load(multi_path, mmap_mode="r")
        pos += (multi > 0.5).sum(axis=1)
        tot += multi.shape[1]
    frac = pos / max(tot, 1)
    pw = (1.0 - frac) / np.maximum(frac, 1e-6)
    pw = np.clip(pw, 1.0, POS_WEIGHT_CAP)
    return torch.tensor(pw, dtype=torch.float32)


def compute_disable_pos_weight(dataset):
    """统计 SetFilter/SetFilterAdvanced 帧中 disableOthers 正样本占比，返回 pos_weight（cap 20）。
    训练数据里「禁用其他滤镜」是少数类（多数帧 disableOthers=False），不加 pos_weight 模型会
    塌缩成恒输出 0、永远不禁用 -> 滤镜无限叠加成灰泥（见 apply_vfx 注释）。加 pos_weight 抵消
    不平衡，模型才学得会正确时机禁用。"""
    fi = EVENT_INDEX["SetFilter"]; fa = EVENT_INDEX["SetFilterAdvanced"]
    pos = 0; tot = 0
    for mel_path, multi_path, _ in dataset.items:
        base = multi_path[:-len(".multi.npy")]
        try:
            dis = np.load(base + ".dis.npy", mmap_mode="r")
        except Exception:
            continue
        d = np.maximum(np.asarray(dis[fi], dtype=np.int64),
                       np.asarray(dis[fa], dtype=np.int64))
        pos += int((d > 0).sum()); tot += int(d.size)
    frac = pos / max(tot, 1)
    pw = (1.0 - frac) / max(frac, 1e-6)
    return torch.tensor(float(np.clip(pw, 1.0, 20.0)), dtype=torch.float32)


def train(best_dir, cache_dir, epochs=60, batch=8, lr=1e-3, device="cpu"):
    ds = VFXDataset(best_dir, cache_dir, device=device)
    if len(ds) == 0:
        print("[vfx] 无可用样本，退出"); return
    assert P == SCHEMA_P, f"param_dim 必须与 effects_schema 一致({SCHEMA_P})，现为 {P}"
    pos_weight = compute_pos_weight(ds).to(device)
    print(f"[vfx] pos_weight: {['%.1f' % x for x in pos_weight.tolist()]}")
    dl = td.DataLoader(ds, batch_size=batch, shuffle=True, num_workers=4,
                       pin_memory=True, drop_last=False)
    model = VFXNet(n_events=V, n_filters=N_FILTERS, n_easings=N_EASINGS,
                   param_dim=P).to(device)
    # 续训：优先从最近一轮的 epoch 检查点接（防中途崩了从头重来）；否则加载 vfx_net.pt 作初始化
    ckpt_dir = os.path.join(os.environ.get("ADOFAI_DATA_DIR",
                     str(ROOT / "data")), "checkpoints")
    os.makedirs(ckpt_dir, exist_ok=True)
    start_ep = 0
    ep_ckpts = sorted(glob.glob(os.path.join(ckpt_dir, "vfx_net_ep*.pt")),
                      key=lambda p: int(re.search(r"vfx_net_ep(\d+)\.pt", p).group(1)))
    if ep_ckpts:
        latest = ep_ckpts[-1]
        try:
            sd = torch.load(latest, map_location=device)
            model.load_state_dict(sd, strict=False)
            start_ep = int(re.search(r"vfx_net_ep(\d+)\.pt", latest).group(1))
            print(f"[vfx] 已从 epoch 检查点 {os.path.basename(latest)} 续训（起点 ep {start_ep+1}）")
        except Exception as e:
            print(f"[vfx] 读取 epoch 检查点失败，改从 vfx_net.pt 初始化: {e}")
    if start_ep == 0 and os.path.exists(os.path.join(ckpt_dir, "vfx_net.pt")):
        try:
            sd = torch.load(os.path.join(ckpt_dir, "vfx_net.pt"), map_location=device)
            model.load_state_dict(sd, strict=False)   # disable_head 为新加，旧权重无此键
            print(f"[vfx] 已从 vfx_net.pt 加载权重继续训练（新增 disable_head + 轨道角度学习）")
        except Exception as e:
            print(f"[vfx] 加载权重失败，改为从头训练: {e}")
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    # AMP 混合精度：CUDA 上开启，显存砍半 + 吃满 TensorCore，约 1.5~2x 提速
    dev_str = str(device)
    use_amp = dev_str.startswith("cuda")
    scaler = torch.amp.GradScaler(enabled=use_amp)
    amp_ctx = torch.amp.autocast(device_type="cuda", enabled=use_amp)
    if use_amp:
        print("[vfx] AMP 混合精度已开启（fp16）")
    disable_pw = compute_disable_pos_weight(ds).to(device)
    print(f"[vfx] disable_pos_weight: {disable_pw.item():.2f}")
    event_crit = nn.BCEWithLogitsLoss(pos_weight=pos_weight)
    param_crit = nn.MSELoss(reduction="none")
    filt_crit = nn.CrossEntropyLoss(ignore_index=-1)   # SetFilter 类监督，无滤镜=-1 跳过
    ease_crit = nn.CrossEntropyLoss()                  # 缓动监督（含 Linear 默认=0）
    disable_crit = nn.BCEWithLogitsLoss(pos_weight=disable_pw)  # SetFilter 的 disableOthers 二分类

    gstep = 0
    for ep in range(start_ep, epochs):
        model.train()
        run = {"ev": 0.0, "pa": 0.0, "fl": 0.0, "ez": 0.0, "ds": 0.0, "n": 0}
        for step, (mel, aux, multi, params, filt, ease, dis) in enumerate(dl):
            mel, aux = mel.to(device), aux.to(device)
            multi, params = multi.to(device), params.to(device)
            filt, ease, dis = filt.to(device), ease.to(device), dis.to(device)
            with amp_ctx:
                ev, pa, fl, ez, dis_logits = model(mel, aux)
                tgt_ev = torch.clamp(multi, 0.0, 1.0).transpose(1, 2)   # (B,T,V)
                tgt_pa = params.permute(0, 3, 1, 2)                     # (B,T,V,P)
                loss_ev = event_crit(ev, tgt_ev)
                # param：仅在事件处监督
                mask = (tgt_ev > 0.5).unsqueeze(-1)           # (B,T,V,1)
                loss_pa = (param_crit(pa, tgt_pa) * mask).sum() / max(mask.sum(), 1.0)
                # filt：仅 SetFilter 事件处监督（filt=-1 的格 ignore）
                loss_fl = filt_crit(fl.reshape(-1, N_FILTERS), filt.reshape(-1))
                # ease：仅在事件激活处监督（含 Linear=0）
                act = (tgt_ev > 0.5).reshape(-1)             # (B*T*V,)
                ez_f = ez.reshape(-1, N_EASINGS)
                ez_t = ease.reshape(-1)
                loss_ez = (ease_crit(ez_f, ez_t) * act).sum() / max(act.sum(), 1.0)
                # disable：SetFilter 的 disableOthers 二分类（BCE）
                loss_ds = disable_crit(dis_logits.reshape(-1), dis.reshape(-1).float())
                loss = loss_ev + 0.5 * loss_pa + 1.0 * loss_fl + 0.5 * loss_ez + 0.5 * loss_ds
            opt.zero_grad()
            scaler.scale(loss).backward()
            scaler.step(opt)
            scaler.update()
            run["ev"] += loss_ev.item(); run["pa"] += loss_pa.item()
            run["fl"] += loss_fl.item(); run["ez"] += loss_ez.item(); run["ds"] += loss_ds.item()
            run["n"] += 1
            gstep += 1
            if step % 50 == 0:
                print(f"[vfx] ep {ep+1:3d}/{epochs} step {gstep:5d} "
                      f"ev={loss_ev.item():.4f} pa={loss_pa.item():.4f} "
                      f"fl={loss_fl.item():.4f} ez={loss_ez.item():.4f} ds={loss_ds.item():.4f}", flush=True)
        print(f"[vfx] ep {ep+1:3d}/{epochs}  ev={run['ev']/run['n']:.4f} "
              f"pa={run['pa']/run['n']:.4f} fl={run['fl']/run['n']:.4f} "
              f"ez={run['ez']/run['n']:.4f} ds={run['ds']/run['n']:.4f}", flush=True)
        # 每轮落盘：epoch 检查点 + 同步刷新 vfx_net.pt（生成端永远用最新），中途崩了可从最近一轮接
        ep_ckpt = os.path.join(ckpt_dir, f"vfx_net_ep{ep+1:03d}.pt")
        torch.save(model.state_dict(), ep_ckpt)
        torch.save(model.state_dict(), os.path.join(ckpt_dir, "vfx_net.pt"))
        print(f"[vfx] 已保存检查点 -> {ep_ckpt}（及 vfx_net.pt）", flush=True)
    out = os.path.join(os.environ.get("ADOFAI_DATA_DIR",
                     str(ROOT / "data")), "checkpoints", "vfx_net.pt")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    torch.save(model.state_dict(), out)
    print(f"[vfx] 已保存权重 -> {out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=r"D:/ADOFAI_AI_Mug/vision/best")
    ap.add_argument("--cache", default=r"D:/ADOFAI_AI_Mug/new_last_128/portable/data/vfx_cache")
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--batch", type=int, default=12)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--rebuild-mel", action="store_true")
    a = ap.parse_args()
    device = get_safe_device()
    print(f"[vfx] device = {device}")
    train(a.data, a.cache, a.epochs, a.batch, a.lr, device)


if __name__ == "__main__":
    main()
