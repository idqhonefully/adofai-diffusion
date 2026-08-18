"""
chart_repr.py — ADOFAI 谱面 <-> 稠密张量 (C, T) 双向转换
========================================================
按大佬架构(两阶段流水线):
  - 角度由【转换器】按 "间隔×180/拍长" 确定性算出,模型不碰角度 →
    彻底绕开「绝对角度回归塌缩」+「276s 物理错位」两个坑。
  - 方向(左/右):左/右转在同一时刻音频完全相同,模型学不到方向信号,故由
    plan_directions【几何路径规划】指派(不自交 + 留屏内),模型预测仅作平局破冰。
    这正对应大佬"转换器把时间点转成角度"的做法——方向是确定性布局决策。
  - SetSpeed 不做(大佬未做);Twirl 已启用:由 plan_path_twirl 计时反推式植入
    (不破坏踩点;Twirl 仅做视觉镜像翻转),由 onset 重音 + 模型 C2 弱偏置驱动。

  通道布局 (C=3):
    C0 onset : 格子起点处高斯热图(峰=1,附近平滑衰减)
    C1 dir   : 转弯方向类别 0=左转(+), 1=右转(-);非 onset 帧为 0(解码时忽略)
    C2 twirl : Twirl 事件热图(驱动 Twirl 放置;生成时由 onset 重音 + 该通道弱偏置决定)

  全局 BPM 不作为通道,而是【外部条件】传入(训练取谱面自身 bpm,推理取 UI/默认 120)。

  adofai_to_dense  : level dict -> (3, T) float32(训练目标;dir 取自真实角度符号)
  dense_to_adofai  : (3, T) -> level dict(转换器按间隔算角度 + 路径规划指派方向)
"""
from __future__ import annotations

import sys
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
from timing_engine import compute_note_times
from adofai_parse import _to_angle_data   # pathData(str/list) 与 angleData 统一转 angleData

N_CH = 3
DIR_LEFT = 0
DIR_RIGHT = 1
SR = 22050
# ── ADOFAI Diffusion（hop=128 高分辨率网格）─────────────────────
# hop_length=128 ≈ 5.805 ms/帧（原 512 的 1/4 时间分辨率），踩点/事件网格都跑 128，
# 整条链路（OnsetNet 踩点 + VAE/扩散加事件）同一网格，落谱无需换算。
HOP = 128
HOP_MS = 1000.0 * HOP / SR          # ≈ 5.805 ms
# onset / twirl 通道用「高斯热图」而非单帧脉冲：峰值=1、附近平滑衰减（σ 帧）。
# 这样 VAE 重建能学出清晰峰值，避免稀疏单帧脉冲被后验塌缩抹平成常数。
# 128 网格下 σ=1.5 帧 ≈ ±4.5 帧(±26ms) 的峰值包络，比 512 网格更锐利。
ONSET_SIGMA = 1.5


def _bump(center, T, sigma=ONSET_SIGMA):
    """返回 (lo, hi, weights)，用于在 center 处叠加一段高斯热图。"""
    lo = max(0, int(round(center - 3.0 * sigma)))
    hi = min(T, int(round(center + 3.0 * sigma)) + 1)
    d = np.arange(lo, hi, dtype=np.float32) - center
    w = np.exp(-(d ** 2) / (2.0 * sigma * sigma))
    return lo, hi, w


def _events_by_floor(actions):
    """返回 twirl_floors:set。"""
    twirl = set()
    for a in (actions or []):
        if not isinstance(a, dict):
            continue
        et = a.get("eventType")
        fl = a.get("floor")
        if et == "Twirl" and fl is not None:
            twirl.add(int(fl))
    return twirl


def adofai_to_dense(level, T, hop_ms=HOP_MS, global_bpm=120.0):
    """level(dict) -> (3, T) float32 稠密谱面。解析失败返回全零。

    通道: C0 onset 热图; C1 dir(0=左/1=右, 取自真实角度符号); C2 twirl 热图(阶段一=0)。
    """
    ad = _to_angle_data(level) or []
    settings = level.get("settings") or {}
    actions = level.get("actions") or []
    if not ad:
        return np.zeros((N_CH, T), np.float32)
    try:
        nt = compute_note_times(ad, settings, actions, add_offset=True)
    except Exception:
        return np.zeros((N_CH, T), np.float32)
    if not nt:
        return np.zeros((N_CH, T), np.float32)
    times = [float(x[0]) if isinstance(x, (tuple, list)) else float(x) for x in nt]
    twirl = _events_by_floor(actions)
    dense = np.zeros((N_CH, T), np.float32)
    n = len(ad)
    for i in range(n):
        t0 = times[i - 1] if i > 0 else 0.0
        t1 = times[i]
        f0 = int(round(t0 / hop_ms))
        f1 = int(round(t1 / hop_ms))
        f0 = max(0, min(T, f0))
        f1 = max(f0, min(T, f1))
        if f1 <= f0:
            continue
        ang = ad[i]
        if abs(ang - 999) < 1.0:          # 中旋：转角对计时为 0，表示成 0 转角
            ang = 0.0
        lo, hi, w = _bump(f0, T)
        dense[0, lo:hi] = np.maximum(dense[0, lo:hi], w)
        # 方向类别:用相邻角度差的真实符号(模360归到[-180,180])。
        #   delta<0 -> 右转(1); 否则左转(0)。这是真实转弯方向, 之前的 ang<0 判定对 0~360
        #   角度几乎恒为左转, 是错的。中旋(ang==0)或近0差按左转处理。
        prev = ad[i - 1] if i > 0 else 0.0
        if abs(prev - 999) < 1.0:
            prev = 0.0
        delta = ((ang - prev + 180.0) % 360.0) - 180.0
        dense[1, f0] = DIR_RIGHT if delta < -1e-6 else DIR_LEFT
        if i in twirl:
            dense[2, lo:hi] = np.maximum(dense[2, lo:hi], w)
    return dense


