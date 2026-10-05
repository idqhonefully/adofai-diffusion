/* ==========================================================================
   layout.js —— 停靠系统 + Shift+M 布局编辑模式 + 脱手即存（docs/49 §5.3）
   --------------------------------------------------------------------------
   四个 pane：#left（来源与段）· #right（检查器）· #report（报告带）· #fl-xk（大直线浮窗）
   四个停靠槽：#dock-{left,right,top,bottom}（各放一个，空槽自动收起）
   拖 pane 到边缘 ⇒ 停靠；拖到中间 ⇒ 变浮窗；槽被占 ⇒ 原住户自动变浮窗（内容不丢）
   ★ 布局存**真文件** <userData>/ui-layout.json（经 preload 的 layoutGet/layoutSet；
     拿不到（比如浏览器直开）就退回 localStorage）
   ★ Shift+M 进入移动模式：**全 UI 冻结**（点击穿透到 pane = 拖整块），
     只留 4 个活口：顶栏「移动模式」按钮 / 横幅「退出」/ Esc·Shift+M / 分隔条
   ========================================================================== */

export const PANE_IDS = ['left', 'right', 'report', 'flx'];
const PANE_SEL = { left: '#left', right: '#right', report: '#report', flx: '#fl-xk' };
const SIDE_DOCK = { left: '#dock-left', right: '#dock-right',
                    top: '#dock-top', bottom: '#dock-bottom' };
const LKEY = 'adofai-ui-layout';

export const DEF_LAYOUT = {
  v: 2, zoom: 1, density: 'normal', bandH: 150,
  groups: {},                    // 参数分组展开态
  presets: {},                   // 预设快照（后续用）
  panes: {
    // ★ 增量改造（2026-09-19）：左右宽度对齐我方工作台（312 / 380）
    left:   { dock: 'left',   w: 312 },
    right:  { dock: 'right',  w: 380 },
    // ★ 增量改造：报告带**默认收起**（只剩标题栏一行摘要 ⇒ 预览区多出 ~90px）
    //   h=0 ⇒ 高度自适应；收起时高度交给内容撑（就是标题栏本身），
    //   所以不写死像素，改字号/密码度都不会把它裁掉。
    //   ⚠ 老配置（已存过 ui-layout.json 的）没有 collapsed 字段 ⇒ 保持展开，
    //     不搞静默迁移；点一下标题栏就收起来，之后就会记住。
    report: { dock: 'bottom', h: 0, collapsed: true },
    flx:    { dock: null, x: 120, y: 44, w: 340, h: 250, collapsed: false, open: true },
  },
};

const clampW = (v) => Math.max(240, Math.min(620, Math.round(v)));
const clampH = (v) => Math.max(180, Math.min(620, Math.round(v)));
const cap = (el, id) => { try { el.setPointerCapture(id); } catch (_e) { /* 合成事件没有真 pointerId */ } };

let L = null;
let layoutMode = false;
let saveTimer = null;
let toastFn = () => {};

export const layout = {
  get L() { return L; },
  get mode() { return layoutMode; },
  setToast(fn) { toastFn = fn; },
  setMode: (on) => setLayoutMode(on),
  apply: () => applyLayout(),
  save: (msg) => saveLayout(msg),        // 程序性改了 L 之后显式落盘（e2e / DevTools 用）
};

const q = (s) => document.querySelector(s);
const paneEl = (k) => document.getElementById(PANE_SEL[k].slice(1));

/* ------------------------------------------------------------ 加载 / 保存 */
async function loadLayout() {
  // ① 优先走真文件（主进程）；② 拿不到就 localStorage；③ 再不行用默认
  try {
    if (window.dsh && window.dsh.layoutGet) {
      const r = await window.dsh.layoutGet();
      if (r && r.ok && r.n === 2) return r.layout;
    }
  } catch (_e) { /* 落到下一档 */ }
  try {
    const s = JSON.parse(localStorage.getItem(LKEY) || 'null');
    if (s && s.v === 2) return s;
  } catch (_e) { /* 用默认 */ }
  return structuredClone(DEF_LAYOUT);
}
function saveLayout(msg) {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    const data = JSON.stringify(L);
    try { localStorage.setItem(LKEY, data); } catch (_e) { /* 无所谓 */ }
    if (window.dsh && window.dsh.layoutSet) window.dsh.layoutSet(data);
    if (msg !== false) toastFn('布局已保存（下次打开复现）');
  }, 400);                       // ★ 「脱手后存」：拖动中不写盘
}

