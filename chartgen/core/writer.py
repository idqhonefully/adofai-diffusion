"""写盘层：Chart -> .adofai (JSON, version 19)。

`settings` 模板照抄**游戏本体导出的 v19 谱面**的字段结构
（游戏目录 `Documents/A Dance of Fire and Ice/Worlds/<任意关卡>/main.adofai`），
保证游戏能直接加载。
"""
from __future__ import annotations

import json
import math
import os

from .solve import Chart

# 与真实 v19 谱面同构的 settings 模板
SETTINGS_TEMPLATE = {
    "version": 19,
    "artist": "",
    "specialArtistType": "None",
    "artistPermission": "",
    "song": "",
    "author": "",
    "separateCountdownTime": False,
    "previewImage": "",
    "previewIcon": "",
    "previewIconColor": "ffffff",
    "previewSongStart": 0,
    "previewSongDuration": 10,
    "seizureWarning": False,
    "levelDesc": "",
    "levelTags": "",
    "artistLinks": "",
    "speedTrialAim": 0.0,
    "difficulty": 0,
    "requiredMods": [],
    "songFilename": "",
    "bpm": 180,
    "volume": 100,
    "offset": 0,
    "pitch": 100,
    "hitsound": "Kick",
    "hitsoundVolume": 25,
    "countdownTicks": 1,
    "tileShape": "Long",
    "trackColorType": "Single",
    "trackColor": "debb7b",
    "secondaryTrackColor": "ffffff",
    "trackColorAnimDuration": 2,
    "trackColorPulse": "None",
    "trackPulseLength": 10,
    "trackStyle": "Standard",
    "trackTexture": "",
    "trackTextureScale": 1,
    "trackGlowIntensity": 100,
    "trackAnimation": "None",
    "beatsAhead": 3,
    "trackDisappearAnimation": "Fade",
    "beatsBehind": 0,
    "backgroundColor": "000000",
    "showDefaultBGIfNoImage": True,
    "showDefaultBGTile": True,
    "defaultBGTileColor": "101121",
    "defaultBGShapeType": "Default",
    "defaultBGShapeColor": "ffffff",
    "bgImage": "",
    "bgImageColor": "ffffff",
    "parallax": [100, 100],
    "bgDisplayMode": "FitToScreen",
    "imageSmoothing": True,
    "lockRot": False,
    "loopBG": False,
    "scalingRatio": 100,
    "relativeTo": "Player",
    "position": [0, 0],
    "rotation": 0,
    "zoom": 200,
    "pulseOnFloor": True,
    "bgVideo": "",
    "loopVideo": False,
    "vidOffset": 0,
    "floorIconOutlines": False,
    "stickToFloors": True,
    "planetEase": "Linear",
    "planetEaseParts": 1,
    "planetEasePartBehavior": "Mirror",
    "defaultTextColor": "ffffff",
    "defaultTextShadowColor": "00000050",
    "congratsText": "",
    "perfectText": "",
    "legacyFlash": False,
    "legacyCamRelativeTo": False,
    "legacySpriteTiles": False,
    "legacyTween": False,
    "disableV15Features": False,
}


# ★★ 导出 settings **白名单**（2026-09-20 主人指定）
#   「从工作台导出的谱子，settings 只包含这些就行，多余的删掉。」
#   ⇒ 只有 `minimal_settings=True`（= `session.export()` 写盘那条路）才过滤，
#     **预览**走 `level_json()`（不带开关）= 与旧版逐字节相同，界面预览不变。
#   · 顺序 = 主人给的顺序，写盘就按这个顺序输出。
#   · `songArtist` 是 v2 的键名、模板里没有 ⇒ 值取自 `artist`（见 build_json）。
#   · 外观（⑤c 算法调度 / 皮肤）要用的键 **全在白名单里** ⇒ 皮肤功能不受影响。
#   · 被删掉的键（version / difficulty / separateCountdownTime / preview* / bg* /
#     zoom / relativeTo / legacy* / requiredMods …）交游戏按自身默认值处理。
EXPORT_SETTINGS_KEYS = (
    "song", "songFilename", "bpm", "volume", "offset", "pitch",
    "hitsound", "hitsoundVolume", "countdownTicks", "tileShape",
    "trackColorType", "trackColor", "secondaryTrackColor",
    "trackColorAnimDuration", "trackColorPulse", "trackPulseLength",
    "trackStyle", "trackTexture", "trackTextureScale", "trackGlowIntensity",
    "trackAnimation", "beatsAhead", "trackDisappearAnimation", "beatsBehind",
    "songArtist",
)

