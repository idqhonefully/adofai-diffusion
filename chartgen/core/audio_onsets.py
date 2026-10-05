"""OGG/WAV → 伪音轨（音头）→ 和 `core.midi.load()` **同构**的 MidiFile。

为什么不是"转成真 MIDI"：
    我们的管线只吃 **onset 时刻**（`Onset.t_ms`），音高只用来当曲名。
    真正需要的是"这一拍有没有音"，而不是"是什么音"。所以直接把音频切成音头，
    比走"多声部转谱 → 再切音"少一道丢音环节。

★ 为什么必须切轨（不切就是灾难性的全采）：
    音频里所有乐器一起响，音头检测会把伴奏、鼓、和声全部收进来。
    实测一条"全采"轨在 171s 上有 1209 个音头 ≈ 每秒 7 个，人根本没法取舍。
    所以这里在**频谱域**做两层切分：

      HPSS（librosa.decompose.hpss）：谱图上把「打击性」和「调性」分开
        → 打击轨（默认不勾，通道 9）
      再把调性分量按频段切成 3 条
        → 旋律·低频 / 旋律·中频 / 旋律·高频

    这样直接在 GUI 里勾选就行，用的是现成的音轨选择 UI。

★ 产出的 MidiFile 和 `core.midi.load()` 完全同构，
  于是 `build_onsets` / `solve` / `writer` 一行都不用改 —— 有 OGG 就能做谱。

    from core.audio_onsets import load_as_midi, detect_onsets, estimate_bpm
    mf = load_as_midi("song.ogg")          # 和 load("song.mid") 一样的接口
    ons = build_onsets(mf.tracks[0].notes, OnsetParams(merge_ms=30.0))

网格吸附很重要：先估出 BPM，再把音头吸附到 `beat/grid` 的网格上，
并把 `t_on_tick` 按同一个网格写死 —— 这样下游的 `beats_of` 拿到的 tick
是干净有理数，三连音/16 分判断才不会因为几毫秒抖动而失效。

★ **但网格本身必须先自洽**（`core/gridfit`，见 docs/17）：
  拿 librosa 估出来的 BPM 直接建网格，0.15% 的周期相对误差会让网格以 `ε·t`
  漂移 —— 64s 的曲子漂 96ms，于是"前半段吸附把偏置纠回来、后半段吸附把
  onset 挪到错格子上"，表现为**靠后位置的随机偏移（50~100ms）**。
  所以这里先用 onset 自己把「周期 + 相位」精修到自洽，再吸附；
  拟合不可信（集中度 R 太低）时**整条轨不吸**。
"""
from __future__ import annotations

import numpy as np

from .midi import MidiFile, Note, Track
from . import gridfit as GF

PPQN = 480

# 频段切分（Hz）。mel 轴做映射，不是硬滤波。
BANDS: tuple[tuple[str, float, float], ...] = (
    ("中频", 250.0, 2000.0),      # ← 主旋律通常在这里，排在第一个非打击轨
    ("低频", 0.0, 250.0),
    ("高频", 2000.0, 8000.0),
)

# ★ 拟合档位。用户口径：音频转出来的音头不精确，而"为手感画横平竖直"比
#   "为采音强留微小弧度"更重要，丢精度可容忍。但四件事是互相拉扯的，实测
#   （五月雪版 FallenEra.ogg，中频轨 653 音头，straight=少）：
#
#     档位      grid/pb   直线   SetSpeed  SetSpeed/100格  模板   重叠<0.5R
#     保细节    12/0.0      3%      42        6.6         1段      138
#     平衡       6/0.5      3%      62        9.5         4段      109
#     +直线=多   6/0.5     23%      95       14.5         1段       67
#
#   换 `straight_pref` 会把 base_bpm 从 119.84 抬到 359.5 —— 那才是直线率的
#   真正开关（base=120 时主音值 166.9ms = 1/3 拍，而 travel=180 需要 k=1/3，
#   不是合法的 2 的幂 ⇒ **直线在原理上就不可能**）。
FIT_PRESETS: dict[str, dict] = {
    "保细节": dict(grid=12, prefer_beat=0.0),   # 三连音/细分全留，直线最低
    "平衡": dict(grid=6, prefer_beat=0.5),      # 默认：保住三连音，杀掉 1/12 抖动
    "方正": dict(grid=4, prefer_beat=0.5),      # 16 分网格，直线明显上升
    "极方正": dict(grid=2, prefer_beat=0.0),    # 8 分网格，三连音会被吃掉
}


