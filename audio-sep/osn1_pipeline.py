# -*- coding: utf-8 -*-
"""osn1_pipeline.py — torch-free OSN1 默认 onset 检测器（零 torch 全链路）

设计
----
  · 用 BS-RoFormer-SW ONNX 取代 Demucs 出 6 通道（砍掉 torch 分离依赖）
  · 6 通道 mel（与训练时 demucs_mel._one_mel 完全一致）喂 OnsetNet ONNX
  · 做用户要的【两道过滤】：
       ① 能量小的 onset 砍掉            —— _gate_silent_onsets
       ② 同音毫秒内重复只保留能量最高者 —— _merge_burst_onsets
  · 全程 torch-free：onnxruntime(CUDA给BSR / CPU给OnsetNet) + librosa + numpy

规格（与 demucs_mel / chart_repr 严格一致）
-----------------------------------------
  SR=22050  HOP=128  N_MELS=128  N_FFT=2048  HOP_MS≈5.805ms
  OnsetNet 6 通道顺序 = [drums, bass, other, vocals, full, accomp]
  BSR 6 源顺序(yaml)  = [bass, drums, other, vocals, guitar, piano]

BSR → OnsetNet 通道映射（尽量贴合 Demucs 语义，不重训）
  drums  <- BSR drums
  bass   <- BSR bass
  other  <- BSR other + guitar + piano   （Demucs other = 除 drums/bass/vocals 外全部）
  vocals <- BSR vocals
  full   <- 原始混音
  accomp <- 混音 − vocals
"""
from __future__ import annotations
import os
import glob
import time
import numpy as np
import librosa
from ort_cli_shim import OrtCliShim, ORT_CLI_ENABLED, ORT_CLI_CUDA

# ============================ 全局规格 ============================
SR = 22050
HOP = 128
N_MELS = 128
N_FFT = 2048
HOP_MS = 1000.0 * HOP / SR            # ≈ 5.805 ms

# OnsetNet 训练时的 6 通道语义顺序
ONSET_CH = ["drums", "bass", "other", "vocals", "full", "accomp"]
# 7 通道（重训用）：BSR 全 6 源拆独立通道 + 原混音 full，丢冗余 accomp
ONSET_CH7 = ["bass", "drums", "other", "vocals", "guitar", "piano", "full"]
# BSR-ONNX 输出的 6 源顺序（BS-Rofo-SW-Fixed.yaml: training.instruments）
BSR_SRC = ["bass", "drums", "other", "vocals", "guitar", "piano"]

# ============================ 门控/合并常量（与 inference_stage2.py 一致）===========================
_SIL_ABS_RMS = 3e-4     # 绝对底：数字静音/解码底噪(~1e-4)之上、可听内容之下
_SIL_REL_RMS = 0.01     # 相对底：比"典型 onset 响度"(中位)低 40dB 视为不可听
_BURST_ABS_MS = 35.0    # 绝对不应期：<35ms 必为同事件机械双触发
_BURST_LINK_MS = 70.0   # 谷深判据适用上限：更远的一律独立
_BURST_VR = 0.55        # 谷深比阈值：两踩点间概率不低于强者 55% = 同一平台

# ============================ 模型路径 ============================
HERE = os.path.dirname(os.path.abspath(__file__))
# fp32 无损基准版：零精度损失，torch-free 形态（管线原状）。
# BSR 显存状态（9-24 实测，torch-free / fp32 无损）：
#   原始模型与「分块 MEA」版(bsr_sw_mea2.onnx)在 ORT 1.30 CUDA 上实测峰值都 ~7.8GB
#   （8188MiB 卡 - 显示基线 ~1GB ≈ 仅 7GB 可用 -> 显示占用一高就 OOM，空闲时才塞得下）。
#   mea2 是 fp32 逐位无损(CPU 比对 max|Δ|=0)且结构更优，但 ORT 的 CUDA 分配器把实际
#   占用照样堆到 ~7.8GB，分块没在实际占用上见效（opt=basic 关融合也仍是 7.8GB）。
#   gpu_mem_limit 在本 ORT 1.30 实测被忽略(设 1/2/4GB 都抢 7.8GB,设1GB还拖到188s)，
#   不能用它压显存。flash attention 不可用(1.30 即最新,无更新版,MHA 不认 use_flash_attention)。
# ⇒ 现状：8GB 卡"贴边能跑、显示忙则 OOM"。真正能压显存的只剩 TensorRT EP(重、待验)。
# 默认沿用已 validate 的 bsr_sw_mha.onnx(fp32 逐位无损, verify_osn1_pipeline 全绿)；
# bsr_sw_mea2.onnx 为等价的候选(同样 ~7.8GB,无 VRAM 收益)。
DEFAULT_BSR_ONNX = os.path.join(HERE, "onnx_poc", "bsr_sw_mha.onnx")  # fp32 无损(ORT 分配器限制→仍 ~7.8GB)
DEFAULT_OSN1_ONNX = os.path.join(HERE, "models", "osn1", "onset_net.onnx")

