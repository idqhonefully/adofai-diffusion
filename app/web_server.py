"""
web_server.py — ADOFAI Diffusion 浏览器 GUI 后端（OnsetNet 踩点 + VAE/扩散加事件）

本地起一个 http 服务，自动打开浏览器。前端（webui/index.html）有两个页签：
  - 生成：上传音频 -> 子进程调 venv 的 inference_stage2.py(加载 onset_net.pt+vae.pt+ddpm.pt) -> .adofai
  - 训练：选数据目录 -> 一键训踩点模型 / 训风格模型(VAE+扩散) / 全训，带实时日志

后端自身用自带 python（无 torch）跑；真正的模型训练/推理在 venv 里用 torch 跑真实权重。
零依赖框架：标准库 http.server；上传用原始字节(body) + X-Settings(JSON) 头。
"""
import os
import sys
import io
import json
import time
import base64
import subprocess
import tempfile
import threading
import uuid
import webbrowser
import urllib.parse
import socketserver
import http.server

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from onset_detector import SR, HOP_LENGTH, detect_onsets, _ffmpeg_exe

SERVER_REF = [None]
# 单次上传体量上限（音频）。超过直接 413 拒绝，避免大文件整包读进内存拖崩服务。
MAX_AUDIO_BYTES = 1 * 1024 ** 3

# 模型路线（VAE+扩散 / OnsetNet）依赖 torch，跑在 venv 里；网页后端用自带 python 跑。
APP_DIR = os.path.dirname(os.path.abspath(__file__))
PORTABLE_ROOT = os.path.dirname(APP_DIR)
VENV_PY = os.path.join(PORTABLE_ROOT, "venv", "Scripts", "python.exe")
INFER_SCRIPT = os.path.join(PORTABLE_ROOT, "app", "training", "inference_stage2.py")
TRAIN_LOG = os.path.join(PORTABLE_ROOT, "data", "train.log")
PREVIEW_DIR = os.path.join(PORTABLE_ROOT, "data", "preview")
os.makedirs(PREVIEW_DIR, exist_ok=True)


def _fix_venv_cfg():
    """修复 venv/pyvenv.cfg 中的硬编码绝对路径 -> 强制改回相对路径。

    打包时 venv 创建于开发机，pyvenv.cfg 里可能写死开发机（或当时打包位置）
    的绝对路径。目标机器/改名后该路径失效，导致 venv python.exe 报
    "did not find executable"。

    本函数把 home / executable 强制重写为相对路径 "..\\..\\python"（相对于
    venv\\Scripts\\python.exe 上两层即便携版根目录）。相对路径不依赖文件夹
    被拷贝到的具体位置，搬到任意盘符/目录都能正常工作。
    """
    cfg_path = os.path.join(PORTABLE_ROOT, "venv", "pyvenv.cfg")
    if not os.path.exists(cfg_path):
        return
    # 模型 venv 依赖便携版自带的标准 Python（python313），不是嵌入式 python
    # （嵌入式 python 无完整标准库、不能作为 venv base，且版本可能不匹配）。
    rel_home = "..\\..\\python313"
    rel_exe = "..\\..\\python313\\python.exe"
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        new_lines = []
        changed = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("home ="):
                if not stripped[5:].strip().startswith(".."):
                    new_lines.append(f"home = {rel_home}\n")
                    changed = True
                else:
                    new_lines.append(line)
            elif stripped.startswith("executable ="):
                if not stripped[11:].strip().startswith(".."):
                    new_lines.append(f"executable = {rel_exe}\n")
                    changed = True
                else:
                    new_lines.append(line)
            elif stripped.startswith("command ="):
                new_lines.append(f"command = {rel_exe} -m venv venv\n")
                changed = True
            else:
                new_lines.append(line)
        if changed:
            with open(cfg_path, "w", encoding="utf-8") as f:
                f.writelines(new_lines)
    except Exception:
        pass  # 修复失败不阻塞启动


_fix_venv_cfg()  # 模块加载时立即修复

# ---------- 训练状态机 ----------
TRAIN_STATE = {"proc": None, "kind": None, "started": 0.0, "status": "idle", "phase": ""}

# VFX 训练独立状态机（与踩点/风格训练互不干扰，共用 TRAIN_LOG 以外的独立日志）
VFX_LOG = os.path.join(PORTABLE_ROOT, "data", "vfx_train.log")
VFX_STATE = {"proc": None, "kind": None, "started": 0.0, "status": "idle", "phase": ""}


