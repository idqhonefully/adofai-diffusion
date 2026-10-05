/* uishot.js —— 截图留证：用 CDP 驱动 headless Chromium 打开工作台页面，
 * 在几个关键状态下各截一张 PNG（默认收起 / 报告带展开 / 音源浮层打开）。
 * 跟 uicheck.js 一样**不碰端口**（只连网关，页面已由主人的壳伺服）。
 *
 * 用法：node tools/uishot.js [url] [chrome.exe] [输出目录]
 * ========================================================================== */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const URL_ = process.argv[2] || 'http://127.0.0.1:8766/workbench/index.html';
const CHROME = process.argv[3] || 'chrome';
const OUT = process.argv[4] || '<REPO>\\output\\logs\\uishot';
const PORT = 9334;
const UDD = '<REPO>\\output\\.tmp\\uishot-profile';
const W = 1560, H = 940;

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
    '--disable-extensions', '--mute-audio', `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${UDD}`, `--window-size=${W},${H}`, 'about:blank',
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
    return r.result && r.result.value;
  };

  await send('Page.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url: URL_ });
  for (let i = 0; i < 60; i++) {                       // 等界面建好
    if (await ev(`!!(document.querySelector('#groups section.grp') && document.getElementById('report'))`)) break;
    await sleep(400);
  }
  await sleep(700);

  const shot = async (name, clip, scale) => {
    const p = { format: 'png' };
    if (clip) p.clip = Object.assign({ scale: scale || 1 }, clip);
    const r = await send('Page.captureScreenshot', p);
    const f = path.join(OUT, name + '.png');
    fs.writeFileSync(f, Buffer.from(r.data, 'base64'));
    console.log('SAVED ' + f);
  };

  // ① 默认：报告带收起
  await ev(`(function(){ try{ localStorage.removeItem('wb.report'); }catch(e){} 
             location.reload(); })()`);
  await sleep(2500);
  for (let i = 0; i < 40; i++) {
    if (await ev(`!!(document.querySelector('#groups section.grp') && document.getElementById('report'))`)) break;
    await sleep(400);
  }
  await sleep(800);
  await shot('1-default-collapsed');
  const rb = await ev(`(function(){ const r = document.getElementById('report').getBoundingClientRect();
    return { top: Math.round(r.top), h: Math.round(r.height), collapsed: document.getElementById('report').classList.contains('collapsed'),
             tip: (document.getElementById('rep-tip')||{}).textContent,
             brief: (document.getElementById('rep-brief')||{}).textContent }; })()`);
  console.log('BAND(collapsed) ' + JSON.stringify(rb));
  await shot('2-report-band', { x: 0, y: Math.max(0, rb.top - 6), width: W, height: Math.min(H - rb.top + 6, rb.h + 12) }, 2);

  // ③ 报告带展开（★ 点的是 #rep-head，不是 #report .zhead —— 绑定在 rep-head 上）
  await ev(`document.getElementById('rep-head').click()`);
  await sleep(700);
  const rb2 = await ev(`(function(){ const r = document.getElementById('report').getBoundingClientRect();
    return { top: Math.round(r.top), h: Math.round(r.height), collapsed: document.getElementById('report').classList.contains('collapsed'),
             chips: (document.getElementById('chips')||{}).textContent,
             stat: (document.getElementById('status')||{}).textContent }; })()`);
  console.log('BAND(expanded) ' + JSON.stringify(rb2));
  await shot('3-report-expanded');
  await shot('3b-report-band-open', { x: 0, y: Math.max(0, rb2.top - 6), width: W, height: Math.min(H - rb2.top + 6, rb2.h + 12) }, 2);

  // ④ 音源浮层
  await ev(`document.getElementById('btn-av-sum').click()`);
  await sleep(500);
  const pr = await ev(`(function(){ const p = document.getElementById('av-pop');
    const r = p.getBoundingClientRect(); const b = document.getElementById('btn-av-sum').getBoundingClientRect();
    return { x: Math.round(b.left - 20), y: Math.round(b.top - 20),
             width: Math.round(r.width + 60), height: Math.round(r.height + 60) }; })()`);
  await shot('4-av-popover', pr);
  await shot('5-full-with-popover');

  ws.close();
  try { child.kill(); } catch (_e) { /* ignore */ }
  console.log('DONE');
  process.exit(0);
})().catch((e) => { console.error('ERR ' + (e && e.stack || e)); process.exit(1); });