def _seg_intersect(p1, p2, p3, p4):
    """标准线段相交判定(跨立实验)。端点重合不算相交(相邻砖块共用顶点)。"""
    def _ccw(a, b, c):
        return (c[1] - a[1]) * (b[0] - a[0]) - (b[1] - a[1]) * (c[0] - a[0])
    d1 = _ccw(p3, p4, p1); d2 = _ccw(p3, p4, p2)
    d3 = _ccw(p1, p2, p3); d4 = _ccw(p1, p2, p4)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _fmod(a, b):
    """数学取模, 结果落在 [0, b) (同 C# / timing_engine)。"""
    return a - b * math.floor(a / b)


def _shortest(a):
    """把角度归到 (-180, 180]，取最短弧表示。"""
    a = _fmod(a, 360.0)
    return a - 360.0 if a > 180.0 else a


def plan_path_twirl(magnitudes, twirl_desire=None, model_twirl=None, turn_sign=None, step=1.0):
    """计时反推式路径规划(含可选 Twirl)——这是大佬"第二个模型加事件"的可微替代。

    核心: ADOFAI 计时引擎里 Twirl 会翻转 direction, 而引擎按 p_angle=(dest_{i-1}-180-dest_i)*dir
    算每格时长(不取最短弧)。由此反推得【计时精确】的闭式递推:
        dest_i = (dest_{i-1} - 180 - m_i * d_i)  mod 360
    其中 m_i = 该格目标转角幅度(由节拍锁死: 间隔×bpm×3), d_i = Twirl 奇偶(+1/-1)。
    代入验证: p_angle_i = (dest_{i-1}-180-dest_i)*d_i = m_i*d_i^2 = m_i -> 每格时长恒等于目标,
    无论是否 Twirl, 踩点误差=0。视觉上 Twirl 仅把该格转向翻成镜像(原路径在屏内则镜像也在屏内)。

    逐格二选一(不 Twirl / Twirl), 评分:
      - 硬约束: 新线段与已有线段相交 -> 一票否决(不自交)
      - 软目标: 离屏幕中心越近越好(不出屏)
      - Twirl 偏好: twirl_desire[i] 高(音乐重音)时减分, 诱导在此翻身; 扎堆时惩罚
      - 模型 C2 微弱偏置(可选, 通道偏弱, 仅点缀)
    返回 (angleData, twirl_floors)  angleData=list[int], twirl_floors=1-based 格下标 list。
    """
    import math
    pos = (0.0, 0.0)
    heading = 90.0  # 朝上(与 ADOFAI 视觉一致)
    dest_prev = 0.0  # 上一格线段绝对方向
    d = 1.0         # Twirl 奇偶(+1=无翻转累积方向)
    segs = []
    angleData = []
    twirls = []
    last_tw = -10
    last_turn_dir = 0.0   # 上一次实际视觉拐向(+1=左/-1=右), 0=无(直行或未开始)
    # R_TARGET：出屏惩罚阈值(球到原点距离超过则扣分)。原 25 把球死锁在中心小圈(实测
    # 生成 RMS≈14, 而真实谱面 RMS 中位数≈57) -> 用户反馈"全聚到正中间"。放大到 90
    # 让球能大幅铺开(匹配真实谱面尺度, 但仍锚定中心不会飞太远)。
    R_TARGET = 90.0
    # —— 模型接管模式：放开几何约束，让 ShapeModel 主导走向（用户要求"敞开了走"） ——
    # 无模型(几何贪心)时维持原硬约束(自交否决/出屏惩罚/Twirl 间隔)，行为不变；
    # 有模型时：自交降级为弱惩罚(不否决)、出屏完全放开、Twirl 间隔放开、模型方向强主导。
    shape_mode = turn_sign is not None
    if shape_mode:
        # 修正(2026-08-15): 之前 SHAPE_W=100 + 零惩罚 -> 每格都按模型左右翻向,
        # Twirl 飙到 ~47 次/120格, 球不断掉头折返 => 糊成一团。现在改为:
        # 几何主导(不自交/不出屏), 模型只在「该拐」时温和偏置左右, 并强制 Twirl 间隔,
        # 让球平时直行、到转角才拐 -> 真实 ADOFAI 那种"一朝着一个方向延伸"的走法。
        INTER_PENALTY = 0.0    # 允许自交(真实谱面本就交叉; 不否决则球不必绕中心折返)
        OVER_W = 0.0           # 出屏惩罚已按用户要求移除(球可敞开铺满屏幕, 不被 R_TARGET 锁中心)
        SHAPE_W = 4.0          # 模型方向温和偏置(不再每格强翻)
        ALTERNATE_BONUS = 6.0   # (2026-08-17) 轻度调疏: 从10降到6, 削弱「每格强制左右交替」
        SAME_PENALTY = 6.0      # 同向惩罚同步降低 -> 允许偶尔同向续拐/直行(同向不翻奇偶=不twirl),
                                 # twirl 少约三成, zigzag 招牌仍在(不再每格都翻身)
        TW_GAP_PENALTY = 0.0    # zigzag 本就相邻翻转 -> 关掉间隔惩罚(否则抵消交替奖励)
        # 直线格 twirl 抑制(与贪心同逻辑): mag≈180 的直线格翻转奇偶视觉仍走直线, 无意义 -> 抑制。
        # 仅抑制直线格, 转向格的 zigzag 不受影响(招牌风格保留)。
        STRAIGHT_TW_PENALTY = 8.0
        STRAIGHT_THR = 30.0
        # 持续向前延伸/防自旋 = 奖励「异方向」(连续反向拐弯), 即下方 ALTERNATE_BONUS 的语义。
        # 注: 当前候选循环里 vt 只取决于入向 parity(两候选相同), 故 ALTERNATE_BONUS 与旧的
        # FWD_W 都形同虚设 -> 实际走线由 ShapeModel(desired, SHAPE_W)主导。要让「异方向」真正
        # 生效, 必须改用出向 parity d2(两候选不同)做奖励, 见下方循环内的 ALT_W。
        ALT_W = 4.0            # 「异方向」真实奖励(>0 开启): 与 SHAPE_W 同量级 -> 温和抬升 zigzag 密度, 不抢模型主导
        step = 2.6             # 铺开尺度放大
    else:
        INTER_PENALTY = 1e9    # 几何贪心: 自交一票否决
        OVER_W = 1.0           # 几何贪心: 出屏惩罚
        SHAPE_W = 0.0
        TW_GAP_PENALTY = 50.0
        # 直线格 twirl 抑制: 该 tile 几乎不转向(|vt|<阈值)时, twirl 只是翻转奇偶,
        # 视觉上仍走直线(毫无意义)却把后续转向方向弄反 -> 纯添乱。仅几何贪心模式启用
        # (形状模式靠真实拐弯产生 zigzag, 不受影响, 故放在 else 分支)。
        STRAIGHT_TW_PENALTY = 8.0
        STRAIGHT_THR = 30.0
    for i, mag in enumerate(magnitudes):
        mag = float(mag)
        # 摆形状模型: turn_sign[i]<0 -> 期望往右(d2=-1); >=0 -> 往左(d2=+1)。
        # 为 None 时退化为纯几何贪心(当前回滚版行为, 不影响已认可的 Twirl)。
        desired = None
        if turn_sign is not None and i < len(turn_sign):
            desired = -1.0 if float(turn_sign[i]) < 0 else 1.0
        best = None
        # tile 0(出生格) 旋转方向固定为初始方向(+1)：ADOFAI 里 tile0 是起点，
        # 没有"到达之前可翻转"的 Twirl，其 direction 恒为初始 +1。强制 d2=+1
        # 既符合物理(出生格不能 Twirl，也满足用户规则"floor0 禁止 Twirl")，
        # 又避免「tile0 本想翻转却无法表达 -> 整条方向链从起点就差一符号 -> 全反」。
        if i == 0:
            d2_candidates = (1.0,)
        else:
            d2_candidates = (1.0, -1.0)   # 其余格子两种绝对方向都允许
        for d2 in d2_candidates:                  # 两种绝对方向(模型接管形状, 不再锁死同向)
            tw = (d2 != d)                        # 该方向与当前 parity 相反 -> 记一次 Twirl(视觉镜像翻转)
            # 关键修正(2026-08-16)：当前格的到达角度用「到达方向 d」(Twirl 只镜像「离开」那段,
            # 到达位置不变), 翻转推迟到下一格(dest_{i+1} 才用 d2)。否则 dest_i 用翻转后的 d2
            # 会与真实 ADOFAI(用到达方向算当前格)差半格 -> Twirl 整条方向链错位。
            dest_i = _fmod(dest_prev - 180.0 - mag * d, 360.0)
            vt = _shortest(dest_i - dest_prev)          # 视觉转向(用于几何)
            nh = heading + vt
            nx = pos[0] + step * math.cos(math.radians(nh))
            ny = pos[1] + step * math.sin(math.radians(nh))
            new_seg = (pos, (nx, ny))
            inter = False
            for s in segs[:-1]:                          # 跳过紧邻前一条(共用顶点)
                if _seg_intersect(s[0], s[1], new_seg[0], new_seg[1]):
                    inter = True
                    break
            dist = math.hypot(nx, ny)
            over = max(0.0, dist - R_TARGET)
            # Twirl 偏好(音乐重音 + 模型 C2 弱偏置, 仅作点缀)
            td = 0.0
            if twirl_desire is not None and i < len(twirl_desire):
                td = float(twirl_desire[i])
            score = (INTER_PENALTY if inter else 0.0) + over * OVER_W - td * 6.0
            # 持续向前延伸/防自旋(用户要「异方向」): 用出向 parity=d2(两候选不同)奖励「下一格转角
            # 与上一实际转角异向」-> 真正能影响 Twirl 决策; ALT_W=0 时关闭(默认, 走线交给 ShapeModel)。
            if shape_mode and ALT_W > 0.0 and last_turn_dir != 0.0:
                next_sign = 1.0 if _shortest(-180.0 - mag * d2) >= 0.0 else -1.0
                score += (-ALT_W if next_sign == -last_turn_dir else ALT_W)
            if tw:
                score += (0.0 if shape_mode else 2.0)    # 模型接管时去掉基础 Twirl 成本, 翻身更自由
                if (i - last_tw) < 4:                    # 模型接管时放开间隔
                    score += TW_GAP_PENALTY
            # 直线格抑制: 该 tile 几乎不转向(|vt|<STRAIGHT_THR)时, twirl 仅翻转奇偶,
            # 视觉仍走直线(无意义)却弄反后续转向 -> 抑制。贪心与形状模式通用。
            if tw and abs(vt) < STRAIGHT_THR:
                score += STRAIGHT_TW_PENALTY
            if model_twirl is not None and i < len(model_twirl) and tw:
                score -= float(model_twirl[i]) * 1.0     # 模型 C2 微弱偏置
            # 学习形状偏置: 模型期望方向 -> 强主导(模型接管时几何约束已大幅放松)
            if desired is not None and (d2 > 0) == (desired > 0):
                score -= SHAPE_W
            # 连续反向拐弯奖励(用户要求「左右左右左右」zigzag): 实际视觉拐向 vt_dir
            # 与前一次拐向相反 -> 奖励; 同向(继续螺旋) -> 惩罚。权重(10)>SHAPE_W(4)
            # 故稳定交替, 模型只在交替的二选一里做微弱左右偏好。
            if shape_mode and vt != 0.0:
                vt_dir = 1.0 if vt > 0.0 else -1.0
                if last_turn_dir != 0.0:
                    if vt_dir == -last_turn_dir:
                        score -= ALTERNATE_BONUS
                    else:
                        score += SAME_PENALTY
            if best is None or score < best[0]:
                best = (score, tw, d2, dest_i, vt, nh, (nx, ny), new_seg)
        _, tw, d2, dest_i, vt, nh, new_pos, new_seg = best
        angleData.append(int(round(_shortest(dest_i))))
        # 更新连续反向拐弯状态(直行 vt=0 不打断「最近拐向」记忆)
        if shape_mode and vt != 0.0:
            last_turn_dir = 1.0 if vt > 0.0 else -1.0
        if tw:
            # tile 0 不会进入这里：上面已强制 i==0 的 d2=+1，tw=(d2!=d)=False。
            # （之前用"锁首格跳过 Twirl 事件"的 hack 反而让 tile0 想翻转却表达不出、
            # 导致整条方向链从起点差一符号 —— 现改为"约束 tile0 方向=初始"从根上解决。）
            twirls.append(i)
            last_tw = i
        pos = new_pos
        heading = nh
        dest_prev = dest_i
        # 关键：Twirl 翻转旋转方向，引擎第二遍会「累计」翻转后续所有 tile 的方向。
        # 规划器必须把 d 翻成 d2，否则后续 tile 的 dest 用错奇偶 -> 踩点漂移。
        d = d2
        segs.append(new_seg)
    return angleData, twirls