# BSR 推理模式（2026-09-25）：True=用【图分割链】(BsrSplitSession)，
# CUDA 峰值 ~3.5~4.1GB（取决于段数档位，见 bsr_split_runner 的分档表），稳进 8GB 卡；
# False=回退单体 bsr_sw_mha.onnx（~7.8GB 贴边 OOM 风险）。切分已证逐位等价。
BSR_USE_SPLIT = True

# BSR 分块【流水线】（2026-09-26）：True=GPU 推理第 N+1 块的同时，CPU 工作线程做第 N 块的
# iSTFT + OLA。二者无数据依赖（iSTFT 只吃本块的谱），串行时 GPU 每块白等这 0.65s。
# 实测（2026-09-26 _probe_pipe.py，5 chunk 稳态）：串行 6.445s → 流水线 5.998s（-6.9%，约-0.45s）。
# ⚠ 真正的大段 GPU 空转是【建会话 1.85s/chunk(28.7%)】——那是 CPU 解析+图优化，期间 GPU 纯等，
#   本流水线治不了它（要另想办法：预优化模型缓存 / 跨块复用会话）。
# 数学累加次序逐块一致（worker 仍按主序完成同一块的叠加）⇒ 输出与串行【逐位一致】
#   （_probe_pipe 已验 6 源全有限；verify_osn1_pipeline 全程走此路径需回归）。
# 环境变量 BSR_PIPELINE=0 可临时回退做 A/B。
BSR_PIPELINE = os.environ.get("BSR_PIPELINE", "1").strip() not in ("0", "false", "False")

# BSR 推理参数（BS-Rofo-SW-Fixed.yaml: inference）
BSR_CHUNK = 588800          # @44100，≈13.35s（模型固定输入长度）
BSR_NUM_OVERLAP = 2
BSR_HOP = 512
BSR_NFFT = 2048
BSR_WIN = 2048
# OnsetNet ONNX 的时间维是【真动态】的（符号维 'T'，2026-09-23 打通），
# 整首歌可以一把送进去，不再有固定窗限制。下面的 max_frames 只是极端长音频的
# 内存保险丝：默认 0 = 完全不限制（即"多长都行"）。
OSN1_MAX_FRAMES = 0

# CUDA 13 运行时文件名（与 venv/nvidia 内的 cu13 版 dll 同名，需按 PATH 挂载）
_CU13_DLLS = ("cublasLt64_13.dll", "cudnn64_9.dll")
_cuda13_orig_path = None
_bsr_sess = None
_osn1_sess = None


# ============================ CUDA 13 运行时挂载（仅给 BSR ONNX 用）============================
def _mount_cuda13():
    """onnxruntime 1.30 的 CUDA EP 需 CUDA13+cuDNN9；torch 自带 cu12 的同名 dll 会冲突，
    故只把 cu13 版 dll 目录塞进 PATH，绝不 import torch。
    挂载顺序须为 [cu13/bin/x86_64, cudnn/bin]（先 cu13 后 cudnn）并 add_dll_directory，
    否则 ORT 的 CUDA arena 显存行为异常（任何 gpu_mem_limit 都 BFCArena 失败）。"""
    global _cuda13_orig_path
    nv = os.path.join(HERE, "venv", "Lib", "site-packages", "nvidia")
    _d = [os.path.join(nv, "cu13", "bin", "x86_64"), os.path.join(nv, "cudnn", "bin")]
    dirs = [x for x in _d if os.path.isdir(x)]
    for x in dirs:
        try:
            os.add_dll_directory(x)
        except Exception:
            pass
    _cuda13_orig_path = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join(dirs) + os.pathsep + _cuda13_orig_path
    return set(dirs)


def _unmount_cuda13():
    global _cuda13_orig_path
    if _cuda13_orig_path is not None:
        os.environ["PATH"] = _cuda13_orig_path
        _cuda13_orig_path = None


