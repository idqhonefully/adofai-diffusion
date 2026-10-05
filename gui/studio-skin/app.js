/**
 * 渲染进程主逻辑（对等旧 `ui/main_window.py` 的 UI 部分）。
 *
 * 分工：**所有业务派生都在 Python sidecar**（`docs/18` 是施工图），
 * 这里只做三件事：①按 schema 生成参数面板 ②把状态发给 sidecar ③把回来的
 * 数据画出来 + 处理播放。
 *
 * 三个必须复刻的旧 UI 行为（`docs/18` §6）：
 *  · **140ms 防抖重建**：任何「进求解」的控件变化都走 `schedule()`，
 *    另外 `ed_song/artist/author/sp_diff` **不**进防抖（只在导出时读）。
 *  · **程序写值不触发重建**：offset / base_bpm 会被 sidecar 回填，
 *    写回时必须打 `applying` 标记，否则「offset 变→重建→改写 offset」死循环。
 *  · **滚轮不抢焦**：只有已聚焦的数字框才响应滚轮（旧 UI 是 QApplication 级
 *    事件过滤器 `NoWheelFilter`），这里用 document 级 capture 监听统一实现。
 */
import { api } from './api.js';
import { PianoRoll } from './views/roll.js';
import { PathView } from './views/path.js';
import { FallingView } from './views/falling.js';
import { Overview } from './views/overview.js';
import { Band } from './views/band.js';
import { bootLayout, applyLayout, clampAllFloats, layout,
         groupOpenState, setGroupOpen, applyGroupOpenState, DEF_LAYOUT } from './layout.js';

const $ = (s) => document.querySelector(s);
const el = (tag, cls) => { const d = document.createElement(tag); if (cls) d.className = cls; return d; };

// ---------------------------------------------------------------- 状态
let SCHEMA = null;
let state = {};
let loadInfo = null;
let payload = null;
let applying = false;          // 程序写值期间不打防抖
let rebuildTimer = null;
let lastAutoOffset = null;
let hasChart = false;
let rebuildCount = 0;          // 重建次数（e2e 用它证「自动 offset 不回环」）
let progressEvents = 0;        // 收到几条 SSE 进度（e2e 用它证长任务通道通）

// ★ 谱面预览不再是这里的 canvas 2D 视图：它由内嵌的 Re_ADOJAS 播放器承担
//   （见 ensurePreview / docs/22）。旧实现归档在 `_archive/chart-view-2d/`。
const views = {
  roll: new PianoRoll($('#cv-roll')),
  path: new PathView($('#cv-path')),
  falling: new FallingView($('#cv-falling')),
};
views.roll.onSeek = (ms) => seek(ms);

/** ★ 全曲预览条：拖动=定位、Shift+拖动=框选区间、滚轮=缩放、双击=复位。 */
const overview = new Overview($('#cv-overview'));
overview.onSeek = (ms) => seek(ms);
overview.onRegion = (t0, t1) => addRegion(t0, t1);
overview.onSelectRegion = (i) => { selectRegion(i); };
// 全曲条选中的「格」直接喂给 ADOFAI 播放器（它的 selectTile 就是同一个语义）
overview.onSelectFloor = (i) => {
  if (i === null || !preview) return;
  try { preview.selectTile(i, true); } catch (_e) { /* 未就绪时忽略 */ }
};
// ★★ 段带（docs/49 方案 A）：段色块 + 标记（双押/三押）+ 播放头；拖边界改时间
const band = new Band($('#cv-band'), {
  frozen: () => document.body.classList.contains('layoutmode'),
  onSeek: (ms) => seek(ms),
  ranges: () => bandRanges(),
  dp: () => bandDp(),
  color: () => bandColor(),
  // ★★ 算法轨道调度（`docs/60`）：涟漪环触发点 + 半径切换点（画在颜色记号之上）
  appear: () => bandAppear(),
  onEditRanges: (ref, t0, t1) => bandEdit(ref, t0, t1),
  onSelectRange: (ref) => bandSelect(ref),
});

/* ============================================================ ★ 段带的数据口
   一切都在**采音轴**（payload.hit / payload.entry 那套轴；docs/24 §5）。
   段带只画、只改「时间」，不碰任何业务派生 —— 业务派生全在 sidecar。 */
function bandRanges() {
  const out = { regions: [], xk: [], segments: [] };
  const dur = payload ? payload.total_ms : 0;
  (state.regions || []).forEach((rg, i) => {
    const t0 = Number(rg.start_ms) || 0;
    const t1 = (rg.end_ms === null || rg.end_ms === undefined) ? dur : Number(rg.end_ms);
    out.regions.push({ i, t0, t1, label: rg.label || `区间${i + 1}` });
  });
  (state.xk_ranges || []).forEach((rg, i) => {
    const t0 = xkRangeMs(rg, 'start'), t1 = xkRangeMs(rg, 'end');
    if (t0 === null || t1 === null) return;          // 格号算不出来就跳过（不瞎画）
    out.xk.push({ i, t0, t1: t1 === null ? dur : t1, label: rg.label || `采bpm${i + 1}` });
  });
  const sgs = state.segments || [];
  sgs.forEach((sg, i) => {
    const at = Number(sg.at_ms) || 0;
    let t0 = at, t1;
    if ((state.segment_mode || 'from') === 'until') {
      t0 = i > 0 ? Number(sgs[i - 1].at_ms) || 0 : 0; t1 = at;
    } else {
      t1 = i + 1 < sgs.length ? Number(sgs[i + 1].at_ms) || 0 : dur;
    }
    out.segments.push({ i, t0, t1, label: sg.label || `分段${i + 1}` });
  });
  return out;
}
/** 采bpm 区间的起止：格号 ⇒ 毫秒（算不出来返回 null） */
function xkRangeMs(rg, side) {
  const kMs = side + '_ms', kTk = side + '_tile';
  if (rg[kTk] !== undefined && rg[kTk] !== null) {
    const v = xkTileToMs(Number(rg[kTk]));
    return Number.isFinite(v) ? v : null;
  }
  const v = Number(rg[kMs]);
  return Number.isFinite(v) ? v : null;
}
/** ★★ 三押的记号：同一「剩余格」下有两块薄格 ⇒ 押数 3（后端不用改，dp_pairs 早就在） */
function bandDp() {
  if (!payload) return [];
  const pr = payload.dp_pairs || [], entry = payload.entry || [];
  if (!pr.length) return [];
  const byRest = new Map();
  for (const pair of pr) {
    const ix = Number(pair[0]), ib = Number(pair[2]);
    if (!Number.isFinite(ix) || !Number.isFinite(ib)) continue;
    if (!byRest.has(ib)) byRest.set(ib, []);
    byRest.get(ib).push(ix);
  }
  const out = [];
  for (const [, thins] of byRest) {
    const first = Math.min(...thins);
    const t = entry[first];
    if (typeof t !== 'number') continue;
    out.push({ t, press: thins.length >= 2 ? 3 : 2 });
  }
  return out.sort((a, b) => a.t - b.t);
}
/** ★★ 换手押上色（`docs/59`）：后端给出**染了色的那些格号**（`payload.color.floors`）。
 *  段带用它打「霓虹」记号（白边黑芯）。**空 = 没开 / 本谱没有换手押** ⇒ 不画。
 *  轴与 `bandDp` 一致：`entry[floor]`（同一根采音轴）。 */
function bandColor() {
  if (!payload) return [];
  const c = payload.color || {};
  const floors = c.floors || [], entry = payload.entry || [];
  const out = [];
  for (const f of floors) {
    const i = Number(f), t = entry[i];
    if (!Number.isFinite(i) || typeof t !== 'number') continue;
    out.push({ t, floor: i });
  }
  return out.sort((a, b) => a.t - b.t);
}
/** ★★ 算法轨道调度（`docs/60`）：后端给「涟漪环触发格」与「半径切换格」。
 *  段带上画成两种记号（涟漪 = 向上三角 / 半径 = 方块点），都挂在**采音轴**上。 */
function bandAppear() {
  if (!payload) return [];
  const a = payload.appearance || {};
  const entry = payload.entry || [];
  const out = [];
  for (const r of (a.ripples || [])) {
    const i = Number(r.floor), t = entry[i];
    if (Number.isFinite(i) && typeof t === 'number') {
      out.push({ t, floor: i, kind: 'ripple', rings: Number(r.rings) || 0 });
    }
  }
  for (const s of (a.radius_spans || [])) {
    const i = Number(s.floor), t = entry[i];
    if (Number.isFinite(i) && typeof t === 'number') {
      out.push({ t, floor: i, kind: 'radius', scale: Number(s.scale) || 0 });
    }
  }
  return out.sort((x, y) => x.t - y.t);
}
/** 拖段边界 ⇒ 写回 state（**松手才写**，拖动中只重画） */function bandEdit(ref, t0, t1) {
  if (!ref) return;
  const lo = Math.round(t0), hi = Math.round(t1);
  if (ref.kind === 'region') {
    const rg = (state.regions || [])[ref.i]; if (!rg) return;
    rg.start_ms = lo; rg.end_ms = hi;
    setStatus(`区间${ref.i + 1} 改为 ${(lo / 1000).toFixed(2)}–${(hi / 1000).toFixed(2)}s`);
  } else if (ref.kind === 'xk') {
    const rg = (state.xk_ranges || [])[ref.i]; if (!rg) return;
    for (const [side, v] of [['start', lo], ['end', hi]]) {
      if (rg[side + '_tile'] !== undefined && rg[side + '_tile'] !== null) {
        rg[side + '_tile'] = xkMsToTile(v);
      } else { rg[side + '_ms'] = v; }
    }
    setStatus(`采bpm 区间${ref.i + 1} 改为 ${(lo / 1000).toFixed(2)}–${(hi / 1000).toFixed(2)}s`);
  } else if (ref.kind === 'segment') {
    const sg = (state.segments || [])[ref.i]; if (!sg) return;
    sg.at_ms = ((state.segment_mode || 'from') === 'until') ? hi : lo;
    setStatus(`分段${ref.i + 1} 改到 ${(sg.at_ms / 1000).toFixed(2)}s`);
  } else { return; }
  renderRegions(); renderSegments(); renderXkRanges(); schedule();
}
function bandSelect(ref) {
  if (!ref) return;
  if (ref.kind === 'region') selectRegion(ref.i);
  else if (ref.kind === 'segment') { selectedSegment = ref.i; renderSegments(); }
}

/* ============================================================ ★ 报告带
   必须看的账**常驻**，不再埋在折叠里（docs/49 §5「不许静默」）。 */
const CHIP_DEFS = [
  // ★ 三押 chip **常驻**（哪怕是 0）：它是「三押开关在哪」的唯一入口 ——
  //   用户 2026-10「拼尽全力找不到三押在哪关」就是因为开关埋在左栏 ② 里，
  //   而只在「有数」时才出现的 chip 根本指不到路。点它 → 明细里写明开关位置。
  ['dp3', '三押', (r) => (r.dp && r.dp.dp_three) || 0, () => true],
  // ★ 用户 2026-10「为三押添加开关」后的两档结果：**不许静默**（选了什么就写什么）
  ['dp3off', '三押已关', (r) => (r.dp && r.dp.dp_three_off) || 0, (v) => v > 0],
  ['dp3skip', '跳过三押', (r) => (r.dp && r.dp.dp_press_skipped) || 0, (v) => v > 0, 'warn'],
  ['dp4', '跳过四押', (r) => (r.dp && r.dp.dp_extra_press) || 0, (v) => v > 0, 'warn'],
  ['lost', '双押丢', (r) => (r.dp && r.dp.lost) || 0, () => true],
  // ★★ 换手押上色（`docs/59`）：**常驻** —— 它是「这个开关在哪」的唯一入口
  //   （同 dp3 那条的道理：只在有数时才出现的 chip 根本指不到路）。
  ['hs', '换手押', (r) => (r.color && r.color.n_events) || 0, () => true],
  ['hsoff', '换手押已关', (r) => ((r.color && r.color.enabled === false) ? 1 : 0),
    (v) => v > 0],
  ['hsskip', '换手押跳过', (r) => ((r.color && ((r.color.n_skipped_press || 0)
    + (r.color.n_skipped_ambig || 0))) || 0), (v) => v > 0, 'warn'],
  // ★★ 算法轨道调度（`docs/60`）：常驻「轨道」入口 + 两类事件计数
  ['ap', '轨道调度', (r) => ((r.appearance && r.appearance.n_events) || 0), () => true],
  ['apoff', '轨道调度已关', (r) => ((r.appearance && r.appearance.enabled === false) ? 1 : 0),
    (v) => v > 0],
  ['aprip', '涟漪环', (r) => ((r.appearance && (r.appearance.ripples || []).length) || 0),
    () => true],
  ['aprad', '半径切换', (r) => ((r.appearance && (r.appearance.radius_spans || []).length) || 0),
    (v) => v > 0],
  ['apskip', '轨道避让', (r) => ((r.appearance && ((r.appearance.n_collide || 0)
    + (r.appearance.n_capped || 0))) || 0), (v) => v > 0, 'warn'],
  ['sp', '调速落第一格', (r) => (r.dp && r.dp.setspeed_used) || 0, (v) => v > 0],
  ['floors', '层', (r) => r.n_floors || 0, () => true],
  ['onsets', '采音点', (r) => r.n_onsets || 0, () => true],
  ['viol', '违规', (r) => r.n_violations || 0, () => true],
  ['warn', '警告', (r) => (r.warning_list || []).length, () => true, 'warn'],
];
function renderChips(r) {
  const box = $('#chips');
  if (!box) return;
  box.innerHTML = '';
  for (const [id, label, get, show, cls] of CHIP_DEFS) {
    const v = get(r);
    if (!show(v)) continue;
    const c = el('span', 'chip' + (cls ? ' ' + cls : '') + (v ? '' : ' dead'));
    c.dataset.c = id;
    c.innerHTML = `${label} <b>${v}</b>`;
    c.onclick = () => showChipDetail(id, r);
    box.appendChild(c);
  }
  const d = $('#detail');
  if (d && d.dataset.for === undefined) d.classList.remove('on');
  syncReportHead();                 // ★ 增量改造：刷报告带标题栏那行摘要
}
function showChipDetail(id, r) {
  const d = $('#detail');
  if (!d) return;
  const dp = r.dp || {};
  const T = {
    dp3: `<b>三押 ${dp.dp_three || 0} 处</b> —— 判据：**同一时刻有 2 条多押轨都有音**`
       + `（押数 = 1 + 同时有音的 dp 轨数）⇒ 那一格拆 3 块。<br>`
       + `组合表：cbpm ≥ 1000 ⇒ <span class="k">30 · 60</span>；低位 ⇒ <span class="k">15 · 15</span>`
       + `（docs/48；参考谱 <code>three_press_interact.adofai</code> 实测）<br>`
       + `★ <b>开关在哪</b>：左栏「<b>② 主轨</b>」组里那一行 <span class="k">三押</span> ——`
       + `三档：<span class="k">拆三押</span>（默认）/ <span class="k">不拆（按双押插一格）</span>`
       + `/ <span class="k">跳过（连双押也不插）</span>。<br>`
       + `（它只在「使用固定双押角度」开着时有意义；关掉那一项时它会自动置灰）`,
    dp4: `⚠ <b>跳过四押 ${dp.dp_extra_press || 0} 处</b>：那些时刻有 **3 条及以上**多押轨同时响。`
       + `本轮只做双押/三押，那些落点**仍按双押插**（不整格丢）。`,
    dp3off: `<b>三押已关 ${dp.dp_three_off || 0} 处</b>：「三押」开关 = <span class="k">不拆</span>，`
       + `那些时刻有 2 条多押轨同时响，但只**按双押插一格**（薄角 1 块）。`
       + `要三押就把开关调回「拆三押」。`,
    dp3skip: `⚠ <b>跳过三押 ${dp.dp_press_skipped || 0} 处</b>：「三押」开关 = <span class="k">跳过</span>，`
       + `那些时刻**连双押也没插**（整组丢掉）—— 那几个音是空的。要落点就调成「不拆」或「拆三押」。`,
    lost: `<b>双押丢 ${dp.lost || 0} 处</b>${dp.lost ? '：' + (r.dp_info || '') : ' —— 一个点都没丢'}`,
    // ★★ 换手押上色（`docs/59`）
    hs: (() => {
      const c = r.color || {};
      const txt = c.text || '（这一轮没算）';
      return `<b>换手押上色</b> —— ${txt}<br>`
        + `判据（你定的）：<span class="k">X</span>=双押组、<span class="k">O</span>=普通格，`
        + `满足 <span class="k">OOX</span> 循环（周期 3 个事件）的结构就是换手押；`
        + `连续 <span class="k">≥2 个周期</span>才算；<span class="k">不看 Twirl</span>。<br>`
        + `颜色固定 = <span class="k">黑底白边霓虹</span>`
        + `（<code>RecolorTrack</code> · Glow · 主 000000 / 副 ffffff · Neon），`
        + `范围 <span class="k">只染薄格那一格</span>，常规格一律不动。<br>`
        + `★ <b>开关在哪</b>：左栏「<b>⑤b 换手押上色（轨道颜色调度）</b>」组 ——`
        + `关掉 ⇒ 一条事件都不写，导出与旧版**逐字节相同**。<br>`
        + `（只写 actions，不碰 angleData/bpm/travel/Twirl ⇒ 时序零影响）`;
    })(),
    hsoff: `<b>换手押上色已关</b> ⇒ 这一轮**一条 RecolorTrack 都没写**，`
      + `导出与关掉之前逐字节相同。要上色就去左栏「⑤b 换手押上色」把开关打开。`,
    hsskip: (() => {
      const c = r.color || {};
      const why = (c.skipped_why || []).map((w, i) => `${i + 1}. ${w}`).join('<br>');
      return `⚠ <b>换手押跳过 ${((c.n_skipped_press || 0) + (c.n_skipped_ambig || 0))} 处</b><br>`
        + `· 押数 ≠ 2 的组（三押/四押）：<b>${c.n_skipped_press || 0}</b> 个 —— 本轮只处理双押<br>`
        + `· 认不出的 (90,90) 候选：<b>${c.n_skipped_ambig || 0}</b> 个`
        + `（与普通直角折角无法区分，按「不认」处理）<br>` + (why || '（无明细）');
    })(),
    // ★★ 算法轨道调度（`docs/60`）
    ap: (() => {
      const a = r.appearance || {};
      const txt = a.text || '（这一轮没算）';
      const rip = (a.ripples || []).map((x) => `格 ${x.floor}（${x.kind || '图形'}）${x.rings + 1} 环`
        + (x.why ? ` — ${x.why}` : ''));
      return `<b>算法轨道调度</b> —— ${txt}<br>`
        + `皮肤：<span class="k">${a.skin || 'Neon'}</span>`
        + `（<code>trackStyle</code> + Glow + Forward 脉冲，写在 <b>settings</b> 层 ⇒ 一条 action 都不花）<br>`
        + `涟漪环触发点 ${(a.ripples || []).length} 处：<br>`
        + (rip.length ? rip.map((s, i) => `&nbsp;&nbsp;${i + 1}. ${s}`).join('<br>') : '&nbsp;&nbsp;（无）')
        + `<br>半径切换 ${(a.radius_spans || []).length} 处：`
        + ((a.radius_spans || []).map((s) => `格 ${s.floor}→${s.scale}%`).join('、') || '（无）')
        + `<br>★ <b>开关在哪</b>：左栏「<b>⑤c 算法轨道调度（皮肤 / 涟漪环 / 半径）</b>」组 ——`
        + `关掉 ⇒ 一条事件都不写、settings 一个字节不动，导出与旧版**逐字节相同**。<br>`
        + `（驱动 = <span class="k">谱面结构</span>：图形段起点 + 密度；不碰 angleData/bpm/travel）`;
    })(),
    apoff: `<b>算法轨道调度已关</b> ⇒ 这一轮一条 <code>RecolorTrack</code>/<code>ScaleRadius</code> 都没写，`
      + `settings 也没改（<code>trackStyle</code> 还是默认 Standard），导出与旧版逐字节相同。`
      + `要开就去左栏「⑤c 算法轨道调度」把开关打开。`,
    aprip: (() => {
      const a = r.appearance || {};
      const rip = (a.ripples || []);
      return `<b>涟漪环 ${rip.length} 处</b>（共 ${a.n_ripple_events || 0} 条事件）<br>`
        + `配方（Hello2025 实测套路）：同一格上按 n = 0…N 各写一对 `
        + `<code>RecolorTrack</code>，<code>startTile=[-n]</code>、<code>endTile=[+n]</code>、`
        + `<code>gapLength=2n-1</code>（这是**步长**，恰好只刷 ±n 两点）、`
        + `<code>angleOffset=${a.ripple_step || 30}·n</code>（180° = 1 拍）<br>`
        + (rip.length ? rip.map((x, i) => `&nbsp;&nbsp;${i + 1}. 格 ${x.floor}`
          + `（${x.kind || '图形'}）${x.rings + 1} 环${x.why ? ` — ${x.why}` : ''}`).join('<br>')
          : '（本谱没有图形段起点 ⇒ 一圈都没打）');
    })(),
    aprad: (() => {
      const a = r.appearance || {};
      const sw = (a.radius_spans || []);
      return `<b>半径切换 ${sw.length} 处</b><br>`
        + `口径（你定的）：<span class="k">密集 → 摊开 ${a.radius_dense || 250}%</span>，`
        + `<span class="k">稀疏 → ${a.radius_quiet || 100}%</span>。`
        + `密度单位是 <b>音/秒</b>（纯实时，与曲子 BPM / 标注无关）：`
        + `≥${a.dense_fps || 8} 算密、≤${a.quiet_fps || 5} 才算回疏（迟滞防抖，`
        + `且至少驻留 1 秒 + 8 格）。`
        + `⚠ 密度单位**不能用「格/拍」**：拍轴只吃 travel/speed_k、不吃 base_bpm，`
        + `同一首音乐标成 122.5 与 980 BPM 会差 8 倍 —— 第一版就因此在 ASGORE 上闪了 4 格。<br>`
        + (sw.length ? sw.map((s) => `&nbsp;&nbsp;格 ${s.floor} → ${s.scale}%`
          + `（切换时密度 ${s.fps} 音/秒）`).join('<br>')
          : '（全谱密度没跨过阈值 ⇒ 0 次切换，默认半径不变）')
        + `<br>★ 反编译实证（<code>scnGame.cs:636</code>）：它只改**显示坐标**`
        + `（沿每格行进方向推 <code>(1-scale/100)</code> 格），<code>startPos</code> 不动 ⇒`
        + `**时序与判定完全不变**；星球位置取自砖块 transform ⇒ 星球跟着走。`;
    })(),
    apskip: (() => {
      const a = r.appearance || {};
      const why = (a.skipped_why || []).map((w, i) => `${i + 1}. ${w}`).join('<br>');
      return `⚠ <b>轨道调度跳过 / 截断</b><br>`
        + `· 与换手押同格而**避让**：<b>${a.n_collide || 0}</b> 个触发点（⑤b 优先级更高）<br>`
        + `· 同格并行度上限截断：<b>${a.n_capped || 0}</b> 环<br>`
        + `· 涟漪会短暂扫过的换手押格：<b>${a.n_overlap_occupied || 0}</b> 个`
        + `（刻意的时间差效果，不跳过）<br>` + (why || '（无明细）');
    })(),
    sp: `<b>调速落双押第一格 ${dp.setspeed_used || 0} 处</b>`
      + `（规则：调速不和旋转重叠 + 位于双押第一格）`,
    floors: `层数 ${r.n_floors || 0} · 采音点 ${r.n_onsets || 0} · 预留槽位 ${(r.dp && r.dp.reserved_used) || 0}`,
    onsets: `采音点 ${r.n_onsets || 0}（含采bpm 骨架砖）`,
    viol: (r.n_violations || 0)
      ? `⚠ ${r.n_violations} 条违规：<br>` + (r.violations || []).map((v) => v.msg || v.code).join('<br>')
      : '<b>0 违规</b>（rules 全过）',
    warn: (r.warning_list || []).length
      ? (r.warning_list || []).map((w, i) => `${i + 1}. ${w}`).join('<br>')
      : '没有警告',
  };
  d.innerHTML = T[id] || '';
  d.dataset.for = id;
  d.classList.toggle('on', d.classList.contains('on') ? d.dataset.for !== id : true);
  if (d.classList.contains('on')) d.dataset.for = id;
}

