"""一次性自测启动器：读取 JS 文件并注入 ADOFAI_GUI_SELFTEST_* 环境变量后跑 gui_main.main()。

用法：
  python run_selftest.py nav            # 导入页 6 导航点测
  python run_selftest.py wb <midi>      # 工作台 xk 浮窗 + BDG 桥面板点测（需先有合成 MIDI）
"""
import os, sys

GUI = os.path.dirname(os.path.abspath(__file__))
os.chdir(GUI)
sys.path.insert(0, GUI)

mode = sys.argv[1] if len(sys.argv) > 1 else "nav"
js_file = os.path.join(GUI, "selftest_%s_js.txt" % mode)
with open(js_file, "r", encoding="utf-8") as f:
    js = f.read()

os.environ["ADOFAI_GUI_SELFTEST"] = "1"
os.environ["ADOFAI_GUI_SELFTEST_CLOSE"] = "1"
os.environ["ADOFAI_GUI_SELFTEST_JS"] = js

if mode == "wb":
    midi = sys.argv[2] if len(sys.argv) > 2 else ""
    if midi and os.path.isfile(midi):
        os.environ["ADOFAI_GUI_STUDIO_MIDI"] = midi
        os.environ["ADOFAI_GUI_OPEN_STUDIO"] = "1"

import gui_main
gui_main.main()
