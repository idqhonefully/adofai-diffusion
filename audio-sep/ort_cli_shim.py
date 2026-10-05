# -*- coding: utf-8 -*-
"""ort_cli_shim.py — 让 Python 把 ONNX 推理外包给 C# CLI(AdofaiOrt)，从而砍掉 Python 的 onnxruntime 依赖。

隔离、可一键删除：仅在环境变量 ADOFAI_ORT_CLI=1 时启用；不设置则 Python 走原 onnxruntime 路径，
已确认正确的管线逻辑（bsr_separate 的 iSTFT+OLA、detect_onsets、门控、JSON 组装）完全不动。

接入点（在 osn1_pipeline.py / osn1_stemjson.py 的 session 获取函数里加一行）：
  - get_bsr_session   → OrtCliShim("bsr")        （single-chunk stft_real 由 C# 算，Python 仍做 OLA）
  - get_osn1_session  → OrtCliShim("onset", path)   # OSN1 全 ONNX onset 统一走 onset_net.onnx

shim.run 的接口与 onnxruntime.InferenceSession.run 对齐：
  BSR  : feed={"mix":(1,2,N)}        -> [stft_real(12,1025,F,2)]
  onset: feed={"mel":(1,6,128,T)}    -> [logits(1,T)]   （caller 取 [0][0] 得 (T,)）

注意：C# CLI 当前默认 CPU（确定性、便于逐位比对）；OnsetNet 本就 CPU 跑，bit-exact。
BSR 生产要 CUDA 提速需 onnxruntime Gpu 包 + --cuda（见 ADOFAI_ORT_CLI_CUDA）。
"""
import os
import sys
import re
import time
import subprocess
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
# 工作室根目录：优先环境变量 ADOFAI_STUDIO_ROOT，其次从本脚本(位于 audio-sep/)向上一级推导。
# 这样打包成可移植文件夹后，只要保持 app-cs/audio-sep/gui/chartgen 兄弟布局即可，无需写死路径。
STUDIO_ROOT = os.environ.get("ADOFAI_STUDIO_ROOT")
if not STUDIO_ROOT:
    STUDIO_ROOT = os.path.dirname(HERE)   # audio-sep 的父目录 = 工作室根
STUDIO_ROOT = os.path.abspath(STUDIO_ROOT)

# C# AdofaiOrt：自包含发布后是 .exe（直接运行，无需外部 dotnet）。
# 默认相对定位到 bundle 内的 <root>/app-cs/out/Release/net10.0/AdofaiOrt.exe。
# 既可被环境变量 ADOFAI_ORT_CLI_DLL 覆盖，也能靠相对布局自动找到，彻底告别写死 D:\。
ORT_DLL = os.environ.get("ADOFAI_ORT_CLI_DLL",
                         os.path.join(STUDIO_ROOT, "app-cs", "out", "Release",
                                      "net10.0", "AdofaiOrt.exe"))
ORT_META = os.environ.get("ADOFAI_ORT_META",
                          os.path.join(HERE, "onnx_poc", "_split_meta_g2.json"))
# DOTNET 仅在 ORT_DLL 仍是 .dll（非自包含）时作为宿主使用；.exe 自包含时不需它。
DOTNET = os.environ.get("ADOFAI_DOTNET", "dotnet")
ORT_CLI_ENABLED = os.environ.get("ADOFAI_ORT_CLI", "0").strip() in ("1", "true", "True")
ORT_CLI_CUDA = os.environ.get("ADOFAI_ORT_CLI_CUDA", "1").strip() in ("1", "true", "True")

_TMP = os.path.join(HERE, "onnx_poc", "_cli_tmp")   # D 盘临时，绝不碰 C 盘
os.makedirs(_TMP, exist_ok=True)
_counter = [0]

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _clean(s):
    """剥掉 ANSI 转义：ORT/CLI 带色的输出若原样送进界面，会渲染成 `[31m` / `Djm` 之类乱码。"""
    return _ANSI.sub("", s or "").replace("\x00", "")


def _classify(text):
    """把 ORT 原话归成三类，供上层说人话：cuda（显卡加速不可用） / mem（内存不足） / other。"""
    t = (text or "").lower()
    if any(k in t for k in ("cudagetdevicecount", "cuda failure 801", "insufficient driver",
                            "no cuda-capable device", "which is missing", "error 126",
                            "onnxruntime_providers_cuda", "cudaerror", "device_id", "cuda")):
        return "cuda"
    if any(k in t for k in ("failed to allocate memory for requested buffer", "out of memory",
                            "bad_alloc", "cuda out of memory")):
        return "mem"
    return "other"


def _log_file():
    """完整原始 CLI 输出落盘到 output/logs/，界面只说结论、诊断细节走文件回传。"""
    d = os.path.join(STUDIO_ROOT, "output", "logs")
    try:
        os.makedirs(d, exist_ok=True)
        return os.path.join(d, "ort_cli_%s.log" % time.strftime("%Y%m%d_%H%M%S"))
    except Exception:
        return None