def _resource(rel):
    """资源定位：冻结后从 _MEIPASS 取，开发时从脚本同目录取。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def _append_log(msg, log_path=TRAIN_LOG):
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def _cuda_usable():
    """探测 venv 的 torch 能否正常初始化 CUDA。

    前面几次训练崩溃会把笔记本显卡驱动上下文搞脏，导致任何 CUDA 初始化直接
    segfault。此时应自动 fallback 到 CPU（OnsetNet 仅 45 万参数，CPU 训练出的
    模型与 GPU 完全一致，只是慢些），保证网页"训练"按钮永远能跑、不会卡死。
    """
    try:
        r = subprocess.run(
            [VENV_PY, "-c",
             "import torch; torch.cuda.is_available() and torch.cuda.current_device()"],
            capture_output=True, timeout=40)
        return r.returncode == 0
    except Exception:
        return False


def _vfx_currently_running():
    """判断 VFX 训练是否正在进行中（含手动启动的训练进程）。"""
    if VFX_STATE["status"] == "running":
        return True
    # 手动启动的训练：检查训练日志近期是否更新
    for c in (os.path.join(PORTABLE_ROOT, "data", "train_vfx.log"), VFX_LOG):
        try:
            if os.path.exists(c) and (time.time() - os.path.getmtime(c)) < 150:
                return True
        except Exception:
            pass
    return False


def _kill_train_vfx():
    """强杀手动启动的 train_vfx.py 进程（网页状态机之外启动的）。"""
    killed = False
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'",
             "get", "processid,commandline"],
            capture_output=True, text=True, timeout=15).stdout
        for line in out.splitlines():
            if "train_vfx.py" in line:
                pid = line.strip().split()[-1]
                try:
                    subprocess.run(["taskkill", "/F", "/PID", pid],
                                    capture_output=True, timeout=10)
                    killed = True
                except Exception:
                    pass
    except Exception:
        pass
    return killed


def _outputs_ready(expect):
    """检查预期产物是否都已生成且非空（用于判定步骤成功）。"""
    if not expect:
        return False
    for p in expect:
        try:
            if not os.path.exists(p) or os.path.getsize(p) < 1024:
                return False
        except Exception:
            return False
    return True


def _training_worker(commands, kind, log_path=TRAIN_LOG, state=TRAIN_STATE):
    """顺序执行 commands: list[(label, argv, env_extra, expect)]。输出实时写入 log_path。

    expect: 该步预期产出的权重文件路径列表；若进程在"活儿干完之后"的 CUDA 拆栈阶段
    崩溃退出（Windows 上常见的 0xC0000409 / 3221226505 栈缓冲区溢出，属善意的收尾崩溃），
    只要预期产物已落盘且非空，就判为成功并继续后续步骤，避免整条训练链被一个无害的退出码打断。

    log_path / state 可传入独立状态机（如 VFX 训练），默认用主训练的 TRAIN_LOG/TRAIN_STATE。
    """
    state["status"] = "running"
    state["kind"] = kind
    state["started"] = time.time()
    # GPU 驱动被前面崩溃搞脏时，CUDA 初始化会 segfault -> 自动 fallback 到 CPU
    use_cpu = not _cuda_usable()
    if use_cpu:
        _append_log("[info] 检测到 CUDA 不可用（驱动状态异常），自动改用 CPU 训练（模型质量一致，速度较慢）", log_path)
    overall_ok = True
    for label, argv, env_extra, expect in commands:
        state["phase"] = label
        _append_log(f"\n===== {label} =====", log_path)
        env = dict(os.environ)
        env.update({
            "PYTHONNOUSERSITE": "1",
            "PYTHONIOENCODING": "utf-8",  # 子进程 stdout 用 utf-8, 避免中文/日文文件名 GBK 崩溃 + 日志乱码
            "MKL_THREADING_LAYER": "sequential", "MKL_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1", "KMP_AFFINITY": "disabled",
            "OPENBLAS_NUM_THREADS": "1", "NUMEXPR_MAX_THREADS": "1",
            "HF_HUB_OFFLINE": "1",   # Demucs 权重已缓存, 离线加载跳过网络重试
            "PYTHONUNBUFFERED": "1",  # 强制子进程无缓冲, 训练日志实时落盘(避免静默假死错觉)
        })
        if use_cpu:
            env["CUDA_VISIBLE_DEVICES"] = "-1"
        env.update(env_extra or {})
        try:
            proc = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    bufsize=1, encoding="utf-8", errors="replace")
            state["proc"] = proc
            for line in proc.stdout:
                _append_log(line.rstrip("\n"), log_path)
            rc = proc.wait()
        except Exception as e:
            _append_log(f"[error] 启动失败: {e}", log_path)
            rc = -1
        state["proc"] = None
        # 成功判定：退出码 0，或（退出码非 0 但预期产物已落盘）
        if rc != 0 and not _outputs_ready(expect):
            overall_ok = False
            _append_log(f"[result] {label} 退出码={rc}（失败，未产出权重）", log_path)
            break
        if rc != 0:
            _append_log(f"[warn] {label} 进程退出码={rc}（疑似 CUDA 收尾崩溃），"
                        f"但权重已正确生成，判定为成功并继续", log_path)
        _append_log(f"[result] {label} 完成 ✓", log_path)
    state["status"] = "done" if overall_ok else "error"
    state["phase"] = ""


def _start_training(kind, data_dir, opts):
    if TRAIN_STATE["status"] == "running":
        return False, "训练正在进行中，请先停止或等待完成"
    if not data_dir or not os.path.isdir(data_dir):
        return False, "数据目录不存在或无效，请先选择有效的训练数据目录"
    try:
        os.makedirs(os.path.dirname(TRAIN_LOG), exist_ok=True)
        open(TRAIN_LOG, "w").close()   # 清空旧日志
    except Exception:
        pass
    ckpt = os.path.join(PORTABLE_ROOT, "data", "checkpoints")
    os.makedirs(ckpt, exist_ok=True)

    finetune = bool(opts.get("finetune_on"))
    ckpt_onset = os.path.join(ckpt, "onset_net.pt")
    onset_cmd = [VENV_PY, os.path.join(PORTABLE_ROOT, "app", "training", "train_onset.py"),
                 "--train_dir", data_dir,
                 "--epochs", str(int(opts.get("onset_epochs", 80)))]
    if finetune:
        # 微调模式：从已有 onset_net.pt 继续训，保留其他歌能力；输出独立文件不覆盖原权重
        onset_cmd += ["--resume", ckpt_onset,
                      "--out", os.path.join(ckpt, "onset_net_finetune.pt")]
    else:
        onset_cmd += ["--out", ckpt_onset]
    diff_env = {"ADOFAI_TRAIN_DIR": data_dir,
                "ADOFAI_DATA_DIR": os.path.join(PORTABLE_ROOT, "data")}
    if opts.get("vae_epochs"):
        diff_env["VAE_EPOCHS"] = str(int(opts["vae_epochs"]))
    if opts.get("ddpm_epochs"):
        diff_env["DDPM_EPOCHS"] = str(int(opts["ddpm_epochs"]))
    diff_cmd = [VENV_PY, os.path.join(PORTABLE_ROOT, "app", "training", "train_stage2.py")]

    onset_expect = [os.path.join(ckpt, "onset_net_finetune.pt" if finetune else "onset_net.pt")]
    diff_expect = [os.path.join(ckpt, "vae.pt"), os.path.join(ckpt, "ddpm.pt")]

    commands = []
    if kind in ("onset", "all"):
        commands.append(("训练踩点模型 OnsetNet（Demucs 6 通道 @128）", onset_cmd, {}, onset_expect))
    if kind in ("diffusion", "all"):
        commands.append(("训练风格模型 VAE+扩散 @128", diff_cmd, diff_env, diff_expect))
    if not commands:
        return False, "未知的训练类型：" + str(kind)
    threading.Thread(target=_training_worker, args=(commands, kind), daemon=True).start()
    return True, "已启动训练（" + kind + "），可在下方日志查看进度"


def _start_vfx_training(data_dir, epochs, rebuild_cache):
    """启动 VFXNet 训练（独立状态机 VFX_STATE/VFX_LOG）。

    两步链路：① 提取视觉事件目标缓存（extract_vfx.py，librosa 解码 + 计时引擎对齐到帧网格）
             ② 训练多任务模型（train_vfx.py，Dataset 首次算 6 通道 Demucs 梅尔落盘）
    与踩点/风格训练共用 GPU，故二者互斥；但自带独立日志与状态，网页标签页互不干扰。
    """
    if VFX_STATE["status"] == "running":
        return False, "VFX 训练正在进行中，请先停止或等待完成"
    if TRAIN_STATE["status"] == "running":
        return False, "请先等待「训练模型」页的训练完成，再启动 VFX 训练（共用同一 GPU 资源）"
    if not data_dir or not os.path.isdir(data_dir):
        return False, "视觉数据目录不存在或无效，请选择包含「歌曲+谱面」配对的目录"
    try:
        os.makedirs(os.path.dirname(VFX_LOG), exist_ok=True)
        open(VFX_LOG, "w").close()   # 清空旧日志
    except Exception:
        pass
    ckpt = os.path.join(PORTABLE_ROOT, "data", "checkpoints")
    os.makedirs(ckpt, exist_ok=True)
    cache_dir = os.path.join(PORTABLE_ROOT, "data", "vfx_cache")
    os.makedirs(cache_dir, exist_ok=True)
    extract_path = os.path.join(PORTABLE_ROOT, "app", "training", "extract_vfx.py")
    train_vfx_path = os.path.join(PORTABLE_ROOT, "app", "training", "train_vfx.py")
    vfx_expect = [os.path.join(ckpt, "vfx_net.pt")]

    need_extract = rebuild_cache or not os.path.exists(os.path.join(cache_dir, "manifest.json"))
    commands = []
    if need_extract:
        commands.append(("① 提取视觉事件目标缓存（librosa 解码 + 计时引擎对齐到帧网格）",
                         [VENV_PY, extract_path, "--data", data_dir, "--out", cache_dir],
                         {}, [os.path.join(cache_dir, "manifest.json")]))
    commands.append(("② 训练 VFXNet 视觉特效模型（Demucs 6 通道 @128，多任务：事件+参数+滤镜）",
                     [VENV_PY, train_vfx_path, "--data", data_dir, "--cache", cache_dir,
                      "--epochs", str(int(epochs))],
                     {}, vfx_expect))
    threading.Thread(target=_training_worker, args=(commands, "vfx"),
                     kwargs={"log_path": VFX_LOG, "state": VFX_STATE}, daemon=True).start()
    return True, "已启动 VFXNet 训练（" + ("含缓存重建" if need_extract else "复用已有缓存") + \
                 "），可在下方日志查看进度"


def plot_onsets_bytes(logmel, times):
    """把"梅尔谱 + onset 位置"画成 PNG 字节，供前端直接显示。"""
    fig = plt.figure(figsize=(14, 5))
    plt.imshow(logmel, origin="lower", aspect="auto", cmap="magma")
    frames = np.asarray(times) * SR / HOP_LENGTH
    plt.scatter(frames, np.full_like(frames, logmel.shape[0] * 0.5),
                c="cyan", s=25, marker="|", label="detected onset")
    plt.xlabel("frame")
    plt.ylabel("mel bin")
    plt.title("Onset positions on mel spectrogram")
    plt.legend(loc="upper right")
    plt.colorbar(format="%+2.0f dB")
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, dpi=120, format="png")
    plt.close(fig)
    return buf.getvalue()


def generate_chart_model(audio_bytes, filename, params):
    """AI 模型路线：音频 -> OnsetNet+VAE+扩散 -> .adofai（用 venv 里的 torch 跑真实权重）。"""
    if not os.path.exists(VENV_PY):
        return {"ok": False, "error": "未找到模型运行环境（venv 缺失）。请确认 portable/venv 存在。"}
    base_bpm = 120.0
    bpm_raw = params.get("bpm")
    user_bpm_given = False
    try:
        if bpm_raw not in (None, "", 0) and float(bpm_raw) > 0:
            base_bpm = float(bpm_raw)
            user_bpm_given = True
    except (TypeError, ValueError):
        pass
    # 用户填了有效 BPM 就严格用他的，不让 Beat This 覆盖；留空才自动检测。
    auto_bpm = not user_bpm_given
    difficulty = int(params.get("difficulty", 1))
    track = params.get("track", "all") or "all"
    vfx = bool(params.get("vfx", False))
    try:
        intensity = float(params.get("intensity", 0.5))
    except (TypeError, ValueError):
        intensity = 0.5

    tmpdir = tempfile.mkdtemp(prefix="adofai_model_")
    ext = os.path.splitext(filename)[1].lower() or ".mp3"
    # 原始上传文件用 input_raw.xxx，解码产物用 input.wav，二者永远不同名，
    # 避免上传 .wav 时 ffmpeg 输入输出指向同一文件导致解码失败。
    raw_path = os.path.join(tmpdir, "input_raw" + ext)
    wav_path = os.path.join(tmpdir, "input.wav")
    adofai_path = os.path.join(tmpdir, "chart.adofai")
    with open(raw_path, "wb") as f:
        f.write(audio_bytes)

    try:
        exe = _ffmpeg_exe()
        if not exe:
            return {"ok": False, "error": "未找到 ffmpeg，无法解码音频。"}
        subprocess.run([exe, "-y", "-i", raw_path, "-ar", "22050", "-ac", "1", wav_path],
                       capture_output=True, timeout=180, check=True)
    except Exception as e:
        return {"ok": False, "error": f"音频解码失败：{e}"}
    if not os.path.exists(wav_path):
        return {"ok": False, "error": "音频解码后未生成 wav。"}

    # 长歌保护：超过 360 秒的曲子整段做 DDPM 扩散会触发超时（序列过长）。
    # 自动截取前 360 秒生成谱面（游戏谱极少超过 6 分钟，360s 已覆盖大多数主歌+副歌）。
    try:
        import wave as _wave
        with _wave.open(wav_path) as _w:
            _dur = _w.getnframes() / float(_w.getframerate())
        if _dur > 360:
            _cut = os.path.join(tmpdir, "input_cut.wav")
            subprocess.run([exe, "-y", "-i", wav_path, "-t", "360",
                            "-ar", "22050", "-ac", "1", _cut],
                           capture_output=True, timeout=180, check=True)
            if os.path.exists(_cut) and os.path.getsize(_cut) > 0:
                wav_path = _cut
    except Exception:
        pass

    # ---- 根据所选音轨决定喂给 AI 的音频与 track ----
    selected = params.get("selected") or []
    try:
        infer_wav, track = _resolve_infer_input(tmpdir, wav_path, selected, adofai_path)
    except Exception as e:
        return {"ok": False, "error": f"音轨处理失败：{e}"}
    return run_inference(infer_wav, track, difficulty, base_bpm, adofai_path, filename,
                          vfx=vfx, intensity=intensity, auto_bpm=auto_bpm)


def _venv_env():
    env = dict(os.environ)
    env.update({
        "PYTHONNOUSERSITE": "1",
        "MKL_THREADING_LAYER": "sequential", "MKL_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1", "KMP_AFFINITY": "disabled",
        "OPENBLAS_NUM_THREADS": "1", "NUMEXPR_MAX_THREADS": "1",
        "HF_HUB_OFFLINE": "1",
        # 离线加载 Demucs 预训练模型：指向打包内的 torch_hub 缓存
        "TORCH_HOME": os.path.join(PORTABLE_ROOT, "torch_hub"),
    })
    # 把便携根目录（ffmpeg.exe 所在）前置到 PATH，确保 venv 内 librosa/audioread
    # 离线也能找到内置 ffmpeg，无需系统安装（群友「did not find executable」根因）。
    _ff = os.path.join(PORTABLE_ROOT, "ffmpeg.exe")
    if os.path.exists(_ff):
        env["PATH"] = PORTABLE_ROOT + os.pathsep + env.get("PATH", "")
    return env


def _run_stem_separate(wav_path, track, out_path):
    """用 venv 分离单条音轨，返回 {ok, error?}。"""
    if not os.path.exists(VENV_PY):
        return {"ok": False, "error": "未找到模型运行环境（venv 缺失）。"}
    try:
        proc = subprocess.run(
            [VENV_PY, os.path.join(PORTABLE_ROOT, "app", "training", "preview_track.py"),
             "--audio", wav_path, "--track", str(track), "--out", out_path],
            env=_venv_env(), capture_output=True, text=True, timeout=600)
    except Exception as e:
        return {"ok": False, "error": f"分离进程启动失败：{e}"}
    out = (proc.stdout or "").strip()
    try:
        r = json.loads(out)
    except Exception:
        return {"ok": False, "error": f"分离失败：{(proc.stderr or out)[:400]}"}
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error", "分离失败")}
    if not os.path.exists(out_path):
        return {"ok": False, "error": "分离结果未生成"}
    return {"ok": True}


def _run_separate_all(wav_path, prefix):
    """用 venv 一次分离全部音轨到 PREVIEW_DIR，返回 {ok, stems:[...], error?}。"""
    if not os.path.exists(VENV_PY):
        return {"ok": False, "error": "未找到模型运行环境（venv 缺失）。"}
    try:
        proc = subprocess.run(
            [VENV_PY, os.path.join(PORTABLE_ROOT, "app", "training", "separate_all.py"),
             "--audio", wav_path, "--outdir", PREVIEW_DIR, "--prefix", prefix],
            env=_venv_env(), capture_output=True, text=True, timeout=600)
    except Exception as e:
        return {"ok": False, "error": f"分离进程启动失败：{e}"}
    out = (proc.stdout or "").strip()
    try:
        r = json.loads(out)
    except Exception:
        return {"ok": False, "error": f"分离失败：{(proc.stderr or out)[:400]}"}
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error", "分离失败")}
    return r


def _merge_stems(paths, out_path, sr=22050):
    """把多个单声道 wav 线性相加并裁剪，合并成一个 wav。"""
    import wave as _w
    arrs = []
    for p in paths:
        if not os.path.exists(p):
            continue
        with _w.open(p, "rb") as w:
            raw = w.readframes(w.getnframes())
        arrs.append(np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767.0)
    if not arrs:
        return False
    L = max(len(a) for a in arrs)
    mixed = np.zeros(L, dtype=np.float32)
    for a in arrs:
        if len(a) < L:
            a = np.pad(a, (0, L - len(a)))
        mixed += a
    mixed = np.clip(mixed, -1.0, 1.0)
    import wave
    data = (mixed * 32767.0).astype("<i2").tobytes()
    with wave.open(out_path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(data)
    return True


def _resolve_infer_input(tmpdir, wav_path, selected, adofai_path):
    """根据所选音轨返回 (infer_wav_path, track)。
       - 含 all/full 或为空        -> 原音频, track=all
       - 单轨                       -> 分离该轨作为输入, track=该轨（复制填充 6 通道只踩该轨）
       - 多轨                       -> 分离全部后合并选中轨, track=all（重分离未选通道静音=只踩选中）
    """
    sel = [s for s in selected if s not in ("all", "full", "")]
    if not selected or "all" in selected or "full" in selected or not sel:
        return wav_path, "all"
    if len(sel) == 1:
        out = os.path.join(tmpdir, "stem_single.wav")
        r = _run_stem_separate(wav_path, sel[0], out)
        if not r.get("ok"):
            raise RuntimeError(r.get("error", "分离失败"))
        return out, sel[0]
    stems = _run_separate_all(wav_path, uuid.uuid4().hex)
    if not stems.get("ok"):
        raise RuntimeError(stems.get("error", "分离失败"))
    files = {s["name"]: os.path.join(PREVIEW_DIR, s["file"]) for s in stems["stems"]}
    chosen = [files[n] for n in sel if n in files]
    if not chosen:
        return wav_path, "all"
    merged = os.path.join(tmpdir, "merged.wav")
    if not _merge_stems(chosen, merged):
        return wav_path, "all"
    return merged, "all"


def run_inference(wav_path, track, difficulty, base_bpm, adofai_path, filename,
                  vfx=False, intensity=0.5, auto_bpm=False):
    """给定已解码 wav 与 track，跑 OnsetNet+VAE/扩散 -> .adofai（可选注入 VFX）。

    auto_bpm=False 时严格使用用户填写的 base_bpm（不被 Beat This 估计值覆盖）；
    仅当用户未填写 BPM（base_bpm 取默认且 auto_bpm=True）才自动检测并覆盖。
    """
    try:
        times_s, flux, logmel = detect_onsets(wav_path)
    except Exception as e:
        return {"ok": False, "error": f"频谱分析失败：{e}"}
    try:
        # 长序列 DDPM 超时保护：歌越长扩散步数越少（50 步在 5 分钟曲上会超 900s 超时）。
        # 依据 detect_onsets 返回的 logmel 帧数估算时长（hop=128, sr=22050，与 line 514 一致）。
        try:
            _n = int(logmel.shape[1])
            _dur = _n * 128.0 / 22050.0
        except Exception:
            _dur = 0.0
        _steps = 25 if _dur > 360 else (35 if _dur > 180 else 50)
        cmd = [VENV_PY, INFER_SCRIPT, "--audio", wav_path, "--out", adofai_path,
               "--bpm", str(base_bpm), "--steps", str(_steps), "--guidance", "2.5",
               "--track", str(track)]
        if auto_bpm:
            cmd += ["--auto_bpm"]
        if vfx:
            cmd += ["--vfx", "--intensity", str(intensity)]
        proc = subprocess.run(
            cmd,
            env=_venv_env(), capture_output=True, text=True, timeout=2400)
    except Exception as e:
        return {"ok": False, "error": f"模型推理进程启动失败：{e}"}
    if not os.path.exists(adofai_path):
        err = (proc.stderr or proc.stdout or "")[:600]
        if track != "all":
            return {"ok": False, "error": f"生成失败（所选音轨「{track}」在该曲中较弱或稀疏，踩点不足，试试选「全部混合」或其他音轨）：{err}"}
        return {"ok": False, "error": f"模型未生成谱面（权重可能未训练或踩点不足）：{err}"}
    try:
        with open(adofai_path, encoding="utf-8-sig") as f:
            adofai_text = f.read()
        level = json.loads(adofai_text)
    except Exception as e:
        return {"ok": False, "error": f"读取/解析谱面失败：{e}"}
    try:
        level["settings"]["difficulty"] = max(1, min(10, difficulty))
        # 根因修复：推理内部把音频解码成固定名 input.wav，但用户下载后真实音频是原始上传文件名。
        # 必须把 settings.song / songName 写回原始歌名，否则游戏在关卡文件夹里找不到音频 -> 直接加载失败。
        _song_name = os.path.basename(filename)
        if _song_name:
            level.setdefault("settings", {})["song"] = os.path.splitext(_song_name)[0]
            level.setdefault("settings", {})["songName"] = os.path.splitext(_song_name)[0]
        adofai_text = json.dumps(level, ensure_ascii=False, indent=1)
        # 写回磁盘（前端可能直接读该文件而非返回文本）
        try:
            with open(adofai_path, "w", encoding="utf-8-sig") as f:
                f.write(adofai_text)
        except Exception:
            pass
    except Exception:
        pass
    angle = level.get("angleData", []) or []
    actions = level.get("actions", []) or []
    twirl = sum(1 for a in actions if isinstance(a, dict) and a.get("eventType") == "Twirl")
    # 本管线只在 generate 里产生 Twirl 动作；其余动作均为 VFX 注入，故用"非 Twirl"计数
    vfx_count = sum(1 for a in actions if isinstance(a, dict) and a.get("eventType") != "Twirl")
    note_count = len(angle)
    duration_s = round(logmel.shape[1] * HOP_LENGTH / SR, 2)
    png = plot_onsets_bytes(logmel, times_s)
    return {
        "ok": True,
        "adofai": adofai_text,
        "preview_png": base64.b64encode(png).decode("ascii"),
        "note_count": note_count,
        "twirl_count": twirl,
        "vfx_count": vfx_count,
        "max_error_ms": None,
        "mean_error_ms": None,
        "bpm_used": float((level.get("settings") or {}).get("bpm", base_bpm)),
        "duration_s": duration_s,
        "song": os.path.basename(filename),
        "pattern": "model128",
        "has_setspeed": False,
    }


def separate_all(audio_bytes, filename):
    """分离全部音轨，返回各分解音轨的可播放 url 列表（不需要训练好的权重）。"""
    if not os.path.exists(VENV_PY):
        return {"ok": False, "error": "未找到模型运行环境（venv 缺失）。"}
    tmpdir = tempfile.mkdtemp(prefix="adofai_sep_")
    ext = os.path.splitext(filename)[1].lower() or ".mp3"
    raw_path = os.path.join(tmpdir, "input_raw" + ext)
    wav_path = os.path.join(tmpdir, "input.wav")
    with open(raw_path, "wb") as f:
        f.write(audio_bytes)
    try:
        exe = _ffmpeg_exe()
        if not exe:
            return {"ok": False, "error": "未找到 ffmpeg，无法解码音频。"}
        subprocess.run([exe, "-y", "-i", raw_path, "-ar", "22050", "-ac", "1", wav_path],
                       capture_output=True, timeout=180, check=True)
    except Exception as e:
        return {"ok": False, "error": f"音频解码失败：{e}"}
    if not os.path.exists(wav_path):
        return {"ok": False, "error": "音频解码后未生成 wav。"}
    r = _run_separate_all(wav_path, uuid.uuid4().hex)
    if not r.get("ok"):
        return r
    stems = []
    for s in r["stems"]:
        stems.append({
            "name": s["name"], "label": s["label"],
            "url": "/preview/" + s["file"], "duration_s": s["duration_s"],
        })
    return {"ok": True, "stems": stems}


def preview_track(audio_bytes, filename, track):
    """分离选中音轨并导出可播放 wav，供网页试听（不需要训练好的 onset/vae/ddpm 权重）。"""
    if not os.path.exists(VENV_PY):
        return {"ok": False, "error": "未找到模型运行环境（venv 缺失）。"}
    track = track or "all"
    tmpdir = tempfile.mkdtemp(prefix="adofai_prev_")
    ext = os.path.splitext(filename)[1].lower() or ".mp3"
    # 原始上传文件用 input_raw.xxx，解码产物用 input.wav，二者永远不同名，
    # 避免上传 .wav 时 ffmpeg 输入输出指向同一文件导致解码失败。
    raw_path = os.path.join(tmpdir, "input_raw" + ext)
    wav_path = os.path.join(tmpdir, "input.wav")
    uid = uuid.uuid4().hex
    out_path = os.path.join(PREVIEW_DIR, uid + ".wav")
    with open(raw_path, "wb") as f:
        f.write(audio_bytes)

    try:
        exe = _ffmpeg_exe()
        if not exe:
            return {"ok": False, "error": "未找到 ffmpeg，无法解码音频。"}
        subprocess.run([exe, "-y", "-i", raw_path, "-ar", "22050", "-ac", "1", wav_path],
                       capture_output=True, timeout=180, check=True)
    except Exception as e:
        return {"ok": False, "error": f"音频解码失败：{e}"}
    if not os.path.exists(wav_path):
        return {"ok": False, "error": "音频解码后未生成 wav。"}

    env = dict(os.environ)
    env.update({
        "PYTHONNOUSERSITE": "1",
        "MKL_THREADING_LAYER": "sequential", "MKL_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1", "KMP_AFFINITY": "disabled",
        "OPENBLAS_NUM_THREADS": "1", "NUMEXPR_MAX_THREADS": "1",
        "HF_HUB_OFFLINE": "1",
    })
    try:
        proc = subprocess.run(
            [VENV_PY, os.path.join(PORTABLE_ROOT, "app", "training", "preview_track.py"),
             "--audio", wav_path, "--track", str(track), "--out", out_path],
            env=env, capture_output=True, text=True, timeout=600)
    except Exception as e:
        return {"ok": False, "error": f"分离进程启动失败：{e}"}
    out = (proc.stdout or "").strip()
    try:
        r = json.loads(out)
    except Exception:
        return {"ok": False, "error": f"分离失败：{(proc.stderr or out)[:400]}"}
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error", "分离失败")}
    if not os.path.exists(out_path):
        return {"ok": False, "error": "分离结果未生成"}
    # 顺手清理 1 小时前的旧试听文件
    try:
        now = time.time()
        for fn in os.listdir(PREVIEW_DIR):
            fp = os.path.join(PREVIEW_DIR, fn)
            if os.path.isfile(fp) and now - os.path.getmtime(fp) > 3600:
                os.remove(fp)
    except Exception:
        pass
    return {
        "ok": True,
        "url": "/preview/" + os.path.basename(out_path),
        "track": track,
        "duration_s": r.get("duration_s"),
    }


class Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _train_log_payload(self):
        n = int(self.headers.get("X-Lines", "300"))
        lines = []
        if os.path.exists(TRAIN_LOG):
            with open(TRAIN_LOG, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        return json.dumps({
            "log": "".join(lines[-n:]),
            "status": TRAIN_STATE["status"],
            "phase": TRAIN_STATE["phase"],
            "running": TRAIN_STATE["status"] == "running",
            "kind": TRAIN_STATE["kind"],
        })

    def _vfx_log_payload(self):
        n = int(self.headers.get("X-Lines", "300"))
        lines = []
        if os.path.exists(VFX_LOG):
            with open(VFX_LOG, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        return json.dumps({
            "log": "".join(lines[-n:]),
            "status": VFX_STATE["status"],
            "phase": VFX_STATE["phase"],
            "running": VFX_STATE["status"] == "running",
            "kind": VFX_STATE["kind"],
        })

    def _shape_train_payload(self):
        """ShapeModel 走线模型训练实时状态：读 data/shape_train.log + GPU + 预处理进度。"""
        import subprocess, glob, time
        n = int(self.headers.get("X-Lines", "500"))
        log_path = os.path.join(PORTABLE_ROOT, "data", "shape_train.log")
        lines = []
        if os.path.exists(log_path):
            with open(log_path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        log_text = "".join(lines[-n:])
        skip = sum(1 for l in lines if "[skip]" in l)
        ok = sum(1 for l in lines if l.strip().startswith("+ "))
        # GPU 利用率（nvidia-smi 命令行；无 GPU/不在 PATH 时降级为 None）
        gpu_util = gpu_mem = None
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
                 "--format=csv,noheader"],
                capture_output=True, text=True, timeout=5)
            if out.returncode == 0 and out.stdout.strip():
                parts = [x.strip() for x in out.stdout.strip().split(",")]
                gpu_util = int(parts[0].replace("%", "")) if parts[0] else None
                gpu_mem = int(parts[1].replace("MiB", "").strip()) if len(parts) > 1 else None
        except Exception:
            pass
        # 预处理进度：特征缓存 npz 数
        feat_dir = os.path.join(PORTABLE_ROOT, "data", "shape_feat_cache")
        feat_done = len(glob.glob(os.path.join(feat_dir, "*.npz"))) if os.path.isdir(feat_dir) else 0
        # 进程是否活跃（三重检测：进程名 > GPU 高占用 > 日志 mtime）
        running = False
        try:
            # 1) 进程名检测（Windows 用 tasklist / Linux 用 ps）
            try:
                out = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq python.exe", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, timeout=5)
                if out.returncode == 0 and "train_shape" in out.stdout:
                    running = True
            except Exception:
                pass
            # 2) GPU 高占用兜底（训练时 GPU 持续 >50%）
            if not running and gpu_util is not None and gpu_util > 50:
                running = True
            # 3) 日志 mtime 兜底（2 分钟内更新过）
            if not running and os.path.exists(log_path):
                running = (time.time() - os.path.getmtime(log_path)) < 120
        except Exception:
            pass
        ckpt = os.path.join(PORTABLE_ROOT, "data", "checkpoints", "shape_model.pt")
        done = os.path.exists(ckpt)
        return json.dumps({
            "log": log_text, "skip": skip, "ok": ok,
            "gpu_util": gpu_util, "gpu_mem": gpu_mem,
            "feat_done": feat_done, "feat_total": 105,
            "running": running, "done": done,
        })

    def _vfx_progress_payload(self):
        """解析 VFX 训练日志 -> 结构化进度（供实时看板网页 vfx_train.html）。

        兼容两种日志落盘位置：
          - 手动启动的训练写到 data/train_vfx.log（本会话用它）
          - 网页启动的训练写到 VFX_LOG（data/vfx_train.log）
        取 mtime 最新者解析。
        """
        import re
        candidates = [
            os.path.join(PORTABLE_ROOT, "data", "train_vfx.log"),
            VFX_LOG,
        ]
        best = None
        best_mtime = -1
        for c in candidates:
            try:
                if os.path.exists(c) and os.path.getmtime(c) > best_mtime:
                    best_mtime = os.path.getmtime(c)
                    best = c
            except Exception:
                pass
        log_text = ""
        if best:
            try:
                with open(best, encoding="utf-8", errors="replace") as f:
                    log_text = f.read()
            except Exception:
                pass
        # 解析 [vfx] ep X/Y step Z ev=a pa=b fl=c ez=d（step 行与 epoch 汇总行同格式）
        pat = re.compile(
            r"\[vfx\]\s+ep\s+(\d+)/(\d+)(?:\s+step\s+(\d+))?\s+ev=([\d.]+)\s+pa=([\d.]+)"
            r"(?:\s+fl=([\d.]+))?(?:\s+ez=([\d.]+))?")
        history = []          # (epoch, step_or_None, ev, pa, fl, ez)
        total_epochs = None
        total_steps = None
        for line in log_text.splitlines():
            m = pat.search(line)
            if m:
                ep = int(m.group(1)); tot = int(m.group(2))
                st = int(m.group(3)) if m.group(3) else None
                ev = float(m.group(4)); pa = float(m.group(5))
                fl = float(m.group(6)) if m.group(6) else None
                ez = float(m.group(7)) if m.group(7) else None
                total_epochs = tot
                history.append((ep, st, ev, pa, fl, ez))
            elif "样本块" in line:
                mm = re.search(r"(\d+)\s*样本块", line)
                if mm:
                    total_steps = int(mm.group(1))
        running = _vfx_currently_running()
        # GPU 占用
        gpu_mem = None
        gpu_util = None
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.used,utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5).stdout
            parts = out.strip().splitlines()[0].split(",")
            gpu_mem = int(parts[0].strip())
            gpu_util = int(parts[1].strip())
        except Exception:
            pass
        last = history[-1] if history else None
        return json.dumps({
            "running": bool(running),
            "status": "running" if running else "idle",
            "total_epochs": total_epochs,
            "total_steps": total_steps,
            "last": ({"epoch": last[0], "step": last[1], "ev": last[2], "pa": last[3],
                      "fl": last[4], "ez": last[5]} if last else None),
            "history": history,
            "gpu_mem": gpu_mem,
            "gpu_util": gpu_util,
            "log": "\n".join(log_text.splitlines()[-200:]),
        })

    def _model_status_payload(self):
        """探测 checkpoints/ 下四个权重文件是否存在且非空，返回结构化状态。

        用于前端真实展示每个模型是否就绪，替换原先写死的「没有模型」提示语。
        """
        ckpt = os.path.join(PORTABLE_ROOT, "data", "checkpoints")
        models = [
            ("onset_net.pt", "踩点模型 OnsetNet"),
            ("vae.pt", "风格模型 VAE"),
            ("ddpm.pt", "扩散模型 DDPM"),
            ("vfx_net.pt", "视觉特效 VFXNet"),
        ]
        out = []
        for fname, label in models:
            p = os.path.join(ckpt, fname)
            try:
                ok = os.path.exists(p) and os.path.getsize(p) >= 1024
                sz = round(os.path.getsize(p) / 1048576, 2) if ok else 0
            except Exception:
                ok, sz = False, 0
            out.append({"file": fname, "label": label, "present": ok, "size_mb": sz})
        return json.dumps({"models": out, "all_ready": all(m["present"] for m in out)})

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            p = _resource(os.path.join("webui", "index.html"))
            try:
                with open(p, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/health":
            self._send(200, json.dumps({"ok": True}))
        elif path == "/api/model_status":
            self._send(200, self._model_status_payload())
        elif path == "/api/train_log":
            self._send(200, self._train_log_payload())
        elif path == "/api/shape_train_log":
            self._send(200, self._shape_train_payload())
        elif path == "/shape_monitor":
            p = _resource(os.path.join("webui", "shape_monitor.html"))
            try:
                with open(p, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path == "/api/vfx_train_log":
            self._send(200, self._vfx_log_payload())
        elif path == "/api/vfx_progress":
            self._send(200, self._vfx_progress_payload())
        elif path == "/vfx_train.html":
            p = _resource(os.path.join("webui", "vfx_train.html"))
            try:
                with open(p, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        elif path.startswith("/preview/"):
            fname = path[len("/preview/"):]
            if not fname or ".." in fname or fname.startswith("/") or fname.startswith("\\"):
                self._send(403, json.dumps({"error": "forbidden"}))
                return
            fpath = os.path.join(PREVIEW_DIR, os.path.basename(fname))
            if not os.path.exists(fpath):
                self._send(404, json.dumps({"error": "not found"}))
                return
            ext = os.path.splitext(fpath)[1].lower().lstrip(".")
            ctype = {"wav": "audio/wav", "mp3": "audio/mpeg",
                     "ogg": "audio/ogg", "m4a": "audio/mp4"}.get(ext, "application/octet-stream")
            try:
                with open(fpath, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data)
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        else:
            # 静态资产（webui/ 目录）：icon.png / favicon.ico / css/js 等前端资源
            fname = path.lstrip("/")
            if (".." in fname) or ("/" in fname) or ("\\" in fname) or (not fname):
                self._send(403, json.dumps({"error": "forbidden"}))
                return
            fpath = _resource(os.path.join("webui", fname))
            if not os.path.exists(fpath) or not os.path.isfile(fpath):
                self._send(404, json.dumps({"error": "not found"}))
                return
            ext = os.path.splitext(fname)[1].lower().lstrip(".")
            ctype = {"png": "image/png", "ico": "image/x-icon", "jpg": "image/jpeg",
                     "jpeg": "image/jpeg", "svg": "image/svg+xml", "css": "text/css",
                     "js": "text/javascript"}.get(ext, "application/octet-stream")
            try:
                with open(fpath, "rb") as f:
                    self._send(200, f.read(), ctype)
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))

    def do_POST(self):
        route = self.path.split("?", 1)[0]
        length = int(self.headers.get("Content-Length", 0))

        if route == "/api/generate":
            try:
                if length > MAX_AUDIO_BYTES:
                    self._send(413, json.dumps(
                        {"ok": False, "error": "音频文件过大（上限 1GB）"}))
                    return
                raw = self.rfile.read(length) if length else b""
                params = json.loads(self.headers.get("X-Settings", "{}"))
                q = urllib.parse.urlparse(self.path).query
                fn = urllib.parse.parse_qs(q).get("name", ["song.mp3"])[0]
                result = generate_chart_model(raw, fn, params)
                self._send(200, json.dumps(result))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)}))
        elif route == "/api/separate":
            try:
                if length > MAX_AUDIO_BYTES:
                    self._send(413, json.dumps(
                        {"ok": False, "error": "音频文件过大（上限 1GB）"}))
                    return
                raw = self.rfile.read(length) if length else b""
                q = urllib.parse.urlparse(self.path).query
                fn = urllib.parse.parse_qs(q).get("name", ["song.mp3"])[0]
                result = separate_all(raw, fn)
                self._send(200, json.dumps(result))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)}))
        elif route == "/api/preview_track":
            try:
                if length > MAX_AUDIO_BYTES:
                    self._send(413, json.dumps(
                        {"ok": False, "error": "音频文件过大（上限 1GB）"}))
                    return
                raw = self.rfile.read(length) if length else b""
                params = json.loads(self.headers.get("X-Settings", "{}"))
                q = urllib.parse.urlparse(self.path).query
                fn = urllib.parse.parse_qs(q).get("name", ["song.mp3"])[0]
                track = params.get("track", "all") or "all"
                result = preview_track(raw, fn, track)
                self._send(200, json.dumps(result))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)}))
        elif route == "/api/train_start":
            try:
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw.decode("utf-8")) if raw else {}
                kind = body.get("kind", "all")
                data_dir = body.get("data_dir", "")
                opts = {k: body.get(k) for k in ("onset_epochs", "vae_epochs", "ddpm_epochs", "finetune_on")}
                ok, msg = _start_training(kind, data_dir, opts)
                self._send(200, json.dumps({"ok": ok, "msg": msg}))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)}))
        elif route == "/api/train_log":
            self._send(200, self._train_log_payload())
        elif route == "/api/train_stop":
            proc = TRAIN_STATE.get("proc")
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
                TRAIN_STATE["status"] = "idle"
                TRAIN_STATE["proc"] = None
                self._send(200, json.dumps({"ok": True, "msg": "已停止训练"}))
            else:
                self._send(200, json.dumps({"ok": True, "msg": "当前没有进行中的训练"}))
        elif route == "/api/train_vfx_start":
            try:
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw.decode("utf-8")) if raw else {}
                data_dir = body.get("data_dir", "")
                epochs = int(body.get("epochs", 60))
                rebuild = bool(body.get("rebuild_cache", False))
                ok, msg = _start_vfx_training(data_dir, epochs, rebuild)
                self._send(200, json.dumps({"ok": ok, "msg": msg}))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "error": str(e)}))
        elif route == "/api/vfx_train_stop":
            proc = VFX_STATE.get("proc")
            if proc:
                try:
                    proc.kill()
                except Exception:
                    pass
                VFX_STATE["status"] = "idle"
                VFX_STATE["proc"] = None
                self._send(200, json.dumps({"ok": True, "msg": "已停止 VFX 训练"}))
            else:
                self._send(200, json.dumps({"ok": True, "msg": "当前没有进行中的 VFX 训练"}))
        elif route == "/api/vfx_force_stop":
            killed = _kill_train_vfx()
            self._send(200, json.dumps({"ok": True, "killed": killed}))
        elif route == "/api/shutdown":
            self._send(200, json.dumps({"ok": True}))
            if SERVER_REF[0] is not None:
                threading.Thread(target=SERVER_REF[0].shutdown).start()
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, *a):
        pass  # 静默


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True


def open_browser(url, health_url):
    import urllib.request
    for _ in range(60):
        try:
            with urllib.request.urlopen(health_url, timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            pass
        time.sleep(0.2)
    try:
        webbrowser.open(url)
    except Exception:
        try:
            os.startfile(url)
        except Exception:
            pass


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0, help="固定端口(默认随机空闲端口)")
    ap.add_argument("--no-browser", action="store_true", help="不起浏览器(仅服务)")
    args = ap.parse_args()

    srv = Server(("127.0.0.1", args.port or 0), Handler)
    SERVER_REF[0] = srv
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"ADOFAI Diffusion 已启动: {url}")
    if not args.no_browser:
        threading.Thread(target=open_browser, args=(url, url + "api/health"),
                         daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
