"""参数 schema：Electron 前端的参数面板**由它生成**（数据驱动，不手写 56 个控件）。

为什么要数据驱动：
  - 旧 UI 的 56 个控件是手写的，改一个参数要动 3 处（控件/取值/rebuild 映射），
    这正是"人道主义灾难"的一部分。
  - 这里**默认值一律从 `core` 的 dataclass 实例上取**（`OnsetParams()` /
    `SolveParams()`），所以 UI 默认值与求解器默认值不可能漂移。
  - 每个字段声明它喂给哪个 core 目标（`target`），rebuild 时按 `target` 反向填充，
    映射关系只有一处。

对齐 `docs/18-ui功能清单.md` §1 的控件清单（分区、标签、范围、默认值一一对应）。
"""
from __future__ import annotations

from . import IMPORTS_OK  # noqa: F401  (确保 sys.path 已就绪)

from core.onsets import OnsetParams
from core.solve import SolveParams, SPEED_TIERS, STRAIGHT_PRESETS

_ON = OnsetParams()
_SV = SolveParams()

#: 一条直线 = 几分音符（旧 UI `main_window.py:848` 的 index→拍数映射）
BEAT_BEATS = (0.0, 0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 4.0)
#: 下拉文字与上面一一对应（注意 "1/16 拍" 对应 0.25 拍 = 16 分音符）
REF_LABELS = ("自动（推荐）", "1/16 拍", "1/8 拍", "1/8 附点", "1/4 拍",
              "1/4 附点", "1/2 拍", "1 拍")
#: ★ 旧 UI 的 Twirl 下拉顺序与 `core.solve.TWIRL_MODES` 常量顺序**不同**
TWIRL_LABELS = ("逐步交替（推荐）", "累积限角", "逐步打分", "完全不使用")
TWIRL_VALUES = ("alternate", "accum", "steer", "off")
STRAIGHT_LABELS = ("少（省速度事件）", "平衡（推荐）", "多（尽量直线）")
STRAIGHT_VALUES = ("少", "平衡", "多")           # 取值本身 → STRAIGHT_PRESETS[..]
DPMODE_LABELS = ("中旋双押", "角度双押")
SNOWN_LABELS = ("自动 6~12", "只要 6 重", "只要 8 重", "只要 12 重")
SNOWN_VALUES = ((6, 8, 10, 12), (6,), (8,), (12,))
#: 雪花花瓣形状（`core.snowflake.SHAPES`）
SNOWSHAPE_LABELS = ("自动（穷举取最花）", "均匀", "前重", "后重", "阶梯", "锯齿", "随机采样")
SNOWSHAPE_VALUES = ("auto", "uniform", "front", "back", "step", "zigzag", "random")
LANEMODE_LABELS = ("按音高分位", "按音高线性", "左右交替")
#: 闭合图形使用策略：UI 选 index → `SolveParams.closed_figure_bias`（用户 2026-10）
CLOSED_LABELS = ("更不倾向", "不干预（默认）", "更倾向")
CLOSED_VALUES = (-1, 0, 1)


def _f(key, group, label, type_, *, default, target="", scope="solve",
       min=None, max=None, step=None, suffix="", options=None,
       schedule=True, help="", lanes=False):
    d = {"key": key, "group": group, "label": label, "type": type_,
         "default": default, "target": target, "scope": scope,
         "schedule": schedule, "help": help}
    if min is not None:
        d["min"] = min
    if max is not None:
        d["max"] = max
    if step is not None:
        d["step"] = step
    if suffix:
        d["suffix"] = suffix
    if options is not None:
        d["options"] = options
    if lanes:
        d["wide"] = True
    return d


def _opts(labels, values):
    return [{"label": str(a), "value": b} for a, b in zip(labels, values)]