/* ============================================================ ★ 参数搜索 / 预设 */
function applyParamSearch(qtxt) {
  const q = String(qtxt || '').trim().toLowerCase();
  document.querySelectorAll('#groups section.grp, #groups-xk section.grp').forEach((sec) => {
    let hit = 0;
    sec.querySelectorAll('.field').forEach((row) => {
      const lbl = row.querySelector('label');
      const key = row.querySelector('input,select') && row.querySelector('input,select').id;
      const hay = ((lbl && lbl.textContent) || '') + ' ' + (key || '');
      const ok = !q || hay.toLowerCase().includes(q);
      row.style.display = ok ? '' : 'none';
      if (ok) hit += 1;
    });
    sec.style.display = (q && !hit) ? 'none' : '';
    if (q && hit) sec.classList.remove('collapsed');
  });
}
const PRESET_DEFS = {
  '默认': {},
  '满直线': { xk_base: '4', use_templates: false, use_snowflake: false, straight_preset: '2' },
  '直拟合·时序优先': { fit_mode: 'direct', quantize_rhythm: false },
  '手工打磨': { use_templates: true, use_snowflake: true, straight_preset: '1' },
};
function applyPreset(name) {
  const ch = PRESET_DEFS[name] || {};
  const keys = Object.keys(ch);
  keys.forEach((k) => setControl(k, ch[k]));
  setStatus(`已套用预设「${name}」：改了 ${keys.length} 个字段（${keys.join(' / ') || '无'}）`);
  schedule();
}

/** 布局一变（拖块/拖分隔条/缩放/停靠）⇒ 所有画布重新量尺寸 */
function sizeViews() {
  try { band.size(); } catch (_e) { /* 还没数据 */ }
  overview.draw();                     // 全曲条自己按 clientWidth 量（它没有 size()）
  drawActive();
  if (preview) { try { preview.resize(); } catch (_e) { /* 忽略 */ } }
}
let selectedRegion = -1;
// ★ 分段采音（`docs/34` 方案 C）：角色是**时间的函数**。有分段时区间被整个忽略。
let selectedSegment = -1;
const SEG_DIMS = [
  ['main', '主轨'], ['sub', '次轨'], ['dp', '双押轨'],
];

// ------------------------------------------------ ★ 内嵌 ADOFAI 播放器
// 谱面预览不再自己画，改用 Re_ADOJAS 的渲染引擎（Three.js + 它的 wasm），
// 由 `vendor/adofai-player.js` 暴露的 `createPreview()` 挂到 #cv-adofai 上。
// 见 docs/22。`previewKey` 变了（重建/offset/采音变了）才重载，避免每帧重建。
let preview = null;
let previewMod = null;
let previewKey = null;
let previewLoading = false;
let previewErr = '';
let previewStarted = false;      // 播过一次之后才用 resume，避免 resume 空转
let lastSuggest = null;          // 最近一次 rebuild 给出的「建议 offset」(ms)
/** ★ 用户**手动碰过** offset 没有（2026-10「开门一遍」）。
 *  `auto_offset` 默认开 ⇒ 每轮重算都会写回自动值；但只要他手动改过 offset，
 *  就再也不覆盖（否则「改一下就弹回去」，看起来像 bug）。换文件时复位。 */
let offsetTouched = false;
// ★ 最近一次 rebuild 的整包结果（e2e / DevTools 要看 dp.dp_three、dp.dp_extra_press
//   这些**只在报告里**的账；以前只能靠 DOM 文本反推，容易假绿）
let lastResult = null;

function previewSignature() {
  return [rebuildCount, state.offset, state.countdown_ticks,
    (state.regions || []).length, state.auto_offset,
    (state.segments || []).length, state.segment_mode,
    // ★ 演出分段（docs/62）：改一段就重挂预览（谱面 JSON 会变）
    JSON.stringify(state.show_segments || []),
    // ★ 预览音源（2026-10）：换**文件** / 换**模式**要重挂播放器（音频源变了）。
    //   Δ 不在这里 —— 它走 `setMusicDelayMs` **就地生效**（见 onFieldChanged），
    //   否则拖一下滑块就要重挂一次播放器。
    state.preview_audio_mode, state.preview_audio_path].join('|');
}

async function ensurePreview() {
  const host = $('#cv-adofai');
  if (!host) return;
  if (!loadInfo || !hasChart || !payload) return;
  const key = previewSignature();
  if (preview && previewKey === key) return;
  if (previewLoading) return;
  previewLoading = true;
  try {
    if (!previewMod) previewMod = await import('./vendor/adofai-player.js');
    const lj = await api.levelJson(state);
    if (!lj || !lj.ok) {
      previewErr = (lj && lj.error) || '拿不到谱面 JSON';
      setStatus(`⚠ 预览：${previewErr}`);
      return;
    }
    const a = await api.audio(state);
    const au = a && a.ok ? api.mediaUrl(a.path) : null;
    if (preview) { preview.destroy(); preview = null; }
    preview = await previewMod.createPreview(host, JSON.stringify(lj.level), au, {
      editorMode: true, trail: true, renderer: 'webgl', hitsound: true,
      disableTrackTexture: true,
      // ★ 2026-10：谱面预览页的时钟是**谱面轴**，不吃 axisLead ⇒ 原曲偏移 Δ
      //   必须从这里进去（`musicDelayMs` 正值 = 音乐相对游戏时间线延后）。
      musicDelayMs: (Number(state.music_delay_ms) || 0) + oggMs(),
    });
    // ★ 增量改造（2026-09-20）：去掉预览框里的「判定条」（引擎内置的准度条 HitErrorMeter）。
    killHitErrorMeter(preview);
    // ★ 第7条（2026-09-21）：补打拍音 —— 引擎**从不自己合成**，必须宿主喊一声（见函数注释）。
    synthesizeHitsounds(preview);
    // ★ 第8条（2026-09-21）：画面整体滞后 100ms（补偿音频输出延迟，见函数注释）。
    applyVisualLag(preview, PREVIEW_VISUAL_LAG_MS);
    previewKey = key;
    previewErr = '';
    previewStarted = false;      // 不自动播：等用户按播放键（旧行为是自动播且暂停无效）
    startPreviewClock();
    // ★ 不要在这里 setStatus：会把刚重建出来的状态文案（层数/区间/警告）顶掉。
  } catch (e) {
    previewErr = String((e && e.message) || e);
    setStatus(`⚠ ADOFAI 预览失败：${previewErr}`);
  } finally {
    previewLoading = false;
  }
}

/**
 * ★ 增量改造（2026-09-20）：去掉预览框里的「判定条」——引擎内置的准度条 HitErrorMeter。
 *
 * 为什么：引擎 `createPlayer()` 会在预览容器（`#cv-adofai`）里挂一张 **zIndex:9998、
 * 覆盖整个画面** 的 canvas 用来画判定条（中间 Perfect 绿 / 左右 Good→Bad→Miss），
 * 而且每次命中（`addHit`）都会把它设成 `visible = true` ⇒ 只要一播放，它就一直
 * 横在画面下方（`barY = 高度 - 85`），很遮挡视野；对**做谱**这件事它没有用处。
 *
 * 做法：预览一建好就 `dispose()`（移除那张 canvas）并把字段置 `null`。引擎内部所有
 * 调用都是 `this.hitErrorMeter?.xxx()` 的可选链 ⇒ 置空后全部变成 no-op，
 * 既不会画、也不会报错。**纯宿主侧处理，不改引擎源码**（vendor 文件逐字节保持原样）。
 */
function killHitErrorMeter(pv) {
  try {
    const p = pv && pv.player;
    if (p && p.hitErrorMeter) {
      try { p.hitErrorMeter.dispose(); } catch (_e) { /* 忽略 */ }
      p.hitErrorMeter = null;
    }
  } catch (_e) { /* 忽略 */ }
  // DOM 兜底：万一引擎日后改了字段名，按特征（zIndex 9998 那张全画布 canvas）藏掉残留
  try {
    const host = $('#cv-adofai');
    if (host) {
      for (const c of host.querySelectorAll('canvas')) {
        if (c.style && c.style.zIndex === '9998') c.style.display = 'none';
      }
    }
  } catch (_e) { /* 忽略 */ }
}

/**
 * ★ 第7条（2026-09-21）：给预览补上「打拍音」（引擎的 hitsound）。
 *
 * **之前为什么没有**：引擎 `createPreview` 的选项解构里 `hitsound = false` 是默认值，
 * 而且**引擎自己从不调用** `preSynthesizeHitsounds()` —— vendor 包里那个方法
 * 只有定义、**零调用点**（`preSynthesizeHitsoundsWithProgress` 同理）。
 *
 * 引擎的设计是：把所有砖块的打拍音**预先混成一条整曲 AudioBuffer**，播放时用
 * `startAtOffset(offset)` 整条喂出去（这样 seek 只需改 offset，不用逐音重排）。
 * 于是没合成过 ⇒ `isSynthesized() === false` ⇒ `startPlay` / `seekTo` / `resume`
 * 里**所有** hitsound 分支被跳过 ⇒ 全程静默。
 *
 * **做法**：① 建预览时传 `hitsound: true`；② 建好后由宿主**等 tileStartTimes 就绪**
 *   再喊一次合成（见 `synthesizeHitsounds` 注释——`createPreview` 返回时
 *   `tileStartTimes` 还是空的，同步喊会被引擎提前 return，所以必须轮询等到它填好；
 *   另在 `togglePlay` 播放前兜底再合一次，是最稳的时机）。
 *
 * **音色与音量不归我们管**，是引擎自己读谱面 settings：
 *   `settings.hitsound`（缺省或 `"None"` → 回落 `"Kick"`）
 *   `settings.hitsoundVolume`（缺省 100）—— 在**合成时**按 `volume/100` 烘进 buffer，
 *   运行期没有音量接口（`HitsoundManager` 只暴露 `setEnabled` / `setOGGCompression`）。
 * 我们的谱面模板正好是 `Kick / 25` ⇒ 听感「每块砖一声轻 Kick」，不压音乐。
 *
 * ⚠ 合成是一次性整曲 buffer（约 时长 × 采样率 × 2ch × 4B；5 分钟歌约 120MB），所以：
 *   · 换谱面 / 换音源 ⇒ 预览重建 ⇒ 必须重新合成（重建走的就是 `ensurePreview`）；
 *   · 中途 seek **不用**重来（引擎用 `startAtOffset` 定位）；
 *   · 合成失败一律吞掉 —— 宁愿没打拍音，也不能把预览搞挂。
 */
let hitsoundToken = 0;
/**
 * ★ 第7条修复（2026-09-25）：等 tileStartTimes 就绪再合成打拍音。
 *
 * **旧实现为什么"不生效"**：原代码在 `ensurePreview` 建完预览后**同步**调一次
 * `preSynthesizeHitsoundsWithProgress()`。但引擎的 `tileStartTimes` 只在**渲染循环每帧**
 * 的 `update(stats)` 里才赋值（`vendor` 84131-84137）——`createPreview` 返回那一刻它还是
 * 空数组。于是 `preSynthesizeHitsoundsWithProgress` 一进函数就撞上 `if
 * (!this.tileStartTimes || this.tileStartTimes.length === 0) return;`（vendor 85972）
 * **提前退出**，整曲 hitsound buffer 永远为 null ⇒ `startPlay` 里的
 * `if (this.hitsoundManager.isSynthesized())` 一直 false ⇒ 全程静默。
 *
 * **修法**：不在建完时同步喊，而是**轮询等到 tileStartTimes 就绪**再合成（渲染循环是
 * rAF，建完一两帧内就填好了）。`hitsoundToken` 防重建竞态：合成途中又重建预览 ⇒ 作废。
 * 另见 `togglePlay` 播放前兜底再合一次（最稳的时机）。
 */
