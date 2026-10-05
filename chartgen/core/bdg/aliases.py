# -*- coding: utf-8 -*-
"""BDG 格式 **唯一** 允许出现「字段名」的地方（`docs/36` §3 / §11.6）。

上游 `BUGJI/beat_data_generator` 改保存格式 ⇒ **只改这个文件**。
其余模块一律 `coerce.take(obj, al.XXX, ..., rep=rep, concept="…")` 取值，
**禁止字面量**（由 `tests/test_bdg_parse.py` 第三组断言守住）。

规矩：
  · 一个「概念」= 一串候选路径（按优先级）。
  · 支持 `"a.b"` 点号路径（如 `loop.interval` 若上游把它拍平）。
  · 加新版本 = 在这张表里**加候选路径**，不是去 `parse.py` 写 `if version == N`。
"""

# ---------------------------------------------------------------- 顶层
ROOT_APP        = ["app", "type", "format"]
ROOT_VERSION    = ["version", "formatVersion", "v"]
ROOT_NAME       = ["name", "title", "projectName"]
ROOT_BASE_BPM   = ["baseBpm", "baseBPM", "bpm", "tempo"]
ROOT_OFFSET     = ["offsetMs", "offset"]
ROOT_AUDIO_NAME = ["audioName", "audio", "audioFile", "songFilename"]
ROOT_AUDIO_MD5  = ["audioMd5", "audioHash", "audioMd5sum"]
ROOT_BPM_LOCKED = ["bpmLocked", "tempoLocked"]
ROOT_TRACKS     = ["tracks", "markerTracks", "lanes"]
ROOT_MARKERS    = ["markers", "points", "events", "beats"]
ROOT_BPM_POINTS = ["bpmPoints", "tempoPoints", "tempo", "bpmEvents"]
ROOT_NOTES      = ["notes", "comments", "annotations"]

# ---------------------------------------------------------------- 轨
TRACK_ID     = ["id", "uid", "key"]
TRACK_NAME   = ["name", "title", "label"]
TRACK_TYPE   = ["type", "kind", "role"]          # ★ 轨道级唯一的自由字段
TRACK_HIDDEN = ["hidden", "muted", "disabled"]   # ★ 整轨不进快照 = 天然「关」档
TRACK_LOCKED = ["locked", "lock"]
TRACK_COLOR  = ["color", "colour"]

# ---------------------------------------------------------------- 点
MARK_ID     = ["id", "uid", "key"]
MARK_TRACK  = ["trackId", "track", "lane", "laneId", "track_id"]
MARK_BEAT   = ["beat", "position", "pos", "t"]
# ★ 实测 v2 **没有**这个字段（timeMs 是 timeOfBeat(beat) 现算的）。
#   上游将来真落一个时间戳时，有就用、没有回落 map 换算 ⇒ 不必改逻辑。
MARK_TIME   = ["timeMs", "time_ms", "time"]
MARK_LOOP   = ["loop", "repeat", "loopConfig"]
MARK_PARENT = ["parentId", "parent", "ownerId"]
MARK_ATTRS  = ["attrs", "attributes", "props", "meta"]

# ---------------------------------------------------------------- 环（loop）
LOOP_INTERVAL = ["interval", "step", "gap", "period"]
LOOP_COUNT    = ["count", "times", "repeats"]
LOOP_EXCLUDE  = ["exclude", "skip", "excludeIndices", "except"]

# ---------------------------------------------------------------- 变速点
BPM_BEAT  = ["beat", "position", "t"]
BPM_MODE  = ["mode", "kind"]
BPM_VALUE = ["value", "bpm", "multiplier", "amount"]

# ================================================================
# 以上是「字段名」。以下是「值」——同样只允许在这里出现字面量。
# ================================================================

APP_ID = "beat-data-generator"

# 变速点 mode 的两个取值（`tempo.ts`）
MODE_ABS  = "abs"
MODE_MULT = "mult"

# 内置音砖轨的 type（`api.ts`：`t.type ?? "beat"`）
TYPE_BUILTIN = "beat"

# 类型化轨 = "<pluginId>:<localId>"
TYPE_SEP = ":"

# 类型化轨的 localId（`<ns>:<localId>`）
PLUGIN_LOCAL_BPM   = "bpm"
PLUGIN_LOCAL_TWIRL = "twirl"

# attrs 里变速插件用的键（类型化 BPM 轨上的点）
# ★ 必须是**候选路径列表**（取值器按表遍历），不是单个字符串。
ATTR_SPEED_TYPE  = ["speedType", "speed", "speedKind"]
ATTR_SPEED_VALUE = ["value", "amount", "speedValue"]
SPEED_TYPE_MULT  = "multiplier"
SPEED_TYPE_ABS   = "absolute"

# ---------------------------------------------------------------- 角色
# `docs/35` §3.6：轨道级唯一的自由字段是 `type` ⇒ 用它承载角色。
ROLE_MAIN = "main"
ROLE_SUB  = "sub"
ROLE_DP   = "dp"
ROLE_OFF  = "off"
BRIDGE_NS = "bridge"