# --------------------------------------------------------------------- 字段
FIELDS: list[dict] = [
    # ① 文件（动作按钮由前端直接实现，这里只放只读信息位）
    _f("file_info", "file", "当前文件", "info", default="未加载",
       scope="ui", schedule=False),

    # ② 主轨（列表/联动的派生在 session.derive 里，见 docs/18 §2.2、docs/23）
    # ★ 主轨 = **多选**，勾哪几条就采哪几条（并集，按 merge_ms 合并）。
    #   原来的「采音模式 + 单主轨 + 补空白阈值」三件套已退役：并集就是
    #   「哪条有音采哪条」，不再需要"只在主轨空白处补"的收口。
    _f("dp_tol", "tracks", "双押判定窗口", "float", default=45.0, scope="tracks",
       min=1.0, max=400.0, step=1.0, suffix=" ms",
       help="双押轨音头与主音相差多少 ms 内算「同一下」"),
    # ★★ 2026-10：默认从「中旋双押(0)」改成「**角度双押(1)**」。两个理由，都是硬的：
    #   ① **口径**：用户 2026-10 定死「双押不是按毫秒均匀计算的，就是一个在不同 bpm
    #      窗口下均匀使用的**多种规定写法**，**不能自定义角度**」——
    #      那说的就是**角度双押 + 固定写法表**（`use_fixed_dp_angle`，默认开）。
    #      中旋是更早的实现（`docs/18`），默认值一直没跟着口径改过来。
    #   ② **时序**：实测（双押样例 MIDI · 主轨 trk2 = 328 个 166.67ms 的音）——
    #      中旋：导出的谱**按键时刻与原始 onset 差 9.7ms**（`verify_press_subset`，
    #      tol 2ms 直接 FAIL）；角度：**0.0us**。
    #      ⚠ 这个 9.7ms **与「原地惩罚」无关**（`inplace_waste=False` 时一模一样），
    #      是「密集音 + 240bpm 的 250ms 砖 + 中旋只能整砖折返」的固有量化误差。
    #      「开门一遍就能出好谱」正好会踩到它 ⇒ 默认必须选对的那条。
    #   中旋仍然可选（老谱师习惯），只是**不再是默认**。
    _f("dp_mode", "tracks", "写法", "combo", default=1, scope="tracks",
       options=_opts(DPMODE_LABELS, (0, 1)),
       help="**角度双押**（默认，走规定写法表，时序最准）/ **中旋双押**（老写法，"
            "整砖折返；密集音上会有几毫秒量化误差）"),
    # ★ a（docs/31 §5.2）：角度双押在**求解前**替这些 onset 预留槽位 ——
    #   钉死速度档（这一格自己不出 SetSpeed）+ 图形让路。关掉就退回纯后置插入（b）。
    _f("dp_reserve", "tracks", "预留槽位", "bool", default=True, scope="tracks",
       help="角度双押专用：求解前先替双押留好身位，保证「调速落在平格上」。"
            "关掉 = 只靠事后插入（撞到图形就借道或报丢）"),
    _f("dp_theta", "tracks", "薄角 θ", "float", default=0.0, scope="tracks",
       min=0.0, max=90.0, step=1.0, suffix="°",
       help="双押拆出来的薄格角度。**0 = 自动**。★ **只有在「使用固定双押角度」"
            "关掉时才生效** —— 开着时角度由规定写法表决定，**不能自定义**"),
    # ★★ 用户 2026-10 **定死**：「双押不是按照毫秒均匀计算的，就是一个在不同 bpm
    #   窗口下均匀使用的多种规定写法。不能自定义角度」⇒ 这一条**默认开**。
    #   规定写法表（`core/dp_angle.fixed_theta`）：
    #     cbpm ≥ 840 → 90°（**连续双押 ≥ 4 个**仍是 30°）
    #     cbpm ≥ 300 → 30°
    #     cbpm < 300 → 15°
    #   写法结构（`docs/31` §2.2.2）：一个双押 = 两格 `[θ, 180·W − θ]`，
    #     交错(W=1 `[30,150]`) / 斜向(cbpm≥360，两对背靠背) /
    #     双押塔(cbpm<360，W=2 `[30,330]`) / 强弱(W=0.5 `[30,60]`)。
    _f("use_fixed_dp_angle", "tracks", "使用固定双押角度", "check", default=True,
       scope="tracks",
       help="★ 双押角度按**规定写法表**取（不同 bpm 窗口用不同规定写法），"
            "**不按毫秒计算、不能自定义角度**。关掉 = 回到老口径"
            "（薄角 θ / 偏移预算 反解）"),
    # ★★ 用户 2026-10：「**为三押添加开关，可以不一定生成双押**」。
    #   三押 = 同一时刻有 2 条多押轨都在响（`docs/48` §3.1 的判据）。
    #   这一项决定「那 3 条同时响的轨」怎么落：
    #     0 拆   → 拆成 `[θ₁, θ₂, 余量]`（三押，`docs/48` 的行为，默认）
    #     1 不拆 → 只按**双押**插一格（薄角 1 块）
    #     2 跳过 → **连那一格双押也不插**（整组丢掉，报告里记账）
    #   ★ 判据本身（1 + 同时有音的 dp 轨数）不受影响 —— 押数照样数、照样上屏。
    _f("three_press_mode", "tracks", "三押", "combo", default=0, scope="tracks",
       options=_opts(("拆三押（默认）", "不拆（按双押插一格）",
                      "跳过（连双押也不插）"), (0, 1, 2)),
       help="同一时刻有 2 条多押轨同时响时：拆成 3 格 `[θ₁,θ₂,余量]` / 只按双押插一格 / "
            "整组不插。**只在「使用固定双押角度」开着时生效**（老口径没有押数概念）。"
            "不管选哪档，报告里都会写清「押数 3 的有几处、怎么处理的」"),
    # ★★ 用户 2026-10：「**力求所有 bpm 下的双押产生的偏移时值降低到 25ms 以内**」
    #   Δ = (θ/180) × (60000 / 该格 bpm)。`0` = 关掉预算，退回固定 15°/30°。
    _f("dp_skew_max_ms", "tracks", "偏移预算", "float", default=25.0,
       scope="tracks", min=0.0, max=200.0, step=1.0, suffix=" ms",
       help="双押两下的间隔 Δ 必须 ≤ 它（**按每格自己的 bpm 算，慢速档会放大**）。"
            "程序据此反解薄角 θ，bpm 越低 θ 越小。**0 = 关掉预算**（固定 15°/30°）。"
            "★ **只有在「使用固定双押角度」关掉时才生效** —— 开着时角度由规定写法表"
            "决定，Δ 只作为**情报**上报（不当判据）"),
    # ★ 用户 2026-10：「现在的多主轨采音自动合并所有音，但是有时候并不需要全部合并，
    #   建议为主轨添加主次级，权重最高的全采，权重低的只插空」。
    #   主轨（`lst_tracks`，全采、取并集）与次级轨（`lst_sub`，只在主轨的空白缝隙里补）
    #   分开勾；实现直接复用 `core.onsets.notes_fill_gaps`（本来就写好了，
    #   只是当年主轨改成多选之后被退役了）。
    _f("sub_gap_ms", "tracks", "插空阈值", "float", default=600.0, scope="tracks",
       min=0.0, max=5000.0, step=50.0, suffix=" ms",
       help="次级轨只在**主轨连续空白超过这么久**的地方补音。"
            "**调到 0 = 不做缝隙判断（等价于并集）**"),

    # ③ 采音 → OnsetParams
    _f("merge_ms", "onset", "合并窗口", "float", default=_ON.merge_ms,
       min=0.0, max=500.0, step=1.0, suffix=" ms",
       target="OnsetParams.merge_ms", help="间隔小于此值的音合并成一次按键"),
    _f("merge_anchor", "onset", "合并取点", "combo", default=_ON.merge_anchor,
       target="OnsetParams.merge_anchor",
       options=_opts(("first（取最早）", "loudest（取力度最大）"), ("first", "loudest"))),
    _f("min_velocity", "onset", "最小力度", "int", default=_ON.min_velocity,
       min=0, max=127, target="OnsetParams.min_velocity"),
    _f("min_interval_ms", "onset", "最小间隔", "float",
       default=_ON.min_interval_ms, min=0.0, max=2000.0, step=1.0, suffix=" ms",
       target="OnsetParams.min_interval_ms", help="相邻按键最小间隔（稀疏化）"),
    _f("max_onsets", "onset", "最大音数", "int", default=_ON.max_onsets,
       min=0, max=200000, target="OnsetParams.max_onsets", help="0 = 不限"),
    _f("pitch_lo", "onset", "音高下限", "int", default=_ON.pitch_lo,
       min=0, max=127, target="OnsetParams.pitch_lo",
       help="换轨时会被自动改写为该轨（或所选轨并集）的音高下限"),
    _f("pitch_hi", "onset", "音高上限", "int", default=_ON.pitch_hi,
       min=0, max=127, target="OnsetParams.pitch_hi"),

    # ④ 求解 / 几何 → SolveParams
    _f("auto_bpm", "solve", "自动选基准 BPM 与参考音值", "check",
       default=True, target="SolveParams.base_bpm(auto)",
       help="勾上则 base_bpm 保持 0（由 solve() 内部自动），基准 BPM 框只是显示"),
    _f("quantize_rhythm", "solve", "量化到音值网格（推荐）", "check",
       default=_SV.quantize_rhythm, target="SolveParams.quantize_rhythm"),
    _f("base_bpm", "solve", "基准 BPM", "float", default=180.0,
       min=10.0, max=4000.0, step=1.0, suffix=" BPM",
       target="SolveParams.base_bpm",
       help="★ 改这个框 = 明确要自己定 ⇒ 会自动取消上面的「自动选基准 BPM」，"
            "你输的值从此不再被自动值覆盖（自动勾着时这框只是显示）。"),
    _f("ref_index", "solve", "一条直线 = ", "combo", default=0,
       target="SolveParams.beat_beats",
       options=_opts(REF_LABELS, (0, 1, 2, 3, 4, 5, 6, 7)),
       help="自动 = 0.0；映射见 sidecar/schema.BEAT_BEATS"),
    _f("bpm_max", "solve", "BPM 上限", "float", default=_SV.bpm_max,
       min=60.0, max=2000.0, step=1.0, suffix=" BPM",
       target="SolveParams.bpm_max"),
    _f("straight_preset", "solve", "直线优先", "combo", default=1,
       target="SolveParams.straight_weight",
       options=_opts(STRAIGHT_LABELS, (0, 1, 2)),
       help="「一条直线值多少个 SetSpeed 事件」λ：少=3.0 / 平衡=1.5 / 多=0.7"),
    _f("allow_set_speed", "solve", "允许减速档（蜗牛）", "check",
       default=_SV.allow_set_speed, target="SolveParams.allow_set_speed",
       help="关掉则行星绝对匀速、长音被夹断（speed_tiers 只留 1）"),
    _f("twirl_index", "solve", "Twirl 策略", "combo", default=0,
       target="SolveParams.twirl_mode",
       options=_opts(TWIRL_LABELS, (0, 1, 2, 3)),
       help="★ UI 顺序 ≠ core 常量顺序，映射见 schema.TWIRL_VALUES"),
    _f("twirl_limit_deg", "solve", "Twirl 阈值", "float",
       default=_SV.twirl_limit_deg, min=90.0, max=1080.0, step=10.0, suffix="°",
       target="SolveParams.twirl_limit_deg", help="累积限角模式：超过它就翻一次"),
    _f("use_templates", "solve", "用节奏型模板（主体路径）", "check",
       default=_SV.use_templates, target="SolveParams.use_templates"),
    _f("template_only_nonstraight", "solve", "模板只修非直线（保住直线为主）",
       "check", default=_SV.template_only_nonstraight,
       target="SolveParams.template_only_nonstraight"),
    _f("use_pause", "solve", "长休止用 Pause 事件（推荐）", "check",
       default=_SV.use_pause, target="SolveParams.use_pause"),
    _f("pause_min_beats", "solve", "等待拍阈值", "float",
       default=_SV.pause_min_beats, min=0.0, max=64.0, step=0.5, suffix=" 拍",
       target="SolveParams.pause_min_beats",
       help="间隔超过这么多拍就改用**暂停节拍**（travel 180 直线 + Pause），"
            "而不是让行星减速爬过去。默认 1 = 只要比一拍长就暂停；"
            "调到 4 就退回旧行为（1~4 拍的等待会用减速档）"),
    _f("travel_min", "solve", "最小角度", "float",
       default=_SV.travel_min, min=0.0, max=120.0, step=5.0, suffix="°",
       target="SolveParams.travel_min",
       help="非双押格的 travel 不许低于它，避免生成过小的角度影响观感。"
            "双押薄格（15°/30°）与中旋折返格是机制需要的形状，不受此限"),
    # ★★ 用户 2026-10 第二版：「**最小夹角不可以在 30° 以下，最大夹角不能超过
    #   270°**」—— 最小角度之外再要一条**上界**。
    #   `0` = **不设上界**（老口径：软上界 340°）⇒ 默认下产物逐字节不变。
    #   与 `travel_min` 一起构成选档的合法窗口；窗口比值 ≥ 9 > 2，而档位是 2 的幂
    #   ⇒ 数学上必有一个合法档，不会因为设了上界就无解。
    _f("travel_max", "solve", "最大夹角", "float",
       default=_SV.travel_max, min=0.0, max=360.0, step=15.0, suffix="°",
       target="SolveParams.travel_max",
       help="非双押格的 travel 不许高于它（0 = 不设上界，退回老口径）。"
            "配合「最小角度」把角度夹在 [min, max] 里；"
            "真的取不到合法档时**照实报 `travel_above_max`**，不静默"),

    # ★ 闭合图形使用策略（用户 2026-10）：「让程序更倾向于或者更不倾向于使用闭合图形」。
    #   闭合 = Σtravel ≡ 180n（行星走完这一段回到主轴方向，观感上「收得住」）。
    #   ★★ 2026-10 用户：「**我严重怀疑：闭合图形限制，完全没生效**」—— 怀疑成立。
    #   根因：这里是全仓**唯一**一个把「真值」当选项 value 的 combo
    #   （`CLOSED_VALUES = (-1, 0, 1)`），而 `session.params_solve` 是按**下标**解的：
    #       state = -1 → clamp 0 → CLOSED_VALUES[0] = -1   ← 「更不倾向」碰巧对
    #       state =  0 →           CLOSED_VALUES[0] = -1   ← 「不干预」错成 -1
    #       state =  1 →           CLOSED_VALUES[1] =  0   ← 「更倾向」错成 0
    #   于是界面三档实际只产生 {-1, -1, 0} 两个值，用户当然觉得「没生效」。
    #   修法与同组的 `dp_mode` / `ref_index` / `snown_index` 对齐：**value = 下标**。
    _f("closed_bias_index", "solve", "闭合图形", "combo", default=1,
       target="SolveParams.closed_figure_bias",
       options=_opts(CLOSED_LABELS, (0, 1, 2)),
       help="闭合图形 = 一段走完行星回到主轴方向（收得住的花）；不闭合会把朝向带偏，"
            "靠后面的格慢慢扭回来。这条是**同分时的偏好**，不会盖过「少换档 / 走直线」"),
    # ★ 回正（`core/straighten.py`）：2026-10 之前**一个参数都没接到 UI 上** ——
    #   用户看不到也调不了，表现就是「回正逻辑好像死了」。
    _f("straighten", "solve", "角度回正", "check",
       default=_SV.straighten, target="SolveParams.straighten",
       help="把长斜轨尽量掰回主轴方向。**只改 Twirl 符号、不动 travel**，时序零影响"),
    _f("straighten_min_run", "solve", "回正：最小斜轨长", "int",
       default=_SV.straighten_min_run, min=2, max=64, suffix=" 格",
       target="SolveParams.straighten_min_run",
       help="连续多少格偏离主轴才算「值得回正的斜轨」。默认 8 —— 实测很多谱面"
            "一段都凑不出来（那种情况下不动手是对的）；想让它更积极就调小"),
    _f("straighten_theta", "solve", "回正：斜轨判定", "float",
       default=_SV.straighten_theta, min=2.0, max=90.0, step=1.0, suffix="°",
       target="SolveParams.straighten_theta",
       help="朝向偏主轴超过这个角度才算「斜」"),

    # ★ 对音阶梯（`docs/25`）——**并列的第二条落点决策路径**，默认关。
    #   关掉 = 今天的老路径，且逐字节不变（`tests/golden/off_hashes.json`）。
    _f("aggressive_pick", "solve", "使用激进的采音策略", "check",
       default=_SV.aggressive_pick, target="SolveParams.aggressive_pick",
       help="逐音降级对音：已有图形 → 当前速度档 → 加 Twirl 镜像 → 变速后重跑 → 暂停节拍。"
            "关掉 = 现有行为（保持逐字节一致）。打开后会明显更贴拍，"
            "代价是变速与暂停节拍可能变多"),
    _f("ladder_outer_mode", "solve", "激进：绕圈偏好", "combo",
       default=_SV.ladder_outer_mode, target="SolveParams.ladder_outer_mode",
       options=_opts(("自动（慢速绕外圈）", "总是绕外圈", "总是绕内圈"),
                     ("auto", "always", "never")),
       help="速度偏低时优先绕外圈、速度较快时优先绕内圈（分界见下一项）。"
            "「优先」不是禁止：同一格先按偏好试，实在不行才用另一半"),
    _f("ladder_outer_cbpm", "solve", "激进：绕圈分界", "float",
       default=_SV.ladder_outer_cbpm, min=60.0, max=2000.0, step=10.0,
       suffix=" cbpm", target="SolveParams.ladder_outer_cbpm",
       help="自动模式下，cbpm 低于它就优先绕外圈，高于它就优先绕内圈"),
    _f("ladder_tier_order", "solve", "激进：变速优先级", "combo",
       default=_SV.ladder_tier_order, target="SolveParams.ladder_tier_order",
       options=_opts(("保直线优先", "少换档优先", "内圈优先"),
                     ("straight", "switch", "inner")),
       help="第 4 级试档位的顺序。保直线优先 = 先找能把这一格走成直线的档"),
    _f("use_snowflake", "solve", "魔法阵（雪花）", "check",
       default=_SV.use_snowflake, target="SolveParams.use_snowflake"),
    _f("snowflake_min_tiles", "solve", "雪花起用", "int",
       default=_SV.snowflake_min_tiles, min=4, max=400, suffix=" 格",
       target="SolveParams.snowflake_min_tiles"),
    _f("snowflake_full_tiles", "solve", "雪花 100%", "int",
       default=int(_SV.snowflake_full_tiles), min=8, max=2000, suffix=" 格",
       target="SolveParams.snowflake_full_tiles"),
    _f("snown_index", "solve", "雪花对称", "combo", default=0,
       target="SolveParams.snowflake_n_rot",
       options=_opts(SNOWN_LABELS, (0, 1, 2, 3))),
    # ---- 雪花：放开的参数（原来只有上面 4 个，见 docs/24 §4）----
    _f("snowflake_shape", "solve", "雪花形状", "combo", default="auto",
       target="SolveParams.snowflake_shape",
       options=_opts(SNOWSHAPE_LABELS, SNOWSHAPE_VALUES),
       help="花瓣把总转角分摊到各步的方式；自动 = 穷举取最花的一朵"),
    _f("snowflake_min_arms", "solve", "雪花最少步数", "int",
       default=_SV.snowflake_min_arms, min=2, max=24, suffix=" 步/瓣",
       target="SolveParams.snowflake_min_arms",
       help="一条花瓣至少走几步（整朵 = 2×步数×对称阶数 格）"),
    _f("snowflake_compact", "solve", "雪花紧凑优先", "check",
       default=_SV.snowflake_compact, target="SolveParams.snowflake_compact",
       help="同分时优先包围盒小的候选（关掉只看「花不花」）"),
    _f("snowflake_uniform_tol_ms", "solve", "雪花等间隔容差", "float",
       default=_SV.snowflake_uniform_tol_ms, min=0.5, max=60.0, step=0.5,
       suffix=" ms", target="SolveParams.snowflake_uniform_tol_ms",
       help="段内相邻间隔起伏不超过它才算「等间隔段」（下雪花的门槛）"),
    _f("snowflake_random", "solve", "雪花随机形状", "check",
       default=_SV.snowflake_random, target="SolveParams.snowflake_random",
       help="勾上则不用穷举，按种子随机采样（同一份输入+同一种子仍可复现）"),
    _f("snowflake_seed", "solve", "雪花种子", "int",
       default=_SV.snowflake_seed, min=0, max=2147483647,
       target="SolveParams.snowflake_seed"),

    # ★ 轨道位置偏移（`PositionTrack`）：重叠闭合图形的**单格错开**。
    #   用户 2026-10 口径：「**给轨道设置的随机位置偏移 改为可选是否开启**」。
    #   坑：`core/track_fx.py` 早就写好了，`SolveParams.use_position_track`
    #   也早就有（默认 True），**但从来没接到 schema/UI 上** —— 于是用户在
    #   界面上根本关不掉它。这一组就是补这个缺口（见 docs/24 §6）。
    #   它只写渲染事件，**不碰 angleData / bpm / travel ⇒ 时序零影响**。
    #   ★★ 2026-10 用户：「**方块位置偏移的选项可以开局为关闭了**」⇒ **默认关**。
    _f("use_position_track", "solve", "轨道位置偏移（叠在一起的圈错开）", "check",
       default=_SV.use_position_track, target="SolveParams.use_position_track",
       help="同一位置重复出现的格按「第几圈」逐格错开，一眼数得出循环了几遍。"
            "只改渲染、不改几何与时序；**默认关闭** = 所有圈都叠在同一处"),
    _f("pos_track_step", "solve", "错开步长", "float",
       default=_SV.pos_track_step, min=0.02, max=1.0, step=0.02, suffix=" 格",
       target="SolveParams.pos_track_step",
       help="每多一圈向外错开多少格（默认 0.22 ≈ 一格的五分之一强）"),
    _f("pos_track_min_beats", "solve", "错开间隔", "float",
       default=_SV.pos_track_min_beats, min=0.0, max=64.0, step=1.0, suffix=" 拍",
       target="SolveParams.pos_track_min_beats",
       help="只有时间上隔得比这更远的重合格才错开。轨道出现动画会提前 8 拍，"
            "8 拍以内的重合玩家靠「出现先后」就分得清，不必错开"),

    # ★ ③b 采bpm（xk base）—— 定稿 = `docs/47`
    #   打开后：**大直线**（无视 onset、按「N 砖/拍」硬铺等间隔骨架）**取代** ② 主轨采音，
    #   其余音轨**只能**当多押用；与「③ 采音 / 激进策略」在该区间内**互斥**。
    #   φ（格子相位）= ⑤ 那个「偏移」，**同一根轴，不另开**。
    _f("xk_base", "xk", "采bpm（xk base）", "combo", default=0, scope="solve",
       options=_opts(("关（走原路径）", "2k（一拍 2 砖）", "4k（一拍 4 砖）",
                      "8k（一拍 8 砖）"), (0, 2, 4, 8)),
       help="大直线 = 直接采 4 分音、**无视所有音符排列**硬铺等间隔骨架："
            "绝对对拍、绝对不好看 ⇒ 必须配多押用。详见 `docs/47`"),
    _f("xk_tbpm", "xk", "tbpm（0 = 没填）", "float", default=0.0, scope="solve",
       min=0.0, max=400.0, step=0.01, suffix=" bpm",
       help="那个测速站给的**歌曲拍速**；会**四舍六入五成双**取整、不含小数。"
            "cbpm = tbpm × N，砖长 = 60000/cbpm。★ 它夹在 50~210，超 210 会给 "
            "half-time ⇒ 与去噪解出的砖长差 2 的幂时**会报出来**（不静默）"),
    _f("xk_span_help", "xk", "区间外 →", "info", default="",
       help="没框到的段落**走原路径**（② 主轨采音 + 常规求解）。"
            "区间在这里加（起止可写**格子号**或**毫秒**）；每段可各自选 N 与多押轨"),

    # ④b 去噪 / 直拟合（`docs/44`）
    # ★ 为什么单独一组：它是**求解方式**的选择，与「④ 求解/几何」里那些
    #   只影响几何形态的开关不是一个层级 —— 它决定**时序准不准**。
    _f("fit_mode", "fit", "求解方式", "combo", default="solve", scope="solve",
       options=_opts(("最优化（原路：模板/三连音/雪花，会改时序）",
                      "直拟合（一砖一音，时序逐点精确）"), ("solve", "direct")),
       help="最优化 = 像人写的谱（有模板/雪花），代价是量化会挪时序；"
            "直拟合 = 每层时长恒等于 Δt（零漂移），代价是几何只有速度档可调"),
    _f("denoise_on", "fit", "去噪（先吸到格上）", "check", default=True,
       scope="solve",
       help="外部来源（RL 生成/手扒）的时间戳有抖动 ⇒ 先吸到格上再拟合，"
            "travel 才是整齐的有理数。关掉 = 完全不动（老路径）"),
    _f("denoise_hint_ms", "fit", "砖长（0 = 自动）", "float", default=0.0,
       scope="solve", min=0.0, max=5000.0, step=0.5, suffix=" ms",
       help="一拍多少毫秒（= 60000/baseBpm）。0 = 自动：取间隔的对数直方图众数"),
    _f("denoise_div", "fit", "吸附分母", "combo", default=0, scope="solve",
       options=_opts(("自动（最细但不撞格）", "1/1", "1/2", "1/4", "1/8",
                      "1/16", "1/32"), (0, 1, 2, 4, 8, 16, 32)),
       help="与 BDG 顶栏的吸附档位同一套。★ 太粗吸不掉抖动、太细两个音会撞进"
            "同一格（宿主 `addMarker` 会**静默返回 null** = 丢音）"),
    # ★★ 2026-10：**补格**（`docs/71` §9 第 3 条手段 · `core/tilefill.py`）
    #   用户原话：「你可以使用**分段采音设置采bpm**，或者通过**双押轨道**（用分段采音
    #   辅助的）进一步加强」。
    #   为什么需要：从 MIDI 还原**人写的**谱面时，一边一条 onset 一格会明显偏疏
    #   （实测参考 2543 键 vs 我们 1742 键）。人打谱会在间隔里按 bpm 网格再铺几格。
    #   ★ 默认 **0 = 关** ⇒ 老路径逐字节不变（`docs/25` §5.3 的先例）。
    _f("fill_div", "fit", "补格（砖长切几份）", "combo", default=0, scope="solve",
       options=_opts(("关", "1/2", "1/4", "1/6", "1/8"), (0, 2, 4, 6, 8)),
       help="★ 在**够长的**间隔里按「砖长/div」补采音点（砖长由采音结果自己测＝采bpm）。"
            "真实 onset 一个都不丢，补的点只落在间隔内部。关 = 老路径。"),
    _f("fill_min_steps", "fit", "补格：间隔至少几份", "int", default=3, scope="solve",
       min=1, max=32, step=1, suffix=" 份",
       help="间隔短于「几份」就不补（太小会把八分音符也切碎）。实测 3 份最好。"),
    _f("fill_max_per_gap", "fit", "补格：每个间隔最多补几个", "int", default=1,
       scope="solve", min=0, max=64, step=1, suffix=" 个",
       help="★ 1（默认）= 只在够长的间隔里补**一个**点；0 = 铺满整个间隔。"
            "实测铺满会把命中打到 41%%（人写的谱不是处处铺满）。"),
    # ★★ 2026-10 实测最重要的一档门：**和弦摊开**。
    #   在 ADOFAI 里同时响的几个音没法一次按完 ⇒ 人手会把和弦摊成连续的几格。
    #   实测（Flower Rocket，1740 个间隔）间隔起点被补的比例：
    #     单音 18.0% · 2 个音 43.8% · 3 个音 68.3% · 4 个音 57.5%
    _f("fill_min_merged", "fit", "补格：和弦门（至少几个同时音）", "int", default=1,
       scope="solve", min=1, max=8, step=1, suffix=" 个",
       help="★ 2 = **只补和弦起头的间隔**（把单音那 18%% 的噪声挡掉）。"
            "1 = 不看和弦（所有够长的间隔都补）。"),
    _f("fill_per_chord", "fit", "补格：按和弦大小摊开", "check", default=False,
       scope="solve",
       help="★ 开：一个 n 音的和弦摊成 n 格（每间隔最多补 n−1 个）。"
            "与「每个间隔最多补几个」取较小者。"),
    # ★★ 2026-10：短格走直 vs 发卡弯的根因修复（`docs/72` §6）。
    #   `speed_penalty` 按 `|log2 k|` 罚，不封顶 ⇒ **短格想走直必然多花八度**，
    #   实测 r=1/8：直线(180,k=1/8) 得 1−0.35×3 = −0.050，输给发卡弯(22.5,k=1) 的 0.000。
    _f("speed_penalty_max_oct", "fit", "速度档代价封顶（八度）", "float", default=0.0,
       scope="solve", min=0.0, max=6.0, step=0.5, suffix=" 个八度",
       help="★ 0 = **老口径（不封顶）**。设成 2 ⇒ ×8/÷8 档的代价与 ×4 一样，"
            "短格才**走得直**而不是打 157.5° 的发卡弯。"
            "★ **补格开着时会自动设成 2**（并上屏说明）—— 补格正是造出短格的那件事。"),
    # ★★ 2026-10：**降速档**的额外代价（`k < 1`）。语料实测（`docs/13` §5 头号病灶）：
    #   人类谱几乎不用比基准更慢的档 —— P12~P15 的 45 张里 0.5x 只占 0.6~5%，
    #   我们长期 28~32%；P14 同曲参照 Mad Piano Party 两张：0.5x 2.74% / 1x 1.95%。
    #   `gain = 直线?1:0 − speed_penalty·|log2 k|` 在等间隔（r≡0.5，8 分密集段常态）下
    #   给出 `180@0.5x = +0.65` > `90@1x = 0.00` ⇒ 整条路靠**降速换直线**铺成
    #   （直线率 79%，真人 38~41%）。真人的选择是**照常转 90°**。
    _f("slow_speed_penalty", "fit", "降速档额外代价", "float", default=0.0,
       scope="solve", min=0.0, max=6.0, step=0.25, suffix=" /八度",
       help="★ 0 = **老口径**（不额外罚 k<1）。每「比基准慢一个八度」额外扣这么多分。"
            "实测 2.0 左右能把 8 分密集段从「全靠 0.5x 走直线」（直线率 79%）"
            "改成「90° 阶梯为主 + 少量 180°」，与 P12~P15 语料（直线 38%、"
            "0.5x 仅 0.6~5%）同族。**只罚变慢，加速档一个字节不动。**"),
    # ★★ 2026-10：**一档至少连续几层**（`SolveParams.speed_min_run`，core 里一直有、
    #   sidecar 从没接手 ⇒ UI 改不动 = 死参数）。语料实测：P12~P15 的 SetSpeed
    #   中位 **4.16/100 格**（同曲参照 Mad Piano Party 反而 25/100），而默认 6
    #   会把短促的变档并成整块 ⇒ 实测只剩 **0.76/100**。设成 1 = 允许逐个音变档。
    _f("speed_min_run", "fit", "一档至少连续几层", "int", default=6,
       scope="solve", min=1, max=32, step=1, suffix=" 层",
       help="★ 6 = 老口径（把零星的变档并成整块）。1 = 允许**逐格变档**："
            "SetSpeed 会明显变多（语料中位 4.16/100 格，我们默认只有 0.76）。"
            "并档时**不许把直线格变少**（`keep_straight`）。"),
    # ★★ 2026-10 用户：「**去正式接线**，镜头调度中，**已经被验证的国士无双式写法允许先接线**，
    #   标记为「**（new）镜头调度**」，**目前只给出一个选项「呼吸」**。
    #   **其他方案等待完全成熟后接入**。**允许设置各种生成时参数**，
    #   **默认收起**，单击选项卡下方的三角形标识展开。」
    #   ⇒ 规格 = `docs/70`；实现 = `core/camera.py`；接线 = `sidecar.session` 步骤 8e。
    #   ★ `camera_mode` 默认 **0 = 关** ⇒ 一个事件都不发、settings 一个字节不动。
    _f("camera_mode", "camera", "镜头调度", "combo", default=0, scope="solve",
       options=_opts(("关", "呼吸"), (0, 1)),
       help="★「（new）镜头调度」。**目前只有「呼吸」**（国士无双式漂移 + 呼吸，`docs/70`）："
            "位置/旋转慢漂移永不停、缩放两层呼吸；零过冲 ease、永不静止。"
            "**其他方案（聚焦 / 折弯循环 / 结尾聚焦球）等完全成熟再接**。"),
    _f("camera_step_beats", "camera", "网格步长", "float", default=8.0,
       scope="solve", min=1.0, max=64.0, step=0.5, suffix=" 拍",
       help="每几拍发一条。落格后每步约 0.5s；再密就「跳」，再疏就「顿」。"),
    _f("camera_dur_beats", "camera", "每条时长", "float", default=16.0,
       scope="solve", min=1.0, max=128.0, step=1.0, suffix=" 拍",
       help="★ 必须 = 步长 × 2 才「任意时刻都有补间在跑 ⇒ 永不静止」。"),
    _f("camera_drift_pos", "camera", "漂移·位置", "check", default=True, scope="solve",
       help="两组不同周期的正弦叠在 x/y 上（双周期才是「漂」，单周期会看成摆动）。"),
    _f("camera_drift_rot", "camera", "漂移·旋转", "check", default=True, scope="solve",
       help="旋转上叠两组正弦，峰峰 ±6°。"),
    _f("camera_pos_gain", "camera", "漂移幅度倍率（位置）", "float", default=1.0,
       scope="solve", min=0.0, max=4.0, step=0.05,
       help="1.0 = 原公式（x 峰峰 3.0 格 / y 峰峰 2.4 格）。"),
    _f("camera_rot_gain", "camera", "漂移幅度倍率（旋转）", "float", default=1.0,
       scope="solve", min=0.0, max=4.0, step=0.05, help="1.0 = 原公式（峰峰 ±6°）。"),
    _f("camera_breath", "camera", "呼吸（缩放）", "check", default=True, scope="solve",
       help="慢基线（15s 一个来回）+ 逐条交替的快呼吸；`(−1)^k` 挂在网格序号上，与网格锁相。"),
    _f("camera_zoom_mid", "camera", "缩放中位", "float", default=220.0, scope="solve",
       min=1.0, max=2000.0, step=5.0,
       help="★ 公式里的 220 是在**基准 zoom = 250** 的谱面上标定的；"
            "本实现按 `× 基准zoom/250` 换算 ⇒ 换的是「相对基准位的观感」。"),
    _f("camera_zoom_amp", "camera", "慢基线摆幅", "float", default=45.0, scope="solve",
       min=0.0, max=500.0, step=5.0, help="15 秒一个来回。0 = 只要快呼吸。"),
    _f("camera_zoom_period_s", "camera", "慢基线周期", "float", default=15.0,
       scope="solve", min=1.0, max=120.0, step=0.5, suffix=" s", help="慢基线几秒一个来回。"),
    _f("camera_zoom_phase", "camera", "慢基线相位", "float", default=0.7, scope="solve",
       min=0.0, max=6.2832, step=0.1, help="错开相位，避免与漂移同相一起冲。"),
    _f("camera_breath_eps", "camera", "呼吸幅度 ε", "float", default=0.11, scope="solve",
       min=0.0, max=1.0, step=0.01,
       help="逐条交替 `(−1)^k`。0.11 ⇒ 每 1.07s 一涨一落。0 = 只有慢基线（会像「呼吸不畅」）。"),
    _f("camera_from_floor", "camera", "从第几格起", "int", default=0, scope="solve",
       min=0, max=2000000, step=1, help="0 = 从头。"),
    _f("camera_to_floor", "camera", "到第几格止", "int", default=0, scope="solve",
       min=0, max=2000000, step=1, help="0 = 到结尾。"),
    _f("camera_max_events", "camera", "最多几条", "int", default=4000, scope="solve",
       min=1, max=200000, step=100, help="撞上限会**报出来**（不静默截断）。"),
    _f("camera_base_zoom", "camera", "基准 zoom", "float", default=200.0, scope="solve",
       min=1.0, max=2000.0, step=5.0,
       help="★ 谱面基准位（写进 settings.zoom）。镜头事件是**绝对位姿**，"
            "基准位不对齐整段就偏。呼吸的换算也用它。"),
    _f("camera_base_pos", "camera", "基准位置", "text", default="0,0", scope="solve",
       help="谱面基准位 `x,y`（单位 = 格，写进 settings.position）。"),
    _f("camera_base_rot", "camera", "基准旋转", "float", default=0.0, scope="solve",
       min=-360.0, max=360.0, step=1.0, suffix="°",
       help="谱面基准位（写进 settings.rotation）。"),
    _f("fill_max_add", "fit", "补格：最多补几个", "int", default=20000, scope="solve",
       min=0, max=200000, step=500, suffix=" 个",
       help="0 = 不限。撞上限会**报出来**（不静默截断）。"),
    # ★★ 2026-10 用户：「我们需要**拟合容差**来尽可能的杀死抖动，0~100 按照毫秒输入和计算，
    #   拟合容差尽可能把正负 0~100ms 的抖动修正至常规线」+「加一个**使用激进的拟合策略**，
    #   其效果为：拟合的时候**最小角度为 15°**，也就是只允许 15 30 45 60 75 90……往后」。
    #   旧字段叫 `denoise_radius_ms`（0 = 全吸）—— 与「容差」的直觉相反（0 最凶），已按
    #   用户口径改正：**0 = 一个点都不挪**。详见 `docs/57`。
    _f("fit_tol_ms", "fit", "拟合容差", "float", default=100.0,
       scope="solve", min=0.0, max=100.0, step=1.0, suffix=" ms",
       help="★ 把 ±容差 之内的抖动**修正到常规线**上（单位毫秒）。"
            "0 = 一个点都不挪（保真）；值越大修得越狠，**超出的原样保留并报出来**。"
            "去噪时它 = 吸附半径；开了下面的「激进拟合」后它 = 吸到 15° 阶梯上的挪动预算。"),
    _f("aggressive_fit", "fit", "使用激进的拟合策略", "check", default=False,
       scope="solve",
       help="★ 只对**直拟合**有效：把「常规线」换成 **15° 阶梯** —— 非双押格的角度"
            "全部落在 15 30 45 60 75 90…（15° 的整数倍）上，并把「最小角度」钉成 15°。"
            "挪动量仍受「拟合容差」限制；挪不动的原样保留并报出来。"
            "代价：时间会被动（每个音 ≤ 容差），且这些点不再落在 BDG 的 1/2^k 吸附格上"
            "（投到编辑器时别开吸附）。"),
    _f("anchor_from_grid", "fit", "桥接锚用格相位", "check", default=True,
       scope="solve",
       help="直拟合+去噪时把 φ（格相位）当 BDG 的 offsetMs ⇒ 宿主的 beatOfTime() "
            "恰好是 k/div（整数格）。关掉就用 .adofai 的 offset（旧行为）"),
    # ★ 选档策略（`docs/46`）：「最小角度 / 直线优先 / 一档最少层数」只对 DP 有效；
    #   「粘住上一档」是保真档（尽量不动速度档，但密集处会留下小角度格子）。
    _f("tier_mode", "fit", "选档策略", "combo", default="dp", scope="solve",
       options=_opts(("DP（用④里的最小角度/直线优先/最少层数）",
                      "粘住上一档（保真：尽量不动速度档）"), ("dp", "sticky")),
       help="两条路谱面形状差别很大 ⇒ 建议拿自己那份数据各生成一次看预览再定。"
            "DP 会让密集处**加速**把小格子铺直；粘住档位则宁可留小角度也不换档"),

    # ⑤ 时序 / 导出
    _f("countdown_ticks", "export", "countdownTicks", "int", default=4,
       min=1, max=12, target="export.countdown_ticks"),
    _f("separate_countdown", "export", "倒计时与歌曲分开计时", "check",
       default=False, target="export.separate_countdown"),
    # ★★ 2026-10 用户两次进游戏实测后**改定的是事件选型**（别再退回 RecolorTrack）：
    #   「我期望他是『**设置**轨道颜色』而不是『**重新设置**轨道颜色』，后者必须是走过后才展示」
    #   +「去游戏源码里找找『**仅作用于当前方块**』的事件」
    #   ⇒ 用 **`ColorTrack` + `justThisTile:true`**（游戏里就叫「设置轨道颜色」）：
    #      · `scnGame.cs:379-461` 建关时**逐格预演**，颜色直接写进那一格 ⇒ **一出现就是该色**；
    #      · `scnGame.cs:447-461`：`justThisTile` 为真 ⇒ **不写回全局当前颜色** ⇒ 只改这一格；
    #      · 编辑器自己的「单格粘贴」正是这套（`scnEditor.PasteTrackColorSingleTile`）。
    _f("color_schedule", "color", "换手押上色", "check", default=True, scope="solve",
       help="★ 只有**换手押**（每 2 个普通格夹 1 个双押，即 `OOX` 循环）才染一色，"
            "常规格一律不动；颜色固定 = **黑底白边霓虹**（Glow + 主 000000 / 副 ffffff + Neon）。"
            "写的是 **`ColorTrack` + `justThisTile:true`**（= 游戏里的「设置轨道颜色」+"
            "「仅作用于当前方块」）⇒ 那格**一出现就是霓虹**，且**一格一条、不堆叠**。"
            "关掉 ⇒ 一条事件都不写，导出与旧版**逐字节相同**。"
            "只写 actions，绝不碰 angleData/bpm/travel/Twirl ⇒ 时序零影响"),
    _f("handswitch_gap_tiles", "color", "双押间隔普通格", "int", default=2,
       scope="solve", min=0, max=8, step=1,
       help="相邻两个双押之间**必须恰好**夹这么多普通格（`OOX` 里的那两个 O）。"
            "默认 2 —— 这是用户样例实测的结构（每 4 格一个双押）"),
    _f("handswitch_min_cycles", "color", "换手押最少周期", "int", default=2,
       scope="solve", min=1, max=16, step=1,
       help="连续多少个周期才算换手押段（默认 2 = `O O X O O X`）。"
            "1 = 孤立的单个双押也算换手押（不推荐，会误伤）"),
    # ★★ 2026-10 用户：「**换手的意义在于：只在换的那个格子进行染色**」并给了记号串
    #   `XOODOOXOODOO`（X=双押 / O=普通格 / **D=染色的那个双押**）
    #   ⇒ **每 2 个双押染 1 个**，且从链内**第 2 个**开始。
    _f("handswitch_color_every", "color", "每几个双押染一个", "int", default=2,
       scope="solve", min=1, max=8, step=1,
       help="★ 用户定义（记号串 `X O O D O O`）：**换手**只在每两个双押里的一格上发生，"
            "所以**只染那一格**。默认 2（每隔一个染）。"
            "1 = 每个双押都染（那就不叫『只在换的那个格子』了）"),
    _f("handswitch_color_phase", "color", "从链内第几个开始", "combo", default=2,
       scope="solve",
       options=_opts(("从第 1 个双押开始", "从第 2 个双押开始"), (1, 2)),
       help="★ 用户记号串是 `X O O D O O …` ⇒ 链内**第 2 个**才是 D（默认 2）。"
            "想反过来标就从第 1 个开始"),
    _f("handswitch_color_span", "color", "上色范围", "combo", default="thin",
       scope="solve",
       options=_opts(("只染薄格（1 格）", "整个双押组（2 格）"), ("thin", "group")),
       help="★ 用户定稿 = **只染薄格那一格**（一条 `ColorTrack` 就够）。"
            "另一档给整个双押组两格都发（⇒ 两条事件，仍是 `justThisTile`、一格一条）"),
    _f("handswitch_track_style", "color", "霓虹样式", "combo", default="Neon",
       scope="solve",
       options=_opts(("Neon", "NeonLight", "Standard", "Basic", "Minimal", "Gems"),
                     ("Neon", "NeonLight", "Standard", "Basic", "Minimal", "Gems")),
       help="`trackStyle`。语料里「一黑一白」的写法 97.7% 用 Single、样式以 Minimal/Standard 为多，"
            "但用户要的是霓虹观感 ⇒ 默认 Neon"),
    _f("handswitch_color_type", "color", "颜色类型", "combo", default="Glow",
       scope="solve",
       options=_opts(("Glow（主底 + 副边）", "Single", "Stripes", "Blink", "Switch",
                      "Rainbow", "Volume"),
                     ("Glow", "Single", "Stripes", "Blink", "Switch", "Rainbow", "Volume")),
       help="`trackColorType`。默认 **Glow** —— 它同时吃主色与副色，"
            "正好表达用户要的「黑底白边」"),
    # ================================================== ⑤d 演出（入场 / 离场）（`docs/62`）
    # ★★ 用户 2026-09 口径（逐字）：「让**用户自己选择预设的入场和出场效果**。允许**添加分段**，
    #   并且在当前生成的谱面里面**使用当前生成的谱面格子数定义**。否则，都只使用**预设的
    #   入场+出场**。特殊的，**三连音的写法中，三连音被特殊标出**。**不选择就自动用 QE 的写法**。」
    # ★ 逐格优先级：① 用户分段 → ② 自动标出的三连音段 → ③ 全局预设（见 `core/show.py`）。
    # ★ 只写 `actions` 里的 `MoveTrack`（+ 抬 `settings.beatsAhead`），**绝不碰** angleData/bpm/
    #   travel/Twirl ⇒ 与 ⑤b / ⑤c 同级，时序零影响。
    _f("show_schedule", "show", "入场 / 离场演出", "check", default=True, scope="solve",
       help="★ 只写**渲染事件**（`MoveTrack`）⇒ **绝不碰** angleData / bpm / travel / Twirl。"
            "出场 = 玩家踩到某格时把**身后那一格**踹走（`span[-1]`，与真实高质量谱的"
            "「甩走」前瞻中位 `−1` 完全一致）。"
            "入场 = 前方 `提前量` 格处先摆成初态、再缓动拉回常态（**两条事件**）。"
            "关掉 ⇒ 一条事件都不写，导出与旧版**逐字节相同**"),
    _f("show_out_move", "show", "预设出场效果", "combo", default="出A", scope="solve",
       options=_opts(("出A · 有力（一顿后缩成 0 甩走）",
                      "出B · 无痕（原地缩没，不位移）",
                      "出C · 含蓄（慢淡出 + 轻飘）",
                      "出D · 炸（最后一拍猛飞右下）",
                      "无（不上出场）"),
                     ("出A", "出B", "出C", "出D", "none")),
       help="没有分段、也不是三连音的格子**全用它**。四招参数见 `docs/62 §1.1`；"
            "表现对照见 §1.0（出A 最有力 / 出B 最不抢戏 / 出C 最含蓄 / 出D 最炸）"),
    _f("show_in_move", "show", "预设入场效果", "combo", default="入A", scope="solve",
       options=_opts(("入A · 大（右上飞入 + 冲过头弹回）",
                      "入B · 干脆（上方旋落 + 落地回弹）",
                      "无（不上入场）"),
                     ("入A", "入B", "none")),
       help="入场 = **初态 + 回位**两条写在同一个触发格上，目标 = 触发格 + 提前量"),
    _f("show_lead", "show", "入场提前量", "int", default=10, scope="solve",
       min=1, max=64, step=1, suffix=" 格",
       help="★ **定稿 10**（用户口径「10」）。取证：真实高质量特效谱里「归位」事件的前瞻"
            "格数中位是 **+13**、10%~90% 分位 7~44（14 张最大谱 / 81827 条 MoveTrack）。"
            "⚠ 提前量变大时 `beatsAhead` 必须跟着抬（= 提前量 + 余量），否则整套飞行动画会播在"
            "**方块出现之前** ⇒ 方块凭空出现在终点。这条由 `core/show.py` 报给 `writer`，自动取 max"),
    _f("show_margin", "show", "入场余量", "float", default=4.0, scope="solve",
       min=0.0, max=16.0, step=1.0, suffix=" 拍",
       help="回位动画比「玩家踩到触发格」早多少拍起飞（`angleOffset = -180×余量`）。"
            "`0` = 模板原样（飞完正好踩上，观感差），**定稿 4 拍**"),
    _f("show_triplet", "show", "三连音段", "combo", default="qe", scope="solve",
       options=_opts(("自动标出 + 用反向 QE（推荐）", "跟随预设（不特殊处理）",
                      "三连音段整段不上演出"),
                     ("qe", "inherit", "none")),
       help="★ 用户：「**三连音的写法中，三连音被特殊标出**。**不选择就自动用 QE 的写法**」。"
            "`Floor.engine`（求解侧打的段标签）连着的一片就是三连音段 —— 它会**自动标出**"
            "（报告 + 段带记号），默认整段换成反向 QE"),
    _f("show_qe_g", "show", "三连音分组", "combo", default="组3", scope="solve",
       options=_opts(("逐格（1 格一条）", "一次 2 格", "一次 3 格（三连音）", "一次 4 格"),
                     ("逐格", "组2", "组3", "组4")),
       help="反向 QE：一条事件一次罩几格。**定稿三连音用「一次 3 格」**"
            "（用户：「QE 的写法很适合三连音那种，**三个三个轨道地出来**」）"),
    # ================================================== ⑤c 算法轨道调度（`docs/60`）
    # ★★ 用户 2026-10 三问的答复（逐字见 `docs/60` §11）：
    #   · 驱动 = **谱面结构**（图形 / 密度），不用 onset ⇒ 不依赖采音质量
    #   · 皮肤 = **Neon 打底**（`settings` 层，0 条 action 就能摆脱"编辑器默认方块"）
    #   · 顺带做 `ScaleRadius`；`Hide` **不做**（归"雪花"那个功能）
    _f("appearance_schedule", "appear", "算法轨道调度", "check", default=True, scope="solve",
       help="★ 只写**渲染**（`settings` 皮肤 + `actions` 里的 `RecolorTrack`/`ScaleRadius`），"
            "**绝不碰** angleData/bpm/travel/Twirl ⇒ 时序零影响。"
            "皮肤把 `trackStyle` 换成 Neon（语料里事件侧用得最多的样式）+ Glow + Forward 脉冲，"
            "整谱就不再是「编辑器默认方块」。"
            "涟漪环 = 图形起点向两侧扩散的同心环（Hello2025 实测过一格 171 条的套路）。"
            "半径调度 = 密集段把轨道摊开 125%、稀疏段回到 100%（`ScaleRadius` 是纯显示位移，"
            "星球会跟着走，判定不变）。"
            "关掉 ⇒ 一条事件都不写、settings 一个字节都不动，导出与旧版**逐字节相同**"),
    _f("appearance_skin", "appear", "打底皮肤", "combo", default="Neon", scope="solve",
       options=_opts(("Neon（推荐）", "NeonLight", "Gems", "Minimal", "Standard", "Basic"),
                     ("Neon", "NeonLight", "Gems", "Minimal", "Standard", "Basic")),
       help="`settings.trackStyle`。社区语料 530 份里事件侧用得最多的是 **Neon**（18049 次），"
            "其次是 Minimal / NeonLight / Standard / Basic / Gems。"
            "⚠ 自定义方块贴图（`trackTexture`）**社区语料里非空只有 4 处**，社区基本不用 ⇒ 不做"),
    _f("appearance_glow", "appear", "霓虹发光", "int", default=100, scope="solve",
       min=0, max=100, step=5, suffix="%",
       help="`trackGlowIntensity`。语料默认 100；想要更收敛可以降到 25~50"),
    _f("appearance_pulse", "appear", "颜色脉冲", "combo", default="Forward", scope="solve",
       options=_opts(("无", "前向流动（推荐）", "后向流动"), ("None", "Forward", "Backward")),
       help="`trackColorPulse`。**Forward** 会让颜色沿轨道流动起来 —— 这是 CFM1 的写法"
            "（553 条 `Glow/Neon/Forward`），不用为每个颜色各写一条事件"),
    _f("appearance_ripple", "appear", "涟漪环", "check", default=True, scope="solve",
       help="在**图形起点**（雪花/模板/自然闭合/三连音 段的起点）打一圈向外扩散的白环，"
            "每环晚 `每环延迟` 度（180° = 1 拍）。配方来自 Hello2025 实测："
            "`startTile=[-n] / endTile=[+n] / gapLength=2n-1 / angleOffset=30n`"),
    _f("appearance_ripple_source", "appear", "涟漪触发", "combo", default="figure", scope="solve",
       options=_opts(("图形段起点（全部）", "只雪花段", "只模板段", "只三连音段",
                      "只自然闭合段"),
                     ("figure", "snowflake", "template", "engine", "natural")),
       help="哪些**图形段**的起点触发涟漪（图形 = `Floor` 上那四个互斥标记）"),
    _f("appearance_ripple_rings", "appear", "最多几环", "int", default=16, scope="solve",
       min=1, max=64, step=1,
       help="每处触发最多向外推几环（同格并行度硬上限 128）。"
            "语料峰值是 Hello2025 的 85 环 / 同格 171 条，这里默认保守"),
    _f("appearance_ripple_step", "appear", "每环延迟", "int", default=30, scope="solve",
       min=0, max=180, step=5, suffix="°（180° = 1 拍）",
       help="相邻两环的时间差。30° = 1/6 拍 ⇒ 16 环正好铺 2.7 拍"),
    _f("appearance_radius", "appear", "半径调度", "check", default=True, scope="solve",
       help="`ScaleRadius`：密集段把轨道摊开、稀疏段收回。"
            "反编译实证（`scnGame.cs:636`）它只改**显示坐标**（沿每格行进方向推 "
            "`(1-scale/100)` 格），`startPos` 不动 ⇒ **时序与判定完全不变**"),
    _f("appearance_radius_quiet", "appear", "稀疏半径", "int", default=100, scope="solve",
       min=25, max=125, step=5, suffix="%",
       help="**行星距离（`ScaleRadius`）**稀疏段的档。100 = 原样。"
            "★ 用户 2026-10：「这个事件**最高的阈值应该为 125**」⇒ 上限钉在 125"),
    _f("appearance_radius_dense", "appear", "密集半径", "int", default=125, scope="solve",
       min=25, max=125, step=5, suffix="%",
       help="**行星距离（`ScaleRadius`）**密集段的档 —— 密集时把轨道**摊开**。"
            "★ 用户 2026-10 改定：**最高 125**（原来是 250）。"
            "反编译实证（`scnGame.cs:636`）它只改**显示坐标**（沿每格行进方向推 "
            "`(1-scale/100)` 格），`startPos` 不动 ⇒ **时序与判定完全不变**，星球跟着走。"
            "（语料里 150/250 更常见，但那是别人的用法 —— 本项目以用户口径为准）"),
    _f("appearance_dense_fps", "appear", "密集阈值", "float", default=8.0, scope="solve",
       min=1.0, max=40.0, step=0.5, suffix="音/秒",
       help="★ 密度单位 = **音/秒**（纯实时，与曲子 BPM / 标注无关）：≥ 此值算「密」"
            "（切到密集半径）。实测参考：ASGORE 那 60s 平均 14 音/秒、峰值 28；"
            "我们自己的样例谱多在 4~5 音/秒。"
            "⚠ 第一版用「格/拍」是**错的**：拍轴只吃 travel/speed_k、不吃 base_bpm，"
            "同一首音乐标成 122.5 还是 980 BPM 会差 8 倍，实测闪出 4 格（见 `docs/60` §14）"),
    _f("appearance_quiet_fps", "appear", "回落阈值", "float", default=5.0, scope="solve",
       min=0.0, max=40.0, step=0.5, suffix="音/秒",
       help="≤ 此值才算回「疏」（迟滞：和上面的密集阈值分开，避免在阈值上来回抖）。"
            "之间那段**保持现状**（不写事件）"),
    _f("appearance_radius_min_sec", "appear", "最短驻留", "float", default=1.0, scope="solve",
       min=0.0, max=10.0, step=0.5, suffix="秒",
       help="★ 进入密集后至少撑这么久才允许回落（防抖）。"
            "**必须同时**满足「最短驻留」与「最短格数」—— 只有拍数是不够的："
            "980 BPM 下 4 拍 = 4 格，实测就闪了那一下"),
    _f("appearance_density_window", "appear", "密度窗口", "float", default=2.0, scope="solve",
       min=0.5, max=16.0, step=0.5, suffix="秒",
       help="算密度时的滑动窗口（±一半，单位**秒**）。窗口越小越「跟手」，越大越平滑"),
    _f("song", "export", "曲名", "text", default="", scope="export",
       schedule=False, target="export.song", help="同时决定导出子目录名"),
    _f("artist", "export", "艺术家", "text", default="", scope="export",
       schedule=False, target="export.artist"),
    _f("author", "export", "作者", "text", default="ADOFAI Chart Generator",
       scope="export", schedule=False, target="export.author"),
    # ★★ 2026-10「开门一遍」（用户：「力求打开软件之后**一遍就可以生成出优秀的铺面**供游玩」）：
    #   这一项**改成默认开**。理由：MIDI 那一路交出去的音频是 `synth.render(...,
    #   lead_ms=preview_lead_ms)` —— 音频**前面补了静音**，而 `offset` 必须把这段
    #   静音算进去（`offset = 首个 onset + S − 第一格 entry`）。默认 0 的话，
    #   **导出的谱在游戏里会整体错位 S 毫秒**（实测 FallenEra S=667ms、Automaton 649ms）
    #   —— 「一遍就能玩」直接破功。
    #   ⚠ 想手改 offset 也行：前端会记「你手动碰过 offset 了」，
    #     之后**不再**用自动值覆盖你的输入（见 `app.js` 的 `offsetTouched`）。
    # ★★ 2026-10-05 主人拍板：自动 offset 的口径**简化**成「采出来的第一个 onset」
    #   （`onsets_sec[0] × 1000`，ms 原样取），不再做 `+ 前置静音 − 第一格` 的补偿。
    #   实测补偿是错的：Amulet 被多扣了 697.3ms（2815.4 → 2118.1），谱面整体提前。
    #   ⚠ 想手改 offset 也行：前端会记「你手动碰过 offset 了」，
    #     之后**不再**用自动值覆盖你的输入（见 `app.js` 的 `offsetTouched`）。
    _f("auto_offset", "export", "自动 = 首个 onset", "check", default=True,
       target="ui.auto_offset",
       help="★ **默认开**（开门一遍就能对上）：offset 自动写成**采出来的第一个 onset**"
            "（采音结果里最上面那个数，单位 ms）。你**手动改过 offset** 之后，"
            "本项不再覆盖你的输入"),
    _f("offset", "export", "offset", "float", default=0.0,
       min=-600000.0, max=600000.0, step=1.0, suffix=" ms",
       target="export.offset_ms"),
    _f("difficulty", "export", "难度", "int", default=0, min=0, max=25,
       scope="export", schedule=False, target="export.difficulty"),
]

