// verify-sidebar.js —— 任务管理器式侧栏改造（#278）的 headless 验收 + 留证图
// 用法：node tools/verify-sidebar.js [url] [chrome.exe]
//   默认 url = http://127.0.0.1:8799/index.html
const { spawn } = require('child_process');
const fs = require('fs');
const http = require('http');

const URL_ = process.argv[2] || 'http://127.0.0.1:8799/index.html';
const CHROME = process.argv[3] || 'chrome';
const PORT = 9341;
const UDD = '<REPO>\\output\\.tmp\\verify-sidebar-profile';
const OUT = '<REPO>\\output\\.tmp';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = (p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: PORT, path: p }, (r) => {
    let b = ''; r.on('data', (c) => (b += c)); r.on('end', () => res(b));
  }).on('error', rej);
});

const results = [];
function check(name, ok, extra) {
  results.push({ name, ok: !!ok });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra !== undefined ? '   ' + extra : ''}`);
}

const BRIDGE = `
window.__SENT = [];
window.__HANDLERS = [];
window.__err = '';
window.addEventListener('error', function(e){ window.__err += (e.message||'') + ' @ ' + (e.filename||'') + ':' + (e.lineno||'') + '\\n'; });
window.chrome = window.chrome || {};
window.chrome.webview = {
  postMessage: function (s) {
    try { window.__SENT.push(JSON.parse(s)); } catch (e) { window.__SENT.push({ __raw: String(s) }); }
  },
  addEventListener: function (t, fn) { if (t === 'message') window.__HANDLERS.push(fn); }
};
`;

(async () => {
  fs.mkdirSync(UDD, { recursive: true });
  const child = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--mute-audio', `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${UDD}`, '--window-size=1200,720', 'about:blank',
  ], { stdio: 'ignore', windowsHide: true });

  let list = [];
  for (let i = 0; i < 80; i++) {
    try {
      list = JSON.parse(await get('/json/list'));
      if (list.some((t) => t.type === 'page')) break;
    } catch (_e) { /* not up yet */ }
    await sleep(250);
  }
  const page = list.find((t) => t.type === 'page');
  if (!page) throw new Error('无法连上 headless 浏览器');

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  const waiters = new Map();
  let seq = 0;
  const events = [];
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && waiters.has(m.id)) {
      const w = waiters.get(m.id); waiters.delete(m.id);
      if (m.error) w.rej(new Error(JSON.stringify(m.error))); else w.res(m.result);
      return;
    }
    if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails || {};
      events.push('EXC ' + ((d.exception && d.exception.description) || d.text));
    }
    if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') {
      events.push('LOG ' + m.params.entry.text + ' @' + (m.params.entry.url || '?'));
    }
  });
  const send = (method, params) => new Promise((res, rej) => {
    const id = ++seq;
    ws.addEventListener('open', () => {});
    ws.send(JSON.stringify({ id, method, params: params || {} }));
    waiters.set(id, { res, rej });
  });
  await new Promise((res) => ws.addEventListener('open', res));
  const shoot = async (clip) => {
    const params = { format: 'png', fromSurface: true, captureBeyondViewport: clip ? true : false };
    if (clip) { clip.scale = 1; params.clip = clip; }
    const r = await send('Page.captureScreenshot', params);
    if (!r || !r.data) throw new Error('captureScreenshot 无结果: ' + JSON.stringify(r));
    return Buffer.from(r.data, 'base64');
  };
  await send('Page.enable');
  await send('Runtime.enable');
  await send('Page.addScriptToEvaluateOnNewDocument', { source: BRIDGE });
  // 垫底色，headless 透明背景会变纯黑，截图不好看（navgeom 也是这个套路）
  await send('Emulation.setDefaultBackgroundColorOverride', { color: { r: 14, g: 17, b: 22, a: 1 } });

  await send('Page.navigate', { url: URL_ });
  // 等 shell-ui.js 的 boot() 跑完（sprite 建好 ⇒ #i-menu 出现），避免 race 把"还没注入图标"误判成失败
  for (let i = 0; i < 60; i++) {
    const ok = await send('Runtime.evaluate', { expression: '!!document.getElementById("i-menu") || !!(window.ShellUI)' });
    if (ok.result && ok.result.value) break;
    await sleep(200);
  }
  const dbg = await send('Runtime.evaluate', { expression: 'JSON.stringify({ shellUI: typeof window.ShellUI, sprite: !!document.getElementById("shell-icon-sprite"), iMenu: !!document.getElementById("i-menu"), err: (window.__err||"").slice(0,500) })' }).then((r) => r.result.value);
  console.log('DEBUG boot:', dbg);

  const measure = async () => JSON.parse(await send('Runtime.evaluate', { expression: `(function(){
    function r2(x){ return Math.round(x); }
    var sb = document.getElementById('sidebar');
    var toggle = document.getElementById('navToggle');
    var logoTitle = document.querySelector('#titlebar .logo');
    var brand = sb.querySelector('.brand');
    var navs = sb.querySelectorAll('.nav');
    var lastTwo = [navs[navs.length-2], navs[navs.length-1]].map(function(n){ return n ? n.getAttribute('data-page') : null; });
    var sbRect = sb.getBoundingClientRect();
    // 折叠态：取第一个 nav 的 .lbl 计算样式
    var firstLbl = sb.querySelector('.nav .lbl');
    var lblDisp = firstLbl ? getComputedStyle(firstLbl).display : '(无)';
    // 指示条
    var ind = document.getElementById('nav-ind');
    // 汉堡图标是否注入
    var toggleIco = toggle ? toggle.querySelector('.ico') : null;
    // sprite 里有没有 menu
    var menuSvg = document.getElementById('i-menu');
    return JSON.stringify({
      sbWidth: r2(sbRect.width),
      sbExpanded: sb.classList.contains('expanded'),
      hasToggle: !!toggle,
      toggleHasIco: !!toggleIco,
      titlebarLogo: !!logoTitle,
      brand: !!brand,
      navCount: navs.length,
      navPages: Array.prototype.map.call(navs, function(n){ return n.getAttribute('data-page'); }),
      lastTwo: lastTwo,
      lblDisplayCollapsed: lblDisp,
      indExists: !!ind,
      menuInSprite: !!menuSvg,
      sideFootText: (document.getElementById('sideFoot')||{textContent:''}).textContent
    });
  })()` }).then((r) => r.result.value));

  const m0 = await measure();
  check('★ 标题栏 logo 已移除', m0.titlebarLogo === false, `titlebarLogo=${m0.titlebarLogo}`);
  check('★ 侧栏品牌块(.brand)已移除', m0.brand === false, `brand=${m0.brand}`);
  check('★ 顶部汉堡开关存在且注入了图标', m0.hasToggle && m0.toggleHasIco, `hasToggle=${m0.hasToggle} ico=${m0.toggleHasIco}`);
  check('★ 默认折叠（侧栏宽≈52、无 expanded 类）', !m0.sbExpanded && Math.abs(m0.sbWidth - 52) <= 2, `width=${m0.sbWidth} expanded=${m0.sbExpanded}`);
  check('★ sprite 里有 menu(汉堡)图标', m0.menuInSprite === true);
  check('★ 导航项共 7 个', m0.navCount === 7, `navCount=${m0.navCount}`);
  check('★ 标签默认隐藏（折叠态 lbl display=none）', m0.lblDisplayCollapsed === 'none', `lbl=${m0.lblDisplayCollapsed}`);
  check('★ 选中指示条仍在', m0.indExists === true);
  check('★ 设置/关于钉在最后两位', JSON.stringify(m0.lastTwo) === JSON.stringify(['settings','about']), JSON.stringify(m0.lastTwo));

  // 截图：折叠态
  const sbRect0 = JSON.parse(await send('Runtime.evaluate', { expression: `(function(){ var r=document.getElementById('sidebar').getBoundingClientRect(); return JSON.stringify({x:Math.floor(r.x),y:Math.floor(r.y),w:Math.ceil(r.width),h:Math.ceil(r.height)}); })()` }).then((r) => r.result.value));
  fs.writeFileSync(OUT + '\\sidebar-collapsed.png', await shoot({ x: sbRect0.x, y: sbRect0.y, width: sbRect0.w, height: Math.min(sbRect0.h, 700) }));

  // 点汉堡展开
  await send('Runtime.evaluate', { expression: `document.getElementById('navToggle').click()` });
  await sleep(350); // 等宽度过渡
  const m1 = await measure();
  check('★ 点汉堡后展开（expanded 类 + 宽≈208）', m1.sbExpanded && Math.abs(m1.sbWidth - 208) <= 3, `width=${m1.sbWidth} expanded=${m1.sbExpanded}`);
  check('★ 展开后标签可见（lbl display≠none）', m1.lblDisplayCollapsed !== 'none', `lbl=${m1.lblDisplayCollapsed}`);

  // 截图：展开态
  const sbRect1 = JSON.parse(await send('Runtime.evaluate', { expression: `(function(){ var r=document.getElementById('sidebar').getBoundingClientRect(); return JSON.stringify({x:Math.floor(r.x),y:Math.floor(r.y),w:Math.ceil(r.width),h:Math.ceil(r.height)}); })()` }).then((r) => r.result.value));
  fs.writeFileSync(OUT + '\\sidebar-expanded.png', await shoot({ x: sbRect1.x, y: sbRect1.y, width: sbRect1.w, height: Math.min(sbRect1.h, 700) }));

  // 切回折叠，再截一张全窗（给主人看整体）
  await send('Runtime.evaluate', { expression: `document.getElementById('navToggle').click()` });
  await sleep(350);
  fs.writeFileSync(OUT + '\\sidebar-full.png', await shoot(null));

  console.log('');
  console.log('—— 运行期异常 ——');
  const real = events.filter((e) => !/favicon/i.test(e));
  check('★ 无 JS 运行时异常', real.length === 0, real.slice(0, 3).join(' | '));

  const pass = results.filter((r) => r.ok).length;
  console.log('\n' + '='.repeat(60));
  console.log(`${pass} 通过 / ${results.length - pass} 失败`);
  console.log('截图: ' + OUT + '\\sidebar-collapsed.png, sidebar-expanded.png, sidebar-full.png');
  console.log('='.repeat(60));

  try { child.kill(); } catch (_e) {}
  process.exit(results.some((r) => !r.ok) ? 1 : 0);
})().catch((e) => { console.error('脚本自身出错:', e && e.stack || e); process.exit(2); });
