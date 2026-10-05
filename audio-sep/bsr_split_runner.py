# -*- coding: utf-8 -*-
"""bsr_split_runner.py — BSR 的【图分割链】运行器。

作为 onnxruntime.InferenceSession 的 drop-in 替身：
    sess = BsrSplitSession()
    spec = sess.run(None, {"mix": arr_1x2x588800})[0]   # (12,1025,F,2) == 原模型 stft_real

为什么需要它（2026-09-25 实测结论）：
- 原始单体 bsr_sw_mha.onnx 在 RTX 4070(CUDA) 峰值 ~7868 MiB，8GB 卡贴边、显示忙即 OOM。
- 把模型按 transformer 块切点切成 N 段，逐段建-跑-删（同一时刻只活 1 个会话）压显存。
- 切分是【逐位等价】的：同设备(GPU)下 链 vs 整图 max|Δ|=0（_run_gpu_peak.py 已证）。
- 不引入任何精度损失：纯 fp32，且关闭 TF32（use_tf32=0）保 CUDA 与 CPU 一致的量级。

🔴🔴 段数不是越少越好 —— 必须实测【共享显存】判断是否超订（2026-09-26 血泪）：
   | 配置(GROUP) | 段数 | 专用峰值 | 共享显存(超订指纹) | 单 chunk |
   |  group=1    |  26  | 3533 MiB |   135 MiB  ✅     |  7.5s   |
   |  group=2    |  14  | 4116 MiB |   192 MiB  ✅     |  7.3s   | ← 当前默认
   |  group=4    |   8  | 7656 MiB |  6186 MiB  🔴    |  >300s  |
   8 段（4 block/组）单段显存顶到 8188 边缘 → 驱动把放不下的部分丢到
   【共享显存 = 系统内存条（走 PCIe）】→ 每个算子都在等内存 → 单 chunk 从 7.5s
   暴涨到 300s+，GPU 利用率/显存曲线呈"狗啃锯齿"。段数由 26→8 时峰值 3533→7656
   是【非线性跳变】（组内中间张量链变长，ORT arena 需更大连续块）。
   ⇒ 改分组前必须先跑 `onnx_poc/_probe_cfg.py <meta> 1`，**共享显存 >500MiB 就是超订，配置作废**。
   生成新档：`python onnx_poc/_build_split_coarse.py <GROUP> <PREFIX>`。

VRAM 安全铁律：绝不复用/常驻全部会话（叠加峰值≈77GB 必 OOM）。
本运行器默认 cache=False：每段跑完即 del 会话 + gc，保证任一时刻只有 1 个会话在卡上。
（cache=True 仅用于离线基准测试，生产严禁。）
「建会话」耗时优化：跑一次 `python bsr_split_runner.py preopt` 生成预优化缓存
（onnx_poc/_opt_ort130_*.onnx，~701MB），之后每 chunk 建会省 ~23%（等价性 bit-exact）。
设 `BSR_OPT=0` 可关闭缓存回退原始子图；ORT 大版本变更缓存自动失效重建。

CUDA 运行时：复用 venv 内 nvidia/cu13 + nvidia/cudnn 的 DLL（全在 D 盘，绝不碰 C 盘，绝不 import torch）。
"""
import os
import gc
import json
import time
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
_META = os.path.join(HERE, "onnx_poc", "_split_meta_g2.json")
_NV = os.path.join(HERE, "venv", "Lib", "site-packages", "nvidia")
_CU13 = os.path.join(_NV, "cu13", "bin", "x86_64")
_CUDNN = os.path.join(_NV, "cudnn", "bin")

_CUDA_MOUNTED = False


def _mount_cuda13():
    global _CUDA_MOUNTED
    if _CUDA_MOUNTED:
        return
    dirs = [x for x in (_CU13, _CUDNN) if os.path.isdir(x)]
    for x in dirs:
        try:
            os.add_dll_directory(x)
        except Exception:
            pass
    os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + os.environ.get("PATH", "")
    _CUDA_MOUNTED = True


