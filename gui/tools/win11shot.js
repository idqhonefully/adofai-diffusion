/* win11shot.js —— 「Win11 观感」视觉留证：无头 Chromium 打开导入页与工作台，
 * 在几个关键状态下各截一张 PNG，供人眼核对（配色 / 图标笔法 / 过渡是否到位）。
 *
 * 与 uicheck / importprobe 的分工：
 *   · 那两个验**结构与行为**（属性在不在、消息发没发、尺寸对不对）；
 *   · 这个只管**看起来对不对** —— 图标是否真的是 Fluent 笔法、按钮里图标和文字的
 *     对齐、导航选中条、材质卡的选中勾、三档排布…… 这些探针说不清，得用眼睛。
 *   两者都要跑：探针能证明"没坏"，截图才能证明"好看"。
 *
 *   · 不碰 8765/8766（只连已经起好的网关），不会关掉主人正在用的窗口
 *
 * 用法：node tools/win11shot.js [baseUrl] [chrome.exe] [outDir]
 *   默认 baseUrl = http://127.0.0.1:8766
 * ========================================================================== */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const BASE = (process.argv[2] || 'http://127.0.0.1:8766').replace(/\/$/, '');
const CHROME = process.argv[3] || 'chrome';
const OUT = process.argv[4] || '<REPO>\\output\\logs\\win11shot';
const PORT = 9336;
const UDD = '<REPO>\\output\\.tmp\\win11shot-profile';
const W = 1560, H = 940;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = (p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: PORT, path: p }, (r) => {
    let b = ''; r.on('data', (c) => (b += c)); r.on('end', () => res(b));
  }).on('error', rej);
});

/* 页面一进来就会 `window.chrome.webview.addEventListener(...)` 和 post，
   没有宿主时直接抛 TypeError 并把整个脚本带停。所以必须在**页面脚本之前**塞一个假桥。
   （与 importprobe 里那份是同一个套路；这里只需要"能收能喂"，不需要记录消息。） */
const STUB = `
window.__SENT = [];
window.__LISTENERS = [];
window.chrome = window.chrome || {};
window.chrome.webview = {
  postMessage: function (s) { window.__SENT.push(s); },
  addEventListener: function (t, f) { if (t === 'message') window.__LISTENERS.push(f); }
};
window.dsh = window.dsh || {};
window.__deliver = function (o) {
  var s = typeof o === 'string' ? o : JSON.stringify(o);
  window.__LISTENERS.forEach(function (f) { try { f({ data: s }); } catch (e) {} });
};
// 垫一层深色底当"系统材质的替身"。
// 🔴 必须垫：两个页面的 body 都是 background:transparent（真机上露的是 DWM 材质），
//    浏览器里没有材质 ⇒ 露成惨白底，而文字是浅色的 —— 配色对不对一眼都看不出来，
//    截图等于白拍。（垫的色号 = 实色档的 #0e1116，与真机深色材质最接近。）
(function () {
  function paint() {
    if (!document.documentElement) { return false; }
    document.documentElement.style.background = '#0e1116';
    return true;
  }
  if (!paint()) {
    var t = setInterval(function () { if (paint()) { clearInterval(t); } }, 10);
  }
})();
`;

const APPINFO = {
  app: 'ADOFAI Studio', studio_dir: '<REPO>', output_dir: '<REPO>\\output',
  audio_sep_dir: '<REPO>\\audio-sep', chartgen_dir: '<REPO>\\chartgen',
  gateway: BASE, sidecar: '127.0.0.1:8765', py_ver: '3.13.12',
  material: 'acrylic',
  materials: [
    { name: 'acrylic', label: 'Acrylic', kind: 3, opaque: false },
    { name: 'mica', label: 'Mica', kind: 2, opaque: false },
    { name: 'solid', label: 'Solid', kind: 1, opaque: true },
  ],
};

