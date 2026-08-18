"""precompute_vfx_mel.py — 预计算 VFXNet 训练所需的 6 通道 Demucs 梅尔缓存。

仅生成 <song>.mel.npz 到 cache 目录，不碰目标 .npz（目标由 extract_vfx 产出）。
目的：把耗时的 demucs 分离提前跑完并验证每首都能产出【全长度】mel，
      避免训练时数据集构建卡在某一首、且能打印每首形状/错误便于定位。

用法：
  python precompute_vfx_mel.py --data D:/ADOFAI_AI_Mug/vision/best \
       --cache D:/ADOFAI_AI_Mug/new_last_128/portable/data/vfx_cache
"""
from __future__ import annotations
import os, sys, glob, time, argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "training"))

import numpy as np
from extract_vfx import _pick_chart, _audio_of
from demucs_mel import demucs_mel


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=r"D:/ADOFAI_AI_Mug/vision/best")
    ap.add_argument("--cache", default=r"D:/ADOFAI_AI_Mug/new_last_128/portable/data/vfx_cache")
    ap.add_argument("--device", default="cuda")
    a = ap.parse_args()
    data_dir = a.data
    cache_dir = a.cache
    os.makedirs(cache_dir, exist_ok=True)

    folders = sorted(glob.glob(os.path.join(data_dir, "*")))
    folders = [d for d in folders if os.path.isdir(d)]
    print(f"[precompute] 待处理 {len(folders)} 首")

    ok = 0
    skip = 0
    t0 = time.time()
    for d in folders:
        song = os.path.basename(d)
        mel_path = os.path.join(cache_dir, song + ".mel.npy")
        adf = _pick_chart(d)
        aud = _audio_of(d)
        if not adf or not aud:
            print(f"  [skip] {song}: 缺 chart/audio")
            skip += 1
            continue
        try:
            ts = time.time()
            mel = demucs_mel(aud, device=a.device)   # (6,128,T) 或从 demucs_cache 命中
            np.save(mel_path, mel.astype(np.float32))  # 未压缩 .npy（避免训练时重复解压）
            dt = time.time() - ts
            ok += 1
            print(f"  [ok] {song:24} shape={mel.shape} {dt:5.1f}s  ({ok}/{len(folders)})")
        except Exception as e:
            print(f"  [ERROR] {song}: {e}")
            skip += 1
    print(f"\n[precompute] 完成：成功 {ok}，跳过 {skip}，耗时 {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
