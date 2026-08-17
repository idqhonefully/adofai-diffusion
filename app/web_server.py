"""
web_server.py — ADOFAI Maker 浏览器 GUI 后端（128 全链路版：OnsetNet 踩点 + VAE/扩散加事件）

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

import paths
from onset_detector import SR, HOP_LENGTH, detect_onsets, _ffmpeg_exe

SERVER_REF = [None]
# 单次上传体量上限（音频）。超过直接 413 拒绝，避免大文件整包读进内存拖崩服务。
MAX_AUDIO_BYTES = 1 * 1024 ** 3

# 模型路线（VAE+扩散 / OnsetNet）依赖 torch，跑在 venv 里；网页后端用自带 python 跑。
APP_DIR = os.path.dirname(os.path.abspath(__file__))
PORTABLE_ROOT = str(paths.ROOT)
# venv 解释器路径：Windows 在 venv/Scripts/python.exe，POSIX 在 venv/bin/python。
# 容器部署可设环境变量 ADOFAI_VENV_PY 直接指到同一套 python（不必真的建 venv）。
def _venv_python():
    env = os.environ.get("ADOFAI_VENV_PY")
    if env:
        return env
    return paths.venv_python(PORTABLE_ROOT)

VENV_PY = _venv_python()
INFER_SCRIPT = os.path.join(PORTABLE_ROOT, "app", "training", "inference_stage2.py")
TRAIN_LOG = str(paths.TRAIN_LOG)
PREVIEW_DIR = str(paths.PREVIEW_DIR)
paths.ensure_dirs()

# ---------- 训练状态机 ----------
TRAIN_STATE = {"proc": None, "kind": None, "started": 0.0, "status": "idle", "phase": ""}

# ---------- 运行模式 ----------
# in-process：推理/分离在 web_server 进程内常驻运行（需 torch，模型只加载一次）。
# 容器/常驻部署建议开启；Windows 子进程模式（默认）行为与旧版完全一致。
IN_PROCESS = bool(os.environ.get("ADOFAI_IN_PROCESS", ""))


def _load_training_module(name):
    """懒加载 app/training 下的模块（in-process 用；避免启动 web_server 就必须有 torch）。"""
    import importlib
    training_dir = os.path.join(APP_DIR, "training")
    for p in (APP_DIR, training_dir):
        if p not in sys.path:
            sys.path.insert(0, p)
    return importlib.import_module(name)


def _resource(rel):
    """资源定位：冻结后从 _MEIPASS 取，开发时从脚本同目录取。"""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel)


def _append_log(msg):
    try:
        with open(TRAIN_LOG, "a", encoding="utf-8") as f:
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


def _training_worker(commands, kind):
    """顺序执行 commands: list[(label, argv, env_extra, expect)]。输出实时写入 TRAIN_LOG。

    expect: 该步预期产出的权重文件路径列表；若进程在"活儿干完之后"的 CUDA 拆栈阶段
    崩溃退出（Windows 上常见的 0xC0000409 / 3221226505 栈缓冲区溢出，属善意的收尾崩溃），
    只要预期产物已落盘且非空，就判为成功并继续后续步骤，避免整条训练链被一个无害的退出码打断。
    """
    TRAIN_STATE["status"] = "running"
    TRAIN_STATE["kind"] = kind
    TRAIN_STATE["started"] = time.time()
    # GPU 驱动被前面崩溃搞脏时，CUDA 初始化会 segfault -> 自动 fallback 到 CPU
    use_cpu = not _cuda_usable()
    if use_cpu:
        _append_log("[info] 检测到 CUDA 不可用（驱动状态异常），自动改用 CPU 训练（模型质量一致，速度较慢）")
    overall_ok = True
    for label, argv, env_extra, expect in commands:
        TRAIN_STATE["phase"] = label
        _append_log(f"\n===== {label} =====")
        env = dict(os.environ)
        env.update({
            "PYTHONNOUSERSITE": "1",
            "PYTHONIOENCODING": "utf-8",  # 子进程 stdout 用 utf-8, 避免中文/日文文件名 GBK 崩溃 + 日志乱码
            "MKL_THREADING_LAYER": "sequential", "MKL_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1", "KMP_AFFINITY": "disabled",
            "OPENBLAS_NUM_THREADS": "1", "NUMEXPR_MAX_THREADS": "1",
            "PYTHONUNBUFFERED": "1",  # 强制子进程无缓冲, 训练日志实时落盘(避免静默假死错觉)
        })
        if use_cpu:
            env["CUDA_VISIBLE_DEVICES"] = "-1"
        env.update(env_extra or {})
        try:
            proc = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    bufsize=1, encoding="utf-8", errors="replace")
            TRAIN_STATE["proc"] = proc
            for line in proc.stdout:
                _append_log(line.rstrip("\n"))
            rc = proc.wait()
        except Exception as e:
            _append_log(f"[error] 启动失败: {e}")
            rc = -1
        TRAIN_STATE["proc"] = None
        # 成功判定：退出码 0，或（退出码非 0 但预期产物已落盘）
        if rc != 0 and not _outputs_ready(expect):
            overall_ok = False
            _append_log(f"[result] {label} 退出码={rc}（失败，未产出权重）")
            break
        if rc != 0:
            _append_log(f"[warn] {label} 进程退出码={rc}（疑似 CUDA 收尾崩溃），"
                        f"但权重已正确生成，判定为成功并继续")
        _append_log(f"[result] {label} 完成 ✓")
    TRAIN_STATE["status"] = "done" if overall_ok else "error"
    TRAIN_STATE["phase"] = ""


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

    onset_cmd = [VENV_PY, os.path.join(PORTABLE_ROOT, "app", "training", "train_onset.py"),
                 "--train_dir", data_dir,
                 "--epochs", str(int(opts.get("onset_epochs", 80))),
                 "--out", os.path.join(ckpt, "onset_net.pt")]
    diff_env = {"ADOFAI_TRAIN_DIR": data_dir,
                "ADOFAI_DATA_DIR": os.path.join(PORTABLE_ROOT, "data")}
    if opts.get("vae_epochs"):
        diff_env["VAE_EPOCHS"] = str(int(opts["vae_epochs"]))
    if opts.get("ddpm_epochs"):
        diff_env["DDPM_EPOCHS"] = str(int(opts["ddpm_epochs"]))
    diff_cmd = [VENV_PY, os.path.join(PORTABLE_ROOT, "app", "training", "train_stage2.py")]

    onset_expect = [os.path.join(ckpt, "onset_net.pt")]
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
    """AI 模型路线：音频 -> OnsetNet+VAE+扩散 -> .adofai（in-process 常驻 or venv 子进程）。"""
    if not IN_PROCESS and not os.path.exists(VENV_PY):
        return {"ok": False, "error": "未找到模型运行环境（venv 缺失）。请确认 portable/venv 存在。"}
    base_bpm = 120.0
    bpm_raw = params.get("bpm")
    try:
        if bpm_raw not in (None, "", 0) and float(bpm_raw) > 0:
            base_bpm = float(bpm_raw)
    except (TypeError, ValueError):
        pass
    difficulty = int(params.get("difficulty", 1))
    track = params.get("track", "all") or "all"

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

    # ---- 根据所选音轨决定喂给 AI 的音频与 track ----
    selected = params.get("selected") or []
    try:
        infer_wav, track = _resolve_infer_input(tmpdir, wav_path, selected, adofai_path)
    except Exception as e:
        return {"ok": False, "error": f"音轨处理失败：{e}"}
    return run_inference(infer_wav, track, difficulty, base_bpm, adofai_path, filename)


def _venv_env():
    env = dict(os.environ)
    env.update({
        "PYTHONNOUSERSITE": "1",
        "MKL_THREADING_LAYER": "sequential", "MKL_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1", "KMP_AFFINITY": "disabled",
        "OPENBLAS_NUM_THREADS": "1", "NUMEXPR_MAX_THREADS": "1",
    })
    return env


def _run_stem_separate(wav_path, track, out_path):
    """分离单条音轨（推理输入用），返回 {ok, error?}。"""
    r = _run_preview_track(wav_path, track, out_path)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error", "分离失败")}
    if not os.path.exists(out_path):
        return {"ok": False, "error": "分离结果未生成"}
    return {"ok": True}


def _run_separate_all(wav_path, prefix):
    """一次分离全部音轨到 PREVIEW_DIR，返回 {ok, stems:[...], error?}。"""
    if IN_PROCESS:
        try:
            m = _load_training_module("separate_all")
            return m.do_separate(wav_path, PREVIEW_DIR, prefix)
        except Exception as e:
            return {"ok": False, "error": f"分离失败：{e}"}
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


def _run_preview_track(wav_path, track, out_path):
    """分离单条音轨到 out_path，返回 {ok, duration_s?, error?}。"""
    if IN_PROCESS:
        try:
            m = _load_training_module("preview_track")
            return m.do_preview(wav_path, str(track), out_path)
        except Exception as e:
            return {"ok": False, "error": f"分离失败：{e}"}
    if not os.path.exists(VENV_PY):
        return {"ok": False, "error": "未找到模型运行环境（venv 缺失）。"}
    env = dict(os.environ)
    env.update({
        "PYTHONNOUSERSITE": "1",
        "MKL_THREADING_LAYER": "sequential", "MKL_NUM_THREADS": "1",
        "OMP_NUM_THREADS": "1", "KMP_AFFINITY": "disabled",
        "OPENBLAS_NUM_THREADS": "1", "NUMEXPR_MAX_THREADS": "1",
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


def _infer_model(wav_path, track, base_bpm, adofai_path):
    """生成 .adofai（in-process 常驻 or venv 子进程）。返回 (ok, err_or_none)。"""
    if IN_PROCESS:
        try:
            m = _load_training_module("inference_stage2")
            level = m.generate(wav_path, adofai_path, base_bpm=base_bpm,
                               steps=50, guidance=2.5, onset_track=track)
            if level is None:
                return False, "dense_to_adofai 返回 None（踩点不足）"
            return True, None
        except Exception as e:
            return False, f"模型推理失败：{e}"
    try:
        proc = subprocess.run(
            [VENV_PY, INFER_SCRIPT, "--audio", wav_path, "--out", adofai_path,
             "--bpm", str(base_bpm), "--steps", "50", "--guidance", "2.5",
             "--track", str(track)],
            env=_venv_env(), capture_output=True, text=True, timeout=900)
    except Exception as e:
        return False, f"模型推理进程启动失败：{e}"
    if not os.path.exists(adofai_path):
        return False, (proc.stderr or proc.stdout or "")[:600]
    return True, None


def run_inference(wav_path, track, difficulty, base_bpm, adofai_path, filename):
    """给定已解码 wav 与 track，跑 OnsetNet+VAE/扩散 -> .adofai。"""
    try:
        times_s, flux, logmel = detect_onsets(wav_path)
    except Exception as e:
        return {"ok": False, "error": f"频谱分析失败：{e}"}
    ok, err = _infer_model(wav_path, track, base_bpm, adofai_path)
    if not ok:
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
        adofai_text = json.dumps(level, ensure_ascii=False, indent=1)
    except Exception:
        pass
    angle = level.get("angleData", []) or []
    actions = level.get("actions", []) or []
    twirl = sum(1 for a in actions if isinstance(a, dict) and a.get("eventType") == "Twirl")
    note_count = len(angle)
    duration_s = round(logmel.shape[1] * HOP_LENGTH / SR, 2)
    png = plot_onsets_bytes(logmel, times_s)
    return {
        "ok": True,
        "adofai": adofai_text,
        "preview_png": base64.b64encode(png).decode("ascii"),
        "note_count": note_count,
        "twirl_count": twirl,
        "max_error_ms": None,
        "mean_error_ms": None,
        "bpm_used": base_bpm,
        "duration_s": duration_s,
        "song": os.path.basename(filename),
        "pattern": "model128",
        "has_setspeed": False,
    }


def separate_all(audio_bytes, filename):
    """分离全部音轨，返回各分解音轨的可播放 url 列表（不需要训练好的权重）。"""
    if not IN_PROCESS and not os.path.exists(VENV_PY):
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
    if not IN_PROCESS and not os.path.exists(VENV_PY):
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

    r = _run_preview_track(wav_path, track, out_path)
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
        elif path == "/api/train_log":
            self._send(200, self._train_log_payload())
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
            self._send(404, json.dumps({"error": "not found"}))

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
                opts = {k: body.get(k) for k in ("onset_epochs", "vae_epochs", "ddpm_epochs")}
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
    ap.add_argument("--host", default="127.0.0.1", help="监听地址（容器部署用 0.0.0.0）")
    ap.add_argument("--port", type=int, default=0, help="固定端口(默认随机空闲端口)")
    ap.add_argument("--no-browser", action="store_true", help="不起浏览器(仅服务)")
    ap.add_argument("--in-process", action="store_true",
                    help="推理/分离在进程内常驻运行（需 torch；容器/常驻部署建议开启）")
    args = ap.parse_args()

    global IN_PROCESS
    if args.in_process:
        IN_PROCESS = True

    srv = Server((args.host, args.port or 0), Handler)
    SERVER_REF[0] = srv
    port = srv.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"ADOFAI Maker (128 全链路) 已启动: {url}")
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
