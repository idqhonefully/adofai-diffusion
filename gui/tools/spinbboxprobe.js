// spinbboxprobe.js —— 定量测「设置齿轮旋转时的墨迹包围盒」
//
// 背景（2026-09-22 主人第三/四轮）：
//   ① 主人先报"转动时整个图标增高一截、转完弹回来"；
//   ② 我用"静态裁框 .ico-clip{overflow:hidden}"去修，主人再看截图："你看设置播放动画的时候是这个样"
//      —— 齿轮下半截被切掉了。而我当时用 `mid_span <= 26` 这个**标量阈值**把它判成了 PASS
//      （实测旋转中墨迹跨度只有 14px，比静止的 20px 还小 —— 那不是"关住了"，那是"切没了"）。
//
// 本探针要分辨的两个假设：
//   A) 墨迹被 22px 裁框切掉（越界部分不可见）
//   B) 墨迹本身没居中于盒子 ⇒ 绕盒心旋转时"扫圈"，视觉上忽大忽小 + 偏移
//
// 做法（关键：**不靠时序**，把角度静态写死，结果可复现）：
//   · 关掉 animation / transition，直接把内层 .ico 的 transform 设成 rotate(0°/15°/…/345°)
//   · 分两轮各截 24 张：裁框 overflow:hidden（现状）与 visible（真容）
//   · 用 CDP 的 clip+scale 直接出**放大 8 倍**的局部图，无需再裁
//   · 测量交给 verify_bbox.py（按与背景色的差找墨迹，换算成"相对裁框"的 CSS px）
//
// 用法：node tools/spinbboxprobe.js
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');
const WebSocket = globalThis.WebSocket;