def _load_mono(path: str, sr: int = 22050) -> tuple[np.ndarray, int]:
    import librosa
    y, sr = librosa.load(path, sr=sr, mono=True)
    return np.asarray(y, dtype=np.float32), int(sr)


def estimate_bpm(path_or_y, sr: int | None = None, *, hop: int = 64,
                 start_bpm: float = 180.0) -> float:
    """估 BPM。可以传路径，也可以传 (y, sr)。"""
    import librosa
    if isinstance(path_or_y, str):
        y, sr = _load_mono(path_or_y, sr or 22050)
    elif isinstance(path_or_y, tuple):
        y, sr = path_or_y
        sr = int(sr)
    else:
        y = path_or_y
        sr = int(sr or 22050)
    y = np.asarray(y, dtype=np.float32)
    env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop)
    est = librosa.feature.tempo(onset_envelope=env, sr=sr, hop_length=hop,
                                start_bpm=start_bpm, aggregate=None)
    est = np.atleast_1d(np.asarray(est, dtype=float))
    return float(np.median(est)) if est.size else float(start_bpm)


def _pick(env: np.ndarray, sr: int, hop: int, *, pct: float = 82.0,
          min_gap_ms: float = 40.0, **_) -> tuple[np.ndarray, np.ndarray]:
    """包络 → 峰值 → (毫秒, 强度)。

    ★ 用「**峰强度的分位数**」当阈值，而不是绝对 dB 或相对 MAD。

    踩过的两个坑：
      ① 绝对 delta：按频段切分后各段量程不同 → 弱频段被疯狂过采。
      ② MAD 相对阈值：MAD 很小 → 阈值形同虚设，实测 4 轨共 9350 个音头
         （55/秒），比全采还惨。
    分位数阈值是**尺度无关**的，而且 `pct` 直接对应"留下最上面的百分之几"，
    采多少是**可数、可调**的 —— 这才是"能取舍"。

    `pct=82` → 只保留最强的前 18% 峰值。想更密就调低，想只留主干就调高。
    """
    from scipy.signal import find_peaks
    env = np.asarray(env, dtype=float)
    if env.size == 0:
        return np.zeros(0), np.zeros(0)
    wait = max(1, int(round(min_gap_ms / (hop / sr * 1000.0))))
    peaks, _props = find_peaks(env, distance=wait)
    if len(peaks) == 0:
        return np.zeros(0), np.zeros(0)
    strength = env[peaks]
    if 0.0 < pct < 100.0:
        thr = float(np.percentile(strength, pct))
        m = strength >= thr
        peaks, strength = peaks[m], strength[m]
    if len(peaks) == 0:
        return np.zeros(0), np.zeros(0)
    t_ms = peaks.astype(float) / sr * hop * 1000.0
    order = np.argsort(t_ms)
    return t_ms[order], strength[order]


def _peak_params(pct: float, min_gap_ms: float, hop: int, sr: int) -> dict:
    return dict(pct=float(pct), min_gap_ms=min_gap_ms)


def rms_envelope(y: np.ndarray, sr: int, win_ms: float = 2.0) -> np.ndarray:
    """短窗 RMS 包络（**样本分辨率**，无 STFT 半窗延迟）。

    ★ 为什么不用 STFT 包络定位音头：`onset_strength` 的窗半宽 `n_fft/2=1024`
      样本 ≈ 46ms，脉冲在到达窗中心**之前**能量就进窗了 ⇒ 峰值**提前**
      （本仓库实测 −35.5ms）。2ms 短窗 RMS 没有这个不对称性。
    """
    w = max(2, int(round(sr * win_ms / 1000.0)))
    yy = np.asarray(y, dtype=np.float64)
    if len(yy) <= w:
        return np.zeros(0)
    c = np.concatenate([[0.0], np.cumsum(yy * yy)])
    e = (c[w:] - c[:-w]) / w
    return np.sqrt(np.maximum(e, 0.0))