# --------------------------------------------------------------- 视图参数（不进 core）
# ★ 原 `chartview` 分组的 4 个字段（cspan/cfollow/ctravel/cbad）已随 2D 谱面预览一起退役：
#   谱面预览现在跑的是内嵌的 Re_ADOJAS 渲染引擎，视角/缩放由它自己管（见 docs/22）。
VIEW_FIELDS: list[dict] = [
    _f("lanes", "fallingview", "轨道数", "combo", default=4, scope="ui",
       schedule=False, options=_opts(("4", "8"), (4, 8))),
    _f("fspeed", "fallingview", "流速", "int", default=100, scope="ui",
       min=10, max=800, schedule=False),
    _f("division", "fallingview", "切分", "combo", default=8, scope="ui",
       schedule=False, options=_opts(("4", "8", "16", "32"), (4, 8, 16, 32))),
    _f("lanemode", "fallingview", "轨道分配", "combo", default=0, scope="ui",
       schedule=False, options=_opts(LANEMODE_LABELS, (0, 1, 2))),
    _f("pfollow", "pathview", "路径跟随", "check", default=True, scope="ui",
       schedule=False),
    # ★ 偏移修正：内嵌 ADOFAJ 播放器自带的「音乐播放延迟补偿」（独立于谱面 offset）。
    #   以前完全没接 —— 库里有这个能力却没暴露过（见 docs/24 §5）。
    _f("music_delay_ms", "chartview", "音乐延迟补偿", "int", default=0,
       scope="ui", schedule=False, min=-500, max=500, step=1, suffix=" ms",
       help="内嵌 ADOFAI 播放器的偏移修正：正值=音乐相对游戏时间线延后播放。"
            "播放中改立即生效，不触发重算谱面"),
    # ★★ 用户 2026-10：「预览时，**建议允许（不强制）使用 ogg**，并允许**调节 ogg 偏移**
    #   来使用**音频混合预览**」。
    #   · `preview_audio_mode` / `preview_audio_path` 走**重算**（要它给 `audio_lead_ms`：
    #     合成音有前置静音 `S = preview_lead_ms`，交出去的文件 `S = 0`；
    #     这两个常量直接决定四个视图与 `<audio>` 的对齐）。
    #   · `preview_audio_offset_ms`（Δ）是**预览专用**的旋钮，**不重算** ——
    #     改一下要立刻听到差别（否则每拖一格都等一遍重算）。它**不写进谱面**。
    _f("preview_audio_mode", "chartview", "预览音源", "combo", default=0,
       scope="ui", options=_opts(("自动（有原曲就用原曲）", "合成音（节拍音）",
                                  "指定文件（选一个 ogg/wav/mp3）"), (0, 1, 2)),
       help="预览放什么声音。**默认「自动」= 今天的行为**（载入 .ogg / BDG 工程带原曲就放原曲，"
            "否则放我们合成的节拍音）。选「指定文件」可以从外面挑一首原曲做**音频混合预览**"),
    _f("preview_audio_path", "chartview", "原曲文件", "path", default="",
       scope="ui", schedule=True,
       help="「预览音源 = 指定文件」时用的那份音频（ogg / oga / wav / flac / mp3）。"
            "点右边「选…」挑文件；换文件会重算一次（轴常量跟着变）"),
    _f("preview_audio_offset_ms", "chartview", "原曲偏移 Δ", "float", default=0.0,
       scope="ui", schedule=False, min=-5000.0, max=5000.0, step=5.0, suffix=" ms",
       help="★ **预览专用**：把原曲整体**推后** Δ 毫秒（负值 = 提前）去和谱面对齐 ——"
            "拖动立即生效、**不重算谱面、不写进 .adofai**。"
            "（成品要也对上，改 ⑤ 的 offset 或开「自动 offset」）"),
]

