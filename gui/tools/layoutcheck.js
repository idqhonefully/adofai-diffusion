/* layoutcheck.js —— 「增量布局改造包」验收：无头 Chromium 打开「Adofai-Chart-Generator 0.5.0 前端 +
 * 布局补丁」，断言新增行为**都对**，且他的原有功能**一个都没丢**。
 *
 *   改造目标（只动排布，不动功能）：
 *     ① 报告带默认收起（只剩标题栏一行摘要）+ 点标题栏收起/展开 + 拖分隔条调高 + 双击复位
 *     ② 工具条那一坨收成药丸 + 浮层（窄窗口不再横向溢出）
 *     ③ 三栏宽度对齐（左 312 / 右 380）
 *   红线：他的移动模式 / 四向停靠 / 预设 / 采bpm HUD / 浮窗 / 报告明细 全部保留。
 *
 *   · 不碰 8765/8766（只连已起好的网关），不会关掉主人正在用的窗口
 *   · 每次跑**清空 profile**（localStorage 里就是布局），保证从 DEF_LAYOUT 开始
 *
 * 用法：node tools/layoutcheck.js [url] [chrome.exe] [outDir]
 * ========================================================================== */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const URL_ = process.argv[2] || 'http://127.0.0.1:8766/studio-skin/index.html';
const CHROME = process.argv[3] || 'chrome';
const OUT = process.argv[4] || '<REPO>\\output\\logs\\layoutshot';
const PORT = 9336;
const UDD = '<REPO>\\output\\.tmp\\layoutcheck-profile';
const W = 1560, H = 940;

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

// 读回存下来的布局（localStorage，saveLayout 有 400ms 防抖 ⇒ 调用前先等 600ms）
const READ_L = `(() => { try { return JSON.parse(localStorage.getItem('adofai-ui-layout') || 'null'); }
  catch (_e) { return null; } })()`;

