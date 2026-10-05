// settingsbounceprobe.js —— 验证「设置图标已撤掉旋转、与其它按钮同构、按下回弹一致」
// 自起静态服务 + 无头 Supermium(CDP)。用法：node tools/settingsbounceprobe.js
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');
const WebSocket = globalThis.WebSocket;

const ROOT = '<REPO>\\gui';
const PORT = 8798;
const CDP = 9348;
const CHROME = 'chrome';
const UDD = '<REPO>\\output\\.tmp\\settingsbounceprobe-profile';
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
  const chrome = spawn(CHROME, ['--headless=new','--no-sandbox','--disable-gpu',
    '--remote-debugging-port=' + CDP, '--user-data-dir=' + UDD, 'about:blank'], { stdio: 'ignore' });
  await sleep(1200);
  const targets = JSON.parse(await get('/json/list'));
  const page = targets.find(t => t.type === 'page') || targets[0];
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let msgId = 0; const pending = {};
  const send = (method, params) => new Promise((res) => { const id = ++msgId; pending[id] = res; ws.send(JSON.stringify({ id, method, params: params || {} })); });
  await new Promise((res) => ws.addEventListener('open', () => res()));
  ws.addEventListener('message', (d) => { const m = JSON.parse(d.data); if (m.id && pending[m.id]) { pending[m.id](m.result); delete pending[m.id]; } });
  await send('Page.enable'); await send('Runtime.enable'); await send('Input.enable');
  await send('Page.navigate', { url: 'http://127.0.0.1:' + PORT + '/index.html' });
  await sleep(1600);

  // 1) 结构 + 计算样式（设置 vs 生成，应一致）
  const struct = await send('Runtime.evaluate', { expression: `(async function(){
    await new Promise(r=>setTimeout(r,300));
    function info(sel){ var nav=document.querySelector(sel); if(!nav) return {nav:false};
      var ico=nav.querySelector(':scope > .ico'); var clip=nav.querySelector('.ico-clip');
      var cs=ico?getComputedStyle(ico):null;
      return { nav:true, hasClip:!!clip, icoClass:ico?ico.className:null, icoDirectChild:!!ico,
        transition: cs?cs.transition:null, transformOrigin: cs?cs.transformOrigin:null,
        animationName: cs?cs.animationName:null }; }
    return JSON.stringify({ settings: info('#sidebar .nav[data-page="settings"]'),
                            generate: info('#sidebar .nav[data-page="generate"]') });
  })()`, awaitPromise: true, returnByValue: true });
  const s = JSON.parse(struct.result.value);
  console.log('=== 结构/样式 ===');
  console.log('设置:', JSON.stringify(s.settings));
  console.log('生成:', JSON.stringify(s.generate));

  // 2) 真鼠标按下设置项，读 .ico 实时 transform（应为 scale(.8) 的 matrix）
  const box = await send('Runtime.evaluate', { expression: `(function(){
    var nav=document.querySelector('#sidebar .nav[data-page="settings"]');
    var r=nav.getBoundingClientRect();
    return JSON.stringify({x:Math.round(r.left+r.width/2), y:Math.round(r.top+r.height/2)});
  })()`, returnByValue: true });
  const b = JSON.parse(box.result.value);
  await send('Input.dispatchMouseEvent', { type:'mousePressed', x:b.x, y:b.y, button:'left', clickCount:1 });
  await sleep(180);
  const press = await send('Runtime.evaluate', { expression: `(function(){
    var ico=document.querySelector('#sidebar .nav[data-page="settings"] > .ico');
    return JSON.stringify({ transform: getComputedStyle(ico).transform, animationName: getComputedStyle(ico).animationName });
  })()`, returnByValue: true });
  const p = JSON.parse(press.result.value);
  console.log('=== 按下时 .ico 实时样式 ===');
  console.log('transform:', p.transform, '| animationName:', p.animationName);
  await send('Input.dispatchMouseEvent', { type:'mouseReleased', x:b.x, y:b.y, button:'left', clickCount:1 });

  // 判定
  const okStruct = s.settings.icoDirectChild && !s.settings.hasClip &&
                   s.generate.transition && s.settings.transition === s.generate.transition;
  const okBounce = /matrix\(0\.8, 0, 0, 0\.8/.test(p.transform) || /matrix\(0\.800/.test(p.transform);
  const okNoSpin = p.animationName === 'none' || p.animationName === '';
  console.log('--- 判定 ---');
  console.log('设置 .ico 是直接子节点且无 .ico-clip，回弹 transition 与生成一致 :', okStruct ? 'PASS ✓' : 'FAIL ✗');
  console.log('按下时 .ico transform = scale(.8) 回弹生效                  :', okBounce ? 'PASS ✓' : 'FAIL ✗');
  console.log('无旋转动画 (animationName=none)                            :', okNoSpin ? 'PASS ✓' : 'FAIL ✗');

  ws.close(); chrome.kill('SIGKILL'); srv.close();
}
main().catch(e => { console.error(e); process.exit(1); });