const ROOT = '<REPO>\\gui';
const PORT = 8796;
const CDP = 9346;
const CHROME = 'chrome';
// ★ 环境变量开关：把"探针环境"和"主人真窗口"的差异逐个复现
//   DSF=1.5   主人的窗口是 150% 缩放（截图实测指示条 34 设备px / 22 CSS px）⇒ 光栅化尺度不同
//   GPU=1     不开 --disable-gpu（默认关，headless 软件渲染）
//   TAG=xxx   输出目录后缀，便于把不同配置的产物分开存
const DSF = process.env.DSF || '1';
const GPU = process.env.GPU === '1';
const TAG = process.env.TAG || ('dsf' + DSF + (GPU ? '_gpu' : ''));
const UDD = '<REPO>\\output\\.tmp\\spinbbox-profile-' + TAG;
const OUT = '<REPO>\\output\\.tmp\\spinbbox-' + TAG;
const MIME = { '.html':'text/html', '.js':'text/javascript', '.css':'text/css', '.svg':'image/svg+xml', '.png':'image/png' };
const WIN = 40;      // 局部窗口边长（CSS px），以图标盒中心为中心 ±20 —— 覆盖 22px 盒 + 对角线余量
const SCALE = 8;     // 放大倍数（CDP clip.scale）
const ANGLES = []; for (let a = 0; a < 360; a += 15) ANGLES.push(a);
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
  const args = ['--headless=new','--no-sandbox','--window-size=900,680',
    '--remote-debugging-port='+CDP,'--user-data-dir='+UDD,'about:blank'];
  if (!GPU) args.splice(2, 0, '--disable-gpu');
  if (DSF !== '1') args.splice(2, 0, '--force-device-scale-factor=' + DSF);
  const chrome = spawn(CHROME, args, { stdio: 'ignore' });
  await sleep(1300);
  const targets = JSON.parse(await get('/json/list'));
  const page = targets.find(t => t.type === 'page') || targets[0];
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  let msgId = 0; const pending = {};
  const send = (method, params) => new Promise((res) => { const id = ++msgId; pending[id] = res; ws.send(JSON.stringify({ id, method, params: params || {} })); });
  await new Promise((res) => ws.addEventListener('open', () => res()));
  ws.addEventListener('message', (d) => { const m = JSON.parse(d.data); if (m.id && pending[m.id]) { pending[m.id](m.result); delete pending[m.id]; } });
  // ⚠ awaitPromise 必须给：脚本里用 Promise 轮询等图标注入，不给的话 CDP 把 Promise 序列化成 {}
  const evaluate = async (expr) => JSON.parse((await send('Runtime.evaluate', {
    expression: expr, returnByValue: true, awaitPromise: true })).result.value);
  await send('Page.enable'); await send('Runtime.enable');
  await send('Page.navigate', { url: 'http://127.0.0.1:' + PORT + '/index.html' });
  await sleep(1500);

  // 等 shell-ui 把图标注入进来（boot() 在 DOMContentLoaded 才跑）；顺带把"干扰墨迹"中和掉：
  //   · #nav-ind  选中指示条（黄色竖条，正好落在窗口左半边）→ 置 0
  //   · 选中项背景   rgba 强调色 0.16        → 置 transparent（否则背景采样点会落在渐变色上）
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
        ico.style.transition = 'none';
        ico.style.animation = 'none';
        var cb = clip.getBoundingClientRect();
        var ib = ico.getBoundingClientRect();
        var cs = getComputedStyle(ico), ccs = getComputedStyle(clip);
        resolve(JSON.stringify({
          dpr: window.devicePixelRatio,
          box: {x: cb.left, y: cb.top, w: cb.width, h: cb.height},
          ico: {x: ib.left, y: ib.top, w: ib.width, h: ib.height},
          icoStyle: {fontSize: cs.fontSize, lineHeight: cs.lineHeight, display: cs.display,
                     transformOrigin: cs.transformOrigin, overflow: cs.overflow},
          clipStyle: {display: ccs.display, overflow: ccs.overflow, width: ccs.width, height: ccs.height},
          sidebar: {x: document.getElementById('sidebar').getBoundingClientRect().left, w: document.getElementById('sidebar').getBoundingClientRect().width}
        }));
      })();
    });
  })()`);
  if (meta.err) { console.error('FAILED:', meta.err); ws.close(); chrome.kill('SIGKILL'); srv.close(); return; }
  console.log('=== 几何 / 计算样式 ===');
  console.log(JSON.stringify(meta, null, 2));

  const clip = {
    x: meta.box.x + meta.box.w / 2 - WIN / 2,
    y: meta.box.y + meta.box.h / 2 - WIN / 2,
    width: WIN, height: WIN, scale: SCALE
  };
  console.log(`\n局部窗口（CSS px）：x=${clip.x} y=${clip.y} ${WIN}x${WIN}  scale=${SCALE} ⇒ PNG ${WIN*SCALE}x${WIN*SCALE}`);

  const shoot = async (round, ang) => {
    await evaluate(`(function(){var ico=document.querySelector('#sidebar .nav[data-page="settings"] .ico');
      ico.style.transform='rotate(${ang}deg)'; void ico.offsetWidth; return JSON.stringify({t:getComputedStyle(ico).transform});})()`);
    const r = await send('Page.captureScreenshot', { format: 'png', clip });
    const f = path.join(OUT, `bbox_${round}_${String(ang).padStart(3,'0')}.png`);
    fs.writeFileSync(f, Buffer.from(r.data, 'base64'));
  };

  for (const round of ['hidden', 'visible']) {
    await evaluate(`(function(){var clip=document.querySelector('#sidebar .nav[data-page="settings"] .ico-clip');
      clip.style.overflow='${round}'; return JSON.stringify({o:getComputedStyle(clip).overflow});})()`);
    for (const a of ANGLES) await shoot(round, a);
    console.log(`  轮次 ${round}: 24 张已截`);
  }

  fs.writeFileSync(path.join(OUT, 'meta.json'), JSON.stringify({ clip, meta, angles: ANGLES }));
  ws.close(); chrome.kill('SIGKILL'); srv.close();
  console.log('DONE  ⇒ 下一步: python313/python.exe gui/tools/verify_bbox.py');
}
main().catch(e => { console.error(e); process.exit(1); });