(async () => {
  // ★ 不删 profile 目录（沙箱对「一次删 50+ 文件」有安全闸，而且没必要）：
  //   改成导航后清 localStorage 再重载 —— 布局就存在那里，一样是干净起点。
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
      events.push('EXC ' + ((d.exception && d.exception.description) || d.text));
    }
    if (m.method === 'Log.entryAdded' && m.params.entry.level === 'error') {
      // ★ 带上 url：CDP 的 `entry.text` 只有「Failed to load resource」这种空话，
      //   不知道是谁 404；`entry.url` 才有真凶（favicon 噪音靠它过滤）。
      const e = m.params.entry;
      events.push('LOG ' + e.text + (e.url ? ' @ ' + e.url : ''));
    }
    // ★ 4xx/5xx 也记下来（不然只剩一句「Failed to load resource」，不知道是谁 404）
    if (m.method === 'Network.responseReceived') {
      const r = m.params.response || {};
      if (r.status >= 400) events.push(`HTTP ${r.status} ${r.url}`);
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
  const shot = async (name, clip) => {
    const p = { format: 'png' };
    if (clip) p.clip = Object.assign({ scale: 1 }, clip);
    const r = await send('Page.captureScreenshot', p);
    const f = path.join(OUT, name + '.png');
    fs.writeFileSync(f, Buffer.from(r.data, 'base64'));
    console.log('SAVED ' + f);
  };

  await send('Page.enable');
  await send('Log.enable');
  await send('Runtime.enable');
  await send('Network.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url: URL_ });

  // 等页面把检查器建出来（说明 schema 拿到了、app.js 跑到底了）
  const waitReady = async () => {
    let g = 0;
    for (let i = 0; i < 60; i++) {
      try { g = await ev(`document.querySelectorAll('#groups section.grp').length`); } catch (_e) { g = 0; }
      if (g > 0) return g;
      await sleep(500);
    }
    return g;
  };
  await waitReady();
  // ★ 清掉上次跑留下的布局（localStorage 就是它的家）→ 重载 → 从 DEF_LAYOUT 开始
  await ev(`try { localStorage.clear(); } catch (_e) {} 1`);
  await send('Page.reload', { ignoreCache: true });
  const groups = await waitReady();
  await sleep(900);
  check('页面初始化完成（schema 拿到、检查器建出分组）', groups > 0, `groups=${groups}`);

  // ─────────────────────────── ① 报告带收起 ───────────────────────────
  const r1 = await ev(`(function(){
    const rep = document.getElementById('report');
    const h = document.getElementById('rep-head');
    return { collapsed: rep.classList.contains('collapsed'),
             h: Math.round(rep.getBoundingClientRect().height),
             caret: (document.getElementById('rep-caret') || {}).textContent || '',
             brief: ((document.getElementById('rep-brief') || {}).textContent || '').trim(),
             splitShown: getComputedStyle(document.querySelector('.split[data-split="bottom"]')).display !== 'none' };
  })()`);
  check('① 报告带**默认收起**', r1.collapsed === true, JSON.stringify(r1.collapsed));
  check('① 收起后只占标题栏那一行（高度 ≤ 34px）', r1.h > 12 && r1.h <= 34, `${r1.h}px`);
  check('① 收起态箭头是 ▸', r1.caret === '▸', r1.caret);
  check('① 收起态标题栏有摘要（收起≠失明）', r1.brief.length > 0, r1.brief.slice(0, 40));
  check('① 收起时那条分隔条自动隐藏（拖它没意义）', r1.splitShown === false);

  await ev(`document.getElementById('rep-head').click(); 1`);
  await sleep(600);
  const r2 = await ev(`(function(){
    const rep = document.getElementById('report');
    return { collapsed: rep.classList.contains('collapsed'),
             h: Math.round(rep.getBoundingClientRect().height),
             caret: (document.getElementById('rep-caret') || {}).textContent || '',
             splitShown: getComputedStyle(document.querySelector('.split[data-split="bottom"]')).display !== 'none',
             chips: document.querySelectorAll('#chips .chip').length,
             status: !!document.getElementById('status'),
             detail: !!document.getElementById('detail'),
             more: !!document.getElementById('more') };
  })()`);
  check('① 点标题栏能展开', r2.collapsed === false);
  check('① 展开后高度明显变大（> 34px）', r2.h > 34, `${r2.h}px`);
  check('① 展开态箭头是 ▾', r2.caret === '▾', r2.caret);
  check('① 展开后分隔条回来了', r2.splitShown === true);
  check('① 展开后原内容都在（chips / status / detail / more）',
    r2.status && r2.detail && r2.more, `chips=${r2.chips}`);
  const L2 = await ev(READ_L);
  check('① 展开态已落盘（panes.report.collapsed=false）',
    !!L2 && L2.panes && L2.panes.report && L2.panes.report.collapsed === false,
    JSON.stringify(L2 && L2.panes && L2.panes.report));

  // 拖动分隔条改高度
  const r3 = await ev(`(function(){
    const sp = document.querySelector('.split[data-split="bottom"]');
    const b = sp.getBoundingClientRect();
    const x = b.left + 2, y = b.top + 2;
    const pe = (t, cy) => new PointerEvent(t, { bubbles:true, cancelable:true, clientX:x, clientY:cy, pointerId:1, isPrimary:true });
    sp.dispatchEvent(pe('pointerdown', y));
    window.dispatchEvent(pe('pointermove', y - 70));
    window.dispatchEvent(pe('pointerup',   y - 70));
    return Math.round(document.getElementById('report').getBoundingClientRect().height);
  })()`);
  await sleep(650);
  const L3 = await ev(READ_L);
  const h3 = L3 && L3.panes && L3.panes.report ? L3.panes.report.h : null;
  check('① 拖分隔条能改高度', r3 > 40 && h3 > 40, `屏上=${r3}px 存=${h3}`);

  // 双击复位
  await ev(`(function(){
    const sp = document.querySelector('.split[data-split="bottom"]');
    sp.dispatchEvent(new MouseEvent('dblclick', { bubbles:true, cancelable:true }));
    return 1;
  })()`);
  await sleep(650);
  const L4 = await ev(READ_L);
  const h4 = L4 && L4.panes && L4.panes.report ? L4.panes.report.h : null;
  check('① 双击分隔条 = 高度复位（h 回 0 = 自适应）', h4 === 0, `存=${h4}`);

  // 再收起 → 落盘
  await ev(`document.getElementById('rep-head').click(); 1`);
  await sleep(650);
  const L5 = await ev(READ_L);
  check('① 再点一下收起，且状态落盘',
    !!L5 && L5.panes.report.collapsed === true && L5.panes.report.h === 0,
    JSON.stringify(L5 && L5.panes && L5.panes.report));

  // ─────────────────────────── ② 工具条药丸 + 浮层 ───────────────────────────
  const r6 = await ev(`(function(){
    const bar = document.getElementById('vbar-chart');
    const sum = document.getElementById('btn-av-sum');
    return { on: bar.classList.contains('on'),
             sw: bar.scrollWidth, cw: bar.clientWidth,
             sumTxt: (sum ? sum.textContent : '').trim(),
             inlineOld: !!document.getElementById('in-music_delay_ms')
                        && !bar.contains(document.getElementById('in-music_delay_ms')),
             hasBtnAuto: !!document.getElementById('btn-music-delay-auto') };
  })()`);
  check('② 工具条没横向溢出（scrollWidth ≤ clientWidth）', r6.sw <= r6.cw + 1, `sw=${r6.sw} cw=${r6.cw}`);
  check('② 工具条只剩一颗药丸且有摘要（音源 / 偏移）',
    /音源/.test(r6.sumTxt) && /偏移/.test(r6.sumTxt), r6.sumTxt);
  check('② 控件已从工具条挪走（不在条里，但在页面上）', r6.inlineOld === true);
  check('② 「按实测建议」按钮还在（id 没变）', r6.hasBtnAuto === true);

  const r7 = await ev(`(function(){
    document.getElementById('btn-av-sum').click();
    const pop = document.getElementById('av-pop');
    const rect = pop.getBoundingClientRect();
    return { shown: !pop.classList.contains('hidden'),
             nField: pop.querySelectorAll('.field').length,
             hasDelay: !!document.getElementById('in-music_delay_ms'),
             hasMode: !!document.getElementById('in-preview_audio_mode'),
             hasPath: !!document.getElementById('in-preview_audio_path'),
             hasDelta: !!document.getElementById('in-preview_audio_offset_ms'),
             inView: rect.top >= 0 && rect.bottom <= innerHeight + 1 && rect.left >= 0 && rect.right <= innerWidth + 1,
             w: Math.round(rect.width), h: Math.round(rect.height) };
  })()`);
  check('② 点药丸能开浮层',
    r7.shown && r7.inView, `${r7.w}x${r7.h}`);
  check('② 浮层里 4 个控件都在（偏移修正 / 音源档 / 原曲文件 / 原曲偏移 Δ）',
    r7.hasDelay && r7.hasMode && r7.hasPath && r7.hasDelta, `field=${r7.nField}`);

  const r8 = await ev(`(function(){
    document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles:true, clientX: 4, clientY: 4 }));
    return document.getElementById('av-pop').classList.contains('hidden');
  })()`);
  check('② 点浮层外面自动关', r8 === true);

  const r9 = await ev(`(function(){
    document.getElementById('btn-av-sum').click();
    const wasOpen = !document.getElementById('av-pop').classList.contains('hidden');
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles:true }));
    return { wasOpen, closed: document.getElementById('av-pop').classList.contains('hidden') };
  })()`);
  check('② Esc 也能关', r9.wasOpen && r9.closed, JSON.stringify(r9));

  // ─────────────────────────── ③ 三栏宽度 ───────────────────────────
  const r10 = await ev(`(function(){
    return { left: Math.round(document.getElementById('left').getBoundingClientRect().width),
             right: Math.round(document.getElementById('right').getBoundingClientRect().width) };
  })()`);
  check('③ 左栏 312 / 右栏 380（对齐我方工作台）',
    Math.abs(r10.left - 312) <= 2 && Math.abs(r10.right - 380) <= 2, JSON.stringify(r10));

  // ─────────────────── ④ 他的原功能一个都没丢 ───────────────────
  const KEEP = {
    '移动模式按钮': '#btn-layout', '恢复默认布局': '#btn-reset-layout',
    '顶栏预设下拉': '#preset', '顶栏密度下拉': '#density',
    '右栏预设条': '#presets', '参数搜索框': '#psearch',
    '采bpm HUD（舞台）': '#xk-hud', '大直线浮窗': '#fl-xk',
    '上停靠槽': '#dock-top', '下停靠槽': '#dock-bottom',
    '左停靠槽': '#dock-left', '右停靠槽': '#dock-right',
    'BDG 桥面板': '#bridge', '报告明细区': '#detail',
    '报告 chips': '#chips', '时序/警告折叠': '#more', '时间轴播放条': '#playbar',
    '段带': '#tlwrap', '全曲概览': '#ovwrap',
    '分隔条 left': '.split[data-split="left"]',
    '分隔条 right': '.split[data-split="right"]',
    '分隔条 bottom': '.split[data-split="bottom"]',
    '分隔条 tl': '.split[data-split="tl"]',
  };
  const missing = await ev(`(function(){
    const K = ${JSON.stringify(KEEP)};
    return Object.keys(K).filter((k) => !document.querySelector(K[k]));
  })()`);
  check(`④ 他的原有 UI 元素全在（${Object.keys(KEEP).length} 项）`,
    missing.length === 0, missing.length ? '缺：' + missing.join(' / ') : '全在');

  const r11 = await ev(`(function(){
    document.dispatchEvent(new KeyboardEvent('keydown', { key:'M', shiftKey:true, bubbles:true }));
    const on = document.body.classList.contains('layoutmode');
    const banner = !!(document.getElementById('lmbanner') || {}).classList
                   && document.getElementById('lmbanner').classList.contains('on');
    const popHidden = getComputedStyle(document.getElementById('av-pop')).display === 'none';
    document.dispatchEvent(new KeyboardEvent('keydown', { key:'Escape', bubbles:true }));
    return { on, banner, popHidden, off: !document.body.classList.contains('layoutmode') };
  })()`);
  check('④ Shift+M 移动模式照旧能用（横幅出现）', r11.on === true && r11.banner === true, JSON.stringify(r11));
  check('④ 移动模式下浮层自动隐藏（内容全冻结，不留能点的东西）', r11.popHidden === true);
  check('④ Esc 能退出移动模式', r11.off === true);

  const r12 = await ev(`(function(){
    const d = window.__dsh || {};
    return { has: typeof d.toggleReport === 'function' && typeof d.avSummary === 'function'
                  && typeof d.reportCollapsed === 'function',
             collapsed: typeof d.reportCollapsed === 'function' ? d.reportCollapsed() : null,
             sum: (d.avSummary ? d.avSummary() : '') };
  })()`);
  check('④ 给 e2e 的钩子接上了（reportCollapsed / toggleReport / avSummary）',
    r12.has === true && r12.collapsed === true, JSON.stringify(r12));

  await shot('1-default-collapsed');
  await ev(`(function(){
    const rep = document.getElementById('report');
    if (rep.classList.contains('collapsed')) document.getElementById('rep-head').click();
    return 1;
  })()`);
  await sleep(600);
  await shot('2-report-expanded');
  await ev(`document.getElementById('btn-av-sum').click(); 1`);
  await sleep(500);
  await shot('3-av-popover');

  // ─────────────── ⑤ 三档窗口宽：不横向溢出、药丸完整可见 ───────────────
  // ★ 1100 = 他 main.js 里 `minWidth: 1100`，是这台程序**声明支持的最小宽度**。
  //   低于它的越界（顶栏按钮探出）不算缺陷 —— 窗口本身拉不到那么窄。
  //   以前测 980/820 得到的 3 条「失败」就是这么来的假失败。
  for (const w of [1440, 1280, 1100]) {
    await send('Emulation.setDeviceMetricsOverride', { width: w, height: H, deviceScaleFactor: 1, mobile: false });
    await sleep(500);
    const rn = await ev(`(function(){
      const bar = document.getElementById('vbar-chart');
      const sum = document.getElementById('btn-av-sum');
      const sumR = sum.getBoundingClientRect();
      const pop = document.getElementById('av-pop');
      const pr = pop.getBoundingClientRect();
      const h = bar.querySelector('.hint');
      const hb = h.getBoundingClientRect();
      const bs = getComputedStyle(bar);
      // 到底谁越过了视口右沿？逐个点名（比只看 scrollWidth 有用得多）
      const over = [];
      document.querySelectorAll('body *').forEach((el) => {
        const r = el.getBoundingClientRect();
        if (r.width > 0 && r.right > innerWidth + 1) {
          over.push(el.tagName.toLowerCase() + (el.id ? '#' + el.id : '')
            + (typeof el.className === 'string' && el.className ? '.' + el.className.trim().split(/\\s+/).join('.') : '')
            + '@' + Math.round(r.left) + '..' + Math.round(r.right));
        }
      });
      return { sw: bar.scrollWidth, cw: bar.clientWidth,
               barW: Math.round(bar.getBoundingClientRect().width), barOff: bar.offsetWidth,
               barOv: bs.overflow, barWrap: bs.flexWrap,
               kids: [...bar.children].map((c) => c.tagName.toLowerCase() + ':'
                     + Math.round(c.getBoundingClientRect().width) + '/sw' + c.scrollWidth).join(' '),
               hintBox: [Math.round(hb.left), Math.round(hb.right), Math.round(hb.width)].join(','),
               hintSW: h.scrollWidth, hintOv: getComputedStyle(h).overflow,
               sumIn: sumR.left >= 0 && sumR.right <= innerWidth + 1,
               // ★ 提示文字与药丸是不是同一行？（他的 .vbar 是 flex-wrap:wrap，
               //   窄窗口会把两者拆成两行 —— 只报告不断言，观感问题得人眼定）
               sameLine: Math.abs(hb.top - sumR.top) < 3,
               barH: Math.round(bar.getBoundingClientRect().height),
               popHidden: pop.classList.contains('hidden'),
               pr: [Math.round(pr.left), Math.round(pr.right), Math.round(pr.top), Math.round(pr.width)].join(','),
               popIn: pr.left >= 0 && pr.right <= innerWidth + 1 && pr.top >= 0,
               over: over.slice(0, 10),
               // ★ 顶栏体检：皮肤给它写死了 height:46px，他原版是内容撑高。
               //   如果他的子元素（品牌/版本/预设下拉/按钮）比 46px 高 ⇒ 被裁掉，
               //   那就是又一处「越界改布局」，必须撤。
               //   注意：这段整个是外层模板字符串的内容 ⇒ 内部一律不许再用反引号。
               topH: (() => {
                 const t = document.getElementById('top');
                 const tr = t.getBoundingClientRect();
                 const ks = [...t.children].map((c) => c.getBoundingClientRect());
                 const tallest = Math.max(0, ...ks.map((r) => r.bottom - tr.top));
                 const widest = Math.max(0, ...ks.map((r) => r.right - tr.left));
                 return 'h=' + Math.round(tr.height) + ' 最高子元素=' + Math.round(tallest)
                   + ' 最右子元素=' + Math.round(widest) + '/内宽' + t.clientWidth
                   + ' 溢出=' + (tallest > tr.height + 1 || widest > t.clientWidth + 1);
               })(),
               docSW: document.documentElement.scrollWidth, bodySW: document.body.scrollWidth,
               iw: innerWidth,
               docOver: document.documentElement.scrollWidth <= innerWidth + 1 };
    })()`);
    check(`⑤ ${w}px：工具条不横向溢出`, rn.sw <= rn.cw + 1,
      `sw=${rn.sw} cw=${rn.cw} barW=${rn.barW}/off${rn.barOff} ov=${rn.barOv} wrap=${rn.barWrap} | ${rn.kids} | hint=${rn.hintBox} sw${rn.hintSW} ov=${rn.hintOv}`);
    check(`⑤ ${w}px：提示与药丸同一行（观感，仅报告）`, rn.sameLine === true,
      `sameLine=${rn.sameLine} 工具条高=${rn.barH}px hint顶=${rn.hintBox}`);
    check(`⑤ ${w}px：药丸完整可见`, rn.sumIn === true);
    check(`⑤ ${w}px：浮层没跑出窗口`, rn.popIn === true,
      `pop[L,R,T,W]=${rn.pr} hidden=${rn.popHidden}`);
    check(`⑤ ${w}px：整页没出横向滚动条`, rn.docOver === true,
      `docSW=${rn.docSW} bodySW=${rn.bodySW} innerWidth=${rn.iw} 越界=${rn.over.join(' | ') || '无'}`);
    check(`⑤ ${w}px：顶栏没被皮肤写死的高度裁掉`, !/溢出=true/.test(rn.topH), rn.topH);
    // ★ 裁一张工具条特写：折行这类事情数字说不清，得看图
    if (w === 1100) {
      const bb = await ev(`(function(){
        const r = document.getElementById('vbar-chart').getBoundingClientRect();
        return { x: 0, y: Math.max(0, Math.round(r.top) - 3),
                 width: innerWidth, height: Math.round(r.height) + 6 };
      })()`);
      await shot('5-vbar-1100', bb);
    }
  }
  await shot('4-narrow-1100');
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });

  const badErr = events.filter((s) => !/favicon/i.test(s));
  check('⑥ 全页无 JS 异常', badErr.length === 0, badErr.join(' | ') || '(无)');

  console.log(`\n===== ${PASS} 通过 / ${FAIL} 失败 =====`);
  try { ws.close(); } catch (_e) { /* ignore */ }
  try { child.kill(); } catch (_e) { /* ignore */ }
  process.exit(FAIL ? 1 : 0);
})().catch((e) => { console.error('ERROR', e); process.exit(2); });
