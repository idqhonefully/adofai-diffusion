# -*- coding: utf-8 -*-
"""precompute_bsr_mel.py — 用【原版 BS-Roformer (PyTorch + CUDA)】离线分离训练歌，
预计算 OnsetNet 训练用的 7 通道 mel，落盘到 BSR_CACHE_DIR。

关键：直接用原版 torch 模型（bs_roformer.utils.demix_track），不走 onnxruntime 慢路径。
      7 通道 mel 完全复用 osn1_pipeline.build_7ch_mel（与训练端 bsr_mel.STEMS 逐字节一致）。

🔴🔴 chunk_size 是速度/显存的命门（实测 onnx_poc/_vram_probe.py，RTX 4070 8GB）：
    yaml 默认 588800(13.35s) → 单块峰值 7.03GB、单块前向 9.44s（整曲 ≈1.4x 实时，比实时还慢！）
    352256( 7.99s) → 单块峰值 3.32GB、单块前向 1.49s（整曲 ≈0.37x 实时）← 默认取这个
    176128( 3.99s) → 单块峰值 1.57GB、单块前向 0.65s（整曲 ≈0.33x 实时）
    ⇒ 588800 会顶满 8GB 并触发慢路径，必须降下来。num_overlap 保持 2（降 1 会让块边界掉音量污染 mel）。

🔴🔴 chunk_size 必须是 STFT hop_length(512) 的整数倍：
    BSRoformer 内部 stft hop=512，会把输出长度裁到 floor(T/512)*512。
    demix_track 的 windowing_array 长度却是 C —— 不整除时二者对不上，直接
    RuntimeError: The size of tensor a (352768) must match tensor b (352800)。
    可用值：512×344=176128(3.99s)、512×688=352256(7.99s)、512×1150=588800(13.35s)。
    改 chunk_size 时务必用 512 的整数倍。

与训练端 bsr_mel.py 的 cache key 必须一致（sha1(abspath|size|mtime|BSR7H128)[:16]）。

用法（audio-sep 运行时，含 bs_roformer + torch + CUDA）：
  venv/Scripts/python.exe precompute_bsr_mel.py
  venv/Scripts/python.exe precompute_bsr_mel.py --dirs melody --limit 3
  venv/Scripts/python.exe precompute_bsr_mel.py --force        # 重算已存在的
  venv/Scripts/python.exe precompute_bsr_mel.py --chunk_size 176400
"""
from __future__ import annotations
import os
import sys
import time
import hashlib
import argparse
import subprocess

import numpy as np
import torch
import yaml
import librosa
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import osn1_pipeline as P  # build_7ch_mel / SR / BSR_SRC

from ml_collections import ConfigDict
from bs_roformer.utils import get_model_from_config, demix_track

# ---------------- 模型（原版 BS-RoFormer-SW）----------------
MODEL_DIR = os.path.join(HERE, "models", "bsroformer",
                         "roformer-model-bs-roformer-sw-by-jarredou")
CKPT = os.path.join(MODEL_DIR, "BS-Rofo-SW-Fixed.ckpt")
CFGY = os.path.join(MODEL_DIR, "BS-Rofo-SW-Fixed.yaml")

# ---------------- 缓存 / 数据 ----------------
CACHE_DIR = os.environ.get("BSR_CACHE_DIR",
                           r"<REPO>\audio-sep\bsr_train_cache")
TRAIN_ROOT = os.environ.get("BSR_TRAIN_ROOT",
                           os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "..", "precompute_train_root"))
TARGET_SR = 44100          # BSR 模型采样率
AUD_EXT = (".ogg", ".mp3", ".wav", ".flac")

BSR_SRC = ["bass", "drums", "other", "vocals", "guitar", "piano"]

# mel 帧 → 秒（22050/128 = 172.27fps ⇒ 每帧 5.805ms）
SEC_PER_FRAME = 128.0 / P.SR


# ============================ cache key（与 bsr_mel.py 完全一致）============================
def bsr_cache_path(audio_path, cache_dir=CACHE_DIR):
    try:
        st = os.stat(audio_path)
        h = hashlib.sha1(
            f"{os.path.abspath(audio_path)}|{st.st_size}|{st.st_mtime:.3f}|BSR7H128".encode()
        ).hexdigest()[:16]
        return os.path.join(cache_dir, h + ".npy")
    except Exception:
        return None


# ============================ 模型加载（一次）============================
VERBOSE = True  # 子进程(--single)模式置 False，避免重复打印 [model] 行