# ============================ ONNX 会话 ============================
def get_bsr_session(onnx_path=DEFAULT_BSR_ONNX):
    global _bsr_sess
    if _bsr_sess is None:
        if ORT_CLI_ENABLED:
            # 推理外包给 C# CLI（AdofaiOrt bsr-stft）；单块 stft_real 由 C# 算，Python 仍做 iSTFT+OLA。
            print(f"[osn1] BSR 会话 → C# CLI (AdofaiOrt)，CPU={not ORT_CLI_CUDA}")
            _bsr_sess = OrtCliShim("bsr")
            return _bsr_sess
        if BSR_USE_SPLIT:
            # 26 段图分割链：CUDA 峰值 ~3.75GB（单体 ~7.8GB），逐位等价、fp32 零损失。
            from bsr_split_runner import BsrSplitSession
            _bsr_sess = BsrSplitSession(cache=False, prefer_cuda=True)
            print(f"[osn1] BSR 会话就绪(图分割链) | provider={_bsr_sess.get_providers()} "
                  f"| {len(_bsr_sess.segs)} 段 | {os.path.basename(onnx_path)}")
        else:
            _mount_cuda13()
            import onnxruntime as ort
            # BSR 是显存大户（ORT 1.30 实测 ~7.8GB，gpu_mem_limit 本版被忽略，无法压）。
            # use_tf32=0 保 fp32 无损（否则 TF32 会引入 ~5e-4 误差）；HEURISTIC 避免
            # EXHAUSTIVE 卷积算法搜索拖慢建会。
            so = ort.SessionOptions()
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
            cuda_opts = {
                "device_id": 0,
                "use_tf32": "0",
                "arena_extend_strategy": "kSameAsRequested",  # 按真实需求增长，避免默认翻倍扩张
                "cudnn_conv_algo_search": "HEURISTIC",
            }
            provs = []
            if "CUDAExecutionProvider" in ort.get_available_providers():
                provs.append(("CUDAExecutionProvider", cuda_opts))
            provs.append("CPUExecutionProvider")
            _bsr_sess = ort.InferenceSession(onnx_path, sess_options=so, providers=provs)
            print(f"[osn1] BSR 会话就绪 | provider={_bsr_sess.get_providers()} | {os.path.basename(onnx_path)}")
    return _bsr_sess


def get_osn1_session(onnx_path=DEFAULT_OSN1_ONNX):
    global _osn1_sess
    if _osn1_sess is None:
        if ORT_CLI_ENABLED:
            print(f"[osn1] OnsetNet 会话 → C# CLI (AdofaiOrt onsetnet)，model={os.path.basename(onnx_path)}")
            _osn1_sess = OrtCliShim("onset", onnx_path)
            return _osn1_sess
        import onnxruntime as ort
        # OnsetNet 极小，纯 CPU 跑最快且无 CUDA 版本冲突
        _osn1_sess = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
        print(f"[osn1] OnsetNet 会话就绪 | provider={_osn1_sess.get_providers()} | {os.path.basename(onnx_path)}")
    return _osn1_sess


# ============================ mel 计算（与 demucs_mel._one_mel 逐字节一致）============================
def _one_mel(y):
    """单声道波形 -> log-mel (128, T)，HOP=128 细网格。"""
    mel = librosa.feature.melspectrogram(y=y, sr=SR, n_fft=N_FFT,
                                          hop_length=HOP, n_mels=N_MELS)
    return np.log1p(mel).astype(np.float32)


# ============================ BSR 分离（ONNX + iSTFT + 重叠相加）============================
def _bsr_istft_chunk(stft_real, n_samples):
    """单个 BSR chunk 的复数谱 -> 6 源单声道音频 @44100。

    stft_real: (12, 1025, F, 2)  6 源 × 2ch，实部/虚部分开。
    返回 (6, n_samples) 单声道（双声道平均，与 demucs_mel 一致）。

    注：librosa.istft(center=True) 已自行去掉 center 两侧填充，输出长度恰为
    (F-1)*hop+win-2*(n_fft//2) = 588800（实测；无需再切 pad）。
    对齐已由 probe 验证：六源之和与原混音 lag=0 余弦=1.0000。
    """
    out = np.zeros((6, n_samples), np.float32)
    for s in range(6):
        c0 = stft_real[s * 2]
        c1 = stft_real[s * 2 + 1]
        z0 = c0[..., 0].astype(np.float32) + 1j * c0[..., 1].astype(np.float32)
        z1 = c1[..., 0].astype(np.float32) + 1j * c1[..., 1].astype(np.float32)
        a0 = librosa.istft(z0, n_fft=BSR_NFFT, hop_length=BSR_HOP,
                           win_length=BSR_WIN, center=True, window="hann")
        a1 = librosa.istft(z1, n_fft=BSR_NFFT, hop_length=BSR_HOP,
                           win_length=BSR_WIN, center=True, window="hann")
        # 长度防御：正常恒为 n_samples
        if len(a0) < n_samples:
            a0 = np.pad(a0, (0, n_samples - len(a0)))
        if len(a1) < n_samples:
            a1 = np.pad(a1, (0, n_samples - len(a1)))
        out[s] = ((a0[:n_samples] + a1[:n_samples]) * 0.5).astype(np.float32)
    return out


def _ola_window(i, n_total, step, chunk):
    """边界感知交叉淡化窗：只在【确有相邻块】的一侧做升/降沿，避免
    全局首块前半段(6.7s)被 Hann 从 0 淡入导致开头踩点被压。"""
    w = np.ones(chunk, np.float32)
    if i > 0:                                   # 左侧有前块 -> 升沿 0->1
        t = np.arange(step, dtype=np.float32)
        w[:step] = 0.5 - 0.5 * np.cos(np.pi * t / step)
    if i + step < n_total:                      # 右侧有后块 -> 降沿 1->0
        t = np.arange(step, dtype=np.float32)
        w[chunk - step:] = 0.5 + 0.5 * np.cos(np.pi * t / step)
    return w


