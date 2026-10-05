/* pagejump.js —— 量「切页瞬间旧页会不会跳位」+ 出留证图。
 * ============================================================================
 * 背景（2026-09-29 第五轮，主人报的 bug）：
 *   主人原话："我在切换页面的一瞬间，旧的页面直接往左上角飞了一段距离，怎么搞的？"
 *
 * 根因（本轮定位，两条叠加）：
 *   ① **padding 丢失**（主因，量级最大）
 *      .page.active{padding:18px 20px 22px} 是写在**active 那条规则**里的。
 *      showPage 摘掉 active、挂上 .leaving 之后，`.page.active` 不再匹配，
 *      而 `.page.leaving` 里没写 padding ⇒ 旧页的 padding **瞬间变成 0**
 *      ⇒ 里面所有内容整体**向左 20px、向上 18px**跳一下。
 *   ② **inset 基准不对**（次因）
 *      `.page.leaving{position:absolute; inset:0}` 的 inset 是相对**包含块的 padding box**，
 *      而 #main 有 `padding:8px 8px 8px 2px`。静态的 .page.active 在 **内容盒**里
 *      （内缩 上8/右8/下8/左2），absolute 后就跑到 padding box 上 ⇒ 外框
 *      **左移 2px、上移 8px、右扩 8px、下扩 8px**（整张卡"变大贴边"）。
 *   ③ **滚动条槽丢失**（附带）
 *      .page.active 有 `scrollbar-gutter:stable`（常驻槽位），.page.leaving 只有
 *      `overflow:hidden` ⇒ 旧页内宽多出 12px，内容横向抖一下。
 *
 * 判据（对每组 源页→目标页 的切换，逐条量）：
 *   A 旧页外框 rect  Δx=Δy=0            （不跳位）
 *   B 旧页外框 rect  Δw=Δh=0            （不变形）
 *   C 旧页首个子元素 rect Δx=Δy=0        （内容不跟着跳）
 *   D 旧页 scrollTop 不变                （不跳回顶部）
 *   E 旧页 computed padding == active 的 padding
 *   F 旧页 computed overflow-y / scrollbar-gutter == active 的
 *   G 切换后确实只有旧页挂 .leaving、新页已 active
 *
 * ⚠ 采样是**同帧同步**的：nav.click() 之下 showPage 同步跑完，紧接着
 *   getBoundingClientRect() 强制同步布局，读到的就是"挂完 .leaving 之后"的真实几何。
 *   page-leave 只改 opacity，不影响 rect ⇒ 不会污染测量。
 *   且必须在 .leaving 被 140ms 定时器摘掉之前读完 —— 同一次 evaluate 里读完，天然满足。
 *
 * 用法：node tools/pagejump.js [url] [chrome路径] [输出目录]
 *   url 省略时自起静态服务（纯几何，不需要后端）。
 * ========================================================================== */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const ROOT = '<REPO>\\gui';
const PORT = 8812;                       // 静态服务（仅在 url 省略时启用）
const CDP = 9352;
const CHROME = process.argv[3] || 'chrome';
const OUT = process.argv[4] || '<REPO>\\output\\logs\\pagejump';
const BACKDROP = '#0E1116';              // 材质基色（主人那台实测档位）
const USE_URL = process.argv[2] || '';
const DSF = 1.5;                         // 主人那台 150% 缩放
const W = 1100, H = 760;
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png' };

