// sidebarprobe.js —— 侧栏几何诊断（量 DOM 盒子，不猜像素）
// 用法：node tools/sidebarprobe.js [url] [chrome.exe]
//   默认 url = http://127.0.0.1:8799/index.html
//
// 回答三个问题：
//   ① 汉堡图标与导航图标是否同一条竖轴（"歪"）
//   ② 展开/收回的过渡里，图标有没有横向位移（"飞"）
//   ③ 折叠态下 .nav-group.bottom / .foot 各自落在哪（"没放最下面"）
const { spawn } = require('child_process');
const fs = require('fs');
const http = require('http');

const URL_ = process.argv[2] || 'http://127.0.0.1:8799/index.html';
const CHROME = process.argv[3] || 'chrome';
const PORT = 9342;
const UDD = '<REPO>\\output\\.tmp\\sidebarprobe-profile';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = (p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: PORT, path: p }, (r) => {
    let b = ''; r.on('data', (c) => (b += c)); r.on('end', () => res(b));
  }).on('error', rej);
});

const BRIDGE = `
window.__SENT = [];
window.__HANDLERS = [];
window.__err = '';
window.addEventListener('error', function(e){ window.__err += (e.message||'') + '\\n'; });
window.chrome = window.chrome || {};
window.chrome.webview = {
  postMessage: function (s) { try { window.__SENT.push(JSON.parse(s)); } catch (e) {} },
  addEventListener: function (t, fn) { if (t === 'message') window.__HANDLERS.push(fn); }
};
`;

// 量盒子的页面函数：返回每个关心的元素的 rect + 横向中心
const SNAP = `(function(){
  function R(sel){
    var e = document.querySelector(sel);
    if (!e) return null;
    var r = e.getBoundingClientRect();
    return { x:+r.x.toFixed(1), y:+r.y.toFixed(1), w:+r.width.toFixed(1), h:+r.height.toFixed(1),
             cx:+(r.x + r.width/2).toFixed(1), cy:+(r.y + r.height/2).toFixed(1) };
  }
  function CS(sel, prop){
    var e = document.querySelector(sel); if (!e) return '(无)';
    return getComputedStyle(e)[prop];
  }
  var navs = document.querySelectorAll('#sidebar .nav');
  var navIcos = [];
  for (var i = 0; i < navs.length; i++){
    var n = navs[i];
    var ico = n.querySelector('.ico');
    var ir = ico ? ico.getBoundingClientRect() : null;
    navIcos.push({
      page: n.getAttribute('data-page'),
      nav: (function(){ var r=n.getBoundingClientRect(); return {x:+r.x.toFixed(1),y:+r.y.toFixed(1),w:+r.width.toFixed(1),h:+r.height.toFixed(1)}; })(),
      icoCx: ir ? +(ir.x + ir.width/2).toFixed(1) : null,
      icoCy: ir ? +(ir.y + ir.height/2).toFixed(1) : null,
      icoW: ir ? +ir.width.toFixed(1) : null,
      icoH: ir ? +ir.height.toFixed(1) : null
    });
  }
  return JSON.stringify({
    sb: R('#sidebar'),
    expanded: document.getElementById('sidebar').classList.contains('expanded'),
    toggleBtn: R('#navToggle'),
    toggleIco: R('#navToggle .ico'),
    toggleIcoMarginRight: CS('#navToggle .ico', 'marginRight'),
    navIcoMarginRight: CS('#sidebar .nav .ico', 'marginRight'),
    navToggleJustify: CS('#navToggle', 'justifyContent'),
    navToggleAlign: CS('#navToggle', 'alignItems'),
    navDirection: CS('#sidebar .nav', 'flexDirection'),
    navJustify: CS('#sidebar .nav', 'justifyContent'),
    lblDisplay: CS('#sidebar .nav .lbl', 'display'),
    ind: R('#nav-ind'),
    sep: R('#nav-sep') || R('.nav-sep'),
    bottomGroup: R('.nav-group.bottom'),
    foot: R('.foot'),
    footMarginTop: CS('.foot', 'marginTop'),
    bottomGroupMarginTop: CS('.nav-group.bottom', 'marginTop'),
    navIcos: navIcos
  });
})()`;

