/* stepfold.js —— 「生成页第 2 步 · 折叠区塌不干净」回归探针（第十一轮，含 #overlay 内联面板）
 * ============================================================================
 * 主人报的 bug（截图 + 原话）：
 *   "这个中间空这么大咋回事"
 *   —— 生成页第 2 步，OSN1 档下「采点方式」行和「当前选择」之间空出 180~260px 纯空白，
 *      切到 MuScriptor 再切回来，空白照旧。
 *
 * 🔴🔴 根因（读 CSS + 实测 DOM 定案，两层缺一不可）：
 *   `#precisionGrid` 靠 `grid-template-rows:1fr ⇄ 0fr` 做手风琴（2026-09-28 加的）。
 *   ① `0fr` 只压**轨道的可用空间**，而轨道的下限默认是 `minmax(auto,0fr)` —— 这个 `auto`
 *      等于网格项的 **min-content 贡献**。原来三张 `.pcard` 直接就是网格项，它们是
 *      `min-height:auto` + `overflow:visible` ⇒ 自动最小尺寸 = 内容高度（实测 **181.4px**）
 *      ⇒ 轨道被内容顶住，塌不到 0（卡 `opacity:0` 看不见，但实体占位 = 主人看到的空白）。
 *   ② 光给 `.pcard` 补 `min-height:0` 也不够：卡片自带 `padding:18px 16px` + 1px 边框，
 *      盒模型自身的下限 = 18×2+1×2 = **38px**，轨道停在 38px（实测过）。
 *   ③ 顺带：因为折叠/展开两态的 `grid-template-rows` 算出来**都是 181.375px**，
 *      这个"折叠动效"其实**一次都没真折叠过**，过渡全程空转。
 *   ⇒ 修法：照本文件既有模式（`.stem-body > .stem-inner` / `.exp-body > .exp-inner`）多包一层
 *      `.prec-inner`（无内边距无边框 + min-height:0 + overflow:hidden）当网格项，
 *      `.pcard` 挪进它里面。塌缩发生在 inner 上，卡片自己的 padding 不参与下限。
 *   ⚠ 同一个坑本文件踩第三次（stem / exp 各一次，注释里都写着"必须 min-height:0"）——
 *      所以这条必须由探针守着，不能只靠注释。genmode_check 只断言"类名折叠了"（桩环境、
 *      没有布局），量不到高度 ⇒ 这类 bug 它天然看不见。
 *
 * 判据（10 条）：
 *   A1 折叠态 `#precisionGrid` 高度 ≤ 1px
 *   A2 折叠态 `grid-template-rows` 计算值解析 ≤ 1px
 *   A3 折叠态「采点方式行 → 当前选择」间隙 ≤ 24px（只允许 hint 自己的 12px 上边距）
 *   A4 折叠态网格项（`.prec-inner`）高度 ≤ 1px —— 塌缩必须发生在这一层
 *   A5 展开态高度 ≥ 100px 且卡片 opacity=1
 *   A6 展开态容器高度 == 卡片自然高度（Δ ≤ 1.5px）⇒ min-height:0/裁剪没把卡片压矮
 *   A7 展开曲线有 ≥ 6 个严格中间值 ⇒ 真在补间（修复前是一帧到顶 = 硬切）
 *   A8 🔴 负对照：把 `.prec-inner` 掰回 `min-height:auto` + `overflow:visible`
 *      （= 复现"网格项没解锁"的旧根因）⇒ 折叠态高度又 ≥ 100px
 *   A9/A10 【家族扫一遍】「三种采点方式怎么选」Expander（`.exp-body > .exp-inner`）同样
 *      收起 ≤1px / 展开 ≥100px —— 它和 `.stem-body > .stem-inner` 是同款 `0fr` 折叠，
 *      属于**同一个坑的另外两个实例**（stem 那处由 `stemsprobe.js` 断言 `innerHClosed` 守着）。
 *
 *   2026-09-30 主人截图报新 bug：点完「开始生成」后那条内联进度面板（#overlay，含 stageList
 *   ①–⑤）糊在了「三种采点方式怎么选」卡片上 = 面板塌不掉、把内容画到兄弟卡片上。
 *   🔴🔴 根因 = #overlay 当时写了 `align-items:start`：网格项 `.ot` 不被拉伸到 0fr 轨道、
 *      保留自然高度（实测 263px）⇒ 占 0 布局高度却把内联面板画到下面卡片上。
 *   E1 收起态 #overlay 高度 ≤ 1px（真塌缩）
 *   E2 收起态网格项 .ot 高度 ≤ 1px（塌缩发生在 .ot，不是 align-items 顶住）
 *   E3 收起态内容不溢出折叠容器：.ot 高度 − #overlay 高度 ≤ 1px（否则 .ot 撑住画到兄弟卡片上）。
 *          ⚠ 同层"后出现兄弟"会盖住"先出现元素的溢出绘制"，elementFromPoint 探不到这层泄漏，
 *          故以 overflow 关系作判据（旧 align-items:start 会让 overflow 涨到 ~263px）。
 *   E4/E5 【负对照】给 #overlay 注回 `align-items:start` ⇒ .ot 撑回 ≥100px、overflow 涨回 ≥100
 *          （复现旧泄漏，探针确实看得见）；撤掉注入 ⇒ 立刻回 ≤1px（证明是这条在起决定作用）。
 *
 * 用法：node tools/stepfold.js [chrome路径]
 * ========================================================================== */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const ROOT = '<REPO>\\gui';
