/* skinshot.js —— 皮肤验收：无头 Chromium 打开「Adofai-Chart-Generator 0.5.0 前端 + 皮肤」，
 * 在同一张"壁纸"上分别截「开皮肤 / 关皮肤」，并断言令牌、透明度、布局、无异常。
 *
 *   · 不碰 8765/8766（只连主人的壳已经起好的网关），不会关掉主人正在用的窗口
 *   · 壁纸 = 给 <html> 垫一层鲜艳渐变，用来**证明**半透明面板确实透出后面的东西
 *     （Electron 里那层就是系统亚克力材质）
 *
 * 用法：node tools/skinshot.js [url] [chrome.exe] [outDir]
 * ========================================================================== */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const URL_ = process.argv[2] || 'http://127.0.0.1:8766/studio-skin/index.html';
const CHROME = process.argv[3] || 'chrome';
const OUT = process.argv[4] || '<REPO>\\output\\logs\\skinshot';
const PORT = 9335;
const UDD = '<REPO>\\output\\.tmp\\skinshot-profile';
const W = 1560, H = 940;

// ★ 垫在页面后面的那层"系统材质替身"：
//   默认用**鲜艳渐变**——它的颜色与界面深色差得远，像素探针一眼就能证伪
//   「半透明确实透出来了」；出效果图时设 SKINSHOT_WALL=<图片 URL/路径> 换成真实
//   壁纸照片（更像真机，但颜色接近时不适合做断言）。
const WALL_IMG = process.env.SKINSHOT_WALL || '';
const WALLPAPER = WALL_IMG
  ? `#101418 url("${WALL_IMG}") center / cover no-repeat fixed`
  : "linear-gradient(135deg,#7b2ff7 0%,#f107a3 22%,#0f9b8e 48%,#f7b733 72%,#ec008c 100%)";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = (p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: PORT, path: p }, (r) => {
    let b = ''; r.on('data', (c) => (b += c)); r.on('end', () => res(b));
  }).on('error', rej);
});

