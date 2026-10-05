# -*- coding: utf-8 -*-
"""osn1_stemjson.py — torch-free 移植 extract_timestamps.py（用户「我的程序」）

流程（与 extract_timestamps.py 逐条对应，不重设计）：
  1. BSR ONNX 分离 6 源(bass/drums/other/vocals/guitar/piano) @44100
  2. 全曲 7ch mel（含各 BSR 分离源 + 原曲 full 通道）喂 onset_net.onnx(综合) 一次 →
     采出的 onset 复制进 vocals / melody / piano 各一份（7ch 联合模型本意用法）
  3. 鼓/贝斯/吉他/other → librosa 频谱通量(瞬态/音头检测，无模型)
  4. 组装「时间戳 JSON」（用户 last_bak 专有格式）→ 交工作台

全程 torch-free：BSR ONNX + OnsetNet ONNX(CPU) + librosa + numpy。
fp32 无损（ONNX 即 fp32，与 .pt 数值一致，已 verify）。
"""
from __future__ import annotations
import math
import os
import sys
import json
import argparse
from pathlib import Path

import numpy as np
import librosa
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from ort_cli_shim import OrtCliShim, ORT_CLI_ENABLED, ORT_CLI_CUDA
from osn1_pipeline import (
    SR, HOP, HOP_MS,
    bsr_separate, get_bsr_session,
    detect_onsets,
    get_osn1_session, DEFAULT_OSN1_ONNX,
    build_7ch_mel,
    build_7ch_mel_focus,
)

SEP_MODEL_NAME = "bsr_roformer_sw_onnx"
# 逐轨置信度过滤阈值：≤ 此值的采点全部抹去。
# onsetnet 三轨用模型真实概率；flux 四轨用 onset_strength 包络归一化值
# （真击打高、弱瞬态低，不会误删鼓点）。
CONF_THRESHOLD = 0.2


# --------------------------------------------------------------------------- 工具
def _rms(w):
    w = np.asarray(w, dtype=np.float32)
    return 0.0 if w.size == 0 else float(np.sqrt(np.mean(w * w)))


def _to22k(sig44):
    s = librosa.resample(np.asarray(sig44, dtype=np.float32), orig_sr=44100, target_sr=SR)
    return s.astype(np.float32)


def normalize_mel(mel6):
    """逐样本标准化 log-mel（与旧版 onset_net.normalize_mel 逐字一致）：每个通道沿 (128,T)
    维做零均值/单位方差。训练/推理共用 —— 漏做会导致采点系统性偏差（2026-09-26 复盘：
    漏做旋律 onset 由 ~700 掉到 662）。"""
    mel6 = np.asarray(mel6, dtype=np.float32)
    mean = mel6.mean(axis=(-2, -1), keepdims=True)
    std = mel6.std(axis=(-2, -1), keepdims=True)
    return (mel6 - mean) / (std + 1e-6)


def onnx_onsets(mel, sess):
    """mel (C,128,T) 任意通道数（7ch 模型即 (7,128,T)）→ (onset 帧(@HOP 网格), prob(T,))。

    形状无关：直接把完整 mel 喂 ONNX，由模型输入维度决定通道数（现 C=7）。
    ONNX 已把 normalize_mel 烘焙进图，外部不再归一。
    min_dist=2 与模型训练分布对齐（与原版 predict_onset_frames 一致）。

    7ch 模型是综合 onset 检测器，喂全曲 7ch mel 即让模型自行判别各内容 onset。
    调用方（extract）喂全曲一次、结果复制到 vocals/melody/piano 三轨；不再用
    「单轨 mel 复制 ×C 喂入」的旧 hack（对 7ch 联合模型无效：通道语义已各自独立，
    复制会喂入无意义输入），也不再走「一次全曲推理 + 三路波形能量门控」（那条会
    让人声/钢琴变成乐器 onset 子集）。build_7ch_mel_focus 仅作历史/兜底保留。"""
    mel = np.ascontiguousarray(np.asarray(mel, dtype=np.float32))
    logits = sess.run(None, {"mel": mel[None]})[0][0].astype(np.float32)
    prob = 1.0 / (1.0 + np.exp(-logits))
    frames, _ = detect_onsets(prob, min_dist=2)
    return frames, prob


