"""
extract_vfx.py — VFXNet 训练数据提取（重写版：自由 / 无限制）
====================================================================

设计哲学（与旧版的根本区别）
----------------------------
旧版：多标签每帧"能激活的视觉事件都激活" -> 模型学会"见块就放一堆" ->
      推理时铺满、杂乱、像一坨垃圾。这不是参数问题，是标签与辅助通道设计错了。

新版三条铁律：
  1) 标签自由：不再做每帧稀疏截断，所有激活事件都进标签（MAX_EVENTS_PER_FRAME=None）。
     用户要求"爱放几个放几个"，模型可学任意组合（单发 / 叠加 / 连放）。
  2) aux 只在「真正的重音」处给强提示：aux[0] 的高斯包络只打在玩法 Twirl
     翻身（人类标注的强点/高潮）上，普通方块不给提示。模型不会"见块就放"，
     只在强拍/翻新/高潮处考虑特效。
  3) SetFilter / SetFilterAdvanced 进标签：滤镜名映射到受控白名单索引存进 filt，
     强度进 params。模型学会"哪段该放什么滤镜、放多浓"，推理端按预测索引选真实
     滤镜名（绝不自创，游戏必识别）。MultiPlanet 已从词表删除，不再处理。

输出目标（与旧版同名文件，可直接被 train_vfx.py 读取）：
    multi   (V, T)  每帧视觉事件多标签（已稀疏截断，每帧最多 2 个正）
    params  (V, 3, T) 每类事件关键参数（duration/强度/旋转）
    filt    (V, T)  int16，SetFilter 滤镜类别（-1=无，否则为受控白名单索引）
    aux     (4, T)  方块节奏辅助：
        aux[0] Twirl 重音包络（仅翻身处）   aux[1] 局部间隔(秒)
        aux[2] 局部转角幅度(/180)            aux[3] 歌曲相对位置(0~1)

用法：
  python extract_vfx.py --data D:/ADOFAI_AI_Mug/vision/best --out data/vfx_cache
  （只产出目标/辅助缓存 + 统计；梅尔在训练 Dataset 里现算）
"""
from __future__ import annotations
import os, sys, json, argparse, glob
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "training"))

import numpy as np
import librosa

from adofai_parse import load_adofai, _to_angle_data
from chart_repr import SR, HOP, HOP_MS
from timing_engine import compute_note_times

# 视觉特效词表 / 事件下标 / 参数维度 —— 统一由 effects_schema 管理（转录自
# ADOFAI-JS 官方字段定义，避免字段名漂移）。extract_params 也来自该模块。
from effects_schema import (VFX_EVENTS, EVENT_INDEX, P, extract_params,
                             filter_index_of, easing_index_of, EASING, FILTER_TYPES)
V = len(VFX_EVENTS)

# 每帧保留几个正事件：None = 不做稀疏截断，所有激活事件都进标签（用户要求自由发挥）。
MAX_EVENTS_PER_FRAME = None

# 归一化上限（用于把原始物理量压到 ~[0,1] 或 [-1,1]）
_DUR_CAP = 2000.0      # ms
_ZOOM_CAP = 2.0        # MoveCamera/MoveTrack 相对 zoom
_ROT_CAP = 180.0
_POS_CAP = 540.0       # 像素位移
_RAD_CAP = 300.0
_MARGIN_CAP = 300.0
_PLANET_CAP = 40.0
_RELSCALE_CAP = 3.0
_INTENS_CAP = 2.0


def _clip(x, lo, hi):
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return 0.0
    return max(lo, min(hi, xf))


def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _flag_enabled(v):
    """ADOFAI 开关字段可能是 bool 或字符串 'Enabled'/'Disabled'，统一转 bool。

    对 SetFilter.disableOthers：'Enabled' 表示「启用『禁用其他滤镜』」=True；
    'Disabled' 表示不禁用=False。旧代码用 bool() 直接转，bool('Disabled')=True
    （非空字符串恒真）会把人类关闭的滤镜错标成「要禁用」，导致滤镜堆叠成灰泥。"""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() == "enabled"
    return bool(v)


# ADOFAI MultiPlanet.planets 是字符串枚举 -> 整数行星数
_PLANET_ENUM = {
    "TwoPlanets": 2, "ThreePlanets": 3, "FourPlanets": 4, "FivePlanets": 5,
    "SixPlanets": 6, "SevenPlanets": 7, "EightPlanets": 8,
}


