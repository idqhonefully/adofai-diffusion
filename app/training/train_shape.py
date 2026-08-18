"""
train_shape.py — 训练「摆形状」模型(ShapeModel) on traindata。

从人工谱面(traindata 里的 .ogg+.adofai 配对)学习每格左右转向：
  - 标签：angleData 逐格转向符号(shortest(angleData[i]-angleData[i-1]) 符号)
  - 特征：OnsetNet 概率 + Demucs 各 stem 能量 + BPM 节拍相位(见 shape_model.extract_tile_features)
训练/推理特征完全一致 -> 学到的"走向风格"可迁移到新歌的 OnsetNet 检测格上。

用法:
  venv/Scripts/python.exe app/training/train_shape.py \
      --data "D:/ADOFAI_AI_Mug/new_last/portable/train" \
      --epochs 80 --out "D:/ADOFAI_AI_Mug/new_last_128/portable/data/checkpoints/shape_model.pt"

注：本脚本会跑 OnsetNet + Demucs 处理全部训练歌，较耗时；按用户铁律，启动需用户明确授权。
"""
from __future__ import annotations
import os
import sys
import re
import json
import argparse
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # portable/
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
TRAIN_CODE = os.path.join(ROOT, "app", "training")
if TRAIN_CODE not in sys.path:
    sys.path.insert(0, TRAIN_CODE)

from timing_engine import compute_note_times
from chart_repr import HOP_MS, _to_angle_data
from onset_net import OnsetNet, predict_onset_frames
from demucs_mel import demucs_mel
from shape_model import ShapeModel, extract_tile_features

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
CKPT_DIR = os.path.join(ROOT, "data", "checkpoints")
FEAT_CACHE = os.path.join(ROOT, "data", "shape_feat_cache")


def _load_json_tolerant(path):
    """容错加载 ADOFAI .adofai：兼容尾逗号/单引号/行注释等非标准 JSON。

    训练集很多谱面是第三方/编辑器导出的非标准 JSON（带尾逗号、单引号键等），
    标准 json.load 直接报错被跳过 -> 训练样本暴减。这里逐级容错兜底。
    """
    raw = open(path, encoding="utf-8-sig").read()
    # 1) 标准
    try:
        return json.loads(raw)
    except Exception:
        pass
    # 2) 去尾逗号（JS / ADOFAI 编辑器常见）
    t = re.sub(r",\s*([}\]])", r"\1", raw)
    try:
        return json.loads(t)
    except Exception:
        pass
    # 3) 去 // 行注释
    t2 = re.sub(r"//[^\n]*", "", t)
    try:
        return json.loads(t2)
    except Exception:
        pass
    # 4) 单引号 -> 双引号（大多数字符串内无单引号冲突）
    t3 = t2.replace("'", '"')
    try:
        return json.loads(t3)
    except Exception:
        pass
    raise ValueError(f"无法解析 JSON: {path}")


def _find_pairs(data_dir):
    pairs = []
    for name in sorted(os.listdir(data_dir)):
        sub = os.path.join(data_dir, name)
        if not os.path.isdir(sub):
            continue
        ogg = ado = None
        for fn in os.listdir(sub):
            low = fn.lower()
            if low.endswith(".ogg") or low.endswith(".mp3") or low.endswith(".wav"):
                ogg = os.path.join(sub, fn)
            elif low.endswith(".adofai"):
                ado = os.path.join(sub, fn)
        if ogg and ado:
            pairs.append((ogg, ado))
    return pairs


def _load_labels_and_features(ogg, ado, onset_net):
    import json, hashlib
    # 特征缓存：demucs+onset 仅首跑计算，重训直接秒加载
    _fcp = None
    try:
        so = os.stat(ogg); sa = os.stat(ado)
        _fh = hashlib.sha1(
            f"{os.path.abspath(ogg)}|{so.st_size}|{so.st_mtime:.3f}|"
            f"{os.path.abspath(ado)}|{sa.st_size}|{sa.st_mtime:.3f}|shape".encode()
        ).hexdigest()[:16]
        _fcp = os.path.join(FEAT_CACHE, _fh + ".npz")
        if _fcp and os.path.exists(_fcp):
            _d = np.load(_fcp)
            return (_d["feats"].astype(np.float32), _d["labels"].astype(np.float32),
                    float(_d["bpm"]))
    except Exception:
        pass
    lvl = _load_json_tolerant(ado)
    ad = _to_angle_data(lvl)
    settings = lvl.get("settings") or {}
    actions = lvl.get("actions") or []
    bpm = float(settings.get("bpm", 120.0))
    if not ad:
        return None
    try:
        nt = compute_note_times(ad, settings, actions, add_offset=True)
    except Exception:
        return None
    if not nt or len(nt) < 4:
        return None
    times = [float(x[0]) if isinstance(x, (tuple, list)) else float(x) for x in nt]
    n = len(ad)
    # 逐格转向标签
    labels = np.zeros(n, dtype=np.float32)
    for i in range(1, n):
        d = ((ad[i] - ad[i - 1] + 180.0) % 360.0) - 180.0
        labels[i] = 1.0 if d >= 0 else -1.0
    labels[0] = labels[1] if n > 1 else 0.0
    tile_frames = [max(0, int(round(t / HOP_MS))) for t in times]
    # 特征：OnsetNet 概率 + Demucs 能量（CPU 稳定，避免 GPU 非法内存访问毒死上下文）
    mel = demucs_mel(ogg, device="cpu")            # (6,128,T)
    if mel is None:
        return None
    T = mel.shape[2]
    onset_prob = np.zeros(T, dtype=np.float32)
    try:
        _, prob = predict_onset_frames(onset_net, mel, device="cpu")
        if prob is not None and len(prob) == T:
            onset_prob = prob.astype(np.float32)
    except Exception as e:
        print(f"  [warn] OnsetNet 失败 {os.path.basename(ogg)}: {e}")
    stem_energy = np.mean(np.abs(mel), axis=1).astype(np.float32)  # (6,T)
    feats = extract_tile_features(tile_frames, onset_prob, stem_energy, bpm, HOP_MS)
    if feats.shape[0] != n:
        # 帧取整导致 1 格偏差，截断对齐
        m = min(feats.shape[0], n)
        feats = feats[:m]; labels = labels[:m]
    if feats.shape[0] < 4:
        return None
    feats = feats.astype(np.float32); labels = labels.astype(np.float32)
    if _fcp:
        try:
            os.makedirs(FEAT_CACHE, exist_ok=True)
            np.savez(_fcp, feats=feats, labels=labels, bpm=np.float32(bpm))
        except Exception:
            pass
    return feats, labels, bpm


