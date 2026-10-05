# -*- coding: utf-8 -*-
"""backend.py — 驱动整条生成流水线。

流水线两环：
  ① audio-sep：音频 → MIDI（真实 4 步，见 to_midi()）
       1) ffmpeg 转 WAV 44.1kHz 立体声（BS-RoFormer 只吃 wav）
       2) BS-RoFormer-SW 分离 6 轨（piano/guitar/bass/other/drums/vocals）
       3) 对 5 条非人声轨逐轨跑 MuScriptor 转谱（精度档 small/medium/large）
       4) merge_stems.py 合成一个干净的 6 轨（含鼓）单文件 MIDI
     ⚠️ 不做分离、直接把整首丢给 MuScriptor 是错的：乐器互遮会严重掉准确率，
        且整首的 chunk 数极多、慢到看起来像卡死。
  ② Adofai-Chart-Generator（sidecar.server）：MIDI → ADOFAI 谱面

启动 chartgen：用 chartgen 自带 runtime/python 跑 `python -m sidecar.server --port <P>`，
CWD 设在 chartgen 目录（sidecar 包需从 chartgen 根直接 import；ROOT 由 sidecar/__init__.py
自动定为 chartgen 目录）。前端（Mica 窗口）通过本模块驱动 HTTP API：
    /api/load  -> 载入 MIDI / 时间戳JSON / BDG
    /api/derive -> 派生参数（自动贴合等）
    /api/rebuild -> 核心生成
    /api/export -> 导出 main.adofai + main.wav
进度走 SSE /api/events（暂未用，rebuild 同步等待即可）。
"""
import os
import re
import sys
import json
import time
import glob
import shutil
import threading
import collections
import subprocess
import http.client
import http.server
import socketserver
import urllib.parse
import urllib.request
import urllib.error

# chartgen 目录 = gui 的上级（adofai-studio）下的 chartgen
HERE = os.path.dirname(os.path.abspath(__file__))
STUDIO = os.path.abspath(os.path.join(HERE, ".."))
CHARTGEN = os.path.join(STUDIO, "chartgen")
AUDIOSEP = os.path.join(STUDIO, "audio-sep")
OUTPUT_DIR = os.path.join(STUDIO, "output")

CHARTGEN_PY = os.path.join(AUDIOSEP, "runtime", "python.exe")

# ----------------------------------------------------------- audio-sep 引擎路径
# ⚠️ 一律用 venv 里的 python 直调模块入口，不用 Scripts/*.exe：
#    audio-sep 的两个 venv 都是「绝对路径」创建的，整个文件夹被挪位后
#    *.exe 启动器内嵌的旧绝对路径就失效了（表现为 rc=1、零输出、零占用，
#    或 venv 重定向器弹框卡死）。直调 python 不吃这一套。
# 分离引擎解释器：开发树用 venv（3.13 + torch + onnxruntime）；
# 🔴 便携包**不带** 6.5GB venv，只有随包的极简 audio-sep/runtime（3.12，无 onnxruntime，
#    靠 ADOFAI_ORT_CLI=1 把 ONNX 推理外包给 C# AdofaiOrt.exe）⇒ venv 不存在时**必须回退 runtime**，
#    否则 osn1 通路第一步就抛「OSN1 引擎解释器缺失」（2026-09-27 群友实测：便携包生成必失败）。
_BSR_VENV = os.path.join(AUDIOSEP, "venv", "Scripts", "python.exe")
_BSR_RUNTIME = os.path.join(AUDIOSEP, "runtime", "python.exe")
BSR_PY = _BSR_VENV if os.path.isfile(_BSR_VENV) else _BSR_RUNTIME
MUS_PY = os.path.join(AUDIOSEP, "mus_env", "Scripts", "python.exe")    # MuScriptor (3.12 + torch)
PY312 = os.path.join(AUDIOSEP, "runtime", "python.exe")               # mus_env 的基座 (3.12)
BSR_MODELS = os.path.join(AUDIOSEP, "models", "bsroformer")
MUS_MODELS = os.path.join(AUDIOSEP, "models", "muscriptor")
MERGE_SCRIPT = os.path.join(AUDIOSEP, "merge_stems.py")
# 混合推理运行器：**只给 large 档用**（small/medium 整份上卡绰绰有余）
MUS_HYBRID = os.path.join(AUDIOSEP, "mus_hybrid.py")
BSR_MODEL_NAME = "roformer-model-bs-roformer-sw-by-jarredou"
WORK_ROOT = os.path.join(OUTPUT_DIR, ".work")   # 中间产物（WAV + 分轨 + MIDI）留在 D 盘

# 串联：分离出的 6 轨里只有 5 条非人声轨转 MIDI（人声不是乐器，不转）。
# 表与 audio-sep/sep_gui.py 的 STEMS 完全一致：(key, 标签, MIDI通道, GM音色号, 是否鼓)
STEMS = [
    ("piano",  "Piano",  0, 0,  False),
    ("guitar", "Guitar", 1, 25, False),
    ("bass",   "Bass",   2, 33, False),
    ("other",  "Other",  3, 48, False),
    ("drums",  "Drums",  9, 0,  True),
]

# 直调 bs_roformer.inference:main
#   （bs-roformer-infer.exe 的 entry point = bs_roformer.inference:main，
#     但那个 .exe 已随挪目录失效，这里等价复刻）
_BSR_SHIM = (
    "import sys\n"
    "from bs_roformer.inference import main\n"
    "sys.argv = ['bs-roformer-infer'] + sys.argv[1:]\n"
    "main()\n"
)


PORT = int(os.environ.get("CHARTGEN_PORT", "8765"))
BASE = "http://127.0.0.1:%d" % PORT

