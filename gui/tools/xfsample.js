/* xfsample.js —— 逐帧量"切页残留"过渡（诊断用，非回归）。
 * 用法: node tools/xfsample.js <url> [outPrefix] [fromPage] [toPage]
 *   例: node tools/xfsample.js http://127.0.0.1:8766/index.html <REPO>/output/.tmp/shots/xf separate workbench
 *
 * 做两遍：
 *   ① 采样遍：页内 setInterval(16ms) 记录 active / leaving 两个 .page 的 computed style
 *      + leaving 的 getAnimations()（名字/playState/currentTime），跑 1.4s 后整表倒出。
 *   ② 截图遍：重新点一次，在指定时刻各拍一张（找"眼睛看到什么"的证据）。
 */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');

const URL_ = process.argv[2];
const PREFIX = (process.argv[3] || '<REPO>/output/.tmp/shots/xf').replace(/\\/g, '/');
const FROM = process.argv[4] || 'separate';
const TO = process.argv[5] || 'workbench';
const CHROME = process.argv[6] || 'chrome';
const PORT = 9343;
const UDD = '<REPO>\\output\\.tmp\\xfsample-profile';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = (p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: PORT, path: p }, (r) => {
    let b = ''; r.on('data', (c) => (b += c)); r.on('end', () => res(b));
  }).on('error', rej);
});
const STUB = `
window.__SENT = []; window.__LISTENERS = [];
window.chrome = window.chrome || {};
window.chrome.webview = { postMessage:function(s){window.__SENT.push(s);}, addEventListener:function(t,f){ if(t==='message') window.__LISTENERS.push(f); } };
window.__deliver = function(o){ var s=typeof o==='string'?o:JSON.stringify(o); window.__LISTENERS.forEach(function(f){try{f({data:s});}catch(e){}}); };
`;

const CLICK = (page) => `(function(){ var ns=document.querySelectorAll("#sidebar .nav"); for(var i=0;i<ns.length;i++){ if(ns[i].getAttribute("data-page")===${JSON.stringify(page)}){ ns[i].click(); return true; } } return false; })()`;

/* 页内采样器：把每一帧写进 window.__LOG（避免 CDP 往返抖动） */
const SAMPLER = `
(function(){
  window.__LOG = [];
  function row(el){
    if(!el) return null;
    var cs = getComputedStyle(el);
    var anims = [];
    try { anims = el.getAnimations().map(function(a){
      var it = a.effect && a.effect.getTiming ? a.effect.getTiming() : {};
      return {n:a.animationName, ps:a.playState, ct:Math.round(a.currentTime||0),
              dur:it.duration, del:it.delay, fill:it.fill};
    }); } catch(e){}
    return {id:el.id, op:+cs.opacity, disp:cs.display, pos:cs.position, z:cs.zIndex,
            tf:cs.transform, bg:cs.backgroundColor, anims:anims};
  }
  function kid(el){
    if(!el) return null;
    var k = el.querySelector(':scope > *');
    if(!k) return null;
    var cs = getComputedStyle(k);
    return {op:+cs.opacity, tf:cs.transform};
  }
  window.__T0 = performance.now();
  var iv = setInterval(function(){
    var t = performance.now() - window.__T0;
    var act = document.querySelector('#main .page.active');
    var lv  = document.querySelector('#main .page.leaving');
    window.__LOG.push({t:Math.round(t), act:row(act), kid:kid(act), lv:row(lv)});
    if(t > 1400){ clearInterval(iv); window.__DONE = true; }
  }, 16);
  return 'sampler-on';
})()`;

