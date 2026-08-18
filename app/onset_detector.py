"""
onset_detector.py — 音频 -> 节拍时间戳（采音）

为什么不用旧工具的"振幅峰值"：
- 旧工具只取每窗最大振幅，钢琴余音/混响让振幅一直高 -> 误判为新拍；
  混乱编曲里它只追最响的，且相邻拍强耦合、错一拍全崩。
- 这里改用 **频谱 flux（频谱流）**：看"各频率能量比上一刻涨了多少"。
    * 钢琴砸下去那一下的音头 -> 频谱猛涨 -> flux 尖峰 -> 记一拍；
    * 之后余音只是同一堆频率在响 -> flux 接近 0 -> 不误触发。
    * 天生抗余音、抗混响，比只看总响度稳得多。

流程：
1. 频谱 flux：每帧梅尔谱 dB 的正向差分之和 -> onset 强度包络
2. 自适应阈值：滑动均值 + k*标准差（响段自动提门槛、静段自动降），超过才算候选
3. 峰值挑选：候选窗口内取局部最大，且距上一个 onset >= wait 帧 -> 去重

实现说明（2026-08-05）：
- 纯 numpy 实现 STFT + 梅尔三角滤波，不依赖 torch/torchaudio
  （本机 import torch 必段错误，而采音算法不需要深度学习框架）。
- 音频解码：优先 ffmpeg（独立进程，避开 Python DLL 冲突）转 raw PCM；
  失败回退标准库 wave（仅 wav）。

输出：时间戳列表（秒），相对音频开头。直接喂给 generate.py / chart_repr.dense_to_adofai。

（备用）estimate_bpm：自相关在 onset 间隔上找主周期，供以后"网格吸附/节拍对齐"用。
"""
import os
import sys
import wave
import subprocess
import numpy as np

SR = 22050
N_MELS = 128
N_FFT = 2048
# 128 全链路：预览图的 onset 检测也用 128 网格，和生成/训练一致
HOP_LENGTH = 128

# 梅尔三角滤波组（模块级缓存，避免重复构造）
_MEL_FILTERBANK = None


# ---------------------------------------------------------------------------
# 音频读取 / 解码
# ---------------------------------------------------------------------------
def _ffmpeg_exe():
    """定位 ffmpeg 可执行文件，按优先级：
    1) portable 根目录自带的 ffmpeg.exe（彻底离线、免系统安装，最优先）
    2) 与当前 python.exe 同目录
    3) imageio-ffmpeg 自带的静态二进制（pip 安装时联网下载，可能缺失）
    4) 系统 PATH 里的 ffmpeg
    这样即便离线 / venv 不完整，也能用内置 ffmpeg 工作。"""
    # portable 根目录（本文件在 app/ 下，回退两级到 portable 根）
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    candidates = [
        os.path.join(_root, "ffmpeg.exe"),
        os.path.join(os.path.dirname(sys.executable), "ffmpeg.exe"),
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.exists(exe):
            return exe
    except Exception:
        pass
    import shutil
    return shutil.which("ffmpeg")


def _load_with_ffmpeg(audio_path):
    """ffmpeg 把任意格式解码成 22050Hz 单声道 16bit raw PCM（内存）。"""
    exe = _ffmpeg_exe()
    if not exe:
        return None
    try:
        proc = subprocess.run(
            [exe, "-hide_banner", "-loglevel", "error", "-i", audio_path,
             "-f", "s16le", "-ac", "1", "-ar", str(SR), "-"],
            capture_output=True, check=True, timeout=120)
        if not proc.stdout:
            return None
        return np.frombuffer(proc.stdout, dtype="<i2").astype(np.float32) / 32768.0
    except Exception:
        return None


def load_mono(audio_path):
    """读取音频 -> 单声道 numpy 波形 @SR。返回 (sig(1D), sr)。"""
    pcm = _load_with_ffmpeg(audio_path)
    if pcm is not None and len(pcm) > 0:
        return pcm.astype(np.float32), SR
    # 回退：标准库 wave（仅支持未压缩 wav）
    with wave.open(audio_path, "rb") as w:
        nch = w.getnchannels()
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    sig = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if nch > 1:
        sig = sig.reshape(-1, nch).mean(axis=1)
    if sr != SR:
        xs = np.arange(len(sig))
        new_len = int(len(sig) * SR / sr)
        sig = np.interp(np.linspace(0, len(sig) - 1, new_len), xs, sig)
    return sig.astype(np.float32), SR


# ---------------------------------------------------------------------------
# 梅尔滤波组 / 频谱
# ---------------------------------------------------------------------------
def _mel_filterbank(sr=SR, n_fft=N_FFT, n_mels=N_MELS):
    global _MEL_FILTERBANK
    if _MEL_FILTERBANK is not None:
        return _MEL_FILTERBANK

    def hz2mel(h):
        return 2595.0 * np.log10(1.0 + h / 700.0)

    def mel2hz(m):
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)

    fmax = sr / 2.0
    mel_pts = np.linspace(hz2mel(0), hz2mel(fmax), n_mels + 2)
    hz_pts = mel2hz(mel_pts)
    bins = np.floor((n_fft + 1) * hz_pts / sr).astype(int)

    fb = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
    for m in range(1, n_mels + 1):
        l, c, r = bins[m - 1], bins[m], bins[m + 1]
        for k in range(l, c):
            if c > l:
                fb[m - 1, k] = (k - l) / (c - l)
        for k in range(c, r):
            if r > c:
                fb[m - 1, k] = (r - k) / (r - c)
    _MEL_FILTERBANK = fb
    return fb


