/* bgcheck.js —— 「切页闪白」回归探针（第八轮重写：白闪真身 = 原生滚动条）
 * ============================================================================
 * 主人报的 bug（原话）：
 *   "切换的时候会突然闪白一下下，就一瞬间白一点点，然后恢复正常。"（深色模式）
 *   "浅色模式切换的时候动画一播放就全部白色不透明了。深色模式是黑色不透明了。"（第七轮修法副作
 *    用：为了遮白闪给 #main 垫了不透明底，把宿主材质整块盖死）
 *
 * 🔴 最终根因（第八轮，实测定案）：
 *   白闪**不是**背景问题，是**原生滚动条**。
 *   全项目此前一处 `color-scheme` 都没有 ⇒ 文档的 used color scheme 落回 **OS 偏好**
 *   （真机是浅色 app 模式）⇒ 深色界面里滚动条仍按**浅色方案**绘制，就是一条发白的竖杠。
 *   切页正好把它逼出来两次：
 *     · `fl-rise-in` 的 `translateY(25vh)`：按 CSS 规范**变换后的边界盒算进滚动溢出区**，
 *       那一瞬 `.page` 滚动高度被撑大 ⇒ 滚动条出现；
 *     · `.page.active.animating{overflow:hidden}` 在 ~620ms 后摘掉 ⇒ 滚动条又回来。
 *   `scrollbar-gutter:stable` 只保证"宽度不跳"，挡不住"颜色是浅色的"。
 *
 * 修法：`:root{color-scheme:dark}` / `html[data-theme="light"]{color-scheme:light}`
 *   （浅色档写 light = 浏览器默认值 ⇒ 浅色档视觉一字节不动）。
 *
 * ⚠ 为什么历轮探针量不到白闪：探针跑 headless，headless 默认用 **overlay 滚动条**、
 *   根本不渲染原生滚动条；而且它默认 prefers-color-scheme 可能不是浅色。
 *   ⇒ 本探针必须：① 起浏览器带 `--disable-features=OverlayScrollbar`；
 *                ② 用 `Emulation.setEmulatedMedia` 显式模拟 `prefers-color-scheme: light`
 *                   （= 真机状态）。两者缺一就是假绿。
 *
 * 判据（9 条）：
 *   A/B 深色·浅色静止态 body 透明           —— 宿主材质照常透出（撤回第六轮实色兜底）
 *   C   静止态 #main 透明
 *   D/E 切页过渡中 #main 仍透明 + 类名不含任何 cover 类（第七轮教训：不许垫底遮闪）
 *   F/G 深色 => color-scheme:dark、浅色 => color-scheme:light
 *   H   深色+OS浅色：.page 右缘滚动条像素必须是**深色**（max<120）—— 本条就是"白闪没了"
 *   I   负对照：把 color-scheme 掰回 light（模拟修复前）⇒ 同一像素必须变**亮**（max>180）
 *
 * 用法：node tools/bgcheck.js [chrome路径]
 * ========================================================================== */
