#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""check-mus-pipeline.py —— 在**真引擎路径**上验证 large 档走混合推理

不走 mus_hybrid.py 自己的 CLI，而是 import `gui/backend.py`，直接调 `_transcribe_stem()`
（跟工作台生成时一模一样的函数、env、`_run_stream`、超时逻辑），看：
  · 命令是不是 mus_hybrid.py（size=large）／原路径（size=medium）
  · 真跑一遍能不能出 MIDI、大小是不是 0
  · 关掉开关（ADOFAI_MUS_HYBRID=0）能不能回到原路径

用 mus_env 解释器跑（要引到 audio-sep 的引擎）：
    <REPO>/audio-sep/mus_env/Scripts/python.exe <REPO>/gui/tools/check-mus-pipeline.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AUDIOSEP = ROOT / "audio-sep"
CLIP = AUDIOSEP / "bench" / "mix10.wav"
OUT = AUDIOSEP / "bench" / "pipeline"

sys.path.insert(0, str(ROOT / "gui"))

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                           ("  —— " + detail) if detail else ""))


def main() -> int:
    import backend  # noqa: PLC0415

    OUT.mkdir(parents=True, exist_ok=True)
    weights = os.path.join(backend.MUS_MODELS, "large", "model.safetensors")
    env = backend._engine_env()

    print("\n=== 命令构造 ===")
    cmd_large = backend._transcribe_cmd(str(CLIP), "o.mid", "large", weights)
    check("C1 large 档走混合推理运行器", "mus_hybrid.py" in cmd_large[1], " ".join(cmd_large[:3]))
    check("C2 默认层数 auto、精度 fp32",
          "--gpu-layers" in cmd_large and cmd_large[cmd_large.index("--gpu-layers") + 1] == "auto"
          and cmd_large[cmd_large.index("--gpu-dtype") + 1] == "fp32")
    w_med = os.path.join(backend.MUS_MODELS, "medium", "model.safetensors")
    cmd_med = backend._transcribe_cmd(str(CLIP), "o.mid", "medium", w_med)
    check("C3 medium 档保持原路径（-m muscriptor）",
          "-m" in cmd_med and "muscriptor" in cmd_med and "mus_hybrid.py" not in cmd_med,
          " ".join(cmd_med[1:4]))
    check("C4 small 档保持原路径",
          "mus_hybrid.py" not in backend._transcribe_cmd(str(CLIP), "o.mid", "small", w_med))

    print("\n=== 开关注入 ===")
    os.environ["ADOFAI_MUS_GPU_LAYERS"] = "20"
    os.environ["ADOFAI_MUS_GPU_DTYPE"] = "fp16"
    c = backend._transcribe_cmd(str(CLIP), "o.mid", "large", weights)
    check("C5 环境变量能改层数/精度",
          c[c.index("--gpu-layers") + 1] == "20" and c[c.index("--gpu-dtype") + 1] == "fp16")
    os.environ["ADOFAI_MUS_HYBRID"] = "0"
    c = backend._transcribe_cmd(str(CLIP), "o.mid", "large", weights)
    check("C6 ADOFAI_MUS_HYBRID=0 回到原路径（可一键回退）",
          "mus_hybrid.py" not in c and "muscriptor" in c)
    for k in ("ADOFAI_MUS_GPU_LAYERS", "ADOFAI_MUS_GPU_DTYPE", "ADOFAI_MUS_HYBRID"):
        os.environ.pop(k, None)

    print("\n=== 真跑一遍（large，走 _transcribe_stem 全套） ===")
    if not CLIP.is_file():
        check("C7 测试音频存在", False, str(CLIP))
        return report()
    out_mid = OUT / "pipeline_large.mid"
    if out_mid.exists():
        out_mid.unlink()
    lines = []
    ok, size, tail = backend._transcribe_stem(str(CLIP), str(out_mid), "large", env,
                                             on_log=lambda s: lines.append(s.rstrip()),
                                             timeout=1800)
    check("C7 引擎路径跑通且产出 MIDI", ok and size > 200, "ok=%s size=%s" % (ok, size))
    check("C8 日志里有「走混合推理」的标记",
          any("混合推理" in l for l in lines), " | ".join(lines[:2]))
    check("C9 日志里有自动切分结果（GPU N/48 层）",
          any("切分完成" in l for l in lines),
          next((l for l in lines if "切分完成" in l), ""))
    print("     产物 %s（%d KB）" % (out_mid.name, size // 1024))
    return report()


def report() -> int:
    total = len(PASS) + len(FAIL)
    print("\n========== %d/%d 通过 ==========" % (len(PASS), total))
    for f in FAIL:
        print("  - 失败：%s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