def _tmp(name):
    _counter[0] += 1
    return os.path.join(_TMP, f"{name}_{_counter[0]}.bin")


def _cuda_bin_dirs():
    """定位 CUDA13/cuDNN9 运行时 dll 目录，注入子进程 PATH 使 C# 的 CUDA EP 能加载 cublas/cudnn。

    降级顺序（铁律级根因）：
    ① 显式覆盖 ADOFAI_CUDA_BIN；
    ② 嵌入式 python 的 site-packages（runtime 内无 nvidia 包，通常命中不了，仅作兜底）；
    ③ AdofaiOrt 所在目录（<root>/app-cs/out/Release/net10.0/，DeployCudaNative 已把全套
       CUDA 运行时拷到此目录）。自包含 .exe 模式下 native dll 也会优先从 exe 自身目录加载，
       但 CUDA EP 解析 cudnn 仍依赖 PATH，故必须把该目录注入子进程 PATH。"""
    dirs = []
    override = os.environ.get("ADOFAI_CUDA_BIN", "")
    if override:
        dirs += [d for d in override.split(";") if d]
    sp = getattr(sys, "prefix", "")
    if sp:
        cand = [
            os.path.join(sp, "Lib", "site-packages", "nvidia", "cu13", "bin", "x86_64"),
            os.path.join(sp, "Lib", "site-packages", "nvidia", "cudnn", "bin"),
        ]
        for c in cand:
            if os.path.isdir(c):
                dirs.append(c)
    # ③ app 根兜底（DeployCudaNative 已部署全套 CUDA 运行时到此目录）。
    #    用模块级常量 ORT_DLL（带默认完整路径），不依赖 ADOFAI_ORT_CLI_DLL 是否被显式设置。
    if ORT_DLL:
        root = os.path.dirname(os.path.abspath(ORT_DLL))
        if root and os.path.isdir(root):
            dirs.append(root)
    # 去重保序
    seen, out = set(), []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


_CUDA_DEAD = False   # 本进程内 CUDA 已试失败 ⇒ 后续调用一律直接走 CPU（不白等）


