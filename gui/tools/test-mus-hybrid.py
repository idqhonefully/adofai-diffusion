#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test-mus-hybrid.py —— mus_hybrid.py（CPU+GPU 混合推理）的回归测试

必须用 mus_env 的解释器跑（要 import muscriptor）：
    <REPO>/audio-sep/mus_env/Scripts/python.exe <REPO>/gui/tools/test-mus-hybrid.py

三段，全部断言，不靠肉眼：

  P 段：切分算术 + 参数解析（纯逻辑，给定可用显存 ⇒ 层数）
  H 段：真建模型（**small 档**，393 MB）—— 层设备 / 精度 / 头部模块落位 / patch 装上了
  E 段：端到端保真（**medium 档** + 5s 密集混音片段）——
        参照 = 原版纯 CPU（fp32、无 autocast）；
        ⭐ 关键断言 E3：用我的运行器跑「纯 CPU」必须与原版纯 CPU **逐音符指纹一致**
           ⇒ 证明"逐张量装载 + 跨设备 forward"这两个新东西没有改动模型输出；
        其余（全 GPU / 混合 / fp16）对比同一个参照，报差异百分比 —— 那是
        GPU(cuBLAS) 与 CPU(MKL) 求和顺序、以及 fp16 舍入带来的正常漂移。

为什么用小/中档验证 large 的代码路径：混合推理的全部新代码与档位无关，
small/medium 秒级可跑、能反复验；large 只在耗时/显存上不同，性能另用
bench-muscriptor-large.py 量。
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # <REPO>
AUDIOSEP = ROOT / "audio-sep"
MUS_PY = AUDIOSEP / "mus_env" / "Scripts" / "python.exe"
HYBRID = AUDIOSEP / "mus_hybrid.py"
SMALL_W = AUDIOSEP / "models" / "muscriptor" / "small" / "model.safetensors"
MEDIUM_W = AUDIOSEP / "models" / "muscriptor" / "medium" / "model.safetensors"
LARGE_W = AUDIOSEP / "models" / "muscriptor" / "large" / "model.safetensors"
CLIP = AUDIOSEP / "bench" / "mix5.wav"               # 5s 密集混音（钢琴轨是空的，别用）
OUTDIR = AUDIOSEP / "bench" / "hybridtest"

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name,
                           ("  —— " + detail) if detail else ""))


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(cmd: list[str], timeout: int = 1800):
    if Path(cmd[cmd.index("-o") + 1]).exists():
        Path(cmd[cmd.index("-o") + 1]).unlink()
    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace")
    out = Path(cmd[cmd.index("-o") + 1])
    return r, (time.perf_counter() - t0), out


