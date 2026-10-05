# -*- coding: utf-8 -*-
"""gen_cli.py — C# 新壳的生成进度桥。

C# 壳 spawn 本脚本（用 audio-sep/runtime/python.exe）来跑整条生成链路，
把 backend.generate() 的进度以**一行一条 JSON**打到 stdout，C# 逐行解析后转成
页面消息（gen_stage / gen_log / generated）。这样 C# 不用重写任何引擎逻辑，
直接复用老壳同一份 backend.py。

用法：
    audio-sep/runtime/python.exe gui/gen_cli.py <audio> <size> [--bpm <v>] [--port <p>] [--workdir <d>]

stdout 协议（每行一个 JSON 对象）：
    {"type":"gen_stage","n":1,"name":"...","detail":"..."}
    {"type":"gen_log","line":"..."}
    {"type":"generated","ok":true,"midi":"...","state":{...},"stems":N,"failed":[...]}
    {"type":"generated","ok":false,"msg":"..."}

--port：指定已由 C# 托管、且端口一致的 chartgen sidecar。给定后若侧已健康，
        就不再调用 backend.start()（避免误杀 C# 的 sidecar 进程、打挂工作台）。

🔴🔴 stdout 必须**强制 UTF-8**（见 _force_utf8 的注释）——
    这是 2026-09-25 真机事故的根因，别再让它退回去。
"""
import argparse
import json
import os
import sys


def _force_utf8():
    """把 stdout/stderr 钉死成 UTF-8。

    🔴 为什么非做不可（2026-09-25 真机实测，两处症状同一个根因）：
      Windows 上 Python 的 stdout **一旦被重定向（管道/文件）**，默认编码就不是
      控制台那套，而是**系统 ANSI 代码页**（本机 ACP=936 / GBK）。而 C# 那边按
      UTF-8 解码（StandardOutputEncoding = UTF8Encoding）。于是：
        ① 中文错码显示 —— 「分离」的 GBK 字节 B7 D6 C0 EB 被逐字节当成坏 UTF-8，
           变成 4 个替换方块；「轨」变成 2 个 ⇒ 界面上就是 `BS-RoFormer-SW □□□□ 6 □□`
           （ASCII 部分毫发无伤，正是这个错的指纹）。
        ② **整条链路被打崩** —— cp936 里没有 `⚠`(U+26A0)。转谱出一轨空 MIDI 时
           backend 会打 `[ok] … ⚠ 几乎为空，该轨可能没有音符`，这行字符串一 encode
           就抛 UnicodeEncodeError，从 to_midi 冒泡上去 → 桥进程当场死，
           C# 只看到"进程没了"，界面就僵在半路（真机现象：跑到 20:35 无声无息）。
      ⇒ 唯一可靠的做法是在**本进程**里把编码钉死，既不依赖环境变量、也不依赖
        Python 版本的 UTF-8 模式默认值（3.15 才默认开，本机是 3.12）。
      errors="backslashreplace"：将来万一混进编不出的字符，降级成 `\\uXXXX`
      也绝不抛异常 —— 显示难看远好过把 5 分钟的活整死在最后一行日志上。
    """
    for name in ("stdout", "stderr"):
        try:
            getattr(sys, name).reconfigure(encoding="utf-8",
                                           errors="backslashreplace")
        except Exception:
            pass   # 老解释器 / 被替换过的流：交给 emit 的兜底


_force_utf8()


def _hermetic():
    """便携包必须自洽：把「用户级 site-packages」从解释器视野里摘掉。

    🔴 为什么非做不可（2026-09-27 群友事故，同一类「假绿」的第二次）：
      Windows 上的普通 Python（包内 runtime 就是这种）启动时会**自动**把
        %APPDATA%\\Python\\Python3XX\\site-packages
      挂进 sys.path，而且**排在包内 site-packages 之前**。后果有两层：
        ① 开发机上「跑得通」是假象 —— numpy/librosa 其实是从宿主机的用户目录
           偷来的，包本身压根没装；我 9-27 就是被这条骗了一整轮。
        ② 换台干净机器（群友的 G:\\360MoveData\\...）立刻
           `ModuleNotFoundError: No module named 'numpy'`，生成必失败。
      这里做两件事让「包 = 全部依赖」成立：
        · 当前进程：把用户级目录从 sys.path 里剔除；
        · 子进程  ：置 PYTHONNOUSERSITE=1，让它们启动时就自己带上这个开关
                    （也顺带堵死开发机上的 ①）。
      注意：**不是**删依赖，只是不许再偷 —— 依赖得真的躺在包内 site-packages 里。
    """
    os.environ["PYTHONNOUSERSITE"] = "1"
    try:
        import site as _site
        cand = _site.getusersitepackages()
        if isinstance(cand, str):
            cand = [cand]
        drop = {os.path.normcase(os.path.abspath(p)) for p in cand if p}
        if drop:
            sys.path[:] = [p for p in sys.path
                           if os.path.normcase(os.path.abspath(p)) not in drop]
    except Exception:
        pass   # 任何异常都不许影响生成链路