function waitTileStartTimes(p, ms, token) {
  return new Promise((resolve) => {
    const t0 = performance.now();
    const step = () => {
      if (token != null && token !== hitsoundToken) return resolve(false); // 已被新预览取代
      if (p.tileStartTimes && p.tileStartTimes.length > 0) return resolve(true);
      if (performance.now() - t0 > ms) return resolve(false);
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
}
async function synthesizeHitsounds(pv) {
  const token = ++hitsoundToken;
  const p = pv && pv.player;
  // 引擎若没暴露这个方法（将来换 vendor 版本）就安静跳过，别抛
  if (!p || typeof p.preSynthesizeHitsoundsWithProgress !== 'function') return;
  if (!p.hitsoundManager || !p.hitsoundManager.isEnabled()) return;
  if (p.hitsoundManager.isSynthesized()) return;
  // 等 tileStartTimes 就绪（渲染循环每帧才填，建完立刻调必然空）
  const ready = await waitTileStartTimes(p, 2000, token);
  if (token !== hitsoundToken) return;   // 合成期间又重建过预览 ⇒ 这次结果作废
  if (!ready) return;                    // 等不到（极端：渲染循环没起）⇒ 留待播放兜底
  try {
    await p.preSynthesizeHitsoundsWithProgress();
  } catch (_e) { /* 合成失败不影响预览播放 */ }
}

/**
 * 打拍音现状快照（e2e / DevTools 取证用）。
 * `synth` = 已合成为整曲 buffer（能响）；`unsynth` = 没合成（静默）。
 */
function hitsoundInfo() {
  const p = preview && preview.player;
  const hm = p && p.hitsoundManager;
  if (!hm) return 'no-manager';
  const st = (p.levelData && p.levelData.settings) || {};
  const type = st.hitsound || 'Kick';
  const vol = st.hitsoundVolume == null ? 100 : st.hitsoundVolume;
  return `${type} @${vol} / ${hm.isEnabled() ? 'on' : 'off'} / ` +
         `${hm.isSynthesized() ? 'synth' : 'unsynth'}`;
}

/**
 * ★ 第8条（2026-09-21）：让预览画面**整体滞后** `PREVIEW_VISUAL_LAG_MS` 毫秒。
 *
 * **现象**：预览里画面比听到的音乐早动约 100ms（听感上"音画不同步"）。
 *
 * **根因**：引擎的渲染时钟 `elapsedTime` 来自 `AudioContext.currentTime` 或
 * `performance.now()` —— 都是"**算**到哪儿了"的时钟；而扬声器真正出声还要经过
 * 输出链路（WASAPI 共享模式缓冲 + 设备），实测约 100ms。引擎把画面钉在**渲染时钟**
 * 上，于是画面恰好比耳朵听到的早一个输出延迟。**不是谱面问题，也不是算错了**。
 *
 * **做法**（不是 `sleep(0.1)` 那种硬延迟）：在引擎的时钟上做一个 `time_offset`。
 * 每帧把 `updatePlayer()` 看到的两根时钟**同时往后挪** `ms`，于是这一帧运动学
 * 全部按"100ms 之前"的世界算 —— 画面整体滞后 100ms，且**不累积**（每帧快照式还原）。
 *
 *   时钟来源只有两种，两种都要挪（`updatePlayer` 里 `elapsedTime` 的赋值）：
 *     · `useAudioContextTime`：`elapsedTime = (ctx.currentTime - audioContextStartOffset)*1e3`
 *         ⇒ `audioContextStartOffset += ms/1e3`
 *     · 否则：`elapsedTime = performance.now() - startTime`
 *         ⇒ `startTime += ms`
 *
 * 🔴 **必须在两个"一次性里程碑"跑完之后才挂偏移**，否则会帮倒忙：
 *   ① `_musicScheduled`：引擎按 `musicStartDelay - elapsedTime` 算"还要等多久才排音乐"。
 *      若此刻时间已被挪小，**音乐会被排晚 `ms`** —— 方向正好相反，音画差变成 200ms。
 *   ② `audioDriftSynced`：引擎一次性把 `elapsedTime` 对齐到 `<audio>.currentTime`。
 *      若此时带着我们的偏移，会被它当成"漂移"一次性**校正掉**，等于白加。
 *   两个都过完之后（正常播放里 < 1.5s）再挂才稳；`elapsedTime > 1500` 是兜底
 *   （万一没音频 / 里程碑条件没满足，晚 1.5s 生效总比永远不生效好）。
 *
 * ⚠ 每帧快照式还原（`finally` 里写回旧值），所以：
 *   · 不吃掉引擎自己对时钟的修正（漂移校正改的是 `startTime`/`audioContextStartOffset`，
 *     我们每帧存的是"当时的"值，改完还原回去，它的累积修改依然保留）；
 *   · `startPlay()` / `seekTo()` 重置时钟后不用特殊处理，下一帧自动重新生效；
 *   · `elapsedTime` **本身**保持"偏移后"的值（不是快照还原的那两根），
 *     所以 `renderPlayer` / 走带条 / HUD 读到的都是同一根滞后的时钟 —— 一致。
 *
 * 100ms 是这台机器上的实测值；换设备/换音频输出（独占模式、蓝牙）会不同，
 * 所以留成常量 + `__dsh.setVisualLag()` 便于真机微调。
 */
const PREVIEW_VISUAL_LAG_MS = 100;

function applyVisualLag(pv, ms) {
  const p = pv && pv.player;
  if (!p || typeof p.updatePlayer !== 'function') return;
  if (p.__wbVisualLagHooked) return;        // 同一个 player 只挂一次
  p.__wbVisualLagHooked = true;

  const orig = p.updatePlayer;
  let lag = Number(ms) > 0 ? Number(ms) : 0;
  p.__wbVisualLag = lag;
  // 供真机微调 / e2e 用：改的是闭包里的 lag，下一帧生效
  p.__wbSetVisualLag = (v) => { lag = Number(v) > 0 ? Number(v) : 0; p.__wbVisualLag = lag; return lag; };

  p.updatePlayer = function (delta) {
    // 里程碑未过 ⇒ 一帧都不许偏移（见上面 ①②）
    const armed = (this._musicScheduled && this.audioDriftSynced)
      || (this.elapsedTime || 0) > 1500;
    if (!armed || !lag) return orig.call(this, delta);

    const sStart = this.startTime;
    const sOff = this.audioContextStartOffset;
    this.startTime = sStart + lag;
    this.audioContextStartOffset = sOff + lag / 1e3;
    try {
      return orig.call(this, delta);
    } finally {
      this.startTime = sStart;
      this.audioContextStartOffset = sOff;
    }
  };
}

/** 离开谱面预览页时把它的音频停掉，避免和全局 <audio> 两份声音打架。 */
function pausePreview() {
  if (!preview) return;
  try { preview.stop() } catch (_e) { /* 忽略 */ }
}

/**
 * 谱面预览页的走带时钟：内嵌播放器有自己的音频与时钟，`<audio>` 那套
 * （`syncFromAudio`）在它播放时是停的，所以这里单独拉一根 100ms 的线，
 * 把全曲条播放头 / 底部滑块 / 时间标签跟着它走。
 */
let previewTimer = null;
function startPreviewClock() {
  if (previewTimer) return;
  previewTimer = setInterval(() => {
    if (!preview || activeTab !== 'chart') return;
    const ms = preview.currentTimeMs || 0;
    const dur = preview.totalDurationMs || 0;
    // ★ 全曲条的数据在采音轴，播放器的时钟在谱面轴 ⇒ 必须换算（docs/24 §5）
    overview.setPlayhead(chartToGrid(ms));
    overview.draw();
    $('#lbl-time').textContent = `${fmt(ms)} / ${fmt(dur)}`;
    if (dur > 0) $('#slider').value = String(Math.round(ms / dur * 1000));
  }, 100);
}
function stopPreviewClock() {
  if (previewTimer) { clearInterval(previewTimer); previewTimer = null; }
}

const player = $('#player');
player.volume = 0.7;

// ------------------------------------------------------- 滚轮不抢焦
// 只允许「已聚焦」的数字框响应滚轮；其余一律吃掉，避免「滚面板顺手改数值」。
document.addEventListener('wheel', (e) => {
  const t = e.target;
  const numeric = t && t.tagName === 'INPUT' && (t.type === 'number' || t.type === 'range');
  if (numeric && document.activeElement !== t) {
    e.preventDefault();
    e.stopPropagation();
  }
}, { capture: true, passive: false });

// ------------------------------------------------------------ 面板生成
function getVal(f) { return state[f.key]; }

function setVal(f, v, programmatic = false) {
  state[f.key] = v;
  if (!programmatic) onFieldChanged(f);
}

function onFieldChanged(f) {
  if (applying) return;
  if (f.group === 'chartview') refreshAvSummary();   // ★ 增量改造：工具条药丸摘要跟手
  if (f.schedule) schedule();
  if (f.key === 'pfollow') { views.path.follow = !!state.pfollow; drawActive(); }
  // ★ 「固定双押角度」一拨就刷新依赖它的置灰（薄角 θ / 偏移预算 / 三押开关），
  //   不必等重算回来 —— 否则用户会以为「三押开关坏了，点不动/能动却没反应」。
  if (f.key === 'use_fixed_dp_angle' || f.key === 'three_press_mode') applyDpLock();
  // ★ 2026-10 「使用激进的拟合策略」/ 求解方式 / 去噪：一拨就刷新依赖它们的置灰
  if (f.key === 'aggressive_fit' || f.key === 'fit_mode' || f.key === 'denoise_on') {
    applyFitLock();
  }
  // ★ offset 被手动改过 ⇒ 之后不再让「自动 offset」覆盖（见 offsetTouched）
  if (f.key === 'offset') {
    offsetTouched = true;
    // ★★ 2026-10：**手改 offset = 明确要手动** ⇒ 顺手把「自动 offset」关掉。
    //   为什么必须这样：后端 `resolve_offset()` 认「自动」优先（导出/预览/视图
    //   都用同一个值），于是「自动开着 + 手改 offset」会互相打架 —— 用户改了 0，
    //   导出的还是自动值（`test_sidecar` 的第三方反解校验就是这么抓到的）。
    //   现在的契约：**自动 offset 由后端说了算；要手改就先关掉它**（这里代劳并上屏）。
    if (state.auto_offset) {
      setControl('auto_offset', false);       // 程序性写值 ⇒ 不会再触发本函数
      setStatus('已改成**手动 offset**（顺手关掉「自动 offset」，免得两边打架）');
    }
  }
  // ★ 反向：**重新勾上「自动 offset」= 明确让它接管** ⇒ 把手动标记清掉。
  //   （否则「先手改 offset，再勾自动」会永远不生效 —— e2e 就是这样抓到的。）
  if (f.key === 'auto_offset' && state.auto_offset) offsetTouched = false;
  // ★★ 2026-10 用户报的 bug：「那个 bpm 一直是不能输入的（输完了会被变回来）」。
  //   根因：`auto_bpm` **默认勾着** ⇒ 后端每次 rebuild 都回一个 `display_bpm`
  //   （**它自己挑**的那个基准），而 `applyPayload` 无条件把它写回输入框
  //   ⇒ 用户敲 200，下一次重算变成 240，看起来「这框根本没法用」。
  //   契约（与 offset 同一套）：**手改基准 BPM = 明确要自己定** ⇒ 顺手把
  //   「自动选基准 BPM」关掉并上屏；之后这个框就是用户的输入，不再被回填。
  if (f.key === 'base_bpm') {
    if (state.auto_bpm) {
      setControl('auto_bpm', false);          // 程序性写值 ⇒ 不会再触发本函数
      setStatus('已改成**手动基准 BPM**：顺手关掉「自动选基准 BPM」，'
        + '否则你输的值下一次重算会被自动值覆盖');
      // ★ 状态栏 ~140ms 后会被这次重算的报告顶掉（那报告里也有「基准 BPM」），
      //   所以再补一个**轻提示**，让「契约变了」这件事当场看得见。
      toast('已改成手动基准 BPM（顺手关掉了「自动选基准 BPM」）');
    }
  }
  if (['lanes', 'division', 'fspeed', 'lanemode'].includes(f.key)) updateFalling();
  // ★ 偏移修正：音乐延迟补偿是**纯播放侧**的校准，不重算谱面，直接打给播放器
  // ★ 2026-10 预览音源：Δ（原曲偏移）同理 —— 也要一起塞进 `musicDelayMs`
  //   （谱面预览页的时钟是谱面轴，不吃 axisLead）。
  if (f.key === 'music_delay_ms' || f.key === 'preview_audio_offset_ms') {
    if (preview) preview.setMusicDelayMs((Number(state.music_delay_ms) || 0) + oggMs());
    if (f.key === 'preview_audio_offset_ms') {
      // 别的四个视图吃 `<audio>` 轴（axisLead 已含 Δ）⇒ 立刻重画一次，
      // 不用等下一次 rAF/秒表（暂停时那两者都不跑）。
      syncFromAudio();
      setStatus(`预览原曲偏移 Δ = ${oggMs() >= 0 ? '+' : ''}${oggMs()} ms`
        + `（预览专用，**不写进谱面**）`);
    }
  }
  // ★ 换音源：`<audio>` 也要跟着换，否则一边放原曲、一边还是上一份合成音
  if (f.key === 'preview_audio_mode' || f.key === 'preview_audio_path') {
    ensureAudio(true);
  }
}

function applyWidthStyle(row, f) {
  if (f.wide) row.classList.add('wide');
}

function buildField(f) {
  if (f.type === 'info') {
    const d = el('div', 'info');
    d.id = `info-${f.key}`;
    // ★ 增量改造（顺手修的原版小 bug）：info 型字段的正文**不一定在 `default` 里** ——
    //   比如 `xk_span_help`（「区间外 →」那一行）`default` 是空串、正文写在 `help` 里，
    //   只印 default 就渲染成一条**空框**（③b 搬回右栏后这条空框更显眼了）。
    //   这里加一条回退：default 为空时用「label + help」。只想回退这一处的话，
    //   把那行改回 `d.textContent = f.default;` 即可，不影响别的逻辑。
    d.textContent = f.default || [f.label, f.help].filter(Boolean).join(' ');
    return d;
  }
  const row = el('div', 'field');
  applyWidthStyle(row, f);
  const lab = el('label');
  lab.textContent = f.label;
  lab.htmlFor = `in-${f.key}`;
  if (f.help) lab.title = f.help;
  row.appendChild(lab);

  const ctl = el('div', 'ctl');
  let input;
  let tail = null;                 // 输入框**后面**要跟的附属控件（目前只有 path 的「选…」）
  if (f.type === 'check') {
    input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = !!getVal(f);
    input.addEventListener('change', () => setVal(f, input.checked));
  } else if (f.type === 'combo') {
    input = document.createElement('select');
    f.options.forEach((o, i) => {
      const op = document.createElement('option');
      op.textContent = o.label;
      op.value = String(i);
      input.appendChild(op);
    });
    const cur = f.options.findIndex((o) => JSON.stringify(o.value) === JSON.stringify(getVal(f)));
    input.selectedIndex = cur < 0 ? 0 : cur;
    input.addEventListener('change', () => setVal(f, f.options[input.selectedIndex].value));
  } else if (f.type === 'text') {
    input = document.createElement('input');
    input.type = 'text';
    input.value = String(getVal(f) ?? '');
    input.addEventListener('input', () => setVal(f, input.value));
  } else if (f.type === 'path') {
    // ★ 2026-10：挑一个**文件**的字段（预览音源用）。
    //   只读文本框 + 「选…」（真路径靠 Electron 的文件对话框拿；手输不改）
    input = document.createElement('input');
    input.type = 'text';
    input.readOnly = true;
    input.value = String(getVal(f) ?? '');
    input.title = String(getVal(f) ?? '');
    input.addEventListener('click', () => pickPath(f, input));
    tail = el('button', 'mini');
    tail.textContent = '选…';
    tail.title = f.help || '选文件';
    tail.onclick = () => pickPath(f, input);
  } else {
    input = document.createElement('input');
    input.type = 'number';
    if (f.min !== undefined) input.min = f.min;
    if (f.max !== undefined) input.max = f.max;
    input.step = f.step !== undefined ? f.step : (f.type === 'int' ? 1 : 'any');
    input.value = String(getVal(f));
    const commit = () => {
      let v = parseFloat(input.value);
      if (!isFinite(v)) v = f.default;
      if (f.min !== undefined) v = Math.max(f.min, v);
      if (f.max !== undefined) v = Math.min(f.max, v);
      if (f.type === 'int') v = Math.round(v);
      setVal(f, v);
    };
    input.addEventListener('change', commit);
    input.addEventListener('blur', commit);
  }
  ctl.appendChild(input);
  input.id = `in-${f.key}`;
  f._input = input;
  if (tail) ctl.appendChild(tail);
  if (f.suffix) {
    const u = el('span', 'unit');
    u.textContent = f.suffix;
    ctl.appendChild(u);
  }
  row.appendChild(ctl);
  return row;
}

/** ★ 「选…」字段（目前只有预览音源的原曲）：挑完写回 state。
 *  `schedule !== false` 的字段要**重算一次** —— 预览音源的轴常量（`audio_lead_ms`：
 *  合成音有前置静音、交出去的文件没有）是后端算的，换了文件必须重算才对得齐。 */
async function pickPath(f, input) {
  const p = window.dsh.openAudio ? await window.dsh.openAudio()
                                 : await window.dsh.openFile();
  if (!p) return;
  state[f.key] = p;
  input.value = p;
  input.title = p;
  if (f.schedule !== false) schedule();
}

function trackList(id, title) {
  const sec = el('section', 'grp');
  const h = el('h4'); h.textContent = title; sec.appendChild(h);
  const body = el('div', 'body');
  const list = el('div', 'list'); list.id = id;
  body.appendChild(list);
  sec.appendChild(body);
  return { sec, list };
}

function buildPanel() {
  const groups = $('#groups');                 // 右：检查器（③③b④④b⑤ 除 xk）
  const groupsL = $('#groups-left');           // 左：来源与段（① 文件 / ② 主轨）
  const groupsXk = $('#groups-xk');            // 浮窗：大直线（③b 采bpm）

  // ★ 显式「不渲染」的字段（**必须写原因**；空集 = 所有 schema 字段都有控件）。
  //   以前这里是「正白名单」，漏加一个字段它就静默消失 ——
  //   `use_fixed_dp_angle` / `three_press_mode` / `sub_gap_ms` 都这么丢过
  //   （用户「拼尽全力找不到三押在哪关」）。现在反过来：默认全渲染，要藏必须写这里。
  const HIDDEN_KEYS = new Set([]);

  groups.innerHTML = ''; groupsL.innerHTML = ''; groupsXk.innerHTML = '';
  const byGroup = {};
  for (const f of SCHEMA.fields) (byGroup[f.group] ||= []).push(f);

  // ① 文件
  {
    const sec = el('section', 'grp');
    const h = el('h4'); h.textContent = '① 文件'; sec.appendChild(h);
    const body = el('div', 'body');
    const row = el('div', 'field');
    row.style.flexWrap = 'wrap';                       // ★ 窄栏里别把按钮文字切掉
    const b = el('button'); b.textContent = '打开 MIDI / BDG 工程 / 时间戳…';
    b.style.flex = '1 1 100%';
    b.title = 'MIDI / BDG 工程 / **毫秒时间戳**（一行一个数，docs/45 §7）都从这里进';
    b.onclick = () => doOpen();
    const sel = document.createElement('select');
    sel.style.flex = '1 1 100%';
    // ★ 首启引导（`docs/49` §10 第 1 项）：第一次打开的人根本不知道
    //   「示例▾」是内置曲子 —— 光写「示例」两个字没人点。把话说全。
    const o0 = document.createElement('option');
    o0.textContent = '示例▾（内置曲子，选一个就能出谱）';
    o0.value = '';
    sel.appendChild(o0);
    sel.onchange = () => { if (sel.value) doLoad(sel.value); sel.selectedIndex = 0; };
    row.appendChild(b); row.appendChild(sel);
    body.appendChild(row);
    const info = buildField(byGroup.file[0]);
    info.id = 'lbl-file';
    body.appendChild(info);
    // ★★ 自动贴合（`docs/58`）：给「AI 扒的 / 抖动极大的」原始文件做**时值体检**，
    //   并一键套用建议参数。**默认什么都不动** —— 不点按钮就没有任何影响。
    {
      const sh = el('div', 'subhead');
      sh.textContent = '自动贴合（时值体检）';
      body.appendChild(sh);
      const trow = el('div', 'field');
      trow.style.flexWrap = 'wrap';
      const tb = el('button', 'mini');
      tb.id = 'td-run';
      tb.textContent = '自动贴合：分析这份曲子的时值';
      tb.style.flex = '1 1 100%';
      tb.title = '逐路找「砖长（= 一条直线的时长）」、量抖动有多大，'
        + '再试算一遍给出「拟合容差 / 去密 / 要不要激进拟合」的建议。'
        + '★ 只是体检：不点下面的「套用」就不会改任何参数。';
      tb.onclick = () => doTempoDiag();
      trow.appendChild(tb);
      body.appendChild(trow);
      const box = el('div', 'hint');
      box.id = 'td-box';
      box.style.whiteSpace = 'pre-wrap';
      box.textContent = '（还没分析。抖动大 / 谱面「整齐但不在音乐上」时，先点一下上面那颗按钮。）';
      body.appendChild(box);
    }
    sec.appendChild(body); groupsL.appendChild(sec);
    sec.dataset.g = 'file';
    bindCollapse(sec, false);
    api.samples().then((r) => {
      (r.samples || []).forEach((name, i) => {
        const op = document.createElement('option');
        op.textContent = name; op.value = r.abs[i];
        sel.appendChild(op);
      });
    });
  }

  // ② 音轨（含 ★ 区间采音）
  {
    const sec = el('section', 'grp');
    const h = el('h4'); h.textContent = '② 主轨（勾选要采音的轨，可多选）'; sec.appendChild(h);
    const body = el('div', 'body');
    // ★★ 2026-10 修 bug：这里原来是**写死的白名单**（只放 dp_tol/dp_mode/dp_reserve/
    //   dp_theta/dp_skew_max_ms）⇒ 后来加进 schema 的 `use_fixed_dp_angle`、
    //   `three_press_mode`、`sub_gap_ms` **一个都没渲染出来** ——
    //   用户「拼尽全力找不到三押在哪关」就是这么来的。
    //   现在改成**整组全渲染**（group='tracks' 的字段一个不漏），顺序由 schema 决定；
    //   要「不显示某个字段」必须显式写进 `HIDDEN` 并写原因，**不许再默默漏掉**。
    for (const f of byGroup.tracks) {
      if (HIDDEN_KEYS.has(f.key)) continue;
      body.appendChild(buildField(f));
    }
    const t1 = el('div', 'subhead');
    t1.id = 'subhead-main';
    t1.textContent = '主轨（可多选）：勾哪几条就采哪几条，取并集';
    const list1 = el('div', 'list'); list1.id = 'lst-tracks';
    const t2 = el('div', 'subhead');
    // ★ 用户 2026-10：「多主轨采音自动合并所有音，但有时候并不需要全部合并，
    //   建议为主轨添加主次级，权重最高的全采，权重低的只插空」。
    t2.textContent = '次级轨（只插空）：只在主轨的空白缝隙里补音';
    const list2 = el('div', 'list'); list2.id = 'lst-sub';
    const t3 = el('div', 'subhead'); t3.textContent = '双押轨（不参与主轨并集）';
    const list3 = el('div', 'list'); list3.id = 'lst-dp';
    body.appendChild(t1); body.appendChild(list1);
    body.appendChild(t2); body.appendChild(list2);
    body.appendChild(t3); body.appendChild(list3);
    const hint = el('div', 'info'); hint.id = 'lbl-track';
    body.appendChild(hint);
    // ---- 区间采音 ----
    const rt = el('div', 'subhead');
    rt.innerHTML = '<b style="color:#9fb0c0">区间采音</b>'
      + '<span class="hint">（框选某段改用别的轨）</span>';
    body.appendChild(rt);
    const rbar = el('div', 'field');
    const bAdd = el('button', 'mini'); bAdd.textContent = '＋ 加区间';
    bAdd.title = '用「当前勾选的轨」在播放头附近加一段区间';
    bAdd.onclick = () => addRegionAtPlayhead();
    const bClr = el('button', 'mini'); bClr.textContent = '清空';
    bClr.onclick = () => { state.regions = []; selectedRegion = -1; renderRegions(); schedule(); };
    rbar.appendChild(bAdd); rbar.appendChild(bClr);
    const rhint = el('span', 'hint'); rhint.textContent = '或在下方全曲条 Shift+拖动框选';
    rbar.appendChild(rhint);
    body.appendChild(rbar);
    const rlist = el('div', 'regions'); rlist.id = 'lst-regions';
    body.appendChild(rlist);
    // ---- 分段采音（docs/34 方案 C）：角色随时间 --------------------------
    const st = el('div', 'subhead');
    st.innerHTML = '<b style="color:#9fb0c0">分段采音</b>'
      + '<span class="hint">（让主/次/双押三个角色都随时间变；'
      + '有分段时上面的区间被整个忽略）</span>';
    body.appendChild(st);
    const sbar = el('div', 'field');
    const sMode = document.createElement('select');
    sMode.id = 'sel-seg-mode';
    for (const [v, txt] of [['from', '从这点起'], ['until', '到这点为止']]) {
      const o = document.createElement('option'); o.value = v; o.textContent = txt;
      sMode.appendChild(o);
    }
    sMode.title = '点的含义：「从这点起用这几条轨」还是「到这点为止」';
    sMode.onchange = () => {
      state.segment_mode = sMode.value;
      setStatus(`分段语义改为「${sMode.selectedOptions[0].textContent}」`);
      renderSegments(); schedule();
    };
    sbar.appendChild(sMode);
    const bSAdd = el('button', 'mini'); bSAdd.textContent = '＋ 加分段';
    bSAdd.title = '在当前播放头处加一条分段（角色默认继承全局 ② 的选择）';
    bSAdd.onclick = () => addSegmentAtPlayhead();
    const bSFrom = el('button', 'mini'); bSFrom.textContent = '从 BDG 角色轨生成';
    bSFrom.title = '把 BDG 编辑器里「主轨/次轨/双押轨/关轨」轨上的点编译成分段';
    bSFrom.onclick = () => genSegmentsFromBridge();
    const bSClr = el('button', 'mini'); bSClr.textContent = '清空';
    bSClr.onclick = () => {
      state.segments = []; selectedSegment = -1;
      renderSegments(); schedule();
    };
    sbar.appendChild(bSAdd); sbar.appendChild(bSFrom); sbar.appendChild(bSClr);
    body.appendChild(sbar);
    const slist = el('div', 'regions'); slist.id = 'lst-segments';
    body.appendChild(slist);
    sec.appendChild(body); groupsL.appendChild(sec);
    sec.dataset.g = 'tracks';
    bindCollapse(sec, false);
  }

  // ③③b④④b⑤⑤b
  const openByDefault = { onset: false, xk: true, solve: false, fit: true, export: true,
    // ★ ⑤b 换手押上色（docs/59）：新组**必须登记在这里**，否则字段根本不渲染
    //   （uiAudit 的 `missing` 立刻变红 —— 这一轮就是这么被抓出来的）。
    color: true,
    // ★ ⑤c 算法轨道调度（docs/60）：新组同样**必须登记在这里**
    appear: true,
    // ★ ⑤d 演出（docs/62）：新组**必须登记**，否则字段根本不渲染（uiAudit 会红）
    show: true };
  for (const [gid, title] of [['onset', '③ 采音'],
    ['xk', '③b 采bpm（xk base · 大直线）'],
    ['solve', '④ 求解 / 几何'],
    ['fit', '④b 去噪 / 直拟合（时序优先）'], ['export', '⑤ 时序 / 导出'],
    ['color', '⑤b 换手押上色（轨道颜色调度）'],
    ['appear', '⑤c 算法轨道调度（皮肤 / 涟漪环 / 半径）'],
    ['show', '⑤d 演出（入场 / 离场 · 分段）']]) {
    const sec = el('section', 'grp');
    const h = el('h4'); h.textContent = title; sec.appendChild(h);
    const body = el('div', 'body');
    for (const f of byGroup[gid]) body.appendChild(buildField(f));
    // ★ 去噪/直拟合（docs/44）：这一组的字段一改就可能**换一条求解路径**，
    //   所以额外给一条「现在到底走的哪条路」的说明行（不许静默）。
    if (gid === 'fit') {
      const row = el('div', 'field');
      const hint = el('span', 'hint');
      hint.id = 'lbl-fit';
      hint.textContent = '最优化 = 像人写的谱（模板/三连音/雪花），代价是量化会挪时序；'
        + '直拟合 = 一砖一音、时序逐点精确（外层几何只有速度档可调）。'
        + '去噪还会顺带告诉 BDG 那边该怎么设分母。';
      row.appendChild(hint);
      body.appendChild(row);
    }
    // ★ ⑤d 演出（docs/62）：分段编辑器 —— **填「起始方块 / 结束方块」**，与游戏里
    //   填 `startTile`/`endTile` 一个口径（都用**当前生成谱面的格子号**）。
    if (gid === 'show') {
      const info = el('div', 'info');
      info.id = 'lbl-show';
      info.textContent = '演出只写**渲染事件**（MoveTrack），绝不碰 angleData/bpm/travel/Twirl。'
        + '没分段、也不是三连音的格子 ⇒ **全程用上面两个预设招**。'
        + '三连音段（求解侧打的段标签）会**自动标出**，默认整段换成反向 QE。';
      body.appendChild(info);
      const bar = el('div', 'field');
      const bAdd = el('button', 'mini'); bAdd.textContent = '＋ 加分段';
      bAdd.id = 'btn-show-add';
      bAdd.title = '加一段：填起始方块 / 结束方块（当前生成谱面的格子号，1 起算），'
        + '段内可各自选入场 / 出场招';
      bAdd.onclick = () => addShowSegment();
      const bClr = el('button', 'mini'); bClr.textContent = '清空';
      bClr.id = 'btn-show-clear';
      bClr.onclick = () => { state.show_segments = []; renderShowSegments(); schedule(); };
      bar.appendChild(bAdd); bar.appendChild(bClr);
      const sp = el('span', 'hint');
      sp.textContent = '留空 = 跟随全局预设；「无」= 这一侧不上。'
        + '优先级：分段 > 三连音段 > 预设';
      bar.appendChild(sp);
      body.appendChild(bar);
      const list = el('div', 'regions'); list.id = 'lst-show';
      body.appendChild(list);
    }
    // ★ ③b 采bpm（xk base / 大直线，docs/47）：区间表 + 上屏报告
    if (gid === 'xk') {
      const xh = el('div', 'info'); xh.id = 'lbl-xk';
      xh.textContent = '采bpm = 「大直线」：把**区间内**硬铺成「N 砖/拍」的等间隔骨架'
        + '（无视音符排列 ⇒ 绝对对拍、绝对不好看，要靠多押加内容）；'
        + '**区间外走原路径**。tbpm 用那个测速站的结果（会四舍六入五成双取整）。';
      body.appendChild(xh);
      const xbar = el('div', 'field');
      const xAdd = el('button', 'mini'); xAdd.textContent = '＋ 加区间（播放头）';
      xAdd.id = 'btn-xk-add';
      xAdd.title = '在播放头附近框一段「只在这段采bpm」';
      xAdd.onclick = () => addXkRangeAtPlayhead();
      const xClr = el('button', 'mini'); xClr.textContent = '清空';
      xClr.id = 'btn-xk-clear';
      xClr.onclick = () => {
        state.xk_ranges = []; renderXkRanges(); schedule();
      };
      xbar.appendChild(xAdd); xbar.appendChild(xClr);
      const xsp = el('span', 'hint');
      xsp.textContent = '起止可以按**毫秒**或**格子号**（1 起算）；每段可各自选 N 与多押轨';
      xbar.appendChild(xsp);
      body.appendChild(xbar);
      const xlist = el('div', 'regions'); xlist.id = 'lst-xk';
      body.appendChild(xlist);
    }
    // ★ 偏移修正（谱面侧）：把 sidecar 算出的「建议 offset」一键写进 offset 框。
    //   以前只把它印在状态栏里，用户得手抄 —— 这就是「偏移修正没被正确使用」的一半。
    if (gid === 'export') {
      const row = el('div', 'field');
      const b = el('button', 'mini');
      b.id = 'btn-apply-offset';
      b.textContent = '应用建议 offset';
      b.title = '把 sidecar 按「首个 onset + 前置静音」算出的 offset 写进 offset 框并重算';
      b.onclick = () => {
        if (lastSuggest === null || lastSuggest === undefined) {
          setStatus('还没有谱面：先加载文件让程序算一次');
          return;
        }
        setControl('offset', Math.round(lastSuggest));
        rebuild();
        setStatus(`偏移修正：offset 已设为 ${Math.round(lastSuggest)}ms（建议值）`);
      };
      row.appendChild(b);
      const h = el('span', 'hint');
      h.textContent = '建议值随音频来源自动变（源音频原样时已扣掉前置静音，见 docs/24 §5）';
      row.appendChild(h);
      body.appendChild(row);
    }
    // ★ ⑤b 换手押上色（`docs/59`）：面板上只放**两行短句**（完整判据在 chips 明细里）。
    if (gid === 'color') {
      const ch = el('div', 'info');
      ch.id = 'lbl-color';
      ch.style.overflowWrap = 'break-word';
      ch.style.wordBreak = 'break-word';
      // ★ 窄栏里**只放两行短句** —— 满行文案会正好填满面板宽度，
      //   e2e 的「没有文本被截断」容差只有 2px，会因亚像素舍入变红（实测 232>230）。
      //   完整判据 / 事件名真相（v19 没有 SetTrackColors）在 chips 明细与字段 tooltip 里。
      ch.textContent = '换手押 = OOX 循环（≥2 周期）\n只有换手押染色，常规格不动';
      body.appendChild(ch);
    }
    // ★ ⑤c 算法轨道调度（`docs/60`）：同样只放**两行短句**（窄栏 + 2px 截断容差）
    if (gid === 'appear') {
      const ch = el('div', 'info');
      ch.id = 'lbl-appear';
      ch.style.overflowWrap = 'break-word';
      ch.style.wordBreak = 'break-word';
      ch.textContent = '驱动 = 谱面结构（图形段起点 + 密度）\n皮肤只改 settings，事件只写 actions';
      body.appendChild(ch);
    }
    sec.appendChild(body);
    // ★ 增量改造：③b 采bpm **不再进「大直线（采bpm）」浮窗**，直接排进右栏检查器
    //   （按 schema 顺序落在 ③ 采音 与 ④ 求解 之间）。
    //   原因：那个浮窗默认尺寸写死 340×250（layout.js 里 `flx{h:250}`，不随内容长高），
    //   而这一组的内容比可视区高 100 多像素 —— 「＋ 加区间」「清空」和下面的区间列表
    //   全被折在浮窗外面，看上去就像"这块板块没了"。右栏是纵向一路铺开的，整组一眼可见，
    //   排法也与工作台一致。
    groups.appendChild(sec);
    sec.dataset.g = gid;
    // ★ 展开态**跨会话记住**（docs/49 §7：高级项默认收起，但记住你展开过的）
    const saved = groupOpenState()[gid];
    bindCollapse(sec, saved === undefined ? !openByDefault[gid] : !saved);
  }

  // ★ 增量改造：③b 搬回右栏后，浮窗 #fl-xk 里就没内容了 —— 打个 fl-empty 标记，
  //   由 workbench-elements.css 把那块整块隐藏（不留一个空浮窗飘在屏幕上）。
  //   必须走 CSS + !important：宿主 layout.js 每次 applyLayout 都会把它的
  //   display 重新写成 flex，用内联样式压不住。
  const flx = $('#fl-xk');
  if (flx && !flx.querySelector('.fbody section')) flx.classList.add('fl-empty');

  buildViewBars();
  renderRegions();
  renderSegments();
  renderXkRanges();
  renderShowSegments();
}

/** 折叠分区（点标题切换；默认收起细节多的组，让首屏只剩「文件 / 音轨 / 导出」）。 */
function bindCollapse(sec, collapsed) {
  const h = sec.querySelector('h4');
  if (!h || h.dataset.bound) return;
  h.dataset.bound = '1';
  const caret = el('span', 'caret'); caret.textContent = '▾';
  h.prepend(caret);
  sec.classList.toggle('collapsed', !!collapsed);
  caret.textContent = collapsed ? '▸' : '▾';
  h.onclick = () => {
    const now = !sec.classList.contains('collapsed');
    sec.classList.toggle('collapsed', now);
    caret.textContent = now ? '▸' : '▾';
    setGroupOpen(sec.dataset.g || h.textContent.trim(), !now);   // ★ 记住展开态
  };
}

function buildViewBars() {
  const vf = (k) => SCHEMA.view_fields.find((f) => f.key === k);
  /** ★★ 2026-10 修 bug（与 ② 主轨同一个病）：这里原来是**写死几个 key**
   *  （`music_delay_ms` / `lanes` / `fspeed` / …）⇒ 后来加进 schema 的
   *  `preview_audio_mode` / `preview_audio_path` / `preview_audio_offset_ms`
   *  **一个都没渲染出来**（「预览音源」功能整个看不见）。
   *  现在按 **group** 整组渲染 —— 以后往 VIEW_FIELDS 里加字段会自动出现。 */
  const renderGroup = (host, gid) => {
    host.innerHTML = '';
    for (const f of SCHEMA.view_fields) {
      if (f.group === gid) host.appendChild(buildField(f));
    }
  };
  // 谱面预览：视角/缩放交给内嵌的 ADOFAI 播放器。
  // ★ 增量改造（2026-09-19）：这一行原来横排「偏移修正 + 按实测建议 + 预览音源三件套」
  //   ≈ 900px ⇒ 窄窗口必出横向滚动条、还把预览区挤小（用户报的「显示不全」）。
  //   现在只留「一行说明 + 一颗药丸」，那几个控件全搬进浮层 #av-pop。
  const c = $('#vbar-chart');
  c.innerHTML = '';
  const hint = el('span', 'hint');
  hint.textContent = '谱面预览 = ADOFAI 官方渲染引擎（Re_ADOJAS，见 docs/22）';
  hint.title = '谱面预览 = ADOFAI 官方渲染引擎（Re_ADOJAS，见 docs/22）'
    + ' / 播放键与空格控制它 / 全曲条选格会同步过去';
  c.appendChild(hint);
  // ★ 药丸：当前音源档 + 偏移值。点它（或点它右边那颗）开浮层。
  const sum = el('button', 'vsum');
  sum.id = 'btn-av-sum';
  sum.title = '点开「音源与偏移」设置';
  sum.onclick = (e) => { e.stopPropagation(); toggleAvPop(sum); };
  c.appendChild(sum);
  // ★★ 浮层**启动即建**（保持隐藏）：这样 chartview 那 4 个控件的 `_input` 从一开机就
  //   存在 ⇒ `setControl()` / `syncControls()` 的回写行为与「控件原本就摆在工具条上」
  //   完全一致（以前踩过：控件晚建 ⇒ 程序性写值静默丢失）。
  buildAvPop();
  refreshAvSummary();
  // 下落式 / 路径：整组渲染
  renderGroup($('#vbar-falling'), 'fallingview');
  renderGroup($('#vbar-path'), 'pathview');
  const fit = el('button'); fit.textContent = '自适应';
  fit.onclick = () => { views.path.fit(); drawActive(); };
  $('#vbar-path').appendChild(fit);
  const pth = el('span', 'hint');
  pth.textContent = '滚轮=缩放 / Shift+滚轮=切跟随 / 左键拖动=平移 / 中键=切跟随 / 双击=自适应';
  $('#vbar-path').appendChild(pth);
}

/* ==========================================================================
   ★ 增量改造（2026-09-19）：工具条「音源与偏移」浮层
   --------------------------------------------------------------------------
   为什么要：原来 `#vbar-chart` 里横排 hint + 偏移修正 + 按实测建议 + 预览音源三件套
   ≈ 900px，窄窗口必出横向滚动条，还把预览区挤小（用户报的「显示不全」）。
   功能一个没少，只是换了它们**摆在哪**：工具条只留一颗药丸，其余进浮层。
   交互：点药丸开 / 点浮层外关 / Esc 关 / 优先浮在工具条上方，上面放不下才放下方。
   浮层在 `buildViewBars()` 里**启动即建**（保持隐藏），使控件的 `_input` 一直在。
   ========================================================================== */
let avPopBuilt = false;
let avAnchor = null;                    // 浮层贴着的那个锚点（改窗口大小时要按它重算位置）
const avPop = () => document.getElementById('av-pop');

function buildAvPop() {
  if (avPopBuilt) return avPop();
  const pop = el('div'); pop.id = 'av-pop'; pop.className = 'hidden';
  const head = el('div', 'avhead');
  const t = el('span'); t.textContent = '音源与偏移（仅预览）';
  head.appendChild(t);
  head.appendChild(el('span', 'grow'));
  const x = el('button', 'ghost mini'); x.textContent = '✕'; x.title = '关闭';
  x.onclick = () => closeAvPop();
  head.appendChild(x);
  pop.appendChild(head);

  const body = el('div', 'avbody');
  const h1 = el('div', 'hint');
  h1.textContent = '偏移修正「正值 = 音乐相对游戏时间线延后」；原曲偏移 Δ 只作用于预览 —— '
    + '两者都**不写进 .adofai**。';
  body.appendChild(h1);
  // ★ 整组渲染（按 group，不写死 key）—— 以后往 VIEW_FIELDS 里加字段会自动出现
  for (const f of SCHEMA.view_fields) {
    if (f.group === 'chartview') body.appendChild(buildField(f));
  }
  const row = el('div', 'row');
  const auto = el('button'); auto.id = 'btn-music-delay-auto';
  auto.textContent = '按实测建议';
  auto.title = '用播放器按按键误差算出的建议补偿（需要先在预览里手动打够 5 次）';
  auto.onclick = () => {
    if (!preview) { setStatus('谱面预览还没就绪'); return; }
    const v = preview.getSuggestedMusicDelayMs();
    if (v === null || v === undefined) {
      setStatus('播放器还没攒够按键样本（先在预览里手动打 5 次以上）');
      return;
    }
    setControl('music_delay_ms', v);
    preview.setMusicDelayMs(v);
    setStatus(`偏移修正：已按实测建议设为 ${v}ms`);
  };
  row.appendChild(auto);
  const ah = el('span', 'hint'); ah.textContent = '先在预览里手动打 5 次以上';
  row.appendChild(ah);
  body.appendChild(row);
  pop.appendChild(body);
  document.body.appendChild(pop);

  // 点浮层外面关掉（浮层挂在 body 下，不在任何 pane 里 ⇒ 不受移动模式冻结影响）
  document.addEventListener('mousedown', (e) => {
    if (pop.classList.contains('hidden')) return;
    if (pop.contains(e.target)) return;
    if (e.target && e.target.closest && e.target.closest('#btn-av-sum, #av-pop')) return;
    closeAvPop();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !pop.classList.contains('hidden')) closeAvPop();
  });
  // ★ 改窗口大小时按锚点重算位置 —— 不然固定坐标会留在原地，
  //   窗口一变窄它就跑到屏幕外（实测窄到 980px 就出界了）。
  window.addEventListener('resize', () => {
    if (pop.classList.contains('hidden') || !avAnchor) return;
    if (!avAnchor.isConnected) { closeAvPop(); return; }
    openAvPop(avAnchor);
  });
  avPopBuilt = true;
  return pop;
}

function openAvPop(anchor) {
  const pop = buildAvPop();
  avAnchor = anchor || avAnchor;
  pop.classList.remove('hidden');
  refreshAvSummary();
  const r = anchor.getBoundingClientRect();
  const w = pop.offsetWidth || 430, h = pop.offsetHeight || 220;
  let left = Math.min(r.right - w, window.innerWidth - w - 8);
  left = Math.max(8, left);
  pop.style.left = Math.round(left) + 'px';
  // 优先浮在工具条**上方**（不挡预览下沿）；上面放不下才改放下方
  if (r.top - 6 - h >= 8) {
    pop.style.top = '';
    pop.style.bottom = Math.round(window.innerHeight - r.top + 6) + 'px';
  } else {
    pop.style.bottom = '';
    pop.style.top = Math.round(Math.min(r.bottom + 6, window.innerHeight - h - 8)) + 'px';
  }
}
function closeAvPop() { const p = avPop(); if (p) p.classList.add('hidden'); }
function toggleAvPop(anchor) {
  const p = buildAvPop();
  if (p.classList.contains('hidden')) openAvPop(anchor); else closeAvPop();
}

/** 药丸上的摘要：音源档 + 偏移值（Δ 非 0 才显示）。跟手刷新。 */
function refreshAvSummary() {
  const b = document.getElementById('btn-av-sum');
  if (!b) return;
  const mode = Number(state.preview_audio_mode || 0);
  const modeTxt = ['自动', '合成音', '指定文件'][mode] || '自动';
  const off = Number(state.music_delay_ms || 0);
  const dlt = Number(state.preview_audio_offset_ms || 0);
  let html = `⚙ 音源 <b>${modeTxt}</b> · 偏移 <b>${off} ms</b>`;
  if (mode === 2 && state.preview_audio_path) {
    const p = String(state.preview_audio_path).split(/[\\/]/).pop();
    html += ` · <b>${p.length > 16 ? p.slice(0, 16) + '…' : p}</b>`;
  }
  if (dlt) html += ` · Δ <b>${dlt} ms</b>`;
  b.innerHTML = html;
  b.title = `音源：${modeTxt} · 偏移修正 ${off} ms`
    + (dlt ? ` · 原曲偏移 Δ ${dlt} ms` : '')
    + '\n（点开可改：预览音源 / 原曲文件 / 偏移修正 / 原曲偏移）';
}

/** ★★ UI 体检（2026-10 加）：**每一个 schema 字段都必须有控件**。
 *
 *  为什么要有这个：`buildPanel`/`buildViewBars` 以前用**写死的 key 列表**渲染 ⇒
 *  往 schema 里新加一个字段，它会**静默消失**（三押开关、固定双押角度、插空阈值、
 *  预览音源三件套全都这么丢过，用户只能「拼尽全力找不到」）。
 *  返回值 = 没有控件的 key 列表（空 = 全都在）；`main()` 里会 toast + console.error，
 *  e2e 也会拿它当断言。 */
function uiAudit() {
  const miss = [];
  for (const f of [...(SCHEMA.fields || []), ...(SCHEMA.view_fields || [])]) {
    if (f.type === 'info') continue;          // info = 只读说明行，本来就不是控件
    if (!f._input) miss.push(f.key);
  }
  return { missing: miss, n_fields: (SCHEMA.fields || []).length,
           n_view: (SCHEMA.view_fields || []).length };
}

/** 程序性写控件值（不触发防抖）。 */
function setControl(key, value) {
  const f = [...SCHEMA.fields, ...SCHEMA.view_fields].find((x) => x.key === key);
  if (!f || !f._input) return;
  applying = true;
  try {
    if (f.type === 'check') f._input.checked = !!value;
    else if (f.type === 'combo') {
      const i = f.options.findIndex((o) => JSON.stringify(o.value) === JSON.stringify(value));
      if (i >= 0) f._input.selectedIndex = i;
    } else f._input.value = String(value);
  } finally {
    applying = false;
  }
  state[key] = value;
  // ★★ 2026-10：**依赖置灰必须跟着「程序性写值」一起刷新**。
  //   `setControl()` 是程序写值的**唯一入口** —— e2e 的 `setParam`、载入文件后
  //   那些 `default_*`（`default_fit_mode` 会把 fit_mode 写成 direct）全走它。
  //   以前只有用户手点才刷新（`onFieldChanged`），于是「载入时间戳来源 ⇒ 求解方式
  //   自动变直拟合 ⇒ 激进拟合那个框却还是灰的」—— 与用户报的 BPM 框同一类毛病
  //   （控件状态和后端真实状态对不上）。e2e 一加断言就抓到了。
  if (key === 'aggressive_fit' || key === 'fit_mode' || key === 'denoise_on') applyFitLock();
  if (key === 'use_fixed_dp_angle' || key === 'three_press_mode') applyDpLock();
  if (f.group === 'chartview') refreshAvSummary();   // ★ 增量改造：工具条药丸摘要跟手
}

function syncControls() {
  for (const f of [...SCHEMA.fields, ...SCHEMA.view_fields]) {
    if (!f._input) continue;
    applying = true;
    try {
      if (f.type === 'check') f._input.checked = !!state[f.key];
      else if (f.type === 'combo') {
        const i = f.options.findIndex((o) => JSON.stringify(o.value) === JSON.stringify(state[f.key]));
        if (i >= 0) f._input.selectedIndex = i;
      } else f._input.value = String(state[f.key]);
    } finally { applying = false; }
  }
}

// ---------------------------------------------------------- 音轨列表
function renderTracks(info) {
  const l1 = $('#lst-tracks'); const l2 = $('#lst-sub'); const l3 = $('#lst-dp');
  l1.innerHTML = ''; l2.innerHTML = ''; l3.innerHTML = '';
  // 三份列表的渲染逻辑一样，只有「绑哪个 state 数组 + 落到哪个容器」不同
  const _mk = (host, key, listKey, after) => {
    for (const t of listKey) {
      const lab = el('label');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = state[key].includes(t.index);
      if (!t.has_notes) cb.disabled = true;
      cb.onchange = () => {
        const s = new Set(state[key]);
        if (cb.checked) s.add(t.index); else s.delete(t.index);
        state[key] = [...s].sort((a, b) => a - b);
        if (key === 'tracks_checked') state.current_track = t.index;
        syncTrackSel();
        (after || onTracksChanged)();
      };
      const txt = el('span', 't' + (t.drum ? ' drum' : ''));
      // ★ BDG 来源：把「建议角色」摆在轨道名后面（只是建议 —— 勾选权在用户）
      const tag = t.suggest
        ? ({ main: '主', sub: '次', dp: '双押', off: '关' }[t.suggest] || t.suggest)
        : '';
      // ★ 时间戳 JSON（分轨）：还多一句人话注释（为什么这么建议 / 钢琴是赠品…）
      txt.textContent = (t.summary || t.label)
        + (tag ? `　［建议：${tag}］` : '')
        + (t.note ? `　（${t.note}）` : '');
      lab.appendChild(cb); lab.appendChild(txt);
      lab.dataset.index = String(t.index);
      if (key === 'tracks_checked') {
        lab.onclick = (e) => {
          if (e.target === cb) return;
          state.current_track = t.index;
          syncTrackSel(); onTracksChanged();
        };
      }
      host.appendChild(lab);
    }
  };
  _mk(l1, 'tracks_checked', info.tracks || []);
  _mk(l2, 'sub_checked', info.sub || []);
  _mk(l3, 'dp_checked', info.dp || []);
  syncTrackSel();
}

function syncTrackSel() {
  for (const lab of document.querySelectorAll('#lst-tracks label')) {
    lab.classList.toggle('sel', Number(lab.dataset.index) === state.current_track);
  }
}

async function onTracksChanged() {
  // 主轨勾选变了 ⇒ 让 sidecar 推导「实际采音集合 + 音高过滤建议」，
  // 这里只做「音高过滤自动覆写 + 提示 + 防抖重建」。
  const d = await api.derive(state);
  if (d.pitch_lo !== null && d.pitch_lo !== undefined) {
    setControl('pitch_lo', d.pitch_lo);
    setControl('pitch_hi', d.pitch_hi);
  }
  $('#lbl-track').textContent = d.hint || '';
  schedule();
}

/** ★★ **自动贴合**（`docs/58`）：给当前曲子做一次「时值体检」，把结论摆在左栏，
 *  再给一颗「按建议套用」的按钮。**体检本身不改任何参数**（不点就不动）。 */
let tdReport = null;

async function doTempoDiag() {
  const box = $('#td-box');
  const btn = $('#td-run');
  if (!loadInfo) { setStatus('先载入一个文件（MIDI / BDG 工程 / 时间戳）。'); return; }
  if (btn) { btn.disabled = true; btn.textContent = '自动贴合：分析中…'; }
  if (box) box.textContent = '正在分析（逐路找砖长 + 试算两遍）… ';
  let d = null;
  try {
    d = await api.tempoDiag(state);
  } catch (e) {
    d = { ok: false, why: `请求失败：${(e && e.message) || e}` };
  }
  if (btn) { btn.disabled = false; btn.textContent = '自动贴合：重新分析'; }
  tdReport = d;
  renderTempoDiag(d);
}

function renderTempoDiag(d) {
  const box = $('#td-box');
  if (!box) return;
  if (!d || d.ok !== true) {
    box.textContent = `⚠ 体检没跑成：${(d && d.why) || '未知原因'}`;
    return;
  }
  const S = d.suggest || {};
  const rows = d.streams || [];
  const lines = [];
  lines.push(`★ 建议砖长（= 一条直线的时长）：**${(+d.brick_ms).toFixed(3)}ms**`
    + `（${(+d.bpm).toFixed(1)} BPM）· 覆盖率 ${((d.brick_cover || 0) * 100).toFixed(1)}%`
    + ` · 来源：${d.brick_source === 'auto' ? '自动网格识别（已精修）' : '音间隔中位数'}`
    + `${d.brick_reliable === false ? ' ⚠ 不可靠（点太少/太乱）' : ''}`);
  lines.push(`★ 建议：拟合容差 **${S.fit_tol_ms}ms** · 去密 merge **${S.merge_ms}ms**`
    + ` · 激进拟合 **${S.aggressive_fit ? '开' : '不开'}**`
    + ` · 主轨 **${S.main_label}**（覆盖率 ${((S.main_cover || 0) * 100).toFixed(1)}%）`);
  const po = (d.probe || {}).off || {};
  const pn = (d.probe || {}).on || {};
  if (po.ok && pn.ok) {
    lines.push(`　试算对比（只对主轨）：不经激进 → 直线 ${((po.straight_frac || 0) * 100).toFixed(1)}%`
      + ` / 踩拍 ${((po.beat_frac || 0) * 100).toFixed(1)}% / 层 ${po.floors}`
      + `；经激进 → 直线 ${((pn.straight_frac || 0) * 100).toFixed(1)}%`
      + ` / 踩拍 ${((pn.beat_frac || 0) * 100).toFixed(1)}% / 层 ${pn.floors}`);
  }
  lines.push('　每路：残差(中位/p90/max) · 覆盖@±' + (S.fit_tol_ms || 0) + 'ms');
  for (const r of rows) {
    const c = (r.cover || {})[String(Math.round(S.fit_tol_ms || 0))];
    lines.push(`　· ${r.label}：${r.median_ms}/${r.p90_ms}/${r.max_ms}ms`
      + ` · ${c === undefined ? '—' : (c * 100).toFixed(1) + '%'}`
      + ` · ${r.n} 点 · 间隔中位 ${r.median_interval_ms}ms`);
  }
  for (const w of (d.why || [])) lines.push('  ' + w);
  box.textContent = lines.join('\n');
  // 「按建议套用」按钮（只在这里出现；体检本身不动参数）
  let ab = $('#td-apply');
  if (!ab) {
    ab = el('button', 'mini');
    ab.id = 'td-apply';
    ab.style.flex = '1 1 100%';
    ab.textContent = '按建议套用（改参数 + 重算）';
    ab.title = '把上面那组参数填进各栏并重算一次。不改文件、不导出。';
    box.parentNode.insertBefore(ab, box.nextSibling);
  }
  ab.onclick = () => doTempoApply();
}

/** 把体检建议填进各栏并重算（**只填参数**：不碰文件、不导出）。 */
async function doTempoApply() {
  const d = tdReport;
  if (!d || d.ok !== true) { setStatus('先点一次「自动贴合：分析」'); return; }
  const S = d.suggest || {};
  if (typeof S.fit_mode === 'string') setControl('fit_mode', S.fit_mode);
  if (typeof S.denoise_on === 'boolean') setControl('denoise_on', S.denoise_on);
  if (typeof S.denoise_hint_ms === 'number') setControl('denoise_hint_ms', S.denoise_hint_ms);
  if (typeof S.fit_tol_ms === 'number') setControl('fit_tol_ms', S.fit_tol_ms);
  if (typeof S.aggressive_fit === 'boolean') setControl('aggressive_fit', S.aggressive_fit);
  if (typeof S.merge_ms === 'number') setControl('merge_ms', S.merge_ms);
  state.tracks_checked = S.tracks_checked || state.tracks_checked;
  state.dp_checked = S.dp_checked || [];
  state.sub_checked = S.sub_checked || [];
  state.current_track = (state.tracks_checked[0] !== undefined
    ? state.tracks_checked[0] : state.current_track);
  renderTracks(loadInfo || {});
  await onTracksChanged();          // 走正规路径（derive + 防抖 + 重算）
  toast(`自动贴合已套用：砖长 ${(+d.brick_ms).toFixed(3)}ms · 容差 ${S.fit_tol_ms}ms`
    + ` · 去密 ${S.merge_ms}ms · 激进 ${S.aggressive_fit ? '开' : '不开'}`);
  setStatus(`自动贴合已套用（砖长 ${(+d.brick_ms).toFixed(3)}ms / 容差 ${S.fit_tol_ms}ms`
    + ` / 去密 ${S.merge_ms}ms）—— 想回退就重新载入文件`);
}

// ------------------------------------------------------------- 防抖重建
function schedule() {
  if (applying) return;
  if (rebuildTimer) clearTimeout(rebuildTimer);
  rebuildTimer = setTimeout(() => { rebuildTimer = null; rebuild(); }, 140);  // 旧 UI 140ms
}

async function rebuild() {
  if (!loadInfo) return;
  rebuildCount += 1;
  setStatus('求解中…');
  const r = await api.rebuild(state);
  lastResult = r;
  if (!r.ok) {
    setStatus((r.stale ? '⚠ 保留了上一张谱面：' : '') + (r.msg || r.error || '失败'));
    // ★ 失败时把 sidecar 带回来的警告也显示出来（否则真因被吃掉 = 静默）
    const wBad = r.warning_list || [];
    if (wBad.length) {
      $('#warnings').textContent = '⚠ ' + wBad.join('；');
      $('#warn-badge').textContent = `（${wBad.length} 条警告）`;
      $('#more').open = true;
    }
    // ★ 失败时**不能留上一轮的 ③b 报告**（用户实测踩过：重建被拒，界面还在显示
    //   上一次的「注入骨架 964 块砖」，看起来像"明明做了却没生效"）。
    //   标记只加一次，别叠成 4 行。
    const lb = $('#lbl-xk');
    const mark = '⚠ 本次**没有重算**';
    if (lb && !String(lb.textContent || '').startsWith(mark)) {
      lb.textContent = `${mark}（${r.msg || r.error || '失败'}）`
        + `　—— 以下是**上一次**的采bpm 报告（可能已过期）：\n${lb.textContent || ''}`;
    }
  applyXkLock();
  applyDpLock();                       // ★ 失败也要把「② 主轨置灰」刷对
  applyFitLock();                      // ★ 失败也要把「激进拟合」那几项刷对
    return;
  }
  hasChart = true;
  // ★ 兜底：能重算成功就说明加载阶段早结束了 ⇒ 顺手把进度条收起来
  //   （SSE 的 `loaded` 事件偶尔丢/晚到时，它是唯一会让进度条赖着不走的地方）
  showProgress(false);
  if (r.check && typeof r.check.suggest_ms === 'number') lastSuggest = r.check.suggest_ms;
  // 程序性写回（★ 必须走 setControl，不触发防抖）
  // ★★ 2026-10：`display_bpm` 只在**自动模式**下回填（那时这个框就是「显示」），
  //   手动模式（`auto_bpm=false`）下它是用户的输入 —— **一个字都不许改**。
  //   以前无条件回填 ⇒ 用户输 200 被 240 覆盖（用户报的「输完了会被变回来」）。
  if (r.display_bpm && state.auto_bpm) {
    setControl('base_bpm', Math.round(r.display_bpm * 1000) / 1000);
  }
  // ★★ 2026-10「开门一遍」：`auto_offset` 现在**默认开**，但**不许抢用户的输入** ——
  //   只要他手动碰过 offset（`offsetTouched`），就不再拿自动值覆盖。
  //   没碰过 ⇒ **每次都写回**（这就是「零误差配方」）。
  //   ⚠ 以前这里是「值变了才写」（`lastAutoOffset` 去重），实测会**自相矛盾**：
  //     自动值恰好没变（比如一直是 0）、而 offset 框被别的途径改成了 12345 ⇒
  //     界面显示 12345、导出却用自动值 0。客户端的显示必须**永远等于后端要用的那个值**。
  //     （写值走 `setControl`，`applying=true` ⇒ 不会触发重算回环，e2e 也在守这条。）
  if (r.auto_offset !== null && r.auto_offset !== undefined && !offsetTouched) {
    lastAutoOffset = r.auto_offset;
    setControl('offset', r.auto_offset);
  }
  payload = r.payload;
  applyPayload();
  setStatus(r.status || '');
  $('#timing').textContent = r.timing || '';
  const w = r.warning_list || [];
  $('#warnings').textContent = w.length ? ('⚠ ' + w.join('；')) : '';
  $('#warn-badge').textContent = w.length ? `（${w.length} 条警告）` : '';
  if (w.length) $('#more').open = true;
  // ★ ④b 去噪/直拟合（docs/44）：这一行直接说明「现在走的是哪条路、代价多大」
  const lblFit = $('#lbl-fit');
  if (lblFit) {
    const fr = r.fit || {}; const dnr = r.denoise || {};
    if (r.fit_mode === 'direct') {
      lblFit.textContent = `当前：直拟合（一砖一音 · 选档 ${fr.tier_mode === 'sticky' ? '粘住上一档' : 'DP'}）`
        + `· 去噪 ` + (dnr.on ? `1/${dnr.div} 格 ${(dnr.step_ms || 0).toFixed(2)}ms`
                             : '关（几何是任意有理数）')
        + ` · 直线 ${((fr.straight_frac || 0) * 100).toFixed(1)}%`
        + ` · SetSpeed ${fr.n_setspeed || 0}`
        + ` · 发卡弯 ${fr.n_hairpin || 0}`
        + (fr.n_fill ? ` · 填充层 ${fr.n_fill}` : '')
        + (fr.n_pause ? ` · 休止格 ${fr.n_pause}` : '')
        + ` · 时序误差 max ${(fr.err_max_ms || 0).toFixed(4)}ms`
        + `（最小角度 ${fr.travel_min_setting || 0}° / 一档≥${fr.speed_min_run || 1} 层）`
        + fitLadderText(fr);
    } else {
      lblFit.textContent = '当前：最优化（④ 求解/几何那套：模板/三连音/雪花）'
        + (dnr.grid ? `　去噪已算（1/${dnr.div}）但最优化这条路不用它` : '');
    }
  }
  // ★ ③b 采bpm（docs/47）：报告上屏（取代了多少真实 onset / 注入多少砖 / 区间外多少）
  const lblXk = $('#lbl-xk');
  if (lblXk) {
    const xr = r.xk || {};
    lblXk.textContent = xr.text
      || '采bpm：关（③b 选「关」，或 tbpm 没填）—— 全曲走原路径。';
  }
  applyXkLock();
  applyDpLock();
  applyFitLock();                      // ★ 2026-10：激进拟合 / 最小角度 / 容差的依赖置灰
  // ★★ 2026-10：小窗「格子 ↔ 毫秒」**当场**刷一次。
  //   它原来只在播放时跟着 `syncFromAudio()` 更新 ⇒ 刚载入文件 / 刚改完 tbpm+N
  //   时显示的还是上一次的格号（看着像「改了没生效」），暂停时更是永远不动。
  //   ⚠ `refreshXkHud` 在 `gridMs` 非有限值时只做 `hidden=true` 就 return（保留旧文案）
  //     ⇒ 这里必须给一个**有限**的值（播放头 0ms 也算）。
  refreshXkHud(audioToGrid(player.currentTime * 1000 || 0));
  const dv = $('#ver');
  if (dv && r.base_bpm) dv.textContent = `v${SCHEMA.version || '0.5.0-preview'} · bpm ${r.base_bpm.toFixed(2)}`;
  $('#lbl-track').textContent = (r.derive && r.derive.hint) || $('#lbl-track').textContent;
  $('#tab-hint').textContent = `${r.n_floors} 层 · ${r.n_onsets} 采音点 · 双押 ${(r.dp && r.dp.dp_hits) || 0}`
    // ★ 三押（docs/48）：**不许静默** —— 有就写出来，四押被跳过也写出来
    + ((r.dp && r.dp.dp_three) ? ` （含三押 ${r.dp.dp_three}）` : '')
    + ((r.dp && r.dp.dp_extra_press) ? ` · 跳过四押 ${r.dp.dp_extra_press}` : '')
    + (r.n_segments ? ` · 分段 ${r.n_segments}${
      r.segment_mode === 'until' ? '（到这点为止）' : '（从这点起）'}` : '');
  if (player.src && Math.abs(player.currentTime * 1000 - 0) < 1) seek(0);
  // ★ 报告带：必须看的账常驻（不许静默）
  renderChips(r);
  // ★ 段带：段 + 标记（双押/三押）+ 数据都跟着这次重建走
  band.segs = [];                       // 逼 setData 重算段（区间/分段刚变过）
  band.setData(payload);
}

function applyPayload() {
  if (!payload) return;
  views.roll.setData(payload);
  views.path.setData(payload);
  overview.setData(payload);
  overview.sel = selectedRegion;
  updateFalling();
  drawActive();
  overview.draw();
  renderRegions();
  band.setData(payload);
  if (activeTab === 'chart') ensurePreview();
}

// --------------------------------------------------------------- 下落式
function bisectRight(arr, x) {
  let lo = 0; let hi = arr.length;
  while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] <= x) lo = m + 1; else hi = m; }
  return lo;
}

