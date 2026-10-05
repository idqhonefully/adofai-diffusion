/* capture.js —— 加载某 URL、可选地切到某个 nav 页面、整页截图存 PNG。
 * 复用 pageeval 的假宿主桥 + CDP 截图思路，给主人出"改完长啥样"的留证图。
 * 用法：node tools/capture.js <url> <out.png> [nav-page] [theme] [chrome] [feed.json]
 *   nav-page = generate|workbench|separate|history|settings|about|train（点了对应侧栏项）
 *   theme    = dark|light（加载后设 <html data-theme>，用于留证浅色模式）
 *   feed.json= 一个 JSON 文件，内容是"宿主消息"（对象或对象数组）。
 *              dev 里没有真宿主，凡是**要靠宿主数据才画得出来**的页面
 *              （分离试听 / 历史记录 / 材质下拉…）都必须先喂一条再截图，
 *              否则拍到的只是空态，证明不了要证明的东西。
 *              喂进去走的是 window.__deliver → 页面的 message 监听 → onHostMessage，
 *              也就是**真宿主那条路上的同一个处理函数**（不是副本）。
 *   pre.js   = 截图前再跑一段 JS（比如点开下拉、展开折叠区）。
 *   flat     = 第 9 个参数，传了就用 `captureBeyondViewport:false` 只拍视口。
 *              🔴 拍**浮层**（下拉菜单这种 position:fixed）时**必须**传它：
 *                captureBeyondViewport 会让 Chromium 按"整页高度"重排一次，
 *                fixed 元素的合成会出错 —— 表现为菜单里透出下层页面的文字，
 *                看着像"背景没铺满"，其实页面本身没问题，是**截图工具在骗人**。
 */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');

const URL_ = process.argv[2];
const OUT = process.argv[3];
const NAV = process.argv[4] || '';
const THEME = process.argv[5] || '';
const CHROME = process.argv[6] || 'chrome';
const PORT = 9341;
const UDD = '<REPO>\\output\\.tmp\\capture-profile';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = (p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: PORT, path: p }, (r) => {
    let b = ''; r.on('data', (c) => (b += c)); r.on('end', () => res(b));
  }).on('error', rej);
});
const STUB = `
window.__SENT = []; window.__LISTENERS = [];
window.chrome = window.chrome || {};
window.chrome.webview = { postMessage:function(s){window.__SENT.push(s);}, addEventListener:function(t,f){ if(t==='message') window.__LISTENERS.push(f); } };
window.__deliver = function(o){ var s=typeof o==='string'?o:JSON.stringify(o); window.__LISTENERS.forEach(function(f){try{f({data:s});}catch(e){}}); };
(function(){ function paint(){ if(!document.documentElement) return false; document.documentElement.style.background='#0e1116'; return true; } if(!paint()){ var t=setInterval(function(){ if(paint()) clearInterval(t); },10);} })();
`;

(async () => {
  fs.mkdirSync(UDD, { recursive: true });
  const child = spawn(CHROME, [
    '--headless=new','--disable-gpu','--no-first-run','--no-default-browser-check',
    '--disable-extensions','--mute-audio','--force-device-scale-factor=1',
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${UDD}`,
    '--window-size=1560,940','about:blank',
  ], { stdio: 'ignore', windowsHide: true });
  let list = [];
  for (let i = 0; i < 80; i++) {
    try { list = JSON.parse(await get('/json/list')); if (list.some(t => t.type === 'page')) break; } catch (_e) {}
    await sleep(250);
  }
  const page = list.find(t => t.type === 'page');
  if (!page) throw new Error('连不上 headless');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  const waiters = new Map(); let seq = 0;
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && waiters.has(m.id)) { const w = waiters.get(m.id); waiters.delete(m.id); if (m.error) w.rej(new Error(JSON.stringify(m.error))); else w.res(m.result); }
  });
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  const send = (method, params) => new Promise((res, rej) => { const id = ++seq; waiters.set(id, { res, rej }); ws.send(JSON.stringify({ id, method, params: params || {} })); });
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Page.addScriptToEvaluateOnNewDocument', { source: STUB });
  await send('Emulation.setDeviceMetricsOverride', { width: 1560, height: 940, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url: URL_ });
  await sleep(2500);
  if (NAV) {
    await send('Runtime.evaluate', {
      expression: '(function(){ var ns=document.querySelectorAll("#sidebar .nav"); for(var i=0;i<ns.length;i++){ if(ns[i].getAttribute("data-page")===' + JSON.stringify(NAV) + '){ ns[i].click(); return true; } } return false; })()',
      returnByValue: true,
    });
    await sleep(700);
  }
  if (THEME) {
    await send('Runtime.evaluate', {
      expression: 'document.documentElement.setAttribute("data-theme",' + JSON.stringify(THEME) + ');',
      returnByValue: true,
    });
    await sleep(800);
  }
  // 喂宿主消息（可选）：dev 里没有真宿主，数据驱动的内容得先喂再截。
  const FEED = process.argv[7] || '';
  if (FEED) {
    const msgs = JSON.parse(fs.readFileSync(FEED, 'utf8'));
    const arr = Array.isArray(msgs) ? msgs : [msgs];
    for (const m of arr) {
      const r = await send('Runtime.evaluate', {
        expression: 'window.__deliver(' + JSON.stringify(m) + '); "delivered"',
        returnByValue: true,
      });
      if (r && r.exceptionDetails) throw new Error('喂消息失败: ' + JSON.stringify(r.exceptionDetails));
    }
    await sleep(700);
  }
  // 截图前再跑一段 JS（可选），比如点开折叠区、滚到某处。
  const PRE = process.argv[8] || '';
  if (PRE) {
    const src = fs.readFileSync(PRE, 'utf8');
    const r = await send('Runtime.evaluate', { expression: src, returnByValue: true, awaitPromise: true });
    if (r && r.exceptionDetails) throw new Error('pre 脚本失败: ' + JSON.stringify(r.exceptionDetails));
    await sleep(600);
  }
  const FLAT = process.argv[9] === 'flat';
  const shot = await send('Page.captureScreenshot', {
    format: 'png',
    captureBeyondViewport: !FLAT,
  });
  fs.writeFileSync(OUT, Buffer.from(shot.data, 'base64'));
  console.log('✓ 截图已存 ' + OUT);
  try { ws.close(); } catch (_e) {}
  try { child.kill(); } catch (_e) {}
  process.exit(0);
})().catch((e) => { console.error('【失败】' + (e && e.stack || e)); process.exit(1); });