def estimate_detector_bias(y: np.ndarray, sr: int, t_ms,
                           *, win_ms: float = 2.0,
                           back_ms: float = 25.0, fwd_ms: float = 55.0,
                           top_frac: float = 0.30,
                           min_n: int = 8, max_abs_ms: float = 46.0,
                           max_iqr_ms: float = 20.0) -> tuple[float, dict]:
    """量出**检波器的常量偏置**（ms，正 = 检出偏早，需要往后挪）。

    做法：对每个粗检出的 onset，在它附近 `[−back, +fwd]` 里找**短窗 RMS 的峰值**，
    峰位相对粗检出的差就是一次"偏置观测"；然后取这批观测的**中位数**。

    ★ 关键决策三条：
      ① **只估一个全局常量，不做逐 onset 修正**。偏置是系统性的（STFT 窗的
         固有不对称），逐 onset 修会把个别 onset 拉到隔壁音上（密排时窗里
         不止一个音）；常量修正天生免疫。
      ② **只信清晰强起音**（`top_frac`：按窗口 RMS 峰值取最强的前 30%）。
         实测（五月雪版 FallenEra.ogg，2101 个观测）：
             全部观测        中位 +32.8  IQR 28.5  ← 弱起音把分布糊开
             峰值前 30%      中位 +39.8  IQR 18.1  ← 与真值对量到的 +39.3 吻合
         真值对上（doublepress_demo）两种取法都通过（IQR ~3ms），
         但真实混音里只有强起音能给出干净的 RMS 峰。
      ③ 再用 MAD 剔离群，防"少数峰值锁错音"污染中位数。

    ★ **不需要任何 MIDI**：输入只有音频波形 + 粗检出时刻。
      合成脉冲标定过（`tools/_ogg_impulse_calib.py`）：该偏置只取决于
      `n_fft/hop/sr`，与素材无关 —— 脉冲/噪声爆发/衰减正弦三种嗓音量到
      −38.3 / −38.1 / −38.0ms（MAD < 1.1ms）。

    返回 `(bias_ms, info)`；判据不过时 `bias_ms = 0.0`（不动，等于没这功能）。
    """
    info: dict = {"ok": False, "reason": "", "n": 0, "n_used": 0, "n_kept": 0,
                  "bias": 0.0, "iqr": 0.0, "top_frac": top_frac}
    t = np.asarray(t_ms, dtype=float)
    if t.size == 0 or sr <= 0:
        info["reason"] = "empty"
        return 0.0, info
    e = rms_envelope(y, sr, win_ms)
    if e.size == 0:
        info["reason"] = "no-envelope"
        return 0.0, info

    obs: list[tuple[float, float]] = []
    for t0 in t:
        lo = int((t0 - back_ms) / 1000.0 * sr)
        hi = int((t0 + fwd_ms) / 1000.0 * sr)
        if lo < 2 or hi >= e.size - 2:
            continue
        seg = e[lo:hi]
        i = int(np.argmax(seg))
        if i <= 1 or i >= len(seg) - 2:       # 峰值贴窗边 ⇒ 可能锁错音
            continue
        obs.append(((lo + i) / sr * 1000.0 - t0, float(seg[i])))
    info["n"] = len(obs)
    if len(obs) < min_n:
        info["reason"] = f"n={len(obs)}<{min_n}"
        return 0.0, info

    arr = np.asarray(obs, dtype=float)
    # ② 只信清晰强起音：按窗口峰值取前 top_frac
    if 0.0 < top_frac < 1.0 and len(arr) > min_n * 2:
        thr = float(np.percentile(arr[:, 1], 100.0 * (1.0 - top_frac)))
        sel = arr[arr[:, 1] >= thr]
        if len(sel) >= min_n:
            arr = sel
    info["n_used"] = int(arr.shape[0])
    a = arr[:, 0]

    med = float(np.median(a))
    mad = float(np.median(np.abs(a - med))) * 1.4826
    tol = max(3.0 * mad, 4.0)
    keep = a[np.abs(a - med) <= tol]
    info["n_kept"] = int(keep.size)
    if keep.size < min_n:
        info["reason"] = f"kept={keep.size}<{min_n}"
        return 0.0, info
    bias = float(np.median(keep))
    iqr = float(np.percentile(keep, 75) - np.percentile(keep, 25))
    info.update(bias=bias, iqr=iqr)
    if iqr > max_iqr_ms:
        info["reason"] = f"iqr={iqr:.1f}>{max_iqr_ms:g}"
        return 0.0, info
    if abs(bias) > max_abs_ms:
        info["reason"] = f"|bias|={abs(bias):.1f}>{max_abs_ms:g}"
        return 0.0, info
    info["ok"] = True
    return bias, info