function updateFalling() {
  if (!payload) return;
  const falls = payload.falls || [];
  const lanes = Number(state.lanes) || 4;
  const mode = Number(state.lanemode) || 0;
  const ps = [...new Set(falls.map((f) => f.pitch))].sort((a, b) => a - b);
  const lo = ps.length ? ps[0] : 0;
  const span = Math.max(1, (ps.length ? ps[ps.length - 1] : 0) - lo);
  const mapped = falls.map((f, i) => {
    let lane;
    if (mode === 0) lane = Math.trunc(((bisectRight(ps, f.pitch) - 0.5) / Math.max(1, ps.length)) * lanes);
    else if (mode === 1) lane = Math.round(((f.pitch - lo) / span) * (lanes - 1));
    else lane = i % lanes;
    return { ...f, lane: Math.max(0, Math.min(lanes - 1, lane)) };
  });
  views.falling.setChart({ bpm0: payload.bpm0, falls: mapped, cap: payload.cap },
    { lanes, speed: Number(state.fspeed) || 100, division: Number(state.division) || 8 });
  views.falling.setTime(audioToGrid(player.currentTime * 1000 || 0));
  if (activeTab === 'falling') drawActive();
}

// ---------------------------------------------------------- 区间采音
/** 用「当前勾选的轨」在 [t0,t1] 加一段区间采音。 */
function addRegion(t0, t1) {
  if (!loadInfo) return;
  const tracks = state.tracks_checked.length ? [...state.tracks_checked]
    : (loadInfo.tracks || []).filter((t) => t.has_notes).map((t) => t.index);
  if (!tracks.length) { setStatus('⚠ 这条曲目没有可采音的音轨'); return; }
  const lo = Math.max(0, Math.min(t0, t1));
  const hi = Math.min(payload ? payload.total_ms : 1e9, Math.max(t0, t1));
  const rg = {
    start_ms: Math.round(lo), end_ms: Math.round(hi), tracks,
    label: `区间${state.regions.length + 1}`,
  };
  state.regions = [...state.regions, rg];
  selectedRegion = state.regions.length - 1;
  setStatus(`已框选 ${(lo / 1000).toFixed(2)}–${(hi / 1000).toFixed(2)}s`
    + `（用 trk${tracks.join(',trk')} 采音）`);
  renderRegions();
  schedule();
}

