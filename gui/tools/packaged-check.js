#!/usr/bin/env node
/* packaged-check.js —— **打包版真机验收**：直接启动对方的分发版 exe（含补丁版 app.asar），
 * 用 CDP 连进它的渲染进程做断言 + 截图。
 *
 * 和我们另一套 tools/*.js 的区别：那些是拿 headless Chromium 开网关 URL 的
 * 「零侵入预览验收」；这个是**真·Electron 打包进程**，验的是「补丁塞进 asar 后
 * 整个应用还能不能跑、皮肤/布局是不是真的生效」——两件事，不能互相替代。
 *
 * 用法：node tools/packaged-check.js [应用目录] [调试端口]
 * 安全约束：
 *   · 只读源目录（Desktop 那份**不碰**），跑的是 D 盘副本
 *   · APPDATA / LOCALAPPDATA / TMP / TEMP 全部重定向到 D 盘 ⇒ 一个字节都不写 C 盘
 *   · 结束用 window.close() 走正常退出路径 ⇒ 主进程的 stopSidecar 会跑，不留孤儿 python
 */
const { spawn, spawnSync } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const APP_DIR = process.argv[2] || '<REPO>\\output\\.tmp\\run_test';
const PORT = Number(process.argv[3] || 9345);
const EXE = path.join(APP_DIR, 'ADOFAI 谱面生成器.exe');
const OUT = '<REPO>\\output\\logs\\packaged';
const APPDATA = '<REPO>\\output\\.tmp\\appdata';
const TMPDIR = '<REPO>\\output\\.tmp\\apptmp';

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
  if (!fs.existsSync(EXE)) throw new Error('找不到 exe：' + EXE);
  fs.mkdirSync(OUT, { recursive: true });
  for (const d of [APPDATA, path.join(APPDATA, 'Local'), TMPDIR]) fs.mkdirSync(d, { recursive: true });

  const logFd = fs.openSync(path.join(OUT, 'app-stdout.log'), 'w');
  const child = spawn(EXE, [`--remote-debugging-port=${PORT}`], {
    cwd: APP_DIR,
    stdio: ['ignore', logFd, logFd],
    env: { ...process.env, APPDATA, LOCALAPPDATA: path.join(APPDATA, 'Local'), TMP: TMPDIR, TEMP: TMPDIR },
  });
  console.log(`已启动 ${path.basename(EXE)} (pid ${child.pid})，调试端口 ${PORT}`);
  let exited = null;
  child.on('exit', (c) => { exited = c; });

  // ── 等 DevTools 端点 + page target
  let page = null;
  for (let i = 0; i < 120; i++) {
    if (exited !== null) throw new Error(`应用提前退出 code=${exited}（看 ${OUT}\\app-stdout.log）`);
    try {
      const list = JSON.parse(await get('/json/list'));
      page = list.find((t) => t.type === 'page');
      if (page) break;
    } catch (_e) { /* 还没起来 */ }
    await sleep(500);
  }
  if (!page) throw new Error('60s 内没等到渲染进程 DevTools');
  console.log('page target: ' + page.url);

  // ── 连 CDP
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  const waiters = new Map();
  let seq = 0;
  const events = [];
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && waiters.has(m.id)) { waiters.get(m.id)(m); waiters.delete(m.id); return; }
    if (m.method === 'Runtime.exceptionThrown') {
      const d = m.params.exceptionDetails || {};
      events.push('EXC ' + ((d.exception && d.exception.description) || d.text));
    }
    if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') {
      const e = m.params.entry;
      const url = e.url || '';
      if (/favicon\.ico/.test(url)) return;            // Electron 不请求 favicon，纯噪音
      events.push('LOG ' + e.text + (url ? ' @' + url : ''));
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
    if (r.exceptionDetails) throw new Error('JS: ' + ((r.exceptionDetails.exception || {}).description));
    return r.result && r.result.value;
  };
  const shot = async (name) => {
    const r = await send('Page.captureScreenshot', { format: 'png' });
    const f = path.join(OUT, name + '.png');
    fs.writeFileSync(f, Buffer.from(r.data, 'base64'));
    console.log('SAVED ' + f);
  };

  await send('Page.enable');
  await send('Log.enable');
  await send('Runtime.enable');

  // ── 等界面初始化（schema 拿到、检查器建出分组）
  let groups = 0;
  for (let i = 0; i < 80; i++) {
    groups = await ev(`document.querySelectorAll('#groups section.grp').length`);
    if (groups > 0) break;
    await sleep(500);
  }
  await sleep(800);

  // ─────────────── ① 打包 + sidecar 存活（证明 app.isPackaged 没被破坏）
  const href = await ev('location.href');
  check('① 页面来自 asar 内部', /app\.asar\/renderer\/index\.html$/.test(href), href);
  const info = await ev('window.dsh.info()');
  check('② app:info 可用（主进程 IPC 通）', !!info && !!info.root, JSON.stringify(info || {}));
  check('② ROOT 指向 <应用目录>\\resources', String((info || {}).root || '').replace(/\\/g, '\\')
        .toLowerCase() === path.join(APP_DIR, 'resources').toLowerCase(), (info || {}).root);
  const heal = await ev('(async()=>{const r=await fetch(window.dsh.base+"/api/health");return await r.json();})()');
  check('③ ★ sidecar 就绪（PACKAGED 判定没翻，否则会退回系统 python 而起不来）',
        !!heal && heal.ok === true, JSON.stringify(heal || {}).slice(0, 200));
  check('④ 检查器建出分组（app.js 作为 ES module 在 asar 里正常执行）', groups > 0, `groups=${groups}`);

  // ─────────────── ② 皮肤有没有真的生效
  const skin = await ev(`(function(){
    const cs=getComputedStyle(document.documentElement);
    const sheets=[...document.styleSheets].map(s=>{let n=0;try{n=s.cssRules.length}catch(_e){n=-1}
      return {href:(s.href||'').split('/').pop(), rules:n};});
    const body=getComputedStyle(document.body);
    const stage=document.getElementById('stage');
    const pane=document.querySelector('.pane');
    return {
      sheets,
      bodyBg: body.backgroundColor,
      bg: cs.getPropertyValue('--bg').trim(),
      panel: cs.getPropertyValue('--panel').trim(),
      accent: cs.getPropertyValue('--accent').trim(),
      stageBg: stage ? getComputedStyle(stage).backgroundColor : '-',
      paneBg: pane ? getComputedStyle(pane).backgroundColor : '-',
      paneBlur: pane ? getComputedStyle(pane).backdropFilter : '-',
    };
  })()`);
  const bySheet = Object.fromEntries(skin.sheets.map((s) => [s.href, s.rules]));
  check('⑤ skin-workbench.css 已加载且规则可读', (bySheet['skin-workbench.css'] || 0) > 50,
        `rules=${bySheet['skin-workbench.css']} 全部表=${JSON.stringify(skin.sheets)}`);
  check('⑤ workbench-elements.css 已加载', (bySheet['workbench-elements.css'] || 0) > 10,
        `rules=${bySheet['workbench-elements.css']}`);
  check('⑥ body 透明（亚克力材质能在内容区透出来的前提）', skin.bodyBg === 'rgba(0, 0, 0, 0)', skin.bodyBg);
  check('⑥ 画布区仍是不透明实底（预览不能糊）', skin.stageBg === 'rgb(13, 17, 23)', skin.stageBg);
  check('⑦ 令牌被重映射（--accent 原 #2f5d80 → 我们的 #4fc3f7）', skin.accent === '#4fc3f7', skin.accent);
  check('⑦ 面板半透明', /^rgba\(/.test(skin.paneBg) && parseFloat(skin.paneBg.split(',').pop()) < 1, skin.paneBg);
  check('⑦ 面板有磨砂', /blur\(/.test(skin.paneBlur), skin.paneBlur);

  // ─────────────── ③ 布局改造在不在
  const rep1 = await ev(`(function(){
    const r=document.getElementById('report');
    return { head: !!document.getElementById('rep-head'), caret: !!document.getElementById('rep-caret'),
             brief: !!document.getElementById('rep-brief'), split: !!document.getElementById('split-report'),
             collapsed: r.classList.contains('collapsed'), h: Math.round(r.getBoundingClientRect().height),
             detailHidden: !document.getElementById('detail').classList.contains('on') };
  })()`);
  check('⑧ 报告带：标题栏 / 箭头 / 摘要 / 拖高条 都在',
        rep1.head && rep1.caret && rep1.brief && rep1.split, JSON.stringify(rep1));
  check('⑨ 报告带默认收起（一行）', rep1.collapsed && rep1.h < 60, `collapsed=${rep1.collapsed} h=${rep1.h}`);
  await shot('01-packaged-默认');

  // 点标题栏 → 展开
  await ev(`document.getElementById('rep-head').click()`);
  await sleep(400);
  const rep2 = await ev(`(function(){const r=document.getElementById('report');
    return {collapsed:r.classList.contains('collapsed'),h:Math.round(r.getBoundingClientRect().height)};})()`);
  check('⑩ 点标题栏能展开', !rep2.collapsed && rep2.h > rep1.h, `h ${rep1.h} → ${rep2.h}`);
  await shot('02-packaged-报告展开');

  // ─────────────── ④ 工具条药丸 + 浮层
  const pill = await ev(`(function(){
    const b=document.getElementById('btn-av-sum'), p=document.getElementById('av-pop');
    const bar=document.getElementById('vbar-chart');
    return { pill: !!b, pillText: b?b.textContent.trim():'-', pop: !!p,
             popHidden: p?p.classList.contains('hidden'):null,
             barWrap: bar?getComputedStyle(bar).flexWrap:'-',
             barOver: bar?(bar.scrollWidth>bar.clientWidth+1):null };
  })()`);
  check('⑪ 药丸在位、浮层已建但隐藏', pill.pill && pill.pop && pill.popHidden === true,
        `pill="${pill.pillText}" popHidden=${pill.popHidden}`);
  check('⑫ 工具条保留了宿主自己的换行规则（皮肤没越界改布局）', pill.barWrap === 'wrap' && pill.barOver === false,
        `flexWrap=${pill.barWrap} 溢出=${pill.barOver}`);
  await ev(`document.getElementById('btn-av-sum').click()`);
  await sleep(400);
  const pop = await ev(`(function(){
    const p=document.getElementById('av-pop');
    const ids=['music_delay_ms'];
    const inputs=[...p.querySelectorAll('input,select')].map(e=>e.id);
    const r=p.getBoundingClientRect();
    return { hidden:p.classList.contains('hidden'), n:p.querySelectorAll('input,select,button').length,
             inputs, w:Math.round(r.width), h:Math.round(r.height),
             inWin: r.left>=0 && r.right<=innerWidth+1 && r.top>=0 };
  })()`);
  check('⑬ 点药丸 → 浮层打开且有控件', pop.hidden === false && pop.n >= 4,
        `hidden=${pop.hidden} 控件数=${pop.n} 尺寸=${pop.w}x${pop.h}`);
  check('⑬ 浮层没跑出窗口', pop.inWin === true);
  await shot('03-packaged-音源浮层');

  // ─────────────── ⑤ 他原有的功能一个没丢
  const keep = await ev(`(function(){
    const q=(s)=>!!document.querySelector(s);
    return { panes:[...document.querySelectorAll('[data-pane]')].map(e=>e.dataset.pane),
             presets:q('#presets'), xkHud:q('#xk-hud'), detail:q('#detail'), flxk:q('#fl-xk'),
             splitL:q('#split-left'), splitR:q('#split-right'), splitB:q('#split-bottom'),
             vbars:q('#viewbars'), tabs:document.querySelectorAll('#tabs button,[role=tab]').length };
  })()`);
  check('⑭ 他的分栏/停靠全在（4 个 pane + 左右下 3 条分隔）',
        keep.panes.length >= 4 && keep.splitL && keep.splitR && keep.splitB && keep.splitB,
        `panes=${keep.panes.join(',')} L/R/B=${keep.splitL}/${keep.splitR}/${keep.splitB}`);
  check('⑭ 他的独有功能都在（预设条 / 采bpm HUD / 报告明细 / 浮动骨架窗）',
        keep.presets && keep.xkHud && keep.detail && keep.flxk,
        `presets=${keep.presets} xk-hud=${keep.xkHud} detail=${keep.detail} fl-xk=${keep.flxk}`);

  // ─────────────── ⑥ 日志干净
  const errs = events.filter((e) => !/favicon/.test(e));
  check('⑮ 渲染进程无 JS 异常 / 无 console error', errs.length === 0, errs.slice(0, 5).join(' | ') || '无');

  // ── 收尾
  console.log(`\n===== ${PASS} 通过 / ${FAIL} 失败 =====`);
  console.log('（应用日志：' + path.join(OUT, 'app-stdout.log') + '）');

  console.log('\n=== app-stdout.log 摘要 ===');
  console.log(fs.readFileSync(path.join(OUT, 'app-stdout.log'), 'utf8').split('\n').filter((l) => l.trim()).slice(0, 12).join('\n'));

  try { await ev('window.close()'); } catch (_e) { /* 关窗后 CDP 会断，正常 */ }
  for (let i = 0; i < 30 && exited === null; i++) await sleep(500);
  if (exited === null) {
    console.log('窗口没在 15s 内自己退出，强杀进程树');
    spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
  } else {
    console.log(`\n应用已正常退出 code=${exited}（走的是 window-all-closed → stopSidecar 的正常路径）`);
  }
  process.exit(FAIL ? 1 : 0);
})().catch((e) => {
  console.error('CRASH: ' + (e && e.stack || e));
  process.exit(2);
});