(async () => {
  fs.mkdirSync(UDD, { recursive: true });
  fs.mkdirSync(OUT, { recursive: true });

  const child = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--mute-audio', '--force-device-scale-factor=1',
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${UDD}`,
    `--window-size=${W},${H}`, 'about:blank',
  ], { stdio: 'ignore', windowsHide: true });

  let list = [];
  for (let i = 0; i < 80; i++) {
    try {
      list = JSON.parse(await get('/json/list'));
      if (list.some((t) => t.type === 'page')) break;
    } catch (_e) { /* 还没起来 */ }
    await sleep(250);
  }
  const page = list.find((t) => t.type === 'page');
  if (!page) { throw new Error('没能连上 headless 浏览器的 DevTools'); }

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
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  const send = (method, params) => new Promise((res, rej) => {
    const id = ++seq; waiters.set(id, { res, rej });
    ws.send(JSON.stringify({ id, method, params: params || {} }));
  });
  async function ev(expression) {
    const r = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) {
      const d = r.exceptionDetails;
      throw new Error('页面里抛异常：' + ((d.exception && d.exception.description) || d.text));
    }
    return r.result.value;
  }

  await send('Runtime.enable');
  await send('Page.enable');
  await send('Page.addScriptToEvaluateOnNewDocument', { source: STUB });
  // 固定视口，免得截图尺寸随窗口抖动（默认 --window-size 只是"初始值"）
  await send('Emulation.setDeviceMetricsOverride',
    { width: W, height: H, deviceScaleFactor: 1, mobile: false });

  const shots = [];
  async function shot(name, note) {
    const r = await send('Page.captureScreenshot', { format: 'png' });
    const f = path.join(OUT, name + '.png');
    fs.writeFileSync(f, Buffer.from(r.data, 'base64'));
    shots.push(f);
    console.log(`  拍下 ${name}.png   ${note || ''}`);
    return f;
  }

  async function goto(url, readyExpr) {
    await send('Page.navigate', { url });
    for (let i = 0; i < 80; i++) {
      try { if (await ev(readyExpr)) return true; } catch (_e) { /* 还在加载 */ }
      await sleep(200);
    }
    return false;
  }

  // ---------------------------------------------------------------- 导入页
  console.log('[1/4] 导入页 · 生成谱面');
  if (!await goto(BASE + '/index.html', 'document.querySelectorAll("#sidebar .nav .ico").length===7')) {
    throw new Error('导入页没建好（侧栏图标没出来）');
  }
  await sleep(400);
  await shot('01-import-generate', '侧栏 7 项 Fluent 图标 + 选中指示条');

  console.log('[2/4] 导入页 · 设置（材质四档下拉）');
  await ev('document.querySelector(\'.nav[data-page="settings"]\').click()');
  await sleep(300);
  await ev(`window.__deliver(${JSON.stringify({ type: 'appinfo', data: APPINFO })})`);
  await sleep(400);
  // ★ 2026-09-22 第三轮：材质从"卡片平铺"改成自绘下拉 ⇒ 计数与点击都换成菜单项。
  const cards = await ev('document.querySelectorAll("#matSelect-menu .dd-opt").length');
  console.log(`  材质项 = ${cards} 个`);
  await shot('02-import-settings', `材质 ${cards} 档下拉（收起态）`);

  // 材质过场的中段：点一下，趁幕布还压着的时候拍一张，证明"暗下来"真的发生了
  await ev('document.getElementById("matSelect").click()');
  await sleep(280);
  await shot('02b-import-settings-open', '材质下拉展开（Win11 浮出菜单）');
  await ev('document.querySelector(\'#matSelect-menu .dd-opt[data-v="mica"]\').click()');
  await sleep(40);
  const veilOn = await ev('document.getElementById("shell-mat-veil").classList.contains("on")');
  await shot('03-import-matfade', veilOn ? '幕布压暗中（过场的第一拍）' : '⚠ 幕布没亮');

  // 🔴 直接读幕布自己的计算样式。
  //    不能用 elementsFromPoint —— 幕布是 pointer-events:none，命中测试**故意跳过**它，
  //    拿它当"有没有盖住"的判据会得到"哪都没盖住"的假结论（本轮栽过一次）。
  //    要看的是：它到底有没有被定位成全屏（fixed + inset:0）、算出来的盒子有多大。
  const veilBox = JSON.parse(await ev(`(function(){
    var v = document.getElementById('shell-mat-veil');
    if (!v) { return JSON.stringify({ missing: true }); }
    var cs = getComputedStyle(v), r = v.getBoundingClientRect();
    return JSON.stringify({
      pos: cs.position, z: cs.zIndex, op: cs.opacity, bg: cs.backgroundColor,
      cls: v.className, w: Math.round(r.width), h: Math.round(r.height),
      top: Math.round(r.top), left: Math.round(r.left),
      view: innerWidth + 'x' + innerHeight
    });
  })()`));
  console.log('  幕布实测 = ' + JSON.stringify(veilBox));
  const veilCovers = !veilBox.missing && veilBox.w >= 0.9 * (parseInt(veilBox.view) || 0)
    && veilBox.h >= 0.9 * (parseInt(String(veilBox.view).split('x')[1]) || 0);
  console.log(`  幕布覆盖全窗：${veilCovers ? '✅' : '❌ 它没有盖住整个视口 —— 过场会是残的'}`);

  // ★ 强度压力测试：把幕布临时拉到 opacity:1（纯黑），再截一张。
  //   目的：分清"幕布压根没盖住某些区域"和"盖住了、只是 0.66 压在深色上差别太小"。
  //   前者是层叠 bug（必须换实现），后者只是参数问题（调强度即可）——不测就会瞎改。
  await ev(`(function(){
    var v = document.getElementById('shell-mat-veil');
    v.style.opacity = '1';
    return true;
  })()`);
  await sleep(80);
  await shot('03b-veilfull', '压力测试：幕布拉到纯黑（哪块没黑就是没盖住）');
  await ev(`(function(){
    var v = document.getElementById('shell-mat-veil');
    v.style.opacity = '';
    return true;
  })()`);

  await sleep(900);

  // ---------------------------------------------------------------- 外框定住 / 内容内滚
  //   主人 2026-09-21 晚的要求：「外框是显示区域，他是定在这不动的，界面在里面滚动」。
  //   ⚠ 这条**必须先把页面撑长**才拍得出来：1560x940 下设置页的内容比外框还矮，
  //     没有可滚的余地，前后两张会一模一样、看起来像"改了但没差别"。
  //     所以克隆几张卡片（抹掉 id，免得造出重复 id 干扰别处），撑到比框高，再对比。
  console.log('[3.5/4] 外框固定 · 内容在内滚动');
  const grow = JSON.parse(await ev(`(function(){
    var pg = document.querySelector('.page.active');
    var src = pg.querySelector('.card');
    for (var i = 0; i < 6; i++) {
      var c = src.cloneNode(true);
      c.removeAttribute('id');
      Array.prototype.forEach.call(c.querySelectorAll('[id]'), function (e) { e.removeAttribute('id'); });
      pg.appendChild(c);
    }
    pg.scrollTop = 0;
    return JSON.stringify({ h: pg.scrollHeight, c: pg.clientHeight });
  })()`));
  console.log(`  撑长后：内容 ${grow.h}px / 框 ${grow.c}px（可滚 ${grow.h - grow.c}px）`);
  const rectOf = () => ev(`(function(){
    var r = document.querySelector('.page.active').getBoundingClientRect();
    return [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)].join(',');
  })()`);
  await sleep(300);
  const rectTop = await rectOf();
  await shot('03c-frame-fixed-top', '滚动位置 0：记住外框上/下边缘在哪');
  await ev(`document.querySelector('.page.active').scrollTop = 420`);
  await sleep(300);
  const rectMid = await rectOf();
  await shot('03d-frame-scrolled', '滚到 420px：内容走了，**外框原地不动**（对比 03c 的边缘位置）');
  console.log(`  外框 rect：滚 0 = ${rectTop}   滚 420 = ${rectMid}   ${rectTop === rectMid ? '✅ 没动' : '❌ 动了（说明滚的还是整块框）'}`);
  await ev(`document.querySelector('.page.active').scrollTop = 0`);

  // ---------------------------------------------------------------- 工作台
  console.log('[4/4] 工作台');
  if (!await goto(BASE + '/workbench/index.html', 'document.querySelectorAll("#tabs .tab .ico").length===4')) {
    throw new Error('工作台没建好（标签图标没出来）');
  }
  await sleep(1200);
  await shot('04-workbench', '顶栏按钮 / 四个标签 / 窗格标题图标');

  // 音源浮层（图标出现在最多的一处：⚙ 药丸 + ✕ 关闭）
  await ev(`(function(){
    var s = document.getElementById('btn-av-sum');
    if (s) s.click();
    return true;
  })()`);
  await sleep(500);
  await shot('05-workbench-avpop', '音源浮层（齿轮药丸 / 关闭图标）');

  console.log('\n输出目录：' + OUT);
  shots.forEach((f) => console.log('  ' + f));

  try { ws.close(); } catch (_e) { /* ignore */ }
  try { child.kill(); } catch (_e) { /* ignore */ }
  process.exit(0);
})().catch((e) => {
  console.error('【失败】' + (e && e.stack || e));
  process.exit(1);
});