function addRegionAtPlayhead() {
  const t = audioToGrid(player.currentTime * 1000 || 0);
  const total = payload ? payload.total_ms : t + 8000;
  addRegion(Math.max(0, t - 2000), Math.min(total, t + 6000));
}

function selectRegion(i) {
  selectedRegion = i;
  overview.sel = i;
  renderRegions();
  overview.draw();
}

function renderRegions() {
  const box = $('#lst-regions');
  if (!box) return;
  box.innerHTML = '';
  const regs = state.regions || [];
  if (!regs.length) {
    const d = el('div', 'hint');
    d.textContent = '（没有区间：全局按「参与采音的轨」采；'
      + '在下方的全曲预览条上 Shift+拖动即可框选）';
    box.appendChild(d);
    return;
  }
  const trs = (loadInfo && loadInfo.tracks) || [];
  regs.forEach((rg, i) => {
    const row = el('div', 'region' + (i === selectedRegion ? ' sel' : ''));
    row.dataset.index = String(i);
    // 标题行：名字 + 删除
    const head = el('div', 'rhead');
    const name = document.createElement('input');
    name.className = 'rname';
    name.value = rg.label || `区间${i + 1}`;
    name.onchange = () => { rg.label = name.value; overview.draw(); };
    const del = el('button', 'rdel'); del.textContent = '✕'; del.title = '删除该区间';
    del.onclick = () => {
      state.regions = regs.filter((_x, k) => k !== i);
      if (selectedRegion === i) selectedRegion = -1;
      renderRegions(); schedule();
    };
    head.appendChild(name); head.appendChild(del);
    row.appendChild(head);
    // 时间行
    const trow = el('div', 'rtime');
    const mkTime = (key, val) => {
      const inp = document.createElement('input');
      inp.type = 'number'; inp.step = '100'; inp.value = String(Math.round(val));
      inp.onchange = () => {
        rg[key] = Math.max(0, Number(inp.value) || 0);
        if (rg.end_ms <= rg.start_ms) rg.end_ms = rg.start_ms + 100;
        renderRegions(); schedule();
      };
      return inp;
    };
    trow.appendChild(mkTime('start_ms', rg.start_ms));
    const arrow = el('span'); arrow.textContent = '→';
    trow.appendChild(arrow);
    trow.appendChild(mkTime('end_ms', rg.end_ms));
    const dur = el('span'); dur.textContent = ` (${((rg.end_ms - rg.start_ms) / 1000).toFixed(2)}s)`;
    trow.appendChild(dur);
    row.appendChild(trow);
    // 轨选择（chips）
    const crow = el('div', 'rrow');
    const lbl = el('span', 'hint'); lbl.textContent = '用轨：';
    crow.appendChild(lbl);
    for (const t of trs) {
      if (!t.has_notes) continue;
      const c = el('div', 'chip' + (rg.tracks.includes(t.index) ? ' on' : '')
        + (t.drum ? ' drum' : ''));
      c.textContent = `trk${t.index}·${t.notes}`;
      c.title = t.drum ? '架子鼓轨' : (t.name || '');
      c.onclick = () => {
        const s = new Set(rg.tracks);
        if (s.has(t.index)) s.delete(t.index); else s.add(t.index);
        rg.tracks = [...s].sort((a, b) => a - b);
        renderRegions(); schedule();
      };
      crow.appendChild(c);
    }
    row.appendChild(crow);
    row.onclick = (e) => {
      if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT'
        || e.target.classList.contains('chip') || e.target.classList.contains('rdel')) return;
      selectRegion(i);
    };
    box.appendChild(row);
  });
}

// ---------------------------------------------------------- 分段采音（docs/34 方案 C）
/** 一条分段：`at_ms` + 三个维度各自 `null`(继承) / `[]`(关掉) / `[轨…]`。 */
function segDefaults() {
  return { at_ms: 0, label: '', main: null, sub: null, dp: null };
}

function addSegment(t) {
  if (!loadInfo) return;
  const total = payload ? payload.total_ms : (t + 8000);
  const at = Math.max(0, Math.min(total, Math.round(t)));
  const list = state.segments || [];
  if (list.some((s) => Math.abs(s.at_ms - at) < 1)) {
    setStatus(`⚠ ${(at / 1000).toFixed(2)}s 已有一条分段（同一时刻「后写的赢」）`);
    return;
  }
  const sg = segDefaults();
  sg.at_ms = at;
  sg.label = `分段${list.length + 1}`;
  state.segments = [...list, sg].sort((a, b) => a.at_ms - b.at_ms);
  selectedSegment = state.segments.findIndex((s) => s.at_ms === at);
  setStatus(`已加分段 @ ${(at / 1000).toFixed(2)}s（三个角色默认继承全局 ② 的选择）`);
  renderSegments(); schedule();
}

function addSegmentAtPlayhead() {
  addSegment(audioToGrid(player.currentTime * 1000 || 0));
}

async function genSegmentsFromBridge() {
  try {
    const r = await api.bridgeSegments();
    if (!r || !r.ok) { setStatus(`⚠ 生成失败：${(r && r.error) || '未知'}`); return; }
    state.segments = (r.segments || []).map((s) => ({
      at_ms: s.at_ms, label: s.label,
      main: s.main === undefined ? null : s.main,
      sub: s.sub === undefined ? null : s.sub,
      dp: s.dp === undefined ? null : s.dp,
    }));
    state.segment_mode = r.mode || 'from';
    const sel = $('#sel-seg-mode'); if (sel) sel.value = state.segment_mode;
    selectedSegment = state.segments.length ? 0 : -1;
    renderSegments(); schedule();
    setStatus(`★ 从 BDG 角色轨生成 ${r.n} 条分段（共 ${r.n_points} 个角色点，`
      + `语义「${state.segment_mode === 'until' ? '到这点为止' : '从这点起'}」）`);
  } catch (e) {
    setStatus(`⚠ 生成失败：${e.message || e}`);
  }
}

function selectSegment(i) {
  selectedSegment = i;
  renderSegments();
}

// ---------------------------------------------------------- ③b 采bpm 区间（docs/47）
/** 骨架**砖长**（ms）：`60000 / (tbpm × N)`；tbpm 或 N 没填就返回 0。 */
function xkPeriodMs(n) {
  const tb = Number(state.xk_tbpm) || 0;
  const k = Number(n || state.xk_base) || 0;
  return (tb > 0 && k > 0) ? 60000 / (tb * k) : 0;
}

/** 格子号（**1 起算**）→ 毫秒：`t = φ + (k−1)·砖长`（φ = offset 那一根轴）。 */
function xkTileToMs(v) {
  const p = xkPeriodMs();
  if (!p) return null;
  return (Number(state.offset) || 0) + (Number(v) - 1) * p;
}

/** 毫秒 → 最近的格子号（1 起算）。 */
function xkMsToTile(v) {
  const p = xkPeriodMs();
  if (!p) return null;
  return Math.round((Number(v) - (Number(state.offset) || 0)) / p) + 1;
}

/** 在播放头附近加一段「只在这段采bpm」。 */
function addShowSegment() {
  // ★ 用**当前生成谱面的格子号**定义（与游戏里填 startTile/endTile 同一口径，1 起算）。
  //   没生成过谱面时先给 [1, 16]，等算出层数再夹回范围内。
  const n = (payload && payload.n_floors) ? payload.n_floors : 0;
  const segs = state.show_segments || [];
  const lo = segs.length ? Math.min((Number(segs[segs.length - 1].hi) || 1) + 1,
    Math.max(1, n || 9999)) : 1;
  const hi = n ? Math.min(n, lo + 15) : 16;
  state.show_segments = [...segs, { lo, hi, in_move: '', out_move: '' }];
  setStatus(`已加演出分段：起始方块 ${lo} → 结束方块 ${hi}`
    + (n ? `（本谱共 ${n} 层）` : '（还没生成谱面，层数未知）'));
  renderShowSegments(); schedule();
}

function renderShowSegments() {
  const box = $('#lst-show');
  if (!box) return;
  box.innerHTML = '';
  const segs = state.show_segments || [];
  const n = (payload && payload.n_floors) ? payload.n_floors : 0;
  const MOVE_IN = [['', '预设'], ['入A', '入A · 大'], ['入B', '入B · 干脆'], ['none', '无']];
  const MOVE_OUT = [['', '预设'], ['出A', '出A · 有力'], ['出B', '出B · 无痕'],
  ['出C', '出C · 含蓄'], ['出D', '出D · 炸'], ['none', '无']];
  if (!segs.length) {
    const d = el('div', 'hint');
    d.textContent = '（没有分段 ⇒ **整谱**都用 ⑤d 上面的预设入场 / 出场；'
      + '三连音段仍然自动标出、自动走 QE）';
    box.appendChild(d);
    return;
  }
  segs.forEach((sg, i) => {
    const row = el('div', 'region');
    const head = el('div', 'rhead');
    const title = el('span', 'rname');
    title.style.cssText = 'flex:1;color:#cfd8e3';
    const a = Number(sg.lo), b = Number(sg.hi);
    title.textContent = `分段 ${i + 1}：方块 ${a} → ${b}`
      + (n ? `（${Math.max(0, b - a + 1)} 格）` : '');
    const del = el('button', 'rdel'); del.textContent = '✕'; del.title = '删除该分段';
    del.onclick = () => {
      state.show_segments = segs.filter((_x, k) => k !== i);
      renderShowSegments(); schedule();
    };
    head.appendChild(title); head.appendChild(del);
    row.appendChild(head);

    // 起止：**填方块号**（1 起算，闭区间）—— 就是游戏里 startTile / endTile 的口径
    const trow = el('div', 'rtime');
    for (const side of ['lo', 'hi']) {
      const lab = el('span', 'hint');
      lab.textContent = side === 'lo' ? '起始方块' : '结束方块';
      const inp = document.createElement('input');
      inp.type = 'number'; inp.step = '1'; inp.min = '1';
      inp.id = `inp-show-${side}-${i}`;
      inp.value = String(Number(sg[side]) || 1);
      inp.style.width = '6.5em';
      inp.title = '当前生成谱面的格子号（1 起算，闭区间）';
      inp.onchange = () => {
        let v = Math.round(Number(inp.value) || 1);
        v = Math.max(1, n ? Math.min(n, v) : v);
        sg[side] = v;
        if (sg.lo > sg.hi) { if (side === 'lo') sg.hi = sg.lo; else sg.lo = sg.hi; }
        if (Number(inp.value) !== v) {
          setStatus(n ? `⚠ 方块号夹回 1~${n}（本谱共 ${n} 层）` : '')
        }
        renderShowSegments(); schedule();
      };
      trow.appendChild(lab); trow.appendChild(inp);
    }
    row.appendChild(trow);

    // 段内招选：留空 = 跟随预设
    const mrow = el('div', 'rtime');
    for (const [key, opts, label] of [['in_move', MOVE_IN, '入场'],
    ['out_move', MOVE_OUT, '出场']]) {
      const lab = el('span', 'hint'); lab.textContent = label;
      const sel = document.createElement('select');
      sel.id = `sel-show-${key}-${i}`;
      for (const [v, txt] of opts) {
        const o = document.createElement('option'); o.value = v; o.textContent = txt;
        sel.appendChild(o);
      }
      sel.value = sg[key] || '';
      sel.onchange = () => { sg[key] = sel.value; schedule(); };
      mrow.appendChild(lab); mrow.appendChild(sel);
    }
    row.appendChild(mrow);
    box.appendChild(row);
  });
}

function addXkRangeAtPlayhead() {
  const t = audioToGrid(player.currentTime * 1000 || 0);
  const total = payload ? payload.total_ms : t + 8000;
  const rg = {
    start_ms: Math.round(Math.max(0, t - 2000)),
    end_ms: Math.round(Math.min(total, t + 6000)),
    xk_base: Number(state.xk_base) || 4,
    tracks: [],
    label: `采bpm${(state.xk_ranges || []).length + 1}`,
  };
  state.xk_ranges = [...(state.xk_ranges || []), rg];
  setStatus(`已加采bpm 区间 ${(rg.start_ms / 1000).toFixed(2)}–${(rg.end_ms / 1000).toFixed(2)}s`
    + '（只有这段铺骨架，段外走原路径）');
  renderXkRanges(); schedule();
}

function renderXkRanges() {
  const box = $('#lst-xk');
  if (!box) return;
  box.innerHTML = '';
  const rs = state.xk_ranges || [];
  if (!rs.length) {
    const d = el('div', 'hint');
    d.textContent = '（没有区间 ⇒ 按「采bpm」选的 N **全曲**铺骨架；'
      + '在这儿框了区间就**只采区间**、区间外走原路径）';
    box.appendChild(d);
    return;
  }
  rs.forEach((rg, i) => {
    const row = el('div', 'region');
    // 标题行：名字 + 删除
    const head = el('div', 'rhead');
    const name = document.createElement('input');
    name.className = 'rname';
    name.value = rg.label || `采bpm${i + 1}`;
    name.onchange = () => { rg.label = name.value; };
    const del = el('button', 'rdel'); del.textContent = '✕'; del.title = '删除该区间';
    del.onclick = () => {
      state.xk_ranges = rs.filter((_x, k) => k !== i);
      renderXkRanges(); schedule();
    };
    head.appendChild(name); head.appendChild(del);
    row.appendChild(head);
    // 起止：**毫秒或格子号**（切换单位时用已知砖长精确换算，不猜）
    const trow = el('div', 'rtime');
    for (const side of ['start', 'end']) {
      const kMs = side + '_ms'; const kTk = side + '_tile';
      const isTile = rg[kTk] !== undefined && rg[kTk] !== null;
      const inp = document.createElement('input');
      inp.type = 'number';
      inp.step = isTile ? '1' : '100';
      inp.value = String(isTile ? rg[kTk] : Math.round(rg[kMs] ?? 0));
      inp.title = (side === 'start' ? '起' : '止') + '（毫秒或格子号，1 起算）';
      const unit = document.createElement('select');
      for (const [v, txt] of [['ms', '毫秒'], ['tile', '格']]) {
        const o = document.createElement('option'); o.value = v; o.textContent = txt;
        unit.appendChild(o);
      }
      unit.value = isTile ? 'tile' : 'ms';
      const commit = () => {
        const nv = Number(inp.value);
        const cap = (payload && payload.total_ms) ? payload.total_ms : 1e9;
        if (unit.value === 'tile') {
          const ms = xkTileToMs(nv);
          if (ms === null) { setStatus('⚠ 按格子填要先填 tbpm 与 N'); return; }
          if (ms > cap + 60000) {
            setStatus(`⚠ 第 ${Math.round(nv)} 格已超出曲子长度（${(cap / 1000).toFixed(1)}s）`
              + ' —— 先核对 tbpm / offset');
            return;
          }
          delete rg[kMs]; rg[kTk] = Math.round(nv);
        } else {
          delete rg[kTk]; rg[kMs] = Math.round(Math.min(Math.max(0, nv), cap));
        }
        const a = (rg.start_ms !== undefined) ? rg.start_ms : xkTileToMs(rg.start_tile);
        const b = (rg.end_ms !== undefined) ? rg.end_ms : xkTileToMs(rg.end_tile);
        if (a !== null && b !== null && b <= a) {
          setStatus('⚠ 采bpm 区间的**终点必须在起点之后**（否则后端会整组拒绝）');
        }
        // ★ 砖数预估：**当场拦住**（后端有硬上限，但别让它先白跑几十秒 —— 那会把
        //   整个 sidecar 卡住，表现就是「进度条和重新生成一起冻死」）
        const p = xkPeriodMs(rg.xk_base);
        if (p && a !== null && b !== null && b > a) {
          const cnt = Math.floor((b - a) / p) + 1;
          if (cnt > 20000) {
            setStatus(`⚠ 这段要铺 **${cnt} 块砖**（超过上限 20000，砖长 ${p.toFixed(3)}ms）`
              + ' —— 多半是 tbpm / N 填错，**已不生成**，请先核对');
            renderXkRanges();
            return;                              // **不 schedule**：别让后端白跑
          }
        }
        renderXkRanges(); schedule();
      };
      inp.onchange = commit;
      unit.onchange = () => {
        if (unit.value === 'tile') {
          const tk = xkMsToTile(Number(inp.value));
          if (tk === null) { setStatus('⚠ 先填 tbpm 与 N 才能按格子填'); unit.value = 'ms'; return; }
          inp.value = String(tk); inp.step = '1';
        } else {
          const ms = xkTileToMs(Number(inp.value));
          inp.value = String(Math.round(ms === null ? 0 : ms)); inp.step = '100';
        }
        commit();
      };
      trow.appendChild(inp); trow.appendChild(unit);
    }
    // 这一段的 N（每段可不同）
    const nsel = document.createElement('select');
    for (const n of [2, 4, 8]) {
      const o = document.createElement('option'); o.value = String(n); o.textContent = `${n}k`;
      nsel.appendChild(o);
    }
    nsel.value = String(rg.xk_base || state.xk_base || 4);
    nsel.title = '这一段的 base（每段可以不一样；混用会引入 SetSpeed，2 的幂可整除）';
    nsel.onchange = () => { rg.xk_base = Number(nsel.value); renderXkRanges(); schedule(); };
    trow.appendChild(nsel);
    // 这一段的**多押轨**（空 = 用全局「双押轨」那组）
    const tr = document.createElement('input');
    tr.type = 'text';
    tr.placeholder = '多押轨(空=全局)';
    tr.style.width = '104px';
    tr.value = (rg.tracks || []).join(',');
    tr.title = '这段的多押轨（轨号，逗号分隔；留空就用 ② 里勾的双押轨）';
    tr.onchange = () => {
      rg.tracks = tr.value.split(',')
        .map((s) => Number(s.trim()))
        .filter((v) => Number.isFinite(v) && v >= 0);
      schedule();
    };
    trow.appendChild(tr);
    row.appendChild(trow);
    box.appendChild(row);
  });
}