def bsr_separate(y44, sess=None):
    """y44: (2, N44) float32 立体声 @44100 -> dict{源名: 单声道@44100}。

    按 chunk=588800 / overlap=2 分块，每块 iSTFT 后用 Hann 窗重叠相加
    （50% 重叠下 Hann 满足 COLA，拼接无接缝）。

    双模式（BSR_PIPELINE）：
      True（默认）— 生产者-消费者流水线：主线程只喂 GPU 推理并把谱推进队列，
                    CPU 工作线程取谱做 iSTFT+OLA。两块之间 GPU 不再空等 CPU。
      False       — 老的单线程串行（A/B 对照 / 排障用）。
    两种模式的累加次序完全相同 ⇒ 输出逐位一致（verify_osn1_pipeline 会钉住这一点）。
    """
    if sess is None:
        sess = get_bsr_session()
    y44 = np.asarray(y44, dtype=np.float32)
    if y44.ndim == 1:
        y44 = np.stack([y44, y44], axis=0)
    N44 = y44.shape[-1]
    step = BSR_CHUNK // BSR_NUM_OVERLAP
    out = np.zeros((6, N44), np.float32)
    wsum = np.zeros((6, N44), np.float32)
    starts = list(range(0, N44, step))
    n_run = len(starts)

    def _prog(k, t=None):
        # 逐块进度：让 backend._run_stream 能推 UI 进度，并在卡死时从日志看出停在哪一块。
        # 之前 BSR 分离全程零输出 → UI 停在「第2步」像死机，外力（关窗/取消）强杀后 rc=-1 且查无痕迹。
        msg = "[bsr] 分离进度 %d/%d" % (k, n_run)
        if t is not None:
            msg += " (本块 %.2fs)" % t
        print(msg, flush=True)

    print("[bsr] 分离开始：共 %d 块（chunk=%.1fs, overlap=%d, split=%s, pipeline=%s）"
          % (n_run, BSR_CHUNK / 44100.0, BSR_NUM_OVERLAP,
             "on" if BSR_USE_SPLIT else "off", "on" if BSR_PIPELINE else "off"), flush=True)

    def _prep(i):
        part = y44[:, i:i + BSR_CHUNK]
        if part.shape[-1] < BSR_CHUNK:
            part = np.pad(part, ((0, 0), (0, BSR_CHUNK - part.shape[-1])))
        return part

    def _accumulate(i, spec):
        """单块的谱 -> 音频 -> 窗 -> 叠加（只在 CPU 线程或串行主线程里调用）。"""
        audio = _bsr_istft_chunk(spec, BSR_CHUNK)        # (6, BSR_CHUNK)
        win = _ola_window(i, N44, step, BSR_CHUNK)
        n_left = min(BSR_CHUNK, N44 - i)
        out[:, i:i + n_left] += audio[:, :n_left] * win[:n_left][None, :]
        wsum[:, i:i + n_left] += win[:n_left][None, :]

    if not BSR_PIPELINE or n_run <= 1:
        for k, i in enumerate(starts, 1):
            t0 = time.perf_counter()
            spec = sess.run(None, {"mix": _prep(i)[None]})[0]   # (12,1025,F,2)
            _accumulate(i, spec)
            _prog(k, time.perf_counter() - t0)
    else:
        import queue as _queue
        import threading as _threading
        # maxsize=1：队列里最多躺 1 个谱（≈113MB 内存），保证"GPU 只领先 1 块"，
        # 内存/显存都不额外膨胀，同时足够把 1.3s 的 CPU 尾巴藏进 GPU 推理里。
        q = _queue.Queue(maxsize=1)
        errs = []

        def _worker():
            failed = False
            while True:
                item = q.get()
                if item is None:
                    return
                if failed:
                    continue          # 出错后仍继续消费，避免主线程 put 永久阻塞
                try:
                    _accumulate(*item)
                except BaseException as e:      # noqa: BLE001 - 必须原样传回主线程
                    errs.append(e)
                    failed = True

        th = _threading.Thread(target=_worker, name="bsr-istft", daemon=True)
        th.start()
        try:
            for k, i in enumerate(starts, 1):
                t0 = time.perf_counter()
                spec = sess.run(None, {"mix": _prep(i)[None]})[0]   # GPU 侧
                q.put((i, spec))                                    # 甩给 CPU 侧
                _prog(k, time.perf_counter() - t0)
        finally:
            q.put(None)
            th.join()
        if errs:
            raise errs[0]

    nz = wsum > 1e-6
    out[nz] /= wsum[nz]
    return {BSR_SRC[s]: out[s] for s in range(6)}, n_run


