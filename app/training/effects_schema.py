"""
effects_schema.py — ADOFAI 视觉特效事件的「单一真相源」
========================================================================

所有视觉特效事件的正确字段名与参数语义，均转录自开源参考实现：
  - ADOFAI-JS (adofaiex/ADOFAI-JS, BSD-3)  src/events/*.ts 的 TypeScript 接口
  - pyadofai (TonyLimps/pyadofai)           确认 action 结构 {eventType, floor, ...}

为什么需要它：
  旧版 apply_vfx 用了一批「旧版/臆想」字段名（MoveCamera 的 cameraOffset /
  cameraAngle / cameraZoom / centered / unit；ShakeScreen 的 strength 等），
  游戏会直接忽略这些非法字段 -> 模型"学"出来的特效根本没生效。
  本模块把 ADOFAI 官方认可的字段名、参数范围、归一化方式集中管理，
  extract_vfx（提标签）与 apply_vfx（吐动作）共用，杜绝字段名漂移。

参数向量约定（VFXNet 的 param_head 输出 (V,3,T) 的每类 3 维语义）：
  vec[0] = mag     强度/缩放类（zoom 偏差、intensity、opacity、scale、亮度）
  vec[1] = spatial 空间位移类（MoveCamera 的 position 幅度、ScalePlanets 相对缩放）
  vec[2] = rotation 角度类（rotation / angleOffset / 行星旋转）
  归一化到约 [0,1]（rotation 为 [-1,1]），训练端、推理端共用同一套。

注入策略（用户拍板，2026-08-11）：
  - MoveTrack / AnimateTrack / PositionTrack：开放注入。它们移动的是「判定轨道的
    视觉显示位置」，不改变 tile 之间的相对转角序列，因此不影响时间戳计算（已与用户确认）。
  - SetFilter / SetFilterAdvanced：开放注入，filterType 由模型预测索引从受控白名单
    FILTER_TYPES（训练集全部 120 种真实滤镜名）选取，绝不自创名称 -> 不崩游戏。
  - 缓动 easing：模型预测 41 种合法 ADOFAI 缓动之一（非标 Flash* 变体归 Linear），
    不再写死 InOutSine/Linear。
  - MultiPlanet：已从 VFX_EVENTS 词表删除（V 20→19）。它是「结构事件」会改路径几何/
    时间戳，注入单行星谱会废谱，故不让模型学、生成端遇到也 return None 兜底。
  - 数量/位置完全自由：训练端不再做每帧稀疏截断，推理端不再做 top-K 精选，模型爱放几个放几个。
  - 幅度自由发挥：生成端 intensity 固定 1.0（不再有滑块/地板压缩），幅度由模型直接决定。
"""
from __future__ import annotations
import math
import numpy as np

# ── 视觉特效词表（顺序即类别下标，与旧版一致以保证缓存/模型维度兼容）────
# 纯视觉、不改判定轨道、不依赖外部素材的事件。
VFX_EVENTS = [
    "MoveTrack", "MoveCamera", "PositionTrack", "AnimateTrack",
    "Flash", "SetFilter", "SetFilterAdvanced", "Bloom",
    "RecolorTrack", "ColorTrack", "ShakeScreen", "HallOfMirrors",
    "ScreenTile", "Hide", "ScalePlanets", "ScaleRadius",
    "ScaleMargin", "SetPlanetRotation", "ScreenScroll",
]
V = len(VFX_EVENTS)
EVENT_INDEX = {e: i for i, e in enumerate(VFX_EVENTS)}
P = 3

# ── 归一化上限（把原始物理量压到 ~[0,1] / [-1,1]）────────────────────────
# MoveCamera.zoom 是「百分比整数」语义：100=原版；>100 缩小画面；<100 放大画面
# （已与用户确认，且训练谱实测取值 100~300）。故以 100 为基准、cap=200 覆盖真实范围。
_ZOOM_CAP = 200.0
_POS_CAP = 3.0         # MoveCamera position 幅度（tile）
_TRACK_POS_CAP = 10.0  # 轨道移动 x/y 位移（tile）：实测主要在 ±8 内，cap=10 覆盖正常区间并裁掉 ±500 脏数据
_ROT_CAP = 180.0       # 角度（deg）
# 震屏/绽放强度真实量纲跨度极大且高度长尾（vision/best 实测）：
#   ShakeScreen intensity 0~3333（均值374）、strength 0~1000（均值36）
#   Bloom intensity -2500~5000（有符号！均值277）、threshold 0~111
# 线性 clip 会把 99% 样本压到 ~0、丢失全部强弱区分 → 模型学不到"强拍狠弱拍轻"。
# 故用「有符号 log1p 映射」做量纲归一（类似分贝，保留比例不压幅度，符合 VFX 自由发挥铁律）：
#   编码 vec = sign(v)*log1p(|v|)/log1p(CAP)；解码 v = sign(vec)*expm1(|vec|*log1p(CAP))。
# CAP 取覆盖 99.9% 真实数据的量纲上界（极端脏值截断，非人为衰减）。
_SHAKE_INT_CAP = 3000.0   # ShakeScreen intensity 量纲上界（数据 max≈3333）
_SHAKE_STR_CAP = 1000.0   # ShakeScreen strength  量纲上界（数据 max≈1000）
_BLOOM_INT_CAP = 5000.0   # Bloom intensity 有符号量纲上界（数据 -2500~5000）
_BLOOM_TH_CAP = 120.0     # Bloom threshold 量纲上界（数据 max≈111）
def _log_enc(v, cap):
    """有符号 log1p 归一化到 [-1,1]（量纲映射，非衰减）。"""
    try:
        vf = float(v)
    except (TypeError, ValueError):
        vf = 0.0
    s = 1.0 if vf >= 0 else -1.0
    return s * (float(np.log1p(abs(vf))) / float(np.log1p(cap)))