(async () => {
  // ⚠ 每次先清 profile：Chrome 的持久 profile 会把 index.html 从缓存里端出来，
  //   量到的还是上一版（改完看不到变化 = 自己骗自己）。这是老坑了。
  fs.rmSync(UDD, { recursive: true, force: true });
  fs.mkdirSync(UDD, { recursive: true });
  fs.mkdirSync(PREFIX.slice(0, PREFIX.lastIndexOf('/')), { recursive: true });
  const child = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--mute-audio', '--force-device-scale-factor=1',
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${UDD}`,
    '--window-size=1560,940', 'about:blank',
  ], { stdio: 'ignore', windowsHide: true });
  let list = [];
  for (let i = 0; i < 80; i++) {
    try { list = JSON.parse(await get('/json/list')); if (list.some(t => t.type === 'page')) break; } catch (_e) {}
    await sleep(250);
  }
  const page = list.find(t => t.type === 'page');
  if (!page) throw new Error('连不上 headless');
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  const waiters = new Map(); let seq = 0;
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && waiters.has(m.id)) { const w = waiters.get(m.id); waiters.delete(m.id); if (m.error) w.rej(new Error(JSON.stringify(m.error))); else w.res(m.result); }
  });
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  const send = (method, params) => new Promise((res, rej) => { const id = ++seq; waiters.set(id, { res, rej }); ws.send(JSON.stringify({ id, method, params: params || {} })); });
  const ev = async (expr, awaitP) => {
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: !!awaitP });
    if (r && r.exceptionDetails) throw new Error('JS 异常: ' + JSON.stringify(r.exceptionDetails));
    return r.result ? r.result.value : undefined;
  };
  const shot = async (file) => {
    const s = await send('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
    fs.writeFileSync(file, Buffer.from(s.data, 'base64'));
  };

  await send('Runtime.enable');
  await send('Page.enable');
  await send('Page.addScriptToEvaluateOnNewDocument', { source: STUB });
  await send('Emulation.setDeviceMetricsOverride', { width: 1560, height: 940, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url: URL_ });
  await sleep(2600);

  // ---------- ① 采样遍 ----------
  await ev(CLICK(FROM)); await sleep(1000);
  await ev(SAMPLER);
  await ev(CLICK(TO));
  for (let i = 0; i < 60; i++) { await sleep(40); const d = await ev('!!window.__DONE'); if (d) break; }
  const log = await ev('JSON.stringify(window.__LOG)');
  fs.writeFileSync(PREFIX + '_log.json', log);
  const rows = JSON.parse(log);
  console.log('=== 采样遍 ' + FROM + ' → ' + TO + '（每 ~16ms 一帧） ===');
  let lastKey = '';
  for (const r of rows) {
    const key = (r.lv ? 'lv' : '--') + '|' + (r.lv ? r.lv.op.toFixed(2) : '') + '|' +
      (r.act ? r.act.id : '') + '|' + (r.kid ? (r.kid.op.toFixed(2) + ',' + (r.kid.tf || '').slice(0, 28)) : '');
    if (r.t > 500 && key === lastKey) continue;
    lastKey = key;
    console.log(
      String(r.t).padStart(5) + 'ms' +
      ' | act=' + (r.act ? r.act.id.replace('page-', '') + ' op=' + r.act.op.toFixed(2) + ' tf=' + (r.act.tf || '-').replace(/matrix\(1, 0, 0, 1, 0, /, '(ty=') : 'none').padEnd(30) +
      ' | kid(op=' + (r.kid ? r.kid.op.toFixed(2) : '-') + ')' +
      ' | lv=' + (r.lv ? r.lv.id.replace('page-', '') + ' op=' + r.lv.op.toFixed(3) + ' pos=' + r.lv.pos + ' z=' + r.lv.z + ' bg=' + r.lv.bg : 'none')
    );
    if (r.lv && r.lv.anims && r.lv.anims.length) {
      console.log('        └ lv.anims: ' + r.lv.anims.map(a => `${a.n} ps=${a.ps} ct=${a.ct} dur=${a.dur} del=${a.del} fill=${a.fill}`).join(' ; '));
    }
  }
  const tail = await ev('JSON.stringify({pages:[].map.call(document.querySelectorAll("#main .page"),function(p){return p.id+":"+p.className;}), stuck:document.querySelectorAll("#main .page.leaving").length, act:document.querySelectorAll("#main .page.active").length})');
  console.log('=== 终态 === ' + tail);

  // ---------- ② 截图遍 ----------
  if (process.argv[7] === 'rapid') {
    /* 连点兜底：A→B 之后 45ms 立刻点回 A（此时 A 还挂着上一轮的 .leaving，正在渐隐中）。
       没兜底(showPage 里 pg.classList.remove("leaving"))的话，A 会带着 .leaving
       （position:absolute + page-leave 渐隐）当上 active ⇒ 当前页自己淡成全透明、
       再等 140ms 定时器摘除才救回来（一段空白/闪烁）。
       这里只量"当前 active 页"的 opacity：必须一直是 ≥0.98（连点兜底生效）。 */
    await ev(CLICK(FROM)); await sleep(900);
    const r = await ev(`(async function(){
      var wait=function(ms){return new Promise(function(r){setTimeout(r,ms);});};
      var navs=document.querySelectorAll('#sidebar .nav');
      var pick=function(p){ for(var i=0;i<navs.length;i++){ if(navs[i].getAttribute('data-page')===p) return navs[i]; } return null; };
      var out=[]; var t0=performance.now();
      var iv=setInterval(function(){
        var a=document.querySelector('#main .page.active');
        var k=a?a.querySelector(':scope > *'):null;
        out.push({t:Math.round(performance.now()-t0), id:a?a.id:null,
                  op:a?+getComputedStyle(a).opacity:null,
                  kid:k?+getComputedStyle(k).opacity:null});
        if(performance.now()-t0>420) clearInterval(iv);
      },16);
      pick(${JSON.stringify(TO)}).click();
      await wait(45);
      pick(${JSON.stringify(FROM)}).click();   // 连点：点回刚离开的那页
      await wait(460);
      return JSON.stringify(out);
    })()`, true);
    const rows = JSON.parse(r);
    console.log('=== 连点兜底 ' + FROM + ' → ' + TO + ' →(45ms)→ ' + FROM + ' ===');
    let last = '';
    for (const q of rows) {
      const key = q.id + q.op;
      if (key === last) continue;
      last = key;
      console.log(String(q.t).padStart(5) + 'ms act=' + String(q.id).replace('page-', '') +
        ' op=' + (q.op == null ? '-' : q.op.toFixed(3)) + ' kidOp=' + (q.kid == null ? '-' : q.kid.toFixed(2)));
    }
    const bad = rows.filter((q) => q.id === 'page-' + FROM && q.op < 0.98);
    console.log('⚠ 点回的那页出现 op<0.98 的帧数 = ' + bad.length + (bad.length ? ' ' + JSON.stringify(bad.slice(0, 4)) : '（应 0）'));
    try { ws.close(); } catch (_e) {}
    try { child.kill(); } catch (_e) {}
    process.exit(bad.length ? 1 : 0);
  }

  await ev(CLICK(FROM)); await sleep(1000);
  await ev(CLICK(TO));
  await sleep(30);  await shot(PREFIX + '_t030.png');   // 点击瞬间：旧页仍满不透明（证明非空白，正在渐隐）
  await sleep(40);  await shot(PREFIX + '_t070.png');   // 旧页渐隐中（opacity 已降、非空白）
  await sleep(60);  await shot(PREFIX + '_t130.png');   // 旧页已隐没、新内容开始从 25vh 上浮
  await sleep(70);  await shot(PREFIX + '_t200.png');   // 新页升起中
  await sleep(900); await shot(PREFIX + '_t1100.png');  // 落位
  console.log('✓ 截图: ' + PREFIX + '_t030/_t070/_t130/_t200/_t1100.png');

  try { ws.close(); } catch (_e) {}
  try { child.kill(); } catch (_e) {}
  process.exit(0);
})().catch((e) => { console.error('【失败】' + (e && e.stack || e)); process.exit(1); });