# ============================ 6 通道 mel 组装（映射到 OnsetNet 语义）============================
def build_6ch_mel(stems, y_full22):
    """stems: BSR 6 源单声道@44100 的 dict；y_full22: 原混音@22050 单声道(长度 L)。
    返回 mel6 (6,128,T) float32 —— 通道顺序 = ONSET_CH。
    """
    y_full22 = np.asarray(y_full22, dtype=np.float32)
    L = len(y_full22)

    def _to22k(sig44):
        s = librosa.resample(np.asarray(sig44, dtype=np.float32),
                             orig_sr=44100, target_sr=SR)
        if len(s) > L:
            s = s[:L]
        elif len(s) < L:
            s = np.pad(s, (0, L - len(s)))
        return s.astype(np.float32)

    drums = _to22k(stems["drums"])
    bass = _to22k(stems["bass"])
    other = _to22k(stems["other"]) + _to22k(stems["guitar"]) + _to22k(stems["piano"])
    vocals = _to22k(stems["vocals"])
    full = y_full22
    accomp = np.clip(y_full22 - vocals, -1.0, 1.0)

    chans = [drums, bass, other, vocals, full, accomp]
    mel6 = np.stack([_one_mel(c) for c in chans], axis=0)   # (6,128,T)
    return mel6


def build_7ch_mel(stems, y_full22):
    """stems: BSR 6 源单声道@44100 的 dict；y_full22: 原混音@22050 单声道(长度 L)。
    返回 mel7 (7,128,T) float32 —— 通道顺序 = ONSET_CH7
      [bass, drums, other, vocals, guitar, piano, full]
    与 build_6ch_mel 的差异：把原 6ch 里被合并进 other 的 guitar/piano 拆成独立
    通道，并明确保留原混音 full；丢掉派生冗余的 accomp（=full−vocals 线性组合，
    信息已被 full/vocals 包含，对模型零损失）。
    """
    y_full22 = np.asarray(y_full22, dtype=np.float32)
    L = len(y_full22)

    def _to22k(sig44):
        s = librosa.resample(np.asarray(sig44, dtype=np.float32),
                             orig_sr=44100, target_sr=SR)
        if len(s) > L:
            s = s[:L]
        elif len(s) < L:
            s = np.pad(s, (0, L - len(s)))
        return s.astype(np.float32)

    bass = _to22k(stems["bass"])
    drums = _to22k(stems["drums"])
    other = _to22k(stems["other"])            # 仅 BSR other（不再掺 guitar/piano）
    vocals = _to22k(stems["vocals"])
    guitar = _to22k(stems["guitar"])
    piano = _to22k(stems["piano"])
    full = y_full22

    chans = [bass, drums, other, vocals, guitar, piano, full]
    mel7 = np.stack([_one_mel(c) for c in chans], axis=0)   # (7,128,T)
    return mel7


def build_7ch_mel_focus(stems, y_full22, focus_wave):
    """以 focus_wave 为主音轨的 7ch 视图：full 通道换成 focus_wave（已@22050 单声道），
    其余 6 通道给真实 BSR 分离上下文（保持训练分布）。

    用途：综合 7ch 模型靠 drums/bass 等瞬态点火，only-other(纯 lead) 段落这些通道全空
    → 模型整体低置信 → 主谱/旋律 lane 死区。构造「以该音轨为主」的视图后，full 通道承载
    该音轨自身的清晰瞬态，模型在该段落内部能稳定点火。detect() 主谱与 stemjson 三路
    per-stem 推理都用它。
    """
    y_full22 = np.asarray(y_full22, dtype=np.float32)
    L = len(y_full22)

    def _to22k(sig44):
        s = librosa.resample(np.asarray(sig44, dtype=np.float32),
                             orig_sr=44100, target_sr=SR)
        if len(s) > L:
            s = s[:L]
        elif len(s) < L:
            s = np.pad(s, (0, L - len(s)))
        return s.astype(np.float32)

    bass = _to22k(stems["bass"])
    drums = _to22k(stems["drums"])
    other = _to22k(stems["other"])
    vocals = _to22k(stems["vocals"])
    guitar = _to22k(stems["guitar"])
    piano = _to22k(stems["piano"])
    full = np.asarray(focus_wave, dtype=np.float32)
    if len(full) > L:
        full = full[:L]
    elif len(full) < L:
        full = np.pad(full, (0, L - len(full)))
    chans = [bass, drums, other, vocals, guitar, piano, full]
    return np.stack([_one_mel(c) for c in chans], axis=0)   # (7,128,T)


