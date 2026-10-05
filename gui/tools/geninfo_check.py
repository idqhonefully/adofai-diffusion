# -*- coding: utf-8 -*-
"""geninfo_check.py — 生成回执（generated 消息）的常驻回归。

为什么需要它
------------
2026-09-25 真机事故：OSN1 通路整条链路（load/derive/rebuild）全部跑成功、
谱面已经生成好，界面却报「生成失败 'midi'」，还把人误导成
「工作台只认 MIDI，不认我自己的 JSON」。

根因：`gen_cli.py` 组装回执时写死 `info["midi"]`，而 OSN1 的产物是
**多轨时间戳 JSON**，没有 midi 键 → KeyError('midi') 被同层 except 接住，
回一条 `ok:false, msg:"'midi'"`。

本脚本把「回执组装 + OSN1 返回形状」钉成断言，跑一次 5 秒、不吃 GPU、不碰 sidecar。
退出码 0 = 全过，非 0 = 有回归。

用法：
    python gui/tools/geninfo_check.py        # 任意带 stdlib 的解释器都行
"""
import json
import os
import sys
import tempfile

GUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if GUI not in sys.path:
    sys.path.insert(0, GUI)

import gen_cli    # noqa: E402
import backend    # noqa: E402

FAILS = []
TOTAL = [0]

_TMP = tempfile.mkdtemp(prefix="geninfo_check_")


def ck(name, cond, extra=""):
    TOTAL[0] += 1
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else "   ← " + str(extra)))
    if not cond:
        FAILS.append(name)


print("== ① OSN1 形状回执：不许再炸，且 midi 回退成 JSON 路径 ==")
osn1_info = {
    "json": os.path.join(_TMP, "osn1_timestamps.json"),
    "midi": None,
    "stems_dir": _TMP,
    "work_dir": _TMP,
    "stems": {"melody": {"onsets": 919}, "drums": {"onsets": 576}},
    "failed": [],
}
try:
    p_osn1 = gen_cli.generated_payload(osn1_info, {"base_bpm": 180.0})
    ck("组装不抛异常（这正是原来炸的地方）", True)
except Exception as e:
    p_osn1 = {}
    ck("组装不抛异常（这正是原来炸的地方）", False, repr(e))

ck("ok=True", p_osn1.get("ok") is True, p_osn1.get("ok"))
ck("midi 回退为 JSON 路径（C# 靠它 File.Exists 后自动进工作台）",
   p_osn1.get("midi") == osn1_info["json"], p_osn1.get("midi"))
ck("额外带 json 字段（真源）", p_osn1.get("json") == osn1_info["json"], p_osn1.get("json"))
ck("stems 计数 = 2", p_osn1.get("stems") == 2, p_osn1.get("stems"))
ck("state 原样透传", p_osn1.get("state") == {"base_bpm": 180.0})

print("== ② 负对照：连 midi 键都没有的 info（旧写法的必炸输入） ==")
legacy = {"json": os.path.join(_TMP, "a.json")}   # 刻意不提供 midi 键
try:
    p_legacy = gen_cli.generated_payload(legacy, {})
    ck("缺 midi 键也不炸", True)
    ck("midi 仍回退成 json", p_legacy.get("midi") == legacy["json"], p_legacy.get("midi"))
except Exception as e:
    ck("缺 midi 键也不炸", False, repr(e))
    ck("midi 仍回退成 json", False)

print("== ③ MuScriptor 形状：原有行为一字不改 ==")
mus_info = {"midi": os.path.join(_TMP, "combined.mid"), "stems_dir": _TMP, "work_dir": _TMP,
            "stems": {"a": 1, "b": 2, "c": 3}, "failed": ["c（超时）"]}
p_mus = gen_cli.generated_payload(mus_info, {})
ck("midi 仍是 MIDI 路径", p_mus.get("midi") == mus_info["midi"], p_mus.get("midi"))
ck("json 为空串（不是 None，C# GetString 不用兜底）", p_mus.get("json") == "", repr(p_mus.get("json")))
ck("stems 计数 = 3", p_mus.get("stems") == 3, p_mus.get("stems"))
ck("failed 原样透传", p_mus.get("failed") == mus_info["failed"])
ck("info 为 None 也不炸", gen_cli.generated_payload(None, {})["midi"] == "")

