"""gui_main.py — ADOFAI 谱面生成器 · 原生 Mica 窗口壳 + WebView2 网页界面

两层结构：
  - 第 1 层：Win32 无边框窗口 + DWM Mica 背景（系统级毛玻璃，透桌面壁纸）
  - 第 2 层：窗口里嵌 WebView2（Chromium 内核），加载 gui/index.html

本窗口是「重构后的前端」：只负责界面与编排，真正的算法交给Adofai-Chart-Generator 后端
（sidecar.server，纯标准库 HTTP 服务）。界面 ↔ 宿主通过 WebView2 WebMessage 通信：
  网页 -> 宿主：{type:'load'|'rebuild'|'export'|'drag'|'close', ...}
  宿主 -> 网页：{type:'loaded'|'rebuilt'|'exported'|'backend_status'|'log', ...}

双击请用「启动.bat」（pythonw 无黑框）。本文件也可直接 pythonw 运行。
"""
from __future__ import annotations
import os, sys, json, shutil, subprocess, threading, traceback, collections, time
import ctypes
import ctypes.wintypes as wt

# ---------- 路径 ----------
ROOT = os.path.dirname(os.path.abspath(__file__))          # .../gui
# STUDIO = gui 的上级目录（<REPO>）
STUDIO = os.environ.get("ADOFAI_STUDIO_ROOT") or os.path.dirname(ROOT)
CHARTGEN = os.path.join(STUDIO, "chartgen")                # Adofai-Chart-Generator算法后端
AUDIOSEP = os.path.join(STUDIO, "audio-sep")               # 分离前置
# 输出目录放 D 盘（不写 C 盘），历史/导出都在这
OUTPUT_DIR = os.path.join(STUDIO, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.environ.setdefault("ADOFAI_DATA_DIR", OUTPUT_DIR)

# 预览音频缓存：Adofai-Chart-Generator合成的预览 wav 复制进来，用虚拟主机 gui.cache 供 WebView2 播放
# （避免 file:// 页跨域拉 http://127.0.0.1 的 CORS / 混合内容问题）
PREVIEW_CACHE = os.path.join(OUTPUT_DIR, "preview_cache")
os.makedirs(PREVIEW_CACHE, exist_ok=True)

# 复用 backend 模块（chartgen sidecar.server 的进程管理 + HTTP 客户端，纯标准库）
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import backend

# ---------- 提前把线程初始化为 STA（WebView2 硬性要求单线程套间）----------
ole32 = ctypes.windll.ole32
ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
ole32.CoInitializeEx.restype = ctypes.HRESULT
try:
    _hr = ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
    dbg_msg = "[dbg] CoInitializeEx(STA) hr=%s" % hex(_hr & 0xFFFFFFFF)
    try:
        with open(os.path.join(ROOT, "gui_debug.log"), "a", encoding="utf-8") as _f:
            _f.write(dbg_msg + "\n")
    except Exception:
        pass
except Exception as e:
    try:
        with open(os.path.join(ROOT, "gui_debug.log"), "a", encoding="utf-8") as _f:
            _f.write("[dbg] CoInitializeEx 异常: %r\n" % (e,))
    except Exception:
        pass

import comtypes
import comtypes.client

# ---------- 其余路径 ----------
TLB  = os.path.join(ROOT, "WebView2.tlb")
LOADER = os.path.join(ROOT, "WebView2Loader.dll")
HTML_PATH = os.path.join(ROOT, "index.html")
# Adofai-Chart-Generator**完整工作台**：从Adofai-Chart-Generator Electron 的 resources/app.asar 解出来的 renderer
# （解包脚本 tools/unasar.js）。它才是「功能全」的那套界面（三栏 / 四标签预览 /
# 全参数检查器 / 播放器 / 报告区），我们自己的 index.html 只负责前置导入。
STUDIO_WEB = os.path.join(ROOT, "studio")
STUDIO_HTML = os.path.join(STUDIO_WEB, "index.html")
DATA_DIR = OUTPUT_DIR
WEBVIEW_CACHE = os.path.join(DATA_DIR, "webview_cache")
# UI 布局持久化（对应 Electron 的 <userData>/ui-layout.json）—— 一律落 D 盘
UI_LAYOUT_FILE = os.path.join(DATA_DIR, "ui-layout.json")
# 界面偏好（窗口材质）：同样是宿主侧属性，只能宿主自己记，前端 localStorage 帮不上忙
UI_PREFS_FILE = os.path.join(DATA_DIR, "ui-prefs.json")
AUDIOSEP_PY = os.path.join(AUDIOSEP, "runtime", "python.exe")
AUDIOSEP_GUI = os.path.join(AUDIOSEP, "sep_gui.py")

# ---------- 界面材质（schedule 第 2 条）----------
# (DWM backdrop preference, WebView 是否刷不透明底)
#   DWM: 1=NONE 2=MAINWINDOW(Mica) 3=TRANSIENTWINDOW(Acrylic) 4=TABBED
# 「透明」= 不施加任何系统材质（窗口露出 DWM 默认底），页面保持透明；
# 「纯色」= 同样不施材质，但把 WebView 与页面都刷成整片颜色 —— 于是不透明的观感才成立。
# ★ 2026-09-21 主人裁定：删掉「透明」档（不施材质那档），与 C# 壳 Materials.cs 同步。
#   它跟「纯色」本就共用一个 DWM kind，区别只在 WebView 底透不透，观感上几乎一样。
#   老配置里存的 "transparency" 由 _norm_material() 兜回默认档，不用清 ui-prefs.json。
MATERIALS = {
    "acrylic":      (3, False, "Acrylic"),
    "mica":         (2, False, "Mica"),
    # ★ 2026-09-22 主人要求加这一档（Win11 22H2 的 Tabbed，资源管理器带标签页那种）。
    #   插在 Mica 后面，三档系统材质排一起。⚠ DWM 里它是 4，不是 3（3 是亚克力）。
    #   新壳 Materials.cs 同步加了这一档，两边的名字/顺序必须一致。
    "tabbed":       (4, False, "Tabbed"),
    # ★ 2026-09-22 主人要求这一档的名字从「实色」改成「纯色」；同日晚再要求
    #   **四档 label 全英文**（他说中文档位名"太土"）⇒ 现在是 Solid。
    #   ⚠ 只改 label（第三个元素），别动键名 "solid" —— 它是落盘的配置值、也是前端认档位的键。
    "solid":        (1, True,  "Solid"),
}
DEFAULT_MATERIAL = "acrylic"

# ---------- 「关于」页读的 explain.md（schedule 第 3 条）----------
# 只认**程序目录**（不会被清理的地方）：gui/ 是我们自己的程序目录，output/ 是数据目录，
# 后者会被「清理临时文件」波及，所以不放进候选。
EXPLAIN_CANDIDATES = [
    os.path.join(ROOT, "explain.md"),
    os.path.join(STUDIO, "explain.md"),
    os.path.join(ROOT, "docs", "explain.md"),
]

# ---------- 训练采点模型（schedule 第 9 条）----------
# ⚠ 本项目仓库里**当前不存在**训练代码（见技能 adofai-audio-sep-inference）。
#   这里只做「入口 + 环境体检 + 缺件如实提示」，绝不代主人启动训练。
#   主人把训练脚本放进下面任一路径后，本页会自动点亮。
TRAIN_CANDIDATES = [
    os.path.join(AUDIOSEP, "train_onset.py"),
    os.path.join(STUDIO, "train", "train_onset.py"),
    os.path.join(ROOT, "train", "train_onset.py"),
]
TRAIN_DATA_CANDIDATES = [
    os.path.join(STUDIO, "dataset", "melody"),
    os.path.join(STUDIO, "train", "dataset"),
    os.path.join(AUDIOSEP, "dataset"),
]

# ---------- comtypes 生成 WebView2 接口 ----------
WV = comtypes.client.GetModule(TLB)

# ---------- ctypes 类型别名（ctypes.wintypes 缺这些）----------
LRESULT = ctypes.c_ssize_t
HCURSOR = wt.HANDLE
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

# ---------- Win32 常量 ----------
WM_DESTROY = 0x0002
WM_SIZE = 0x0005
WM_CLOSE = 0x0010
WM_SETICON = 0x0080
ICON_BIG = 1
ICON_SMALL = 0
WM_ERASEBKGND = 0x0014
WM_NCLBUTTONDOWN = 0x00A1
HTCAPTION = 2
WM_APP_LOG = 0x8000 + 1
WM_APP_DONE = 0x8000 + 2
WM_APP_DRAG = 0x8000 + 3
WM_APP_PICK = 0x8000 + 4
WM_APP_RESIZE = 0x8000 + 5
WM_APP_WEBMSG = 0x8000 + 6   # 把排队的「宿主->网页」消息投递出去（只能在 UI 线程做）
WM_APP_SELFTEST = 0x8000 + 7 # 开发自检（ExecuteScript 读回 DOM 状态）
WM_APP_DSHDLG = 0x8000 + 8   # 网页要原生对话框（openFile/openAudio/openDir）
WM_APP_NAV = 0x8000 + 9      # 切换页面（导入页 <-> Adofai-Chart-Generator）；wparam=1 回导入页，0 进工作台
WM_APP_STUDIOJS = 0x8000 + 10  # 在工作台页面里跑一段 JS（ExecuteScript 必须在 UI 线程）
WM_CONTEXTMENU = 0x007B   # 彻底屏蔽窗口右键菜单

# 窗口缩放 / 最小最大化
WM_NCCALCSIZE    = 0x0083
WM_NCHITTEST     = 0x0084
WM_GETMINMAXINFO = 0x0024
WM_SETCURSOR     = 0x0020
SW_MINIMIZE = 6
SW_MAXIMIZE = 3
SW_RESTORE  = 9
HTCLIENT = 1
HTLEFT = 10
HTRIGHT = 11
HTTOP = 12
HTTOPLEFT = 13
HTTOPRIGHT = 14
HTBOTTOM = 15
HTBOTTOMLEFT = 16
HTBOTTOMRIGHT = 17
IDC_SIZEWE   = 32644
IDC_SIZENS   = 32645
IDC_SIZENWSE = 32642
IDC_SIZENESW = 32643

# HTML 边缘热区 → 非客户区命中类型（发起标准 Windows 缩放）
EDGE_HT = {
    "left": HTLEFT, "right": HTRIGHT, "top": HTTOP,
    "bottom": HTBOTTOM, "topleft": HTTOPLEFT, "topright": HTTOPRIGHT,
    "bottomleft": HTBOTTOMLEFT, "bottomright": HTBOTTOMRIGHT,
}
WS_THICKFRAME  = 0x00040000
WS_SYSMENU     = 0x00080000
WS_MINIMIZEBOX = 0x00020000
WS_MAXIMIZEBOX = 0x00010000
WIN_MIN_W, WIN_MIN_H = 560, 420
RESIZE_BORDER = 6                  # 窗口边缘「可拖缩放」的厚度（页面自报用；也用于 WM_NCHITTEST 兜底）

DWMWA_SYSTEMBACKDROP_PREFERENCE = 38
DWMSBT_MAINWINDOW = 2          # Mica
DWMSBT_TRANSIENTWINDOW = 3     # Acrylic
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
DWMWA_USE_IMMERSIVE_DARK_MODE = 20

CS_HREDRAW = 0x0002
CS_VREDRAW = 0x0001
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
CW_USEDEFAULT = 0x80000000

WND_CLASS = "ADOFAI_GUI_Class"
# Adofai-Chart-Generator是三栏 + 多面板布局（它自己的 Electron 窗口默认 1560x940），
# 窗口开小了会挤成一条 —— 默认就跟它对齐。
WIN_W, WIN_H = 1560, 940

# ---------- 全局状态 ----------
HWND_MAIN = None
WEBVIEW_ENV = None
WEBVIEW_CTRL = None
WEBVIEW = None
WEBVIEW_MSG_HANDLER = None
ALIVE = []                     # 保活 COM handler，防 GC
log_q = collections.deque()
log_lock = threading.Lock()
# 「宿主 -> 网页」待发消息队列。必须在 UI 线程投递（见 post_to_web 注释）。
web_q = collections.deque()
web_q_lock = threading.Lock()
PAGE_READY = False             # 网页已跑起来（它 load 完会立刻发 get_schema）
_PROBE_TRIES = [0]             # 自检探针重探计数（界面没建好时回 PENDING，见 ScriptDone）
pick_ctx = {"dest": ""}
last_zoomed = False

# ---------- 后端状态 ----------
backend_busy = False
last_out = None
last_code = None
last_error = None

# ---------- Adofai-Chart-Generator（dsh 桥）状态 ----------
dsh_calls = {}                 # dsh_call id -> {"name","args"}（待 UI 线程处理）
dsh_call_lock = threading.Lock()
PENDING_STUDIO_LOAD = {"path": None}   # 生成完自动切工作台并载入的 MIDI 路径
STUDIO_JS_Q = collections.deque()      # 在工作台页面执行 JS（UI 线程投递）
STUDIO_JS_LOCK = threading.Lock()
NAV_URL = [None]                # 最近一次导航的目标 URL（调试用）

# ---------- WNDCLASSEX 结构 ----------
class WNDCLASSEX(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT),
                ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE),
                ("hIcon", wt.HICON), ("hCursor", HCURSOR),
                ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]