def _log_dec(x, cap):
    """[-1,1] -> 有符号真实量纲（_log_enc 逆变换）。"""
    try:
        xf = float(x)
    except (TypeError, ValueError):
        xf = 0.0
    s = 1.0 if xf >= 0 else -1.0
    return s * float(np.expm1(abs(xf) * float(np.log1p(cap))))
_SCALE_CAP = 1.5       # ScreenTile/ScaleMargin/ScalePlanets(relativeScale) 相对缩放偏差（默认 1.0）
_RADIUS_CAP = 100.0     # ScaleRadius 轨道半径【百分制】：默认 100=原始大小，覆盖 scale∈[0,200]

# 受控滤镜白名单 —— 取自训练集 vision/best 中**真实出现**的全部 120 种滤镜名
# （含游戏原生的 Fisheye/Grayscale… 与 CameraFilterPack_* 系列）。按字母排序定序，
# 训练端(extract)与推理端(apply)共用同一份 -> 索引一致、绝不自创、游戏必识别。
# SetFilter / SetFilterAdvanced 的 filter 字段值只能从这里选。
FILTER_TYPES = [
    "Aberration", "Arcade", "Blizzard", "Blur", "BlurFocus",
    "CameraFilterPack_AAA_SuperComputer", "CameraFilterPack_Atmosphere_Rain",
    "CameraFilterPack_Atmosphere_Rain_Pro", "CameraFilterPack_Blizzard",
    "CameraFilterPack_Blur_Bloom", "CameraFilterPack_Blur_BlurHole",
    "CameraFilterPack_Blur_GaussianBlur", "CameraFilterPack_Blur_Movie",
    "CameraFilterPack_Blur_Noise", "CameraFilterPack_Blur_Radial",
    "CameraFilterPack_Blur_Tilt_Shift", "CameraFilterPack_Blur_Tilt_Shift_Hole",
    "CameraFilterPack_Blur_Tilt_Shift_V", "CameraFilterPack_Color_BrightContrastSaturation",
    "CameraFilterPack_Color_Chromatic_Aberration", "CameraFilterPack_Color_GrayScale",
    "CameraFilterPack_Color_Noise", "CameraFilterPack_Color_Sepia",
    "CameraFilterPack_Colors_Brightness", "CameraFilterPack_Colors_DarkColor",
    "CameraFilterPack_Colors_HUE_Rotate", "CameraFilterPack_Colors_Threshold",
    "CameraFilterPack_Distortion_Dream2", "CameraFilterPack_Distortion_FishEye",
    "CameraFilterPack_Distortion_Heat", "CameraFilterPack_Distortion_Lens",
    "CameraFilterPack_Distortion_Noise", "CameraFilterPack_Distortion_ShockWave",
    "CameraFilterPack_Distortion_Water_Drop", "CameraFilterPack_Distortion_Wave_Horizontal",
    "CameraFilterPack_Drawing_BluePrint", "CameraFilterPack_Drawing_CellShading",
    "CameraFilterPack_Drawing_Laplacian", "CameraFilterPack_Drawing_Manga_FlashWhite",
    "CameraFilterPack_Drawing_Manga_Flash_Color", "CameraFilterPack_Drawing_NewCellShading",
    "CameraFilterPack_Drawing_Paper", "CameraFilterPack_Drawing_Toon",
    "CameraFilterPack_FX_DigitalMatrix", "CameraFilterPack_FX_DigitalMatrixDistortion",
    "CameraFilterPack_FX_Dot_Circle", "CameraFilterPack_FX_Drunk",
    "CameraFilterPack_FX_EarthQuake", "CameraFilterPack_FX_Glitch1",
    "CameraFilterPack_FX_Glitch2", "CameraFilterPack_FX_Glitch3",
    "CameraFilterPack_FX_Hypno", "CameraFilterPack_FX_Plasma",
    "CameraFilterPack_Film_Grain", "CameraFilterPack_Glitch_Mozaic",
    "CameraFilterPack_Glow_Glow", "CameraFilterPack_Glow_Glow_Color",
    "CameraFilterPack_Light_Water2", "CameraFilterPack_Noise_TV_2",
    "CameraFilterPack_Noise_TV_3", "CameraFilterPack_Oculus_NightVision1",
    "CameraFilterPack_OldFilm_Cutting1", "CameraFilterPack_Pixel_Pixelisation",
    "CameraFilterPack_Real_VHS", "CameraFilterPack_Sharpen_Sharpen",
    "CameraFilterPack_TV_80", "CameraFilterPack_TV_ARCADE_2",
    "CameraFilterPack_TV_Artefact", "CameraFilterPack_TV_Chromatical",
    "CameraFilterPack_TV_Chromatical2", "CameraFilterPack_TV_CompressionFX",
    "CameraFilterPack_TV_Distorted", "CameraFilterPack_TV_LED",
    "CameraFilterPack_TV_Noise", "CameraFilterPack_TV_Old_Movie",
    "CameraFilterPack_TV_Old_Movie_2", "CameraFilterPack_TV_Posterize",
    "CameraFilterPack_TV_VHS", "CameraFilterPack_TV_VHS_Rewind",
    "CameraFilterPack_TV_Vcr", "CameraFilterPack_TV_WideScreenHV",
    "CameraFilterPack_TV_WideScreenHorizontal", "CameraFilterPack_TV_WideScreenVertical",
    "CameraFilterPack_VHS_Tracking", "CameraFilterPack_Vision_Aura",
    "CameraFilterPack_Vision_Crystal", "CameraFilterPack_Vision_Plasma",
    "CameraFilterPack_Vision_Tunnel", "Compression", "Contrast", "Drawing",
    "EdgeBlackLine", "EightiesTV", "FiftiesTV", "Fisheye", "GaussianBlur",
    "Glitch", "Grain", "Grayscale", "HexagonBlack", "Invert", "LED",
    "LightWater", "MotionBlur", "Neon", "OilPaint", "Petals", "PetalsInstant",
    "PixelSnow", "Pixelate", "Posterize", "Rain", "Sepia", "Sharpen",
    "Static", "SuperDot", "VHS", "WaterDrop", "Waves", "Weird3D",
]