(async () => {
  fs.mkdirSync(UDD, { recursive: true });
  const child = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--mute-audio', `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${UDD}`, '--window-size=1200,760', 'about:blank',
  ], { stdio: 'ignore', windowsHide: true });

  let list = [];
  for (let i = 0; i < 80; i++) {
    try {
      list = JSON.parse(await get('/json/list'));
      if (list.some((t) => t.type === 'page')) break;
    } catch (_e) {}
    await sleep(250);
  }
  const page = list.find((t) => t.type === 'page');
  if (!page) throw new Error('无法连上 headless 浏览器');

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  const waiters = new Map();
  let seq = 0;
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && waiters.has(m.id)) {
      const w = waiters.get(m.id); waiters.delete(m.id);
      if (m.error) w.rej(new Error(JSON.stringify(m.error))); else w.res(m.result);
    }
  });
  const send = (method, params) => new Promise((res, rej) => {
    const id = ++seq;
    ws.send(JSON.stringify({ id, method, params: params || {} }));
    waiters.set(id, { res, rej });
  });
  await new Promise((res) => ws.addEventListener('open', res));

  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.addScriptToEvaluateOnNewDocument', { source: BRIDGE });
  // 等 shell-ui.js 的 boot() 把 sprite 建好（#i-menu 出现）—— 别在 boot 前就量，会误判成"图标没注入"
  const waitBoot = async () => {
    for (let i = 0; i < 60; i++) {
      const ok = await send('Runtime.evaluate', { expression: '!!document.getElementById("i-menu")' });
      if (ok.result && ok.result.value) return true;
      await sleep(200);
    }
    return false;
  };
  await send('Page.navigate', { url: URL_ });
  await waitBoot();

  const evalJson = async (expr) => JSON.parse((await send('Runtime.evaluate', { expression: expr })).result.value);
  const snap = () => evalJson(SNAP);

  const show = (tag, s) => {
    console.log(`\n──── ${tag} ────`);
    console.log(`  侧栏 rect      x=${s.sb.x} w=${s.sb.w}  expanded=${s.expanded}`);
    console.log(`  汉堡按钮 rect   x=${s.toggleBtn.x} w=${s.toggleBtn.w} h=${s.toggleBtn.h}`);
    console.log(`  汉堡图标       cx=${s.toggleIco.cx} cy=${s.toggleIco.cy} (${s.toggleIco.w}x${s.toggleIco.h})`);
    console.log(`  汉堡图标 margin-right = ${s.toggleIcoMarginRight}   justify=${s.navToggleJustify} align=${s.navToggleAlign}`);
    console.log(`  导航项         flexDirection=${s.navDirection} justify=${s.navJustify} lbl.display=${s.lblDisplay}`);
    console.log(`  导航图标 margin-right = ${s.navIcoMarginRight}`);
    console.log('  ── 各导航项图标中心 ──');
    s.navIcos.forEach((n) => {
      console.log(`     ${String(n.page).padEnd(10)} nav.x=${String(n.nav.x).padEnd(6)} nav.w=${String(n.nav.w).padEnd(6)} icoCx=${String(n.icoCx).padEnd(6)} icoCy=${String(n.icoCy).padEnd(6)} ico=${n.icoW}x${n.icoH}`);
    });
    if (s.toggleIco && s.navIcos.length) {
      const navCx = s.navIcos[0].icoCx;
      const d = +(navCx - s.toggleIco.cx).toFixed(1);
      console.log(`  ⇒ 汉堡图标 cx=${s.toggleIco.cx} vs 导航图标 cx=${navCx} → ${d === 0 ? '★ 同轴 ✓' : '★ 偏 ' + d + 'px ✗'}`);
    }
    console.log(`  指示条         cx=${s.ind ? s.ind.cx : '(无)'} cy=${s.ind ? s.ind.cy : ''}`);
    console.log(`  分隔线 .nav-sep ${s.sep ? 'y=' + s.sep.y + ' w=' + s.sep.w : '(已删除 ✓)'}`);
    console.log(`  .nav-group.bottom  y=${s.bottomGroup.y} h=${s.bottomGroup.h} margin-top=${s.bottomGroupMarginTop}`);
    console.log(`  .foot(就绪)     ${s.foot ? 'y=' + s.foot.y + ' margin-top=' + s.footMarginTop : '(已删除 ✓)'}`);
    if (s.sb && s.bottomGroup) {
      const gBottom = s.bottomGroup.y + s.bottomGroup.h;
      const sbBottom = s.sb.y + s.sb.h;
      console.log(`  ⇒ 底组下沿 y=${+gBottom.toFixed(1)}，侧栏内容底 y=${+(sbBottom - 8).toFixed(1)}（侧栏自身 padding-bottom 8）`);
      console.log(`  ⇒ 底组距侧栏底还有 ${+(sbBottom - gBottom).toFixed(1)} px（含侧栏 padding-bottom 8 ⇒ 期望 8）`);
    }
  };

  const s0 = await snap();
  show('折叠态', s0);

  // ---- 过渡采样：点汉堡展开 ----
  await send('Runtime.evaluate', { expression: `(function(){
    window.__samp = [];
    var t0 = performance.now();
    function cx(sel){ var e=document.querySelector(sel); if(!e) return null; var r=e.getBoundingClientRect(); return Math.round((r.x+r.width/2)*10)/10; }
    function tick(){
      var t = Math.round(performance.now()-t0);
      window.__samp.push({ t:t, sbw: Math.round(document.getElementById('sidebar').getBoundingClientRect().width),
        tog: cx('#navToggle .ico'), n0: cx('.nav[data-page="generate"] .ico'), n4: cx('.nav[data-page="train"] .ico') });
      if (t < 520) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
    document.getElementById('navToggle').click();
  })()` });
  let samp = [];
  for (let i = 0; i < 40; i++) {
    await sleep(120);
    samp = await evalJson('JSON.stringify(window.__samp || [])');
    if (samp.length && samp[samp.length - 1].t > 500) break;
  }
  const TOG0 = samp[0] ? samp[0].tog : null;
  const N0_0 = samp[0] ? samp[0].n0 : null;
  const N4_0 = samp[0] ? samp[0].n4 : null;
  let togRange = 0, nRange = 0;
  let togMin = 1e9, togMax = -1e9, nMin = 1e9, nMax = -1e9;
  samp.forEach((s) => {
    if (s.tog != null) { togMin = Math.min(togMin, s.tog); togMax = Math.max(togMax, s.tog); }
    if (s.n0 != null) { nMin = Math.min(nMin, s.n0); nMax = Math.max(nMax, s.n0); }
  });
  togRange = +(togMax - togMin).toFixed(1);
  nRange = +(nMax - nMin).toFixed(1);
  console.log(`\n──── 展开过渡（rAF 采样 ${samp.length} 帧，约 ${samp.length && samp[samp.length-1].t}ms）────`);
  samp.filter((_, i) => i % Math.max(1, Math.ceil(samp.length / 12)) === 0).forEach((s) => {
    console.log(`   t=${String(s.t).padStart(3)}ms  sbw=${String(s.sbw).padStart(4)}  汉堡icon.cx=${String(s.tog).padStart(6)}  生成icon.cx=${String(s.n0).padStart(6)}  训练icon.cx=${String(s.n4).padStart(6)}`);
  });
  console.log(`  ⇒ 汉堡图标横向行程 = ${togRange}px（起点 ${TOG0}）`);
  console.log(`  ⇒ 生成图标横向行程 = ${nRange}px（起点 ${N0_0}，末点 ${nMax}）`);
  console.log(`  ⇒ 训练图标起点 = ${N4_0}`);

  await sleep(300);
  const s1 = await snap();
  show('展开态', s1);

  // ---- 收回采样 ----
  await send('Runtime.evaluate', { expression: `(function(){
    window.__samp2 = [];
    var t0 = performance.now();
    function cx(sel){ var e=document.querySelector(sel); if(!e) return null; var r=e.getBoundingClientRect(); return Math.round((r.x+r.width/2)*10)/10; }
    function tick(){
      var t = Math.round(performance.now()-t0);
      window.__samp2.push({ t:t, sbw: Math.round(document.getElementById('sidebar').getBoundingClientRect().width),
        tog: cx('#navToggle .ico'), n0: cx('.nav[data-page="generate"] .ico') });
      if (t < 520) requestAnimationFrame(tick);
    }
    requestAnimationFrame(tick);
    document.getElementById('navToggle').click();
  })()` });
  let samp2 = [];
  for (let i = 0; i < 40; i++) {
    await sleep(120);
    samp2 = await evalJson('JSON.stringify(window.__samp2 || [])');
    if (samp2.length && samp2[samp2.length - 1].t > 500) break;
  }
  let t2min = 1e9, t2max = -1e9, n2min = 1e9, n2max = -1e9;
  samp2.forEach((s) => {
    if (s.tog != null) { t2min = Math.min(t2min, s.tog); t2max = Math.max(t2max, s.tog); }
    if (s.n0 != null) { n2min = Math.min(n2min, s.n0); n2max = Math.max(n2max, s.n0); }
  });
  console.log(`\n──── 收回过渡（rAF 采样 ${samp2.length} 帧）────`);
  samp2.filter((_, i) => i % Math.max(1, Math.ceil(samp2.length / 10)) === 0).forEach((s) => {
    console.log(`   t=${String(s.t).padStart(3)}ms  sbw=${String(s.sbw).padStart(4)}  汉堡icon.cx=${String(s.tog).padStart(6)}  生成icon.cx=${String(s.n0).padStart(6)}`);
  });
  console.log(`  ⇒ 汉堡图标横向行程 = ${+(t2max - t2min).toFixed(1)}px`);
  console.log(`  ⇒ 生成图标横向行程 = ${+(n2max - n2min).toFixed(1)}px`);

  // ---- 附带：关于页那条红字的误报（同一轮修掉的 key 不匹配）----
  console.log('\n──── 附带：关于页 explainStatus（请求名 "getexplain" ↔ 回执 key）────');
  const statusOf = async () => (await send('Runtime.evaluate', { expression:
    'JSON.stringify({ cls: (document.getElementById("explainStatus")||{}).className || "(无)", ' +
    'txt: (((document.getElementById("explainStatus")||{}).textContent) || "").slice(0, 70) })' })).result.value;
  const clickAbout = () => send('Runtime.evaluate', { expression:
    '(function(){ var n = document.querySelector(\'.nav[data-page="about"]\'); if(!n) return "NO_NAV"; n.click(); return "OK"; })()' });
  const feed = (obj) => send('Runtime.evaluate', { expression:
    'window.__devHostMsg(' + JSON.stringify(JSON.stringify(obj)) + ')' });

  // ① 宿主正常应答 ⇒ 绝不该出现红字（这是主人截图那一条）
  await clickAbout();
  await feed({ type: 'explain', ok: true, path: '<REPO>\\gui\\explain.md', text: '# 标题\n\n正文', size: '1.2 KB' });
  await sleep(3200);
  const st1 = JSON.parse(await statusOf());
  const ok1 = !/err/.test(st1.cls) && !/宿主没有回应/.test(st1.txt);
  console.log(`${ok1 ? 'PASS' : 'FAIL'}  宿主应答后不该误报「宿主没有回应」   class="${st1.cls}"  txt="${st1.txt}"`);

  // ② 宿主真的不答 ⇒ 兜底红字**必须还在**（证明护栏没被我顺手拆掉，兜底分支走得到）
  await send('Page.navigate', { url: URL_ });
  await waitBoot();
  await clickAbout();
  await sleep(3200);
  const st2 = JSON.parse(await statusOf());
  const ok2 = /err/.test(st2.cls) && /宿主没有回应/.test(st2.txt);
  console.log(`${ok2 ? 'PASS' : 'FAIL'}  宿主不答时兜底红字仍然出现   class="${st2.cls}"  txt="${st2.txt}"`);

  const err = (await send('Runtime.evaluate', { expression: 'window.__err || ""' })).result.value;
  if (err) console.log('\n页面异常: ' + err.slice(0, 400));

  try { child.kill(); } catch (_e) {}
  process.exit(0);
})().catch((e) => { console.error('脚本自身出错:', (e && e.stack) || e); process.exit(2); });