# ---------- 加载 DLL / 设置 argtypes ----------
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi
ole32 = ctypes.windll.ole32

user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEX)]
user32.RegisterClassExW.restype = wt.ATOM

# 图标加载
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x10
LR_DEFAULTSIZE = 0x40

user32.LoadImageW.argtypes = [wt.HINSTANCE, wt.LPCWSTR, wt.UINT, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.LoadImageW.restype = wt.HANDLE

user32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendMessageW.restype = LRESULT

user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.CreateWindowExW.restype = wt.HWND

user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.GetMessageW.restype = ctypes.c_long
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.TranslateMessage.restype = wt.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.DestroyWindow.argtypes = [wt.HWND]
user32.DestroyWindow.restype = wt.BOOL
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostQuitMessage.restype = None
user32.LoadCursorW.argtypes = [wt.HINSTANCE, wt.LPCWSTR]
user32.LoadCursorW.restype = HCURSOR
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL
user32.UpdateWindow.argtypes = [wt.HWND]
user32.UpdateWindow.restype = wt.BOOL
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
user32.ReleaseCapture.argtypes = []
user32.ReleaseCapture.restype = wt.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wt.POINT)]
user32.GetCursorPos.restype = wt.BOOL
user32.IsZoomed.argtypes = [wt.HWND]
user32.IsZoomed.restype = wt.BOOL
user32.SetCursor.argtypes = [HCURSOR]
user32.SetCursor.restype = HCURSOR
user32.LOWORD = lambda v: v & 0xFFFF
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetClientRect.restype = wt.BOOL
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.SystemParametersInfoW.argtypes = [wt.UINT, wt.UINT, ctypes.POINTER(wt.RECT), wt.UINT]
user32.SystemParametersInfoW.restype = wt.BOOL
SPI_GETWORKAREA = 0x0030   # 主屏去掉任务栏后的可用区

dwmapi.DwmSetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]
dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long


def _fit_webview(hwnd):
    """把 WebView2 **铺满整个客户区**（初始 & 每次 WM_SIZE 都要调）。

    🔴 2026-09-19 二次定案：**必须铺满（不再内缩）**。
       上一版为了「让父窗口拿到边缘命中区」把 WebView2 内缩了 RESIZE_BORDER(6) 像素，
       代价是窗口四周露出一圈 6px 的 Mica 底色 —— 壁纸偏亮时就是一圈发蓝的亮框，
       主人看到的就是「**边框填不满**」（2026-09-19 报障，附截图）。

       现在的做法：**页面自报边缘**。页面在 mousedown 时判断落点是否在最外
       RESIZE_BORDER 像素里，是则 `post({type:'resize', edge:'left'|'topright'|…})`
       → 宿主 WM_APP_RESIZE → `ReleaseCapture() + WM_NCLBUTTONDOWN(HTxxx)`，
       交给系统原生缩放循环（与顶栏拖窗口的 `type:'drag'` 同一条路，机制已验证）。
       两个入口各自带一份：
         · `gui/workbench/__dsh_bridge.js`（工作台页 + studio 页，DOMContentLoaded 注入）
         · `gui/index.html`（原生导入页，自带的 post()）

       保留 WM_NCHITTEST：WebView2 起不来时窗口仍有原生边缘命中区，不至于彻底拖不动。
    """
    if WEBVIEW_CTRL is None or not hwnd:
        return
    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    w = max(1, r.right - r.left)
    h = max(1, r.bottom - r.top)
    try:
        WEBVIEW_CTRL.Bounds = wt.RECT(0, 0, w, h)
    except Exception:
        pass


comdlg32 = ctypes.windll.comdlg32

class MINMAXINFO(ctypes.Structure):
    _fields_ = [("ptReserved", wt.POINT), ("ptMaxSize", wt.POINT),
                ("ptMaxPosition", wt.POINT), ("ptMinTrackSize", wt.POINT),
                ("ptMaxTrackSize", wt.POINT)]

class MARGINS(ctypes.Structure):
    _fields_ = [("cxLeftWidth", ctypes.c_int), ("cxRightWidth", ctypes.c_int),
                ("cyTopHeight", ctypes.c_int), ("cyBottomHeight", ctypes.c_int)]

dwmapi.DwmExtendFrameIntoClientArea.argtypes = [wt.HWND, ctypes.POINTER(MARGINS)]
dwmapi.DwmExtendFrameIntoClientArea.restype = ctypes.c_long

class OPENFILENAMEW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", wt.DWORD), ("hwndOwner", wt.HWND), ("hInstance", wt.HINSTANCE),
        ("lpstrFilter", wt.LPCWSTR), ("lpstrCustomFilter", wt.LPCWSTR),
        ("nMaxCustFilter", wt.DWORD), ("nFilterIndex", wt.DWORD),
        ("lpstrFile", wt.LPWSTR), ("nMaxFile", wt.DWORD),
        ("lpstrFileTitle", wt.LPCWSTR), ("nMaxFileTitle", wt.DWORD),
        ("lpstrInitialDir", wt.LPCWSTR), ("lpstrTitle", wt.LPCWSTR),
        ("Flags", wt.DWORD), ("nFileOffset", wt.WORD), ("nFileExtension", wt.WORD),
        ("lpstrDefExt", wt.LPCWSTR), ("lCustData", ctypes.POINTER(wt.LPARAM)),
        ("lpfnHook", ctypes.c_void_p), ("lpTemplateName", wt.LPCWSTR),
        ("pvReserved", ctypes.c_void_p), ("dwReserved", wt.DWORD), ("FlagsEx", wt.DWORD)]

comdlg32.GetOpenFileNameW.argtypes = [ctypes.POINTER(OPENFILENAMEW)]
comdlg32.GetOpenFileNameW.restype = wt.BOOL
OFN_FILEMUSTEXIST = 0x1000
OFN_PATHMUSTEXIST = 0x0800
OFN_NOCHANGEDIR = 0x0008

SRC_FILTER = "谱面工程\\0*.mid;*.midi;*.json;*.bdg\\0时间戳JSON\\0*_timestamps.json\\0MIDI\\0*.mid;*.midi\\0所有文件\\0*.*\\0\\0"
AUDIO_FILTER = "音频文件\\0*.mp3;*.wav;*.ogg;*.flac;*.m4a;*.aac\\0所有文件\\0*.*\\0\\0"

# ---------- 文件夹选择对话框（SHBrowseForFolderW）----------
shell32 = ctypes.windll.shell32
ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
ole32.CoTaskMemFree.restype = None

class BROWSEINFO(ctypes.Structure):
    # 🔴 lParam 必须是指针宽（LPARAM）。写成 c_long 的话 x64 上结构只有 56 字节、
    #    iImage 落在 52，而 Windows 会往 56 写 iImage ⇒ 结构外 4 字节被踩。
    #    正确布局：0/8/16/24/32/[4 填充]/40/48/56，sizeof=64。
    _fields_ = [
        ("hwndOwner", wt.HWND),
        ("pidlRoot", ctypes.c_void_p),
        ("pszDisplayName", wt.LPWSTR),
        ("lpszTitle", wt.LPCWSTR),
        ("ulFlags", ctypes.c_uint),
        ("lpfn", ctypes.c_void_p),
        ("lParam", wt.LPARAM),
        ("iImage", ctypes.c_int),
    ]

shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFO)]
shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, wt.LPWSTR]
shell32.SHGetPathFromIDListW.restype = wt.BOOL
BIF_RETURNONLYFSDIRS = 0x0001
BIF_NEWDIALOGSTYLE = 0x0040


def native_open_file():
    """原生打开文件对话框，返回选中文件的绝对路径或 None。"""
    buf = ctypes.create_unicode_buffer(2048)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.hwndOwner = HWND_MAIN
    ofn.lpstrFilter = wt.LPCWSTR(SRC_FILTER)
    ofn.lpstrFile = ctypes.cast(buf, wt.LPWSTR)
    ofn.nMaxFile = 2048
    ofn.lpstrTitle = wt.LPCWSTR("选择谱面工程（MIDI / 时间戳JSON / BDG）")
    ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR
    if comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
        return buf.value
    return None


def native_open_audio():
    """原生打开音频文件对话框（Adofai-Chart-Generator「选…」预览音源用），返回路径或 None。"""
    buf = ctypes.create_unicode_buffer(2048)
    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    ofn.hwndOwner = HWND_MAIN
    ofn.lpstrFilter = wt.LPCWSTR(AUDIO_FILTER)
    ofn.lpstrFile = ctypes.cast(buf, wt.LPWSTR)
    ofn.nMaxFile = 2048
    ofn.lpstrTitle = wt.LPCWSTR("选择音频文件（用于采音 / 预览音源）")
    ofn.Flags = OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR
    if comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
        return buf.value
    return None


def native_open_dir(title=None):
    """原生文件夹选择对话框（Adofai-Chart-Generator导出用），返回目录路径或 None。

    🔴 2026-09-20 修：`pszDisplayName` 字段类型是 LPWSTR(=c_wchar_p)，直接塞
    `ctypes.create_unicode_buffer()`（c_wchar_Array_260）会被 ctypes 拒掉：
        TypeError: incompatible types, c_wchar_Array_260 instance instead of c_wchar_p instance
    异常被 _do_dsh_native 吃掉 → 前端 openDir 的 Promise 变 reject → 无人接 →
    unhandledrejection ⇒ 界面上「点导出一点反应都没有」。
    必须像 native_open_file 那样 cast（数组本体留着，返回值仍从 disp.value 取）。
    """
    disp = ctypes.create_unicode_buffer(260)
    bi = BROWSEINFO()
    bi.hwndOwner = HWND_MAIN
    bi.pszDisplayName = ctypes.cast(disp, wt.LPWSTR)
    bi.lpszTitle = wt.LPCWSTR(title or "选择导出目录（会新建一个曲目文件夹）")
    bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_NEWDIALOGSTYLE
    pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
    if not pidl:
        return None
    out = ctypes.create_unicode_buffer(4096)
    shell32.SHGetPathFromIDListW(pidl, out)
    try:
        ole32.CoTaskMemFree(pidl)
    except Exception:
        pass
    return out.value or None


