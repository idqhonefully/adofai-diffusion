/**
 * ADOFAI Studio · 工作台逻辑（自有 Mica 壳 · 重新排版移植）
 * ------------------------------------------------------------------
 * 业务派生全在 Python sidecar；本文件只做三件事：
 *   ① 按 schema 生成参数面板（检查器 + 左栏 + 视图工具条）
 *   ② 把拼装好的 state 发给 sidecar
 *   ③ 把返回的 payload 画到 5 个视图类 + 内嵌 ADOFAI 播放器 + 处理播放
 *
 * 预览不重造：黑盒复用 vendor/adofai-player.js（createPreview）+ views/*.js。
 * 状态机严格对齐Adofai-Chart-Generator前端（load 返回空 state、derive 推音高、140ms 防抖重建、
 * 程序写值不触发防抖、offset/auto_bpm 不回环）。
 */

import { PianoRoll } from './views/roll.js';
import { PathView } from './views/path.js';
import { FallingView } from './views/falling.js';
import { Overview } from './views/overview.js';
import { Band } from './views/band.js';
import { refreshTlPal } from './views/stagepal.js';

// ---------------------------------------------------------------- 工具
const $ = (s) => document.querySelector(s);
const el = (tag, cls) => { const d = document.createElement(tag); if (cls) d.className = cls; return d; };
const BASE = (window.dsh && window.dsh.base) || ('http://127.0.0.1:' + (window.location.port || '8766'));