def detect_onsets(y: np.ndarray, sr: int, *, hop: int = 64,
                  pct: float = 75.0, min_gap_ms: float = 40.0,
                  trim_silence_db: float = -45.0) -> np.ndarray:
    """全频单轨音头检测 → 毫秒数组（升序）。

    ★ 参数是实测定的（五月雪版 FallenEra.ogg，71.3s 那一段三连音十六分）：
      hop=64（帧长 2.9ms）、delta=0.015、min_gap=40ms。
      用 hop=128/delta=0.06 会把 55.6ms 的三连音头**整个漏掉**，只留下一串
      166.7ms 的八分脉冲 —— 那正是"MIDI 里没有 16 16 8 8"的错觉来源。

    `min_gap_ms` 掐同一音头的重复触发（不掐会有大量 ~11.6ms 的假峰值），
    但**必须小于 55.6ms**，否则真·三连音也会被误杀。

    做完谱请优先用 `load_as_midi(split=...)`：单轨等于全采，没法取舍。
    """
    import librosa
    y = np.asarray(y, dtype=np.float32)
    S = librosa.feature.melspectrogram(y=y, sr=sr, hop_length=hop,
                                       n_fft=2048, n_mels=128, fmax=8000)
    env = librosa.onset.onset_strength(
        S=librosa.power_to_db(S, ref=np.max), aggregate=np.median)
    t_ms, _ = _pick(env, sr, hop, pct=pct, min_gap_ms=min_gap_ms)
    if trim_silence_db < 0 and len(t_ms):
        rms = np.sqrt(np.convolve(y.astype(np.float64) ** 2,
                                  np.ones(hop) / hop, mode="same"))
        thr = (10.0 ** (trim_silence_db / 20.0)) * (rms.max() or 1.0)
        loud = np.nonzero(rms > thr)[0]
        if len(loud):
            lo, hi = loud[0] / sr * 1000.0, loud[-1] / sr * 1000.0
            t_ms = t_ms[(t_ms >= lo) & (t_ms <= hi)]
    return np.asarray(t_ms, dtype=float)


class ConversionCancelled(BaseException):
    """在 `progress` 回调里抛它来取消转换。

    继承 `BaseException` 是**故意的**：`_say` 只吞 `Exception`，
    所以取消信号不会被吞掉，能干净地穿出 `load_as_midi`。
    """


def _say(progress, frac: float, msg: str) -> None:
    """进度回调。签名 progress(0..1, 文案)，抛异常不影响转换。"""
    if progress is None:
        return
    try:
        progress(max(0.0, min(1.0, float(frac))), msg)
    except Exception:                                   # noqa: BLE001
        pass


def _finalize(t: np.ndarray, min_gap_ms: float = 0.0) -> np.ndarray:
    """去重 + （可选）最小间隔稀疏化。"""
    t = np.unique(np.round(np.asarray(t, dtype=float), 6))
    if min_gap_ms > 0 and len(t) > 1:
        keep = [0]
        for i in range(1, len(t)):
            if t[i] - t[keep[-1]] >= min_gap_ms:
                keep.append(i)
        t = t[keep]
    return t


# 吸附安全阀的默认值（见 core/gridfit.py 与 docs/17 §4 F1）
MIN_CONF = 0.30        # 网格集中度下限：低于它 ⇒ **宁可不吸**
MAX_MOVE_FRAC = 0.35   # 单次挪动上限（× 网格步长）；0.5 就是"永远挪"