const { spawn, execSync } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const ROOT = '<REPO>\\gui';
const PORT = 8813;
const CDP = 9353;
const CHROME = process.argv[2] || 'chrome';
const OUT = '<REPO>\\output\\logs\\bgcheck';
const DSF = 1.5;
const W = 1280, H = 820;
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png' };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = (p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: CDP, path: p }, (r) => {
    let b = ''; r.on('data', (c) => (b += c)); r.on('end', () => res(b));
  }).on('error', rej);
});

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

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const srv = await serve();
  const url = 'http://127.0.0.1:' + PORT + '/index.html';

  const child = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--mute-audio', '--remote-debugging-port=' + CDP,
    /* 🔴 缺这一条就量不到滚动条：headless 默认 overlay 滚动条 */
    '--disable-features=OverlayScrollbar',
    '--user-data-dir=<REPO>\\output\\.tmp\\bgcheck-profile',
    '--window-size=' + W + ',' + H, 'about:blank',
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
    if (r.exceptionDetails) throw new Error('page err: ' + JSON.stringify(r.exceptionDetails.exception));
    return r.result && r.result.value;
  };
  /* 🔴 模拟真机的 OS 偏好（浅色 app 模式）—— 不模拟这一步，"未声明 color-scheme"会落成深色滚动条，假绿 */
  const setOsScheme = (v) => send('Emulation.setEmulatedMedia', {
    media: '', features: [{ name: 'prefers-color-scheme', value: v }],
  });

  const checks = [];
  const push = (n, ok, d) => { checks.push({ name: n, ok: !!ok, detail: d }); console.log((ok ? 'PASS ' : 'FAIL ') + n + '  | ' + d); };
  const lum = (s) => { const m = /rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(s || ''); return m ? Math.max(+m[1], +m[2], +m[3]) : -1; };

  await send('Page.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: DSF, mobile: false });
  await setOsScheme('light');
  await send('Page.navigate', { url });
  for (let i = 0; i < 60; i++) {
    if (await ev('!!document.getElementById("nav-ind")')) break;
    await sleep(300);
  }
  await sleep(1500);

  /* 采样 .page 右缘滚动条：注入高块保证溢出（.page 本身 overflow-y:auto），
     再取右缘内侧 8 设备像素、竖直居中那一点。 */
  const sbPixel = async (file) => {
    const rect = await ev(`(function(){
      var p = document.querySelector('#main .page.active') || document.querySelector('#main .page');
      if(!p) return null;
      var r = p.getBoundingClientRect();
      return { l:r.left, t:r.top, w:r.width, h:r.height,
               sh:p.scrollHeight, ch:p.clientHeight, ov:getComputedStyle(p).overflowY };
    })()`);
    console.log('  .page rect =', JSON.stringify(rect));
    const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
    fs.writeFileSync(file, Buffer.from(shot.data, 'base64'));
    const x = Math.round((rect.l + rect.w) * DSF) - 8;
    const y = Math.round((rect.t + rect.h / 2) * DSF);
    const out = execSync('<REPO>/python313/python.exe gui/tools/pngprobe.py "' + file + '" --px ' + x + ',' + y).toString();
    const m = out.match(/rgba\([^)]+\)/);
    console.log('  滚动条采样 @(' + x + ',' + y + ') =', m ? m[0] : out.trim());
    return m ? m[0] : null;
  };
  const addSpacer = () => ev(`(function(){
    var p = document.querySelector('#main .page.active') || document.querySelector('#main .page');
    if(!p) return 0;
    if(!document.getElementById('__sbSpacer')){
      var s = document.createElement('div'); s.id='__sbSpacer';
      s.style.cssText='height:2200px;flex:0 0 auto;pointer-events:none;';
      p.appendChild(s);
    }
    return p.scrollHeight;
  })()`);

  // ---- A/B：静止态 body 必须透明（宿主材质透出；撤回第六轮的实色兜底）----
  const darkBody = await ev('(function(){document.documentElement.removeAttribute("data-theme");return getComputedStyle(document.body).backgroundColor;})()');
  const lightBody = await ev('(function(){document.documentElement.setAttribute("data-theme","light");return getComputedStyle(document.body).backgroundColor;})()');
  console.log('dark body bg  =', darkBody);
  console.log('light body bg =', lightBody);
  push('[深色静止] body 透明（材质透出）', darkBody === 'rgba(0, 0, 0, 0)', darkBody);
  push('[浅色静止] body 透明（材质透出）', lightBody === 'rgba(0, 0, 0, 0)', lightBody);
  await ev('document.documentElement.removeAttribute("data-theme"); 1');

  // ---- C：静止态 #main 透明 ----
  const mainSteady = await ev('(function(){var m=document.querySelector("#main");return {cls:m.className, bg:getComputedStyle(m).backgroundColor};})()');
  console.log('steady #main =', JSON.stringify(mainSteady));
  push('[静止] #main 透明（不垫任何底）', mainSteady.bg === 'rgba(0, 0, 0, 0)', mainSteady.bg);

  // ---- D/E：切页过渡中 #main 仍须透明、且类名不含任何 cover 类 ----
  const during = await ev(`(function(){
    var m = document.querySelector("#main");
    var nav = document.querySelector('#sidebar .nav[data-page="history"]');
    if(nav) nav.click();
    return { cls:m.className, bg:getComputedStyle(m).backgroundColor };
  })()`);
  console.log('during-transition #main =', JSON.stringify(during));
  push('[过渡中] #main 仍透明（材质不被盖）', during.bg === 'rgba(0, 0, 0, 0)', during.bg);
  const coverRe = /pg-transitioning|cover-in|page-hold/;
  push('[过渡中] #main 不含任何 cover 类（第七轮教训）', !coverRe.test(during.cls || ''), 'class="' + (during.cls || '') + '"');

  // ---- F/G：color-scheme 必须跟主题走 ----
  await sleep(900);
  const csDark = await ev('(function(){document.documentElement.removeAttribute("data-theme");return getComputedStyle(document.documentElement).colorScheme;})()');
  const csLight = await ev('(function(){document.documentElement.setAttribute("data-theme","light");return getComputedStyle(document.documentElement).colorScheme;})()');
  console.log('color-scheme dark =', csDark, '| light =', csLight);
  push('[深色] color-scheme=dark', csDark === 'dark', csDark);
  push('[浅色] color-scheme=light', csLight === 'light', csLight);
  await ev('document.documentElement.removeAttribute("data-theme"); 1');

  // ---- H：深色 + OS浅色 ⇒ 滚动条必须是深色（这一条就是"白闪没了"）----
  await ev('document.querySelector(\'#sidebar .nav[data-page="history"]\').click(); 1');
  await sleep(900);
  await addSpacer();
  await sleep(400);
  const sbDark = await sbPixel(path.join(OUT, 'scrollbar_dark.png'));
  const lDark = lum(sbDark);
  push('[深色+OS浅色] 右缘滚动条为深色（max<120，即无白杠）', lDark >= 0 && lDark < 120, sbDark + ' max=' + lDark);

  // ---- I：负对照 —— 把 color-scheme 掰回 light（= 修复前的真机状态）⇒ 同一像素必须变亮 ----
  await ev('document.documentElement.style.colorScheme="light"; 1');
  await sleep(250);
  const sbNeg = await sbPixel(path.join(OUT, 'scrollbar_negative.png'));
  const lNeg = lum(sbNeg);
  push('[负对照] 掰回 color-scheme:light ⇒ 滚动条变亮（max>180，白杠复现）', lNeg > 180,
    sbNeg + ' max=' + lNeg + '（修复前就是这个态）');
  await ev('document.documentElement.style.colorScheme=""; 1');
  await sleep(250);

  // ---- 浅色档正常态截图（材质透出核对）----
  await ev('document.documentElement.setAttribute("data-theme","light"); 1');
  await sleep(300);
  const shotL = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  fs.writeFileSync(path.join(OUT, 'normal_light.png'), Buffer.from(shotL.data, 'base64'));
  await ev('document.documentElement.removeAttribute("data-theme"); 1');
  await sleep(300);
  const shotD = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  fs.writeFileSync(path.join(OUT, 'normal_dark.png'), Buffer.from(shotD.data, 'base64'));

  const pass = checks.filter((c) => c.ok).length;
  console.log('\n=== bgcheck 结果 ' + pass + '/' + checks.length + ' ===');
  fs.writeFileSync(path.join(OUT, 'bgcheck.json'), JSON.stringify(checks, null, 2));
  srv.close();
  try { child.kill('SIGKILL'); } catch (e) {}
  process.exit(pass === checks.length ? 0 : 1);
})().catch((e) => { console.error('bgcheck 失败:', e); process.exit(2); });