function renderSegments() {
  const box = $('#lst-segments');
  if (!box) return;
  const sel = $('#sel-seg-mode');
  if (sel) sel.value = state.segment_mode || 'from';
  box.innerHTML = '';
  const segs = state.segments || [];
  if (!segs.length) {
    const d = el('div', 'hint');
    d.textContent = '（没有分段：全曲按「② 音轨」的三个角色采。'
      + '加一条分段即可让角色随时间变；也可以从 BDG 角色轨一键生成）';
    box.appendChild(d);
    return;
  }
  const trs = (loadInfo && loadInfo.tracks) || [];
  const mode = state.segment_mode || 'from';
  // ★ 让用户看得见「这条规则实际管哪一段」（两种模式是镜像）
  const spanOf = (i) => {
    const prev = i > 0 ? segs[i - 1].at_ms : 0;
    const nx = i + 1 < segs.length ? segs[i + 1].at_ms : null;
    return mode === 'until' ? [prev, segs[i].at_ms] : [segs[i].at_ms, nx];
  };
  segs.forEach((sg, i) => {
    const [a, b] = spanOf(i);
    const row = el('div', 'region' + (i === selectedSegment ? ' sel' : ''));
    row.dataset.index = String(i);
    // 标题行
    const head = el('div', 'rhead');
    const name = document.createElement('input');
    name.className = 'rname';
    name.value = sg.label || `分段${i + 1}`;
    name.onchange = () => { sg.label = name.value; schedule(); };
    const del = el('button', 'rdel'); del.textContent = '✕'; del.title = '删除该分段';
    del.onclick = () => {
      state.segments = segs.filter((_x, k) => k !== i);
      if (selectedSegment === i) selectedSegment = -1;
      renderSegments(); schedule();
    };
    head.appendChild(name); head.appendChild(del);
    row.appendChild(head);
    // 时刻行
    const trow = el('div', 'rtime');
    const inp = document.createElement('input');
    inp.type = 'number'; inp.step = '10'; inp.value = String(Math.round(sg.at_ms));
    inp.title = '这一段的时刻（毫秒）';
    inp.onchange = () => {
      sg.at_ms = Math.max(0, Number(inp.value) || 0);
      state.segments = [...segs].sort((x, y) => x.at_ms - y.at_ms);
      renderSegments(); schedule();
    };
    trow.appendChild(inp);
    const bNow = el('button', 'mini'); bNow.textContent = '取播放头';
    bNow.onclick = () => {
      sg.at_ms = Math.round(audioToGrid(player.currentTime * 1000 || 0));
      state.segments = [...segs].sort((x, y) => x.at_ms - y.at_ms);
      renderSegments(); schedule();
    };
    trow.appendChild(bNow);
    const span = el('span', 'hint');
    span.textContent = b === null
      ? `→ 曲末（开区间）· ${(sg.at_ms / 1000).toFixed(2)}s 起`
      : `→ 管 ${(a / 1000).toFixed(2)}~${(b / 1000).toFixed(2)}s`;
    span.title = mode === 'until'
      ? '「到这点为止」：这条规则管到本段时刻结束'
      : '「从这点起」：这条规则从本段时刻起、到下一段为止';
    trow.appendChild(span);
    row.appendChild(trow);
    // 三个维度：继承 / 全关 / 逐轨
    for (const [dim, txt] of SEG_DIMS) {
      const crow = el('div', 'rrow');
      const lbl = el('span', 'hint'); lbl.textContent = `${txt}：`;
      crow.appendChild(lbl);
      const v = sg[dim];
      const bInh = el('div', 'chip' + (v === null || v === undefined ? ' on' : ''));
      bInh.textContent = '继承';
      bInh.title = '这一维沿用「② 音轨」里的全局选择（不是清空）';
      bInh.onclick = () => { sg[dim] = null; renderSegments(); schedule(); };
      const bOff = el('div', 'chip' + (Array.isArray(v) && v.length === 0 ? ' on' : ''));
      bOff.textContent = '全关';
      bOff.title = '这一段这一维一条都不采（与「继承」是两件事）';
      bOff.onclick = () => { sg[dim] = []; renderSegments(); schedule(); };
      crow.appendChild(bInh); crow.appendChild(bOff);
      for (const t of trs) {
        if (!t.has_notes) continue;
        const cur = Array.isArray(v) ? v : [];
        const c = el('div', 'chip' + (cur.includes(t.index) ? ' on' : '')
          + (t.drum ? ' drum' : ''));
        c.textContent = `trk${t.index}·${t.notes}`;
        c.title = t.drum ? '架子鼓轨' : (t.name || '');
        c.onclick = () => {
          const s = new Set(Array.isArray(v) ? v : []);
          if (s.has(t.index)) s.delete(t.index); else s.add(t.index);
          sg[dim] = [...s].sort((x, y) => x - y);
          renderSegments(); schedule();
        };
        crow.appendChild(c);
      }
      row.appendChild(crow);
    }
    row.onclick = (e) => {
      if (e.target.tagName === 'INPUT' || e.target.classList.contains('chip')
        || e.target.classList.contains('rdel')) return;
      selectSegment(i);
    };
    box.appendChild(row);
  });
}

// ---------------------------------------------------------------- 视图
let activeTab = 'chart';

function drawActive() {
  const v = views[activeTab];
  if (v) v.draw();
}

function setTab(name) {
  activeTab = name;
  for (const b of document.querySelectorAll('.tab')) b.classList.toggle('active', b.dataset.tab === name);
  // ★ 谱面预览这一页的容器叫 `cv-adofai`（不是 `cv-chart`），要单独映射，
  //   否则按 `cv-${name}` 匹配会把所有视图都关掉（页面全黑、容器 0×0）。
  const wantId = name === 'chart' ? 'cv-adofai' : `cv-${name}`;
  for (const c of document.querySelectorAll('.view')) c.classList.toggle('active', c.id === wantId);
  for (const b of document.querySelectorAll('.vbar')) b.classList.toggle('on', b.dataset.for === name);
  // ★ 谱面预览这一页就是内嵌的 ADOFAI 播放器（`cv-adofai` 自己就是 `.view`，
  //   上面那行 toggle 已经把它切对了，这里只需要管播放器的生命周期）
  if (name === 'chart') {
    ensurePreview();
    startPreviewClock();
  } else {
    pausePreview();
    stopPreviewClock();
  }
  if (name === 'falling') $('#tab-hint').textContent = '下落式跟着音频走（offset 已计入）';
  if (name === 'chart') $('#tab-hint').textContent = 'ADOFAI 官方渲染引擎（Re_ADOJAS）· 预览用';
  if (name === 'path') $('#tab-hint').textContent = '播放头按 entryTime 轴（不含 offset）';
  drawActive();
}

window.addEventListener('resize', () => {
  clampAllFloats();
  applyLayout();
  sizeViews();
});

// ------------------------------------------------------------ 加载/导出
async function doOpen() {
  const p = await window.dsh.openFile();
  if (p) doLoad(p);
}

async function doLoad(path, allowAudio) {
  // ★★ 2026-10 用户口径：「ogg2adofai 在正式版**隐藏**，假装不存在，也不能选择；
  //   逻辑层不要动一个字」。
  //   ⇒ 只在**界面层**把「从音频文件采音」这条入口拿掉（对话框不再收音频扩展名，
  //     按钮文案也不再提音频）。这里再兜一道：**万一**从别的途径（拖拽/命令行）
  //     递进来一个音频路径，就当它不是本版本支持的载入格式，**不进算法**。
  //   `sidecar/` 与 `core/` 一个字都没改 —— 那边照旧认音频，只是界面不给路。
  //   音频仍然可以当**预览音源**（①采音 → 预览音源 → 指定文件），那条路没动。
  //
  //   `allowAudio` 只给 **e2e / 排障**用（`__dsh.loadRaw`）：界面没有任何入口，
  //   但后端那条路要留着被测 —— 否则「逻辑层没动」就没有证据。
  if (!allowAudio && /\.(ogg|oga|wav|flac|mp3)$/i.test(String(path || ''))) {
    setStatus('⚠ 不是本版本支持的载入格式（支持：MIDI / BDG 工程 / 时间戳）');
    return;
  }
  showProgress(true);
  setProgress(0.02, '开始…');
  // ★★ `await` 之前就装好兜底：**加载一旦抛异常，进度条以前永远收不回来**
  //    （`showProgress(false)` 在 await 下面，抛了就跳过 ⇒ 界面挂着一个不动的进度条，
  //     看起来就是「进度条死了」）
  let r;
  try {
    r = await api.load(path);
  } catch (e) {
    showProgress(false);
    setStatus(`⚠ 加载失败：${(e && e.message) || e}`);
    return;
  }
  showProgress(false);
  if (!r.ok) {
    setStatus(`⚠ ${r.error || '加载失败'}`);
    return;
  }
  loadInfo = r;
  state.tracks_checked = r.default_tracks_checked || [];
  state.sub_checked = r.default_sub_checked || [];
  state.dp_checked = r.default_dp_checked || [];
  state.current_track = r.default_current_track || 0;
  state.song = r.name || '';
  state.offset = 0;
  // ★ 来源自带默认值时照它走（docs/45 §7）：
  //   毫秒时间戳 ⇒ merge_ms=0（30ms 会把密集处的点悄悄并掉）+ 直拟合 + 去噪开。
  if (typeof r.default_merge_ms === 'number') setControl('merge_ms', r.default_merge_ms);
  if (r.default_fit_mode) setControl('fit_mode', r.default_fit_mode);
  if (typeof r.default_denoise_on === 'boolean') setControl('denoise_on', r.default_denoise_on);
  lastAutoOffset = null;
  offsetTouched = false;         // ★ 换文件 ⇒ 「自动 offset」重新接管
  setControl('song', state.song);
  syncControls();
  player.pause(); player.removeAttribute('src'); player.load();
  $('#lbl-file').textContent = `${path}\n${r.stats || ''}`
    + (r.is_stem_json ? stemInfoText(r) : '')
    + (r.is_ts && !r.is_stem_json
      ? `\n来源：毫秒时间戳（${(r.ts || {}).n_kept || 0} 个点 · `
        + `${((r.ts || {}).ms_lo || 0).toFixed(0)}~${((r.ts || {}).ms_hi || 0).toFixed(0)}ms`
        + `${(r.ts || {}).n_skipped ? ` · 跳过 ${r.ts.n_skipped} 行` : ''}）`
        + ((r.ts_grid || {}).ok
          ? `\n网格：砖长 ${(r.ts_grid.period_ms || 0).toFixed(3)}ms`
            + `（bpm ${(r.ts_grid.bpm || 0).toFixed(3)}）· 相位 `
            + `${(r.ts_grid.phase_ms || 0).toFixed(3)}ms · 分母 1/${r.ts_grid.div || 4}`
          : `\n★ 网格没通过：${(r.ts_grid || {}).reason || '?'}（去噪会跳过）`)
      : '')
    + (r.is_bdg ? `\n来源：BDG 工程（${(r.bdg || {}).parser || '?'}）${(r.bdg || {}).report ? ' · ' + r.bdg.report : ''}`
      + ((r.bdg || {}).fit && r.bdg.fit.n_approx
        ? `\n★ 他的变速有 ${r.bdg.fit.n_approx} 处不在我们的合法档上（见 docs/38 §9.3）` : '')
      : '')
    + (r.grid_fit ? `\n网格：${r.grid_fit}` : '')
    + (r.detector_bias ? `\n检波偏置：${r.detector_bias.toFixed(2)}ms 已校正` : '');
  renderTracks(r);
  // ★ 顶栏来源徽标（docs/49）：MIDI / OGG / 时间戳 / BDG 一眼看出
  const sb = $('#src-badge');
  if (sb) {
    sb.textContent = r.is_stem_json
      ? `来源：时间戳 JSON（分轨 · ${((r.stem || {}).n_live || 0)} 路）`
      : r.is_bdg ? '来源：BDG 工程'
        : r.is_ts ? '来源：毫秒时间戳'
          : r.is_audio ? '来源：音频（OGG/WAV）'
            : r.is_midi === false ? '来源：?' : '来源：MIDI';
  }
  state.regions = [];            // 换文件后区间作废（时间是针对上一首的）
  selectedRegion = -1;
  state.segments = [];           // 分段同理（时刻也是针对上一首的）
  selectedSegment = -1;
  state.segment_mode = 'from';
  overview.clearFloor();         // 换文件后选中的格也作废
  renderRegions();
  renderSegments();
  await onTracksChanged();
  // ★★ 2026-10「开门一遍」：自动挑主轨时**排除**了「起点太晚」的轨（否则生成的谱
  //   会整段跳过前面有音乐的部分）—— 排除了谁，**说清楚**（不许静默）。
  const _late = r.pick_skipped_late || [];
  setStatus(r.is_stem_json
    ? `已加载时间戳 JSON（分轨）：${(r.tracks || []).length} 路音轨`
      + '　默认只勾了主旋律（其余的建议已标出，自己勾）'
    : r.is_bdg
      ? `已加载 BDG 工程：${(r.tracks || []).length} 条音轨（角色自己勾 —— 建议已标出）`
      : `已加载 ${(r.tracks || []).length} 轨`
        + (_late.length
          ? `　★ 自动主轨挑了**从开头起**的轨；跳过起点太晚的 ${_late.length} 条`
            + `（轨 ${_late.join(' / ')}）—— 要它们请手动勾上`
          : ''));
  refreshBridge();
  // ★★ 载入时的取舍账也上屏（`docs/56`：某路是空的 / 未排序 / 合并了多少 /
  //   原曲找不到 —— 都要**当场**看得见，不许等一次 rebuild 就没了）。
  const lw = r.warning_list || [];
  $('#warnings').textContent = lw.length ? ('⚠ ' + lw.join('；')) : '';
  $('#warn-badge').textContent = lw.length ? `（${lw.length} 条警告）` : '';
  if (lw.length) $('#more').open = true;
}

/** ★ 时间戳 JSON（DEMUCS 分轨 · `docs/56` §4.3）：文件信息里那张「每一路」小表。 */
function stemInfoText(r) {
  const st = r.stem || {};
  const rows = st.stems || [];
  const roleZh = { main: '主', sub: '次', dp: '双押', off: '关' };
  const nEmpty = rows.filter((x) => x.empty).length;
  let s = `\n来源：${st.src || '时间戳 JSON'}（${st.n_live || 0} 路 · `
    + `${st.n_points || 0} 个音头${nEmpty ? ` · ★ 空路 ${nEmpty}` : ''}）`
    + `\n  version ${st.version}${st.version_ok ? '' : '（★ 我们认 1，仍然解析了）'}`
    + ` · hop ${(st.hop_ms || 0).toFixed(4)}ms · ${st.sample_rate || 0}Hz`
    + ` · 时长 ${((st.duration_ms || 0) / 1000).toFixed(3)}s`
    + `\n  ${st.separation_model || '（无 separation_model）'}`
    + ` · has_vocals ${st.has_vocals === null || st.has_vocals === undefined
      ? '?' : (st.has_vocals ? '是' : '否')}`
    + ` · bpm_hint ${st.bpm_hint ? st.bpm_hint : '（无）'}`
    + `${st.main_key ? `　网格按「${st.main_key}」算` : ''}`;
  for (const x of rows) {
    const rg = x.role ? `　［建议：${roleZh[x.role] || x.role}］` : '';
    // ★ 取舍账一行说全（合并 / 丢弃 / 未排序 / sec-frame 偏差）
    const bits = [];
    if (x.n_dup) bits.push(`并 ${x.n_dup}`);
    if (x.n_bad) bits.push(`丢 ${x.n_bad}`);
    if (x.n_unsorted) bits.push('未排序已排');
    if (x.n_frame_mismatch) bits.push(`帧偏 ${x.n_frame_mismatch}`);
    s += `\n  · ${x.label}·${x.key}　${x.method || '（无 method）'}`
      + `${x.model ? ` ${x.model}` : ''}${x.source ? ` ← ${x.source}` : ''}`
      + `　${x.empty ? '［空 · 不建轨］'
        : `${x.n} 点 ${(x.ms_lo || 0).toFixed(0)}~${(x.ms_hi || 0).toFixed(0)}ms`}`
      + `${bits.length ? `　（${bits.join(' / ')}）` : ''}`
      + rg;
  }
  if ((r.ts_grid || {}).ok) {
    s += `\n网格：砖长 ${(r.ts_grid.period_ms || 0).toFixed(3)}ms`
      + `（bpm ${(r.ts_grid.bpm || 0).toFixed(3)}）· 相位 `
      + `${(r.ts_grid.phase_ms || 0).toFixed(3)}ms · 分母 1/${r.ts_grid.div || 4}`;
  } else if (r.ts_grid) {
    s += `\n★ 网格没通过：${r.ts_grid.reason || '?'}（去噪会跳过，直拟合仍可用）`;
  }
  s += `\n预览音源：${r.source_audio ? `已按 JSON 绑定 → ${r.source_audio}`
    : '（未绑定 —— 看上面的警告）'}`;
  return s;
}

async function doExport() {
  if (!hasChart) { setStatus('还没有生成谱面。'); return; }
  const dir = await window.dsh.openDir({ title: '选择导出目录（会新建一个曲目文件夹）' });
  if (!dir) return;
  setStatus('导出中…');
  const r = await api.exportTo(state, dir);
  if (!r.ok) { setStatus(`⚠ ${r.error || r.msg || '导出失败'}`); return; }
  setStatus(r.msg || `导出到 ${r.dir}`);
  $('#warnings').textContent = r.verify_ok === false ? `⚠ 校验：${r.verify}` : '';
}

// ---------------------------------------------------------------- 播放
function fmt(v) {
  const s = Math.max(0, v) / 1000;
  const m = Math.floor(s / 60);
  return `${m}:${(s - m * 60).toFixed(2).padStart(5, '0')}`;
}

function refreshTime() {
  const pos = player.currentTime * 1000 || 0;
  const dur = (player.duration || 0) * 1000;
  $('#lbl-time').textContent = `${fmt(pos)} / ${fmt(dur)}`;
  if (dur > 0) $('#slider').value = String(Math.round(pos / dur * 1000));
}

// ------------------------------------------------------- 时间轴换算（docs/24 §5）
// payload 里所有时刻都在**采音轴**（= onset 轴）。两个常量由 sidecar 给出：
//   <audio> 轴   = 采音轴 + LEAD      （LEAD = 交给播放器的那份音频补的静音）
//   谱面轴(tile) = 采音轴 − SHIFT     （SHIFT = audio_shift_ms = offset − LEAD）
// 谱面预览页用的是它自己的时钟（谱面轴），别的视图用 <audio> 时钟（音频轴）。
// ★★ 2026-10「预览音源」（用户：「允许（不强制）使用 ogg，并允许调节 ogg 偏移来
//   **音频混合预览**」）：
//     `payload.preview_audio` 非空 ⇒ 交出去的是**文件**（原曲 / 用户指定），
//     此时 `preview_audio_offset_ms`（Δ，正 = 原曲整体**延后**）生效：
//         `<audio>` 轴再整体推后 Δ ⇒ 采音轴 = <audio>轴 − (LEAD + Δ)
//     ⚠ **只动 `<audio>` 轴**：`谱面轴 ↔ 采音轴` 的 SHIFT 由 .adofai 的 offset 与
//       `S` 决定，跟 Δ 无关 ⇒ `axisShift()` **不加** Δ（加了就会把谱面整体挪走）。
//     ⚠ Δ **不写进谱面**（预览专用）：成品要也对上就改 ⑤ 的 offset / 自动 offset。
//     谱面预览页（内嵌播放器）不吃 axisLead，它走 `musicDelayMs`（见 ensurePreview）。
function oggMs() {
  return (payload && payload.preview_audio)
    ? (Number(state.preview_audio_offset_ms) || 0) : 0;
}
function axisLead() { return ((payload && payload.audio_lead_ms) || 0) + oggMs(); }
function axisShift() { return (payload && payload.audio_shift_ms) || 0; }
/** <audio> 的 currentTime(ms) → 采音轴（喂给 roll/path/falling/全曲条）。 */
function audioToGrid(ms) { return (ms || 0) - axisLead(); }
/** 采音轴 → <audio>.currentTime(ms)。 */
function gridToAudio(ms) { return (ms || 0) + axisLead(); }
/** 采音轴 → 谱面轴（喂给内嵌的 ADOFAI 播放器）。 */
function gridToChart(ms) { return (ms || 0) - axisShift(); }
/** 谱面轴 → 采音轴（把播放器的时钟换算回 payload 的轴）。 */
function chartToGrid(ms) { return (ms || 0) + axisShift(); }

let rafId = null;
let uiTimer = null;

/** 把音频时钟同步到其余视图 + 全曲预览 + 播放条（幂等，谁调都行）。 */
function syncFromAudio() {
  const grid = audioToGrid(player.currentTime * 1000 || 0);
  views.roll.setPlayhead(grid);
  views.path.setPlayhead(grid);
  views.falling.setTime(grid);
  overview.setPlayhead(grid);
  band.setPlayhead(grid);              // ★ 段带播放头
  drawActive();
  overview.draw();
  refreshXkHud(grid);
  refreshTime();
}

/** ③b 采bpm 与 ② 主轨的关系（`docs/47` §1 第 5 条 / §4 的互斥表）。
 *
 *  ★ 互斥是**按区间**的，不是全局：
 *    · **全曲采bpm**（选了 N 且没框区间）⇒ 主轨/次级轨**整条失效** ⇒ 置灰；
 *    · **只框了区间**（③b 有区间）⇒ 框架内主轨失效、**框外仍走主轨采音** ⇒ 不置灰，只提示；
 *    · 关 ⇒ 恢复原样。
 */
function applyXkLock() {
  const n = Number(state.xk_base) || 0;
  const nRanges = (state.xk_ranges || []).length;
  const whole = n > 0 && nRanges === 0;
  const partial = nRanges > 0;
  const t1 = $('#subhead-main');
  if (t1) {
    if (whole) {
      t1.textContent = '主轨（**采bpm 已接管全曲 ⇒ 本组不参与采音**）：勾了也不会被采';
    } else if (partial) {
      t1.textContent = '主轨（可多选）：**框架内**由采bpm 骨架取代，**框外仍按这里采**';
    } else {
      t1.textContent = '主轨（可多选）：勾哪几条就采哪几条，取并集';
    }
  }
  for (const id of ['#lst-tracks', '#lst-sub']) {
    const box = $(id);
    if (!box) continue;
    box.classList.toggle('xk-locked', whole);
    for (const inp of box.querySelectorAll('input')) inp.disabled = whole;
  }
}

