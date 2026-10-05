"""求解层：onset 序列 -> angleData / actions。

======================= v3：直线优先（按实测反馈重写）=======================
v2 的毛病（用户实测）：**整张谱面 0% 是直线**。根因是基准音值被设成 90°
（`target_travel=90`），于是"最常出现的那个音值"全部映射成 90° 拐角，
没有任何一层是 180° 直线；而真谱恰好相反。

真谱实测（社区语料 296 个谱面 / 794797 层，复现脚本 `tools/_analyze_straight.py`）：

    travel == 180°（直线）占 **37.7%**，是绝对第一，中位数谱面 36.0%
    90°/270° 各 8.8%/7.8%（成对出现 —— Twirl 翻的是转向符号）
    30°/330° 各 7.6%/7.2%，150°/210° 3.5%/3.7% …
    回头（travel<15 或 >345）中位 0.7%
    SetSpeed 事件密度 1%~12%

所以本版把优化目标写成：

    最大化 (直线层数 - λ × SetSpeed 事件数)

λ = `straight_weight`（UI 的「直线优先」）。做法是两层搜索：

  1) **选参考音值 C**（"一条直线 = 几分音符"）。候选 = 实际出现的音值及其 2/3 倍，
     约束 base_bpm = midi_bpm / C 落在 [bpm_min, bpm_max]。
  2) **DP 定每层的速度档 k**（行星速度 = 1/k）。角行程 travel = 180° × (Δt/C) / k。
     · k = 1  → 行星匀速，不产生任何事件
     · k = Δt/C → travel 正好 180° = **直线**（行星按 1/k 减速）
     DP 在"直线 +1"和"事件 -λ"之间权衡，自然得到"能直线就直线、宁可少插事件"。

    C 与 k 一组，等价于：**让音乐里最常出现的音值变成直线**，
    长音用 1/2、1/3、1/4 的速度档接住（这就是真谱里那些 snails 在做的事）。

模型（游戏源码 + 语料实测双向验证，见 docs/02、docs/03）：

    转角      turn_i = s_i × (travel_i − 180°)       # s_i = ±1
    航向      heading_i = heading_{i−1} + turn_i      # heading = exitangle
    坐标      tile_{i+1} = tile_i + 2R × u(heading_i)
    a_i = (90 − heading_i) mod 360

红线（游戏 scrLevelMaker.CalculateFloorEntryTimes）：
    angleMoved <= 1e-6 || >= 2π  且非中旋  =>  强制 2 拍（回头方块）
本生成器**完全不使用中旋 999**。

第一格（用户实测）：真谱 95.6% 的 `angleData[0] == 0`，而
    travel_0 = (180 − a[0]) mod 360 = 180°
即"开局那一格一定是直线"。所以本版**强制第一层 travel = 180、angleData[0] = 0**，
无论用户在 UI 里怎么调。见 `FIRST_TRAVEL`。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .onsets import Onset
from .path import Path, build_path  # noqa: F401  (build_path: 兼容便捷入口)
from .snowflake import Snowflake

# 180° 对应 60000/bpm 毫秒
ANG_DEG_PER_MSEC_BEAT = 1000.0 / 3.0        # travel = dt * bpm / (1000/3)

# 游戏硬红线之外的工程余量（避免落到 mod 边界上）
TRAVEL_HARD_MIN = 2.0
TRAVEL_HARD_MAX = 358.0
# 软约束：近回头（画出来是发卡弯）也尽量避免
TRAVEL_SOFT_MIN = 20.0
TRAVEL_SOFT_MAX = 340.0

#: 「暂停节拍」至少要有这么多拍，否则这个等待等于没用（会被当成 0 拍）
#: 见 `solve()` 里 Pause 格的速度档判定。
PAUSE_MIN_BEATS = 0.25

#: 允许的速度档。**只用 2 的幂**（用户硬规则：方便解谱/读谱）。
#: 这里的 k 是 **BPM 除数**：该层 BPM = base_bpm / k，行星速度 = 1/k。
#:   k = 1/2 → 行星快一倍（×2）      k = 2 → 行星慢一半（×1/2）  … 上限 ×8
#: 密集段用 k<1（加速，把小音值推成直线），稀疏段用 k>1（减速，把长音压回合法区间）。
SPEED_TIERS: tuple[float, ...] = (1 / 8, 1 / 4, 1 / 2, 1.0, 2.0, 4.0, 8.0)
SPEED_POW2 = SPEED_TIERS
SPEED_MAX = 8.0

#: 判定「这一层是直线」的容差（度）。0.05° 对应 0.1ms 量级，远小于一帧
STRAIGHT_TOL = 0.05

#: 第一层（开局站位）的固定角行程。真谱 95.6% 如此
FIRST_TRAVEL = 180.0

TWIRL_MODES = ("off", "accum", "steer", "alternate")

#: 「直线优先」三档：一条直线抵多少个 SetSpeed 事件
STRAIGHT_PRESETS = {"少": 3.0, "平衡": 1.5, "多": 0.7}


@dataclass
class SolveParams:
    base_bpm: float = 0.0            # 0 = 自动（由参考音值推出）
    beat_beats: float = 0.0          # 手动指定「一条直线 = 几拍」；0 = 自动
    straight_weight: float = 1.5     # 「直线优先」λ
    speed_penalty: float = 0.35      # 每偏离基准速度一个八度的代价（抑制 ×8 这种极端档）
    #: ★★ 速度档代价**只按前 N 个八度算**（`0` = 老口径，不封顶；`docs/72` §6）。
    #:   为什么需要：`|log2 k|` 不封顶时，**短格想走直就必然要多花八度** ——
    #:   实测 r=1/8（32.609ms 的格）：直线 `travel=180 k=1/8` 得 1−0.35×3 = **−0.050**，
    #:   而「发卡弯」`travel=22.5 k=1` 得 **0.000** ⇒ **走直反而输 0.05**，
    #:   于是整条路被发卡弯铺满（直线率 68% → 8.5%、每格占地 354 → 3.8 格²）。
    #:   封到 2 个八度后直线 +0.30 > 发卡 0.00。
    #:   ★ **默认 0 = 老口径**（`docs/25` §5.3 的先例），只在 `|log2 k| > N`
    #:   （即 ×8 / ÷8 档）时与老口径不同 ⇒ 影响面很窄。
    #:   ★ 补格（`fill_div > 0`）会**自动**把它设成 2.0 并上屏说明（不许静默）——
    #:   因为补格正是造出 r=1/8 短格的那件事。
    speed_penalty_max_oct: float = 0.0
    #: ★★ **降速档的额外代价**（`k < 1`，即「比基准慢」的那几档）。
    #:
    #:   为什么需要它（语料实测，`docs/13` §5 把它列为**头号病灶**）：
    #:   人类谱**几乎不用比基准更慢的速度档** —— P12~P15 的 45 张里 `0.5x`
    #:   只占 0.6~5%，而我们的产物长期是 **28~32%**。P14 同曲参照
    #:   `Mad Piano Party` 两张：`0.5x` 2.74% / `1x` 1.95%。
    #:
    #:   代价在这里：`gain = 直线?1:0 − speed_penalty·|log2 k|`。
    #:   音头等间隔（`r ≡ 0.5`）时 —— 这正是 8 分音符密集段的常态 ——
    #:       `travel=180 k=0.5`  gain = 1 − 0.35 = **+0.65**   ← 把速度降一半换直线
    #:       `travel= 90 k=1  `  gain = 0 − 0    = ** 0.00**
    #:   于是**整条路都靠「降速换直线」铺成**（直线率 79%，与真人的 38~41% 差一倍）。
    #:   而真人的选择是**照常转向 90°**（Renewed：`v=0.5` 63.7% 全在 `k=1`）。
    #:
    #:   ⇒ 这个值 = **每慢一个八度**额外扣的分（在 `speed_penalty` 之外）。
    #:   实测 `r=0.5` 要翻盘需要 > 0.65；`2.0` 左右即可让 8 分密集段
    #:   落成「90° 阶梯 + 少量 180°」，与语料的 90°(35%)/180°(41%) 同族。
    #:
    #:   ★ **默认 0.0 = 老口径逐字节不变**（与 `speed_penalty_max_oct` 同一纪律）。
    slow_speed_penalty: float = 0.0
    speed_switch_penalty: float = 3.0  # 换一次速度档的代价（人类谱不会到处 SetSpeed）
    #: ★★ 用户 2026-10：「当前算法**某些时候会贪心地倾向于原地打转**，建议**增加原地惩罚**，
    #:   让算法可以更倾向于**向规划的方向铺设轨道**而不是原地打转」。
    #:   口径：每格对「前进量」的贡献 = `cos(travel − 180)` ——
    #:     直线 180° ⇒ +1 · 135°/225° ⇒ +0.707 · **90°/270° ⇒ 0** · 45°/315° ⇒ −0.707
    #:     · 回头方块（0°/360°）⇒ −1。
    #:   为什么要这一项：一长串 90° / 270° 的 travel 会让行星**绕一个小正方形回到原点**
    #:   （净位移 0 = 原地打转），而老的 `gain` 只看「是不是正好 180°」（0/1 二值），
    #:   于是 90° 与 135° 同分、与 270° 同分 ⇒ 在音乐时值凑不出直线格时，
    #:   算法会随手挑那些「转圈回到原点」的写法。
    #:   `0` = **关掉**（回到 0/1 二值口径，逐字节不变，供 A/B 与回归）。
    #:   只影响「选哪个档/哪个角」，**不动时长**（时长由 (r,k) 唯一决定）⇒ 时序零影响。
    #:   ★★ 实测（三个样本，2026-10）：把这一项当成**逐条 DP 的加分**效果不好 ——
    #:     `0.2` 会把直线率拉低 4~5 个点、原地打转反而变多；`≤0.1` 又什么都改不动
    #:     （DP 是离散的，够不到阈值）。**真正解决问题的是图形层的原地惩罚**
    #:     （`core/figures.score` 的 `waste`），那一项能把 Automaton_Waltz 的
    #:     100+ 格 90° 方阵直接拆掉。所以这一项**默认 0（关）**，只留作 A/B 旋钮。
    inplace_penalty: float = 0.0
    #: ★★ **原地惩罚（图形层，默认开）** —— 用户 2026-10 那条建议的**主落点**。
    #:   关掉 = `core/figures.score` 回到「只看 0/1 的 Y>0」那条老口径
    #:   （A/B 与冻结哈希用；`tools/_ladder_freeze.py` 里钉了一组）。
    #:   具体口径见 `core/figures.py::score` 里 `waste` 那一段（每 3 格白转
    #:   ≈ 一个 SetSpeed 的代价）。
    inplace_waste: bool = True
    speed_min_run: int = 6           # 一个速度档至少连续用这么多层，短于此的段并给邻居
    use_pause: bool = True           # 长休止用 Pause 事件，不要硬扭轨道去填时间
    # ★ 用户口径（2026-10）：**需要等待的拍优先用暂停节拍，不要用减速档**。
    #   间隔 > `pause_min_beats`（拍）就写成「travel 180 直线 + Pause 补足时间」。
    #   `pause_min_beats=1.0` 等价于「只要比一拍长就暂停」——
    #   实测这样「直线率」从 47~57% 回到 70~93%，行星速度表里**不再出现减速档**，
    #   而且时序误差更小（见 docs/24 §2 的 A/B 表）。
    #   `pause_min_beats ∈ (1, r]` 的间隔仍会交给 DP（那种情况才会用减速档）。
    #   ★★ 2026-10 用户改默认：**1.0 → 4.0**（「pause_min_beats=1 默认将等于 4」）。
    #   也就是「1~4 拍的等待重新交回 DP」，只有超过 4 拍的长休止才走 Pause。
    #   副作用：`tests/golden/off_hashes.json` 的基线**跟着重冻**（它记的是
    #   「当前默认参数下的产物」），并在那里补了一条**显式钉死 1.0** 的用例。
    pause_min_beats: float = 4.0
    # ★ 用户口径（2026-10）：「允许设置最小角度，避免程序生成过小的角度排版影响观感」。
    #   非双押格（含 DP 拆出来的剩余格）的 `travel` 不得低于它，单位**度**。
    #   默认 20°，与旧常量 `TRAVEL_SOFT_MIN` 同值（以前是写死的，现在是参数）。
    #   双押的「薄格」`θ`（15°/30°）与中旋折返格是机制需要的形状，按 rules 豁免。
    travel_min: float = 20.0
    # ★ 用户口径（2026-10，第二版）：「**最小夹角不可以在 30° 以下，最大夹角不能
    #   超过 270°**」—— 所以最小角度之外还要一条**上界**。
    #   `0` = **不设上界**（老口径：软上界 `TRAVEL_SOFT_MAX=340`、硬上界 358）
    #   ⇒ 默认下逐字节不变（`tests/golden/off_hashes.json` 机器验收）。
    #   `> 0` = 选档时候选 travel 一律不得超过它；数学上必有解
    #   （合法窗口 [travel_min, travel_max] 的比值 ≥ 9 > 2，而档位是 2 的幂
    #     ⇒ 里面必有一个档），取不到才退到「越界最少」并把违规交给 `rules` 报出。
    travel_max: float = 0.0
    speed_tiers: tuple = SPEED_TIERS
    bpm_min: float = 80.0
    bpm_max: float = 400.0
    allow_set_speed: bool = True
    allow_twirl: bool = True
    emit_twirl: bool = True          # 由生成器写 Twirl（用户自己写时关掉）
    quantize_rhythm: bool = True     # 把 Δt 吸附到「音值网格」（按几分音符写）
    quantize_max_beats: float = 4.0  # 超过这么多拍的间隔按「休止」处理，保持精确
    ppqn: int = 480                  # MIDI 的 division
    midi_bpm: float = 0.0            # MIDI 基准拍速（用于音值折算）
    twirl_mode: str = "alternate"    # off / accum / steer / alternate
    twirl_limit_deg: float = 240.0   # accum 模式：累积转角超过它就翻一次
    steer_noflip_penalty: float = 0.35   # steer：倾向"不翻"，越大图标越少
    steer_centroid_w: float = 0.30       # steer：向外铺开的权重
    steer_lookback: int = 48
    planar_radius: float = 1.0

    # ---- 节奏型模板（主体路径；匹配不上才走上面的 DP 兜底）----
    use_templates: bool = True
    template_tol: float = 0.006
    #: 模板会把该格时长锁成「音值×拍」；实际 Δt 离它超过这么多毫秒就不套模板
    template_timing_tol_ms: float = 0.5
    #: True = 只在 DP 会给出**非直线**的地方套模板（保住「直线为主」红线）
    template_only_nonstraight: bool = True
    #: 模板只用在**短段落**里：所在段落时长超过它就交回 DP（那边会用速度×2 走直线）
    template_max_span_s: float = 10.0
    #: 段落怎么切：Δt 超过这么多毫秒算「休止」，段落断开（300ms ≈ 乐句级）
    template_rest_ms: float = 300.0
    #: [(floor 起始下标, Template)] —— 由 solve() 填，_finish() 用来锁 Twirl
    template_spans: list = field(default_factory=list)
    #: ★ v0.2 项5：用**时间戳窗口 DP** 做全局最优对位（替代贪心最长优先），
    #:   覆盖率更高。关掉则回退旧的贪心 `templates.match`。
    template_dp: bool = True
    #: ★★ 迭代 2.2（用户 2026-10）：**把「折弯循环」排版当成求解器的第一权重**。
    #:
    #: 用户原话：「你需要学习这个采音的排版设计。目前的生成逻辑中有关于这个排版的
    #: 生成路径，你需要做的是**把这个排版作为求解器第一权重使用的路径**。」
    #:
    #: 打开时额外加载 `patterns/templates_stair.json`（`travel = 30·60·90×7` +
    #: 隔位 Twirl、一个循环正好一小节），它的 `weight = 100` ⇒
    #: ① 贪心 `match()` 里**第一个被尝试**；② DP 在**覆盖率平手**时它赢。
    #:
    #: ★ **默认关** —— 按项目惯例（`docs/25` §5.3「原逻辑是保留的，新逻辑单开一个
    #: 选项」）。关着时模板库与排序与老版本**逐字节一致**，`tests/golden` 不动。
    #:
    #: ★★ 用户 2026-10 的**定论**（不要再改成默认开）：
    #: 「这个**蛇形主要适用于变体**，这种**匀速 4/4 点击还是优先直线**比较好」
    #: ⇒ 折弯循环**只做变体**；匀速 4/4 的场景走直线优先（`straight_weight`）。
    stair_first: bool = False

    # ---- 三连音引擎（长/短三连音段，见 core/triplet_engine.py）----
    #: 总开关。把「等值三连音连续段」铺成恒定 travel + 交错 Twirl 掩码 ⇒ 匀速 + 回正
    use_triplet_engine: bool = True
    #: 段长 ≥ 这个就交给引擎（低于它让模板/DP 处理）
    triplet_min_tiles: int = 2
    #: 段长 ≥ 这个时引擎**优先于模板**（长的归引擎，短的归三角形）
    triplet_engine_min: int = 4
    #: 判定「同一个音值」的容差（拍）
    triplet_tol: float = 0.01
    #: [(onset 起始下标, Run)] —— 由 solve() 填，_finish() 用来锁 Twirl
    engine_spans: list = field(default_factory=list)

    # ---- 自然闭合段（零事件直铺）----
    #: 总开关。一段音值若「按 travel = 180·音值 直接铺、不加任何 Twirl」就能闭合
    #: （Σtravel − 180n ≡ 0 mod 360），那就**原样直铺** —— 这也是 README 的两条不变量。
    #: 典型：`16 16 8 8` → travel `60·60·120·120`，Σ=360、180n=720、差 −360 ≡ 0 ✔
    #: ★ 优先级最高：引擎和模板都要给它让路。用引擎去"规范化"这种段是错的
    #:   —— 本来闭合的东西被拆开再用 Twirl 拼回去，路径就不再是原来那条（就是斜线的来源）。
    use_natural_spans: bool = True
    #: 自然段最长找多少格（防止 O(n·L) 扫描过慢）
    natural_max_tiles: int = 24
    #: 自然段的 travel 允许区间
    natural_travel_min: float = 45.0
    natural_travel_max: float = 180.0
    #: 由 solve() 填：{onset 下标: travel} —— 自然闭合段的逐格 travel
    natural_hit: dict = field(default_factory=dict)
    natural_reuse_speed: bool = True
    # ★ 一个自然段要产生 SetSpeed 就必须**够长**才划算。
    #   1~2 格的段进出各弹一次速度，等于白花两个事件换一个跟 DP 没差的图形。
    #   实测（Automaton_Waltz, fill+snow）：
    #     0 → 222 个 SetSpeed；4/8 → 192（但破双射，雪花段多 2 拍）；12 → 188 ✔；24 → 190
    natural_speed_min_tiles: int = 12
    # ★ 速度档"粘性"：DP 格能沿用当前档画出来就不换档（用转角去贴）。    #   ⚠ **默认关闭**：它会把 SetSpeed 从 222 降到 192（−13%），但会打破
    #     `core/verify.py` 的双射校验 —— 写入文件被 vendored parser 反解时，
    #     雪花段（@709:72格）的总时长多出正好 2 拍（370bpm → 324.5ms）。
    #     初步判断是雪花覆盖层 `_apply_snowflakes` 依赖覆盖前的 travel，
    #     粘性改了 travel 之后它的匀速推导对不上。修好雪花那一层再开。
    sticky_speed: bool = False
    # ★ 基准 BPM 吸附到整数。默认关（见 solve() 里的实测：OGG 源上会炸时序）。
    snap_base_bpm: bool = False
    # ★ 对音阶梯（`docs/25`）：**并列的第二条落点决策路径**，默认关。
    #   关掉 = 今天的老路径（`_dp_tiers` + `_plan_twirls`），且必须**逐字节相同**
    #   （`tests/golden/off_hashes.json` 冻结哈希机器验收）。
    #   打开 = 逐音降级：图形 → 当前档 → 加 Twirl 镜像 → 变速后重跑 1/2/3 → Pause。
    aggressive_pick: bool = False
    #: ★ 绕圈偏好（用户 2026-10）：「**速度偏低时优先绕外圈（cbpm < 400），
    #: 速度较快时优先绕内圈**」。`auto` = 按 `ladder_outer_cbpm` 自动；
    #: `always` / `never` = 手动钉死。注意它是**偏好**：第 4 级先按偏好试一遍，
    #: 都不行再放开两半兜一次，不会因为偏好把整格逼进第 5 级。
    ladder_outer_mode: str = "auto"
    #: 绕圈偏好的 cbpm 分界（`auto` 模式下生效）。默认 400。
    ladder_outer_cbpm: float = 400.0
    #: 阶梯第 4 级的档位顺序：`straight`（保直线优先，默认）/ `switch`（少换档优先）
    #: / `inner`（内圈优先）。见 `core/ladder.tier_order`。
    ladder_tier_order: str = "straight"
    # ★ 闭合图形使用策略（用户 2026-10）：`> 0` 更倾向闭合、`< 0` 更不倾向、`0` 不干预。
    #   由 `figures.choose` / `score` 读取，插在「直线格数」之后当一条实打实的偏好。
    closed_figure_bias: int = 0
    # ★ 重叠闭合图形：逐格 PositionTrack 错开（只动渲染，时序零影响）。
    #   ★★ 2026-10 用户：「**方块位置偏移的选项可以开局为关闭了**」
    #   ⇒ 默认改成 **False**（以前是 True）。它只写渲染事件（不改 travel/bpm），
    #      所以对时序零影响；但默认开着会在导出里塞一堆 PositionTrack，
    #      而且会对「叠在一起的圈」做主观错位 —— 让用户自己决定要不要。
    use_position_track: bool = False
    pos_track_step: float = 0.22
    # 轨道出现动画提前 8 拍 → 8 拍以内的重合靠"出现先后"就分得清，
    # 只有隔得更远的重合格才需要 PositionTrack 错位。
    pos_track_min_beats: float = 8.0
    sticky_travel_slack: float = 45.0

    # ---- 角度回正（项1，见 core/straighten.py）----
    #: 总开关（用户口径：默认开启）。只在**真正存在长斜轨**时才动手。
    straighten: bool = True
    #: 回正参数（2026-10 起接到 UI 上 —— 之前**一个都没暴露**，用户只能看着它「不动」）
    #: `straighten_min_run=8` 是硬门槛：连续 ≥8 格 deviation > theta 才算「斜轨」。
    #: 实测三首样本里 FallenEra / MemoryLocked **一段都凑不出来**
    #: （最大 deviation 30°/45°，但连续段最长只有 2 格）—— 那种情况下不动手是**对的**，
    #: 不是 bug；想让它更积极就把 min_run 调小。
    straighten_theta: float = 20.0
    straighten_min_run: int = 8
    #: 一个 Twirl 图标抵多少「度·格」的偏轴停留（越大越省 Twirl）
    straighten_flip_penalty: float = 180.0
    #: heading 量化网格（度）
    straighten_grid: float = 0.5
    #: 收益小于它就不采纳（避免无意义抖动）
    straighten_min_gain: float = 1.0
    #: 回正统计（由 _finish 填，供 UI/调试）
    straighten_meta: dict = field(default_factory=dict)
    template_flips: list = field(default_factory=list)
    # ★ 模板多轮时是否逐轮重放 Twirl。**默认 True 是对的**（只写一轮会让第 2 轮
    #   之后全部错位），但它会改变段内奇偶，进而打乱雪花 `_apply_snowflakes`
    #   的「段前拨正/段尾拨回」——实测 Automaton_Waltz 的雪花段总时长会多 2 拍，
    #   第三方 parser 双射校验 FAIL。修雪花那一层之前先用它做 A/B。
    template_all_rounds: bool = True
    dp_k: dict = field(default_factory=dict)
    #: 由 solve() 填：[(onset 起始, 格数)]
    natural_spans: list = field(default_factory=list)

    # ---- 双押预留槽位（a，见 docs/31 §5.2）----
    #: 要预留双押槽位的 **onset 下标**（由 sidecar 从双押轨音头推出来 / UI 可覆盖）。
    #: **默认空 ⇒ 完全不启用，老路径逐字节不变**（golden 哈希机器验收）。
    #:
    #: 预留做什么：求解**开始前**就把「这一格要出双押」当**约束**交给速度档 DP ——
    #:    ① 这一格的 k 强制跟上一格一样 ⇒ **它自己不会写出 SetSpeed**
    #:       （参考谱 A 的硬规则：「调速一定要放在平着的格子上面」；
    #:       双押的薄格 `θ` 一旦同时是 SetSpeed 格就不合规）
    #:    ② 这一格进 `_blocked` ⇒ 图形逻辑（自然段 / 引擎 / 模板）不许占它
    #:    ③ 若这一步把它顶出 travel 硬范围，就退回原档并记进 `dp_reserve_lost`
    #:       （**不许静默**，状态栏要报）
    dp_reserve: tuple = ()
    #: ★★ 用户 2026-10 **定死**：「双押**不是**按毫秒均匀计算的，就是**一个在不同 bpm
    #:   窗口下均匀使用的多种规定写法**。**不能自定义角度**」。
    #:   `True`（默认）= 薄角按**规定写法表**取（`dp_angle.fixed_theta`：
    #:   `≥840→90°`（连续双押 ≥4 回 30°）· `≥300→30°` · `<300→15°`）；
    #:   `False` = 老口径（`dp_theta` 自定义 / `dp_skew_max_ms` 预算反解），**保留不删**。
    #:   ⇒ 开着时 `dp_theta` 与 `dp_skew_max_ms` **不参与选角**（要如实在界面上说明）。
    use_fixed_dp_angle: bool = True
    #: ★★ 用户 2026-10：「**为三押添加开关，可以不一定生成双押**」⇒ 三档：
    #:   `0`（默认）= **拆三押**（押数 ≥3 ⇒ `[θ₁,θ₂,余量]`，行为与 `docs/48` 一致）
    #:   `1`       = **不拆**（押数 ≥3 的那一组按**双押**插一格，记 `three_off`）
    #:   `2`       = **跳过**（押数 ≥3 的那一组**连双押也不插**，记 `press_skipped`）
    #:   默认 0 ⇒ 老路径逐字节不变（`tests/golden/off_hashes.json`）。
    #:   ⚠ 只在 `use_fixed_dp_angle=True` 时有意义（老口径没有押数概念 ⇒ 一律 `three_legacy`）。
    three_press_mode: int = 0
    #: 薄角 θ（度）。**0 = 自动**。★ 只在 `use_fixed_dp_angle=False` 时才被采纳。
    dp_theta: float = 0.0
    #: ★★ 双押偏移预算（ms）—— 用户 2026-10：「**力求所有 bpm 下的双押产生的
    #:   偏移时值降低到 25ms 以内**」。Δ = (θ/180)·(60000/该格 bpm)，
    #:   `choose_thin` 在候选阶梯里挑满足它的最大薄角；`<= 0` = 关掉预算，
    #:   退回固定两档（<300→15°，≥300→30°）。
    #:   ⚠ 该口径**已被 2026-10 的「固定双押角度」取代**（见上）——
    #:   只在 `use_fixed_dp_angle=False` 时生效；开着时 Δ 降级为**情报**（上屏但不选角）。
    dp_skew_max_ms: float = 25.0
    #: 由 solve() 填：预留统计 {n, flat, lost}
    dp_reserve_meta: dict = field(default_factory=dict)

    # ---- 魔法阵（雪花）----
    #: 总开关。开启后，等间隔够长的段落会被换成一朵雪花。
    use_snowflake: bool = False
    #: 段长低于这个就绝对不用（用户口径：10 格）
    snowflake_min_tiles: int = 10
    #: 到这个段长就是 100% 用；之间线性升权（用户口径：10→0%，之后递增到 100%）
    snowflake_full_tiles: float = 48.0
    #: 允许的旋转阶数（用户口径：6 以下没有能看的）
    snowflake_n_rot: tuple = (6, 8, 10, 12)
    #: 判定「等间隔」的容差（ms）。段内 Δt 起伏不超过它才算一段
    snowflake_uniform_tol_ms: float = 3.0
    #: 由 solve() 填：[(onset 起始下标, Snowflake)]
    snowflake_spans: list = field(default_factory=list)
    #: 由 solve() 填：每格的 r（占几拍），雪花解 BPM 除数要用
    snowflake_rs: list = field(default_factory=list)
    #: 随机种子。同一份输入 + 同一个种子 = 同样的雪花形状（可复现）
    snowflake_seed: int = 20240213
    # ---- 雪花：放开参数（用户口径「允许使用参数的部位极少」）----
    #: 花瓣形状。`None` = 自动（穷举 `SHAPES_DETERMINISTIC` 取最花的那朵）。
    #: 取值见 `core.snowflake.SHAPES`：uniform / front / back / step / zigzag / random
    snowflake_shape: str = "auto"
    #: 一条花瓣最少走几步（整朵 = 2·arms·N 格）。默认 2（= 一去一回）。
    snowflake_min_arms: int = 2
    #: 是否优先「包围盒小」的候选（同分时）。关掉则只看「花不花」。
    snowflake_compact: bool = True
    #: 形状用固定种子（`False` = 穷举确定性搜索；`True` = 旧的随机采样路径）
    snowflake_random: bool = False
    #: 随机采样次数（仅 `snowflake_random=True` 有意义）
    snowflake_trials: int = 32

    # ---- 兼容旧字段（v2 的调用方还在传；已不使用）----
    target_travel: float = 180.0
    ref_quantile: float = 0.85
    band_min: float = 12.0
    band_max: float = 350.0
    lead_in_beats: float = 1.0


@dataclass
class Floor:
    travel: float          # 角行程（度）
    bpm: float             # 该层生效的 BPM
    twirl: bool            # 该层是否有 Twirl 事件
    turn: float            # 带符号转角（度）
    heading: float         # exitangle（度）
    angle: float           # angleData 值（度）
    speed_k: float = 1.0   # 速度档（行星速度 = base_bpm / bpm = 1/k）
    pause_beats: float = 0.0   # Pause 事件：这一格额外停几拍（只加时间）
    # ★ SetSpeed 的 angleOffset：本格走到第几度时该事件才生效（见 core/dp_offset）。
    #   0 = 本格起点生效（默认）。>0 时本格被切成「前段旧速 / 后段新速」两段。
    angle_offset: float = 0.0
    x: float = 0.0
    y: float = 0.0
    snowflake: bool = False    # 属于魔法阵（雪花）——速度档豁免 2 的幂规则
    snowflake_id: int = -1     # 第几朵（0,1,2…）
    # ★ 这一格由哪条「图形逻辑」拥有（角度双押插入时不许拆这些格，
    #   否则会把模板/自然闭合/引擎图形拆坏）。互斥，最多一个为真。
    template: bool = False     # 手写节奏型模板段
    natural: bool = False      # 自然闭合段
    engine: bool = False       # 三连音引擎段
    # ★ 双押**预留槽位**（a，见 docs/31 §5.2）：这一格是求解前就替双押留好的身位，
    #   档已钉死（自己不出 SetSpeed）、图形也已让路。角度双押插入时**应当**拆它。
    dp_reserved: bool = False


@dataclass
class Chart:
    base_bpm: float
    floors: list[Floor] = field(default_factory=list)
    first_onset_ms: float = 0.0
    meta: dict = field(default_factory=dict)

    @property
    def angle_data(self) -> list[float]:
        return [f.angle for f in self.floors]

    @property
    def n_twirl(self) -> int:
        return sum(1 for f in self.floors if f.twirl)

    @property
    def set_speed_floors(self) -> list[tuple[int, float]]:
        """需要输出 SetSpeed 的 (floor, bpm)：BPM 与上一有效值不同时。"""
        out: list[tuple[int, float]] = []
        cur = self.base_bpm
        for i, f in enumerate(self.floors):
            if abs(f.bpm - cur) > 1e-9:
                out.append((i, f.bpm))
                cur = f.bpm
        return out

    @property
    def n_speed_events(self) -> int:
        return len(self.set_speed_floors)

    def dp_pole_indexes(self) -> set[int]:
        """双押插入的「折返格 / midspin 格」下标（见 core.dp_midspin）。

        这些格子不是音乐意义上的方块，统计直线率/音值分布时要排除，
        否则插了几百个双押之后「直线率」会被无意义地拉低。
        """
        out: set[int] = set()
        for (ix, iy, _ib) in (self.meta.get("dp_pairs") or []):
            out.add(ix)
            out.add(iy)
        return out

    def report_travel(self, i: int, f) -> float:
        """统计用的 travel。

        双押插入会把**原格**的 travel 吃掉约 0.25°（`travel_f − Σs`），
        那点扰动对「是不是直线」无关紧要，但会让 `_is_straight` 判否。
        所以统计时还原成插入前的值（见 `core.dp_midspin.apply`）。
        """
        return (self.meta.get("dp_base_travel") or {}).get(i, f.travel)

    @property
    def straight_frac(self) -> float:
        dp = self.dp_pole_indexes()
        fs = [(i, f) for i, f in enumerate(self.floors)
              if i >= 1 and i not in dp]
        if not fs:
            fs = [(i, f) for i, f in enumerate(self.floors) if i not in dp]
        if not fs:
            return 0.0
        return sum(1 for i, f in fs
                   if _is_straight(self.report_travel(i, f))) / len(fs)

    def travel_hist(self, top: int = 8) -> list[tuple[float, int, float]]:
        import collections
        dp = self.dp_pole_indexes()
        fs = [(i, f) for i, f in enumerate(self.floors)
              if i >= 1 and i not in dp]
        if not fs:
            fs = [(i, f) for i, f in enumerate(self.floors) if i not in dp]
        c = collections.Counter(round(self.report_travel(i, f), 3)
                                for i, f in fs)
        n = max(1, len(fs))
        return [(v, k, k / n * 100) for v, k in c.most_common(top)]


# ---------------------------------------------------------------------------
def _grid_fit(rs: list[float], m: int = 12, tol: float = 0.02) -> float:
    """音值落在 `1/m` 拍网格上的比例。

    `m=12` 对应 15° 网格（`12r ∈ ℤ` ⟺ travel 是 15° 的倍数）；
    三连音族也能覆盖。TUF 语料实测 15° 网格覆盖 97.9%。
    """
    if not rs:
        return 0.0
    n = 0
    for r in rs:
        x = r * m
        if abs(x - round(x)) <= tol:
            n += 1
    return n / len(rs)


def _snap_bpm(v: float, tol: float = 0.004) -> float:
    """基准 BPM 吸附到**人写得出来的整数**。

    ★ 这一步**不影响时序**：`rs = dt·base/60000` 是在吸附之后才算的，
      而 travel 与 k 都按新的 base 重算，所以
          `travel/180 × 60000/(base/k) = rs·60000/base = dt`
      恒成立。几何上 travel 会等比缩放同一个比例（≤0.4%），可以忽略。

    实测会命中的例子：119.84 → 120、359.5 → 360、179.755 → 180。
    """
    if v <= 1.0:
        return float(v)
    n = round(v)
    if n > 1 and abs(v - n) / v <= tol:
        return float(n)
    # 常见手写值：整数的 1/2、3/4、2/3、4/3、3/2、2、1/3、1/4 … 倍
    for num, den in ((1, 2), (3, 4), (2, 3), (4, 3), (3, 2), (2, 1),
                     (1, 3), (3, 1), (1, 4), (4, 1)):
        c = n * num / den
        if c > 1.0 and abs(v - c) / v <= tol:
            return float(c)
    return round(float(v), 3)


def _snap_nice(v: float, rel_tol: float = 3e-4) -> float:
    """把浮点噪声（180.00018）吸附到人写得出来的值（180）。"""
    for cand in (round(v), round(v * 2) / 2, round(v * 3) / 3, round(v * 4) / 4,
                 round(v, 2), round(v, 1)):
        if cand > 1.0 and abs(cand - v) / v < rel_tol:
            return float(cand)
    return float(v)


def interval_stats(onsets: list[Onset]) -> dict:
    dts = sorted(onsets[i + 1].t_ms - onsets[i].t_ms for i in range(len(onsets) - 1))
    dts = [d for d in dts if d > 0.5]
    if not dts:
        return {"n": 0, "p50": 200.0, "p85": 200.0, "p95": 200.0, "max": 200.0, "min": 200.0}
    def q(p):
        return dts[min(len(dts) - 1, max(0, int(len(dts) * p)))]
    return {"n": len(dts), "min": dts[0], "p50": q(0.50), "p85": q(0.85),
            "p95": q(0.95), "p99": q(0.99), "max": dts[-1]}


# ---------------------------------------------------------------------------
#  速度档 DP
# ---------------------------------------------------------------------------
def _is_straight(t: float) -> bool:
    return abs(t - 180.0) < STRAIGHT_TOL


def _tpl_is_triplet(t) -> bool:
    """这个模板是不是「三连音图形」—— 三角形家族，或音值里有分母含 3 的。

    ★ 三连音模板要**豁免** `template_max_span_s` 那道闸：三连音谱面天然段落长，
      闸掉它等于把三连音模板整个关死（实测 FallenEra 11 段 → 0 段）。
    """
    from fractions import Fraction
    if "三角形" in (getattr(t, "src", "") or ""):
        return True
    for x in t.notes:
        if Fraction(round(float(x), 6)).limit_denominator(192).denominator % 3 == 0:
            return True
    return False


def _tier_options(r: float, tiers, travel_min: float = TRAVEL_SOFT_MIN,
                  travel_max: float = 0.0) -> list[tuple[float, float]]:
    """给定 Δt 相对参考音值的倍数 r，返回候选 (travel, k)，优先软约束。

    k 一律取自 tiers（2 的幂）；连硬约束都出界时（超长休止 / 极短音符），
    退到「离 r 最近的 2 的幂」—— 这样 travel 会落在 127°~360°，绝不出现回头。

    `travel_min`（度）= 用户口径的**最小角度**：低于它的候选取不到就往下放宽
    （先放宽到硬下限，再退到最近的 2 的幂），保证任何 r 都有解。

    `travel_max`（度）= 用户口径的**最大夹角**（`> 0` 才生效；`0` = 老口径）：
    候选一律不得超过它。合法窗口 `[travel_min, travel_max]` 的比值 ≥ 9 > 2，
    所以**必含一个 2 的幂档**；真的取不到（r 极端）才退到「越界最少」的那一档
    —— 违规由 `rules.check_chart` 的 `travel_above_max` 报出，**不许静默**。
    """
    hi_soft = TRAVEL_SOFT_MAX if travel_max <= 0 else min(TRAVEL_SOFT_MAX, float(travel_max))
    hi_hard = TRAVEL_HARD_MAX if travel_max <= 0 else min(TRAVEL_HARD_MAX, float(travel_max))
    hard: list[tuple[float, float]] = []
    soft: list[tuple[float, float]] = []
    for k in tiers:
        t = 180.0 * r / k
        if TRAVEL_HARD_MIN <= t <= hi_hard:
            hard.append((t, k))
            if max(TRAVEL_SOFT_MIN, float(travel_min)) <= t <= hi_soft:
                soft.append((t, k))
    if soft:
        return soft
    if hard:
        return hard
    if travel_max <= 0:                       # 老口径：离 r 最近的 2 的幂
        k = 2.0 ** round(math.log2(max(r, 1e-9)))
        return [(180.0 * r / k, k)]
    # 新口径：连 hard 都是空的 ⇒ 挑「越界最少、且最接近 180」的那一档
    lo = max(TRAVEL_HARD_MIN, float(travel_min))
    hi = float(travel_max)

    def _pen(item: tuple[float, float]) -> tuple[float, float, float]:
        t, _k = item
        return (max(0.0, lo - t) + max(0.0, t - hi), abs(t - 180.0), _k)

    return [min(((180.0 * r / k, k) for k in tiers), key=_pen)]


def _spmo(p) -> float:
    """读 `SolveParams.speed_penalty_max_oct`（老 `SolveParams` 对象没有这个字段时当 0）。"""
    return float(getattr(p, "speed_penalty_max_oct", 0.0) or 0.0)


def _ssp(p) -> float:
    """读 `SolveParams.slow_speed_penalty`（老对象没有这个字段时当 0）。"""
    return float(getattr(p, "slow_speed_penalty", 0.0) or 0.0)


def _dp_tiers(rs: list[float], tiers, straight_weight: float,
              speed_penalty: float = 0.0, speed_switch_penalty: float | None = None,
              travel_min: float = TRAVEL_SOFT_MIN, inplace_penalty: float = 0.0,
              travel_max: float = 0.0, speed_penalty_max_oct: float = 0.0,
              slow_speed_penalty: float = 0.0):
    """逐层选速度档，最大化 Σ(直线 − 速度代价) − λ·Σ(档位切换)。

    `straight_weight`   = 直线优先度（也是每层的基础代价尺度）
    `speed_switch_penalty` = 换一次速度档的代价。人类谱不会到处 SetSpeed，
                             所以这个值越大，速度档越「成块」出现。
    `travel_min`        = 最小角度（度），透传给 `_tier_options`。
    `inplace_penalty`   = ★ **原地惩罚**（用户 2026-10）：每格再按
                          `cos(travel − 180)` 加减分（直线 = +1，90°/270° 直角 = 0，
                          回头 = −1）。见 `SolveParams.inplace_penalty`。
                          `0` = 老的 0/1 口径（逐字节不变）。
    `speed_penalty_max_oct` = ★★ **速度档代价只按前 N 个八度算**（`0` = 老口径，不封顶）。
                          见 `SolveParams.speed_penalty_max_oct`。
    返回 (travels, ks, straight_frac, event_frac)。
    """
    n = len(rs)
    if n == 0:
        return [], [], 0.0, 0.0
    switch_pen = straight_weight if speed_switch_penalty is None else speed_switch_penalty
    opts = [_tier_options(r, tiers, travel_min, travel_max) for r in rs]

    def gain(t: float, k: float) -> float:
        g = 1.0 if _is_straight(t) else 0.0
        # ★★ 原地惩罚（用户 2026-10：「算法会贪心地倾向于**原地打转**，建议增加
        #   原地惩罚，让算法更倾向于向规划的方向铺设轨道」）。
        #   每格的「前进量」= 新朝向与旧朝向的同向程度 = `cos(travel − 180)`：
        #     180°（直线）⇒ +1 · 135°/225° ⇒ +0.707 · 90°/270°（直角）⇒ **0**
        #     · 45°/315° ⇒ −0.707 · 0°/360°（回头方块）⇒ −1
        #   一长串 `90/270` 会让行星**绕正方形回到原点**（净位移 0 = 原地打转），
        #   而它们的 `cos` 恒为 0 ⇒ 现在打不过任何「更接近直线」的选择。
        #   ⚠ 只改**选哪个档/哪个角**，不动时长（时长由 (r,k) 唯一决定）⇒ 时序零影响。
        if inplace_penalty:
            g += float(inplace_penalty) * math.cos(math.radians(t - 180.0))
        if speed_penalty and k > 0:
            # ★★ `speed_penalty_max_oct > 0` ⇒ **代价只按前 N 个八度算**。
            #   为什么需要（`docs/72` §6 的根因）：`|log2 k|` 不封顶时，
            #   **短格想走直就必然要多花八度**，于是被罚到比「发卡弯」还低。
            #   实测 r=1/8（32.609ms 的格，= 补格落点）：
            #       直线 travel=180 k=1/8  gain = 1 − 0.35×3 = **−0.050**
            #       发卡 travel= 22.5 k=1   gain = 0 − 0     = **+0.000**  ← 赢 0.05
            #   于是整条路被 22.5°/67.5° 的发卡弯铺满（直线率 68% → 8.5%、占地 354 → 3.8）。
            #   封到 2 个八度后：直线 +0.30 > 发卡 0.00 ⇒ 走直。
            #   ★ 只在 `|log2 k| > 2`（即 ×8 / ÷8 档）时与老口径不同 ⇒ 影响面很窄。
            oct_ = abs(math.log2(k))
            if speed_penalty_max_oct > 0:
                oct_ = min(oct_, float(speed_penalty_max_oct))
            g -= speed_penalty * oct_
        # ★★ 降速档（k < 1）的**额外**代价：真人几乎不用比基准更慢的档
        #   （P12~P15 实测 0.5x 占 0.6~5%，我们 28~32%）。见 `SolveParams.slow_speed_penalty`。
        #   只罚 k < 1 ⇒ 加速档一个字节都不动（老口径 = 0.0 时本段不执行）。
        if slow_speed_penalty and 0.0 < k < 1.0:
            g -= float(slow_speed_penalty) * abs(math.log2(k))
        return g

    prev: dict[float, tuple[float, float | None]] = {}
    for t, k in opts[0]:
        prev[k] = (gain(t, k), None)
    backs: list[dict[float, float | None]] = []
    for i in range(1, n):
        cur: dict[float, tuple[float, float | None]] = {}
        bk: dict[float, float | None] = {}
        for t, k in opts[i]:
            g = gain(t, k)
            best = None
            for pk, (pv, _) in prev.items():
                v = pv + g - (0.0 if pk == k else switch_pen)
                if best is None or v > best[0]:
                    best = (v, pk)
            cur[k] = (best[0], best[1])
            bk[k] = best[1]
        prev = cur
        backs.append(bk)

    state = max(prev.items(), key=lambda kv: kv[1][0])[0]
    ks = [state]
    for i in range(n - 1, 0, -1):
        state = backs[i - 1][state]
        ks.append(state)
    ks.reverse()

    travels: list[float] = []
    n_str = 0
    n_ev = 0
    for i, k in enumerate(ks):
        t = next(tt for tt, kk in opts[i] if kk == k)
        travels.append(t)
        if _is_straight(t):
            n_str += 1
        if (i == 0 and k != 1) or (i > 0 and ks[i] != ks[i - 1]):
            n_ev += 1
    return travels, ks, n_str / n, n_ev / n


def choose_reference(beats: list[float], p: SolveParams, midi_bpm: float,
                     force: float = 0.0):
    """选「一条直线 = 几分音符」。

    候选：实际出现的量化音值，以及它们的 2/3 倍（让长音也能落到直线上）。
    目标：max `straight_frac − λ·event_frac`，
         同分优先"更直"→"事件更少"→"基准 BPM 更接近歌曲本身"。
    `force > 0` 时直接钉死参考音值（手动基准 BPM 走这条路）。
    返回 (C_beats, base_bpm, straight_frac, event_frac, ks)。
    """
    from . import rhythm
    if force > 0:
        C = float(force)
        rs = [b / C for b in beats]
        _, ks, s, e = _dp_tiers(rs, p.speed_tiers, p.straight_weight, p.speed_penalty,
                                        p.speed_switch_penalty,
                                        travel_min=p.travel_min,
                                        travel_max=float(getattr(p, "travel_max", 0.0) or 0.0),
                                        speed_penalty_max_oct=_spmo(p),
                                        slow_speed_penalty=_ssp(p))
        return C, midi_bpm / C, s, e, ks

    cands: set[float] = set()
    for b in beats:
        if b > p.quantize_max_beats:          # 休止不参与
            continue
        q = rhythm.quantize(b)
        cands.add(round(q, 6))
        for m in (2.0, 3.0, 4.0):
            v = q * m
            if v in rhythm.NOTE_VALUES:
                cands.add(round(v, 6))
    if p.beat_beats > 0:
        cands = {round(float(p.beat_beats), 6)}

    best = None
    for C in sorted(cands):
        if C <= 1e-6:
            continue
        bpm = midi_bpm / C
        if not (p.bpm_min - 0.5 <= bpm <= p.bpm_max + 0.5):
            continue
        rs = [b / C for b in beats]
        _, ks, s, e = _dp_tiers(rs, p.speed_tiers, p.straight_weight, p.speed_penalty,
                                        p.speed_switch_penalty,
                                        travel_min=p.travel_min,
                                        travel_max=float(getattr(p, "travel_max", 0.0) or 0.0),
                                        speed_penalty_max_oct=_spmo(p),
                                        slow_speed_penalty=_ssp(p))
        nat = abs(math.log2(max(1e-9, bpm / max(1e-9, midi_bpm))))
        key = (s - p.straight_weight * e, s, -e, -nat)
        if best is None or key > best[0]:
            best = (key, C, bpm, s, e, ks)
    if best is None:                                  # 没有任何候选合法
        C = float(p.beat_beats) or 1.0
        bpm = max(p.bpm_min, min(p.bpm_max, midi_bpm / C if midi_bpm > 0 else 180.0))
        if p.base_bpm > 0:
            bpm = p.base_bpm
        rs = [b / C for b in beats]
        travels, ks, s, e = _dp_tiers(rs, p.speed_tiers, p.straight_weight, p.speed_penalty,
                                        p.speed_switch_penalty,
                                        travel_min=p.travel_min,
                                        travel_max=float(getattr(p, "travel_max", 0.0) or 0.0),
                                        speed_penalty_max_oct=_spmo(p),
                                        slow_speed_penalty=_ssp(p))
        return C, bpm, s, e, ks
    _, C, bpm, s, e, ks = best
    return C, bpm, s, e, ks


def auto_base_bpm(onsets: list[Onset], target_travel: float = 180.0,
                  ref_quantile: float = 0.85, ppqn: int = 480,
                  midi_bpm: float = 0.0, bpm_hint: float = 0.0) -> float:
    """自动基准 BPM：优先读用户在生成页填的值 (`bpm_hint`，写进 JSON)。

    · `bpm_hint` 有效(>0)：<200 自动 x2，>=200 原样用；吸附到干净数。
    · 否则退回旧的 onset 间隔估速 (MIDI/TS 等无 hint 的工程)。
    """
    if bpm_hint and bpm_hint > 0:
        v = bpm_hint * 2.0 if bpm_hint < 200.0 else bpm_hint
        return _snap_nice(float(v))
    beats, _ = beats_of(onsets, ppqn, midi_bpm)
    if beats and midi_bpm > 0:
        p = SolveParams(straight_weight=STRAIGHT_PRESETS["平衡"], ppqn=ppqn,
                        midi_bpm=midi_bpm)
        _, bpm, _, _, _ = choose_reference(beats, p, midi_bpm)
        return _snap_nice(bpm)
    st = interval_stats(onsets)
    q = st["p85"] if ref_quantile >= 0.85 else st["p50"]
    return min(max(_snap_nice(target_travel * ANG_DEG_PER_MSEC_BEAT / max(1.0, q)),
                   20.0), 4000.0)


# ---------------------------------------------------------------------------
def beats_of(onsets: list[Onset], ppqn: int, midi_bpm: float):
    """onset 间隔的「拍数」（精确，来自 tick）+ 量化误差（ms）。"""
    n = len(onsets)
    if n < 2 or ppqn <= 0 or midi_bpm <= 0:
        return [], 0.0
    from . import rhythm
    beat_ms = 60000.0 / midi_bpm
    out: list[float] = []
    err = 0.0
    for i in range(n - 1):
        a, b = onsets[i], onsets[i + 1]
        dt = b.t_ms - a.t_ms
        if b.tick > a.tick:
            beats = (b.tick - a.tick) / ppqn
            err = max(err, abs(beats - dt / beat_ms) * beat_ms)
        else:
            beats = dt / beat_ms
        out.append(beats)
    return out, err


# ---------------------------------------------------------------------------
def _apply(floors: list[Floor], flips: list[bool], p: SolveParams):
    """给定每层的 Twirl 翻转位，算出 heading / angleData / tile 坐标。

    几何算法已抽到 `core/path.py::Path`（唯一真源），这里只负责构造 + 写回，
    保证「算法一处、调用多处」。
    """
    path = build_path(floors, flips, radius=p.planar_radius,
                      allow_twirl=bool(p.allow_twirl))
    path.commit_to(floors, write_twirl=True)
    return path.points


OVERLAP_R = 1.75


def _overlap_pairs(pts, recent: int) -> list[tuple[int, int]]:
    bad = []
    for j in range(len(pts)):
        for i in range(max(0, j - recent), j - 1):
            if math.hypot(pts[j][0] - pts[i][0], pts[j][1] - pts[i][1]) < OVERLAP_R:
                bad.append((i, j))
    return bad


def _reserve_dp_slots(rs: list[float], p: SolveParams,
                      blocked: set[int]) -> tuple[set[int], dict]:
    """挑出可以替双押**预留槽位**的 onset 下标（`docs/31` §5.2 的 a）。

    ★ 只在**这里**筛「不可能预留的」（被模板段 / Pause 格占住的）；
      真正的「钉档」在 `solve()` 的扫描循环里做 —— 因为只有那里才知道
      **上一格的生效速度档 `_cur_k`**（`sticky_speed` 会让它≠`ks[i−1]`，
      在循环外按 `ks` 推会推出一个并不成立的「不换档」）。

    返回 `(resv, meta)`；`resv` 为空 ⇒ 调用方一行都不执行。
    """
    meta = {"n": 0, "lost": 0, "lost_idx": []}
    idx = sorted({int(i) for i in (p.dp_reserve or ())
                  if 0 <= int(i) < len(rs)})
    resv: set[int] = set()
    for i in idx:
        if i <= 0 or i in blocked:
            meta["lost"] += 1
            meta["lost_idx"].append(i)
            continue
        resv.add(i)
    meta["n"] = len(resv)
    return resv, meta


def _merge_short_runs(ks: list[float], rs: list[float], min_run: int,
                      travel_min: float = 15.0,
                      keep_straight: bool = False,
                      travel_max: float = 0.0) -> list[float]:
    """把过短的速度档段并到邻居上。

    人类谱不会到处 SetSpeed —— 一个速度档至少要用一小段。比 `min_run` 层还短的段
    直接并给邻居（前提：并完之后 travel 还在合理范围内，不会变成回头方块，
    也不低于用户的**最小角度** `travel_min`）。

    ★ `keep_straight=True`（`docs/46`，**直拟合**那条路用）：并档**不许把直线格变少**。
      为什么：solve() 里一个档覆盖一整段图形，并档是"顺手的"；可直拟合是**一砖一音**，
      并档会强行改写邻居的角度 —— 实测会把「直线优先」这个参数**整个吃掉**
      （真数据上 w=3.0/1.5/0.7/0.3 全变成 83~83.6% 直线，参数形同虚设）。
      默认 `False` ⇒ `solve()` 那条路逐字节不变。
    """
    n = len(ks)
    if min_run <= 1 or n == 0:
        return ks
    lo = max(15.0, float(travel_min or 0.0))
    hi = 345.0 if float(travel_max or 0.0) <= 0 else min(345.0, float(travel_max))

    def straight_at(x: int, k: float) -> bool:
        return abs(180.0 * rs[x] / k - 180.0) < STRAIGHT_TOL

    out = list(ks)
    changed = True
    while changed:
        changed = False
        i = 0
        while i < n:
            j = i
            while j + 1 < n and out[j + 1] == out[i]:
                j += 1
            if j - i + 1 < min_run:
                if i > 0:
                    cand = out[i - 1]
                elif j + 1 < n:
                    cand = out[j + 1]
                else:
                    cand = out[i]
                ok = (cand != out[i]
                      and all(lo <= 180.0 * rs[x] / cand <= hi
                              for x in range(i, j + 1)))
                if ok and keep_straight:
                    ok = (sum(1 for x in range(i, j + 1)
                              if straight_at(x, cand))
                          >= sum(1 for x in range(i, j + 1)
                                 if straight_at(x, out[x])))
                if ok:
                    for x in range(i, j + 1):
                        out[x] = cand
                    changed = True
            i = j + 1
    return out


def _setspeed_floors(floors: list[Floor]) -> set[int]:
    """哪些层会写出 SetSpeed（该层 BPM 与上一层不同）。"""
    ss: set[int] = set()
    prev = floors[0].bpm if floors else 1.0
    for i, f in enumerate(floors):
        if abs(f.bpm - prev) > 1e-9:
            ss.add(i)
        prev = f.bpm
    return ss


def _setspeed_floors_pairs(floors: list[Floor]) -> list[tuple[int, float]]:
    """同 `_setspeed_floors`，但返回 (floor, bpm)。"""
    out: list[tuple[int, float]] = []
    prev = floors[0].bpm if floors else 1.0
    for i, f in enumerate(floors):
        if abs(f.bpm - prev) > 1e-9:
            out.append((i, f.bpm))
        prev = f.bpm
    return out


def _apply_snowflakes(floors: list[Floor], flips: list[bool], p: SolveParams) -> None:
    """把选中的段落整段换成魔法阵（雪花）。

    不改 `floors` 的长度 —— 段里有几格就是几格，雪花按这个格数生成。

    ★ **两遍式**（2026-10 重写，修「雪花段多 2 拍 / 324ms」的时序差）：

      雪花会给段内每格写一个**非 2 的幂**的速度档（绝对匀速的要求），于是段的
      **首尾立刻多出 SetSpeed**。旧写法只用「雪花之前」的 SetSpeed 集合去找
      「可以拨的直线格」，结果把 Twirl 拨到了**新长出来的 SetSpeed 格**上 ——
      `writer.build_actions` 一看「Twirl 与 SetSpeed 同格」就把它往后挪一格，
      而 **angleData 是按原位置算的** ⇒ 反解器少读一次奇偶翻转 ⇒ 下游整段偏 180°。
      实测 Automaton_Waltz 雪花段之后第 385 格起时序多 **324ms**（正是 2 拍 @370bpm）。

      所以现在：

      ① 预演：先算出「雪花写完之后的 BPM」，得到**未来的 SetSpeed 格集合**；
      ② 只在「整朵都合法（越界 / 解不出 k / 低于最小角度都算不合法）」时才落地，
         并且段前拨正 / 段尾拨回**只挑未来的 SetSpeed 之外、且不在任何雪花段内的直线格**。

    做法：
      · 段内 Twirl 一律清掉（会破坏几何），并记下进入该段时的奇偶
      · 逐格写 `travel`（由 Snowflake 给出）
      · 逐格解出 BPM 除数 `k = 180·r/travel` —— 这样每格 dt 精确等于音乐给的间隔，
        等间隔段落自然就是**绝对匀速**
    朝向 / angleData / 坐标不在这里算，交给后面的 `_apply()`。
    """
    spans = getattr(p, "snowflake_spans", None)
    rs = getattr(p, "snowflake_rs", None)
    if not spans or not rs or not floors:
        return
    n = len(floors)
    tmin = float(getattr(p, "travel_min", 0.0) or 0.0)

    # ---------------- ① 预演：未来的 BPM / SetSpeed 格 ----------------
    keep: list[tuple[int, int, int, list[float], list[float]]] = []
    bpm_after = [float(f.bpm) for f in floors]
    for sid, (start, spec) in enumerate(spans):
        L = spec.tiles
        if start < 0 or start + L > n:
            continue
        tv = [float(x) for x in spec.travels()]
        ks: list[float] = []
        ok = True
        for j in range(L):
            i = start + j
            t = tv[j]
            f = floors[i]
            base_bpm = f.bpm * f.speed_k
            r = rs[i - 1] if 0 < i <= len(rs) else 1.0
            k = (180.0 * r / t) if (r > 0 and base_bpm > 0 and t > 0) else 0.0
            if not (15.0 < t < 345.0) or k <= 0 or (tmin > 0 and t < tmin - 1e-9):
                ok = False
                break
            ks.append(k)
        if not ok:
            continue
        for j in range(L):
            i = start + j
            bpm_after[i] = floors[i].bpm * floors[i].speed_k / ks[j]
        keep.append((sid, start, L, tv, ks))
    if not keep:
        return

    ss_after: set[int] = set()
    for i in range(1, n):
        if abs(bpm_after[i] - bpm_after[i - 1]) > 1e-9:
            ss_after.add(i)
    snow_of: dict[int, int] = {}
    for sid, start, L, _tv, _ks in keep:
        for j in range(L):
            snow_of[start + j] = sid

    def _has_straight(from_i: int, step: int) -> int:
        """找一个可以拨的直线格（turn=0，拨了不动物理）。返回下标，找不到 -1。

        ★ 必须同时躲开「雪花之后的 SetSpeed 格」和任何雪花段内部 —— 否则 Twirl
          要么撞 SetSpeed（writer 会挪位 → 时序错），要么被雪花自己清掉。
        """
        i = from_i
        while 0 <= i < n:
            if (abs(floors[i].travel - 180.0) < 1e-9 and i not in ss_after
                    and i not in snow_of):
                return i
            i += step
        return -1

    # ---------------- ② 落地 ----------------
    for sid, start, L, tv, ks in keep:
        # 段内要清 Twirl，这会改掉下游奇偶。段前拨正 + 段尾拨回都要拨得动，
        # 否则整朵跳过（宁可没有，也不要坏时序）。
        par_in = False
        for i in range(min(start, len(flips))):
            if flips[i]:
                par_in = not par_in
        par_out = par_in
        for i in range(start, min(start + L, len(flips))):
            if flips[i]:
                par_out = not par_out
        j_in = _has_straight(start - 1, -1) if par_in else -2
        j_out = _has_straight(start + L, +1) if par_out else -2
        if j_in == -1 or j_out == -1:
            for j in range(L):
                snow_of.pop(start + j, None)
            continue
        if par_in:
            flips[j_in] = not flips[j_in]
        for i in range(start, start + L):
            flips[i] = False
        if par_out:
            flips[j_out] = not flips[j_out]
        for j in range(L):
            i = start + j
            f = floors[i]
            f.travel = tv[j]
            f.snowflake = True
            f.snowflake_id = sid
            f.speed_k = ks[j]
            f.bpm = bpm_after[i]


def _relocate_twirls(floors: list[Floor], flips: list[bool]) -> int:
    """把落在 **SetSpeed 格**上的 Twirl 往后挪到第一个空位（社区硬规则）。

    ★ **必须在 `_apply_snowflakes` 之后调用**：雪花会给段内每格换一个非 2 的幂的
      速度档，于是段的边界会**新长出 SetSpeed**。旧顺序（雪花之前挪）看不见这些
      新事件，于是 `writer.build_actions` 只好在写盘时再挪一次 —— 而那时
      angleData 已经算完了，反解出来的奇偶就和模型对不上（实测 324ms）。

    返回挪动的个数（正常应恒为 0；不为 0 说明上游漏了一处，会记进 `ch.meta`）。
    """
    ss = _setspeed_floors(floors)
    hit = ss & {i for i, v in enumerate(flips) if v}
    if not hit:
        return 0
    moved = 0
    for i in sorted(hit):
        j = i + 1
        while j < len(floors) and (j in ss or flips[j]):
            j += 1
        flips[i] = False
        if j < len(floors):
            flips[j] = True
        moved += 1
    return moved


def _protected_floors(p: SolveParams, n: int) -> set[int]:
    """回正**不许碰**的格：模板 / 三连音引擎 / 自然闭合 / 雪花 段。

    这些段的 Twirl 由各自逻辑说了算（或必须为 False），回正只能绕开。
    索引口径与 `_finish` 内逐个 span 的处理保持一致。
    """
    prot: set[int] = set()

    def _add(lo: int, hi: int):
        for i in range(max(0, int(lo)), min(n, int(hi))):
            prot.add(i)

    for s, total, _t in (getattr(p, "template_flips", None) or []):
        _add(s, s + total)
    for s0, run in (getattr(p, "engine_spans", None) or []):
        _add(s0 + 1, s0 + 1 + len(getattr(run, "flips", []) or []))
    for s0, L in (getattr(p, "natural_spans", None) or []):
        _add(s0 + 1, s0 + 1 + L)
    for s0, spec in (getattr(p, "snowflake_spans", None) or []):
        _add(s0, s0 + getattr(spec, "tiles", 0))
    return prot


def _straighten_flips(floors: list[Floor], flips: list[bool],
                      p: SolveParams) -> list[bool]:
    """角度回正：只改 Twirl 符号，不动 travel（时序零影响）。见 `core/straighten.py`。"""
    from . import straighten as _st
    n = len(floors)
    prot = _protected_floors(p, n)
    forbid = _setspeed_floors(floors)
    plan = _st.plan_straighten(
        floors, flips,
        radius=float(p.planar_radius),
        allow_twirl=True,
        pinned=prot,
        forbidden=forbid,
        theta=float(p.straighten_theta),
        min_run=int(p.straighten_min_run),
        flip_penalty=float(p.straighten_flip_penalty),
        grid=float(p.straighten_grid),
        min_gain=float(p.straighten_min_gain),
    )
    p.straighten_meta = {
        "changed": plan.changed,
        "offaxis_before": round(plan.offaxis_before, 3),
        "offaxis_after": round(plan.offaxis_after, 3),
        "twirls_before": plan.twirls_before,
        "twirls_after": plan.twirls_after,
        "skipped": plan.skipped,
    }
    return plan.flips


def _finish(floors: list[Floor], flips: list[bool], p: SolveParams):
    """落地前最后一道：**Twirl 不许和 SetSpeed 同格**（社区硬规则）。

    撞了就往后挪到第一个「既没有 SetSpeed、也没有别的 Twirl」的层。
    Twirl 零成本，挪位置不破坏时序（只改变转向符号，即路径镜像）。

    **魔法阵放在最后覆盖** —— 它要独占段内的 Twirl 位，不能被上面的挪位逻辑打乱。
    """
    if p.allow_twirl and p.emit_twirl and floors:
        # 模板段：Twirl 由模板说了算，先把它标成「已定」，免得被下面的挪位逻辑打乱
        spans = getattr(p, "template_spans", None)
        espans = getattr(p, "engine_spans", None)
        nspans = getattr(p, "natural_spans", None) or []
        if spans or espans or nspans:
            flips = list(flips)
        if nspans:
            # 自然闭合段**不许有任何 Twirl** —— 它靠 Σtravel 自己闭合，
            # 一旦翻奇偶，a 的变化符号全反，闭合就没了（这正是斜线的来源）。
            for s0, L in nspans:
                for j in range(L):
                    idx = s0 + 1 + j
                    if 0 <= idx < len(flips):
                        flips[idx] = False
        if spans:
            # ★ 用 template_flips（带总格数）而不是 spans（只有模板本身）：
            #   多轮重复的模板要把 Twirl 逐轮重放，`range(t.n)` 只覆盖第 1 轮。
            tf = getattr(p, "template_flips", None) or [
                (s - 1, t.n, t) for s, t in spans]
            _allround = bool(getattr(p, "template_all_rounds", True))
            for s, total, t in tf:
                if not (0 <= s < len(flips)):
                    continue
                # 让这一段从 ccw=False（模板作者设定的方向）开始
                par = 0
                for i in range(s):
                    if flips[i]:
                        par ^= 1
                if par:
                    flips[s] = not flips[s]
                for j in range(total if _allround else t.n):
                    idx = s + j
                    if idx < len(flips):
                        flips[idx] = (bool(t.twirl[j % t.n])
                                      if t.twirl else False)
        if espans:
            # 三连音引擎：掩码从 ccw=False 起步才有意义（S 的推导基于这个前提）
            for s0, run in espans:
                s = s0 + 1                      # onset 下标 → floor 下标
                if not (0 <= s < len(flips)):
                    continue
                par = 0
                for i in range(s):
                    if flips[i]:
                        par ^= 1
                if par:
                    flips[s] = not flips[s]
                for j, fl in enumerate(run.flips):
                    idx = s + j
                    if idx < len(flips):
                        flips[idx] = bool(fl)
        ss = _setspeed_floors(floors)
        hit = ss & {i for i, v in enumerate(flips) if v}
        if hit:
            moved = list(flips)
            for i in sorted(hit):
                j = i + 1
                while j < len(floors) and (j in ss or moved[j]):
                    j += 1
                moved[i] = False
                if j < len(floors):
                    moved[j] = True
            flips = moved
    # ★ 角度回正（项1）：在雪花覆盖**之前**做 —— 雪花段不许动，已 pin 死。
    #   尊重 twirl_mode="off"（用户明确关掉 Twirl 时不擅自加图标）。
    if (getattr(p, "straighten", False) and p.allow_twirl and p.emit_twirl
            and p.twirl_mode != "off" and floors):
        flips = _straighten_flips(floors, flips, p)
    # ★ 顺序不能颠倒：雪花会改段内的 BPM ⇒ 会**新长出 SetSpeed**。
    #   所以「Twirl 不许和 SetSpeed 同格」的挪位必须放在雪花**之后**再做一遍，
    #   否则 writer 会在写盘时偷偷挪（那时 angleData 已定 → 反解差 180°）。见 docs/24 §3。
    _apply_snowflakes(floors, flips, p)
    p.twirl_moved_by_snow = _relocate_twirls(floors, flips)
    _apply(floors, flips, p)


def _plan_twirls(floors: list[Floor], p: SolveParams) -> None:
    """决定 Twirl 翻转位（= 每层的转向符号）。

    Twirl 是**全局状态**：在某一层翻转，会改变从该层起直到下一次翻转的所有层的转向符号。
    所以翻转次数 = 谱面上会出现多少个「旋转」图标，能省则省。

    ============ 四种策略 ============
    off        完全不翻转（图标 0）。等长连打会把行星锁进闭合多边形里打转。
    alternate  每层都翻转。铺得最开、但图标最多。
    steer      逐步打分：让行星朝着"远离局部质心"的方向走，路径自然铺开。
    accum      累积转角超过阈值就翻。

    v3 起大量层是 180°（直线），它们的 turn == 0，翻不翻都一样，
    所以下面所有策略都会跳过 turn == 0 的层。

    ============ 为什么不能按"方块贴太近"去翻？============
    实测你自己的真实谱面（CLionSister，8479 层）：
        Twirl 只占 17.9%，但非相邻方块间距 < 1.75R 的有 14732 对、最小间距 0.00R。
    也就是说 **ADOFAI 真谱本来就大量重叠**，这是正常的谱面观感，不是缺陷。
    """
    n = len(floors)
    if n == 0:
        return
    flips = [False] * n

    if not p.allow_twirl or not p.emit_twirl or p.twirl_mode == "off":
        _finish(floors, flips, p)
        return

    if p.twirl_mode == "alternate":
        last = 0
        for i, f in enumerate(floors):
            if abs(f.travel - 180.0) < 1e-6:
                continue
            cur = ((f.travel + 180.0) % 360.0 + 180.0) % 360.0 - 180.0
            sign = 1 if cur > 0 else -1
            want = -last if last else 1
            if sign != want:
                flips[i] = True
                last = -sign
            else:
                last = sign
        _finish(floors, flips, p)
        return

    if p.twirl_mode == "accum":
        limit = max(90.0, min(1080.0, p.twirl_limit_deg))
        acc = 0.0
        for i, f in enumerate(floors):
            turn = ((f.travel + 180.0) % 360.0 + 180.0) % 360.0 - 180.0
            if abs(turn) < 1e-9:
                continue
            if abs(acc + turn) > limit:
                flips[i] = True
                acc = 0.0
            else:
                acc += turn
        _finish(floors, flips, p)
        return

    # ---------------- steer：逐步打分 ----------------
    R2 = 2.0 * p.planar_radius
    K = max(8, int(p.steer_lookback))
    NO_FLIP_PENALTY = float(p.steer_noflip_penalty)
    CENTROID_W = float(p.steer_centroid_w)

    pts: list[tuple[float, float]] = [(0.0, 0.0)]
    heading = 90.0
    ccw = False
    for i, f in enumerate(floors):
        lo = max(0, i - K)
        cx = sum(q[0] for q in pts[lo:i + 1]) / max(1, i + 1 - lo)
        cy = sum(q[1] for q in pts[lo:i + 1]) / max(1, i + 1 - lo)
        best = None
        for cand in (0, 1):
            c2 = (not ccw) if cand else ccw
            turn = (180.0 - f.travel) if c2 else (f.travel + 180.0)
            turn = ((turn + 180.0) % 360.0) - 180.0
            h2 = ((heading + turn) + 180.0) % 360.0 - 180.0
            x = pts[-1][0] + R2 * math.cos(math.radians(h2))
            y = pts[-1][1] + R2 * math.sin(math.radians(h2))
            rx, ry = x - cx, y - cy
            rn = math.hypot(rx, ry)
            radial = (math.cos(math.radians(h2)) * rx + math.sin(math.radians(h2)) * ry) / rn if rn > 1e-9 else 0.0
            dmin = 1e9
            for j in range(lo, max(0, i - 1)):
                d = math.hypot(x - pts[j][0], y - pts[j][1])
                if d < dmin:
                    dmin = d
            crowd = -max(0.0, 1.6 * p.planar_radius - dmin) * 3.0
            score = 1.0 * radial + CENTROID_W * min(rn, 30.0 * R2) / R2 + crowd - (NO_FLIP_PENALTY if cand else 0.0)
            if best is None or score > best[0]:
                best = (score, cand, h2, x, y)
        _, cand, h2, x, y = best
        if cand:
            ccw = not ccw
            flips[i] = True
        heading = h2
        pts.append((x, y))

    _finish(floors, flips, p)


# ---------------------------------------------------------------------------
def _prepare_rhythm(onsets: list[Onset], p: SolveParams) -> dict:
    """音值 / 基准 BPM / `rs` / `rq` —— **两条路径共用**的前处理。

    ★ 为什么抽出来：`aggressive_pick` 的阶梯路径必须与老路径用**同一套**
      `base_bpm` 与 `rs`，否则两条路径会各挑各的八度，A/B 比对就没意义了
      （`docs/25` §4 与 `docs/双押逻辑.md` §「架构问题」都点过这个坑）。

    本函数是 `solve()` 原本「1) 音值」到「选基准 BPM」那一段的**逐行搬运**，
    行为完全一致 —— `tests/golden/off_hashes.json` 的冻结哈希守着这一点。
    """
    n_on = len(onsets)
    dts = [onsets[i + 1].t_ms - onsets[i].t_ms for i in range(n_on - 1)]

    # ---------------- 1) 音值（拍数） ----------------
    mbpm = p.midi_bpm if p.midi_bpm > 0 else 120.0
    beat_ms = 60000.0 / mbpm
    beats, tick_err = beats_of(onsets, p.ppqn, mbpm)
    if not beats:                       # 没有 tick：退回毫秒折算
        beats = [d / beat_ms for d in dts]
        tick_err = 0.0

    quant_err = 0.0
    if p.quantize_rhythm:
        from . import rhythm
        nb = []
        for b in beats:
            if b > p.quantize_max_beats:      # 休止：保持精确，不吸附
                nb.append(b)
                continue
            q = rhythm.quantize(b)
            quant_err = max(quant_err, abs(q - b) * beat_ms)
            nb.append(q)
        beats = nb
    qbeats = list(beats)                # 量化后的音值（MIDI 拍），模板匹配用这个

    # ---------------- 2) 选参考音值（或用手动基准 BPM） ----------------
    if p.base_bpm > 0:
        # 手动基准 BPM：直接以「60000/base_bpm 毫秒 = 一条直线」为准，不依赖音值概念
        base_bpm = _snap_nice(float(p.base_bpm))
        beats = [max(d, 0.5) * base_bpm / 60000.0 for d in dts]
        ref_beats = 60000.0 / base_bpm
        got = choose_reference(beats, p, mbpm, force=1.0)
        C = got[0]
    else:
        C, base_bpm, _sf, _ef, _ks = choose_reference(beats, p, mbpm)
        # 基准 BPM 吸附到人写得出来的数（MIDI 导出的 333333us 其实想写 180）
        base_bpm = _snap_nice(float(base_bpm))
        ref_beats = C
    # ★ 吸附到人写得出来的整数（119.84→120、359.5→360、179.755→180）。
    #   ⚠ **默认关闭**：理论上时序无关（`rs` 在这之后才从 dt 反算，
    #   `travel/180 × 60000/(base/k) = rs·60000/base = dt` 恒成立），但实测
    #   在 OGG 源上 119.84→120 这一步把时序从 1410us 炸到 **19954us**，
    #   同时 off45 从 95.9% 变差到 99.1%。说明链路上有对 base **不连续**的地方
    #   （最可疑：`_is_straight(tv)` 把接近 180 的 travel 强行掰成 180，
    #     以及 `_merge_short_runs` / DP 分档在 base 微动后跳到不同解）。
    #   先把那个不连续点找出来，再开这个开关。
    #   MIDI 源不受影响（base 本来就是 180/360，吸附是恒等变换）。
    if getattr(p, "snap_base_bpm", False):
        base_bpm = _snap_bpm(float(base_bpm))

    # ★ 基准 BPM：除了 DP 自己选的那个，再试「**主间隔 = 1 拍**」的候选
    #   （以及它们的 2 倍/半倍，因为 k 本来就是 2 的幂），按「travel 落在网格上
    #   的比例」挑最好的那个。
    #
    #   为什么必须做：轨迹抖动（抖动实测 off45=96%、off30=81%）几乎全部来自
    #   base 选错，而不是音头不准。base=119.84 时 166.9ms = 1/3 拍、
    #   111.3ms = 2/9 拍 ⇒ travel 60/40/20，**没有任何网格能对上**；
    #   base=360 时它们是 1 / 2/3 / 1/3 拍 ⇒ travel 180/120/60，全部落在 30° 网格。
    #   这一步不影响时序（rs 在这之后才从 dt 反算）。
    #   ⚠ **默认关闭**：`_grid_fit` 这个判据是坏的 —— 它只看「r 是不是 1/12 拍的
    #     整数倍」，而 base 越粗这个条件越容易满足。实测它会挑出 719 和 60：
    #       平衡 grid=6  → base=719  SetSpeed=211  模板 0 段   （抖动好了，手感崩了）
    #       保细节 grid=12 → base=60   off45=99.5%  时序 14501us **FAIL**
    #     正解应当奖励「r 逼近**简单分数**（分母 ≤6）」并约束 base 落在合理区间，
    #     而不是「落在 1/12 网格上」。修好判据再打开。
    if getattr(p, "snap_base_grid", False):
        _sd = sorted(dts)
        _med = _sd[len(_sd) // 2] if _sd else 0.0
        _raw = [base_bpm] + ([60000.0 / _med] if _med > 1.0 else [])
        _raw += [x * 2.0 for x in list(_raw)] + [x / 2.0 for x in list(_raw)]
        _cands = [c for c in (_snap_bpm(x) for x in _raw) if 30.0 <= c <= 2000.0]
        if len(_cands) > 1:
            _best, _best_s = base_bpm, -1.0
            for c in dict.fromkeys(_cands):
                _rs = [max(d, 0.5) * c / 60000.0 for d in dts]
                s = _grid_fit(_rs)
                if s > _best_s + 1e-9:
                    _best, _best_s = c, s
            base_bpm = _best
            if p.base_bpm > 0:
                ref_beats = 60000.0 / base_bpm

    # 用**同一套** r 重跑一遍 DP，确保决策 >=< 实现
    rs = [max(d, 0.5) * base_bpm / 60000.0 for d in dts]

    # 匹配用**量化后的音值**折算到本谱基准（rs 里混了毫秒噪声，直接匹配命中率很低）
    scale = (base_bpm / mbpm) if mbpm > 0 else 1.0
    rq = [b * scale for b in qbeats] if qbeats else rs

    return {"n_on": n_on, "dts": dts, "rs": rs, "rq": rq, "qbeats": qbeats,
            "base_bpm": base_bpm, "mbpm": mbpm, "ref_beats": ref_beats,
            "tick_err": tick_err, "quant_err": quant_err, "scale": scale}


# ---------------------------------------------------------------------------
def solve(onsets: list[Onset], p: SolveParams) -> Chart:
    """onset 序列 -> 完整谱面。"""
    ch = Chart(base_bpm=float(p.base_bpm or 180.0))
    if len(onsets) < 2:
        ch.meta["error"] = "onset 少于 2 个，无法成谱"
        return ch

    prep = _prepare_rhythm(onsets, p)
    n_on, dts = prep["n_on"], prep["dts"]
    rs, rq = prep["rs"], prep["rq"]
    qbeats = prep["qbeats"]
    base_bpm, mbpm = prep["base_bpm"], prep["mbpm"]
    tick_err, quant_err = prep["tick_err"], prep["quant_err"]
    ref_beats = prep["ref_beats"]

    # 老路径的全局 DP 选档。
    # ★ `aggressive_pick=True` 时它**不是**落点决策 —— 只当第 1 级的
    #   「档位建议器」（`docs/25` §9.0 Q5），真正的 travel/k/twirl 由
    #   `core/ladder.py` 逐音决定。
    _, ks, straight_frac, event_frac = _dp_tiers(rs, p.speed_tiers, p.straight_weight, p.speed_penalty,
                                        p.speed_switch_penalty,
                                        travel_min=p.travel_min,
                                        inplace_penalty=p.inplace_penalty,
                                        travel_max=float(getattr(p, "travel_max", 0.0) or 0.0),
                                        speed_penalty_max_oct=_spmo(p),
                                        slow_speed_penalty=_ssp(p))
    ks = _merge_short_runs(ks, rs, p.speed_min_run, travel_min=p.travel_min,
                           travel_max=float(getattr(p, "travel_max", 0.0) or 0.0))
    ch.base_bpm = base_bpm

    # ★ 对音阶梯（`docs/25`）——**并列的第二条路径**，由 `p.aggressive_pick` 把关。
    #   关掉时下面这一行都不会执行：老路径逐字节不变
    #   （`tests/golden/off_hashes.json` 冻结哈希机器验收）。
    #   打开时：图形 → 当前档 → 加 Twirl 镜像 → 变速后重跑 → 暂停节拍。
    if getattr(p, "aggressive_pick", False):
        from .ladder import solve_aggressive
        return solve_aggressive(onsets, p, prep, ks, base_bpm=base_bpm)

    # ---------------- 2.5) 节奏型模板优先（主体路径） ----------------
    hit_at: dict[int, tuple[int, object]] = {}
    spans: list = []
    span_meta: list = []
    # ★ 模板段要重复多轮时，Twirl 必须**逐轮重放**。
    #   只写一轮（`range(t.n)`）会让第 2 轮之后全部错位 —— 见 _finish。
    tpl_flips: list = []                     # (起始 onset, 总格数, 模板)
    tpl_reps = 0

    # ---------------- 2.44) 自然闭合段：挪到模板之后 ----------------
    # 见 2.6) 前的那一段。优先级：**手写模板 > 自然闭合段 > 三连音引擎**
    # —— 具体写法优先，通用规则兜底（用户口径：没必要一竿子打死）。
    nat_hit: dict[int, tuple[float, float]] = {}      # idx → (travel, speed_k)
    nat_spans: list[tuple[int, int]] = []
    _tail_k = ks[len(ks) // 2] if len(ks) else 1

    # ---------------- 2.43) 长休止 → Pause 事件（必须先定） ----------------
    # 用户口径：音乐长时间没声音，就用暂停节拍，**不要硬对音把轨道扭得奇奇怪怪**。
    # 做法：这一格走一条直线（180° = 1 拍），剩下的时间全交给 Pause。
    # ★ 必须排在模板/自然段/引擎**之前**：`pause_beats` 是按 **base_bpm 的拍**算的，
    #   所以 Pause 格的速度档只能是 k=1。一旦被自然段抢走（给了它 k=8），
    #   8 拍就会被当成 8 × (60000/(base/8))，一格多出 7 倍时长
    #   —— MemoryLocked 曾因此全谱多出 84 秒。
    #   下面三个扫描都要把 Pause 格当**屏障**绕开，不能跨过去拼闭合。
    pauses = [0.0] * len(dts)
    pause_at: set[int] = set()
    n_pause = 0
    if p.use_pause:
        for i, r in enumerate(rs):
            if r <= p.pause_min_beats:
                continue
            # 强制走一条直线（1 拍）+ Pause 填剩下的时间。
            # 长休止最怕的就是「为了硬对上时间而选一个极慢的速度档」（k=512 那种爬行格）。
            extra = r - 1.0
            if extra > 1e-6:
                pauses[i] = r          # ★ 存**总拍数 r**，不是 r−1。
                pause_at.add(i)        #   实际暂停拍数要在知道速度档后才能算：
                n_pause += 1           #   pb = r/k − 1（见 floors 循环）

    # ---------------- 2.45) 三连音等值连续段 ----------------
    # 一段「音值完全相同、且属于三连音族」的连打。长段归引擎（恒定 travel + 交错 Twirl
    # ⇒ 匀速 + 回正），短段留给三角形模板。
    from .triplet_engine import (is_triplet as _is_tri,
                                 plan_run as _tri_plan)
    tri_runs: list[tuple[int, int, float]] = []       # (起始 onset, 格数, r)
    _i = 0
    while _i < len(rq):
        if not _is_tri(rq[_i]):
            _i += 1
            continue
        _s0, _v = _i, rq[_i]
        while _i < len(rq) and _is_tri(rq[_i]) and abs(rq[_i] - _v) <= p.triplet_tol:
            _i += 1
        tri_runs.append((_s0, _i - _s0, _v))
    eng_first: set[int] = set()
    if p.use_triplet_engine:
        for _s0, _L, _v in tri_runs:
            if _L >= p.triplet_engine_min:
                eng_first.update(range(_s0, _s0 + _L))

    if p.use_templates:
        from .templates import load as _load_tpl, match as _match_tpl
        from .templates import match_dp as _match_tpl_dp
        from .templates import STAIR_PATH as _STAIR
        dp_tv = [180.0 * rs[i] / (ks[i] if i < len(ks) else tail_k)
                 for i in range(len(rs))]

        # 段落切分：Δt 超过 template_rest_ms 算休止；模板只用在短段落里
        pass_of = [0] * len(dts)
        pass_ms: list[float] = []
        _i = 0
        while _i < len(dts):
            _j = _i
            while _j + 1 < len(dts) and dts[_j] <= p.template_rest_ms:
                _j += 1
            pass_ms.append(sum(dts[_i:_j + 1]))
            for _x in range(_i, _j + 1):
                pass_of[_x] = len(pass_ms) - 1
            _i = _j + 1

        def _accept(s, t, reps):
            L = t.n * reps
            # 长休止格是屏障：travel 被写死成 180 且带 Pause，模板跨不过去
            if any((s + j) in pause_at for j in range(L)):
                return False
            # 引擎优先级更高的长三连音段：模板让路
            if any((s + j) in eng_first for j in range(L)):
                return False
            # 用户口径：短的段落用 90°（模板）；长的段落交回 DP（速度×2 走直线）
            # ★ 三连音模板豁免这道闸（否则三连音谱面等于关掉模板）
            if p.template_max_span_s > 0 and not _tpl_is_triplet(t) and \
                    pass_ms[pass_of[s]] > p.template_max_span_s * 1000.0:
                return False
            if not p.template_only_nonstraight:
                return True
            seg = dp_tv[s:s + L]
            return any(not _is_straight(x) for x in seg)

        _matcher = _match_tpl_dp if getattr(p, "template_dp", True) else _match_tpl
        # ★ 迭代 2.2：`stair_first` 把「折弯循环」变体作为**第一权重**加进来
        _tpls = _load_tpl(extra=_STAIR) if getattr(p, "stair_first", False) \
            else _load_tpl()
        for s, t, reps in _matcher(rq, _tpls, p.template_tol,
                                   timing=rs, beat_ms=60000.0 / base_bpm,
                                   timing_tol_ms=p.template_timing_tol_ms,
                                   accept=_accept):
            spans.append((s + 1, t))
            span_meta.append((s, t.n * reps))
            tpl_flips.append((s, t.n * reps, t))
            tpl_reps += reps
            for j in range(t.n * reps):
                hit_at[s + j] = (j % t.n, t)
    p.template_spans = spans
    p.template_flips = tpl_flips

    # ---------------- 2.55) 图形候选 + 单一评分器（自然段 / 引擎 合并） ----------------
    # 原来这里是**两条平行 greedy 路径**：自然段先抢长窗口，引擎只能捡剩下的。
    # 现在合成一条：同一个窗口上两个生成器各出候选，用**同一个** scorer 比。
    # 评分顺序见 core/figures.py：
    #   ① 不产生 SetSpeed → ② Y>0 → ③ 不自重叠 → ④ 闭合（硬过滤） → ⑤ Twirl 少
    # ★ 手写模板仍然优先：`hit_at` 是屏障，模板段不会被这里覆盖。
    #   （用户口径：照五月雪逐格抄，老谱师的排版不用再搜一遍。）
    eng_spans: list = []
    eng_hit: dict[int, tuple[int, object]] = {}
    # ★ DP 格的最终速度档：黏性逻辑可能让它≠ks[i]，必须记下来给 travels/floors 用，
    #   否则 travel（按 _cur_k 画）和 bpm（按 ks[i] 写）对不上 —— 时序直接炸。
    dp_k: dict[int, float] = {}
    # ★ ADOFAI 行话的 BPM 是 16 分音口径：行话 BPM = base_bpm ÷ 4。
    #   「拉链」（travel 60）是给行话 ≥360（= 我们的 base_bpm ≥1440）用的；
    #   常速优先「寻常」（travel 120 + 一次 SetSpeed×2）。
    _tri_prefer = 60.0 if base_bpm >= 1440.0 else 120.0
    from .figures import Walk as _Walk, choose as _choose_fig

    # ---------------- 2.54) ★ 双押预留槽位（a，docs/31 §5.2） ----------------
    #   把「要出双押的 onset」当**约束**：轮到它时强制沿用**上一格的生效档**
    #   ⇒ 这一格自己不出 SetSpeed（双押薄格必须是干净的，参考谱硬规则
    #   「调速一定要放在平着的格子上面」），并进 `_blocked` 让图形让路。
    #   真正的钉档在下面的扫描循环里（那里才知道生效的 `_cur_k`）。
    #   `dp_reserve` 为空时**一行都不执行** ⇒ 老路径逐字节不变。
    dp_resv: set[int] = set()
    dp_resv_ok: set[int] = set()
    _dprm: dict = {"n": 0, "flat": 0, "lost": 0, "lost_idx": []}
    if getattr(p, "dp_reserve", None):
        dp_resv, _dprm = _reserve_dp_slots(
            rs, p, blocked=set(hit_at) | set(pause_at))
        p.dp_reserve_meta = _dprm

    _blocked = set(hit_at) | set(pause_at) | dp_resv
    _w = _Walk(R2=2.0 * p.planar_radius, allow_twirl=bool(p.allow_twirl))
    _cur_k = 1.0                       # 第 0 层（开局站位）的速度档
    _i = 0
    while _i < len(rs):
        if _i in hit_at:               # 模板：按模板自己的 travel/Twirl 推进状态
            _tj, _t = hit_at[_i]
            _tw = bool(_t.twirl[_tj]) if _tj < len(_t.twirl) else False
            _w.step(_t.travel[_tj], _tw)
            _cur_k = 1.0
            _i += 1
            continue
        if _i in pause_at:             # 长休止：1 拍直线 + Pause，档固定 base_bpm
            _w.step(180.0, False)
            _cur_k = 1.0
            _i += 1
            continue
        if _i in dp_resv:              # ★ 双押预留槽位：图形让路，沿用上一格的档
            _rk = _cur_k if _cur_k else 1.0
            _rtv = 180.0 * rs[_i] / _rk
            # ★ 守卫：沿用这一档不能把 travel 顶出硬范围，**也不能顶破用户的
            #   最小角度**（`travel_min`，默认 20°）。否则预留出来的那一格
            #   自己就成了违规格（实测 `all_Automaton_Waltz` 出现过 11.25°）。
            _lo = max(TRAVEL_HARD_MIN, float(getattr(p, "travel_min", 0.0) or 0.0))
            if _lo - 1e-9 <= _rtv <= TRAVEL_HARD_MAX:
                _w.step(180.0 if _is_straight(_rtv) else _rtv, False)
                dp_k[_i] = _rk
                _cur_k = _rk
                dp_resv_ok.add(_i)
                _dprm["flat"] = _dprm.get("flat", 0) + (1 if _is_straight(_rtv) else 0)
                _i += 1
                continue
            # 沿用这一档会把 travel 顶出硬范围 ⇒ 放弃预留，交回常规路径（报出来）
            _dprm["lost"] = _dprm.get("lost", 0) + 1
            _dprm.setdefault("lost_idx", []).append(_i)
            _dprm["n"] = max(0, _dprm.get("n", 0) - 1)
        _fig = _choose_fig(_i, rs, ks, p, _w, blocked=_blocked,
                           tail_k=_tail_k, prefer_travel=_tri_prefer,
                           prev_k=_cur_k)
        if _fig is None:               # 没图可画 → 老实交回 DP
            _k = ks[_i] if _i < len(ks) else _tail_k
            _tv = 180.0 * rs[_i] / _k
            # ★ 速度档粘性：能靠**转角**贴出来就别换档。
            #   只有当前档把 travel 顶出 [min,max]，或角度明显变差时才换。
            #   （用户口径：优先图形贴，实在贴不上再变速，而不是随便变速。）
            if p.sticky_speed:
                _tvc = 180.0 * rs[_i] / _cur_k
                # ★ 额外的网格守卫：只在 _tvc 落在 45° 网格上时才粘。
                #   否则会写出非整角的 travel，第三方 parser 反解时对
                #   「180 vs 540」这类同余歧义会判错（实测正好差 2 拍）。
                _ongrid = abs(_tvc / 45.0 - round(_tvc / 45.0)) < 1e-9
                if (_ongrid
                        and max(p.natural_travel_min, p.travel_min) - 1e-9 <= _tvc
                        and _tvc <= p.natural_travel_max + 1e-9
                        and abs(_tvc - 180.0) <= max(abs(_tv - 180.0),
                                                      p.sticky_travel_slack)):
                    _k, _tv = _cur_k, _tvc
            _w.step(180.0 if _is_straight(_tv) else _tv, False)
            dp_k[_i] = _k
            _cur_k = _k
            _i += 1
            continue
        if _fig.kind == "nat":
            for _x in range(_fig.n):
                nat_hit[_i + _x] = (_fig.travels[_x], _fig.ks[_x])
            nat_spans.append((_i, _fig.n))
        else:
            eng_spans.append((_i, _fig.run))
            for _x in range(_fig.n):
                eng_hit[_i + _x] = (_x, _fig.run)
        for _x in range(_fig.n):       # 推进 walk：后面候选的 Y/重叠都靠它
            _w.step(_fig.travels[_x], _fig.twirls[_x])
        _cur_k = _fig.ks[-1]
        _i += _fig.n
    p.natural_hit = nat_hit
    p.natural_spans = nat_spans
    p.engine_spans = eng_spans
    p.dp_k = dp_k

    # ---------------- 2.55) 魔法阵（雪花）选中段 ----------------
    # 只在**等间隔**的段落里下雪花：雪花是绝对匀速的，间隔在抖的段落塞进去反而毁时序。
    # 段长 < snowflake_min_tiles 直接跳过；之后权重递增到 snowflake_full_tiles 时 100%。
    snow_spans: list = []
    p.snowflake_spans = []
    p.snowflake_rs = rs
    if p.use_snowflake and n_on > 2:
        import random as _random
        from .snowflake import plan as _snow_plan, should_use as _snow_use
        from .snowflake import SHAPES_DETERMINISTIC, SHAPES as _ALL_SHAPES
        from .snowflake import MIN_ARMS as _MIN_ARMS
        _srng = _random.Random(int(p.snowflake_seed) if p.snowflake_seed else 20240213)
        tol = max(0.5, float(p.snowflake_uniform_tol_ms))
        # ★ 「允许使用参数的部位极少」的修复：形状 / 每臂最少步数 / 紧凑优先 /
        #   随机采样 / 最小角度 全部从这里透传给 `snowflake.plan`。
        _shape = str(getattr(p, "snowflake_shape", "auto") or "auto").strip().lower()
        if _shape in ("", "auto", "自动"):
            _shapes: tuple | None = None
        elif _shape == "random":
            _shapes = _ALL_SHAPES
        elif _shape in _ALL_SHAPES:
            _shapes = (_shape,)
        else:
            _shapes = None
        _det = not bool(getattr(p, "snowflake_random", False))
        i = 0
        n_dt = len(dts)
        while i < n_dt:
            j = i
            while (j + 1 < n_dt and abs(dts[j + 1] - dts[i]) <= tol
                   and (j + 1) not in pause_at):
                j += 1
            L = j - i + 1
            spec = (_snow_plan(
                L, n_choices=tuple(p.snowflake_n_rot),
                min_arms=int(getattr(p, "snowflake_min_arms", _MIN_ARMS) or _MIN_ARMS),
                rng=_srng,
                trials=int(getattr(p, "snowflake_trials", 32) or 32),
                compact=bool(getattr(p, "snowflake_compact", True)),
                deterministic=_det,
                shapes=_shapes,
                travel_min=float(p.travel_min),
            ) if L >= p.snowflake_min_tiles else None)
            if spec is not None and _snow_use(L, p.snowflake_min_tiles,
                                              p.snowflake_full_tiles, i):
                snow_spans.append((i + 1, spec))        # +1：第 0 层是开局站位
            i = j + 1
        p.snowflake_shape_used = sorted(
            {s.shape for _s0, s in snow_spans}) or []
    p.snowflake_spans = snow_spans

    # 结算每层的角行程
    travels: list[float] = [FIRST_TRAVEL]                     # 第 0 层 = 开局站位
    tail_k = _tail_k
    for i, dt in enumerate(dts):
        if i in pause_at:                     # 长休止：直线 + Pause
            travels.append(180.0)
        elif i in nat_hit:                    # 自然闭合段：travel = 180·音值 / 速度档
            travels.append(nat_hit[i][0])
        elif i in hit_at:                     # 模板段：直接用模板角度（speed 1）
            j, t = hit_at[i]
            travels.append(t.travel[j])
        elif i in eng_hit:                    # 三连音引擎：恒定 travel（时长由 speed_k 定）
            travels.append(eng_hit[i][1].travel)
        else:
            k = dp_k.get(i, ks[i] if i < len(ks) else tail_k)
            tv = 180.0 * rs[i] / k
            if _is_straight(tv):
                tv = 180.0
            travels.append(tv)
    travels.append(FIRST_TRAVEL)       # 尾层（不影响任何 onset）

    floors: list[Floor] = []
    for i, tv in enumerate(travels):
        pb = 0.0
        if i == 0:
            k = 1
        elif (i - 1) in pause_at:
            # ★ Pause 格**不强行 k=1**（那会在暂停格上多发一个 SetSpeed，
            #   紧跟着下一格又弹回去 —— 实测 12 处 Pause 白花 24 个速度事件）。
            #   但也不能无条件沿用前一格：前一格是减速档时
            #   `pb = r/k − 1` 会被吃成 0，等待就**悄悄变回「缓速爬过去」**了
            #   （用户口径明确不要这个）。所以只在「沿用后仍留下真的暂停」时才沿用：
            #
            #     duration = (travel/180 + pb)·60000/(base/k) = (1+pb)·60000·k/base
            #     要 = r·60000/base   ⇒   **pb = r/k − 1**
            #
            #   `pb < PAUSE_MIN_BEATS` 就退回 k=1（pb = r−1，永远是真暂停，
            #   代价是暂停前后各多一个 SetSpeed）。
            _r = pauses[i - 1]
            _pk = floors[-1].speed_k if floors else 1.0
            k = _pk if (_pk <= _r + 1e-9
                        and _r / _pk - 1.0 >= PAUSE_MIN_BEATS) else 1.0
            pb = max(0.0, _r / k - 1.0)
        elif (i - 1) in nat_hit:                          # 自然闭合段：沿用 DP 速度档
            k = nat_hit[i - 1][1]
        elif (i - 1) in eng_hit:                          # 三连音引擎：自己的 BPM 除数
            k = eng_hit[i - 1][1].speed_k
        elif (i - 1) in hit_at:                           # 模板段固定 speed 1
            k = 1
        elif i - 1 < len(ks):
            k = dp_k.get(i - 1, ks[i - 1])
        else:
            k = tail_k
        floors.append(Floor(travel=tv, bpm=base_bpm / k, twirl=False,
                            turn=0.0, heading=0.0, angle=0.0, speed_k=k,
                            pause_beats=pb))

    # ★ 标记每格属于哪条图形逻辑（角度双押插入时不许拆它们）。
    #   口径：floors[i]（i≥1）对应 onset i−1；三个 hit 表都是按 onset 索引的。
    for _i in range(1, len(floors)):
        _o = _i - 1
        if _o in hit_at:
            floors[_i].template = True
        elif _o in nat_hit:
            floors[_i].natural = True
        elif _o in eng_hit:
            floors[_i].engine = True
        elif _o in dp_resv_ok:              # ★ a：双押预留槽位（标记供 `dp_angle` 认领）
            floors[_i].dp_reserved = True

    _plan_twirls(floors, p)

    # ★ 预留槽位的**最终校验**（`docs/31` §5.2）：雪花是**外部覆盖层**，它会改段内
    #   每格的 BPM —— 于是某个预留格可能在扫描循环之后**长出 SetSpeed**。
    #   这种格不能再当双押槽位（薄格必须干净），撤销标记并计入 `lost`，
    #   交给 `dp_angle` 的 `_setspeed_here` 护栏去换位 / 报丢（**不许静默**）。
    if dp_resv_ok:
        for _o in sorted(dp_resv_ok):
            _fi = _o + 1
            if _fi >= len(floors):
                continue
            if abs(floors[_fi].bpm - floors[_fi - 1].bpm) > 1e-9:
                floors[_fi].dp_reserved = False
                _dprm["n"] = max(0, _dprm.get("n", 0) - 1)
                _dprm["flat"] = max(0, _dprm.get("flat", 0) - 1)
                _dprm["lost"] = _dprm.get("lost", 0) + 1
                _dprm.setdefault("lost_idx", []).append(_o)

    ch.floors = floors
    # ★ 重叠闭合图形的提示：单格 PositionTrack 错开（用户口径：单格最直观，
    #   一眼数得出几圈）。只写渲染事件，不碰 angleData/bpm/travel ⇒ 时序零影响。
    if getattr(p, "use_position_track", False):
        from . import track_fx as _tfx
        _tfx.apply(ch, step=float(getattr(p, "pos_track_step", 0.22)),
                   min_beats=float(getattr(p, "pos_track_min_beats", 8.0)))
    else:
        # ★ 用户 2026-10：「给轨道设置的随机位置偏移 改为可选是否开启」。
        #   关掉时也要落一个**确定的 0**，否则 `meta` 里干脆没有这个键，
        #   状态栏 / payload / 单测都只能看到"缺席"，分不清「关掉了」还是「没算」。
        ch.meta["pos_tracks"] = []
        ch.meta["pos_track_n"] = 0
    ch.first_onset_ms = onsets[0].t_ms
    st = interval_stats(onsets)
    ch.meta.update({
        "n_onsets": n_on,
        "n_floors": len(floors),
        "n_lead": 1,                       # 永远有开局站位层（第一格 = 直线）
        "lead_ms": FIRST_TRAVEL / 180.0 * (60000.0 / max(1e-9, base_bpm)),
        "duration_ms": onsets[-1].t_ms - onsets[0].t_ms,
        "ref_beats": ref_beats,
        "straight_frac": straight_frac,
        "event_frac": event_frac,
        "tick_err_ms": tick_err,
        "clamped": 0,
        "quant_err_ms": quant_err,
        "interval": st,
        "tpl_hits": len(spans),
        "tpl_rounds": tpl_reps,
        "tpl_covered": len(hit_at),
        "tpl_names": [t.name for _, t in spans],
        "tpl_spans": span_meta,
        "tri_runs": len(tri_runs),
        "nat_count": len(nat_spans),
        "nat_tiles": len(nat_hit),
        "eng_count": len(eng_spans),
        "eng_tiles": len(eng_hit),
        "dp_reserve_n": int((p.dp_reserve_meta or {}).get("n", 0)),
        "dp_reserve_flat": int((p.dp_reserve_meta or {}).get("flat", 0)),
        "dp_reserve_lost": int((p.dp_reserve_meta or {}).get("lost", 0)),
        "eng_spans": [(s0, r.n, round(r.r, 4), r.travel, r.speed_k, r.s_sum)
                      for s0, r in eng_spans],
        "snow_count": len(snow_spans),
        "snow_tiles": sum(s.tiles for _, s in snow_spans),
        "snow_spans": [(int(st), s.n_rot, s.arms, s.tiles) for st, s in snow_spans],
        "snow_shapes": sorted({s.shape for _s0, s in snow_spans}),
        "twirl_moved_snow": int(getattr(p, "twirl_moved_by_snow", 0) or 0),
        "travel_min": float(p.travel_min),
        "travel_max": float(getattr(p, "travel_max", 0.0) or 0.0),
        "min_travel_seen": (min((f.travel for f in floors[1:-1]), default=180.0)
                            if len(floors) > 2 else 180.0),
        "straighten": dict(getattr(p, "straighten_meta", {}) or {}),
    })
    return ch


# ---------------------------------------------------------------------------
def path_overlap_stats(ch: Chart, recent: int = 24) -> dict:
    """自重叠统计。

    ★ 魔法阵（雪花）**故意**原路折返 —— 花瓣闭合必然和自己的前几格重合，
      那不是排版失误。所以额外给一份「排掉魔法阵内部重合」的读数，
      那份才是真正需要 PositionTrack 往外推的部分。
    """
    pts = [(f.x, f.y) for f in ch.floors]
    if len(pts) < 3:
        return {"min_dist": 0.0, "overlaps": 0, "bbox": (0.0, 0.0),
                "overlaps_no_snow": 0, "min_dist_no_snow": 0.0}
    # 每层属于哪一朵雪花（0 = 不属于）
    snow_of: dict[int, int] = {}
    for k, sp in enumerate(ch.meta.get("snow_spans") or [], start=1):
        s0, _nrot, _arms, tiles = sp
        for f in range(max(0, s0 - 1), min(len(pts), s0 + tiles + 1)):
            snow_of[f] = k

    mn = 1e9
    mn_ns = 1e9
    worst_ns = (-1, -1)
    n_ov = 0
    n_ov_ns = 0
    # ★ 距离分布。`OVERLAP_R = 1.75` 是**旧口径**，而一格 = 2·planar_radius = 2.0，
    #   所以 1.75 只能捞到「几乎完全重合」。真正糊成一团要在 2.0（一格）附近看。
    hist = {"<0.5": 0, "<1.0": 0, "<1.5": 0, "<2.0": 0, "<3.0": 0}
    zones: dict[int, int] = {}
    for i in range(len(pts)):
        for j in range(max(0, i - recent), i - 1):
            d = math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1])
            same_snow = snow_of.get(i) is not None and snow_of.get(i) == snow_of.get(j)
            if d < mn:
                mn = d
            if d < OVERLAP_R:
                n_ov += 1
            if not same_snow:
                if d < mn_ns:
                    mn_ns = d
                    worst_ns = (j, i)
                if d < OVERLAP_R:
                    n_ov_ns += 1
                for key, lim in (("<0.5", 0.5), ("<1.0", 1.0), ("<1.5", 1.5),
                                 ("<2.0", 2.0), ("<3.0", 3.0)):
                    if d < lim:
                        hist[key] += 1
                        break
                if d < 2.0:
                    zones[(i // 32) * 32] = zones.get((i // 32) * 32, 0) + 1
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return {"min_dist": mn, "overlaps": n_ov,
            "overlaps_no_snow": n_ov_ns,
            "min_dist_no_snow": (mn_ns if mn_ns < 1e9 else 0.0),
            "worst_no_snow": worst_ns,
            "hist": hist,
            "zones": sorted(zones.items(), key=lambda kv: -kv[1])[:8],
            "bbox": (max(xs) - min(xs), max(ys) - min(ys))}


def times_from_chart(ch: Chart) -> list[float]:
    """按游戏模型从谱面反算每层 entryTime（ms，以第 0 层为 0）。

    时长口径（与 `vendor/adofai_timemodel` + 反编译源码一致）：
        本格时长 = ∫ (dAngle/180)·(60000/seg_bpm)
    - 无速度事件：整格一段 → `(travel/180)·(60000/bpm)`
    - `angle_offset = φ > 0`：本格切成 `[0,φ]`（**前一段用上一格的速度**）
      与 `[φ, travel]`（本格 bpm）两段 —— 这是 `SetSpeed.angleOffset` 的语义
      （绝对角度 = 该格起点 + φ），见 `core/dp_offset` 顶部注释。
    - `Pause` 给该格加 `pause_beats` 拍 → 额外时长 `pause_beats·60000/bpm`。
    """
    out = [0.0]
    t = 0.0
    prev_bpm = float(ch.base_bpm) or 1.0
    for f in ch.floors[:-1]:
        A = f.travel
        phi = float(getattr(f, "angle_offset", 0.0) or 0.0)
        b_now = float(f.bpm) or 1.0
        if phi > 0.0 and abs(b_now - prev_bpm) > 1e-12:
            dt = (phi / 180.0) * (60000.0 / prev_bpm) + \
                ((A - phi) / 180.0) * (60000.0 / b_now)
        else:
            dt = (A / 180.0) * (60000.0 / b_now)
        t += dt + f.pause_beats * (60000.0 / b_now)
        out.append(t)
        prev_bpm = b_now
    return out


def total_lead_ms(ch: Chart, countdown_ticks: int = 4) -> float:
    """需要**前置**到音频里的静音总长（ms）。

    第 0 层是"开局站位"（travel = 180° = 一拍），倒计时 (cd−1) 拍，
    所以开谱到第一次按下 = cd × 一拍。
    """
    beat_ms = 60000.0 / max(1e-9, ch.base_bpm)
    t0 = (ch.floors[0].travel / 180.0 * beat_ms) if ch.floors else beat_ms
    return max(0, int(countdown_ticks) - 1) * beat_ms + t0


def game_entry_times(ch: Chart, countdown_ticks: int = 4) -> list[float]:
    """游戏语义下的 entryTime[]（ms，以谱面 t=0 为基准）。

    entryTime[0] = 0
    entryTime[1] = (countdownTicks−1)×拍 + T_0      ← 倒计时只补在第 1 层
    entryTime[j] = entryTime[j−1] + T_{j−1}
    """
    C = 60000.0 / max(1e-9, ch.base_bpm)
    et = times_from_chart(ch)
    add = max(0, int(countdown_ticks) - 1) * C
    return [et[0]] + [e + add for e in et[1:]]


def entry_time_of_onsets(ch: Chart, countdown_ticks: int = 4) -> list[float]:
    """每个 onset 对应的 entryTime（ms，不含 offset）。

    行星开局停在最前面那个「开局站位」方块上，第一次按下才走到第 1 个方块：
        按下 i 的 entryTime = entryTime[i + 1]
    """
    lead = 1 if ch.meta.get("n_lead") else 0
    get = game_entry_times(ch, countdown_ticks)
    return [get[i + lead] for i in range(max(0, len(get) - lead))]


def check_offset(ch: Chart, offset_ms: float, countdown_ticks: int,
                 onset_times_ms: list[float], audio_len_ms: float,
                 shipped_shift_ms: float | None = None) -> dict:
    """自检：把 offset / 倒计时 / 前置静音套上去之后，时间对不对。

    注意时间基准：我们**上架的那份音频**在最前面补了 shipped_shift_ms 的静音，
    所以第 i 个 onset 在成品音频里的时刻是 onset[i] + shipped_shift。
    预测的命中时刻 = offset + entryTime[i+1]。
    """
    S = total_lead_ms(ch, countdown_ticks) if shipped_shift_ms is None else float(shipped_shift_ms)
    et = entry_time_of_onsets(ch, countdown_ticks)
    n = min(len(et), len(onset_times_ms))
    errs = [abs((offset_ms + et[i]) - (onset_times_ms[i] + S)) for i in range(n)]
    all_et = times_from_chart(ch)
    cd_add = max(0, int(countdown_ticks) - 1) * (60000.0 / max(1e-9, ch.base_bpm))
    last_entry = all_et[-1] + cd_add
    return {
        "max_err_ms": max(errs) if errs else 0.0,
        "mean_err_ms": (sum(errs) / len(errs)) if errs else 0.0,
        "n": n,
        "suggest_ms": (onset_times_ms[0] + S - et[0]) if (et and onset_times_ms) else 0.0,
        "offset_plus_last_s": (offset_ms + last_entry) / 1000.0,
        "audio_len_s": audio_len_ms / 1000.0,
        "tail_gap_s": (audio_len_ms - (offset_ms + last_entry)) / 1000.0,
        "lead_ms": S,
    }


def jitter_stats(ch: Chart) -> dict:
    """**抖动**指标 —— 「横平竖直」的真实含义。

    用户口径：「横平竖直不是绝对意义的，而是**降低抖动**。让它必须横平竖直
    就没必要了。」所以要量的不是"有多少条直线"，而是：

      off45     不在 45° 网格上的格占比 —— 这些就是**看得到的微小弧度/抖动**
      off30     不在 30° 网格上的格占比（三连音族要落在 30/15 网格上）
      dev45     到最近 45° 网格线的**平均偏差（度）** —— 越小越"稳"
      distinct  出现的不同 travel 值个数 —— 越少越规整
      wobble    相邻非直线格之间 |Δtravel| 的均值 —— 一格一格乱飘就是它大
    """
    fs = list(ch.floors[1:]) if len(ch.floors) > 1 else []
    if not fs:
        return {"off45": 0.0, "off30": 0.0, "dev45": 0.0,
                "distinct": 0, "wobble": 0.0}
    tv = [float(f.travel) for f in fs]
    n = len(tv)

    def _off(grid: float) -> int:
        return sum(1 for t in tv if abs(t / grid - round(t / grid)) > 1e-6)

    dev = sum(abs(t - 45.0 * round(t / 45.0)) for t in tv) / n
    wb = [abs(tv[i] - tv[i - 1]) for i in range(1, n)
          if not _is_straight(tv[i]) or not _is_straight(tv[i - 1])]
    return {
        "off45": _off(45.0) / n,
        "off30": _off(30.0) / n,
        "dev45": dev,
        "distinct": len({round(t, 4) for t in tv}),
        "wobble": (sum(wb) / len(wb)) if wb else 0.0,
    }


def speed_profile(ch: Chart) -> dict:
    """行星线速度（相对基准）分布 —— 用来判断「鬼畜」程度。

    线速度 ∝ travel(deg) / dt = travel / (travel/180 × 60000/bpm) = bpm × 180/60000
    => 每层的相对速度 = 该层 bpm / base_bpm。没有速度事件时恒等于 1。
    """
    if not ch.floors:
        return {"min": 1.0, "max": 1.0, "events": 0,
                "event_frac": 0.0, "straight_frac": 0.0}
    r = [f.bpm / ch.base_bpm for f in ch.floors]
    fs = ch.floors[1:] or ch.floors
    return {"min": min(r), "max": max(r), "events": ch.n_speed_events,
            "event_frac": ch.n_speed_events / max(1, len(fs)),
            "straight_frac": ch.straight_frac}


def describe(ch: Chart) -> str:
    sp = speed_profile(ch)
    return (f"层={len(ch.floors)}  直线={sp['straight_frac']*100:.0f}%  "
            f"Twirl={ch.n_twirl}  SetSpeed={ch.n_speed_events}  "
            f"base_bpm={ch.base_bpm:g}  速度 {sp['min']:.2f}x~{sp['max']:.2f}x")