# ⚠️ extract_params 已迁至 effects_schema.extract_params（转录 ADOFAI-JS 官方
# 字段：MoveCamera 的 position/rotation/zoom、Flash 的 opacity、ShakeScreen 的
# intensity 等），本模块不再维护一份，避免字段名与游戏不一致。


def _bump(center, T, sigma=1.5):
    lo = max(0, int(round(center - 3.0 * sigma)))
    hi = min(T, int(round(center + 3.0 * sigma)) + 1)
    d = np.arange(lo, hi, dtype=np.float32) - center
    return lo, hi, np.exp(-(d ** 2) / (2.0 * sigma * sigma))


def _sparsify(multi, maxk):
    """每帧最多保留信号最强的 maxk 个事件（其余清零）。就地修改 multi (V,T)。"""
    if maxk is None or maxk >= V:
        return
    for t in range(multi.shape[1]):
        col = multi[:, t]
        # 取绝对值最大的 maxk 个下标（软标签恒非负，直接取最大值即可）
        if (col > 0.01).sum() <= maxk:
            continue
        idx = np.argsort(col)[-maxk:]
        keep = np.zeros(V, dtype=bool)
        keep[idx] = True
        multi[:, t] = np.where(keep, col, 0.0)


def build_frame_targets(level, T, filter_index):
    """level(dict) + 帧数 T + filter_index(name->int) -> (multi, params, filt, aux)。

    multi  (V, T)  float32  每帧事件高斯软标签（不做稀疏截断，所有激活事件都保留）
    params (V, 3, T) float32
    filt   (V, T)  int16    SetFilter 滤镜类别（-1=无，否则为白名单索引）
    ease   (V, T)  int16    每事件每帧缓动索引（默认 0=Linear）
    dis    (V, T)  int8     SetFilter 的 disableOthers（0/1，仅 SetFilter 类有效）
    aux    (4, T)  float32  方块节奏辅助通道
    """
    ad = _to_angle_data(level) or []
    settings = level.get("settings") or {}
    if not isinstance(settings, dict):
        settings = {}
    actions = level.get("actions") or []
    if not isinstance(actions, list):
        actions = []

    multi = np.zeros((V, T), np.float32)
    params = np.zeros((V, P, T), np.float32)
    filt = np.full((V, T), -1, np.int16)
    ease = np.zeros((V, T), np.int16)            # 每事件每帧缓动索引（默认 0=Linear）
    dis = np.zeros((V, T), np.int8)             # SetFilter 的 disableOthers 标签
    if not ad:
        aux = _empty_aux(T)
        return multi, params, filt, ease, dis, aux

    try:
        nt = compute_note_times(ad, settings, actions, add_offset=True)
    except Exception:
        return multi, params, filt, ease, dis, _empty_aux(T)
    if not nt:
        return multi, params, filt, ease, dis, _empty_aux(T)
    times = [float(x[0]) if isinstance(x, (tuple, list)) else float(x) for x in nt]
    n = len(times)                      # 计时引擎吐多少个 tile 时间（可能略多于 angleData）
    ad = (ad + [0] * max(0, n - len(ad)))[:n]   # 夹紧，避免下标越界

    tile_frames = [max(0, min(T - 1, int(round(t / HOP_MS)))) for t in times]

    # 收集 Twirl 翻身所在的下标（真正的重音）
    twirl_floors = set()
    for a in actions:
        if isinstance(a, dict) and a.get("eventType") == "Twirl":
            try:
                twirl_floors.add(int(round(float(a.get("floor")))))
            except Exception:
                pass

    # 辅助通道：
    #   aux[0] 只在 Twirl 重音处给高斯包络（普通 tile 不给 -> 模型不会见块就放）
    #   aux[1] 局部间隔（节奏上下文，所有 tile 广播）
    #   aux[2] 局部转角幅度（节奏上下文，所有 tile）
    #   aux[3] 歌曲相对位置（0~1）
    aux = np.zeros((4, T), np.float32)
    for i, tf in enumerate(tile_frames):
        if i in twirl_floors:
            lo, hi, w = _bump(tf, T)
            aux[0, lo:hi] = np.maximum(aux[0, lo:hi], w)
        ang = abs(ad[i]) if abs(ad[i] - 999) > 1.0 else 0.0
        aux[2, tf] = _clip(ang / 180.0, 0, 1)
    for f in range(T):
        seg = 0
        for i in range(n - 1):
            if tile_frames[i] <= f < tile_frames[i + 1]:
                seg = i
                break
        else:
            seg = n - 2 if n >= 2 else 0
        seg = max(0, min(n - 2, seg))
        inter = (times[seg + 1] - times[seg]) / 1000.0 if n >= 2 else 0.5
        aux[1, f] = _clip(inter, 0.05, 4.0)
    aux[3, :] = np.linspace(0.0, 1.0, T, dtype=np.float32)

    # 视觉事件 -> 帧对齐（多标签 + 参数 + 滤镜 + 缓动）。SetFilter 类也进标签（滤镜名
    # 从受控白名单索引、强度进 params），不再跳过 -> 模型学会"哪段该放什么滤镜"。
    for a in actions:
        if not isinstance(a, dict):
            continue
        et = a.get("eventType")
        if et not in EVENT_INDEX:
            continue
        # 用户要求（2026-08-14）：训练端禁用位置轨道 PositionTrack（难看、且会改判定
        # 轨道视觉位置），模型不学它；推理端 apply_vfx 同样过滤。双重保险。
        if et == "PositionTrack":
            continue
        fl = a.get("floor")
        if fl is None:
            continue
        try:
            fi = int(round(float(fl)))
        except Exception:
            continue
        if not (0 <= fi < len(times)):
            continue
        tf = tile_frames[fi]
        ei = EVENT_INDEX[et]
        lo, hi, w = _bump(tf, T)
        # 缓动：所有事件都记录（默认 Linear=0），训练端学、推理端用模型预测覆盖。
        ease[ei, lo:hi] = easing_index_of(a.get("ease", "Linear"))
        if et in ("SetFilter", "SetFilterAdvanced"):
            # 事件存在性同样进 multi（否则 event_head 学不到"这格有滤镜"，推理不会生成）
            multi[ei, lo:hi] = np.maximum(multi[ei, lo:hi], w)
            fn = a.get("filter")
            filt[ei, lo:hi] = filter_index_of(fn)     # 滤镜类索引（受控白名单）
            it = _num(a.get("intensity", 50.0), 50.0)
            params[ei, 0, lo:hi] = _clip((it - 50.0) / 50.0, -1, 1)  # 强度~[-1,1]
            dis[ei, lo:hi] = 1 if _flag_enabled(a.get("disableOthers")) else 0  # 模型学何时禁用其他滤镜
            continue
        multi[ei, lo:hi] = np.maximum(multi[ei, lo:hi], w)
        vec = extract_params(et, a)
        params[ei, :, lo:hi] = np.maximum(params[ei, :, lo:hi], vec[:, None])

    # 稀疏化：MAX_EVENTS_PER_FRAME=None 时 _sparsify 直接跳过（不做截断，所有激活事件保留）
    _sparsify(multi, MAX_EVENTS_PER_FRAME)

    return multi, params, filt, ease, dis, aux