# ============================ OnsetNet ONNX 推理（时间维全动态，多长都一把过）============================
def run_osn1(mel6, onnx_path=DEFAULT_OSN1_ONNX, max_frames=OSN1_MAX_FRAMES):
    """mel (C,128,T) -> logits (T,) float32（C=通道数，由模型输入维度决定，现 7ch=7）。

    2026-09-23 起导出的 ONNX 时间维是真动态（符号维 'T'），T=16 ~ T=31000(3分钟)
    已实测全部 OK，因此这里**不再分块、不再补零**，整个 mel 一次前向。
    max_frames 仅在极端长音频（如 10 分钟以上）时作为内存保险丝兜底，
    默认 0 = 不限制；触发时只是内部分刀，对调用方不可见。
    """
    sess = get_osn1_session(onnx_path)
    mel6 = np.ascontiguousarray(np.asarray(mel6, dtype=np.float32))
    T = mel6.shape[-1]
    if max_frames and T > max_frames:
        logits = np.empty(T, dtype=np.float32)
        for s in range(0, T, max_frames):
            e = min(T, s + max_frames)
            logits[s:e] = sess.run(None, {"mel": mel6[None, :, :, s:e]})[0][0]
        return logits
    return sess.run(None, {"mel": mel6[None]})[0][0].astype(np.float32)


# ============================ 峰值拾取（移植 predict_onset_frames 后处理，纯 numpy）============================
def detect_onsets(prob, peak_thr=0.3, min_dist=2, thr_local=0.15, local_win=64):
    """sigmoid 后的概率 (T,) -> onset 帧列表（@128 网格）。

    与 onset_net.predict_onset_frames 的峰值拾取逐一对应：
      全局相对阈值保最强段 + 滑动窗口局部阈值补回中段弱奏漏检。
    """
    prob = np.asarray(prob, dtype=np.float32)
    T = prob.shape[0]
    peak = float(prob.max())
    if peak < 1e-6:
        return [], prob
    th_global = max(peak_thr * peak, 0.02 * peak)
    win = max(8, int(local_win))
    frames, last = [], -10 ** 9
    for f in range(1, T - 1):
        if prob[f] >= prob[f - 1] and prob[f] > prob[f + 1]:
            lo, hi = max(0, f - win), min(T, f + win + 1)
            win_peak = float(prob[lo:hi].max())
            local_ratio = win_peak / peak if peak > 1e-6 else 0.0
            floor_local = 0.01 * win_peak if local_ratio < 0.3 else 0.04
            th_local_f = max(thr_local * win_peak, floor_local)
            if prob[f] > th_global or prob[f] > th_local_f:
                if f - last < min_dist:
                    continue
                frames.append(int(f))
                last = f
    return frames, prob


# ============================ 门控①：能量小的 onset 砍掉 ================================
def gate_silent_onsets(y, frames, prob, sr=SR, hop=HOP, win_ms=35.0):
    """能量门控：砍掉落在(近)静音里的伪 onset。

    y: 全混合单通道波形(采样率 sr)；frames: onset 帧列表(@hop 网格)；
    prob: (T,) onset 概率。返回 (门控后 frames, prob)——被砍帧位及其 ±1 邻域概率清零。
    阈值 = max(绝对底, 1% × 全部 onset 的中位本地 RMS)。
    """
    if not frames:
        return frames, prob
    y = np.asarray(y)
    if y.size == 0:
        return frames, prob
    half = max(1, int(win_ms * 1e-3 * sr))

    def _rms(f):
        c = int(f) * hop
        lo, hi = max(0, c - half), min(y.size, c + half)
        if hi <= lo:
            return 0.0
        return float(np.sqrt(np.mean(np.square(y[lo:hi]))))

    lv = np.array([_rms(f) for f in frames])
    ref = float(np.median(lv)) if lv.size else 0.0
    thr = max(_SIL_ABS_RMS, _SIL_REL_RMS * ref)
    keep = lv >= thr
    if not keep.any():
        return frames, prob
    kept = [int(f) for f, k in zip(frames, keep) if k]
    n_cut = len(frames) - len(kept)
    if n_cut:
        prob = np.array(prob, copy=True)
        for f, k in zip(frames, keep):
            if not k:
                for d in (-1, 0, 1):
                    i = int(f) + d
                    if 0 <= i < len(prob):
                        prob[i] = 0.0
        print(f"[osn1] ①静音门控: 砍掉 {n_cut} 个静音段伪 onset（剩 {len(kept)}）")
    return kept, prob