/** ★★ 「使用固定双押角度」开着时：「薄角 θ」「偏移预算」**不参与选角** ⇒ 置灰 + 写明原因。
 *  用户 2026-10 定死：「双押不是按毫秒均匀计算的…**不能自定义角度**」。 */
function applyDpLock() {
  const on = state.use_fixed_dp_angle !== false;
  for (const k of ['dp_theta', 'dp_skew_max_ms']) {
    const f = (SCHEMA.fields || []).find((x) => x.key === k);
    if (f && f._input) {
      f._input.disabled = on;
      f._input.title = on
        ? '已开「使用固定双押角度」⇒ 角度由**规定写法表**定（≥840→90° / ≥300→30° / <300→15°），'
          + '本项**不参与选角**。要回去用老口径就把那个开关关掉'
        : (f.help || '');
    }
  }
  // ★ 反向：「三押」开关（拆 / 不拆 / 跳过）只在固定表下有意义 ——
  //   老口径没有押数概念（`use_fixed=False` 一律记 `three_legacy`）⇒ 那边置灰。
  const f3 = (SCHEMA.fields || []).find((x) => x.key === 'three_press_mode');
  if (f3 && f3._input) {
    f3._input.disabled = !on;
    f3._input.title = on
      ? (f3.help || '')
      : '关着「使用固定双押角度」⇒ 老口径**没有押数概念**（一律按双押拆），'
        + '本项不生效。要三押就把那个开关打开';
  }
}

/** ★★ 「使用激进的拟合策略」（`docs/57` · 用户 2026-10）：
 *   · 勾上 ⇒ 「最小角度」被**钉死成 15°** ⇒ 那个框置灰、显示 15（后端也强制）；
 *   · 它**只对直拟合有效** ⇒ 求解方式是「最优化」时整个开关置灰（免得点了没反应）；
 *   · 不勾 ⇒ 两个控件都恢复原样。 */
function applyFitLock() {
  const direct = String(state.fit_mode || '') === 'direct';
  const agg = !!state.aggressive_fit;
  const fa = (SCHEMA.fields || []).find((x) => x.key === 'aggressive_fit');
  if (fa && fa._input) {
    fa._input.disabled = !direct;
    fa._input.title = direct ? (fa.help || '')
      : '「使用激进的拟合策略」只对**直拟合**有效 —— 现在求解方式是「最优化」，'
        + '先把它切成「直拟合」再来开（切了它才管得着）';
  }
  const fm = (SCHEMA.fields || []).find((x) => x.key === 'travel_min');
  if (fm && fm._input) {
    if (agg && direct) {
      fm._input.disabled = true;
      fm._input.value = '15';
      fm._input.title = '已开「使用激进的拟合策略」⇒ 最小角度**钉死 15°**（15 30 45 60 …），'
        + '本项不参与；要自己调就把那个开关关掉';
    } else {
      fm._input.disabled = false;
      fm._input.title = (fm.help || '');
      fm._input.value = String(state.travel_min);
    }
  }
  // 容差：只在「去噪」或「激进拟合」有一条开着时才有意义
  const ft = (SCHEMA.fields || []).find((x) => x.key === 'fit_tol_ms');
  if (ft && ft._input) {
    const live = !!state.denoise_on || agg;
    ft._input.disabled = !live;
    ft._input.title = live ? (ft.help || '')
      : '「去噪」关着、也没开「激进拟合」⇒ 没有要修正的抖动，本项不参与';
  }
}

/** ★ 直拟合那一行的文案（`docs/57`）：把「15° 阶梯」的账摆在状态行里，不许只在报告里。 */
function fitLadderText(fr) {
  if (!fr || !fr.ladder_on) return '';
  return ` · ★ 15° 阶梯（容差 ${Math.round(fr.ladder_tol_ms || 0)}ms）：`
    + `修正 ${fr.n_ladder_moved || 0} 音（挪 中位 ${(fr.ladder_move_median_ms || 0).toFixed(2)}`
    + `/max ${(fr.ladder_move_max_ms || 0).toFixed(2)}ms）`
    + `· 挪不动原样 ${fr.n_ladder_raw || 0}`
    + (fr.n_ladder_bad ? ` · ⚠ 非阶梯格 ${fr.n_ladder_bad} 层`
      : ' · 非双押格角度全在 15° 整数倍上 ✔');
}

/** ③b 采bpm 的「格子 ↔ 毫秒」参考（docs/47 §3）——右上角那块。
 *  骨架是 `t = φ + (k−1)·砖长`（φ = offset），所以**格子号与毫秒双向都能读**。 */
function refreshXkHud(gridMs) {
  const b = $('#xk-hud');
  if (!b) return;
  if (!Number.isFinite(gridMs)) { b.hidden = true; return; }
  // ★ 常驻小窗：**没配置也要显示**（否则用户会说「没有默认展示的小窗口」）
  b.hidden = false;
  const p = xkPeriodMs();
  if (!p) {
    b.textContent = '采bpm 骨架：未配置\n'
      + '（③b 里选 N + 填 tbpm 后，这里显示 格子 ↔ 毫秒）';
    return;
  }
  const off = Number(state.offset) || 0;
  const raw = Math.floor((gridMs - off) / p) + 1;       // 1 起算的**物量号**
  // ★ 2026-10：播放头还在**前奏**（谱面第一格之前）时 `raw ≤ 0` —— 直接显示
  //   「第 -3 格」很唬人。这里夹到 1 并说明原因（格子号本来就是 1 起算的）。
  const k = Math.max(1, raw);
  const t0 = off + (k - 1) * p;
  const n = Number(state.xk_base) || 0;
  b.textContent = `采bpm 骨架 tbpm${Number(state.xk_tbpm) || 0}×${n}k\n`
    + `第 ${k} 格${raw < 1 ? '（播放头还在前奏，尚未进谱）' : ''} ↔ ${gridMs.toFixed(1)}ms\n`
    + `格长 ${p.toFixed(3)}ms　本格 ${t0.toFixed(1)}–${(t0 + p).toFixed(1)}ms`;
}

function tick() {
  syncFromAudio();
  rafId = player.paused ? null : requestAnimationFrame(tick);
}
/** 兜底时钟：窗口被遮挡 / rAF 不触发时，也保证播放头在动。 */
function startUiClock() {
  if (uiTimer) return;
  uiTimer = setInterval(() => { if (!player.paused) syncFromAudio(); }, 100);
}
function stopUiClock() {
  if (uiTimer) { clearInterval(uiTimer); uiTimer = null; }
}

/** 音频路径（记住上一次装的是哪一份，换音源时好判断要不要重挂 `<audio>`）。 */
let audioShown = null;

async function ensureAudio(refresh = false) {
  if (player.src && !refresh) return true;
  setStatus('准备音频…（无音源时用内置合成音色，可能几秒）');
  const r = await api.audio(state);
  if (!r.ok) { setStatus(`⚠ ${r.error || '没有可用音频'}`); return false; }
  const url = api.mediaUrl(r.path);
  if (player.src !== url) {
    player.src = url;
    // ★ 换音源要把位置归零：不归零的话 `<audio>` 会停在旧时间的同一刻度上，
    //   而新那份音频的长度/内容都不一样（实测会听到中间突然接上）。
    player.currentTime = 0;
    player.load();
  }
  audioShown = r.path;
  // 只显示文件名（长路径会把播放条顶变形），完整路径放 title
  const base = String(r.path).split(/[\\/]/).pop();
  const el2 = $('#audio-src');
  el2.textContent = `音频：${base}`;
  el2.title = r.path;
  setStatus('音频就绪');
  return true;
}

/** 等音频元数据就绪（设 currentTime 之前必须先有 duration，否则会被丢掉）。 */
function whenReady(timeoutMs = 2000) {
  if (player.readyState >= 1) return Promise.resolve();
  return new Promise((res) => {
    const done = () => { clearTimeout(tid); res(); };
    const tid = setTimeout(done, timeoutMs);
    player.addEventListener('loadedmetadata', done, { once: true });
  });
}

async function togglePlay() {
  // ★ 谱面预览页由内嵌的 ADOFAI 播放器接管播放/暂停（它自带音频与时钟）
  if (activeTab === 'chart' && preview) {
    if (preview.isPlaying) {
      preview.pause();
      $('#btn-play').textContent = '▶ 播放';
    } else if (!previewStarted) {
      // ★ 第7条修复：播放前确保打拍音已合成。建预览时 tileStartTimes 还没算出来，
      //   那时合成会提前 return；这里等引擎填好 tileStartTimes 再合一次兜底。
      const ph = preview && preview.player;
      if (ph && ph.hitsoundManager && !ph.hitsoundManager.isSynthesized()) {
        await waitTileStartTimes(ph, 1000);
        try { await ph.preSynthesizeHitsoundsWithProgress(); } catch (_e) {}
      }
      preview.startPlay(Math.max(0, preview.currentTimeMs));
      previewStarted = true;
      $('#btn-play').textContent = '⏸ 暂停';
    } else {
      preview.resume();
      $('#btn-play').textContent = '⏸ 暂停';
    }
    return;
  }
  if (!player.paused) {
    player.pause();
    $('#btn-play').textContent = '▶ 播放';
    return;
  }
  if (!loadInfo) { setStatus('先加载一个文件。'); return; }
  if (!hasChart) await rebuild();
  if (!await ensureAudio()) return;
  // ★ Re_ADOJAS `handlePlayWithSeek`：有选中的格就从**那一格**开始播
  //   （没选就从头开始）。它的做法是播完 selectTile 就 deselect；
  //   这里**保留**选中标记 —— 全曲条是常驻的，标记留着等于「从这里播」的锚点，
  //   播完就消失反而让人以为没选中（有意偏离，见 docs/21）。
  if (overview.selFloor !== null && overview.nFloors) {
    await whenReady();
    seek(overview.floorTime(overview.selFloor));
  }
  const p = player.play();
  if (p && p.catch) p.catch((e) => setStatus(`⚠ 播放失败：${e.message}`));
  $('#btn-play').textContent = '⏸ 暂停';
  views.falling.reset();
  if (!rafId) rafId = requestAnimationFrame(tick);
}

// -------------------------------------------------------- 格（tile）导航
/** 选中第 i 格并定位过去（对等 Re_ADOJAS 的 slider → seekTo + selectTile）。 */
function gotoFloor(i) {
  if (!overview.nFloors) return;
  overview.selectFloor(i);
  if (overview.selFloor === null) return;
  seek(overview.floorTime(overview.selFloor));
}

/** `←` / `→`：逐格（没选中时从播放头所在的格起步）。 */
function stepFloor(d) {
  if (!overview.nFloors) return;
  overview.stepFloor(d);
  if (overview.selFloor === null) return;
  seek(overview.floorTime(overview.selFloor));
}

function seek(ms) {
  const m = Math.max(0, ms);          // 采音轴（payload 的轴口径）
  // 谱面预览页优先驱动内嵌播放器（它有自己的音频源，吃**谱面轴**）
  if (activeTab === 'chart' && preview) {
    try { preview.seekTo(gridToChart(m), false); } catch (_e) { /* 未就绪时忽略 */ }
  }
  player.currentTime = gridToAudio(m) / 1000;
  refreshTime();
  // ★★ 2026-10：拖动/定位播放头后**小窗「格子 ↔ 毫秒」立刻跟上**。
  //   以前它只跟播放刷新 ⇒ 暂停时拖到哪儿、小窗都还停在旧格号（e2e 就是因此红的）。
  refreshXkHud(m);
  views.path.setPlayhead(m);
  views.roll.setPlayhead(m);
  views.falling.setTime(m);
  overview.setPlayhead(m);
  drawActive();
  overview.draw();
}

// ------------------------------------------------------------ 进度 / SSE
function showProgress(on) { $('#progress').classList.toggle('hidden', !on); }
function setProgress(frac, msg) {
  $('#pfill').style.width = `${Math.round(Math.max(0, Math.min(1, frac)) * 100)}%`;
  if (msg) $('#ptext').textContent = msg;
}

api.events((m) => {
  progressEvents += 1;
  if (m.kind === 'progress') setProgress(m.frac, m.msg);
  else if (m.kind === 'loaded') showProgress(false);
  // ★ 桥的状态（投射/收回/吸附偏移）走同一条 SSE，不用轮询
  else if (m.kind === 'bridge') refreshBridge();
  // ★ 「启动并桥接」的进度与结果（kind=host）也走它（docs/40）
  else if (m.kind === 'host') {
    const box = $('#br-hoststat');
    if (box && m.msg) box.textContent = `◐ ${m.msg}`;
    if (m.done) {
      const r = (m.result || {});
      if (r.ok) {
        setStatus(`★ 已启动并桥接（pid ${r.pid}${r.already ? ' · 桥本来就连着' : ''}）`);
        if (box) box.textContent = `● 已桥接（宿主 pid ${r.pid}）`;
      } else {
        setStatus('⚠ 启动并桥接失败：' + (r.error || '未知'));
        if (box) box.textContent = '⚠ ' + (r.error || '未知');
      }
      refreshHost();
      refreshBridge();
    }
  }
});

// ---------------------------------------------------------------- 绑定
function setStatus(s) { $('#status').textContent = s; syncReportHead(); }

/** ★ 增量改造（2026-09-19）：报告带**默认收起**，只剩标题栏一行 ——
 *  所以必须把「状态首句 + 警告条数」摘要上去，否则收起后等于失明。
 *  数据源就用现成的 #status 与 #chips（不新增状态，不碰后端契约）。 */
function syncReportHead() {
  const b = document.getElementById('rep-brief');
  if (!b) return;
  const st = String(($('#status') || {}).textContent || '').trim();
  const nWarn = document.querySelectorAll('#chips .chip.warn:not(.dead)').length;
  const t = st.length > 54 ? st.slice(0, 54) + '…' : (st || '（等待加载文件）');
  b.textContent = t + (nWarn ? ` · ⚠ 警告 ${nWarn}` : '');
}

/** 轻提示（布局存取 / 预设 / 段编辑都用它，不抢焦点、不打断） */
let toastTimer = null;
function toast(msg) {
  const t = $('#toast');
  if (!t) return;
  t.textContent = msg;
  t.classList.add('on');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('on'), 1900);
}

function bind() {
  $('#btn-rebuild').onclick = () => rebuild();
  $('#btn-export').onclick = () => doExport();
  $('#btn-about').onclick = () => showAbout();
  $('#btn-play').onclick = () => togglePlay();
  $('#m-close').onclick = () => $('#modal').classList.add('hidden');
  $('#pcancel').onclick = () => api.cancel();
  $('#vol').oninput = () => { player.volume = Number($('#vol').value) / 100; };
  // ★ 只在「提交」时 seek（等价旧 UI 的 sliderReleased），拖动中不 seek
  $('#slider').oninput = () => {
    const dur = (player.duration || 0) * 1000;
    if (dur > 0) $('#lbl-time').textContent = `${fmt(Number($('#slider').value) / 1000 * dur)} / ${fmt(dur)}`;
  };
  $('#slider').onchange = () => {
    const dur = (player.duration || 0) * 1000;
    if (dur > 0) {
      // 滑块是**音频轴**（0..音频总长），seek/全曲条是**采音轴** ⇒ 换算（docs/24 §5）
      const t = audioToGrid(Number($('#slider').value) / 1000 * dur);
      seek(t);
      overview.selectFloor(overview.floorAt(t));   // 底部播放条同样反查格号
    }
  };
  player.onplay = () => {
    $('#btn-play').textContent = '⏸ 暂停';
    if (!rafId) rafId = requestAnimationFrame(tick);
    startUiClock();
  };
  player.onpause = () => { $('#btn-play').textContent = '▶ 播放'; stopUiClock(); };
  player.onended = () => { $('#btn-play').textContent = '▶ 播放'; stopUiClock(); };
  player.onloadedmetadata = () => refreshTime();
  // ★ 权威时钟是 `timeupdate` + 100ms 定时器，不是 rAF：窗口被遮挡/后台时
  //   rAF 会被 Chromium 节流甚至停掉，只靠 rAF 会让播放头和滑块**冻住**
  //   （e2e 实测到的 bug；主进程同时关了 backgroundThrottling）。
  player.ontimeupdate = () => { if (!rafId && !uiTimer) syncFromAudio(); };
  player.onseeked = () => syncFromAudio();
  for (const b of document.querySelectorAll('.tab')) b.onclick = () => setTab(b.dataset.tab);

  window.dsh.onMenu((name) => {
    if (name === 'open') doOpen();
    else if (name === 'export') doExport();
    else if (name === 'rebuild') rebuild();
    else if (name === 'about') showAbout();
    else if (name.startsWith('tab:')) setTab(name.slice(4));
  });
  window.dsh.onSidecarDown((d) => setStatus(`⚠ Python sidecar 已退出（code=${d.code}）：看 app/.logs/sidecar.log`));

  // 键盘：Ctrl+O/S 由原生菜单管；空格/方向/Home/End 是**新增**（旧 UI 没有）
  // ★ 方向键语义照搬 Re_ADOJAS：**暂停时 = 逐格**（`←`/`→` 上下一格、
  //   Home/End 首尾格），播放时它整个 return 不抢；我们播放时退回原来的 ±5s，
  //   免得丢掉一个常用操作。Esc = 取消选中格。
  document.addEventListener('keydown', (e) => {
    const tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
    const playing = !player.paused;
    if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
    else if (e.code === 'ArrowRight') {
      e.preventDefault();
      if (playing) seek((player.currentTime + 5) * 1000); else stepFloor(1);
    } else if (e.code === 'ArrowLeft') {
      e.preventDefault();
      if (playing) seek(Math.max(0, player.currentTime - 5) * 1000); else stepFloor(-1);
    } else if (e.code === 'Home') {
      e.preventDefault();
      if (playing) seek(0); else gotoFloor(0);
    } else if (e.code === 'End') {
      e.preventDefault();
      if (playing) seek((player.duration || 0) * 1000);
      else gotoFloor(overview.nFloors - 1);
    } else if (e.code === 'Escape') {
      overview.clearFloor();
    }
  });
}

function showAbout() {
  $('#m-body').textContent = ABOUT;
  $('#modal').classList.remove('hidden');
}

const ABOUT = `快捷键：Ctrl+O 打开 / Ctrl+S 导出 / Ctrl+R 重新生成
         Ctrl+1..4 切页签 / F12 开发者工具
         空格 播放暂停
         暂停时 ←→ 逐格（上一格/下一格）/ Home End 首尾格 / Esc 取消选中
         播放时 ←→ 前后 5s / Home End 音频首尾
         播放键：有选中的格就从那一格开始播，否则从 0 开始

全曲预览条（导航照搬 Re_ADOJAS 编辑器 Timeline，单位为「格」不是毫秒）：
  点击 / 拖动 = 定位并选中该处的格；Shift+拖动 = 框选区间采音；
  滚轮 = 缩放；中键 = 平移；双击 = 恢复全曲

参数速查（与求解器字段一一对应，详见 docs/15、docs/18）：
  ② 主轨   可多选：勾哪几条就采哪几条，取并集（哪条有音采哪条），
            按「合并窗口」合并同时音。双押轨单独勾，不参与主轨并集。
  ③ 采音   合并窗口 / 合并取点 / 最小力度 / 最小间隔 / 最大音数 / 音高范围
  ④ 求解   自动基准BPM：勾上时「基准 BPM」只是**显示**回填值，真正基准由
                   solve() 内部再算一次，不要把它当输入
           直线优先 λ：少=3.0 / 平衡=1.5 / 多=0.7
           Twirl 阈值：仅「累积限角」模式生效
           Pause 阈值：间隔超过几拍才算长休止（默认 4.0）
           雪花：段长 < 起用 绝不用；到 100% 值线性升权
  ⑤ 时序   countdownTicks：开谱到首次按下 = 该拍数
           offset：谱面 t=0 对应的音频时刻；勾「自动」= 首个 onset
  视图     谱面预览 = ADOFAI 官方渲染引擎；路径跟随是独立开关
`;

// ---------------------------------------------------------------- 启动
// ------------------------------------------------------------ BDG 桥（docs/38）
// 「投射到编辑器」把我们的音点按角色写进 BDG；「收回改动」把他在编辑器里
// 改过的进度（删/挪/加）按毫秒拿回来，并**钉成采音结果**后重算。
let brState = null;

function brSetDot(cls, title) {
  const d = $('#br-dot');
  d.className = 'bdot' + (cls ? ' ' + cls : '');
  d.title = title || '';
}

async function refreshBridge() {
  const box = $('#br-status');
  let j = null;
  try { j = await api.bridge(false); } catch (_e) { j = null; }
  if (!j || !j.ok) {
    $('#br-url').textContent = '（sidecar 没应答）';
    brSetDot('', 'sidecar 没应答');
    box.textContent = '（sidecar 没应答）';
    box.className = 'bstat bad';
    return null;
  }
  brState = j;
  $('#br-url').textContent = j.url || '';
  const st = j.state || {};
  const on = !!st.connected && !!st.authed;
  brSetDot(on ? 'on' : '', on ? '已连接' : '未连接');
  const bits = [];
  bits.push(on
    ? `● 已连接${st.peer && st.peer.plugin ? '（插件 ' + st.peer.plugin + '）' : ''}`
    : '○ 未连接 —— 把上面的连接串贴进 BDG 面板（插件 → ADO 谱面桥）');
  if (st.projects) {
    bits.push(`已收到 ${st.projects} 次工程（${st.n_tracks} 轨 / ${st.n_points} 点）`);
  }
  if (st.sent_n) bits.push(`已投射 ${st.sent_n} 点（批次 ${st.run || '—'}）`);
  const ack = st.import_ack;
  let warn = false;
  // ★ 时序锚有没有被采用 + 有多少点走了 ms→beat 换算（`docs/41` #2）
  if (ack && ack.anchor) {
    bits.push(`★ 已采用我们的时序锚：bpm=${ack.anchor.baseBpm}`
      + ` offset=${ack.anchor.offsetMs}ms`
      + (ack.via_ms ? `（${ack.via_ms} 点按毫秒换算的拍位）` : ''));
  } else if (ack && ack.n_placed) {
    warn = true;
    bits.push('⚠ 这一批**没有带时序锚** ⇒ 编辑器还是它自己的 BPM，'
      + '小节线不会跟我们的谱一致');
  }
  if (ack && !ack.anchor && ack.n_tracks) {
    bits.push(`   （新建了 ${ack.n_tracks} 条内置踩点轨）`);
  }
  // ★ 分泳道（`docs/42`）：投了几条泳道 + 有几处「多轨同时响」
  if (ack && ack.n_lanes) {
    bits.push(`已投送 ${ack.n_lanes} 条泳道（按源轨分轨）`
      + (ack.n_cross_track
        ? `；★ ${ack.n_cross_track} 处是「多轨同时响」，归到了最早那条源轨的泳道`
        : ''));
  }
  if (ack && ack.n_off_grid) {
    warn = true;
    bits.push(`★ 投射有 ${ack.n_off_grid} 点被 BDG 吸附挪动，最大 ${ack.drift_max_ms}ms`
      + (ack.drift_over ? `（超预算 ${ack.drift_over}）` : '')
      + (ack.n_skipped ? `，另有 ${ack.n_skipped} 点被挤到同拍丢掉` : '')
      + '\n   ⇒ 请在 BDG 顶栏关掉「节拍网格」再投一次');
  }
  const d = st.diff;
  if (d && d.counts) {
    bits.push('收回对账：留下 ' + (d.counts.kept || 0) + ' · 移动 ' + (d.counts.moved || 0)
      + ' · 换角色 ' + (d.counts.role_changed || 0) + ' · 删除 ' + (d.counts.deleted || 0)
      + ' · 新增 ' + (d.n_added || 0));
  }
  if (st.last_warn) bits.push('⚠ ' + st.last_warn);
  box.textContent = bits.join('\n');
  box.className = 'bstat' + (warn ? ' warn' : (on ? '' : 'bad'));
  // ★ 收回的轨道项目（docs/45）：BDG 面板上那个按钮一按，这里要跟着出现
  await refreshBack();
  return j;
}

// ------------------------------------------ ★ 收回的轨道项目（docs/45）
let lastBackRun = null;