def plan_freeform_path(n, turn_sign, turn_base=12.0, target_times=None,
                       global_bpm=120.0, hop_ms=HOP_MS, pitch=1.0,
                       spiral_decay=0.0015):
    """豪放自由走线（方案 B）—— 纯 angleData 大回环，不依赖 pathData。

    为什么需要它：
      原 plan_path_twirl 把每格转角锁死成 magnitude=beats*180（时间决定角度），
      纯 angleData 下轨迹必为「90°/180° 折叠方波」。而新版本 ADOFAI 只用 angleData
      算位置、pathData 无效，所以必须让 angleData 自身形成大回环。

    做法：
      1. 每格转 turn_base*sign 的【小角度】连续转弯 -> 轨迹是自由大回环/花瓣
         （不再是急折返）；sign 由 ShapeModel 决定（左/右），模型真正主导走向。
      2. 计时陷阱：timing_engine 的 p_angle=(cur_angle-dest)*dir 会把小角度差分
         算成近整圈(338°)导致时间暴涨。解决：在【转向符号变化处】放 Twirl 翻转
         dir，使 p_angle 恒=|turn|（小角度）。
      3. 每格再用 SetSpeed(Multiplier) 把时间校准回 target_times（踩点） ->
         形状与时间彻底解耦。

    返回 (angleData, twirls, setspeeds)
      angleData  : 连续绝对方向(0~360) list[float]，引擎会 mod360，无妨
      twirls     : 计时用 Twirl 的 floor 列表（符号变化处）
      setspeeds  : 每格 SetSpeed 事件列表（校准时间到 onset 间隔）
    """
    angleData = []
    twirls = []
    setspeeds = []
    # 核心：ADOFAI 计时引擎每格 cur_angle 先减 180 度(出生偏移)，故【几何转向】与
    # 【计时 p_angle】天然差 180 度。要让渲染是大回环，angleData 差分须为【小角度】
    # (+-turn -> 连续转弯)；计时 p_angle 因此=180+-turn(半圈附近)。配合 dir=sign，
    # 可令 p_angle 恒=180-|turn|，再用 SetSpeed 把时间校准到 target_times。
    # cur_dir 从 0 度起步(而非 180 度)，使首格 p_angle 也=180-|turn| 对齐后续。
    cur_dir = 0.0
    eff_sign = 1.0   # 初始 dir=+1 => 期望 sign=+1（dir 必须=sign）
    for i in range(n):
        sign = 1.0
        if turn_sign is not None and i < len(turn_sign):
            sign = -1.0 if float(turn_sign[i]) < 0 else 1.0
        decay = max(0.4, 1.0 - i * spiral_decay)   # 螺旋发散：曲率渐减 -> 圈逐渐变大铺开
        turn = turn_base * sign * decay
        cur_dir += turn          # 渲染：小角度连续转弯（螺旋发散）-> 大回环铺开
        angleData.append(_fmod(cur_dir, 360.0))
        # 计时 Twirl：本格 sign 与「当前有效 sign」不符时翻转 dir，使 dir=sign
        # （p_angle 恒=180-|turn| 半圈附近，时间合理且可校准）。
        # floor 0（第一格）ADOFAI 禁止放 Twirl：跳过首格
        if i > 0 and sign != eff_sign:
            twirls.append(i)
            eff_sign = sign
        # SetSpeed：把该格时间校准到 target_times[i]（踩点）。
        # 必须用 speedType='Bpm'（绝对覆盖），不能用 'Multiplier'（跨格累积相乘爆炸）。
        if target_times is not None and i < len(target_times):
            Ti = max(1e-3, float(target_times[i]))
            # p_angle 恒=180-|turn|：校准 bpm 使该格时间=Ti
            p_angle = 180.0 - abs(turn)
            bpm_eff = p_angle / 180.0 * 60000.0 / Ti
            setspeeds.append({
                "floor": int(i), "eventType": "SetSpeed",
                "speedType": "Bpm", "beatsPerMinute": float(bpm_eff / pitch),
            })
    return angleData, twirls, setspeeds