def spectral_flux_onsets(wave, sr, hop, delta=0.08, wait_ms=25.0,
                         pre_avg_ms=80.0, post_avg_ms=80.0,
                         pre_max_ms=20.0, post_max_ms=20.0):
    """通用瞬态检测（移植 extract_timestamps.spectral_flux_onsets）。"""
    env = librosa.onset.onset_strength(y=wave, sr=sr, hop_length=hop)
    frames = librosa.onset.onset_detect(
        onset_envelope=env, sr=sr, hop_length=hop, backtrack=True,
        pre_max=max(1, int(pre_max_ms * 1e-3 * sr / hop)),
        post_max=max(1, int(post_max_ms * 1e-3 * sr / hop)),
        pre_avg=max(1, int(pre_avg_ms * 1e-3 * sr / hop)),
        post_avg=max(1, int(post_avg_ms * 1e-3 * sr / hop)),
        delta=delta, wait=max(1, int(wait_ms * 1e-3 * sr / hop)),
    )
    return [int(f) for f in frames]


def _gate_by_energy(wave, frames, sr, hop, rel=0.01, abs_floor=3e-4):
    """砍掉落在(近)静音里的伪 onset（基于该 stem 自身波形局部 RMS）。

    自适应绝对底：max(abs_floor, rel × 本 stem 局部 RMS 中位数)。
    旧版写死 0.005（比自适应高约 10×），会砍掉安静曲/轻声段真 onset
    （实测《人是猫》砍 13.3% 真·人声 onset）。rel=0.01 / abs_floor=3e-4
    与 osn1_pipeline.gate_silent_onsets 的 _SIL_*_RMS 一致。
    """
    if not frames:
        return []
    half = max(1, int(20e-3 * sr) // 2)
    lv = np.empty(len(frames), dtype=np.float32)
    for i, f in enumerate(frames):
        c = int(f) * hop
        lo, hi = max(0, c - half), min(len(wave), c + half)
        lv[i] = 0.0 if hi <= lo else float(np.sqrt(np.mean(wave[lo:hi] ** 2)))
    ref = float(np.median(lv)) if lv.size else 0.0
    thr = max(abs_floor, rel * ref)
    return [int(f) for f, e in zip(frames, lv) if e >= thr]


def detect_vocals(waves22):
    """基于波形 RMS 比判定人声（移植 extract_timestamps.detect_vocals）。"""
    if "vocals" not in waves22:
        return False
    v = _rms(waves22["vocals"])
    instr = [_rms(waves22[n]) for n in ("drums", "bass", "guitar", "piano", "other") if n in waves22]
    if not instr:
        return False
    mx = max(instr)
    if mx <= 0:
        return False
    return (v > 0.40 * mx) and (v > 0.02)


def _sec_from_frames(frames):
    return [round(f * HOP_MS / 1000.0, 4) for f in frames]


def frame_local_energy(wave, frames, sr, hop):
    """每帧局部 RMS（@hop 网格），作 flux 轨的能量代理，与 frames 同序等长。"""
    half = max(1, int(20e-3 * sr) // 2)
    out = []
    for f in frames:
        c = int(f) * hop
        lo, hi = max(0, c - half), min(len(wave), c + half)
        out.append(0.0 if hi <= lo else float(np.sqrt(np.mean(wave[lo:hi] ** 2))))
    return out


def merge_by_bpm(frames, energy, bpm, hop_ms=HOP_MS):
    """按用户填入 bpm 算 beat_ms/24 为合并窗口，窗口内多采只保留能量最高的一个点。

    受 @hop_ms 帧网格约束：实际生效窗口 = ceil(beat_ms/24 / hop_ms) 帧（下限 1 帧）。
    例 bpm=240 → 250ms/拍 ÷24 = 10.4167ms → ceil→1 帧(≈23ms) 内相邻采点取能量最高。
    energy 与 frames 同序等长：ONNX 轨用 prob，flux 轨用 frame_local_energy 的 RMS。
    """
    frames = [int(f) for f in frames]
    if len(frames) != len(energy) or not frames:
        return frames
    if not bpm or bpm <= 0:
        return frames
    burst_ms = (60000.0 / bpm) / 24.0
    w_frames = max(1, int(math.ceil(burst_ms / hop_ms)))
    keep = []
    i, n = 0, len(frames)
    while i < n:
        j = i
        while j + 1 < n and (frames[j + 1] - frames[j]) <= w_frames:
            j += 1
        if j > i:
            best = max(range(i, j + 1), key=lambda k: energy[k])
            keep.append(frames[best])
        else:
            keep.append(frames[i])
        i = j + 1
    print(f"[stemjson] 按bpm={bpm}去密(窗口≈{w_frames * hop_ms:.1f}ms/{w_frames}帧): {n}→{len(keep)}")
    return keep


# --------------------------------------------------------------------------- 主流程
def extract(audio_path, out_path=None, bpm_hint=None, include_piano=True,
            lead_stem="auto", vocal_mode="auto"):
    audio_path = str(audio_path)
    print(f"[stemjson] 分离模型: {SEP_MODEL_NAME} | 踩点模型: onset_net.onnx(综合) | "
          f"主旋律音轨: {lead_stem} | 人声检测: {vocal_mode}")

    # —— 提前算 out_dir（用于 stems 缓存复用判断，命中则跳过 GPU 分离）——
    if out_path is None:
        _b = Path(audio_path)
        _out_eff = str(_b.parent / f"{_b.stem}_timestamps.json")
    else:
        _out_eff = out_path
    _out_dir = os.path.dirname(os.path.abspath(_out_eff))
    _STEM_NAMES = ["bass", "drums", "other", "vocals", "guitar", "piano"]

    # —— 载入原混音（@22050 单声道基准，便宜、不耗 GPU）——
    y_full22, _ = librosa.load(audio_path, sr=SR, mono=True)       # (L,)
    y_full22 = y_full22.astype(np.float32)

    # —— ① BSR ONNX 分离 6 源（带缓存复用：若 out_dir/stems 已分离过则跳过 GPU）——
    #   2026-10-03：用户重跑生成时 backend 复用同名 work_dir，此处命中缓存即跳过 4 分钟
    #   GPU 分离，直接读已落盘的 6 路 wav。原曲混音仍需读取（做 full 通道，几秒、不耗 GPU）。
    _cache_stems = os.path.join(_out_dir, "stems")
    _cache_paths = [os.path.join(_cache_stems, f"{n}.wav") for n in _STEM_NAMES]
    if all(os.path.isfile(p) and os.path.getsize(p) > 0 for p in _cache_paths):
        print(f"[stemjson] 复用已分离 stems（跳过 GPU 分离）: {_cache_stems}")
        stems44 = {}
        for _n, _p in zip(_STEM_NAMES, _cache_paths):
            _w, _ = sf.read(_p, always_2d=True)
            stems44[_n] = _w.mean(axis=1).astype(np.float32)
        n_run = 0
    else:
        y44, _ = librosa.load(audio_path, sr=44100, mono=False)        # (2, N44)
        if y44.ndim == 1:
            y44 = np.stack([y44, y44], axis=0)
        if y44.shape[0] == 1:
            y44 = np.repeat(y44, 2, axis=0)
        y44 = y44.astype(np.float32)
        bsr = get_bsr_session()
        stems44, n_run = bsr_separate(y44, bsr)
        print(f"[stemjson] BSR 分离完成：{n_run} chunk，6 源 = {list(stems44.keys())}")

    # —— 转 22050（每源独立，不合并）—— 鼓/贝斯/吉他/other 用分离轨做频谱通量
    waves22 = {k: _to22k(v) for k, v in stems44.items()}
    # 旋律 OnsetNet 的 7 通道输入来自 BSR 真实 6 分离 + 原曲 full（见下方 build_7ch_mel），
    # 不再用 6× 原曲拷贝；BSR 分轨同时供鼓/贝斯/吉他/other 频谱通量（见下方循环）。
    # 人声/旋律/钢琴三轨共用全曲一次模型输出（复制），统一走 onset_net.onnx。

    # —— 人声是否存在 ——
    if vocal_mode == "off":
        has_vocals = False
    elif vocal_mode == "force":
        has_vocals = True
    else:
        has_vocals = detect_vocals(waves22)
    print(f"[stemjson] 人声检测: {'有' if has_vocals else '无'}")

    # —— 主旋律音轨选择（auto = 按能量挑 other/piano/guitar 最强）——
    if lead_stem == "auto":
        cand = [n for n in ("other", "piano", "guitar") if n in waves22]
        lead = max(cand, key=lambda n: _rms(waves22[n])) if cand else (
            "other" if "other" in waves22 else next(iter(waves22)))
    else:
        lead = lead_stem if lead_stem in waves22 else "other"

    # —— 旋律/人声/钢琴：全曲喂 onset_net 一次，结果复制到三轨（用户指定设计）——
    #   设计：全曲 7ch mel（含各 BSR 分离源 + 原曲 full 通道）喂模型 → 采出的 onset
    #   复制进 vocals / melody / piano 各一份（完全相同）；鼓/贝斯/吉他/other 走频谱
    #   通量(音头检测)单独输出（见下）。这是 7ch 联合模型的本意用法：各通道语义独立，
    #   喂全曲即让模型自行判别各内容 onset，不必 per-stem 三路各跑（那样反而把人声压成
    #   乐器 onset 子集、且多耗 3× 推理）。
    mel_ckpt = "onset_net.onnx"
    mel_sess = get_osn1_session(DEFAULT_OSN1_ONNX)

    # 全曲 7ch mel（不聚焦任何单轨）
    mel7 = build_7ch_mel(stems44, y_full22)                 # (7,128,T) 全曲
    f_full, p_full = onnx_onsets(mel7, mel_sess)
    # 能量门控（按全曲混音局部 RMS）+ 按 bpm 去密
    _e_map = {int(f): (float(p_full[f]) if 0 <= int(f) < len(p_full) else 0.0) for f in f_full}
    _main_frames = _gate_by_energy(y_full22, [int(f) for f in f_full], SR, HOP)
    # ★ 置信度过滤（onsetnet 真实概率）：≤ 阈值的弱 onset 全部抹去
    _n0 = len(_main_frames)
    _main_frames = [f for f in _main_frames if _e_map.get(f, 0.0) > CONF_THRESHOLD]
    print(f"[stemjson] 主旋律 onsetnet 概率过滤(≤{CONF_THRESHOLD}): {_n0}→{len(_main_frames)}")
    _main_energy = [_e_map[f] for f in _main_frames]
    _main_frames = merge_by_bpm(_main_frames, _main_energy, bpm_hint)
    print(f"[stemjson] 主旋律(OSN1 onset_net @ 7ch 全曲) 踩点: {len(_main_frames)}")

    # 复制三份：vocals / melody / piano 各自一份完全相同的 onset
    _mf_i = [int(f) for f in _main_frames]
    _mf_prob = [round(float(_e_map[f]), 4) for f in _main_frames]
    result_stems = {}
    result_stems["melody"] = {
        "source": "full_mix", "method": "onsetnet", "model": mel_ckpt,
        "onsets_sec": _sec_from_frames(_mf_i),
        "onsets_frame": _mf_i,
        "onsets_prob": _mf_prob,
    }
    # 人声：全曲模型已含人声 onset，直接复制（不再 per-stem，不再丢独立轨）
    result_stems["vocals"] = {
        "source": "full_mix", "method": "onsetnet", "model": mel_ckpt,
        "onsets_sec": _sec_from_frames(_mf_i),
        "onsets_frame": list(_mf_i),
        "onsets_prob": list(_mf_prob),
    }
    if include_piano and "piano" in waves22:
        result_stems["piano"] = {
            "source": "full_mix", "method": "onsetnet", "model": mel_ckpt,
            "onsets_sec": _sec_from_frames(_mf_i),
            "onsets_frame": list(_mf_i),
            "onsets_prob": list(_mf_prob),
        }

    # —— 鼓 / 贝斯 / 吉他 / other：频谱通量(音头检测) ——
    for nm in ("drums", "bass", "guitar", "other"):
        if nm not in waves22:
            continue
        fr = spectral_flux_onsets(waves22[nm], SR, HOP)
        fr = _gate_by_energy(waves22[nm], fr, SR, HOP)
        _env = librosa.onset.onset_strength(y=waves22[nm], sr=SR, hop_length=HOP)
        _env_size = _env.size
        # ★ 修复置信度（2026-10-05）：
        #   旧实现拿 spectral_flux_onsets(backtrack=True) 返回的「回溯帧」(onset 起点，
        #   此时包络≈0) 上的 env 去除以全曲 max —— 真鼓点被算成≈0、整套被误删
        #   （实测鼓 409 点→0、贝斯 183→3）。正确做法：在 onset 附近 ±12ms 取**局部峰值**
        #   包络，再用稳健的 p95 归一（不让个别超强击打把其余真击打压到阈值下）。
        _w = max(1, int(12e-3 * SR / HOP))          # ±12ms 找局部峰值窗口
        _env_p95 = float(np.percentile(_env, 95)) if _env_size else 1.0
        _env_p95 = _env_p95 if _env_p95 > 0 else 1.0
        def _local_conf(f):
            fi = int(f)
            lo, hi = max(0, fi - _w), min(_env_size, fi + _w + 1)
            _pk = float(_env[lo:hi].max()) if hi > lo else 0.0
            return _pk / _env_p95
        fr_conf = [_local_conf(f) for f in fr]
        # ★ 置信度过滤（≤ 阈值的弱瞬态全部抹去，真鼓点保留）
        _n0 = len(fr)
        fr_keep = [f for f, c in zip(fr, fr_conf) if c > CONF_THRESHOLD]
        keep_conf = [c for f, c in zip(fr, fr_conf) if c > CONF_THRESHOLD]
        print(f"[stemjson] {nm} 频谱通量置信度过滤(≤{CONF_THRESHOLD}): {_n0}→{len(fr_keep)}")
        fr = merge_by_bpm(fr_keep, keep_conf, bpm_hint)
        # merge 后重算置信度（局部峰值 / p95，与上面同口径）
        _fr_i = [int(f) for f in fr]
        _fr_conf = [round(_local_conf(f), 4) for f in _fr_i]
        print(f"[stemjson] {nm} 频谱通量踩点: {len(fr)}")
        result_stems[nm] = {
            "source": nm, "method": "spectral_flux",
            "onsets_sec": _sec_from_frames(fr),
            "onsets_frame": _fr_i,
            "onsets_prob": _fr_conf,
        }

    # —— 元信息 ——
    dur = float(len(y_full22)) / SR
    out_path = _out_eff
    out_dir = _out_dir
    # —— 落盘分离音轨（供「分离试听」；命中缓存时此处为重写，无害）——
    try:
        _stem_out = os.path.join(out_dir, "stems")
        os.makedirs(_stem_out, exist_ok=True)
        for _nm, _sig in stems44.items():
            _sig = np.asarray(_sig, dtype=np.float32)
            _clip = np.clip(_sig, -1.0, 1.0)
            sf.write(os.path.join(_stem_out, f"{_nm}.wav"),
                     (_clip * 32767.0).astype(np.int16), 44100)
        print(f"[stemjson] 已落盘 {len(stems44)} 路分离音轨 -> {_stem_out}")
    except Exception as _e:
        print(f"[stemjson] 落盘分离音轨失败（不影响时间戳）: {_e}")
    try:
        source_audio = os.path.relpath(os.path.abspath(audio_path), out_dir)
    except ValueError:
        source_audio = os.path.abspath(audio_path)
    out = {
        "version": 1,
        "source_audio": source_audio,
        "duration_sec": round(dur, 3),
        "sample_rate": SR,
        "hop_ms": round(HOP_MS, 4),
        "separation_model": SEP_MODEL_NAME,
        "onset_model": "onset_net.onnx",
        "lead_stem": lead,
        "vocal_mode": vocal_mode,
        "has_vocals": has_vocals,
        "bpm_hint": bpm_hint,
        "stems": result_stems,
    }
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[stemjson] 已写出 JSON: {out_path}")
    return out_path, out


def main():
    ap = argparse.ArgumentParser(description="BSR 分离→多路时间戳→JSON（torch-free，移植 extract_timestamps）")
    ap.add_argument("--audio", required=True, help="输入音频路径")
    ap.add_argument("--out", default=None, help="输出 JSON 路径（默认 <名>_timestamps.json 同目录）")
    ap.add_argument("--bpm", type=float, default=None, help="可选 BPM 提示，写入 bpm_hint 供工作台定网格")
    ap.add_argument("--no-piano", action="store_true", help="不输出 piano 赠品通道")
    ap.add_argument("--lead-stem", default="auto",
                    choices=["auto", "other", "piano", "guitar", "vocals"],
                    help="主旋律吃哪根分离轨：auto=按能量自动挑最强(默认)")
    ap.add_argument("--vocal-mode", default="auto",
                    choices=["auto", "force", "off"],
                    help="人声通道：auto=自动检测(默认) / force / off")
    args = ap.parse_args()
    extract(args.audio, args.out, bpm_hint=args.bpm,
            include_piano=not args.no_piano,
            lead_stem=args.lead_stem,
            vocal_mode=args.vocal_mode)


if __name__ == "__main__":
    main()
