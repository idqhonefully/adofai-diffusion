"""
apply_vfx.py — 把 VFXNet 的帧级视觉特效预测，吸附到生成谱面的方块上，
注入为 ADOFAI 顶层 actions（与 Twirl 同格式：{"floor": <0-based tile>, "eventType": ...}）。

 设计（用户拍板，2026-08-11：完全自由发挥）
 ------------------------------------
   - 模型对每一时刻预测 20 类特效的激活概率 ev[e,fi]。
   - 特效吸附到「重音格」：玩法 Twirl 翻身 + 模型本帧峰值极高的强拍。
     （仅用于把特效对齐到方块，不算数量限制。）
   - 每个重音格注入「所有超过存在性下限 ABS_THR 的事件」—— 爱放几个放几个，
     不做 top-K 精选、不堆满上限、不限制总数。模型自由发挥。
   - 特效幅度随该格音乐强度(模型峰值)缩放：强拍更狠、弱拍更轻。
   - 已开放全部视觉事件（含 MoveTrack/AnimateTrack/PositionTrack/SetFilter 等），
     filterType 仅从受控白名单选，绝不自创。仅 MultiPlanet 不注入（结构事件）。
   - aux 构造与训练端 extract_vfx 完全一致：aux[0] 只在 Twirl 重音处给包络，
     aux[1] 局部间隔、aux[2] 转角、aux[3] 歌曲位置。

用法：
    from apply_vfx import apply_vfx
    level2 = apply_vfx(level, audio_path, intensity=0.5, device="cuda")
"""
from __future__ import annotations
import os

import numpy as np
import torch

from extract_vfx import VFX_EVENTS
from effects_schema import build_action, FILTER_TYPES
from chart_repr import compute_note_times, HOP_MS
from vfx_net import VFXNet, predict_vfx
from demucs_mel import demucs_mel
try:
    from device_util import get_safe_device
except Exception:
    def get_safe_device():
        return "cuda" if torch.cuda.is_available() else "cpu"

N_FILTERS = len(FILTER_TYPES)  # 与训练端 effects_schema / train_vfx 同步（当前 120 种真实滤镜名）
# apply_vfx.py 位于 <portable>/app/training/apply_vfx.py；退 3 层到 <portable> 根
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CKPT = os.path.join(_ROOT, "data", "checkpoints", "vfx_net.pt")

# 每帧绝对下限：模型对该事件把握低于此值就不注入（不塞入完全不信的特效）
ABS_THR = 0.30
# SetFilter/SetFilterAdvanced 专用更低的出现门槛：训练集外歌曲上模型把滤镜几乎只绑在
# 「重音/翻身」帧，非重音帧想加滤镜的概率常<0.30 而被丢弃，导致很多歌一个滤镜都没有
# （用户反馈「新歌没滤镜」）。放低到 0.15，让「有点想加」的帧也能出滤镜（提高出现率，
# 非人为幅度上限，符合自由发挥原则）。
SETFILTER_THR = 0.30  # 2026-08-15 用户反馈滤镜添加过频，由 0.15 提至 0.30 减弱次数（仍低于 ABS_THR，保证有歌可出滤镜）
# 非 Twirl 帧要成为重音，模型峰值必须达到此值（适中：让更多强拍能注入特效）
ACCENT_BAR = 0.55
# 滤镜种类采样温度：训练数据滤镜种类极不均衡（Fisheye/Grayscale 占 35%），
# filt 头 argmax 会垄断成 Grayscale。用温度 softmax + top-k 采样打散，让种类多样。
FILT_TEMP = 2.2
FILT_TOPK = 8
# 灰阶去重偏好（用户反馈"滤镜全是灰阶、没有别的"，2026-08-14）：
# 训练数据 Grayscale 等去色类占比高，仅温度采样仍偏它。在 filt 头 logit 上给纯灰阶减分，
# 强制彩色/鲜艳滤镜（Neon/Fisheye/Glitch/VHS…）更易出现。只改"选哪种滤镜"的偏好，
# 不动任何幅度/位置/数量（守 VFX 自由发挥铁律）。
_GRAY_NAMES = ["Grayscale", "CameraFilterPack_Color_GrayScale"]
_GRAY_IDXS = []
for _gn in _GRAY_NAMES:
    try:
        _GRAY_IDXS.append(FILTER_TYPES.index(_gn))
    except Exception:
        pass
GRAYSCALE_BIAS = 2.0

