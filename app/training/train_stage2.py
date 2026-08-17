"""train_stage2.py — 训练 B 路 (MuG-Diffusion 风格):
  1) 训 ChartVAE (重建稠密谱面, β=1e-4 + free_bits 防塌缩)
  2) 冻结 VAE, 训 DDPM 在潜空间去噪 (梅尔谱+onset 条件, CFG)
每轮打印真实 loss, 不藏不糊。产物: vae.pt / ddpm.pt (存到 ADOFAI_DATA_DIR/checkpoints)。
"""
from __future__ import annotations
import os, sys
from pathlib import Path
import torch
import torch.nn.functional as F
torch.set_num_threads(1)   # 沙箱环境限制线程数, 防止 OpenMP/MKL 崩溃

ROOT = Path(__file__).resolve().parents[2]   # portable/
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "training"))
os.environ.setdefault("MKL_THREADING_LAYER", "sequential")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("KMP_AFFINITY", "disabled")

import paths
from dataset import DenseChartDataset
from vae import ChartVAE
from diffusion import DDPM

TRAIN_DIR = os.environ.get("ADOFAI_TRAIN_DIR", paths.TRAIN_DIR_DEFAULT)
OUT_DIR = str(paths.CHECKPOINTS_DIR)
BATCH = 8
VAE_EPOCHS = int(os.environ.get("VAE_EPOCHS", 60))
DDPM_EPOCHS = int(os.environ.get("DDPM_EPOCHS", 120))
LR = 1e-3
DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

os.makedirs(OUT_DIR, exist_ok=True)
print(f"[train] device={DEVICE}  out={OUT_DIR}")


def main():
    ds = DenseChartDataset(TRAIN_DIR)
    print(f"[train] dataset samples={len(ds)} (来自 train/ 的谱面切片)")
    if len(ds) == 0:
        print("[train] 数据集为空, 退出"); return
    dl = torch.utils.data.DataLoader(ds, batch_size=BATCH, shuffle=True, num_workers=0)

    # ---------- 1) VAE ----------
    vae = ChartVAE().to(DEVICE)
    opt = torch.optim.Adam(vae.parameters(), lr=LR)
    print(f"[train] === VAE 开始训练 ({VAE_EPOCHS} epochs) ===")
    for ep in range(VAE_EPOCHS):
        vae.train()
        tot = 0.0; n = 0
        for mel, dense, bpm, oe in dl:
            mel = mel.to(DEVICE); dense = dense.to(DEVICE)
            opt.zero_grad()
            recon, mu, lv = vae(dense)
            loss, rl, kl = ChartVAE.loss(recon, dense, mu, lv)
            loss.backward(); opt.step()
            tot += loss.item(); n += 1
        print(f"[vae] ep{ep+1:03d} loss={tot/max(n,1):.4f} (recon~{rl:.4f} kl~{kl:.4f})")
    torch.save(vae.state_dict(), os.path.join(OUT_DIR, "vae.pt"))
    print(f"[vae] saved -> {OUT_DIR}/vae.pt")

    # ---------- 2) DDPM ----------
    vae.eval()
    ddpm = DDPM().to(DEVICE)
    opt2 = torch.optim.Adam(ddpm.parameters(), lr=LR)
    print(f"[train] === DDPM 开始训练 ({DDPM_EPOCHS} epochs) ===")
    for ep in range(DDPM_EPOCHS):
        ddpm.train()
        tot = 0.0; n = 0
        for mel, dense, bpm, oe in dl:
            mel = mel.to(DEVICE); dense = dense.to(DEVICE); oe = oe.to(DEVICE)
            with torch.no_grad():
                mu, lv = vae.encode(dense)
                z0 = vae.reparam(mu, lv)          # 潜变量作为扩散目标
            opt2.zero_grad()
            loss = ddpm(z0, mel, oe, cond_drop=True)
            loss.backward(); opt2.step()
            tot += loss.item(); n += 1
        print(f"[ddpm] ep{ep+1:03d} noise_mse={tot/max(n,1):.4f}")
    torch.save(ddpm.state_dict(), os.path.join(OUT_DIR, "ddpm.pt"))
    print(f"[ddpm] saved -> {OUT_DIR}/ddpm.pt")
    print("[train] 完成。下一步用 inference_stage2.py 生成谱面。")


if __name__ == "__main__":
    main()
