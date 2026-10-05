/* pageeval.js —— 在（带假宿主桥的）页面里跑一段 JS 并把结果打出来。
 * ============================================================================
 * 为什么需要它：外观核对经常是"先拿到某个元素的精确矩形，再去 pngprobe 裁那
 * 一块看像素"。以前每次都要临时写一个 node+CDP 脚本，写完就扔；这个小工具把
 * 那层壳固定下来，只用传表达式。
 *
 * 与 importprobe.js 的分工：
 *   · importprobe  = 断言集合（64 条），跑一遍看红绿；
 *   · pageeval     = 一次性取值/探路（"这个图标在屏幕哪一块？""它的计算字号多少？"）。
 *
 * ⚠ 一定会塞假宿主桥：页面脚本一进来就 `chrome.webview.addEventListener`，
 *   没有宿主时直接抛 TypeError 把整段脚本带停，什么都量不到。
 *
 * 用法：node tools/pageeval.js <url> '<js 表达式>' [chrome.exe] [--focus]
 *   表达式里可以用 await（按 async 函数体求值），返回值会被 JSON.stringify。
 * 例：
 *   node tools/pageeval.js http://127.0.0.1:8896/index.html \
 *     'JSON.stringify(document.querySelector("#sidebar .nav[data-page=settings] .ico").getBoundingClientRect())'
 *
 * 🔴 `--focus`（2026-09-22 新增）：无头页默认**不被视为聚焦**（`document.hasFocus()`
 *   恒为 false），于是 `button.click()` **不会给按钮焦点**、`blur` 事件也永远不触发。
 *   所以凡是"验 focus/blur 分支"的探针，不加这个开关就等于那条分支**根本没走到** ——
 *   量到的"没反应"是环境造成的假象（正是"探针要验兜底分支先确认它走得到"那条铁律）。
 *   开关打开后 `document.hasFocus()` 为真、点击会真的移焦点，focus/blur 类行为才可测。
 * ========================================================================== */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const URL_ = process.argv[2];
const EXPR = process.argv[3];
const FOCUS = process.argv.includes('--focus');
const CHROME = process.argv[4] && !process.argv[4].startsWith('--')
  ? process.argv[4] : 'chrome';
const PORT = 9338;
const UDD = '<REPO>\\output\\.tmp\\pageeval-profile';

if (!URL_ || !EXPR) { console.log(require('fs').readFileSync(__filename, 'utf8').split('*/')[0]); process.exit(2); }

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = (p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: PORT, path: p }, (r) => {
    let b = ''; r.on('data', (c) => (b += c)); r.on('end', () => res(b));
  }).on('error', rej);
});

const STUB = `
window.__SENT = []; window.__LISTENERS = [];
window.chrome = window.chrome || {};
window.chrome.webview = {
  postMessage: function (s) { window.__SENT.push(s); },
  addEventListener: function (t, f) { if (t === 'message') window.__LISTENERS.push(f); }
};
window.dsh = window.dsh || {};
window.__deliver = function (o) {
  var s = typeof o === 'string' ? o : JSON.stringify(o);
  window.__LISTENERS.forEach(function (f) { try { f({ data: s }); } catch (e) {} });
};
(function () {
  function paint() {
    if (!document.documentElement) { return false; }
    document.documentElement.style.background = '#0e1116';
    return true;
  }
  if (!paint()) { var t = setInterval(function () { if (paint()) { clearInterval(t); } }, 10); }
})();
`;

(async () => {
  fs.mkdirSync(UDD, { recursive: true });
  const child = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--mute-audio', '--force-device-scale-factor=1',
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${UDD}`,
    '--window-size=1560,940', 'about:blank',
  ], { stdio: 'ignore', windowsHide: true });

  let list = [];
  for (let i = 0; i < 80; i++) {
    try { list = JSON.parse(await get('/json/list')); if (list.some((t) => t.type === 'page')) break; } catch (_e) { }
    await sleep(250);
  }
  const page = list.find((t) => t.type === 'page');
  if (!page) { throw new Error('连不上 headless'); }
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  const waiters = new Map(); let seq = 0;
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && waiters.has(m.id)) {
      const w = waiters.get(m.id); waiters.delete(m.id);
      if (m.error) w.rej(new Error(JSON.stringify(m.error))); else w.res(m.result);
    }
  });
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  const send = (method, params) => new Promise((res, rej) => {
    const id = ++seq; waiters.set(id, { res, rej });
    ws.send(JSON.stringify({ id, method, params: params || {} }));
  });
  await send('Runtime.enable');
  await send('Page.enable');
  // ★ --focus：让页面被当成"已聚焦的窗口"，点击才会真的移动焦点（见文件头说明）
  if (FOCUS) {
    try { await send('Emulation.setFocusEmulationEnabled', { enabled: true }); }
    catch (e) { console.error('【警告】--focus 打开失败：' + e.message); }
  }
  await send('Page.addScriptToEvaluateOnNewDocument', { source: STUB });
  await send('Emulation.setDeviceMetricsOverride', { width: 1560, height: 940, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url: URL_ });
  await sleep(2500);

  const r = await send('Runtime.evaluate', {
    expression: '(async function(){' + EXPR + '})()',
    returnByValue: true, awaitPromise: true,
  });
  if (r.exceptionDetails) {
    console.error('页面里抛异常：' + (r.exceptionDetails.exception || {}).description);
    process.exitCode = 1;
  } else {
    console.log(typeof r.result.value === 'string' ? r.result.value : JSON.stringify(r.result.value, null, 1));
  }
  try { ws.close(); } catch (_e) { }
  try { child.kill(); } catch (_e) { }
  process.exit(process.exitCode || 0);
})().catch((e) => { console.error('【失败】' + (e && e.stack || e)); process.exit(1); });