# 🔴 隐藏子进程控制台窗口：本程序用 pythonw 启动（无控制台），若起子进程时不带
#    CREATE_NO_WINDOW，每起一个控制台程序（ffmpeg / python.exe / netstat / taskkill）
#    都会在屏幕上闪一个黑框 —— 用户看到的就是"闪过 10 来个 Python 命令行框"。
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
_STARTUPINFO = None
if os.name == "nt":
    try:
        _STARTUPINFO = subprocess.STARTUPINFO()
        _STARTUPINFO.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        _STARTUPINFO.wShowWindow = 0    # SW_HIDE
    except Exception:
        _STARTUPINFO = None


def _popen_kw():
    """所有子进程统一加上「不弹黑框」参数。"""
    kw = {"creationflags": CREATE_NO_WINDOW}
    if _STARTUPINFO is not None:
        kw["startupinfo"] = _STARTUPINFO
    return kw

# 🔴 临时目录一律放 D 盘：Adofai-Chart-Generator预览音频用 tempfile.mkdtemp() 落盘，
#    不注入 TMP/TEMP 会写进 C:\Users\...\Temp（违反"绝不写 C 盘"铁律）。
TMP_DIR = os.path.join(STUDIO, "output", ".tmp")

_proc = None


def _http(method, path, body=None, timeout=300):
    url = BASE + path
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ----------------------------------------------------------- chartgen 子进程管理
def is_healthy():
    """后端是否就绪。"""
    try:
        with urllib.request.urlopen(BASE + "/api/health", timeout=2) as r:
            d = json.loads(r.read().decode("utf-8"))
            return bool(d.get("ok"))
    except Exception:
        return False


def _free_port(port):
    """杀掉占用指定端口的进程（常用于清掉上次会话残留的 sidecar）。

    sidecar 是长驻进程，若上次会话没干净退出，它会一直占着端口；而那个
    老进程是「没带 TMP/TEMP 重定向」起出来的，预览音会写进 C 盘。这里
    按端口定位并杀掉它，确保本会话起的是带正确重定向的干净进程。
    """
    try:
        # 注意：中文 Windows 的 netstat 输出是 GBK，必须用字节读 + 安全解码，
        # 否则 text=True 的 utf-8 解码会因非 ASCII 字符抛 UnicodeDecodeError。
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, timeout=15, **_popen_kw()).stdout
        out = out.decode("gbk", errors="replace")
        for line in out.splitlines():
            # 形如：TCP    127.0.0.1:8765    0.0.0.0:0    LISTENING    12345
            if ":%d" % port not in line or "LISTENING" not in line:
                continue
            pid = line.split()[-1]
            if pid.isdigit():
                try:
                    subprocess.run(["taskkill", "/PID", pid, "/F"],
                                   capture_output=True, timeout=15, **_popen_kw())
                except Exception:
                    pass
    except Exception:
        pass


def start(timeout=120):
    """启动 chartgen 子进程并等待就绪。

    幂等：本会话自己起的、且仍健康，直接返回。但若端口被「非本会话」起的
    sidecar 占用（上次会话残留），那个老进程没带 TMP/TEMP 重定向，预览音会
    落进 C 盘 —— 必须先清掉它再起干净的。
    """
    global _proc
    if _proc is None and is_healthy():
        # 端口被别人占了：杀掉残留，准备起一个带正确重定向的新进程
        _free_port(PORT)
        time.sleep(1)
    if is_healthy():
        return True
    if _proc is not None:
        try:
            _proc.kill()
        except Exception:
            pass
        _proc = None
    if not os.path.isfile(CHARTGEN_PY):
        raise RuntimeError("找不到 chartgen 运行时: %s" % CHARTGEN_PY)
    env = dict(os.environ)
    env["PYTHONPATH"] = CHARTGEN + os.pathsep + (env.get("PYTHONPATH") or "")
    # 临时目录重定向到 D 盘（预览 wav 落盘 + 其它临时产物）
    try:
        os.makedirs(TMP_DIR, exist_ok=True)
        env["TMP"] = TMP_DIR
        env["TEMP"] = TMP_DIR
        env["TMPDIR"] = TMP_DIR
    except Exception:
        pass
    _proc = subprocess.Popen(
        # ★ `--root` 显式给出仓库根（core/ patterns/ samples/ 都在这）——
        #   Adofai-Chart-Generator主进程也是这么起的（main.js: argv = [... '--root', ROOT]），
        #   只靠 cwd 推断在某些启动方式下会踩空。
        [CHARTGEN_PY, "-m", "sidecar.server", "--port", str(PORT),
         "--root", CHARTGEN],
        cwd=CHARTGEN, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **_popen_kw())
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_healthy():
            return True
        time.sleep(0.5)
    raise RuntimeError("sidecar.server 启动超时（%ds 内未就绪）" % timeout)


def stop():
    """停止 chartgen 子进程。"""
    global _proc
    if _proc is not None:
        try:
            _proc.kill()
        except Exception:
            pass
        _proc = None


# ----------------------------------------------------------- 前端网关（单端口同源）
# Adofai-Chart-Generator前端（gui/studio/）是 ES module + fetch + SSE，而对它有两道硬约束：
#   · file:// 下 ES module 会被 Chromium 的 CORS 拦死（WebView2 不像 Electron 那样宽松）；
#   · sidecar **不发** Access-Control-Allow-Origin，跨端口 fetch 一样会被拦。
# 所以这里用「一个端口同时干两件事」，让前端与 API **天然同源**、零 CORS：
#   ① 静态：/            → 伺服 studio/（Adofai-Chart-Generator完整前端）
#   ② 反代：/api/* /media → 打到内部 sidecar 端口（含 SSE 流式、Range 透传）
GATEWAY_PORT = int(os.environ.get("STUDIO_GATEWAY_PORT", "8766"))
# ★ 自有 Mica 壳「重新排版移植」工作台（gui/workbench/），同源伺服。
#   旧的合作方 Adofai-Chart-Generator 前端（gui/studio/）已弃用并删除，不再伺服。
WORKBENCH_WEB = os.path.join(HERE, "workbench")
_gw = None


