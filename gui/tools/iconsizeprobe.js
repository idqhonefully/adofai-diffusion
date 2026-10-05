// iconsizeprobe.js —— 采样「设置图标」点击旋转期间/之后的
//   · 渲染高度(getBoundingClientRect.height) —— 是否"增高"
//   · 计算样式的 transform(矩阵) / rotate / transition / animationName —— 是否在动画结束后又跑一次"回弹过渡"
// 自起静态服务 + 无头 Supermium(CDP)。
// 用法：node tools/iconsizeprobe.js
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');
const WebSocket = globalThis.WebSocket;

const ROOT = '<REPO>\\gui';
const PORT = 8796;
const CDP = 9346;
const CHROME = 'chrome';
const UDD = '<REPO>\\output\\.tmp\\iconsizeprobe-profile';
const MIME = { '.html':'text/html', '.js':'text/javascript', '.css':'text/css', '.svg':'image/svg+xml', '.png':'image/png' };
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

function serve() {
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      let p = decodeURIComponent(req.url.split('?')[0]);
      if (p === '/') p = '/index.html';
      const fp = path.join(ROOT, p);
      fs.readFile(fp, (e, buf) => {
        if (e) { res.writeHead(404); res.end('nf'); return; }
        res.writeHead(200, { 'Content-Type': MIME[path.extname(fp)] || 'application/octet-stream' });
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
  const srv = await serve();
  const chrome = spawn(CHROME, ['--headless=new','--no-sandbox','--disable-gpu','--remote-debugging-port='+CDP,'--user-data-dir='+UDD,'about:blank'], { stdio: 'ignore' });
  await sleep(1200);
  const targets = JSON.parse(await get('/json/list'));
  const page = targets.find(t => t.type === 'page') || targets[0];
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let msgId = 0; const pending = {};
  const send = (method, params) => new Promise((res) => { const id = ++msgId; pending[id] = res; ws.send(JSON.stringify({ id, method, params: params || {} })); });
  await new Promise((res) => ws.addEventListener('open', () => res()));
  ws.addEventListener('message', (d) => { const m = JSON.parse(d.data); if (m.id && pending[m.id]) { pending[m.id](m.result); delete pending[m.id]; } });
  await send('Page.enable'); await send('Runtime.enable');
  await send('Page.navigate', { url: 'http://127.0.0.1:' + PORT + '/index.html' });
  await sleep(1500);

  const expr = `(async function(){
    await new Promise(r => { if (document.readyState === 'complete') r(); else window.addEventListener('load', r); });
    await new Promise(r => setTimeout(r, 400));
    var setNav = document.querySelector('#sidebar .nav[data-page="settings"]');
    var ico = setNav ? setNav.querySelector('.ico') : null;
    var samples = [];
    function snap(t){
      var cs = getComputedStyle(ico);
      var rect = ico.getBoundingClientRect();
      samples.push({
        t: t,
        h: +rect.height.toFixed(2),
        w: +rect.width.toFixed(2),
        transform: cs.transform.slice(0, 40),
        rotate: cs.rotate,
        transitionProperty: cs.transitionProperty,
        transitionDuration: cs.transitionDuration,
        animationName: cs.animationName,
        animationPlayState: cs.animationPlayState
      });
    }
    snap(0);                 // 点击前
    setNav.click();          // 触发旋转（同时 :active 按下）
    for (var i=1;i<=22;i++){ // 每 40ms 一帧，约 880ms（覆盖 .55s 动画 + 之后余波）
      await new Promise(r => setTimeout(r, 40));
      snap(i*40);
    }
    return JSON.stringify({ samples: samples });
  })()`;
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  const out = r.result && r.result.value ? r.result.value : JSON.stringify(r);
  console.log('=== iconsizeprobe 采样 ===');
  try {
    const o = JSON.parse(out);
    console.log('t(ms)\theight\twidth\ttransform/rotate\t\t\t\t\t\t\tanimation / transition');
    for (const s of o.samples) {
      console.log(
        String(s.t).padEnd(5),
        String(s.h).padEnd(7),
        String(s.w).padEnd(6),
        (s.rotate && s.rotate !== 'none' ? ('rotate='+s.rotate) : (s.transform||'none')).slice(0,38).padEnd(40),
        'a='+s.animationName+(s.animationName!=='none'?('/'+s.animationPlayState):'')+' tp='+s.transitionProperty.replace(/[\\s]+/g,'')+' td='+s.transitionDuration
      );
    }
    // 判定：动画结束后(>600ms)是否还在跑 transform 过渡（弹回）
    const late = o.samples.filter(s => s.t >= 600);
    const bounce = late.filter(s => (s.transitionProperty||'').indexOf('transform') !== -1 && (s.transitionDuration||'').indexOf('0.4s') !== -1 && s.animationName === 'none');
    const restH = o.samples[0].h;
    const maxH = Math.max.apply(null, o.samples.map(s=>s.h));
    console.log('--- 判定 ---');
    console.log('静止高度:', restH, ' 旋转中最大高度:', maxH, ' (差值='+(maxH-restH).toFixed(2)+'px, 这是旋转对角线溢出, 属正常物理)');
    console.log('动画结束后(>=600ms)是否仍有 transform 过渡在跑(=弹回):', bounce.length ? ('YES ✗ 在 '+bounce.map(b=>b.t).join(',')+'ms') : 'NO ✓ 干净');
  } catch (e) { console.log(out); console.log('(解析失败)', e.message); }
  ws.close(); chrome.kill('SIGKILL'); srv.close();
}
main().catch(e => { console.error(e); process.exit(1); });
