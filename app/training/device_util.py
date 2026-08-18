"""device_util.py — 安全设备探测（解决老显卡 CUDA 假阳性）。

背景：打包的 PyTorch 是按 RTX 4070（Ada / 算力 8.9）编译的。
老显卡（如 GTX 10 系 Pascal / 算力 6.1）上 `torch.cuda.is_available()`
仍返回 True，但一旦真正加载模型 / 启动计算内核就会报
  CUDA error: no kernel image is available for execution on the device
因为打包内核里根本没有该架构的镜像。

本模块用「一次真实内核计算」探测 CUDA 是否真能用；用不了就退回 CPU，
让所有功能在任意显卡（或无独显）上都能跑（只是 CPU 更慢）。
"""
from __future__ import annotations
import torch


def get_safe_device():
    """返回 "cuda" 或 "cpu"。

    cuda 真正可用 = 存在 CUDA 设备 且 能成功执行一次内核计算
    （避开老架构 no-kernel-image 的假阳性）。
    """
    if not torch.cuda.is_available():
        return "cpu"
    try:
        # 真正启动一个计算内核，验证当前显卡架构被打包的 PyTorch 支持
        t = torch.tensor([1.0], device="cuda")
        _ = t * 2.0 + 1.0
        del t
        torch.cuda.empty_cache()
        return "cuda"
    except Exception:
        return "cpu"