# ── 滤镜名规范化（关键修复）──
# ADOFAI 游戏读 .adofai 时，SetFilter/SetFilterAdvanced 的 `filter` 字段只认上方那 37 个
# 简称（Fisheye/VHS/Grayscale…）。但训练数据里大量是 Unity 底层资源名 CameraFilterPack_*，
# 模型也学着吐这些名。游戏遇到不认识的滤镜名会直接崩溃 → 关卡加载失败。
# 这里把底层名映射到最接近的合法简称；未知底层名兜底为 Fisheye。
# 注意：映射只改"名字字符串"，不动任何强度/幅度（守自由发挥铁律）。
FILTER_ALIAS = {
    "CameraFilterPack_AAA_SuperComputer": "Glitch",
    "CameraFilterPack_Atmosphere_Rain": "Rain",
    "CameraFilterPack_Atmosphere_Rain_Pro": "Rain",
    "CameraFilterPack_Blizzard": "Blizzard",
    "CameraFilterPack_Blur_Bloom": "Blur",
    "CameraFilterPack_Blur_BlurHole": "Blur",
    "CameraFilterPack_Blur_GaussianBlur": "GaussianBlur",
    "CameraFilterPack_Blur_Movie": "MotionBlur",
    "CameraFilterPack_Blur_Noise": "Grain",
    "CameraFilterPack_Blur_Radial": "Blur",
    "CameraFilterPack_Blur_Tilt_Shift": "Blur",
    "CameraFilterPack_Blur_Tilt_Shift_Hole": "Blur",
    "CameraFilterPack_Blur_Tilt_Shift_V": "Blur",
    "CameraFilterPack_Color_BrightContrastSaturation": "Contrast",
    "CameraFilterPack_Color_Chromatic_Aberration": "Aberration",
    "CameraFilterPack_Color_GrayScale": "Grayscale",
    "CameraFilterPack_Color_Noise": "Grain",
    "CameraFilterPack_Color_Sepia": "Sepia",
    "CameraFilterPack_Colors_Brightness": "Contrast",
    "CameraFilterPack_Colors_DarkColor": "Contrast",
    "CameraFilterPack_Colors_HUE_Rotate": "Aberration",
    "CameraFilterPack_Colors_Threshold": "Posterize",
    "CameraFilterPack_Distortion_Dream2": "Waves",
    "CameraFilterPack_Distortion_FishEye": "Fisheye",
    "CameraFilterPack_Distortion_Heat": "Fisheye",
    "CameraFilterPack_Distortion_Lens": "Fisheye",
    "CameraFilterPack_Distortion_Noise": "Glitch",
    "CameraFilterPack_Distortion_ShockWave": "Waves",
    "CameraFilterPack_Distortion_Water_Drop": "WaterDrop",
    "CameraFilterPack_Distortion_Wave_Horizontal": "Waves",
    "CameraFilterPack_Drawing_BluePrint": "Drawing",
    "CameraFilterPack_Drawing_CellShading": "Drawing",
    "CameraFilterPack_Drawing_Laplacian": "Drawing",
    "CameraFilterPack_Drawing_Manga_FlashWhite": "Drawing",
    "CameraFilterPack_Drawing_Manga_Flash_Color": "Drawing",
    "CameraFilterPack_Drawing_NewCellShading": "Drawing",
    "CameraFilterPack_Drawing_Paper": "Drawing",
    "CameraFilterPack_Drawing_Toon": "Drawing",
    "CameraFilterPack_FX_DigitalMatrix": "Glitch",
    "CameraFilterPack_FX_DigitalMatrixDistortion": "Glitch",
    "CameraFilterPack_FX_Dot_Circle": "SuperDot",
    "CameraFilterPack_FX_Drunk": "Waves",
    "CameraFilterPack_FX_EarthQuake": "Glitch",
    "CameraFilterPack_FX_Glitch1": "Glitch",
    "CameraFilterPack_FX_Glitch2": "Glitch",
    "CameraFilterPack_FX_Glitch3": "Glitch",
    "CameraFilterPack_FX_Hypno": "Neon",
    "CameraFilterPack_FX_Plasma": "Neon",
    "CameraFilterPack_Film_Grain": "Grain",
    "CameraFilterPack_Glitch_Mozaic": "Pixelate",
    "CameraFilterPack_Glow_Glow": "Neon",
    "CameraFilterPack_Glow_Glow_Color": "Neon",
    "CameraFilterPack_Light_Water2": "LightWater",
    "CameraFilterPack_Noise_TV_2": "Static",
    "CameraFilterPack_Noise_TV_3": "Static",
    "CameraFilterPack_Oculus_NightVision1": "Neon",
    "CameraFilterPack_OldFilm_Cutting1": "EightiesTV",
    "CameraFilterPack_Pixel_Pixelisation": "Pixelate",
    "CameraFilterPack_Real_VHS": "VHS",
    "CameraFilterPack_Sharpen_Sharpen": "Sharpen",
    "CameraFilterPack_TV_80": "EightiesTV",
    "CameraFilterPack_TV_ARCADE_2": "Arcade",
    "CameraFilterPack_TV_Artefact": "VHS",
    "CameraFilterPack_TV_Chromatical": "Aberration",
    "CameraFilterPack_TV_Chromatical2": "Aberration",
    "CameraFilterPack_TV_CompressionFX": "Compression",
    "CameraFilterPack_TV_Distorted": "VHS",
    "CameraFilterPack_TV_LED": "LED",
    "CameraFilterPack_TV_Noise": "Static",
    "CameraFilterPack_TV_Old_Movie": "EightiesTV",
    "CameraFilterPack_TV_Old_Movie_2": "EightiesTV",
    "CameraFilterPack_TV_Posterize": "Posterize",
    "CameraFilterPack_TV_VHS": "VHS",
    "CameraFilterPack_TV_VHS_Rewind": "VHS",
    "CameraFilterPack_TV_Vcr": "VHS",
    "CameraFilterPack_TV_WideScreenHV": "EightiesTV",
    "CameraFilterPack_TV_WideScreenHorizontal": "EightiesTV",
    "CameraFilterPack_TV_WideScreenVertical": "EightiesTV",
    "CameraFilterPack_VHS_Tracking": "VHS",
    "CameraFilterPack_Vision_Aura": "Neon",
    "CameraFilterPack_Vision_Crystal": "Neon",
    "CameraFilterPack_Vision_Plasma": "Neon",
    "CameraFilterPack_Vision_Tunnel": "Weird3D",
}