/* ------------------------------------------------------------ 停靠 */
function mount(k) {
  const P = L.panes[k], el = paneEl(k);
  if (!el) return;
  if (P.dock) {
    const host = q(SIDE_DOCK[P.dock]);
    if (host && el.parentNode !== host) host.appendChild(el);
    el.classList.add('docked', 'pane');
    el.classList.remove('floating', 'float');
    el.style.left = el.style.top = el.style.right = el.style.bottom = '';
  } else {
    const app = document.getElementById('app');
    if (el.parentNode !== app) app.appendChild(el);
    el.classList.add('floating', 'pane', 'float');
    el.classList.remove('docked');
    el.style.display = P.open === false ? 'none' : 'flex';
  }
}
function clampFloat(k) {
  const P = L.panes[k];
  if (P.dock) return;
  const z = L.zoom || 1;
  // ★ 浮窗的「家」= **中间那块视图预览区**（`#stage`，不含标签栏）——
  //   按视口左上角放会压在左栏上；按 `#center` 放会压住标签栏（截图里都踩到过）。
  const c = document.getElementById('stage');
  const r = c ? c.getBoundingClientRect()
    : { left: 0, top: 80, right: innerWidth, bottom: innerHeight };
  const x0 = r.left / z + 24, y0 = r.top / z + 12;
  const x1 = r.right / z - 24, y1 = r.bottom / z - 40;
  P.x = Math.round(Math.max(x0, Math.min(x1 - P.w, P.x)));
  P.y = Math.round(Math.max(y0, Math.min(y1, P.y)));
}
function dockPane(k, side) {
  const P = L.panes[k], el = paneEl(k), z = L.zoom || 1;
  if (side && SIDE_DOCK[side]) {
    for (const o of PANE_IDS) {                      // 槽被占 ⇒ 原住户变浮窗（不丢）
      if (o !== k && L.panes[o].dock === side) {
        const oe = paneEl(o), b = oe.getBoundingClientRect();
        Object.assign(L.panes[o], { dock: null,
          x: Math.round(b.left / z), y: Math.round(b.top / z),
          w: clampW(b.width / z), h: clampH(b.height / z) });
        mount(o);
      }
    }
    q(SIDE_DOCK[side]).appendChild(el);
    P.dock = side;
  } else {
    P.dock = null;
    mount(k);
  }
  applyLayout();
}

/* ------------------------------------------------------------ 应用 */
export function applyLayout() {
  const z = L.zoom || 1, app = document.getElementById('app');
  app.style.zoom = z;
  // ★ CSS zoom 会把 height:100% 一起放大 ⇒ 按比例缩回，免得撑出滚动条
  app.style.height = (100 / z) + '%';
  app.style.width = (100 / z) + '%';
  document.documentElement.dataset.density = L.density || 'normal';
  const dens = document.getElementById('density');
  if (dens) dens.value = L.density || 'normal';
  document.getElementById('tlwrap').style.height = Math.round(L.bandH || 150) + 'px';
  for (const k of PANE_IDS) {
    const P = L.panes[k], el = paneEl(k);
    if (!el) continue;
    mount(k);
    if (P.dock === 'left' || P.dock === 'right') {
      el.style.width = Math.round(P.w) + 'px'; el.style.height = '100%';
    } else if (P.dock === 'top' || P.dock === 'bottom') {
      // ★ 增量改造：停靠在上下时也支持「收起」——收起 = **不设高度**，交给内容撑。
      //   CSS 里 `.pane.docked.collapsed > *:not(.zhead):not(.fhead)` 已把内容藏掉，
      //   于是一整块只剩标题栏那一行（不写死像素，改字号/密度都不会裁掉它）。
      el.classList.toggle('collapsed', !!P.collapsed);
      el.style.height = P.collapsed ? '' : ((P.h || 0) > 0 ? Math.round(P.h) + 'px' : '');
      el.style.width = '100%';
    } else {
      el.style.left = Math.round(P.x) + 'px'; el.style.top = Math.round(P.y) + 'px';
      el.style.width = Math.round(P.w) + 'px'; el.style.height = Math.round(P.h) + 'px';
      el.classList.toggle('collapsed', !!P.collapsed);
    }
  }
  for (const s of ['left', 'right', 'top', 'bottom']) {
    const d = q(SIDE_DOCK[s]);
    if (d) d.classList.toggle('empty', !PANE_IDS.some((k) => L.panes[k].dock === s));
  }
  const sp = (s) => q(`.split[data-split=${s}]`);
  if (sp('left')) sp('left').style.display = L.panes.left.dock === 'left' ? '' : 'none';
  if (sp('right')) sp('right').style.display = L.panes.right.dock === 'right' ? '' : 'none';
  // ★ 增量改造：报告带收起时把那条分隔条也藏掉（收起态拖它没有意义，
  //   还白占 5px、更会让人以为「拖了没反应」）
  if (sp('bottom')) sp('bottom').style.display =
    (L.panes.report.dock === 'bottom' && !L.panes.report.collapsed) ? '' : 'none';
  // ★ 增量改造：报告带的收起态在**任何停靠位置**都生效 + 同步标题栏箭头
  const repEl = document.getElementById('report');
  if (repEl) repEl.classList.toggle('collapsed', !!L.panes.report.collapsed);
  const rc = document.getElementById('rep-caret');
  if (rc) rc.textContent = L.panes.report.collapsed ? '▸' : '▾';
}

