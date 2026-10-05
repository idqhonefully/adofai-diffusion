#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""mus_hybrid.py —— MuScriptor 的 CPU+GPU 混合推理运行器（只给 large 档用）

为什么需要它
------------
large 档 = dim1536 × 48 层，fp32 权重 5.09 GiB。整份压到 8 GB 显卡上是"刚好塞满"：
    5.09 GiB 权重 + ~1.5 GiB 流式 KV cache + 音频/激活 ≈ 7.6 GiB（实测）
留给系统/别的程序的余量几乎为零；更关键的是解码是**显存带宽瓶颈**：每生成一个 token
都要把 5.09 GiB 权重从显存完整读一遍（4070 Laptop ~256 GB/s ⇒ 单 token 下限 ~20 ms）。
所以 large 要么塞不下、要么塞下了也很慢。

它做什么
--------
把 48 层按显存预算切成两段：**前 K 层放 GPU，其余留 CPU**。
隐藏态只在分界处搬两次（进 GPU 一次、回 CPU 一次）；单 token 的隐藏态只有
1536 × 4 B = 6 KB，相对每 token 上百 MB 的权重读取完全可忽略，所以
"K 层在 GPU 上跑多快，整体就多快"——K 越大越快，K 受显存预算限制。
条件器（mel 频谱）、词嵌入、输出头跟第一段同设备，mel 仍在 GPU 上跑。

不改第三方包
------------
`muscriptor` 包**一个字节都不改**。所有改动都在本进程内：
  1) monkeypatch `StreamingTransformer.forward` —— 只在层循环里多了"发现设备/精度
     不同就搬一次"的判断，逐层语义与原版完全一致；
  2) 权重用 safetensors mmap **逐张量**拷贝，替代 `load_model` 里"整份权重先上设备"
     那一步 —— 这一步在 large 上就是 OOM/塞满的根源；
  3) 其余（CLI 参数、条件器、tokenizer、解码循环）全部复用 muscriptor 原实现：
     本脚本把参数原样透传给 `muscriptor transcribe`。

用法（muscriptor transcribe 的参数原样透传，只加下面这些开关）
------------
    python mus_hybrid.py --gpu-layers auto transcribe in.wav -o out.mid --model <weights>

    --gpu-layers auto|N   前 N 层上 GPU；auto（默认）= 按当前可用显存算；0 = 纯 CPU
    --gpu-dtype fp32|fp16 GPU 段权重精度：float32（默认，与纯 CPU 逐位一致）
                          / float16（权重与 KV 都减半 ⇒ 同样显存能多放一倍层，且更快）
    --reserve-mb N        auto 时给激活/KV/碎片预留的显存（默认 1024）
    --prepend-tokens N    条件 token 数（默认 514 = mel512+乐器1+数据集1，仅影响预算估算）
    --max-gen-len N       单 chunk 最大生成步数（默认 2000，与包内常量一致）
    --plan-only           只打印切分方案（JSON 到 stdout）不加载模型
    --free-mb N           配合 --plan-only/auto 模拟可用显存（回归测试用）
    --hybrid-off          完全透传，不做任何 patch

环境变量（给不给参数都能用，参数优先）
    ADOFAI_MUS_GPU_LAYERS / ADOFAI_MUS_GPU_DTYPE / ADOFAI_MUS_RESERVE_MB
