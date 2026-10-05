#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""test-nativedir.py —— 「导出没反应」的回归测试（零 GUI，可随便跑）

背景（2026-09-20 真机日志）：
    点工作台「导出谱面…」→ 前端 doExport() → 桥 openDir → 宿主 _do_dsh_native("openDir")
    → native_open_dir() 抛 TypeError:
        incompatible types, c_wchar_Array_260 instance instead of c_wchar_p instance
    异常被 _do_dsh_native 的 except 吃掉 → 回 dsh_reply{ok:false} → 前端 Promise reject
    → 无人接 → unhandledrejection → **界面上一点反应都没有**。

做法：不从本文件重新抄一遍代码（那就测不到真代码了），而是**把 gui_main.py 里
     OPENFILENAMEW / BROWSEINFO / native_open_* 那一整段原文抠出来 exec**，
    再把 shell32 换成假的（返回一个假 pidl），从而在无桌面环境下走完全流程。

跑的断言：
  A. BROWSEINFO 结构在 x64 上必须是 64 字节：lParam@48(8 字节)、iImage@56
     （写成 c_long 会变 56 字节 / iImage@52 ⇒ Windows 会往结构外写 4 字节）
  B. 走完 native_open_dir() 全程不抛异常，并返回假 pidl 对应的路径
  C. 用户取消（pidl = 0）时返回 None，不抛异常
  D. native_open_file / native_open_audio 的 lpstrFile 必须是 cast 过的 LPWSTR
     （同一个坑的另一半：当初目录对话框就是漏了 cast）

用法： python gui/tools/test-nativedir.py
"""
import ctypes
import ctypes.wintypes as wt
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
GUI_MAIN = os.path.normpath(os.path.join(HERE, "..", "gui_main.py"))

FAIL = []
NOTE = []


def chk(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (("   [" + str(detail) + "]") if detail else ""))
    if not ok:
        FAIL.append(name)


def extract_block(src):
    """把 gui_main.py 里 OPENFILENAMEW + BROWSEINFO + native_open_* 的原文抠出来。"""
    a = src.index("class OPENFILENAMEW")
    b = src.index("def _read_ui_layout")
    return src[a:b]


def build_ns(block):
    """在受控命名空间里 exec 这段原文（shell32 之后会被换成假的）。"""
    ns = {
        "__name__": "nativedir_probe",
        "ctypes": ctypes,
        "wt": wt,
        "comdlg32": ctypes.windll.comdlg32,
        "ole32": ctypes.windll.ole32,
        "HWND_MAIN": 0,          # 无桌面，owner 给 0 即可
    }
    exec(compile(block, GUI_MAIN, "exec"), ns)
    return ns


class FakeShell(object):
    """假的 shell32：SHBrowseForFolderW 返回给定 pidl，SHGetPathFromIDListW 写入给定路径。"""

    def __init__(self, pidl, path):
        self.pidl = pidl
        self.path = path
        self.calls = []

    def SHBrowseForFolderW(self, _bi):
        self.calls.append("browse")
        return self.pidl

    def SHGetPathFromIDListW(self, pidl, out):
        self.calls.append(("getpath", pidl))
        if self.path is None:
            return 0
        # out 是 create_unicode_buffer(4096)：按 ctypes 数组写回去
        s = self.path
        out.value = s
        return 1


class FakeOle(object):
    """假的 ole32。

    🔴 必须换掉：代码末尾会 `ole32.CoTaskMemFree(pidl)`，而测试里的 pidl 是自己编的
    假指针（0x1234）——真去 free 一个非法指针会**硬崩进程**（无 traceback、退出码 127），
    连 try/except 都拦不住。顺带断言「pidl 确实被释放了」。
    """

    def __init__(self):
        self.freed = []

    def CoTaskMemFree(self, p):
        self.freed.append(p)


def main():
    src = io.open(GUI_MAIN, encoding="utf-8").read()
    block = extract_block(src)
    ns = build_ns(block)

    BROWSEINFO = ns["BROWSEINFO"]
    native_open_dir = ns["native_open_dir"]
    native_open_file = ns["native_open_file"]

    print("=== 抠出来跑的原文：%s 第 %d 行起 ==="
          % (os.path.basename(GUI_MAIN), src[:src.index("class OPENFILENAMEW")].count("\n") + 1))
    print()

    # ---------- A. 结构布局 ----------
    off = dict((f, getattr(BROWSEINFO, f).offset) for f, _ in BROWSEINFO._fields_)
    size = ctypes.sizeof(BROWSEINFO)
    chk("A1 BROWSEINFO 在 x64 上是 64 字节（LPARAM 必须指针宽）", size == 64,
        "sizeof=%d  offsets=%s" % (size, off))
    chk("A2 iImage 落在 offset 56（否则 Windows 往结构外写 4 字节）", off.get("iImage") == 56,
        "iImage@%s" % off.get("iImage"))
    chk("A3 lParam 落在 offset 48", off.get("lParam") == 48, "lParam@%s" % off.get("lParam"))

    # ---------- B. 正常选目录（这条就是真机上炸掉的那条） ----------
    fake = FakeShell(pidl=0x1234, path=r"<REPO>\output\导出测试")
    ole = FakeOle()
    ns["shell32"] = fake
    ns["ole32"] = ole
    try:
        got = native_open_dir("选择导出目录（会新建一个曲目文件夹）")
        err = None
    except Exception as e:
        got, err = None, "%s: %s" % (type(e).__name__, e)
    chk("B1 native_open_dir() 走完全程不抛异常",
        err is None, err or "ok")
    chk("B2 返回选中的目录路径", got == r"<REPO>\output\导出测试", repr(got))
    chk("B3 真的调了 SHBrowseForFolderW", fake.calls and fake.calls[0] == "browse",
        repr(fake.calls))
    chk("B4 拿到的 pidl 被 CoTaskMemFree 释放（不漏内存）", ole.freed == [0x1234],
        repr(ole.freed))

    # ---------- C. 取消 ----------
    ns["shell32"] = FakeShell(pidl=0, path=None)
    ns["ole32"] = FakeOle()
    try:
        got2, err2 = native_open_dir(None), None
    except Exception as e:
        got2, err2 = "★", "%s: %s" % (type(e).__name__, e)
    chk("C1 用户取消（pidl=0）返回 None 且不抛异常", got2 is None and err2 is None,
        "got=%r err=%r" % (got2, err2))

    # ---------- D. 同一个坑的另一半：文件对话框那两处是 cast 过的 ----------
    for fn_name in ("native_open_file", "native_open_audio"):
        f = ns[fn_name]
        chk("D %s 的 lpstrFile 用 ctypes.cast(..., LPWSTR)" % fn_name,
            "ctypes.cast(buf, wt.LPWSTR)" in block.split("def %s" % fn_name)[1].split("def ")[0],
            "对照 native_open_dir 当初就是漏了这句")

    print()
    if FAIL:
        print("==== 失败 %d 条 ====" % len(FAIL))
        for f in FAIL:
            print("  ✗ " + f)
        return 1
    print("==== 全部通过 ====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