/* ------------------------------------------------------------ 移动模式 */
function hud(s) {
  const el = document.getElementById('lmhud');
  if (el) el.textContent = s || ('拖块到「左/右/上/下」边缘 = 停靠\n拖到中间 = 变成浮窗\n'
    + '滚轮 = 缩放这一块\nCtrl+滚轮 = 整体缩放\nEsc / Shift+M 退出 · 松手自动存');
}
export function setLayoutMode(on) {
  layoutMode = !!on;
  document.body.classList.toggle('layoutmode', layoutMode);
  const b = document.getElementById('lmbanner');
  if (b) b.classList.toggle('on', layoutMode);
  const btn = document.getElementById('btn-layout');
  if (btn) btn.classList.toggle('primary', layoutMode);
  hud('');
  if (layoutMode) toastFn('移动模式：内容已冻结（点不动）；拖块到边缘=停靠，拖到中间=浮动');
}

const sideAt = (x, y) => {
  const W = innerWidth, H = innerHeight;
  if (x < W * 0.15) return 'left';
  if (x > W * 0.85) return 'right';
  if (y < H * 0.14) return 'top';
  if (y > H * 0.86) return 'bottom';
  return 'float';
};

/* ------------------------------------------------------------ 交互 */
export function initLayout(hooks = {}) {
  const onChange = hooks.onChange || (() => {});
  const dz = document.createElement('div');
  dz.id = 'dzhint';
  dz.innerHTML = ['left', 'right', 'top', 'bottom', 'float']
    .map((s) => `<i data-side="${s}"></i>`).join('');
  document.body.appendChild(dz);
  const showDz = (side) => { dz.classList.add('on');
    dz.querySelectorAll('i').forEach((i) => i.classList.toggle('on', i.dataset.side === side)); };
  const hideDz = () => { dz.classList.remove('on');
    dz.querySelectorAll('i').forEach((i) => i.classList.remove('on')); };

  // —— pane 拖动 / 缩放（只在移动模式）
  for (const k of PANE_IDS) {
    const el = paneEl(k);
    if (!el) continue;
    el.addEventListener('pointerdown', (e) => {
      if (!layoutMode) return;
      if (e.target.closest('#lmbanner, #top, #dzhint')) return;
      e.preventDefault(); e.stopPropagation();
      cap(el, e.pointerId);
      const P = L.panes[k], b = el.getBoundingClientRect(), z0 = L.zoom || 1;
      const x0 = e.clientX, y0 = e.clientY;
      const resizing = Math.abs(x0 - b.right) <= 8 || Math.abs(y0 - b.bottom) <= 8;
      const nearR = Math.abs(x0 - b.right) <= 8, nearB = Math.abs(y0 - b.bottom) <= 8;
      const snap = { x: Math.round(b.left / z0), y: Math.round(b.top / z0),
                     w: clampW(b.width / z0), h: clampH(b.height / z0), dock: P.dock };
      let moved = false;
      el.classList.add('dragging');
      const move = (ev) => {
        const z = L.zoom || 1;
        const dx = (ev.clientX - x0) / z, dy = (ev.clientY - y0) / z;
        if (Math.abs(dx) + Math.abs(dy) > 4) moved = true;
        if (resizing) {
          if (nearR && !P.dock) { P.w = Math.max(200, snap.w + dx); hud(`宽 ${Math.round(P.w)}px`); }
          else if (nearR) { P.w = Math.max(200, snap.w + dx); hud(`宽 ${Math.round(P.w)}px`); }
          if (nearB) {
            if (P.dock === 'top' || P.dock === 'bottom') { P.h = Math.max(34, snap.h + dy); }
            else if (!P.dock) { P.h = Math.max(90, snap.h + dy); }
            hud(`高 ${Math.round(P.h)}px`);
          }
        } else {
          if (!moved) return;
          if (P.dock !== null) Object.assign(P, { dock: null, x: snap.x, y: snap.y,
                                                  w: snap.w, h: snap.h });
          P.x = Math.round(snap.x + dx); P.y = Math.round(snap.y + dy);
          const side = sideAt(ev.clientX, ev.clientY);
          showDz(side);
          hud('→ ' + ({ float: '浮动（放到哪算哪）', left: '停靠到左边', right: '停靠到右边',
                        top: '停靠到上边', bottom: '停靠到下边' }[side]));
        }
        applyLayout(); onChange();
      };
      const up = (ev) => {
        el.classList.remove('dragging'); hideDz();
        if (moved && !resizing) {
          const side = sideAt(ev.clientX, ev.clientY);
          if (side === 'float') { L.panes[k].dock = null; clampFloat(k); }
          else dockPane(k, side);
        }
        applyLayout(); saveLayout(); onChange(); hud('');
        window.removeEventListener('pointermove', move);
        window.removeEventListener('pointerup', up);
      };
      window.addEventListener('pointermove', move);
      window.addEventListener('pointerup', up);
    });
    // 滚轮：缩放这一块 / Ctrl+滚轮：整体缩放
    el.addEventListener('wheel', (e) => {
      if (!layoutMode) return;
      e.preventDefault(); e.stopPropagation();
      const P = L.panes[k];
      if (e.ctrlKey) {
        L.zoom = Math.max(0.7, Math.min(1.8, (L.zoom || 1) * (e.deltaY < 0 ? 1.06 : 1 / 1.06)));
        applyLayout(); onChange(); hud(`整体缩放 ×${L.zoom.toFixed(2)}`); saveLayout(false);
        return;
      }
      const f = e.deltaY < 0 ? 1.06 : 1 / 1.06;
      if (P.dock === 'left' || P.dock === 'right') P.w = Math.max(200, Math.min(660, P.w * f));
      else if (P.dock === 'top' || P.dock === 'bottom') P.h = Math.max(34, Math.min(320, (P.h || 60) * f));
      else { P.w = Math.max(200, P.w * f); P.h = Math.max(90, P.h * f); }
      applyLayout(); onChange(); saveLayout(false);
      hud(`${Math.round(P.w)}×${P.h || ''}`);
    }, { passive: false });
  }

  // —— 浮窗的折叠 / 关闭（非移动模式也能点）
  document.querySelectorAll('#fl-xk .x').forEach((b) => b.addEventListener('pointerdown', (e) => {
    if (layoutMode) return;
    e.stopPropagation();
    const P = L.panes.flx;
    if (b.dataset.act === 'collapse') P.collapsed = !P.collapsed;
    else { P.open = false; toastFn('大直线浮窗已关（「恢复默认布局」可再开）'); }
    applyLayout(); saveLayout();
  }));

  // —— 分隔条（非移动模式也能拖）
  const SPLIT_PANE = { left: 'left', right: 'right', bottom: 'report' };
  document.querySelectorAll('.split').forEach((sp) => {
    sp.addEventListener('pointerdown', (e) => {
      const which = sp.dataset.split, x0 = e.clientX, y0 = e.clientY;
      const z = L.zoom || 1;
      if (which === 'tl') {                       // 段带高度
        cap(sp, e.pointerId); sp.classList.add('on');
        const h0 = L.bandH || 150;
        const mv = (ev) => { L.bandH = Math.max(60, Math.min(innerHeight / z - 220,
          h0 - (ev.clientY - y0) / z)); applyLayout(); onChange(); };
        const up = () => { sp.classList.remove('on'); saveLayout();
          window.removeEventListener('pointermove', mv);
          window.removeEventListener('pointerup', up); };
        window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up);
        return;
      }
      const pk = SPLIT_PANE[which], P = pk && L.panes[pk];
      if (!P || !P.dock) return;
      cap(sp, e.pointerId); sp.classList.add('on');
      const w0 = P.w || 0, h0 = P.h || 0;
      const mv = (ev) => {
        const dx = (ev.clientX - x0) / z, dy = (ev.clientY - y0) / z;
        if (which === 'left') P.w = Math.max(200, Math.min(660, w0 + dx));
        else if (which === 'right') P.w = Math.max(200, Math.min(660, w0 - dx));
        else P.h = Math.max(34, Math.min(320, h0 - dy));
        applyLayout(); onChange();
      };
      const up = () => { sp.classList.remove('on'); saveLayout();
        window.removeEventListener('pointermove', mv);
        window.removeEventListener('pointerup', up); };
      window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up);
    });
  });

  // —— ★ 增量改造：分隔条**双击复位**（原来只有拖动，没有「一键回默认」）
  document.querySelectorAll('.split').forEach((sp2) => {
    sp2.addEventListener('dblclick', () => {
      const which = sp2.dataset.split;
      if (which === 'tl') {
        L.bandH = DEF_LAYOUT.bandH;
        toastFn('段带高度已复位');
      } else {
        const pk = SPLIT_PANE[which];
        const P = pk && L.panes[pk];
        if (!P) return;
        if (which === 'bottom') { P.h = 0; toastFn('报告带高度已复位（自适应）'); }
        else {
          P.w = DEF_LAYOUT.panes[pk].w;
          toastFn(`${which === 'left' ? '左栏' : '右栏'}宽度已复位`);
        }
      }
      applyLayout(); saveLayout(false); onChange();
    });
  });

  // —— ★ 增量改造：报告带点标题栏 = 收起 / 展开（默认收起）
  const rhead = document.getElementById('rep-head');
  if (rhead) rhead.addEventListener('click', () => {
    if (layoutMode) return;              // 移动模式下这一下是「拖整块」，别抢
    const P = L.panes.report;
    P.collapsed = !P.collapsed;
    applyLayout(); saveLayout(false);
    toastFn(P.collapsed ? '报告带已收起（点标题栏展开）' : '报告带已展开（拖分隔条调高）');
  });

  // —— 顶栏 / 横幅 / 快捷键
  const btn = document.getElementById('btn-layout');
  if (btn) btn.onclick = () => setLayoutMode(!layoutMode);
  const ex = document.getElementById('lm-exit');
  if (ex) ex.onclick = () => setLayoutMode(false);
  document.getElementById('btn-reset-layout').onclick = () => {
    L = structuredClone(DEF_LAYOUT);
    try { localStorage.removeItem(LKEY); } catch (_e) { /* 无所谓 */ }
    applyLayout(); saveLayout(); onChange();
    toastFn('布局已恢复默认');
  };
  document.getElementById('density').onchange = (e) => {
    L.density = e.target.value; applyLayout(); onChange(); saveLayout(false);
  };
  document.addEventListener('keydown', (e) => {
    const tag = (e.target && e.target.tagName) || '';
    if (/INPUT|SELECT|TEXTAREA/.test(tag)) return;
    if (e.shiftKey && (e.key === 'M' || e.key === 'm')) {
      e.preventDefault(); setLayoutMode(!layoutMode); return;
    }
    if (e.key === 'Escape' && layoutMode) { setLayoutMode(false); return; }
  });
  window.addEventListener('resize', () => { clampAllFloats(); applyLayout(); onChange(); });
}

