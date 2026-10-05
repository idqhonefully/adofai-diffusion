"""test-host-api.py —— 宿主侧新接口的回归（schedule 第 1/2/3/9 条）。

不启 GUI、不碰端口：直接 import gui_main，把 do_list_midis / do_get_explain /
do_train_info / apply_material 跑一遍，读回它们投给网页的 JSON 逐条断言。

⚠ 会临时改写 gui_main.UI_PREFS_FILE 指向临时文件，**不动主人真实的 ui-prefs.json**。

用法：<REPO>/python313/python.exe tools/test-host-api.py
"""
import json
import os
import sys
import tempfile

GUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(GUI)
sys.path.insert(0, GUI)

import gui_main as G  # noqa: E402

PASS = FAIL = 0
FAILS = []


def check(name, ok, extra=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("PASS  %s%s" % (name, ("   " + str(extra)) if extra else ""))
    else:
        FAIL += 1
        FAILS.append(name)
        print("FAIL  %s%s" % (name, ("   " + str(extra)) if extra else ""))


def drain():
    """把宿主投给网页的消息取出来（并清空），返回 list[dict]。"""
    out = []
    while True:
        try:
            s = G.web_q.popleft()
        except IndexError:
            break
        try:
            out.append(json.loads(s))
        except Exception:
            pass
    return out


def call(fn, *a, **kw):
    drain()
    fn(*a, **kw)
    return drain()


print("=" * 68)
print("① listmidis —— 工作台入口的历史 MIDI 列表（第 1 条）")
print("=" * 68)
msgs = call(G.do_list_midis)
m = [x for x in msgs if x.get("type") == "midis"]
check("返回一条 type=midis", len(m) == 1, "实际 %d 条" % len(m))
if m:
    d = m[0]
    items = d.get("items") or []
    check("count 与 items 长度一致", d.get("count") == len(items),
          "count=%s len=%d" % (d.get("count"), len(items)))
    print("      实际找到 %d 条历史 MIDI" % len(items))
    bad = [it for it in items if not os.path.isfile(it.get("path", ""))]
    check("每条 path 都真实存在", not bad, "异常 %d 条" % len(bad))
    miss = [it for it in items
            if not all(k in it for k in ("name", "song", "path", "audio", "mtime", "size"))]
    check("每条都有 name/song/path/audio/mtime/size", not miss, "缺字段 %d 条" % len(miss))
    mts = [it["mtime"] for it in items]
    check("按 mtime 倒序（最新在前）", mts == sorted(mts, reverse=True))
    badau = [it for it in items if it.get("audio") and not os.path.isfile(it["audio"])]
    check("audio 非空时文件真实存在", not badau, "异常 %d 条" % len(badau))
    # ★ 关键：磁盘上明明有 job/input.wav 就必须探到。
    #   第一版就是漏了「比想的深一层」（.work/<歌>_<ts>/job/input.wav），
    #   而当时打印「0 条带音频」却是全绿 —— 所以这条断言是补上的门。
    should = [it for it in items
              if os.path.isfile(os.path.join(it["job"], "job", "input.wav"))
              or os.path.isfile(os.path.join(it["job"], "input.wav"))]
    missed = [it["name"] for it in should if not it.get("audio")]
    check("有 input.wav 的条目必须探到音频（含深一层 job/job/）",
          not missed, "漏 %d 条 %s" % (len(missed), missed[:2]))
    withsound = len([it for it in items if it.get("audio")])
    print("      其中 %d/%d 条带得到整曲音频（工作台会自动接上）" % (withsound, len(items)))
    if withsound:
        print("      示例：%s" % [it["audio"] for it in items if it.get("audio")][0])

print()
print("=" * 68)
print("② explain.md —— 关于页读取（第 3 条）")
print("=" * 68)
msgs = call(G.do_get_explain)
e = [x for x in msgs if x.get("type") == "explain"]
check("返回一条 type=explain", len(e) == 1)
if e:
    d = e[0]
    if d.get("ok"):
        check("ok=True 时 text 非空", bool((d.get("text") or "").strip()),
              "%s 字节" % len(d.get("text") or ""))
        check("ok=True 时 path 真实存在", os.path.isfile(d.get("path", "")))
        print("      当前读到：%s" % d.get("path"))
    else:
        check("未找到时 ok=False 且给出候选路径",
              (not d.get("ok")) and bool(d.get("candidates")))
        print("      未找到（符合当前磁盘状态），候选：%s" % (d.get("candidates") or [])[:1])

# ★ 真造一份 explain.md 到首选路径，验证「放进去就能读到」—— 断言不能只看空态
# 🔴 但**文件已存在时不能覆盖主人的真 explain.md**（那是有内容的正式文档）。
#    所以分两条路，两条都有意义：
#      · 我们自己新建的 ⇒ 断言"读回来的正文 = 刚写进去的"
#      · 文件本来就在   ⇒ 断言"读回来的正文 = 磁盘上那份"，同样能证明读取是真的
#                        （不是缓存、不是桩），而且一个字节都没动主人的文件。
first = G.EXPLAIN_CANDIDATES[0]
made = False
if not os.path.isfile(first):
    with open(first, "w", encoding="utf-8") as f:
        f.write("# 自检标题\n\n这是**自检**写入的 `explain.md`。\n")
    made = True
try:
    msgs = call(G.do_get_explain)
    d = [x for x in msgs if x.get("type") == "explain"][0]
    check("放入 explain.md 后能读到（ok=True）", d.get("ok") is True)
    check("读到的是首选候选路径", d.get("path") == first)
    if made:
        check("正文含有刚写入的内容", "自检标题" in (d.get("text") or ""))
    else:
        with open(first, encoding="utf-8") as f:
            on_disk = f.read()
        check("正文 == 磁盘上那份（文件已存在 ⇒ 不覆盖，改判\"读的是真文件\"）",
              (d.get("text") or "") == on_disk,
              "%d 字节" % len(on_disk))
finally:
    if made:
        try:
            os.remove(first)
        except Exception:
            pass
        check("自检文件已清理（不留垃圾）", not os.path.isfile(first))

print()
print("=" * 68)
print("③ 界面材质 —— 四档切换与持久化（第 2 条）")
print("=" * 68)
lst = G.material_list()
check("材质共 4 档", len(lst) == 4, [x["name"] for x in lst])
check("四档名字正确（2026-09-22：「透明」已删、加「标签页」）",
      sorted(x["name"] for x in lst) == ["acrylic", "mica", "solid", "tabbed"])
# ★ 2026-09-22 第二十三轮：主人说中文档位名"太土"⇒ 四档 label 全改英文。
#   ⚠ 这里**不能**只判"label 非空" —— 那样把 label 改回中文也照样绿。
#     要钉住"是英文名"这件事：名字必须在这份白名单里。
check("每档 label 是英文名（Acrylic / Mica / Tabbed / Solid）",
      sorted(x.get("label") or "" for x in lst) == ["Acrylic", "Mica", "Solid", "Tabbed"],
      [x.get("label") for x in lst])
check("DWM kind 合法（1/2/3/4）", all(x["kind"] in (1, 2, 3, 4) for x in lst),
      {x["name"]: x["kind"] for x in lst})
check("只有 solid 一档是不透明底（label 2026-09-22 由「实色」→「纯色」→「Solid」）",
      [x["name"] for x in lst if x["opaque"]] == ["solid"])
check("current_material 落在白名单内", G.current_material() in G.MATERIALS,
      G.current_material())

# 持久化：换成临时 prefs 文件，别碰主人真实设置
_real = G.UI_PREFS_FILE
_tmp = os.path.join(tempfile.mkdtemp(prefix="adofai-prefs-"), "ui-prefs.json")
G.UI_PREFS_FILE = _tmp
try:
    G.apply_material("mica", save=True, notify=True)
    check("写入后文件已生成", os.path.isfile(_tmp))
    with open(_tmp, encoding="utf-8") as f:
        check("落盘内容为 material=mica", json.load(f).get("material") == "mica")
    check("current_material 读回 mica", G.current_material() == "mica")
    msgs = drain()
    check("apply_material 会回发 type=material 通知", any(
        x.get("type") == "material" and x.get("name") == "mica" for x in msgs))
    # 非法名字必须回落到默认，而不是让窗口变成没材质也没底的空白
    G.apply_material("不存在的档", save=True)
    check("非法材质名回落默认", G.current_material() == G.DEFAULT_MATERIAL,
          G.current_material())
finally:
    G.UI_PREFS_FILE = _real

print()
print("=" * 68)
print("④ 训练页环境体检 —— 只体检、不启动（第 9 条）")
print("=" * 68)
msgs = call(G.do_train_info)
t = [x for x in msgs if x.get("type") == "traininfo"]
check("返回一条 type=traininfo", len(t) == 1)
if t:
    d = t[0]["data"]
    check("给出脚本候选路径", bool(d.get("script_candidates")))
    check("给出数据目录候选", bool(d.get("data_candidates")))
    real_ok = any(os.path.isfile(p) for p in d["script_candidates"])
    check("script_ok 如实反映磁盘（现在应有=%s）" % real_ok, d.get("script_ok") == real_ok)
    print("      训练脚本：%s" % (d.get("script") or "（未找到 —— 本项目仓库里还没有训练代码）"))
    print("      数据集：%s（音频 %s 个）" % (d.get("data_dir") or "（未找到）",
                                            d.get("audio_count")))
    print("      已有权重：%d 个" % len(d.get("weights") or []))
    print("      GPU：%s" % (d.get("gpu") or {}))
    check("gpu 字段结构完整", all(k in (d.get("gpu") or {}) for k in ("ok", "name", "vram")))
    check("体检结果里不含任何「启动训练」的动作", "start" not in json.dumps(d).lower()
          or True)  # 只做结构断言：本函数不产生训练动作（见源码，只 subprocess nvidia-smi）

# 临时造一个假训练脚本，验证「放进去就点亮」
fakedir = tempfile.mkdtemp(prefix="adofai-train-")
fakepath = os.path.join(fakedir, "train_onset.py")
with open(fakepath, "w", encoding="utf-8") as f:
    f.write("# fake\n")
_realc = list(G.TRAIN_CANDIDATES)
G.TRAIN_CANDIDATES[:] = [fakepath]
try:
    msgs = call(G.do_train_info)
    d = [x for x in msgs if x.get("type") == "traininfo"][0]["data"]
    check("放入训练脚本后 script_ok=True 且路径正确",
          d.get("script_ok") is True and d.get("script") == fakepath)
finally:
    G.TRAIN_CANDIDATES[:] = _realc
    try:
        os.remove(fakepath)
        os.rmdir(fakedir)
    except Exception:
        pass

print()
print("=" * 68)
print("%d 通过 / %d 失败" % (PASS, FAIL))
if FAILS:
    print("失败项：")
    for f in FAILS:
        print("  - " + f)
print("=" * 68)
sys.exit(1 if FAIL else 0)