let PASS = 0, FAIL = 0;
const check = (name, ok, detail) => {
  (ok ? PASS++ : FAIL++);
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '   [' + detail + ']' : ''}`);
};

(async () => {
  fs.mkdirSync(UDD, { recursive: true });
  fs.mkdirSync(OUT, { recursive: true });
  const child = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--mute-audio', '--force-color-profile=srgb',
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${UDD}`,
    `--window-size=${W},${H}`, 'about:blank',
  ], { stdio: 'ignore', windowsHide: true });

  let list = [];
  for (let i = 0; i < 80; i++) {
    try { list = JSON.parse(await get('/json/list')); if (list.some((t) => t.type === 'page')) break; }
    catch (_e) { /* 等浏览器起来 */ }
    await sleep(250);
  }
  const page = list.find((t) => t.type === 'page');
  if (!page) throw new Error('没能连上 headless 浏览器的 DevTools');

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  const waiters = new Map();
  let seq = 0;
  const events = [];
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && waiters.has(m.id)) { waiters.get(m.id)(m); waiters.delete(m.id); return; }
    if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails || {};
      events.push('EXC ' + (d.exception && d.exception.description || d.text));
    }
    if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') {
      events.push('LOG ' + m.params.entry.text);
    }
  });
  await new Promise((r) => ws.addEventListener('open', r));
  const send = (method, params) => new Promise((res, rej) => {
    const id = ++seq;
    waiters.set(id, (m) => (m.error ? rej(new Error(method + ': ' + m.error.message)) : res(m.result)));
    ws.send(JSON.stringify({ id, method, params: params || {} }));
  });
  const ev = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error('JS: ' + (r.exceptionDetails.exception || {}).description);
    return r.result && r.result.value;
  };
  const shot = async (name, clip, scale) => {
    const p = { format: 'png' };
    if (clip) p.clip = Object.assign({ scale: scale || 1 }, clip);
    const r = await send('Page.captureScreenshot', p);
    const f = path.join(OUT, name + '.png');
    fs.writeFileSync(f, Buffer.from(r.data, 'base64'));
    console.log('SAVED ' + f);
    return f;
  };

  await send('Page.enable');
  await send('Log.enable');
  await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url: URL_ });
  // ★ 干净起点（2026-09-19）：布局存在 localStorage（adofai-ui-layout），
  //   这个 profile 是复用的 ⇒ 上一次跑留下的「报告带展开 / ③b 浮窗开着」会被截图带进去，
  //   看着像我们的默认值。导航后清掉再重载，截图才反映**真实默认态**。
  await sleep(1500);
  await ev(`try { localStorage.clear(); } catch (_e) {} 1`);
  await send('Page.reload', { ignoreCache: true });
  await sleep(800);

  // 等他们的 app.js 把检查器建出来（说明 schema 拿到了、页面跑到底了）
  let groups = 0;
  for (let i = 0; i < 60; i++) {
    groups = await ev(`document.querySelectorAll('#groups section.grp').length`);
    if (groups > 0) break;
    await sleep(500);
  }
  await sleep(900);
  check('页面初始化完成（拿到 schema、检查器建出分组）', groups > 0, `groups=${groups}`);

  // 垫一层壁纸：模仿 Electron 里页面后面的系统材质
  // ★ 用 JSON.stringify 传值，别直接拼进模板串 —— 壁纸是 url("…") 时里面的引号
  //   会把注入的 JS 打断（SyntaxError: Unexpected identifier 'http'）。
  const withWall = async () => ev(`document.documentElement.style.background = ${JSON.stringify(WALLPAPER)}; 1`);
  const noWall = async () => ev(`document.documentElement.style.background = ""; 1`);

  // ── ① 关皮肤（把 link 禁用）＋ 壁纸 → 基线
  await ev(`(function(){var l=document.querySelector('link[href*="skin-workbench"]'); if(l) l.disabled=true; return 1;})()`);
  await withWall();
  await sleep(600);
  await shot('1-before-no-skin');
  const before = await ev(`(function(){
    const cs=getComputedStyle(document.body);
    return { bodyBg: cs.backgroundColor, accent: getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() };
  })()`);

  // ── ② 开皮肤 ＋ 壁纸 → 半透明面板应透出壁纸
  await ev(`(function(){var l=document.querySelector('link[href*="skin-workbench"]'); if(l) l.disabled=false; return 1;})()`);
  await sleep(700);
  await shot('2-after-with-skin');
  const after = await ev(`(function(){
    const root=getComputedStyle(document.documentElement);
    const body=getComputedStyle(document.body);
    const panel=getComputedStyle(document.querySelector('.pane'));
    const top=getComputedStyle(document.getElementById('top'));
    const stage=getComputedStyle(document.getElementById('stage'));
    const tl=getComputedStyle(document.getElementById('tlwrap'));
    const tabs=getComputedStyle(document.getElementById('tabs'));
    return {
      accent: root.getPropertyValue('--accent').trim(),
      panelVar: root.getPropertyValue('--panel').trim(),
      panelBg: panel.backgroundColor,
      bodyBg: body.backgroundColor,
      topBlur: top.backdropFilter || top.webkitBackdropFilter,
      panelBlur: panel.backdropFilter || panel.webkitBackdropFilter,
      topH: Math.round(document.getElementById('top').getBoundingClientRect().height),
      stageBg: stage.backgroundColor, tlBg: tl.backgroundColor, tabsBg: tabs.backgroundColor,
      chipRadius: (function(){var c=document.querySelector('.chip'); return c?getComputedStyle(c).borderRadius:'(无 chip)';})(),
      grpRadius: (function(){var g=document.querySelector('section.grp'); return g?getComputedStyle(g).borderRadius:'(无分组)';})(),
      btnBg: getComputedStyle(document.querySelector('button')).backgroundColor,
      btnRadius: getComputedStyle(document.querySelector('button')).borderRadius,
      font: body.fontSize,
    };
  })()`);

  check('① 皮肤已生效：--accent 换成青蓝 #4fc3f7',
        /#4fc3f7|rgb\(79, 195, 247\)/i.test(after.accent), `${before.accent} → ${after.accent}`);
  check('② 面板变量是半透明（α<1，材质才透得出来）',
        /rgba?\([^)]*0?\.\d+\)/.test(after.panelVar) && after.panelBg !== 'rgb(24, 30, 40)', `${after.panelVar} / ${after.panelBg}`);
  check('③ 页面自身底色透明（不盖住系统材质）', after.bodyBg === 'rgba(0, 0, 0, 0)', after.bodyBg);
  check('④ 顶栏/面板带磨砂 backdrop-filter', /blur\(/.test(after.topBlur) && /blur\(/.test(after.panelBlur),
        `top=${after.topBlur} pane=${after.panelBlur}`);
  check('⑤ 画布区保持不透明实底（预览不能糊）',
        after.stageBg !== 'rgba(0, 0, 0, 0)' && after.tlBg !== 'rgba(0, 0, 0, 0)', `stage=${after.stageBg} tl=${after.tlBg}`);
  check('⑥ 顶栏高度 = 46px（工作台规格）', after.topH === 46, String(after.topH));
  check('⑦ 圆角规格：按钮 6px / 分组 8px / 胶囊 999px',
        after.btnRadius === '6px' && after.grpRadius === '8px' && (/999px/.test(after.chipRadius) || after.chipRadius === '(无 chip)'),
        `btn=${after.btnRadius} grp=${after.grpRadius} chip=${after.chipRadius}`);

  // ── ③ 皮肤开、无壁纸 → 材质不可用时的降级观感（应是纯净深色，不脏）
  await noWall();
  await sleep(500);
  await shot('3-after-fallback-no-material');

  // ── ④ 细节特写
  const geo = await ev(`(function(){
    const t=document.getElementById('top').getBoundingClientRect();
    const r=document.getElementById('report').getBoundingClientRect();
    const g=document.getElementById('groups').getBoundingClientRect();
    return { top:{x:0,y:0,width:innerWidth,height:Math.round(t.height)+4},
             insp:{x:Math.round(g.left-4),y:Math.round(g.top-30),width:Math.round(g.width+8),height:Math.min(420,Math.round(g.height)+34)},
             rep:{x:0,y:Math.max(0,Math.round(r.top)-4),width:innerWidth,
                  height:Math.min(window.innerHeight-Math.round(r.top)+4,Math.round(r.height)+8)} };
  })()`);
  await shot('4-crop-topbar', geo.top, 2);
  await shot('5-crop-inspector', geo.insp, 2);
  await shot('6-crop-report', geo.rep, 2);

  // ── ⑤ 最小窗口（他们的 minWidth/minHeight = 1100×700）下不破版
  await send('Emulation.setDeviceMetricsOverride', { width: 1100, height: 700, deviceScaleFactor: 1, mobile: false });
  await withWall();
  await sleep(800);
  const mini = await ev(`(function(){
    const st=document.getElementById('stage'), work=document.getElementById('work'), bar=document.getElementById('vbar-chart');
    const hint = bar ? bar.querySelector('.hint') : null;
    const hs = hint ? getComputedStyle(hint) : null;
    return { stageH: Math.round(st.getBoundingClientRect().height),
             workH: Math.round(work.getBoundingClientRect().height),
             docOverflowX: document.documentElement.scrollWidth - window.innerWidth,
             barWrap: bar ? getComputedStyle(bar).flexWrap : 'n/a',
             barOverflow: bar ? (bar.scrollWidth > bar.clientWidth + 1) : false,
             hintWs: hs ? hs.whiteSpace : 'n/a',
             hintOv: hs ? hs.overflow : 'n/a',
             hintTrunc: hint ? (hint.scrollWidth > hint.clientWidth + 1) : false };
  })()`);
  await shot('7-min-1100x700');
  check('⑧ 1100×700 最小窗口下预览窗格未被挤没',
        mini.stageH >= 200 && mini.workH > 300, `stage=${mini.stageH} work=${mini.workH}`);
  check('⑨ 页面无横向溢出', mini.docOverflowX <= 0, `溢出 ${mini.docOverflowX}px`);
  // ⑩ 皮肤**不许**改工具条的换行与滚动 —— v1 曾把 `.vbar` 改成
  //    `flex-wrap:nowrap + overflow-x:auto`（照搬我们自己的工作台），后果是窄窗口里
  //    提示文字溢出、药丸被推出可视区。这里断言「保留他的 wrap 且自身不横向溢出」。
  check('⑩ 皮肤没越界改工具条：保留他的 wrap 且不横向溢出',
        mini.barWrap === 'wrap' && mini.barOverflow === false,
        `flexWrap=${mini.barWrap} 条内横向溢出=${mini.barOverflow}`);
  // ⑩b 提示文字长到放不下时须「省略号」而不是溢出：nowrap 是 ellipsis 生效的前提
  check('⑩b 工具条提示文字收缩用省略号（不是溢出）',
        mini.hintWs === 'nowrap' && /hidden/.test(mini.hintOv),
        `whiteSpace=${mini.hintWs} overflow=${mini.hintOv} 已截断=${mini.hintTrunc}`);

  const bad = events.filter((s) => !/favicon|net::ERR|Failed to load resource/i.test(s));
  check('⑪ 全页无 JS 异常', bad.length === 0, bad.slice(0, 3).join(' | ') || '(无)');
  const netErrs = events.filter((s) => /net::ERR|Failed to load resource/i.test(s));
  if (netErrs.length) console.log(`      （另有 ${netErrs.length} 条资源/请求失败，浏览器预览无 sidecar 时正常：${netErrs[0].slice(0, 70)}…）`);

  console.log('\nBEFORE ' + JSON.stringify(before));
  console.log('AFTER  ' + JSON.stringify(after));
  console.log(`\n==== ${PASS} 通过 / ${FAIL} 失败 ====`);
  ws.close();
  try { child.kill(); } catch (_e) { /* ignore */ }
  process.exit(FAIL ? 1 : 0);
})().catch((e) => { console.error('ERR ' + (e && e.stack || e)); process.exit(1); });
