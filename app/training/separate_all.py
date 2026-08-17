"""separate_all.py — 一次分离全部音轨并导出可播放 wav，供网页「第二步」列出试听。

用法: python separate_all.py --audio <wav> --outdir <dir> --prefix <uid>
输出到 stdout 的 JSON（唯一输出）:
  {"ok": true, "stems": [{"name","label","file","duration_s"}, ...]}
  {"ok": false, "error": "..."}

必须在 torch 环境里跑（需要 demucs）。分离只跑一次（htdemucs），
再合成 full(原) / accomp(原-人声)。

核心逻辑在 do_separate()：子进程模式（CLI）与 web_server 的 in-process
模式共用，避免两套实现漂移。
"""
from __future__ import annotations
import os, sys, json, argparse

ROOT = os.path.dirname(os.path.abspath(__file__))          # app/training/
APP = os.path.dirname(ROOT)                                # app/
for p in (APP, ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from separation import SR, separate_stems, write_wav

# 第二步要列出的「分解音轨」（不含原音频；原音频由前端用上传文件直接播放）
STEMS_OUT = [
    ("drums", "鼓点 drums"),
    ("bass", "贝斯 bass"),
    ("other", "旋律 / 钢琴 other"),
    ("vocals", "人声 vocals"),
    ("accomp", "伴奏（去人声）accomp"),
]


def do_separate(audio, outdir, prefix):
    """分离全部音轨并落盘。返回 {"ok": true, "stems": [...]} 或 {"ok": false, "error": ...}。"""
    try:
        stems = separate_stems(audio)
        out = []
        for key, label in STEMS_OUT:
            y = stems[key]
            fname = f"{prefix}_{key}.wav"
            fpath = os.path.join(outdir, fname)
            write_wav(fpath, y, SR)
            out.append({
                "name": key, "label": label, "file": fname,
                "duration_s": round(len(y) / SR, 2),
            })
        return {"ok": True, "stems": out}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--prefix", required=True)
    a = ap.parse_args()
    print(json.dumps(do_separate(a.audio, a.outdir, a.prefix)))


if __name__ == "__main__":
    main()