print("== ④ 源码静态断言：裸索引 info[\"midi\"] 不许复活 ==")


def bare_midi_subscripts(path):
    """用 AST 找**真代码**里对 info["midi"] 的裸下标取值。

    不能用 `'info["midi"]' in src` 这种字符串匹配 —— 注释和 docstring 里
    提到的写法会被误判（本脚本第一版就是这么翻车的）。
    """
    import ast
    tree = ast.parse(open(path, encoding="utf-8").read())
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) \
                and node.value.id == "info" and isinstance(node.slice, ast.Constant) \
                and node.slice.value == "midi":
            hits.append(node.lineno)
    return hits


for fn in ("gen_cli.py", "backend.py"):
    hits = bare_midi_subscripts(os.path.join(GUI, fn))
    ck(f"{fn} 的代码里没有 info[\"midi\"] 裸下标", not hits, "行号 " + str(hits))

bsrc = open(os.path.join(GUI, "backend.py"), encoding="utf-8").read()
ck("backend.py 的 load_path 走 .get 兜底", 'info.get("json") or info.get("midi")' in bsrc)
ck("to_stemjson_osn1 已返回兼容键 midi", '"midi": None' in bsrc)
g1src = open(os.path.join(GUI, "gen_cli.py"), encoding="utf-8").read()
ck("gen_cli.py 的 midi 走 .get 并有 json 回退", 'info.get("midi") or json_path' in g1src)

print("== ⑤ OSN1 轨数统计：真实 JSON 进、轨数出 ==")
tmp = tempfile.mkdtemp(prefix="geninfo_")
jpath = os.path.join(tmp, "osn1_timestamps.json")
with open(jpath, "w", encoding="utf-8") as fh:
    json.dump({
        "version": 1,
        "stems": {
            "melody": {"source": "piano", "method": "onsetnet",
                       "onsets_sec": [0.1, 0.2, 0.3], "onsets_frame": [4, 9, 13]},
            "drums": {"source": "drums", "method": "spectral_flux",
                      "onsets_sec": [0.5], "onsets_frame": [22]},
            "guitar": {"source": "guitar", "method": "spectral_flux",
                       "onsets_sec": [], "onsets_frame": []},
        },
    }, fh)

stats = backend._stem_stats_from_json(jpath)
ck("三条轨都统计到", sorted(stats.keys()) == ["drums", "guitar", "melody"], sorted(stats.keys()))
ck("melody 踩点 3（模型）", stats["melody"]["onsets"] == 3 and stats["melody"]["method"] == "onsetnet",
   stats.get("melody"))
ck("drums 踩点 1（频谱通量）", stats["drums"]["onsets"] == 1 and stats["drums"]["method"] == "spectral_flux",
   stats.get("drums"))
ck("guitar 空轨计 0 也不算失败", stats["guitar"]["onsets"] == 0, stats.get("guitar"))

print("== ⑥ 统计函数对坏输入要静默降级（不能反过来把生成搞崩） ==")
msgs = []
ck("文件不存在 → 空字典", backend._stem_stats_from_json(os.path.join(tmp, "nope.json"),
                                                        on_log=msgs.append) == {})
ck("坏 JSON → 空字典", (lambda: (open(os.path.join(tmp, "bad.json"), "w").write("{oops"),
                                 backend._stem_stats_from_json(os.path.join(tmp, "bad.json"),
                                                               on_log=msgs.append))[-1] == {})())
ck("降级时留下了 warn（可追溯）", len(msgs) >= 1, msgs)

print()
if FAILS:
    print("❌ %d/%d 项失败：" % (len(FAILS), TOTAL[0]))
    for f in FAILS:
        print("   - " + f)
    sys.exit(1)
print("✅ 全部通过（%d 项；OSN1 回执不再被误报成生成失败）" % TOTAL[0])
sys.exit(0)
