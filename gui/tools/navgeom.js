/* navgeom.js —— 量侧栏「选中指示条」的几何关系（条 vs 项的框）。
 * ============================================================================
 * 背景：主人 22:03 反馈「这个条跑出来了，不应该在那个框框里吗」。
 * 光看截图分不清「条在侧栏左边距里」还是「条在项的圆角框内」，所以这里把
 * #sidebar / .brand / .nav.active / #nav-ind 的盒子一次量出来，并裁一张放大图。
 *
 * 用法：node tools/navgeom.js [url] [chrome.exe] [输出目录]
 * ========================================================================== */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const URL_ = process.argv[2] || 'http://127.0.0.1:8896/index.html';
const CHROME = process.argv[3] || 'chrome';
const OUT = process.argv[4] || '<REPO>\\output\\logs\\navgeom';
/* 第 5 个参数 = 垫底色。这个页面**自己不带底色**（侧栏/外框都靠 DWM 材质当底），
   headless 里没有材质 ⇒ 默认白底，跟主人实际看到的深色完全两样。
   要出"能跟主人截图对照"的留证图，就给一个材质基色（实测 #0E1116 那档）。 */
const BACKDROP = process.argv[5] || '';
const PORT = 9337;
const UDD = '<REPO>\\output\\.tmp\\navgeom-profile';
const DSF = 1.5;                       // 主人那台是 150% 缩放，截图对齐它
const W = 1100, H = 760;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = (p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: PORT, path: p }, (r) => {
    let b = ''; r.on('data', (c) => (b += c)); r.on('end', () => res(b));
  }).on('error', rej);
});

(async () => {
  fs.mkdirSync(UDD, { recursive: true });
  fs.mkdirSync(OUT, { recursive: true });
  const child = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--mute-audio', '--remote-debugging-port=' + PORT,
    '--user-data-dir=' + UDD, '--window-size=' + W + ',' + H, 'about:blank',
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
  await send('Page.navigate', { url: URL_ });
  for (let i = 0; i < 60; i++) {
    if (await ev('!!document.getElementById(\'nav-ind\')')) break;
    await sleep(300);
  }
  await sleep(1200);

  if (BACKDROP) {
    await ev('document.documentElement.style.background=' + JSON.stringify(BACKDROP) + ';1');
    await sleep(300);
  }

  const geo = await ev('(function(){' +
    'var R=function(el){ if(!el) return null; var r=el.getBoundingClientRect();' +
    '  return {x:+r.left.toFixed(2),y:+r.top.toFixed(2),w:+r.width.toFixed(2),h:+r.height.toFixed(2)}; };' +
    'var sb=document.getElementById("sidebar"); var ind=document.getElementById("nav-ind");' +
    'var act=document.querySelector(".nav.active"); var card=document.querySelector(".nav.active .ico");' +
    'var cs=getComputedStyle(sb), ci=ind?getComputedStyle(ind):null, ca=act?getComputedStyle(act):null;' +
    'var host=sb.parentElement?getComputedStyle(sb.parentElement):null;' +
    'return { dpr:devicePixelRatio, inner:[innerWidth,innerHeight],' +
    '  body:R(document.body), sidebar:R(sb), sidebarPad:[cs.paddingLeft,cs.paddingTop],' +
    '  sidebarBg:cs.backgroundColor, sidebarOverflow:cs.overflowX,' +
    '  brand:R(document.querySelector("#sidebar .brand")), logo:R(document.querySelector("#sidebar .blogo")),' +
    '  activeNav:act?act.getAttribute("data-page"):null, activeBox:R(act), activeBg:ca?ca.backgroundColor:null,' +
    '  activePad:[ca?ca.paddingLeft:null,ca?ca.paddingRight:null],' +
    '  indEl:!!ind, ind:R(ind), indLeft:ci?ci.left:null, indW:ci?ci.width:null,' +
    '  indOpacity:ci?ci.opacity:null, indOpacityInline:ind?ind.style.opacity:null,' +
    '  indTransform:ind?ind.style.transform:null, indBg:ci?ci.backgroundColor:null,' +
    '  parentTag:sb.parentElement?sb.parentElement.id:null, parentPad:host?[host.paddingLeft,host.marginLeft,host.borderLeftWidth]:null };' +
  '})()');
  console.log('GEO ' + JSON.stringify(geo, null, 1));

  /* 逐项走一遍：每一项当选中项时，条的左缘都该与该项框的左缘齐平（偏移 0）、中心对齐、落在框内。
     只量"当前那一项"是不够的 —— 项高不一样（图标里有一个是字体字形），逐项走才盖得住。
     ⚠ 每切一次必须**等过渡跑完**再量 rect：过渡中 rect 给的是动画当前值，
       同步量会读到"还没动"的旧位置（上一版就这么误报过 inBox=false）。 */
  const walk = await ev('(async function(){' +
    'var sb=document.getElementById("sidebar"); var ind=document.getElementById("nav-ind");' +
    'var api=window.ShellUI && window.ShellUI.navInd; var out=[];' +
    'var wait=function(ms){ return new Promise(function(r){ setTimeout(r,ms); }); };' +
    'var navs=sb.querySelectorAll(".nav"); var cur=sb.querySelector(".nav.active"); var prev=cur;' +
    'for(var i=0;i<navs.length;i++){ var n=navs[i];' +
    '  if(prev) prev.classList.remove("active"); n.classList.add("active"); prev=n; if(api) api();' +
    '  await wait(480);' +
    '  var r=ind.getBoundingClientRect(), a=n.getBoundingClientRect();' +
    '  out.push({page:n.getAttribute("data-page"), dxLeft:Math.round(r.left-a.left),' +
    '    dyCenter:Math.round((r.top+r.height/2)-(a.top+a.height/2)),' +
    '    inBox:(r.left>=a.left-1 && r.right<=a.right+1 && r.top>=a.top-1 && r.bottom<=a.bottom+1),' +
    '    tf: ind.style.transform, ty: Math.round(r.top), ay: Math.round(a.top),' +
    '    guard: (window.__navIndGuard||null)});' +
    '}' +
    'if(prev) prev.classList.remove("active");' +
    'if(cur) cur.classList.add("active"); if(api) api();' +
    'return out; })()');
  console.log('WALK ' + JSON.stringify(walk));

  const shot = async (name, clip, scale) => {
    const p = { format: 'png' };
    if (clip) p.clip = Object.assign({ scale: scale || 1 }, clip);
    const r = await send('Page.captureScreenshot', p);
    const f = path.join(OUT, name + '.png');
    fs.writeFileSync(f, Buffer.from(r.data, 'base64'));
    console.log('SAVED ' + f);
  };

  // 整窗（留证）
  await shot('0-full');
  // 侧栏整条（x 0..110）
  await shot('1-sidebar', { x: 0, y: 0, width: 110, height: H }, 4);
  // 只裁「生成」那一项 + 左右各留 30px，放大 6 倍 ⇒ 条与框的关系一眼可读
  if (geo.activeBox) {
    const y0 = Math.max(0, geo.activeBox.y - 24);
    const hh = Math.min(H - y0, geo.activeBox.h + 48);
    await shot('2-active-item', { x: 0, y: y0, width: 130, height: hh }, 6);
  }

  ws.close();
  try { child.kill(); } catch (_e) { /* ignore */ }
  console.log('DONE');
  process.exit(0);
})().catch((e) => { console.error('ERR ' + ((e && e.stack) || e)); process.exit(1); });
