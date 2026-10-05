# -*- coding: utf-8 -*-
"""导出 OSN1 置信度 json —— 完全镜像 Studio 原本写 osn1_timestamps.json 的格式。

原本的 osn1_timestamps.json 每轨是一个 dict，含平行的 onsets_sec / onsets_frame
数组（melody 还带 model 字段），顶层带 version / source_audio / sample_rate /
hop_ms / separation_model / onset_model / lead_stem / vocal_mode / has_vocals /
bpm_hint / duration_sec / stems 一整套元数据。

本脚本：读工程目录里的 osn1_timestamps.json，重跑 ONNX onset 模型拿到逐帧概率，
给每个采点算出真实模型置信度，作为一条**平行的 onsets_prob 数组**加入每轨
（与 onsets_sec / onsets_frame 一一对齐），其余字段原样照抄。并按阈值滤掉
置信度<=阈值的采点（同步从三个平行数组里一起删，保证对齐）。

用法:
  python export_osn1_conf_json.py --workdir <工程目录> --out <输出json>
可选:
  --model <onnx路径>   默认 audio-sep/models/osn1/onset_net.onnx
  --threshold 0.2      置信度<=该值的采点被抹去（默认 0.2）
  --filter-lookup      连 drums/bass/guitar/other 这些 lookup 轨也一并按阈值删
                       （默认只删 melody/vocals/piano 三轨，因它们才是模型直给的
                         onset 置信度；其余轨的 onsets_prob 是全曲模型曲线 lookup，
                         量纲不同，0.2 阈值会清空鼓点，故默认保留）
"""
import argparse
import json
import os
import sys

import numpy as np
import librosa

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from osn1_pipeline import (SR, HOP_MS, DEFAULT_OSN1_ONNX,
                           get_osn1_session, build_7ch_mel)
from osn1_stemjson import onnx_onsets, _sec_from_frames

CONF_FILTER_THRESHOLD = 0.2  # 默认抹去置信度<=0.2 的采点
STEM_NAMES = ["bass", "drums", "other", "vocals", "guitar", "piano"]


def load_stem_wave(path, sr):
    y, _ = librosa.load(path, sr=sr, mono=True)
    return y.astype(np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default=DEFAULT_OSN1_ONNX)
    ap.add_argument("--threshold", type=float, default=CONF_FILTER_THRESHOLD)
    ap.add_argument("--filter-lookup", action="store_true")
    args = ap.parse_args()

    wd = args.workdir
    ts_path = os.path.join(wd, "osn1_timestamps.json")
    ts = json.load(open(ts_path, encoding="utf-8"))

    # 1) 读 6 路 stems (44100 单声道)
    stems_dir = os.path.join(wd, "stems")
    stems44 = {}
    for nm in STEM_NAMES:
        p = os.path.join(stems_dir, nm + ".wav")
        if not os.path.exists(p):
            raise FileNotFoundError("缺 stems: " + p)
        stems44[nm] = load_stem_wave(p, 44100)

    # 2) 读原混音 -> 22050 单声道
    src = ts.get("source_audio")
    if src and os.path.exists(src):
        y_full22, _ = librosa.load(src, sr=SR, mono=True)
    else:
        cand = []
        for root, _, files in os.walk(os.path.dirname(wd)):
            for f in files:
                if f.lower().endswith((".ogg", ".mp3", ".wav", ".flac")):
                    cand.append(os.path.join(root, f))
        if not cand:
            raise FileNotFoundError("找不到原曲音频用于重建 full 通道")
        y_full22, _ = librosa.load(cand[0], sr=SR, mono=True)
    y_full22 = y_full22.astype(np.float32)

    # 3) 跑模型拿全曲逐帧概率
    mel7 = build_7ch_mel(stems44, y_full22)
    sess = get_osn1_session(args.model)
    _, prob = onnx_onsets(mel7, sess)  # prob: (T,) 逐帧概率
    prob = np.asarray(prob, dtype=np.float32)
    T = len(prob)

    # 4) 逐轨：照抄原字段 + 算 onsets_prob + 过滤
    new_stems = {}
    for nm, info in ts.get("stems", {}).items():
        frames = info.get("onsets_frame", []) or []
        secs = info.get("onsets_sec", []) or []
        # 照抄原轨所有字段
        out_stem = dict(info)
        if not frames:
            out_stem["onsets_prob"] = []
            new_stems[nm] = out_stem
            continue
        # 算每个采点的真实模型概率（全曲曲线 lookup）
        prob_at = []
        for f in frames:
            fi = int(f)
            prob_at.append(float(prob[fi]) if 0 <= fi < T else 0.0)
        # 是否模型直给置信度轨：原 method=='onsetnet'
        is_model_onset = (info.get("method") == "onsetnet")
        do_filter = is_model_onset or args.filter_lookup
        if do_filter:
            keep_sec, keep_fr, keep_pr = [], [], []
            dropped = 0
            for s, fr, pr in zip(secs, frames, prob_at):
                if pr <= args.threshold:
                    dropped += 1
                    continue
                keep_sec.append(s)
                keep_fr.append(fr)
                keep_pr.append(round(pr, 4))
            out_stem["onsets_sec"] = keep_sec
            out_stem["onsets_frame"] = keep_fr
            out_stem["onsets_prob"] = keep_pr
            print("  %-8s 原 %d 点 -> 保留 %d 点 (删 %d, <=%.2f)"
                  % (nm, len(frames), len(keep_fr), dropped, args.threshold))
        else:
            # lookup 轨默认保留，但仍挂上 onsets_prob
            out_stem["onsets_prob"] = [round(float(p), 4) for p in prob_at]
            print("  %-8s 保留 %d 点 (lookup 轨默认不滤, 已挂 onsets_prob)"
                  % (nm, len(frames)))
        # 统一写回（有采点轨也别漏）
        new_stems[nm] = out_stem

    # 5) 顶层：原样照抄 osn1_timestamps.json，只替换 stems
    out = dict(ts)
    out["stems"] = new_stems
    json.dump(out, open(args.out, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("写出 -> " + args.out)


if __name__ == "__main__":
    main()