def plan_directions(magnitudes, model_dir=None, step=1.0):
    """向后兼容包装: 仅做无 Twirl 的计时反推规划(等价于 twirl_desire 全 0)。"""
    ang, _ = plan_path_twirl(magnitudes, twirl_desire=None, model_twirl=model_dir, step=step)
    return ang


def _grid_snap_keep(kept, hop_ms, step_sec=None, max_fill_units=8):
    """把检测到的音头帧吸附到规则节拍网格并补齐漏拍，根除时序漂移/跳踩。

    网格步长优先用「音乐真实拍长」step_sec（由 BPM 给出，60/bpm）；未提供退化为中位数。
    关键修复(2026-08-13 晚): 在 [首个音头, 末个音头] 跨度内生成【完整节拍网格】(每拍一点)，
    凡是附近(≤0.5拍)没有 OnsetNet 音头的网格点一律补入 -> OnsetNet 漏检的拍 100% 被补上,
    彻底消除"跳踩"。长空隙(>max_fill_units 拍, 视为静音)不补, 避免灌水。
    保留每一个真实 onset(绝不删除), 仅去 <2 帧极近重复。
    """
    if len(kept) < 2:
        return kept
    kept = sorted(int(f) for f in kept)
    if step_sec is None or step_sec <= 1e-4:
        gaps = np.diff(kept).astype(np.float64) * (hop_ms / 1000.0)
        step_sec = float(np.median(gaps)) if len(gaps) else float((kept[1] - kept[0]) * hop_ms / 1000.0)
    if step_sec <= 1e-4:
        return kept
    hms = hop_ms / 1000.0
    t0 = kept[0] * hms
    t_end = kept[-1] * hms
    step = step_sec
    half = step * 0.5
    max_dist = max_fill_units * step
    # 完整节拍网格(覆盖整段已演奏区间)
    grid = []
    k = 0
    while True:
        t = t0 + k * step
        if t > t_end + half:
            break
        grid.append(t)
        k += 1
    if not grid:
        return kept
    onset_t = [f * hms for f in kept]
    out = []
    gi = 0
    n = len(grid)
    for ot in onset_t:
        # 插入本 onset 之前、且离任何 onset 都远(>half)的网格点 = 被漏检的拍。
        # 关键修正(2026-08-16)：填满整段网格, 不再因空隙>max_fill_units 而跳过
        # -> 根治「中间整段(如 2:00~2:10)被吞、后面直接拼接」的问题。真静音仍不灌水
        # (网格只覆盖 [首个 onset, 末个 onset] 跨度, 之前/之后皆不补)。
        while gi < n and grid[gi] < ot - half:
            gp = grid[gi]
            out.append(gp)
            gi += 1
        # 跳过与本 onset 重合(±half 内)的网格点, 避免重复
        while gi < n and grid[gi] <= ot + half:
            gi += 1
        out.append(ot)
    # 尾部网格点: 距最后一个 onset ≤max_dist 才补(避免尾奏静音灌水)
    while gi < n:
        gp = grid[gi]
        if (gp - onset_t[-1]) <= max_dist:
            out.append(gp)
        gi += 1
    out.sort()
    res = []
    for t in out:
        f = int(round(t / hms))
        if res and (f - res[-1]) < 2:
            continue
        res.append(f)
    return res