class ShapeDataset(Dataset):
    def __init__(self, pairs, onset_net, limit=None):
        self.items = []
        for i, (ogg, ado) in enumerate(pairs):
            if limit and i >= limit:
                break
            try:
                r = _load_labels_and_features(ogg, ado, onset_net)
            except Exception as e:
                print(f"  [skip] {os.path.basename(ado)}: {e}")
                continue
            if r is None:
                continue
            feats, labels, bpm = r
            self.items.append((feats, labels))
            print(f"  + {os.path.basename(ado)}  tiles={len(labels)}")

    def __len__(self):
        return len(self.items)

    def __getitem__(self, idx):
        return self.items[idx]


def _collate(batch):
    feats, labels = zip(*batch)
    L = max(f.shape[0] for f in feats)
    F = feats[0].shape[1]
    x = np.zeros((len(batch), L, F), dtype=np.float32)
    y = np.zeros((len(batch), L), dtype=np.float32)
    mask = np.zeros((len(batch), L), dtype=np.float32)
    for i, (f, l) in enumerate(zip(feats, labels)):
        x[i, :f.shape[0]] = f
        y[i, :l.shape[0]] = l
        mask[i, :l.shape[0]] = 1.0
    return torch.from_numpy(x), torch.from_numpy(y), torch.from_numpy(mask)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.join(ROOT, "..", "new_last", "portable", "train"))
    ap.add_argument("--epochs", type=int, default=80)
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--out", default=os.path.join(CKPT_DIR, "shape_model.pt"))
    ap.add_argument("--limit", type=int, default=0, help="仅前 N 首(冒烟测试用)")
    args = ap.parse_args()

    print(f"[train_shape] device={DEVICE} data={args.data}")
    pairs = _find_pairs(args.data)
    print(f"[train_shape] 找到 {len(pairs)} 对训练数据")
    if not pairs:
        print("无训练数据，退出"); return

    print("[train_shape] 加载 OnsetNet 权重...")
    onset_net = OnsetNet(in_channels=6)
    op = os.path.join(CKPT_DIR, "onset_net.pt")
    if os.path.exists(op):
        onset_net.load_state_dict(torch.load(op, map_location="cpu"))
    # 特征提取用 CPU：OnsetNet 在 GPU 上对个别长歌会触发 illegal memory access，
    # 一旦出错会毒死整个 CUDA 上下文，导致后续训练全崩。CPU 稳定不崩。
    onset_net = onset_net.to("cpu").eval()
    print(f"[train_shape] OnsetNet 已加载(device=cpu，特征提取稳定)")

    ds = ShapeDataset(pairs, onset_net, limit=args.limit or None)
    if len(ds) == 0:
        print("[train_shape] 无可用样本，退出"); return
    dl = DataLoader(ds, batch_size=args.batch, shuffle=True, collate_fn=_collate)

    # 训练模型放 GPU（特征提取已全在 CPU，上下文干净）；GPU 不可用则退 CPU
    try:
        model = ShapeModel().to(DEVICE)
        train_device = DEVICE
    except Exception as e:
        print(f"[train_shape] GPU 建模型失败，退 CPU: {e}")
        model = ShapeModel().to("cpu")
        train_device = "cpu"
    print(f"[train_shape] ShapeModel 训练设备: {train_device}")
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    crit = nn.BCEWithLogitsLoss(reduction="none")

    print(f"[train_shape] 开始训练 {args.epochs} 轮, 样本={len(ds)}")
    for ep in range(args.epochs):
        model.train()
        tot = 0.0; cnt = 0
        for x, y, mask in dl:
            x, y, mask = x.to(train_device), y.to(train_device), mask.to(train_device)
            logit = model(x).reshape(y.shape)
            tgt = (y + 1.0) / 2.0  # +1/-1 -> 1/0
            loss = (crit(logit, tgt) * mask).sum() / mask.sum().clamp(min=1.0)
            opt.zero_grad(); loss.backward(); opt.step()
            tot += loss.item() * mask.sum().item(); cnt += mask.sum().item()
        print(f"[train_shape] ep {ep+1}/{args.epochs}  loss={tot/max(cnt,1):.4f}")
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    torch.save(model.state_dict(), args.out)
    print(f"[train_shape] 已保存 -> {args.out}")


if __name__ == "__main__":
    main()