# 白名单里这几个数值键的**写法**：整数值统一写 `2` 而不是 `2.0`
#   （模板给 int、外观计划（⑤c）却写 float ⇒ 不归一化的话同一份 settings 里会 2/2.0 混着出现）
_EXPORT_INT_KEYS = ("offset", "pitch", "volume", "hitsoundVolume", "countdownTicks",
                    "trackPulseLength", "trackTextureScale", "trackGlowIntensity",
                    "trackColorAnimDuration", "beatsAhead", "beatsBehind")


def _num(v: float):
    """角度/数值写法：整数就写整数，否则保留 6 位小数（去掉多余 0）。"""
    if abs(v - round(v)) < 1e-9:
        return int(round(v))
    return round(float(v), 6)


def _bpm(v: float):
    """BPM 写法。

    注意：清洗/吸附已经在 core.solve._pick_bpm 里做过，且 travel 是由**吸附后**的
    BPM 反算的，所以这里只做去噪的 6 位小数，绝不能再次改动数值，
    否则写进文件的 BPM 就和 angleData 不自洽了（会引入毫秒级误差）。
    """
    if abs(v - round(v)) < 1e-9:
        return int(round(v))
    return round(float(v), 6)


def build_actions(ch: Chart, countdown_ticks: int = 4) -> list[dict]:
    """生成 actions：Twirl + SetSpeed（按层序交错）。

    ★ **这里不再擅自挪 Twirl**（2026-10 改）。历史教训：旧版看到「Twirl 与 SetSpeed
      同格」就把 Twirl 往后挪一格，可 **angleData 是按原位置算好的** —— 于是
      反解器少读一次奇偶翻转，下游整段偏 180°，实测雪花段之后时序多 324ms。

      现在「Twirl 不许和 SetSpeed 同格」只在**求解侧**处理（`solve._relocate_twirls`，
      跑在 `_apply_snowflakes` 之后、`_apply` 之前，所以几何会跟着重算）。
      写盘侧只做**忠实输出**，并把撞车的位置记进 `ch.meta["twirl_on_setspeed"]`
      交给 `core.rules` 报错 —— 宁可报错，也不能悄悄改几何。
    """
    twirls = {i for i, f in enumerate(ch.floors) if f.twirl}
    speeds = {i: bpm for i, bpm in ch.set_speed_floors}
    # ★ 重叠闭合图形的提示：逐格 PositionTrack（justThisTile）。
    #   只改渲染位置，不改 angleData / bpm / travel ⇒ 时序不受影响。
    pos = {int(i): (float(dx), float(dy))
           for i, dx, dy in (ch.meta.get("pos_tracks") or [])}
    # ★★ 换手押上色（`docs/59`）：**只写渲染事件**，不碰几何/时序。
    #   由 `sidecar.session` 的「步骤 8b」算好放在 `meta["color_events"]`（见 core.colorize）。
    #   在这里按 floor 合并进主循环 —— 保持整份 actions **按 floor 有序**（真实谱面都这样）。
    color: dict[int, list[dict]] = {}
    for e in (ch.meta.get("color_events") or []):
        color.setdefault(int(e["floor"]), []).append(e)
    # ★★ 算法轨道调度（`docs/60`）：涟漪环 `RecolorTrack` + 半径 `ScaleRadius`。
    #   皮肤那半在 **settings** 层（见 `build_json` 的 `appearance_settings`），这里只管事件。
    app: dict[int, list[dict]] = {}
    for e in (ch.meta.get("appearance_events") or []):
        app.setdefault(int(e["floor"]), []).append(e)
    # ★★ 演出（入场 / 离场，`docs/62`）：`MoveTrack`。
    #   由 `sidecar.session` 的「步骤 8d」算好放在 `meta["show_events"]`（见 `core.show`）。
    #   ★ **按 floor 并进主循环**，并且在**上色/轨道调度之前**写 —— 同格顺序必须是
    #     「入场初态 → 入场回位 → 演出动作 → 上色」（`docs/62 §5.10`）。
    show: dict[int, list[dict]] = {}
    for e in (ch.meta.get("show_events") or []):
        show.setdefault(int(e["floor"]), []).append(e)
    # ★★ 镜头调度（`docs/70` · 用户 2026-10「（new）镜头调度」）：
    #   由 `sidecar.session` 的「步骤 8e」算好放在 `meta["camera_events"]`（见 `core.camera`）。
    #   ★ 放在**最后**：镜头只改"看哪里"，不改"按哪里"；同格顺序 = 演出 → 上色 → 调度 → 镜头。
    cam: dict[int, list[dict]] = {}
    for e in (ch.meta.get("camera_events") or []):
        cam.setdefault(int(e["floor"]), []).append(e)
    n = len(ch.floors)

    on_speed = sorted(twirls & set(speeds))
    ch.meta["twirl_on_setspeed"] = on_speed
    ch.meta["twirl_moved"] = 0
    safe_twirls = twirls

    out: list[dict] = []
    for i in range(n):
        if i in speeds:
            out.append({
                "floor": i,
                "eventType": "SetSpeed",
                "speedType": "Bpm",
                "beatsPerMinute": _bpm(speeds[i]),
                "bpmMultiplier": 1,
                "angleOffset": 0,
            })
        if i in pos:
            dx, dy = pos[i]
            # ★ 字段必须**逐字对齐游戏**（参考：五月雪版 FallenEra 的 37 个
            #   PositionTrack，那份谱能正常加载）：
            #     positionOffset / rotation / scale / opacity / editorOnly
            #   `editorOnly` 是**字符串枚举**（"Disabled"/"Enabled"），不是布尔。
            #   少写 rotation/scale/opacity 会让 LevelEvent.Decode 取到 null 直接
            #   `NullReferenceException`（实测游戏报 "Object reference not set"）。
            #   也不要自己加 relativeTo / justThisTile / ease / duration ——
            #   这个版本不认，加了同样会炸。
            out.append({
                "floor": i,
                "eventType": "PositionTrack",
                "positionOffset": [_num(dx), _num(dy)],
                "rotation": 0,
                "scale": 100,
                "opacity": 100,
                "editorOnly": "Disabled",
            })
        if getattr(ch.floors[i], "pause_beats", 0.0) > 1e-9:
            # Pause：这一格额外停 N 拍（只加时间，不减）
            # ★ 用户 2026-10：「pause_min_beats 默认将等于 4，并且**带计数嘀嗒字段（为 4）**」。
            #   以前写死 `-1`（= 不显示倒计时节拍）。现在跟谱面的 `countdownTicks` 走，
            #   默认 4 —— 两者一致，等待时间里玩家能看到与开局同样的节拍提示。
            out.append({
                "floor": i,
                "eventType": "Pause",
                "duration": _bpm(round(ch.floors[i].pause_beats, 6)),
                "countdownTicks": max(0, int(countdown_ticks)),
                "angleCorrectionDir": -1,
            })
        if i in safe_twirls:
            out.append({"floor": i, "eventType": "Twirl"})
        for e in show.get(i, ()):           # ★ 演出（`docs/62`）—— 必须在 color/app 之前
            out.append(dict(e))
        for e in color.get(i, ()):          # ★ 换手押上色（`docs/59`）
            out.append(dict(e))
        for e in app.get(i, ()):            # ★ 算法轨道调度（`docs/60`）
            out.append(dict(e))
        for e in cam.get(i, ()):            # ★ 镜头调度（`docs/70`）—— 最后，只动相机
            out.append(dict(e))
    # ★ 记账（不许静默）：写了几条、有没有落在越界的 floor 上（越界 = 整条丢弃并报出来）
    _n = sum(len(v) for v in color.values())
    _oor = sorted(k for k in color if k < 0 or k >= n)
    ch.meta["color_written"] = _n - sum(len(color[k]) for k in _oor)
    ch.meta["color_out_of_range"] = _oor
    _an = sum(len(v) for v in app.values())
    _aoor = sorted(k for k in app if k < 0 or k >= n)
    ch.meta["appearance_written"] = _an - sum(len(app[k]) for k in _aoor)
    ch.meta["appearance_out_of_range"] = _aoor
    # ★ 演出记账（不许静默）：同上
    _sn = sum(len(v) for v in show.values())
    _soor = sorted(k for k in show if k < 0 or k >= n)
    ch.meta["show_written"] = _sn - sum(len(show[k]) for k in _soor)
    ch.meta["show_out_of_range"] = _soor
    # ★ 镜头记账（不许静默）：同上
    _cn = sum(len(v) for v in cam.values())
    _coor = sorted(k for k in cam if k < 0 or k >= n)
    ch.meta["camera_written"] = _cn - sum(len(cam[k]) for k in _coor)
    ch.meta["camera_out_of_range"] = _coor
    return out


