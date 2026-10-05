# -*- coding: utf-8 -*-
"""大直线 / 采bpm —— xk base 的骨架（定稿 = `docs/47`）。

## 它是什么

用户口径（`docs/47` §1 第 4 条，原话）：

    大直线 = 直接采 4 分音，**无视所有音符排列强行采音**。
             绝对能对上拍，但是绝对不好看。

⇒ 本模块**不看 onsets、不测 tempo、不找最长直线段**，只按 `tbpm / N / φ`
硬铺一条等间隔骨架。

## 口径（`docs/47` §1）

    cbpm = tbpm × N          N ∈ {2,4,8}；**0 = 关**（xk base 不参与）
    砖长 = 60000 / cbpm
    t    = φ + k · 砖长       （φ = 我们现有的 `offset`，**同一根轴**）
    k    = 格子号（冰与火之舞里那个「物量」）

★ 「4 分音」是**相对说法**：= 一拍 4 砖 = `N=4`（**不是**乐理上的 ¼ 拍）。
★ `tbpm` 必须**先四舍六入五成双取整、不含小数**（见 `round_tbpm`）。

## 本模块**故意不做**的事（都由调用方决定）

* **不猜 φ**：相位的来源是外部（我们界面上的 `offset` / 桥接锚）。本模块只吃它。
* **不采音**：`docs/47` §1 第 5 条 —— 采bpm 开 ⇒ 主轨失效、其余音轨只当多押。
  多押是 `core/dp_*` / 将来的 `multipress` 的事。
* **区间外**：用户口径「**区间外走原路径**」（`docs/47` §1 第 13 条）⇒
  本模块只负责**区间内**那段骨架，`tiles()` 之外的时间它一个字都不吐。

## 必须报出来的（不许静默，`docs/47` §2.2）

那个测速站输出被夹在 `[50, 210]`（`minBPM 50 / maxBPM 210`）⇒ tbpm 可能**差一个八度**，
而 `cbpm = tbpm × N` ⇒ **错一个八度整条骨架全错**。`octave_note()` 专门盯这一条。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN

#: 允许的 N；**0 = 关**（xk base 不参与，走老路）
N_CHOICES = (0, 2, 4, 8)
N_ON = (2, 4, 8)

#: 骨架每一块砖的形状：一拍 N 砖、全直线 ⇒ 每块都是 180°
TILE_TRAVEL = 180.0

#: 判定表里的「大直线」这个名字（报告用）
NAME = "大直线"

#: ★★ **砖数硬上限** —— 没有它，一个填错的 tbpm（或一段离谱的区间）能生成几十万块砖，
#   把 sidecar（单线程）**卡死几十秒**，界面表现为「进度条和重新生成一起冻住」。
#   实测：`0~1e8ms` × 150ms 砖 ＝ 666667 块 ⇒ **53 秒**。超上限**直接报错**，不硬算。
MAX_TILES = 20000

#: 砖长的合理区间（ms）：比这更细/更粗一定是 tbpm 或 N 填错了
TILE_MS_MIN = 1.0
TILE_MS_MAX = 60000.0

_EPS = 1e-9


class XkError(ValueError):
    """口径错误（N 不合法 / tbpm 空或非数 …）—— **抛，不静默吞**。"""


# ============================================================ 取整 / 换算
def round_tbpm(raw) -> int:
    """**四舍六入五成双**（banker's rounding）取整，**不含小数**（`docs/47` §1 第 8 条）。

    ★ 为什么不用内建 `round()`：那个站给的是**文本**（`"119.84"` / `"120.5"`），
      裸 `round()` 在二进制浮点的 `.5` 上会偶发失手；走 `Decimal` 才是真的「五成双」。

        round_tbpm("120.5") -> 120      （成双：往偶的取）
        round_tbpm("121.5") -> 122
        round_tbpm("119.84") -> 120
        round_tbpm(133) -> 133
    """
    if raw is None:
        raise XkError("tbpm 是空的")
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            raise XkError("tbpm 是空的")
    else:
        s = str(raw)
    try:
        d = Decimal(s)
    except (InvalidOperation, ValueError):
        raise XkError("tbpm 解析不了：{!r}".format(raw)) from None
    if not d.is_finite():
        raise XkError("tbpm 不是有限数：{!r}".format(raw))
    out = int(d.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))
    if out <= 0:
        raise XkError("tbpm 必须 > 0（取整后是 {}）".format(out))
    return out


def check_n(n) -> int:
    """`N` 的合法性 —— 只允许 `N_CHOICES`，越界**报错不兜底**。"""
    if isinstance(n, bool):
        raise XkError("N 不是布尔：{!r}".format(n))
    try:
        v = int(n)
    except (TypeError, ValueError):
        raise XkError("N 不是整数：{!r}".format(n)) from None
    if float(n) != float(v):
        raise XkError("N 必须是整数：{!r}".format(n))
    if v not in N_CHOICES:
        raise XkError("N 只能是 {}（拿到 {}）".format(list(N_CHOICES), v))
    return v


def cbpm(tbpm, n) -> float:
    """`cbpm = tbpm × N`（= 我们代码里的 `base_bpm`）。"""
    n = check_n(n)
    if n == 0:
        raise XkError("N=0（xk base 关）时没有 cbpm")
    return float(int(tbpm)) * float(n)


def tile_ms(tbpm, n) -> float:
    """**砖长**（ms）：`60000 / cbpm = 60000 / (tbpm × N)`。

    一拍 N 砖 ⇒ 砖长 = 一拍的 1/N。这就是全部；「叫几分音」只是标签（`docs/47` §1 第 2 条）。
    """
    return 60000.0 / cbpm(tbpm, n)


# ============================================================ 骨架
@dataclass
class BigLine:
    """一条**等间隔**骨架：`t = φ + k · 砖长`。

    `tbpm` 必须是**整数**（由 `round_tbpm` 得到）；`phase_ms` = φ = 我们现有的 `offset`。
    `N = 0` 时本对象**不成立**（`ok=False`），用 `BigLine.maybe()` 拿「可能为空」的实例。
    """

    tbpm: int
    n: int
    phase_ms: float = 0.0
    ok: bool = True
    why: str = ""
    #: 如果传进来的 tbpm 不是整数、被 `round_tbpm` 取整过，这里记原值（**不许静默**）
    rounded_from: float = None

    # ---------------------------------------------------------- 构造
    def __post_init__(self):
        if not self.ok:
            return
        n = check_n(self.n)
        if n == 0:
            self.ok, self.n, self.why = False, 0, "xk base 关（N=0）⇒ 没有骨架"
            return
        raw = self.tbpm
        tb = round_tbpm(raw)                             # 顺带挡掉 NaN/Inf/<=0/空
        if isinstance(raw, float) and float(raw) != float(tb):
            self.rounded_from = float(raw)
        self.tbpm, self.n = tb, n
        self.phase_ms = float(self.phase_ms)
        p = self.period_ms
        if not (TILE_MS_MIN <= p <= TILE_MS_MAX):
            raise XkError(
                "砖长 {:.4f}ms 不合理（tbpm {} × {}k ⇒ cbpm {:.0f}）—— "
                "允许 {:.0f}~{:.0f}ms；tbpm 多半填错了（别把 cbpm / 谱面 BPM 当 tbpm）"
                .format(p, self.tbpm, self.n, self.cbpm, TILE_MS_MIN, TILE_MS_MAX))

    @classmethod
    def maybe(cls, tbpm, n, phase_ms: float = 0.0) -> "BigLine":
        """`N=0` / tbpm 空 ⇒ 返回 `ok=False` 的空骨架（**不抛**，让调用方走老路）。"""
        try:
            n = check_n(n)
        except XkError as exc:
            return cls(0, 0, phase_ms, ok=False, why=str(exc))
        if n == 0:
            return cls(0, 0, phase_ms, ok=False, why="xk base 关（N=0）⇒ 走原路径")
        try:
            return cls(tbpm, n, phase_ms, ok=True)
        except XkError as exc:
            return cls(0, 0, phase_ms, ok=False, why=str(exc))

    # ---------------------------------------------------------- 换算
    @property
    def cbpm(self) -> float:
        return cbpm(self.tbpm, self.n)

    @property
    def period_ms(self) -> float:
        """砖长（ms）—— 与 `core/denoise.GridPlan.period_ms` 同一口径，可直接对表。"""
        return tile_ms(self.tbpm, self.n)

    def ms_of(self, k) -> float:
        """第 `k` 块砖的时刻（ms）。`k` 允许负数与小数（小数 = 砖内插值）。"""
        return self.phase_ms + float(k) * self.period_ms

    def tile_of(self, ms) -> int:
        """`ms` 落在**第几块砖**里（向下取整；物量号 = `k + 1`）。"""
        v = float(ms)
        if not (-1e15 < v < 1e15):
            raise XkError("时间不是有限数：{!r}".format(ms))
        return int(math.floor((v - self.phase_ms) / self.period_ms + _EPS))

    def _guard(self, lo: int, hi: int, what: str = "区间") -> int:
        """★ 砖数硬上限（见 `MAX_TILES`）—— 超了就**报错**，绝不做几十万块的活。"""
        cnt = hi - lo + 1
        if cnt > MAX_TILES:
            raise XkError(
                "{}里要铺 **{}** 块砖，超过上限 {}（砖长 {:.3f}ms）——"
                "多半是 tbpm / N 填错，或者区间起止填得太大；"
                "全曲采bpm 时请把 **N 调小**或**核对 tbpm**，别硬算".format(
                    what, cnt, MAX_TILES, self.period_ms))
        return cnt

    def floor_no(self, ms) -> int:
        """「**物量**」号（1 起算的格子号）= `tile_of(ms) + 1`。"""
        return self.tile_of(ms) + 1

    def in_tile(self, ms) -> float:
        """`ms` 在它那块砖里的位置（ms，`[0, 砖长)`）—— 上屏参考用。"""
        return float(ms) - self.phase_ms - self.tile_of(ms) * self.period_ms

    def snap(self, ms) -> float:
        """吸到**最近的**格（半边点靠 Python 的 `round` 半数向偶 —— 与骨架自身的
        格子对齐，不参与「撞格丢音」那套；采bpm 下没有 onset 会被丢）。"""
        k = int(round((float(ms) - self.phase_ms) / self.period_ms))
        return self.ms_of(k)

    def beat_of(self, ms) -> float:
        """第几**拍**（小数）—— `t = φ + 拍 · N · 砖长`，所以 拍 = k / N。"""
        return (float(ms) - self.phase_ms) / (self.period_ms * self.n)

    def drift_of(self, ms) -> float:
        """`ms` 离最近格的**有向**偏移（ms）—— 报「对没对上拍」用。"""
        return float(ms) - self.snap(ms)

    # ---------------------------------------------------------- 区间
    def tiles(self, t0: float, t1: float) -> list:
        """`[t0, t1]` 内的砖号（升序）。**端点各算一次**：含 `t0` 所在的砖、
        到 `t1` 所在的砖为止。"""
        if t1 < t0:
            t0, t1 = t1, t0
        k0, k1 = self.tile_of(t0), self.tile_of(t1)
        self._guard(k0, k1)
        return list(range(k0, k1 + 1))

    def layer(self, t0: float, t1: float) -> list:
        """区间内的骨架 = `[(k, ms), …]`（每块砖一个 180° 直线格）。"""
        return [(k, self.ms_of(k)) for k in self.tiles(t0, t1)]

    def tiles_within(self, t0: float, t1: float) -> list:
        """**毫秒落在 `[t0, t1]` 内**的砖号。

        ★ 与 `tiles()` 的区别：`tiles()` 会带上「**包含** `t0` 的那块砖」，
        那块砖的毫秒可能**小于** `t0`（格子与区间起点没对齐时）。
        xk base 的区间要的是**格子在区间里**，所以用这一个 ——
        否则骨架会往区间**前面**多铺一块（实测会跟上一段撞砖）。
        """
        if t1 < t0:
            t0, t1 = t1, t0
        k0, k1 = self.tile_of(t0), self.tile_of(t1)
        self._guard(k0, k1)
        return [k for k in range(k0, k1 + 1)
                if t0 - _EPS <= self.ms_of(k) <= t1 + _EPS]

    def spans_of(self, t0: float, t1: float) -> list:
        """区间内的骨架（**带精确边界**），供接进 solve：

            [{"k": k, "ms": ms, "travel": 180.0, "bpm": cbpm}, …]
        """
        return [{"k": k, "ms": ms, "travel": TILE_TRAVEL, "bpm": self.cbpm}
                for k, ms in self.layer(t0, t1)]

    # ---------------------------------------------------------- 报告
    def to_dict(self) -> dict:
        if not self.ok:
            return {"ok": False, "why": self.why}
        return {
            "ok": True, "tbpm": self.tbpm, "n": self.n,
            "cbpm": round(self.cbpm, 6),
            "period_ms": round(self.period_ms, 6),
            "phase_ms": round(self.phase_ms, 6),
            "tile_ms": round(self.period_ms, 6),
        }

    def report_text(self) -> str:
        if not self.ok:
            return "大直线：关（{}）".format(self.why)
        return ("大直线：tbpm {} × {}k ⇒ cbpm {:.0f} · 砖长 {:.3f}ms · 相位 {:+.3f}ms"
                "（格子 k ↔ t = φ + k·砖长）".format(
                    self.tbpm, self.n, self.cbpm, self.period_ms, self.phase_ms))


# ============================================================ 起止点
def parse_point(raw, line: BigLine, unit: str = "ms") -> float:
    """把**用户填的一个点**换成毫秒（`docs/47` §3：起止点**可以是格子或毫秒**）。

    `unit="tile"` ⇒ `raw` 是**格子号**（物量号，1 起算）；`unit="ms"` ⇒ 直接是毫秒。
    非法值**抛 `XkError`**（不猜、不兜底）。
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise XkError("{}点是空的".format(unit))
    try:
        v = float(str(raw).strip())
    except (TypeError, ValueError):
        raise XkError("{}点解析不了：{!r}".format(unit, raw)) from None
    if unit in ("tile", "grid", "格", "格子", "物量"):
        if line is None or not line.ok:
            raise XkError("按格子给点需要一条骨架（现在 xk base 是关的）")
        if v != int(v):
            raise XkError("格子号必须是整数：{!r}".format(raw))
        return line.ms_of(int(v) - 1)                     # 物量号 1 起算 ⇒ k = 号-1
    if unit in ("ms", "毫秒", "time"):
        return v
    raise XkError("不认识的点单位：{!r}（只有 ms / tile）".format(unit))