def load_model(chunk_size=352256, num_overlap=2):
    raw = yaml.safe_load(open(CFGY, encoding="utf-8"))
    raw["model"]["flash_attn"] = False      # 走纯 einsum，避开 SDPA（与导出脚本一致）
    # 🔴 覆盖 yaml 默认 chunk_size（588800 在 8GB 上会顶满 + 走慢路径）
    raw.setdefault("inference", {})
    raw["inference"]["chunk_size"] = int(chunk_size)
    raw["inference"]["num_overlap"] = int(num_overlap)
    config = ConfigDict(raw)
    model = get_model_from_config("bs_roformer", config)
    ck = torch.load(CKPT, map_location="cpu", weights_only=False)
    missing, unexpected = model.load_state_dict(ck, strict=False)
    model.eval()
    model.cuda()
    torch.backends.cudnn.benchmark = True
    if VERBOSE:
        print(f"[model] 加载完成 missing={len(missing)} unexpected={len(unexpected)} "
          f"stems={len(model.mask_estimators)} "
          f"chunk_size={config.inference.chunk_size}({config.inference.chunk_size/TARGET_SR:.2f}s) "
          f"num_overlap={config.inference.num_overlap}", flush=True)
    return config, model


# ============================ 单首分离 → 7ch mel ============================
def separate_to_7ch_mel(audio_path, config, model):
    y, sr = sf.read(audio_path, always_2d=True)        # (T,2)
    y = y.T.astype(np.float32)                          # (2,T)
    if sr != TARGET_SR:
        y = librosa.resample(y, orig_sr=sr, target_sr=TARGET_SR)
    mono = y.mean(axis=0)
    y_full22 = librosa.resample(mono, orig_sr=TARGET_SR, target_sr=P.SR)  # 原混音@22050

    mix_t = torch.from_numpy(np.ascontiguousarray(y)).float()   # (2,T) @44100
    # demix_track 返回 (dict{name:(2,T)}, first_chunk_time)
    sources, _ = demix_track(config, model, mix_t, "cuda")
    stems44 = {name: np.asarray(sources[name], dtype=np.float32).mean(axis=0)
               for name in BSR_SRC}                      # 每路 mono @44100
    mel7 = P.build_7ch_mel(stems44, y_full22)           # (7,128,T)
    return mel7


# ============================ 单首处理（子进程 --single 模式）============================
def process_single(audio_path, chunk_size, num_overlap, cache_dir, force=False):
    """被父进程以子进程方式调用：加载模型→分离→落缓存→退出码 0/1。

    每首歌一个全新进程：彻底隔离 CUDA 显存碎片/长程累积状态（实测长进程跑
    20+ 首后会因状态累积直接崩进程，而全新子进程必成功）。"""
    global CACHE_DIR, VERBOSE
    CACHE_DIR = cache_dir
    VERBOSE = False  # 子进程不打重复的 [model] 加载行
    cp = bsr_cache_path(audio_path)
    if cp and os.path.exists(cp) and not force:
        print(f"[skip] {os.path.basename(audio_path)} 已缓存", flush=True)
        return 0
    try:
        config, model = load_model(chunk_size, num_overlap)
        if torch.cuda.is_available():
            torch.cuda.reset_peak_memory_stats()
        t1 = time.time()
        mel7 = separate_to_7ch_mel(audio_path, config, model)
        if mel7.ndim != 3 or mel7.shape[0] != 7 or mel7.shape[1] != 128:
            raise ValueError(f"mel 形状异常 {mel7.shape}")
        if np.isnan(mel7).any():
            raise ValueError("mel 含 NaN")
        np.save(cp, mel7.astype(np.float32))
        dt = time.time() - t1
        audio_sec = mel7.shape[-1] * SEC_PER_FRAME
        peak = (torch.cuda.max_memory_allocated() / 1024 ** 3) if torch.cuda.is_available() else 0.0
        print(f"[ok] {os.path.basename(audio_path)} mel={mel7.shape} 音频={audio_sec:.0f}s "
              f"用时={dt:.1f}s ({dt/audio_sec:.3f}x实时) 峰值显存={peak:.2f}GB", flush=True)
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {audio_path}: {type(e).__name__}: {e}", flush=True)
        return 1


