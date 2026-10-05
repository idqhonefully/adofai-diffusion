/* pagestack.js —— 「切页瞬间两个 .page 外框叠层导致底色加深」回归探针（第九轮）
 * ============================================================================
 * 主人报的 bug（原话 + 截图）：
 *   "他切换界面的时候那个小界面里面会闪一下不透明的白色或者黑色，咋回事"
 *   "我猜测是切换的时候两个这个卡片页重叠在一起导致颜色加深了一下"
 *
 * 🔴🔴 根因（读 CSS 定案，主人猜对了）：
 *   `.page.active, .page.leaving{ background:var(--fill-frame); }` —— **两页共用同一份底色**。
 *   切页时旧页挂 `.leaving`（`position:absolute; z-index:2` 叠在新页上方）跑 `page-leave`
 *   （**只渐隐 opacity 1→0**，`.1s`）；而新页的**外框底色不参与动画**（只有 `.page.active > *`
 *   这些子元素跑 `fl-rise-in`，带 100ms 延迟）。
 *   ⇒ 切页那一瞬（t≈0）：新页外框 `--fill-frame` 已经铺满 + 旧页外框 `--fill-frame` 还全不透明
 *     地压在它上面 ⇒ **同一层底色被叠了两遍**。
 *     浅色档 `--fill-frame: rgba(255,255,255,0.45)` ⇒ 叠两层有效 α = 1−(0.55)² = **0.6975**
 *     （45% → 70% 白）⇒ 面板一下变白 —— 就是主人看到的"闪一下不透明的白色"。
 *     深色档 `rgba(255,255,255,0.04)` ⇒ 0.04 → 0.0784（+3.8% 白），比浅色轻微但同源。
 *   ⚠ 之前的探针（pagemarginprobe 的 XF 段）只断言"旧页渐隐、新内容 hold"，测的是 **opacity
 *     时序 + 文字不叠**，**从没量过"两页外框底色叠了几层"** —— 所以一直漏掉这条。
 *
 * 判据（每档主题 2 条，共 4 条）：
 *   ① 切页瞬间（t=0 冻结）面板底色 Δ vs 静止态 —— 🔴 必须 ≈ 0（不许叠层加深）
 *   ② 对照：把 `.leaving` 的底色还原成 `--fill-frame`（复现旧行为）⇒ 同点必须明显变化
 * （即"复现旧行为"负对照，见铁律 5）
 *
 * 为什么能确定性量到：用 CDP 点真实 nav 触发 showPage，**同一次 evaluate 里**立刻
 *   `document.getAnimations().forEach(a=>{a.pause(); a.currentTime=0})`
 *   ⇒ 把两页动画都钉在 t=0（旧页 opacity=1、新页子元素 opacity=0，只剩两层外框可见）。
 *   再截图取面板空白区均色 —— 纯外框底色，不含卡片/文字。
 *
 * 用法：node tools/pagestack.js [chrome路径]
 * ========================================================================== */
const { spawn, execSync } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const ROOT = '<REPO>\\gui';
const PORT = 8815;
const CDP = 9355;
const CHROME = process.argv[2] || 'chrome';
const OUT = '<REPO>\\output\\logs\\pagestack';
const PY = '<REPO>/python313/python.exe';
const DSF = 1.5;
const W = 1280, H = 820;
/* 中性灰底：本程序 body/外框都透明、底色靠宿主材质透出；headless 里得给个"能同时看出
   变白 / 变暗"的中性背衬，否则浅色档 45% 白压在近白底上量不出 Δ（假绿）。 */
