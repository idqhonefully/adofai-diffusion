// gearsize_sweep.js —— 扫描设置齿轮字号，找"45° 对角齿也不超出 22px 裁框"的安全值
//
// 背景：主人的真实投诉是"动画过程(45°附近)齿轮被切，播完(0°/360°)正常"。
//   根因 = 齿轮字形墨迹几乎顶到 em 盒边，旋转 45° 时对角齿撑到 22×√2 对角线 > 裁框 ⇒ 被切。
//   16px 不够（对角仍微微超出，DPR1.5 下被切）。本探针在真实字体下扫 12..16px，
//   用像素测量"对角齿离裁框中心最远多远"，给出每个字号下 45° 是否安全。
//
// 用法：node tools/gearsize_sweep.js
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');
const WebSocket = globalThis.WebSocket;

const ROOT = '<REPO>\\gui';
const PORT = 8797;
const CDP = 9347;
const CHROME = 'chrome';
const TAG = 'sweep';
const UDD = '<REPO>\\output\\.tmp\\gearsweep-profile';
const OUT = '<REPO>\\output\\.tmp\\gearsweep';
const MIME = { '.html':'text/html', '.js':'text/javascript', '.css':'text/css', '.svg':'image/svg+xml', '.png':'image/png' };
const WIN = 44;      // 局部窗口（CSS px），以裁框中心 ±22，覆盖 22px 盒 + 对角线
const SCALE = 6;
const SIZES = [12, 13, 14, 15, 16];
const ANGLES = [0, 45];
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

function serve() {
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      let p = decodeURIComponent(req.url.split('?')[0]);
      if (p === '/') p = '/index.html';
      fs.readFile(path.join(ROOT, p), (e, buf) => {
        if (e) { res.writeHead(404); res.end('nf'); return; }
        res.writeHead(200, { 'Content-Type': MIME[path.extname(p)] || 'application/octet-stream' });
        res.end(buf);
      });
    });
    srv.listen(PORT, () => resolve(srv));
  });
}
const get = (pp) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: CDP, path: pp }, (r) => { let b=''; r.on('data',c=>b+=c); r.on('end',()=>res(b)); }).on('error', rej);
});

async function main() {
  fs.rmSync(OUT, { recursive: true, force: true });
  fs.mkdirSync(OUT, { recursive: true });
  const srv = await serve();
  const args = ['--headless=new','--no-sandbox','--disable-gpu','--force-device-scale-factor=1.5',
    '--window-size=900,680','--remote-debugging-port='+CDP,'--user-data-dir='+UDD,'about:blank'];
  const chrome = spawn(CHROME, args, { stdio: 'ignore' });
  await sleep(1300);
  const targets = JSON.parse(await get('/json/list'));
  const page = targets.find(t => t.type === 'page') || targets[0];
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let msgId = 0; const pending = {};
  const send = (method, params) => new Promise((res) => { const id = ++msgId; pending[id] = res; ws.send(JSON.stringify({ id, method, params: params || {} })); });
  await new Promise((res) => ws.addEventListener('open', () => res()));
  ws.addEventListener('message', (d) => { const m = JSON.parse(d.data); if (m.id && pending[m.id]) { pending[m.id](m.result); delete pending[m.id]; } });
  const evaluate = async (expr) => JSON.parse((await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })).result.value);
  await send('Page.enable'); await send('Runtime.enable');
  await send('Page.navigate', { url: 'http://127.0.0.1:' + PORT + '/index.html' });
  await sleep(1500);

  const meta = await evaluate(`(function(){
    return new Promise(function(resolve){
      var tries = 0;
      (function poll(){
        var nav = document.querySelector('#sidebar .nav[data-page="settings"]');
        var clip = nav && nav.querySelector('.ico-clip');
        var ico  = nav && nav.querySelector('.ico');
        if (!clip || !ico) { if (++tries < 80) return setTimeout(poll, 50); return resolve(JSON.stringify({err:'icon never injected'})); }
        nav.style.background = 'transparent';
        var ind = document.getElementById('nav-ind'); if (ind) ind.style.opacity = '0';
        ico.style.transition = 'none'; ico.style.animation = 'none';
        var cb = clip.getBoundingClientRect();
        resolve(JSON.stringify({ dpr: window.devicePixelRatio,
          box:{x:cb.left,y:cb.top,w:cb.width,h:cb.height} }));
      })();
    });
  })()`);
  if (meta.err) { console.error('FAILED:', meta.err); ws.close(); chrome.kill('SIGKILL'); srv.close(); return; }
  console.log('=== clip box ===', JSON.stringify(meta));

  const clip = { x: meta.box.x + meta.box.w/2 - WIN/2, y: meta.box.y + meta.box.h/2 - WIN/2, width: WIN, height: WIN, scale: SCALE };
  console.log('local win:', JSON.stringify(clip));

  const shoot = async (size, ang, round) => {
    await send('Runtime.evaluate', { expression: `(function(){
      var nav=document.querySelector('#sidebar .nav[data-page="settings"]');
      var clipEl=nav.querySelector('.ico-clip');
      var ico=nav.querySelector('.ico');
      var glyph=ico.querySelector('.ico-glyph')||ico;
      glyph.style.fontSize='${size}px';
      clipEl.style.overflow='${round}';
      ico.style.transform='rotate(${ang}deg)';
      void ico.offsetWidth;
    })()`, returnByValue: true });
    const r = await send('Page.captureScreenshot', { format: 'png', clip });
    const f = path.join(OUT, `sz${size}_a${ang}_${round}.png`);
    fs.writeFileSync(f, Buffer.from(r.data, 'base64'));
  };

  for (const size of SIZES) {
    for (const ang of ANGLES) {
      for (const round of ['visible','hidden']) {
        await shoot(size, ang, round);
      }
    }
  }
  fs.writeFileSync(path.join(OUT, 'meta.json'), JSON.stringify({ clip, meta, SIZES, ANGLES }));
  ws.close(); chrome.kill('SIGKILL'); srv.close();
  console.log('DONE ⇒ run verify_sweep.py');
}
main().catch(e => { console.error(e); process.exit(1); });