# —— 闪光(Flash)频率抑制（用户反馈生成的 Flash 太密、快闪刺眼，2026-08-14）——
# 只控制时间密度，绝不改 opacity/duration/plane（遵守视觉自由发挥铁律）。
# FLASH_MODE:
#   "off"    —— 完全不注入 Flash（彻底止住快闪）
#   "sparse" —— 默认：相邻 Flash 至少间隔 FLASH_GAP_SEC 秒，且整首不超过 FLASH_MAX_COUNT 个
FLASH_MODE = "sparse"
FLASH_GAP_SEC = 1.5
FLASH_MAX_COUNT = 24

# —— 绽放(Bloom)阈值下压（用户反馈生成的 Bloom 阈值太高、泛光只在最亮处出现、效果偏弱，2026-08-14）——
# 只调整 Bloom 的 threshold 字段（下压到更明显的区间），绝不改 intensity/color（遵守视觉自由发挥铁律）。
# 与 Flash 降频同层：属于「注入后的后处理干预」，不碰 effects_schema.build_action 的幅度映射。
# 模型生成阈值多在 15~35（cap=120 线性映射结果），×BLOOM_TH_SCALE 落到约 5~12，泛光明显且不糊屏。
BLOOM_TH_SCALE = 0.35
# 绽放(Bloom)强度下压（用户反馈生成的 Bloom 强度偏高、整体偏过曝，2026-08-14）：
# 仅对 Bloom 的 intensity 字段做乘性下压，不动 threshold/color（守 VFX 自由发挥铁律）。
# 与阈值下压同层：注入后的后处理干预，不碰 effects_schema.build_action 的幅度映射。
# 模型生成强度多在 40~170（cap=5000 有符号逆变换结果），×BLOOM_INT_SCALE 落到约 6~26，泛光更柔和。
# 用户反馈 0.5 仍偏高（2026-08-14 20:07）由 0.5 降至 0.3；现再反馈"还是有点高"（2026-08-15）降至 0.15。
BLOOM_INT_SCALE = 0.15


def _bump(center, T, sigma=1.5):
    lo = max(0, int(round(center - 3.0 * sigma)))
    hi = min(T, int(round(center + 3.0 * sigma)) + 1)
    d = np.arange(lo, hi, dtype=np.float32) - center
    return lo, hi, np.exp(-(d ** 2) / (2.0 * sigma * sigma))


def _clip(x, lo, hi):
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, xf))


def _build_aux(level, T):
    """构造 4 通道节奏辅助 (4,T)，与训练时 extract_vfx 的 aux 格式严格对齐。

    aux[0] Twirl 重音高斯包络（仅翻身处）  aux[1] 局部间隔(秒)
    aux[2] 局部转角幅度(/180)              aux[3] 歌曲相对位置(0~1)
    """
    aux = np.zeros((4, T), np.float32)
    aux[3, :] = np.linspace(0.0, 1.0, T, dtype=np.float32)
    angle_data = level.get("angleData", []) or []
    actions = level.get("actions", []) or []
    try:
        nt = compute_note_times(angle_data, level.get("settings", {}) or {},
                                 actions, add_offset=True)
        times = [float(x[0]) if isinstance(x, (tuple, list)) else float(x) for x in nt]
    except Exception:
        return aux
    if not times:
        return aux
    n = len(times)
    frames = [max(0, min(T - 1, int(round(t / HOP_MS)))) for t in times]
    ad = (list(angle_data) + [0] * max(0, n - len(angle_data)))[:n]

    # Twirl 翻身下标（真正重音）
    twirl_floors = set()
    for a in actions:
        if isinstance(a, dict) and a.get("eventType") == "Twirl":
            try:
                twirl_floors.add(int(round(float(a.get("floor")))))
            except Exception:
                pass

    # aux[0] 只在 Twirl 重音处打高斯包络
    for i, f in enumerate(frames):
        if i in twirl_floors and 0 <= f < T:
            lo, hi, w = _bump(f, T)
            aux[0, lo:hi] = np.maximum(aux[0, lo:hi], w)
    # aux[2] 转角幅度（节奏上下文，所有 tile）
    for i, f in enumerate(frames):
        if 0 <= f < T:
            ang = abs(ad[i]) if abs(ad[i] - 999) > 1.0 else 0.0
            aux[2, f] = _clip(ang / 180.0, 0, 1)
    # aux[1] 局部间隔（按所在 segment 广播）
    for f in range(T):
        seg = 0
        for i in range(n - 1):
            if frames[i] <= f < frames[i + 1]:
                seg = i
                break
        else:
            seg = n - 2 if n >= 2 else 0
        seg = max(0, min(n - 2, seg))
        inter = (times[seg + 1] - times[seg]) / 1000.0 if n >= 2 else 0.5
        aux[1, f] = _clip(inter, 0.05, 4.0)
    return aux


