"""Electron 前端的后端：**只用标准库**的 HTTP + SSE 服务。

设计要点（也都是被本机约束逼出来的选择）：

1. **端口由 Electron 选好传进来**（`--port`），Python 不打印端口给父进程读。
   ⇒ 父子进程之间**不需要管道**（本机沙箱会拦 `stdio: 'pipe'`），
   日志写文件、握手用 `GET /api/health` 轮询。
2. **长任务（OGG→音头，30~60s）跑在工作线程**，进度通过 SSE
   （`GET /api/events`）推给前端；`POST /api/cancel` 置取消位，
   `_prog` 回调里抛 `ConversionCancelled`（`BaseException` 子类，不会被吞）。
3. **音频用 `/media?path=` 提供**，自己实现 `Range`（HTML5 `<audio>` 拖动需要）。
   只允许读「项目根 / 临时目录 / 用户显式选过的文件」下的东西 —— 本地服务也不
   应该变成任意文件读取。
4. 视图数据（谱面/卷帘/路径/下落式）**全部由 Python 打包**（`session.payload`），
   前端只负责画，保证与旧 UI 的口径一致。

    python -m sidecar.server --port 8765 [--root DIR] [--log FILE]
"""
from __future__ import annotations

import argparse
import atexit
import io
import json
import glob
import os
import queue
import re
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from . import ROOT
from . import bridge as BR
from . import hostctl as HC
from . import schema as SC
from . import ws as WS
from .session import BadRequest, Session
from core.bdg import aliases as al

MAX_BODY = 8 * 1024 * 1024

#: 版本号读不到文件时的兜底（正常永远用不到 —— 见 `version()`）
VERSION_FALLBACK = "0.0.0-unknown"


def _human_size(n: float) -> str:
    """字节数 → 人类可读串（对齐 C# HostApi.HumanSize）。"""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return (f"{n:.1f} {unit}") if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def version() -> str:
    """★ 本次跑的**到底是哪个号** —— **唯一真源 = 仓库根的 `VERSION` 文件**。

    打包时它被 `extraResources` 复制到 `resources/VERSION`，所以包内也读得到。

    为什么改成读文件（以前写死在这个文件里两次）：
      `docs/55` §4 记着这个坑 —— 号原本**写死在三处**（`VERSION` /
      `app/package.json` / 这里），漏改一处就会出现「界面显示旧号、包里是新的」。
      `tools/pack_verify.py` 的「包内 VERSION == /api/health 的号」那条断言
      就是为它写的。现在报告端只剩一个来源，漏改这件事**在结构上不可能发生**。
    """
    for base in (getattr(APP, "root", "") or "", ROOT):
        try:
            p = os.path.join(base, "VERSION")
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as fh:
                    v = fh.read().strip()
                if v:
                    return v
        except OSError:
            continue
    return VERSION_FALLBACK


def _audio_caps() -> dict:
    """音频采音那一支的能力自检（`docs/49` §10 第 5 项「环境自检」）。

    为什么这么查：`core/audio_onsets.py` 模块级**只** import numpy，
    librosa/scipy 是在函数里才 import 的（懒加载）⇒ 直接看 `find_spec` 就知道
    在不在，**不用真的 import**（import librosa 要 1~2 秒，health 是握手接口，不能慢）。

    注意 numpy **不在**这里：`core/denoise.py`/`gridfit.py`/`synth.py` 是模块级
    import numpy，缺了 sidecar 根本起不来 —— 所以它是**必需**的，不是「可选能力」。
    """
    import importlib.util as _u
    out = {}
    for m in ("librosa", "scipy", "soundfile", "numba"):
        try:
            out[m] = _u.find_spec(m) is not None
        except (ImportError, ValueError):
            out[m] = False
    out["ok"] = all(out[m] for m in ("librosa", "scipy"))
    return out