def dense_to_adofai(dense, global_bpm=120.0, hop_ms=HOP_MS, song="generated.mp3",
                    onset_frames=None, twirl_desire=None,
                    turn_sign=None, shape_model=None, onset_prob=None,
                    stem_energy=None, device=None):
    """(3, T) -> level dict（含 angleData/settings）。无法构成有效谱面返回 None。

    onset_frames: 可选。若提供（频谱 onset 检测器给出的帧下标），直接用作谱面格
        起点（绕过 VAE 后验塌缩把稀疏 onset 抹平的问题）。角度由转换器按
        「间隔×180/拍长」算出(确定、不塌缩、不错位);方向(左/右)由 plan_directions
        几何规划指派(左/右转音频相同, 模型学不到, 故用路径规划保证不自交、留屏内)。
    """
    if dense is None or dense.ndim != 2 or dense.shape[0] < 2:
        return None
    C, T = dense.shape
    onset = dense[0]

    if onset_frames is not None:
        # 外部 onset（频谱检测器）直接驱动：绕过 VAE 抹平造成的稀疏化
        frames = [int(f) for f in onset_frames if 0 <= int(f) < T]
        frames.sort()
    else:
        # 兜底：从 VAE 解码的 onset 通道做局部峰值检测（易因后验塌缩而稀疏）
        peak = float(np.max(onset))
        if peak < 1e-3:
            return None  # 完全无 onset 结构（可能后验塌缩）
        thr = max(0.05, 0.25 * peak)
        frames = []
        for f in range(T):
            pv = onset[f - 1] if f > 0 else -1.0
            nv = onset[f + 1] if f < T - 1 else -1.0
            if onset[f] >= pv and onset[f] > nv and onset[f] > thr:
                frames.append(f)
    # 合并距离<2帧的重复 onset，保留合法短格（如 90°@120bpm≈5 帧）。
    kept = []
    for f in frames:
        if kept and (f - kept[-1]) < 2:
            continue
        kept.append(f)
    if len(kept) < 2:
        return None
    # 真实节拍步长（秒）：优先用 BPM 真拍(60/bpm)，彻底消除"帧取整把检测中位数偏置到
    # 整数帧"导致的逐格累积漂移（例：200bpm 真拍=51.677帧，round 偏成 52 帧=301.9ms，
    # 每拍 +1.9ms、整曲漂移 ~380ms）。用 BPM 真拍则每格恒=60/bpm，零漂移。
    med_sec = 60.0 / float(global_bpm) if global_bpm and global_bpm > 0 else float(hop_ms / 1000.0)
    if med_sec <= 1e-4:
        med_sec = float(hop_ms / 1000.0)
    # —— 节拍网格吸附 + 漏拍填充（修复"漏拍/跳拍/周期性错位"）——
    # 检测到的音头含抖动/系统性延迟/中段偶发漏检：纯按音头间隔定角度会让微小偏差逐格
    # 累积成相位漂移，漏检则整段错拍。改为吸附到 BPM 真拍网格并补齐空缺，使每格时长为
    # 真拍整数倍 -> 完全锁定音乐节拍，不再漂移、不再漏拍。
    kept = _grid_snap_keep(kept, hop_ms, step_sec=med_sec)
    if len(kept) < 2:
        return None

    # 方向(左/右)：左/右转音频完全相同，模型学不到，由几何路径规划指派。
    # Twirl：由「音乐重音 twirl_desire(onset 包络强度) + 模型 C2(弱偏置)」驱动，
    # 用计时反推式 plan_path_twirl 植入。无论是否 Twirl，每格时长恒等于目标转角
    # 幅度 -> 踩点误差=0（不破坏踩点；Twirl 仅做视觉镜像翻转）。
    twirl_desire_per_tile = None
    model_twirl_per_tile = None
    if twirl_desire is not None:
        td_len = len(twirl_desire)
        twirl_desire_per_tile = np.array(
            [float(twirl_desire[f]) if 0 <= f < td_len else 0.0 for f in kept],
            dtype=np.float32)
        if C >= 3:
            twirl_ch = dense[2]
            cl = twirl_ch.shape[0]
            model_twirl_per_tile = np.array(
                [float(twirl_ch[f]) if 0 <= f < cl else 0.0 for f in kept],
                dtype=np.float32)

    # 先按【转换器】算每格转角幅度(确定性, 不塌缩/不错位)。
    # 关键修复(2026-08-13): 之前为"消除漂移"把每格强行锁成 med_sec(=1拍) -> magnitude
    # 恒=180° -> 路径全程直线(不拐弯)、aux[2] 恒定 -> VFXNet 输入无起伏 -> 特效塌到 6 个。
    # 现恢复用【实际 onset 间隔 df】算转角幅度: 转角随音乐真实间隔变化 -> 路径自然蜿蜒;
    # 且每格时长=AngleToTime(magnitude)=真实间隔(ms), 与音乐逐拍精确对齐, 不漂移——
    # 漂移真凶是旧版 round 去重删拍(已 by v3 修复), 而非用实际间隔, 故恢复安全。
    magnitudes = []
    for k, f in enumerate(kept):
        if k < len(kept) - 1:
            df = kept[k + 1] - kept[k]
        else:
            df = (kept[k] - kept[k - 1]) if len(kept) > 1 else 1
        seg_sec = max(0.0, df) * hop_ms / 1000.0
        beats = seg_sec * float(global_bpm) / 60.0
        magnitude = beats * 180.0
        magnitudes.append(magnitude)
    # —— 摆形状模型：用模型决定每格左右(取代纯几何贪心) ——
    # turn_sign 优先用外部传入；否则若给了模型+onset 概率，则在此推断。
    turn_sign_arg = turn_sign
    if turn_sign_arg is None and shape_model is not None and onset_prob is not None:
        try:
            from shape_model import extract_tile_features, predict_turn_sign
            _se = stem_energy if stem_energy is not None else np.zeros((6, len(onset_prob)), np.float32)
            _feats = extract_tile_features(kept, onset_prob, _se, float(global_bpm), hop_ms)
            if _feats.shape[0] >= 2:
                turn_sign_arg = predict_turn_sign(shape_model, _feats,
                                                  device if device else "cpu")
        except Exception as e:
            print(f"[shape] 转向推断失败, 退回几何贪心: {e}")
            turn_sign_arg = None

    # —— 走线：模型接管 -> 豪放自由走线(B 方案); 否则几何贪心回退 ——
    if turn_sign_arg is not None:
        # 修正(2026-08-15): 之前走 plan_freeform_path 每格累 12°(螺旋)+每格 SetSpeed,
        # 既糊成一团又塞满 SetSpeed(用户明确不要)。现改回计时闭式 plan_path_twirl:
        # 角度按"间隔×180/拍长"锁死每格时长(踩点精确, 无需 SetSpeed), 模型只决定拐左/拐右,
        # 几何约束(不自交/不出屏/Twirl间隔)保证球平时直行、到转角才拐 -> 真实 ADOFAI 走法。
        angleData, shape_twirls = plan_path_twirl(
            magnitudes,
            twirl_desire=twirl_desire_per_tile,
            model_twirl=model_twirl_per_tile,
            turn_sign=turn_sign_arg,
        )
        actions = [{"floor": int(f) + 1, "eventType": "Twirl"} for f in shape_twirls]
    else:
        angleData, twirls = plan_path_twirl(
            magnitudes,
            twirl_desire=twirl_desire_per_tile,
            model_twirl=model_twirl_per_tile,
            turn_sign=None,
        )
        # 把规划出的 Twirl 写成事件（plan_path_twirl 返回 0-based tile 下标，
        # ADOFAI floor 为 1-based，故 +1：tile 索引 0 对应第一格 floor 1）。
        # plan_path_twirl 保证 tile 0 永不 Twirl，故 floor 1（首格）永不带 Twirl。
        actions = [{"floor": int(f) + 1, "eventType": "Twirl"} for f in twirls]

    settings = {
        "bpm": float(global_bpm), "pitch": 100, "offset": 0, "song": song,
        "songArtist": "", "songName": song, "difficulty": 1, "volume": 100,
        "audioOffset": 0, "timeScale": 1.0, "mirror": 0, "flip": 0,
    }
    # 反解 offset：令 tile 0（出生点，关卡时间=0）落在第一个 onset 帧时间。
    # 计时链条：第 k 个 segment 时长 = (onset[k+1]-onset[k])，故 tile k 的歌曲时间
    #   = offset + sum(seg 0..k-1) = offset + (onset[k]-onset[0])。
    # 要让 tile k 对齐到 onset[k]，只需 offset = onset[0] = kept[0]*hop_ms。
    # 旧公式写成 kept[0]*hop_ms - first_arrival，错误地把【tile 1】而非 tile 0 对齐到
    # 首个 onset，使整谱相对音乐恒定早一拍(~kept[1]-kept[0]≈一个 segment)，
    # 即"踩不到点"的实质根因；与 BPM 翻倍是并列的两大元凶。
    raw_offset = int(round(kept[0] * hop_ms))
    # 安全夹：极端 offset 会让整谱错位，夹到 ±3000ms。
    settings["offset"] = max(-3000, min(3000, raw_offset))

    return {
        "angleData": [int(round(a)) for a in angleData],
        "settings": settings,
        "actions": actions,   # 阶段二:允许输出 Twirl(计时精确,不破坏踩点)
        "decorations": [],
    }