# ★ 实测宿主的插件类型是 `<api.id>:<localId>`（`plugin-api.d.ts` 的 TrackTypeSchema.id
#   由宿主加插件前缀，见 example-basic 里 `api.id + ":flip"`）。
#   ⇒ **只认冒号后面的 localId**，这样我们换插件 id / 命名空间都不会失效。
ROLE_BY_LOCAL = {
    ROLE_MAIN: ROLE_MAIN,
    ROLE_SUB:  ROLE_SUB,
    ROLE_DP:   ROLE_DP,
    ROLE_OFF:  ROLE_OFF,
    # 宿主自带的 ADOFAI 导出插件那两条控制轨：**不是音轨**
    PLUGIN_LOCAL_BPM:   ROLE_OFF,
    PLUGIN_LOCAL_TWIRL: ROLE_OFF,
}

# 内置轨（无 type 或 type == "beat"）= 主音轨
ROLE_BY_TYPE = {
    TYPE_BUILTIN: ROLE_MAIN,
    ROLE_MAIN:    ROLE_MAIN,
    ROLE_SUB:     ROLE_SUB,
    ROLE_DP:      ROLE_DP,
    ROLE_OFF:     ROLE_OFF,
}

# ★ **我们插件**的角色轨 localId —— 分段采音的自动填充只认这四条。
#   与 `ROLE_BY_LOCAL` 的区别：不含宿主自带 ADOFAI 导出插件那两条控制轨
#   （`PLUGIN_LOCAL_BPM/TWIRL`）—— 那上面的点是**变速**，不是
#   「从这儿起这条轨是什么角色」，拿它生成分段会凭空多出成批垃圾段。
ROLE_TRACK_LOCALS = (ROLE_MAIN, ROLE_SUB, ROLE_DP, ROLE_OFF)

# 投送时给轨道起的名字（`docs/38` §3.1）
ROLE_NAME = {
    ROLE_MAIN: "ADO·主轨",
    ROLE_SUB:  "ADO·次轨",
    ROLE_DP:   "ADO·双押轨",
    ROLE_OFF:  "ADO·关轨",
}

# ★★ 泳道 → 轨名（2026-10 · 用户「三押轨道不出现」）。
#   三押不是新**角色**（收回、对账、attrs 的 `adbRole` 全按角色走，加角色要动一大片），
#   它只是**双押泳道内部再分一条**：押数 = 2 进 `dp:`，押数 ≥ 3 进 `dp:3`。
#   角色仍是 `dp` ⇒ 收回/对账/插件那边**逐字节不变**，只是在 BDG 里**看得见**了。
LANE_DP3 = "dp:3"
LANE_NAME = {
    LANE_DP3: "ADO·三押轨",
}
# ★ 显式泳道 → 角色（从泳道键前缀推：`dp:3` → `dp`）。
#   **收回时轨名要能认回角色**（`parse.role_for_name` 走的是名字表）——
#   不认的话「ADO·三押轨」会塌成 `main`：真机实测 67 个三押点被对账判成
#   「删除」，而且它们会被并进主轨采音。角色必须是 `dp`。
LANE_ROLE = {k: k.split(":")[0] for k in LANE_NAME}

# ---------------------------------------------------------------- 展开用
# 补出来的循环子点：id 前缀 / 分隔符（只在我们内部用，不写回上游）
SYNTH_ID_PREFIX = "synth~"
SYNTH_ID_SEP    = "~"

# ---------------------------------------------------------------- 标量词表
# ★ 这些词可能与字段名同形（如 "off" / "ms" / "beat"），所以必须住在这里，
#   兄弟模块才能引用而不写字面量。
BOOL_TRUE_WORDS  = ("1", "true", "yes", "y", "on")
BOOL_FALSE_WORDS = ("0", "false", "no", "n", "off", "")
UNIT_MS  = "ms"
UNIT_S   = "s"
SUB_BAR  = "bar"
SUB_BEAT = "beat"
BAR_BEATS = 4.0            # 宿主写死「4 拍一小节」（tempo.ts）

# ---------------------------------------------------------------- 往返对账
# ★ 我们写进 `marker.attrs` 的同步标记（宿主不用它，只用于「投射→收回」对账）。
#   改名要同时改 `bridge_plugin/renderer.js`（那边是 JS，只能靠这张表对齐）。
SYNC_IDX  = ["adbIdx"]      # 我们的 onset 序号 —— ★ 身份：没了 = 用户删了
SYNC_RUN  = ["adbRun"]      # 本次投送批次号（区分上一批残留）
SYNC_ROLE = ["adbRole"]     # 投送时的角色（用户拖到别的轨 = 改角色）
SYNC_SRC  = ["adbSrc"]      # 来源（midi/audio/manual，可选）
# ★ 这个音来自哪条**源轨**（`docs/42` 分泳道用）。`adbTracks` 只在**跨轨簇**
#   （一次按键是好几条源轨同时响）时出现 —— 那是「归到哪条泳道」只好取最早一条的证据。
SYNC_TRACK  = ["adbTrack"]      # 源轨号（int）
SYNC_TRACKS = ["adbTracks"]     # 跨轨簇：全部源轨号（list[int]）