class Broker:
    """极简 SSE 广播：进度/状态消息。"""

    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._last: list[dict] = []

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=512)
        with self._lock:
            self._subs.append(q)
            for m in self._last[-20:]:
                try:
                    q.put_nowait(m)
                except queue.Full:
                    pass
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def push(self, kind: str, **kw) -> None:
        m = {"kind": kind, "t": time.time(), **kw}
        with self._lock:
            self._last.append(m)
            self._last = self._last[-100:]
            for q in self._subs:
                try:
                    q.put_nowait(m)
                except queue.Full:
                    pass


class App:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.session = Session(self.root)
        self.broker = Broker()
        self.cancel_flag = threading.Event()
        self.busy = threading.Lock()
        self.allowed_files: set[str] = set()
        # ★ BDG 桥：一次性 token + 协议层（`docs/35` §3.8 / `docs/37` §5）
        self.bridge_token = WS.new_token()
        self.bridge = BR.Bridge(self, self.bridge_token)
        # ★ 「启动并桥接」：起宿主 + 用 CDP 把连接串推进插件（`docs/40`）
        self.hostctl = HC.HostCtl(self.root, log=lambda m: print("[host] " + m))
        self.host_starting = False
        atexit.register(self.shutdown)

    def shutdown(self) -> None:
        """★ 我们**自己起的**宿主进程要带走 —— 别给用户留孤儿窗口。

        `stop_and_unbridge` 只停 pidfile 里记着的那个进程；如果是用户手动
        `host:dev` 起的，它会明确说「不是我起的」并且**不动手**（不猜、不杀别人的进程）。
        """
        try:
            r = self.hostctl.stop_and_unbridge()
            if r.get("stopped"):
                print("[host] 已停掉我们起的宿主（pid %s）" % r.get("pid"))
        except Exception as exc:                                # noqa: BLE001
            print("[host] 停宿主时出意外（忽略）：%s" % exc)

    # ------------------------------------------------------------ 安全路径
    def allow(self, path: str) -> None:
        self.allowed_files.add(os.path.abspath(path))

    def resolve_media(self, path: str) -> str:
        p = os.path.abspath(path)
        if not os.path.isfile(p):
            raise BadRequest("文件不存在")
        if p in self.allowed_files:
            return p
        for base in (self.root, os.path.abspath(__import__("tempfile").gettempdir())):
            try:
                if os.path.commonpath([p, base]) == base:
                    return p
            except ValueError:
                pass
        raise BadRequest("不允许读取该路径")


APP: App | None = None