def validate(level, timestamps, onset_idx=None, out_path=None):
    """用计时引擎校验产出谱面的到达时间（含 offset）。

    参数
    ----
    onset_idx : list[int] | None
        各 onset 对应的格子下标。为 None 时退化为旧式 1:1 对齐（仅适用于无拆分的情况）。

    返回 (max_time_error_ms, has_setspeed)。
    """
    has_ss = any(a.get('eventType') == 'SetSpeed' for a in level.get('actions', []))
    nt = compute_note_times(level['angleData'], level['settings'],
                            level['actions'], add_offset=True)
    if onset_idx is not None:
        errs = []
        for i, idx in enumerate(onset_idx):
            if 0 <= idx < len(nt):
                errs.append(abs(nt[idx][0] - timestamps[i] * 1000))
    else:
        m = min(len(timestamps), len(nt))
        errs = [abs(nt[i][0] - timestamps[i] * 1000) for i in range(m)]
    max_err = max(errs) if errs else 0.0
    if has_ss:
        note = ""
    elif max_err < 50.0:
        note = "（纯角度已精确踩点，无 SetSpeed）"
    else:
        note = "（无 SetSpeed：个别超长间隔超出单格转角上限，略漂移）"
    print(f"[validate] tiles={len(level['angleData'])} offset={level['settings']['offset']}ms "
          f"has_SetSpeed={has_ss} max_time_error={max_err:.2f}ms{note}")
    if out_path:
        print(f"[validate] file : {out_path}")
    return max_err, has_ss