def parse_span(a, b, line: BigLine, unit: str = "ms") -> tuple:
    """起止点 → `(t0_ms, t1_ms)`（自动排好先后）。`unit` 对两端**相同**。"""
    t0, t1 = parse_point(a, line, unit), parse_point(b, line, unit)
    return (t0, t1) if t1 >= t0 else (t1, t0)


# ============================================================ 八度校验
def octave_note(tbpm, n, period_ms: float, *, tol: float = 0.06) -> str:
    """把「tbpm × N 得到的砖长」与**另一条路解出来的砖长**比（`docs/47` §2.2）。

    那个测速站输出被夹在 `[50, 210]` ⇒ 超 210 的曲子会给 **half-time**
    （240 → 120、300 → 150）。而 `cbpm = tbpm × N` 错一个八度**整条骨架全错**
    ⇒ 所以这里**只报不改**（要改是用户的事）。

    返回 `""`（对得上 / 数据不足）或一句人话。
    """
    if not period_ms or period_ms <= 0 or not n:
        return ""
    mine = tile_ms(tbpm, n)
    if mine <= 0:
        return ""
    ratio = period_ms / mine
    # ★ 查 2 的幂到 ×8 / ÷8：那个站夹在 [50, 210]（约 4.2 倍宽），
    #   所以真实 tbpm 240/480/960 都可能被报成同一个值 ⇒ 差几倍都要认出来。
    for m, said in ((2.0, "×2"), (4.0, "×4"), (8.0, "×8"),
                    (0.5, "÷2"), (0.25, "÷4"), (0.125, "÷8")):
        if abs(ratio - m) <= tol * m:
            return ("⚠ 八度嫌疑：按 tbpm {} × {}k 算出的砖长是 {:.3f}ms，"
                    "而去噪/网格解出来的是 {:.3f}ms —— 差了 **{}** "
                    "（那个测速站夹在 50~210，超 210 会给 half-time）。"
                    "现在按{}。".format(tbpm, n, mine, period_ms, said,
                                      "你填的 tbpm"))
    return ""


def report_text(line: BigLine, *, t0: float = None, t1: float = None,
                octave: str = "") -> str:
    """一行总报告（上屏用）。"""
    out = [line.report_text()]
    if line.ok and t0 is not None and t1 is not None:
        ks = line.tiles(t0, t1)
        out.append("区间 {:.3f}~{:.3f}ms ⇒ 格子 {}~{}（{} 块砖）".format(
            min(t0, t1), max(t0, t1), ks[0] + 1, ks[-1] + 1, len(ks)))
        out.append("区间外走**原路径**（主轨采音 + 常规求解）")
    if octave:
        out.append(octave)
    return "\n".join(out)