class Handler(BaseHTTPRequestHandler):
    server_version = "adocharter"
    protocol_version = "HTTP/1.1"

    def version_string(self):                                # noqa: D102
        # ★ `Server:` 头也带上真号（`server_version` 是类属性，只能在这里拼）
        return "adocharter/" + version()

    # -------------------------------------------------------------- 基础
    def log_message(self, fmt, *args):                       # noqa: A003
        if APP and getattr(APP, "verbose", False):
            sys.stderr.write("[http] " + fmt % args + "\n")

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, exc: Exception, code: int = 400) -> None:
        self._json({"ok": False, "error": str(exc),
                    "trace": traceback.format_exc()[-4000:]}, code)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > MAX_BODY:
            raise BadRequest("请求体过大")
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as exc:                              # noqa: BLE001
            raise BadRequest(f"JSON 解析失败：{exc}") from None

    # ---------------------------------------------------------------- GET
    def do_GET(self):                                        # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/api/health":
                # ★★ 2026-10 打包用：**把解释器路径与能力报出来**（环境自检）。
                #   为什么要报 `exe`：本机装了 3.14.3，包里也带了一份 3.14.3 ——
                #   只看版本号**分不出**到底跑的是哪一份（真机验证时差点被这个骗过去）。
                #   `audio` = 音频采音那一支（librosa/scipy）在不在；
                #   缺了只是「从音频文件采音」不可用，MIDI / .bdg 照常（懒加载）。
                return self._json({"ok": True, "version": version(),
                                   "root": APP.root, "py": sys.version.split()[0],
                                   "exe": sys.executable,
                                   "frozen": bool(getattr(sys, "frozen", False)),
                                   "audio": _audio_caps()})
            if u.path == "/api/schema":
                return self._json({"ok": True, **SC.schema()})
            if u.path == "/api/samples":
                items = [os.path.basename(p) for p in APP.session.samples()]
                return self._json({"ok": True, "samples": items,
                                   "abs": APP.session.samples()})
            if u.path == "/api/events":
                return self._sse()
            if u.path == "/api/bridge":
                br = APP.bridge
                host, port = self.server.server_address[:2]
                out = {"ok": True, "url": br.url(host, port),
                       "token": br.token, "state": br.state()}
                if (q.get("full") or [""])[0] in ("1", "true", "yes"):
                    out["project"] = br.summary()
                return self._json(out)
            if u.path == "/api/bridge/back":
                # ★ 收回的轨道项目（`docs/45`）：看一眼（**只读**，不改我们的状态）
                return self._json(_back_view(APP.session, {}))
            if u.path == "/api/host":
                # ★ 「启动并桥接」：宿主装没装 / 在不在跑 / 调试端口通不通（docs/40）
                h, p = self.server.server_address[:2]
                st = APP.hostctl.state()
                st["starting"] = APP.host_starting
                st["url"] = APP.bridge.url(h, p)
                return self._json({"ok": True, **st})
            if u.path == "/ws":
                return self._ws(q)
            if u.path == "/media":
                return self._media(q.get("path", [""])[0])
            if u.path == "/api/projects":
                return self._projects()
            return self._json({"ok": False, "error": "未知路径"}, 404)
        except BadRequest as exc:
            return self._err(exc, 400)
        except Exception as exc:                              # noqa: BLE001
            return self._err(exc, 500)

    def _projects(self):
        """★ 2026-09-27 bug 修复：统一工程列表口子（替代 C# HostApi 三件套）。

        原 C# Midis/Stems/History 只认 `*.mid` / `*_stems_combined.mid` /
        `*.adofai` / `stems/*.wav` —— OSN1 模式产物是 `osn1_timestamps.json`
        （无 .mid、无 .adofai、不落分轨 wav），于是三个列表全空、工作台与历史
        进不去。这里直接扫 `output/.work`，把 OSN1 也纳入：
          · OSN1 → 进 midis（path=json）与 history（source_midi=json）
          · BSR  → 进 midis / history / stems（保持原 C# 口径）
        """
        root = APP.root
        studio = os.path.dirname(root)
        work = os.path.join(studio, "output", ".work")
        out = os.path.join(studio, "output")

        # 预扫 output 下全部 .adofai（用于 has_chart / 找源）
        adofai_names = set()
        if os.path.isdir(out):
            for dp, _d, fs in os.walk(out):
                for f in fs:
                    if f.endswith(".adofai"):
                        adofai_names.add(f.lower())

        def song_of(proj_dir):
            base = os.path.basename(proj_dir)
            base = re.sub(r"_osn1.*$", "", base)
            base = re.sub(r"_\d{9,}$", "", base)
            return base

        midis, history, stems = [], [], []

        if os.path.isdir(work):
            for proj in sorted(os.listdir(work)):
                pd = os.path.join(work, proj)
                if not os.path.isdir(pd):
                    continue
                song = song_of(pd)
                # ---- OSN1 ----
                js = glob.glob(os.path.join(pd, "*_timestamps.json"))
                if js:
                    jf = js[0]
                    try:
                        with open(jf, "r", encoding="utf-8") as fh:
                            meta = json.load(fh)
                    except Exception:
                        meta = {}
                    raw_src = meta.get("source_audio") or meta.get("audio") or ""
                    if raw_src:
                        raw_src = os.path.normpath(
                            os.path.join(os.path.dirname(jf), raw_src))
                    song = (meta.get("song") or song_of(pd)) or song_of(pd)
                    mt = os.path.getmtime(jf)
                    sz = os.path.getsize(jf)
                    has_chart = any(os.path.basename(a).lower().startswith(song.lower())
                                   for a in adofai_names)
                    midis.append({
                        "name": os.path.basename(jf),
                        "song": song,
                        "path": jf,
                        "job": pd,
                        "size": _human_size(sz),
                        "bytes": sz,
                        "mtime": int(mt * 1000),
                        "audio": raw_src if (raw_src and os.path.isfile(raw_src)) else "",
                        "has_chart": bool(has_chart),
                        "kind": "osn1",
                    })
                    history.append({
                        "name": f"{song} · OSN1转写",
                        "size": _human_size(sz),
                        "path": jf,
                        "source_dir": pd,
                        "mtime": int(mt * 1000),
                        "source_midi": jf,
                        "kind": "osn1",
                    })
                # ---- BSR ----
                cands = glob.glob(os.path.join(pd, "*_stems_combined.mid"))
                if not cands:
                    cands = glob.glob(os.path.join(pd, "*.mid"))
                elif cands:
                    fp = cands[0]
                    audio = ""
                    for c in (os.path.join(pd, "job", "input.wav"),
                              os.path.join(pd, "input.wav")):
                        if os.path.isfile(c):
                            audio = c
                            break
                    if not audio:
                        for sub in (os.path.join(pd, "job"), pd):
                            if not os.path.isdir(sub):
                                continue
                            for ext in (".wav", ".ogg", ".mp3", ".flac", ".m4a"):
                                hit = glob.glob(os.path.join(sub, "*" + ext))
                                if hit:
                                    audio = hit[0]
                                    break
                            if audio:
                                break
                    sz = os.path.getsize(fp)
                    mt = os.path.getmtime(fp)
                    song = os.path.basename(pd)
                    midis.append({
                        "name": os.path.basename(fp),
                        "song": song,
                        "path": fp,
                        "job": pd,
                        "size": _human_size(sz),
                        "bytes": sz,
                        "mtime": int(mt * 1000),
                        "audio": audio,
                        "has_chart": any(song.lower() in a for a in adofai_names),
                        "kind": "bsr",
                    })
                    history.append({
                        "name": os.path.basename(fp),
                        "size": _human_size(sz),
                        "path": fp,
                        "source_dir": pd,
                        "mtime": int(mt * 1000),
                        "source_midi": fp,
                        "kind": "bsr",
                    })

                # 分离音轨（OSN1 / BSR 通用：扫 <pd>/stems/*.wav）
                stem_dir = os.path.join(pd, "stems")
                if os.path.isdir(stem_dir):
                    wavs = sorted(glob.glob(os.path.join(stem_dir, "*.wav")))
                    if wavs:
                        sitems = []
                        latest = 0
                        for s in wavs:
                            smt = int(os.path.getmtime(s) * 1000)
                            if smt > latest:
                                latest = smt
                            sitems.append({"name": os.path.basename(s),
                                           "path": s, "mtime": smt})
                        stems.append({"job": pd, "song": song,
                                      "items": sitems, "mtime": latest})

        midis.sort(key=lambda x: x["mtime"], reverse=True)
        history.sort(key=lambda x: x["mtime"], reverse=True)
        stems.sort(key=lambda x: x["mtime"], reverse=True)
        return self._json({"ok": True, "midis": midis,
                           "history": history, "stems": stems})

    def _ws(self, q):
        """`GET /ws?token=…` —— 升到 WebSocket，交给 BDG 协议层。

        ★ **token 在升级之前校验**：宿主的文档明说「安装插件即视为信任、不弹权限确认」
          ⇒ 本机任何程序都可能连上来，校验是**我们**的责任（`docs/35` §3.8）。
        """
        if not WS.is_upgrade(self.headers):
            return self._json({"ok": False, "error": "需要 WebSocket 升级请求"}, 400)
        tok = (q.get("token") or [""])[0] or (self.headers.get("X-Bridge-Token") or "")
        if not WS.token_ok(APP.bridge.token, tok):
            APP.bridge.stats["rejected"] += 1
            return self._json({"ok": False, "error": "token 不匹配"}, 403)
        try:
            head = WS.handshake_response(self.headers)
        except WS.WsError as exc:
            return self._err(exc, 400)
        self.wfile.write(head)
        self.wfile.flush()
        # 升级之后这一条 socket 归协议层，HTTP 层不许再写
        self.close_connection = True
        conn = WS.Conn(self.rfile, self.wfile, self.connection)
        APP.bridge.serve(conn)
        return None

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = APP.broker.subscribe()
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    m = q.get(timeout=15.0)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                payload = json.dumps(m, ensure_ascii=False)
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError,
                OSError):
            pass
        finally:
            APP.broker.unsubscribe(q)

    def _media(self, path: str):
        if not path:
            raise BadRequest("缺少 path")
        p = APP.resolve_media(path)
        size = os.path.getsize(p)
        ctype = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
                 ".oga": "audio/ogg", ".flac": "audio/flac",
                 ".mid": "audio/midi", ".midi": "audio/midi"}.get(
                     os.path.splitext(p)[1].lower(), "application/octet-stream")
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        partial = False
        if rng and rng.startswith("bytes="):
            spec = rng[6:].split(",")[0].strip()
            try:
                a, _, b = spec.partition("-")
                if a:
                    start = max(0, int(a))
                    end = min(size - 1, int(b)) if b else size - 1
                elif b:
                    start = max(0, size - int(b))
                partial = True
            except ValueError:
                start, end, partial = 0, size - 1, False
        if start > end or start >= size:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(p, "rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                chunk = fh.read(min(1 << 16, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                left -= len(chunk)

    # --------------------------------------------------------------- POST
    def do_POST(self):                                       # noqa: N802
        u = urlparse(self.path)
        try:
            body = self._body()
        except BadRequest as exc:
            return self._err(exc, 400)
        try:
            return self._route(u.path, body)
        except BadRequest as exc:
            return self._err(exc, 400)
        except Exception as exc:                              # noqa: BLE001
            return self._err(exc, 500)

    def _route(self, path: str, body: dict):
        s = APP.session
        if path == "/api/load":
            return self._load(body)
        if path == "/api/cancel":
            APP.cancel_flag.set()
            return self._json({"ok": True})
        if path == "/api/tempo_diag":
            # ★ 自动贴合（`docs/58`）：纯体检，**不改任何状态/默认值**
            try:
                out = s.tempo_diag(body.get("state") or {})
            except Exception as exc:                        # noqa: BLE001
                import traceback
                out = {"ok": False, "why": str(exc),
                       "trace": traceback.format_exc()[-2000:]}
            return self._json(out)
        if path == "/api/derive":
            return self._json({"ok": True, **s.derive(body.get("state") or {})})
        if path == "/api/rebuild":
            t0 = time.perf_counter()
            out = s.rebuild(body.get("state") or {})
            out["elapsed_ms"] = (time.perf_counter() - t0) * 1000.0
            return self._json(out)
        if path == "/api/cap_times":
            return self._json({"ok": True, "cap": s.cap_times(body.get("state") or {})})
        if path == "/api/beat_grid":
            return self._json({"ok": True,
                               "grid": s.beat_grid_ms(int(body.get("division", 4)))})
        if path == "/api/audio":
            p = s.audio_for_current(body.get("state") or {})
            if not p:
                return self._json({"ok": False, "error": "没有可用音频"})
            APP.allow(p)
            return self._json({"ok": True, "path": p,
                               "url": "/media?path=" + _quote(p)})
        if path == "/api/leveljson":
            lj = s.level_json(body.get("state") or {})
            if lj is None:
                return self._json({"ok": False, "error": "还没有生成谱面"})
            return self._json({"ok": True, "level": lj})
        if path == "/api/export":
            d = body.get("dir")
            if not d:
                raise BadRequest("缺少导出目录")
            out = s.export(body.get("state") or {}, d)
            if out.get("ok"):
                APP.broker.push("export", dir=out["dir"])
            return self._json(out)
        if path == "/api/allow":
            p = body.get("path")
            if p:
                APP.allow(p)
            return self._json({"ok": True})
        # ---------------------------------------------------------- BDG 往返（docs/38）
        if path == "/api/bridge/import":
            ons = body.get("onsets")
            if not ons:
                # ★ 默认载荷（`docs/42`）：我们的 onsets 带**源轨**，
                #   外加**双押单独一条泳道**（用户口径：双押单开一个轨道）
                ons = [{"t_ms": o.t_ms, "role": al.ROLE_MAIN,
                        "src_tracks": list(getattr(o, "src_tracks", ()) or ())}
                       for o in s.onsets]
                ons += s.dp_lane_onsets()
            # ★ 把我们谱的**时序锚**一起交过去（`docs/41` #2）：
            #   以前只推拍位、锚留宿主的，用户看到 BPM 对不上只能猜。
            #   `base_bpm`/`offset_ms` 给了就用给的（调试 / 手动指定）。
            bp, om = s.bridge_anchor(body.get("state") or {})
            if body.get("base_bpm") is not None:
                bp = float(body["base_bpm"])
            if body.get("offset_ms") is not None:
                om = float(body["offset_ms"])
            out = APP.bridge.push_import(ons, src=body.get("src") or "session",
                                         clear=body.get("clear", True),
                                         base_bpm=bp, offset_ms=om)
            if out.get("ok"):
                APP.broker.push("bridge", what="import", state=APP.bridge.state())
            return self._json(out)
        if path == "/api/bridge/adopt":
            out = APP.bridge.adopt()
            if out.get("ok"):
                # ★ 收回来的进度**钉进采音结果**：之后 rebuild 就用它（docs/38 §9.5）
                out["adopted"] = s.adopt_onsets(out.get("onsets") or [])
                APP.broker.push("bridge", what="adopt", state=APP.bridge.state())
            return self._json(out)
        if path == "/api/bridge/clear":
            s.clear_override()
            APP.broker.push("bridge", what="clear", state=APP.bridge.state())
            return self._json({"ok": True})
        # ------------------------------------------------- 收回轨道项目（docs/45）
        # ★ 插件面板上那个「返回数据到谱面生成器」按钮走的是 **WS**（`type:"tracks"`），
        #   这里两个 HTTP 口是给**界面**用的：看一眼收回来什么、清掉、重新落地。
        if path == "/api/bridge/back":
            return self._json(_back_view(s, body.get("state") or {}))
        if path == "/api/bridge/back/clear":
            out = s.clear_back()
            APP.broker.push("bridge", what="back_clear", state=APP.bridge.state())
            return self._json(out)
        if path == "/api/bridge/back/apply":
            # 手动重新落地一次（比如回放/调试）：载荷从 body 给，或在桥里取最近一次
            payload = body.get("payload") or APP.bridge.back
            if not payload:
                return self._json({"ok": False, "error": "没有收回过轨道（先在 BDG 面板点「返回数据到谱面生成器」）"})
            out = s.restore_tracks(payload)
            APP.broker.push("bridge", what="back", state=APP.bridge.state())
            return self._json(out)
        if path == "/api/bridge/segments":
            # ★ 分段采音的自动填充：把 BDG 角色轨上的点编译成 `from` 模式的段落
            out = APP.bridge.segments_payload()
            if out.get("ok"):
                APP.broker.push("bridge", what="segments", state=APP.bridge.state())
            return self._json(out)
        # ---------------------------------------------------------- 启动并桥接（docs/40）
        if path == "/api/host/start":
            if APP.host_starting:
                return self._json({"ok": False, "error": "已经有一次「启动并桥接」在跑"}, 409)
            url = APP.bridge.url(port=self.server.server_address[1])
            APP.host_starting = True

            def work():
                try:
                    out = APP.hostctl.start_and_bridge(
                        url,
                        progress=lambda f, m: APP.broker.push("host", frac=f, msg=m),
                        should_cancel=APP.cancel_flag.is_set)
                    APP.broker.push("host", frac=1.0, done=True, result=out)
                except BaseException as exc:                     # noqa: BLE001
                    APP.broker.push("host", done=True,
                                    result={"ok": False, "error": str(exc)})
                finally:
                    APP.host_starting = False

            threading.Thread(target=work, daemon=True).start()
            return self._json({"ok": True, "started": True, "url": url,
                               "note": "后台在起宿主；进度走 SSE（kind=host）"})
        if path == "/api/host/stop":
            out = APP.hostctl.stop_and_unbridge()
            APP.broker.push("host", done=True, result=out)
            return self._json(out)
        if path == "/api/host/inject":
            # 手动重推一次连接串（面板里点过连接、但换了端口时用）
            url = body.get("url") or APP.bridge.url(port=self.server.server_address[1])
            return self._json(APP.hostctl.inject_url(url))
        return self._json({"ok": False, "error": "未知接口 " + path}, 404)

    def _load(self, body: dict):
        path = body.get("path") or ""
        if not os.path.exists(path):
            raise BadRequest("文件不存在：%s" % path)
        if not APP.busy.acquire(blocking=False):
            return self._json({"ok": False, "error": "已有任务在跑"}, 409)
        APP.cancel_flag.clear()
        APP.allow(path)
        result: dict = {}

        def work():
            try:
                APP.broker.push("progress", frac=0.0, msg="开始…")
                info = APP.session.load(
                    path,
                    progress=lambda f, m: APP.broker.push("progress", frac=f, msg=m),
                    should_cancel=APP.cancel_flag.is_set)
                result.update({"ok": True, **info})
                APP.broker.push("progress", frac=1.0, msg="完成")
                APP.broker.push("loaded", path=path)
            except BaseException as exc:                      # noqa: BLE001
                import core.audio_onsets as ao
                if isinstance(exc, ao.ConversionCancelled):
                    result.update({"ok": False, "cancelled": True,
                                   "error": "已取消音频转换"})
                else:
                    result.update({"ok": False, "error": str(exc),
                                   "trace": traceback.format_exc()[-4000:]})
            finally:
                APP.busy.release()

        th = threading.Thread(target=work, daemon=True)
        th.start()
        th.join()
        return self._json(result)


def _quote(s: str) -> str:
    from urllib.parse import quote
    return quote(os.path.abspath(s), safe="")


def _back_view(s, state: dict) -> dict:
    """★ 收回的**轨道项目**视图（`docs/45`）—— GET / POST 共用。

    用户口径：「我们的工具使用的音轨就是 BDG 里面带时值数据的音轨」。
    所以这里既报「收了什么」，也把 `info`（= 三条音轨列表，收回模式下就是那些轨）
    一起交出去，前端 renderTracks 直接吃。
    """
    m = s.back_meta or {}
    return {
        "ok": bool(m), "meta": m, "text": s.back_text(),
        "info": (s.track_map() if s.midi is not None else None),
        "anchor": s.bridge_anchor(state or {}),
        "lanes": [{"name": l["name"], "role": l["role"],
                   "src_track": l["src_track"], "n": l["n"],
                   "ms_lo": l["ms_lo"], "ms_hi": l["ms_hi"]}
                  for l in (s.lanes_back or [])],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--bridge-token", default="",
                    help="固定 BDG 桥的一次性 token（不填则每次随机；"
                         "固定它 = 重启后插件那条 URL 仍然有效，可自动重连）")
    a = ap.parse_args(argv)

    global APP
    APP = App(a.root)
    APP.verbose = a.verbose
    if a.bridge_token:
        APP.bridge.token = a.bridge_token
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    print(f"[sidecar] listening on http://{a.host}:{a.port}  root={APP.root}",
          flush=True)
    print(f"[sidecar] BDG 桥: {APP.bridge.url(a.host, a.port)}", flush=True)
    try:
        srv.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