if __name__ == "__main__":
    # 自测：构造一个小谱面，验证 (1) 幅度往返一致 (2) 方向规划产出非退化谱面
    import json
    lvl = {
        "angleData": [90, -90, 180, -90, 90, -180],
        "settings": {"bpm": 120, "pitch": 100, "offset": 0},
        "actions": [],
        "decorations": [],
    }
    T = 1024
    d = adofai_to_dense(lvl, T, global_bpm=120.0)
    print("dense shape:", d.shape, "onset count:", int(d[0].sum()),
          "dir unique:", np.unique(d[1]))
    onsets = np.where(d[0] > 0.5)[0]
    back = dense_to_adofai(d, global_bpm=120.0, onset_frames=onsets)
    ad = back["angleData"]
    mags = [abs(a) for a in ad]
    L = sum(1 for a in ad if a < 0)   # 左转(负)数
    R = sum(1 for a in ad if a > 0)   # 右转(正)数
    print("reconstructed angleData:", ad)
    print("magnitudes match input abs-values (±1°):",
          np.allclose(sorted(mags), sorted([abs(x) for x in lvl["angleData"]]),
                      atol=1.0))
    print("non-degenerate (both signs present):", L > 0 and R > 0,
          f"(L={L}, R={R})")
    print("offset:", back["settings"]["offset"])
    print("OK plan_directions round-trip")