async function req(path, opts = {}) {
  const r = await fetch(BASE + path, opts);
  let j = null;
  try { j = await r.json(); } catch (_e) { j = { ok: false, error: `HTTP ${r.status}（非 JSON）` }; }
  if (!r.ok && j && !j.error) j.error = `HTTP ${r.status}`;
  if (j) j.httpStatus = r.status;
  return j;
}
const api = {
  base: BASE,
  health: () => req('/api/health'),
  schema: () => req('/api/schema'),
  samples: () => req('/api/samples'),
  load: (path) => req('/api/load', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ path }) }),
  derive: (state) => req('/api/derive', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ state }) }),
  rebuild: (state) => req('/api/rebuild', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ state }) }),
  levelJson: (state) => req('/api/leveljson', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ state }) }),
  audio: (state) => req('/api/audio', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ state }) }),
  exportTo: (state, dir) => req('/api/export', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ state, dir }) }),
  cancel: () => req('/api/cancel', { method: 'POST', body: '{}' }),
  events: (onMsg) => {
    const es = new EventSource(BASE + '/api/events');
    es.onmessage = (ev) => { try { onMsg(JSON.parse(ev.data)); } catch (_e) { /* ignore */ } };
    es.onerror = () => { /* 自动重连 */ };
    return () => es.close();
  },
  mediaUrl: (p) => `${BASE}/media?path=${encodeURIComponent(p)}`,
  // 同源受控通道（只伺服本项目目录，带 Range）—— 目前用于**探测**工程自带原曲在不在
  mediaLocalUrl: (p) => `${BASE}/media_local?path=${encodeURIComponent(p)}`,
  // ───────── BDG 编辑器桥（sidecar 已提供全套端点） ─────────
  bridge: (full = false) => req(`/api/bridge${full ? '?full=1' : ''}`),
  bridgeImport: (body) => req('/api/bridge/import', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }),
  bridgeAdopt: () => req('/api/bridge/adopt', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }),
  bridgeBack: () => req('/api/bridge/back'),
  bridgeBackApply: (payload) => req('/api/bridge/back/apply', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ payload: payload || null }) }),
  bridgeBackClear: () => req('/api/bridge/back/clear', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }),
  host: () => req('/api/host'),
  hostStart: () => req('/api/host/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }),
  hostStop: () => req('/api/host/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }),
};

// ---------------------------------------------------------------- 状态
let SCHEMA = null;
let state = {};
let loadInfo = null;
let payload = null;
let applying = false;
let rebuildTimer = null;
let lastAutoOffset = null;
let hasChart = false;
let rebuildCount = 0;
let progressEvents = 0;
let lastSuggest = null;
let offsetTouched = false;
let selectedRegion = -1;
let selectedSegment = -1;
let lastResult = null;

// 预览
let preview = null, previewMod = null, previewKey = null, previewLoading = false, previewStarted = false, previewPaused = false;
// 预览是**自动加载**的。只有两种情况会停下并亮出提示条（底部一行小字）：
//   ① 环境起不了 WebGL —— 预检阶段就拦下，绝不调用 createPreview（那会同步卡死主线程）；
//   ② 加载真的失败/超时。此时 previewBlocked=true，不再自动重试，等用户点提示条上的「重试」。
let previewBlocked = false;
let previewClock = null;
let uiTimer = null, rafId = null;

let activeTab = 'chart';

// 播放器（全局 <audio>，给非谱面预览页用）
let player = null;
let lastPageError = null;
// 全局错误兜底：把页面里的运行时异常回传给宿主（写进 gui_debug.log），避免静默失败
window.addEventListener('error', (e) => {
  const frame = (() => { try { const st = String((e.error && e.error.stack) || ''); const m = st.split('\n')[1] || ''; return m.trim().slice(0, 160); } catch (_) { return ''; } })();
  lastPageError = 'error: ' + (e.message || (e.error && e.error.message) || '?') + ' @' + (e.lineno || 0) + (frame ? ' [' + frame + ']' : '');
  try { window.dsh && window.dsh.post && window.dsh.post({ type: 'page_error', msg: String(e.message || ''), src: String(e.filename || ''), line: e.lineno || 0, col: e.colno || 0, frame: frame }); } catch (_) {}
});
window.addEventListener('unhandledrejection', (e) => {
  const r = e.reason; const m = (r && (r.message || r.stack)) || String(r);
  lastPageError = 'unhandledrejection: ' + m;
  try { window.dsh && window.dsh.post && window.dsh.post({ type: 'page_error', msg: 'unhandledrejection: ' + m }); } catch (_) {}
});

// ---------------------------------------------------------------- 视图
const views = {
  roll: new PianoRoll($('#cv-roll')),
  path: new PathView($('#cv-path')),
  falling: new FallingView($('#cv-falling')),
};
views.roll.onSeek = (ms) => seek(ms);

const overview = new Overview($('#cv-overview'));
overview.onSeek = (ms) => seek(ms);
overview.onRegion = (t0, t1) => addRegion(t0, t1);
overview.onSelectRegion = (i) => selectRegion(i);
overview.onSelectFloor = (i) => { if (i === null || !preview) return; try { preview.selectTile(i, true); } catch (_e) { /* 未就绪 */ } };

const band = new Band($('#cv-band'), {
  onSeek: (ms) => seek(ms),
  ranges: () => bandRanges(),
  dp: () => bandDp(),
  onEditRanges: (ref, t0, t1) => bandEdit(ref, t0, t1),
  onSelectRange: (ref) => bandSelect(ref),
});

// ★ 主题切换重绘：明暗挂在 <html data-theme> 上（宿主切材质/明暗时换），
//   工作台与它同属一个 WebView2 文档根，所以这里监听到属性变化就去刷调色板 + 重画
//   时间轴两块画布（否则只剩容器底换了、canvas 还是老色 —— 就是「浅色下时间轴还是黑的」）。
//   ⚠ 别在 app.js 里直接调用主 gui 的 applyTheme（两套作用域不通）；用 MutationObserver 解耦最稳。
(function watchTheme() {
  let last = document.documentElement.getAttribute('data-theme') || 'dark';
  const mo = new MutationObserver(() => {
    const now = document.documentElement.getAttribute('data-theme') || 'dark';
    if (now === last) return;
    last = now;
    refreshTlPal();                 // 让 tlPal() 缓存重读 CSS 变量
    try { overview.draw(); } catch (_e) {}
    try { band.draw(); } catch (_e) {}
  });
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
})();

// ===================================================== 时间轴换算（docs/24 §5）
function oggMs() { return (payload && payload.preview_audio) ? (Number(state.preview_audio_offset_ms) || 0) : 0; }
function axisLead() { return ((payload && payload.audio_lead_ms) || 0) + oggMs(); }
function axisShift() { return (payload && payload.audio_shift_ms) || 0; }
function audioToGrid(ms) { return (ms || 0) - axisLead(); }
function gridToAudio(ms) { return (ms || 0) + axisLead(); }
function gridToChart(ms) { return (ms || 0) - axisShift(); }
function chartToGrid(ms) { return (ms || 0) + axisShift(); }

// ===================================================== 段带数据口
function bandRanges() {
  const out = { regions: [], xk: [], segments: [] };
  const dur = payload ? payload.total_ms : 0;
  (state.regions || []).forEach((rg, i) => {
    const t0 = Number(rg.start_ms) || 0;
    const t1 = (rg.end_ms === null || rg.end_ms === undefined) ? dur : Number(rg.end_ms);
    out.regions.push({ i, t0, t1, label: rg.label || `区间${i + 1}` });
  });
  (state.xk_ranges || []).forEach((rg, i) => {
    const t0 = Number(rg.start_ms) || 0;
    const t1 = (rg.end_ms === null || rg.end_ms === undefined) ? dur : Number(rg.end_ms);
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
function bandEdit(ref, t0, t1) {
  if (ref && ref.kind === 'region') { const r = state.regions[ref.i]; if (r) { r.start_ms = Math.round(t0); r.end_ms = Math.round(t1); } }
  else if (ref && ref.kind === 'xk') { const r = state.xk_ranges[ref.i]; if (r) { r.start_ms = Math.round(t0); r.end_ms = Math.round(t1); } }
  else if (ref && ref.kind === 'segment') { const r = state.segments[ref.i]; if (r) { r.at_ms = Math.round(t0); } }
  schedule();
}
function bandSelect(ref) { selectedRegion = ref ? ref.i : -1; }

// ===================================================== 检查器（schema 驱动）
const LEFT_GROUPS = new Set(['file', 'tracks']);
const HIDDEN_KEYS = new Set([]);   // 显式要藏的字段写这里并注明原因

function getVal(f) { return state[f.key]; }
function setVal(f, v) {
  state[f.key] = v;
  // chartview 那几个（音源 / 偏移）改动后，工具条上的摘要要跟着变
  if (f.group === 'chartview') { try { refreshAvSummary(); } catch (_e) {} }
  onFieldChanged(f);
}

function buildField(f) {
  if (f.type === 'info') {
    const d = el('div', 'info');
    d.id = `info-${f.key}`;
    // ★ 2026-09-19：有些 info 字段的正文在 `help` 里、`default` 是**空串**
    //   （例如 ③b 的 `xk_span_help`，"区间外 →" 那条）⇒ 只印 default 就是一条
    //   **空框**，看着像「这里少了内容」。回退顺序：default → 「标签 + help」。
    d.textContent = f.default || [f.label, f.help].filter(Boolean).join(' ');
    return d;
  }
  const row = el('div', 'field');
  if (f.type === 'float' || f.type === 'int') row.classList.add('wide');
  const lab = el('label');
  lab.textContent = f.label;
  lab.htmlFor = `in-${f.key}`;
  if (f.help) lab.title = f.help;
  row.appendChild(lab);

  const ctl = el('div', 'ctl');
  let input;
  if (f.type === 'check' || f.type === 'bool') {
    input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = !!getVal(f);
    input.addEventListener('change', () => setVal(f, input.checked));
  } else if (f.type === 'combo') {
    input = document.createElement('select');
    (f.options || []).forEach((o) => {
      const op = document.createElement('option');
      op.textContent = o.label;
      op.value = JSON.stringify(o.value);
      input.appendChild(op);
    });
    const cur = (f.options || []).findIndex((o) => JSON.stringify(o.value) === JSON.stringify(getVal(f)));
    input.selectedIndex = cur < 0 ? 0 : cur;
    input.addEventListener('change', () => {
      try { setVal(f, JSON.parse(input.value)); } catch (_e) { setVal(f, input.value); }
    });
  } else if (f.type === 'text') {
    input = document.createElement('input');
    input.type = 'text';
    input.value = String(getVal(f) ?? '');
    input.addEventListener('input', () => setVal(f, input.value));
  } else if (f.type === 'path') {
    input = document.createElement('input');
    input.type = 'text';
    input.readOnly = true;
    input.value = String(getVal(f) ?? '');
    input.title = String(getVal(f) ?? '');
    input.addEventListener('click', () => pickPath(f, input));
    const tail = el('button', 'mini');
    tail.textContent = '选…';
    tail.title = f.help || '选文件';
    tail.onclick = () => pickPath(f, input);
    ctl.appendChild(input); ctl.appendChild(tail);
  } else { // float / int / 兜底
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
  if (f.type !== 'path') ctl.appendChild(input);
  input.id = `in-${f.key}`;
  f._input = input;
  if (f.suffix) { const u = el('span', 'unit'); u.textContent = f.suffix; ctl.appendChild(u); }
  row.appendChild(ctl);
  return row;
}

async function pickPath(f, input) {
  const p = window.dsh.openAudio ? await window.dsh.openAudio() : await window.dsh.openFile();
  if (!p) return;
  applyPickedPath(f, input, p);
}

/** 挑完文件的落地动作（`pickPath` 与 e2e 钩子走**同一份**代码，别两边各写一遍）。 */
function applyPickedPath(f, input, p) {
  state[f.key] = p;
  if (input) { input.value = p; input.title = p; }
  if (f.group === 'chartview') {
    try { refreshAvSummary(); } catch (_e) {}
    // ★ 用户自己挑的这份音源要**记住**（下次进来直接就是它，见 restoreAvPick）
    saveAvPick();
  }
  if (f.schedule !== false) schedule();
}

/** 程序性写控件值（不触发防抖重建）。 */
function setControl(key, value) {
  const all = [...(SCHEMA.fields || []), ...(SCHEMA.view_fields || [])];
  const f = all.find((x) => x.key === key);
  if (!f || !f._input) { state[key] = value; refreshAvSummary(); return; }
  applying = true;
  try {
    if (f.type === 'check' || f.type === 'bool') f._input.checked = !!value;
    else if (f.type === 'combo') {
      const i = (f.options || []).findIndex((o) => JSON.stringify(o.value) === JSON.stringify(value));
      if (i >= 0) f._input.selectedIndex = i;
    } else f._input.value = String(value);
  } finally { applying = false; }
  state[key] = value;
  // 控件显示（自绘下拉的触发器 / 数字框 / 勾选框）跟着 select 走。见文件末尾那段接管说明。
  syncDd(f._input);
  if (key === 'aggressive_fit' || key === 'fit_mode' || key === 'denoise_on') applyFitLock();
  if (key === 'use_fixed_dp_angle' || key === 'three_press_mode') applyDpLock();
  if (f.group === 'chartview') { try { refreshAvSummary(); } catch (_e) {} }
}

function syncControls() {
  const all = [...(SCHEMA.fields || []), ...(SCHEMA.view_fields || [])];
  for (const f of all) {
    if (!f._input) continue;
    applying = true;
    try {
      if (f.type === 'check' || f.type === 'bool') f._input.checked = !!state[f.key];
      else if (f.type === 'combo') {
        const i = (f.options || []).findIndex((o) => JSON.stringify(o.value) === JSON.stringify(state[f.key]));
        if (i >= 0) f._input.selectedIndex = i;
      } else f._input.value = String(state[f.key]);
    } finally { applying = false; }
    syncDd(f._input);          // 自绘下拉的触发器跟着 select 走（见文件末尾那段接管说明）
  }
  try { refreshAvSummary(); } catch (_e) {}
}

function onFieldChanged(f) {
  if (applying) return;
  if (f.schedule) schedule();
  if (f.key === 'pfollow') { views.path.follow = !!state.pfollow; drawActive(); }
  if (f.key === 'use_fixed_dp_angle' || f.key === 'three_press_mode') applyDpLock();
  if (f.key === 'aggressive_fit' || f.key === 'fit_mode' || f.key === 'denoise_on') applyFitLock();
  if (f.key === 'offset') {
    offsetTouched = true;
    if (state.auto_offset) { setControl('auto_offset', false); setStatus('已改成手动 offset（顺手关掉自动 offset，免得两边打架）'); }
  }
  if (f.key === 'auto_offset' && state.auto_offset) offsetTouched = false;
  if (f.key === 'base_bpm') {
    if (state.auto_bpm) { setControl('auto_bpm', false); setStatus('已改成手动基准 BPM：顺手关掉「自动选基准 BPM」'); toast('已改成手动基准 BPM'); }
  }
  if (['lanes', 'division', 'fspeed', 'lanemode'].includes(f.key)) updateFalling();
  if (f.key === 'music_delay_ms' || f.key === 'preview_audio_offset_ms') {
    if (preview) preview.setMusicDelayMs((Number(state.music_delay_ms) || 0) + oggMs());
    if (f.key === 'preview_audio_offset_ms') { syncFromAudio(); }
  }
  if (f.key === 'preview_audio_mode' || f.key === 'preview_audio_path') {
    // ★ 记住这一份选择：下次进工作台直接就是它（见 restoreAvPick / saveAvPick）
    saveAvPick();
    ensureAudio(true);
  }
}

// 依赖置灰
function applyDpLock() {
  const on = state.use_fixed_dp_angle !== false;
  for (const k of ['dp_theta', 'dp_skew_max_ms']) {
    const f = (SCHEMA.fields || []).find((x) => x.key === k);
    if (f && f._input) { f._input.disabled = on; f._input.title = on ? '已开「固定双押角度」⇒ 角度由规定写法表定，本项不参与选角' : (f.help || ''); }
  }
  const f3 = (SCHEMA.fields || []).find((x) => x.key === 'three_press_mode');
  if (f3 && f3._input) { f3._input.disabled = !on; f3._input.title = on ? (f3.help || '') : '关着「固定双押角度」⇒ 老口径没有押数概念，本项不生效'; }
  // 上面直接写了 .disabled / .title，自绘下拉的触发器要跟着置灰与换提示（见文件末尾那段接管说明）。
  syncDdAll();
}
function applyFitLock() {
  const direct = String(state.fit_mode || '') === 'direct';
  const agg = !!state.aggressive_fit;
  const fa = (SCHEMA.fields || []).find((x) => x.key === 'aggressive_fit');
  if (fa && fa._input) { fa._input.disabled = !direct; fa._input.title = direct ? (fa.help || '') : '「激进拟合」只对直拟合有效，求解方式是「最优化」时整项置灰'; }
  const fm = (SCHEMA.fields || []).find((x) => x.key === 'travel_min');
  if (fm && fm._input) {
    if (agg && direct) { fm._input.disabled = true; fm._input.value = '15'; fm._input.title = '已开「激进拟合」⇒ 最小角度钉死 15°'; }
    else { fm._input.disabled = false; fm._input.title = (fm.help || ''); fm._input.value = String(state.travel_min); }
  }
  const ft = (SCHEMA.fields || []).find((x) => x.key === 'fit_tol_ms');
  if (ft && ft._input) { const live = !!state.denoise_on || agg; ft._input.disabled = !live; }
  // 同上：置灰/提示写的是原生控件，自绘下拉的触发器要跟着走。
  syncDdAll();
}
function applyXkLock() {
  const n = Number(state.xk_base) || 0;
  const nRanges = (state.xk_ranges || []).length;
  const whole = n > 0 && nRanges === 0;
  const partial = nRanges > 0;
  const t1 = $('#subhead-main');
  if (t1) {
    if (whole) t1.textContent = '主轨（采bpm 已接管全曲 ⇒ 本组不参与采音）：勾了也不会被采';
    else if (partial) t1.textContent = '主轨（可多选）：框架内由采bpm 骨架取代，框外仍按这里采';
    else t1.textContent = '主轨（可多选）：勾哪几条就采哪几条，取并集';
  }
  for (const id of ['#lst-tracks', '#lst-sub']) {
    const box = $(id); if (!box) continue;
    box.classList.toggle('xk-locked', whole);
    for (const inp of box.querySelectorAll('input')) inp.disabled = whole;
  }
}

// ===================================================== 面板拼装
function buildPanels() {
  const groupsL = $('#panel-left');
  const groupsR = $('#groups');
  groupsL.innerHTML = ''; groupsR.innerHTML = '';

  const byGroup = {};
  for (const f of (SCHEMA.fields || [])) (byGroup[f.group] ||= []).push(f);

  // ① 文件（左栏）
  // ★ 2026-09-21 主人裁定（schedule.txt 第 1 条）：工作台主界面**不放加载入口**。
  //   原先这里有两件东西 ——
  //     · 按钮「打开 MIDI / BDG 工程 / 时间戳…」
  //     · 下拉「示例▾（内置曲子，选一个就能出谱）」
  //   都已移除。加载改由**进工作台之前**决定：导入页侧边栏「工作台」→ 列历史
  //   MIDI → 选一条 → 顶部「前往工作台」；生成完也会自动带入（宿主
  //   PENDING_STUDIO_LOAD / _maybe_auto_load_studio 那条路）。
  //   手动兜底仍在：宿主菜单 open / Ctrl+O → doOpen()（函数保留，别删）。
  //   这里只留「当前到底载入了什么」的显示（#lbl-file）+ 自动贴合体检。
  {
    const sec = el('section', 'grp');
    const h = el('h4'); h.textContent = '① 文件'; sec.appendChild(h);
    const body = el('div', 'body');
    const info = buildField((byGroup.file || [])[0]);
    info.id = 'lbl-file'; body.appendChild(info);
    sec.appendChild(body); groupsL.appendChild(sec);
    sec.dataset.g = 'file'; bindCollapse(sec, false);
  }

  // ② 主轨（左栏）
  {
    const sec = el('section', 'grp');
    const h = el('h4'); h.textContent = '② 主轨（勾选要采音的轨，可多选）'; sec.appendChild(h);
    const body = el('div', 'body');
    for (const f of (byGroup.tracks || [])) {
      if (HIDDEN_KEYS.has(f.key)) continue;
      body.appendChild(buildField(f));
    }
    const t1 = el('div', 'subhead'); t1.id = 'subhead-main'; t1.textContent = '主轨（可多选）：勾哪几条就采哪几条，取并集';
    const list1 = el('div', 'list'); list1.id = 'lst-tracks';
    const t2 = el('div', 'subhead'); t2.textContent = '次级轨（只插空）：只在主轨的空白缝隙里补音';
    const list2 = el('div', 'list'); list2.id = 'lst-sub';
    const t3 = el('div', 'subhead'); t3.textContent = '双押轨（不参与主轨并集）';
    const list3 = el('div', 'list'); list3.id = 'lst-dp';
    body.appendChild(t1); body.appendChild(list1);
    body.appendChild(t2); body.appendChild(list2);
    body.appendChild(t3); body.appendChild(list3);
    const hint = el('div', 'info'); hint.id = 'lbl-track'; body.appendChild(hint);
    // 区间采音
    body.appendChild(segHeader('区间采音', '框选某段改用别的轨'));
    const rbar = el('div', 'field');
    const bAdd = el('button', 'mini'); bAdd.textContent = '＋ 加区间'; bAdd.onclick = () => addRegionAtPlayhead();
    const bClr = el('button', 'mini'); bClr.textContent = '清空'; bClr.onclick = () => { state.regions = []; selectedRegion = -1; renderRegions(); schedule(); };
    rbar.appendChild(bAdd); rbar.appendChild(bClr); body.appendChild(rbar);
    const rlist = el('div', 'regions'); rlist.id = 'lst-regions'; body.appendChild(rlist);
    // 分段采音
    body.appendChild(segHeader('分段采音', '让主/次/双押三角色随时间变'));
    const sbar = el('div', 'field');
    const sMode = document.createElement('select'); sMode.id = 'sel-seg-mode';
    for (const [v, txt] of [['from', '从这点起'], ['until', '到这点为止']]) { const o = document.createElement('option'); o.value = v; o.textContent = txt; sMode.appendChild(o); }
    sMode.onchange = () => { state.segment_mode = sMode.value; renderSegments(); schedule(); };
    sbar.appendChild(sMode);
    const bSAdd = el('button', 'mini'); bSAdd.textContent = '＋ 加分段'; bSAdd.onclick = () => addSegmentAtPlayhead();
    const bSClr = el('button', 'mini'); bSClr.textContent = '清空'; bSClr.onclick = () => { state.segments = []; selectedSegment = -1; renderSegments(); schedule(); };
    sbar.appendChild(bSAdd); sbar.appendChild(bSClr); body.appendChild(sbar);
    const slist = el('div', 'regions'); slist.id = 'lst-segments'; body.appendChild(slist);
    sec.appendChild(body); groupsL.appendChild(sec);
    sec.dataset.g = 'tracks'; bindCollapse(sec, false);
  }

  // ③ 采音 / ③b 采bpm / ④ 求解 / ④b 去噪 / ⑤ 导出（右栏检查器）
  // ★ 0.5.0 新组（⑤b 换手押 / ⑤c 算法调度 / ⑤d 演出）**必须登记在这里**，否则默认收起；
  //   原版把它们都设成展开（uiAudit 会检查字段是否真的渲染出来）。
  const openByDefault = { onset: false, xk: true, solve: false, fit: true, export: true,
    color: true, appear: true, show: true };
  for (const g of (SCHEMA.groups || [])) {
    const gid = g.id;
    if (LEFT_GROUPS.has(gid)) continue;
    if (!byGroup[gid] || !byGroup[gid].length) continue;
    const sec = el('section', 'grp');
    const h = el('h4'); h.textContent = g.title; sec.appendChild(h);
    const body = el('div', 'body');
    for (const f of byGroup[gid]) { if (HIDDEN_KEYS.has(f.key)) continue; body.appendChild(buildField(f)); }
    if (gid === 'fit') {
      const row = el('div', 'field');
      const hint = el('span', 'hint'); hint.id = 'lbl-fit';
      hint.textContent = '最优化 = 像人写的谱（模板/三连音/雪花），代价是量化会挪时序；直拟合 = 一砖一音、时序逐点精确。去噪还会告诉 BDG 那边怎么设分母。';
      row.appendChild(hint); body.appendChild(row);
    }
    // ★ ⑤d 演出（docs/62）：分段编辑器 —— 填「起始方块 / 结束方块」
    //   （与游戏里填 startTile/endTile 一个口径：**当前生成谱面的格子号**，1 起算，闭区间）
    if (gid === 'show') {
      const info = el('div', 'info'); info.id = 'lbl-show';
      info.textContent = '演出只写渲染事件（MoveTrack），绝不碰 angleData/bpm/travel/Twirl。'
        + '没分段、也不是三连音的格子 ⇒ 全程用上面两个预设招；'
        + '三连音段（求解侧打的段标签）会自动标出，默认整段换成反向 QE。';
      body.appendChild(info);
      const bar = el('div', 'field');
      const bAdd = el('button', 'mini'); bAdd.id = 'btn-show-add'; bAdd.textContent = '＋ 加分段';
      bAdd.title = '加一段：填起始方块 / 结束方块（当前生成谱面的格子号，1 起算），'
        + '段内可各自选入场 / 出场招';
      bAdd.onclick = () => addShowSegment();
      const bClr = el('button', 'mini'); bClr.id = 'btn-show-clear'; bClr.textContent = '清空';
      bClr.onclick = () => { state.show_segments = []; renderShowSegments(); schedule(); };
      bar.appendChild(bAdd); bar.appendChild(bClr);
      const sp = el('span', 'hint');
      sp.textContent = '留空 = 跟随全局预设；「无」= 这一侧不上。优先级：分段 > 三连音段 > 预设';
      bar.appendChild(sp);
      body.appendChild(bar);
      const slist = el('div', 'regions'); slist.id = 'lst-show';
      body.appendChild(slist);
    }
    if (gid === 'xk') {
      const xh = el('div', 'info'); xh.id = 'lbl-xk';
      xh.textContent = '采bpm = 大直线：把区间内硬铺成 N 砖/拍的等间隔骨架（无视音符排列 ⇒ 绝对对拍、绝对不好看，靠多押加内容）；区间外走原路径。';
      body.appendChild(xh);
      const xbar = el('div', 'field');
      const xAdd = el('button', 'mini'); xAdd.id = 'btn-xk-add';
      xAdd.textContent = '＋ 加区间（播放头）';
      xAdd.title = '在播放头附近框一段「只在这段采bpm」';
      xAdd.onclick = () => addXkRangeAtPlayhead();
      const xClr = el('button', 'mini'); xClr.id = 'btn-xk-clear';
      xClr.textContent = '清空';
      xClr.title = '删掉所有采bpm 区间（回到「全曲按 N 铺骨架」）';
      xClr.onclick = () => { state.xk_ranges = []; renderXkRanges(); schedule(); };
      xbar.appendChild(xAdd); xbar.appendChild(xClr);
      const xsp = el('span', 'hint');
      xsp.textContent = '起止可以按毫秒或格子号（1 起算）；每段可各自选 N 与多押轨';
      xbar.appendChild(xsp);
      body.appendChild(xbar);
      const xlist = el('div', 'regions'); xlist.id = 'lst-xk'; body.appendChild(xlist);
    }
    if (gid === 'export') {
      const row = el('div', 'field');
      const b = el('button', 'mini'); b.id = 'btn-apply-offset'; b.textContent = '应用建议 offset';
      b.onclick = () => {
        if (lastSuggest === null || lastSuggest === undefined) { setStatus('还没有谱面：先加载文件'); return; }
        setControl('offset', Math.round(lastSuggest)); rebuild(); setStatus(`偏移修正：offset 已设为 ${Math.round(lastSuggest)}ms（建议值）`);
      };
      row.appendChild(b); body.appendChild(row);
    }
    // ★ ⑤b 换手押上色（docs/59）：面板上只放两行短句（完整判据在字段 tooltip 里）
    if (gid === 'color') {
      const ch = el('div', 'info'); ch.id = 'lbl-color';
      ch.textContent = '换手押 = OOX 循环（≥2 周期）\n只有换手押染色，常规格不动';
      body.appendChild(ch);
    }
    // ★ ⑤c 算法轨道调度（docs/60）：同样只放两行短句
    if (gid === 'appear') {
      const ch = el('div', 'info'); ch.id = 'lbl-appear';
      ch.textContent = '驱动 = 谱面结构（图形段起点 + 密度）\n皮肤只改 settings，事件只写 actions';
      body.appendChild(ch);
    }
    sec.appendChild(body);
    // ★ 所有组（含 ③b 采bpm / 大直线）统一按 schema 顺序进检查器，不再单独开面板
    groupsR.appendChild(sec);
    sec.dataset.g = gid;
    const saved = groupOpenState()[gid];
    bindCollapse(sec, saved === undefined ? !openByDefault[gid] : !saved);
  }

  buildBridge();
  buildViewBars();
  renderRegions(); renderSegments(); renderXkRanges(); renderShowSegments();
}

function segHeader(title, sub) {
  const d = el('div', 'subhead');
  d.innerHTML = `<b style="color:#9fb0c0">${title}</b><span class="hint">（${sub}）</span>`;
  return d;
}

function bindCollapse(sec, collapsed) {
  const h = sec.querySelector('h4');
  if (!h || h.dataset.bound) return;
  h.dataset.bound = '1';
  // 折叠箭头：一个 chevron 靠**旋转**表达开合（Win11 的做法，Expander / NavigationView
  // 都是同一个字形转过来），不再换字符 —— 换字符会有"两个字跳一下"的断裂感。
  // 🔴 开合状态写在 data-open 上、**不读字形**：探针也读这一个属性，
  //    否则"看着是开的、程序认为是关的"这种两边对不上，永远查不出来。
  const caret = el('span', 'caret');
  caret.innerHTML = ShellUI.icon('chevron');
  const setCaret = (open) => { caret.dataset.open = open ? 'true' : 'false'; };
  h.prepend(caret);
  sec.classList.toggle('collapsed', !!collapsed);
  setCaret(!collapsed);
  h.ondblclick = () => {
    const now = !sec.classList.contains('collapsed');
    sec.classList.toggle('collapsed', now);
    setCaret(!now);
    setGroupOpen(sec.dataset.g || h.textContent.trim(), !now);
  };
}

// 播放键的字形只有两态（播放 / 暂停），单独收一个函数：
// 原先同一句话在 5 个地方各写了一遍（点按钮、音频 onplay / onpause / onended、
// 预览播放分支），加图标时漏一处就是"按下去图标不变"——收口之后不会漏。
function setPlayBtn(playing) {
  ShellUI.label($('#btn-play'), playing ? 'pause' : 'play', playing ? '暂停' : '播放');
}

function buildViewBars() {
  const renderGroup = (host, gid) => {
    host.innerHTML = '';
    for (const f of (SCHEMA.view_fields || [])) if (f.group === gid) host.appendChild(buildField(f));
  };
  const c = $('#vbar-chart'); c.innerHTML = '';
  // ★ 这一条只留**一个**控件：可点的「⚙ 音源 … · 偏移 … ms」药丸（点开 = 音源与偏移浮层）。
  //   原来这里有 6 样东西（引擎提示 + 偏移输入 + 按实测建议 + 正值提示 + 预览音源 + 原曲
  //   偏移 Δ），横排 ~900px —— 中栏稍窄就顶出横向滚动条，主人报「显示不全」。
  //   · 引擎提示删掉：上面 #tabs 那行已经在写「ADOFAI 官方渲染引擎 · 自动出图」。
  //   · 其余全进浮层（#av-pop），药丸上只报当前的音源/偏移值。
  const sum = el('button', 'vsum');
  sum.id = 'btn-av-sum';
  sum.title = '点开「音源与偏移」设置：预览音源 / 原曲文件 / 偏移修正 / 原曲偏移 Δ';
  sum.onclick = (e) => { e.stopPropagation(); toggleAvPop(sum); };
  c.appendChild(sum);
  renderGroup($('#vbar-falling'), 'fallingview');
  renderGroup($('#vbar-path'), 'pathview');
  const fit = el('button'); fit.textContent = '自适应'; fit.onclick = () => { views.path.fit(); drawActive(); };
  $('#vbar-path').appendChild(fit);
  // ★ 浮层**启动即建**（保持隐藏）：这样 chartview 那 4 个控件的 _input 从一开机就存在，
  //   setControl()/syncControls() 的回写行为与"控件原本就摆在工具条上"时完全一致。
  buildAvPop();
  refreshAvSummary();
}

// ------------------------------------------------- 音源与偏移浮层
// chartview 那 4 个字段（偏移修正 / 预览音源 / 原曲文件 / 原曲偏移 Δ）都建在这个浮层里。
// ⚠️ buildField() 会把控件登记到 f._input（setControl/syncControls 靠它回写），
//    所以这些控件**必须建且只建一次** —— 浮层只做显示/隐藏，不做重建。
let avPopBuilt = false;
function avPop() { return $('#av-pop'); }

function buildAvPop() {
  if (avPopBuilt) return avPop();
  const pop = el('div'); pop.id = 'av-pop'; pop.className = 'hidden';
  const head = el('div', 'avhead');
  const t = document.createElement('span'); t.textContent = '音源与偏移（仅预览）';
  head.appendChild(t);
  head.appendChild(el('span', 'grow'));
  const x = el('button', 'ghost mini'); x.innerHTML = ShellUI.icon('close'); x.title = '关闭';
  x.onclick = () => closeAvPop();
  head.appendChild(x);
  pop.appendChild(head);
  const body = el('div', 'avbody');
  const h1 = el('div', 'hint');
  h1.textContent = '偏移修正「正值 = 音乐相对游戏时间线延后」；原曲偏移 Δ 只作用于预览，'
                 + '两者都**不写进 .adofai**。';
  body.appendChild(h1);
  for (const f of (SCHEMA.view_fields || [])) if (f.group === 'chartview') body.appendChild(buildField(f));
  const row = el('div', 'row');
  const auto = el('button'); auto.id = 'btn-music-delay-auto'; auto.textContent = '按实测建议';
  auto.title = '用你在预览里手动打点攒出的样本，反推一个音乐延迟补偿值';
  auto.onclick = () => {
    if (!preview) { setStatus('谱面预览还没就绪'); return; }
    const v = preview.getSuggestedMusicDelayMs();
    if (v === null || v === undefined) { setStatus('播放器还没攒够按键样本（先在预览里手动打 5 次以上）'); return; }
    setControl('music_delay_ms', v); preview.setMusicDelayMs(v);
    setStatus(`偏移修正：已按实测建议设为 ${v}ms`);
  };
  row.appendChild(auto);
  const ah = el('span', 'hint'); ah.textContent = '先在预览里手动打 5 次以上'; row.appendChild(ah);
  body.appendChild(row);
  pop.appendChild(body);
  document.body.appendChild(pop);
  // 点浮层外面 / 按 Esc 关掉。
  // ★ 必须用**捕获阶段**：外壳桥（__dsh_bridge.js）在窗口最外 6px 上用捕获阶段拦
  //   mousedown 去做边缘缩放，并会 stopPropagation() —— 冒泡阶段注册的这里就收不到了
  //   （点在了边缘那一圈，浮层会赖着不关）。同节点同阶段的其它监听不受 stopPropagation
  //   影响，所以捕获阶段能收到。
  document.addEventListener('mousedown', (e) => {
    if (pop.classList.contains('hidden')) return;
    if (pop.contains(e.target)) return;
    if (e.target && e.target.closest && e.target.closest('#btn-av-sum, #av-pop')) return;
    closeAvPop();
  }, true);
  avPopBuilt = true;
  return pop;
}

/** 浮层的定位锚点：药丸亮着就用药丸；**药丸收起来了**（音源已就绪）就用预览框当锚，
 *  别让浮层飘到窗口左下角去。 */
function avAnchor() {
  const b = $('#btn-av-sum');
  if (b && !b.classList.contains('hidden') && b.offsetParent !== null) return b;
  return $('#stage') || $('#vbar-chart') || b;
}

function openAvPop(anchor) {
  const pop = buildAvPop();
  pop.classList.remove('hidden');
  const a = (anchor && anchor.offsetParent !== null) ? anchor : avAnchor();
  const r = a.getBoundingClientRect();
  const w = pop.offsetWidth || 340, h = pop.offsetHeight || 200;
  let left = Math.min(r.right - w, window.innerWidth - w - 8);
  left = Math.max(8, left);
  let bottom = window.innerHeight - r.top + 6;            // 默认浮在工具条上方
  if (bottom + h > window.innerHeight - 8) bottom = Math.max(8, window.innerHeight - h - 8);
  pop.style.left = Math.round(left) + 'px';
  pop.style.bottom = Math.round(bottom) + 'px';
  refreshAvSummary();
}
function closeAvPop() { const p = avPop(); if (p) p.classList.add('hidden'); }
function toggleAvPop(anchor) { const p = buildAvPop(); if (p.classList.contains('hidden')) openAvPop(anchor); else closeAvPop(); }

// ───────────────── 预览音源：记忆 + 就绪判定（2026-09-21 主人裁定）
// ① 「预览音源」要**记住用户上次选的那份**：下次进来直接就是选好的，音乐自动接上，
//    不用每次都「选…」一遍。
// ② 音源**就绪**（要的那份音频真接上了）时，工具条上那颗「⚙ 音源 … 」药丸**整个不显示**
//    —— 它下面那条 30px 的空条也一起收掉；**没就绪才亮出来**（提示 + 点开就能改）。
// ③ 一次都没选过（首次进来 / 换了台机器）时，若本工程自带原曲就**自动接上**：
//    落盘约定 = `<载入的 MIDI 所在目录>/job/input.wav`（audio-sep 流水线①ffmpeg 转出来
//    的那份整首歌）。**先探存在再绑** —— 绝不写一个假路径进去（那样后端会刷
//    「预览音源指定的文件不存在」警告，看着像坏了）。
const AVPICK_KEY = 'adoc.avpick';
let avPickFromStore = false;   // 启动时是否恢复了「用户上次的选择」⇒ 恢复过就尊重它，不自动接续
let avProbedDir = '';          // 探测过的工程目录（避免每次重算都探一遍）
let lastAudioSig = null;       // 已经挂到播放条 <audio> 上的音源指纹

function saveAvPick() {
  try {
    localStorage.setItem(AVPICK_KEY, JSON.stringify({
      mode: Number(state.preview_audio_mode || 0),
      path: String(state.preview_audio_path || ''),
    }));
  } catch (_e) { /* 隐私模式 / 配额：忽略，不影响使用 */ }
}

/** 启动时把上次的选择灌回 state（必须赶在 buildPanels() 之前，控件才对得上）。 */
function restoreAvPick() {
  let o = null;
  try { o = JSON.parse(localStorage.getItem(AVPICK_KEY) || 'null'); } catch (_e) { o = null; }
  if (!o || typeof o !== 'object') return false;
  const keys = new Set([...(SCHEMA.fields || []), ...(SCHEMA.view_fields || [])].map((f) => f.key));
  const m = Number(o.mode);
  if (!keys.has('preview_audio_mode') || !(m === 0 || m === 1 || m === 2)) return false;
  state.preview_audio_mode = m;
  if (keys.has('preview_audio_path') && typeof o.path === 'string') state.preview_audio_path = o.path;
  avPickFromStore = true;
  return true;
}

/** 两份路径是不是同一个文件（分隔符 / 大小写 / 尾斜杠都算同一个）。 */
function sameAudioPath(a, b) {
  const n = (s) => String(s || '').trim().replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
  const x = n(a);
  return !!x && x === n(b);
}

/** 预览音源**真的就绪**了吗（用户要的那份音频接上了）。
 *  · 自动 / 合成音 → 就绪（那是设计内行为，没什么可提醒的）。
 *  · 指定文件 → 路径非空**且**后端真把它解析出来了。`payload.preview_audio` 就是后端
 *    `preview_audio_file()` 的结果：文件不在时它是空串（那一轮退回合成音），一看便知。 */
function avAudioReady() {
  if (Number(state.preview_audio_mode || 0) !== 2) return true;
  const pick = String(state.preview_audio_path || '').trim();
  if (!pick) return false;
  return sameAudioPath((payload && payload.preview_audio) || '', pick);
}

/** 工具条那颗药丸：就绪 ⇒ 收掉（连那条空条一起）；没就绪 ⇒ 亮成警告样式。跟手刷新。 */
function refreshAvSummary() {
  const b = $('#btn-av-sum'); if (!b) return;
  const mode = Number(state.preview_audio_mode || 0);
  const modeTxt = ['自动', '合成音', '指定文件'][mode] || '自动';
  const off = Number(state.music_delay_ms || 0);
  const dlt = Number(state.preview_audio_offset_ms || 0);
  const ready = avAudioReady();
  let detail = `音源 <b>${modeTxt}</b> · 偏移 <b>${off} ms</b>`;
  if (mode === 2 && state.preview_audio_path) {
    const p = String(state.preview_audio_path).split(/[\\/]/).pop();
    detail += ` · <b>${p.length > 14 ? p.slice(0, 14) + '…' : p}</b>`;
  }
  if (dlt) detail += ` · Δ <b>${dlt} ms</b>`;
  // 药丸前缀：就绪时是齿轮、未就绪时是警告三角 —— **形状本身**就说出了状态，
  // 不必只靠颜色（强光 / 色觉差异下，颜色是最不可靠的那一维）。
  b.innerHTML = (ready ? ShellUI.icon('settings', 'ico-gap') : ShellUI.icon('warning', 'ico-gap'))
              + (ready ? '' : '音源未就绪 · ') + detail;
  b.title = `音源：${modeTxt} · 偏移修正 ${off} ms` + (dlt ? ` · 原曲偏移 Δ ${dlt} ms` : '')
    + (ready ? '' : '\n\n⚠ 要的那份音频没接上（没挑文件 / 文件不在了）—— 预览现在放的是合成节拍音。点开挑一份。')
    + '\n点开可改：预览音源 / 原曲文件 / 偏移修正 / 原曲偏移 Δ（快捷键 Ctrl+Shift+A）';
  b.classList.toggle('hidden', ready);
  b.classList.toggle('warn', !ready);
  const bar = $('#vbar-chart');
  if (bar) bar.classList.toggle('empty', ready);   // 空条也收掉，别留一条光板
}

/** 一进来（还没记住用户的选择）就把本工程自带的那份原曲接成预览音源。
 *  **先探存在**（同源 /media_local 只伺服本项目目录，HEAD 一下就知道在不在）。 */
async function bindProjectAudio() {
  if (avPickFromStore) return false;                                  // 用户选过 ⇒ 尊重他的选择
  if (String(state.preview_audio_path || '').trim()) return false;
  if (Number(state.preview_audio_mode || 0) === 1) return false;      // 明确要合成音 ⇒ 别多事
  // 直接载入音频 / BDG 工程时，引擎自己就绑了原曲（`source_audio`，自动档直接用）⇒ 不动它
  if (loadInfo && (loadInfo.is_audio || loadInfo.is_bdg)) return false;
  const mid = String((loadInfo && loadInfo.path) || '');
  const m = /^(.*)[\\/][^\\/]+$/.exec(mid);
  const dir = m ? m[1] : '';
  if (!dir || dir === avProbedDir) return false;
  avProbedDir = dir;
  const cand = dir + '\\job\\input.wav';
  let ok = false;
  try { ok = !!(await fetch(api.mediaLocalUrl(cand), { method: 'HEAD' })).ok; }
  catch (_e) { ok = false; }
  if (!ok) return false;
  state.preview_audio_mode = 2;
  state.preview_audio_path = cand;
  toast('音源：已自动接上本工程的原曲（job/input.wav）');
  return true;
}

/** 把「后端这一轮解析出来的那份音频」挂到播放条那个 <audio> 上（另外三个视图用它）。
 *  ⚠ 只在**音源真的变了**的时候挂：`ensureAudio(true)` 会把 currentTime 拽回 0，
 *    而改任何参数都会触发重算 ⇒ 不加这道闸，播放头会被反复弹回开头。 */
async function syncAudioToSource() {
  if (!player) return;
  const want = String((payload && payload.preview_audio) || '');
  const sig = want ? ('file:' + want) : 'synth';
  if (sig === lastAudioSig) return;
  const had = lastAudioSig;
  lastAudioSig = sig;
  if (!want) {
    // 退回合成音：**不急着渲染**（整首歌的节拍音要几秒 CPU），只把上一份文件摘掉、
    // 把话说清楚 —— 等用户真按播放，`togglePlay → ensureAudio` 再生成。
    if (had) {
      try { player.pause(); player.removeAttribute('src'); player.load(); } catch (_e) {}
      const a = $('#audio-src');
      if (a) { a.textContent = '音频：合成节拍音（播放时生成）'; a.title = ''; }
    }
    return;
  }
  await ensureAudio(true, true);          // 指定文件 / 原曲：只查一次路径，很便宜，别抢状态行
}

// 分组展开态记忆（轻量：用 localStorage）
function groupOpenState() { try { return JSON.parse(localStorage.getItem('adoc.groupOpen') || '{}'); } catch (_e) { return {}; } }
function setGroupOpen(g, open) { const s = groupOpenState(); s[g] = open; try { localStorage.setItem('adoc.groupOpen', JSON.stringify(s)); } catch (_e) {} }

// ===================================================== 音轨列表
function renderTracks(info) {
  const l1 = $('#lst-tracks'), l2 = $('#lst-sub'), l3 = $('#lst-dp');
  l1.innerHTML = ''; l2.innerHTML = ''; l3.innerHTML = '';
  const _mk = (host, key, listKey) => {
    for (const t of listKey) {
      const lab = el('label');
      const cb = document.createElement('input'); cb.type = 'checkbox';
      cb.checked = state[key].includes(t.index);
      if (!t.has_notes) cb.disabled = true;
      cb.onchange = () => {
        const s = new Set(state[key]);
        if (cb.checked) s.add(t.index); else s.delete(t.index);
        state[key] = [...s].sort((a, b) => a - b);
        if (key === 'tracks_checked') state.current_track = t.index;
        syncTrackSel(); onTracksChanged();
      };
      const txt = el('span', 't' + (t.drum ? ' drum' : ''));
      const tag = t.suggest ? ({ main: '主', sub: '次', dp: '双押', off: '关' }[t.suggest] || t.suggest) : '';
      txt.textContent = (t.summary || t.label) + (tag ? `　［建议：${tag}］` : '') + (t.note ? `　（${t.note}）` : '');
      lab.appendChild(cb); lab.appendChild(txt);
      lab.dataset.index = String(t.index);
      if (key === 'tracks_checked') {
        lab.onclick = (e) => { if (e.target === cb) return; state.current_track = t.index; syncTrackSel(); onTracksChanged(); };
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
  for (const lab of document.querySelectorAll('#lst-tracks label')) lab.classList.toggle('sel', Number(lab.dataset.index) === state.current_track);
}

// ===================================================== 区间/分段/采bpm
function addRegionAtPlayhead() { const t = audioToGrid(player.currentTime * 1000 || 0); const total = payload ? payload.total_ms : t + 8000; addRegion(Math.max(0, t - 2000), Math.min(total, t + 6000)); }
function addRegion(t0, t1) {
  if (!loadInfo) return;
  const tracks = state.tracks_checked.length ? [...state.tracks_checked] : (loadInfo.tracks || []).filter((t) => t.has_notes).map((t) => t.index);
  if (!tracks.length) { setStatus('⚠ 这条曲目没有可采音的音轨'); return; }
  const lo = Math.max(0, Math.min(t0, t1)), hi = Math.min(payload ? payload.total_ms : 1e9, Math.max(t0, t1));
  state.regions = [...state.regions, { start_ms: Math.round(lo), end_ms: Math.round(hi), tracks, label: `区间${state.regions.length + 1}` }];
  selectedRegion = state.regions.length - 1;
  setStatus(`已框选 ${(lo / 1000).toFixed(2)}–${(hi / 1000).toFixed(2)}s（用 trk${tracks.join(',trk')} 采音）`);
  renderRegions(); schedule();
}
function renderRegions() {
  const box = $('#lst-regions'); if (!box) return; box.innerHTML = '';
  (state.regions || []).forEach((rg, i) => { const d = el('div', 'reg'); d.innerHTML = `<span>${rg.label}</span><span>${(rg.start_ms / 1000).toFixed(2)}–${(rg.end_ms / 1000).toFixed(2)}s</span>`; box.appendChild(d); });
}
function addSegmentAtPlayhead() { const t = audioToGrid(player.currentTime * 1000 || 0); state.segments = [...state.segments, { at_ms: Math.round(t), label: `分段${state.segments.length + 1}` }]; selectedSegment = state.segments.length - 1; renderSegments(); schedule(); }
function renderSegments() {
  const box = $('#lst-segments'); if (!box) return; box.innerHTML = '';
  (state.segments || []).forEach((sg, i) => { const d = el('div', 'reg'); d.innerHTML = `<span>${sg.label}</span><span>${(sg.at_ms / 1000).toFixed(2)}s</span>`; box.appendChild(d); });
}
// ------------------------------------------------- ③b 采bpm 区间（docs/47）
// 砖长 = 60000 / (tbpm × N)；格子号 **1 起算**，t = φ + (k−1)·砖长（φ = 「偏移」那根轴）。
//   ★ 2026-09-19 补齐：原先这里是**只读占位**（一行 `label 起–止s`），
//     空列表时连占位都不渲染 ⇒ 面板上看起来「少了这一块」。现在按原版补：
//     空状态提示 + 可编辑行（名字 / ✕ / 起止＋毫秒↔格 / N / 多押轨）+ 两条硬拦
//     （终点必须在起点之后；砖数超 20000 就**不 schedule**，别让后端白跑几十秒）。
function xkPeriodMs(n) {
  const tb = Number(state.xk_tbpm) || 0;
  const k = Number(n || state.xk_base) || 0;
  return (tb > 0 && k > 0) ? 60000 / (tb * k) : 0;
}
function xkTileToMs(v) {
  const p = xkPeriodMs();
  if (!p) return null;
  return (Number(state.offset) || 0) + (Number(v) - 1) * p;
}
function xkMsToTile(v) {
  const p = xkPeriodMs();
  if (!p) return null;
  return Math.round((Number(v) - (Number(state.offset) || 0)) / p) + 1;
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
    // ★ 空状态**必须说人话**：以前这里什么都不画 ⇒ 面板上那段是"隐身"的，
    //   用户看着像少了东西（也看不出「不框区间 = 全曲铺」这条语义）。
    const d = el('div', 'hint');
    d.textContent = '（没有区间 ⇒ 按「采bpm」选的 N 全曲铺骨架；在这儿框了区间就只采区间、区间外走原路径）';
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
    const del = el('button', 'rdel');
    del.innerHTML = ShellUI.icon('close'); del.title = '删除该区间';
    del.onclick = () => {
      state.xk_ranges = (state.xk_ranges || []).filter((_x, k) => k !== i);
      renderXkRanges(); schedule();
    };
    head.appendChild(name); head.appendChild(del); row.appendChild(head);
    // 起止：**毫秒 或 格子号**（切单位时用已知砖长精确换算，不猜）
    const trow = el('div', 'rtime');
    for (const side of ['start', 'end']) {
      const kMs = side + '_ms'; const kTk = side + '_tile';
      const isTile = rg[kTk] !== undefined && rg[kTk] !== null;
      const tag = el('span'); tag.textContent = (side === 'start' ? '起' : '止');
      const inp = document.createElement('input');
      inp.type = 'number';
      inp.step = isTile ? '1' : '100';
      inp.value = String(isTile ? rg[kTk] : Math.round(rg[kMs] ?? 0));
      inp.title = (side === 'start' ? '起' : '止') + '（毫秒或格子号，1 起算）';
      const unit = document.createElement('select');
      unit.className = 'xkunit';
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
              + ' —— 先核对 tbpm / 偏移');
            return;
          }
          delete rg[kMs]; rg[kTk] = Math.round(nv);
        } else {
          delete rg[kTk]; rg[kMs] = Math.round(Math.min(Math.max(0, nv), cap));
        }
        const a = (rg.start_ms !== undefined) ? rg.start_ms : xkTileToMs(rg.start_tile);
        const b = (rg.end_ms !== undefined) ? rg.end_ms : xkTileToMs(rg.end_tile);
        if (a !== null && b !== null && b <= a) {
          setStatus('⚠ 采bpm 区间的终点必须在起点之后（否则后端会整组拒绝）');
        }
        // ★ 砖数预估：当场拦住。后端也有硬上限，但别让它先白跑几十秒
        //   （那会把整个 sidecar 卡住，表现就是「进度条和重新生成一起冻死」）。
        const p = xkPeriodMs(rg.xk_base);
        if (p && a !== null && b !== null && b > a) {
          const cnt = Math.floor((b - a) / p) + 1;
          if (cnt > 20000) {
            setStatus(`⚠ 这段要铺 ${cnt} 块砖（超过上限 20000，砖长 ${p.toFixed(3)}ms）`
              + ' —— 多半是 tbpm / N 填错，已不生成，请先核对');
            renderXkRanges();
            return;                              // 不 schedule：别让后端白跑
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
      trow.appendChild(tag); trow.appendChild(inp); trow.appendChild(unit);
    }
    row.appendChild(trow);
    // 这一段的 N 与多押轨（每段可不同；留空 = 用 ② 那组全局多押轨）
    const nrow = el('div', 'rrow');
    const nlab = el('span'); nlab.textContent = 'N';
    const nsel = document.createElement('select');
    for (const n of [2, 4, 8]) {
      const o = document.createElement('option'); o.value = String(n); o.textContent = `${n}k`;
      nsel.appendChild(o);
    }
    nsel.value = String(rg.xk_base || state.xk_base || 4);
    nsel.title = '这一段的 base（每段可以不一样；混用会引入 SetSpeed，2 的幂可整除）';
    nsel.onchange = () => { rg.xk_base = Number(nsel.value); renderXkRanges(); schedule(); };
    const tr = document.createElement('input');
    tr.type = 'text';
    tr.className = 'xktrk';
    tr.placeholder = '多押轨(空=全局)';
    tr.value = (rg.tracks || []).join(',');
    tr.title = '这段的多押轨（轨号，逗号分隔；留空就用 ② 里勾的双押轨）';
    tr.onchange = () => {
      rg.tracks = tr.value.split(',').map((s) => Number(s.trim()))
        .filter((v) => Number.isFinite(v) && v >= 0);
      schedule();
    };
    nrow.appendChild(nlab); nrow.appendChild(nsel); nrow.appendChild(tr);
    row.appendChild(nrow);
    box.appendChild(row);
  });
}
function selectRegion(i) { selectedRegion = i; overview.sel = i; renderRegions(); overview.draw(); }

// ------------------------------------------------- ⑤d 演出分段（docs/62 §4.1）
// `show_segments = [{lo, hi, in_move, out_move}]`：起止用**当前生成谱面的格子号**
// （与游戏里填 startTile/endTile 同一口径，1 起算、闭区间）；`''` = 跟随预设，`'none'` = 不上。
//   没生成过谱面（拿不到 n_floors）时先给 [1,16]，等算出层数再夹回范围内。
function addShowSegment() {
  const n = (payload && payload.n_floors) ? payload.n_floors : 0;
  const segs = state.show_segments || [];
  const lo = segs.length ? Math.min((Number(segs[segs.length - 1].hi) || 1) + 1, Math.max(1, n || 9999)) : 1;
  const hi = n ? Math.min(n, lo + 15) : 16;
  state.show_segments = [...segs, { lo, hi, in_move: '', out_move: '' }];
  setStatus(`已加演出分段：起始方块 ${lo} → 结束方块 ${hi}`
    + (n ? `（本谱共 ${n} 层）` : '（还没生成谱面，层数未知）'));
  renderShowSegments(); schedule();
}

function renderShowSegments() {
  const box = $('#lst-show'); if (!box) return;
  box.innerHTML = '';
  const segs = state.show_segments || [];
  const n = (payload && payload.n_floors) ? payload.n_floors : 0;
  // ★ 谱面层数变化时把已存分段的方块号夹回 [1, n]，防止 re-derive 后越界
  //   （只在 n 已知时收紧，不改动 n=0 时用户手动设的值）。
  if (n) segs.forEach((sg) => {
    sg.lo = Math.max(1, Math.min(n, Number(sg.lo) || 1));
    sg.hi = Math.max(sg.lo, Math.min(n, Number(sg.hi) || sg.lo));
  });
  const MOVE_IN = [['', '预设'], ['入A', '入A · 大'], ['入B', '入B · 干脆'], ['none', '无']];
  const MOVE_OUT = [['', '预设'], ['出A', '出A · 有力'], ['出B', '出B · 无痕'],
    ['出C', '出C · 含蓄'], ['出D', '出D · 炸'], ['none', '无']];
  if (!segs.length) {
    const d = el('div', 'hint');
    d.textContent = '（没有分段 ⇒ 整谱都用 ⑤d 上面的预设入场 / 出场；三连音段仍然自动标出、自动走 QE）';
    box.appendChild(d); return;
  }
  segs.forEach((sg, i) => {
    const row = el('div', 'region');
    const head = el('div', 'rhead');
    const title = el('span', 'rname');
    title.textContent = `分段 ${i + 1}：方块 ${Number(sg.lo)} → ${Number(sg.hi)}`
      + (n ? `（${Math.max(0, Number(sg.hi) - Number(sg.lo) + 1)} 格）` : '');
    const del = el('button', 'rdel'); del.innerHTML = ShellUI.icon('close'); del.title = '删除该分段';
    del.onclick = () => {
      state.show_segments = segs.filter((_x, k) => k !== i);
      renderShowSegments(); schedule();
    };
    head.appendChild(title); head.appendChild(del);
    row.appendChild(head);
    // 起止：填方块号（1 起算，闭区间）
    const trow = el('div', 'rtime');
    for (const side of ['lo', 'hi']) {
      const lab = el('span', 'hint');
      lab.textContent = side === 'lo' ? '起始方块' : '结束方块';
      const inp = document.createElement('input');
      inp.type = 'number'; inp.step = '1'; inp.min = '1';
      inp.id = `inp-show-${side}-${i}`;
      inp.value = String(Number(sg[side]) || 1);
      inp.title = '当前生成谱面的格子号（1 起算，闭区间）';
      inp.onchange = () => {
        let v = Math.round(Number(inp.value) || 1);
        v = Math.max(1, n ? Math.min(n, v) : v);
        sg[side] = v;
        if (sg.lo > sg.hi) { if (side === 'lo') sg.hi = sg.lo; else sg.lo = sg.hi; }
        if (Number(inp.value) !== v) setStatus(n ? `⚠ 方块号夹回 1~${n}（本谱共 ${n} 层）` : '');
        renderShowSegments(); schedule();
      };
      trow.appendChild(lab); trow.appendChild(inp);
    }
    row.appendChild(trow);
    // 段内招选：留空 = 跟随预设
    const mrow = el('div', 'rtime');
    for (const [key, opts, label] of [['in_move', MOVE_IN, '入场'], ['out_move', MOVE_OUT, '出场']]) {
      const lab = el('span', 'hint'); lab.textContent = label;
      const sel = document.createElement('select');
      sel.id = `sel-show-${key}-${i}`;
      for (const [v, txt] of opts) { const o = document.createElement('option'); o.value = v; o.textContent = txt; sel.appendChild(o); }
      sel.value = sg[key] || '';
      sel.onchange = () => { sg[key] = sel.value; schedule(); };
      mrow.appendChild(lab); mrow.appendChild(sel);
    }
    row.appendChild(mrow);
    box.appendChild(row);
  });
}

// ===================================================== 求解链路
async function onTracksChanged() {
  const d = await api.derive(state);
  if (d.pitch_lo !== null && d.pitch_lo !== undefined) { setControl('pitch_lo', d.pitch_lo); setControl('pitch_hi', d.pitch_hi); }
  $('#lbl-track').textContent = d.hint || '';
  schedule();
}

function schedule() {
  if (applying) return;
  if (rebuildTimer) clearTimeout(rebuildTimer);
  rebuildTimer = setTimeout(() => { rebuildTimer = null; rebuild(); }, 140);
}

// 重建指纹：用来在「显式点重新生成」时告诉用户本次结果是否和上次完全相同
let lastRebuildFp = null;
async function _rebuildFp(r) {
  try {
    const sig = typeof r.payload === 'string' ? r.payload : JSON.stringify(r.payload || '');
    const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(sig));
    return [...new Uint8Array(buf)].map(b => b.toString(16).padStart(2, '0')).join('');
  } catch (e) { return null; }
}

async function rebuild(announce = false) {
  if (!loadInfo) return;
  rebuildCount += 1;
  setStatus('求解中…');
  const r = await api.rebuild(state);
  lastResult = r;
  if (!r.ok) {
    setStatus((r.stale ? '⚠ 保留了上一张谱面：' : '') + (r.msg || r.error || '失败'));
    const wBad = r.warning_list || [];
    if (wBad.length) { $('#warnings').textContent = '⚠ ' + wBad.join('；'); $('#warn-badge').textContent = `（${wBad.length} 条警告）`; $('#more').open = true; }
    refreshRepBrief();
    applyXkLock(); applyDpLock(); applyFitLock();
    return;
  }
  hasChart = true;
  showProgress(false);
  if (r.check && typeof r.check.suggest_ms === 'number') lastSuggest = r.check.suggest_ms;
  if (r.display_bpm && state.auto_bpm) setControl('base_bpm', Math.round(r.display_bpm * 1000) / 1000);
  if (r.auto_offset !== null && r.auto_offset !== undefined && !offsetTouched) { lastAutoOffset = r.auto_offset; setControl('offset', r.auto_offset); }
  payload = r.payload;
  // ★ 自测/调试钩子：给 headless 探针一个确定性「谱面已就绪」信号，
  //   避免它抢在异步 rebuild 之前点按钮（那样 payload 还没设、n_floors 取不到）。
  window.__chartReady = true;
  window.__nFloors = r.n_floors;
  applyPayload();
  setStatus(r.status || '');
  // ★ 每次成功求解都记指纹，供「显式点重新生成」比对是否和上次相同（确定性求解，参数没动=完全相同）
  const prevFp = lastRebuildFp;
  lastRebuildFp = await _rebuildFp(r);
  if (announce) {
    if (prevFp && lastRebuildFp && prevFp === lastRebuildFp)
      toast(`已重新生成 · 与上次完全相同（左侧改参数才会变谱面）`);
    else
      toast(`已重新生成 · ${r.n_floors} 层 · ${r.n_onsets} 采音点`);
  }
  $('#timing').textContent = r.timing || '';
  const w = r.warning_list || [];
  $('#warnings').textContent = w.length ? ('⚠ ' + w.join('；')) : '';
  $('#warn-badge').textContent = w.length ? `（${w.length} 条警告）` : '';
  if (w.length) $('#more').open = true;
  refreshRepBrief();               // 警告数刚更新，摘要跟着刷（setStatus 在这之前调过）
  const lblFit = $('#lbl-fit');
  if (lblFit) {
    const fr = r.fit || {}, dnr = r.denoise || {};
    if (r.fit_mode === 'direct') lblFit.textContent = `当前：直拟合（1/${dnr.div || '?'} 格）· 直线 ${((fr.straight_frac || 0) * 100).toFixed(1)}% · SetSpeed ${fr.n_setspeed || 0} · 发卡弯 ${fr.n_hairpin || 0} · 时序误差 max ${(fr.err_max_ms || 0).toFixed(4)}ms`;
    else lblFit.textContent = '当前：最优化（④ 求解/几何那套：模板/三连音/雪花）' + (dnr.grid ? '　去噪已算但最优化不用它' : '');
  }
  const lblXk = $('#lbl-xk');
  if (lblXk) { const xr = r.xk || {}; lblXk.textContent = xr.text || '采bpm：关（③b 选「关」或 tbpm 没填）—— 全曲走原路径。'; }
  applyXkLock(); applyDpLock(); applyFitLock();
  const dv = $('#ver'); if (dv && r.base_bpm) dv.textContent = `bpm ${r.base_bpm.toFixed(2)}`;
  $('#tab-hint').textContent = `${r.n_floors} 层 · ${r.n_onsets} 采音点 · 双押 ${(r.dp && r.dp.dp_hits) || 0}` + ((r.dp && r.dp.dp_three) ? ` （含三押 ${r.dp.dp_three}）` : '') + ((r.dp && r.dp.dp_extra_press) ? ` · 跳过四押 ${r.dp.dp_extra_press}` : '');
  if (player.src && Math.abs(player.currentTime * 1000 - 0) < 1) seek(0);
  renderChips(r);
  band.segs = []; band.setData(payload);
  reportStatus();
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
  // 数据到位后重新量一次（band 的时长决定刻度密度）
  sizeViews();
  // 谱面数据到位 → 自动出图（无需用户点任何按钮）。若已判定环境不支持/失败则不再重试。
  if (activeTab === 'chart' && !previewBlocked) ensurePreview();
  // ★ 音源「就绪 / 未就绪」要看 `payload.preview_audio`（重算完才有）⇒ 这里再刷一次药丸：
  //   就绪就把那颗药丸连它那条空条一起收掉，没就绪才亮出来。
  try { refreshAvSummary(); } catch (_e) {}
  // 顺手把这份音频挂到播放条的 <audio>（另外三个视图用）——换音源才重挂，见函数注释
  try { syncAudioToSource(); } catch (_e) {}
}

// ===================================================== 下落式
function bisectRight(arr, x) { let lo = 0, hi = arr.length; while (lo < hi) { const m = (lo + hi) >> 1; if (arr[m] <= x) lo = m + 1; else hi = m; } return lo; }
function updateFalling() {
  if (!payload) return;
  const falls = payload.falls || [];
  const lanes = Number(state.lanes) || 4;
  const mode = Number(state.lanemode) || 0;
  const ps = [...new Set(falls.map((f) => f.pitch))].sort((a, b) => a - b);
  const lo = ps.length ? ps[0] : 0;
  const span = Math.max(1, (ps.length ? ps[ps.length - 1] : 0) - lo);
  const mapped = falls.map((f) => {
    let lane;
    if (mode === 0) lane = Math.trunc(((bisectRight(ps, f.pitch) - 0.5) / Math.max(1, ps.length)) * lanes);
    else if (mode === 1) lane = Math.round(((f.pitch - lo) / span) * (lanes - 1));
    else lane = falls.indexOf(f) % lanes;
    return { ...f, lane: Math.max(0, Math.min(lanes - 1, lane)) };
  });
  views.falling.setChart({ bpm0: payload.bpm0, falls: mapped, cap: payload.cap }, { lanes, speed: Number(state.fspeed) || 100, division: Number(state.division) || 8 });
  views.falling.setTime(audioToGrid(player.currentTime * 1000 || 0));
  if (activeTab === 'falling') drawActive();
}

// ===================================================== 谱面预览（黑盒）
function previewSignature() {
  return [rebuildCount, state.offset, state.countdown_ticks, (state.regions || []).length, state.auto_offset, (state.segments || []).length, state.segment_mode,
    // ★ 演出分段（docs/62）：改一段就重挂预览（谱面 JSON 会变）
    JSON.stringify(state.show_segments || []),
    state.preview_audio_mode, state.preview_audio_path].join('|');
}
// 引擎 handle 的「层数」是**属性** preview.tileCount（不是方法）。
// 但不同版本可能又给成方法，这里统一兜一层，免得又白炸一次。
function previewTiles() {
  if (!preview) return null;
  try { const t = preview.tileCount; return (typeof t === 'function') ? t.call(preview) : (t == null ? null : t); }
  catch (_e) { return null; }
}

// WebGL 可用性预检：远程桌面 / 虚拟显示 / 无 GPU 合成面时 WebGL 起不来，
// createPreview 会**同步卡死主线程**（连 setTimeout 看门狗都不触发）。必须先拦下。
function webglOk() {
  try {
    const c = document.createElement('canvas');
    return !!(c.getContext('webgl2') || c.getContext('webgl') || c.getContext('experimental-webgl'));
  } catch (_e) { return false; }
}

async function ensurePreview() {
  const host = $('#cv-adofai');
  if (!host) { pageLog('ensurePreview: NO HOST'); return; }
  // 谱面还没生成 → 静默等待（不弹任何东西）
  if (!loadInfo || !hasChart || !payload) { pageLog('ensurePreview: skip loadInfo/hasChart/payload=' + [!!loadInfo, !!hasChart, !!payload]); hidePhNote(); return; }
  const key = previewSignature();
  if (preview && previewKey === key) { pageLog('ensurePreview: same key, skip'); return; }
  if (previewLoading) { pageLog('ensurePreview: already loading'); return; }
  // ★ 建预览前先探 WebGL：不可用就直接放弃，绝不调用 createPreview（否则整页冻死）
  if (!webglOk()) {
    previewBlocked = true;
    pageLog('ensurePreview: webgl unavailable, abort');
    showPhNote('当前环境起不了 WebGL，官方渲染预览无法显示；检查器 / 参数 / 生成 / 导出与另外三个视图都不受影响');
    return;
  }
  previewLoading = true;
  showPhNote('正在加载预览引擎…');
  try {
    pageLog('ensurePreview: import engine…');
    if (!previewMod) previewMod = await import('./vendor/adofai-player.js');
    pageLog('ensurePreview: engine loaded, levelJson…');
    const lj = await api.levelJson(state);
    if (!lj || !lj.ok) { const m = (lj && lj.error) || '拿不到谱面 JSON'; setStatus('⚠ 预览：' + m); pageLog('ensurePreview: levelJson bad'); previewBlocked = true; showPhNote('预览加载失败：' + m, true); return; }
    pageLog('ensurePreview: levelJson ok, audio…');
    const a = await api.audio(state);
    const au = a && a.ok ? api.mediaUrl(a.path) : null;
    pageLog('ensurePreview: audio=' + (a && a.ok) + ' url=' + (au ? 'set' : 'null'));
    if (preview) { try { preview.destroy(); } catch (_e) {} preview = null; }
    previewStarted = false; previewPaused = false;
    pageLog('ensurePreview: createPreview…');
    // ★ 兜底看门狗：万一 createPreview 变成「异步的慢」而不是同步卡死，也被这里收住。
    const CW = 45000;
    const _cp = previewMod.createPreview(host, JSON.stringify(lj.level), au, {
      editorMode: true, trail: true, renderer: 'webgl', hitsound: true, disableTrackTexture: true,
      musicDelayMs: (Number(state.music_delay_ms) || 0) + oggMs(),
    });
    preview = await Promise.race([
      _cp,
      new Promise((_, rej) => setTimeout(() => rej(new Error('预览引擎 45s 未响应（环境缺少可用的 WebGL/GPU 合成面）')), CW)),
    ]);
    killHitErrorMeter(preview);
    previewKey = key; previewStarted = false; previewPaused = false;
    // ★ 重施音量：引擎的 loadMusic() 会按谱面 settings.volume 覆盖一次 music.volume，
    //   不压回去的话「重建预览 / 换音源」之后滑块位置和实际音量就对不上了。
    applyVolume(currentVol());
    // ★ 打拍音（2026-09-25 修复）：workbench 此前 hitsound:false 且从不合成 ⇒ 真机全程静默。
    //   改为 true 后，等 tileStartTimes 就绪再合成整曲 buffer（见 synthesizeHitsounds）。
    synthesizeHitsounds(preview);
    startPreviewClock();
    hidePhNote();
    pageLog('ensurePreview: ok tiles=' + previewTiles());
    reportStatus();
  } catch (e) {
    const m = (e && e.message ? e.message : e);
    previewBlocked = true;
    setStatus('⚠ ADOFAI 预览失败：' + m);
    pageLog('ensurePreview: ERR ' + m);
    showPhNote('预览加载失败：' + m, true);
    reportStatus();
  }
  finally { previewLoading = false; }
}
// ★ 去掉预览播放时的「判定条」（引擎内置的准度条 / HitErrorMeter）：
//   引擎 createPlayer 时会在容器里挂一张 zIndex:9998 的 canvas 画判定条，而且每次命中
//   （addHit）都会把它设成 visible=true —— 于是播放时它一直横在画面下方，很遮挡视野。
//   这里在预览建好后立刻 dispose（移除 canvas）并把字段置 null，之后引擎内部所有
//   this.hitErrorMeter?.xxx() 都变成 no-op，判定条不会再出现。纯宿主侧处理，不改引擎源码。
function killHitErrorMeter(pv) {
  try {
    const p = pv && pv.player;
    if (p && p.hitErrorMeter) {
      try { p.hitErrorMeter.dispose(); } catch (_e) {}
      p.hitErrorMeter = null;
    }
  } catch (_e) {}
  // DOM 兜底：万一引擎日后改了字段名，按特征（zIndex 9998）把残留的判定条画布藏掉
  try {
    const host = $('#cv-adofai');
    if (host) for (const c of host.querySelectorAll('canvas')) {
      if (c.style && c.style.zIndex === '9998') c.style.display = 'none';
    }
  } catch (_e) {}
}
// 提示条上的「重试」：清掉阻断标记再跑一次
function retryPreview() { previewBlocked = false; ensurePreview(); }
function previewAvailable() { return !!preview; }

// ★ 打拍音合成（2026-09-25，移植自 studio-skin 第7条修复，适配 workbench 的 preview 结构）
// 之前 workbench 的 createPreview 传 hitsound:false 且从不调合成，真机全程静默。
// 引擎的 `tileStartTimes` 只在渲染循环每帧的 update(stats) 里赋值，建完立刻同步合成必然
// 撞上空数组提前 return；所以必须**轮询等它就绪**再合成。hitsoundToken 防重建竞态
// （合成途中又重建预览 ⇒ 作废这次结果）。引擎用默认 Kick / 音量100（谱面不设 hitsound 时）。
let hitsoundToken = 0;
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
  const p = pv && pv.player; // workbench 的 preview 包了一层，真正的 Player 在 .player
  // 引擎若没暴露这个方法（将来换 vendor 版本）就安静跳过，别抛
  if (!p || typeof p.preSynthesizeHitsoundsWithProgress !== 'function') return;
  if (!p.hitsoundManager || !p.hitsoundManager.isEnabled()) return;
  if (p.hitsoundManager.isSynthesized()) return;
  // 等 tileStartTimes 就绪（渲染循环每帧才填，建完立刻调必然空）
  const ready = await waitTileStartTimes(p, 2000, token);
  if (token !== hitsoundToken) return;   // 合成期间又重建过预览 ⇒ 这次结果作废
  if (!ready) return;                    // 等不到（极端：渲染循环没起）⇒ 留待播放兜底
  try { await p.preSynthesizeHitsoundsWithProgress(); } catch (_e) { /* 合成失败不影响预览播放 */ }
}
// ★ 音量接线（2026-09-26）——主人：「这个音量按钮是假的」。
//   实测确认：`#vol` 原本只写 `player.volume`（<audio id="player">），而**默认的「谱面」页
//   声音根本不是它发的**：音乐是引擎自己播的（`preview.player.music`），打拍音走
//   `hitsoundManager` 的 gainNode（合成时把 hitsoundVolume 烘进 buffer，运行期只剩 gainNode 能调）。
//   两条链与 #player 毫无关系 ⇒ 在谱面页拖滑块毫无反应，看着就是个假按钮。
//   这里把同一个值同时施加到三条链上。**纯宿主侧处理，不改 vendor**（同 killHitErrorMeter 的路子）。
function currentVol() {
  const el = $('#vol');
  const n = Number(el && el.value);
  return Math.min(1, Math.max(0, (Number.isFinite(n) ? n : 70) / 100));
}
function applyVolume(v) {
  const vol = Math.min(1, Math.max(0, Number(v)));
  try { player.volume = vol; } catch (_e) {}
  const p = preview && preview.player;   // workbench 的 preview 包了一层，真 Player 在 .player
  if (!p) return;                        // 谱面页还没出图：先记在滑块上，ensurePreview 出图后会补施
  // ① 引擎音乐（MusicPlayer，有 volume 读写；audio 未加载时赋值会抛，交给 catch）
  try { if (p.music) p.music.volume = vol; } catch (_e) {}
  // ② 打拍音：buffer 已烘死，只有这个增益节点能动
  try {
    const hs = p.hitsoundManager;
    if (hs && typeof hs.getGainNode === 'function') hs.getGainNode().gain.value = vol;
  } catch (_e) {}
}
function pausePreview() { if (preview) { try { preview.stop(); } catch (_e) {} } previewStarted = false; previewPaused = false; }
function startPreviewClock() { stopPreviewClock(); previewClock = setInterval(() => { if (!preview || activeTab !== 'chart') return; const ms = chartToGrid(preview.currentTimeMs); views.roll.setPlayhead(ms); views.path.setPlayhead(ms); views.falling.setTime(ms); overview.setPlayhead(ms); band.setPlayhead(ms); refreshTimeFromMs(preview.currentTimeMs); }, 100); }
function stopPreviewClock() { if (previewClock) { clearInterval(previewClock); previewClock = null; } }

// ===================================================== 标签切换
// 谱面页 = 自动出图，无需点任何按钮。环境不支持 / 加载失败时，只在舞台底部亮一行小字；
// 绝不弹整块卡片、也绝不挡画布。
function setTab(name) {
  activeTab = name;
  closeAvPop();                    // 切标签就收掉「音源与偏移」浮层（它属于谱面预览标签）
  for (const b of document.querySelectorAll('.tab')) b.classList.toggle('active', b.dataset.tab === name);
  const wantId = name === 'chart' ? 'cv-adofai' : `cv-${name}`;
  for (const c of document.querySelectorAll('.view')) c.classList.toggle('active', c.id === wantId);
  for (const b of document.querySelectorAll('.vbar')) b.classList.toggle('on', b.dataset.for === name);
  if (name === 'chart') {
    if (!previewBlocked) { ensurePreview(); startPreviewClock(); }
  } else { pausePreview(); stopPreviewClock(); hidePhNote(); }
  if (name === 'falling') $('#tab-hint').textContent = '下落式跟着音频走（offset 已计入）';
  if (name === 'chart') $('#tab-hint').textContent = 'ADOFAI 官方渲染引擎 · 自动出图';
  if (name === 'path') $('#tab-hint').textContent = '播放头按 entryTime 轴（不含 offset）';
  if (name === 'roll') $('#tab-hint').textContent = '钢琴卷帘：横轴时间 / 纵轴音高';
  drawActive();
  // 切页后画布刚从 display:none 变可见，下一帧再量一次（roll/path/falling 的 draw 里自量，这是双保险）
  requestAnimationFrame(sizeViews);
}

// 舞台底部的一行小字提示（加载中 / 失败时才有；正常出图时它完全不存在）
function showPhNote(text, withRetry) {
  const bar = $('#ph-note');
  if (!bar) return;
  const tx = $('#ph-text'), rt = $('#ph-retry');
  if (tx) tx.textContent = text || '';
  if (rt) rt.classList.toggle('hidden', !withRetry);
  bar.classList.remove('hidden');
}
function hidePhNote() {
  const bar = $('#ph-note');
  if (bar) bar.classList.add('hidden');
}
function drawActive() { const v = views[activeTab]; if (v) v.draw(); }

// 布局一变（窗口缩放 / 拖分隔条 / 首次显示）就把所有画布重新量一遍真实尺寸。
// ★ 必调：canvas 的绘图缓冲不会自己跟 CSS 走，不量就永远停在默认 300×150，
//   却被 CSS 拉伸到实际显示尺寸 → 里面的字和刻度被整体放大（就是那副"雷霆界面"）。
//   这条链路是Adofai-Chart-Generator原版的 sizeViews()，我上一版漏移植了。
function sizeViews() {
  try { band.size(); } catch (_e) { /* 还没数据 */ }
  try { overview.draw(); } catch (_e) { /* 全曲条没有 size()，draw 里自己量 clientWidth */ }
  try { drawActive(); } catch (_e) {}
  if (preview) { try { preview.resize(); } catch (_e) {} }
}
window.addEventListener('resize', () => sizeViews());

// ------------------------------------------------- 分隔条拖动
// ★ 原版 studio 在 layout.js 里给 .split 挂了 pointerdown/move/up；搬这个工作台时
//   **拖动实现整个漏掉了**，五条 .split 只剩 `cursor: col-resize` 当装饰 ⇒ 主人报
//   「分隔线不能调节大小了」。这里按原版语义补上：左栏 +dx、右栏 -dx（往左拖变宽）、
//   段带 -dy（往上拖变高），区间钳位也照原版（左右 200~660）。
const SPLIT_TARGET = { left: '#left', right: '#rcol', tl: '#tlwrap' };
const SPLIT_RANGE = { left: [200, 660], right: [200, 660], tl: [60, 400] };

// ------------------------------------------------- 报告带：收起 / 展开 + 高度可调
// ★ 报告带原来固定 152px 高、永远占着底部，主人报「挡界面」。现在：
//   · 默认**收起**（只剩标题栏一行，行里 #rep-brief 仍报当前状态/警告数，信息不丢）；
//   · 展开高度 = --report-h，拖它上面那条 .split[data-split="report"] 改（双击复位）；
//   · 收起态也记进 localStorage，下次进来还是你上次的样子。
const REPORT_H0 = 152, REPORT_MIN = 66, REPORT_MAX = 460;
function repGet(k, dflt) { try { const v = localStorage.getItem(k); return v === null ? dflt : v; } catch (_e) { return dflt; } }
function repSet(k, v) { try { localStorage.setItem(k, v); } catch (_e) {} }
let reportH = Math.max(REPORT_MIN, Math.min(REPORT_MAX, Number(repGet('adoc.reportH', REPORT_H0)) || REPORT_H0));
let reportCol = repGet('adoc.reportCollapsed', '1') !== '0';

function applyReportLayout() {
  const rp = $('#report'); if (!rp) return;
  rp.classList.toggle('collapsed', reportCol);
  document.documentElement.style.setProperty('--report-h', reportH + 'px');
  const sp = $('#split-report'); if (sp) sp.classList.toggle('hidden', reportCol);
  const c = $('#rep-caret'); if (c) c.dataset.open = reportCol ? 'false' : 'true';
  const tip = $('#rep-tip'); if (tip) tip.textContent = reportCol ? '点标题展开 · 拖分隔条调高' : '点数字看明细';
}
function setReportCollapsed(v) {
  reportCol = !!v;
  repSet('adoc.reportCollapsed', reportCol ? '1' : '0');
  applyReportLayout();
  sizeViews();
}
function setReportH(v) {
  reportH = Math.round(Math.max(REPORT_MIN, Math.min(REPORT_MAX, v)));
  document.documentElement.style.setProperty('--report-h', reportH + 'px');
  sizeViews();
}
// 收起时标题栏右侧那行摘要：状态首句 + 警告条数（比只写「报告 / 警告」有用）
function refreshRepBrief() {
  const b = $('#rep-brief'); if (!b) return;
  const st = $('#status') ? $('#status').textContent.replace(/\s+/g, ' ').trim() : '';
  const wb = $('#warn-badge') ? $('#warn-badge').textContent.trim() : '';
  let s = (st.split(/[。；]/)[0] || '').trim();
  if (s.length > 46) s = s.slice(0, 46) + '…';
  if (wb) s = (s ? s + ' · ' : '') + wb;
  b.textContent = s;
  b.title = st;
}

function bindSplits() {
  document.querySelectorAll('.split').forEach((sp) => {
    sp.addEventListener('pointerdown', (e) => {
      const which = sp.dataset.split;
      // ★ 报告带：它不在 SPLIT_TARGET 里（改的不是某个元素的 flex，而是 --report-h 变量），
      //   而且方向相反 —— 报告带在底部，往上拖(Δy<0)才是变高。
      if (which === 'report') {
        const y0 = e.clientY, h0 = reportH;
        sp.classList.add('on');
        try { sp.setPointerCapture(e.pointerId); } catch (_e) { /* 无所谓 */ }
        const mv = (ev) => setReportH(h0 + (y0 - ev.clientY));
        const up = () => {
          sp.classList.remove('on');
          window.removeEventListener('pointermove', mv);
          window.removeEventListener('pointerup', up);
          window.removeEventListener('pointercancel', up);
          repSet('adoc.reportH', String(reportH));
          sizeViews();
        };
        window.addEventListener('pointermove', mv);
        window.addEventListener('pointerup', up);
        window.addEventListener('pointercancel', up);
        e.preventDefault();
        return;
      }
      const sel = SPLIT_TARGET[which];
      if (!sel) return;
      const el = document.querySelector(sel);
      if (!el) return;
      const [lo, hi] = SPLIT_RANGE[which] || [100, 800];
      const x0 = e.clientX, y0 = e.clientY;
      const box = el.getBoundingClientRect();
      const isRow = which === 'tl';
      const b0 = isRow ? box.height : box.width;
      sp.classList.add('on');
      try { sp.setPointerCapture(e.pointerId); } catch (_e) { /* 无所谓 */ }
      const mv = (ev) => {
        const d = isRow ? (y0 - ev.clientY)
                        : (which === 'left' ? (ev.clientX - x0) : (x0 - ev.clientX));
        el.style.flex = '0 0 ' + Math.round(Math.max(lo, Math.min(hi, b0 + d))) + 'px';
        sizeViews();
      };
      const up = () => {
        sp.classList.remove('on');
        window.removeEventListener('pointermove', mv);
        window.removeEventListener('pointerup', up);
        window.removeEventListener('pointercancel', up);
        sizeViews();
      };
      window.addEventListener('pointermove', mv);
      window.addEventListener('pointerup', up);
      window.addEventListener('pointercancel', up);
      e.preventDefault();          // 防止拖动时选中文字 / 触发原生拖拽
    });
    sp.addEventListener('dblclick', () => {          // 双击复位该条分隔
      if (sp.dataset.split === 'report') { setReportH(REPORT_H0); repSet('adoc.reportH', String(REPORT_H0)); return; }
      const sel = SPLIT_TARGET[sp.dataset.split];
      const el = sel ? document.querySelector(sel) : null;
      if (el) { el.style.flex = ''; sizeViews(); }
    });
  });
}

// ===================================================== 播放
function fmt(v) { const s = Math.max(0, v) / 1000; const m = Math.floor(s / 60); return `${m}:${(s - m * 60).toFixed(2).padStart(5, '0')}`; }
function refreshTime() { const pos = player.currentTime * 1000 || 0; const dur = (player.duration || 0) * 1000; $('#lbl-time').textContent = `${fmt(pos)} / ${fmt(dur)}`; if (dur > 0) $('#slider').value = String(Math.round(pos / dur * 1000)); }
function refreshTimeFromMs(ms) { const dur = (player.duration || 0) * 1000; $('#lbl-time').textContent = `${fmt(ms)} / ${fmt(dur)}`; if (dur > 0) $('#slider').value = String(Math.round(ms / dur * 1000)); }

function syncFromAudio() {
  const grid = audioToGrid(player.currentTime * 1000 || 0);
  views.roll.setPlayhead(grid); views.path.setPlayhead(grid); views.falling.setTime(grid);
  overview.setPlayhead(grid); band.setPlayhead(grid); drawActive(); overview.draw(); refreshTime();
}

async function ensureAudio(refresh = false, quiet = false) {
  if (player.src && !refresh) return true;
  if (!quiet) setStatus('准备音频…（无音源时用内置合成音色，可能几秒）');
  const r = await api.audio(state);
  if (!r.ok) { if (!quiet) setStatus(`⚠ ${r.error || '没有可用音频'}`); return false; }
  const url = api.mediaUrl(r.path);
  if (player.src !== url) { player.src = url; player.currentTime = 0; player.load(); }
  const base = String(r.path).split(/[\\/]/).pop();
  const a = $('#audio-src'); a.textContent = `音频：${base}`; a.title = r.path;
  if (!quiet) setStatus('音频就绪');
  return true;
}

async function togglePlay() {
  if (activeTab === 'chart' && preview) {
    // ★ 暂停/继续必须靠我们自己的意图标志，不能信 preview.isPlaying：
    //   引擎的 isPlaying 只在彻底 stop 时才变 false，pause 只切内部 isPaused，
    //   否则「暂停后再点播放」会再次进入 pause 分支、被 pausePlay 的守卫 no-op 掉，
    //   表现为按钮卡死、怎么点都没反应。
    if (!previewStarted) {
      await synthesizeHitsounds(preview);
      preview.startPlay(Math.max(0, preview.currentTimeMs));
      previewStarted = true; previewPaused = false; setPlayBtn(true);
    } else if (previewPaused) {
      preview.resume(); previewPaused = false; setPlayBtn(true);
    } else {
      preview.pause(); previewPaused = true; setPlayBtn(false);
    }
    return;
  }
  if (!player.paused) { player.pause(); setPlayBtn(false); return; }
  if (!loadInfo) { setStatus('先加载一个文件。'); return; }
  if (!hasChart) await rebuild();
  if (!await ensureAudio()) return;
  const p = player.play();
  if (p && p.catch) p.catch((e) => setStatus(`⚠ 播放失败：${e.message}`));
  setPlayBtn(true);
  views.falling.reset();
  if (!uiTimer) uiTimer = setInterval(syncFromAudio, 100);
}

function seek(ms) {
  const m = Math.max(0, ms);
  if (activeTab === 'chart' && preview) { try { preview.seekTo(gridToChart(m), false); } catch (_e) {} }
  player.currentTime = gridToAudio(m) / 1000;
  refreshTime();
  views.path.setPlayhead(m); views.roll.setPlayhead(m); views.falling.setTime(m);
  overview.setPlayhead(m); band.setPlayhead(m); drawActive(); overview.draw();
}

// ===================================================== 报告
function renderChips(r) {
  const box = $('#chips'); if (!box) return; box.innerHTML = '';
  const add = (label, val, bad) => { const c = el('span', 'chip' + (bad ? ' bad' : '')); c.innerHTML = `${label} <b>${val}</b>`; box.appendChild(c); };
  add('层', r.n_floors || 0);
  add('采音点', r.n_onsets || 0);
  const dp = r.dp || {};
  add('双押', dp.dp_hits || 0);
  if (dp.dp_three) add('三押', dp.dp_three, true);
  if (dp.dp_extra_press) add('跳过四押', dp.dp_extra_press, true);
  if (r.n_violations) add('违规', r.n_violations, true);
  const w = (r.warning_list || []).length; if (w) add('警告', w, true);
}

// ===================================================== 加载 / 导出
// 把当前页面状态快照回传给宿主（走 WebMessage 通道，比 ExecuteScript 探针更稳）
function reportStatus() {
  let tiles = previewTiles();
  const s = {
    schema: SCHEMA ? (SCHEMA.fields || []).length : 0,
    groups: (typeof document !== 'undefined') ? document.querySelectorAll('#groups .grp').length : 0,
    controls: (typeof document !== 'undefined') ? document.querySelectorAll('#groups input, #groups select, #groups textarea').length : 0,
    loaded: !!loadInfo,
    file: loadInfo ? (loadInfo.path || loadInfo.name || null) : null,
    hasChart: !!hasChart,
    tab: activeTab,
    tiles: tiles,
    err: lastPageError,
    diag: (function () {
      try {
        const rc = (el) => { if (!el) return null; const r = el.getBoundingClientRect(); return { w: Math.round(r.width), h: Math.round(r.height), t: Math.round(r.top), l: Math.round(r.left) }; };
        const cs = (el, p) => (el ? getComputedStyle(el)[p] : null);
        const ph = $('#ph-note'), st = $('#stage'), tl = $('#tlwrap'), cb = $('#cv-band');
        return {
          win: { iw: innerWidth, ih: innerHeight, dpr: devicePixelRatio, vvScale: (window.visualViewport ? visualViewport.scale : null) },
          ph: { rect: rc(ph), display: cs(ph, 'display'), txt: ph ? ph.textContent.slice(0, 80) : null },
          stage: { rect: rc(st), bg: cs(st, 'backgroundColor') },
          tlwrap: rc(tl), band: { rect: rc(cb), cw: cb ? cb.width : null, ch: cb ? cb.height : null },
          bars: { tabs: rc($('#tabs')), viewbars: rc($('#viewbars')), vbarChart: rc($('#vbar-chart')), playbar: rc($('#playbar')), ovwrap: rc($('#ovwrap')) },
          cols: { left: rc($('#left')), right: rc($('#right')), work: rc($('#work')) },
          panels: {
            leftSecs: Array.from(document.querySelectorAll('#panel-left > .grp > h4')).map((e) => e.textContent.trim()),
            rightSecs: Array.from(document.querySelectorAll('#panel > .grp > h4')).map((e) => e.textContent.trim()),
            leftLen: $('#panel-left') ? $('#panel-left').textContent.length : null,
            rightLen: $('#panel') ? $('#panel').textContent.length : null,
            leftHead: $('#panel-left') ? $('#panel-left').innerText.replace(/\s+/g, ' ').trim().slice(0, 260) : null,
            lists: ['#lst-tracks', '#lst-sub', '#lst-dp'].map((s) => { const e = $(s); return e ? [e.children.length, e.closest('.grp') ? e.closest('.grp').style.display : '?'] : null; }),
            nRegions: (state.regions || []).length, nSegments: (state.segments || []).length,
          },
          canv: Array.from(document.querySelectorAll('#stage canvas, #ovwrap canvas')).map((c) => ({ id: c.id, w: c.width, h: c.height, cw: c.clientWidth, ch: c.clientHeight })),
          report: { rect: rc($('#report')), sh: $('#report') ? $('#report').scrollHeight : null, oh: $('#report') ? $('#report').offsetHeight : null },
          status: { rect: rc($('#status')), fs: cs($('#status'), 'fontSize'), len: $('#status') ? $('#status').textContent.length : null, txt: $('#status') ? $('#status').textContent.slice(0, 120) : null },
          more: { rect: rc($('#more')), open: $('#more') ? $('#more').open : null },
          chips: { rect: rc($('#chips')), n: $('#chips') ? $('#chips').children.length : null },
          timing: { rect: rc($('#timing')), fs: cs($('#timing'), 'fontSize'), len: $('#timing') ? $('#timing').textContent.length : null },
          warnings: { rect: rc($('#warnings')), len: $('#warnings') ? $('#warnings').textContent.length : null },
        };
      } catch (e) { return 'DIAG_ERR:' + e; }
    })(),
  };
  try { window.dsh && window.dsh.post && window.dsh.post({ type: 'workbench_status', status: s }); } catch (_) {}
  return s;
}

async function doOpen() { const p = await window.dsh.openFile(); if (p) doLoad(p); }

async function doLoad(path) {
  pageLog('doLoad:start ' + path);
  if (/\.(ogg|oga|wav|flac|mp3)$/i.test(String(path || ''))) { setStatus('⚠ 本版本不支持直接采音频（可用作预览音源）'); return; }
  showProgress(true); setProgress(0.02, '开始…');
  let r;
  try { pageLog('doLoad:api.load before'); r = await api.load(path); pageLog('doLoad:api.load ok ' + (r && r.ok)); }
  catch (e) { showProgress(false); setStatus(`⚠ 加载失败：${(e && e.message) || e}`); pageLog('doLoad:api.load ERR ' + ((e && e.message) || e)); reportStatus(); return; }
  showProgress(false);
  if (!r.ok) { setStatus(`⚠ ${r.error || '加载失败'}`); reportStatus(); return; }
  loadInfo = r;
  // ★ 音源接续（2026-09-21 主人裁定）：一次都没选过 ⇒ 把本工程自带的那份原曲接成预览音源，
  //   让「一进来就有原曲」，而不是先响合成节拍音。选过就尊重用户的选择（见 bindProjectAudio）。
  await bindProjectAudio();
  state.tracks_checked = r.default_tracks_checked || [];
  state.sub_checked = r.default_sub_checked || [];
  state.dp_checked = r.default_dp_checked || [];
  state.current_track = r.default_current_track || 0;
  state.song = r.name || '';
  state.offset = 0;
  if (typeof r.default_merge_ms === 'number') setControl('merge_ms', r.default_merge_ms);
  if (r.default_fit_mode) setControl('fit_mode', r.default_fit_mode);
  if (typeof r.default_denoise_on === 'boolean') setControl('denoise_on', r.default_denoise_on);
  lastAutoOffset = null; offsetTouched = false;
  // 换了歌就重新给预览一次机会（上一首如果因 WebGL/失败被拦下，这里解封，等生成完再自动出图）
  previewBlocked = false;
  hidePhNote();
  setControl('song', state.song);
  syncControls();
  player.pause(); player.removeAttribute('src'); player.load();
  lastAudioSig = null;   // 播放器被清空了 ⇒ 下一轮重算要把音源重新挂上（见 syncAudioToSource）
  const sb = $('#src-badge');
  if (sb) sb.textContent = r.is_stem_json ? '来源：时间戳 JSON（分轨）' : r.is_bdg ? '来源：BDG 工程' : r.is_ts ? '来源：毫秒时间戳' : r.is_midi === false ? '来源：?' : '来源：MIDI';
  $('#lbl-file').textContent = `${path}\n${r.stats || ''}`;
  renderTracks(r);
  state.regions = []; selectedRegion = -1;
  state.segments = []; selectedSegment = -1;
  state.segment_mode = 'from';
  overview.clearFloor();
  renderRegions(); renderSegments();
  pageLog('doLoad:onTracksChanged before');
  await onTracksChanged();
  pageLog('doLoad:onTracksChanged ok');
  setStatus(`已加载 ${(r.tracks || []).length} 轨`);
  const lw = r.warning_list || [];
  $('#warnings').textContent = lw.length ? ('⚠ ' + lw.join('；')) : '';
  $('#warn-badge').textContent = lw.length ? `（${lw.length} 条警告）` : '';
  if (lw.length) $('#more').open = true;
  refreshRepBrief();
  reportStatus();
}

function pageLog(step) {
  try { window.dsh && window.dsh.post && window.dsh.post({ type: 'page_log', step: String(step) }); } catch (_) {}
}

// ★ 预览掉帧排查（schedule 第 6 条）：引擎自带采样，每 2 秒打一条
//     `[Perf] frame=12.31ms | renderPlayer=… updatePlayer=… syncVideo=…`
//   （perfAdd/perfTick 按 updatePlayer / syncVideo / renderPlayer / total 分项统计）。
//   真机排查时不必开 DevTools —— 把它转发给宿主落进 gui_debug.log，
//   一眼就能看出瓶颈在 update 还是 render。
//   只转发带 `[Perf]` 前缀的那一条，其它 console 输出一概不碰（别把日志刷爆）。
//   4K 下落式（views/falling.js）走的是自己那条渲染路径，它的耗时也在同一个 total 里。
(function hookPerfLog(){
  if (window.__perfLogHooked) return;
  window.__perfLogHooked = true;
  const orig = console.log;
  console.log = function(){
    try {
      if (arguments.length && typeof arguments[0] === 'string'
          && arguments[0].indexOf('[Perf]') === 0) {
        pageLog(arguments[0]);
      }
    } catch (_e) { /* 转发失败绝不能影响引擎自己打日志 */ }
    return orig.apply(console, arguments);
  };
})();

// ★ 2026-09-20：「导出点了没反应」的教训 —— 这条链路有三处会**静默失败**：
//   ① hasChart 为假 ⇒ 原来只写 setStatus，而报告带默认收起，那行小字等于看不见；
//   ② window.dsh / openDir 不存在（比如页面不在 WebView2 里跑）⇒ 直接抛 TypeError；
//   ③ 宿主那边出错（当天真凶：native_open_dir 抛 ctypes TypeError）⇒ dsh_reply{ok:false}
//      ⇒ 桥把 Promise reject 掉，doExport 里没人接 ⇒ unhandledrejection ⇒ 界面零反馈。
//   现在统一：凡是失败，一律 toast + setStatus，把原因摊到脸上。
async function doExport() {
  try {
    if (!hasChart) {
      toast('还没有生成谱面 —— 先点「重新生成」');
      setStatus('还没有生成谱面。');
      return;
    }
    if (!window.dsh || typeof window.dsh.openDir !== 'function') {
      toast('没有宿主对话框通道（页面不在 WebView2 里跑？）');
      setStatus('⚠ 不能弹目录选择框：window.dsh.openDir 不存在。');
      return;
    }
    setStatus('等待选择导出目录…');
    const dir = await window.dsh.openDir({ title: '选择导出目录（会新建一个曲目文件夹）' });
    if (!dir) { setStatus('已取消导出（没有选目录）'); return; }
    setStatus('导出中…');
    const r = await api.exportTo(state, dir);
    if (!r.ok) { setStatus(`⚠ ${r.error || r.msg || '导出失败'}`); toast('导出失败：' + (r.error || r.msg || '')); return; }
    setStatus(r.msg || `导出到 ${r.dir}`);
    $('#warnings').textContent = r.verify_ok === false ? `⚠ 校验：${r.verify}` : '';
  } catch (e) {
    const m = (e && e.message) ? e.message : String(e);
    setStatus('⚠ 导出失败：' + m);
    toast('导出失败：' + m);
  }
}

// ===================================================== 进度 / 提示
function showProgress(on) { $('#progress').classList.toggle('hidden', !on); }
function setProgress(frac, msg) { $('#pfill').style.width = `${Math.round(Math.max(0, Math.min(1, frac)) * 100)}%`; if (msg) $('#ptext').textContent = msg; }
// 报告文本：长句子按「；」断行，免得挤成一坨、看着像系统报错
function setStatus(s) {
  const t = String(s == null ? '' : s);
  $('#status').textContent = t.length > 90 ? t.replace(/；\s*/g, '；\n') : t;
  refreshRepBrief();               // 收起时标题栏那行摘要跟着更新
}
let toastTimer = null;
function toast(msg) { const t = $('#toast'); if (!t) return; t.textContent = msg; t.classList.add('on'); clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.remove('on'), 1900); }

// ===================================================== 绑定
// ===================================================== BDG 编辑器桥面板（docs/38）
// 「投射到编辑器」把音点按角色写进 BDG；「收回改动」把编辑器里改过的进度（删/挪/加）按
// 毫秒拿回来，钉成采音结果后重算。sidecar 已提供 /api/bridge* 与 /api/host* 全套端点。
let brState = null, hostState = null, lastBackRun = null;

function brSetDot(cls, title) {
  const d = $('#br-dot');
  if (!d) return;
  d.className = 'bdot' + (cls ? ' ' + cls : '');
  d.title = title || '';
}

function buildBridge() {
  const root = el('div', 'bridge'); root.id = 'bridge';
  root.innerHTML =
    '<div class="bhead">' +
      '<span class="btitle">BDG 编辑器桥</span>' +
      '<span id="br-dot" class="bdot" title="未连接">○</span>' +
      '<span id="br-url" class="burl" title="连接串（贴进 BDG 面板）"></span>' +
      '<button id="br-copy" class="ghost mini" title="复制连接串">⧉</button>' +
    '</div>' +
    '<div class="brow">' +
      '<button id="br-host" data-ico="link" title="把 BDG 宿主拉起来，并把连接串推进它的插件面板（一键）">启动并桥接</button>' +
      '<button id="br-hoststop" class="ghost" title="停掉我们起的那个宿主进程">■ 停止宿主</button>' +
    '</div>' +
    '<div class="brow">' +
      '<button id="br-push" title="把我们当前的音点按角色投射进 BDG 编辑器">投射到编辑器</button>' +
      '<button id="br-pull" title="收回编辑器里的改动（对账：留下/移动/删除/新增）">收回改动</button>' +
    '</div>' +
    '<div id="br-hoststat" class="bstat"></div>' +
    '<div id="br-status" class="bstat">（未检查）</div>' +
    '<div id="back-box" class="bstat hidden"></div>' +
    '<div class="brow">' +
      '<button id="br-backuse" class="ghost mini" title="用收回的轨道重算谱面（音轨 = BDG 里带时值数据的那些轨）">用收回的轨道重建</button>' +
      '<button id="br-backclear" class="ghost mini" title="清掉收回的轨道，回到「从选轨采音」">清掉收回</button>' +
    '</div>';
  const host = $('#panel-left');
  if (host) host.appendChild(root);
}

// ★ 看一眼桥连接状态（只读），并级联刷新「收回的轨道」框
async function refreshBridge() {
  const box = $('#br-status');
  if (!box) return null;
  let j = null;
  try { j = await api.bridge(false); } catch (_e) { j = null; }
  if (!j || !j.ok) {
    $('#br-url').textContent = '（sidecar 没应答）';
    brSetDot('', 'sidecar 没应答');
    box.textContent = '（sidecar 没应答）'; box.className = 'bstat bad';
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
  if (st.projects) bits.push(`已收到 ${st.projects} 次工程（${st.n_tracks} 轨 / ${st.n_points} 点）`);
  if (st.sent_n) bits.push(`已投射 ${st.sent_n} 点（批次 ${st.run || '—'}）`);
  if (st.last_warn) bits.push('⚠ ' + st.last_warn);
  box.textContent = bits.join('\n');
  box.className = 'bstat' + (on ? '' : ' bad');
  await refreshBack();
  return j;
}

// ★ 收回的轨道项目（docs/45）：BDG 面板上那个按钮一按，这里要跟着出现
async function refreshBack() {
  const box = $('#back-box');
  if (!box) return null;
  let r = null;
  try { r = await api.bridgeBack(); } catch (_e) { r = null; }
  if (!r || !r.ok || !r.meta || !(r.meta.n_lanes)) {
    box.classList.add('hidden');
    return null;
  }
  const m = r.meta;
  const lines = [`⤴ 已收回 ${m.n_lanes} 条音轨 / ${m.n_points} 点`
    + `（合并成 ${m.n_onsets} 个 onset${m.n_dp ? ' + 双押 ' + m.n_dp : ''}）`];
  if (!m.has_file) lines.push('⚠ 还没加载 MIDI/OGG ⇒ 现在重建不了，先加载源文件');
  for (const l of (m.lanes || []).slice(0, 8)) {
    lines.push(`　· ${(l.name || '?').slice(0, 22)}　${l.role}　${l.n} 点　${(l.ms_lo || 0).toFixed(0)}~${(l.ms_hi || 0).toFixed(0)}ms`);
  }
  if ((m.lanes || []).length > 8) lines.push(`　… 还有 ${m.lanes.length - 8} 条`);
  lines.push('★ 现在「② 主轨」列表里的就是这些轨');
  box.textContent = lines.join('\n');
  box.classList.remove('hidden');
  if (r.info) { loadInfo = Object.assign({}, loadInfo, r.info); renderTracks(loadInfo); }
  if (lastBackRun !== m.run) { lastBackRun = m.run; await rebuild(); }
  return r;
}

async function doBridgePush() {
  if (!loadInfo) { setStatus('先加载文件（BDG 工程 / MIDI / 时间戳）。'); return; }
  const st = (brState && brState.state) || {};
  if (!st.connected || !st.authed) {
    setStatus('⚠ BDG 桥没连上：先在 BDG 里打开插件面板并点连接。');
    await refreshBridge(); return;
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
  const d = r.diff || {}, c = d.counts || {}, ad = r.adopted || {};
  setStatus(`收回 ${ad.n || 0} 点：留下 ${c.kept || 0} · 移动 ${c.moved || 0}`
    + ` · 换角色 ${c.role_changed || 0} · 删除 ${c.deleted || 0} · 新增 ${d.n_added || 0} —— 已按编辑器版本重算`);
  await rebuild(); await refreshBridge();
}

async function doBridgeBackUse() {
  if (!loadInfo) { setStatus('先加载文件（BDG 工程 / MIDI / 时间戳）。'); return; }
  const r = await api.bridgeBackApply(null);
  if (!r.ok) { setStatus('⚠ ' + (r.error || '没有可用的收回数据')); return; }
  lastBackRun = null; await refreshBack();
  setStatus('已用收回的轨道重建（音轨 = BDG 里带时值数据的那些轨）');
}

async function doBridgeBackClear() {
  const r = await api.bridgeBackClear();
  if (r.info && loadInfo) { loadInfo = Object.assign({}, loadInfo, r.info); renderTracks(loadInfo); }
  lastBackRun = null;
  const bb = $('#back-box'); if (bb) bb.classList.add('hidden');
  await rebuild();
  setStatus('已清掉收回的轨道（回到从选轨采音）');
}

// ★ 宿主（BDG）状态：装没装 / 在不在跑 / 调试端口通不通
async function refreshHost() {
  const box = $('#br-hoststat');
  if (!box) return null;
  let j = null;
  try { j = await api.host(); } catch (_e) { j = null; }
  if (!j || !j.ok) {
    box.textContent = '（问不到宿主状态）'; box.className = 'bstat bad'; return null;
  }
  hostState = j;
  const bits = [];
  if (j.tool === false) {
    bits.push('○ 本便携版不含 BDG 宿主（GPL-3.0 的独立软件，未随包分发）');
    bits.push('   ⇒ 要用「编辑器联动」：自己装一份 Beat Data Generator，');
    bits.push('      把包内 bridge_plugin 放进它的插件目录，再把上面连接串贴进「ADO 谱面桥」面板');
    bits.push('   （不装也不影响其它功能：出谱 / 导出 / 预览都照常）');
  } else if (!j.present) {
    bits.push('○ BDG 宿主还没装 ⇒ 先装好宿主');
  } else if (j.starting) {
    bits.push('◐ 正在启动宿主…（进度见下）');
  } else if (j.pid_alive || j.cdp_up) {
    bits.push('● BDG 宿主在跑' + (j.pid ? `（pid ${j.pid}）` : '（不是本程序起的）'));
    if (!j.cdp_up) bits.push(`   ⚠ 调试端口 ${j.port} 不通 ⇒ 没法自动推连接串，请手动贴`);
  } else {
    bits.push('○ BDG 宿主没在跑 ⇒ 点上面的「▶ 启动并桥接」');
  }
  box.textContent = bits.join('\n');
  box.className = 'bstat' + (j.tool === false ? '' : (j.present && (j.pid_alive || j.cdp_up) ? '' : ' bad'));
  const b = $('#br-hoststop'); if (b) b.disabled = !j.pid;
  const bs = $('#br-host');
  if (bs) {
    bs.disabled = j.tool === false;
    bs.title = j.tool === false
      ? '本便携版不含 BDG 宿主；装好宿主后请把连接串手动贴进插件面板'
      : '把 BDG 宿主拉起来，并把连接串推进它的插件面板（一键）';
  }
  return j;
}

async function doHostStart() {
  if (hostState && hostState.tool === false) {
    setStatus('本便携版不含 BDG 宿主 —— 自己装好 BDG 后，把包内 bridge_plugin 放进它的插件目录，再把连接串手动贴进面板');
    return;
  }
  const r = await api.hostStart();
  if (!r.ok) { setStatus('⚠ ' + (r.error || '启动失败')); await refreshHost(); return; }
  setStatus('正在启动 BDG 宿主并桥接…（进度见桥下面那行）');
  await refreshHost();
}

async function doHostStop() {
  const r = await api.hostStop();
  if (!r.ok) { setStatus('⚠ 停宿主失败：' + (r.error || '')); await refreshHost(); return; }
  setStatus(r.stopped ? '已停掉我们起的 BDG 宿主' : `（没停：${r.note || '这个宿主不是本程序起的'}）`);
  await refreshHost(); await refreshBridge();
}

function bindBridge() {
  const on = (id, fn) => { const e = $('#' + id); if (e) e.onclick = fn; };
  on('br-push', () => doBridgePush());
  on('br-pull', () => doBridgeAdopt());
  on('br-backuse', () => doBridgeBackUse());
  on('br-backclear', () => doBridgeBackClear());
  on('br-host', () => doHostStart());
  on('br-hoststop', () => doHostStop());
  on('br-copy', async () => {
    const u = (brState && brState.url) || ($('#br-url') && $('#br-url').textContent) || '';
    if (!u) return;
    try { await navigator.clipboard.writeText(u); setStatus('连接串已复制'); } catch (_e) { setStatus('复制失败：' + u); }
  });
}

function bind() {
  $('#btn-rebuild').onclick = () => rebuild(true);
  $('#btn-export').onclick = () => doExport();
  $('#btn-play').onclick = () => togglePlay();
  $('#btn-about').onclick = () => showAbout();
  $('#m-close').onclick = () => hideModal();
  const modal = $('#modal');
  if (modal) modal.onclick = (e) => { if (e.target === modal) hideModal(); };
  $('#pcancel').onclick = () => api.cancel();
  // ★ 音量滑块必须同时打到「引擎那条链」（谱面页听的是引擎，不是 #player）——见 applyVolume。
  $('#vol').oninput = () => { applyVolume(currentVol()); };
  $('#slider').oninput = () => { const dur = (player.duration || 0) * 1000; if (dur > 0) $('#lbl-time').textContent = `${fmt(Number($('#slider').value) / 1000 * dur)} / ${fmt(dur)}`; };
  $('#slider').onchange = () => { const dur = (player.duration || 0) * 1000; if (dur > 0) { const t = audioToGrid(Number($('#slider').value) / 1000 * dur); seek(t); } };
  player.onplay = () => { setPlayBtn(true); if (activeTab !== 'chart' && !uiTimer) uiTimer = setInterval(syncFromAudio, 100); };
  player.onpause = () => { setPlayBtn(false); if (uiTimer) { clearInterval(uiTimer); uiTimer = null; } };
  player.onended = () => { setPlayBtn(false); if (uiTimer) { clearInterval(uiTimer); uiTimer = null; } };
  player.onloadedmetadata = () => refreshTime();
  player.ontimeupdate = () => { if (!uiTimer) syncFromAudio(); };
  player.onseeked = () => syncFromAudio();
  for (const b of document.querySelectorAll('.tab')) b.onclick = () => setTab(b.dataset.tab);
  if (window.dsh && window.dsh.onMenu) window.dsh.onMenu((name) => { if (name === 'open') doOpen(); else if (name === 'export') doExport(); else if (name === 'rebuild') rebuild(); });
  if (window.dsh && window.dsh.onSidecarDown) window.dsh.onSidecarDown((d) => setStatus(`⚠ Python sidecar 已退出（code=${d.code}）`));
  bindBridge();
  bindSplits();                    // ★ 分隔条可拖动（原来漏了，见 bindSplits 注释）
  // 报告带：点标题栏 = 收起 / 展开（默认收起；展开高度拖它上面的分隔条）
  const repHead = $('#rep-head');
  if (repHead) repHead.onclick = () => setReportCollapsed(!reportCol);
  const dens = $('#density');
  if (dens) dens.onchange = () => { document.documentElement.dataset.density = dens.value; };
  const brt = $('#ph-retry');
  if (brt) brt.onclick = () => retryPreview();
  document.addEventListener('keydown', (e) => {
    const tag = (e.target && e.target.tagName) || '';
    // ★ Ctrl+Shift+A = 开关「音源与偏移」浮层。**放在「输入框里不响应」那道闸之前**：
    //   音源就绪时那颗药丸是收起来的（主人 2026-09-21 裁定），这里是它唯一的显式入口，
    //   不能因为焦点恰好在某个输入框里就失灵。
    if ((e.ctrlKey || e.metaKey) && e.shiftKey && (e.key === 'a' || e.key === 'A')) {
      e.preventDefault(); toggleAvPop(null); return;
    }
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return;
    // Esc：关模态 / 关浮层 / 取消选中
    if (e.key === 'Escape') { hideModal(); closeAvPop(); return; }
    const ctrl = e.ctrlKey || e.metaKey;
    if (ctrl) {
      const n = { '1': 'chart', '2': 'roll', '3': 'path', '4': 'falling' }[e.key];
      if (n) { e.preventDefault(); setTab(n, true); return; }
      if (e.key === 'o' || e.key === 'O') { e.preventDefault(); doOpen(); return; }
      if (e.key === 's' || e.key === 'S') { e.preventDefault(); doExport(); return; }
      if (e.key === 'r' || e.key === 'R') { e.preventDefault(); rebuild(); return; }
      return;
    }
    const playing = !player.paused;
    if (e.code === 'Space') { e.preventDefault(); togglePlay(); }
    else if (e.code === 'ArrowRight') { e.preventDefault(); if (playing) seek((player.currentTime + 5) * 1000); }
    else if (e.code === 'ArrowLeft') { e.preventDefault(); if (playing) seek(Math.max(0, player.currentTime - 5) * 1000); }
    else if (e.code === 'Home') { e.preventDefault(); seek(0); }
    else if (e.code === 'End') { e.preventDefault(); seek((player.duration || 0) * 1000); }
  });
}

// ===================================================== 参数说明 / 关于（还原Adofai-Chart-Generator「?」帮助按钮 + 帮助→关于/参数说明 原生菜单）
const ABOUT = `参数说明 / 关于
====================
版本：ADOFAI 工作台（Mica 原生壳） · 渲染引擎 v0.4.6 · 基于Adofai-Chart-Generator 算法

快捷键：
  Ctrl+O      打开 MIDI / 音频 / BDG 工程 / 时间戳
  Ctrl+S      导出谱面
  Ctrl+R      重新生成
  Ctrl+1..4   切换 谱面预览 / 钢琴卷帘 / 谱面路径 / 4K下落式
  F12         开发者工具
  空格        播放 / 暂停
  播放时 ← →  前后 5 秒；Home / End  音频首 / 尾
  暂停时 ← →  逐格（上一格 / 下一格）；Home / End  首 / 尾格
  Esc         关闭本窗口 / 取消选中

左栏（来源与段）：
  ① 文件   — 打开歌曲，显示 格式 / ppqn / 轨数 / 时长
  ② 主轨   — 勾选要采音的轨（可多选，取并集）；双押轨单独勾
             区间采音：在底部「全曲预览条」上 Shift+拖动 框选
             分段采音：在底部「全曲预览条」上双击切分段

右栏（参数，schema 驱动，与引擎字段一一对应；右栏已拆两半，左半是检查器、右半是「③b 采bpm · 大直线」面板）：
  ③ 采音    合并窗口 / 合并取点 / 最小力度 / 最小间隔 / 最大音数 / 音高范围
  ④ 求解    自动基准BPM（勾上时「基准 BPM」只是显示回填，真正基准由求解器内部再算）
             直线优先 λ：少=3.0 / 平衡=1.5 / 多=0.7
             Twirl 阈值 / Pause 阈值 含义同Adofai-Chart-Generator原版
  ③b 采bpm · 大直线（右栏右半，可「浮起」为独立窗口）= 把区间内硬铺成 N 砖/拍的等间隔骨架
  ⑤ 时序    offset：谱面 t=0 对应的音频时刻；勾「自动」= 首个 onset
  左栏底部「BDG 编辑器桥」：把音点投射进 Beat Data Generator / 收回编辑器里的改动重算

四视图：
  谱面预览 = ADOFAI 官方渲染引擎（WebGL，打开自动出图）
  钢琴卷帘 / 谱面路径 / 4K下落式 = 2D 视图，不依赖 WebGL
  全曲预览条（底部）：点击/拖动 定位并选中；滚轮 缩放；中键 平移；双击 恢复全曲`;

function showAbout() {
  const t = $('#m-title'), b = $('#m-body'), m = $('#modal');
  if (t) t.textContent = '参数说明 / 关于';
  if (b) b.textContent = ABOUT;
  if (m) m.classList.remove('hidden');
}
function hideModal() { const m = $('#modal'); if (m) m.classList.add('hidden'); }

api.events((m) => {
  progressEvents += 1;
  if (m.kind === 'progress') setProgress(m.frac, m.msg);
  else if (m.kind === 'loaded') showProgress(false);
});

// ===================================================== 启动
async function main() {
  const h = await api.health();
  const hb = $('#health'); if (hb) hb.classList.toggle('bad', !h.ok);
  SCHEMA = await api.schema();
  if (!SCHEMA || !SCHEMA.fields) { setStatus('⚠ 拿不到参数 schema，sidecar 可能没起来'); return; }
  state = Object.assign({}, SCHEMA.defaults || {});
  state.regions = []; state.segments = []; state.segment_mode = 'from'; state.xk_ranges = [];
  state.show_segments = [];
  // ★ 预览音源：把「上次选的那份」灌回来（必须赶在 buildPanels() 之前，控件才对得上）。
  //   灌上了 ⇒ avPickFromStore=true ⇒ doLoad 里就不再自动接续别的音源。
  try { restoreAvPick(); } catch (_e) {}
  // 早期就持一份音频引用（不自动播，等用户按播放）
  player = $('#player');
  player.volume = 0.7;
  buildPanels();
  applyDpLock(); applyFitLock();
  bind();
  applyReportLayout();             // 报告带：恢复上次的收起态 / 高度（默认收起）
  refreshRepBrief();
  setTab('chart');
  // ★ BDG 桥：载入即拉一次状态 + 每 4 秒轮询（连接/宿主变化异步发生，轮询最稳）
  refreshBridge(); refreshHost();
  setInterval(() => { refreshBridge(); refreshHost(); }, 4000);
  try { const info = await window.dsh.info(); $('#ver').textContent = `v${info.version} · py-sidecar :${info.port}`; } catch (_e) { $('#ver').textContent = 'py-sidecar'; }
  setStatus('就绪');
  const sb = $('#src-badge'); if (sb) sb.textContent = '未加载';
  // 暴露给宿主自动载入（必须先赋值，再通知宿主 ready，避免宿主回调用到未定义的 window.__dsh）
  window.__dsh = {
    load: doLoad, rebuild, export: doExport, tab: setTab, status: reportStatus,
    get state() { return state; },
    // ── 预览音源（e2e 探针用；都走**真实那份**代码，别另写一套）──────────────
    //    就绪判定 / 药丸显示 / 记住的选择，一次报全
    av: () => ({
      mode: Number(state.preview_audio_mode || 0),
      path: String(state.preview_audio_path || ''),
      resolved: String((payload && payload.preview_audio) || ''),
      ready: avAudioReady(),
      pickFromStore: avPickFromStore,
      saved: (() => { try { return localStorage.getItem(AVPICK_KEY); } catch (_e) { return null; } })(),
      pill: (() => {
        const b = $('#btn-av-sum'); if (!b) return null;
        return { hidden: b.classList.contains('hidden'), warn: b.classList.contains('warn'), txt: b.textContent };
      })(),
      bar: (() => {
        const v = $('#vbar-chart'); if (!v) return null;
        return { empty: v.classList.contains('empty'), h: Math.round(v.getBoundingClientRect().height),
                 display: getComputedStyle(v).display };
      })(),
    }),
    avSetMode: (m) => {
      const f = (SCHEMA.view_fields || []).find((x) => x.key === 'preview_audio_mode');
      if (!f || !f._input) return false;
      setVal(f, Number(m));
      return true;
    },
    avPickPath: (p) => {
      const f = (SCHEMA.view_fields || []).find((x) => x.key === 'preview_audio_path');
      if (!f) return false;
      applyPickedPath(f, f._input, String(p));
      return true;
    },
    // 只写 state + 刷药丸显示（不重算、不渲染合成音）—— 给探针单测「就绪规则」用
    avRaw: (o) => {
      if (o && 'mode' in o) state.preview_audio_mode = Number(o.mode);
      if (o && 'path' in o) state.preview_audio_path = String(o.path);
      refreshAvSummary();
      return true;
    },
    avPop: () => toggleAvPop(null),
  };
  // 首帧后量一次画布（此时布局已稳定，flex 尺寸已落定）
  requestAnimationFrame(() => { sizeViews(); requestAnimationFrame(sizeViews); });
  // ★ 通知宿主：页面就绪（宿主收到后会用 window.__dsh.load(midi) 自动载入）
  window.dsh.ready({ port: (window.dsh && (await window.dsh.info().catch(() => ({}))).port) || 0, fields: (SCHEMA.fields || []).length });
}

// ══════════════════════════════════════════════════════════════════════════════
// 自绘下拉框接管（Win11 ComboBox）—— 组件在 shell-ui.js 的 ShellUI.Dropdown，外观见 shell-ui.css ⑦
// ------------------------------------------------------------------------------
// ★ 2026-09-22 主人："工作台里面的下拉选单也换成这个"（= 设置页那套自绘版）。
// 为什么**不**像设置页那样把原生 <select> 整个删掉换自绘触发器：
//   · 外面有代码按 id 取它（`$('#density')`、`$('#sel-seg-mode')`），读 .value、挂 .onchange；
//   · 检查器的字段把控件登记在 f._input 上，setControl / syncControls / applyDpLock /
//     applyFitLock 都直接写它（selectedIndex / value / disabled / title）；
//   · 一部分 select 每次 render 都重建（区间行、演出分段），是"用完就扔"的。
// 所以采用「**隐形 select 当数据源 + 自绘触发器当脸**」：
//   选完写回 select 并派发 change ⇒ 既有逻辑一行都不用改；"谁写值"始终只有 select 一条路，
//   触发器不参与决策，只负责好看。反向（select ➜ 触发器）只在几个值/置灰写入口补 syncDd。
// ══════════════════════════════════════════════════════════════════════════════

/* 尺度档：区间行（.region 里那几个小输入旁边）走 xs，其余走 sm。
   sm = 26px / 12.5px 字 —— 正是原来那批 select 的实测度量，所以换完尺寸不变。 */
function pickDdSize(sel) { return sel.closest && sel.closest('.region') ? 'xs' : 'sm'; }

function destroyDd(sel) {
  const dd = sel && sel._dd;
  if (!dd) return;
  try { dd.destroy(); } catch (_e) { /* 已经拆过了就算了 */ }
  sel._dd = null;
  delete sel.dataset.dd;
}

/* select ➜ 触发器：值 / 置灰 / 提示。幂等，随便多调。
   宿主行被重建后老 select 会脱离文档，顺手在这儿收尸（dd 的菜单挂在 body 上，不 destroy 会攒着）。 */
function syncDd(sel) {
  const dd = sel && sel._dd;
  if (!dd) return;
  if (!sel.isConnected) { destroyDd(sel); return; }
  if (dd.value() !== sel.value) dd.setValue(sel.value);
  if (dd.el.disabled !== !!sel.disabled) dd.el.disabled = !!sel.disabled;
  const t = sel.title || '';
  if (dd.el.title !== t) dd.el.title = t;
}
function syncDdAll() {
  const list = document.querySelectorAll('select[data-dd]');
  for (let i = 0; i < list.length; i++) syncDd(list[i]);
}

/* 把 select 换成「隐形 select + 自绘触发器」。可重复调用：已升过的跳过、空清单跳过。 */
function upgradeSelects(root) {
  if (!window.ShellUI || !ShellUI.Dropdown) return 0;
  const list = !root ? document.querySelectorAll('select')
    : root.tagName === 'SELECT' ? [root]
      : root.querySelectorAll('select');
  let n = 0;
  for (const sel of list) {
    if (sel.dataset.dd || sel.multiple || sel.size > 1) continue;
    /* 🔴 **必须**跳过已脱离文档的控件。观察器会把"同一批里加了又删"的节点也作为 addedNodes
       交出来（render 连调两次就是这样），那种 sel 的 parentNode 已经是 null；
       给它接管的唯一后果是抛异常（见 ShellUI.Dropdown.create 的注释），且报错点毫无线索。
       跳过是安全的：它真被插进文档时观察器会再响一次，那时再接。 */
    if (!sel.isConnected) continue;
    const opts = [...sel.options].map((o) => ({ value: o.value, label: o.textContent }));
    if (!opts.length) continue;              // 空清单：留着原生控件（等补了选项、下次扫到再接）
    const dd = ShellUI.Dropdown.create({
      mount: sel,
      keepMount: true,
      size: pickDdSize(sel),
      onChange: (v) => {
        // 🔴 只"写回 + 派发"，不直接调业务函数 —— 既有 onchange / change 监听照旧跑，
        //    新增一条捷径就等于给同一件事开第二条路，将来必分叉。
        sel.value = v;
        sel.dispatchEvent(new Event('change', { bubbles: true }));
      },
    });
    // 第二道防线：组件那边万一又退化成"交出半成品"，这里也别把异常甩给业务代码。
    if (!dd || typeof dd.setOptions !== 'function') continue;
    sel.dataset.dd = '1';
    dd.setOptions(opts);
    dd.setValue(sel.value);
    sel._dd = dd;
    syncDd(sel);                             // 置灰 / 提示一并抬过去
    n++;
  }
  return n;
}

/* 动态重建的行（区间行 / 演出分段每次 render 都重造）⇒ 用观察器兜住新来的 select，
   同时给被移除的收尸。
   🔴 回调里要往 DOM 插触发器 = 又制造 childList 变更 ⇒ 处理期间先 disconnect、完事再 observe，
      否则就是"改→回调→再改"的自激（这个坑本项目踩过一次，主线程会被占死）。 */
const ddMo = new MutationObserver((recs) => {
  const add = [], del = [];
  for (const r of recs) {
    for (const n of r.addedNodes) if (n.nodeType === 1) add.push(n);
    for (const n of r.removedNodes) if (n.nodeType === 1) del.push(n);
  }
  if (!add.length && !del.length) return;    // 菜单插到 body 之类：不是 select，早退
  ddMo.disconnect();
  try {
    for (const n of del) {
      const hits = n.tagName === 'SELECT' ? [n] : (n.querySelectorAll ? n.querySelectorAll('select[data-dd]') : []);
      for (const s of hits) destroyDd(s);
    }
    for (const n of add) upgradeSelects(n);
  } finally {
    ddMo.observe(document.body, { childList: true, subtree: true });
  }
});

upgradeSelects();                                       // 静态 HTML 里的那批（如顶栏「密度」）
ddMo.observe(document.body, { childList: true, subtree: true });

main();
