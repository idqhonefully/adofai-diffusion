/* themeshot.js —— 明暗两套主题的留证截图（2026-09-22 新增）。
 * ============================================================================
 * 为什么需要它：导入页与工作台**自己都不刷底色**（靠宿主壳的 DWM 材质当底），
 * 所以 headless 里默认渲染成白底 —— 那跟主人实际看到的两码事。
 * 想看"浅色主题到底长什么样"，必须：① 垫一层该主题的底色，② 把 data-theme 设上。
 * 这两件事都在这一个工具里做，免得每轮又现写一遍 CDP。
 *
 * 用法：
 *   node tools/themeshot.js <url> <dark|light> [输出目录] [settings]
 * 第 4 个参数给 settings（或 pages=settings,workbench）时：先注一个**假宿主桥**，
 * 再点侧栏「设置」、把 appinfo 喂进去 —— 否则设置页里的材质卡是空的（真宿主不在），
 * 截图就成了"页面加载中"。
 * 输出：<out>/<theme>-full.png、<out>/<theme>-sidebar.png（导入页时才裁侧栏）
 * ========================================================================== */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const URL_ = process.argv[2] || 'http://127.0.0.1:8896/index.html';
const THEME = (process.argv[3] || 'dark').toLowerCase() === 'light' ? 'light' : 'dark';
const OUT = process.argv[4] || ('<REPO>\\output\\logs\\themeshot-' + THEME);
const PAGE = (process.argv[5] || '').toLowerCase();

/* 假桥：与 importprobe.js 用的同一套（页面发出的消息收进 __SENT，
   回灌走 window.__devHostMsg —— 与真宿主消息进的是同一个 onHostMessage）。 */
const BRIDGE = `
window.__SENT = [];
window.__HANDLERS = [];
window.chrome = window.chrome || {};
window.chrome.webview = {
  postMessage: function (s) {
    try { window.__SENT.push(JSON.parse(s)); }
    catch (e) { window.__SENT.push({ __raw: String(s) }); }
  },
  addEventListener: function (t, fn) { if (t === 'message') window.__HANDLERS.push(fn); }
};
`;

/* 与 C# 宿主 Materials.ToWire() / Appearances.ToWire() 对齐的一份 appinfo 素材。 */
const APPINFO = {
  app: 'ADOFAI Studio', studio_dir: '<REPO>', output_dir: '<REPO>\\output',
  gateway: 'http://127.0.0.1:8766', sidecar: '127.0.0.1:8765',
  material: 'mica',
  appearance: THEME,
  appearances: [
    { name: 'dark', label: '深色', dark: true, base_hex: '#0E1116' },
    { name: 'light', label: '浅色', dark: false, base_hex: '#F3F3F3' },
  ],
  materials: [
    { name: 'acrylic', label: 'Acrylic', kind: 3, opaque: false, appearances: [] },
    { name: 'mica', label: 'Mica', kind: 2, opaque: false, appearances: ['dark', 'light'] },
    { name: 'solid', label: 'Solid', kind: 1, opaque: true, appearances: ['dark', 'light'] },
  ],
};

const CHROME = 'chrome';
const PORT = 9339;
const UDD = '<REPO>\\output\\.tmp\\themeshot-profile';

/* 该主题的"窗口底"色 —— 与 C# Appearances.BaseHex 保持一致（改了那边这里也要改）。 */
const BASE = { dark: '#0E1116', light: '#F3F3F3' };
const DSF = 1.5, W = 1180, H = 820;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = (p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: PORT, path: p }, (r) => {
    let b = ''; r.on('data', (c) => (b += c)); r.on('end', () => res(b));
  }).on('error', rej);
});