export function clampAllFloats() { PANE_IDS.forEach(clampFloat); }

/** 启动：装布局 → 应用 → 绑定交互（await 因为布局可能来自真文件） */
export async function bootLayout(hooks = {}) {
  L = await loadLayout();
  applyLayout();
  clampAllFloats();
  applyLayout();
  // ★ 再等一帧：`#center` 的 rect 要等这次布局生效后才量得准
  //   （不然浮窗会停在 y=44 那种"压在标签栏上"的位置 —— 截图里踩到过）
  requestAnimationFrame(() => { clampAllFloats(); applyLayout(); });
  initLayout(hooks);
  if (hooks.onGroups) hooks.onGroups();     // 分组展开态回填（buildPanel 之后调）
  return L;
}

export function groupOpenState() { return (L && L.groups) || {}; }
export function setGroupOpen(g, open) {
  if (!L) return;                       // buildPanel 可能早于 bootLayout（启动顺序）
  (L.groups ||= {})[g] = !!open;
  saveLayout(false);
}
/** 启动时把**存下来的**分组展开态回填到已经建好的分区上
 *  （buildPanel 跑在 bootLayout 之前 —— 那时还读不到布局文件） */
export function applyGroupOpenState() {
  if (!L || !L.groups) return;
  document.querySelectorAll('section.grp').forEach((sec) => {
    const g = sec.dataset.g;
    if (!g) return;
    const open = L.groups[g];
    if (open === undefined) return;
    sec.classList.toggle('collapsed', !open);
    const c = sec.querySelector('h4 .caret');
    if (c) c.textContent = open ? '▾' : '▸';
  });
}