/** 看一眼 sidecar 上「从 BDG 收回的轨道」。首次出现/批次变了 ⇒ 刷新音轨列表并重算。 */
async function refreshBack() {
  const box = $('#back-box');
  let r = null;
  try { r = await api.bridgeBack(); } catch (_e) { r = null; }
  if (!box) return null;
  if (!r || !r.ok || !r.meta || !(r.meta.n_lanes)) {
    box.classList.add('hidden');
    return null;
  }
  const m = r.meta;
  const lines = [`⤴ 已收回 ${m.n_lanes} 条音轨 / ${m.n_points} 点`
    + `（我们这边合并成 ${m.n_onsets} 个 onset`
    + `${m.n_dp ? ' + 双押 ' + m.n_dp : ''}`
    + `${m.n_added ? ' · 新加 ' + m.n_added : ''}`
    + `${m.n_dup ? ' · 同刻合并 ' + m.n_dup : ''}）`];
  if (!m.has_file) lines.push('⚠ 还没加载 MIDI/OGG ⇒ 现在重建不了，先加载源文件');
  if ((m.anchor_mismatch_ms || 0) > 1.0) {
    lines.push(`⚠ 宿主那边改过 BPM/offset（锚差 ${m.anchor_mismatch_ms}ms）`);
  }
  for (const l of (m.lanes || []).slice(0, 8)) {
    lines.push(`　· ${(l.name || '?').slice(0, 22)}　${l.role}　${l.n} 点　`
      + `${(l.ms_lo || 0).toFixed(0)}~${(l.ms_hi || 0).toFixed(0)}ms`);
  }
  if ((m.lanes || []).length > 8) lines.push(`　… 还有 ${m.lanes.length - 8} 条`);
  lines.push('★ 现在「② 主轨」列表里的就是这些轨（音轨 = BDG 里带时值数据的轨）');
  box.textContent = lines.join('\n');
  box.classList.remove('hidden');
  if (r.info) {                    // 音轨列表 = 收回的那些轨
    loadInfo = Object.assign({}, loadInfo, r.info);
    renderTracks(loadInfo);
  }
  if (lastBackRun !== m.run) {     // 新批次 ⇒ 用收回的进度重算
    lastBackRun = m.run;
    await rebuild();
  }
  return r;
}

async function doBridgeBackUse() {
  if (!loadInfo) { setStatus('先加载文件（BDG 工程 / MIDI / 时间戳）。'); return; }
  const r = await api.bridgeBackApply(null);
  if (!r.ok) { setStatus('⚠ ' + (r.error || '没有可用的收回数据')); return; }
  lastBackRun = null;
  await refreshBack();
  setStatus('已用收回的轨道重建（音轨 = BDG 里带时值数据的那些轨）');
}

async function doBridgeBackClear() {
  const r = await api.bridgeBackClear();
  if (r.info && loadInfo) { loadInfo = Object.assign({}, loadInfo, r.info); renderTracks(loadInfo); }
  lastBackRun = null;
  $('#back-box').classList.add('hidden');
  await rebuild();
  setStatus('已清掉收回的轨道（回到从选轨采音）');
}

async function doBridgePush() {
  if (!loadInfo) { setStatus('先加载文件（BDG 工程 / MIDI / 时间戳）。'); return; }
  const st = (brState && brState.state) || {};
  if (!st.connected || !st.authed) {
    setStatus('⚠ BDG 桥没连上：先在 BDG 里打开插件面板并点连接。');
    await refreshBridge();
    return;
  }
  setStatus('投射中…');
  const r = await api.bridgeImport({ src: 'app' });
  if (!r.ok) { setStatus('⚠ 投射失败：' + (r.error || '')); await refreshBridge(); return; }
  setStatus(`已把 ${r.n} 点投射进编辑器（批次 ${r.run}）`);
  await refreshBridge();
}

async function doBridgeAdopt() {
  setStatus('收回中…');
  const r = await api.bridgeAdopt();
  if (!r.ok) { setStatus('⚠ 收回失败：' + (r.error || '')); await refreshBridge(); return; }
  const d = r.diff || {};
  const c = d.counts || {};
  const ad = r.adopted || {};
  setStatus(`收回 ${ad.n || 0} 点：留下 ${c.kept || 0} · 移动 ${c.moved || 0}`
    + ` · 换角色 ${c.role_changed || 0} · 删除 ${c.deleted || 0} · 新增 ${d.n_added || 0}`
    + ' —— 已按编辑器版本重算');
  await rebuild();               // ★ 用收回来的进度重算谱面（onsets_override 生效）
  await refreshBridge();
}

// ------------------------------------------ ★ 启动并桥接（宿主，docs/40）
let hostState = null;

/** 宿主（BDG）的状态行：装没装 / 在不在跑 / 调试端口通不通。 */
async function refreshHost() {
  const box = $('#br-hoststat');
  if (!box) return null;
  let j = null;
  try { j = await api.host(); } catch (_e) { j = null; }
  if (!j || !j.ok) {
    box.textContent = '（问不到宿主状态）';
    box.className = 'bstat bad';
    return null;
  }
  hostState = j;
  const bits = [];
  if (j.tool === false) {
    // ★★ 便携版：包里**没有**宿主联动工具（`tools/host.js` 没打进包，因为联动要
    //    npm / 仓库 / 600MB 下载 —— 拿到 zip 的人都没有）。
    //    这里必须说**用户能照做的话**，不许把 `npm run host:fetch` 甩出去：
    //    那是开发者提示，在便携版里是**死胡同**（真机踩过）。
    bits.push('○ 本便携版**不含** BDG 宿主（GPL-3.0 的独立软件，没有随包分发）');
    bits.push('   ⇒ 要用「编辑器联动」：自己装一份 Beat Data Generator，');
    bits.push('      把包内 `resources\\bridge_plugin` 放进它的插件目录，');
    bits.push('      再把上面那条连接串贴进「ADO 谱面桥」面板的地址栏');
    bits.push('   （不装也**不影响任何其它功能**：出谱、导出、预览都照常）');
  } else if (!j.present) {
    bits.push('○ BDG 宿主还没装');
    bits.push('   ⇒ 先 `npm run host:fetch`（走代理 127.0.0.1:7897，约 600MB）');
  } else if (!j.deps) {
    bits.push('○ BDG 宿主在，但依赖没装好');
    bits.push('   ⇒ 先 `npm run host:fetch`');
  } else if (j.starting) {
    bits.push('◐ 正在启动宿主…（进度见下）');
  } else if (j.pid_alive || j.cdp_up) {
    bits.push('● BDG 宿主在跑'
      + (j.pid ? `（pid ${j.pid}${j.pid_alive ? '' : ' · 已退出?'}）` : '（不是本程序起的）'));
    if (!j.cdp_up) bits.push(`   ⚠ 调试端口 ${j.port} 不通 ⇒ 没法自动推连接串，请手动贴`);
  } else {
    bits.push('○ BDG 宿主没在跑');
    bits.push('   ⇒ 点上面的「▶ 启动并桥接」');
  }
  box.textContent = bits.join('\n');
  box.className = 'bstat' + (j.tool === false ? ''
    : (j.present && j.deps && (j.pid_alive || j.cdp_up) ? '' : ' bad'));
  const b = $('#br-hoststop');
  if (b) b.disabled = !j.pid;
  const bs = $('#br-host');
  if (bs) {
    bs.disabled = j.tool === false;              // 便携版里这个按钮没有意义 ⇒ 直接禁用
    bs.title = j.tool === false
      ? '本便携版不含 BDG 宿主；装好宿主后请把连接串手动贴进插件面板'
      : '把 BDG 宿主拉起来，并把连接串推进它的插件面板（一键）';
  }
  return j;
}

async function doHostStart() {
  // ★ 便携版兜底：按钮已经被禁用，这里再拦一层 —— 不许把 `npm run host:fetch`
  //   这种**开发者话**甩给拿到 zip 的人（那是个死胡同）。
  if (hostState && hostState.tool === false) {
    setStatus('本便携版不含 BDG 宿主 —— 自己装好 BDG 后，'
      + '把包内 resources\\bridge_plugin 放进它的插件目录，再把连接串手动贴进面板');
    return;
  }
  const r = await api.hostStart();
  if (!r.ok) { setStatus('⚠ ' + (r.error || '启动失败')); await refreshHost(); return; }
  setStatus('正在启动 BDG 宿主并桥接…（进度见桥下面那行）');
  $('#br-hoststat').textContent = '◐ 正在启动宿主…';
  await refreshHost();
}

async function doHostStop() {
  const r = await api.hostStop();
  if (!r.ok) { setStatus('⚠ 停宿主失败：' + (r.error || '')); await refreshHost(); return; }
  setStatus(r.stopped ? '已停掉我们起的 BDG 宿主'
    : `（没停：${r.note || '这个宿主不是本程序起的'}）`);
  await refreshHost();
  await refreshBridge();
}

/** ★ 顶栏：预设 / 参数搜索 / 来源徽标（docs/49 §7） */
function bindTopbar() {
  const pre = $('#preset');
  if (pre) {
    pre.value = '默认';
    pre.onchange = () => { applyPreset(pre.value); pre.selectedIndex = 0; };
  }
  const ps = $('#psearch');
  if (ps) {
    ps.oninput = () => applyParamSearch(ps.value);
    // 支持鼠标点选/粘贴，Ctrl+P 聚焦（见 bind() 的快捷键）
    ps.addEventListener('keydown', (e) => { if (e.key === 'Escape') { ps.value = ''; applyParamSearch(''); } });
  }
}

function bindBridge() {
  // ★★ 2026-10 修：这里以前**漏了 `#br-push`** —— HTML 里有这个按钮、下面也有
  //   `doBridgePush()`，就是没人把两者接起来 ⇒「投射到编辑器」点了**毫无反应**
  //   （连一句提示都没有：`#status` 一个字都不变）。用户报的就是这个。
  //   根因是加「启动并桥接」那批按钮时顺手把它漏了，而且**没有任何测试会抓它** ——
  //   所以下面 e2e 里加了「桥面板每个按钮都必须有处理器」的守卫。
  $('#br-push').onclick = () => doBridgePush();
  $('#br-pull').onclick = () => doBridgeAdopt();
  $('#br-backuse').onclick = () => doBridgeBackUse();
  $('#br-backclear').onclick = () => doBridgeBackClear();
  $('#br-host').onclick = () => doHostStart();
  $('#br-hoststop').onclick = () => doHostStop();
  $('#br-copy').onclick = async () => {
    const u = (brState && brState.url) || $('#br-url').textContent || '';
    if (!u) return;
    try { await navigator.clipboard.writeText(u); setStatus('连接串已复制'); }
    catch (_e) { setStatus('复制失败，手动选中即可：' + u); }
  };
  refreshBridge();
  refreshHost();
}

async function main() {
  const h = await api.health();
  $('#health').classList.toggle('bad', !h.ok);
  // ★★ 环境自检（`docs/49` §10 第 5 项）——**打包版尤其要看这个**：
  //   · 用的是哪个解释器（包内自带 / 系统装的）—— 两者版本号可能一样，
  //     光看版本分不出来（真机验证时差点被骗）；
  //   · 音频采音那一支（librosa/scipy）在不在 —— 缺了**不许静默**：
  //     MIDI / .bdg 照常，但「从音频文件采音」不能用，必须当面说清。
  //   旧 sidecar 没有这些字段 ⇒ 当没看见，不报错。
  try {
    const exe = h.exe || '';
    const bundled = /[\\/]runtime[\\/]python[\\/]/i.test(exe);
    // ★ 2026-10：界面里不再有「从音频文件采音」这条入口（用户口径：假装不存在），
    //   所以这里也不再把音频能力摆到前台。`/api/health` 里照旧报（给排障用）。
    $('#health').title = `sidecar ${h.version} · Python ${h.py}`
      + (exe ? `\n解释器：${exe}${bundled ? '（包内自带）' : '（系统装的）'}` : '');
  } catch (_e) { /* 忽略：health 的形状由 sidecar 决定 */ }
  SCHEMA = await api.schema();
  if (!SCHEMA || !SCHEMA.fields) {
    setStatus('⚠ 拿不到参数 schema，sidecar 可能没起来');
    return;
  }
  // ★ 版本号：**后端给了就用后端的**（`GET /api/health` 的 version），
  //   这里只是"后端没给"时的兜底 —— 别再把它写死覆盖掉真值。
  SCHEMA.version = SCHEMA.version || '0.5.0-preview';
  state = Object.assign({}, SCHEMA.defaults);
  // ★★ 新 UI 的启动顺序（docs/49 方案 A）：
  //   ① 先记下 toast 函数（layout 也会用）
  //   ② buildPanel：把参数分派到 左「来源」/ 右「检查器」/ 大直线浮窗
  //   ③ bootLayout：装布局（真文件 ⇒ localStorage ⇒ 默认）→ 应用尺寸 → 绑拖拽/停靠/Shift+M
  //   ④ sizeViews：所有画布按新尺寸重画
  layout.setToast((m) => toast(m));
  buildPanel();
  // ★ 开机就把「依赖关系」的置灰摆对（不等第一次重算）：
  //   固定双押角度 → dp_theta/偏移预算/三押开关；激进拟合 → 最小角度 / 容差
  applyDpLock();
  applyFitLock();
  bind();
  await bootLayout({
    onChange: () => sizeViews(),
  });
  applyGroupOpenState();          // ★ 把上次记住的分组展开态回填（buildPanel 跑得更早）
  sizeViews();
  // ★★ 首启引导（只弹一次）：新人打开是**空的**，不知道从哪开始。
  //   不做成大遮罩（那要动布局），先给一句话 + 把「示例▾」的文案说全。
  try {
    if (!window.localStorage.getItem('adoc.seenIntro')) {
      window.localStorage.setItem('adoc.seenIntro', '1');
      setTimeout(() => toast(
        '第一次用？左栏「来源与段」里选「示例▾（内置曲子）」，'
        + '选一首就能直接出谱；想用自己的曲子就点「打开 MIDI / BDG 工程 / 时间戳…」'), 1500);
    }
  } catch (_e) { /* 隐私模式等：忽略，不许因此起不来 */ }
  bindBridge();
  bindTopbar();
  setTab('chart');
  const info = await window.dsh.info();
  $('#ver').textContent = `v${info.version} · py-sidecar :${info.port}`;
  setStatus(`就绪（Electron ${info.electron} · 端口 ${info.port}）`);
  // ★★ UI 体检（2026-10）：**每个 schema 字段都必须有控件** ——
  //   以前用写死的 key 列表渲染，新字段会静默消失（三押开关就是这么丢的）。
  //   现在起：缺控件就 toast + console.error（不许静默），e2e 也会断言。
  try {
    const aud = uiAudit();
    if (aud.missing.length) {
      console.error('[UI 体检] 这些 schema 字段没有控件：', aud.missing);
      toast(`⚠ UI 体检：${aud.missing.length} 个字段没渲染出来（${aud.missing.join(' / ')}）`
        + ' —— 这是 bug，请报给作者');
    } else {
      console.log(`[UI 体检] ${aud.n_fields} 个参数 + ${aud.n_view} 个视图参数全部有控件 ✔`);
    }
  } catch (e) { console.error('[UI 体检] 失败', e); }
  const sb = $('#src-badge');
  if (sb) sb.textContent = '未加载';
  window.dsh.ready({ port: info.port, fields: SCHEMA.fields.length });

  // 调试/自动化钩子（app/e2e.js 用它驱动一遍全链路，也方便手动在 DevTools 里点）
  window.__dsh = {
    get state() { return state; },
    schema: () => SCHEMA,
    setParam: (k, v) => setControl(k, v),
    load: doLoad,
    // ★ 仅供 **e2e / 排障**：直打后端口径（含音频采音）。界面里**没有**这条入口
    //   —— 用户口径「ogg2adofai 在正式版隐藏、假装不存在」，但逻辑层不许动，
    //   所以后端那条路要留一个能被测的把手（否则「没动」没有证据）。
    loadRaw: (p) => doLoad(p, true),
    // ★★ 2026-10：`rebuild()` 先**掐掉待触发的防抖**再算。
    //   否则 `setParam(...)`（排 140ms 防抖）+ 显式 `rebuild()` 会**赛跑**：
    //   求解现在可能要 1s+，排队的那一次会在显式那次之后落地，
    //   于是测试/DevTools 立刻读 `lastResult()` 读到的是**上一轮**的结果
    //   （e2e 实测：四押的 dp_extra_press 读到 0，而实际是 67）。
    rebuild: () => {
      if (rebuildTimer) { clearTimeout(rebuildTimer); rebuildTimer = null; }
      return rebuild();
    },
    export: doExport,
    tab: setTab,
    views,
    api,
    status: () => $('#status').textContent,
    // ★ 增量改造（2026-09-19）：报告带收起 / 工具条浮层 —— e2e 与 DevTools 可直接驱动
    reportCollapsed: () => { const e2 = $('#report'); return !!(e2 && e2.classList.contains('collapsed')); },
    toggleReport: () => { const h2 = $('#rep-head'); if (h2) h2.click(); return true; },
    repBrief: () => (($('#rep-brief') || {}).textContent || '').trim(),
    avSummary: () => (($('#btn-av-sum') || {}).textContent || '').trim(),
    toggleAvPop: () => { const b2 = $('#btn-av-sum'); if (b2) toggleAvPop(b2); return true; },
    // ★ 最近一次 rebuild 的整包结果（含 `dp.dp_three` / `dp.dp_extra_press`）
    lastResult: () => lastResult,
    timing: () => $('#timing').textContent,
    payload: () => payload,
    draw: drawActive,
    ready: () => !!SCHEMA && !!loadInfo,
    loadInfo: () => loadInfo,
    // 走「改勾选」的完整路径（含 pitch_lo/hi 自动改写 + 防抖重建），
    // 别直接写 state.tracks_checked —— 那样会留下过期的音高过滤把音滤掉。
    setTracks: (list) => { state.tracks_checked = list; return onTracksChanged(); },
    overview,
    // ★ 段带 + 布局（docs/49）：e2e / DevTools 可直接驱动
    band,
    layout,
    applyLayout,
    setLayoutMode: (on) => import('./layout.js').then((m) => m.setLayoutMode(on)),
    sizeViews,
    chips: () => [...document.querySelectorAll('#chips .chip')].map((c) => c.textContent.trim()),
    // ★★ UI 体检：哪些 schema 字段**没有控件**（空 = 全都在）。e2e 拿它当断言。
    uiAudit,
    // BDG 桥（docs/38）：e2e / DevTools 可直接驱动
    bridge: () => refreshBridge(),
    bridgePush: doBridgePush,
    // ★ 启动并桥接（docs/40）：e2e / DevTools 可驱动
    host: () => refreshHost(),
    // ★ 每次**重新问一遍**（返回 Promise）—— 以前返回缓存对象，调试时会看到
    //   「pid=0 但界面说已桥接 pid=1234」这种自相矛盾的快照。
    hostState: () => refreshHost(),
    hostStart: doHostStart,
    hostStop: doHostStop,
    bridgeAdopt: doBridgeAdopt,
    bridgeState: () => brState,
    // 播放/时间轴（e2e 用）
    play: togglePlay,
    seek,
    pos: () => player.currentTime * 1000,
    // ★ 轴换算（docs/24 §5）：`<audio>` 轴 ⇄ 采音轴（payload 的轴）
    toGrid: (ms) => audioToGrid(ms),
    toAudio: (ms) => gridToAudio(ms),
    axis: () => ({ lead: axisLead(), shift: axisShift() }),
    dur: () => (player.duration || 0) * 1000,
    sliderValue: () => Number($('#slider').value),
    timeLabel: () => $('#lbl-time').textContent,
    // 当前格：谱面预览在播就用播放器的格号；否则按 <audio> 位置在全曲条上反查
    curFloor: () => (preview && preview.isPlaying
      ? preview.currentTileIndex
      : (overview.floorAt(audioToGrid(player.currentTime * 1000 || 0)) || 0)),
    // ★ 格导航（Re_ADOJAS 语义；e2e 用）
    selFloor: () => overview.selFloor,
    nFloors: () => overview.nFloors,
    floorAt: (ms) => overview.floorAt(ms),
    floorTime: (i) => overview.floorTime(i),
    selectFloor: (i) => overview.selectFloor(i),
    stepFloor,
    gotoFloor,
    clearFloor: () => overview.clearFloor(),
    stats: () => ({ rebuildCount, hasChart, audioSrc: !!player.src, progressEvents }),
    // ★ 内嵌的 ADOFAI 播放器
    adofai: () => preview,
    adofaiErr: () => previewErr,
    adofaiState: () => (preview ? {
      tiles: preview.tileCount,
      dur: preview.totalDurationMs,
      t: preview.currentTimeMs,
      playing: preview.isPlaying,
      sel: preview.selectedTileIndex,
    } : null),
    // ★ 增量改造（2026-09-20）：预览框「判定条」（准度条 HitErrorMeter）是否已去掉。
    //   `killed` = 已 dispose 且字段置 null（正常）；`alive` = 还在（= 改造没生效）。
    hitErrorMeter: () => {
      const p2 = preview && preview.player;
      if (!p2) return 'no-preview';
      return p2.hitErrorMeter ? 'alive' : 'killed';
    },
    // ★ 同一件事的**手动把手**（e2e / DevTools）：对任意 preview 句柄再掐一次。
    //   存在的意义：DOM 兜底那段分支平时不会触发（引擎重建时会 `innerHTML=''` 清掉容器），
    //   只有显式调用才验得到 —— 否则那条分支是"写了但没人走过"的死代码。
    killMeter: (pv) => { killHitErrorMeter(pv || preview); return true; },
    // ★ 第7条（2026-09-21）：预览打拍音（hitsound）现状 + 手动把手（e2e / DevTools）。
    //   现状串形如 `Kick @25 / on / synth`；`setHitsound` 可运行期开关（buffer 不用重合成）。
    hitsound: () => hitsoundInfo(),
    setHitsound: (on) => {
      const p2 = preview && preview.player;
      if (!p2 || !p2.hitsoundManager) return false;
      p2.setHitsoundEnabled(!!on);
      return true;
    },
    synthHitsound: () => { synthesizeHitsounds(preview); return true; },
    // ★ 第8条（2026-09-21）：预览画面滞后量（ms）+ 真机微调用的把手。
    //   `visualLag()` 返回当前生效值；`setVisualLag(ms)` 改完下一帧生效（0 = 关掉）。
    visualLag: () => {
      const p2 = preview && preview.player;
      return p2 ? (Number(p2.__wbVisualLag) || 0) : 'no-preview';
    },
    setVisualLag: (ms) => {
      const p2 = preview && preview.player;
      if (!p2 || typeof p2.__wbSetVisualLag !== 'function') return false;
      return p2.__wbSetVisualLag(ms);
    },
    reloadPreview: () => { previewKey = null; return ensurePreview(); },
    // ★ 分段采音（docs/34 方案 C；e2e / DevTools 用）
    segAdd: addSegment,
    segList: () => (state.segments || []),
    segMode: (m) => {
      state.segment_mode = m || 'from';
      const sel = $('#sel-seg-mode'); if (sel) sel.value = state.segment_mode;
      renderSegments(); renderXkRanges(); return schedule();
    },
    segSet: (list) => {
      state.segments = list || [];
      renderSegments(); renderXkRanges(); return schedule();
    },
    segFromBridge: genSegmentsFromBridge,
    // ★ ⑤d 演出分段（docs/62）：e2e / 单测用的口子（填方块号，1 起算，闭区间）
    showSegAdd: (lo, hi, inMove, outMove) => {
      state.show_segments = [...(state.show_segments || []),
        { lo: Number(lo) || 1, hi: Number(hi) || 1,
          in_move: inMove || '', out_move: outMove || '' }];
      renderShowSegments(); return schedule();
    },
    showSegList: () => (state.show_segments || []),
    showSegClear: () => { state.show_segments = []; renderShowSegments(); return schedule(); },
  };
}

main().catch((e) => {
  setStatus(`⚠ 初始化失败：${e.message}`);
  if (window.dsh && window.dsh.reportError) window.dsh.reportError(String(e.stack || e));
});
