# -*- coding: utf-8 -*-
"""test-minimal-settings.py —— 「导出的 settings 只留白名单」回归测试（零 GUI、秒级）

背景（2026-09-20 主人）：
    「从工作台导出的谱子只包含 settings 字段只包含这些就行，多余的删掉」

口径：
    · `writer.build_json(..., minimal_settings=True)`（= `session.export()` 写盘那条路）
      ⇒ settings **只有** `writer.EXPORT_SETTINGS_KEYS` 里那 25 个键，顺序固定。
    · `level_json()`（界面预览）**不带**开关 ⇒ 与旧版逐字节相同（本测试也钉住这条）。

用法：
    python313/python.exe gui/tools/test-minimal-settings.py
    python313/python.exe gui/tools/test-minimal-settings.py <导出的 main.adofai>   # 可选：验真机产物
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))          # <REPO>
# ★ 默认测我们自己的工作副本；`MINSET_ROOT=<某份 core 的父目录>` 可以指到**交付出去的那份**
#   （给Adofai-Chart-Generator的最小补丁包就是这么自证的：用他包里的文件跑同一套断言）。
PKG = os.environ.get("MINSET_ROOT") or os.path.join(ROOT, "chartgen")
sys.path.insert(0, PKG)

from core import writer                                        # noqa: E402

PASS = FAIL = 0


def chk(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print("%s  %s%s" % ("PASS" if ok else "FAIL", name, ("   [%s]" % detail) if detail else ""))


# ---------------------------------------------------------------- 假 Chart（鸭子类型）
class _Floor(object):
    def __init__(self, twirl=False, pause_beats=0.0, angle=0.0):
        self.twirl = twirl
        self.pause_beats = pause_beats
        self.angle = angle


class _Chart(object):
    def __init__(self, meta=None):
        self.base_bpm = 280.0
        self.floors = [_Floor(), _Floor(), _Floor(True), _Floor()]
        self.set_speed_floors = [(3, 140.0)]
        self.meta = dict(meta or {})


KW = dict(song="Les Apollo - M3_stems_combined", artist="", author="Someone",
          song_filename="Les Apollo - M3.ogg", offset_ms=-110, difficulty=10,
          countdown_ticks=4, separate_countdown=True, beats_ahead=14, beats_behind=2)

WL = list(writer.EXPORT_SETTINGS_KEYS)
print("=" * 76)
print("### 导出 settings 白名单回归（白名单 %d 个键）" % len(WL))
print("=" * 76)

# ---------- A. 导出模式：只有白名单 ----------
full = writer.build_json(_Chart(), **KW)
mini = writer.build_json(_Chart(), minimal_settings=True, **KW)
sf, sm = full["settings"], mini["settings"]

chk("A1 导出模式 settings 键集 == 白名单（一个不多、一个不少）",
    set(sm) == set(WL), "多出=%s 缺少=%s" % (sorted(set(sm) - set(WL)), sorted(set(WL) - set(sm))))
chk("A2 键的**顺序**与白名单一致（写盘可 diff）", list(sm) == WL,
    "实际前 3 = %s" % list(sm)[:3])
chk("A3 真的删掉了多余键", len(sf) > len(sm),
    "旧全量 %d 键 → 现在 %d 键（删掉 %d 个）" % (len(sf), len(sm), len(sf) - len(sm)))
for bad in ("version", "previewImage", "bgImage", "zoom", "requiredMods", "legacyFlash"):
    if bad in sm:
        chk("A4 多余键 %s 已删除" % bad, False, "还在！")
chk("A4 典型多余键全部消失（version/preview*/bg*/zoom/requiredMods/legacy*）",
    not any(k in sm for k in ("version", "previewImage", "previewIcon", "bgImage",
                              "backgroundColor", "zoom", "requiredMods", "legacyFlash")),
    "剩余多余=%s" % [k for k in sm if k not in WL])

# ---------- B. 值没丢 ----------
chk("B1 关键值原样保留", all([
    sm["song"] == KW["song"],
    sm["songFilename"] == KW["song_filename"],
    sm["bpm"] == 280,
    sm["offset"] == -110,
    sm["countdownTicks"] == 4,
    sm["beatsAhead"] == 14,
    float(sm["beatsBehind"]) == 2.0,
]), json.dumps({k: sm[k] for k in ("song", "bpm", "offset", "beatsAhead", "beatsBehind")},
               ensure_ascii=False))
chk("B2 模板兜底的键有值（不是 None/空）",
    sm["volume"] == 100 and sm["pitch"] == 100 and sm["hitsound"] == "Kick"
    and sm["tileShape"] == "Long" and sm["trackPulseLength"] == 10,
    "volume=%r pitch=%r hitsound=%r tileShape=%r" % (sm["volume"], sm["pitch"],
                                                     sm["hitsound"], sm["tileShape"]))

# ---------- C. 作者不丢：artist → songArtist ----------
m2 = writer.build_json(_Chart(), minimal_settings=True, **dict(KW, artist="Plum"))
chk("C1 artist 落到 songArtist（白名单里没有 artist/author 这两个旧键）",
    m2["settings"]["songArtist"] == "Plum" and "artist" not in m2["settings"],
    "songArtist=%r" % m2["settings"]["songArtist"])

# ---------- D. 皮肤（⑤c 算法调度）不会被白名单剪掉 ----------
skin = {"trackStyle": "Neon", "trackColorType": "Glow", "trackColor": "ffffff",
        "secondaryTrackColor": "ffffff", "trackColorPulse": "Forward",
        "trackPulseLength": 10, "trackGlowIntensity": 100, "trackColorAnimDuration": 2}
m3 = writer.build_json(_Chart(meta={"appearance_settings": skin}), minimal_settings=True, **KW)
got = m3["settings"]
chk("D1 皮肤那一组 settings 键全在白名单里（不会被剪）",
    all(got.get(k) == v for k, v in skin.items()),
    "; ".join("%s=%r" % (k, got.get(k)) for k in skin))

# ---------- D2. 数值写法：整数就写整数（跟主人给的目标样式一致） ----------
num_bad = {k: sm[k] for k in ("beatsBehind", "trackColorAnimDuration", "trackPulseLength",
                              "trackGlowIntensity", "hitsoundVolume", "volume", "offset")
           if isinstance(sm[k], float)}
chk("D2 整数值写成 int（不是 2.0 / 4.0）", not num_bad, "仍是 float 的: %s" % num_bad)

# ---------- E. 预览那条路不变 ----------
chk("E1 预览（不带开关）仍是全量 = 与旧版一致", len(sf) == len(writer.SETTINGS_TEMPLATE) + 1
    and "version" in sf and "zoom" in sf,
    "预览键数 %d（模板 %d + songArtist）" % (len(sf), len(writer.SETTINGS_TEMPLATE)))

# ---------- F. 真机产物（可选） ----------
if len(sys.argv) > 1:
    p = sys.argv[1]
    d = json.load(io.open(p, encoding="utf-8-sig"))
    ks = list(d["settings"])
    chk("F1 磁盘上那份导出的 settings 键集 == 白名单", set(ks) == set(WL),
        "文件 %s | 键数 %d" % (p, len(ks)))
    chk("F2 磁盘上那份的顺序也一致", ks == WL, "前 3 = %s" % ks[:3])
    print("     ---- 实际写出的 settings ----")
    print(json.dumps(d["settings"], ensure_ascii=False, indent=1))

print("\n==== 通过 %d / 失败 %d ====" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