/* 被测的切换组合：源页 → 目标页（覆盖高/矮、有/无滚动条两类源页） */
const CASES = [
  ['generate', 'history'],
  ['history', 'train'],
  ['train', 'workbench'],
  ['workbench', 'generate'],
];

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
  const srv = USE_URL ? null : await serve();
  const url = USE_URL || ('http://127.0.0.1:' + PORT + '/index.html');

  const child = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--mute-audio', '--remote-debugging-port=' + CDP,
    '--user-data-dir=<REPO>\\output\\.tmp\\pagejump-profile',
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

  await send('Page.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: DSF, mobile: false });
  await send('Page.navigate', { url });
  for (let i = 0; i < 60; i++) {
    if (await ev('!!document.getElementById("nav-ind")')) break;
    await sleep(300);
  }
  await sleep(1500);

  // 垫底色（页面侧栏/外框都是 transparent，靠 DWM 材质当底；headless 没材质 ⇒ 必须自己垫）
  await ev('document.documentElement.style.background=' + JSON.stringify(BACKDROP) + ';1');
  await sleep(300);

  /* 页面内的采样器：一次 evaluate 内同步读完"挂 .leaving 前 / 后"的几何 */
  await ev(`window.__pjProbe = function(target){
    var R = function(el){ if(!el) return null; var r = el.getBoundingClientRect();
      return { x:+r.left.toFixed(2), y:+r.top.toFixed(2), w:+r.width.toFixed(2), h:+r.height.toFixed(2) }; };
    var CS = function(el){ var c = getComputedStyle(el);
      return { pos:c.position, pad:c.padding, ovX:c.overflowX, ovY:c.overflowY,
               gutter:c.scrollbarGutter, disp:c.display, z:c.zIndex, op:c.opacity }; };
    var first = function(p){ return p ? p.querySelector(':scope > *') : null; };
    var old = document.querySelector('#main .page.active');
    if(!old) return { err:'no active page' };
    var rec = { src:old.id, dst:'page-'+target,
      before:{ page:R(old), kid:R(first(old)), scrollTop:old.scrollTop, cs:CS(old) } };
    var nav = document.querySelector('#sidebar .nav[data-page="'+target+'"]');
    if(!nav) return { err:'no nav ' + target };
    nav.click();                                    /* ← 真实切页路径 */
    var lv = document.querySelector('#main .page.leaving');
    var ac = document.querySelector('#main .page.active');
    rec.leavingFound = !!lv;
    rec.leavingId = lv ? lv.id : null;
    rec.activeId  = ac ? ac.id : null;
    rec.activeRect = ac ? R(ac) : null;
    rec.activeCs   = ac ? CS(ac) : null;
    if(lv){
      rec.after = { page:R(lv), kid:R(first(lv)), scrollTop:lv.scrollTop, cs:CS(lv) };
    }
    return rec;
  }; 1`);

  const checks = [];
  const push = (n, ok, d) => checks.push({ name: n, ok: !!ok, detail: d });
  const d2 = (a, b) => +(a - b).toFixed(2);

  for (let i = 0; i < CASES.length; i++) {
    const [from, to] = CASES[i];
    // 先稳定地进到源页
    await ev('document.querySelector(\'#sidebar .nav[data-page="' + from + '"]\').click(); 1');
    await sleep(1300);                              // 等切页动画 + animating 摘除全部走完
    const r = await ev('window.__pjProbe(' + JSON.stringify(to) + ')');
    const tag = from + '→' + to;

    if (r.err || !r.leavingFound || !r.after) {
      push('[' + tag + '] 切页后应存在 .leaving 旧页', false, JSON.stringify(r));
      await sleep(500);
      continue;
    }
    const dx = d2(r.after.page.x, r.before.page.x);
    const dy = d2(r.after.page.y, r.before.page.y);
    const dw = d2(r.after.page.w, r.before.page.w);
    const dh = d2(r.after.page.h, r.before.page.h);
    const kdx = d2(r.after.kid.x, r.before.kid.x);
    const kdy = d2(r.after.kid.y, r.before.kid.y);

    console.log('CASE ' + tag +
      '\n  旧页外框 before ' + JSON.stringify(r.before.page) + '  after ' + JSON.stringify(r.after.page) +
      '\n           Δ ' + JSON.stringify({ dx, dy, dw, dh }) +
      '\n  旧页首子 before ' + JSON.stringify(r.before.kid) + '  after ' + JSON.stringify(r.after.kid) +
      '\n           Δ ' + JSON.stringify({ kdx, kdy }) +
      '\n  旧页 padding before ' + r.before.cs.pad + '  after ' + r.after.cs.pad +
      '\n  旧页 overflow-y before ' + r.before.cs.ovY + '  after ' + r.after.cs.ovY +
      '  gutter before ' + r.before.cs.gutter + '  after ' + r.after.cs.gutter +
      '\n  旧页 scrollTop before ' + r.before.scrollTop + '  after ' + r.after.scrollTop +
      '\n  旧页 id ' + r.leavingId + '  新页 id ' + r.activeId +
      '\n  新页外框 ' + JSON.stringify(r.activeRect) + ' padding ' + (r.activeCs ? r.activeCs.pad : '-'));

    push('[' + tag + '] 旧页外框不位移（Δx=Δy=0）', dx === 0 && dy === 0, 'Δx=' + dx + ' Δy=' + dy);
    push('[' + tag + '] 旧页外框不变形（Δw=Δh=0）', dw === 0 && dh === 0, 'Δw=' + dw + ' Δh=' + dh);
    push('[' + tag + '] 旧页内容不跳（首子元素 Δx=Δy=0）', kdx === 0 && kdy === 0, 'Δx=' + kdx + ' Δy=' + kdy);
    push('[' + tag + '] 旧页 scrollTop 不变', r.after.scrollTop === r.before.scrollTop,
      r.before.scrollTop + '→' + r.after.scrollTop);
    /* ⚠ 基准取**旧页自己切换前**的值 —— 不能拿"新页此刻的值"当基准：新页刚被激活，
       正挂着 .page.active.animating{overflow:hidden}，overflow-y 天然是 hidden，
       拿它比会误报（本探针首版就踩了这个坑，4 组全假 FAIL）。 */
    push('[' + tag + '] 旧页 padding 前后一致（不丢）', r.after.cs.pad === r.before.cs.pad,
      r.before.cs.pad + ' → ' + r.after.cs.pad);
    push('[' + tag + '] 旧页 overflow-y 前后一致', r.after.cs.ovY === r.before.cs.ovY,
      r.before.cs.ovY + ' → ' + r.after.cs.ovY);
    push('[' + tag + '] 旧页 scrollbar-gutter 前后一致', r.after.cs.gutter === r.before.cs.gutter,
      r.before.cs.gutter + ' → ' + r.after.cs.gutter);
    push('[' + tag + '] 新页外框与旧页切换前同位同尺寸（页边一动没动）',
      !!(r.activeRect && r.before.page) &&
      r.activeRect.x === r.before.page.x && r.activeRect.y === r.before.page.y &&
      r.activeRect.w === r.before.page.w && r.activeRect.h === r.before.page.h,
      JSON.stringify(r.activeRect) + ' vs ' + JSON.stringify(r.before.page));
    push('[' + tag + '] 旧页是上一个源页、新页是目标页', r.leavingId === 'page-' + from && r.activeId === 'page-' + to,
      r.leavingId + ' / ' + r.activeId);
    await sleep(500);
  }

  const pass = checks.filter((c) => c.ok).length;
  console.log('\n===== pagejump 结果 ' + pass + '/' + checks.length + ' =====');
  checks.forEach((c) => console.log((c.ok ? 'PASS ' : 'FAIL ') + c.name + (c.ok ? '' : '   <<< ' + c.detail)));
  fs.writeFileSync(path.join(OUT, 'pagejump.json'), JSON.stringify(checks, null, 2));

  try { child.kill(); } catch (_e) { /* ignore */ }
  if (srv) try { srv.close(); } catch (_e) { /* ignore */ }
  process.exit(pass === checks.length ? 0 : 1);
})().catch((e) => { console.error('pagejump 崩了: ' + e.stack); process.exit(2); });