# ---------- UI 布局持久化（对应 Electron 的 <userData>/ui-layout.json）----------
def _read_ui_layout():
    try:
        if os.path.isfile(UI_LAYOUT_FILE):
            with open(UI_LAYOUT_FILE, "r", encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    return ""


def _write_ui_layout(text):
    try:
        with open(UI_LAYOUT_FILE, "w", encoding="utf-8") as f:
            f.write(text if text is not None else "")
    except Exception as e:
        _dbg("layoutSet err: %r" % (e,))


# ---------- 界面偏好（窗口材质）持久化 ----------
def _read_prefs():
    try:
        if os.path.isfile(UI_PREFS_FILE):
            with open(UI_PREFS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
    except Exception as e:
        _dbg("_read_prefs err: %r" % (e,))
    return {}


def _write_prefs(d):
    try:
        with open(UI_PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except Exception as e:
        _dbg("_write_prefs err: %r" % (e,))


def current_material():
    """当前材质名（读不到就回默认）。"""
    m = _read_prefs().get("material")
    return m if m in MATERIALS else DEFAULT_MATERIAL


def apply_material(name, save=True, notify=False):
    """把材质同时施到 DWM 与 WebView 底上，并（可选）落盘。

    - DWM backdrop preference 只管窗口背板材质
    - WebView 底管页面后面那层是透明还是不透明；两者必须一起切，
      只切一个会出现「DWM 画了亚克力但页面自己刷了一层实底」= 材质白给。
    """
    if name not in MATERIALS:
        name = DEFAULT_MATERIAL
    kind, opaque, _label = MATERIALS[name]
    try:
        if HWND_MAIN:
            set_backdrop(HWND_MAIN, kind)
        set_opaque(opaque)
        _dbg("apply_material(%s) kind=%d opaque=%s" % (name, kind, opaque))
    except Exception as e:
        _dbg("apply_material err: %r" % (e,))
    if save:
        d = _read_prefs()
        d["material"] = name
        _write_prefs(d)
    if notify:
        post_to_web({"type": "material", "name": name,
                     "kind": kind, "opaque": opaque})
    return name


def material_list():
    return [{"name": k, "label": v[2], "kind": v[0], "opaque": v[1]}
            for k, v in MATERIALS.items()]


def _dsh_info():
    """对应Adofai-Chart-Generator dsh.info()：version / port / electron。"""
    return {"version": "0.4.6", "port": backend.PORT,
            "electron": "ADOFAI Studio / WebView2"}


def _do_dsh_native(name, args):
    """执行一条 dsh_call（原生侧），返回 (ok, result, error)。"""
    try:
        if name == "openFile":
            return (True, native_open_file() or "", None)
        elif name == "openAudio":
            return (True, native_open_audio() or "", None)
        elif name == "openDir":
            o = args[0] if args else None
            title = (o or {}).get("title") if isinstance(o, dict) else None
            return (True, native_open_dir(title) or "", None)
        elif name == "reveal":
            p = args[0] if args else ""
            if p and os.path.exists(p):
                try:
                    # explorer /select 要显示窗口，不能用 _popen_kw 的 SW_HIDE
                    subprocess.Popen(["explorer", "/select,", p])
                except Exception:
                    pass
            return (True, "", None)
        elif name == "info":
            return (True, _dsh_info(), None)
        elif name == "layoutGet":
            return (True, _read_ui_layout(), None)
        elif name == "layoutSet":
            _write_ui_layout(args[0] if args else "")
            return (True, "", None)
        elif name == "backToImport":
            if HWND_MAIN:
                user32.PostMessageW(HWND_MAIN, WM_APP_NAV, 1, 0)   # 1 = 回导入页
            return (True, "", None)
        else:
            return (False, None, "未知 dsh 调用: %s" % name)
    except Exception as e:
        return (False, None, str(e))


def _handle_dsh_call(cid):
    with dsh_call_lock:
        call = dsh_calls.pop(cid, None)
    if not call:
        return
    ok, result, error = _do_dsh_native(call["name"], call.get("args", []))
    post_to_web({"type": "dsh_reply", "id": cid, "ok": ok,
                 "result": result, "error": error})


kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HINSTANCE


# ---------- DWM 辅助 ----------
def set_backdrop(hwnd, kind):
    v = ctypes.c_int(kind)
    dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_SYSTEMBACKDROP_PREFERENCE,
                                ctypes.byref(v), ctypes.sizeof(v))

def set_round(hwnd):
    v = ctypes.c_int(DWMWCP_ROUND)
    dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
                                ctypes.byref(v), ctypes.sizeof(v))

def set_dark(hwnd):
    v = ctypes.c_int(1)
    dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
                                ctypes.byref(v), ctypes.sizeof(v))

def extend_frame(hwnd):
    """把 DWM 玻璃/Acrylic 材质延伸到整个客户区（否则 WS_THICKFRAME 下 WebView2 透明背景糊成白底）。"""
    m = MARGINS(-1, -1, -1, -1)
    r = dwmapi.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(m))
    _dbg("DwmExtendFrameIntoClientArea hr=%s" % hex(r & 0xFFFFFFFF))