const BACKDROP = { r: 122, g: 122, b: 122, a: 255 };
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
    '--disable-features=OverlayScrollbar',
    '--user-data-dir=<REPO>\\output\\.tmp\\pagestack-profile',
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

  const checks = [];
  const push = (n, ok, d) => { checks.push({ name: n, ok: !!ok, detail: d }); console.log((ok ? 'PASS ' : 'FAIL ') + n + '  | ' + d); };

  await send('Page.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: DSF, mobile: false });
  await send('Emulation.setDefaultBackgroundColorOverride', { color: BACKDROP });
  await send('Page.navigate', { url });
  for (let i = 0; i < 60; i++) {
    if (await ev('!!document.getElementById("nav-ind")')) break;
    await sleep(300);
  }
  await sleep(1500);

  // 面板空白区（右下大块，避开标题/卡片）
  const panelRegion = async () => {
    const r = await ev(`(function(){
      var p = document.querySelector('#main .page.active');
      if(!p) return null;
      var b = p.getBoundingClientRect();
      return { l:b.left, t:b.top, w:b.width, h:b.height };
    })()`);
    if (!r) throw new Error('没找到 .page.active');
    /* ⚠ pngprobe 的 --avg 是 **x0,y0,W,H**（不是 x1,y1）—— 见 --help 里 --ascii 那条同样的坑。 */
    const x0 = Math.round((r.l + 0.45 * r.w) * DSF), y0 = Math.round((r.t + 0.35 * r.h) * DSF);
    const ww = Math.round(0.47 * r.w * DSF), hh = Math.round(0.57 * r.h * DSF);
    return { box: [x0, y0, ww, hh], rect: r };
  };
  const avgAt = async (file, box) => {
    const shot = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
    fs.writeFileSync(file, Buffer.from(shot.data, 'base64'));
    const out = execSync(PY + ' gui/tools/pngprobe.py "' + file + '" --avg ' + box.join(',')).toString();
    const m = out.match(/#[0-9A-Fa-f]{6}|rgba?\([^)]+\)/);
    return m ? m[0] : out.trim();
  };
  const rgb = (s) => {
    const t = (s || '').trim();
    let m = /^#([0-9a-fA-F]{6})$/.exec(t);
    if (m) { const n = parseInt(m[1], 16); return [(n >> 16) & 255, (n >> 8) & 255, n & 255]; }
    m = /rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(t);
    return m ? [+m[1], +m[2], +m[3]] : null;
  };
  const mean = (a) => Math.round((a[0] + a[1] + a[2]) / 3);

  const gotoPage = async (name) => {
    await ev(`(function(){var n=document.querySelector('#sidebar .nav[data-page="${name}"]'); if(n) n.click(); return !!n;})()`);
    await sleep(900);
  };
  /* 从当前页切到 target，并在同一帧把两页动画钉死在 t=0（旧页 opacity=1 / 新页子元素 opacity=0） */
  const switchFreezeT0 = async (target) => ev(`(function(){
    var n = document.querySelector('#sidebar .nav[data-page="${target}"]');
    if(!n) return 'NO_NAV';
    n.click();
    try { document.getAnimations().forEach(function(a){ a.pause(); a.currentTime = 0; }); } catch(e){}
    var oldP = document.querySelector('#main .page.leaving');
    var newP = document.querySelector('#main .page.active');
    return { leaving: !!oldP, active: newP ? newP.id : null,
             oldOp: oldP ? getComputedStyle(oldP).opacity : null,
             oldBg: oldP ? getComputedStyle(oldP).backgroundColor : null,
             newBg: newP ? getComputedStyle(newP).backgroundColor : null };
  })()`);

  const NEG_CSS = '.page.leaving{background:var(--fill-frame) !important;}';
  const clearNeg = () => ev(`(function(){var s=document.getElementById('__negStack'); if(s) s.remove(); return 1;})()`);
  const addNeg = () => ev(`(function(){var s=document.createElement('style'); s.id='__negStack'; s.textContent=${JSON.stringify(NEG_CSS)}; document.head.appendChild(s); return 1;})()`);

  for (const theme of ['dark', 'light']) {
    await ev(`document.documentElement.${theme === 'light' ? 'setAttribute("data-theme","light")' : 'removeAttribute("data-theme")'}; 1`);
    await clearNeg();
    await gotoPage('generate');
    await gotoPage('separate');           // 分离试听页：几乎全空，右下大块就是纯外框
    const r0 = await panelRegion();
    const pSingle = await avgAt(OUT + '\\' + theme + '_single.png', r0.box);

    /* A：当前代码（应已修）—— 切页瞬间两页外框是否叠层 */
    const stA = await switchFreezeT0('history');
    const pA = await avgAt(OUT + '\\' + theme + '_t0.png', r0.box);
    let a = rgb(pSingle), b = rgb(pA);
    const dA = (a && b) ? mean(b) - mean(a) : null;
    console.log(`\n[${theme}] A(当前) single=${pSingle}  t0=${pA}  Δ=${dA}  state=${JSON.stringify(stA)}`);
    push(`[${theme}] 切页瞬间面板底色 Δ ≈ 0（两页外框不叠层、不许加深）`, dA !== null && Math.abs(dA) <= 4,
      `single=${pSingle} t0=${pA} Δ=${dA}`);

    /* B：负对照 —— 强制旧页 .leaving 重新带 --fill-frame（复现修复前行为）*/
    await addNeg();
    await gotoPage('generate');
    await gotoPage('separate');
    const pSingle2 = await avgAt(OUT + '\\' + theme + '_neg_single.png', r0.box);
    await switchFreezeT0('history');
    const pB = await avgAt(OUT + '\\' + theme + '_neg_t0.png', r0.box);
    a = rgb(pSingle2); b = rgb(pB);
    const dB = (a && b) ? mean(b) - mean(a) : null;
    console.log(`[${theme}] B(负对照) single=${pSingle2}  t0=${pB}  Δ=${dB}`);
    push(`[${theme}][负对照] 强制旧页带底色 ⇒ 底色明显变化（探针确实看得见叠层）`, dB !== null && dB >= 4,
      `Δ=${dB}（修复前就是这个态）`);
    await clearNeg();
  }

  fs.writeFileSync(OUT + '\\pagestack.json', JSON.stringify(checks, null, 2));
  const pass = checks.filter((c) => c.ok).length;
  console.log('\n=== pagestack 结果 ' + pass + '/' + checks.length + ' ===');
  try { srv.close(); } catch (_e) {}
  try { child.kill(); } catch (_e) {}
  process.exit(pass === checks.length ? 0 : 1);
})();