def fit_onsets(t_ms: np.ndarray, bpm: float, *, grid: int = 6,
               tol_frac: float = 0.5, prefer_beat: float = 0.0,
               beat_tol_frac: float = 0.18,
               min_gap_ms: float = 0.0,
               refine: bool = True, min_conf: float = MIN_CONF,
               max_move_frac: float = MAX_MOVE_FRAC,
               lattice: tuple[float, float] | None = None,
               fit: "GF.GridFit | None" = None) -> np.ndarray:
    """把音头**拟合**到网格上 —— 允许丢一点精度换横平竖直。

    用户口径：音频转出来的音头不精确（码率/检测误差），而"为了手感画横平竖直"
    和"为了采音强留微小弧度"之间，绝大多数谱师选前者。除了碎核/速核/硬核，
    一般曲目不会有太多不规则。**丢一点精度是可容忍的。**

    四层：
      ⓪ **自洽网格拟合**（`core.gridfit`）：不再相信 librosa 估出来的周期，
         用 onset 自己把 `period` 精修到 `~1e-4` 相对精度、并解出相位 φ。
         ★ 这一步专治「靠后位置随机偏移」：估计周期的 0.15% 相对误差会让网格
           以 `ε·t` 漂移（64s 漂 96ms），漂移后吸附就把 onset 挪到**错格**上。
      ① `grid` 网格吸附（6 = 1/6 拍，保留三连音；4 = 16 分；2 = 8 分；1 = 拍）
      ② `prefer_beat` > 0 时，离整拍/半拍足够近的（`beat_tol_frac` 拍以内）
         直接拉到整拍位置 —— 这条专门消掉"微小弧度"
      ③ 合并落到同一格的音头，并按 `min_gap_ms` 再掐一次

    安全阀（F1）：拟合集中度 `R < min_conf` 时**整条轨不吸**，原样返回 ——
    不吸只是保留检波器的常量偏置，吸错则是随位置漂移的错误。
    单次挪动也不超过 `max_move_frac · period`。

    `lattice=(period, phase)`：多条轨共用同一个已拟合好的网格（**多轨必须共用**，
    否则各轨相位不同、互相错位）。`fit`：把拟合结果塞进这个 list 供诊断。
    """
    t = np.asarray(t_ms, dtype=float)
    if len(t) == 0 or bpm <= 0:
        return _finalize(t, min_gap_ms)

    if lattice is not None:
        period, phi = float(lattice[0]), float(lattice[1])
    else:
        hint = GF.period_of(bpm, grid)
        g = GF.fit_grid(t, hint, refine=refine, min_conf=min_conf)
        if fit is not None:
            fit.append(g)
        if not g.ok:
            return _finalize(t, min_gap_ms)
        period, phi = g.period, g.phase

    t = GF.snap_lattice(t, period, phi, tol_frac=tol_frac,
                        max_move_frac=max_move_frac)

    if prefer_beat > 0.0 and len(t):
        # 每个 prefer_beat 拍一个高优先级锚点（与网格共用相位 φ）
        bstep = period * max(1, int(grid)) * prefer_beat
        target = np.rint((t - phi) / bstep) * bstep + phi
        close = np.abs(target - t) <= beat_tol_frac * bstep
        t = np.where(close, target, t)

    return _finalize(t, min_gap_ms)



def _mel_band(n_mels: int, fmax: float, lo_hz: float, hi_hz: float):
    import librosa
    freqs = librosa.mel_frequencies(n_mels=n_mels, fmax=fmax)
    return (int(np.searchsorted(freqs, lo_hz)),
            max(1, int(np.searchsorted(freqs, hi_hz))))


def power_db(S: np.ndarray, ref: float) -> np.ndarray:
    """功率谱 → dB，**共用同一个 ref**（尺度一致才能跨频段比较/共用阈值）。"""
    return 10.0 * np.log10(np.maximum(S, 1e-10) / max(1e-10, ref))