# ============================ 预优化缓存（2026-09-26，砍「建会话」耗时）============================
# 每个 chunk 要对 N 个子图各新建一次 InferenceSession（14 段档 ~1.8s/chunk，占 28%）。
# 一次性把每个子图用 ORT_ENABLE_ALL 优化后存盘（~701MB，写 onnx_poc/_opt_ort130_*.onnx），
# 之后每次"建会话"从优化文件重载（图已更小/已融合，解析+重优化更快）。
# 实测 1.825 -> 1.404 s/chunk（-23%）；等价性：相对 L2 = 0（bit-exact），见 _probe_eq.py。
# 注意：这只是【跳过图优化】，仍每 chunk 建 N 个会话（任一时刻仍只 1 个在卡上），不违反显存铁律。
_OPT_DIR = os.path.join(HERE, "onnx_poc")
_OPT_MAJOR = None  # 懒加载 onnxruntime 大版本，用于缓存文件名隔离（ORT 大版本变更需重生成）


def _opt_major():
    global _OPT_MAJOR
    if _OPT_MAJOR is None:
        import onnxruntime as _ort
        _OPT_MAJOR = _ort.__version__.split(".")[0]
    return _OPT_MAJOR


def _opt_path(src_rel):
    """给定子图相对路径，返回其预优化缓存文件绝对路径（带 ORT 大版本前缀）。"""
    base = src_rel[:-5] if src_rel.endswith(".onnx") else src_rel
    return os.path.join(_OPT_DIR, "_opt_ort%s_%s.onnx" % (_opt_major(), os.path.basename(base)))


def preoptimize_models(force=False):
    """一次性生成 BSR 子图预优化缓存（~701MB，写 onnx_poc/_opt_ort130_*.onnx）。

    生成后 BsrSplitSession 自动走缓存（每 chunk「建会话」省 ~23%）；
    删除这些文件或设 BSR_OPT=0 即回退原始子图。等价性已证 bit-exact（见 _probe_eq.py）。
    运行：python -m bsr_split_runner preopt   （或 bsr_split_runner.py preopt）
    """
    _mount_cuda13()
    import onnxruntime as ort
    ort.set_default_logger_severity(3)
    provs = [("CUDAExecutionProvider", {
        "device_id": 0, "use_tf32": "0",
        "arena_extend_strategy": "kSameAsRequested",
        "cudnn_conv_algo_search": "HEURISTIC",
    }), "CPUExecutionProvider"]
    meta = json.load(open(_META))
    segs = meta["segs"]
    n = 0
    t0 = time.time()
    for j, s in enumerate(segs):
        src = os.path.join(HERE, s["fn"])
        dst = _opt_path(s["fn"])
        if os.path.exists(dst) and not force:
            continue
        so = ort.SessionOptions()
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.optimized_model_filepath = dst
        ort.InferenceSession(src, sess_options=so, providers=provs)
        n += 1
        print("[preopt] %d/%d %s  (累计 %.1fs)" % (j + 1, len(segs), os.path.basename(dst), time.time() - t0), flush=True)
    print("[preopt] 完成：生成 %d 个优化文件 -> %s" % (n, _OPT_DIR), flush=True)
    print("[preopt] 之后 BsrSplitSession 会自动使用（BSR_OPT=0 可关闭）。", flush=True)