_hermetic()

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import backend  # noqa: E402


def emit(obj):
    """打一行 JSON 到 stdout 并立刻 flush（C# 边跑边读，绝不能攒缓冲）。

    兜底顺序：UTF-8 原样 → 纯 ASCII（ensure_ascii=True，永不失败）。
    任何情况下都不许把异常抛出去：调用方可能是 GPU 链路的日志回调。
    """
    try:
        line = json.dumps(obj, ensure_ascii=False)
    except Exception:
        line = '{"type":"gen_log","line":"[emit: 对象无法序列化]"}'
    try:
        sys.stdout.write(line + "\n")
    except Exception:
        # 最后的兜底：非 ASCII 全转义成 \uXXXX，纯 ASCII 一定写得出去
        try:
            sys.stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")
        except Exception:
            return
    try:
        sys.stdout.flush()
    except Exception:
        pass


def generated_payload(info, st):
    """组装 ok:true 的回执。抽成独立函数是为了能被回归脚本直接测。

    🔴 2026-09-25 真机事故：这里原来写死 `"midi": info["midi"]`。
       OSN1 通路的产物是**多轨时间戳 JSON**、根本没有 MIDI 键，
       于是 KeyError('midi') 在组装回执那一行抛出 —— 而它在 try 块里，
       被同一个 except 接住，回给界面一条 `ok:false, msg:"'midi'"`。
       结果是：**load / derive / rebuild 全都跑成功了、谱面已经在内存里**，
       界面却报「生成失败 'midi'」，还误导成「工作台只认 MIDI」。
       ⇒ 取值一律走 .get()，并让 "midi" 在 OSN1 下回退成 JSON 路径：
          C# 用这个路径 File.Exists 后自动灌进工作台，而工作台的
          api.load 正是 chartgen 的 load，本来就能吃时间戳 JSON。
    """
    info = info or {}
    json_path = info.get("json") or ""
    return {
        "type": "generated",
        "ok": True,
        "midi": info.get("midi") or json_path,
        "json": json_path,
        "state": st,
        "stems": len(info.get("stems") or {}),
        "failed": info.get("failed") or [],
    }


def main():
    ap = argparse.ArgumentParser(description="ADOFAI Studio 生成链路 CLI 桥")
    ap.add_argument("audio", help="输入音频绝对路径")
    ap.add_argument("size", nargs="?", default="medium",
                    choices=("small", "medium", "large"),
                    help="转谱精度档（默认 medium）")
    ap.add_argument("--bpm", type=float, default=None,
                    help="用户填的基础 BPM；给了就覆盖模型识别值（auto_bpm=False）")
    ap.add_argument("--port", type=int, default=None,
                    help="已由 C# 托管的 sidecar 端口；给定且健康则跳过 start()")
    ap.add_argument("--workdir", default=None,
                    help="中间产物目录（默认 output/.work/<song>_<ts>）")
    ap.add_argument("--mode", default="muscriptor",
                    choices=("muscriptor", "osn1"),
                    help="踩点方式：muscriptor=MuScriptor 逐轨转谱 / osn1=OSN1 自训练采点模型")
    args = ap.parse_args()

    # 对齐到指定 sidecar 端口（与 C# 托管实例一致）
    if args.port:
        backend.PORT = int(args.port)
        backend.BASE = "http://127.0.0.1:%d" % backend.PORT

    def on_stage(n, name, detail=""):
        # 日志显示问题绝不许打断生成：emit 自己已有兜底，这里再加一道
        try:
            emit({"type": "gen_stage", "n": n, "name": name, "detail": detail})
        except Exception:
            pass

    def on_log(line):
        try:
            emit({"type": "gen_log", "line": line})
        except Exception:
            pass

    # 第 14 条：用户填基础 BPM 替换模型识别值
    state = None
    if args.bpm is not None and args.bpm > 0:
        state = {"auto_bpm": False, "base_bpm": float(args.bpm)}

    # sidecar 已健康（通常是 C# 托管）→ 跳过 start()，否则会误杀 C# 进程
    ensure = not (args.port and backend.is_healthy())

    try:
        rebuild_r, info, st = backend.generate(
            args.audio, args.size, state=state, work_dir=args.workdir,
            on_log=on_log, on_stage=on_stage, ensure_sidecar=ensure,
            mode=args.mode)
        emit(generated_payload(info, st))
    except Exception as e:  # 整条链路任何一步失败都如实回传，绝不假装成功
        # 🔴 报错这条**自己也得兜底**：曾出现过"异常信息里带 ⚠，
        #    结果在 except 里再炸一次，桥进程连失败都没来得及上报"。
        try:
            emit({"type": "generated", "ok": False, "msg": str(e)[:3000]})
        except Exception:
            emit({"type": "generated", "ok": False,
                  "msg": "链路异常，且错误信息无法编码；请查程序日志"})


if __name__ == "__main__":
    main()