def load_as_midi(path: str, *, sr: int = 22050, hop: int = 64,
                 bpm: float | None = None, grid: int = 6,
                 snap: bool = True, min_gap_ms: float = 40.0,
                 pct: float = 75.0,
                 split: str = "hp+bands", n_mels: int = 128,
                 fmax: float = 8000.0,
                 trim_silence_db: float = -45.0,
                 prefer_beat: float = 0.5, beat_tol_frac: float = 0.18,
                 fit: str | None = None,
                 refine: bool = True, min_conf: float = MIN_CONF,
                 max_move_frac: float = MAX_MOVE_FRAC,
                 debias: bool = True,
                 progress=None) -> MidiFile:
    """OGG/WAV → MidiFile（与 `core.midi.load` 同构，可直接喂给整条管线）。

    `split`：
        "none"      单条全采轨（**不推荐**，等于没取舍）
        "hp"        打击 / 调性 两条
        "bands"     调性按频段切 3 条
        "hp+bands"  打击 + 3 条频段（默认）

    `progress`：`progress(0..1, 文案)`，驱动 GUI 进度条（全程约 30~60s）。

    精度相关的三个开关（详见 docs/17）：
      `debias=True`    用短窗 RMS 标定**检波器常量偏置**并整曲减掉。
                       线上实测：真值对上把 −35.5ms 压到 +2.8ms（|max| 3.3ms）
      `refine=True`    用 onset 自己**精修网格周期 + 相位**（治靠后漂移）
      `min_conf`       网格集中度门槛；不过则**不吸附**（安全阀，宁可不吸）
    """
    import librosa

    if fit is not None:
        fp = FIT_PRESETS.get(fit)
        if fp is None:
            raise ValueError(f"未知拟合档 {fit!r}，可选 {list(FIT_PRESETS)}")
        grid = fp["grid"]
        prefer_beat = fp["prefer_beat"]

    _say(progress, 0.02, "解码音频…")
    y, sr = _load_mono(path, sr)
    dur_ms = len(y) / sr * 1000.0
    if bpm is None:
        _say(progress, 0.12, "估计 BPM…")
        bpm = estimate_bpm((y, sr), sr, hop=hop)
    pkw = _peak_params(pct, min_gap_ms, hop, sr)

    _say(progress, 0.25, "计算频谱…")

    S = librosa.feature.melspectrogram(y=y, sr=sr, hop_length=hop,
                                       n_fft=2048, n_mels=n_mels, fmax=fmax)
    ymax = float(np.abs(y).max() or 1.0)
    # 全局静音窗（所有轨共用，保证时间基准一致）
    rms = np.sqrt(np.convolve(y.astype(np.float64) ** 2,
                              np.ones(hop) / hop, mode="same"))
    thr = (10.0 ** (trim_silence_db / 20.0)) * (rms.max() or 1.0)
    loud = np.nonzero(rms > thr)[0]
    t_lo, t_hi = ((loud[0] / sr * 1000.0, loud[-1] / sr * 1000.0)
                  if len(loud) else (0.0, dur_ms))

    # (轨名, 谱, 通道)  —— 通道 9 = 打击乐，GUI 的 is_drum_only() 会默认不勾
    # ★ 全部分支共用**同一个 dB 参考**（整首混音的最大值），
    #   否则每个频段各自归一化会把弱频段抬成满量程 → 疯狂过采。
    ref = float(S.max()) or 1.0
    specs: list[tuple[str, np.ndarray, int]] = []
    if split == "none":
        specs.append(("音头·全采（不推荐）", power_db(S, ref), 0))
    else:
        _say(progress, 0.40, "分离打击 / 旋律…")
        H, P = librosa.decompose.hpss(S)
        Hdb, Pdb = power_db(H, ref), power_db(P, ref)
        if split in ("hp", "hp+bands"):
            specs.append(("音头·打击", Pdb, 9))
        if split == "hp":
            specs.append(("音头·调性", Hdb, 0))
        else:
            for name, lo_hz, hi_hz in BANDS:
                a, b = _mel_band(n_mels, fmax, lo_hz, hi_hz)
                specs.append((f"音头·旋律·{name}", Hdb[a:b], 0))

    tracks: list[Track] = []
    n_sp = max(1, len(specs))

    # ---------------------------------------------------------------- 第一遍
    # 只做检测，**先不吸附**：因为网格必须**全局拟合一次**（见下），
    # 而且各轨必须共用同一个 (period, φ)，否则各轨相位不同会互相错位。
    raw_t: list[np.ndarray] = []
    raw_s: list[np.ndarray] = []
    for ti, (name, sub, chan) in enumerate(specs):
        _say(progress, 0.45 + 0.4 * ti / n_sp, f"检测音头：{name}…")
        if sub.size == 0 or sub.shape[0] == 0:
            raw_t.append(np.zeros(0))
            raw_s.append(np.zeros(0))
            continue
        env = librosa.onset.onset_strength(S=sub, aggregate=np.median)
        t_ms, st = _pick(env, sr, hop, **pkw)
        # 静音窗先滤（t_ms/st 必须同步裁剪）
        if len(t_ms):
            m = (t_ms >= t_lo) & (t_ms <= t_hi)
            t_ms, st = t_ms[m], st[m]
        raw_t.append(t_ms)
        raw_s.append(st)

    # 两个池子，用途不同：
    #   `all_pool` → **标定检波偏置**：要用上所有嗓门，打击轨的 RMS 峰最干净
    #                （实测：只用旋律轨 IQR 23.4ms 不过门槛，加上打击轨 18.1ms 过）
    #   `pool`     → **网格拟合**：优先调性轨（实际会被采的那条音乐线）
    all_pool = (np.concatenate([a for a in raw_t if len(a)])
                if any(len(a) for a in raw_t) else np.zeros(0))
    mel = [raw_t[i] for i, sp in enumerate(specs) if sp[2] != 9 and len(raw_t[i])]
    pool = np.concatenate(mel) if mel else all_pool

    # ------------------------------------------- 检波偏置自标定（常量，全局一次）
    # ★ 偏置来自 STFT 窗的不对称（见 estimate_detector_bias）：**系统性**
    #   却会整曲平移谱面。先减掉它，后面的网格相位才等于"音乐自己的相位"。
    bias_ms, bias_info = 0.0, {"ok": False, "reason": "off"}
    if debias and len(all_pool) >= 8:
        _say(progress, 0.84, "标定检波偏置…")
        bias_ms, bias_info = estimate_detector_bias(y, sr, all_pool)
        if bias_ms:
            # 常量平移：所有轨一起挪（相对关系不变）
            raw_t = [a + bias_ms for a in raw_t]
            all_pool = all_pool + bias_ms
            pool = pool + bias_ms

    # ------------------------------------------------- 全局网格拟合（一次性）
    fits: list[GF.GridFit] = []
    lattice: tuple[float, float] | None = None
    bpm_used = float(bpm)
    if snap and len(pool):
        _say(progress, 0.90, "自洽网格拟合…")
        g = GF.fit_grid(pool, GF.period_of(bpm, grid),
                        refine=refine, min_conf=min_conf)
        fits.append(g)
        if g.ok:
            lattice = (g.period, g.phase)
            # ★ 用精修后的周期反推 BPM 写进 tempo map：
            #   这样 `tick = t·bpm·PPQN/60000` 落在干净的 `PPQN/grid` 格上
            #   （网格自带相位也不影响 Δtick，见 gridfit.snap_lattice）
            bpm_used = g.bpm_for(grid)

    # ---------------------------------------------------------------- 第二遍
    for ti, (name, sub, chan) in enumerate(specs):
        t_ms, st = raw_t[ti], raw_s[ti]
        if snap and len(t_ms) and lattice is not None:
            # ★ fit_onsets 会**合并同格音头**，返回的数组比输入短。
            #   所以强度必须按「最近的原始峰」回映射，不能直接沿用下标
            #   （否则 boolean index 长度对不上，直接 IndexError）。
            src_t, src_s = t_ms.copy(), st.copy()
            t_ms = fit_onsets(t_ms, bpm, grid=grid, prefer_beat=prefer_beat,
                              beat_tol_frac=beat_tol_frac, lattice=lattice,
                              max_move_frac=max_move_frac)
            if len(t_ms):
                idx = np.clip(np.searchsorted(src_t, t_ms), 0, len(src_t) - 1)
                st = src_s[idx]
            else:
                st = np.zeros(0)
        elif snap:
            # 安全阀触发 / 无数据：原样保留（不吸）—— 不会比"不吸"更差
            t_ms = _finalize(t_ms)
        # 强度 → velocity，让 GUI 的「最小力度」滑块真的有用
        if len(st) > 1:
            lo_s, hi_s = float(np.percentile(st, 5)), float(st.max())
            rng = max(1e-9, hi_s - lo_s)
            vel = np.clip(30 + 97 * (st - lo_s) / rng, 1, 127).astype(int)
        else:
            vel = np.full(len(t_ms), 100, dtype=int)
        notes = []
        for t, v in zip(t_ms, vel):
            tick = int(round(t / 60000.0 * bpm_used * PPQN))
            notes.append(Note(track=ti, channel=chan, pitch=60,
                              velocity=int(v), t_on_ms=float(t),
                              t_off_ms=float(t) + 10.0,
                              t_on_tick=tick, t_off_tick=tick + 1))
        tracks.append(Track(index=ti, name=name, notes=notes))

    mf = MidiFile(path=path, format=1, ppqn=PPQN)
    mf.tracks = tracks
    mf.tempo_map = [(0, float(bpm_used))]
    mf.time_sig = [(0, 4, 4)]
    mf.length_ms = dur_ms
    mf.audio_peak = ymax          # 方便以后做归一化
    mf.grid_fit = fits[-1] if fits else None      # 诊断用（tools/diag_ogg_offset）
    mf.detector_bias = float(bias_ms)             # 已从所有 onset 上减掉的常量
    mf.bias_info = bias_info
    mf.grid_fit = fits[-1] if fits else None      # 诊断用（tools/diag_ogg_offset）
    _say(progress, 1.0, "完成")
    return mf


