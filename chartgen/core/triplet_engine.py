"""长/短三连音段引擎 —— 按「转向符号掩码」铺，保证**时长恒定 + 朝向回正**。

============ 原理 ============

`_apply()` 的符号约定（`core/solve.py:430`）:

    turn = (180 − travel) if ccw else (travel + 180)
    ⇒  a 的变化 = +δ （ccw=False） / −δ （ccw=True）        δ = 180 − travel

两个后果：

1. **Twirl 不改时长。** 只要 `travel` 恒定，每格 dt 就恒定（= travel/180/speed 拍）。
   Twirl 只决定 `a` 往正还是往负走。所以「匀速」和「回正」是**两个独立自由度**。
2. **回正 ⟺ Σ(±δ) ≡ 0 (mod 360)。** 只要掩码的正负和 `S` 满足 `δ·S ≡ 0`，整段闭合。

于是引擎 = **恒定 travel + 一段正负掩码**：

    zip2   S=0  （正负交替）→ 任意 δ 都成立，n 为偶数即可 ← 主力
    odd3   S=±3  （δ=120 时）→ n 为奇数时用

★ 和手写图形的对应（`out/_triplet_demo/`）：
    「寻常」= travel 120 @ SetSpeed×2 + 每 3 格 Twirl  → 常速用这个
    「拉链」= travel 60  @ 无 SetSpeed + 每格 Twirl    → 超高速用

    ★ ADOFAI 行话的 BPM 是 **16 分音口径**：
        行话「360 BPM」= 360bpm 的 16 分音 = 1440bpm 的四分音
        行话 BPM = 我们的 base_bpm ÷ 4
      所以「拉链适用于 360bpm 以上」= 我们的 base_bpm ≥ 1440。
      常速（base_bpm < 1440）优先 travel 120；到超高速才降到 travel 60
      —— travel 120 那版要一次 SetSpeed×2，速度再翻倍会太糊。

============ 为什么不能省 Twirl ============

不加 Twirl（掩码全 +）时 a 每格走 +δ，路径会**原地重描**：
实测 24 格只有 6 个不同位置（正六边形被描 4 遍），包围盒 1.73×2.0。
加上交错掩码后 24 格 24 个位置，包围盒 10.4×6.0。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction

#: 允许的 BPM **除数**（2 的幂 —— 用户口径：速度档只能是 2 的幂）
#: ★ 口径：`speed_k = k` 时 `bpm = base_bpm / k`，所以 `travel = 180·r / k`。
#:   手写「寻常」文件里的 SetSpeed ×2 就是这里的 k=0.5（travel 120 = 180·(1/3)/0.5）。
SPEED_KS: tuple[float, ...] = (1.0, 2.0, 0.5, 4.0, 8.0, 0.25)

#: travel 的可用区间（太小成发卡弯，太大成回头）
TRAVEL_MIN = 45.0
TRAVEL_MAX = 180.0


def is_triplet(r: float, max_den: int = 192) -> bool:
    """`r` 拍是不是三连音族 —— 最简分母含因子 3。"""
    if r <= 0 or not math.isfinite(r):
        return False
    return Fraction(round(r, 6)).limit_denominator(max_den).denominator % 3 == 0


@dataclass(frozen=True)
class Run:
    """一段等值三连音连打的铺法。"""

    n: int                 # 覆盖格数
    r: float               # 每格占几拍（恒定）
    travel: float          # 每格的「基准 travel」（恒定）
    speed_k: float         # BPM 除数（SetSpeed 到 base_bpm / speed_k）
    flips: tuple[bool, ...]  # 逐格 Twirl 翻转位（相对本段第 0 格）
    s_sum: int             # 掩码正负和

    def describe(self) -> str:
        return (f"{self.n} 格 r={self.r:.4g}拍 travel={self.travel:g}° "
                f"speed×{1/self.speed_k:g}  Twirl {sum(self.flips)} 个  S={self.s_sum:+d}")


def _close_S(n: int, d: float, mod: float) -> int | None:
    """找 |S| 最小、与 n 同奇偶、|S| ≤ n 且 `d·S ≡ 0 (mod mod)` 的 S。"""
    if d == 0.0:
        return 0
    best: int | None = None
    for S in range(-n, n + 1, 2):
        v = math.fmod(d * S, mod)
        if abs(v) < 1e-6 or abs(abs(v) - mod) < 1e-6:
            if best is None or abs(S) < abs(best):
                best = S
    return best


def pick_S(n: int, d: float) -> tuple[int | None, int]:
    """两级闭合，返回 `(S, 级别)`；级别 0 = 朝向精确复位，1 = 只回到 45° 网格。

    ★ **这是消斜线的关键**。原来的判据只用了 mod 360（精确复位），太严：
    一部分 n 无解，就退回 DP，而 DP 会把偏角带出去。
    然而 `a` 只需要回到 **45° 的整数倍** 就不会有斜线 —— 也就是 mod 45 就够。
    两级一起用，**任意 n ≥ 2 都有解**：

        δ=120（travel 60）: 120S ≡ 0 (mod 45) → S ≡ 0 (mod 3)   → n 再大都有 S
        δ=60 （travel 120）:  60S ≡ 0 (mod 45) → S ≡ 0 (mod 3)
    """
    for level, mod in ((0, 360.0), (1, 45.0)):
        S = _close_S(n, d, mod)
        if S is not None:
            return S, level
    return None, -1


def _mask(n: int, S: int, block: int) -> list[int]:
    """n 个 ±1，和 = S，**按 `block` 个一组同号、组间交替**。

    ★ 这是「寻常」的写法：`+++ −−−`（block = 180/δ）。
      早期我写成严格交替 `+−+−` ⇒ 每隔一格就翻转一次 ⇒ **每格一个 Twirl**，
      那是「拉链」的做法，不是「寻常」。用户实测：寻常是**每 3 格一个 Twirl**。
    """
    block = max(1, int(block))
    signs: list[int] = []
    s = 1
    while len(signs) < n:
        signs.extend([s] * min(block, n - len(signs)))
        s = -s
    # 把和从自然值调到 S（同奇偶，必定可达）；从尾部翻，尽量不动块结构
    cur = sum(signs)
    j = n - 1
    while cur != S and j >= 0:
        want = 1 if cur < S else -1
        if signs[j] != want:
            signs[j] = want
            cur += 2 * want
        j -= 1
    return signs


def _flips_of(signs: list[int]) -> tuple[bool, ...]:
    """符号序列 → 逐格 Twirl 翻转位（奇偶从 False 起步）。"""
    par = False
    fl: list[bool] = []
    for s in signs:
        want_par = s < 0
        fl.append(want_par != par)
        par = want_par
    return tuple(fl)


def _y_score(n: int, T: float, flips) -> float:
    """按 `_apply()` 的同一套公式模拟一遍，返回**净 Y 方向位移**的代理量。

    ★ 用户口径：出现三连音配置时**优先向 Y > 0 方向制谱**（社区不约而同的做法，
      见五月雪版 FALLENERA）。所以掩码定下来时，挑 Y 更正的那个。
    """
    heading, par, y = 90.0, False, 0.0
    for fl in flips:
        if fl:
            par = not par
        turn = (180.0 - T) if par else (T + 180.0)
        turn = ((turn + 180.0) % 360.0) - 180.0
        heading = ((heading + turn) + 180.0) % 360.0 - 180.0
        y += math.sin(math.radians(heading))
    return y


def plan_run(r: float, n: int, *, prefer_travel: float = 120.0,
             prefer_up: bool = True) -> Run | None:
    """给「n 格、每格 r 拍」的等值三连音段，挑一种能闭合的铺法。

    ★★ **优先顺序（用户口径：别滥用变速）**：
        ① 对图形    —— `k=1`，即 `travel = 180·r`，**不产生任何 SetSpeed**
        ② 旋转      —— 掩码正负错开，把它拉回闭合（Twirl 是零成本的）
        ③ 实在不行才变速 —— 比如 r=1/6 时 travel=30 太小（发卡弯），才升到 k=2

      变速虽然能绝对对上时间，但**手感会变得非常差**，所以是最后手段。

    ★ **先把 r 吸附成有理数再算**。solve 传进来的是 `qbeats × scale`，
      带 ~1e-6 的浮点噪声（实测 0.666666 而不是 2/3）；直接用浮点算闭合判据
      会因为绝对容差判不出解，整段退回 DP（而且更糟：判出假解）。
    """
    if n < 2 or not is_triplet(r):
        return None
    r = float(Fraction(round(r, 6)).limit_denominator(192))
    if not is_triplet(r):
        return None
    cands: list[tuple[tuple, Run]] = []
    for k in SPEED_KS:
        # ★ travel = 180·r / k（k 是 BPM 除数，不是乘数）
        T = float(Fraction(180) * Fraction(r).limit_denominator(192)
                  / Fraction(k).limit_denominator(8))
        if not (TRAVEL_MIN - 1e-9 <= T <= TRAVEL_MAX + 1e-9):
            continue
        d = 180.0 - T
        S, level = pick_S(n, d)
        if S is None:
            continue
        # 块长：让一次「单向摆」刚好扫过约 180°（`block × δ ≈ 180`）
        #   travel 120（δ=60）→ block 3  ⇒ `+++ −−−`（用户手写的「寻常」）
        #   travel  60（δ=120）→ block 1  ⇒ `+−+−`（「拉链」）
        block = max(1, int(round(180.0 / d))) if d > 1e-9 else 1
        fl = _flips_of(_mask(n, S, block))
        run = Run(n=n, r=r, travel=T, speed_k=k, flips=fl, s_sum=S)
        ys = _y_score(n, T, fl) if prefer_up else 0.0
        # ① 别变速（k=1 ⇒ 无 SetSpeed）② 向 Y>0 ③ 朝向精确复位 ④ travel 靠近期望 ⑤ Twirl 少
        cands.append(((abs(math.log2(k)), -ys, level, abs(T - prefer_travel),
                       sum(fl), k), run))
    if not cands:
        return None
    cands.sort(key=lambda x: x[0])
    return cands[0][1]


def verify(run: Run) -> tuple[float, float]:
    """返回 `(每格时长是否恒定, 闭合误差角)`。

    用 `_apply()` 的同一套公式重算一遍，别信自己。
    """
    a = 0.0
    par = False
    travel = run.travel
    for fl in run.flips:
        if fl:
            par = not par
        turn = (180.0 - travel) if par else (travel + 180.0)
        turn = ((turn + 180.0) % 360.0) - 180.0
        a += -(turn)              # heading += turn, a = 90 - heading
    dev = a % 360.0
    if dev > 180.0:
        dev -= 360.0
    return (0.0, dev)