class BsrSplitSession:
    """26 段 BSR 切分链的 drop-in 会话。

    接口兼容 onnxruntime.InferenceSession：.get_providers() / .run(output_names, feed)。
    内部逐段加载 ONNX、跑、释放，保证 VRAM 峰值 ≤ 单体模型约一半。
    """

    def __init__(self, cache=False, prefer_cuda=True):
        _mount_cuda13()
        import onnxruntime as ort
        ort.set_default_logger_severity(3)  # 仅 ERROR：压掉 CUDA device_id 的无害告警洪流
        self.ort = ort
        with open(_META) as f:
            self.meta = json.load(f)
        self.real_inputs = self.meta["real_inputs"]
        self.segs = self.meta["segs"]
        self.cache = cache
        self._use_opt = self._decide_opt()
        self.use_cuda = prefer_cuda and ("CUDAExecutionProvider" in ort.get_available_providers())
        self.final_out = "stft_real"

        self.so = ort.SessionOptions()
        self.so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        # 关 TF32 保 fp32 无损；HEURISTIC 卷积算法搜索避免 EXHAUSTIVE 拖慢建会；
        # kSameAsRequested 让 arena 按需增长、不默认翻倍扩张。
        self.cuda_opts = {
            "device_id": 0,
            "use_tf32": "0",
            "arena_extend_strategy": "kSameAsRequested",
            "cudnn_conv_algo_search": "HEURISTIC",
        }
        self.provs = []
        if self.use_cuda:
            self.provs.append(("CUDAExecutionProvider", self.cuda_opts))
        self.provs.append("CPUExecutionProvider")

        self._sessions = {}
        self._segs_in = [set(s["in"]) for s in self.segs]
        self._needed = [None] * len(self.segs)
        acc = set()
        for j in range(len(self.segs) - 1, -1, -1):
            self._needed[j] = set(acc)
            acc |= self._segs_in[j]

    # ---- 会话获取（cache=False 时每次新建，跑完即删）----
    def _seg(self, j):
        if self.cache:
            if j not in self._sessions:
                self._sessions[j] = self._mk(j)
            return self._sessions[j]
        return self._mk(j)

    def _decide_opt(self):
        """是否走预优化缓存：env BSR_OPT 优先；默认有缓存文件就用、没有就回退原始子图。"""
        env = os.environ.get("BSR_OPT", "").strip().lower()
        if env in ("0", "false"):
            return False
        all_exist = all(os.path.exists(_opt_path(s["fn"])) for s in self.segs)
        if env in ("1", "true") and not all_exist:
            print("[BsrSplitSession] BSR_OPT=1 但优化缓存缺失，回退原始子图", flush=True)
            return False
        return all_exist

    def _mk(self, j):
        """建单个子图会话；启用优化缓存且文件存在时从优化文件重载（更快）。"""
        if self._use_opt:
            path = _opt_path(self.segs[j]["fn"])
            if not os.path.exists(path):
                path = os.path.join(HERE, self.segs[j]["fn"])   # 极端缺失：回退该子图原始文件
        else:
            path = os.path.join(HERE, self.segs[j]["fn"])
        return self.ort.InferenceSession(path, sess_options=self.so, providers=self.provs)

    def get_providers(self):
        return [p[0] if isinstance(p, tuple) else p for p in self.provs]

    def run(self, output_names, feed):
        """feed: {"mix": np.ndarray [1,2,588800]} -> [stft_real [12,1025,F,2]]。"""
        state = {k: np.asarray(v, dtype=np.float32) for k, v in feed.items()}
        captured = {}
        for j, s in enumerate(self.segs):
            so = self._seg(j)
            f = {t: state[t] for t in s["in"] if t in state}
            out = so.run(None, f)
            for nm, arr in zip([o.name for o in so.get_outputs()], out):
                state[nm] = arr
                if nm == self.final_out:
                    captured[nm] = arr
            # 只保留未来段还会消费的张量（全局张量随 state 自动传递）
            state = {k: v for k, v in state.items() if k in self._needed[j]}
            if not self.cache:
                del so
                gc.collect()
        if self.final_out in captured:
            return [captured[self.final_out]]
        return [state[self.final_out]]


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "preopt":
        preoptimize_models()
    else:
        # 自检：跑一个随机 chunk，确认形状与有限性
        import numpy as np
        np.random.seed(0)
        s = BsrSplitSession(cache=False, prefer_cuda=True)
        print("[BsrSplitSession] provider =", s.get_providers(), "use_opt =", s._use_opt)
        x = np.random.randn(1, 2, 588800).astype(np.float32)
        y = s.run(None, {"mix": x})[0]
        print("[BsrSplitSession] stft_real shape =", y.shape, "finite =", np.isfinite(y).all())
