"""会话：Electron 前端的全部业务逻辑（与旧 PySide6 `MainWindow` 逐条对等）。

对等基准 = `docs/18-ui功能清单.md`。每条重要分支都在注释里标了旧 UI 的行号，
方便逐条核对（`:NNN` 指 `ui/main_window.py` 的行）。

**为什么逻辑放在 Python 这边**：`docs/15` 定的架构是「Electron 前端 + Python
core 当 sidecar」，一切「已验证的逻辑」都不重写。前端只负责画和收参数，
所有派生（主轨判定、采音轨集合、双押插入时机、offset 写回、命中时刻重映射）
都在这里发生 —— 这样新旧 UI 的行为可以用同一份测试比对。

⚠ 本文件含中文，**不要**用 PowerShell 的 `Get-Content`/`Set-Content` 改它
（Windows 默认码页会把 UTF-8 解成 ANSI，已踩过一次：117 个中文字符永久丢字）。
"""
from __future__ import annotations

import bisect
import os
import tempfile

from . import ROOT, IMPORTS_OK  # noqa: F401

from core import audio_onsets as audio_mod
from core import appearance as appearance_mod
from core import bdg as bdg_mod
from core import bigline as bigline_mod
from core import colorize as colorize_mod
from core import denoise as denoise_mod
from core import tilefill as fill_mod
from core import camera as cam_mod
#: ★ 整数就写整数（`settings.position` 这类给游戏读的字段，参考谱里也是整数）
from core.writer import _num as _wnum
from core import dp_angle, dp_midspin
from core import fitdirect as fitdirect_mod
from core import midi as midi_mod
from core import onsets as onsets_mod
from core import rhythm as rhythm_mod
from core import rules as rules_mod
from core import segments as seg_mod
from core import show as show_mod
from core import solve as solve_mod
from core import stem_json as stem_mod
from core import synth, verify, writer
from core import tempo_diag as tempo_diag_mod
from core import ts_source as ts_mod
from core import xkbase as xkbase_mod
from core.bdg import aliases as al
from core.solve import SPEED_TIERS, STRAIGHT_PRESETS, SolveParams
from . import schema as SC

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  #: 仓库根（不写死盘符）

AUDIO_EXTS = (".ogg", ".oga", ".wav", ".flac", ".mp3")
# ★ BDG 工程（`docs/38` §9）：把它做成一种 `MidiFile` ⇒ 下游「选轨→采音→求解」一行不用改
BDG_EXTS = (".bdg",)
# ★ 毫秒时间戳（`docs/45` §7）：同上，也是一种 `MidiFile`
TS_EXTS = ts_mod.TS_EXTS
# ★ 时间戳 JSON（DEMUCS 分轨 · `docs/56`）：**内容嗅探**认它（后缀与纯时间戳同为
#   `.json`），所以它的分支必须**排在通用 TS 分支之前**。
STEM_EXTS = stem_mod.STEM_EXTS

#: 求解方式（`docs/44` / `docs/45`）：
#:   solve  = 最优化（模板/三连音/雪花，会改时序）
#:   direct = 直拟合（一砖一音，时序逐点精确，几何服从）
FIT_SOLVE = "solve"
FIT_DIRECT = "direct"

#: ★★ 「使用激进的拟合策略」钉死的「最小角度」（`docs/57`）：用户 2026-10
#:   「拟合的时候最小角度为 **15°**，也就是只允许 15 30 45 60 75 90……往后」。
FIT_LADDER_MIN_DEG = 15.0
#: 拟合容差的上下限（用户口径「0~100 按照毫秒输入和计算」）
FIT_TOL_MIN_MS = 0.0
FIT_TOL_MAX_MS = 100.0


def _fit_state(st) -> tuple[float, bool]:
    """从 state 里取 `(拟合容差ms, 激进拟合)` —— 越界一律夹回来（不许静默用怪值）。"""
    try:
        tol = float(st.get("fit_tol_ms", 100.0) or 0.0)
    except (TypeError, ValueError):
        tol = 100.0
    tol = max(FIT_TOL_MIN_MS, min(FIT_TOL_MAX_MS, tol))
    return tol, bool(st.get("aggressive_fit", False))

#: ★ 2026-10「开门一遍」：自动挑主轨时，「起点比全曲最早音晚多少以内」还算**覆盖全曲**
#:   （2000ms ≈ 120bpm 下一小节；超出的轨会被排除，见 `track_map`）
PICK_COVER_SLACK_MS = 2000.0


class BadRequest(Exception):
    """参数/状态不合法（HTTP 400）。"""


# ------------------------------------------------------------------ 小工具
def _dp_reasons(rep: dict) -> str:
    """把 `core.dp_angle.plan` 的丢点账翻成人话（**双押不许静默丢**）。

    见 `docs/31` §5.1「b」：任何没插进去的目标音，状态栏都必须说得出来为什么。
    """
    bits = []
    for key, label in (("owned", "撞模板/引擎/雪花"),
                       ("setspeed", "调速与 Twirl 同格，无处安放"),
                       ("dropped", "剩余角度不够"),
                       ("dup", "同一格抢两次"),
                       ("out_of_range", "落在站位/尾层外")):
        n = int(rep.get(key) or 0)
        if n:
            bits.append(f"{label} {n}")
    return " / ".join(bits) or "原因未记录"


def _avg_pitch(t) -> float:
    ns = t.notes
    return sum(n.pitch for n in ns) / len(ns) if ns else 0.0


def _avg_vel(t) -> float:
    ns = t.notes
    return sum(n.velocity for n in ns) / len(ns) if ns else 0.0


def _span(t) -> float:
    ns = t.notes
    if not ns:
        return 0.0
    return max(n.pitch for n in ns) - min(n.pitch for n in ns)


def _fmt_time(v: float) -> str:
    """旧 UI `:1251-1257`：`分:秒.百分秒`。"""
    v = max(0.0, float(v))
    return f"{int(v // 60000)}:{(v % 60000) / 1000:05.2f}"