# ============================ 门控②：同音毫秒内重复只保留能量最高者 ================================
def merge_burst_onsets(frames, prob, min_keep=2, enabled=True):
    """判据 F: 合并同一事件的连发伪踩点(全模式)，簇内保留概率最强一击。

    enabled=False: 原样返回（供 A/B 对比关闭拦截）。
    frames: onset 帧列表(@128 网格)；prob: (T,) 概率。
    返回 (合并后 frames, prob)——被并帧位及其 ±1 邻域概率清零。
    病态输入(合并后不足 min_keep)不合并，安全回退。
    """
    frames = [int(f) for f in frames]
    if not enabled:
        print(f"[osn1] ②同音多采拦截=关: 跳过合并，保留全部 {len(frames)} 个踩点")
        return frames, prob
    if len(frames) < min_keep:
        return frames, prob
    p = np.asarray(prob)

    def _vr(a, b):
        seg = p[a + 1:b]
        if seg.size == 0:
            return 1.0
        mx = max(float(p[a]), float(p[b]))
        if mx <= 1e-9:
            return 1.0
        return float(seg.min()) / mx

    keep, i, n_cut, biggest = [], 0, 0, (0, -1.0)
    while i < len(frames):
        j = i
        while j + 1 < len(frames):
            gap = (frames[j + 1] - frames[j]) * HOP_MS
            if gap < _BURST_ABS_MS or (
                    gap <= _BURST_LINK_MS and _vr(frames[j], frames[j + 1]) >= _BURST_VR):
                j += 1
            else:
                break
        if j > i:
            mem = frames[i:j + 1]
            ps = [float(p[f]) for f in mem]
            best = mem[int(np.argmax(ps))]
            keep.append(best)
            n_cut += len(mem) - 1
            if len(mem) > biggest[0]:
                biggest = (len(mem), best * HOP_MS)
        else:
            keep.append(frames[i])
        i = j + 1
    if n_cut == 0 or len(keep) < min_keep:
        return frames, prob
    prob = np.array(prob, copy=True)
    _kept = set(keep)
    for f in frames:
        if f in _kept:
            continue
        for d in (-1, 0, 1):
            k = f + d
            if 0 <= k < len(prob):
                prob[k] = 0.0
    print(f"[osn1] ②连发簇合并: 裁掉 {n_cut} 个同事件再触发伪踩点，"
          f"合并 {len(frames) - len(keep)} 簇(最大簇 {biggest[0]} 击@"
          f"{biggest[1] / 1000:.1f}s)，保留 {len(keep)} 个")
    return keep, prob


# ============================ 前导静音裁剪（与 inference_stage2._leading_silence_frames 一致）============================
_SIL_TRIM_RMS = 1e-3    # 20ms 窗 RMS 越过此值视为有内容

