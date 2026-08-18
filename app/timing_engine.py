"""
timing_engine.py — ADOFAI 计时的唯一真理源（忠实移植自 SharpFAI LevelUtils.GetNoteTimes）

为什么存在：
- 旧桥接里的 get_note_times 是"行星从 tile0 起步"的简化模型，且漏了 pitch/Twirl/Pause/Hold/
  MultiPlanet/FreeRoam/999(mid-spin)。真实引擎（SharpFAI）里这些全部进计时。
- 这里逐行对应 C# 源码，确保产出的 .adofai 每个 tile 到达时间与输入时间戳精确一致。

覆盖的计时因素（全部来自源码 switch / 设置）：
Settings : bpm, pitch(整数, effective = pitch/100), offset
Actions  : SetSpeed(Multiplier|Bpm), Twirl, Pause, Hold, MultiPlanet, FreeRoam
angleData: 普通值；999 = mid-spin（该格 deltaTime=0）；首格相对出生方向 180°

核心公式（源码行 250-253）：
    AngleToTime(angle, bpm) = angle/180 * (60/bpm) * 1000   (毫秒)
"""

import math


def _fmod(a, b):
    """数学取模，行为同 C# a - b*floor(a/b)，结果落在 [0, b)。"""
    return a - b * math.floor(a / b)


def _angle_to_time(angle, bpm):
    """源码 AngleToTime：angle(度) -> 毫秒。"""
    return angle / 180.0 * (60.0 / bpm) * 1000.0


def _new_chart(angle):
    return {
        'angle': angle, 'bpm': 0.0, 'direction': 0,
        'extraHold': 0.0, 'extraBeats': 0.0,
        'midr': False, 'multiPlanet': -1,
    }


def compute_note_times(angle_data, settings, actions, add_offset=False, return_directions=False):
    """
    忠实移植 SharpFAI LevelUtils.GetNoteTimes。

    参数
    ----
    angle_data : list[float]   每格绝对角；999 = mid-spin（中途旋转格，不耗时）
    settings   : dict          需含 'bpm'(float), 'pitch'(int, 会 /100), 'offset'(int)
    actions    : list[dict]    每个事件含 'eventType','floor' 及事件字段
    add_offset : bool          为 True 时所有时间加上 settings['offset']

    返回
    ----
    list[(time_ms, is_hold)]，长度 = len(angle_data) + 1
      - 索引 0..N-1 对应 tile 0..N-1 的到达时间
      - 索引 N 是尾部哨兵（tile N-1 -> 结束标记）的到达时间，生成时一般不校验
      - 不含 offset 时为"关卡时间"；add_offset=True 时为相对歌曲开头的实际时间
    """
    pitch = float(settings.get('pitch', 100)) / 100.0
    base_bpm = float(settings.get('bpm', 100))
    offset = int(settings.get('offset', 0))

    n = len(angle_data)
    parsed = []
    for i in range(n):
        a = angle_data[i]
        if abs(a - 999) < 0.01:                       # mid-spin
            prev = angle_data[i - 1] if i > 0 else 0.0
            ang = _fmod(prev + 180.0, 360.0)
            p = _new_chart(ang)
            p['midr'] = True
            parsed.append(p)
        else:
            parsed.append(_new_chart(_fmod(a, 360.0)))
    parsed.append(_new_chart(0.0))                    # 尾部哨兵（结束标记）

    # ---- 事件循环：维护一个 running_bpm（行为与 C# 全局 bpm 变量一致）----
    running_bpm = base_bpm
    for ev in actions:
        et = ev.get('eventType')
        fl = ev.get('floor', 0)
        # 与真实 ADOFAI 计时引擎一致：floor F 的 Twirl 翻转的是「离开 F 进入 F+1」
        # 那段的转向，即引擎里索引 F 的 tile 方向，故事件落在 parsed[fl]（0-based）。
        # （2026-08-16 修正：v9 误改成 parsed[fl-1] 导致每个 Twirl 整体错后一格，
        # 表现为「开头旋转多一个/少一个、后面全反」。已还原为 0-based。）
        if not (0 <= fl < len(parsed)):
            continue
        ob = parsed[fl]
        if et == 'SetSpeed':
            if ev.get('speedType', 'Bpm') == 'Multiplier':
                running_bpm = float(ev.get('bpmMultiplier', 1.0)) * running_bpm
            else:   # Bpm
                running_bpm = float(ev.get('beatsPerMinute', base_bpm)) * pitch
            ob['bpm'] = running_bpm
        elif et == 'Twirl':
            ob['direction'] = -1
        elif et == 'Pause':
            ob['extraBeats'] = float(ev.get('duration', 0.0)) / 2.0
        elif et == 'Hold':
            ob['extraHold'] = float(ev.get('duration', 0.0))
            ob['extraBeats'] = float(ev.get('duration', 0.0))
        elif et == 'MultiPlanet':
            pl = str(ev.get('planets', 'TwoPlanets'))
            ob['multiPlanet'] = 1 if 'Three' in pl else 0
        elif et == 'FreeRoam':
            ob['extraBeats'] = (float(ev.get('duration', 1.0)) - 1.0) / 2.0
        # 其余事件（Flash/Shake/相机/装饰 etc.）不影响计时，忽略

    # ---- 第二遍：累计 direction + 解析每格有效 bpm ----
    current_bpm = base_bpm * pitch
    direction = 1
    for ob in parsed:
        if ob['direction'] == -1:
            direction *= -1
        ob['direction'] = direction
        if ob['bpm'] == 0:
            ob['bpm'] = current_bpm
        else:
            current_bpm = ob['bpm']

    # ---- 时间累加 ----
    note_time = []
    directions = []
    cur_angle = 0.0
    cur_time = 0.0
    is_multi = False
    for ob in parsed:
        cur_angle = _fmod(cur_angle - 180.0, 360.0)
        cur_bpm = ob['bpm']
        dest = ob['angle']
        d = _fmod(dest - cur_angle, 360.0)
        if d <= 0.001 or d >= 359.999:
            p_angle = 360.0
        else:
            p_angle = _fmod((cur_angle - dest) * ob['direction'], 360.0)
        p_angle += ob['extraBeats'] * 360.0

        angle_temp = p_angle
        if is_multi:
            p_angle = p_angle - 60.0 if p_angle > 60.0 else p_angle + 300.0
        mp = ob['multiPlanet']
        if mp != -1:
            is_multi = (mp == 1)
            p_angle = (p_angle - 60.0 if p_angle > 60.0 else p_angle + 300.0) if is_multi else angle_temp

        delta = 0.0 if ob['midr'] else _angle_to_time(p_angle, cur_bpm)
        cur_time += delta
        cur_angle = dest
        note_time.append((cur_time, ob['extraHold'] > 0))
        directions.append(ob['direction'])

    if add_offset:
        note_time = [(t + offset, h) for (t, h) in note_time]
    if return_directions:
        return note_time, directions
    return note_time


def solve_offset(angle_data, timestamps, actions, base_bpm=120.0, pitch=100):
    """
    给定 angleData + 每格 SetSpeed + 时间戳，反推让 tile i 恰好落在 timestamps[i] 的 offset。
    忠实于引擎：offset = timestamps[0]*1000 - note_time_level[0]。
    返回 (offset_int, note_time_level_no_offset)
    """
    settings = {'bpm': float(base_bpm), 'pitch': int(pitch), 'offset': 0}
    nt = compute_note_times(angle_data, settings, actions, add_offset=False)
    offset = round(timestamps[0] * 1000 - nt[0][0])
    return offset, nt