def to_smf(mf: MidiFile, out_path: str, track_index: int = 0) -> str:
    """把 (同构的) MidiFile 写成标准 type-0 MIDI 文件。

    这样"OGG→MIDI"也能当成独立工具用：拿到 .mid 就能给别的软件/别的流程。
    """
    def vlq(n: int) -> bytes:
        b = [n & 0x7F]
        n >>= 7
        while n:
            b.append((n & 0x7F) | 0x80)
            n >>= 7
        return bytes(reversed(b))

    notes = sorted(mf.tracks[track_index].notes,
                   key=lambda n: n.t_on_tick) if mf.tracks else []
    us_per_beat = int(round(60_000_000.0 / max(1e-9, mf.bpm0)))

    ev: list[tuple[int, int, bytes]] = []
    ev.append((0, 0, b"\xFF\x51\x03" + us_per_beat.to_bytes(3, "big")))
    for n in notes:
        vel = max(1, min(127, int(n.velocity)))
        ev.append((int(n.t_on_tick), 2, bytes([0x90, n.pitch & 0x7F, vel])))
        ev.append((int(n.t_off_tick), 1, bytes([0x80, n.pitch & 0x7F, 0])))
    ev.append((int(notes[-1].t_off_tick) if notes else 0, 3, b"\xFF\x2F\x00"))
    ev.sort(key=lambda e: (e[0], e[1]))

    out = bytearray()
    prev = 0
    for tick, _pri, payload in ev:
        out += vlq(max(0, tick - prev)) + payload
        prev = tick

    with open(out_path, "wb") as fh:
        fh.write(b"MThd" + (6).to_bytes(4, "big") + (0).to_bytes(2, "big")
                 + (1).to_bytes(2, "big") + mf.ppqn.to_bytes(2, "big"))
        fh.write(b"MTrk" + len(out).to_bytes(4, "big") + bytes(out))
    return out_path