def mel_log(sig, sr=SR):
    """波形 -> 梅尔谱 log 功率 (n_mels, T)。纯 numpy STFT + 梅尔三角滤波。"""
    sig = np.asarray(sig, dtype=np.float32)
    L = len(sig)
    if L < N_FFT:
        sig = np.pad(sig, (0, N_FFT - L + 1))
        L = len(sig)
    n_frames = 1 + (L - N_FFT) // HOP_LENGTH
    frames = np.lib.stride_tricks.sliding_window_view(sig, N_FFT)[::HOP_LENGTH][:n_frames]
    win = np.hamming(N_FFT).astype(np.float32)
    windowed = frames * win
    spec = np.fft.rfft(windowed, axis=1)
    mag = np.abs(spec) ** 2
    fb = _mel_filterbank(sr, N_FFT, N_MELS)
    mel = mag @ fb.T
    logmel = np.log(mel + 1e-6)
    return logmel.T  # (n_mels, T)


def spectral_flux(logmel):
    """频谱流：每帧的正向能量变化之和。diff 后长度 T-1。"""
    d = np.diff(logmel, axis=1)
    d = np.clip(d, 0, None)          # 只取"上升"部分（onset = 能量突增）
    return d.sum(axis=0)             # (T-1,)


def onset_envelope(logmel):
    """逐帧 onset 强度包络 (T,)，归一化到 [0,1]。

    用于阶段二扩散的 *局部* 条件：把"这一刻有没有音头/多强"作为可直接抄的
    局部信号喂给模型（而非只用全局梅尔）。模型由此才能学会在鼓点处转角、
    按能量变化角度——否则只能回退到训练集平均谱（全常量），即 conditioning
    collapse。与 detect_onsets 同源（都来自 spectral_flux），保证训练/推理一致。
    """
    d = np.diff(logmel, axis=1)
    d = np.clip(d, 0, None)
    env = d.sum(axis=0)                              # (T-1,)
    env = np.concatenate([env[:1], env], axis=0)     # (T,)
    if env.shape[0] >= 3:
        env = np.convolve(env, np.ones(3, dtype=np.float32) / 3.0, mode="same")
    mx = env.max()
    if mx > 1e-8:
        env = env / mx
    return env.astype(np.float32)                    # (T,)


# ---------------------------------------------------------------------------
# 峰值挑选（自适应阈值）
# ---------------------------------------------------------------------------
def pick_peaks(flux, wait=4, pre_max=4, post_max=4, avg_win=24, k=1.0, delta=1e-4):
    """
    flux        : onset 强度包络 (帧,)
    wait        : 相邻 onset 最小间距（帧），去抖
    pre/post_max: 局部最大判定窗口（帧）
    avg_win     : 滑动均值/标准差窗口（帧）~ avg_win*hop ≈ 0.56s @SR22050
    k           : 阈值 = 均值 + k*标准差；越大越严格（只留明显 onset）
    delta       : 额外绝对门槛，滤掉底噪
    """
    N = len(flux)
    if N == 0:
        return np.array([], dtype=int)
    kernel = np.ones(avg_win) / avg_win
    avg = np.convolve(flux, kernel, mode="same")
    avg2 = np.convolve(flux ** 2, kernel, mode="same")
    std = np.sqrt(np.maximum(avg2 - avg ** 2, 0))
    thresh = avg + k * std + delta

    cand = np.where(flux > thresh)[0]
    onsets = []
    last = -10 ** 9
    for c in cand:
        if c - last < wait:
            continue
        lo = max(0, c - pre_max)
        hi = min(N, c + post_max + 1)
        if flux[c] >= flux[lo:hi].max() - 1e-9:   # 窗口内局部最大
            onsets.append(int(c))
            last = c
    return np.array(onsets, dtype=int)


