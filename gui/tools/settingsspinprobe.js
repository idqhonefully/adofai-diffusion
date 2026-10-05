// settingsspinprobe.js —— 验证「设置图标转圈」+「回弹更深更慢」
// 自起静态服务 + 无头 Supermium(CDP) + WebSocket 跑 Runtime.evaluate。
// 用法：node tools/settingsspinprobe.js
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');
const WebSocket = globalThis.WebSocket;

const ROOT = '<REPO>\\gui';
const PORT = 8797;
const CDP = 9347;
const CHROME = 'chrome';
const UDD = '<REPO>\\output\\.tmp\\settingsspinprobe-profile';
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
  const chrome = spawn(CHROME, [
    '--headless=new', '--no-sandbox', '--disable-gpu',
    '--remote-debugging-port=' + CDP, '--user-data-dir=' + UDD,
    'about:blank'
  ], { stdio: 'ignore' });
  await sleep(1200);
  const ver = JSON.parse(await get('/json/version'));
  const targets = JSON.parse(await get('/json/list'));
  const page = targets.find(t => t.type === 'page') || targets[0];
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let msgId = 0; const pending = {};
  const send = (method, params) => new Promise((res) => {
    const id = ++msgId; pending[id] = res;
    ws.send(JSON.stringify({ id, method, params: params || {} }));
  });
  await new Promise((res) => ws.addEventListener('open', () => res()));
  ws.addEventListener('message', (d) => { const m = JSON.parse(d.data); if (m.id && pending[m.id]) { pending[m.id](m.result); delete pending[m.id]; } });
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.navigate', { url: 'http://127.0.0.1:' + PORT + '/index.html' });
  await sleep(1500);

  const expr = `(async function(){
    await new Promise(r => { if (document.readyState === 'complete') r(); else window.addEventListener('load', r); });
    await new Promise(r => setTimeout(r, 400));
    var setNav = document.querySelector('#sidebar .nav[data-page="settings"]');
    var ico = setNav ? setNav.querySelector('.ico') : null;
    var transition = ico ? getComputedStyle(ico).transition : '(no ico)';
    var transformOrigin = ico ? getComputedStyle(ico).transformOrigin : '';
    var before = ico ? ico.className : '(no ico)';
    if (setNav) setNav.click();
    await new Promise(r => setTimeout(r, 40));
    var after = ico ? { className: ico.className, animationName: getComputedStyle(ico).animationName, animationDuration: getComputedStyle(ico).animationDuration } : null;
    var activeRule = null;
    for (var i=0;i<document.styleSheets.length;i++){
      var ss = document.styleSheets[i]; var rules; try { rules = ss.cssRules; } catch(e){ continue; }
      if(!rules) continue;
      for (var j=0;j<rules.length;j++){
        var rule = rules[j];
        if (rule.selectorText && rule.selectorText.indexOf('.nav:active .ico') !== -1){
          activeRule = { selector: rule.selectorText, transform: rule.style.transform, transition: rule.style.transition };
        }
      }
    }
    return JSON.stringify({
      setNavExists: !!setNav, icoExists: !!ico,
      transition: transition, transformOrigin: transformOrigin,
      beforeClickClass: before,
      afterClick: after, activeRule: activeRule,
      err: window.__err || ''
    });
  })()`;
  const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
  const out = r.result && r.result.value ? r.result.value : (JSON.stringify(r));
  console.log('=== settingsspinprobe 结果 ===');
  console.log(out);
  try {
    const o = JSON.parse(out);
    const ok = o.icoExists && o.afterClick && o.afterClick.className.indexOf('spin') !== -1 && o.afterClick.animationName === 'ico-spin';
    const bnc = o.transition.indexOf('0.4s') !== -1 && o.transition.indexOf('1.8') !== -1;
    const deep = o.activeRule && o.activeRule.transform && o.activeRule.transform.indexOf('scale(0.8)') !== -1;
    console.log('--- 判定 ---');
    console.log('设置点击触发 spin 类 + 动画 ico-spin :', ok ? 'PASS ✓' : 'FAIL ✗');
    console.log('回弹 transition 含 0.4s & 1.8 (更慢更深) :', bnc ? 'PASS ✓' : 'FAIL ✗');
    console.log('按下 :active 缩放含 scale(.8) (更深)   :', deep ? 'PASS ✓' : 'FAIL ✗');
  } catch (e) { console.log('(解析失败)', e.message); }
  ws.close(); chrome.kill('SIGKILL'); srv.close();
}
main().catch(e => { console.error(e); process.exit(1); });