def _empty_aux(T):
    aux = np.zeros((4, T), np.float32)
    aux[3, :] = np.linspace(0.0, 1.0, T, dtype=np.float32)
    return aux


def _pick_chart(folder):
    """优先 main.adofai -> level -> backup -> sub1.. -> 其他 adofai。返回路径或 None。"""
    pri_map = {"main.adofai": 0, "level.adofai": 1, "backup.adofai": 2}
    def pri(name):
        nl = name.lower()
        if nl in pri_map:
            return pri_map[nl]
        import re as _re
        m = _re.match(r"sub(\d+)\.adofai", nl)
        return 10 + int(m.group(1)) if m else 100
    best = (999, "")
    for f in os.listdir(folder):
        if f.lower().endswith(".adofai"):
            if pri(f) < best[0]:
                best = (pri(f), os.path.join(folder, f))
    return best[1] or None


def _audio_of(folder):
    for ext in ("*.ogg", "*.mp3", "*.wav"):
        fs = sorted(glob.glob(os.path.join(folder, ext)))
        if fs:
            return fs[0]
    return None


def collect_filter_names(best_dir, top_n=24):
    """统计 best 谱面里 SetFilter 滤镜名频次，返回 (top_names, filter_index)。

    注意：本版训练不依赖滤镜分类（标签里滤镜类恒为 0），此函数保留仅为
    兼容旧接口/统计，训练脚本不会真正用 filt 头。
    """
    from collections import Counter
    cnt = Counter()
    for d in sorted(glob.glob(os.path.join(best_dir, "*"))):
        if not os.path.isdir(d):
            continue
        adf = _pick_chart(d)
        if not adf:
            continue
        lvl = load_adofai(adf)
        if not isinstance(lvl, dict):
            continue
        for a in (lvl.get("actions") or []):
            if isinstance(a, dict) and a.get("eventType") in ("SetFilter", "SetFilterAdvanced"):
                fn = a.get("filter")
                if fn:
                    cnt[fn] += 1
    top = [n for n, _ in cnt.most_common(top_n)]
    return top, {n: i for i, n in enumerate(top)}