def build_json(ch: Chart, *, song: str = "", artist: str = "", author: str = "",
               song_filename: str = "", offset_ms: float = 0.0,
               difficulty: int = 0, preview_start: int = 0,
               preview_duration: int = 10, countdown_ticks: int = 4,
               separate_countdown: bool = False,
               track_animation: str = "Fade", beats_ahead: int = 8,
               beats_behind: float = 4,
               minimal_settings: bool = False,
               extra_settings: dict | None = None) -> dict:
    """`track_animation` / `beats_ahead`：轨道出现动画。

    ★ 出现动画提前 **8 拍**：这样 8 拍以内的重叠本来就靠"出现时间差"分开了，
      真正需要 PositionTrack 错位的只有**间隔超过 8 拍**的重合
      （见 `core.track_fx`，那里按拍数门槛过滤）。

    ⚠ v19 的 `trackAnimation` **没有 "Appear"**。真实取值（623 张语料统计）：
      None / Fade / Extend / Grow / Grow_Spin / Assemble / Assemble_Far / Drop
      `trackDisappearAnimation`：None / Fade / Shrink / Retract / Shrink_Spin /
      Scatter / Scatter_Far
      这里默认用 `Fade`（出现侧最常用，语义即"出现/淡入"）。
    """
    s = dict(SETTINGS_TEMPLATE)
    # ★★ 演出（⑤d，`docs/62 §3.7`）：入场提前量 = `lead + margin`，**必须**让
    #   `beatsAhead` 盖住它，否则整套飞行动画会播在**方块出现之前**（方块凭空出现在终点）。
    #   `core.show.plan()` 把需要值报在 `meta["show_beats_ahead"]`，这里取 max —— 只抬不降。
    _need = float(ch.meta.get("show_beats_ahead") or 0.0)
    if _need > float(beats_ahead):
        beats_ahead = int(math.ceil(_need))
    s.update({
        "song": song,
        "artist": artist,
        # ★ v2 的键名：白名单里有它、模板里没有 ⇒ 这里喂进去（导出时 artist 被别人顶掉也不丢作者）
        "songArtist": artist,
        "author": author,
        "songFilename": song_filename,
        "bpm": _bpm(ch.base_bpm),
        "offset": int(round(offset_ms)),
        "difficulty": difficulty,
        "previewSongStart": preview_start,
        "previewSongDuration": preview_duration,
        # 社区约定：countdownTicks = 4
        "countdownTicks": max(0, int(countdown_ticks)),
        "separateCountdownTime": bool(separate_countdown),
        "trackAnimation": str(track_animation),
        "beatsAhead": int(beats_ahead),
        # ★ beatsBehind = 0 在结构上等价于「格子永不消失」：`TimelineManager`
        #   只在 `scaledBeatsBehind > 0` 时才建消失关键帧。语料里 0 占 423/633，
        #   但那样**走过的格子会全部留在画面上**，跟游戏观感不符，所以默认给 4。
        "beatsBehind": float(beats_behind),
    })
    if extra_settings:
        s.update(extra_settings)
    # ★★ 算法轨道调度（`docs/60`）的**皮肤**：只覆盖列出的那几个外观键。
    #   开关关掉时 `meta` 里没有这个键 ⇒ settings 一个字节都不动。
    if ch.meta.get("appearance_settings"):
        s.update(dict(ch.meta["appearance_settings"]))
    if minimal_settings:
        # ★ 只留白名单（缺的键用模板默认值补齐，保证"游戏要什么有什么"）；
        #   字典顺序 = 白名单顺序 ⇒ 写出来的 settings 顺序固定、可 diff。
        s = {k: s.get(k, SETTINGS_TEMPLATE.get(k, "")) for k in EXPORT_SETTINGS_KEYS}
        for _k in _EXPORT_INT_KEYS:
            _v = s.get(_k)
            if isinstance(_v, float) and abs(_v - round(_v)) < 1e-9:
                s[_k] = int(round(_v))
    # ★★ 镜头调度（`docs/70`）的**基准位**：`position / rotation / zoom / relativeTo`。
    #   ★ 必须放在 `minimal_settings` **之后** —— 白名单里没有这几个键，放前面会被削掉，
    #     而镜头事件写的是**绝对位姿**，基准位不对齐 = 整段镜头偏掉。
    #   ★ 只在镜头开着时才有这个键 ⇒ 关着时 settings 一个字节都不动。
    if ch.meta.get("camera_settings"):
        s.update(dict(ch.meta["camera_settings"]))
    return {
        "angleData": [_num(f.angle) for f in ch.floors],
        "settings": s,
        "actions": build_actions(ch, countdown_ticks=int(countdown_ticks)),
        "decorations": [],
    }


def write(ch: Chart, path: str, **kw) -> str:
    data = build_json(ch, **kw)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent="\t")
    return path


def write_dir(ch: Chart, out_dir: str, name: str = "main", audio_src: str | None = None,
              **kw) -> str:
    """按 ADOFAI 的「一个曲目一个目录」结构落盘：<out_dir>/<name>.adofai (+ 音频)。"""
    os.makedirs(out_dir, exist_ok=True)
    if audio_src:
        ext = os.path.splitext(audio_src)[1]
        dst = os.path.join(out_dir, name + ext)
        if os.path.abspath(audio_src) != os.path.abspath(dst):
            import shutil
            shutil.copy2(audio_src, dst)
        kw.setdefault("song_filename", name + ext)
    p = os.path.join(out_dir, name + ".adofai")
    return write(ch, p, **kw)
