// spinclip_probe.js —— 证明「设置齿轮旋转不再撑大」：
//   1) 报告设置 .ico 的计算样式 overflow（修复后应为 hidden）
//   2) 截「静止」与「旋转到包围盒最大(~45°)」两张全窗图
//   3) 按图标中心裁出 48x48 放大 6x 的两张小图，交给 pngprobe 出字符画 + 给人看
// 自起静态服务 + 无头 Supermium(CDP)。用法：node tools/spinclip_probe.js
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');
const WebSocket = globalThis.WebSocket;

const ROOT = '<REPO>\\gui';
const PORT = 8795;
const CDP = 9345;
const CHROME = 'chrome';
const UDD = '<REPO>\\output\\.tmp\\spinclip-profile';
const OUT = '<REPO>\\output\\.tmp';
const MIME = { '.html':'text/html', '.js':'text/javascript', '.css':'text/css', '.svg':'image/svg+xml', '.png':'image/png' };
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const b64 = (s) => Buffer.from(s, 'base64');

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
  fs.mkdirSync(OUT, { recursive: true });
  const srv = await serve();
  const chrome = spawn(CHROME, ['--headless=new','--no-sandbox','--disable-gpu','--window-size=900,680','--remote-debugging-port='+CDP,'--user-data-dir='+UDD,'about:blank'], { stdio: 'ignore' });
  await sleep(1300);
  const targets = JSON.parse(await get('/json/list'));
  const page = targets.find(t => t.type === 'page') || targets[0];
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let msgId = 0; const pending = {};
  const send = (method, params) => new Promise((res) => { const id = ++msgId; pending[id] = res; ws.send(JSON.stringify({ id, method, params: params || {} })); });
  await new Promise((res) => ws.addEventListener('open', () => res()));
  ws.addEventListener('message', (d) => { const m = JSON.parse(d.data); if (m.id && pending[m.id]) { pending[m.id](m.result); delete pending[m.id]; } });
  await send('Page.enable'); await send('Runtime.enable');
  await send('Page.navigate', { url: 'http://127.0.0.1:' + PORT + '/index.html' });
  await sleep(1600);

  const info = await send('Runtime.evaluate', { expression: `(function(){
    var setNav = document.querySelector('#sidebar .nav[data-page="settings"]');
    var ico = setNav ? setNav.querySelector('.ico') : null;
    var cs = ico ? getComputedStyle(ico) : null;
    var r = ico ? ico.getBoundingClientRect() : null;
    return JSON.stringify({
      dpr: window.devicePixelRatio,
      sidebarExpanded: document.getElementById('sidebar').classList.contains('expanded'),
      overflow: cs ? cs.overflow : '(no ico)',
      cx: r ? Math.round(r.left + r.width/2) : -1,
      cy: r ? Math.round(r.top + r.height/2) : -1,
      boxW: r ? Math.round(r.width) : -1
    });
  })()`, returnByValue: true });
  const meta = JSON.parse(info.result.value);
  console.log('=== 元数据 ===');
  console.log(meta);

  const shot = async (name) => {
    const s = await send('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(path.join(OUT, name), b64(s.data));
    return path.join(OUT, name);
  };
  const restPng = await shot('spinclip_rest.png');

  // 点设置，轮询包围盒高度直到接近最大（≈对角线），抓"旋转中"瞬间
  await send('Runtime.evaluate', { expression: `document.querySelector('#sidebar .nav[data-page="settings"]').click();`, returnByValue: true });
  let midRect = null;
  for (let i = 0; i < 18; i++) {
    await sleep(35);
    const rr = await send('Runtime.evaluate', { expression: `(function(){var ico=document.querySelector('#sidebar .nav[data-page="settings"] .ico');var r=ico.getBoundingClientRect();return JSON.stringify({h:Math.round(r.height),w:Math.round(r.width)});})()`, returnByValue: true });
    const o = JSON.parse(rr.result.value);
    if (!midRect || o.h > midRect.h) midRect = o;
    if (o.h >= 30) break; // 到最大包围盒就停
  }
  const midPng = await shot('spinclip_mid.png');
  console.log('旋转中最大包围盒:', midRect, ' (22 方盒对角线 ≈ 31；裁切后墨迹应被限制在盒内)');

  // 计算裁图坐标（图标中心固定，旋转不改变中心）
  const half = 24;
  const crop = `${meta.cx - half},${meta.cy - half},${half*2},${half*2}`;
  console.log('--- pngprobe 裁图坐标（DPR=' + meta.dpr + '，截图 px 与 CSS px 一致）：');
  console.log('  裁切区 =', crop, ' （图标中心', meta.cx + ',' + meta.cy + '）');
  console.log('  rest 图:', restPng);
  console.log('  mid  图:', midPng);
  console.log('--- 下一步请运行：');
  console.log(`  python313/python.exe gui/tools/pngprobe.py "${restPng}" --crop ${crop} -o "${OUT}/spinclip_rest_icon.png" --scale 6`);
  console.log(`  python313/python.exe gui/tools/pngprobe.py "${midPng}"  --crop ${crop} -o "${OUT}/spinclip_mid_icon.png"  --scale 6`);
  console.log(`  python313/python.exe gui/tools/pngprobe.py "${restPng}" --ascii ${crop}`);
  console.log(`  python313/python.exe gui/tools/pngprobe.py "${midPng}"  --ascii ${crop}`);

  ws.close(); chrome.kill('SIGKILL'); srv.close();
  console.log('DONE');
}
main().catch(e => { console.error(e); process.exit(1); });