def _dbg(msg):
    try:
        with open(os.path.join(ROOT, "gui_debug.log"), "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


# ---------- WebView2 引导（C 导出 + comtypes 接口）----------
_loader = ctypes.windll.LoadLibrary(LOADER)
_create_env = _loader.CreateCoreWebView2EnvironmentWithOptions
_create_env.argtypes = [wt.LPCWSTR, wt.LPCWSTR, ctypes.c_void_p,
                        ctypes.POINTER(WV.ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler)]
_create_env.restype = ctypes.HRESULT


class EnvCompleted(comtypes.COMObject):
    _com_interfaces_ = [WV.ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler]
    def Invoke(self, result, environment):
        global WEBVIEW_ENV
        rh = hex(result & 0xFFFFFFFF) if isinstance(result, int) else result
        _dbg("EnvCompleted.Invoke result=%s env=%s" % (rh, "OK" if environment else "None"))
        if result < 0 or environment is None:
            _dbg("环境创建失败 -> 弹窗提示")
            ctypes.windll.user32.MessageBoxW(None,
                u"WebView2 环境创建失败\nHRESULT=%s\n\n可能原因：本机 WebView2 Runtime 与内置 SDK 不兼容，或首次运行需要联网下载。\n详细请查看 gui 目录下的 gui_debug.log" % rh,
                u"ADOFAI GUI", 0x10)
            return
        WEBVIEW_ENV = environment
        ALIVE.append(self)
        h = ControllerCompleted()
        ALIVE.append(h)
        iface = h.QueryInterface(WV.ICoreWebView2CreateCoreWebView2ControllerCompletedHandler)
        _dbg("CreateCoreWebView2Controller hwnd=%s" % HWND_MAIN)
        environment.CreateCoreWebView2Controller(int(HWND_MAIN), iface)


class ControllerCompleted(comtypes.COMObject):
    _com_interfaces_ = [WV.ICoreWebView2CreateCoreWebView2ControllerCompletedHandler]
    def Invoke(self, result, controller):
        global WEBVIEW_CTRL, WEBVIEW, WEBVIEW_MSG_HANDLER
        rh = hex(result & 0xFFFFFFFF) if isinstance(result, int) else result
        _dbg("ControllerCompleted.Invoke result=%s ctrl=%s" % (rh, "OK" if controller else "None"))
        if result < 0 or controller is None:
            _dbg("控制器创建失败 -> 弹窗提示")
            ctypes.windll.user32.MessageBoxW(None,
                u"WebView2 控制器创建失败\nHRESULT=%s\n\n可能父窗口句柄无效，或 WebView2 Runtime 异常。\n详见 gui_debug.log" % rh,
                u"ADOFAI GUI", 0x10)
            return
        WEBVIEW_CTRL = controller
        ALIVE.append(self)
        try:
            # 透明背景：让 Mica 透出来
            ctrl2 = controller.QueryInterface(WV.ICoreWebView2Controller2)
            if os.environ.get("ADOFAI_GUI_OPAQUE"):
                ctrl2.DefaultBackgroundColor = WV.COREWEBVIEW2_COLOR(A=255, R=20, G=20, B=24)
                _dbg("不透明调试背景已设 (A=255)")
            else:
                ctrl2.DefaultBackgroundColor = WV.COREWEBVIEW2_COLOR(A=0, R=0, G=0, B=0)
                _dbg("透明背景已设 (A=0)")
        except Exception as e:
            _dbg("设背景色失败(非致命): %r" % (e,))
        try:
            # ★ 铺满客户区（不再内缩）—— 内缩会在窗口四周留一圈 Mica 底色，主人报
            #   「边框填不满」。边缘缩放改由页面自报（type:'resize' → WM_APP_RESIZE），
            #   理由见 _fit_webview 的注释。
            _fit_webview(HWND_MAIN)
            webview = controller.CoreWebView2
            WEBVIEW = webview
            _dbg("已获取 CoreWebView2")
            try:
                # ⚠️ SetVirtualHostNameToFolderMapping 不在基础接口 ICoreWebView2 上，
                #    它从 ICoreWebView2_3 起才加入。之前直接对基础接口调用会抛
                #    AttributeError（日志里那句"虚拟主机映射失败"），导致预览音频的
                #    https://gui.cache/preview.wav 是死链 —— 预览放不出声。
                #    正确做法：QueryInterface 到 _3 再调。
                _access_kind = getattr(WV, 'COREWEBVIEW2_HOST_RESOURCE_ACCESS_KIND_ALLOW', 2)
                wv_map = webview
                for _ver in (3, 4, 5, 6, 7, 8, 9, 10):
                    iface_ver = getattr(WV, "ICoreWebView2_%d" % _ver, None)
                    if iface_ver is None:
                        continue
                    try:
                        wv_map = webview.QueryInterface(iface_ver)
                        _dbg("虚拟主机映射将使用 ICoreWebView2_%d" % _ver)
                        break
                    except Exception:
                        wv_map = webview
                wv_map.SetVirtualHostNameToFolderMapping(
                    "gui.assets", ROOT, _access_kind)
                _dbg("虚拟主机映射已设: gui.assets -> %s" % ROOT)
                wv_map.SetVirtualHostNameToFolderMapping(
                    "gui.cache", PREVIEW_CACHE, _access_kind)
                _dbg("虚拟主机映射已设: gui.cache -> %s" % PREVIEW_CACHE)
            except Exception as e:
                _dbg("虚拟主机映射失败(非致命): %r" % (e,))
            settings = webview.Settings
            settings.IsWebMessageEnabled = True
            try:
                settings.IsContextMenuEnabled = False
            except Exception as e:
                _dbg("禁用右键菜单(非致命): %r" % (e,))
            mh = MsgHandler()
            WEBVIEW_MSG_HANDLER = mh
            ALIVE.append(mh)
            iface = mh.QueryInterface(WV.ICoreWebView2WebMessageReceivedEventHandler)
            webview.add_WebMessageReceived(iface)
            _dbg("消息处理器已注册")
            if os.environ.get("ADOFAI_GUI_OPEN_STUDIO"):
                # 开发/验证用：直接进【自有 Mica 壳】工作台（重新排版移植版，/workbench）
                _studio_url = backend.gateway_base() + "/workbench/index.html"
                webview.Navigate(_studio_url)
                _dbg("Navigate 工作台(初始) 已调用: %s" % _studio_url)
            else:
                # 用同源网关伺服导入页（http://127.0.0.1:8766/index.html），
                # 这样页面里的 fetch / <audio src="/media_local?..."> 与 /api 天然同源零 CORS。
                # （早期用 file:// 会导致相对路径解析到 file:///media_local 而失败）
                _file_url = backend.gateway_base() + "/index.html"
                webview.Navigate(_file_url)
                _dbg("Navigate 导入页(网关) 已调用: %s" % _file_url)
        except Exception as e:
            _dbg("控制器初始化异常: %r" % (e,))
            import traceback
            _dbg(traceback.format_exc())
            ctypes.windll.user32.MessageBoxW(None,
                u"WebView2 界面加载异常: %r" % (e,),
                u"ADOFAI GUI", 0x10)


class MsgHandler(comtypes.COMObject):
    _com_interfaces_ = [WV.ICoreWebView2WebMessageReceivedEventHandler]
    def Invoke(self, sender, args):
        try:
            s = args.TryGetWebMessageAsString()
            if isinstance(s, tuple):
                s = s[-1]
            if not s:
                return
            handle_web_message(s)
        except Exception:
            pass


# ---------- 宿主 <-> 网页 通信 ----------
def post_to_web(obj):
    """把一条消息交给网页（线程安全）。

    🔴 这里是本工具最容易踩的坑：WebView2 的接口**只允许在它所属的 UI 线程调用**。
    do_generate / do_tune / backend_start 这些都跑在 threading.Thread 里，若在工作
    线程直接调 WEBVIEW.PostWebMessageAsString，属于跨套间的未定义调用 —— 结果是
    被 except 静默吞掉、消息全丢。症状就是：任务管理器里 GPU 早跑完了、预览音都
    生成好了，界面却永远停在"正在处理…"。

    正确姿势：工作线程只把 JSON 塞进队列 + PostMessageW 唤醒 UI 线程，
    由 UI 线程（wndproc 收到 WM_APP_WEBMSG）真正调用 WebView2 投递。
    """
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except Exception:
        return
    with web_q_lock:
        web_q.append(s)
        # 防呆：万一 UI 线程长时间不抽（比如被模态框卡住），别把内存顶爆；
        # 日志类消息丢老的即可。
        if len(web_q) > 4000:
            for _ in range(len(web_q) - 2000):
                web_q.popleft()
    if HWND_MAIN:
        user32.PostMessageW(HWND_MAIN, WM_APP_WEBMSG, 0, 0)


def _drain_web_queue(limit=300):
    """UI 线程专用：把排队的消息真正投给网页。"""
    if WEBVIEW is None or not PAGE_READY:
        return          # 网页还没就绪，消息留在队列里，等它就绪后再统一补发
    sent = 0
    while sent < limit:
        with web_q_lock:
            if not web_q:
                return
            s = web_q.popleft()
        try:
            WEBVIEW.PostWebMessageAsString(s)
        except Exception as e:
            _dbg("PostWebMessageAsString 失败: %r" % (e,))
            return
        sent += 1
    # 队列还有剩（日志很多时会走到这），再唤一次本消息继续抽
    if HWND_MAIN:
        user32.PostMessageW(HWND_MAIN, WM_APP_WEBMSG, 0, 0)


def push_log(text):
    with log_lock:
        log_q.append(text)
    if HWND_MAIN:
        user32.PostMessageW(HWND_MAIN, WM_APP_LOG, 0, 0)


def push_done(out, code, error=None):
    global last_out, last_code, last_error
    last_out, last_code, last_error = out, code, error
    if HWND_MAIN:
        user32.PostMessageW(HWND_MAIN, WM_APP_DONE, 0, 0)


# ---------- 页面切换（导入页 <-> Adofai-Chart-Generator）----------
def _navigate_studio():
    """导航到自有 Mica 壳工作台（经同源网关，零 CORS）。"""
    if WEBVIEW is None:
        return
    url = backend.gateway_base() + "/workbench/index.html"
    NAV_URL[0] = url
    try:
        WEBVIEW.Navigate(url)
        _dbg("Navigate 工作台: %s" % url)
    except Exception as e:
        _dbg("Navigate 工作台失败: %r" % (e,))


def _navigate_import():
    """导航回原生导入页（同源网关 http://127.0.0.1:8766/index.html）。"""
    if WEBVIEW is None:
        return
    url = backend.gateway_base() + "/index.html"
    NAV_URL[0] = url
    try:
        WEBVIEW.Navigate(url)
        _dbg("Navigate 导入页(网关): %s" % url)
    except Exception as e:
        _dbg("Navigate 导入页失败: %r" % (e,))


def _maybe_auto_load_studio():
    """工作台就绪后，若前面有「生成完自动载入 MIDI」的待办，现在执行。"""
    path = PENDING_STUDIO_LOAD.get("path")
    if not path:
        return
    PENDING_STUDIO_LOAD["path"] = None
    if not os.path.isfile(path):
        _dbg("auto_load_studio: MIDI 不存在，跳过: %s" % path)
        return
    _dbg("auto_load_studio: %s" % path)
    _exec_studio_js(
        "try{ if(window.__dsh && window.__dsh.load) window.__dsh.load(%s); }catch(e){}"
        % json.dumps(path))
    _probe_studio_state(4.0)


def _probe_studio_state(delay=4.0):
    """载入后延迟读回工作台页面的真实状态，写进 gui_debug.log（排障就看这一行）。"""
    def _go():
        time.sleep(delay)
        _exec_studio_js(
            "(function(){try{"
            "if(window.__dsh&&window.__dsh.status){return 'STATUS:'+JSON.stringify(window.__dsh.status());}"
            "var t=document.body?document.body.innerText:'';"
            "return 'STUDIO:'+JSON.stringify({url:location.href,title:document.title,"
            "dsh:!!window.__dsh,winCtl:!!document.getElementById('win-ctl'),"
            "backBtn:!!document.getElementById('btn-back-import'),"
            "text:t.replace(/\\s+/g,' ').slice(0,300)});"
            "}catch(e){return 'STUDIO_ERR:'+e;}})()")
        if os.environ.get("ADOFAI_GUI_STUDIO_CLOSE"):
            _delayed_close(5.0)
    threading.Thread(target=_go, daemon=True).start()


def _exec_studio_js(script):
    """把一段 JS 排队，等 UI 线程（WM_APP_STUDIOJS）真正在工作台页面里执行。"""
    with STUDIO_JS_LOCK:
        STUDIO_JS_Q.append(script)
    if HWND_MAIN:
        user32.PostMessageW(HWND_MAIN, WM_APP_STUDIOJS, 0, 0)


def _drain_studio_js(limit=50):
    if WEBVIEW is None:
        return
    n = 0
    while n < limit:
        with STUDIO_JS_LOCK:
            if not STUDIO_JS_Q:
                return
            s = STUDIO_JS_Q.popleft()
        _exec_js(s)          # _exec_js 在 UI 线程调 WebView2.ExecuteScript
        n += 1


# ---------- 消息分发 ----------
def handle_web_message(s):
    global PAGE_READY
    try:
        d = json.loads(s)
    except Exception:
        return
    if not PAGE_READY:
        # 网页 load 完第一件事就是 post({type:'get_schema'})，收到即代表它已就绪，
        # 这时才能安全地把积压消息补发下去（早于此刻发的都会被网页丢掉）。
        PAGE_READY = True
        _dbg("网页已就绪，补发积压消息 %d 条" % len(web_q))
        if HWND_MAIN:
            user32.PostMessageW(HWND_MAIN, WM_APP_WEBMSG, 0, 0)
        # WebView 此刻才真正存在 ⇒ 把材质的「WebView 底」那半边补上。
        # 否则启动瞬间那次 set_opaque 会因为 WEBVIEW_CTRL 还是 None 而被静默跳过，
        # 结果：选了「实色」重启回来仍是透明的（DWM 半边生效、WebView 半边丢）。
        try:
            apply_material(current_material(), save=False)
        except Exception as _e:
            _dbg("页面就绪后补施材质失败: %r" % (_e,))
        _start_selftest()
    t = d.get("type")
    if t == "pickfile":
        global pick_ctx
        pick_ctx = {"dest": d.get("dest", "")}
        if HWND_MAIN:
            user32.PostMessageW(HWND_MAIN, WM_APP_PICK, 0, 0)
        return
    if t == "drag":
        if HWND_MAIN:
            user32.PostMessageW(HWND_MAIN, WM_APP_DRAG, 0, 0)
    elif t == "resize":
        # 页面自报的边缘缩放（WebView2 铺满客户区，父窗口拿不到 WM_NCHITTEST）。
        # 最大化时不给缩放（与 Windows 原生行为一致；页面侧也挡了一道）。
        if HWND_MAIN and not user32.IsZoomed(HWND_MAIN):
            ht = EDGE_HT.get(str(d.get("edge", "")))
            if ht is not None:
                user32.PostMessageW(HWND_MAIN, WM_APP_RESIZE, ht, 0)
    elif t == "winminimize":
        if HWND_MAIN:
            user32.ShowWindow(HWND_MAIN, SW_MINIMIZE)
    elif t == "winmaximize":
        if HWND_MAIN:
            user32.ShowWindow(HWND_MAIN,
                              SW_RESTORE if user32.IsZoomed(HWND_MAIN) else SW_MAXIMIZE)
    elif t == "close":
        if HWND_MAIN:
            user32.PostMessageW(HWND_MAIN, WM_CLOSE, 0, 0)
    elif t == "listhistory":
        send_history()
    elif t == "setbackdrop":
        if HWND_MAIN:
            set_backdrop(HWND_MAIN, int(d.get("mat", 2)))
    elif t == "setmaterial":
        # schedule 第 2 条：设置里切界面材质（透明 / 亚克力 / 云母 / 实色）
        apply_material(str(d.get("name") or ""), save=True, notify=True)
    elif t == "setopaque":
        set_opaque(bool(d.get("on", False)))
    elif t == "listmidis":
        # schedule 第 1 条：工作台入口要「列出所有生成过的 MIDI」
        do_list_midis()
    elif t == "getexplain":
        # schedule 第 3 条：关于页读 explain.md
        do_get_explain()
    elif t == "mkexplain":
        do_mk_explain(force=bool(d.get("force")))
    elif t == "traininfo":
        # schedule 第 9 条：训练页环境体检（只报状态，绝不自动开训）
        do_train_info()
    elif t == "openfile":
        p = d.get("path")
        if p and os.path.isfile(p):
            try:
                os.startfile(p)
            except Exception as e:
                _dbg("openfile err: %r" % (e,))
    elif t == "openfolder":
        folder = d.get("folder", "")
        if folder and os.path.isdir(folder):
            try:
                os.startfile(folder)
            except Exception as e:
                _dbg("openfolder err: %r" % (e,))
        else:
            p = d.get("path", "")
            if p:
                _d = os.path.dirname(p)
                if os.path.isdir(_d):
                    os.startfile(_d)
    elif t == "deletehist":
        p = d.get("path")
        if p and os.path.isfile(p):
            try:
                os.remove(p)
                send_history()
            except Exception as e:
                _dbg("deletehist err: %r" % (e,))
    # ---------- 侧边栏各页所需数据 ----------
    elif t == "appinfo":
        do_appinfo()
    elif t == "liststems":
        do_list_stems()
    elif t == "cleanwork":
        do_clean_work()
    elif t == "open_in_workbench":
        do_open_in_workbench(d.get("path"))
    # ---------- 流水线：Mica 前端驱动 chartgen 后端 ----------
    elif t == "backend_start":
        threading.Thread(target=backend_start, daemon=True).start()
    elif t == "backend_status":
        post_to_web({"type": "backend_status",
                     "ok": backend.is_healthy(),
                     "port": backend.PORT})
    elif t == "get_schema":
        threading.Thread(target=do_get_schema, daemon=True).start()
    elif t == "generate":
        threading.Thread(target=do_generate,
                        args=(d.get("audio", ""), d.get("size", "medium")),
                        daemon=True).start()
    elif t == "tune":
        threading.Thread(target=do_tune, args=(d.get("state", {}),), daemon=True).start()
    elif t == "save":
        threading.Thread(target=do_save,
                        args=(d.get("audio", ""), d.get("state", {})),
                        daemon=True).start()
    elif t == "separate_launch":
        threading.Thread(target=do_separate_launch, daemon=True).start()
    # ---------- Adofai-Chart-Generator（dsh 桥）消息 ----------
    elif t == "dsh_call":
        cid = d.get("id")
        name = d.get("name")
        args = d.get("args", [])
        if cid is not None:
            try:
                cid = int(cid)
            except Exception:
                cid = None
            if cid is not None:
                with dsh_call_lock:
                    dsh_calls[cid] = {"name": name, "args": args}
                if HWND_MAIN:
                    user32.PostMessageW(HWND_MAIN, WM_APP_DSHDLG, cid, 0)
        return
    elif t == "dsh_ready":
        # 工作台前端就绪（app.js 末尾 window.dsh.ready(...)）。若有「生成完自动载入」待办，现在执行。
        _dbg("dsh_ready: %r" % (d.get("info"),))
        _maybe_auto_load_studio()
        return
    elif t == "workbench_status":
        # 工作台自检：结构化状态快照（schema 字段数 / 已加载文件 / 是否出谱 / 标签 / 控件数 / 错误）
        _dbg("WORKBENCH_STATUS %r" % (d.get("status"),))
        return
    elif t == "page_error":
        # 工作台页面里的 JS 运行时错误（全局兜底）。
        # ★ 必须带 src/line/col：只打 message 的话（例如 "reading 'toFixed' 报错"）
        #   根本定位不到是哪个文件哪一行抛的，等于瞎猜。
        _src = d.get("src") or ""
        if _src:
            _src = os.path.basename(str(_src))
        _dbg("PAGE_ERROR %s @%s:%s:%s" % (d.get("msg"), _src, d.get("line"), d.get("col")))
        return
    elif t == "page_log":
        # 工作台自检：分步流水线日志（定位 doLoad 卡在哪一步）
        _dbg("PAGE_LOG %r" % (d.get("step"),))
        return
    elif t == "dsh_error":
        _dbg("dsh_error: %s" % (d.get("msg"),))
        return
    elif t == "nav_studio" or t == "open_workbench":
        # 导入页点「工作台」按钮（open_workbench），或生成完的自动切换：都由宿主导航
        # ★ 手动点进来时若没有待载入谱面，就自动载入「最近一次生成的 MIDI」——
        #   否则工作台是个空壳（左栏没文件、没音轨，中间没谱面），
        #   用户会以为「界面上的东西全没了」（2026-09-19 实测就是这个现象）。
        if not PENDING_STUDIO_LOAD.get("path"):
            try:
                _auto = backend.latest_combined_midi()
            except Exception as e:
                _auto = None
                _dbg("open_workbench: 查最近 MIDI 失败 %r" % (e,))
            if _auto and os.path.isfile(_auto):
                PENDING_STUDIO_LOAD["path"] = _auto
                _dbg("open_workbench: 无待载入谱面，自动载入最近生成 %s" % _auto)
            else:
                _dbg("open_workbench: 无待载入谱面，也没有可自动载入的 MIDI（空工作台）")
        if HWND_MAIN:
            user32.PostMessageW(HWND_MAIN, WM_APP_NAV, 0, 0)   # 0 = 进工作台
        return


# ---------- 后端编排（驱动 chartgen sidecar.server）----------
def backend_start():
    try:
        backend.start(timeout=120)
        post_to_web({"type": "backend_status", "ok": True,
                     "msg": "Adofai-Chart-Generator算法后端已就绪（端口 %d）" % backend.PORT})
    except Exception as e:
        post_to_web({"type": "backend_status", "ok": False,
                     "msg": "后端启动失败：%s" % e})


def do_load(path):
    if not path or not os.path.isfile(path):
        post_to_web({"type": "loaded", "ok": False,
                     "msg": "请先选择有效的文件（MIDI / 时间戳JSON / BDG）"})
        return
    try:
        backend.start(timeout=60)
    except Exception as e:
        post_to_web({"type": "loaded", "ok": False, "msg": "后端未就绪：%s" % e})
        return
    try:
        r = backend.load(path)
    except Exception as e:
        post_to_web({"type": "loaded", "ok": False, "msg": "载入失败：%s" % e})
        return
    if not r.get("ok"):
        post_to_web({"type": "loaded", "ok": False, "msg": r.get("msg") or "载入失败"})
        return
    post_to_web({"type": "loaded", "ok": True,
                 "tracks": r.get("tracks", []),
                 "default_checked": r.get("default_tracks_checked", []),
                 "msg": r.get("msg", "载入成功")})


def do_rebuild(state):
    global backend_busy
    backend_busy = True
    post_to_web({"type": "rebuild_status", "text": "生成中（Adofai-Chart-Generator算法）…", "busy": True})
    try:
        backend.start(timeout=60)
        # 先 derive 再 rebuild，确保参数完整（勾主轨等）
        try:
            d = backend.derive(state)
            if d.get("ok"):
                state = d.get("state", state)
        except Exception:
            pass
        r = backend.rebuild(state)
    except Exception as e:
        backend_busy = False
        post_to_web({"type": "rebuilt", "ok": False, "msg": "生成异常：%s" % e})
        return
    backend_busy = False
    if not r.get("ok"):
        post_to_web({"type": "rebuilt", "ok": False, "msg": r.get("msg") or "生成失败"})
        return
    post_to_web({"type": "rebuilt", "ok": True,
                 "elapsed_ms": r.get("elapsed_ms"),
                 "msg": (r.get("msg") or ""),
                 "state": state})


def do_export(state, out_dir):
    if not out_dir:
        out_dir = os.path.join(OUTPUT_DIR, "export")
    os.makedirs(out_dir, exist_ok=True)
    try:
        backend.start(timeout=60)
        r = backend.export(state, out_dir)
    except Exception as e:
        post_to_web({"type": "exported", "ok": False, "msg": "导出异常：%s" % e})
        return
    if not r.get("ok"):
        post_to_web({"type": "exported", "ok": False, "msg": r.get("msg") or "导出失败"})
        return
    chart = r.get("chart") or os.path.join(out_dir, "main", "main.adofai")
    post_to_web({"type": "exported", "ok": True,
                 "chart": chart, "audio": bool(r.get("audio")),
                 "verify": r.get("verify", ""),
                 "msg": r.get("msg", "导出完成")})


def do_separate_launch():
    if not (os.path.isfile(AUDIOSEP_PY) and os.path.isfile(AUDIOSEP_GUI)):
        post_to_web({"type": "separate_launched", "ok": False,
                     "msg": "找不到 audio-sep：%s" % AUDIOSEP})
        return
    try:
        # 不带 CREATE_NO_WINDOW 就会弹一个控制台黑框（pythonw 起的子进程同样会）
        subprocess.Popen([AUDIOSEP_PY, AUDIOSEP_GUI], cwd=AUDIOSEP,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         **backend._popen_kw())
        post_to_web({"type": "separate_launched", "ok": True,
                     "msg": "已拉起 audio-sep 分离界面（独立窗口），分离后把产物喂回本工具"})
    except Exception as e:
        post_to_web({"type": "separate_launched", "ok": False, "msg": "拉起失败：%s" % e})


# ---------- 流水线编排（选音乐 → 选精度 → MuScriptor转谱+chartgen生成 → 微调 → 保存）----------
def preview_url_for(state):
    """把Adofai-Chart-Generator预览音频复制进 gui.cache 虚拟主机目录，返回可直接给 <audio> 的 URL。"""
    try:
        path = backend.preview_audio(state)
        if not path or not os.path.isfile(path):
            return None
        dst = os.path.join(PREVIEW_CACHE, "preview.wav")
        # 同文件不重复拷（靠内容 mtime 粗判即可）
        if not (os.path.exists(dst) and os.path.getmtime(dst) >= os.path.getmtime(path)):
            shutil.copy2(path, dst)
        return "https://gui.cache/preview.wav"
    except Exception as e:
        _dbg("preview_url_for err: %r" % (e,))
        return None


def do_get_schema():
    try:
        sc = backend.get_schema()
        post_to_web({"type": "schema", "ok": True, "schema": sc})
    except Exception as e:
        post_to_web({"type": "schema", "ok": False, "msg": "获取参数失败：%s" % e})


def _open_run_log(name, tag="gen"):
    """把整条链路的阶段/日志落盘到 output/logs/，事后可查（界面消息丢了也不怕）。"""
    try:
        d = os.path.join(OUTPUT_DIR, "logs")
        os.makedirs(d, exist_ok=True)
        safe = "".join(c for c in (name or "run") if c not in '\\/:*?"<>|') or "run"
        path = os.path.join(d, "%s-%s-%s.log" % (tag, safe, time.strftime("%Y%m%d-%H%M%S")))
        fh = open(path, "a", encoding="utf-8", buffering=1)
        fh.write("# ADOFAI Studio 运行日志  %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        return fh, path
    except Exception as e:
        _dbg("_open_run_log err: %r" % (e,))
        return None, None


def do_generate(audio, size):
    global backend_busy
    backend_busy = True
    if not audio or not os.path.isfile(audio):
        post_to_web({"type": "generated", "ok": False, "msg": "请先选择有效的音乐文件"})
        backend_busy = False
        return
    try:
        backend.start(timeout=120)
    except Exception as e:
        post_to_web({"type": "generated", "ok": False, "msg": "后端未就绪：%s" % e})
        backend_busy = False
        return
    try:
        name = os.path.splitext(os.path.basename(audio))[0] or "main"
    except Exception:
        name = "main"

    run_fh, run_path = _open_run_log(name)
    t0 = time.time()
    _dbg("generate 开始: %s (size=%s) 日志=%s" % (audio, size, run_path))

    def _file(line):
        if run_fh is None:
            return
        try:
            run_fh.write("[%7.1fs] %s\n" % (time.time() - t0, line))
        except Exception:
            pass

    def on_log(line):
        if not line:
            return
        _file(line)
        post_to_web({"type": "gen_log", "line": line})

    def on_stage(n, stage_name, detail=""):
        _file("=== 阶段 %s | %s | %s" % (n, stage_name, detail))
        _dbg("stage %s %s %s (%.0fs)" % (n, stage_name, detail, time.time() - t0))
        post_to_web({"type": "gen_stage", "n": n, "name": stage_name, "detail": detail})

    post_to_web({"type": "gen_stage", "n": 0, "name": "启动", "detail": "准备引擎…"})
    try:
        rebuild_r, info, state = backend.generate(audio, size, state=None,
                                                  on_log=on_log, on_stage=on_stage)
    except Exception as e:
        _file("!!! 生成失败: %s" % e)
        _dbg("generate 异常: %r" % (e,))
        post_to_web({"type": "generated", "ok": False, "msg": "生成失败：%s" % e,
                     "detail": str(e)})
        try:
            if run_fh:
                run_fh.close()
        except Exception:
            pass
        backend_busy = False
        return
    _file("MIDI: %s" % (info or {}).get("midi"))
    _file("失败轨: %s" % ((info or {}).get("failed") or "无"))
    state = dict(state or {})
    state["song"] = name
    # 预览（谱面画布 + 试听音频）由Adofai-Chart-Generator自己做，这里不再算 leveljson / 复制预览音
    _file("总耗时 %.0fs" % (time.time() - t0))
    try:
        if run_fh:
            run_fh.close()
    except Exception:
        pass
    post_to_web({"type": "generated", "ok": True, "state": state,
                 "midi": (info or {}).get("midi"),
                 "stems": len((info or {}).get("stems") or {}),
                 "failed": (info or {}).get("failed") or [],
                 "elapsed_ms": (rebuild_r or {}).get("elapsed_ms"),
                 "logfile": run_path,
                 "msg": (rebuild_r or {}).get("msg", "")})
    # 生成完：自动切到Adofai-Chart-Generator 完整工作台，并载入刚产出的 MIDI（让用户在全套 UI 里精修/导出）
    _midi = (info or {}).get("midi")
    if _midi and os.path.isfile(_midi):
        PENDING_STUDIO_LOAD["path"] = _midi
        # 只通知导入页显示「正在打开工作台」，真正导航由宿主自己发（不等网页回传，
        # 否则网页一旦没实现该分支，界面就永远停在导入页 —— 2026-09-19 踩过的坑）
        post_to_web({"type": "nav_studio"})
        _dbg("生成完成，1.2s 后切工作台并载入: %s" % _midi)

        def _go_studio():
            time.sleep(1.2)          # 留一点时间让导入页把「生成完成」显示出来
            if HWND_MAIN:
                user32.PostMessageW(HWND_MAIN, WM_APP_NAV, 0, 0)

        threading.Thread(target=_go_studio, daemon=True).start()
    backend_busy = False


def do_tune(state):
    global backend_busy
    backend_busy = True
    post_to_web({"type": "tune_progress", "text": "重新生成预览中…", "busy": True})
    try:
        backend.start(timeout=60)
        d = backend.derive(state)
        if d.get("ok"):
            state = d.get("state", state)
        r = backend.rebuild(state)
    except Exception as e:
        post_to_web({"type": "rebuilt", "ok": False, "msg": "微调失败：%s" % e})
        backend_busy = False
        return
    if not r.get("ok"):
        post_to_web({"type": "rebuilt", "ok": False, "msg": r.get("msg") or "微调失败"})
        backend_busy = False
        return
    lvl = None
    au = None
    try:
        lvl = backend.leveljson(state)
    except Exception as e:
        _dbg("leveljson err: %r" % (e,))
    try:
        au = preview_url_for(state)
    except Exception as e:
        _dbg("preview_url err: %r" % (e,))
    post_to_web({"type": "rebuilt", "ok": True, "state": state,
                 "elapsed_ms": r.get("elapsed_ms"), "msg": r.get("msg", "")})
    post_to_web({"type": "preview", "level": lvl, "audio_url": au})
    backend_busy = False


def do_save(audio, state):
    try:
        chart, song_dir = backend.save(state, audio)
    except Exception as e:
        post_to_web({"type": "saved", "ok": False, "msg": "保存失败：%s" % e})
        return
    if not chart:
        post_to_web({"type": "saved", "ok": False, "msg": "保存失败：未返回谱面路径"})
        return
    post_to_web({"type": "saved", "ok": True, "chart": chart, "dir": song_dir})


# ---------- 历史记录（输出目录下的 .adofai）----------
def find_source_midi(adofai_path):
    """在 output/.work 里，按歌曲名匹配最近一次生成的 *stems_combined.mid。"""
    import glob as _g
    name = os.path.basename(adofai_path).lower().replace(".adofai", "")
    try:
        for m in sorted(_g.glob(os.path.join(OUTPUT_DIR, ".work", "*", "*.mid")),
                        key=os.path.getmtime, reverse=True):
            base = os.path.basename(m).lower()
            if name in base or base.replace("_stems_combined.mid", "") in name:
                return m
    except Exception:
        pass
    return None


def send_history():
    import glob as _g
    items = []
    try:
        for fp in _g.glob(os.path.join(OUTPUT_DIR, "**", "*.adofai"), recursive=True):
            sz = os.path.getsize(fp)
            if sz >= 1024 * 1024:
                size = "%.1f MB" % (sz / 1024.0 / 1024.0)
            else:
                size = "%.1f KB" % (sz / 1024.0)
            items.append({"name": os.path.basename(fp), "size": size,
                          "path": fp, "source_dir": os.path.dirname(fp),
                          "mtime": int(os.path.getmtime(fp) * 1000),
                          "source_midi": find_source_midi(fp) or ""})
        items.sort(key=lambda x: x["mtime"], reverse=True)
    except Exception as e:
        _dbg("send_history err: %r" % (e,))
    post_to_web({"type": "history", "items": items})


def do_appinfo():
    info = {
        "app": "ADOFAI Studio",
        "studio_dir": STUDIO,
        "output_dir": OUTPUT_DIR,
        "audio_sep_dir": AUDIOSEP,
        "chartgen_dir": CHARTGEN,
        "gateway": "http://127.0.0.1:%d" % backend.GATEWAY_PORT,
        "sidecar": "127.0.0.1:%d" % backend.PORT,
        "cwd": os.getcwd(),
        "py_ver": sys.version.split()[0] if hasattr(sys, "version") else "",
        # 设置页要的：当前材质 + 可选项（schedule 第 2 条）
        "material": current_material(),
        "materials": material_list(),
        # 关于页要的：explain.md 的落点（schedule 第 3 条）
        "explain_candidates": EXPLAIN_CANDIDATES,
    }
    post_to_web({"type": "appinfo", "data": info})


def _human_size(n):
    if n >= 1024 * 1024:
        return "%.1f MB" % (n / 1024.0 / 1024.0)
    return "%.1f KB" % (n / 1024.0)


MIDI_AUDIO_EXT = (".wav", ".mp3", ".flac", ".m4a", ".ogg")


def do_list_midis():
    """列出 output/.work 下**所有生成过的合成 MIDI**，连同它那一次用的整曲音频。

    工作台入口拿它做「历史 MIDI 选择列表」：选中哪条，点「前往工作台」就把
    这条 MIDI（以及同目录 job/input.wav）一起带进去，用户不必再手挑文件。
    """
    import glob as _g
    items = []
    work = os.path.join(OUTPUT_DIR, ".work")
    try:
        # 先一次性把 output 下的谱面名收齐，再逐条匹配 —— 别每条 MIDI 都递归扫一遍
        # output（历史产物可能上千个文件，逐条扫会把 UI 线程卡住）。
        try:
            charts = [os.path.basename(x)
                      for x in _g.glob(os.path.join(OUTPUT_DIR, "**", "*.adofai"),
                                       recursive=True)]
        except Exception:
            charts = []
        seen = set()
        cands = _g.glob(os.path.join(work, "*", "*_stems_combined.mid"))
        if not cands:
            cands = _g.glob(os.path.join(work, "*", "*.mid"))
        for fp in cands:
            if fp in seen:
                continue
            seen.add(fp)
            job = os.path.dirname(fp)
            song = os.path.basename(job)
            # 这一次生成用的整曲：流水线第①步落的 job/input.wav。
            # 🔴 注意**比想的深一层**：MIDI 在 .work/<歌曲>_<时间戳>/，而 job/ 是它的子目录
            #    ⇒ 真实路径是 <job>/job/input.wav（工作台前端 bindProjectAudio 探的就是这层）。
            audio = ""
            for cand in (os.path.join(job, "job", "input.wav"),
                         os.path.join(job, "input.wav")):
                if os.path.isfile(cand):
                    audio = cand
                    break
            if not audio:
                for sub in (os.path.join(job, "job"), job):
                    for ext in MIDI_AUDIO_EXT:
                        hit = _g.glob(os.path.join(sub, "*" + ext))
                        if hit:
                            audio = hit[0]
                            break
                    if audio:
                        break
            try:
                sz = os.path.getsize(fp)
                mt = int(os.path.getmtime(fp) * 1000)
            except Exception:
                sz, mt = 0, 0
            items.append({
                "name": os.path.basename(fp),
                "song": song,
                "path": fp,
                "job": job,
                "size": _human_size(sz),
                "bytes": sz,
                "mtime": mt,
                "audio": audio,
                # 谱面是否已经出过（output 里有没有同名的 .adofai）—— 列表里给个提示用
                "has_chart": any(song.lower() in c.lower() for c in charts),
            })
        items.sort(key=lambda x: x["mtime"], reverse=True)
    except Exception as e:
        _dbg("do_list_midis err: %r" % (e,))
    post_to_web({"type": "midis", "items": items,
                 "work_dir": work, "count": len(items)})


def do_get_explain():
    """读「关于」页的 explain.md（只认程序目录，见 EXPLAIN_CANDIDATES）。"""
    MAXC = 512 * 1024
    for p in EXPLAIN_CANDIDATES:
        try:
            if os.path.isfile(p):
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    txt = f.read(MAXC)
                post_to_web({"type": "explain", "ok": True, "path": p,
                             "text": txt,
                             "size": _human_size(os.path.getsize(p)),
                             "candidates": EXPLAIN_CANDIDATES})
                _dbg("explain: 读取 %s（%d 字节）" % (p, len(txt)))
                return
        except Exception as e:
            _dbg("do_get_explain err(%s): %r" % (p, e))
    post_to_web({"type": "explain", "ok": False, "path": "",
                 "candidates": EXPLAIN_CANDIDATES})
    _dbg("explain: 未找到 explain.md（候选 %r）" % (EXPLAIN_CANDIDATES,))


EXPLAIN_TEMPLATE = """# ADOFAI Studio 说明

在这里写你的说明文档，保存成 **UTF-8** 的 `.md`，回到「关于」页就会自动渲染。

## 这个文件能写什么

- 标题（`#`、`##`、`###`）
- **粗体**、*斜体*、`行内代码`
- 列表、> 引用、``` 代码块 ```
- 表格、链接、图片、分隔线（`---`）

## 生成链路

| 步骤 | 做什么 | 工具 |
|---|---|---|
| ① | 转 WAV（44.1k 立体声） | ffmpeg |
| ② | 分离 6 轨 | BS-RoFormer-SW |
| ③ | 逐轨转谱 | MuScriptor |
| ④ | 合并成单文件多轨 MIDI | 自家脚本 |
| ⑤ | 出谱（.adofai） | Adofai-Chart-Generator算法 |

---

写完直接刷新「关于」页即可，不需要重启。
"""


def do_mk_explain(force=False):
    """在首选路径生成一份 explain.md 模板（已存在则不覆盖）。"""
    p = EXPLAIN_CANDIDATES[0]
    try:
        if os.path.isfile(p) and not force:
            post_to_web({"type": "explain_written", "ok": False, "path": p,
                         "msg": "文件已存在，没有覆盖"})
            return
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(EXPLAIN_TEMPLATE)
        _dbg("mkexplain: 写入 %s" % p)
        post_to_web({"type": "explain_written", "ok": True, "path": p})
    except Exception as e:
        _dbg("do_mk_explain err: %r" % (e,))
        post_to_web({"type": "explain_written", "ok": False, "path": p,
                     "msg": str(e)})


def do_train_info():
    """训练采点模型页的环境体检。

    ⚠ 只体检、不启动。工人（训练进程）必须由主人在场手动开始 —— 这是铁律。
    """
    script = ""
    for p in TRAIN_CANDIDATES:
        if os.path.isfile(p):
            script = p
            break
    data_dir = ""
    audio_n = 0
    for p in TRAIN_DATA_CANDIDATES:
        if os.path.isdir(p):
            data_dir = p
            try:
                for root, _d, files in os.walk(p):
                    audio_n += sum(1 for f in files
                                   if f.lower().endswith((".wav", ".mp3", ".ogg", ".flac")))
            except Exception:
                pass
            break
    weights = []
    for pat in (os.path.join(AUDIOSEP, "models", "**", "*.pt"),
                os.path.join(STUDIO, "models", "**", "*.pt"),
                os.path.join(AUDIOSEP, "**", "onset*.pt")):
        try:
            import glob as _g
            for w in _g.glob(pat, recursive=True):
                if w not in weights:
                    weights.append(w)
        except Exception:
            pass
    gpu = {"ok": False, "name": "", "vram": ""}
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, timeout=6, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        line = (r.stdout or "").strip().splitlines()
        if r.returncode == 0 and line:
            parts = [x.strip() for x in line[0].split(",")]
            gpu = {"ok": True, "name": parts[0] if parts else "",
                   "vram": parts[1] if len(parts) > 1 else ""}
    except Exception as e:
        _dbg("do_train_info: nvidia-smi 查询失败 %r" % (e,))
    post_to_web({"type": "traininfo", "data": {
        "script": script,
        "script_ok": bool(script),
        "script_candidates": TRAIN_CANDIDATES,
        "data_dir": data_dir,
        "audio_count": audio_n,
        "data_candidates": TRAIN_DATA_CANDIDATES,
        "weights": weights,
        "gpu": gpu,
        "py": os.path.join(AUDIOSEP, "runtime", "python.exe"),
    }})


def do_list_stems():
    import glob as _g
    items = []
    job = ""
    try:
        mid = backend.latest_combined_midi()
        job = os.path.dirname(mid) if mid else ""
        if job and os.path.isdir(job):
            for s in sorted(_g.glob(os.path.join(job, "stems", "*.wav"))):
                items.append({"name": os.path.basename(s), "path": s,
                              "mtime": int(os.path.getmtime(s) * 1000)})
    except Exception as e:
        _dbg("list_stems err: %r" % (e,))
    post_to_web({"type": "stems", "job": job, "items": items})


def do_clean_work():
    removed_files = 0
    bytes_freed = 0
    try:
        for d in (os.path.join(OUTPUT_DIR, ".work"), os.path.join(OUTPUT_DIR, ".tmp")):
            if os.path.isdir(d):
                for root, _dirs, files in os.walk(d):
                    for f in files:
                        fp = os.path.join(root, f)
                        try:
                            bytes_freed += os.path.getsize(fp)
                            removed_files += 1
                        except Exception:
                            pass
                shutil.rmtree(d, ignore_errors=True)
    except Exception as e:
        _dbg("clean_work err: %r" % (e,))
    post_to_web({"type": "cleaned", "files": removed_files,
                 "bytes": bytes_freed,
                 "text": "已清理 %d 个临时文件，释放 %s"
                 % (removed_files, ("%.1f MB" % (bytes_freed / 1024.0 / 1024.0))
                    if bytes_freed >= 1024 * 1024 else ("%d KB" % (bytes_freed / 1024)))})


def do_open_in_workbench(midi):
    """设置待载入谱面并导航到工作台（用户可视地在工作台里微调/导出）。"""
    if midi and os.path.isfile(midi):
        PENDING_STUDIO_LOAD["path"] = midi
    else:
        try:
            _auto = backend.latest_combined_midi()
            if _auto and os.path.isfile(_auto):
                PENDING_STUDIO_LOAD["path"] = _auto
        except Exception:
            pass
    if HWND_MAIN:
        user32.PostMessageW(HWND_MAIN, WM_APP_NAV, 0, 0)   # 0 = 进工作台


def set_opaque(on):
    global WEBVIEW_CTRL
    if WEBVIEW_CTRL is None:
        return
    try:
        ctrl2 = WEBVIEW_CTRL.QueryInterface(WV.ICoreWebView2Controller2)
        if on:
            ctrl2.DefaultBackgroundColor = WV.COREWEBVIEW2_COLOR(A=255, R=20, G=20, B=24)
        else:
            ctrl2.DefaultBackgroundColor = WV.COREWEBVIEW2_COLOR(A=0, R=0, G=0, B=0)
        _dbg("set_opaque(%s) OK" % on)
    except Exception as e:
        _dbg("set_opaque err: %r" % (e,))


# ---------- 开发自检（环境变量触发；正常双击 启动.bat 完全不生效）----------
class ScriptDone(comtypes.COMObject):
    """ExecuteScript 完成回调：把网页读回的 JSON 记进 gui_debug.log。

    ★ 探针有**中间态**：sidecar 冷启动时页面 main() 还在 await health/schema，
      此时点按钮只会点到空气，`__domProbe()` 会回 'PENDING'（或干脆 NO_PROBE）。
      以前只探一次，会把「还没建好」误判成一堆 FAIL（2026-09-19 踩过）。
      现在：**只有拿到真探针结果（含 "layout"）才收工关窗**，否则 1 秒后重探，
      每 5 次记一行进度（最多 150 次 ≈ 150s）。
    """
    _com_interfaces_ = [WV.ICoreWebView2ExecuteScriptCompletedHandler]
    def Invoke(self, error_code, result_json):
        try:
            rj = result_json if isinstance(result_json, str) else (result_json or "")
            # 🔴 是否处于自检模式：`ADOFAI_GUI_SELFTEST` 或 `_AUDIO` 任一即可
            #   （run_selftest.py 设前者；直接双击/命令行走真链路自检只设后者）。
            _st = bool(os.environ.get("ADOFAI_GUI_SELFTEST")
                       or os.environ.get("ADOFAI_GUI_SELFTEST_AUDIO"))
            if _st:
                _dbg("EXEC_JS_DONE %s" % (rj[:110],))
            # ★ 重探**只在自检模式**下做。正常启动（双击 启动-工作台.bat）时页面里没有
            #   `__domProbe`，而别的 ExecuteScript（如自动载入 MIDI 的 __dsh.load）也会
            #   走这个回调、结果同样不含 "layout" ⇒ 不门控就会空转 150 次把日志刷满
            #   （2026-09-19 主人在正常窗口上看到 `SELFTEST_DOM(未就绪，重探 N)` 才发现）。
            if _st and ('"layout"' not in rj) and _PROBE_TRIES[0] < 150:
                _PROBE_TRIES[0] += 1
                if _PROBE_TRIES[0] % 5 == 1:
                    _dbg("SELFTEST_DOM(未就绪，重探 %d) %s" % (_PROBE_TRIES[0], rj[:120]))
                # 🔴 必须**限速**重探：直接 PostMessage 自己会把 150 次在一秒内打完，
                #    把还没排到的注入脚本（WM_APP_STUDIOJS）挤到后面 ⇒ 反而永远探不到。
                def _again():
                    time.sleep(1.0)
                    if HWND_MAIN:
                        user32.PostMessageW(HWND_MAIN, WM_APP_SELFTEST, 2, 0)
                threading.Thread(target=_again, daemon=True).start()
                return
            _dbg("SELFTEST_DOM %s" % (result_json,))
            if os.environ.get("ADOFAI_GUI_SELFTEST_CLOSE"):
                _delayed_close(3.0)
        except Exception:
            pass


def _exec_js(script):
    """在网页里跑一段 JS（只能在 UI 线程调用，理由同 post_to_web）。"""
    if WEBVIEW is None:
        return
    try:
        if os.environ.get("ADOFAI_GUI_SELFTEST"):
            _dbg("EXEC_JS %s" % (script[:70].replace("\n", " "),))
        h = ScriptDone()
        ALIVE.append(h)
        iface = h.QueryInterface(WV.ICoreWebView2ExecuteScriptCompletedHandler)
        WEBVIEW.ExecuteScript(script, iface)
    except Exception as e:
        _dbg("ExecuteScript 异常: %r" % (e,))


def _delayed_close(sec=3.0):
    def _go():
        time.sleep(sec)
        if HWND_MAIN:
            user32.PostMessageW(HWND_MAIN, WM_CLOSE, 0, 0)
    threading.Thread(target=_go, daemon=True).start()


def _selftest_action(action):
    """UI 线程执行自检动作：1=打开遮罩  2=读回 DOM 状态（可选关窗）  3=关窗。"""
    if action == 1:
        _exec_js("window.__devShowOverlay && window.__devShowOverlay()")
    elif action == 2:
        # ★ 关窗交给 ScriptDone：探针回 PENDING 时还要接着重探，不能这就关（见 ScriptDone 注释）
        _exec_js("(window.__domProbe ? window.__domProbe() : 'NO_PROBE')")
    elif action == 3:
        if HWND_MAIN:
            user32.PostMessageW(HWND_MAIN, WM_CLOSE, 0, 0)


def _start_selftest():
    """网页就绪后按环境变量跑一次自检。

    ADOFAI_GUI_SELFTEST=1                —— 只测消息通道（推假消息 + 读回 DOM）
    ADOFAI_GUI_SELFTEST_AUDIO=<音频路径> —— 走真实 4 步链路 + chartgen 出谱
    ADOFAI_GUI_SELFTEST_CLOSE=1          —— 自检完自动关窗
    """
    audio = os.environ.get("ADOFAI_GUI_SELFTEST_AUDIO")
    if not os.environ.get("ADOFAI_GUI_SELFTEST") and not audio:
        return

    def work():
        time.sleep(0.4)
        if HWND_MAIN:
            user32.PostMessageW(HWND_MAIN, WM_APP_SELFTEST, 1, 0)   # 先打开遮罩
        time.sleep(0.4)
        if audio:
            try:
                do_generate(audio, os.environ.get("ADOFAI_GUI_SELFTEST_SIZE", "small"))
            except Exception as e:
                _dbg("SELFTEST do_generate 异常: %r" % (e,))
        else:
            # 🔴 故意从【工作线程】投递 —— 正是以前消息全丢的场景
            post_to_web({"type": "gen_stage", "n": 0, "name": "自检", "detail": "工作线程投递"})
            for n in range(1, 6):
                post_to_web({"type": "gen_stage", "n": n,
                             "name": "自检阶段 %d" % n, "detail": "detail-%d" % n})
                for i in range(3):
                    post_to_web({"type": "gen_log",
                                 "line": "[smoke] 工作线程消息 n=%d i=%d" % (n, i)})
                time.sleep(0.15)
            post_to_web({"type": "generated", "ok": True, "state": {}, "stems": 5,
                         "failed": [], "msg": "自检完成"})
        # 可选：在页面里跑一段自定义 JS（用来点按钮 / 读 DOM 做真机回归，如 ADOFAI_GUI_SELFTEST_JS="els.click()"）
        _js = os.environ.get("ADOFAI_GUI_SELFTEST_JS")
        if _js:
            _exec_studio_js(_js)
        time.sleep(2.5)
        if HWND_MAIN:
            user32.PostMessageW(HWND_MAIN, WM_APP_SELFTEST, 2, 0)   # 读回 DOM 状态

    threading.Thread(target=work, daemon=True).start()


# ---------- 窗口过程 ----------
def wndproc(hwnd, msg, wparam, lparam):
    global HWND_MAIN, last_zoomed
    if msg == WM_ERASEBKGND:
        return 1  # 背景由 DWM Mica 提供，禁止白底擦除
    elif msg == WM_CONTEXTMENU:
        return 0  # 禁止窗口内右键（不弹任何菜单）
    elif msg == WM_SIZE:
        if WEBVIEW_CTRL is not None:
            _fit_webview(hwnd)      # 铺满新客户区（跟住窗口尺寸）
        try:
            zoomed = bool(user32.IsZoomed(hwnd))
            if zoomed != last_zoomed:
                last_zoomed = zoomed
                post_to_web({"type": "winstate", "zoomed": zoomed})
        except Exception:
            pass
        return 0
    elif msg == WM_DESTROY:
        try:
            backend.stop()
        except Exception:
            pass
        try:
            if WEBVIEW_CTRL is not None:
                WEBVIEW_CTRL.Close()
        except Exception:
            pass
        user32.PostQuitMessage(0)
        return 0
    elif msg == WM_CLOSE:
        user32.DestroyWindow(hwnd)
        return 0
    elif msg == WM_APP_WEBMSG:
        _drain_web_queue()
        return 0
    elif msg == WM_APP_SELFTEST:
        _selftest_action(int(wparam or 0))
        return 0
    elif msg == WM_APP_LOG:
        with log_lock:
            text = log_q.popleft() if log_q else ""
        if text:
            post_to_web({"type": "log", "text": text})
        return 0
    elif msg == WM_APP_DONE:
        post_to_web({"type": "done", "out": last_out, "code": last_code, "error": last_error})
        post_to_web({"type": "state", "busy": False})
        return 0
    elif msg == WM_APP_DRAG:
        user32.ReleaseCapture()
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        lp = (pt.y << 16) | pt.x
        user32.SendMessageW(hwnd, WM_NCLBUTTONDOWN, HTCAPTION, lp)
        return 0
    elif msg == WM_APP_PICK:
        path = native_open_file()
        if path:
            post_to_web({"type": "filepath", "path": path,
                         "dest": pick_ctx.get("dest", "")})
        return 0
    elif msg == WM_APP_DSHDLG:
        _handle_dsh_call(int(wparam or 0))
        return 0
    elif msg == WM_APP_NAV:
        if wparam == 1:
            _navigate_import()
        else:
            _navigate_studio()
        return 0
    elif msg == WM_APP_STUDIOJS:
        _drain_studio_js()
        return 0
    elif msg == WM_APP_RESIZE:
        user32.ReleaseCapture()
        pt = wt.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        lp = (pt.y << 16) | pt.x
        user32.SendMessageW(hwnd, WM_NCLBUTTONDOWN, wparam, lp)
        return 0
    elif msg == WM_NCCALCSIZE:
        if wparam:
            return 0   # 客户区填满窗口（无边框）
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    elif msg == WM_NCHITTEST:
        res = user32.DefWindowProcW(hwnd, msg, wparam, lparam)
        if res != HTCLIENT:
            return res
        if user32.IsZoomed(hwnd):
            return HTCLIENT
        x = lparam & 0xFFFF
        y = (lparam >> 16) & 0xFFFF
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        cx = x - r.left; cy = y - r.top
        W = r.right - r.left; H = r.bottom - r.top
        b = RESIZE_BORDER
        if cx <= b and cy <= b: return HTTOPLEFT
        if cx >= W - b and cy <= b: return HTTOPRIGHT
        if cx <= b and cy >= H - b: return HTBOTTOMLEFT
        if cx >= W - b and cy >= H - b: return HTBOTTOMRIGHT
        if cx <= b: return HTLEFT
        if cx >= W - b: return HTRIGHT
        if cy <= b: return HTTOP
        if cy >= H - b: return HTBOTTOM
        return HTCLIENT
    elif msg == WM_SETCURSOR:
        if user32.LOWORD(lparam) == HTCLIENT:
            pt = wt.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            ht = user32.SendMessageW(hwnd, WM_NCHITTEST, 0,
                                     (pt.y << 16) | pt.x) & 0xFFFF
            cursors = {
                HTLEFT: IDC_SIZEWE, HTRIGHT: IDC_SIZEWE,
                HTTOP: IDC_SIZENS, HTBOTTOM: IDC_SIZENS,
                HTTOPLEFT: IDC_SIZENWSE, HTBOTTOMRIGHT: IDC_SIZENWSE,
                HTTOPRIGHT: IDC_SIZENESW, HTBOTTOMLEFT: IDC_SIZENESW,
            }
            cid = cursors.get(ht, 0)
            if cid:
                user32.SetCursor(user32.LoadCursorW(None, ctypes.cast(cid, wt.LPCWSTR)))
                return 1
    elif msg == WM_GETMINMAXINFO:
        mmi = ctypes.cast(ctypes.c_void_p(lparam), ctypes.POINTER(MINMAXINFO)).contents
        mmi.ptMinTrackSize = wt.POINT(WIN_MIN_W, WIN_MIN_H)
        return 0
    elif msg == WM_NCLBUTTONDOWN:
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


# ---------- 入口 ----------
def main():
    global HWND_MAIN
    # 「直接进工作台（第四步）」模式：
    #   - 显式给了 ADOFAI_GUI_STUDIO_MIDI → 载入那个
    #   - 只设了 ADOFAI_GUI_OPEN_STUDIO（没给具体文件）→ 自动找 output/.work 下最新一次生成的合成 MIDI 载入
    #   - 都找不到 → 进空白工作台，用户自己点「打开 MIDI/BDG」
    _dev_midi = os.environ.get("ADOFAI_GUI_STUDIO_MIDI")
    if _dev_midi:
        PENDING_STUDIO_LOAD["path"] = _dev_midi
        os.environ["ADOFAI_GUI_OPEN_STUDIO"] = "1"
        _dbg("DEV: 直接进工作台并待载入 %s" % _dev_midi)
    elif os.environ.get("ADOFAI_GUI_OPEN_STUDIO"):
        _auto = backend.latest_combined_midi()
        if _auto:
            PENDING_STUDIO_LOAD["path"] = _auto
            _dbg("OPEN_STUDIO: 自动载入最新合成 MIDI %s" % _auto)
        else:
            _dbg("OPEN_STUDIO: 未找到历史 MIDI，进空白工作台")
    try:
        ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        ole32.CoInitializeEx.restype = ctypes.HRESULT
        _r = ole32.CoInitializeEx(None, 2)
        _dbg("main: CoInitializeEx(STA) 再次调用 hr=%s" % hex(_r & 0xFFFFFFFF))
    except Exception as e:
        _dbg("main: CoInitializeEx 异常: %r" % (e,))
    hinst = kernel32.GetModuleHandleW(None)
    wproc = WNDPROC(wndproc)

    icon_ico = os.path.join(ROOT, "icon.ico")
    hIconLarge = None
    hIconSmall = None
    if os.path.isfile(icon_ico):
        hIconLarge = user32.LoadImageW(None, wt.LPCWSTR(icon_ico), IMAGE_ICON, 32, 32,
                                        LR_LOADFROMFILE)
        hIconSmall = user32.LoadImageW(None, wt.LPCWSTR(icon_ico), IMAGE_ICON, 16, 16,
                                        LR_LOADFROMFILE)
        _dbg("LoadImageW icon (ico) -> large=%s small=%s" % (hex(hIconLarge or 0), hex(hIconSmall or 0)))

    wc = WNDCLASSEX()
    wc.cbSize = ctypes.sizeof(WNDCLASSEX)
    wc.style = CS_HREDRAW | CS_VREDRAW
    wc.lpfnWndProc = wproc
    wc.hInstance = hinst
    wc.hCursor = user32.LoadCursorW(None, ctypes.cast(32512, wt.LPCWSTR))  # IDC_ARROW
    wc.hIcon = wt.HICON(hIconLarge or 0)
    wc.hIconSm = wt.HICON(hIconSmall or 0)
    wc.hbrBackground = None  # NULL：不画背景，露出 Mica
    wc.lpszClassName = wt.LPCWSTR(WND_CLASS)
    atom = user32.RegisterClassExW(ctypes.byref(wc))
    if not atom:
        ctypes.windll.user32.MessageBoxW(None, "RegisterClassExW 失败", "ADOFAI GUI", 0x10)
        return

    wa = wt.RECT()
    if user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(wa), 0):
        win_x = wa.left + max(0, (wa.right - wa.left - WIN_W) // 2)
        win_y = wa.top + max(0, (wa.bottom - wa.top - WIN_H) // 2)
    else:
        win_x = max(0, (user32.GetSystemMetrics(78) - WIN_W) // 2)
        win_y = max(0, (user32.GetSystemMetrics(79) - WIN_H) // 2)

    hwnd = user32.CreateWindowExW(0, wt.LPCWSTR(WND_CLASS),
        wt.LPCWSTR("ADOFAI 谱面生成器"),
        WS_POPUP | WS_VISIBLE,
        win_x, win_y, WIN_W, WIN_H,
        None, None, hinst, None)
    if not hwnd:
        ctypes.windll.user32.MessageBoxW(None,
            "CreateWindowExW 失败（句柄为 0，检查 ctypes argtypes）", "ADOFAI GUI", 0x10)
        return
    HWND_MAIN = hwnd

    if hIconLarge:
        user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hIconLarge)
    if hIconSmall:
        user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hIconSmall)
    # 启动即按上次选择的材质施一遍（DWM 这半边；WebView 那半边要等它建好，见页面就绪处）
    _mat = current_material()
    _kind0 = MATERIALS[_mat][0]
    set_backdrop(hwnd, _kind0)               # 默认 Acrylic；用户可在设置里改
    _dbg("main: 初始材质 %s (DWM kind=%d)" % (_mat, _kind0))
    set_round(hwnd)                          # 圆角
    set_dark(hwnd)                           # 深色标题栏
    user32.ShowWindow(hwnd, 1)
    user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, 0)
    user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, 0)
    user32.UpdateWindow(hwnd)

    # 启动 WebView2
    os.makedirs(WEBVIEW_CACHE, exist_ok=True)
    env_handler = EnvCompleted()
    ALIVE.append(env_handler)
    iface = env_handler.QueryInterface(WV.ICoreWebView2CreateCoreWebView2EnvironmentCompletedHandler)
    hr = _create_env(None, WEBVIEW_CACHE, None, iface)
    rh = hex(hr & 0xFFFFFFFF) if isinstance(hr, int) else hr
    _dbg("CreateCoreWebView2EnvironmentWithOptions hr=%s" % rh)
    if hr < 0:
        ctypes.windll.user32.MessageBoxW(hwnd,
            u"WebView2 初始化失败\nHRESULT=%s\n请确认本机已安装 WebView2 Runtime" % rh,
            u"ADOFAI GUI", 0x10)

    # 看门狗：5 秒后若网页仍未就绪，提示去看日志
    def _watchdog():
        time.sleep(5)
        if WEBVIEW is None:
            _dbg("WATCHDOG: 5 秒后 WebView 仍未就绪")
            ctypes.windll.user32.MessageBoxW(HWND_MAIN,
                u"WebView2 未能在 5 秒内就绪，界面可能黑屏。\n请打开 gui 目录下的 gui_debug.log 查看失败原因，并把内容发给我。",
                u"ADOFAI GUI", 0x10)
    threading.Thread(target=_watchdog, daemon=True).start()

    # 启动单端口同源网关（伺服Adofai-Chart-Generator前端 + 反代 /api，零 CORS）
    try:
        backend.gateway_start()
        _dbg("gateway 已启动: %s" % backend.gateway_base())
    except Exception as e:
        _dbg("gateway 启动失败(非致命): %r" % (e,))

    # 启动Adofai-Chart-Generator算法后端（独立子进程，脱离本窗口生命周期前一直驻留）
    threading.Thread(target=backend_start, daemon=True).start()

    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        try:
            ctypes.windll.user32.MessageBoxW(None, tb, "ADOFAI GUI 崩溃", 0x10)
        except Exception:
            pass