def main() -> int:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    mh = load_module(HYBRID, "mus_hybrid")
    bench = load_module(ROOT / "gui" / "tools" / "bench-muscriptor-large.py", "benchmod")

    # ---------------------------------------------------------------- P 段
    print("\n=== P 段：切分算术（large，给定可用显存） ===")
    F = 7065 * 1024 * 1024
    p32 = mh.compute_plan(LARGE_W, "auto", "fp32", 1024, 514, 2000, free_bytes=F)
    check("P1 auto/fp32 @6.9G ⇒ 43 层上 GPU", p32["gpu_layers"] == 43,
          "实得 %d" % p32["gpu_layers"])
    check("P2 单层预算 = 137.5 MB（权重 108 + KV 29.5）",
          abs(p32["per_layer_mb"] - 137.48) < 0.5, "实得 %.2f" % p32["per_layer_mb"])
    check("P3 计划占用不超过可用显存", p32["plan_gpu_mb"] <= p32["free_mb"],
          "%s <= %s" % (p32["plan_gpu_mb"], p32["free_mb"]))
    p16 = mh.compute_plan(LARGE_W, "auto", "fp16", 1024, 514, 2000, free_bytes=F)
    check("P4 auto/fp16 ⇒ 48/48 全上 GPU（权重与 KV 各减半）",
          p16["gpu_layers"] == 48, "实得 %d" % p16["gpu_layers"])
    check("P5 fp16 单层预算 ≈ fp32 的一半",
          abs(p16["per_layer_mb"] * 2 - p32["per_layer_mb"]) < 4.0,
          "%.1f vs %.1f" % (p16["per_layer_mb"], p32["per_layer_mb"]))
    p24 = mh.compute_plan(LARGE_W, "24", "fp32", 1024, 514, 2000, free_bytes=F)
    check("P6 显式 --gpu-layers 24 生效", p24["gpu_layers"] == 24)
    check("P7 显式超过层数 ⇒ 夹到 48",
          mh.compute_plan(LARGE_W, "999", "fp32", 1024, 514, 2000, free_bytes=F)["gpu_layers"] == 48)
    check("P8 负数/0 ⇒ 0（纯 CPU）",
          mh.compute_plan(LARGE_W, "-5", "fp32", 1024, 514, 2000, free_bytes=F)["gpu_layers"] == 0)
    tiny = mh.compute_plan(LARGE_W, "auto", "fp32", 1024, 514, 2000,
                           free_bytes=512 * 1024 * 1024)
    check("P9 显存不够 ⇒ 退到 0 层而不是硬塞", tiny["gpu_layers"] == 0,
          "实得 %d" % tiny["gpu_layers"])

    print("\n=== P 段：参数解析（自己的开关抠掉，其余透传） ===")
    opts, rest = mh.split_args(
        ["--gpu-layers=8", "--reserve-mb", "512", "transcribe", "a.wav",
         "-o", "b.mid", "--model", str(SMALL_W), "--device", "cuda"])
    check("P10 内联写法 --gpu-layers=8 能解析", opts.get("gpu_layers") == "8")
    check("P11 --reserve-mb 512 能解析", opts.get("reserve_mb") == "512")
    check("P12 其余参数原样透传",
          rest == ["transcribe", "a.wav", "-o", "b.mid", "--model", str(SMALL_W),
                   "--device", "cuda"], str(rest))
    check("P13 本地权重能识别", mh._weights_from_args(rest) == SMALL_W)
    check("P14 size 关键字不算本地权重（交回原实现）",
          mh._weights_from_args(["transcribe", "a.wav", "--model", "large"]) is None)
    check("P15 --device cpu 被识别（⇒ 走纯 CPU）",
          mh._wants_cpu_device(["--device", "cpu"]) and not mh._wants_cpu_device(["--device", "cuda"]))

    # ---------------------------------------------------------------- H 段
    print("\n=== H 段：真建 small 混合模型（7 层 GPU / 7 层 CPU） ===")
    import torch  # noqa: PLC0415

    if not torch.cuda.is_available():
        check("H0 CUDA 可用", False, "无 GPU，跳过 H/E 段")
        return report()
    mh.install_hybrid_forward()
    import muscriptor.modules.transformer as tfm  # noqa: PLC0415

    check("H1 patch 已装（forward._mus_hybrid）",
          getattr(tfm.StreamingTransformer.forward, "_mus_hybrid", False) is True)
    t0 = time.perf_counter()
    model = mh.build_hybrid_model(SMALL_W, 7, "fp32", torch.device("cuda"))
    build_s = time.perf_counter() - t0
    layers = model._model.transformer.layers
    devs = [str(l.norm2.weight.device) for l in layers]
    check("H2 前 7 层在 cuda", all(d.startswith("cuda") for d in devs[:7]), str(devs[:7]))
    check("H3 后 7 层在 cpu", all(d == "cpu" for d in devs[7:]), str(devs[7:]))
    head = [str(model._model.emb.weight.device),
            str(model._model.linear.weight.device),
            str(model._model.condition_provider.conditioners["self_wav"].output_proj.weight.device)]
    check("H4 头部（词嵌入/输出头/mel 投影）跟 GPU 段同设备",
          all(h.startswith("cuda") for h in head), str(head))
    check("H5 条件器的 device 属性也跟着改（否则 mel 会在 CPU 上算）",
          str(model._model.condition_provider.conditioners["self_wav"].device).startswith("cuda"))
    check("H6 切分只搬了 7 层，没把整份权重塞上卡",
          torch.cuda.memory_allocated() < 400 * 1024 * 1024,
          "allocated %.0f MiB" % (torch.cuda.memory_allocated() / 2 ** 20))
    print("  （small 建模型 + 切分 %.1fs）" % build_s)

    m2 = mh.build_hybrid_model(SMALL_W, 7, "float16", torch.device("cuda"))
    l2 = m2._model.transformer.layers
    check("H7 GPU 段为 fp16", l2[0].norm2.weight.dtype == torch.float16)
    check("H8 CPU 段仍 fp32（CPU 上 fp16 明显更慢）",
          l2[7].norm2.weight.dtype == torch.float32)
    check("H9 头部仍是 fp32（mel 数值不能在 fp16 下算）",
          m2._model.condition_provider.conditioners["self_wav"].output_proj.weight.dtype
          == torch.float32)

    # ---------------------------------------------------------------- E 段
    print("\n=== E 段：端到端保真（medium 档 + mix5.wav，参照 = 原版纯 CPU） ===")
    if not CLIP.is_file():
        check("E0 测试音频存在", False, str(CLIP))
        return report()

    runs = {
        "参照(原版纯CPU)": [str(MUS_PY), "-m", "muscriptor", "transcribe", str(CLIP),
                        "-o", str(OUTDIR / "ref_cpu.mid"), "--model", str(MEDIUM_W),
                        "--device", "cpu"],
        "运行器纯CPU(0层)": [str(MUS_PY), str(HYBRID), "--gpu-layers", "0", "transcribe",
                        str(CLIP), "-o", str(OUTDIR / "hyb_0.mid"), "--model", str(MEDIUM_W),
                        "--device", "cuda"],
        "全GPU(24层)": [str(MUS_PY), str(HYBRID), "--gpu-layers", "24", "transcribe",
                     str(CLIP), "-o", str(OUTDIR / "hyb_gpu.mid"), "--model", str(MEDIUM_W),
                     "--device", "cuda"],
        "混合(12层)": [str(MUS_PY), str(HYBRID), "--gpu-layers", "12", "transcribe",
                    str(CLIP), "-o", str(OUTDIR / "hyb_mix.mid"), "--model", str(MEDIUM_W),
                    "--device", "cuda"],
        "全GPU+fp16": [str(MUS_PY), str(HYBRID), "--gpu-layers", "24", "--gpu-dtype", "float16",
                     "transcribe", str(CLIP), "-o", str(OUTDIR / "hyb_fp16.mid"),
                     "--model", str(MEDIUM_W), "--device", "cuda"],
    }
    got = {}
    for label, cmd in runs.items():
        r, dt, out = run(cmd)
        ok = r.returncode == 0 and out.exists()
        got[label] = {"rc": r.returncode, "out": out, "s": round(dt, 1),
                      "log": (r.stdout or "") + (r.stderr or "")}
        print("  · %-14s rc=%s  %.1fs  %s" % (label, r.returncode, dt, out.name))
        if not ok:
            print("    %s" % got[label]["log"][-400:].replace("\n", " | "))
    check("E1 五路全部退出码 0 且都有产物",
          all(v["rc"] == 0 and v["out"].exists() for v in got.values()),
          ", ".join("%s=%s" % (k, v["rc"]) for k, v in got.items()))

    ref = bench.parse_midi(got["参照(原版纯CPU)"]["out"]) if got["参照(原版纯CPU)"]["out"].exists() else None
    check("E2 参照（纯 CPU）本身有音符（否则这条测试没意义）",
          bool(ref) and ref["notes"] > 0, "notes=%s" % (ref or {}).get("notes"))

    def sig(label):
        return bench.parse_midi(got[label]["out"]) if got[label]["out"].exists() else None

    h0 = sig("运行器纯CPU(0层)")
    if ref and h0:
        check("⭐E3 运行器纯 CPU 与原版纯 CPU 逐音符指纹完全一致（装载/patch 忠实）",
              h0["sig"] == ref["sig"] and h0["notes"] == ref["notes"],
              "%s(%d) vs %s(%d)" % (h0["sig"], h0["notes"], ref["sig"], ref["notes"]))

    for label, why in (("全GPU(24层)", "fp32 全 GPU：cuBLAS 与 MKL 求和顺序不同"),
                       ("混合(12层)", "fp32 混合：一半层在 CPU"),
                       ("全GPU+fp16", "GPU 段 fp16 舍入")):
        d = sig(label)
        if not (d and ref and ref["notes"]):
            check("E4 %s 有产物可比" % label, False)
            continue
        delta = abs(d["notes"] - ref["notes"]) / float(ref["notes"])
        check("E4 %s 音符数漂移 ≤10%%（%s）" % (label, why), delta <= 0.10,
              "%d vs 参照 %d（%.1f%%）sig %s" % (d["notes"], ref["notes"], delta * 100, d["sig"]))

    log_mix = got["混合(12层)"]["log"]
    m = re.search(r"切分完成：GPU (\d+)/(\d+) 层（(\w+)）", log_mix)
    check("E5 混合那一路日志确认是 12/24 层上 GPU", bool(m) and m.group(1) == "12",
          (m.group(0) if m else log_mix[-200:].replace("\n", " | ")))
    log0 = got["运行器纯CPU(0层)"]["log"]
    check("E6 纯 CPU 那一路日志确认 0 层上 GPU",
          bool(re.search(r"切分完成：GPU 0/24 层", log0)))

    print("\n  产物指纹一览：")
    for label in runs:
        d = sig(label)
        print("    %-14s %5s notes  sig %s" % (label, (d or {}).get("notes", "-"),
                                              (d or {}).get("sig", "-")))
    return report()


def report() -> int:
    total = len(PASS) + len(FAIL)
    print("\n========== %d/%d 通过 ==========" % (len(PASS), total))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - %s" % f)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