class Session:
    """单个窗口一个会话（本地单用户，不需要并发会话）。"""

    def __init__(self, root: str = ROOT):
        self.root = root
        self.midi = None
        self.midi_path = ""
        self.bpm_hint = 0.0                  # * 2026-10: 生成页填的 BPM (JSON `bpm_hint`)
        self.source_audio: str | None = None     # OGG/WAV 源（有它就直接用原曲）
        #: ★ 2026-10：最近一次 rebuild 算出的「自动 offset」（None = 没算 / 没开）。
        #:  `resolve_offset()` 用它，保证 **导出 / 预览 / 视图数据用同一个 offset**。
        self.auto_offset_ms: float | None = None
        self.bdg_project = None                  # ★ BDG 来源时的规范化工程（docs/38 §9）
        self.bdg_report = None
        self.bdg_hints: dict = {}                # {轨下标: 建议角色}
        self.onsets_override = None              # ★ 编辑器收回的进度（优先于选轨采音）
        self.onsets: list = []
        self.chart = None
        self.chart_entry: list[float] = []
        self.chart_times: list[float] = []
        self.hit_times: list[float] = []
        self.preview_lead_ms = 0.0
        self.dp_info = ""
        self.dp_report: dict = {}
        self.last_status = ""
        self.last_timing = ""
        self._preview_key = None
        self._preview_path: str | None = None
        self.dp_inserted = 0
        # ★ 上一次重建算出的**双押点下标**（`docs/42`：双押单开一条泳道要用）
        self.dp_hit_idx: list[int] = []
        # ★ 押数账（`docs/48` 三押）：{three, extra, group_lost}
        self.dp_press_n: dict = {}
        # ★ 换手押上色（`docs/59`）：方案 + 人话报告，给 UI 看，不许静默
        self.color_plan = colorize_mod.ColorPlan()
        self.color_report: dict = {}
        # ★ 算法轨道调度（`docs/60`）：皮肤 + 涟漪环 + 半径调度
        self.appearance_plan = appearance_mod.AppearancePlan()
        self.appearance_report: dict = {}
        # ★ 演出（`docs/62`）：入场 / 离场 / 反向 QE + 分段（不许静默）
        self.show_plan = show_mod.ShowPlan()
        self.show_report: dict = {}
        self.region_meta: list[dict] = []
        # ★ 分段采音（`docs/34` 方案 C）：角色是**时间的函数**，有它时区间被取代
        self.segment_meta: list[dict] = []
        self.segment_mode = seg_mod.DEFAULT_MODE
        # ★ 去噪 / 直拟合（`docs/44`）：report 给 UI 看，不许静默
        self.fit_mode = FIT_SOLVE
        self.denoise_meta: dict = {}
        #: ★ 补格（`core/tilefill.py`）这一步的账 —— 关着时是空字典
        self.fill_meta: dict = {}
        self.fit_meta: dict = {}
        # ★ ③b 采bpm（xk base，定稿 `docs/47`）：骨架注入报告 + **钉住的 cbpm**
        #   （`xk_cbpm == 0` ⇒ 没开，老路径**逐字节不变**）
        self.xk_meta: dict = {}
        self.xk_cbpm = 0.0
        #: 本次重建 ③b 采bpm 是否开着（主轨失效的判定要用）
        self.xk_on = False
        # ★ 从 BDG 收回来的**轨道项目**（`docs/45`）：有它时「我们的音轨」
        #   就是 BDG 里那些带时值数据的轨（用户口径）。
        self.lanes_back: list[dict] = []
        self.dp_back: list[dict] = []
        self.back_meta: dict = {}
        # ★ 毫秒时间戳来源（`docs/45` §7）：解析报告 + 网格规划
        self.ts_meta: dict | None = None
        self.ts_plan: dict | None = None
        self.warnings: list[str] = []
        # ★★ 载入时的取舍账（`docs/56`）：**每次 rebuild 都要带上** ——
        #   「原曲找不到」「某路是空的」「未排序/合并了多少」这些话在整份文件
        #   还开着的时候就一直成立；只显示到第一次重算，用户就再也看不到了。
        self.load_warnings: list[str] = []
        self.load_info: dict = {}

    # ================================================================ 文件
    def samples(self) -> list[str]:
        """旧 UI `_sample_menu` `:548-558`：扫 `samples/` 下的 midi/音频。

        ★ 2026-10：**时间戳 JSON** 也进这个菜单（靠**内容嗅探**，避免把 `samples/`
        里任意一个 `.json` 都摆上来）—— 用户一眼就能看到我们支持这个格式。

        ★★ 2026-10：**`samples/audio/` 也扫**。以前只扫顶层，而顶层原来摆的是
        `FallenEra.mid` / `Automaton_Waltz.mid` / `MemoryLocked.mid` ——
        **那三首是第三方音乐**，已按 `samples/README.md` 的规矩挪进 `samples/_external/`
        （**不随包分发**）。于是顶层只剩自制素材，菜单会变空 ⇒ 把自制的那几份
        （`audio/doublepress_demo_*.{mid,ogg}`）一起列进来。
        `_external/` **不扫**（`samples/` 下只在已知的子目录里找，不做递归）。
        """
        base = os.path.join(self.root, "samples")
        if not os.path.isdir(base):
            return []
        exts = (".mid", ".midi") + AUDIO_EXTS
        out = []
        dirs = [base] + [d for d in (os.path.join(base, "audio"),)
                         if os.path.isdir(d)]
        for d0 in dirs:
            for f in sorted(os.listdir(d0)):
                p = os.path.join(d0, f)
                if not os.path.isfile(p):
                    continue
                low = f.lower()
                if low.endswith(exts):
                    out.append(p)
                elif low.endswith(STEM_EXTS) and stem_mod.looks_like_stem_json_file(p):
                    out.append(p)
        return out

    def load(self, path: str, progress=None, should_cancel=None) -> dict:
        """旧 UI `load_midi()` `:568-658`。"""
        if not path or not os.path.exists(path):
            raise BadRequest(f"文件不存在：{path}")
        ext = os.path.splitext(path)[1].lower()
        self.warnings = []
        # ★★ 换文件 ⇒ 上一份的**来源**整个作废（以前只有 TS 分支清，别的来源
        #   留下的 `ts_meta` / `stem_meta` 会串到新文件的信息里）。
        self.ts_meta = None
        self.ts_plan = None
        self.stem_meta = None
        self.stem_hints = {}
        self.stem_notes = {}
        self.stem_report = None
        self.stem_defaults = ([], [], [])
        self.load_warnings = []
        self.bpm_hint = 0.0                  # * 换文件 => 清空上一份的 hint

        if ext in AUDIO_EXTS:
            def _prog(frac, msg):
                if should_cancel is not None and should_cancel():
                    # 与旧 UI 一致：抛 ConversionCancelled
                    # （BaseException 子类，不会被 except Exception 吞掉）
                    raise audio_mod.ConversionCancelled()
                if progress:
                    progress(float(frac), str(msg))

            mf = audio_mod.load_as_midi(path, progress=_prog)
            self.source_audio = path
        elif ext in BDG_EXTS:
            # ★ BDG 工程当来源（`docs/38` §9）：每条 BDG 轨 = 一条音轨，
            #   角色**不由我们猜** —— 只给建议，用户在三条列表里勾。
            p, rep = bdg_mod.load_file(path)
            if not rep.ok:
                raise BadRequest("BDG 解析失败：" + ("；".join(rep.errs[:2]) or "未知"))
            mf = bdg_mod.source.to_midi_like(p)
            self.bdg_project = p
            self.bdg_report = rep
            self.bdg_hints = bdg_mod.source.role_hints(p)
            # 工程里写了音频名 ⇒ 同目录有就拿来试听
            self.source_audio = None
            if p.audio_name:
                cand = os.path.join(os.path.dirname(path), p.audio_name)
                if os.path.exists(cand):
                    self.source_audio = cand
        elif ext in STEM_EXTS and stem_mod.looks_like_stem_json_file(path):
            # ★★ 时间戳 JSON（DEMUCS 分轨 · `docs/56`）：**一路 = 一条音轨**。
            #   `onsets_sec`（秒→毫秒）是唯一真源；`onsets_frame` 只做交叉校验。
            #   角色**不由我们猜**，只给建议（照 BDG 那套）—— 勾选权在用户。
            stems, srep = stem_mod.read_stem_json(path)
            self.stem_report = srep
            smeta = dict(srep.get("meta") or {})
            self.bpm_hint = float(smeta.get("bpm_hint") or 0.0)
            if not any(s.ms for s in stems):
                raise BadRequest(
                    "这份时间戳 JSON 里**每一路都是空的** —— 一个音头都没有，成不了谱")
            try:
                mf = stem_mod.to_midi_like(stems, name=os.path.splitext(
                    os.path.basename(path))[0],
                    duration_ms=smeta.get("duration_ms", 0.0), meta=smeta)
            except ValueError as exc:
                raise BadRequest(str(exc))
            self.stem_meta = getattr(mf, "stem_meta", smeta)
            self.stem_hints = getattr(mf, "stem_hints", {}) or {}
            self.stem_notes = getattr(mf, "stem_notes", {}) or {}
            self.stem_defaults = stem_mod.default_checked(stems)
            self.ts_meta = getattr(mf, "ts_meta", None)
            self.ts_plan = getattr(mf, "ts_plan", None)
            # ★ JSON 里的原曲（`source_audio`）**自动绑预览音源**；找不到不报错，
            #   只是不绑 —— 但试过哪些地方**必须上屏**（不许静默）。
            self.source_audio = None
            found, tried = stem_mod.resolve_audio(smeta.get("source_audio", ""), path)
            if found:
                self.source_audio = found
            elif tried:
                self.warnings.append(
                    "★ JSON 里写的原曲找不到：{}（试过：{}）—— **不绑**预览音源"
                    .format(smeta.get("source_audio"), " / ".join(tried)))
            self.bdg_project = None
            self.bdg_report = None
            self.bdg_hints = {}
            self.warnings.extend(srep.get("warnings") or [])
            self.warnings.append(stem_mod.report_text(srep, self.ts_plan))
        elif ext in TS_EXTS:
            # ★ 毫秒时间戳当来源（`docs/45` §7）：一行一个数 ⇒ 一条轨、每点一个 Note。
            #   与 BDG 同一个套路（做成 `MidiFile`）⇒ 下游一行不用改。
            ts, trep = ts_mod.read_ts(path)
            if len(ts) < 2:
                why = trep.get("json_reject")
                if why:
                    # ★★ 认得出是 JSON 但读不出时间戳（结构化文件）⇒ 明确拒绝，
                    #    **绝不**退化成「把每行里的数字当时刻」出一张垃圾谱。
                    raise BadRequest("这份文件读不出时间戳：{}".format(why))
                raise BadRequest(
                    "这份文件里没读够时间戳（{} 行 / 留 {} 个）——"
                    "支持「一行一个毫秒数」/ CSV（取每行最后一个数）/ "
                    "mm:ss.xxx / JSON 数组".format(trep["n_lines"], len(ts)))
            mf = ts_mod.to_midi_like(ts, name=os.path.splitext(
                os.path.basename(path))[0])
            self.ts_meta = trep
            self.ts_plan = getattr(mf, "ts_plan", None)
            self.source_audio = None
            self.bdg_project = None
            self.bdg_report = None
            self.bdg_hints = {}
        else:
            mf = midi_mod.load(path)
            self.source_audio = None
            self.bdg_project = None
            self.bdg_report = None
            self.bdg_hints = {}

        self.midi = mf
        self.midi_path = path
        self.onsets = []
        self.onsets_override = None            # 换文件 ⇒ 收回来的进度作废
        self.chart = None
        self.chart_entry = []
        self.chart_times = []
        self.hit_times = []
        self.dp_report = {}
        self.load_info = self.track_map()

        info = dict(self.load_info)
        info["path"] = path
        info["name"] = os.path.splitext(os.path.basename(path))[0]
        info["is_audio"] = ext in AUDIO_EXTS
        info["stats"] = getattr(mf, "stats", lambda: "")()

        # 旧 UI `:627-637`：主轨光标默认 = 平均音高最高的旋律轨
        cand = [t for t in mf.tracks if t.notes and not t.is_drum_only()]
        if cand:
            # ★★ 2026-10「开门一遍」（用户：「力求打开软件之后**一遍就可以生成出优秀的
            #   铺面**供游玩」）：旋律性之外还要**别挑一条只覆盖后半首的轨**。
            #   实测 `MemoryLocked.mid` 的 trk0（小提琴，333 个音）第一个音在 **71.85s**，
            #   按旋律性胜出 ⇒ 自动 offset = 71850ms ⇒ 生成出来的谱**整段跳过前 71 秒**
            #   （而那 71 秒里钢琴一直在响）——「一遍就能玩」直接破功。
            #   做法：先筛「起点接近全曲最早音」的那些轨（`PICK_COVER_SLACK_MS`），
            #   再在它们里面按（平均音高，点数）挑；一条都不满足才退回旧口径。
            _t_first = min((n.t_on_ms for t in mf.tracks for n in t.notes), default=0.0)
            _cover = [t for t in cand
                      if min(n.t_on_ms for n in t.notes)
                      <= _t_first + PICK_COVER_SLACK_MS]
            pool = _cover or cand
            pick = max(pool, key=lambda t: (_avg_pitch(t), len(t.notes))).index
            info["pick_full_cover"] = bool(_cover)      # 是否筛过「覆盖全曲」
            info["pick_pool_n"] = len(pool)
            _missed = [t.index for t in cand if t not in pool]
            if _missed:
                # ★ 不许静默：把因为「起点太晚」被排除的轨报出来
                info["pick_skipped_late"] = _missed
        else:
            pick = 0
        # ★ 主轨现在是**多选 + 取并集**（哪条有音采哪条），所以默认只勾**启发式最优的那一条**。
        #   默认把全部非鼓轨都勾上等于「全采」：开箱结果音点暴增、直线率骤降
        #   （实测 doublepress_demo：184 → 328 onset，直线率 32% → 15%）。
        #   想要并集自己多勾几条即可。
        info["default_tracks_checked"] = [pick] if cand else []
        info["default_sub_checked"] = []      # ★ 次级轨默认空（想插空自己勾）
        info["default_dp_checked"] = [t.index for t in mf.tracks
                                      if t.notes and t.is_drum_only()]
        info["default_current_track"] = pick
        info["ppqn"] = mf.ppqn
        info["midi_bpm"] = mf.bpm0
        info["length_ms"] = mf.length_ms
        info["is_bdg"] = bool(getattr(self, "bdg_project", None))
        info["is_ts"] = bool(getattr(self, "ts_meta", None))
        if info["is_ts"]:
            # ★ 时间戳来源（`docs/45` §7）：三条默认值一起给前端 ——
            #   · `merge_ms=0`：30ms 的默认合并会把密集处的音**悄悄并掉**（时间戳是"点"，不是"音"）；
            #   · `fit_mode=direct`：这条路本来就是「时序优先」来的；
            #   · 去噪开：吸完格再拟合，`travel` 才是整齐有理数。
            info["ts"] = dict(self.ts_meta or {})
            info["ts_grid"] = dict(getattr(self, "ts_plan", None) or {})
            info["default_merge_ms"] = 0.0
            info["default_fit_mode"] = FIT_DIRECT
            info["default_denoise_on"] = True
            info["grid_fit"] = getattr(self.midi, "grid_fit", None)
        info["is_stem_json"] = bool(getattr(self, "stem_meta", None))
        if info["is_stem_json"]:
            # ★★ 时间戳 JSON（DEMUCS 分轨 · `docs/56` §4.2）：一路一条音轨，
            #    建议角色 + 默认勾选**照 BDG 那套**给（勾选权仍在用户）。
            #    ★ 建议是一回事、**默认勾**是另一回事：默认只勾 `melody` 当主轨
            #      （`docs/56` §6.1 的保守口径）—— 别的路只标出来，不乱动。
            hints = dict(getattr(self, "stem_hints", {}) or {})
            notes = dict(getattr(self, "stem_notes", {}) or {})
            dm, ds, dd = getattr(self, "stem_defaults", ([], [], []))
            info["stem"] = dict(self.stem_meta or {})
            info["stem_roles"] = {str(k): v for k, v in hints.items()}
            info["stem_notes"] = {str(k): v for k, v in notes.items()}
            info["stem_report"] = stem_mod.report_text(
                getattr(self, "stem_report", None) or {}, self.ts_plan)
            info["default_tracks_checked"] = list(dm) or ([pick] if cand else [])
            info["default_sub_checked"] = list(ds)
            info["default_dp_checked"] = list(dd)
            info["default_current_track"] = (list(dm) or [pick] or [0])[0]
            # ★ 原曲绑定（省得用户再找一遍）；找不到就是空串（上面已上屏说明）
            info["source_audio"] = self.source_audio or ""
        if info["is_bdg"]:
            # BDG 来源：默认勾选按**我们的建议角色**来（用户在界面上改了就是他的选择）
            hints = dict(getattr(self, "bdg_hints", {}) or {})
            info["bdg_roles"] = {str(k): v for k, v in hints.items()}
            mains = [i for i, r in hints.items() if r == "main"]
            subs = [i for i, r in hints.items() if r == "sub"]
            dps = [i for i, r in hints.items() if r == "dp"]
            info["default_tracks_checked"] = mains or ([pick] if cand else [])
            info["default_sub_checked"] = subs
            info["default_dp_checked"] = dps or info["default_dp_checked"]
            info["default_current_track"] = (mains or [pick] or [0])[0]
            info["bdg"] = {
                "name": getattr(self.bdg_project, "name", ""),
                "base_bpm": getattr(self.bdg_project, "base_bpm", 0.0),
                "offset_ms": getattr(self.bdg_project, "offset_ms", 0.0),
                "parser": getattr(getattr(self, "bdg_report", None), "parser", ""),
                "report": (self.bdg_report.summary() if self.bdg_report else ""),
                "speed": bdg_mod.source.speed_hints(self.bdg_project),
                "fit": bdg_mod.source.ladder_fit(self.bdg_project),
            }
        if getattr(mf, "grid_fit", None) is not None:
            info["grid_fit"] = mf.grid_fit.describe()
        if getattr(mf, "bias_info", None) is not None:
            info["detector_bias"] = float(getattr(mf, "detector_bias", 0.0) or 0.0)
        # ★★ 2026-10：载入时的**取舍账**也要交出去（以前只交 rebuild 的）
        #   —— 「原曲找不到」「某路是空的」「未排序/合并/丢弃了多少」必须**当场**上屏，
        #   不许等一次 rebuild 就没了。
        info["warning_list"] = list(self.warnings)
        # ★ 载入账**钉住**：之后每一次 rebuild 都带上它（见 `__init__` 的注释）
        self.load_warnings = list(self.warnings)
        return info

    def adopt_onsets(self, rows, merge_ms: float = 1.0) -> dict:
        """★ 把「编辑器收回的进度」钉成采音结果（`docs/38` §9.5）。

        `rows` = `[{ms, role, src_tracks}]`（`/api/bridge/adopt` 给的就是这个单位）。
        ⇒ 之后 `rebuild` 就用它，不再从选轨重采，直到换文件 / 清掉。

        ★ 修了一个**真实的不一致**：以前这里只留 `ms`，**源轨与角色都被吃掉**
          ⇒ 走完「收回改动」再投射，5 条泳道会塌回一条 `main:`（`docs/45` §6）。
          现在：
            · `src_tracks` 原样带过去（泳道不塌）；
            · `role == dp` 的点进 `dp_back`（和「返回数据到谱面生成器」同口径），
              不再冒充主轨 onset。
        """
        ons = []
        dp = []
        for r in rows or []:
            ms = float(r.get("ms", r.get("t_ms", 0.0)) or 0.0)
            role = str(r.get(al.K_ROLE) or al.ROLE_MAIN)
            tr = tuple(int(x) for x in (r.get(al.K_SRC_TRACKS) or ()))
            if role == al.ROLE_DP:
                idx = r.get(al.K_IDX)
                dp.append({"t_ms": ms, "role": al.ROLE_DP,
                           "idx": int(idx) if idx is not None else None,
                           "src_tracks": list(tr)})
                continue
            ons.append(onsets_mod.Onset(t_ms=ms, velocity=100, pitch=60,
                                        n_merged=1, pitches=(60,), src_tracks=tr,
                                        tick=int(round(ms * 960 * 120.0 / 60000.0))))
        ons.sort(key=lambda o: o.t_ms)
        self.onsets_override = ons if ons else None
        self.dp_back = dp
        n_lanes = len({(o.src_tracks[:1] or (-1,))[0] for o in ons})
        return {"n": len(ons), "n_dp": len(dp), "n_lanes": n_lanes,
                "ms_lo": round(ons[0].t_ms, 3) if ons else 0.0,
                "ms_hi": round(ons[-1].t_ms, 3) if ons else 0.0}

    def clear_override(self) -> None:
        self.onsets_override = None

    def lanes_map(self) -> dict:
        """★ 收回的轨道项目 → 三条列表（`docs/45`）。

        `index` = **泳道下标**（不是 MIDI 轨号 —— 收回模式下 MIDI 轨不参与）。
        `has_notes` 决定勾选框能不能点（空轨灰掉）。
        """
        trs, subs, dps = [], [], []
        role_zh = {al.ROLE_MAIN: "主", al.ROLE_SUB: "次",
                   al.ROLE_DP: "双押", al.ROLE_OFF: "关"}
        for i, l in enumerate(self.lanes_back):
            role = l["role"]
            tr = "trk%s" % l["src_track"] if l["src_track"] is not None else "—"
            name = l["name"] or "ADO·%s轨" % role_zh.get(role, role)
            summary = "{}　{}点　{:.0f}~{:.0f}ms　（BDG 收回·{}）".format(
                name, l["n"], l["ms_lo"], l["ms_hi"], tr)
            base = {"index": i, "notes": l["n"], "has_notes": bool(l["n"]),
                    "name": name, "drum": role == al.ROLE_DP, "channels": (),
                    "pitch_lo": 0, "pitch_hi": 0, "suggest": role,
                    "bdg": True, "src_track": l["src_track"], "lane": l["lane"],
                    "ms_lo": l["ms_lo"], "ms_hi": l["ms_hi"],
                    "summary": summary}
            trs.append(dict(base))
            subs.append(dict(base, label=summary + "（次级：只插空）"))
            dps.append(dict(base, label=summary + "（当双押轨）"))
        return {"tracks": trs, "sub": subs, "dp": dps,
                "lanes_back": True, "n_lanes": len(self.lanes_back)}

    def track_map(self) -> dict:
        """三条可勾选轨列表的内容（旧 UI `:611-654`）。

        ★ 有「从 BDG 收回的轨道项目」时（`docs/45`），这三条列表**就是那些轨**：
          用户口径「我们的工具使用的音轨就是 BDG 里面带时值数据的音轨」。
        """
        if self.lanes_back:
            return self.lanes_map()
        if self.midi is None:
            return {"tracks": [], "sub": [], "dp": []}
        trs, subs, dps = [], [], []
        # ★ 「建议角色」只有一个来源：BDG 工程（`bdg_hints`）或时间戳 JSON（`stem_hints`）
        hints = (getattr(self, "bdg_hints", None)
                 or getattr(self, "stem_hints", None) or {})
        # ★ 时间戳 JSON（`docs/56`）：与 BDG 同形状 —— 多一条「人话注释」，
        #   免得用户猜我们为什么这么建议（例：钢琴是赠品通道、人声建议主轨但默认不勾）
        snotes = getattr(self, "stem_notes", None) or {}
        for t in self.midi.tracks:
            trs.append({
                "index": t.index,
                "summary": onsets_mod.track_summary(t),
                "notes": len(t.notes),
                "drum": bool(t.is_drum_only()),
                "has_notes": bool(t.notes),
                "name": t.name,
                "channels": t.channels,
                "pitch_lo": min((n.pitch for n in t.notes), default=0),
                "pitch_hi": max((n.pitch for n in t.notes), default=0),
                # ★ BDG 来源：把「建议角色」带给界面（只是建议，勾选权在用户）
                "suggest": hints.get(t.index, ""),
                "note": snotes.get(t.index, ""),
            })
            kind = "架子鼓" if t.is_drum_only() else "旋律"
            # ★ 次级轨：与主轨同一份列表内容，只换标签（「只插空」）
            subs.append({
                "index": t.index,
                "label": (f"次级轨 trk{t.index} {t.name or '(无名)'}"
                          f"（{len(t.notes)} 音·{kind}）"),
                "notes": len(t.notes),
                "drum": bool(t.is_drum_only()),
                "has_notes": bool(t.notes),
            })
            dps.append({
                "index": t.index,
                "label": (f"双押轨 trk{t.index} {t.name or '(无名)'}"
                          f"（{len(t.notes)} 音·{kind}）"),
                "notes": len(t.notes),
                "drum": bool(t.is_drum_only()),
                "has_notes": bool(t.notes),
            })
        return {"tracks": trs, "sub": subs, "dp": dps}

    # ============================================================ 选轨派生
    def _dp_tracks(self, st) -> list[int]:
        """旧 UI `_dp_tracks()` `:717-728`。"""
        trs = self.midi.tracks
        return [i for i in (st.get("dp_checked") or [])
                if 0 <= i < len(trs) and trs[i].notes]

    def _sub_tracks(self, st) -> list[int]:
        """★ 次级轨（用户 2026-10）：「权重最高的全采，权重低的**只插空**」。

        主轨（`tracks_checked`）全采；次级轨（`sub_checked`）只在主轨连续空白
        超过 `sub_gap_ms` 的地方补音。与双押轨互斥（双押轨是并排轨，不参与采音）。
        """
        trs = self.midi.tracks
        dp = set(self._dp_tracks(st))
        return [i for i in sorted({int(x) for x in (st.get("sub_checked") or [])})
                if 0 <= i < len(trs) and trs[i].notes and i not in dp]

    def _selected(self, st, dp: list[int]) -> list[int]:
        """★ 主轨 = 勾选的那几条（多选），**取并集**：哪条有音采哪条。

        双押轨不参与并集（它们是「同一下同时按下」的并排轨）。
        ★ 2026-10 起，如果另外勾了**次级轨**（`_sub_tracks`），主轨并集变成
          「骨架」：次级轨只往它的空白缝隙里插音（`_fill_mode`）。
        """
        trs = self.midi.tracks
        return [i for i in sorted({int(x) for x in (st.get("tracks_checked") or [])})
                if 0 <= i < len(trs) and trs[i].notes and i not in dp]

    def derive(self, st) -> dict:
        """把「勾选的轨」推导成实际采音集合 + 音高过滤建议。

        ★ 主轨是多选：`selected` 就是**勾选的那几条取并集**（哪条有音采哪条），
        没有主/补之分。仍保留「换轨时自动覆写 pitch_lo/hi」的旧行为
        （对等 `_on_track_changed()`，会冲掉用户手改的音高过滤，前端会提示）。
        """
        if self.midi is None:
            return {"primary": 0, "selected": [], "dp_tracks": [],
                    "pitch_lo": None, "pitch_hi": None, "hint": "未加载"}
        if self.lanes_back:
            # ★ 轨道项目模式（`docs/45`）：音轨 = 从 BDG 收回的那些，
            #   所以「选中哪几条 MIDI 轨」这件事整个不适用（勾选框只表示高亮）。
            n_main = sum(1 for l in self.lanes_back
                         if l["role"] in (al.ROLE_MAIN, al.ROLE_SUB))
            n_dp = sum(1 for l in self.lanes_back if l["role"] == al.ROLE_DP)
            return {"primary": 0, "selected": [], "dp_tracks": [],
                    "sub_tracks": [], "fill_mode": False,
                    "pitch_lo": None, "pitch_hi": None,
                    "lanes_back": True, "n_lanes_back": len(self.lanes_back),
                    "hint": ("音轨 = 从 BDG 收回的 {} 条（主/次 {} + 双押 {}）· "
                             "共 {} 点 · 时间用 BDG 的时值（不再从 MIDI 采音）"
                             .format(len(self.lanes_back), n_main, n_dp,
                                     len(self.onsets_override or [])))}
        trs = self.midi.tracks
        dp = self._dp_tracks(st)
        sel = self._selected(st, dp)
        sub = self._sub_tracks(st)
        prim = sel[0] if sel else 0     # 只为兼容旧字段：并集里的第一条
        return_sub = [i for i in sub if i not in set(sel)]
        self.sub_tracks = return_sub
        self.fill_mode = bool(sel and return_sub)

        cur = int(st.get("current_track", 0))
        lo, hi = None, None
        if 0 <= cur < len(trs) and trs[cur].notes:
            lo = min(n.pitch for n in trs[cur].notes)
            hi = max(n.pitch for n in trs[cur].notes)
        alln = [n for i in sel for n in trs[i].notes]
        # ★ 音高建议要覆盖「用户**在任何地方**显式选中的轨」= 主轨 ∪ 区间轨 ∪ 双押轨。
        #   只按主轨推导会埋一个哑弹：自动改写的 pitch_lo/hi 会把区间里指定的轨、
        #   或双押轨整条滤没（实测 主轨 accent-bell → 91..91，节拍器双押轨被滤空，
        #   双押一处都插不进去；区间改用的 melody 轨也变成 0 个点）。
        extra_idx = set(dp)
        extra_idx |= set(return_sub)      # ★ 次级轨也要覆盖，否则会被音高过滤滤空
        for rg in (st.get("regions") or []):
            for x in (rg.get("tracks") or []):
                try:
                    extra_idx.add(int(x))
                except (TypeError, ValueError):
                    continue
        # ★ 分段里的轨也要覆盖（同一个哑弹：分段改用的轨会被自动改写的
        #   pitch_lo/hi 整条滤没，实测会变成 0 个点）
        for sg in (st.get("segments") or []):
            if not isinstance(sg, dict):
                continue
            for k in (seg_mod.K_MAIN, seg_mod.K_SUB, seg_mod.K_DP):
                for x in (sg.get(k) or []):
                    try:
                        extra_idx.add(int(x))
                    except (TypeError, ValueError):
                        continue
        extra_idx = {i for i in extra_idx if 0 <= i < len(trs)}
        alln += [n for i in extra_idx for n in trs[i].notes]
        if alln:
            lo = min(n.pitch for n in alln)
            hi = max(n.pitch for n in alln)

        n_notes = sum(len(trs[i].notes) for i in sel)
        if sel:
            hint = (f"主轨 {['trk%d' % i for i in sel]} 取并集 · {n_notes} 个 note-on"
                    f" · 双押轨 {dp or '（无）'}")
            if return_sub:
                hint += (f" · 次级轨 {['trk%d' % i for i in return_sub]}"
                         f" 只插空（> {float(st.get('sub_gap_ms', 600.0)):g}ms 的空白）")
        else:
            hint = "主轨一条都没勾（可在区间里单独指定音轨）"
        return {"primary": prim, "selected": sel, "dp_tracks": dp,
                "sub_tracks": return_sub, "fill_mode": bool(self.fill_mode),
                "pitch_lo": lo, "pitch_hi": hi, "hint": hint}

    # ============================================================ 参数映射
    def params_onset(self, st) -> onsets_mod.OnsetParams:
        return onsets_mod.OnsetParams(
            merge_ms=float(st.get("merge_ms", 30)),
            merge_anchor=str(st.get("merge_anchor", "first")),
            min_velocity=int(st.get("min_velocity", 1)),
            min_interval_ms=float(st.get("min_interval_ms", 0)),
            pitch_lo=int(st.get("pitch_lo", 0)),
            pitch_hi=int(st.get("pitch_hi", 127)),
            max_onsets=int(st.get("max_onsets", 0)),
        )

    def params_solve(self, st, onsets, dp_reserve=()) -> tuple[SolveParams, dict]:
        """旧 UI `_params_solve()` `:846-887`。返回 (params, 显示信息)。

        `dp_reserve` = 要替双押**预留槽位**的 onset 下标（`docs/31` §5.2 的 a）。
        空元组 ⇒ `solve()` 里那段一行都不执行，老路径逐字节不变。
        """
        mf = self.midi
        ppqn = mf.ppqn
        mbpm = mf.bpm0
        preset = SC.STRAIGHT_VALUES[
            min(max(int(st.get("straight_preset", 1)), 0),
                len(SC.STRAIGHT_VALUES) - 1)]
        ref_i = min(max(int(st.get("ref_index", 0)), 0), len(SC.BEAT_BEATS) - 1)
        tw_i = min(max(int(st.get("twirl_index", 0)), 0), len(SC.TWIRL_VALUES) - 1)
        snow_i = min(max(int(st.get("snown_index", 0)), 0), 3)

        # ★★ 速度档代价的封顶（`docs/72` §6 的根因修复）。
        #   补格（`fill_div > 0`）会造出 r=1/8 的短格，而**短格想走直就必然要多花八度** ——
        #   不封顶时「直线 travel=180 k=1/8」得 1−0.35×3 = **−0.050**，
        #   输给「发卡弯 travel=22.5 k=1」的 **0.000**（差 0.05）⇒ 整条路被发卡弯铺满。
        #   所以补格开着时**自动**封到 2 个八度，并且**上屏说明**（不许静默）；
        #   用户显式写了 `speed_penalty_max_oct` 就听用户的。
        _spmo = float(st.get("speed_penalty_max_oct") or 0.0)
        if int(st.get("fill_div") or 0) > 0 and _spmo <= 0:
            _spmo = 2.0
            self.warnings.append(
                "补格开着 ⇒ 自动把「速度档代价封顶」设成 **2 个八度**"
                "（`speed_penalty_max_oct=2`）：不然补出来的 1/8 拍短格会被判成"
                "「走直不如发卡弯」，整条路会碎（`docs/72` §6）。"
                "想用老口径就显式写 `--set speed_penalty_max_oct=0`…"
                "（注意 0 = 不封顶 = 老口径，所以要用别的值覆盖时写 >0 的数）")

        p = SolveParams(
            straight_weight=STRAIGHT_PRESETS[preset],
            speed_penalty_max_oct=_spmo,
            # ★★ 降速档额外代价（`k < 1`）：真人不降速换直线。默认 0 = 老口径。
            slow_speed_penalty=float(st.get("slow_speed_penalty") or 0.0),
            # ★ 一档至少连续几层（core 里早就有、sidecar 一直没接线 ⇒ 死参数）。
            speed_min_run=int(st.get("speed_min_run", 6) or 6),
            beat_beats=float(SC.BEAT_BEATS[ref_i]),
            ppqn=ppqn,
            midi_bpm=mbpm,
            twirl_mode=SC.TWIRL_VALUES[tw_i],
            twirl_limit_deg=float(st.get("twirl_limit_deg", 240)),
            bpm_max=float(st.get("bpm_max", 400)),
            allow_set_speed=bool(st.get("allow_set_speed", True)),
            speed_tiers=(SPEED_TIERS if st.get("allow_set_speed", True) else (1,)),
            quantize_rhythm=bool(st.get("quantize_rhythm", True)),
            use_templates=bool(st.get("use_templates", True)),
            template_only_nonstraight=bool(
                st.get("template_only_nonstraight", True)),
            use_pause=bool(st.get("use_pause", True)),
            pause_min_beats=float(st.get("pause_min_beats", 4.0)),
            # ★★ 「使用激进的拟合策略」把「最小角度」**钉死**成 15°（用户口径：
            #   「拟合的时候最小角度为 15°，只允许 15 30 45 60 75 90……」）。
            #   界面那边会把框置灰并显示 15；后端**照样强制**，不信前端。
            #   ★ 2026-10 第二版：用户又定了「**最小夹角不许低于 30°**」——
            #   所以取 `max(阶梯下界, 用户框里的值)`：15° 只是阶梯的**步长**，
            #   用户写了 30 就按 30 走（阶梯仍然只走 15° 的整数倍）。
            travel_min=max(
                FIT_LADDER_MIN_DEG if bool(st.get("aggressive_fit", False)) else 0.0,
                float(st.get("travel_min", 20.0))),
            # ★★ 用户 2026-10 第二版：「**最大夹角不能超过 270°**」。
            #   `0` = 不设上界（老口径）⇒ 默认下逐字节不变。
            travel_max=float(st.get("travel_max", 0.0) or 0.0),
            # ★ 闭合图形使用策略（用户 2026-10）：UI 存的是 index，映射成 -1/0/+1
            closed_figure_bias=int(SC.CLOSED_VALUES[
                min(max(int(st.get("closed_bias_index", 1)), 0),
                    len(SC.CLOSED_VALUES) - 1)]),
            # ★ 回正（2026-10 才接到 UI 上）
            straighten=bool(st.get("straighten", True)),
            straighten_min_run=int(st.get("straighten_min_run", 8)),
            straighten_theta=float(st.get("straighten_theta", 20.0)),
            # ★ 对音阶梯（docs/25）：**并列的第二条路径**，默认关。
            #   关掉时 `solve()` 里那一行分支不会执行 —— 老路径逐字节不变。
            aggressive_pick=bool(st.get("aggressive_pick", False)),
            ladder_outer_mode=str(st.get("ladder_outer_mode", "auto")),
            ladder_outer_cbpm=float(st.get("ladder_outer_cbpm", 400.0)),
            ladder_tier_order=str(st.get("ladder_tier_order", "straight")),
            use_snowflake=bool(st.get("use_snowflake", False)),
            snowflake_min_tiles=int(st.get("snowflake_min_tiles", 10)),
            snowflake_full_tiles=float(st.get("snowflake_full_tiles", 48)),
            snowflake_n_rot=tuple(SC.SNOWN_VALUES[snow_i]),
            # ★ 「雪花允许使用参数的部位极少」的修复：形状 / 最少臂 / 紧凑 /
            #   等间隔容差 / 随机采样 / 种子 全部接进来（见 docs/24 §4）。
            snowflake_shape=str(st.get("snowflake_shape", "auto")),
            snowflake_min_arms=int(st.get("snowflake_min_arms", 2)),
            snowflake_compact=bool(st.get("snowflake_compact", True)),
            snowflake_random=bool(st.get("snowflake_random", False)),
            snowflake_trials=int(st.get("snowflake_trials", 32)),
            snowflake_uniform_tol_ms=float(st.get("snowflake_uniform_tol_ms", 3.0)),
            snowflake_seed=int(st.get("snowflake_seed", 20240213)),
            # ★ 轨道位置偏移（用户 2026-10：「改为可选是否开启」）——
            #   core 里一直有这个开关，但 sidecar 从没接手过，UI 因此改不动。
            # ★ 2026-10 用户：「方块位置偏移的选项可以开局为关闭了」⇒ 默认 False
            use_position_track=bool(st.get("use_position_track", False)),
            pos_track_step=float(st.get("pos_track_step", 0.22)),
            pos_track_min_beats=float(st.get("pos_track_min_beats", 8.0)),
            # ★ 双押预留槽位（a，docs/31 §5.2）：空 ⇒ 老路径逐字节不变
            dp_reserve=tuple(dp_reserve or ()),
            dp_theta=float(st.get("dp_theta", 0.0)),
            dp_skew_max_ms=float(st.get("dp_skew_max_ms", 25.0)),
            # ★★ 用户 2026-10 **定死**：双押角度 = 不同 bpm 窗口的**规定写法**，
            #   **不按毫秒、不能自定义** ⇒ 默认 True（这一条就是那个开关）
            use_fixed_dp_angle=bool(st.get("use_fixed_dp_angle", True)),
            # ★★ 用户 2026-10：「为三押添加开关，可以不一定生成双押」
            #   0 拆三押（默认，老行为）/ 1 不拆（按双押插）/ 2 跳过（连双押也不插）
            three_press_mode=int(st.get("three_press_mode", 0)),
            # ★★ 用户 2026-10：「算法会贪心倾向于原地打转」⇒ 图形层的**原地惩罚**
            #   （默认开；`False` = 老口径，A/B 与冻结哈希用）。见 core/figures.py。
            inplace_waste=bool(st.get("inplace_waste", True)),
        )
        shown_bpm = None
        if st.get("auto_bpm", True):
            # ** 2026-10 用户指令：自动基准 BPM **直接读用户在生成页填的值**
            #   (写进 JSON 的 `bpm_hint`)；填的值 <200 自动 x2，>=200 原样用。
            #   OSN1 时间戳工程带 `bpm_hint` (backend 落盘) => 这里直接吃它；
            #   并把结果写进 `p.base_bpm`，让 solve 走`手动基准`分支，
            #   **显示值 == 求解输入** (修掉风险 10)。
            if getattr(self, "bpm_hint", 0.0) and self.bpm_hint > 0:
                _av = solve_mod.auto_base_bpm(
                    onsets, 180.0, 0.85, ppqn=ppqn,
                    midi_bpm=mbpm, bpm_hint=self.bpm_hint)
                p.base_bpm = float(_av)
                shown_bpm = float(_av)
            else:
                # 旧逻辑 (无 bpm_hint: MIDI/TS 工程): 只回填显示，
                # `p.base_bpm` 保持 0，真正基准由 solve() 内部再算一次
                try:
                    beats, _ = solve_mod.beats_of(onsets, ppqn, mbpm or 120.0)
                    if beats:
                        _C, bpm, _a, _b, _c = solve_mod.choose_reference(
                            beats, p, mbpm or 120.0)
                    else:
                        bpm = 0.0
                    if bpm <= 0:
                        bpm = solve_mod.auto_base_bpm(onsets, 180.0, 0.85, ppqn=ppqn,
                                                      midi_bpm=mbpm)
                    shown_bpm = float(bpm)
                except Exception:
                    shown_bpm = None
        else:
            p.base_bpm = float(st.get("base_bpm", 180))
            shown_bpm = p.base_bpm
        return p, {"display_bpm": shown_bpm}

    # ============================================================ offset 自检
    def audio_lead_ms(self, st) -> float:
        """**我们实际交出去的那份音频**最前面补了多少毫秒静音（`S`）。

        ★ 这是「偏移修正」的关键前提（docs/24 §5）。`audio_for_current()` 是两副面孔：

          · MIDI / 无源音频 → `synth.render(lead_ms=preview_lead_ms)` 重新合成，
            音频**补了 `preview_lead_ms` 的前置静音**（给倒计时 + 开局站位让时间）
          · 有 `source_audio`（用户直接载入的 ogg/mp3） → **原样返回原始文件**，`S = 0`

        旧代码无条件把 `S` 当成 `preview_lead_ms`，于是 OGG 输入的
        「建议 offset」「自动 offset」全都多算了 `lead`，实测预览/成品整体偏
        **2313ms**（`docs/21` §5 的实测值）。

        统一口径：游戏时刻 = `offset + entryTime`，音频时刻 = `游戏时刻`；
        ⇒ `offset = onsets[0] + S − entryTime[0]`。

        ★ 2026-10「预览音源」三档（`preview_audio_file`）：只要交出去的是**文件**
          （原曲 / 用户指定的 ogg）⇒ `S = 0`，与 `source_audio` 同一口径。
          `preview_audio_offset_ms` **不算在这里** —— 它是预览专用的前端旋钮，
          不写进谱面（否则 offset 会跟着它漂）。
        """
        if self.preview_audio_file(st):
            return 0.0
        return float(self.preview_lead_ms or 0.0)

    def shipped_len_ms(self, st) -> float:
        """交出去的音频总长（ms）—— 源音频原样时不含前置静音。"""
        if self.midi is None:
            return 0.0
        return float(self.midi.length_ms) + self.audio_lead_ms(st)

    def _offset_check(self, chart, offset_ms, cd, audio_len_ms, shipped) -> dict:
        """`solve.check_offset` 的**双押安全版**。

        为什么不能直接用 `solve_mod.check_offset`：它内部用
        `entry_time_of_onsets()`，那是最朴素的「层 ↔ onset」1:1 映射；
        插了中旋层之后映射整体后移，误差会被算成十几秒（实测 12624ms 的假报）。
        这里改用**已经重映射过的 `self.hit_times`**，物理含义不变：

            预测命中时刻 = offset + hit_time[i]
            成品音频里的实际时刻 = onset[i].t_ms + shipped_shift

        ⚠ 单位提醒：`check_offset` 的字段叫 `*_ms` 但旧 UI 的文案写成 `us`，
        实际就是**毫秒**。这里纠正文案。
        """
        onsets = self.onsets
        if not self.dp_inserted:
            return solve_mod.check_offset(chart, offset_ms, cd,
                                         [o.t_ms for o in onsets],
                                         audio_len_ms, shipped)
        n = min(len(self.hit_times), len(onsets))
        errs = [abs((offset_ms + self.hit_times[i]) - (onsets[i].t_ms + shipped))
                for i in range(n)]
        all_et = solve_mod.times_from_chart(chart)
        cd_add = max(0, int(cd) - 1) * (60000.0 / max(1e-9, chart.base_bpm))
        last_entry = (all_et[-1] if all_et else 0.0) + cd_add
        return {
            "max_err_ms": max(errs) if errs else 0.0,
            "mean_err_ms": (sum(errs) / len(errs)) if errs else 0.0,
            "n": n,
            "suggest_ms": ((onsets[0].t_ms + shipped - self.hit_times[0])
                           if (self.hit_times and onsets) else 0.0),
            "offset_plus_last_s": (offset_ms + last_entry) / 1000.0,
            "audio_len_s": audio_len_ms / 1000.0,
            "tail_gap_s": (audio_len_ms - (offset_ms + last_entry)) / 1000.0,
            "lead_ms": shipped,
            "mapped": True,
        }

    def first_onset_ms(self) -> float:
        """★ 「自动 offset」的**唯一口径**：采出来的**第一个 onset**（ms），原样返回。

        2026-10-05 主人拍板（实测口径）：自动 offset 就是采音结果里最上面那个数，
        等价于采音 JSON 的 `stems.<轨>.onsets_sec[0] × 1000`
        （实测 Amulet：`2.8154` → **2815.4ms**）。

        · **不**加前置静音 `S` —— 旧公式 `onsets[0] + S − entryTime[0]` 里的 S 与
          「第一格 entry」两项一律去掉，`audio_lead_ms()` 不再参与本值。
        · 7ch 路由下 melody / vocals / piano 三轨是**同一批 onset**
          （`source=full_mix`，见 `osn1_stemjson.extract`），首值必然相同
          ⇒「取最上面那条轨」与「取当前轨」得到的是同一个数。

        自动 offset（回填 offset 框）与手动模式下的「建议 offset」都**必须**走这里，
        免得同一个东西两个出口各说各话。
        """
        return float(self.onsets[0].t_ms) if self.onsets else 0.0

    # ============================================================ 区间采音
    def norm_regions(self, st) -> list[dict]:
        """规范化区间列表：裁掉无音轨/零长度的，按开始时间排序，夹在曲子范围内。

        区间（`state["regions"]`）的语义：**这段时间内改用指定的几条音轨采音**，
        区间外仍走全局的「② 音轨」选择。它只改「采哪些音」，不改几何/时序逻辑 ——
        所以求解器、模板、雪花、双押全部无感。
        """
        raw = st.get("regions") or []
        if self.midi is None:
            return []
        trs = self.midi.tracks
        total = float(self.midi.length_ms)
        out = []
        for i, rg in enumerate(raw):
            try:
                t0 = float(rg.get("start_ms", 0.0))
                t1 = float(rg.get("end_ms", 0.0))
            except (TypeError, ValueError):
                continue
            if t1 <= t0:
                continue
            t0 = max(0.0, min(t0, total))
            t1 = max(0.0, min(t1, total))
            if t1 - t0 < 1.0:
                continue
            tis = [int(x) for x in (rg.get("tracks") or [])
                   if 0 <= int(x) < len(trs) and trs[int(x)].notes]
            d = {"index": i, "start_ms": t0, "end_ms": t1, "tracks": tis,
                 "label": str(rg.get("label") or f"区间{i + 1}")}
            out.append(d)
        out.sort(key=lambda r: (r["start_ms"], r["end_ms"]))
        return out

    def apply_regions(self, st, base: list, p_on) -> list:
        """把区间内的 onset 换成「用区间指定音轨采出来的那一批」。

        · 区间外：`base`（全局选择的结果）原样保留
        · 区间内：**先整段丢掉**全局 onset，再放进区间自己的；区间重叠时**后写的赢**
        · 最后按 `merge_ms` 去重（边界上两侧各有一个 onset 时只留最早的那个，
          与 `build_onsets` 内部的合并语义一致）
        """
        regs = self.norm_regions(st)
        self.region_meta = []
        if not regs:
            self.region_meta = []
            return base
        trs = self.midi.tracks
        out = list(base)
        for rg in regs:
            tis = rg["tracks"]
            if not tis:
                self.region_meta.append({**{k: rg[k] for k in
                                            ("index", "label", "start_ms",
                                             "end_ms")},
                                         "tracks": [], "n": 0, "skipped": "无音轨"})
                continue
            # ★ 区间也是「这几条轨取并集」，与主轨同一套语义
            r_on = onsets_mod.build_onsets_multi([trs[i] for i in tis], p_on)
            t0, t1 = rg["start_ms"], rg["end_ms"]
            r_on = [o for o in r_on if t0 <= o.t_ms < t1]
            out = [o for o in out if not (t0 <= o.t_ms < t1)] + r_on
            self.region_meta.append({"index": rg["index"], "label": rg["label"],
                                     "start_ms": t0, "end_ms": t1,
                                     "tracks": tis,
                                     "n": len(r_on), "skipped": ""})
        out.sort(key=lambda o: o.t_ms)
        gap = max(0.0, float(p_on.merge_ms))
        dedup: list = []
        for o in out:
            if dedup and (o.t_ms - dedup[-1].t_ms) < gap:
                continue
            dedup.append(o)
        return dedup

    # ======================================================== 分段采音（方案 C）
    def norm_segments(self, st) -> list:
        """`state["segments"]` → `core.segments.Segment` 列表。

        与「区间」（`norm_regions`）的区别见 `docs/34`：
        区间只能改**主轨**一维、且必须自己填结尾毫秒；分段能让**三个角色**
        都随时刻变，最后一段天然到曲末。**同时存在时分段优先**（会报警告）。
        """
        self.segment_mode = seg_mod.mode_of(st)
        if self.midi is None:
            return []
        trs = self.midi.tracks
        return seg_mod.normalize(
            st.get("segments") or [],
            mode=self.segment_mode, total_ms=float(self.midi.length_ms),
            n_tracks=len(trs), has_notes=lambda i: bool(trs[i].notes))

    def apply_segments(self, st, segs, p_on) -> list:
        """★ 用分段重算**整条主采音**（`docs/34` 方案 C）。

        它**取代**「步骤 4 全局主轨」+「步骤 4b 区间」两步 —— 因为分段的
        第一片本来就是「全局」，不需要在它之外再拼一次。
        """
        trs = self.midi.tracks
        d = self.derive(st)
        ons, self.segment_meta = seg_mod.sample(
            trs, segs, self.segment_mode, float(self.midi.length_ms),
            d["selected"], d["sub_tracks"], p_on,
            gap_ms=float(st.get("sub_gap_ms", seg_mod.DEFAULT_GAP_MS)),
            glob_dp=d["dp_tracks"])
        sm = seg_mod.summary(self.segment_meta)
        if sm[seg_mod.K_N_CROSS]:
            # ★ 不许静默：边界处两次按键并成一次 —— 段界只决定「谁进池」，
            #   聚类是全曲一次的，所以这属于**预期行为**，但必须让用户看见。
            self.warnings.append(
                f"分段边界处有 {sm[seg_mod.K_N_CROSS]} 处相邻音被 merge_ms 并成一次"
                f"（调小 merge_ms 或把边界挪开可避免）")
        for r in self.segment_meta:
            if r[seg_mod.K_N] == 0:
                self.warnings.append(
                    f"分段「{r[seg_mod.K_LABEL]}」"
                    f"（{r[seg_mod.K_T0] / 1000:.2f}s~{r[seg_mod.K_T1] / 1000:.2f}s）"
                    f"一个点都没采到：主{r[seg_mod.K_MAIN] or '—'} "
                    f"次{r[seg_mod.K_SUB] or '—'} 在这段时间里没有可用的音")
        return ons

    # ======================================================== 去噪（`docs/44`）
    def _denoise_onsets(self, st) -> dict:
        """★ 把抖动时间戳吸到格上，**就地替换** `self.onsets`（时间变了，身份不变）。

        用 `core.denoise`（它是宿主吸附 `snapBeat` 的逐字对等实现）⇒ 我们这边吸完
        的点，交给 BDG 也在同一批格上，不会再被它的吸附挪一次（`docs/44` §结论）。

        参数（都在 state 里，界面上可改）：
          · `denoise_on`       —— 关掉就完全不动（老路径）
          · `denoise_hint_ms`  —— 已知砖长（0 = 自动：间隔对数直方图众数）
          · `denoise_div`      —— 分母（0 = 自动：最细但不撞格的那一档）
          · `fit_tol_ms`       —— ★ **拟合容差**（0~100ms；用户 2026-10 定）：
              0 = 一个点都不挪（保真）；给值 = 只把 ±容差 以内的抖动吸到格上，
              **超出的原样保留并报出来**（BDG 的吸附是全有全无，我们比它细一档）。
              ※ 以前这个参数叫 `denoise_radius_ms` 且 **0 = 全吸** —— 与用户
                「容差」的直觉相反（0 却是最凶的），已按用户口径改正。
          · `aggressive_fit`   —— ★ 激进拟合：把「常规线」换成 **15° 阶梯**
              （见 `core/fitdirect.snap_to_ladder`）。这时**去噪不再挪点**
              （同一个容差预算不能花两次），由阶梯吸附统一负责时间修正。
        """
        self.denoise_meta = {}
        if not bool(st.get("denoise_on", True)) or len(self.onsets) < 2:
            return {}
        tol, agg = _fit_state(st)
        hint = float(st.get("denoise_hint_ms") or 0.0) or None
        div = int(st.get("denoise_div") or 0) or None
        # ★ 容差 = 吸附半径（`0` = 一个点都不挪）；激进模式下去噪只算网格、不挪点
        rad = 0.0 if agg else tol
        ms = [float(o.t_ms) for o in self.onsets]
        try:
            g = denoise_mod.plan(ms, hint=hint, div=div)
            dr = denoise_mod.denoise(ms, g, radius=rad)
        except Exception as exc:                                # noqa: BLE001
            self.warnings.append(f"去噪算不动（{exc}）⇒ 原样用未去噪的点")
            return {}
        if not dr.get("ok"):
            self.warnings.append("去噪没做：" + str(dr.get("error") or "?")
                                 + "（原样用未去噪的点）")
            return {}
        # ★ 就地替换：按 `src` 映射回原 onset（撞格丢掉的点不在里面）
        src = list(dr.get("src") or range(len(dr["ts"])))
        new = []
        for j, t in enumerate(dr["ts"]):
            o = self.onsets[src[j]] if 0 <= src[j] < len(self.onsets) else None
            if o is None:
                continue
            o.t_ms = float(t)
            new.append(o)
        n_before = len(self.onsets)
        self.onsets = new
        rep = dr.get("report") or {}
        self.denoise_meta = {
            "on": True, "grid": dr["grid"], "mode": dr.get("mode"),
            "n_in": n_before, "n_out": dr["n_out"], "n_dropped": dr["n_dropped"],
            "n_out_of_radius": dr.get("n_out_of_radius", 0),
            "report": rep, "text": dr.get("text", ""),
            "phase_ms": dr["grid"].get("phase_ms"),
            "period_ms": dr["grid"].get("period_ms"),
            "step_ms": dr["grid"].get("step_ms"),
            "div": dr["grid"].get("div"), "bpm": dr["grid"].get("bpm"),
            "conf": dr["grid"].get("conf"),
        }
        self.warnings.append(
            "去噪 {}/{} 点落格 · 砖长 {:.3f}ms(bpm {:.2f}) · 1/{} 格 {:.3f}ms · "
            "挪动 中位 {:.2f}/max {:.2f}ms · 容差 {:.0f}ms".format(
                dr["n_out"], n_before, self.denoise_meta["period_ms"] or 0.0,
                self.denoise_meta["bpm"] or 0.0, self.denoise_meta["div"] or 0,
                self.denoise_meta["step_ms"] or 0.0,
                rep.get("move_median_ms", 0.0), rep.get("move_max_ms", 0.0), tol))
        if agg:
            self.warnings.append(
                "★ 激进拟合：去噪**只算网格不挪点**（容差 {:.0f}ms 交给 15° 阶梯吸附，"
                "同一个预算不花两次）".format(tol))
        if dr["n_dropped"]:
            self.warnings.append(
                "⚠ 去噪有 {} 个音**撞进同一格**被丢（分母 1/{} 太细或本来就同拍）"
                .format(dr["n_dropped"], self.denoise_meta["div"]))
        if dr.get("n_out_of_radius"):
            if agg:
                # ★★ 2026-10：激进模式下 `rad` 是**故意**的 0（去噪只算网格），
                #   以前这里照样打印 `rad` ⇒ 用户设了 50ms 却看到
                #   「拟合容差 0.0ms…**原样保留**（想全吸就把容差调大）」——
                #   读了会以为「激进策略根本没生效」，而且照着它调容差**永远没用**
                #   （激进模式下挪点的是 15° 阶梯，不是去噪）。
                self.warnings.append(
                    "去噪**故意一个点都没挪**（激进模式：容差 {:.0f}ms 整个交给 "
                    "15° 阶梯）；下面那条「修正 N 音」才是激进模式真正的账"
                    .format(tol))
            else:
                self.warnings.append(
                    "拟合容差 {:.1f}ms：{} 个点离格超过它，**原样保留**（想全吸就把"
                    "容差调大）".format(rad, dr["n_out_of_radius"]))
        return self.denoise_meta

    def tempo_diag(self, st=None) -> dict:
        """★ **自动贴合**（`docs/58`）：给当前载入的曲子做一次「时值体检」。

        回答四件事：哪条路能定砖长 / 真砖长是多少 / 抖多大 / 该填什么参数。
        **纯报告**：不改 `self` 的任何状态，也不动默认值 —— 套不套用由用户点按钮。

        `st` 可给当前 state：只用它排序「主轨」的偏好（用户已经勾了哪条轨就当参考）。
        """
        if self.midi is None or not getattr(self.midi, "tracks", None):
            return {"ok": False, "why": "先载入一个文件（MIDI / BDG 工程 / 时间戳）"}
        st = st or {}
        streams = []
        for t in self.midi.tracks:
            ms = [float(n.t_on_ms) for n in t.notes]
            if len(ms) < 2:
                continue
            streams.append({"key": str(int(t.index)), "label": t.name or ("trk%d" % t.index),
                            "ms": ms, "notes": len(ms),
                            "picked": int(t.index) in set(st.get("tracks_checked") or [])})
        if len(streams) < 1:
            return {"ok": False, "why": "这份文件里没有可分析的音轨（都太短）"}
        # ★ 用户已经勾了主轨 ⇒ 体检也认它是主轨（别和用户打架），报告里只提醒
        prefer = [s["key"] for s in streams if s.get("picked")]
        try:
            rep = tempo_diag_mod.diagnose(streams)
        except Exception as exc:                            # noqa: BLE001
            return {"ok": False, "why": "体检算不动：%s: %s" % (type(exc).__name__, exc)}
        rep["picked_main"] = prefer
        if rep.get("ok") and prefer and prefer[0] != str(rep.get("main_key")):
            rep.setdefault("why", []).append(
                "★ 你现在勾的主轨不是体检建议的那条（建议 %s）—— 想听我的就点「套用」"
                % rep["suggest"]["main_label"])
        rep["source_hint"] = (getattr(self, "stem_meta", None) or {}).get("src") or ""
        rep["n_tracks"] = len(streams)
        return rep

    # ======================================================== 桥的时序锚
    def dp_lane_onsets(self) -> list:
        """★ **双押单独一条泳道**（`docs/42`，用户口径「把双押单开一个轨道」）。

        双押轨本身不参与采音（它只标记**哪些 onset 出双押**），所以这里交出去的是
        **真的那批双押点**（上一次 `rebuild` 算出来的 `dp_hit_idx` 对应的 onset），
        而不是双押轨上的音。

        ★ 没跑过 `rebuild` / 没有双押轨 ⇒ 返回空列表（不编、不猜）。
        """
        idx = [int(i) for i in (getattr(self, "dp_hit_idx", None) or [])]
        # ★ 从 BDG 收回来时（`docs/45`），双押轨的点**就是**权威 —— 用它们，
        #   不再靠「双押轨上的音 ↔ onset 对齐」重新认一遍。
        if self.dp_back:
            return [dict(d) for d in self.dp_back]
        if not idx or not self.onsets:
            return []
        # ★★ 2026-10 · 用户「三押轨道不出现」：押数 ≥3 的点**另开一条泳道**
        #   （`al.LANE_DP3` → 「ADO·三押轨」）。角色仍是 `dp` ⇒ 收回/对账/`adbRole`
        #   一个字节都不用改；只是 BDG 里终于**看得见**三押了。
        press = [int(x) for x in (getattr(self, "dp_press_list", None) or [])]
        out = []
        for j, i in enumerate(idx):
            if 0 <= i < len(self.onsets):
                o = self.onsets[i]
                p = press[j] if j < len(press) else 2
                out.append({"t_ms": float(o.t_ms), "role": al.ROLE_DP, "idx": i,
                            "lane": al.LANE_DP3 if p >= 3 else ("%s:" % al.ROLE_DP),
                            "press": int(p),
                            "src_tracks": list(getattr(o, "src_tracks", ()) or ())})
        return out

    def bridge_anchor(self, st) -> tuple:
        """★ 交给 BDG 的**时序锚** `(base_bpm, offset_ms)`（`docs/41` #2）。

        ★ 为什么用**我们谱的**：用户实测他那边填的 `baseBpm=120 / offsetMs=2101`
          是**他自己手写的粗略对音**（原话），不是权威值。而我们导出的 `.adofai`
          里的 `settings.bpm / settings.offset` 才是真正定义这首歌时序的那两个数。

        ★ 为什么 `base_bpm` 取**谱面 BPM**（不是最低档）：我们变速的档位是
          `solve.SPEED_POW2` 的倍数，`谱面BPM / 档位` 多半是整数 ⇒ 所有点都落在
          **整拍**上，宿主那边连细分都不用调。实测（`tools/_subdiv_check.py`）：
          FallenEra 那份 `settings.bpm=360` + 档位 {180,360} ⇒ **1/1 就精确 0.000ms**。
          取最低档反而要 1/2。详见 `docs/41` §3.5 的表。
        """
        if self.chart is None:
            return 0.0, 0.0
        bp = float(getattr(self.chart, "base_bpm", 0.0) or 0.0)
        # ★ 直拟合 + 去噪时（`docs/44` §结论）：锚必须是**格相位**，不是 `.adofai`
        #   的 offset —— 宿主那边 `time = offsetMs + beat·(60000/baseBpm)`，
        #   只有把 offsetMs 设成我们的格相位 φ，`beatOfTime(点)` 才**恰好**是 `k/div`
        #   （整数格）⇒ 用户那边顶栏的分母一调就对齐，不会被吸附挪。
        if (self.fit_mode == FIT_DIRECT and self.denoise_meta
                and bool(st.get("anchor_from_grid", True))):
            ph = self.denoise_meta.get("phase_ms")
            if ph is not None and bp > 0:
                self.warnings.append(
                    "桥接锚用**格相位** φ={:.3f}ms（不是 .adofai 的 offset={:.3f}ms，"
                    "差 {:.3f}ms —— 两者口径不同，格相位才能让点落在 k/div 上）"
                    .format(float(ph), float(st.get("offset", 0.0) or 0.0),
                            float(ph) - float(st.get("offset", 0.0) or 0.0)))
                return round(bp, 6), round(float(ph), 6)
        # offset：与导出 `.adofai` 用同一个数（`writer` 那边取的也是这个）
        om = float(st.get("offset", 0.0) or 0.0)
        if bp <= 0:
            return 0.0, 0.0
        return round(bp, 6), round(om, 6)

    def ensure_engine(self) -> bool:
        """★ 没有源文件、但手上有采音点时，造一个**只带时轴**的引擎（`docs/45` §7）。

        为什么需要它：用户可以**只在 BDG 里**开工（自己摆点 / 用插件的「导入时间戳」
        丢一份时间戳进去），改完按「返回数据到谱面生成器」回来 —— 这时我们这边
        **一份 MIDI/OGG 都没有**，而 `rebuild` 的全部下游（`ppqn` / `bpm0` /
        `length_ms`）都要一个 `midi`。

        造出来的东西：一条空的轨 + `bpm0` = 去噪估的砖长（估不出来就用 onsets 的
        众数间隔，再不行 120）+ `length_ms` = 最后一个点 + 4 拍。
        ★ 没有音频 ⇒ 预览没声音，但**谱面/时序照常算**（这一点会在状态栏说明）。
        """
        if self.midi is not None:
            return True
        src = list(self.onsets_override or self.onsets or [])
        if len(src) < 2:
            return False
        ts = [float(o.t_ms) for o in src]
        try:
            mf = ts_mod.to_midi_like(ts, name="（仅时间轴）")
        except Exception as exc:                                # noqa: BLE001
            self.warnings.append(f"造「仅时间轴」引擎失败（{exc}）")
            return False
        self.midi = mf
        self.midi_path = ""
        self.ts_meta = {"n": len(ts), "n_lines": 0, "n_kept": len(ts),
                        "n_skipped": 0, "n_dup": 0, "src": "（仅时间轴）",
                        "ms_lo": ts[0], "ms_hi": ts[-1]}
        self.ts_plan = getattr(mf, "ts_plan", None)
        self.load_info = self.track_map()
        self.warnings.append(
            "★ 没有源文件 ⇒ 已造「仅时间轴」引擎（bpm {:.3f}）：谱面与时序照常算，"
            "但**没有音频可预览**、也没有 MIDI 轨可采".format(mf.bpm0))
        return True

    # ================================================ 从 BDG 收回轨道项目
    def restore_tracks(self, payload: dict) -> dict:
        """★ 把 BDG 里的**带时值数据的轨道**收回成我们的「音轨项目」（`docs/45`）。

        用户口径（2026-10）：「我需要工程可以回到我们的工具里面 …… 此时我们的工具
        使用的音轨**就是** BDG 里面带时值数据的音轨」。

        ⇒ 收回来的每一**条轨**在我们的界面里就是一条音轨：

          · `role=main/sub` 的点 → 我们的 onset（时间用宿主算好的 `ms`）；
          · `role=dp` 的点     → 双押轨（单独一条泳道，`dp_back`）；
          · `role=off` 的点    → 不收（那是「这段什么都不采」的指令轨）。

        ★ 三处「不许静默」：
          1. 撞格/重复点：同一时刻两条泳道都有点 ⇒ 去重并报数；
          2. 锚不一致：宿主那边若改过 `baseBpm/offsetMs`，它的 `timeMs` 就和
             「拍位×它的锚」对不上 —— 报出最大差值（这就是「对不上音」的根子）；
          3. 没有加载文件时：轨道能收、但重建要 MIDI 才能跑 ⇒ 明说。
        """
        tracks = [t for t in (payload.get("tracks") or []) if isinstance(t, dict)]
        anchor = payload.get("anchor") or {}
        bp_h = float(anchor.get("baseBpm") or 0.0)
        off_h = float(anchor.get("offsetMs") or 0.0)
        lanes: list[dict] = []
        ons: list = []
        dp: list[dict] = []
        n_pts = n_added = n_skipped = 0
        worst_anchor_ms = 0.0
        for t in tracks:
            role = str(t.get("role") or al.ROLE_MAIN)
            src = t.get("src_track")
            src = int(src) if src is not None else None
            pts = []
            for p in (t.get("points") or []):
                if not isinstance(p, dict):
                    continue
                ms = p.get("ms")
                if ms is None:
                    n_skipped += 1
                    continue
                ms = float(ms)
                idx = p.get("idx")
                idx = int(idx) if idx is not None else None
                if idx is None:
                    n_added += 1
                beat = float(p.get("beat") or 0.0)
                tr = tuple(int(x) for x in (p.get(al.K_SRC_TRACKS)
                                            or p.get(al.K_POINTS) or ())
                           if x is not None)
                if not tr and src is not None:
                    tr = (src,)
                if bp_h > 0:
                    want = off_h + beat * 60000.0 / bp_h
                    worst_anchor_ms = max(worst_anchor_ms, abs(want - ms))
                pts.append({"idx": idx, "beat": beat, "ms": ms, "src_tracks": tr})
            pts.sort(key=lambda q: q["ms"])
            lanes.append({"name": str(t.get("name") or ""), "role": role,
                          "src_track": src, "lane": str(t.get("lane") or ""),
                          "n": len(pts), "points": pts,
                          "ms_lo": round(pts[0]["ms"], 3) if pts else 0.0,
                          "ms_hi": round(pts[-1]["ms"], 3) if pts else 0.0})
            n_pts += len(pts)

        # ---- 非 dp 的点并成 onset 序列（按 ms 去重；跨泳道同刻 = 一次按键）----
        flat = []
        for li, ln in enumerate(lanes):
            if ln["role"] in (al.ROLE_OFF, al.ROLE_DP):
                continue
            for p in ln["points"]:
                flat.append((p["ms"], li, p))
        flat.sort(key=lambda q: (q[0], q[1]))
        n_dup = 0
        prev_ms = None
        for ms, li, p in flat:
            if prev_ms is not None and abs(ms - prev_ms) < 1.0:
                n_dup += 1
                continue
            prev_ms = ms
            ons.append(onsets_mod.Onset(
                t_ms=float(ms), velocity=100, pitch=60, n_merged=1,
                pitches=(60,), src_tracks=tuple(int(x) for x in p["src_tracks"]),
                tick=int(round(ms * 960 * 120.0 / 60000.0))))
        for ln in lanes:
            if ln["role"] != al.ROLE_DP:
                continue
            for p in ln["points"]:
                dp.append({"t_ms": float(p["ms"]), "role": al.ROLE_DP,
                           "idx": p["idx"], "src_tracks": list(p["src_tracks"])})

        self.lanes_back = lanes
        self.dp_back = dp
        self.onsets_override = ons or None
        if ons:
            self.onsets = list(ons)
        self.back_meta = {
            "run": str(payload.get("run") or ""),
            "anchor_peer": {"base_bpm": bp_h, "offset_ms": off_h},
            "n_lanes": len(lanes), "n_points": n_pts, "n_onsets": len(ons),
            "n_dp": len(dp), "n_added": n_added, "n_dup": n_dup,
            "n_skipped": n_skipped,
            "anchor_mismatch_ms": round(worst_anchor_ms, 4),
            "has_file": self.midi is not None,
            "lanes": [{"name": l["name"], "role": l["role"],
                       "src_track": l["src_track"], "n": l["n"],
                       "ms_lo": l["ms_lo"], "ms_hi": l["ms_hi"]} for l in lanes],
        }
        if self.load_info:
            self.load_info.update(self.track_map())
        if not self.midi:
            # ★ 收回了轨道但一份源文件都没有 ⇒ **造一个「仅时间轴」引擎**
            #   （`docs/45` §7）：用户可能只在 BDG 里开工，回来时我们这边什么都没有。
            if not self.ensure_engine():
                self.warnings.append(
                    "★ 轨道收回来了（{} 轨 / {} 点），但**还没加载 MIDI/OGG**、"
                    "也不够造时轴 ⇒ 现在重建不了。".format(len(lanes), n_pts))
        if worst_anchor_ms > 1.0:
            self.warnings.append(
                "★ 宿主锚与我们不一致：它的 timeMs 和「拍位×它的锚」最大差 "
                "{:.1f}ms ⇒ 那边可能改过 baseBpm/offsetMs（这会让音整体错位）"
                .format(worst_anchor_ms))
        if n_dup:
            self.warnings.append(
                "收回时有 {} 个点与别的泳道同刻（<1ms）⇒ 合成一次按键（已报数）"
                .format(n_dup))
        return {"ok": True, "meta": self.back_meta,
                "info": (self.track_map() if self.midi is not None else None),
                "text": self.back_text()}

    def back_text(self) -> str:
        m = self.back_meta or {}
        if not m:
            return "[收回] 还没有从 BDG 收回任何轨道"
        out = ["[收回] {} 轨 / {} 点 → onset {} · 双押 {}".format(
            m.get("n_lanes"), m.get("n_points"), m.get("n_onsets"), m.get("n_dp"))]
        if m.get("n_added"):
            out.append("    其中 {} 点是你新加的（没有我们的对账标记）".format(m["n_added"]))
        if m.get("n_dup"):
            out.append("    同刻合并 {} 点".format(m["n_dup"]))
        if m.get("anchor_mismatch_ms", 0) > 1.0:
            out.append("    ★ 锚不一致：最大 {:.1f}ms".format(m["anchor_mismatch_ms"]))
        for l in (m.get("lanes") or [])[:12]:
            out.append("    · {:22s} {:<5s} {:>4d} 点  {:.1f}~{:.1f}ms".format(
                (l["name"] or "?")[:22], l["role"], l["n"],
                l["ms_lo"], l["ms_hi"]))
        return "\n".join(out)

    def clear_back(self) -> dict:
        """清掉收回的轨道项目（回到「从选轨采音」）。"""
        self.lanes_back = []
        self.dp_back = []
        self.back_meta = {}
        self.onsets_override = None
        if self.load_info:
            self.load_info.update(self.track_map())
        return {"ok": True, "info": (self.track_map() if self.midi is not None else None)}

    # ================================================================ 求解
    # ================================================================ ③b 采bpm
    def xk_lines(self, st, ns=None) -> dict:
        """按 tbpm 建骨架（每个 N 一条）。`ns` 省略就用全局 N。"""
        tb = bigline_mod.round_tbpm(st.get("xk_tbpm") or 0.0)
        ph = float(st.get("offset", 0.0) or 0.0)
        out = {}
        for n in (ns if ns is not None else [int(st.get("xk_base") or 0)]):
            if int(n):
                out[int(n)] = bigline_mod.BigLine(tb, int(n), phase_ms=ph)
        return out

    def _xk_apply(self, st) -> dict:
        """③b 采bpm：把**区间内**的真实 onset 换成骨架砖（定稿 = `docs/47` §3）。

        * 关着（N=0 且没框区间）⇒ **一行都不执行**，老路径逐字节不变；
        * 有区间 ⇒ **只采区间**（段外走原路径）；没区间 ⇒ **全曲采bpm**；
        * 出错（重叠 / N 非法 / tbpm 没填 / 起止不是数）⇒ **整组不生效**并上屏，**不静默降级**。
        """
        self.xk_meta, self.xk_cbpm = {}, 0.0
        n_glob = int(st.get("xk_base") or 0)
        raw = list(st.get("xk_ranges") or [])
        if not n_glob and not raw:
            return self.xk_meta
        try:
            tb = bigline_mod.round_tbpm(st.get("xk_tbpm") or 0.0)
        except bigline_mod.XkError as exc:
            self.warnings.append(
                "采bpm **整组不生效**（走原路径）：tbpm {}。"
                "tbpm 用那个测速站的结果，会四舍六入五成双取整".format(exc))
            return self.xk_meta
        # ① 每段的 N（没写就继承全局 N）—— N 决定骨架，必须先定下来
        cand, picked = {n_glob}, []
        for i, item in enumerate(raw):
            if not isinstance(item, dict):
                self.warnings.append(
                    "采bpm **整组不生效**：第 {} 段区间不是字典".format(i + 1))
                return self.xk_meta
            n = int(item.get("xk_base") or item.get("n") or 0) or n_glob
            if not n:
                self.warnings.append(
                    "采bpm **整组不生效**：第 {} 段没写 N，全局也没选 N".format(i + 1))
                return self.xk_meta
            picked.append(n)
            cand.add(n)
        # ② 骨架（候选 N 各一条）
        try:
            lines = self.xk_lines(st, ns=sorted(x for x in cand if x))
        except bigline_mod.XkError as exc:
            self.warnings.append("采bpm **整组不生效**：{}".format(exc))
            return self.xk_meta
        # ③ 起止点：**可以写格子号（1 起算）也可以写毫秒**（`docs/47` §3）
        fixed = []
        for item, n in zip(raw, picked):
            rec, bad = {}, None
            for side in ("start", "end"):
                tk = item.get(side + "_tile")
                if tk is not None and tk != "":
                    try:
                        rec[side + "_ms"] = bigline_mod.parse_point(tk, lines[n], "tile")
                    except bigline_mod.XkError as exc:
                        bad = exc
                        break
                else:
                    rec[side + "_ms"] = item.get(side + "_ms", item.get(side))
            if bad is not None:
                self.warnings.append("采bpm **整组不生效**：{}".format(bad))
                return self.xk_meta
            try:
                float(rec["start_ms"]), float(rec["end_ms"])
            except (TypeError, ValueError):
                self.warnings.append(
                    "采bpm **整组不生效**：区间的起止不是数（{!r} / {!r}）".format(
                        rec.get("start_ms"), rec.get("end_ms")))
                return self.xk_meta
            fixed.append({"start_ms": rec["start_ms"], "end_ms": rec["end_ms"],
                          "xk_base": n, "tracks": item.get("tracks") or [],
                          "label": str(item.get("label") or "")})
        # ④ 没框区间 ⇒ **全曲采bpm**（用户口径：很多时候并不需要全曲 ⇒ 框了就只采框内）
        if not fixed:
            # ★★ 全曲的右端必须是**曲子长度**，不是「现有 onset 的范围」：
            #   采bpm 开 + ② 主轨不勾（**这是合法用法**，主轨本来就失效）⇒
            #   `self.onsets` 此时是空的 ⇒ 用 onset 范围会得到**空区间** ⇒ 不注入
            #   ⇒ 最后报「采音点少于 2 个」。用户真机就踩在这上面。
            t1 = float(getattr(self.midi, "length_ms", 0.0) or 0.0)
            if t1 <= 0:
                t1 = max((o.t_ms for o in self.onsets), default=0.0)
            if t1 <= 0:
                self.warnings.append(
                    "采bpm **整组不生效**：算不出「全曲」有多长"
                    "（没有音频长度，也没有任何 onset）")
                return self.xk_meta
            t0 = 0.0
            fixed = [{"start_ms": t0, "end_ms": t1, "xk_base": n_glob,
                      "tracks": [], "label": "全曲"}]
            self.warnings.append(
                "采bpm：**没框区间 ⇒ 全曲采bpm**（{:.0f}~{:.0f}ms ＝ 整首曲子）；"
                "只想采某一段就在 ③b 里框区间".format(t0, t1, t1))
        # ⑤ 注入
        try:
            rs = xkbase_mod.normalize_ranges(fixed)
            self.onsets, meta = xkbase_mod.inject(self.onsets, lines, rs)
        except bigline_mod.XkError as exc:
            self.warnings.append("采bpm **整组不生效**：{}".format(exc))
            return self.xk_meta
        self.xk_cbpm = float(tb) * float(n_glob or max(picked or [0]) or 0)
        self.xk_meta = meta.to_dict()
        self.xk_meta["tbpm"] = tb
        self.xk_meta["base_n"] = n_glob or max(picked or [0]) or 0
        # ★ `report_text()` 自己就以「采bpm：」开头 ⇒ **别再套一层前缀**（否则会「采bpm：采bpm：」）
        self.warnings.append(meta.report_text())
        return self.xk_meta

    def rebuild(self, st) -> dict:
        """旧 UI `rebuild()` `:890-1068`（步骤号与 docs/18 §2.3 一致）。"""
        # ★ 载入时那本账（原曲找不到 / 某路空 / 未排序…）**一直在**，见 `__init__`
        self.warnings = list(self.load_warnings)
        # ★ ③b 报告**每次重建都重置** —— 失败时绝不能留上一轮的陈旧报告
        #   （用户实测踩过：重建被拒，界面还在显示上一次的「注入骨架 964 块砖」）
        self.xk_meta, self.xk_cbpm, self.xk_on = {}, 0.0, False
        if self.midi is None and not self.ensure_engine():
            return {"ok": False,
                    "msg": "先加载一个 MIDI / 音频（或一份毫秒时间戳文件；"
                           "也可以先在 BDG 里做好再用「返回数据到谱面生成器」）"}
        d = self.derive(st)
        used = d["selected"]
        segs = self.norm_segments(st)
        if self.lanes_back and (segs or self.norm_regions(st)):
            self.warnings.append(
                "★ 有「BDG 收回的轨道项目」时区间/分段**整个忽略** —— "
                "音轨就是收回来的那些（要回去用它们就把收回清空）")
        # ★★ ③b 采bpm 开着时，② 主轨**本来就该失效**（定稿 `docs/47` §1 第 5 条）
        #   ⇒ **一条都不勾是正确用法**，不能再按「没有可采音的音轨」拒算。
        #   用户真机踩过：不勾主轨 + 开采bpm ⇒ 每次重建都被拒、只保留上一张谱面，
        #   表现就是「重新生成 / 进度条像死了一样」，而且**双押永远走不到**。
        self.xk_on = bool(int(st.get("xk_base") or 0) or (st.get("xk_ranges") or []))
        if not used and not self.lanes_back and not self.xk_on:   # 步骤 2
            # 允许「全局一轨都不勾、只用区间/分段」的用法
            if not segs and not self.norm_regions(st):
                # ★★ 2026-10「不许静默」：**最常见的真因**是「勾成多押的那条轨 == 主轨」
                #   —— 多押轨按口径**不参与主轨并集**（`_selected` 会剔除它），
                #   于是看上去勾了轨，实际一条可采的都没有。
                #   旧文案只说「勾选一下」，用户会反复勾同一根轨，永远修不好。
                _dpc = [int(x) for x in (st.get("dp_checked") or [])]
                _tc = [int(x) for x in (st.get("tracks_checked") or [])]
                _overlap = [x for x in _tc if x in _dpc]
                if _overlap:
                    self.warnings.append(
                        "★ 轨 {} **同时被勾成「主轨」和「多押轨」** —— 多押轨按口径"
                        "不参与主轨并集，所以现在一条可采音的轨都没有。"
                        "把主轨换成别的轨，或取消那几条的多押勾选".format(_overlap))
                # ★ 与旧 UI 一致：**不清空 chart**（中断语义，风险 19）
                return {"ok": False,
                        "msg": ("没有可采音的音轨（② 里勾选一下，或换成「多轨」）"
                                + (f"；⚠ 轨 {_overlap} 既是主轨又是多押轨"
                                   f"（多押轨不参与主轨并集）" if _overlap else "")),
                        "derive": d, "stale": self.chart is not None,
                        "warning_list": list(self.warnings),
                        "xk": dict(self.xk_meta or {})}
        if not used and not self.lanes_back and self.xk_on:
            self.warnings.append(
                "采bpm 开着：② 主轨**不参与采音**（骨架取代它）—— "
                "不勾主轨是对的；这一段的内容只来自③b 的骨架 + 多押轨")

        trs = self.midi.tracks
        p_on = self.params_onset(st)                            # 步骤 3

        if self.lanes_back:
            # ★ 轨道项目模式（`docs/45`）：音轨 = 从 BDG 收回来的那些，
            #   **不再从 MIDI 采样** —— 时间直接用宿主算好的 `ms`（带时值数据）。
            self.onsets = list(self.onsets_override or [])
            used = []
        elif not used and not segs:
            # 全局一条都不勾，但框了区间 ⇒ 只从区间采（允许这种用法）
            self.onsets = []
        elif getattr(self, "fill_mode", False) and getattr(self, "sub_tracks", []) \
                and used:
            # ★ 步骤 4（主次级）：主轨全采 → 次级轨**只插空**（用户 2026-10）。
            #   复用 `core.onsets.notes_fill_gaps` —— 它本来就是为这件事写的
            #   （「主轨为主，只在主轨出现 > gap_ms 空白的地方把其它轨的音补进来」），
            #   只是当年主轨改成多选之后被退役了。现在按「权重」重新启用。
            prim_notes = [n for i in used for n in trs[i].notes]
            sub_notes = [n for i in self.sub_tracks for n in trs[i].notes]
            gap = float(st.get("sub_gap_ms", 600.0))
            self.onsets = onsets_mod.build_onsets(
                onsets_mod.notes_fill_gaps(prim_notes, sub_notes, gap), p_on)
        else:                                                   # 步骤 4：主轨并集
            self.onsets = onsets_mod.build_onsets_multi(
                [trs[i] for i in used], p_on)

        # 步骤 4b：★ 分段采音（`docs/34` 方案 C）——**有分段时它取代下面两步**
        #   理由：分段的第一片本来就是「全局」，不需要在它之外再拼一次；
        #   而且它的边界语义是「先切割、后聚类」，与区间的「先聚类、后切割」
        #   不兼容（实测老做法 2.8% 的输入会吃音/多音，见 `core/segments.py`）。
        self.segment_meta = []
        if segs and not self.lanes_back:
            if self.norm_regions(st):
                self.warnings.append(
                    "★ 区间与分段同时存在：**分段优先，区间已整个忽略**"
                    "（要保留区间就把分段清空）")
            self.onsets = self.apply_segments(st, segs, p_on)
        else:
            # 步骤 4b'：区间覆盖（框选某段改用别的音轨采音）
            self.onsets = self.apply_regions(st, self.onsets, p_on)

        # 步骤 4c：★ 编辑器收回的进度**优先**（docs/38 §9.5）
        #   用户在 BDG 里改过（删/挪/加）之后，「收回」就把结果钉在这儿，
        #   再 rebuild 用的就是它 —— 直到换文件或用户点掉。
        if getattr(self, "onsets_override", None) is not None:
            self.onsets = list(self.onsets_override)

        # 步骤 4d：★ ③b 采bpm（`docs/47`）—— 区间内的真实 onset 换成**骨架砖**。
        #   关着时这个方法**第一行就返回** ⇒ 老路径逐字节不变。
        self._xk_apply(st)

        # 步骤 4d′：★ **补格**（`docs/71` §9 第 3 条手段 · `core/tilefill.py`）。
        #   用户 2026-10：「你可以使用**分段采音设置采bpm**，或者通过**双押轨道**
        #   （用分段采音辅助的）进一步加强」。
        #   为什么需要：从 MIDI 还原**人写的**谱面时，「一条 onset 一格」会明显偏疏
        #   （实测参考 2543 键 vs 我们 1742 键）—— 人打谱会在够长的间隔里按 bpm 网格
        #   再铺几格。这里与 `xkbase` 同一套架构：**注入合成 onset，不改 `solve()`**。
        #   ★ `fill_div=0`（默认）⇒ **一行都不执行**，老路径逐字节不变（`docs/25` §5.3）。
        self.fill_meta = {}
        if int(st.get("fill_div") or 0) > 0 and len(self.onsets) >= 2:
            try:
                self.onsets, _fm = fill_mod.fill(
                    self.onsets,
                    div=int(st.get("fill_div") or 0),
                    min_steps=int(st.get("fill_min_steps") or 3),
                    max_per_gap=int(st.get("fill_max_per_gap") or 0),
                    min_merged=int(st.get("fill_min_merged") or 1),
                    per_chord=bool(st.get("fill_per_chord")),
                    max_add=int(st.get("fill_max_add") or 0),
                )
                self.fill_meta = _fm.to_dict()
            except Exception as _fe:                             # noqa: BLE001
                # ★ 不静默：抛了就说清楚，并且**一格都不补**（不猜一个网格硬上）
                self.warnings.append(
                    "补格 **整组不生效**（走原路径）：{}: {}".format(
                        type(_fe).__name__, _fe))
                self.fill_meta = {}

        merged = [n for i in used for n in trs[i].notes]         # 步骤 5
        for i in (getattr(self, "sub_tracks", None) or []):      # 次级轨也进卷帘
            merged.extend(trs[i].notes)
        for rg in (self.region_meta or []):                      # 区间音轨也进卷帘
            for i in rg["tracks"]:
                merged.extend(trs[i].notes)
        for r in (self.segment_meta or []):                      # 分段音轨也进卷帘
            for k in (seg_mod.K_MAIN, seg_mod.K_SUB, seg_mod.K_DP):
                for i in r[k]:
                    merged.extend(trs[i].notes)
        if len(self.onsets) < 2:
            # ★ **失败也要把警告带走**：否则用户看不到「采bpm 整组不生效：砖数超上限」
            #   这类真正的原因，只看到一句「采音点少于 2 个」——这就是静默。
            return {"ok": False,
                    "msg": "采音点少于 2 个，无法成谱。放宽过滤条件试试。",
                    "derive": d, "stale": self.chart is not None,
                    "warning_list": list(self.warnings),
                    "xk": dict(self.xk_meta or {})}

        # ------------------------------------------- 步骤 5b 双押落点（提到求解**之前**）
        # ★ 为什么提前（`docs/31` §5.2 的 a）：角度双押要「把哪些 onset 出双押」
        #   当**约束**交给 `solve()` 预留槽位，所以落点必须在 solve 之前算好。
        #   这里只用 onset 下标，不依赖谱面（`pre` 要等 solve 完才有）。
        dp_tis = d["dp_tracks"]
        # ★★ `docs/48` §6.1 的前置坑：**轨号必须留着** —— 三押的判据是
        #   「同一时刻有 2 条多押轨都有音」，丢掉轨号就数不出「同时有几条轨」。
        dp_tt: list[tuple[float, int]] = []
        dp_hit_idx: list[int] = []
        dp_skipped = 0
        # ★ 分段显式改过双押轨时才按片采 —— 否则逐字节走老路径（不回归）
        dp_span = bool(segs) and seg_mod.any_override(segs, seg_mod.ROLE_DP)
        dp_empty: list[int] = []      # 勾了双押、却被 ①采音过滤滤成 0 个音的轨
        if dp_span:
            dp_tt = seg_mod.dp_marks(trs, segs, self.segment_mode,
                                     float(self.midi.length_ms), dp_tis,
                                     self.params_onset(st), per_span=True)
        elif dp_tis:
            _po = self.params_onset(st)
            for i in dp_tis:
                _o = onsets_mod.build_onsets(trs[i].notes, _po)
                if not _o:
                    dp_empty.append(int(i))
                for o in _o:
                    dp_tt.append((o.t_ms, int(i)))
            dp_tt.sort()
        dp_t: list[float] = [t for (t, _tr) in dp_tt]
        if dp_tis or dp_span:
            tol = float(st.get("dp_tol", 45.0))
            if self.xk_on and dp_t:
                # ★★ 采bpm 骨架下改成**一对一**：一个双押音只认**离它最近的那一块砖**。
                #   老判据是「**砖 → 找音**」（每块砖各自在 ±tol 里找音），砖比 `2×tol`
                #   还密时**一个双押音会圈中好几块砖** ⇒ 扇出：
                #   实测 cbpm 1440（砖长 41.7ms）双押轨 146 个音 ⇒ 被标成 **383** 块砖；
                #   cbpm 2880（20.8ms）⇒ **638** 块 —— 用户看到的就是「双押一堆音」。
                _hit, _ = xkbase_mod.dp_marks(self.onsets, dp_t, tol_ms=tol)
                dp_hit_idx = list(_hit)
                dp_skipped = max(0, len(self.onsets) - len(dp_hit_idx))
            else:
                _j = 0
                for k, T in enumerate(o.t_ms for o in self.onsets):
                    while _j < len(dp_t) and dp_t[_j] < T - tol:
                        _j += 1
                    jj, hit = _j, False
                    while jj < len(dp_t) and dp_t[jj] <= T + tol:
                        hit = True
                        jj += 1
                    if hit:
                        dp_hit_idx.append(k)
                    else:
                        dp_skipped += 1

        # ★★ 2026-10「不许静默」：勾了双押轨、却**一处双押都没有** ⇒ 必须说清是哪一种。
        #   用户报的原话是「双押轨因为未知原因不见了，三押轨也是」—— 真机复现：
        #   ①采音的「最小力度 127」+ 音高 42..76 把三条双押轨**整条滤成 0 个音**
        #   ⇒ `dp_tt` 空 ⇒ `dp_onsets=0` ⇒ 双押 0 ⇒ 空泳道根本不往 BDG 投，
        #   界面上只看到一个刺眼的「双押 0」，**一句原因都不给**。放开过滤立刻恢复
        #   （同一套轨：双押 178 处 / 三押 67 处）。这是第二种「看起来像 bug」的静默。
        if (dp_tis or dp_span) and not dp_hit_idx:
            _f = self.params_onset(st)
            _tr_s = "[" + ", ".join("trk%d" % i for i in (dp_tis or [])) + "]"
            if dp_empty:
                self.warnings.append(
                    "★★ 勾了双押轨 {}，但 ①采音过滤把它们**滤成 0 个音** ⇒ 一处双押都"
                    "插不进去（当前 最小力度={} / 音高={}~{} / 最小间隔={}ms）。"
                    "放宽这几项，或换一条真的同时响的轨".format(
                        "[" + ", ".join("trk%d" % i for i in dp_empty) + "]",
                        _f.min_velocity, _f.pitch_lo, _f.pitch_hi,
                        _f.min_interval_ms))
            elif dp_t:
                self.warnings.append(
                    "★★ 双押轨 {} 一共 {} 个音，但**没有一个**落在主轨落点的 "
                    "±{:.0f}ms 内 ⇒ 双押 0。把「双押容差」调大，或换一条真的与"
                    "主轨同时响的轨".format(_tr_s, len(dp_t),
                                          float(st.get("dp_tol", 45.0))))

        # ★★ 三押（`docs/48` §3）：**押数 = 1 + 同时有音的 dp 轨数**。
        #   在 onset 轴上先把「同时」的 dp 音按轨聚类，再把每组落到对应的落点上。
        #   `dp_press[i]` 与 `dp_hit_idx[i]` **平行**（1/2 = 单双押，3 = 三押，≥4 = 四押+）。
        dp_press: list[int] = [1] * len(dp_hit_idx)
        dp_group_lost = 0
        if dp_hit_idx:
            _grp = dp_angle.group_marks(dp_tt, tol)
            dp_press, dp_group_lost = dp_angle.press_of(
                [self.onsets[k].t_ms for k in dp_hit_idx], _grp, tol)
        n_three = sum(1 for p in dp_press if int(p) == 3)
        n_extra = sum(1 for p in dp_press if int(p) >= 4)
        self.dp_press_n = {"three": n_three, "extra": n_extra,
                           "group_lost": dp_group_lost}

        # ★ 步骤 5b'：采bpm 开着时，**把双押落砖的情况报出来**（用户口径：不许静默）。
        #   `dp_hit_idx` 是「哪些**砖**上有双押」；这里反过来算「多押轨的标记里有几个
        #   **落不到砖上**」—— 落不上多半是 tbpm/N 跟音乐对不上（砖长 vs 容差）。
        if self.xk_cbpm > 0 and dp_t and (dp_tis or dp_span):
            _tol = float(st.get("dp_tol", 45.0))
            _hits, _miss = xkbase_mod.dp_marks(self.onsets, dp_t, tol_ms=_tol)
            _per = float((self.xk_meta or {}).get("period_ms") or 0.0)
            self.xk_meta["dp_marks"] = len(dp_t)
            self.xk_meta["dp_on_tiles"] = len(_hits)
            self.xk_meta["dp_missed"] = _miss
            self.xk_meta["dp_tol_ms"] = _tol
            if _miss:
                self.warnings.append(
                    "采bpm：多押轨 {} 个音里**只有 {} 个落在骨架砖上**（落不上 {} 个，"
                    "容差 ±{:.0f}ms）—— 落不上多半说明 tbpm/N 跟音乐对不上".format(
                        len(dp_t), len(_hits), _miss, _tol))
            else:
                self.warnings.append(
                    "采bpm：多押轨 {} 个音 ⇒ **一对一**标成 {} 块砖（砖长 {:.3f}ms；"
                    "已关掉「一块砖圈多个音」的扇出）".format(
                        len(dp_t), len(_hits), _per))

        # ★ a：角度双押 + 有落点 ⇒ 让 solve() 预留槽位（空元组 = 老路径逐字节不变）
        reserve: tuple = ()
        if (int(st.get("dp_mode", 0)) == 1 and dp_hit_idx
                and bool(st.get("dp_reserve", True))):
            reserve = tuple(dp_hit_idx)

        p_sv, sv_info = self.params_solve(st, self.onsets,   # 步骤 6
                                          dp_reserve=reserve)
        # ★ 步骤 6a：采bpm **钉死基准 BPM**（`docs/47` §2.4）
        #   `r = Δt / (60000/base_bpm)`；钉到 cbpm 后，骨架砖的 Δt = 砖长 ⇒ **r ≡ 1.0**
        #   ⇒ `travel = 180·r/k` 取 `k=1` 就是 180° 直线、时长逐位精确。
        if self.xk_cbpm > 0:
            _old = float(p_sv.base_bpm or 0.0)
            if abs(_old - self.xk_cbpm) > 1e-9:
                self.warnings.append(
                    "采bpm：基准 BPM **钉死到 cbpm {:.0f}**（原来是 {}）——"
                    " 不钉死就铺不出等间隔骨架".format(
                        self.xk_cbpm,
                        "自动（求解器自己挑八度）" if _old <= 0
                        else "{:.2f}".format(_old)))
            p_sv.base_bpm = float(self.xk_cbpm)
            if getattr(p_sv, "snap_base_bpm", False):
                p_sv.snap_base_bpm = False
                self.warnings.append(
                    "采bpm：已**关掉**「基准 BPM 吸附到整数」—— 它会挪砖长、"
                    "破坏「绝对对拍」")
        nf_before = 0
        # ★ 步骤 6b/7：求解方式（`docs/44`）—— 直拟合时先去噪再一砖一音铺
        self.fit_mode = str(st.get("fit_mode") or FIT_SOLVE)
        # ★★ 2026-10 修的 bug（用户口径「AI 的 MIDI 用激进策略跑不通」）：
        #   「④ 求解 · 使用激进的**采音**策略」(`aggressive_pick`) 是
        #   `core.ladder` 那条路，**只有最优化会走**（`solve()` 里那行早退）。
        #   直拟合下勾了它 = 勾了个寂寞 —— 以前**一个字都不说**（静态静默）。
        #   现在明说，并且把「要它就切回最优化」讲清楚。
        if (self.fit_mode == FIT_DIRECT
                and bool(st.get("aggressive_pick", False))):
            self.warnings.append(
                "★ 「使用激进的采音策略」只对**最优化**有效 ⇒ 这次（直拟合）**没"
                "用上**。要它就先把「求解方式」切回「最优化」；"
                "直拟合下的角度收敛请用「使用激进的拟合策略」+「拟合容差」")
        # ★★ 2026-10：最大夹角 < 180° 与「直线格 = 180°」是**定义上的冲突** ——
        #   两条路的直线格（以及长休止的 Pause 格）都必然是 180°。
        #   不许静默：照常写出 180° 直线格，但把这件事上屏。
        _tmax = float(st.get("travel_max", 0.0) or 0.0)
        if 0.0 < _tmax < 180.0:
            self.warnings.append(
                "⚠ 最大夹角 {:.0f}° < 180°：**直线格与 Pause 格按定义就是 180°**"
                "（一砖一音/暂停节拍都靠它），这一档满足不了 —— 会照常写出 180° "
                "直线格。要卡上界请给 ≥ 180° 的值".format(_tmax))
        self.denoise_meta = {}
        self.fit_meta = {}
        if self.fit_mode == FIT_DIRECT:
            # ★ 采bpm 开着时：**骨架砖不参与去噪**（它们本来就精确落在格上，
            #   混进去会把「区间外真实 onset」的格判带偏）。去噪只处理真实 onset。
            _syn = [o for o in self.onsets if getattr(o, "synth", False)]
            if _syn:
                self.onsets = [o for o in self.onsets if not getattr(o, "synth", False)]
            self._denoise_onsets(st)
            if _syn:
                self.onsets = sorted(self.onsets + _syn, key=lambda o: o.t_ms)
            # ★ 八度校验（`docs/47` §2.2）：与去噪解出的砖长比，差 2 的幂就**报出来**
            if self.xk_cbpm > 0 and self.denoise_meta.get("period_ms"):
                _oc = bigline_mod.octave_note(
                    int(self.xk_meta.get("tbpm") or 0),
                    int(self.xk_meta.get("base_n") or 0),
                    float(self.denoise_meta["period_ms"]))
                if _oc:
                    self.warnings.append(_oc)
                    self.xk_meta["octave"] = _oc
            if len(self.onsets) < 2:
                # 去噪若把点撞光了，退回最优化（不许静默）
                self.warnings.append("直拟合：去噪后不足 2 个点 ⇒ 退回最优化")
                self.fit_mode = FIT_SOLVE
        if self.fit_mode == FIT_DIRECT:
            _tol, _agg = _fit_state(st)
            try:
                self.chart, frep = fitdirect_mod.build(
                    self.onsets, p_sv,
                    # ★ 实际排 tile 的基准 BPM：优先吃用户填的 bpm_hint
                    #   （<200 自动 x2，与「基准 BPM」框显示一致）；
                    #   无 hint（MIDI/TS 工程）回落去噪估速（原行为，不回归）。
                    base_bpm=(self.xk_cbpm
                              or ((h := float(self.bpm_hint or 0.0)) and (h * 2.0 if h < 200.0 else h))
                              or float(self.denoise_meta.get("bpm") or 0.0)),
                    tier_mode=str(st.get("tier_mode") or "dp"),
                    angle_ladder=_agg, angle_tol_ms=_tol)
                self.fit_meta = frep.to_dict()
                self.warnings.append(
                    "直拟合 {} 层 · 直线 {:.1%} · SetSpeed {} · 发卡弯 {} · "
                    "时序误差 max {:.4f}ms".format(
                        frep.n_floors, frep.straight_frac, frep.n_setspeed,
                        frep.n_hairpin, frep.err_max_ms))
                if frep.ladder_on:
                    # ★ 激进拟合的账（不许静默）：修了几个、挪多少、几个挪不动、
                    #   还有几层不在阶梯上（双押/中旋折返格是机制需要的形状）。
                    self.warnings.append(
                        "★ 15° 阶梯（容差 {:.0f}ms）：修正 {} 音 · 挪动 中位 {:.2f}/"
                        "max {:.2f}ms（合计 {:+.1f}ms）· 挪不动原样 {} 音 · "
                        "非阶梯格 {} 层".format(
                            frep.ladder_tol_ms, frep.n_ladder_moved,
                            frep.ladder_move_median_ms, frep.ladder_move_max_ms,
                            frep.ladder_move_sum_ms, frep.n_ladder_raw,
                            frep.n_ladder_bad))
                    if frep.n_ladder_bad == 0:
                        self.warnings.append(
                            "非双押格的角度**全部**落在 15 30 45 60 75 90……（15° 的整数倍）上")
                    else:
                        self.warnings.append(
                            "⚠ 仍有 {} 层不在 15° 的整数倍上（双押/中旋折返或长休止的 "
                            "Pause 格；要更严就调大「拟合容差」）".format(frep.n_ladder_bad))
                if frep.err_max_ms > 0.05:
                    self.warnings.append(
                        "★ 直拟合本该逐点精确，实测 max {:.3f}ms ⇒ 是 bug，请报"
                        .format(frep.err_max_ms))
                if not self.denoise_meta:
                    self.warnings.append(
                        "直拟合未去噪：travel 会是任意有理数（开了「去噪」才会落整齐格）")
            except Exception as exc:                            # noqa: BLE001
                self.warnings.append(f"直拟合失败（{exc}）⇒ 退回最优化")
                self.fit_mode = FIT_SOLVE
        if self.fit_mode != FIT_DIRECT:
            self.chart = solve_mod.solve(self.onsets, p_sv)      # 步骤 7
        nf_before = len(self.chart.floors)
        cd = int(st.get("countdown_ticks", 4))

        # ---------------------------------------------------- 步骤 8 双押插入
        # ★ 必须在所有派生量之前（风险 7）
        n_dp = 0
        self.dp_info = ""
        # ★ 存下来给「双押单开一条泳道」用（`docs/42`）：这批就是**真的双押点**
        self.dp_hit_idx = [int(x) for x in dp_hit_idx]
        # ★★ 押数（与 `dp_hit_idx` 平行）：2 = 双押，≥3 = 三押/四押。
        #   2026-10：投送时按它把 ≥3 的分到**另一条泳道**（`dp:3` → 「ADO·三押轨」），
        #   用户报「三押轨道不出现」—— 以前三押点和双押点全挤在一条轨里，看不见。
        self.dp_press_list = [int(x) for x in dp_press]
        self.dp_report = {"dp_onsets": len(dp_t), "dp_hits": len(dp_hit_idx),
                          "dp_skipped": dp_skipped}
        if dp_tis and len(self.chart.floors) > 2 and dp_hit_idx:
            lead = 1 if self.chart.meta.get("n_lead") else 0
            _of = self.chart.meta.get("onset_floors") or []
            pre = solve_mod.times_from_chart(self.chart)
            tg = [pre[min((int(_of[k]) if k < len(_of) else k + lead),
                          len(pre) - 1)] for k in dp_hit_idx]
            if tg:
                rep: dict = {}
                if int(st.get("dp_mode", 0)) == 1:
                    _tpm = int(getattr(p_sv, "three_press_mode", 0) or 0)
                    plan = dp_angle.plan(self.chart, tg, report=rep,
                                         travel_min=p_sv.travel_min,
                                         theta=(float(p_sv.dp_theta) or None),
                                         skew_max_ms=float(p_sv.dp_skew_max_ms),
                                         use_fixed=bool(p_sv.use_fixed_dp_angle),
                                         presses=dp_press,
                                         # ★ 用户 2026-10 三档开关（见 schema）
                                         three_press=(_tpm != 1),
                                         skip_press=(_tpm == 2))
                    dp_angle.apply(self.chart, plan,
                                   skew_max_ms=float(p_sv.dp_skew_max_ms))
                    n_dp = len(plan)
                    stt = dp_angle.stats(self.chart)
                    self.dp_info = (f"角度双押 {n_dp} 处（反向 "
                                    f"{stt.get('reverse', 0)} / 正常 "
                                    f"{stt.get('normal', 0)}）")
                    # ★★ 固定角度表：**先说清角度是谁定的**（不许静默）
                    if rep.get("theta_src") == "fixed":
                        _cnt = rep.get("theta_counts") or {}
                        self.dp_info += "，**固定写法** θ " + " / ".join(
                            "{:g}°×{}".format(k, v) for k, v in sorted(_cnt.items()))
                        self.dp_info += ("（按 bpm 窗口查表；**薄角 θ / 偏移预算已忽略**）")
                        if float(p_sv.dp_theta or 0) > 0 or \
                                abs(float(p_sv.dp_skew_max_ms) - 25.0) > 1e-9:
                            self.warnings.append(
                                "★ 已开「使用固定双押角度」⇒ 「薄角 θ」与「偏移预算」"
                                "**不参与选角**（角度由规定写法表定）。要回去用它们就把"
                                "那个开关关掉")
                    # ★ 双押偏移 Δ：固定表下**只当情报**（不是判据）
                    if rep.get("hits"):
                        self.dp_info += (f"，Δ（情报）≤{rep.get('skew_max_ms', 0):.1f}ms"
                                         f"（θ {rep.get('theta_min', 0):g}~"
                                         f"{rep.get('theta_max', 0):g}°）")
                    # ★★ 三押（`docs/48` §6.2 第 7 步）：**处数 / 组合 / 跳过的四押 /
                    #   老口径未拆** —— 一条都不许静默。
                    _tp = stt.get("press_hist") or {}
                    _n3 = int(rep.get("triple_used") or 0)
                    if _n3:
                        self.dp_info += (f"，**三押 {_n3} 处**（押数 "
                                         + " / ".join(f"{k}押×{v}"
                                                      for k, v in sorted(_tp.items())
                                                      if int(k) >= 3)
                                         + "）")
                        self.dp_report["dp_three"] = _n3
                    if rep.get("extra_press"):
                        _ex = int(rep["extra_press"])
                        self.dp_info += (f"；⚠ **跳过四押 {_ex} 处**"
                                         f"（3 条及以上多押轨同时响）")
                        self.warnings.append(
                            "★ {} 处有 3 条及以上的多押轨同时响（四押/5押）——"
                            " 本轮**跳过**（只做双押/三押），那些落点按双押插入".format(_ex))
                        self.dp_report["dp_extra_press"] = _ex
                    if rep.get("three_legacy"):
                        self.dp_info += (f"；⚠ {rep['three_legacy']} 处三押**未拆**"
                                         f"（老口径没有押数概念）")
                        self.warnings.append(
                            "★ 关着「使用固定双押角度」⇒ 老口径没有押数概念：{} 处"
                            "三押**没拆**（保持 `[θ, 余量]` 两格）。要三押就把那个"
                            "开关打开".format(rep["three_legacy"]))
                    # ★★ 用户 2026-10 第 3 项：三押开关的后两档 —— **不许静默**
                    if rep.get("three_off"):
                        _to = int(rep["three_off"])
                        self.dp_info += (f"；三押**已关**（{_to} 处按双押插一格）")
                        self.warnings.append(
                            "★ 「三押」开关选的是**不拆** ⇒ {} 处「2 条多押轨同时响」"
                            "按**双押**插了一格（薄角 1 块）。要三押就把开关调回"
                            "「拆三押」".format(_to))
                        self.dp_report["dp_three_off"] = _to
                    if rep.get("press_skipped"):
                        _ps = int(rep["press_skipped"])
                        self.dp_info += (f"；⚠ **跳过三押 {_ps} 处**"
                                         f"（连双押也不插）")
                        self.warnings.append(
                            "★ 「三押」开关选的是**跳过** ⇒ {} 处「2 条多押轨同时响」"
                            "**一格都没插**（连双押也不插）。那些音就是空的，"
                            "要落点就把开关调成「不拆」或「拆三押」".format(_ps))
                        self.dp_report["dp_press_skipped"] = _ps
                    _gl = int((self.dp_press_n or {}).get("group_lost") or 0)
                    if _gl:
                        self.dp_info += f"；⚠ 同时押里 {_gl} 组没落到任何落点"
                        self.warnings.append(
                            f"多押轨有 {_gl} 组「同时音」没落到任何落点上"
                            f"（容差 ±{float(st.get('dp_tol', 45.0)):.0f}ms）")
                    if rep.get("skew_over"):
                        self.dp_info += f"；⚠ {rep['skew_over']} 处 Δ 超预算"
                        self.warnings.append(
                            f"双押偏移预算超了 {rep['skew_over']} 处"
                            f"（该格 bpm 太低，薄角已到下限）")
                    # ★ docs/31 §5.1「b」：丢了几个、为什么丢，一律说出来
                    if rep.get("reserved_used"):
                        self.dp_info += f"，走预留槽位 {rep['reserved_used']} 格"
                    # ★★ 调速落在双押第一格（2026-10 用户完整规则）：**赚回来的账要报**
                    if rep.get("setspeed_used"):
                        self.dp_info += (f"，**调速落双押第一格** "
                                         f"{rep['setspeed_used']} 处")
                    if rep.get("soft_used"):
                        self.dp_info += f"，借道自然段 {rep['soft_used']} 格"
                    if rep.get("twirl_busy"):
                        self.dp_info += f"，Twirl 改反向 {rep['twirl_busy']}"
                    if rep.get("moved"):
                        self.dp_info += (f"，换位 {rep['moved']}"
                                         f"（≤{rep['moved_ms_max']:.0f}ms）")
                    if rep.get("lost"):
                        why = _dp_reasons(rep)
                        self.dp_info += f"；⚠ 丢 {rep['lost']} 处（{why}）"
                        self.warnings.append(
                            f"角度双押丢了 {rep['lost']} 处：{why}")
                else:
                    plan = dp_midspin.plan(self.chart, tg, report=rep,
                                           travel_min=p_sv.travel_min)
                    dp_midspin.apply(self.chart, plan)
                    n_dp = len(plan)
                    self.dp_info = f"中旋双押 {n_dp} 处"
                self.dp_report.update(rep)
                self.dp_report["dp_inserted"] = n_dp
                _drn = int(self.chart.meta.get("dp_reserve_n") or 0)
                if _drn:
                    self.dp_info += (f"，预留 {_drn} 格"
                                     f"（其中平格 {self.chart.meta.get('dp_reserve_flat', 0)}）")
                if rep.get("dp_extra"):
                    self.warnings.append(f"双押多插了 {rep['dp_extra']} 个音")

        # ------------------------------------------- 步骤 8b 换手押上色（`docs/59`）
        # ★ 必须在**双押插入之后**：双押组的权威记录 `meta["dp_pairs"]` 是
        #   `core.dp_angle.apply` / `dp_midspin.apply` 回填的；几何判据也要看**最终** travel。
        # ★ 只产出事件、只写 `meta["color_events"]`；**绝不碰** floors/angleData/bpm/twirl。
        self.color_plan = colorize_mod.plan(
            self.chart,
            enabled=bool(st.get("color_schedule", True)),
            gap_tiles=int(st.get("handswitch_gap_tiles", 2)),
            min_cycles=int(st.get("handswitch_min_cycles", 2)),
            span=str(st.get("handswitch_color_span", colorize_mod.SPAN_THIN)),
            # ★ 只在换的那个格子染色：每 N 个双押染 1 个、从链内第几个起算
            color_every=int(st.get("handswitch_color_every", colorize_mod.COLOR_EVERY)),
            color_phase=int(st.get("handswitch_color_phase", colorize_mod.COLOR_PHASE)),
            cfg={"style": str(st.get("handswitch_track_style", "Neon")),
                 "color_type": str(st.get("handswitch_color_type", "Glow"))})
        self.chart.meta["color_events"] = list(self.color_plan.events)
        self.color_report = self.color_plan.to_dict()
        self.color_report["text"] = self.color_plan.report_text()
        # ★ 不许静默：跳过/认不出来的一律进 warnings（但有上限，免得刷屏）
        if self.color_plan.enabled:
            for w in self.color_plan.skipped_why[:4]:
                self.warnings.append("换手押上色：" + w)
            if len(self.color_plan.skipped_why) > 4:
                self.warnings.append(
                    f"换手押上色：另有 {len(self.color_plan.skipped_why) - 4} 条跳过原因（略）")

        # -------------------------------------- 步骤 8c 算法轨道调度（`docs/60`）
        # ★ 在 8b **之后**：涟漪环必须知道 ⑤b 占了哪些格（用户口径「⑤b 优先级更高」）。
        # ★ 驱动信号 = **谱面结构**（图形段 / 密度），**不用** onset ⇒ 不依赖采音质量。
        # ★ 皮肤写在 settings（`meta["appearance_settings"]`），事件写在 `meta["appearance_events"]`；
        #   **绝不碰** floors/angleData/bpm/twirl ⇒ 时序零影响。
        _occ = set(self.color_plan.tiles) if self.color_plan.enabled else set()
        _src = str(st.get("appearance_ripple_source", "figure"))
        _sources = (appearance_mod.FIGURE_FLAGS if _src == "figure"
                    else (_src,))
        self.appearance_plan = appearance_mod.plan(
            self.chart,
            enabled=bool(st.get("appearance_schedule", True)),
            skin_style=str(st.get("appearance_skin", appearance_mod.DEFAULT_SKIN)),
            skin_glow=int(st.get("appearance_glow", 100)),
            skin_pulse=str(st.get("appearance_pulse", "Forward")),
            ripple=bool(st.get("appearance_ripple", True)),
            ripple_rings=int(st.get("appearance_ripple_rings",
                                    appearance_mod.RIPPLE_RINGS)),
            ripple_step=float(st.get("appearance_ripple_step",
                                     appearance_mod.RIPPLE_STEP_DEG)),
            ripple_sources=_sources,
            radius=bool(st.get("appearance_radius", True)),
            radius_quiet=int(st.get("appearance_radius_quiet",
                                    appearance_mod.RADIUS_QUIET)),
            radius_dense=int(st.get("appearance_radius_dense",
                                    appearance_mod.RADIUS_DENSE)),
            dense_fps=float(st.get("appearance_dense_fps", appearance_mod.DENSE_FPS)),
            quiet_fps=float(st.get("appearance_quiet_fps", appearance_mod.QUIET_FPS)),
            min_sec=float(st.get("appearance_radius_min_sec",
                                 appearance_mod.RADIUS_MIN_SEC)),
            density_window=float(st.get("appearance_density_window",
                                        appearance_mod.DENSITY_WINDOW_S)),
            occupied=_occ)
        self.chart.meta["appearance_events"] = list(self.appearance_plan.events)
        self.chart.meta["appearance_settings"] = dict(self.appearance_plan.settings)
        self.appearance_report = self.appearance_plan.to_dict()
        self.appearance_report["text"] = self.appearance_plan.report_text()
        # ★ 不许静默：跳过/避让/截断一律进 warnings（有上限，免得刷屏）
        if self.appearance_plan.enabled:
            for w in self.appearance_plan.skipped_why[:4]:
                self.warnings.append("算法轨道调度：" + w)
            if len(self.appearance_plan.skipped_why) > 4:
                self.warnings.append(
                    f"算法轨道调度：另有 {len(self.appearance_plan.skipped_why) - 4} 条跳过原因（略）")

        # -------------------------------------- 步骤 8d 演出（入场 / 离场）（`docs/62`）
        # ★ 在 8c **之后**：演出要避让 ⑤b/⑤c 已经占掉的格（同格打架 ⇒ 让位并记账）。
        # ★ 驱动 = **谱面结构**（`Floor.engine` 三连音段标签）+ **用户分段**，不用 onset。
        # ★ 只写 `meta["show_events"]`（`MoveTrack`）+ `meta["show_beats_ahead"]`；
        #   **绝不碰** floors/angleData/bpm/twirl ⇒ 时序零影响。
        _show_segs = []
        for _s in (st.get("show_segments") or []):
            try:
                _lo = int(_s.get("lo"))
                _hi = int(_s.get("hi"))
            except (TypeError, ValueError):
                self.warnings.append(f"演出分段：`{_s}` 的起始/结束方块不是整数 ⇒ 跳过")
                continue
            _show_segs.append(show_mod.ShowSegment(
                lo=_lo, hi=_hi,
                in_move=str(_s.get("in_move") or ""),
                out_move=str(_s.get("out_move") or "")))
        _occ_show = set()
        if self.color_plan.enabled:
            _occ_show |= set(self.color_plan.tiles)
        if self.appearance_plan.enabled:
            _occ_show |= set(self.appearance_plan.floors)
        self.show_plan = show_mod.plan(self.chart, show_mod.ShowParams(
            enabled=bool(st.get("show_schedule", True)),
            out_move=str(st.get("show_out_move", "出A")),
            in_move=str(st.get("show_in_move", "入A")),
            lead=int(st.get("show_lead", 10)),
            margin=float(st.get("show_margin", 4.0)),
            segments=tuple(_show_segs),
            triplet=str(st.get("show_triplet", "qe")),
            qe_g=str(st.get("show_qe_g", show_mod.TRIPLET_QE)),
            collide_floors=tuple(sorted(_occ_show))))
        self.chart.meta["show_events"] = list(self.show_plan.events)
        self.chart.meta["show_beats_ahead"] = float(self.show_plan.required_beats_ahead)
        self.show_report = self.show_plan.to_dict()
        self.show_report["text"] = self.show_plan.report_text()
        # ★ 不许静默：跳过/截断/撞车一律进 warnings（有上限，免得刷屏）
        if self.show_plan.params.enabled:
            for w in self.show_plan.skipped_why[:4]:
                self.warnings.append("演出：" + w)

        # -------------------------------------- 步骤 8e 镜头调度（`docs/70` · 用户 2026-10）
        # ★★ 用户原话：「**去正式接线**，镜头调度中，**已经被验证的国士无双式写法允许先接线**，
        #   标记为「**（new）镜头调度**」，**目前只给出一个选项「呼吸」**。
        #   **其他方案等待完全成熟后接入**。」
        #   ⇒ 这里只接 `core/camera.py` 的 **呼吸（漂移 + 呼吸）**；
        #     `docs/70` §6/§7 的**聚焦**属于「其他方案」，还没接。
        # ★ 只写 `meta["camera_events"]`（`MoveCamera`）+ `meta["camera_settings"]`（基准位）；
        #   **绝不碰** floors/angleData/bpm/twirl ⇒ **时序零影响**（镜头只改"看哪里"）。
        # ★ `camera_mode = 0`（默认）⇒ 一个事件都不发、settings 一个字节都不动（老路径不变）。
        self.camera_report: dict = {}
        try:
            # ★ `camera_base_pos` 在 schema 里是**文本框**（`"x,y"`），不是列表 ——
            #   直接索引会拿到字符（`"0,0"[0] == "0"`）。两种形态都收。
            _raw_pose = st.get("camera_base_pos") or [0.0, 0.0]
            if isinstance(_raw_pose, str):
                _parts = [p for p in _raw_pose.replace("，", ",").split(",") if p.strip()]
                try:
                    _cpose = [float(_parts[0]), float(_parts[1])] if len(_parts) >= 2 \
                        else [0.0, 0.0]
                except (TypeError, ValueError):
                    self.warnings.append(
                        "镜头：基准位置 %r 读不出来 ⇒ 按 (0,0) 走" % _raw_pose)
                    _cpose = [0.0, 0.0]
            else:
                _cpose = [float(_raw_pose[0]), float(_raw_pose[1])]
            _cp = cam_mod.CameraParams(
                mode=int(st.get("camera_mode") or 0),
                step_beats=float(st.get("camera_step_beats", 8.0)),
                dur_beats=float(st.get("camera_dur_beats", 16.0)),
                drift_pos=bool(st.get("camera_drift_pos", True)),
                drift_rot=bool(st.get("camera_drift_rot", True)),
                pos_gain=float(st.get("camera_pos_gain", 1.0)),
                rot_gain=float(st.get("camera_rot_gain", 1.0)),
                breath=bool(st.get("camera_breath", True)),
                zoom_mid=float(st.get("camera_zoom_mid", 220.0)),
                zoom_amp=float(st.get("camera_zoom_amp", 45.0)),
                zoom_period_s=float(st.get("camera_zoom_period_s", 15.0)),
                zoom_phase=float(st.get("camera_zoom_phase", 0.7)),
                breath_eps=float(st.get("camera_breath_eps", 0.11)),
                from_floor=int(st.get("camera_from_floor") or 0),
                to_floor=int(st.get("camera_to_floor") or 0),
                max_events=int(st.get("camera_max_events", 4000)),
            )
            _z0 = float(st.get("camera_base_zoom", 200.0) or 200.0)
            _rot0 = float(st.get("camera_base_rot", 0.0) or 0.0)
            _ctimes = solve_mod.times_from_chart(self.chart)
            _cev, _cmeta = cam_mod.plan(
                _ctimes, _cp, base_bpm=float(self.chart.base_bpm or 0.0),
                pose=(float(_cpose[0]), float(_cpose[1]), _rot0), zoom0=_z0)
            self.camera_report = _cmeta.to_dict()
            if _cev:
                self.chart.meta["camera_events"] = _cev
                # ★ 基准位也写进 settings：镜头事件是**绝对位姿**，基准位不对齐整段就偏
                self.chart.meta["camera_settings"] = {
                    "position": [_wnum(float(_cpose[0])), _wnum(float(_cpose[1]))],
                    "rotation": _wnum(_rot0),
                    "zoom": _wnum(_z0),
                    "relativeTo": "Player",
                    "lockRot": False,
                }
                # ★ 不变量自检（`docs/70` §9）——
                #   不达标**进 warnings**，不静默（宁可上屏也不悄悄发出去）
                for _w in cam_mod.check_events(_cev, _ctimes)[:4]:
                    self.warnings.append("镜头：" + _w)
        except Exception as _ce:                                  # noqa: BLE001
            # ★ 不静默：抛了就说清楚，并且**一个事件都不发**（不猜一套镜头硬上）
            self.warnings.append(
                "镜头调度 **整组不生效**（走原路径）：{}: {}".format(
                    type(_ce).__name__, _ce))
            self.camera_report = {"ok": False, "why": str(_ce)}
            if len(self.show_plan.skipped_why) > 4:
                self.warnings.append(
                    f"演出：另有 {len(self.show_plan.skipped_why) - 4} 条跳过原因（略）")

        # ---------------------------------------------------- 步骤 9 派生量
        self.preview_lead_ms = solve_mod.total_lead_ms(self.chart, cd)
        self.chart_entry = solve_mod.game_entry_times(self.chart, cd)
        self.chart_times = solve_mod.times_from_chart(self.chart)
        nf = len(self.chart.floors)
        self.dp_inserted = nf - nf_before       # 插了几个双押层（决定导出怎么校验）
        lead = 1 if self.chart.meta.get("n_lead") else 0
        o2n = self.chart.meta.get("dp_old2new") or {}
        # ★ 直拟合会给长间隔拆**填充层** ⇒ 「第 i 个 onset 在第 i+1 层」不成立，
        #   得用 `onset_floors` 那张表（`core/fitdirect.py` 建的）。
        of = self.chart.meta.get("onset_floors") or []
        self.hit_times = []
        for i in range(len(self.onsets)):
            j = int(of[i]) if i < len(of) else min(i + lead, nf - 1)
            j = min(max(0, j), nf - 1)
            j = o2n.get(j, j)
            self.hit_times.append(self.chart_entry[min(j, nf - 1)])

        # --------------------------------------------------- 步骤 10 自动 offset
        # ★★ 2026-10-05 主人拍板（实测口径）：自动 offset = **采出来的第一个 onset**（ms），
        #   原样取，不加减任何补偿 —— 见 `first_onset_ms()`。
        #   旧口径 `onsets[0] + S − entryTime[0]` 额外扣掉了「首个 onset 落点所在格的
        #   entryTime」（实测 Amulet 2815.4 − 697.3 = 2118.1ms），谱面在游戏里整体提前，
        #   是错的。注意：这条口径对「MIDI 合成音」和「源音频原样交付」**一视同仁**。
        shift = self.audio_lead_ms(st)
        aud_len = self.shipped_len_ms(st)
        auto_offset = None
        if st.get("auto_offset"):
            auto_offset = self.first_onset_ms()
        # ★★ 记下来给 `resolve_offset()` 用（导出 / 预览 / 视图数据都要同一个值）
        self.auto_offset_ms = auto_offset

        # ---------------------------------------------------- 步骤 12 统计
        floors = self.chart.floors
        base_bpm = self.chart.base_bpm
        offset = float(auto_offset if auto_offset is not None
                       else st.get("offset", 0.0))
        chk = self._offset_check(self.chart, offset, cd, aud_len, shift)
        beat_ms = 60000.0 / max(1e-9, self.midi.bpm0)
        try:
            rs = rhythm_mod.extract(self.onsets, self.midi.ppqn, beat_ms)
            rhythm_txt = " ".join(f"{nm}:{frac * 100:.0f}%"
                                  for nm, frac, _c, _e in rs.hist(4))
            rhythm_err = rs.max_err_ms(beat_ms)
        except Exception as exc:                                # noqa: BLE001
            rhythm_txt, rhythm_err = "—", None
            self.warnings.append(f"节奏统计失败：{exc}")
        hist = self.chart.travel_hist(4)
        hist_txt = " ".join(f"{t:g}°×{c}" for t, c, _f in hist)
        viol = rules_mod.check_chart(self.chart)
        # ★ 只有 level != "info" 才算「违规」。info（雪花速度档豁免、双押中旋说明）
        #   以前也被算进 `n_violations`，于是状态栏会为一句提示打上 ⚠ 违规 —— 误导。
        viol_err = [v for v in viol if v.get("level") != "info"]
        viol_info = [v for v in viol if v.get("level") == "info"]
        n_pause = sum(1 for f in floors if f.pause_beats > 1e-9)
        n_tw = self.chart.n_twirl
        n_ss = self.chart.n_speed_events
        straight = self.chart.straight_frac
        n_tpl_cov = int(self.chart.meta.get("tpl_covered", 0) or 0)
        n_tpl = int(self.chart.meta.get("tpl_hits", 0) or 0)
        # ★ 轨道位置偏移的条数：关掉开关时 core 会落一个确定的 0（见 solve.py）
        n_pt = int(self.chart.meta.get("pos_track_n", 0) or 0)
        ov = solve_mod.path_overlap_stats(self.chart)
        sp = solve_mod.speed_profile(self.chart)

        # ---------------------------------------------------- 步骤 13 文案
        msg = (f"{len(used)} 轨 / {sum(len(trs[i].notes) for i in used)} note"
               f" → {len(self.onsets)} onset；{nf} 层，直线率"
               f" {straight * 100:.0f}%，Twirl {n_tw}"
               f"（{n_tw / max(1, nf) * 100:.0f}%），SetSpeed {n_ss}"
               f"（{n_ss / max(1, nf) * 100:.0f}%），Pause {n_pause}，"
               f"模板 {n_tpl} 段 / 覆盖 {n_tpl_cov} 格")
        # ★ 轨道位置偏移（PositionTrack）：只在真错开了 / 或被显式关掉时才提，
        #   免得状态栏变噪音。
        if n_pt:
            msg += f"；轨道错开 {n_pt} 格"
        elif not st.get("use_position_track", False):
            msg += "；轨道位置偏移已关闭"
        if n_dp:
            msg += f"；{self.dp_info}"
        msg += f"；基准 BPM {base_bpm:.3f}，cd={cd}"
        if st.get("auto_offset"):
            # ★ 2026-10-05：offset 的定义已改成「采出来的第一个 onset，原样取」。
            #   旧口径那句「命中误差」= |offset + hit_time[i] − onset[i]| 在新定义下
            #   **恒等于**「首个 onset 落点格前的 entryTime」（实测 Amulet 697.3ms）——
            #   它是定义的直接推论、不是错误，印出来只会让人以为坏了。故不再显示。
            msg += f"；自动 offset={offset:.0f}ms（= 首个 onset）"
        else:
            # ★ 与「自动 offset」同一口径（= 首个 onset）。别再用 `chk['suggest_ms']`
            #   那套带 entryTime 补偿的旧算法，否则同一个东西会显示两个数。
            msg += f"；建议 offset={self.first_onset_ms():.0f}ms"
        if viol_err:
            msg += f"；⚠ 规则违规 {len(viol_err)} 处"
        elif viol_info:
            msg += f"；提示 {len(viol_info)} 条"
        if self.region_meta:
            n_r = sum(1 for r in self.region_meta if r.get("n"))
            n_on = sum(int(r.get("n", 0)) for r in self.region_meta)
            msg += f"；区间 {len(self.region_meta)} 段（{n_r} 段生效 / 共采 {n_on} 点）"
        if self.segment_meta:
            n_s = sum(1 for r in self.segment_meta if r.get("n"))
            n_on = sum(int(r.get("n", 0)) for r in self.segment_meta)
            msg += (f"；分段 {len(self.segment_meta)} 段（{n_s} 段生效 / 共采 {n_on} 点"
                    + ("·到这点为止" if self.segment_mode == seg_mod.MODE_UNTIL
                       else "·从这点起") + "）")
        if self.chart.meta.get("snow_count"):
            msg += (f"；魔法阵 {self.chart.meta['snow_count']} 朵"
                    f"（{self.chart.meta.get('snow_tiles', 0)} 格）")
        # ★ 换手押上色（`docs/59`）：只在**开着**且**有账可说**时上屏（0 处也报，不许静默）
        if self.color_plan.enabled and (self.color_plan.n_events
                                        or self.color_plan.skipped_why
                                        or self.color_plan.n_skipped_press
                                        or self.color_plan.n_skipped_ambig):
            msg += "；" + self.color_plan.report_text()
        # ★ 算法轨道调度（`docs/60`）：同上 —— 开着就说，哪怕 0 处也报（不许静默）
        if self.appearance_plan.enabled and (self.appearance_plan.n_events
                                             or self.appearance_plan.skipped_why
                                             or self.appearance_plan.figure_spans):
            msg += "；" + self.appearance_plan.report_text()
        # ★ 对音阶梯（docs/25）：打开时报出每一级各答了几格，
        #   否则用户看不出「激进」到底激进在哪。
        _rc = self.chart.meta.get("ladder_rung_counts")
        if _rc:
            msg += ("；激进采音 图形{} 当前档{} +Twirl{} 变速{} 暂停{}".format(
                _rc.get(1, 0), _rc.get(2, 0), _rc.get(3, 0),
                _rc.get(4, 0), _rc.get(5, 0)))
        if self.warnings:
            msg += f"；⚠ {len(self.warnings)} 条警告"
        # ★ 预览音源（2026-10）：这一轮到底拿哪份音频做预览 —— **上屏，不许静默**
        _pa = self.preview_audio_file(st) or ""
        if _pa:
            msg += ("；预览音源 " + os.path.basename(_pa)
                    + ("（原曲）" if _pa == (self.source_audio or "") else "（指定）"))
        self.last_status = msg

        timing = (f"音值 {rhythm_txt}"
                  + (f"（量化误差 ≤{rhythm_err:.1f}ms）"
                     if rhythm_err is not None else "")
                  + f" · travel TOP {hist_txt}"
                  + f" · 前置静音 {self.preview_lead_ms:.0f}ms"
                  + f" · 行星速度 {sp.get('min', 1):g}~{sp.get('max', 1):g}"
                  + f" · 尾部余量 {chk['tail_gap_s']:.1f}s")
        self.last_timing = timing

        return {
            "ok": True,
            # ★ `app.js:604` 读的就是这个键 —— 以前**没人生产它**，
            #   于是「⚠ N 条警告」那一条一直是死的。这里补上（不许静默）。
            "warning_list": list(self.warnings),
            "derive": d,
            # ★ 去噪 / 直拟合的**机器可读报告**（`docs/44`）：面板/状态栏/单测都看它
            "fit_mode": self.fit_mode,
            "denoise": dict(self.denoise_meta or {}),
            "fit": dict(self.fit_meta or {}),
            # ★ ③b 采bpm（`docs/47`）：骨架注入报告（含给人看的一整段 `text`）
            "xk": dict(self.xk_meta or {}),
            # ★ 从 BDG 收回的轨道项目（`docs/45`）：有它时音轨就是那些轨
            "back": dict(self.back_meta or {}),
            "display_bpm": sv_info["display_bpm"],
            "auto_offset": auto_offset,
            "n_onsets": len(self.onsets),
            "n_floors": nf,
            # ★ 轨道位置偏移条数（0 = 关掉或本谱没有需要错开的重合）
            "pos_track_n": n_pt,
            "base_bpm": base_bpm,
            "preview_lead_ms": self.preview_lead_ms,
            # ★ 预览音源（2026-10）：这一轮交出去的**文件**（空串 = 合成音）。
            #   前端据此把 `preview_audio_offset_ms` 折进 `<audio>` 轴（见 app.js axisLead）。
            "preview_audio": _pa,
            "status": msg,
            "timing": timing,
            "dp": self.dp_report,
            "dp_info": self.dp_info,
            # ★★ 换手押上色（`docs/59`）：报告给 chips / 单测 / e2e 看（不许静默）
            "color": dict(self.color_report or {}),
            # ★★ 算法轨道调度（`docs/60`）：皮肤 / 涟漪环 / 半径调度
            "appearance": dict(self.appearance_report or {}),
            # ★★ 演出（`docs/62`）：入场 / 离场 / 反向 QE / 分段
            "show": dict(self.show_report or {}),
            # ★★ 镜头调度（`docs/70` · 用户 2026-10「（new）镜头调度」）：
            #   条数 / 覆盖格 / 静止秒数 / 缩放范围 / 不变量违规 —— 不许静默
            "camera": dict(self.camera_report or {}),
            "check": chk,
            "n_violations": len(viol_err),
            "violations": viol_err[:20],
            "n_infos": len(viol_info),
            "overlap": ov,
            "speed": sp,
            "regions": self.region_meta or [],
            "n_regions": len(self.region_meta or []),
            # ★ 分段采音（`docs/34` 方案 C）：每片的时刻/角色/点数，给 UI 画泳道
            "segments": self.segment_meta or [],
            "n_segments": len(self.segment_meta or []),
            "segment_mode": self.segment_mode,
            "payload": self.payload(st, merged, offset=offset),
        }

    # ============================================================ 视图数据
    def payload(self, st, merged=None, offset=None) -> dict:
        """给四个视图的原始数据（前端自己画，常量与旧视图逐条对齐）。

        ★ **轴口径（docs/24 §5，实测校准）**：payload 里所有「时刻」一律输出在
          **采音/音频轴**（= onset 轴，也就是 `<audio>.currentTime` 减掉前置静音后的轴）：

              采音轴 = 谱面轴 + audio_shift_ms           （audio_shift_ms = offset − S）
              <audio> 轴 = 采音轴 + audio_lead_ms        （S = 交出去的音频补的静音）

          以前这里混着两套轴（`notes` 在采音轴、`entry/hit/falls` 在谱面轴），
          MIDI 下 `S == lead == 谱面首格` 恰好重合所以看不出来；
          **载入 ogg（S = 0）时两者差整个 `offset`（实测 1529ms）**，采音点对不上音符。
        """
        if self.chart is None:
            return {}
        floors = self.chart.floors
        dp_pairs = self.chart.meta.get("dp_pairs") or []
        off = float(offset if offset is not None
                    else (st.get("offset", 0.0) or 0.0))
        lead = self.audio_lead_ms(st)
        shift = off - lead
        out = {
            "floors": [{
                "angle": f.angle, "travel": f.travel, "twirl": bool(f.twirl),
                "turn": f.turn, "heading": f.heading, "bpm": f.bpm,
                "k": f.speed_k, "x": f.x, "y": f.y,
                "pause": f.pause_beats, "tpl": bool(f.template),
                "nat": bool(f.natural), "eng": bool(f.engine),
                "snow": bool(f.snowflake), "snow_id": f.snowflake_id,
                "off": f.angle_offset,
            } for f in floors],
            "entry": [float(x) + shift for x in self.chart_entry],
            "times": [float(x) + shift for x in self.chart_times],
            "hit": [float(x) + shift for x in self.hit_times],
            "bpm0": self.chart.base_bpm,
            "dp_pairs": [list(p) for p in dp_pairs],
            # ★★ 换手押上色（`docs/59`）：给前端画「段带标记 + 卡片计数」用。
            #   事件本体在 `core.writer.build_actions` 里按 floor 合并进 actions。
            "color": dict(self.color_report or {}),
            # ★★ 算法轨道调度（`docs/60`）：给前端画段带标记 + 卡片计数。
            "appearance": dict(self.appearance_report or {}),
            # ★★ 演出（`docs/62`）：入场 / 离场 / 反向 QE / 用户分段
            "show": dict(self.show_report or {}),
            "offset": off,
            # ★ 两个换算常量（前端只需这两个，别再自己拼 offset/lead）
            "audio_shift_ms": shift,
            "audio_lead_ms": lead,
            # ★ 预览音源（2026-10）：非空 = 这一轮预览放的是**文件**（原曲 / 指定），
            #   前端的 `preview_audio_offset_ms` 只在它非空时生效（见 app.js axisLead）。
            "preview_audio": str(self.preview_audio_file(st) or ""),
            "countdown_ticks": int(st.get("countdown_ticks", 4)),
            "total_ms": float(self.midi.length_ms),
            "lead_ms": float(self.preview_lead_ms),
            "cap": self.cap_times(st),
            "beat_grid": self.beat_grid_ms(),
            "regions": self.region_meta or [],
            # ★ 分段泳道（`docs/34` 方案 C）：全曲预览条按这个画「一条轨一行」的色块
            "segments": self.segment_meta or [],
            "segment_mode": self.segment_mode,
            "region_spans": [[r["start_ms"], r["end_ms"]] for r in
                             (self.region_meta or [])],
            # ★ 轨道位置偏移（PositionTrack）实际写了几条。
            #   关掉开关时 core 落的是确定的 0（见 solve.py），
            #   所以前端/e2e 能靠它区分「关掉了」与「本谱没有重合」——
            #   ⚠ 别读 `rebuild()` 返回值里的那个同名键，那是另一个 dict。
            "pos_track_n": int(self.chart.meta.get("pos_track_n", 0) or 0),
            "pos_tracks": [[int(i), float(dx), float(dy)] for i, dx, dy
                           in (self.chart.meta.get("pos_tracks") or [])],
        }
        if merged is not None:
            cap = 40000
            if len(merged) > cap:
                self.warnings.append(f"卷帘只画前 {cap} 个音符（共 {len(merged)}）")
                merged = merged[:cap]
            out["notes"] = [{"t": n.t_on_ms, "d": n.t_off_ms - n.t_on_ms,
                             "p": n.pitch} for n in merged]
        out["falls"] = self._falls(st)
        return out

    def _falls(self, st) -> list[dict]:
        """旧 UI `_falling_notes()` `:1138-1166`（`is_hold` 恒 False 也照搬）。

        ★ 时刻输出在**采音轴**（`payload` 的轴口径），所以用 `shift` 而不是 `offset`。
        """
        if not self.chart or self.midi is None:
            return []
        lanes = int(st.get("lanes", 4))
        if lanes not in (4, 8):
            lanes = 4
        mode = int(st.get("lanemode", 0))
        shift = float(st.get("offset", 0.0) or 0.0) - self.audio_lead_ms(st)
        trs = self.midi.tracks
        used = self._selected(st, self._dp_tracks(st))
        notes = [n for i in used for n in trs[i].notes]
        if not notes:
            return []
        ranked = sorted({n.pitch for n in notes})
        lo = min(ranked)
        span = max(1, max(ranked) - lo)
        out = []
        for i, o in enumerate(self.onsets):
            press = shift + (self.hit_times[i] if i < len(self.hit_times) else 0.0)
            p = o.pitch
            if mode == 0:
                r = (bisect.bisect_right(ranked, p) - 0.5) / max(1, len(ranked))
                lane = int(r * lanes)
            elif mode == 1:
                lane = int(round((p - lo) / span * (lanes - 1)))
            else:
                lane = i % lanes
            out.append({"press": press, "lane": max(0, min(lanes - 1, lane)),
                        "pitch": p, "vel": o.velocity, "hold": False})
        return out

    def cap_times(self, st) -> list[float]:
        """旧 UI `_cap_times_ms()` `:1123-1136`：下落式切分线（**采音轴**）。"""
        if not self.chart or len(self.chart_entry) < 2:
            return []
        div = int(st.get("division", 8))
        if div not in (4, 8, 16, 32):
            div = 8
        offset = float(st.get("offset", 0.0) or 0.0) - self.audio_lead_ms(st)
        step = (60000.0 / max(1e-9, self.chart.base_bpm)) * 4.0 / div
        if step <= 0:
            return []
        t0 = offset + self.chart_entry[1]
        end = offset + self.chart_entry[-1]
        t = t0 - ((t0 - offset) % step)
        out = []
        while t <= end and len(out) < 20000:
            out.append(t)
            t += step
        return out

    def beat_grid_ms(self, division: int = 4) -> list[float]:
        """旧 UI `_beat_grid_ms()` `:1070-1083`：只取前 30 秒，且只用 `bpm0`。"""
        if self.midi is None:
            return []
        bpm0 = self.midi.bpm0 or 120.0
        step = (60000.0 / bpm0) * 4.0 / max(1, division)
        end = min(self.midi.length_ms, 30000.0)
        out, t = [], 0.0
        while t < end:
            out.append(t)
            t += step
        return out

    def resolve_offset(self, st) -> float:
        """**这一轮真正要用的 offset**（ms）——「自动 offset」开着就用算出来的那个。

        ★★ 2026-10 修 bug：以前**只有 `rebuild()` 内部**用了自动值，
        `export()` / `level_json()` 还各读各的 `st["offset"]` ⇒
        客户端没把返回值回写进 state 时，**导出的谱和预览对不上**
        （`test_sidecar` 的「第三方反解校验」实测 9.7ms、最大 9722us 直接 FAIL：
        层号 #92 期望 22666.667ms、实际 2.083ms）。

        口径：「自动 offset」= **后端说了算**（前端把它的返回值显示在 offset 框里）。
        用户要手改 offset ⇒ 前端**自动关掉**「自动 offset」（见 `app.js`
        `onFieldChanged`），于是这里自然回落到手动值。**两边不会互相打架。**
        """
        if st.get("auto_offset") and self.auto_offset_ms is not None:
            return float(self.auto_offset_ms)
        return float(st.get("offset", 0.0) or 0.0)

    def level_json(self, st) -> dict | None:
        """把当前谱面导出成 `.adofai` 的 JSON（给前端内嵌的 ADOFAI 播放器用）。

        ★ 与 `export()` 写盘走的是**同一个** `writer.build_json`、同一组参数，
        所以「预览看到的」和「导出成品」不会变成两套时序。
        """
        if self.chart is None:
            return None
        return writer.build_json(
            self.chart,
            song=str(st.get("song", "")), artist=str(st.get("artist", "")),
            author=str(st.get("author", "")),
            offset_ms=self.resolve_offset(st),
            difficulty=int(st.get("difficulty", 0)),
            countdown_ticks=int(st.get("countdown_ticks", 4)),
            separate_countdown=bool(st.get("separate_countdown", False)))

    # ================================================================ 音频
    def preview_audio_file(self, st) -> str | None:
        """★ ★ 用户 2026-10：「预览时，**建议允许（不强制）使用 ogg**，并允许**调节 ogg 偏移**
        来使用**音频混合预览**」。

        口径（`preview_audio_mode`，**默认 0 = 与今天逐字节相同**）：

        | 值 | 名字 | 交出去的音频 |
        | --- | --- | --- |
        | 0 | **自动**（默认） | 有原曲就用原曲（= 今天：载入 .ogg / BDG 工程带音频名）|
        | 1 | **合成音** | 强制用我们合成的节拍音（即使载入的是音频源）|
        | 2 | **指定文件** | 用 `preview_audio_path` 那个文件（用户自己挑的原曲）|

        返回 `None` = 用合成音（`synth.render`）。

        ★ 「偏移」不在后端 —— 那是**预览专用**的前端旋钮（`preview_audio_offset_ms`），
          只改 `<audio>` 轴，**不写进谱面**（见前端 `axisLead()`）。
        """
        mode = int(st.get("preview_audio_mode", 0) or 0)
        if mode == 1:
            return None
        path = str(st.get("preview_audio_path") or "").strip()
        if mode == 2:
            if not path:
                return None
            if not os.path.isfile(path):
                # ★ 不许静默：说了要用它却不在，就上屏（退回合成音）。
                # ⚠ `audio_lead_ms` 一次 rebuild 里会被调用好几次 ⇒ **去重**，
                #   否则同一条警告会刷屏。
                _w = f"★ 预览音源指定的文件不存在：{path} —— 这一轮**退回合成音**"
                if _w not in self.warnings:
                    self.warnings.append(_w)
                return None
            return path
        if self.source_audio and os.path.exists(self.source_audio):
            return self.source_audio
        return None

    def audio_for_current(self, st) -> str | None:
        """旧 UI `_audio_for_current()` `:1169-1195`（+ 2026-10 的「预览音源」三档）。"""
        if self.midi is None:
            return None
        _pv = self.preview_audio_file(st)
        if _pv:
            return _pv
        idxs = self._selected(st, self._dp_tracks(st)) \
            or [int(st.get("current_track", 0))]
        key = (self.midi_path, tuple(idxs), len(self.onsets),
               round(float(st.get("merge_ms", 30)), 3),
               int(st.get("min_velocity", 1)),
               round(float(st.get("min_interval_ms", 0)), 3))
        if key == self._preview_key and self._preview_path \
                and os.path.exists(self._preview_path):
            return self._preview_path
        d = tempfile.mkdtemp(prefix="adofai_preview_")
        wav = os.path.join(d, "preview.wav")
        synth.render(self.midi, wav, track_index=idxs[0], track_indexes=idxs,
                     onsets=self.onsets, click=True,
                     lead_ms=float(self.preview_lead_ms or 0.0))
        self._preview_key, self._preview_path = key, wav
        return wav

    # ================================================================ 导出
    def export(self, st, out_dir: str) -> dict:
        """旧 UI `export_chart()` `:1260-1300`。"""
        if self.chart is None:
            return {"ok": False, "msg": "还没有生成谱面。"}
        name = (str(st.get("song", "")) or "main").strip()
        for ch in '\\/:*?"<>|':
            name = name.replace(ch, "")
        name = name.strip() or "main"
        outdir = os.path.join(out_dir, name)
        wav = self.audio_for_current(st)
        _off = self.resolve_offset(st)
        p = writer.write_dir(
            self.chart, outdir, name="main", audio_src=wav,
            song=str(st.get("song", "")), artist=str(st.get("artist", "")),
            author=str(st.get("author", "")),
            offset_ms=_off,
            difficulty=int(st.get("difficulty", 0)),
            countdown_ticks=int(st.get("countdown_ticks", 4)),
            separate_countdown=bool(st.get("separate_countdown", False)),
            # ★ 2026-09-20 主人指定：**导出**的 settings 只写白名单里的键（见 writer.EXPORT_SETTINGS_KEYS），
            #   其余（version / preview* / bg* / zoom / legacy* …）全部删掉。
            #   `level_json()`（界面预览）**不带**这个开关 ⇒ 预览与旧版逐字节相同。
            minimal_settings=True)
        cd = int(st.get("countdown_ticks", 4))
        chk = self._offset_check(self.chart, _off, cd,
                                 self.shipped_len_ms(st), self.audio_lead_ms(st))
        vr_txt, vr_ok = "", None
        try:
            if self.dp_inserted:
                # ★ 插了双押层后「层 ↔ onset」不再 1:1，逐层对照会给出假失败
                #   （实测 max 999.8ms）。改用**按键集合**校验（core.verify）。
                vr = verify.verify_press_subset(p, list(self.hit_times), tol_ms=5.0)
                vr_txt = ("[按键盘] " + vr.summary()
                          + f"  （已插入 {self.dp_inserted} 个双押层，"
                            f"逐层 1:1 校验不适用）")
            else:
                # ★★ 2026-10：**直拟合 + 15° 阶梯**下，「层 ↔ onset」的时间会被
                #   **主动**挪动（挪动量 ≤ 「拟合容差」，见 `core.fitdirect`）——
                #   再拿**原始** onset 去比，必然报一个吓人的 FAIL（实测 max 37.9ms），
                #   可那正是「拟合」本身，不是序列化错了。这就是一种静默的假警报。
                #   ★★ 2026-10 第二次口径修正：同样的假警报在**最优化**那条路上也有 ——
                #   `solve()` 会**量化音值**（`quantize_rhythm`），实测 Grin 多轨上
                #   离原始 onset 最多 **3.1ms**（>1ms 容差）⇒ 又报一次 FAIL。
                #   所以彻底改成：**序列化校验一律拿「谱面模型自己的时刻」比**
                #   （`times_from_chart`），把「相对原始 onset 偏了多少」另起一行如实印。
                #   这才分清「写盘/反解有没有丢东西」与「谱面本来就不等于输入」两件事。
                _exp = [o.t_ms for o in self.onsets]
                _extra = ""
                try:
                    _tt = solve_mod.times_from_chart(self.chart)
                    _of = list(self.chart.meta.get("onset_floors") or [])
                    _n_on = len(self.onsets)
                    if len(_of) == _n_on and _of:
                        # 直拟合：路径上有 **onset → 层** 的对应表（含填充层/休止格）
                        _b = _of[0]
                        _model = [_tt[_of[j]] - _tt[_b] for j in range(_n_on)]
                    elif len(_tt) == _n_on + 1:
                        # 最优化：层 ↔ onset **1:1**（首层是开局站位，`lead_floors=1`）
                        _model = [_tt[1 + j] - _tt[1] for j in range(_n_on)]
                    else:
                        _model = None
                    if _model is not None:
                        _dev = max(abs(_model[j]
                                       - (self.onsets[j].t_ms - self.onsets[0].t_ms))
                                   for j in range(_n_on))
                        if _dev > 0.05:
                            _why = ("直拟合 15° 阶梯**主动**改了时刻"
                                    if (self.chart.meta.get("fit") or {}).get("ladder_on")
                                    else "谱面**量化了音值**（`quantize_rhythm`）")
                            _exp = _model
                            _extra = (f"\n★ {_why}：相对原始 onset 最大 {_dev:.2f}ms ——"
                                      f"上面这条是**序列化**校验（拿谱面模型自己的时刻比），"
                                      f"不是拿原始 onset 硬比")
                except Exception:                               # noqa: BLE001
                    _extra = ""                                  # 算不出模型时刻就退回原口径
                vr = verify.verify_file(p, _exp, tol_ms=1.0, lead_floors=1)
                vr_txt = vr.summary() + _extra
            vr_ok = bool(vr.ok)
        except Exception as exc:                                # noqa: BLE001
            vr_txt = f"第三方反解校验失败：{exc}"
        nf = len(self.chart.floors)
        msg = (f"导出到 {outdir}\n"
               f"层数 {nf}，直线率 {self.chart.straight_frac * 100:.0f}%，"
               f"Twirl {self.chart.n_twirl}，SetSpeed {self.chart.n_speed_events}\n"
               f"countdownTicks={cd}  offset={_off:.0f}ms"
               f"（{'自动' if st.get('auto_offset') else '手动'}）"
               f"  前置静音 {self.preview_lead_ms:.0f}ms\n"
               f"尾部余量 {chk['tail_gap_s']:.1f}s\n{vr_txt}")
        self.last_status = vr_txt or msg
        return {"ok": True, "dir": outdir, "chart": p,
                "audio": bool(wav), "verify": vr_txt, "verify_ok": vr_ok,
                "msg": msg, "check": chk}