def _make_action(name, pa_vec, intensity, mag=1.0, ease_idx=0, filt_idx=0, disable=False):
    """委托 effects_schema.build_action —— 字段名按 ADOFAI-JS 官方定义，滤镜名受控、
    缓动由模型预测索引决定。"""
    return build_action(name, pa_vec, intensity, mag, ease_idx=ease_idx, filt_idx=filt_idx, disable=disable)


def _sanitize(o):
    """递归把 NaN/Inf 换成 0，避免写出非法 JSON 导致游戏加载失败。"""
    if isinstance(o, dict):
        return {k: _sanitize(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_sanitize(v) for v in o]
    if isinstance(o, bool):
        return o
    if isinstance(o, (int, float)):
        try:
            f = float(o)
        except Exception:
            return o
        return 0.0 if not np.isfinite(f) else o
    return o


def _ftime_of(a, nt):
    """取某动作（含 floor）对应的方块时间（秒）。nt 为 0-based tile 时间列表。"""
    try:
        fl = int(a.get("floor", 1)) - 1
    except Exception:
        return 0.0
    if 0 <= fl < len(nt):
        t = nt[fl]
        if isinstance(t, (list, tuple)):
            try:
                return float(t[0])
            except Exception:
                return 0.0
        try:
            return float(t)
        except Exception:
            return 0.0
    return 0.0


def _filter_flash(actions, nt):
    """对所有 Flash 动作按时间降频 / 关闭。原地修改 actions。

    - FLASH_MODE=="off"    : 移除全部 Flash（彻底止住快闪）。
    - FLASH_MODE=="sparse" : 相邻保留的 Flash 至少间隔 FLASH_GAP_SEC 秒，
      且整首不超过 FLASH_MAX_COUNT 个；其余 Flash 丢弃。
    不改动任何保留 Flash 的 opacity/duration/plane（视觉自由发挥铁律）。
    """
    if FLASH_MODE == "off":
        keep = [a for a in actions if a.get("eventType") != "Flash"]
        removed = len(actions) - len(keep)
        actions[:] = keep
        if removed:
            print(f"[vfx] FLASH_MODE=off：已移除 {removed} 个 Flash（彻底关闭闪光）")
        return
    # sparse：按时间最小间隔 + 数量上限
    flash_items = [(i, a) for i, a in enumerate(actions) if a.get("eventType") == "Flash"]
    if not flash_items:
        return
    flash_items.sort(key=lambda x: _ftime_of(x[1], nt))   # 按时间升序
    last_keep = -1e9
    keep_idx = set()
    count = 0
    for i, a in flash_items:
        t = _ftime_of(a, nt)
        if (t - last_keep) >= FLASH_GAP_SEC and count < FLASH_MAX_COUNT:
            keep_idx.add(i)
            last_keep = t
            count += 1
    new_actions = [a for i, a in enumerate(actions)
                   if a.get("eventType") != "Flash" or i in keep_idx]
    removed = len(actions) - len(new_actions)
    actions[:] = new_actions
    if removed:
        print(f"[vfx] FLASH_MODE=sparse：Flash 降频，保留 {count} 个、移除 {removed} 个"
              f"（间隔≥{FLASH_GAP_SEC}s，上限{FLASH_MAX_COUNT}）")


def _lower_bloom_threshold(actions):
    """对全部 Bloom 动作的 threshold 做下压（仅改 threshold 字段，不动 intensity/color）。
    原地修改 actions。阈值是「亮度高于此值的像素才发光」：越低泛光范围越大、效果越明显。

    与 _filter_flash 同层：注入后的后处理干预，不动 build_action 的模型自由发挥映射（守 VFX 铁律）。
    """
    changed = 0
    for a in actions:
        if a.get("eventType") != "Bloom":
            continue
        try:
            th = float(a.get("threshold", 0.0))
        except Exception:
            continue
        new_th = max(0.0, th * BLOOM_TH_SCALE)
        a["threshold"] = round(new_th, 3)
        changed += 1
    if changed:
        print(f"[vfx] Bloom 阈值下压：{changed} 个 Bloom 的阈值 ×{BLOOM_TH_SCALE}"
              f"（泛光更明显，不动强度/颜色）")


def _lower_bloom_intensity(actions):
    """对全部 Bloom 动作的 intensity 做乘性下压（仅改 intensity 字段，不动 threshold/color）。
    原地修改 actions。强度越高泛光越亮、越易过曝刺眼。

    与 _lower_bloom_threshold 同层：注入后的后处理干预，不动 build_action 的模型自由发挥映射
    （守 VFX 铁律）。系数 BLOOM_INT_SCALE 可调（调小=更弱）。
    """
    changed = 0
    for a in actions:
        if a.get("eventType") != "Bloom":
            continue
        try:
            it = float(a.get("intensity", 0.0))
        except Exception:
            continue
        new_it = max(0.0, it * BLOOM_INT_SCALE)
        a["intensity"] = round(new_it, 3)
        changed += 1
    if changed:
        print(f"[vfx] Bloom 强度下压：{changed} 个 Bloom 的 intensity ×{BLOOM_INT_SCALE}"
              f"（泛光更柔和，不动阈值/颜色）")


def apply_vfx(level, audio, vfx_ckpt=None, intensity=1.0, device=None):
    """给生成谱面 level 注入视觉特效动作，原地修改并返回 level。
    intensity 固定 1.0（幅度不再有滑块/地板压缩，由模型自由决定）；
    mag=1.0（不再随音乐强度缩放，纯模型控制）。"""
    if vfx_ckpt is None:
        vfx_ckpt = CKPT
    if not os.path.exists(vfx_ckpt):
        print(f"[vfx] 权重缺失({vfx_ckpt})，跳过特效注入")
        return level
    if device is None:
        try:
            device = get_safe_device()
        except Exception:
            device = "cpu"

    # 1) 模型
    sd = torch.load(vfx_ckpt, map_location="cpu")
    model = VFXNet(n_events=len(VFX_EVENTS), n_filters=N_FILTERS, param_dim=3)
    model.load_state_dict(sd, strict=True)
    model = model.to(device).eval()

    # 2) 输入（mel 复用 demucs 分离结果；aux 由 level 反算方块时间构造，与训练一致）
    mel = demucs_mel(audio, device=device)        # (6,128,T)
    T = mel.shape[2]
    aux = _build_aux(level, T)                     # (4,T)

    # 3) 推理
    with torch.no_grad():
        ev, pa, fl, ez, dis = predict_vfx(model, mel, aux, device=device)  # (V,T),(V,3,T),(F,T),(V,E,T),(T,)

    # 4) 定位方块时间
    try:
        nt = compute_note_times(level.get("angleData", []), level.get("settings", {}),
                                level.get("actions", []), add_offset=True)
    except Exception:
        print("[vfx] compute_note_times 失败，无法定位方块，跳过")
        return level
    if not nt:
        return level

    I = float(np.clip(intensity, 0.0, 1.0))
    # 自由发挥：不限制每格注入数量，所有超过存在性下限的特效都注入。
    twirl_floors = set()
    for a in level.get("actions", []):
        if a.get("eventType") == "Twirl":
            try:
                twirl_floors.add(int(a["floor"]))
            except Exception:
                pass
    peak = ev.max(axis=0)            # (T,) 每帧峰值事件概率
    angle_data = level.get("angleData", [])
    new_actions = []
    for i, tms in enumerate(nt):
        if i >= len(angle_data):
            break
        if isinstance(tms, (tuple, list)):
            tms = tms[0]
        fi = max(0, min(T - 1, int(round(tms / HOP_MS))))
        # 重音门：只认 Twirl 翻身 或 模型峰值极高的强拍；其余格一律不注入
        # 注意 nt 索引 i 是 0-based（对应 floor=i+1），twirl_floors 存的是 1-based floor，故用 i+1 比较
        is_accent = (i + 1 in twirl_floors) or (float(peak[fi]) >= ACCENT_BAR)
        if not is_accent:
            continue
        tile_peak = float(peak[fi])
        # 幅度自由发挥：mag 固定 1.0（不再随音乐强度缩放），强度 intensity 固定 1.0
        mag = 1.0
        # 候选：ev 超过存在性下限 ABS_THR 的事件全部注入（自由发挥，不限数量）。
        cands = []
        for e in range(len(VFX_EVENTS)):
            name = VFX_EVENTS[e]
            s = float(ev[e, fi])
            # SetFilter 用更低的出现门槛，其余事件用 ABS_THR
            thr = SETFILTER_THR if name in ("SetFilter", "SetFilterAdvanced") else ABS_THR
            if s < thr:
                continue
            # 滤镜类(SetFilter/SetFilterAdvanced)不受全局重音门限制：
            # 只要模型本帧认为「该加滤镜」(自身概率越过 ABS_THR) 就注入，
            # 避免某些歌曲重音稀少导致一个滤镜都出不来（用户反馈「新歌没滤镜」）。
            # 其余事件仍须落在重音格上（与音乐节奏对齐，避免满屏乱飞）。
            if name not in ("SetFilter", "SetFilterAdvanced") and not is_accent:
                continue
            cands.append((s, e, name))
        for s, e, name in cands:
            # 用户要求（2026-08-14）：解锁全部视觉特效，模型自由发挥；仅禁用 PositionTrack
            # （位置轨道难看，且训练端已禁用不让模型学）。MultiPlanet 由 build_action 返回
            # None 兜底不注入。
            if name in ("PositionTrack", "SetFilterAdvanced"):
                continue
            # 模型预测该事件该用哪种缓动 / 哪种滤镜（自由，不再写死 InOutSine/Linear）
            ez_vec = ez[e, :, fi]                       # (E,)
            ease_idx = int(np.argmax(ez_vec))
            if name in ("SetFilter", "SetFilterAdvanced"):
                # 带温度 softmax + top-k 采样，避免 Grayscale 等高频类垄断
                _lg = fl[:, fi] / FILT_TEMP
                for _gi in _GRAY_IDXS:           # 压低纯灰阶，让别的滤镜出来
                    _lg[_gi] -= GRAYSCALE_BIAS
                _p = np.exp(_lg - _lg.max()); _p /= _p.sum()
                _top = np.argsort(_p)[::-1][:FILT_TOPK]
                _pt = _p[_top]; _pt /= _pt.sum()
                _rng = np.random.default_rng(int(fi) + 1)
                filt_idx = int(_top[_rng.choice(len(_top), p=_pt)])
                # 模型 disable_head 因训练数据 85% 为 False 而塌缩（输出恒≈0，max=0.045），
                # 无法可靠决定 disableOthers → 滤镜会无限堆叠成"灰泥"。
                # 推理端强制每帧滤镜独占（disableOthers=True），避免灰蒙蒙叠加。
                # 根治需重训时给 BCE 加 pos_weight≈5.7 抵消类别不平衡（见 train_vfx.py）。
                disable = True
            else:
                filt_idx = 0
                disable = False
            act = _make_action(name, pa[e, :, fi], intensity, mag,
                               ease_idx=ease_idx, filt_idx=filt_idx, disable=disable)
            if act is None:
                continue
            act["floor"] = i + 1  # ADOFAI floor 为 1-based（nt 索引 i 对应 floor=i+1）
            new_actions.append(act)

    level = _sanitize(level)
    level.setdefault("actions", []).extend(new_actions)
    # 闪光频率抑制：按方块时间对 Flash 降频 / 关闭（用户反馈快闪刺眼，2026-08-14）
    try:
        _filter_flash(level.setdefault("actions", []), nt)
    except Exception as e:
        print(f"[vfx] Flash 过滤跳过({e})")
    # 绽放阈值下压：让泛光更明显（用户反馈生成的 Bloom 阈值太高，2026-08-14）
    try:
        _lower_bloom_threshold(level.setdefault("actions", []))
    except Exception as e:
        print(f"[vfx] Bloom 阈值下压跳过({e})")
    # 绽放强度下压：让泛光更柔和不过曝（用户反馈生成的 Bloom 强度偏高，2026-08-14）
    try:
        _lower_bloom_intensity(level.setdefault("actions", []))
    except Exception as e:
        print(f"[vfx] Bloom 强度下压跳过({e})")
    print(f"[vfx] 注入 {len(new_actions)} 个视觉特效动作 "
          f"(intensity={I:.2f}, 自由发挥：数量/位置不限)")
    return level


if __name__ == "__main__":
    import sys, json
    if len(sys.argv) < 3:
        print("usage: apply_vfx.py <input.adofai> <audio> [intensity]")
        sys.exit(1)
    lv = json.load(open(sys.argv[1], encoding="utf-8"))
    out = apply_vfx(lv, sys.argv[2],
                    intensity=float(sys.argv[3]) if len(sys.argv) > 3 else 0.5)
    dst = sys.argv[1] + ".vfx.json"
    json.dump(out, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("written", dst)