# ============================ 歌曲发现（与 train_onset_bsr._pairs 一致）============================
def discover_audio(train_dirs):
    paths = []
    seen = set()
    for td_ in train_dirs:
        if not os.path.isdir(td_):
            print(f"[discover] 跳过不存在目录: {td_}")
            continue
        for name in sorted(os.listdir(td_)):
            d = os.path.join(td_, name)
            if not os.path.isdir(d):
                continue
            key = os.path.basename(os.path.normpath(d))
            if key in seen:
                continue
            og = sorted([os.path.join(d, f) for f in os.listdir(d)
                         if f.lower().endswith(AUD_EXT)])
            ad = sorted([os.path.join(d, f) for f in os.listdir(d)
                         if f.lower().endswith(".adofai")])
            if og and ad:
                seen.add(key)
                paths.append(og[0])     # 与 _pairs 取 og[0] 一致 → cache key 命中
    return paths


def main():
    global CACHE_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--dirs", default="melody;vocal",
                    help="训练子目录，分号分隔。默认 melody;vocal 全量。")
    ap.add_argument("--limit", type=int, default=0, help="仅处理前 N 首（冒烟用）。")
    ap.add_argument("--force", action="store_true", help="重算已存在的缓存。")
    ap.add_argument("--cache_dir", default=CACHE_DIR)
    ap.add_argument("--chunk_size", type=int, default=352256,
                    help="BSR 推理块长（采样点@44100）。必须是 512 的整数倍！"
                         "8GB 卡推荐 352256(7.99s) 或 176128(3.99s)。")
    ap.add_argument("--num_overlap", type=int, default=2,
                    help="块重叠倍数。1=不重叠（快但块边界会掉音量污染 mel），保持 2。")
    ap.add_argument("--single", default="",
                    help="仅处理单首（子进程模式），传音频绝对路径；配合父进程派发使用。")
    ap.add_argument("--song_timeout", type=float, default=900.0,
                    help="单首子进程超时(秒)：超过则杀掉该子进程并记 FAIL（看门狗）。默认 900。")
    ap.add_argument("--retries", type=int, default=1,
                    help="单首失败/超时后的重试次数（默认 1）。")
    a = ap.parse_args()

    # ---- 单首子进程模式 ----
    if a.single:
        rc = process_single(a.single, a.chunk_size, a.num_overlap, a.cache_dir, a.force)
        raise SystemExit(rc)

    # ---- 父进程：逐首派发子进程 + 看门狗 ----
    CACHE_DIR = a.cache_dir
    os.makedirs(CACHE_DIR, exist_ok=True)

    dirs = [os.path.join(TRAIN_ROOT, x) for x in a.dirs.split(";")]
    paths = discover_audio(dirs)
    if a.limit:
        paths = paths[:a.limit]
    print(f"[discover] 共 {len(paths)} 首待处理（dirs={a.dirs}）", flush=True)
    if not paths:
        return

    done = skip = fail = 0
    t0 = time.time()
    py = sys.executable
    n = len(paths)
    for i, ap_ in enumerate(paths, 1):
        cp = bsr_cache_path(ap_)
        if cp and os.path.exists(cp) and not a.force:
            skip += 1
            continue
        name = os.path.basename(ap_)
        ok = False
        for attempt in range(1 + a.retries):
            try:
                proc = subprocess.run(
                    [py, __file__, "--single", ap_,
                     "--cache_dir", CACHE_DIR,
                     "--chunk_size", str(a.chunk_size),
                     "--num_overlap", str(a.num_overlap)]
                    + (["--force"] if a.force else []),
                    timeout=a.song_timeout,
                    # 继承父进程 stdout/stderr → 子进程 [ok]/[FAIL] 进同一日志
                )
                if proc.returncode == 0:
                    ok = True
                    break
                print(f"[retry {attempt+1}/{a.retries}] {i}/{n} {name} "
                      f"rc={proc.returncode}", flush=True)
            except subprocess.TimeoutExpired:
                # 子进程超时（卡死/极端慢）→ 杀掉（subprocess 已 TerminateProcess）
                print(f"[WATCHDOG] 第 {i}/{n} 首 {name} 超过 {a.song_timeout:.0f}s "
                      f"无响应，已杀掉子进程", flush=True)
                break
        if ok:
            done += 1
        else:
            fail += 1
            print(f"[FAIL] {i}/{n} {ap_}: 子进程失败/超时（重试 {a.retries} 次均失败）",
                  flush=True)

    print(f"\n[done] 处理={n} 新算={done} 跳过={skip} 失败={fail} "
          f"用时 {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