GROUPS = [
    {"id": "file", "title": "① 文件"},
    {"id": "tracks", "title": "② 主轨（勾选要采音的轨，可多选）"},
    {"id": "onset", "title": "③ 采音"},
    {"id": "xk", "title": "③b 采bpm（xk base · 大直线）"},
    {"id": "solve", "title": "④ 求解 / 几何"},
    {"id": "fit", "title": "④b 去噪 / 直拟合（时序优先）"},
    {"id": "export", "title": "⑤ 时序 / 导出"},
    {"id": "color", "title": "⑤b 换手押上色（轨道颜色调度）"},
    {"id": "appear", "title": "⑤c 算法轨道调度（皮肤 / 涟漪环 / 半径）"},
    {"id": "show", "title": "⑤d 演出（入场 / 离场 · 分段）"},
    {"id": "chartview", "title": "⑥ 谱面预览（偏移修正）"},
    {"id": "fallingview", "title": "下落式 / 路径"},
]

TRACK_LIST_ROLES = {
    "onset": ("lst_tracks", "主轨（可多选）：勾哪几条就采哪几条，取并集"),
    "sub": ("lst_sub", "次级轨（只插空）：只在主轨的空白缝隙里补音"),
    "dp": ("lst_dp", "勾选当作双押轨的轨（不参与主轨并集）"),
}