const PORT = 8817;
const CDP = 9357;
const CHROME = process.argv[2] || 'chrome';
const OUT = '<REPO>\\output\\logs\\stepfold';
const DSF = 1.5;
const W = 1280, H = 820;
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2' };

/* 假宿主桥：页面脚本一进来就 chrome.webview.addEventListener，没有宿主直接抛 TypeError */
const STUB = `
window.__SENT = []; window.__LISTENERS = [];
window.chrome = window.chrome || {};
window.chrome.webview = {
  postMessage: function (s) { window.__SENT.push(s); },
  addEventListener: function (t, f) { if (t === 'message') window.__LISTENERS.push(f); }
};
`;

/* 负对照：复现旧根因（网格项没解锁 ⇒ 轨道被内容顶住） */
const NEG_CSS = '.precision > .prec-inner{min-height:auto !important;overflow:visible !important;}';

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
    '--user-data-dir=<REPO>\\output\\.tmp\\stepfold-profile',
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
  await send('Page.addScriptToEvaluateOnNewDocument', { source: STUB });
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: DSF, mobile: false });
  await send('Page.navigate', { url });
  for (let i = 0; i < 60; i++) {
    if (await ev('!!document.getElementById("nav-ind")')) break;
    await sleep(300);
  }
  await sleep(1500);

  /* ---- 快照：一次取齐"容器高度 / 计算行高 / 网格项高度 / 间隙 / 卡片高度" ---- */
  const SNAP = `(function(){
    var g = document.getElementById('precisionGrid');
    if (!g) return null;
    var inner = g.firstElementChild;
    var pane = document.querySelectorAll('.step-pane')[1];
    var sel  = pane.querySelector('.selbar').getBoundingClientRect();
    var hint = pane.querySelector('.hint').getBoundingClientRect();
    var card = g.querySelector('.pcard');
    var r = g.getBoundingClientRect(), ri = inner ? inner.getBoundingClientRect() : null;
    return {
      folded: g.classList.contains('folded'),
      gridH: +r.height.toFixed(2),
      rows: getComputedStyle(g).gridTemplateRows,
      innerTag: inner ? (inner.className || inner.tagName) : null,
      innerH: ri ? +ri.height.toFixed(2) : null,
      innerMinH: inner ? getComputedStyle(inner).minHeight : null,
      innerOv: inner ? getComputedStyle(inner).overflow : null,
      gap: +(hint.top - sel.bottom).toFixed(2),
      cardH: card ? +card.getBoundingClientRect().height.toFixed(2) : null,
      cardOp: card ? getComputedStyle(card).opacity : null
    };
  })()`;

  const setNeg = (on) => ev(`(function(){
    var s = document.getElementById('__negFold');
    if (${on ? 'true' : 'false'}) {
      if (!s) { s = document.createElement('style'); s.id = '__negFold'; s.textContent = ${JSON.stringify(NEG_CSS)}; document.head.appendChild(s); }
    } else if (s) { s.remove(); }
    return 1;
  })()`);

  const shot = async (name) => {
    const s = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
    fs.writeFileSync(OUT + '\\' + name, Buffer.from(s.data, 'base64'));
  };

  /* 进生成页第 2 步 */
  await ev(`document.querySelector('#sidebar .nav[data-page=generate]').click(); 1`);
  await sleep(900);
  await ev(`document.querySelector('#miniStep .stp[data-s="2"]').click(); 1`);
  await sleep(900);

  /* ---------- A：折叠态（OSN1，默认档） ---------- */
  await ev(`(function(){ var b=document.getElementById('modeOsn1'); if(b) b.click(); return 1; })()`);
  await sleep(900);
  const a = await ev(SNAP);
  if (!a) throw new Error('没找到 #precisionGrid（结构被改过？探针与页面契约失配）');
  console.log('\n[折叠态] ' + JSON.stringify(a));
  await shot('step2_folded.png');
  push('折叠态：精度区高度 ≤ 1px（真塌缩，不留空白）', a.folded && a.gridH <= 1, `gridH=${a.gridH}px rows=${a.rows}`);
  push('折叠态：grid-template-rows 解析 ≤ 1px', /^0(\.0+)?px$/.test(a.rows.trim()), `rows=${a.rows}`);
  push('折叠态：采点方式行→「当前选择」间隙 ≤ 24px（只剩 hint 自己的 12px 上边距）', a.gap <= 24, `gap=${a.gap}px`);
  push('折叠态：网格项（.prec-inner）高度 ≤ 1px —— 塌缩发生在这一层', a.innerH !== null && a.innerH <= 1, `inner=${a.innerTag} h=${a.innerH} min-h=${a.innerMinH} overflow=${a.innerOv}`);

  /* ---------- B：展开态（MuScriptor）+ 补间曲线 ---------- */
  await ev(`document.getElementById('modeMus').click(); 1`);
  const curve = [];
  for (let i = 0; i < 26; i++) { curve.push(+(await ev('+document.getElementById("precisionGrid").getBoundingClientRect().height.toFixed(1)'))); await sleep(16); }
  await sleep(500);
  const b = await ev(SNAP);
  console.log('\n[展开态] ' + JSON.stringify(b));
  console.log('[展开曲线] ' + curve.join(' '));
  await shot('step2_unfolded.png');
  push('展开态：精度区高度 ≥ 100px 且卡片 opacity=1（真展开）', b.gridH >= 100 && +b.cardOp === 1, `gridH=${b.gridH}px cardH=${b.cardH} opacity=${b.cardOp}`);
  const dCard = (b.gridH !== null && b.cardH !== null) ? Math.abs(b.gridH - b.cardH) : null;
  push('展开态：容器高度 == 卡片自然高度（Δ≤1.5px，min-height:0/裁剪没压矮卡片）', dCard !== null && dCard <= 1.5, `gridH=${b.gridH} cardH=${b.cardH} Δ=${dCard === null ? 'n/a' : dCard.toFixed(2)}`);
  const full = Math.max.apply(null, curve);
  const mids = curve.filter((v) => v > 1 && v < full - 1).length;
  push('展开曲线有 ≥ 6 个严格中间值（真补间，不是一帧到顶）', mids >= 6, `中间帧=${mids} 满高=${full} 曲线=${curve.slice(0, 14).join(',')}…`);

  /* ---------- C：负对照（复现旧根因：网格项没解锁） ---------- */
  await ev(`document.getElementById('modeOsn1').click(); 1`);
  await sleep(900);
  await setNeg(true);
  await sleep(300);
  const c = await ev(SNAP);
  console.log('\n[负对照·折叠态] ' + JSON.stringify(c));
  await shot('step2_negcontrol.png');
  push('[负对照] 掰回 min-height:auto + overflow:visible ⇒ 折叠态又留 ≥100px（复现旧根因，探针确实看得见）', c.gridH >= 100, `gridH=${c.gridH}px（修复前就是这个态）`);
  await setNeg(false);
  await sleep(400);
  const d = await ev(SNAP);
  push('[负对照] 撤掉注入 ⇒ 立刻回到 ≤1px（证明是这条在起决定作用，不是别的）', d.gridH <= 1, `gridH=${d.gridH}px`);

  /* ---------- D：家族扫一遍 —— 「三种采点方式怎么选」Expander（同款 0fr 折叠） ---------- */
  const EXP = `(function(){
    var ex = document.getElementById('pickHelp');
    if (!ex) return null;
    var body = ex.querySelector('.exp-body'), inner = ex.querySelector('.exp-inner');
    return { open: ex.classList.contains('open'),
             h: +body.getBoundingClientRect().height.toFixed(2),
             rows: getComputedStyle(body).gridTemplateRows,
             innerH: inner ? +inner.getBoundingClientRect().height.toFixed(2) : null };
  })()`;
  const e1 = await ev(EXP);
  await ev(`(function(){ var h=document.getElementById('pickHelpHead'); if(h) h.click(); return 1; })()`);
  await sleep(900);
  const e2 = await ev(EXP);
  console.log('\n[Expander·收起] ' + JSON.stringify(e1) + '\n[Expander·展开] ' + JSON.stringify(e2));
  push('[家族] Expander 收起态 .exp-body 高度 ≤ 1px（同款 0fr 折叠也得塌干净）',
    !!e1 && !e1.open && e1.h <= 1 && (e1.innerH === null || e1.innerH <= 1), e1 ? `h=${e1.h} inner=${e1.innerH} rows=${e1.rows}` : 'no #pickHelp');
  push('[家族] Expander 展开态高度 ≥ 100px（真展开、内容不被裁掉）',
    !!e2 && e2.open && e2.h >= 100, e2 ? `h=${e2.h} inner=${e2.innerH}` : 'no #pickHelp');

  /* ---------- E：「正在生成」内联面板 #overlay 的 0fr 折叠也得塌干净（2026-09-30 主人截图） ----------
   * 🔴🔴 同款坑第五处：#overlay 用 grid-template-rows:0fr⇄1fr 做手风琴（2026-09-30 新加的内联面板），
   *     网格项 = .ot（#overlay>.ot{min-height:0;overflow:hidden}）。
   *     根因复现：若给 #overlay 写 align-items:start，.ot 不被拉伸到 0fr 轨道、保留自然高度
   *           （实测 263px）⇒ 面板占 0 布局高度却把内容画到兄弟卡片上（主人看到的"糊住"）。
   *     修法：删掉 align-items:start（本文件另两处手风琴 .exp-body/.precision 都没写 ⇒ 默认 stretch ⇒ 干净）。 */
  const OV = `(function(){
    var o = document.getElementById('overlay');
    if (!o) return null;
    var ot = o.querySelector('.ot');
    var r = o.getBoundingClientRect();
    var otr = ot ? ot.getBoundingClientRect() : null;
    var cx = r.left + r.width / 2;
    var probeY = r.top + 100;               /* 落在"若泄漏，.ot 会画到这里"的位置 */
    var elAt = document.elementFromPoint(cx, probeY);
    return {
      show: o.classList.contains('show'),
      gridH: +r.height.toFixed(2),
      rows: getComputedStyle(o).gridTemplateRows,
      align: getComputedStyle(o).alignItems,
      otH: otr ? +otr.height.toFixed(2) : null,
      otMinH: ot ? getComputedStyle(ot).minHeight : null,
      otOv: ot ? getComputedStyle(ot).overflow : null,
      probeX: +cx.toFixed(1), probeY: +probeY.toFixed(1),
      elAt: elAt ? (elAt.id || elAt.className || elAt.tagName) : null,
      elIsPanel: !!(elAt && (elAt.id === 'overlay' || elAt.id === 'genProg' || elAt.id === 'stageList' || (elAt.closest && elAt.closest('#overlay'))))
    };
  })()`;
  const setOvNeg = (on) => ev(`(function(){
    var s = document.getElementById('__negOverlay');
    if (${on ? 'true' : 'false'}) {
      if (!s) { s = document.createElement('style'); s.id = '__negOverlay';
        s.textContent = '#overlay{align-items:start !important;}'; document.head.appendChild(s); }
    } else if (s) { s.remove(); }
    return 1;
  })()`);

  const o1 = await ev(OV);
  console.log('\n[Overlay·收起] ' + JSON.stringify(o1));
  /* overflow = .ot 高度 − #overlay 容器高度：折叠态应为 0（内容不溢出半透明"盒子"）。
   * 旧的 align-items:start 会让 .ot 撑到 263px 而容器仍是 0 ⇒ overflow=263 ⇒ 内容画到下面卡片上。
   * 注意：同层里"后出现的兄弟"会盖住"先出现元素的溢出绘制"，所以 elementFromPoint 探不到这层泄漏；
   *      真正能区分修复前后的判据是 overflow（下面 E4 负对照会让它涨回 ≥100）。 */
  const ovOverlap = (o1 && o1.otH !== null) ? +(o1.otH - o1.gridH).toFixed(2) : null;
  push('[Overlay] 收起态 #overlay 高度 ≤ 1px（真塌缩）',
    !!o1 && !o1.show && o1.gridH <= 1, o1 ? `gridH=${o1.gridH} rows=${o1.rows}` : 'no #overlay');
  push('[Overlay] 收起态网格项 .ot 高度 ≤ 1px（塌缩发生在 .ot，不是 align-items 顶住）',
    !!o1 && o1.otH !== null && o1.otH <= 1, o1 ? `otH=${o1.otH} min-h=${o1.otMinH} overflow=${o1.otOv}` : 'no #overlay');
  push('[Overlay] 收起态内容不溢出容器：.ot 高度 − #overlay 高度 ≤ 1px（否则画到兄弟卡片上）',
    ovOverlap !== null && ovOverlap <= 1, o1 ? `overflow=${ovOverlap} otH=${o1.otH} gridH=${o1.gridH}` : 'no #overlay');

  /* E 负对照：给 #overlay 注回 align-items:start ⇒ 泄漏复现（.ot 撑到 ≥100px + 探测点变面板内部） */
  await setOvNeg(true);
  await sleep(250);
  const o2 = await ev(OV);
  console.log('\n[Overlay·负对照] ' + JSON.stringify(o2));
  push('[Overlay·负对照] 注回 align-items:start ⇒ .ot 撑回 ≥100px（复现旧泄漏，探针确实看得见）',
    !!o2 && o2.otH !== null && o2.otH >= 100, o2 ? `otH=${o2.otH} align=${o2.align}` : 'n/a');
  await setOvNeg(false);
  await sleep(250);
  const o3 = await ev(OV);
  push('[Overlay·负对照] 撤掉注入 ⇒ .ot 立刻回 ≤1px（证明是 align-items 在起决定作用）',
    !!o3 && o3.otH !== null && o3.otH <= 1, o3 ? `otH=${o3.otH}` : 'n/a');

  fs.writeFileSync(OUT + '\\stepfold.json', JSON.stringify({ checks: checks, folded: a, unfolded: b, negFold: c, restored: d, expClosed: e1, expOpen: e2, ovClosed: o1, ovNeg: o2, ovRestored: o3, curve: curve }, null, 2));
  const pass = checks.filter((x) => x.ok).length;
  console.log('\n=== stepfold 结果 ' + pass + '/' + checks.length + ' ===');
  try { srv.close(); } catch (_e) { }
  try { child.kill(); } catch (_e) { }
  process.exit(pass === checks.length ? 0 : 1);
})().catch((e) => { console.error('【失败】' + (e && e.stack || e)); process.exit(1); });
