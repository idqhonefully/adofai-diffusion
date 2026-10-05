# -*- coding: utf-8 -*-
"""gen_cli 编码修复的负/正对照（临时脚本，跑完即删）。

背景：C# 壳按 UTF-8 解码子进程 stdout，而 Windows 上 Python 的 stdout
被重定向时默认是 ANSI 代码页（本机 cp936）。真机事故 = 中文变方块 + `⚠`
编不出来把桥进程打崩。

本脚本把子进程环境强制成 cp936，跑两组：
  A 负对照：走"修复前"的写法（直接 json.dumps + sys.stdout.write）
           → 期望：UnicodeEncodeError，非零退出（证明旧行为确实会崩）
  B 正对照：用**真实的 gui/gen_cli.py**（_force_utf8 已生效）emit 同一行
           → 期望：退出码 0，字节是合法 UTF-8，⚠ 原样在（证明修好了）
"""
import json
import os
import subprocess
import sys

PY = r"<REPO>\audio-sep\runtime\python.exe"
LINE = "[ok] 吉他 完成 → input_guitar.mid（0 KB）  ⚠ 几乎为空，该轨可能没有音符"

# 强制子进程落到 cp936（复刻真机：无 locale 变量、无 PYTHONUTF8）
env = dict(os.environ)
env["PYTHONUTF8"] = "0"
env["PYTHONCOERCECLOCALE"] = "0"
env.pop("PYTHONIOENCODING", None)
for k in ("LC_ALL", "LC_CTYPE", "LANG", "LANGUAGE"):
    env.pop(k, None)

PRE = (
    "import json,sys;"
    "sys.stderr.write('stdout_enc=' + str(sys.stdout.encoding) + '\\n');"
    "sys.stdout.write(json.dumps({'type':'gen_log','line':LINE_LIT},ensure_ascii=False)+'\\n');"
    "sys.stdout.flush()"
).replace("LINE_LIT", repr(LINE))
POST = (
    "import sys;sys.path.insert(0, r'<REPO>\\gui');"
    "import gen_cli;"
    "sys.stderr.write('stdout_enc=' + str(sys.stdout.encoding) + '\\n');"
    "gen_cli.emit({'type':'gen_log','line':LINE_LIT})"
).replace("LINE_LIT", repr(LINE))


def run(label, code):
    p = subprocess.Popen([PY, "-c", code], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, cwd=r"<REPO>",
                         env=env)
    out, err = p.communicate()
    print("=" * 72)
    print("%s  exit=%s" % (label, p.returncode))
    print("  stderr :", err.decode("utf-8", "replace").strip().splitlines()[-1:])
    print("  bytes  :", out.hex(" ") or "(空)")
    try:
        txt = out.decode("utf-8")
        print("  utf-8  : OK ->", txt.strip())
        print("  ⚠ 在里面吗:", "⚠" in txt)
    except UnicodeDecodeError as e:
        print("  utf-8  : 解码失败 ->", e)
    return p.returncode, out


print("子进程环境：ACP 代码页下（PYTHONUTF8=0，无 locale 变量）")
rc_pre, out_pre = run("A 负对照（修复前写法）", PRE)
rc_post, out_post = run("B 正对照（真实 gen_cli.py）", POST)

print("=" * 72)
ok = (rc_pre != 0) and (rc_post == 0) and ("⚠" in out_post.decode("utf-8", "replace"))
print("判定：", "✅ 修复有效（旧写法崩 / 新代码活且输出 UTF-8）" if ok
      else "❌ 与预期不符，需复查")
sys.exit(0 if ok else 1)