def defaults() -> dict:
    """全部字段的默认值（前端初始状态）。"""
    out = {}
    for f in FIELDS + VIEW_FIELDS:
        out[f["key"]] = f["default"]
    out["tracks_checked"] = []          # lst_tracks 勾选（轨号）
    out["sub_checked"] = []             # lst_sub 勾选（次级轨：只插空）
    out["dp_checked"] = []              # lst_dp 勾选（轨号）
    out["current_track"] = 0            # 光标轨
    out["dp_tracks_avail"] = []         # 有音的双押轨候选
    # ★ 区间采音：`[{start_ms, end_ms, tracks: [轨号], mode, label, fill_gap_ms}]`
    #   语义 = 这段时间**改用指定音轨采音**，段外仍走全局「② 音轨」。
    out["regions"] = []
    # ★ 分段采音（`docs/34` 方案 C）：`[{at_ms, label, main, sub, dp}]`
    #   三个维度各自 `null` = 继承全局 ② / `[]` = 显式关掉 / `[轨号…]` = 显式指定。
    #   `segment_mode`：`from`（从这点起，默认）/ `until`（到这点为止）。
    #   ★ 有分段时**区间被整个忽略**（分段表达能力严格更强）。
    out["segments"] = []
    # ★ 采bpm 区间（`docs/47` §3）：`[{start_ms|start_tile, end_ms|end_tile, xk_base, tracks}]`
    #   **显式 `[start, end]`**（不是半开）：起止都可以写**格子号**（1 起算）或**毫秒**。
    #   语义 = 这一段用**骨架取代采音**（主轨在该段失效），段外仍走原路径。
    #   ⚠ 重叠 ⇒ 后端**抛错**（猜哪个赢都是错）；首尾相接的交界砖会**去重并报数**。
    out["xk_ranges"] = []
    # ★ 演出分段（⑤d，`docs/62 §4.1`）：`[{lo, hi, in_move, out_move}]`
    #   **起始方块 / 结束方块**用**当前生成谱面的格子号**（和游戏里填 `startTile`/
    #   `endTile` 一个口径），闭区间。段内留空 = 跟随 ⑤d 的预设；`"none"` = 这一侧不上。
    #   优先级：用户分段 > 自动标出的三连音段 > 全局预设。
    out["show_segments"] = []
    out["segment_mode"] = "from"
    return out


def schema() -> dict:
    return {"groups": GROUPS, "fields": FIELDS, "view_fields": VIEW_FIELDS,
            "defaults": defaults(),
            "beat_beats": list(BEAT_BEATS),
            "twirl_values": list(TWIRL_VALUES),
            "straight_values": list(STRAIGHT_VALUES),
            "snown_values": [list(v) for v in SNOWN_VALUES],
            "speed_tiers": list(SPEED_TIERS),
            "straight_presets": dict(STRAIGHT_PRESETS),
            "track_lists": TRACK_LIST_ROLES}