(async () => {
  fs.mkdirSync(UDD, { recursive: true });
  fs.mkdirSync(OUT, { recursive: true });
  const child = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--mute-audio', '--remote-debugging-port=' + PORT,
    '--user-data-dir=' + UDD, '--window-size=' + W + ',' + H, 'about:blank',
  ], { stdio: 'ignore', windowsHide: true });

  let list = [];
  for (let i = 0; i < 80; i++) {
    try { list = JSON.parse(await get('/json/list')); if (list.some((t) => t.type === 'page')) break; }
    catch (_e) { /* 还没起来 */ }
    await sleep(250);
  }
  const page = list.find((t) => t.type === 'page');
  if (!page) throw new Error('没能连上 headless 浏览器的 DevTools');

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  const waiters = new Map();
  let seq = 0;
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && waiters.has(m.id)) { waiters.get(m.id)(m); waiters.delete(m.id); }
  });
  await new Promise((r) => ws.addEventListener('open', r));
  const send = (method, params) => new Promise((res, rej) => {
    const id = ++seq;
    waiters.set(id, (m) => (m.error ? rej(new Error(method + ': ' + m.error.message)) : res(m.result)));
    ws.send(JSON.stringify({ id, method, params: params || {} }));
  });
  const ev = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error('page err: ' + JSON.stringify(r.exceptionDetails.exception));
    return r.result && r.result.value;
  };

  await send('Page.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: DSF, mobile: false });
  // ★ 假桥必须在**页面脚本之前**注入，否则页面 post() 那一刻 webview 还不存在。
  await send('Page.addScriptToEvaluateOnNewDocument', { source: BRIDGE });
  await send('Page.navigate', { url: URL_ });
  for (let i = 0; i < 60; i++) {
    if (await ev('!!document.body && !!document.querySelector("#sidebar .nav")')) break;
    await sleep(200);
  }
  // ★ 主题必须尽早定好：晚设会出现"先按深色画一帧"的闪烁，截出来的正好是那一帧。
  await ev('document.documentElement.style.background=' + JSON.stringify(BASE[THEME]) + ';' +
           'document.documentElement.setAttribute("data-theme",' + JSON.stringify(THEME) + ');' +
           'try{localStorage.setItem("dsh.appearance",' + JSON.stringify(THEME) + ');}catch(e){}');

  if (PAGE.indexOf('settings') >= 0) {
    await ev('document.querySelector(".nav[data-page=settings]").click();');
    await sleep(500);
    await ev('window.__devHostMsg(' + JSON.stringify(JSON.stringify({ type: 'appinfo', data: APPINFO })) + ')');
    await sleep(700);
  }
  await sleep(900);

  const shot = async (name, clip, scale) => {
    const p = { format: 'png' };
    if (clip) p.clip = Object.assign({ scale: scale || 1 }, clip);
    const r = await send('Page.captureScreenshot', p);
    const f = path.join(OUT, name + '.png');
    fs.writeFileSync(f, Buffer.from(r.data, 'base64'));
    console.log('SAVED ' + f);
  };

  await shot(THEME + '-full');
  const isImport = await ev('!!document.getElementById("sidebar")');
  if (isImport) await shot(THEME + '-sidebar', { x: 0, y: 0, width: 120, height: H }, 3);

  // 关键读数：主题属性、正文色、外框/卡片底 —— 留证图之外还要有数字（截图只能"看像不像"）
  const read = await ev('(function(){' +
    'var cs=getComputedStyle(document.documentElement);' +
    'var g=function(n){return cs.getPropertyValue(n).trim();};' +
    'var page=document.querySelector(".page.active");' +
    'return JSON.stringify({theme:document.documentElement.getAttribute("data-theme"),' +
    ' text:g("--text"), dim:g("--text-dim"), frame:g("--fill-frame"), card:g("--fill-card"),' +
    ' bodyColor:getComputedStyle(document.body).color,' +
    ' pageBg:page?getComputedStyle(page).backgroundColor:null,' +
    ' htmlBg:getComputedStyle(document.documentElement).backgroundColor});})()');
  console.log('READ ' + read);

  ws.close();
  try { child.kill(); } catch (_e) { /* ignore */ }
  console.log('DONE');
  process.exit(0);
})().catch((e) => { console.error('ERR ' + ((e && e.stack) || e)); process.exit(1); });