class _GwHandler(http.server.SimpleHTTPRequestHandler):
    """同源网关：静态前端 + 反代 sidecar。"""

    server_version = "ADOFAIStudio/1.0"
    protocol_version = "HTTP/1.1"          # SSE 要长连接

    def __init__(self, *a, **kw):
        kw["directory"] = HERE
        super().__init__(*a, **kw)

    def log_message(self, fmt, *args):     # 静音，别刷屏
        pass

    def translate_path(self, path):
        """静态根：其余 → gui/（本工具自己的 Mica 壳导入页）"""
        p = urllib.parse.unquote(urllib.parse.urlparse(path).path)
        base = HERE
        rel = p.lstrip("/")
        base = os.path.normpath(base)
        target = os.path.normpath(os.path.join(base, rel.replace("/", os.sep)))
        if target != base and not target.startswith(base + os.sep):
            return base                      # 挡目录穿越
        return target

    def end_headers(self):
        try:
            self.send_header("Cache-Control", "no-store")
        except Exception:
            pass
        super().end_headers()

    @staticmethod
    def _is_api(path):
        p = urllib.parse.urlparse(path).path
        return p.startswith("/api/") or p in ("/media", "/ws")

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        try:
            self.wfile.write(b)
        except Exception:
            pass

    def _proxy(self, method):
        path = urllib.parse.urlparse(self.path).path
        if path == "/ws":
            # BDG 桥的 WebSocket 升级要走裸隧道，暂不做（不影响出谱主链路）
            return self._json(501, {"ok": False, "error": "网关暂不支持 WebSocket"})
        try:
            ln = int(self.headers.get("Content-Length") or 0)
        except Exception:
            ln = 0
        body = self.rfile.read(ln) if ln > 0 else None
        hdrs = {}
        for k, v in self.headers.items():
            if k.lower() in ("host", "connection", "accept-encoding"):
                continue
            hdrs[k] = v
        try:
            conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=3600)
            conn.request(method, self.path, body=body, headers=hdrs)
            resp = conn.getresponse()
        except Exception as e:
            return self._json(502, {"ok": False, "error": "sidecar 不可达：%r" % (e,)})
        ctype = resp.getheader("Content-Type") or ""
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in ("transfer-encoding", "connection", "content-length"):
                continue
            self.send_header(k, v)
        if "text/event-stream" in ctype:
            # SSE：逐行转发 + 立刻 flush（绝不能等 read() 收完 —— 那是长连接）
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                while True:
                    line = resp.fp.readline()
                    if not line:
                        break
                    self.wfile.write(line)
                    self.wfile.flush()
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            return
        data = resp.read()
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/media_local":
            # ★ 受控本地媒体路由：仅允许伺服本项目目录（STUDIO）内的真实文件，
            #   支持 Range（<audio> 拖进度必需）。sidecar 的 /media 有白名单、
            #   output/.work 下的分轨 wav 会被拒，故这里单独开一条同源通道。
            return self._media_local()
        if self._is_api(self.path):
            return self._proxy("GET")
        return super().do_GET()

    def do_HEAD(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/media_local":
            return self._media_local(head=True)
        if self._is_api(self.path):
            return self._proxy("HEAD")
        return super().do_HEAD()

    @staticmethod
    def _media_local_allowed(abspath):
        abspath = os.path.normpath(os.path.abspath(abspath))
        for root in (STUDIO,):
            r = os.path.normpath(os.path.abspath(root))
            if abspath == r or abspath.startswith(r + os.sep):
                return True
        return False

    def _media_local(self, head=False):
        q = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(q)
        p = (params.get("path") or [None])[0]
        if not p or not os.path.isfile(p) or not self._media_local_allowed(p):
            return self._json(403, {"ok": False, "error": "非法或不存在的路径（仅限本项目目录）"})
        fp = os.path.abspath(p)
        try:
            size = os.path.getsize(fp)
            ctype = self.guess_type(fp) or "application/octet-stream"
            rng = self.headers.get("Range")
            if rng and rng.startswith("bytes="):
                spec = rng[len("bytes="):].split(",")[0].strip()
                if "-" in spec:
                    start_s, end_s = spec.split("-", 1)
                    start = int(start_s) if start_s else 0
                    end = int(end_s) if end_s else size - 1
                    if end >= size:
                        end = size - 1
                    length = end - start + 1
                    with open(fp, "rb") as f:
                        f.seek(start)
                        data = f.read(length)
                    self.send_response(206)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Accept-Ranges", "bytes")
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
                    self.send_header("Content-Length", str(length))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    if not head and data:
                        self.wfile.write(data)
                    return
            with open(fp, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if not head and data:
                self.wfile.write(data)
        except Exception as e:
            return self._json(500, {"ok": False, "error": "读取失败：%r" % (e,)})

    def do_POST(self):
        if self._is_api(self.path):
            return self._proxy("POST")
        return self._json(404, {"ok": False, "error": "未知路径"})


class _GwServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def gateway_start():
    """起同源网关（幂等），返回端口。"""
    global _gw, GATEWAY_PORT
    if _gw is not None:
        return GATEWAY_PORT
    # ★ 自有工作台优先；Adofai-Chart-Generator studio/ 仍在则一并允许（兼容过渡期）
    if not os.path.isdir(WORKBENCH_WEB):
        raise RuntimeError("找不到前端目录: %s" % (WORKBENCH_WEB,))
    _free_port(GATEWAY_PORT)               # 清掉上次会话残留
    try:
        httpd = _GwServer(("127.0.0.1", GATEWAY_PORT), _GwHandler)
    except OSError:
        httpd = _GwServer(("127.0.0.1", 0), _GwHandler)   # 退而求其次：随机空闲端口
    GATEWAY_PORT = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    _gw = httpd
    return GATEWAY_PORT


def gateway_base():
    """前端工作台基址。"""
    return "http://127.0.0.1:%d" % GATEWAY_PORT


def gateway_stop():
    global _gw
    if _gw is not None:
        try:
            _gw.shutdown()
        except Exception:
            pass
        _gw = None


# ----------------------------------------------------------- chartgen HTTP 客户端
def schema():
    return _http("GET", "/api/schema", timeout=15)


def load(path):
    return _http("POST", "/api/load", {"path": path}, timeout=300)


def derive(state):
    return _http("POST", "/api/derive", {"state": state}, timeout=180)


def rebuild(state):
    return _http("POST", "/api/rebuild", {"state": state}, timeout=900)


def export(state, out_dir):
    return _http("POST", "/api/export", {"dir": out_dir, "state": state}, timeout=180)


# ----------------------------------------------------------- 镜像接口（前端用）
def get_schema():
    """拉取Adofai-Chart-Generator全部微调参数 schema（约 60 字段 / 8 组）。"""
    start()
    return schema()


def leveljson(state):
    """取当前谱面的 .adofai JSON（预览用），无谱面时返回 None。"""
    start()
    r = _http("POST", "/api/leveljson", {"state": state}, timeout=120)
    return r.get("level") if r.get("ok") else None


def preview_audio(state):
    """取当前谱面预览音频的绝对路径（Adofai-Chart-Generator合成节拍音 / 原曲），无则 None。"""
    start()
    r = _http("POST", "/api/audio", {"state": state}, timeout=300)
    return r.get("path") if r.get("ok") else None


def latest_combined_midi():
    """找 output/.work 下最新一次生成的合成 MIDI（*_stems_combined.mid），无则 None。
    用于「直接进工作台」时自动载入上一次产物，省得用户再手挑文件。"""
    work = os.path.join(OUTPUT_DIR, ".work")
    if not os.path.isdir(work):
        return None
    cands = glob.glob(os.path.join(work, "*", "*_stems_combined.mid"))
    if not cands:
        cands = glob.glob(os.path.join(work, "*", "*.mid"))
    if not cands:
        return None
    cands.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return cands[0]


# ----------------------------------------------------------- ① audio-sep 转写链路
def _find_ffmpeg():
    hits = glob.glob(os.path.join(AUDIOSEP, "ffmpeg", "**", "ffmpeg.exe"), recursive=True)
    return hits[0] if hits else None


def _read_pyvenv_home(cfg_path):
    try:
        with open(cfg_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.strip().lower().startswith("home"):
                    return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return None


def fix_env():
    """自愈：校正 venv 的 pyvenv.cfg 基座路径。

    audio-sep 的 venv 不可重定位（pyvenv.cfg 写死绝对路径）。把整个 audio-sep
    文件夹挪个位置，mus_env 的基座就指空了 -> Scripts/python.exe 会弹出模态
    错误框并卡死（进程不退出、GPU/CPU 零占用）。
    这里在每次调用引擎前把 mus_env 的基座纠正到「随包自带的 audio-sep/runtime」，
    之后随便挪目录都能自愈。返回问题列表（空 = 正常）。
    """
    problems = []

    mus_cfg = os.path.join(AUDIOSEP, "mus_env", "pyvenv.cfg")
    if os.path.isfile(mus_cfg):
        want = os.path.dirname(PY312)
        cur = _read_pyvenv_home(mus_cfg)
        if (cur or "").lower() != want.lower():
            try:
                with open(mus_cfg, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write("home = %s\n" % want)
                    fh.write("include-system-site-packages = false\n")
                    fh.write("version = 3.12.0\n")
                    fh.write("executable = %s\n" % PY312)
                    fh.write("command = %s -m venv %s\n"
                             % (PY312, os.path.join(AUDIOSEP, "mus_env")))
            except Exception as e:
                problems.append("无法修正 mus_env/pyvenv.cfg：%r" % (e,))
        if not os.path.isfile(PY312):
            problems.append("mus_env 基座缺失：%s" % PY312)

    # venv（分离引擎）的基座是一个外部 Python 3.13，不在包内，只做存在性检查
    venv_cfg = os.path.join(AUDIOSEP, "venv", "pyvenv.cfg")
    if os.path.isfile(venv_cfg):
        cur = _read_pyvenv_home(venv_cfg)
        if cur and not os.path.isfile(os.path.join(cur, "python.exe")):
            problems.append("分离引擎 venv 的基座不存在：%s" % cur)
    return problems


def _engine_env():
    env = dict(os.environ)
    ff = _find_ffmpeg()
    if ff:
        env["PATH"] = os.path.dirname(ff) + os.pathsep + env.get("PATH", "")
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # 🔴 便携包必须自洽：摘掉「用户级 site-packages」，禁止从宿主机偷依赖。
    #    Windows 普通 Python 会自动把 %APPDATA%\Python\Python3XX\site-packages 挂进
    #    sys.path 且优先级高于包内 site-packages ⇒ 开发机上依赖是"偷"来的也能跑，
    #    换台干净机器立刻 ModuleNotFoundError（2026-09-27 群友事故）。
    #    子进程在启动时读这个变量，故必须在 spawn 前放进 env。
    env["PYTHONNOUSERSITE"] = "1"
    env["HF_ENDPOINT"] = "https://hf-mirror.com"
    env["BS_ROFORMER_MODELS_PATH"] = BSR_MODELS
    # 🔴 便携包（BSR_PY 回退到 runtime，无 onnxruntime）必须把 ONNX 推理外包给 C# CLI，
    #    否则 osn1 通路第一步就因缺 onnxruntime 崩（对应 line 56 注释的既定设计）。
    #    开发树 venv 自带 onnxruntime 时不强制，保留 Python 直跑能力。
    if BSR_PY == _BSR_RUNTIME:
        env["ADOFAI_ORT_CLI"] = "1"
    # 临时目录也拉到 D 盘（引擎内部可能用 tempfile）
    try:
        os.makedirs(TMP_DIR, exist_ok=True)
        env["TMP"] = TMP_DIR
        env["TEMP"] = TMP_DIR
        env["TMPDIR"] = TMP_DIR
    except Exception:
        pass
    return env


_SPLIT_RE = re.compile(r"[\r\n]+")
_LINE_MAX = 400       # 单行上限：tqdm 进度条的单行可以长到几 MB，直接截断


def _short_cmd(cmd, n=260):
    s = " ".join('"%s"' % c if (" " in str(c)) else str(c) for c in cmd)
    return s if len(s) <= n else s[:n] + " …"


def _run_stream(cmd, env, on_log=None, cwd=None, timeout=3600, tail_lines=30):
    """跑子进程并逐行消费输出（防管道阻塞 + 给前端实时日志）。返回 (rc, 尾部行)。

    这里要同时对付三个会让人误判"卡死"的坑：

    1) **\\r 不分行**：ffmpeg / tqdm 用 \\r 原地刷进度条，不写 \\n。只按 \\n 读的话，
       整条进度会在结束时才作为"一行"涌出来 —— 界面全程看起来是死的。
       所以这里按 [\\r\\n] 同时切分。
    2) **静默期没有超时**：子进程若长时间不吐一个字，read() 会一直阻塞，
       原来的超时判断根本执行不到。改用看门狗线程兜底 kill。
    3) **静默期没有任何输出**：GPU 预热 / 大模型加载阶段可以静默好几分钟，
       看起来就像卡死。这里每 30 秒补一条 [..] 心跳，界面能看出"它还活着"。
    """
    tail = collections.deque(maxlen=tail_lines)
    if on_log:
        try:
            on_log("$ " + _short_cmd(cmd))
        except Exception:
            pass
    proc = subprocess.Popen(cmd, cwd=cwd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            bufsize=0, **_popen_kw())
    t0 = time.time()
    t0_wall = time.time()
    flag = {"timeout": False}
    last_emit = [t0]

    def _watchdog():
        next_beat = time.time() + 30
        while proc.poll() is None:
            now = time.time()
            if timeout and (now - t0_wall) > timeout:
                flag["timeout"] = True
                try:
                    proc.kill()
                except Exception:
                    pass
                return
            if now >= next_beat:
                next_beat = now + 30
                if on_log:
                    try:
                        on_log("[..] 引擎运行中 %ds（静默期属正常，继续等）"
                               % int(now - t0_wall))
                    except Exception:
                        pass
                last_emit[0] = now
            time.sleep(1.0)

    threading.Thread(target=_watchdog, daemon=True).start()

    def emit(raw):
        s = raw.strip()
        if not s:
            return
        if len(s) > _LINE_MAX:
            s = s[:_LINE_MAX] + " …"
        now = time.time()
        prev = tail[-1] if tail else None
        # tqdm 会反复重刷同一行（或前一行加长）：0.4s 内不再重复上报，避免刷爆界面
        if (s == prev or (prev and s.startswith(prev))) and (now - last_emit[0]) < 0.4:
            return
        last_emit[0] = now
        tail.append(s)
        if on_log:
            try:
                on_log(s)
            except Exception:
                pass

    buf = ""
    try:
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk.decode("utf-8", "replace")
            parts = _SPLIT_RE.split(buf)
            buf = parts.pop()
            for p in parts:
                emit(p)
            if len(buf) > 8192:     # 兜底：既无 \n 也无 \r 的巨怪行
                emit(buf)
                buf = ""
        if buf:
            emit(buf)
        proc.wait(timeout=120)
    finally:
        try:
            if proc.stdout is not None:
                proc.stdout.close()
        except Exception:
            pass
    if flag["timeout"]:
        raise RuntimeError("子进程超时（%ds 未结束）：%s" % (timeout, _short_cmd(cmd)))
    return proc.returncode, list(tail)


def _to_wav(inp, job, env, on_log=None):
    """[1/4] ffmpeg 转 WAV。文件名必须是 input.wav —— BS-RoFormer 按输入名输出 input_<stem>.wav。"""
    ff = _find_ffmpeg()
    if not ff:
        raise RuntimeError("未找到 ffmpeg（应在 %s/ffmpeg/**/ffmpeg.exe）" % AUDIOSEP)
    wav = os.path.join(job, "input.wav")
    rc, tail = _run_stream([ff, "-y", "-i", inp, "-ar", "44100", "-ac", "2", wav],
                           env, on_log=on_log, timeout=900)
    if rc != 0 or not os.path.isfile(wav):
        raise RuntimeError("ffmpeg 转 WAV 失败 (rc=%s)：%s" % (rc, "\n".join(tail[-8:])))
    return wav


def _separate(job, stems_dir, env, on_log=None, timeout=1800):
    """[2/4] BS-RoFormer-SW 分离 6 轨。job 里放 input.wav，输出到 stems_dir。"""
    ckpt = os.path.join(BSR_MODELS, BSR_MODEL_NAME, "BS-Rofo-SW-Fixed.ckpt")
    if not os.path.isfile(ckpt):
        raise RuntimeError("BS-RoFormer 权重缺失：%s" % ckpt)
    os.makedirs(stems_dir, exist_ok=True)
    cmd = [BSR_PY, "-c", _BSR_SHIM,
           "--input_folder", job, "--store_dir", stems_dir,
           "--models_dir", BSR_MODELS, "--device", "cuda"]
    rc, tail = _run_stream(cmd, env, on_log=on_log, timeout=timeout)
    if rc != 0:
        raise RuntimeError("BS-RoFormer 分离失败 (rc=%s)：%s" % (rc, "\n".join(tail[-8:])))
    got = [k for k, _l, _c, _p, _d in STEMS
           if os.path.isfile(os.path.join(stems_dir, "input_%s.wav" % k))]
    if not got:
        raise RuntimeError("分离跑完了但没找到任何分轨（%s）：%s"
                           % (stems_dir, "\n".join(tail[-8:])))
    return got


def _hybrid_runner_for(size):
    """large 档要不要走 CPU+GPU 混合推理？返回 mus_hybrid.py 路径或 None。

    🔴 只对 large 生效。原路径（`-d cuda`）会把整份权重先搬上卡：
       large 5.09 GiB 权重 + 5.09 GiB 状态字典 = 约 10.8 GB 峰值，8 GB 卡装不下，
       Windows 会把显存翻页到内存（不报 OOM，只是慢到十几分钟才加载完）。
       混合推理逐张量装载、按当前可用显存把前 K 层放 GPU，峰值不再超预算。

    关掉（回到原路径）：ADOFAI_MUS_HYBRID=0
    调层数/精度：ADOFAI_MUS_GPU_LAYERS=auto|N、ADOFAI_MUS_GPU_DTYPE=fp32|fp16
    """
    if os.environ.get("ADOFAI_MUS_HYBRID", "1").strip().lower() in ("0", "off", "false", "no"):
        return None
    if size != "large":
        return None
    return MUS_HYBRID if os.path.isfile(MUS_HYBRID) else None


def _transcribe_cmd(stem_wav, out_mid, size, weights):
    """[3/4] 单条 stem 的转谱命令。"""
    hyb = _hybrid_runner_for(size)
    if hyb:
        return [MUS_PY, hyb,
                "--gpu-layers", os.environ.get("ADOFAI_MUS_GPU_LAYERS", "auto"),
                "--gpu-dtype", os.environ.get("ADOFAI_MUS_GPU_DTYPE", "fp32"),
                "transcribe", stem_wav, "-o", out_mid,
                "--model", weights, "--device", "cuda"]
    return [MUS_PY, "-m", "muscriptor", "transcribe", stem_wav, "-o", out_mid,
            "--model", weights, "--device", "cuda"]


def _transcribe_stem(stem_wav, out_mid, size, env, on_log=None, timeout=3600):
    """[3/4] 单条 stem 跑 MuScriptor 转谱。返回 (是否成功, 文件大小, 尾部行)。"""
    weights = os.path.join(MUS_MODELS, size, "model.safetensors")
    cmd = _transcribe_cmd(stem_wav, out_mid, size, weights)
    if on_log and _hybrid_runner_for(size):
        on_log("[info] %s 档走 CPU+GPU 混合推理（mus_hybrid.py，层数=%s）"
               % (size, os.environ.get("ADOFAI_MUS_GPU_LAYERS", "auto")))
    rc, tail = _run_stream(cmd, env, on_log=on_log, timeout=timeout)
    ok = (rc == 0 and os.path.isfile(out_mid))
    return ok, (os.path.getsize(out_mid) if ok else 0), tail


def _merge(stems_dir, stem_mid_map, out_combined, env, on_log=None, timeout=600):
    """[4/4] 把各分轨 MIDI 合成一个干净的 6 轨（含鼓）单文件 MIDI。"""
    spec = {"out": out_combined, "ticks": 480,
            "stems": [{"label": label, "ch": ch, "prog": prog, "drums": drums,
                       "mid": stem_mid_map[key]}
                      for key, label, ch, prog, drums in STEMS if key in stem_mid_map]}
    spec_path = os.path.join(stems_dir, "_merge_spec.json")
    with open(spec_path, "w", encoding="utf-8") as fh:
        json.dump(spec, fh, ensure_ascii=False)
    rc, tail = _run_stream([MUS_PY, MERGE_SCRIPT, spec_path], env,
                           on_log=on_log, timeout=timeout)
    if rc != 0 or not os.path.isfile(out_combined):
        raise RuntimeError("合并 MIDI 失败 (rc=%s)：%s" % (rc, "\n".join(tail[-8:])))
    return out_combined


def to_midi(audio_path, size="medium", work_dir=None, on_log=None, on_stage=None,
            sep_timeout=1800, stem_timeout=3600):
    """audio-sep 的完整转写链路：音频 → MIDI（流水线第一环，共 4 步）。

    on_log(line)            —— 逐行实时日志（引擎自身输出）
    on_stage(n, name, det)  —— 阶段切换通知，n = 1/2/3/4
    返回 {"midi", "stems_dir", "work_dir", "stems": {键: mid}, "failed": [...]}
    """
    if size not in ("small", "medium", "large"):
        raise ValueError("精度档必须是 small/medium/large，收到: %r" % size)
    if not os.path.isfile(audio_path):
        raise RuntimeError("音频文件不存在: %s" % audio_path)
    for path, what in ((BSR_PY, "分离引擎解释器"), (MUS_PY, "MuScriptor 解释器"),
                       (MERGE_SCRIPT, "合并脚本")):
        if not os.path.isfile(path):
            raise RuntimeError("找不到%s：%s" % (what, path))
    probs = fix_env()
    if probs:
        raise RuntimeError("audio-sep 环境异常：" + "；".join(probs))
    weights = os.path.join(MUS_MODELS, size, "model.safetensors")
    if not os.path.isfile(weights):
        raise RuntimeError("找不到 %s 档权重：%s\n（可在 audio-sep 里跑 "
                           "fetch_muscriptor_mirror.py --size %s 下载）" % (size, weights, size))

    song = os.path.splitext(os.path.basename(audio_path))[0] or "song"
    safe = "".join(c for c in song if c not in '\\/:*?"<>|').strip() or "song"
    work_dir = work_dir or os.path.join(WORK_ROOT, "%s_%d" % (safe, int(time.time())))
    job = os.path.join(work_dir, "job")        # BS-RoFormer 只吃文件夹
    stems_dir = os.path.join(work_dir, "stems")
    os.makedirs(job, exist_ok=True)
    os.makedirs(stems_dir, exist_ok=True)
    env = _engine_env()

    def stage(n, name, detail=""):
        if on_stage:
            try:
                on_stage(n, name, detail)
            except Exception:
                pass

    # 1) 转 WAV
    stage(1, "转换输入为 WAV", "ffmpeg → 44.1kHz 立体声")
    _to_wav(audio_path, job, env, on_log=on_log)

    # 2) BS-RoFormer 分离 6 轨
    stage(2, "BS-RoFormer-SW 分离 6 轨", "piano / guitar / bass / other / drums / vocals")
    _separate(job, stems_dir, env, on_log=on_log, timeout=sep_timeout)

    # 3) 逐轨 MuScriptor 转谱（分开转可避免乐器互遮，比丢整首准得多）
    stem_mid_map, failed = {}, []
    total = len(STEMS)
    for idx, (instr, label, _c, _p, _d) in enumerate(STEMS, 1):
        sw = os.path.join(stems_dir, "input_%s.wav" % instr)
        if not os.path.isfile(sw):
            failed.append("%s（未分离出该轨）" % label)
            if on_log:
                on_log("[warn] %s 轨不存在，跳过" % label)
            continue
        stage(3, "逐轨转谱 MuScriptor", "%d/%d  %s（%s 档）" % (idx, total, label, size))
        sm = os.path.join(stems_dir, "input_%s.mid" % instr)
        ok, sz, tail = _transcribe_stem(sw, sm, size, env, on_log=on_log,
                                        timeout=stem_timeout)
        if ok:
            stem_mid_map[instr] = sm
            note = "" if sz > 200 else "  ⚠ 几乎为空，该轨可能没有音符"
            if on_log:
                on_log("[ok] %s 完成 → %s（%d KB）%s"
                       % (label, os.path.basename(sm), sz // 1024, note))
        else:
            last = tail[-1] if tail else "无输出"
            failed.append("%s（%s）" % (label, last))
            if on_log:
                on_log("[err] %s 转谱失败：%s" % (label, last))
    if not stem_mid_map:
        raise RuntimeError("5 条非人声轨全都没转出 MIDI，无法继续")

    # 4) 合并
    stage(4, "合并为单文件多轨 MIDI", "%d 轨" % len(stem_mid_map))
    out_combined = os.path.join(work_dir, "%s_stems_combined.mid" % safe)
    _merge(stems_dir, stem_mid_map, out_combined, env, on_log=on_log)

    return {"midi": out_combined, "stems_dir": stems_dir, "work_dir": work_dir,
            "stems": stem_mid_map, "failed": failed}


# ----------------------------------------------------------- 串联：转谱 + 生成
def _stem_stats_from_json(json_path, on_log=None):
    """读回 OSN1 时间戳 JSON，统计每条轨的踩点数（供界面显示轨数）。

    单独抽出来是为了能被回归脚本直接测 —— 否则验一次就得跑 6 分钟 GPU。
    任何异常都只记一条 warn：JSON 已经在盘上，后面 load 照样能出谱。
    """
    stems_map = {}
    try:
        with open(json_path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
        for k, v in (payload.get("stems") or {}).items():
            stems_map[k] = {"onsets": len((v or {}).get("onsets_sec") or []),
                            "method": (v or {}).get("method", "")}
    except Exception as e:
        if on_log:
            on_log("[warn] 统计 OSN1 踩点轨失败（不影响出谱）：%s" % e)
    return stems_map


def _find_osn1_workdir(work_root, safe):
    """按歌名复用已分离的 work_dir：找 <work_root>/<safe>_osn1_* 中 stems/ 已含 6 路

    <nm>.wav 的目录返回，让重跑直接命中 osn1_stemjson 的分离缓存、跳过 4 分钟 GPU
    分离。最新匹配的优先；无则返 None（调用方新建时间戳目录）。
    """
    if not os.path.isdir(work_root):
        return None
    _names = ["bass", "drums", "other", "vocals", "guitar", "piano"]
    try:
        cand = sorted(
            d for d in os.listdir(work_root)
            if d.startswith(safe + "_osn1_") and os.path.isdir(os.path.join(work_root, d))
        )
    except OSError:
        return None
    for d in reversed(cand):
        sd = os.path.join(work_root, d, "stems")
        if all(os.path.isfile(os.path.join(sd, f"{n}.wav")) for n in _names):
            return os.path.join(work_root, d)
    return None


def to_stemjson_osn1(audio_path, work_dir=None, on_log=None, on_stage=None,
                     bpm_hint=None, timeout=3600):
    """OSN1 采点通路：音频 → 多轨时间戳 JSON（torch-free，替换 MuScriptor 逐轨转谱）。

    用 BSR-ONNX 分离 6 轨 + OnsetNet 多轨踩点，产出 chartgen 能直接 load 的时间戳
    JSON（与 MuScriptor 路径共用后半段 load → derive → rebuild）。
    解释器由 BSR_PY 统一解析：开发树 = audio-sep/venv（3.13, onnxruntime+librosa+cuda13）；
    便携包 = audio-sep/runtime（3.12, librosa）+ ADOFAI_ORT_CLI=1（ONNX 推理外包给
    C# AdofaiOrt.exe，故 runtime 不需要 onnxruntime）。
    """
    if not os.path.isfile(audio_path):
        raise RuntimeError("音频文件不存在: %s" % audio_path)
    if not os.path.isfile(BSR_PY):
        raise RuntimeError("OSN1 引擎解释器缺失：%s" % BSR_PY)
    stemjson = os.path.join(AUDIOSEP, "osn1_stemjson.py")
    if not os.path.isfile(stemjson):
        raise RuntimeError("osn1_stemjson.py 缺失：%s" % stemjson)
    probs = fix_env()
    if probs:
        raise RuntimeError("audio-sep 环境异常：" + "；".join(probs))

    song = os.path.splitext(os.path.basename(audio_path))[0] or "song"
    safe = "".join(c for c in song if c not in '\\/:*?"<>|').strip() or "song"
    # 复用已分离过的同名 work_dir（stems/ 含 6 路 wav），避免重复 GPU 分离
    if work_dir is None:
        work_dir = _find_osn1_workdir(WORK_ROOT, safe)
    if work_dir is None:
        work_dir = os.path.join(WORK_ROOT, "%s_osn1_%d" % (safe, int(time.time())))
    os.makedirs(work_dir, exist_ok=True)
    json_path = os.path.join(work_dir, "osn1_timestamps.json")

    env = _engine_env()
    cmd = [BSR_PY, stemjson, "--audio", audio_path, "--out", json_path]
    if bpm_hint:
        cmd += ["--bpm", str(bpm_hint)]
    if on_stage:
        try:
            on_stage(2, "OSN1 分离 + 采点", "BSR-ONNX 分离 6 轨 → OnsetNet 多轨踩点")
        except Exception:
            pass
    rc, tail = _run_stream(cmd, env, on_log=on_log, cwd=AUDIOSEP, timeout=timeout)
    if rc != 0 or not os.path.isfile(json_path):
        raise RuntimeError("OSN1 采点失败 (rc=%s)：%s" % (rc, "\n".join(tail[-8:])))

    # 读回 JSON，把每条轨的踩点数统计出来给界面显示。
    # 🔴 这里必须对齐 to_midi() 的返回形状（有 "stems" 字典）：
    #    之前 stems 恒为 {}，界面就会显示「生成完成（0/5 条轨…）」，
    #    看着像全失败，其实是把 OSN1 的轨数硬套了 MuScriptor 的显示口径。
    stems_map = _stem_stats_from_json(json_path, on_log=on_log)

    # "midi": None 是给旧调用方的兼容键 —— OSN1 通路本来就没有 MIDI，
    # 产物是多轨时间戳 JSON（chartgen 的 load 原生支持它）。
    # 老代码里 `info["midi"]` 这种硬取值曾把整条已跑完的链路报成失败，见 gen_cli.generated_payload。
    return {"json": json_path, "midi": None, "stems_dir": work_dir,
            "work_dir": work_dir, "stems": stems_map, "failed": []}


# ----------------------------------------------------------- 串联：转谱 + 生成
def generate(audio_path, size="medium", state=None, work_dir=None,
             on_log=None, on_stage=None, stem_timeout=3600,
             ensure_sidecar=True, mode="muscriptor"):
    """完整生成：① audio-sep 转写链路（4 步）→ ② chartgen load/derive/rebuild。

    返回 (rebuild_result, info, state)。info 见 to_midi()/to_stemjson_osn1()。
    微调界面在此结果上增量改 state 再调 derive/rebuild。

    ensure_sidecar：是否由本函数负责拉起/保活 chartgen sidecar。
        C# 新壳已经自己托管了 sidecar（端口一致），此时必须为 False，
        否则 start() 会误杀 C# 进程、把工作台网关打挂。
    mode：踩点方式。
        "muscriptor"（默认）= MuScriptor 逐轨转谱 → 合成 MIDI；
        "osn1"            = OSN1 自训练采点模型 → 多轨时间戳 JSON（torch-free）。
    """
    if ensure_sidecar:
        start()  # 确保 chartgen 后端起来
    if mode == "osn1":
        # OSN1 通路不吃 MuScriptor 权重，把用户填的 base_bpm 当 bpm_hint 写进 JSON
        bpm_hint = None
        if isinstance(state, dict) and state.get("base_bpm"):
            bpm_hint = state["base_bpm"]
        info = to_stemjson_osn1(audio_path, work_dir=work_dir, on_log=on_log,
                                on_stage=on_stage, bpm_hint=bpm_hint)
    else:
        info = to_midi(audio_path, size, work_dir=work_dir, on_log=on_log,
                       on_stage=on_stage, stem_timeout=stem_timeout)

    if on_stage:
        try:
            on_stage(5, "Adofai-Chart-Generator算法生成谱面", "MIDI/时间戳 → load → derive → rebuild")
        except Exception:
            pass

    # 取值一律 .get：OSN1 通路没有 "midi" 键，裸索引会在「链路已跑完」之后
    # 才抛 KeyError，把成功报成失败（2026-09-25 真机事故，见 gen_cli.generated_payload）。
    load_path = info.get("json") or info.get("midi") or ""
    load_r = load(load_path)
    if not load_r.get("ok"):
        raise RuntimeError("chartgen load 失败: %s" % load_r.get("error"))

    st = dict(state or {})
    # Adofai-Chart-Generator要求 tracks_checked，缺它 rebuild 报"没有可采音的音轨"；
    # 默认勾选启发式最优主轨（load 已算好 default_tracks_checked）。
    if "tracks_checked" not in st and load_r.get("default_tracks_checked"):
        st["tracks_checked"] = load_r["default_tracks_checked"]

    derive_r = derive(st)
    st = derive_r.get("state", st)
    rebuild_r = rebuild(st)
    return rebuild_r, info, st


# ----------------------------------------------------------- 保存：建音乐名子目录
def save(state, audio_path, song_name=None, timeout=180):
    """导出谱面到 OUTPUT_DIR/<音乐名>/，并把原音乐复制进去。

    Adofai-Chart-Generator export 内部已写到 <dir>/<song>/main/main.adofai（+main.wav）。
    我们额外把原音乐复制进 <song>/ 顶层，满足"和着音乐一起保存"。
    返回 (chart_path, song_dir)。
    """
    name = song_name or os.path.splitext(os.path.basename(audio_path or ""))[0] or "main"
    st = dict(state or {})
    st["song"] = name
    r = export(st, OUTPUT_DIR)
    if not r.get("ok"):
        raise RuntimeError("导出失败: %s" % r.get("error"))

    song_dir = os.path.join(OUTPUT_DIR, name)
    # 原音乐复制进同目录（与 adofai 一起）
    if audio_path and os.path.isfile(audio_path):
        dst = os.path.join(song_dir, os.path.basename(audio_path))
        try:
            if not os.path.exists(dst):
                shutil.copy2(audio_path, dst)
        except Exception:
            pass
    return r.get("chart"), song_dir