def describe(mf: MidiFile) -> str:
    tot = sum(len(t.notes) for t in mf.tracks)
    lines = [f"OGG 音头：{len(mf.tracks)} 轨 / 共 {tot} 个音头"
             f"  bpm≈{mf.bpm0:g}  时长={mf.length_ms/1000:.1f}s"]
    gf = getattr(mf, "grid_fit", None)
    if gf is not None:
        lines.append(f"    网格拟合：{gf.describe()}"
                     + ("" if gf.ok else "   ← 不吸附（安全阀）"))
    bi = getattr(mf, "bias_info", None)
    if bi is not None:
        b = getattr(mf, "detector_bias", 0.0)
        lines.append(f"    检波偏置：{b:+.2f}ms（{'已校正' if b else '未校正'} "
                     f"n={bi.get('n', 0)}→{bi.get('n_kept', 0)}"
                     + (f" {bi.get('reason')}" if bi.get("reason") else "") + "）")
    for t in mf.tracks:
        lines.append(f"    trk{t.index} {t.name:<18} {len(t.notes):>5} 个"
                     + ("   ← 默认不勾（打击）" if t.is_drum_only() else ""))
    return "\n".join(lines)


__all__ = ["load_as_midi", "detect_onsets", "estimate_bpm", "snap_to_grid",
           "fit_onsets", "to_smf", "describe", "PPQN", "BANDS",
           "FIT_PRESETS", "ConversionCancelled", "MIN_CONF", "MAX_MOVE_FRAC"]

# 旧名字留着，等价于 fit_onsets（prefer_beat=0）
snap_to_grid = fit_onsets