def _call(args):
    """执行 AdofaiOrt，返回 CLI 的 stdout。

    🔴 设计要点（2026-09-27 群友 rc=1 → 9-27 真机两连崩）：
    ① **错误归类成中文结论**。宿主（backend）只回传子进程 stderr 的尾巴，若把 ORT 原话
       （`CUDA failure 801` / `BFCArena::AllocateRawInternal` …）直接甩到界面，用户既看不懂
       又像天塌了。故先分 cuda / mem / other 三类，只说「显卡加速不可用」或「内存不足」
       这类能行动的话；**完整原始输出落盘 output/logs/ort_cli_*.log** 供回传诊断。
    ② **CUDA 失败自动回退 CPU**。便携包要能在「没有可用 NVIDIA/CUDA 环境」的机器上照常
       出谱（慢很多、且分离约需 4.5GB 内存），而不是当场弹框失败。健康机器仍走 CUDA 快路。
    ③ **剥掉 ANSI 转义**：ORM/CLI 的带色输出在界面里会渲染成 `[31m`、`Djm` 之类乱码。
    """
    global _CUDA_DEAD
    # 自包含 .exe 直接执行；非自包含 .dll 才需要 dotnet 宿主
    if str(ORT_DLL).lower().endswith(".exe"):
        base = [ORT_DLL] + list(args)
    else:
        base = [DOTNET, ORT_DLL] + list(args)

    env = None
    want_cuda = False
    if ORT_CLI_CUDA and not _CUDA_DEAD:
        cdirs = _cuda_bin_dirs()
        if cdirs:
            want_cuda = True
            # 注入 CUDA13/cuDNN9 运行时到子进程 PATH（C# 进程需要这些 dll）
            cur = os.environ.get("PATH", "")
            env = dict(os.environ)
            env["PATH"] = ";".join(cdirs + [cur])
        else:
            # 找不到 CUDA 运行时 → 直接 CPU，避免 --cuda 触发但因缺 dll 而崩溃
            sys.stderr.write("[ort_cli_shim] 未找到 CUDA 运行时，直接走 CPU\n")

    def _run(with_cuda):
        # 字节模式读取：dotnet 宿主在管道重定向时可能向 stdout/stderr 写入非 UTF-8 字节
        # （首次运行提示按系统代码页编码），text=True 会直接抛 UnicodeDecodeError。
        # 数据一律走文件 IO，这里只负责失败诊断，故用 errors="replace" 安全解码。
        return subprocess.run(base + (["--cuda"] if with_cuda else []),
                              capture_output=True, env=env)

    def _tail(r, n=15):
        out = r.stdout.decode("utf-8", "replace") if r.stdout else ""
        err = r.stderr.decode("utf-8", "replace") if r.stderr else ""
        lines = [_clean(x) for x in (out + "\n" + err).splitlines() if x.strip()]
        return "\n".join(lines[-n:])

    r = _run(want_cuda)
    if r.returncode != 0:
        first = _tail(r)
        lf = _log_file()
        if lf:
            try:
                with open(lf, "w", encoding="utf-8") as fh:
                    fh.write("CMD: %s\nRC=%d (cuda=%s)\n\n--- raw stdout ---\n%s\n\n--- raw stderr ---\n%s\n"
                             % (" ".join(base + (["--cuda"] if want_cuda else [])),
                                r.returncode, want_cuda,
                                r.stdout.decode("utf-8", "replace") if r.stdout else "",
                                r.stderr.decode("utf-8", "replace") if r.stderr else ""))
            except Exception:
                lf = None
        sys.stderr.write("[ort_cli_shim] CLI 失败 rc=%d (cuda=%s) 完整日志: %s\n"
                         % (r.returncode, want_cuda, lf or "（无法写日志）"))
        if want_cuda:
            _CUDA_DEAD = True        # 后续调用别再试 CUDA，省掉每次的无效等待
            r2 = _run(False)
            if r2.returncode == 0:
                sys.stderr.write("[ort_cli_shim] 本机显卡加速不可用，已自动切换 CPU 模式继续"
                                 "（速度较慢，分离约需 4.5GB 内存）\n")
                r = r2
            else:
                second = _tail(r2)
                log_hint = ("\n完整日志：%s" % lf) if lf else ""
                if _classify(second) == "mem":
                    raise RuntimeError(
                        "分离引擎内存不足（CPU 模式约需 4.5GB 可用内存）：本机显卡加速不可用，"
                        "已改走 CPU 但内存仍不够。\n"
                        "→ 方案A（CPU 兜底）：请先关闭浏览器、游戏等占用内存的程序，"
                        "让空闲内存达到约 4~5GB 后再重试。\n"
                        "→ 方案B（显卡加速）：若机器有 NVIDIA 显卡却仍走不了加速，"
                        "多半是显卡太老（如 GTX 10 系等老卡；包内 CUDA 13 已不再支持，更新驱动无效），"
                        "需换 RTX 20 系及以上的 N 卡才能走显卡加速。" + log_hint)
                raise RuntimeError(
                    "AdofaiOrt 失败（显卡加速不可用，CPU 回退也失败）。\n[CUDA] %s\n[CPU] %s%s"
                    % (first, second, log_hint))
        else:
            raise RuntimeError("AdofaiOrt 失败：%s%s"
                               % (first, (("\n完整日志：%s" % lf) if lf else "")))

    # 调试回显（默认关）：ADOFAI_ORT_CLI_ECHO=1 时把 CLI 输出打到 stderr（已剥 ANSI），
    # 便于确认每步实际走的 EP（provider=CUDA/CPU）及 cudnn 加载情况。
    if os.environ.get("ADOFAI_ORT_CLI_ECHO", "").strip() in ("1", "true", "True"):
        if r.stdout:
            sys.stderr.write(_clean(r.stdout.decode("utf-8", "replace")))
        if r.stderr:
            sys.stderr.write(_clean(r.stderr.decode("utf-8", "replace")))
    return r.stdout.decode("utf-8", "replace") if r.stdout else ""


class OrtCliShim:
    """onnxruntime.InferenceSession 的 drop-in 替身，把 run 转发到 C# CLI。"""

    def __init__(self, kind, model_path=None):
        self.kind = kind
        self.model_path = model_path

    def get_providers(self):
        return ["C#-CLI" + ("+CUDA" if ORT_CLI_CUDA else "-CPU")]

    def run(self, output_names, feed):
        if self.kind == "bsr":
            mix = np.ascontiguousarray(feed["mix"], dtype=np.float32)   # (1,2,N)
            mp = _tmp("mix"); sp = _tmp("stft")
            mix.reshape(-1).astype(np.float32).tofile(mp)
            _call(["bsr-stft", mp, sp, ORT_META])
            stft = np.fromfile(sp, dtype=np.float32)
            F = stft.size // (12 * 1025 * 2)
            return [stft.reshape(12, 1025, F, 2)]
        else:  # onset
            mel = np.ascontiguousarray(feed["mel"], dtype=np.float32)   # (1,6,128,T)
            mp = _tmp("mel"); sp = _tmp("logits")
            mel.reshape(-1).astype(np.float32).tofile(mp)
            _call(["onsetnet", mp, self.model_path, sp])
            logits = np.fromfile(sp, dtype=np.float32)                 # (T,)
            return [logits.reshape(1, -1)]                             # (1,T) -> caller [0][0]=(T,)