# 对账结果的状态词表
EDIT_KEPT    = "kept"
EDIT_MOVED   = "moved"
EDIT_DELETED = "deleted"
EDIT_ADDED   = "added"
EDIT_ROLE    = "role_changed"
EDIT_STALE   = "stale"       # 带标记但属于**上一批**的残留

# ================================================================
# ★ 下面不是「上游字段」，是**我们自己的结构键 / 模型属性名**。
#   为什么也放这张表：它们与上游字段**同名**（beat / role / tracks / points / attrs …），
#   放一起才能让「兄弟模块禁止字面量」这条纪律继续成立；
#   而且它们要和 `bridge_plugin/renderer.js` 对齐，一个地方改名 = 两边都看得到。
# ================================================================
K_IDX    = "idx"             # 我们的 onset 序号（也是 Project/Point 的属性名）
K_BEAT   = "beat"
K_ROLE   = "role"
K_RUN    = "run"
K_MS     = "ms"
K_ATTRS  = "attrs"
K_NAME   = "name"
K_CLEAR  = "clear"
K_ONSETS = "onsets"
K_TRACKS = "tracks"
K_POINTS = "points"
K_ADDED  = "added"
K_STALE  = "stale"
K_STATUS = "status"
K_EDITS  = "edits"
K_COUNTS = "counts"
K_INDEX  = "index"
K_NOTES  = "notes"
K_TYPE   = "type"
K_HIDDEN = "hidden"
K_SUGGEST = "suggest"        # 我们**建议**的角色（只是建议，由用户定）
K_MS_LO  = "ms_lo"
K_MS_HI  = "ms_hi"
K_DRUM   = "drum"
K_SUMMARY = "summary"
K_LOOP   = "loop_parents"
K_CHILD  = "loop_children"
K_AT     = "at"
K_MODE   = "mode"
K_VALUE  = "value"
K_PLUGIN_TRACK = "pluginTrack"
# ★ 我们**发给宿主的 import 载荷**里的时序锚（`docs/41` #2）：
#   字段名沿用宿主自己的写法（`baseBpm` / `offsetMs`），所以归在这里。
K_BASE_BPM = "baseBpm"
K_OFFSET_MS = "offsetMs"
# ★ 分泳道（`docs/42`）
K_LANE = "lane"                 # 泳道键 `<role>:<源轨>`
K_SRC_TRACKS = "src_tracks"     # `Sent` 上的源轨（我们内部用）
K_N_LANES = "n_lanes"
K_N_CROSS_TRACK = "n_cross_track"
# ★ 收回（BDG → 我们）：带时值数据的**轨道项目**（`docs/45`）
K_LANES = "lanes"               # 收回的轨道列表
K_LANE_POINTS = "lane_points"
K_SRC_TRACK = "src_track"       # 单数：这条泳道的源轨号
K_ANCHOR = "anchor"             # 对端报过来的时序锚 {baseBpm, offsetMs}
K_N_POINTS = "n_points"
K_N_ADDED = "n_added"           # 用户新加的点（没有我们的对账标记）
K_N_DUP = "n_dup"
K_N_SKIPPED = "n_skipped"
K_TEXT = "text"                 # 人话报告
K_LANES_BACK = "lanes_back"     # 「现在用的是收回的轨道」这个开关

# ---------------------------------------------------------------- 解析器标签
# 写进报告，说明这份数据是走哪条路下来的（§6）。
PARSER_STANDARD = "standard"
PARSER_NESTED   = "nested"    # 上游把工程多包了一层 {project:{…}}
PARSER_SNAPSHOT = "snapshot"  # ★ 插件 `api.project.snapshot()`：**没有版本号**，但点自带 timeMs
PARSER_GENERIC  = "generic"   # 形态整个变了，只靠「带 beat 的对象列表」救
PARSER_BROKEN   = "broken"    # 连 dict 都不是 / JSON 截断

# ---------------------------------------------------------------- 写回精度
# 宿主 store.ts:315 `const round = (b) => Math.round(b * 1e6) / 1e6`
# ⇒ **关吸附路径的精度是 1e-6 拍**，不是整拍！（我原来读错了）
PRECISION_DIGITS = 6

# ---------------------------------------------------------------- 写回路径
# 写回时该走哪条（`snap.plan` 的结论）
SNAP_PLAN_OFF      = "route:no-snap"      # 必须走不吸附路径
SNAP_PLAN_SAFE     = "route:snap-ok"      # 有档位完全不挪点
SNAP_PLAN_READBACK = "route:snap+verify"  # 可吸附，但必须回读比对