def leading_silence_frames(y, sr=SR, hop=HOP, win_ms=20.0, pre_ms=25.0, sustain=3):
    """返回应裁掉的前导静音帧数 n0（0=不裁）。"""
    y = np.asarray(y)
    total_f = y.size // hop
    keep_f = max(1, int(0.5 * sr / hop))
    if total_f <= keep_f:
        return 0
    half = max(1, int(win_ms * 1e-3 * sr) // 2)
    rms = np.empty(total_f, np.float64)
    for f in range(total_f):
        c = f * hop
        lo, hi = max(0, c - half), min(y.size, c + half)
        rms[f] = float(np.sqrt(np.mean(np.square(y[lo:hi])))) if hi > lo else 0.0
    cross = -1
    for f in range(total_f - sustain):
        if all(rms[f + k] > _SIL_TRIM_RMS for k in range(sustain)):
            cross = f
            break
    if cross < 0:
        return 0
    n0 = max(0, cross - int(pre_ms * 1e-3 * sr / hop))
    return min(n0, total_f - keep_f)


# ============================ 顶层编排 ================================
def detect(audio_path, dual_block=True, do_lead_trim=True,
           bsr_onnx=DEFAULT_BSR_ONNX, osn1_onnx=DEFAULT_OSN1_ONNX):
    """音频 -> OSN1 默认 onset 检测（含两道用户要求的过滤）。

    参数
    ----
      dual_block : 是否启用②同音多采拦截（False=A/B 关拦截对比）
      do_lead_trim: 是否裁剪前导静音（与参考 generate() 一致，裁掉时长补进 trim_ms）
    返回 dict：
      frames      —— onset 帧列表(@128 网格，已加回前导静音偏移，即原曲时间轴)
      prob        —— (T,) 全曲 onset 概率
      trim_ms     —— 前导静音裁剪毫秒数（时间戳已含此偏移，供对齐用）
      n_raw / n_sil / n_final —— 各阶段踩点计数
      sr / hop / hop_ms
      timestamps_ms —— 每帧绝对毫秒（= frame*HOP_MS + trim_ms）
    """
    # —— 载入音频：原混音 @22050（单声道，长度基准 L）+ 分离输入 @44100 立体声 ——
    y_full22, _ = librosa.load(audio_path, sr=SR, mono=True)        # (L,)
    y44, _ = librosa.load(audio_path, sr=44100, mono=False)         # (2, N44)
    if y44.ndim == 1:
        y44 = np.stack([y44, y44], axis=0)
    if y44.shape[0] == 1:
        y44 = np.repeat(y44, 2, axis=0)
    y_full22 = y_full22.astype(np.float32)
    y44 = y44.astype(np.float32)

    # —— 前导静音裁剪：与参考 generate() 一致 ——
    #   分离在【全曲】上跑，之后只在 mel 帧级切掉前导静音（n0 帧 @22050）。
    #   （务必不要在 y44 上按 n0*HOP 采样切——44100 与 22050 网格不同，会切一半时长。）
    n0 = leading_silence_frames(y_full22) if do_lead_trim else 0
    trim_ms = n0 * HOP_MS

    # —— ① BSR ONNX 分离 6 源（全曲）——
    sess = get_bsr_session(bsr_onnx)
    stems, n_run = bsr_separate(y44, sess)
    print(f"[osn1] BSR 分离完成：{n_run} 个 chunk，6 源 = {list(stems.keys())}")

    # —— ② 组装 7 通道 mel（全曲，映射到 OnsetNet 语义；7ch 重训模型）——
    mel7 = build_7ch_mel(stems, y_full22)   # (7,128,T)，T 对应全曲

    # —— ②b 组装「以主音轨(lead)为主」的 7ch 视图，补 only-other/纯 lead 段落死区 ——
    #     auto 选 other/piano/guitar 中最强一根作为 lead；无则回退原混音。
    #     综合视图负责鼓/贝斯出现的段落，lead 视图负责只有主音轨的段落，二者并集覆盖全曲。
    _lead_cand = [n for n in ("other", "piano", "guitar") if n in stems]
    if _lead_cand:
        _lead_name = max(_lead_cand, key=lambda n: float(np.sqrt(np.mean(np.square(stems[n])))))
        _lead44 = stems[_lead_name]
    else:
        _lead44 = None

    def _to22k_local(sig44):
        s = librosa.resample(np.asarray(sig44, dtype=np.float32),
                             orig_sr=44100, target_sr=SR)
        _L = len(y_full22)
        if len(s) > _L:
            s = s[:_L]
        elif len(s) < _L:
            s = np.pad(s, (0, _L - len(s)))
        return s.astype(np.float32)

    _lead_wave = _to22k_local(_lead44) if _lead44 is not None else y_full22
    mel7_lead = build_7ch_mel_focus(stems, y_full22, _lead_wave)

    # —— 前导静音在 mel 帧级切掉；门控波形同步切 ——
    if n0 > 0:
        mel7 = np.ascontiguousarray(mel7[:, :, n0:])
        mel7_lead = np.ascontiguousarray(mel7_lead[:, :, n0:])
        y_for_gate = y_full22[n0 * HOP:] if (n0 * HOP) < len(y_full22) else y_full22
    else:
        y_for_gate = y_full22

    # —— ③ OnsetNet ONNX 推理 -> 概率（综合 + lead 视图逐帧取最大，并集补死区）——
    logits = run_osn1(mel7, osn1_onnx)
    prob = 1.0 / (1.0 + np.exp(-logits))
    prob = prob.astype(np.float32)
    logits_lead = run_osn1(mel7_lead, osn1_onnx)
    prob_lead = (1.0 / (1.0 + np.exp(-logits_lead))).astype(np.float32)
    prob = np.maximum(prob, prob_lead)

    # —— ④ 峰值拾取（原始候选）——
    frames, prob = detect_onsets(prob)
    n_raw = len(frames)

    # —— ⑤ 门控①：能量小的砍掉 ——
    frames, prob = gate_silent_onsets(y_for_gate, frames, prob)
    n_sil = len(frames)

    # —— ⑥ 门控②：同音毫秒内重复只保留能量最高者 ——
    frames, prob = merge_burst_onsets(frames, prob, enabled=bool(dual_block))
    n_final = len(frames)

    # —— 加回前导静音偏移，得到原曲时间轴帧 ——
    frames_abs = [f + n0 for f in frames]
    ts_ms = [int(round(f * HOP_MS + trim_ms)) for f in frames_abs]

    print(f"[osn1] 踩点：raw={n_raw} -> 静音门控后={n_sil} -> 连发合并后={n_final}")
    return {
        "frames": frames_abs,
        "prob": prob,
        "trim_ms": trim_ms,
        "n_raw": n_raw, "n_sil": n_sil, "n_final": n_final,
        "sr": SR, "hop": HOP, "hop_ms": HOP_MS,
        "timestamps_ms": ts_ms,
    }


# ============================ CLI 验证 ================================
if __name__ == "__main__":
    import sys
    ap = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "test_mix.wav")
    dual = "--no-block" not in sys.argv
    t0 = __import__("time").time()
    res = detect(ap, dual_block=dual)
    dt = __import__("time").time() - t0
    print(f"\n===== 结果 =====")
    print(f"文件: {ap}")
    print(f"耗时: {dt:.2f}s")
    print(f"踩点计数: raw={res['n_raw']}  静音门控后={res['n_sil']}  最终={res['n_final']}")
    print(f"前导静音裁剪: {res['trim_ms']:.0f}ms")
    print(f"前 20 个时间戳(ms): {res['timestamps_ms'][:20]}")