def _norm_filter(name):
    """把滤镜名规范成 ADOFAI 认识的简称（避免游戏因不认识的滤镜名崩溃）。"""
    if not isinstance(name, str):
        return "Fisheye"
    if name in FILTER_ALIAS:
        return FILTER_ALIAS[name]
    if name.startswith("CameraFilterPack"):
        return "Fisheye"
    return name


FILTER_INDEX = {f: i for i, f in enumerate(FILTER_TYPES)}


def filter_index_of(name):
    """滤镜名 -> 白名单索引（不在名单内归 0 = Fisheye，绝不越界）。"""
    if not isinstance(name, str):
        return 0
    return FILTER_INDEX.get(name, 0)


# 缓动词表（ADOFAI 官方 41 种合法 easing，按标准顺序；非标的 Flash* 变体不收录，
# 解析时一律归 0=Linear 以免游戏不认）。训练/推理共用，保证生成的 easing 必合法。
EASING = [
    "Linear",
    "InQuad", "OutQuad", "InOutQuad", "OutInQuad",
    "InCubic", "OutCubic", "InOutCubic", "OutInCubic",
    "InQuart", "OutQuart", "InOutQuart", "OutInQuart",
    "InQuint", "OutQuint", "InOutQuint", "OutInQuint",
    "InSine", "OutSine", "InOutSine", "OutInSine",
    "InExpo", "OutExpo", "InOutExpo", "OutInExpo",
    "InCirc", "OutCirc", "InOutCirc", "OutInCirc",
    "InElastic", "OutElastic", "InOutElastic", "OutInElastic",
    "InBack", "OutBack", "InOutBack", "OutInBack",
    "InBounce", "OutBounce", "InOutBounce", "OutInBounce",
]
EASING_INDEX = {e: i for i, e in enumerate(EASING)}
N_EASINGS = len(EASING)


