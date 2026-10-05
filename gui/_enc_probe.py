# -*- coding: utf-8 -*-
"""编码探针（临时）：复刻 gen_cli.py 的 emit，用于定位 C# 读取端的乱码来源。

只在 stderr 打自述（stdout 编码 / UTF-8 模式 / 首选编码），stdout 打一行真 JSON。
"""
import json
import locale
import sys

sys.stderr.write(
    "PROBE stdout_enc=%s utf8_mode=%s pref=%s fs_enc=%s\n"
    % (sys.stdout.encoding, sys.flags.utf8_mode,
       locale.getpreferredencoding(False), sys.getfilesystemencoding())
)
sys.stderr.flush()

sys.stdout.write(json.dumps({
    "type": "gen_stage",
    "n": 2,
    "name": "BS-RoFormer-SW 分离 6 轨",
    "detail": "piano / guitar / bass / other / drums / vocals",
}, ensure_ascii=False) + "\n")
sys.stdout.flush()