def build(best_dir, out_dir, limit=None):
    best_dir = str(best_dir)
    out_dir = str(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    filter_names, filter_index = collect_filter_names(best_dir, top_n=24)
    print(f"[vfx] 受控滤镜白名单 {len(FILTER_TYPES)} 种（训练学习，推理时模型预测选择）")

    folders = sorted(glob.glob(os.path.join(best_dir, "*")))
    folders = [d for d in folders if os.path.isdir(d)]
    if limit:
        folders = folders[:limit]

    manifest = []
    event_total = np.zeros(V, np.int64)
    song_event_hist = np.zeros(V, np.int64)
    for d in folders:
        adf = _pick_chart(d)
        aud = _audio_of(d)
        if not adf or not aud:
            continue
        lvl = load_adofai(adf)
        if not isinstance(lvl, dict):
            continue
        try:
            y, _ = librosa.load(aud, sr=SR, mono=True)
            T = (y.shape[0] + HOP - 1) // HOP  # 与 mel 帧数对齐（center 误差 ±1 帧，训练时 clip）
        except Exception as e:
            print(f"  [skip] {os.path.basename(d)} 音频加载失败: {e}")
            continue
        multi, params, filt, ease, dis, aux = build_frame_targets(lvl, T, filter_index)
        # 统计
        cnt = (multi > 0.5).sum(axis=1).astype(np.int64)  # 每类事件激活帧数（≈事件峰数）
        event_total += cnt
        present = (multi.max(axis=1) > 0.01)
        song_event_hist += present.astype(np.int64)
        name = os.path.basename(d)
        b = os.path.join(out_dir, name)
        np.save(b + ".multi.npy", multi)
        np.save(b + ".params.npy", params)
        np.save(b + ".filt.npy", filt)
        np.save(b + ".ease.npy", ease)
        np.save(b + ".dis.npy", dis)
        np.save(b + ".aux.npy", aux)
        manifest.append({
            "song": name, "adofai": os.path.basename(adf),
            "audio": os.path.basename(aud), "T": int(T),
            "n_tiles": int(len(_to_angle_data(lvl) or [])),
            "bpm": float((lvl.get("settings") or {}).get("bpm", 120.0) or 120.0),
            "event_frames": {VFX_EVENTS[i]: int(cnt[i]) for i in range(V)},
        })
        print(f"  [ok] {name:32} T={T:6d} tiles={manifest[-1]['n_tiles']:4d}")

    meta = {
        "events": VFX_EVENTS,
        "n_events": V,
        "param_dim": P,
        "max_events_per_frame": MAX_EVENTS_PER_FRAME,
        "filters": FILTER_TYPES,
        "n_filters": len(FILTER_TYPES),
        "easings": EASING,
        "n_easings": len(EASING),
        "hop_ms": HOP_MS,
        "sr": SR,
        "n_songs": len(manifest),
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "songs": manifest}, f, ensure_ascii=False, indent=1)
    # 统计报告
    print("\n================ VFX 数据提取统计（稀疏版） ================")
    print(f"歌曲数: {len(manifest)}  帧级网格 hop={HOP_MS:.3f}ms  每帧保留正事件={'无上限' if MAX_EVENTS_PER_FRAME is None else MAX_EVENTS_PER_FRAME}")
    print(f"{'事件':20}{'出现谱面数':>10}{'总激活帧':>12}")
    for i in range(V):
        print(f"{VFX_EVENTS[i]:20}{int(song_event_hist[i]):>10}{int(event_total[i]):>12}")
    print(f"\n滤镜种类: {len(FILTER_TYPES)}（白名单，模型已学习分类）  缓动种类: {len(EASING)}（含 Linear 默认）")
    return manifest, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=r"D:/ADOFAI_AI_Mug/vision/best")
    ap.add_argument("--out", default=r"D:/ADOFAI_AI_Mug/new_last_128/portable/data/vfx_cache")
    ap.add_argument("--limit", type=int, default=0, help="仅处理前 N 个（调试用）")
    a = ap.parse_args()
    build(a.data, a.out, limit=a.limit or None)


if __name__ == "__main__":
    main()