# ---------------------------------------------------------------------------
# 备用：BPM 自相关估计（给以后"网格吸附"用）
# ---------------------------------------------------------------------------
def estimate_bpm(times, bpm_min=70, bpm_max=200):
    """在 onset 时间戳上做 comb/自相关，估主节奏 BPM。失败返回 None。"""
    if len(times) < 4:
        return None
    grid = 0.01
    n = int(times[-1] / grid) + 1
    x = np.zeros(n)
    for t in times:
        idx = _clamp_idx(t, grid, n)
        if idx >= 0:
            x[idx] += 1.0
    lo_lag = int((60.0 / bpm_max) / grid)
    hi_lag = int((60.0 / bpm_min) / grid)
    best, best_lag = -1, lo_lag
    for lag in range(lo_lag, min(hi_lag, n)):
        s = np.sum(x[:-lag] * x[lag:]) if lag < n else 0
        if s > best:
            best, best_lag = s, lag
    if best_lag <= 0:
        return None
    return 60.0 / (best_lag * grid)


def _clamp_idx(t, grid, n):
    idx = int(t / grid)
    return idx if 0 <= idx < n else -1


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def _find_onset_start(flux, peak, prev_peak=None, frac=0.1):
    """从峰值往前找 flux 跌破 frac*峰值 的帧 = onset 上升沿起点（音头那一下）。
    搜索下限限定在上一个 peak 之后，避免两个 onset 包络重叠时往前越界导致乱序。"""
    thr = frac * flux[peak]
    lo = (prev_peak + 1) if prev_peak is not None else 0
    k = peak
    while k > lo and flux[k] > thr:
        k -= 1
    return k


def detect_onsets(audio_path, wait=4, avg_win=24, k=1.0, delta=1e-4, lead_frac=0.1):
    """返回 (times秒列表, flux数组, logmel数组)。"""
    sig, sr = load_mono(audio_path)
    logmel = mel_log(sig, sr)
    flux = spectral_flux(logmel)
    peaks = pick_peaks(flux, wait=wait, avg_win=avg_win, k=k, delta=delta)
    # onset 取上升沿起点（而非峰值）以贴近真实音头时刻，且保证时间戳单调递增
    starts, prev = [], None
    for c in peaks:
        s = _find_onset_start(flux, c, prev, frac=lead_frac)
        starts.append(s)
        prev = c
    times = (np.array(starts) + 1) * HOP_LENGTH / SR
    return times.tolist(), flux, logmel


# ---------------------------------------------------------------------------
# 合成测试音 + 自测（不依赖真实歌曲即可验证采音基本正确）
# ---------------------------------------------------------------------------
def _save_wav(path, sig, sr):
    sig = np.clip(sig, -1.0, 1.0)
    pcm = (sig * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())


def synth_click_track(out_wav, bpm=120, n_beats=32, sr=SR, seed=0):
    """生成已知节拍的音频：每拍一个 kick(低频衰减)+瞬态点击，叠稳态噪声。
    返回真实节拍时间戳（秒），用于和检测结果比对。"""
    rng = np.random.default_rng(seed)
    beat = 60.0 / bpm
    total = int((n_beats + 2) * beat * sr)
    sig = np.zeros(total, dtype=np.float32)
    for i in range(n_beats):
        t0 = int(i * beat * sr)
        dur = int(0.12 * sr)
        tt = np.arange(dur) / sr
        env = np.exp(-tt * 25)
        sig[t0:t0 + dur] += 0.9 * env * np.sin(2 * np.pi * 55 * tt)   # kick
        sig[t0] += 0.6                                               # 瞬态 click
    sig += 0.02 * rng.standard_normal(total).astype(np.float32)       # 稳态噪声
    sig /= np.max(np.abs(sig))
    _save_wav(out_wav, sig, sr)
    return np.array([i * beat for i in range(n_beats)], dtype=float)


if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    out_wav = os.path.abspath(os.path.join(here, "..", "data", "_synthetic_test.wav"))
    true_times = synth_click_track(out_wav, bpm=120, n_beats=32)
    detected, flux, logmel = detect_onsets(out_wav)

    det = np.array(detected)
    errs, matched = [], 0
    for tt in true_times:
        if len(det) == 0:
            break
        d = np.abs(det - tt)
        m = d.min()
        if m < 0.1:
            errs.append(m)
            matched += 1
    print("[自测] 真实节拍=%d  检测到=%d  匹配(0.1s内)=%d  额外(假阳性/弱拍)=%d" % (
        len(true_times), len(detected), matched, len(detected) - matched))
    if errs:
        print("[自测] 匹配到的平均时间误差=%.1fms  最大=%.1fms" % (
            np.mean(errs) * 1000, np.max(errs) * 1000))
    bpm = estimate_bpm(detected)
    print("[自测] 自相关估 BPM=%s  (真实=120.0)" % ("None" if bpm is None else round(bpm, 1)))
