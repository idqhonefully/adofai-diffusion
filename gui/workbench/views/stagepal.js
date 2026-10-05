/* ==========================================================================
   stagepal.js —— 时间轴（段带 #tlwrap / 全曲条 #ovwrap）的主题调色板
   --------------------------------------------------------------------------
   唯一真相源 = style.css 里的 `--tl-*` 自定义属性（深/浅两套，挂在 html[data-theme]）。
   本模块只干一件事：把那些变量读成 canvas 能吃的颜色字符串。

   为什么要绕这一道：
     canvas 的 fillStyle 不认 CSS 变量，只能吃具体颜色。颜色若写死在 JS 里，
     主题一切就只剩容器底换了、canvas 还是老色 —— 那正是主人这次报的
     「浅色下时间轴还是黑的」。所以底色/栅格/文字这一整套都从 CSS 变量读。

   ⚠ 别在每帧 draw() 里调 getComputedStyle（会有样式重算开销）。
     这里给的是缓存对象：主题切换时调 refreshTlPal() 刷一次，平时直接取。
   ========================================================================== */

// 深色档兜底：CSS 变量读不到时（headless 首帧、样式表未就绪）用它，保证画面不空白。
const DARK = {
  bg: '#0a0d12', axis: '#0d1218', grid: '#1b232c',
  ruler: 'rgba(255,255,255,0.10)', tick: 'rgba(255,255,255,0.28)',
  text: '#8b98a4', textStrong: '#eaf4ff', dim: '#5b6874',
  shade: 'rgba(255,255,255,0.035)', segline: '#0b0f14',
  floor: 'rgba(127,179,213,0.20)',
  tileCell: 'rgba(126,231,135,0.13)', tileEdge: '#7ee787', tileText: '#b8f0bf',
};

// 调色板键 ⇄ CSS 变量名。
// `hud` 是 overview 的别名（底部说明文字），与 `text` 同一个变量。
const VARS = {
  bg: '--tl-bg',
  axis: '--tl-axis',
  grid: '--tl-grid',
  ruler: '--tl-line',
  tick: '--tl-tick',
  text: '--tl-text',
  textStrong: '--tl-text-strong',
  dim: '--tl-dim',
  shade: '--tl-shade',
  segline: '--tl-segline',
  floor: '--tl-floor',
  tileCell: '--tl-tile-cell',
  tileEdge: '--tl-tile-edge',
  tileText: '--tl-tile-text',
};

let _cache = null;

function readPalette() {
  const out = Object.assign({}, DARK);
  try {
    const cs = getComputedStyle(document.documentElement);
    for (const k of Object.keys(VARS)) {
      const v = (cs.getPropertyValue(VARS[k]) || '').trim();
      if (v) out[k] = v;
    }
  } catch (e) { /* 读不到 → 保留深色档 */ }
  out.hud = out.text;                 // overview 的别名
  return out;
}

/** 取当前调色板（带缓存）。draw() 里用它。 */
export function tlPal() {
  if (!_cache) _cache = readPalette();
  return _cache;
}

/** 主题变了 → 重读变量。调用方随后重画一次即可。 */
export function refreshTlPal() {
  _cache = readPalette();
  return _cache;
}