def easing_index_of(name):
    """缓动名 -> 词表索引（未知/非标归 0 = Linear，保证游戏安全）。"""
    if not isinstance(name, str):
        return 0
    return EASING_INDEX.get(name, 0)


def _clip(x, lo, hi):
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return float(lo)
    return max(lo, min(hi, xf))


def _num(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _vec2_mag(v):
    """Vec2([x,y]) -> 标量幅度。"""
    try:
        xs = float(v[0]); ys = float(v[1])
    except Exception:
        return 0.0
    return float(np.hypot(xs, ys))


def _brightness(hexc):
    """#RRGGBB -> 0~1 亮度。"""
    if not isinstance(hexc, str) or len(hexc) < 7:
        return 0.5
    try:
        r = int(hexc[1:3], 16); g = int(hexc[3:5], 16); b = int(hexc[5:7], 16)
        return (r + g + b) / (3.0 * 255.0)
    except Exception:
        return 0.5


def _parse_hex(hexc):
    """ADOFAI 颜色串（'rrggbb' / 'rrggbbaa' / '#...'，有无 # 均可）-> (r,g,b) 0~255。

    ADOFAI 的 trackColor 常为 8 位 RGBA（如 '091767ff'），前 6 位才是 RGB，
    末 2 位是 alpha；也可能无 '#' 前缀。统一正确解析为 RGB 三通道。"""
    if not isinstance(hexc, str):
        return (255, 255, 255)
    s = hexc.strip().lstrip('#')
    if len(s) >= 6:
        s = s[:6]
    else:
        return (255, 255, 255)
    try:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except Exception:
        return (255, 255, 255)


def _rgb_to_hsv(r, g, b):
    """(0~255) -> (h 0~1, s 0~1, v 0~1)。颜色事件参数向量复用 [V,H,S] 三通道编码，
    配合 build_action 的 HSV->RGB 可无损还原任意颜色（不动网络结构，param_dim 仍=3）。"""
    r, g, b = r / 255.0, g / 255.0, b / 255.0
    mx, mn = max(r, g, b), min(r, g, b)
    d = mx - mn
    v = mx
    if d <= 1e-9:
        return (0.0, 0.0, v)
    s = d / mx if mx > 0 else 0.0
    if mx == r:
        h = ((g - b) / d) % 6.0
    elif mx == g:
        h = (b - r) / d + 2.0
    else:
        h = (r - g) / d + 4.0
    return (h / 6.0, s, v)


def _hsv_to_rgb(h, s, v):
    """(h 0~1, s 0~1, v 0~1) -> (r,g,b) 0~255 整数。"""
    h = h - math.floor(h)
    i = int(h * 6.0); f = h * 6.0 - i
    p = v * (1.0 - s); q = v * (1.0 - f * s); t = v * (1.0 - (1.0 - f) * s)
    i = i % 6
    if i == 0:
        r, g, b = v, t, p
    elif i == 1:
        r, g, b = q, v, p
    elif i == 2:
        r, g, b = p, v, t
    elif i == 3:
        r, g, b = p, q, v
    elif i == 4:
        r, g, b = t, p, v
    else:
        r, g, b = v, p, q
    return (int(round(r * 255)), int(round(g * 255)), int(round(b * 255)))


def _rgb_to_hex(r, g, b, alpha=255):
    """(0~255) -> 'rrggbbaa' 八位串（ADOFAI trackColor 习惯带 alpha）。"""
    return "%02X%02X%02X%02X" % (int(r) & 255, int(g) & 255, int(b) & 255, int(alpha) & 255)


# ── 提取：ADOFAI action 对象 -> 归一化参数向量 (3,) ─────────────────────
def extract_params(et, obj):
    """返回 (vec3 [mag, spatial, rotation], ) 归一化到约 [0,1]（rotation [-1,1]）。
    仅解析 ADOFAI 官方认可的字段（来自 ADOFAI-JS 接口）；未知字段忽略。"""
    vec = np.zeros(3, np.float32)
    if et == "MoveCamera":
        # zoom 是百分比整数：默认 100=原版；>100 缩小画面；<100 放大画面
        z = _num(obj.get("zoom", 100.0), 100.0)
        vec[0] = _clip((z - 100.0), -_ZOOM_CAP, _ZOOM_CAP) / _ZOOM_CAP
        pos = obj.get("position", [0, 0]) or [0, 0]
        vec[1] = _clip(_vec2_mag(pos), 0, _POS_CAP) / _POS_CAP
        rot = _num(obj.get("rotation", 0.0), 0.0)
        vec[2] = _clip(rot, -_ROT_CAP, _ROT_CAP) / _ROT_CAP
    elif et == "Flash":
        op = _num(obj.get("opacity", 1.0), 1.0)
        vec[0] = _clip(op, 0, 1.0)
        vec[1] = _brightness(obj.get("color", "#FFFFFF"))
        # plane（Foreground/Background）：由模型学，自由发挥；缺省按 Background
        vec[2] = 1.0 if str(obj.get("plane", "Background")).lower() == "foreground" else -1.0
    elif et == "Bloom":
        it = _num(obj.get("intensity", 0.0), 0.0)
        th = _num(obj.get("threshold", 0.0), 0.0)
        vec[0] = _log_enc(it, _BLOOM_INT_CAP)        # 有符号强度
        vec[1] = _clip(th, 0, _BLOOM_TH_CAP) / _BLOOM_TH_CAP
    elif et == "ShakeScreen":
        it = _num(obj.get("intensity", 0.0), 0.0)
        st = _num(obj.get("strength", 0.0), 0.0)
        vec[0] = _log_enc(it, _SHAKE_INT_CAP)
        vec[1] = _log_enc(st, _SHAKE_STR_CAP)
    elif et == "HallOfMirrors":
        ang = _num(obj.get("angleOffset", 0.0), 0.0)
        vec[2] = _clip(ang, -_ROT_CAP, _ROT_CAP) / _ROT_CAP
    elif et == "ScreenTile":
        ang = _num(obj.get("angleOffset", 0.0), 0.0)
        vec[2] = _clip(ang, -_ROT_CAP, _ROT_CAP) / _ROT_CAP
        sc = _num(obj.get("scale", 1.0), 1.0)
        vec[0] = _clip(sc - 1.0, -_SCALE_CAP, _SCALE_CAP) / _SCALE_CAP
    elif et == "ScalePlanets":
        rs = _num(obj.get("relativeScale", 1.0), 1.0)
        vec[0] = _clip(rs - 1.0, -_SCALE_CAP, _SCALE_CAP) / _SCALE_CAP
    elif et == "ScaleRadius":
        # 百分制：默认 scale=100（原始大小），非相对倍率；偏移 100、cap=_RADIUS_CAP
        sc = _num(obj.get("scale", 100.0), 100.0)
        vec[0] = _clip(sc - 100.0, -_RADIUS_CAP, _RADIUS_CAP) / _RADIUS_CAP
    elif et == "ScaleMargin":
        sc = _num(obj.get("scale", 1.0), 1.0)
        vec[0] = _clip(sc - 1.0, -_SCALE_CAP, _SCALE_CAP) / _SCALE_CAP
    elif et == "RecolorTrack":
        # 颜色事件：用 HSV 三通道编码（复用 param 的 3 维），完整保留色相/饱和度。
        # 旧版只提亮度且 8 位 RGBA hex 解析错位 -> 模型对颜色是瞎子；现修正。
        r, g, b = _parse_hex(obj.get("trackColor", "#FFFFFF"))
        h, s, v = _rgb_to_hsv(r, g, b)
        vec[0], vec[1], vec[2] = v, h, s
    elif et == "ColorTrack":
        # ColorTrack 真实字段也是 trackColor（非 color），兜底 color
        r, g, b = _parse_hex(obj.get("trackColor", obj.get("color", "#FFFFFF")))
        h, s, v = _rgb_to_hsv(r, g, b)
        vec[0], vec[1], vec[2] = v, h, s
    elif et == "SetPlanetRotation":
        rot = _num(obj.get("rotation", 0.0), 0.0)
        vec[2] = _clip(rot, -_ROT_CAP, _ROT_CAP) / _ROT_CAP
    elif et == "Hide":
        dur = _num(obj.get("duration", 0.3), 0.3)
        vec[0] = _clip(dur / 2.0, 0, 1)
    elif et in ("MoveTrack", "AnimateTrack", "PositionTrack"):
        # 移动判定轨道：学 x / y 位移（有符号）+ 角度（rotation/angleOffset 由模型决定）
        off = obj.get("positionOffset", [0, 0]) or [0, 0]
        vec[1] = _clip(_num(off[0], 0.0), -_TRACK_POS_CAP, _TRACK_POS_CAP) / _TRACK_POS_CAP   # x（有符号）
        vec[2] = _clip(_num(off[1], 0.0), -_TRACK_POS_CAP, _TRACK_POS_CAP) / _TRACK_POS_CAP   # y（有符号）
        ang_key = "rotation" if et != "PositionTrack" else "angleOffset"
        ang = _num(obj.get(ang_key, 0.0), 0.0)
        vec[0] = _clip(ang, -_ROT_CAP, _ROT_CAP) / _ROT_CAP   # 角度（归一化，自由发挥）
    # SetFilter / SetFilterAdvanced：滤镜名 + 强度在 extract_vfx 主循环里单独解析（进 filt/params 标签）
    return vec


# ── 生成：归一化参数向量 -> 合法 ADOFAI action（正确字段名）─────────────
def build_action(name, vec3, intensity, mag=1.0, ease_idx=0, filt_idx=0, disable=False):
    """vec3: [mag, spatial, rotation]（模型预测，归一化）。
    intensity: 0~1（本版固定 1.0 = 不限制幅度，模型自由发挥）；mag: 音乐强度系数。
    ease_idx: 缓动词表索引（模型学得的缓动）；filt_idx: 滤镜白名单索引（模型学得的滤镜）。
    返回合法 action dict（使用 ADOFAI 官方字段名），或 None（不注入）。"""
    I = float(np.clip(intensity, 0.0, 1.0))
    m = float(np.clip(mag, 0.4, 1.6))
    v0, v1, v2 = (float(x) for x in vec3[:3])
    _e = EASING[int(np.clip(ease_idx, 0, len(EASING) - 1))]
    _ft = FILTER_TYPES[int(np.clip(filt_idx, 0, len(FILTER_TYPES) - 1))]

    # 仅 MultiPlanet（结构事件，改路径几何 + 时间戳）不注入；其余一律放行进谱。
    if name == "MultiPlanet":
        return None

    # ── 轨道移动类（不影响时间戳，用户确认开放；学 x/y/角度，自由发挥不设上限）──
    if name in ("MoveTrack", "AnimateTrack"):
        # 直接逆变换还原物理量：vec 已是模型自由输出，幅度由模型决定
        x = _clip(v1, -1, 1) * _TRACK_POS_CAP
        y = _clip(v2, -1, 1) * _TRACK_POS_CAP
        # 用户要求：MoveTrack 的角度字段按真实人工谱面写 angleOffset（非 rotation，
        # rotation 在 MoveTrack 里是无效字段，游戏不认），并强制 angleOffset=180。
        # 同时补真实谱面都有的 startTile/endTile/eventTag 字段，与人工谱一致。
        return {"eventType": name,
                "startTile": [0, "ThisTile"], "endTile": [0, "ThisTile"],
                "gapLength": 0, "duration": 0.5,
                "positionOffset": [round(x, 3), round(y, 3)],
                "angleOffset": 180, "opacity": 100,
                "ease": _e, "eventTag": "", "enabled": True}
    if name == "PositionTrack":
        x = _clip(v1, -1, 1) * _TRACK_POS_CAP
        y = _clip(v2, -1, 1) * _TRACK_POS_CAP
        ang = _clip(v0, -1, 1) * _ROT_CAP
        return {"eventType": "PositionTrack",
                "positionOffset": [round(x, 3), round(y, 3)],
                "angleOffset": int(round(ang)), "ease": _e,
                "enabled": True}
    # ── 屏幕滤镜类（受控白名单，绝不自创名称；滤镜名由模型预测的 filt_idx 决定）──
    if name == "SetFilter":
        # 逆变换还原游戏强度(0~100)，幅度由模型决定，不设上限（仅下限防负）
        it = max(0.0, _clip(v0, -1.0, 1.0) * 50.0 + 50.0)
        act = {"eventType": name, "filter": _norm_filter(_ft),
               "intensity": round(it, 3), "duration": 0.5,
               "ease": _e, "enabled": True}
        if disable:
            act["disableOthers"] = True   # 模型学得的"需要禁用其他滤镜"
        return act
    if name == "SetFilterAdvanced":
        # ADOFAI 官方字段：filter/intensity/rotation/noise/transitionTime/speed。
        # 不能用 SetFilter 的 duration（SetFilterAdvanced 根本没有该字段，游戏读到会崩溃）；
        # 也不会缺 rotation/noise/transitionTime/speed（缺了加载直接失败）。
        # rotation/noise 模型未预测，用安全默认 0；transitionTime/speed 用常用默认。
        it = max(0.0, _clip(v0, -1.0, 1.0) * 50.0 + 50.0)
        act = {"eventType": name, "filter": _norm_filter(_ft),
               "intensity": round(it, 3),
               "rotation": 0.0, "noise": 0.0,
               "transitionTime": 0.5, "speed": 1.0,
               "ease": _e, "enabled": True}
        if disable:
            act["disableOthers"] = True
        return act

    if name == "MoveCamera":
        # 逆变换还原物理量，幅度由模型自由决定，不设人为上限
        z = max(0.0, 100.0 + _clip(v0, -1.0, 1.0) * _ZOOM_CAP)   # 百分比整数(<100放大,>100缩小)
        p = _clip(v1, -1.0, 1.0) * _POS_CAP                       # 位置幅度(tile)
        rot = _clip(v2, -1, 1) * _ROT_CAP
        return {"eventType": "MoveCamera", "duration": 0.5,
                "relativeTo": "Tile", "position": [round(p, 3), round(p, 3)],
                "rotation": round(rot, 2), "zoom": round(z, 1),
                "ease": _e, "enabled": True}
    if name == "Flash":
        op = max(0.0, _clip(v0, -1.0, 1.0))                       # 透明度 0~1（物理域）
        pl = "Foreground" if v2 >= 0.0 else "Background"          # plane 由模型决定（v2 学自训练）
        return {"eventType": "Flash", "duration": 0.4,
                "color": "#FFFFFF", "opacity": round(op, 3),
                "flashStyle": "Classic", "plane": pl, "ease": _e, "enabled": True}
    if name == "Bloom":
        it = _log_dec(_clip(v0, -1.0, 1.0), _BLOOM_INT_CAP)     # 有符号逆变换
        th = max(0.0, _clip(v1, -1.0, 1.0)) * _BLOOM_TH_CAP
        return {"eventType": "Bloom", "intensity": round(it, 3),
                "threshold": round(th, 3), "enabled": True}
    if name == "ShakeScreen":
        it = _log_dec(_clip(v0, -1.0, 1.0), _SHAKE_INT_CAP)
        st = _log_dec(_clip(v1, -1.0, 1.0), _SHAKE_STR_CAP)
        return {"eventType": "ShakeScreen", "duration": 0.6,
                "intensity": round(it, 3), "strength": round(st, 3),
                "fadeOut": "Enabled", "ease": _e, "enabled": True}
    if name == "HallOfMirrors":
        ang = _clip(v2, -1, 1) * _ROT_CAP
        return {"eventType": "HallOfMirrors", "angleOffset": int(round(ang)),
                "enabled": True}
    if name == "ScreenTile":
        ang = _clip(v2, -1, 1) * _ROT_CAP
        sc = max(0.01, 1.0 + _clip(v0, -1.0, 1.0) * _SCALE_CAP)  # 仅下限防0
        return {"eventType": "ScreenTile", "angle": int(round(ang)),
                "scale": round(sc, 3),
                "bothDirections": True, "enabled": True}
    if name == "ScalePlanets":
        rs = max(0.01, 1.0 + _clip(v0, -1.0, 1.0) * _SCALE_CAP)
        return {"eventType": "ScalePlanets",
                "relativeScale": round(rs, 3), "enabled": True}
    if name == "ScaleRadius":
        # 百分制：默认 scale=100（原始大小）；v0=0 -> 100
        sc = max(1.0, 100.0 + _clip(v0, -1.0, 1.0) * _RADIUS_CAP)
        return {"eventType": "ScaleRadius",
                "scale": round(sc, 1), "enabled": True}
    if name == "ScaleMargin":
        sc = max(0.01, 1.0 + _clip(v0, -1.0, 1.0) * _SCALE_CAP)
        return {"eventType": "ScaleMargin",
                "scale": round(sc, 3), "enabled": True}
    if name == "RecolorTrack":
        h = _clip(v1, 0, 1)
        s = _clip(v2, 0, 1)
        v = _clip(v0, 0, 1)
        r, g, b = _hsv_to_rgb(h, s, v)
        return {"eventType": "RecolorTrack", "trackColor": _rgb_to_hex(r, g, b, 255),
                "trackColorType": "Single", "trackStyle": "NeonLight",
                "trackGlowIntensity": 100, "enabled": True}
    if name == "ColorTrack":
        h = _clip(v1, 0, 1)
        s = _clip(v2, 0, 1)
        v = _clip(v0, 0, 1)
        r, g, b = _hsv_to_rgb(h, s, v)
        return {"eventType": "ColorTrack", "trackColor": _rgb_to_hex(r, g, b, 255),
                "trackColorType": "Single", "trackStyle": "NeonLight",
                "trackGlowIntensity": 100, "enabled": True}
    if name == "SetPlanetRotation":
        rot = _clip(v2, -1, 1) * _ROT_CAP
        return {"eventType": "SetPlanetRotation",
                "rotation": int(round(rot)), "relative": True, "enabled": True}
    if name == "Hide":
        dur = max(0.0, _clip(v0, -1.0, 1.0) * 2.0)               # 逆变换(标签 dur/2)
        return {"eventType": "Hide", "hideTile": True, "opacity": 0,
                "duration": round(dur, 3), "enabled": True}
    if name == "ScreenScroll":
        st = 0.5 + 0.5 * I
        return {"eventType": "ScreenScroll", "duration": 1.0,
                "strength": round(st, 3), "enabled": True}
    return None
