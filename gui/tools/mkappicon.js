/* mkappicon.js —— 把真正的应用图标（gui/icon.ico 里那帧 4096×4096 PNG）高质量缩成
 * 网页可用的小图 gui/app-icon.png。侧栏品牌格 34px，取 128×128 足够（2x 屏也清楚）。
 *
 * 为什么不用现成的 .ico：那两个文件一个 2.8MB、一个 1.87MB，直接塞进页面等于每次
 * 启动白读两兆。也不引 Pillow —— 项目里没有，且没必要为一张缩图装依赖。
 * 交给无头 Chromium 的 canvas 缩放（imageSmoothingQuality:'high'，比手写盒式滤波好）。
 *
 * 用法：node tools/mkappicon.js [尺寸] [源] [目标]
 * 默认 128 / <REPO>/gui/icon.ico / <REPO>/gui/app-icon.png
 * ========================================================================== */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const SIZE = parseInt(process.argv[2] || '128', 10);
const SRC = process.argv[3] || '<REPO>\\gui\\icon.ico';
const DST = process.argv[4] || '<REPO>\\gui\\app-icon.png';
const TMP = '<REPO>\\output\\.tmp\\_appicon_src.png';

/* .ico 在 Chromium 的 <img> 里解不出来（EncodingError）—— 所以先在 node 侧把
   ICO 目录里那帧 PNG 原样抠出来落盘，再喂给 canvas。10 行，比找解码库省事。 */
function extractIcoPng(icoPath) {
  const d = fs.readFileSync(icoPath);
  const n = d.readUInt16LE(4);
  let off = 6;
  for (let i = 0; i < n; i++) {
    const size = d.readUInt32LE(off + 8);
    const o = d.readUInt32LE(off + 12);
    off += 16;
    if (d.slice(o, o + 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]))) {
      const blob = d.slice(o, o + size);
      fs.mkdirSync(path.dirname(TMP), { recursive: true });
      fs.writeFileSync(TMP, blob);
      console.log(`从 .ico 抠出第 ${i + 1} 帧 PNG：${blob.length} 字节`);
      return TMP;
    }
  }
  throw new Error('这个 .ico 里没有 PNG 帧（可能全是 BMP 帧）');
}
const CHROME = process.env.CHROME || 'chrome';
const PORT = 9338;
const UDD = '<REPO>\\output\\.tmp\\mkappicon-profile';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = (p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: PORT, path: p }, (r) => {
    let b = ''; r.on('data', (c) => (b += c)); r.on('end', () => res(b));
  }).on('error', rej);
});

(async () => {
  fs.mkdirSync(UDD, { recursive: true });
  const child = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--mute-audio', '--allow-file-access-from-files',
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${UDD}`, 'about:blank',
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
  await send('Runtime.enable');
  const src = 'file:///' + (/\.ico$/i.test(SRC) ? extractIcoPng(SRC) : SRC).replace(/\\/g, '/');
  /* 🔴 不要把页面留在 about:blank 再 <img src="file://…"> —— 那会报
     EncodingError: The source image cannot be decoded（实测踩过）。
     直接把 PNG 自身当文档打开，Chromium 会生成一个只含 <img> 的页面，
     同源、可读、可上 canvas。 */
  await send('Page.navigate', { url: src });
  for (let i = 0; i < 40; i++) {
    const ok = await send('Runtime.evaluate', {
      expression: 'document.images.length>0 && document.images[0].complete && document.images[0].naturalWidth>0',
      returnByValue: true,
    });
    if (ok.result.value) break;
    await sleep(200);
  }

  const js = `(async () => {
    const img = document.images[0];
    if (!img || !img.naturalWidth) { return JSON.stringify({ err: '文档里没有可用的 <img>' }); }
    const c = document.createElement('canvas');
    c.width = c.height = ${SIZE};
    const g = c.getContext('2d');
    g.imageSmoothingEnabled = true;
    g.imageSmoothingQuality = 'high';
    g.drawImage(img, 0, 0, ${SIZE}, ${SIZE});
    const d = g.getImageData(0, 0, ${SIZE}, ${SIZE}).data;
    let al = 0, n = 0, corner = 0;
    for (let i = 3; i < d.length; i += 4) { al += d[i]; n++; }
    for (let y = 0; y < 6; y++) for (let x = 0; x < 6; x++) corner += d[(y * ${SIZE} + x) * 4 + 3];
    return JSON.stringify({ nat: img.naturalWidth + 'x' + img.naturalHeight,
      data: c.toDataURL('image/png').slice(22), alphaAvg: (al / n).toFixed(1), cornerAlpha: (corner / 36).toFixed(1) });
  })()`;
  const r = await send('Runtime.evaluate', { expression: js, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) { throw new Error('页面里抛异常: ' + JSON.stringify(r.exceptionDetails).slice(0, 300)); }
  const info = JSON.parse(r.result.value);
  if (info.err) { throw new Error(info.err); }
  fs.writeFileSync(DST, Buffer.from(info.data, 'base64'));
  console.log(`源 ${SRC} (${info.nat}) → ${DST}  ${SIZE}×${SIZE}`);
  console.log(`  平均 alpha=${info.alphaAvg}（255=不透明）/ 左上角 alpha=${info.cornerAlpha}（0=圆角外透明）`);
  console.log(`  输出 ${fs.statSync(DST).size} 字节`);
  try { child.kill(); } catch (_e) {}
  await sleep(400);
  process.exit(0);
})();