"""

from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path

PROG = "mus_hybrid"
_MIB = 1024 * 1024

DEFAULT_RESERVE_MB = 1024
DEFAULT_PREPEND_TOKENS = 514      # mel 512 + instrument_group 1 + dataset_name 1
DEFAULT_MAX_GEN_LEN = 2000        # transcription_model.transcribe() 里的 max_gen_len

_STR_FLAGS = ("--gpu-dtype",)
_INT_FLAGS = ("--reserve-mb", "--prepend-tokens", "--max-gen-len", "--free-mb")
_VAL_FLAGS = ("--gpu-layers",) + _STR_FLAGS + _INT_FLAGS
_BOOL_FLAGS = ("--plan-only", "--hybrid-off")


def log(msg: str) -> None:
    sys.stderr.write("[%s] %s\n" % (PROG, msg))
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# 参数：自己的开关抠出来，其余原样透传给 muscriptor CLI
# ---------------------------------------------------------------------------


def split_args(argv: list[str]) -> tuple[dict, list[str]]:
    opts: dict = {}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _BOOL_FLAGS:
            opts[tok[2:].replace("-", "_")] = True
            i += 1
            continue
        if tok in _VAL_FLAGS:
            if i + 1 >= len(argv):
                raise SystemExit("%s: %s 后面缺参数值" % (PROG, tok))
            opts[tok[2:].replace("-", "_")] = argv[i + 1]
            i += 2
            continue
        hit = False
        for flag in _VAL_FLAGS:
            if tok.startswith(flag + "="):
                opts[flag[2:].replace("-", "_")] = tok[len(flag) + 1 :]
                hit = True
                break
        if hit:
            i += 1
            continue
        rest.append(tok)
        i += 1
    return opts, rest


def _pick(opts: dict, name: str, env: str, default):
    if name in opts and opts[name] is not None:
        return opts[name]
    val = os.environ.get(env)
    return default if val is None or val == "" else val


# ---------------------------------------------------------------------------
# 轻量读权重头（不需要 torch）：层数 / 单层字节 / 非层字节
# ---------------------------------------------------------------------------


def read_safetensors_header(path: Path) -> dict:
    with open(path, "rb") as fh:
        raw = fh.read(8)
        if len(raw) != 8:
            raise ValueError("不是 safetensors 文件: %s" % path)
        n = struct.unpack("<Q", raw)[0]
        if n <= 0 or n > 256 * _MIB:
            raise ValueError("safetensors 头部长度异常: %d" % n)
        return json.loads(fh.read(n).decode("utf-8"))


def model_facts(weights: Path) -> tuple[int, int, int]:
    """返回 (层数, 单层最大字节数, 非层参数字节数)——都是 fp32 磁盘字节。"""
    hdr = read_safetensors_header(weights)
    per_layer: dict[int, int] = {}
    other = 0
    for key, meta in hdr.items():
        if key == "__metadata__":
            continue
        size = meta["data_offsets"][1] - meta["data_offsets"][0]
        parts = key.split(".")
        if len(parts) > 2 and parts[0] == "transformer" and parts[1] == "layers":
            idx = int(parts[2])
            per_layer[idx] = per_layer.get(idx, 0) + size
        else:
            other += size
    if not per_layer:
        raise ValueError("权重里没有 transformer.layers.* : %s" % weights)
    return len(per_layer), max(per_layer.values()), other


def read_dim(weights: Path) -> int:
    cfg = weights.parent / "config.json"
    if cfg.is_file():
        try:
            return int(json.loads(cfg.read_text(encoding="utf-8"))["dim"])
        except Exception:
            pass
    return 1536


# ---------------------------------------------------------------------------
# 切分方案
# ---------------------------------------------------------------------------


def compute_plan(
    weights: Path,
    gpu_layers="auto",
    gpu_dtype: str = "fp32",
    reserve_mb: int = DEFAULT_RESERVE_MB,
    prepend_tokens: int = DEFAULT_PREPEND_TOKENS,
    max_gen_len: int = DEFAULT_MAX_GEN_LEN,
    free_bytes: int | None = None,
    wav_bytes: int = 0,
) -> dict:
    """算出前 K 层放 GPU。返回方案 dict（可 JSON 化）。"""
    weights = Path(weights)
    n_layers, layer_bytes_fp32, head_bytes_fp32 = model_facts(weights)
    dim = read_dim(weights)
    itemsize = 2 if str(gpu_dtype).lower() in ("float16", "fp16", "half") else 4
    scale = itemsize / 4.0

    layer_bytes = int(layer_bytes_fp32 * scale)
    head_bytes = int(head_bytes_fp32 * scale)
    # KV cache：每层每 token = 2(k,v) × dim × itemsize
    kv_per_layer = (int(prepend_tokens) + int(max_gen_len)) * 2 * dim * itemsize
    per_layer = layer_bytes + kv_per_layer
    static = head_bytes + int(wav_bytes) + int(reserve_mb) * _MIB

    if free_bytes is None:
        import torch  # noqa: PLC0415  —— 只有真跑的时候才需要 torch

        free_bytes = torch.cuda.mem_get_info()[0]

    if str(gpu_layers).lower() == "auto":
        k = int(max(0, (free_bytes - static)) // per_layer)
        mode = "auto"
    else:
        k = int(gpu_layers)
        mode = "explicit"
    k = max(0, min(k, n_layers))

    return {
        "weights": str(weights),
        "n_layers": n_layers,
        "gpu_layers": k,
        "cpu_layers": n_layers - k,
        "mode": mode,
        "gpu_dtype": "float16" if itemsize == 2 else "float32",
        "dim": dim,
        "layer_mb": round(layer_bytes / _MIB, 2),
        "kv_per_layer_mb": round(kv_per_layer / _MIB, 2),
        "per_layer_mb": round(per_layer / _MIB, 2),
        "head_mb": round(head_bytes / _MIB, 2),
        "reserve_mb": int(reserve_mb),
        "free_mb": round(free_bytes / _MIB, 1),
        "budget_mb": round((free_bytes - static) / _MIB, 1),
        "plan_gpu_mb": round((static + k * per_layer) / _MIB, 1),
    }


# ---------------------------------------------------------------------------
# 混合推理本体
# ---------------------------------------------------------------------------


def _move_head_to(model, dev) -> None:
    """条件器 + 词嵌入 + 输出头/out_norm 搬到主设备（GPU 段的一部分）。

    条件器里存了 `self.device` 这个**普通属性**，用它来造张量/搬输入；
    只 `.to(dev)` 不动它的话，mel 会在旧设备上算完再和 GPU 权重撞车。
    """
    model.condition_provider.to(dev)
    for cond in model.condition_provider.conditioners.values():
        if getattr(cond, "device", None) is not None:
            cond.device = dev
    model.emb.to(dev)
    if model.out_norm is not None:
        model.out_norm.to(dev)
    model.linear.to(dev)


def install_hybrid_forward() -> None:
    """把层循环换成"跨设备自动搬运"版；语义与原版逐层一致。"""
    import torch  # noqa: PLC0415

    import muscriptor.modules.transformer as tfm  # noqa: PLC0415

    if getattr(tfm.StreamingTransformer.forward, "_mus_hybrid", False):
        return

    def forward(self, x, prepend_length=0, model_state=None):
        del prepend_length  # 原版也没用：位置来自 state['offsets']
        b, t, c = x.shape
        state = self.get_state(model_state)
        enter_dev, enter_dtype = x.device, x.dtype

        if state is not None:
            offsets = state["offsets"].to(device=enter_dev)
        else:
            offsets = torch.zeros(b, dtype=torch.long, device=enter_dev)
        positions = torch.arange(t, device=enter_dev).view(1, -1, 1)
        positions = positions + offsets.view(-1, 1, 1)
        pos_emb = tfm.create_sin_embedding(
            positions, c, max_period=self.max_period, dtype=torch.float32
        )
        x = x + (pos_emb * (positions >= 0).float()).to(enter_dtype)

        cur_dev, cur_dtype = x.device, x.dtype
        for layer in self.layers:
            w = layer.norm2.weight
            if w.device != cur_dev or w.dtype != cur_dtype:
                # 分界处搬一次；单 token 几 KB，可忽略
                x = x.to(device=w.device, dtype=w.dtype)
                cur_dev, cur_dtype = w.device, w.dtype
            x = layer(x, model_state=model_state)
        if x.device != enter_dev or x.dtype != enter_dtype:
            x = x.to(device=enter_dev, dtype=enter_dtype)
        return x

    forward._mus_hybrid = True  # type: ignore[attr-defined]
    tfm.StreamingTransformer.forward = forward


def _load_weights_per_tensor(model, weights: Path) -> None:
    """逐张量从 safetensors mmap 拷进模型（峰值内存 ≈ 单层大小）。"""
    import torch  # noqa: PLC0415
    from safetensors import safe_open  # noqa: PLC0415

    from muscriptor.transcription_model import (  # noqa: PLC0415
        _remap_single_codebook_keys,
    )

    targets: dict = dict(model.named_parameters())
    targets.update(dict(model.named_buffers()))

    copied = 0
    with safe_open(str(weights), framework="pt", device="cpu") as fh:
        raw_keys = list(fh.keys())
        # 旧多码本检查点把 emb/linear 存成 ModuleList 的第 0 项：emb.0.* → emb.*
        # 返回的是 {新键: 旧键}
        name_map = _remap_single_codebook_keys({k: k for k in raw_keys})
        seen = set()
        for new_key, raw_key in name_map.items():
            dst = targets.get(new_key)
            if dst is None:
                continue
            src = fh.get_tensor(raw_key)
            if tuple(dst.shape) != tuple(src.shape):
                raise ValueError(
                    "形状不匹配 %s: 模型 %s vs 权重 %s"
                    % (new_key, tuple(dst.shape), tuple(src.shape))
                )
            with torch.no_grad():
                dst.copy_(src)
            seen.add(new_key)
            copied += 1
    missing = [k for k in targets if k not in seen]
    if missing:
        raise ValueError(
            "权重缺 %d 个张量（前 5 个：%s）—— 模型与 config.json 不匹配？"
            % (len(missing), ", ".join(missing[:5]))
        )
    log("权重装载完成：%d 张量（逐张量 mmap 拷贝），无缺失" % copied)


def build_hybrid_model(weights: Path, gpu_layers: int, gpu_dtype: str, primary):
    """按方案建模型：前 gpu_layers 层在 primary、其余留 CPU。"""
    import torch  # noqa: PLC0415

    from muscriptor.models.lm import LMModel  # noqa: PLC0415, F401  —— 触发模块注册
    from muscriptor.tokenizer.mt3 import MT3Tokenizer  # noqa: PLC0415
    from muscriptor.transcription_model import (  # noqa: PLC0415
        TranscriptionModel,
        _build_model,
        _resolve_config,
    )

    weights = Path(weights)
    dtype = torch.float16 if gpu_dtype == "float16" else torch.float32
    cfg = _resolve_config(str(weights), weights)

    model = _build_model(torch.device("cpu"), cfg)   # 先全在 CPU 上建，不占显存
    model.eval()
    _load_weights_per_tensor(model, weights)

    layers = model.transformer.layers
    _move_head_to(model, primary)
    for i in range(int(gpu_layers)):
        layers[i].to(device=primary, dtype=dtype)   # 逐层搬，不整份上卡

    n = len(layers)
    on_gpu = ", ".join(
        "%d" % i for i in range(n) if layers[i].norm2.weight.device.type != "cpu"
    )
    log(
        "切分完成：GPU %d/%d 层（%s）｜CPU %d 层（fp32）"
        % (int(gpu_layers), n, "float16" if dtype == torch.float16 else "float32",
           n - int(gpu_layers))
    )
    log("GPU 层号：%s" % (on_gpu if on_gpu else "（无）"))

    tokenizer = MT3Tokenizer(instrument_vocabulary="MT3_FULL_PLUS", max_shift_steps=1001)
    return TranscriptionModel(model=model, tokenizer=tokenizer, device=primary)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _weights_from_args(rest: list[str]) -> Path | None:
    for i, tok in enumerate(rest):
        if tok in ("--model", "-m") and i + 1 < len(rest):
            cand = rest[i + 1]
            p = Path(cand)
            return p if p.is_file() else None
        if tok.startswith("--model="):
            p = Path(tok.split("=", 1)[1])
            return p if p.is_file() else None
    return None


def _wants_cpu_device(rest: list[str]) -> bool:
    """透传参数里显式写了 --device cpu / -d cpu 吗。"""
    for i, tok in enumerate(rest):
        if tok in ("--device", "-d") and i + 1 < len(rest):
            return rest[i + 1] == "cpu"
        if tok.startswith("--device="):
            return tok.split("=", 1)[1] == "cpu"
    return False


def main() -> int:
    opts, rest = split_args(sys.argv[1:])

    if opts.get("hybrid_off"):
        log("--hybrid-off：原样透传（不 patch）")
        import muscriptor.main as M  # noqa: PLC0415

        sys.argv = ["muscriptor"] + rest
        M.app()
        return 0

    weights = _weights_from_args(rest)
    if weights is None:
        # 非本地权重（size 关键字 / hf:// / http://）→ 交回原实现，别自作主张拦截
        log("未找到本地 weights 文件：原样透传给 muscriptor（混合推理只对本地权重生效）")
        import muscriptor.main as M  # noqa: PLC0415

        sys.argv = ["muscriptor"] + rest
        M.app()
        return 0

    gpu_layers = _pick(opts, "gpu_layers", "ADOFAI_MUS_GPU_LAYERS", "auto")
    gpu_dtype = str(_pick(opts, "gpu_dtype", "ADOFAI_MUS_GPU_DTYPE", "fp32")).lower()
    gpu_dtype = "float16" if gpu_dtype in ("float16", "fp16", "half") else "float32"
    reserve_mb = int(_pick(opts, "reserve_mb", "ADOFAI_MUS_RESERVE_MB", DEFAULT_RESERVE_MB))
    prepend = int(_pick(opts, "prepend_tokens", "ADOFAI_MUS_PREPEND_TOKENS", DEFAULT_PREPEND_TOKENS))
    max_gen = int(_pick(opts, "max_gen_len", "ADOFAI_MUS_MAX_GEN_LEN", DEFAULT_MAX_GEN_LEN))
    free_mb = opts.get("free_mb")

    if _wants_cpu_device(rest):
        # 显式要求纯 CPU：等价于 --gpu-layers 0
        gpu_layers = "0"

    plan = compute_plan(
        weights,
        gpu_layers=gpu_layers,
        gpu_dtype=gpu_dtype,
        reserve_mb=reserve_mb,
        prepend_tokens=prepend,
        max_gen_len=max_gen,
        free_bytes=None if free_mb is None else int(float(free_mb) * _MIB),
    )
    log(
        "方案：GPU %d/%d 层（%s）｜可用显存 %.1fG − 预留 %dM，单层 %.1fM（权重）"
        "+ %.1fM（KV） ⇒ 预计占用 %.1fG"
        % (
            plan["gpu_layers"], plan["n_layers"], plan["gpu_dtype"],
            plan["free_mb"] / 1024.0, plan["reserve_mb"],
            plan["layer_mb"], plan["kv_per_layer_mb"], plan["plan_gpu_mb"] / 1024.0,
        )
    )

    if opts.get("plan_only"):
        sys.stdout.write(json.dumps(plan, ensure_ascii=False) + "\n")
        return 0

    cfg = {"plan": plan, "gpu_dtype": gpu_dtype}

    def _hybrid_load(model_path, device=None, dtype=None):
        """替掉 muscriptor CLI 里的 _load_model（同签名）。"""
        return build_hybrid_model(
            Path(model_path), cfg["plan"]["gpu_layers"], cfg["gpu_dtype"], _primary_device()
        )

    def _primary_device():
        import torch  # noqa: PLC0415

        if cfg["plan"]["gpu_layers"] <= 0:
            return torch.device("cpu")
        if not torch.cuda.is_available():
            raise RuntimeError("需要 CUDA 但当前不可用；用 --gpu-layers 0 走纯 CPU")
        return torch.device("cuda")

    import muscriptor.main as M  # noqa: PLC0415

    install_hybrid_forward()
    M._load_model = _hybrid_load  # type: ignore[assignment]

    argv = list(rest)
    if "--device" not in argv and "-d" not in argv:
        argv += ["--device", "cuda" if plan["gpu_layers"] > 0 else "cpu"]
    sys.argv = ["muscriptor"] + argv
    log("交给 muscriptor CLI：%s" % " ".join(argv))
    M.app()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
