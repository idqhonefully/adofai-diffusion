/* iconsheet.js —— 「候选图标比对表」：把若干候选字形在同一尺度下渲染成 PNG，
 * 再用 pngprobe/字符画 与参考截图逐像素比对，谁像用谁。
 *
 * 为什么需要它：观感类判断（"这个图标是不是那个意思"）探针证明不了 —— 上一轮就是
 * 探针全绿、图标却画成了太阳。判据只能是**真实像素**。参考图来自 150% DPI 的真机截图，
 * 所以这里也按 deviceScaleFactor=1.5 渲染（20px CSS → 30px 物理），尺度才对得上。
 *
 * 用法：node tools/iconsheet.js [输出png] [chrome.exe]
 * 输出：一张横向排列的候选表（每格 60×60 逻辑，图标 20px 居中）
 * ========================================================================== */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const OUT = process.argv[2] || '<REPO>\\output\\.tmp\\iconsheet.png';
const CHROME = process.argv[3] || 'chrome';
const PORT = 9337;
const UDD = '<REPO>\\output\\.tmp\\iconsheet-profile';
const CELL = 60, N = 6;

const FLUENT24 = fs.readFileSync('<REPO>/output/.tmp/set24.svg', 'utf8')
  .match(/ d="([^"]+)"/)[1];
const FLUENT20 = fs.readFileSync('<REPO>/output/.tmp/set20.svg', 'utf8')
  .match(/ d="([^"]+)"/)[1];

// 我们当前页面里那版（手画齿轮：环 + 轴孔 + 8 齿）
const OURS = '<circle cx="10" cy="10" r="6.2"/><circle cx="10" cy="10" r="2.4"/>' +
  '<path d="M10 1.6v2.1M10 16.3v2.1M18.4 10h-2.1M3.7 10H1.6M15.94 4.06l-1.48 1.48M5.54 14.46 4.06 15.94M15.94 15.94l-1.48-1.48M5.54 5.54 4.06 4.06"/>';

// 按参考图重画的新版（细描边齿轮：外圈带 8 齿 + 内环）
const OURS2 = '<circle cx="10" cy="10" r="3.6"/>' +
  '<path d="M10 1.9v2.5M10 15.6v2.5M18.1 10h-2.5M4.4 10H1.9M15.73 4.27l-1.77 1.77M6.04 13.96l-1.77 1.77M15.73 15.73l-1.77-1.77M6.04 6.04 4.27 4.27"/>' +
  '<path d="M6.1 3.9h7.8v2.2H6.1zM6.1 13.9h7.8v2.2H6.1zM3.9 6.1v7.8h2.2V6.1zM13.9 6.1v7.8h2.2V6.1z" fill="none"/>';

const cands = [
  ['fluent24', `<svg viewBox="0 0 24 24" width="20" height="20"><path d="${FLUENT24}" fill="currentColor"/></svg>`],
  ['fluent20', `<svg viewBox="0 0 20 20" width="20" height="20"><path d="${FLUENT20}" fill="currentColor"/></svg>`],
  ['ours-now', `<svg viewBox="0 0 20 20" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round">${OURS}</svg>`],
  ['ours-new', `<svg viewBox="0 0 20 20" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round">${OURS2}</svg>`],
  ['segmdl2-E713', `<span style="font-family:'Segoe MDL2 Assets';font-size:20px;line-height:20px">&#xE713;</span>`],
  ['segfluent-E713', `<span style="font-family:'Segoe Fluent Icons';font-size:20px;line-height:20px">&#xE713;</span>`],
];

const html = `<!doctype html><meta charset="utf-8"><style>
  html,body{margin:0;background:#1E2026;}
  .row{display:flex;}
  .cell{width:${CELL}px;height:${CELL}px;display:flex;align-items:center;justify-content:center;
        color:#D6D6D6;background:#1E2026;}
</style><div class="row">${cands.map(([, svg]) => `<div class="cell">${svg}</div>`).join('')}</div>`;

const file = '<REPO>\\output\\.tmp\\iconsheet.html';
fs.writeFileSync(file, html, 'utf8');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = (p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: PORT, path: p }, (r) => {
    let b = ''; r.on('data', (c) => (b += c)); r.on('end', () => res(b));
  }).on('error', rej);
});

(async () => {
  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  const child = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--mute-audio', '--force-device-scale-factor=1.5',
    '--allow-file-access-from-files',
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${UDD}`,
    `--window-size=${CELL * N + 20},120`, 'about:blank',
  ], { stdio: 'ignore', windowsHide: true });

  let list = [];
  for (let i = 0; i < 80; i++) {
    try { list = JSON.parse(await get('/json/list')); if (list.some((t) => t.type === 'page')) break; } catch (_e) {}
    await sleep(250);
  }
  const page = list.find((t) => t.type === 'page');
  if (!page) { throw new Error('没能连上 headless 浏览器'); }

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  const waiters = new Map();
  let seq = 0;
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && waiters.has(m.id)) {
      const w = waiters.get(m.id); waiters.delete(m.id);
      if (m.error) w.rej(new Error(JSON.stringify(m.error))); else w.res(m.result);
    }
  });
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  const send = (m, p) => new Promise((res, rej) => {
    const id = ++seq; waiters.set(id, { res, rej });
    ws.send(JSON.stringify({ id, method: m, params: p || {} }));
  });

  await send('Page.enable');
  await send('Emulation.setDeviceMetricsOverride',
    { width: CELL * N + 20, height: 120, deviceScaleFactor: 1.5, mobile: false });
  await send('Page.navigate', { url: 'file:///' + file.replace(/\\/g, '/') });
  await sleep(900);

  const fonts = await send('Runtime.evaluate', {
    expression: `JSON.stringify({mdl2: document.fonts.check("20px 'Segoe MDL2 Assets'"),
      fluent: document.fonts.check("20px 'Segoe Fluent Icons'")})`, returnByValue: true,
  });
  console.log('字体可用性：', fonts.result.value);

  const r = await send('Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(OUT, Buffer.from(r.data, 'base64'));
  console.log('已写出', OUT);
  cands.forEach((c, i) => console.log(`  格 ${i} (x=${i * CELL}..${(i + 1) * CELL}) = ${c[0]}`));
  try { child.kill(); } catch (_e) {}
  await sleep(400);
  process.exit(0);
})();
