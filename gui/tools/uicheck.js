/* uicheck.js —— 用 CDP 驱动 headless Chromium 跑一遍工作台页面的交互自检
 * ============================================================================
 * 为什么不用 run_selftest.py：它会 _free_port 杀掉 8765/8766 上正在跑的进程 ——
 * 也就是主人此刻开着的那个窗口。这个脚本只连网关（页面已由主人的壳伺服），
 * 开一个独立的 headless 浏览器访问同一个 URL，全程不碰任何端口占用者。
 *
 * 用法：
 *   node tools/uicheck.js [url] [chrome.exe]
 *   默认 url = http://127.0.0.1:8766/workbench/index.html
 *   默认 chrome = 从 PATH 取 chrome/chromium（可传第 2 参数为自定义 chrome 路径）
 * 输出：逐条 PASS/FAIL + 页面里的 JS 异常（Runtime.exceptionThrown / Log error）。
 * ============================================================================
 */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const URL_ = process.argv[2] || 'http://127.0.0.1:8766/workbench/index.html';
const CHROME = process.argv[3] || process.env.UICHECK_CHROME || 'chrome';
const PORT = 9333;
const UDD = process.env.UICHECK_UDD || '<REPO>\\output\\.tmp\\uicheck-profile';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = (p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: PORT, path: p }, (r) => {
    let b = ''; r.on('data', (c) => (b += c)); r.on('end', () => res(b));
  }).on('error', rej);
});

const results = [];
function check(name, ok, extra) {
  results.push({ name, ok: !!ok, extra: extra === undefined ? '' : String(extra) });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra !== undefined ? '   ' + extra : ''}`);
}

(async () => {
  fs.mkdirSync(UDD, { recursive: true });
  const child = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--mute-audio', `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${UDD}`, '--window-size=1560,940', 'about:blank',
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
  const events = [];
  let seq = 0;
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
      events.push('LOG ' + m.params.entry.text + ' @ ' + (m.params.entry.url || ''));
    }
  });
  await new Promise((res, rej) => {
    ws.addEventListener('open', res); ws.addEventListener('error', rej);
  });
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
  await send('Log.enable');
  await send('Page.enable');
  await send('Page.navigate', { url: URL_ });

  // 等界面建好（buildPanels 跑过 = app.js 没在早期炸）
  let built = false;
  for (let i = 0; i < 60; i++) {
    try { built = await ev("!!document.querySelector('#groups section.grp')"); } catch (_e) { built = false; }
    if (built) break;
    await sleep(250);
  }
  check('页面建好（#groups 有 section.grp = app.js 跑到底了）', built);

  // ★ 先清干净这台浏览器的 localStorage 再重载：应用会**记住**报告带的收起态 / 高度，
  //   不清就会带着上一轮的结果开跑（上一轮点过"展开"，这轮的"默认收起"断言就假失败）。
  await ev('try{localStorage.clear()}catch(e){}');
  await send('Page.reload', { ignoreCache: true });
  built = false;
  for (let i = 0; i < 60; i++) {
    try { built = await ev("!!document.querySelector('#groups section.grp')"); } catch (_e) { built = false; }
    if (built) break;
    await sleep(250);
  }
  check('清空 localStorage 后重载，界面仍建好', built);

  // ── 左栏 ① 文件：**不再放加载入口**（2026-09-21 主人裁定，schedule.txt 第 1 条）──
  //   原先那两件（按钮「打开 MIDI / BDG 工程 / 时间戳…」+ 下拉「示例▾（内置曲子…）」）
  //   已删。加载改由「导入页侧边栏 → 工作台 → 选一条历史 MIDI → 顶部前往工作台」
  //   带入（生成完也会自动带入）；工作台自己只显示"当前载入了什么"。
  //   手动兜底仍留：宿主菜单 open / Ctrl+O（doOpen 保留）。
  const r0 = await ev(`(function(){
    const left = document.getElementById('panel-left');
    const txt = left ? (left.textContent || '') : '';
    const btns = [...document.querySelectorAll('button')].map((b) => b.textContent || '');
    const sels = [...document.querySelectorAll('select')].map((s) => s.textContent || '');
    return {
      btnInLeft: btns.some((t) => /打开\s*MIDI/.test(t)),
      selInLeft: sels.some((t) => /内置曲子/.test(t)),
      txtInLeft: /打开\\s*MIDI|内置曲子/.test(txt),
      hasFileLabel: !!document.getElementById('lbl-file'),
      leftSecs: left ? left.querySelectorAll('section.grp').length : 0
    };
  })()`);
  check('★ 左栏不再有「打开 MIDI / BDG 工程 / 时间戳…」按钮',
        !r0.btnInLeft && !r0.txtInLeft, JSON.stringify(r0));
  check('★ 左栏不再有「示例▾（内置曲子…）」下拉', !r0.selInLeft);
  check('★ ① 文件 组仍在（只显示"载入了什么"：#lbl-file）+ 左栏仍是 2 组',
        r0.hasFileLabel && r0.leftSecs === 2, `secs=${r0.leftSecs}`);
  // ★ 别去 body 里找「Ctrl+O」文案：帮助面板是**点了才建**的，常驻 DOM 里没有
  //   （第一版就是这么写的，前提不成立 → 假失败）。直接验真兜底路径：
  //   合成一次 Ctrl+O，看它是否走到 doOpen → 桥的 openFile。
  const r0b = await ev(`(function(){
    const d = window.dsh;
    if (!d || typeof d.openFile !== 'function') return { hasDsh: false };
    const orig = d.openFile;
    let called = 0;
    d.openFile = function () { called++; return Promise.resolve(null); };   // 返回 null = 用户取消
    const e = new KeyboardEvent('keydown', { key: 'o', ctrlKey: true, bubbles: true, cancelable: true });
    document.dispatchEvent(e);
    const prevented = e.defaultPrevented;
    setTimeout(function () { d.openFile = orig; }, 30);   // 用完还原，别影响后续断言
    return { hasDsh: true, called: called, prevented: prevented };
  })()`);
  check('★ 手动兜底入口仍在（Ctrl+O 真的调到 doOpen → 桥的 openFile）',
        r0b.hasDsh && r0b.called === 1 && r0b.prevented === true, JSON.stringify(r0b));

  // ── 问题 1：报告带 收起/展开 + 调高 ──────────────────────────────────────
  const r1 = await ev(`(function(){
    const rp = document.getElementById('report');
    const sp = document.getElementById('split-report');
    const chips = document.getElementById('chips');
    const cs = getComputedStyle(rp);
    return {
      collapsed: rp.classList.contains('collapsed'),
      chipsHidden: getComputedStyle(chips).display === 'none',
      splitterHidden: sp.classList.contains('hidden'),
      // 判据读 data-open：2026-09-21 起折叠箭头改成 CSS 旋转的 chevron（Win11 做法），
      // 页面上再也没有 ▸/▾ 字符 —— 继续读字形会永远拿到空串，那条断言就变成瞎过。
      caret: (document.getElementById('rep-caret') || { dataset: {} }).dataset.open,
      h: cs.height,
      hasBrief: !!document.getElementById('rep-brief'),
      splitCursor: getComputedStyle(sp).cursor,
      tip: (document.getElementById('rep-tip') || {}).textContent
    };
  })()`);
  check('① 报告带默认收起', r1.collapsed, JSON.stringify(r1));
  check('① 收起时 chips 不显示、分隔条隐藏、箭头处于收起态（data-open=false）',
        r1.chipsHidden && r1.splitterHidden && r1.caret === 'false');
  check('① 有收起摘要行 #rep-brief', r1.hasBrief);
  check('① 分隔条光标是 row-resize（能拖的样子）', r1.splitCursor === 'row-resize', r1.splitCursor);

  const r2 = await ev(`(function(){
    document.getElementById('rep-head').click();
    const rp = document.getElementById('report');
    return {
      collapsed: rp.classList.contains('collapsed'),
      chipsShown: getComputedStyle(document.getElementById('chips')).display !== 'none',
      splitterShown: !document.getElementById('split-report').classList.contains('hidden'),
      caret: document.getElementById('rep-caret').dataset.open,
      h: getComputedStyle(rp).height,
      tip: document.getElementById('rep-tip').textContent
    };
  })()`);
  check('① 点标题栏能展开（chips + 分隔条都出来，箭头转到展开态）',
        !r2.collapsed && r2.chipsShown && r2.splitterShown && r2.caret === 'true', JSON.stringify(r2));
  check('① 展开高度 = --report-h 默认 152px', r2.h === '152px', r2.h);

  // 拖动分隔条（合成 pointer 事件）：往上拖 60px ⇒ 高度 +60
  const r3 = await ev(`(function(){
    const sp = document.getElementById('split-report');
    const y0 = sp.getBoundingClientRect().top + 2;
    const mk = (t, y) => new PointerEvent(t, { bubbles: true, cancelable: true, clientX: 400, clientY: y, button: 0, pointerId: 1 });
    sp.dispatchEvent(mk('pointerdown', y0));
    window.dispatchEvent(mk('pointermove', y0 - 60));
    window.dispatchEvent(mk('pointerup', y0 - 60));
    return { h: getComputedStyle(document.getElementById('report')).height,
             varv: getComputedStyle(document.documentElement).getPropertyValue('--report-h').trim(),
             stored: localStorage.getItem('adoc.reportH') };
  })()`);
  check('① 拖分隔条能改高度（上拖 60 → 212px）', r3.h === '212px', JSON.stringify(r3));
  check('① 高度写进 localStorage（下次进来还记得）', r3.stored === '212', r3.stored);

  // 双击复位
  const r4 = await ev(`(function(){
    const sp = document.getElementById('split-report');
    sp.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
    return getComputedStyle(document.getElementById('report')).height;
  })()`);
  check('① 双击分隔条复位回 152px', r4 === '152px', r4);

  // ── 问题 3：谱面预览工具条 + 浮层能开能关 ──────────────────────────────────
  // ★ 2026-09-21 主人裁定改了行为，断言跟着改（「改设计必须同步改断言」）：
  //   音源**就绪** ⇒ 那颗「⚙ 音源 … 」药丸**连它那条空工具条一起收掉**；
  //   **没就绪**（指定文件却没挑 / 文件不在了）⇒ 亮成警告样式，点开就能改。
  //   所以「横向溢出 / 药丸完整可见」那两条老断言必须在**药丸亮着**的状态下验 ——
  //   那才是当初报「显示不全」的场景；就绪时那条工具条高 0，验了也是空过。
  const r5 = await ev(`(function(){
    const bar = document.getElementById('vbar-chart');
    const sum = document.getElementById('btn-av-sum');
    return { on: bar.classList.contains('on'),
             empty: bar.classList.contains('empty'),
             barH: Math.round(bar.getBoundingClientRect().height),
             hasSum: !!sum,
             hidden: sum ? sum.classList.contains('hidden') : null,
             sumTxt: sum ? sum.textContent : '',
             oldInline: !!document.getElementById('in-music_delay_ms') && !bar.contains(document.getElementById('in-music_delay_ms')) };
  })()`);
  check('③ 默认（自动档）⇒ 音源就绪 ⇒ 药丸收着 + 那条空工具条也收掉（高 0）',
        r5.hasSum && r5.hidden === true && r5.empty === true && r5.barH === 0, JSON.stringify(r5));
  check('③ 控件已从工具条挪进浮层（不在条里）', r5.oldInline);

  const r6 = await ev(`(function(){
    // 药丸收着时的入口 = Ctrl+Shift+A（键盘处理里调的就是这个）
    window.__dsh.avPop();
    document.getElementById('av-pop');
    const pop = document.getElementById('av-pop');
    const rect = pop.getBoundingClientRect();
    return { shown: !pop.classList.contains('hidden'), nFields: pop.querySelectorAll('.field').length,
             hasDelay: !!document.getElementById('in-music_delay_ms'),
             hasMode: !!document.getElementById('in-preview_audio_mode'),
             hasPath: !!document.getElementById('in-preview_audio_path'),
             hasDelta: !!document.getElementById('in-preview_audio_offset_ms'),
             inView: rect.top >= 0 && rect.bottom <= innerHeight + 1 && rect.left >= 0,
             w: Math.round(rect.width), h: Math.round(rect.height) };
  })()`);
  check('③ ★ 药丸收着时浮层仍能开（入口没丢），4 个控件都在里面',
        r6.shown && r6.nFields === 4 && r6.hasDelay && r6.hasMode && r6.hasPath && r6.hasDelta, JSON.stringify(r6));
  check('③ 浮层没跑出窗口（不会被裁掉）', r6.inView, `${r6.w}x${r6.h}`);
  await ev("document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))");

  // ── 未就绪（主人截图里那个状态：指定文件却没挑）⇒ 药丸必须亮 ────────────────
  const r6b = await ev(`(function(){
    window.__dsh.avRaw({ mode: 2, path: '' });
    const bar = document.getElementById('vbar-chart');
    const sum = document.getElementById('btn-av-sum');
    const r = sum.getBoundingClientRect();
    return { empty: bar.classList.contains('empty'),
             barH: Math.round(bar.getBoundingClientRect().height),
             hidden: sum.classList.contains('hidden'), warn: sum.classList.contains('warn'),
             sw: bar.scrollWidth, cw: bar.clientWidth,
             sumIn: r.left >= 0 && r.right <= innerWidth + 1,
             pill: Math.round(r.width) + 'x' + Math.round(r.height), txt: sum.textContent };
  })()`);
  check('③ ★ 未就绪 ⇒ 药丸亮成警告样式，那条工具条回来',
        r6b.hidden === false && r6b.warn === true && r6b.empty === false && r6b.barH > 0,
        JSON.stringify({ hidden: r6b.hidden, warn: r6b.warn, barH: r6b.barH, txt: r6b.txt }));
  check('③ 未就绪时工具条没被顶出横向滚动条（scrollWidth ≤ clientWidth）',
        r6b.sw <= r6b.cw + 1, `sw=${r6b.sw} cw=${r6b.cw}`);
  check('③ 未就绪时药丸完整可见（整个在窗口里）', r6b.sumIn, r6b.pill);

  const r7 = await ev(`(function(){
    const inp = document.getElementById('in-music_delay_ms');
    inp.value = '-40'; inp.dispatchEvent(new Event('change', { bubbles: true }));
    const t = document.getElementById('btn-av-sum').textContent;
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    return { sum: t, closed: document.getElementById('av-pop').classList.contains('hidden') };
  })()`);
  check('③ 改偏移值 → 摘要同步更新', /-40\s*ms/.test(r7.sum), r7.sum);
  check('③ Esc 能关浮层', r7.closed);

  const r8 = await ev(`(function(){
    document.getElementById('btn-av-sum').click();
    const open1 = !document.getElementById('av-pop').classList.contains('hidden');
    document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: 5, clientY: 5 }));
    const closed = document.getElementById('av-pop').classList.contains('hidden');
    return { open1, closed };
  })()`);
  check('③ 点摘要也能开；点外面自动关', r8.open1 && r8.closed, JSON.stringify(r8));

  // ── 窄窗口再验一次（主人就是窄窗口下撞上"显示不全"的；保持"药丸亮着"状态才有效）──
  for (const w of [1180, 980, 820]) {
    await send('Emulation.setDeviceMetricsOverride',
               { width: w, height: 780, deviceScaleFactor: 1, mobile: false });
    await sleep(350);
    const rn = await ev(`(function(){
      const bar = document.getElementById('vbar-chart');
      const sum = document.getElementById('btn-av-sum');
      if (!sum) return { sw: bar.scrollWidth, cw: bar.clientWidth, sumIn: false, pill: null };
      const r = sum.getBoundingClientRect();
      return { sw: bar.scrollWidth, cw: bar.clientWidth,
               sumIn: r.left >= 0 && r.right <= innerWidth + 1,
               pill: Math.round(r.width) + 'x' + Math.round(r.height) };
    })()`);
    check(`③ ${w}px 窄窗口下（未就绪、药丸亮着）工具条仍不溢出、药丸完整可见`,
          rn.sw <= rn.cw + 1 && rn.sumIn, JSON.stringify(rn));
  }
  await ev("window.__dsh.avRaw({ mode: 0, path: '' })");   // 还原成默认档，别把状态留给后面的用例
  await send('Emulation.clearDeviceMetricsOverride');

  // ── 问题 2：边缘缩放的页面自报（光标判定 + 滚动条让位） ────────────────────
  const r9 = await ev(`(function(){
    const mv = (x, y) => document.dispatchEvent(new MouseEvent('mousemove', { bubbles: true, clientX: x, clientY: y }));
    const cur = () => document.documentElement.style.cursor;
    const rep = document.getElementById('report').getBoundingClientRect();
    const repY = Math.round(rep.top + rep.height / 2);      // 报告带那一行没有滚动条
    mv(innerWidth - 3, repY);         const right2 = cur();  // 右缘（无滚动条处）应能拖
    mv(3, repY);                      const left = cur();
    mv(3, 3);                         const tl = cur();
    mv(innerWidth - 3, innerHeight - 3); const br = cur();
    mv(600, 300);                     const mid = cur();
    // 右缘压在竖直滚动条上时**故意让位**（工作台右栏滚动条就在窗口最右，抢掉就没法滚）
    const sc = document.querySelector('#right .zbody') || document.querySelector('.zbody');
    let overSb = 'n/a';
    if (sc) { const r = sc.getBoundingClientRect();
      mv(r.left + sc.clientLeft + sc.clientWidth + 2, 300); overSb = cur(); }
    return { right2, left, tl, br, mid, overSb, w: innerWidth, h: innerHeight };
  })()`);
  check('② 右缘（无滚动条处）/左/左上/右下光标正确',
        r9.right2 === 'ew-resize' && r9.left === 'ew-resize'
        && r9.tl === 'nwse-resize' && r9.br === 'nwse-resize', JSON.stringify(r9));
  check('② 页面中部不抢光标', r9.mid === '', r9.mid || '(空)');
  check('② 右缘落在滚动条上时让位（滚动条照常能拖）', r9.overSb === '' || r9.overSb === 'n/a', r9.overSb);

  // 边缘按下：桥会 preventDefault + stopPropagation（真发了 type:resize 给宿主，
  // 由窗口顶部拖动同款机制完成缩放）；这里用 defaultPrevented 观测"确实拦下了"。
  const r10 = await ev(`(function(){
    const mk = (x, y) => new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0, clientX: x, clientY: y });
    const rep = document.getElementById('report').getBoundingClientRect();
    const repY = Math.round(rep.top + rep.height / 2);
    const eEdge = mk(innerWidth - 3, repY);
    document.dispatchEvent(eEdge);
    const eMid = mk(600, 300);
    document.dispatchEvent(eMid);
    return { edgePrevented: eEdge.defaultPrevented, midPrevented: eMid.defaultPrevented };
  })()`);
  check('② 边缘按下被桥拦下（= 已通知宿主缩放）', r10.edgePrevented === true, JSON.stringify(r10));
  check('② 页面中部按下不被拦（不影响正常操作）', r10.midPrevented === false);

  // ── ② 补测：8 条边**全都**要发对 edge 名（宿主 EDGE_HT 认这 8 个 key） ──────
  //   桥在加载时就固化 `window.chrome.webview`，所以必须**在页面脚本之前**注入替身
  //   （CDP addScriptToEvaluateOnNewDocument），运行时补 window.chrome 是抓不到的。
  const stubScript = await send('Page.addScriptToEvaluateOnNewDocument', { source: `
    (function(){
      window.__posted = [];
      var ls = [];
      var stub = {
        postMessage: function (s) { try { window.__posted.push(String(s)); } catch (e) {} },
        addEventListener: function (t, f) { if (/^message/i.test(t)) { ls.push(f); } },
        removeEventListener: function (t, f) { ls = ls.filter(function (x) { return x !== f; }); }
      };
      // 给测试用的"宿主 → 页面"投递口（真实宿主用 PostWebMessageAsString）
      window.__deliver = function (obj) {
        ls.forEach(function (f) { try { f({ data: obj }); } catch (e) {} });
      };
      try {
        window.chrome = window.chrome || {};
        window.chrome.webview = stub;
      } catch (e) { /* 只读就放弃 */ }
      if (!(window.chrome && window.chrome.webview === stub)) {
        try { Object.defineProperty(window, 'chrome', { value: { webview: stub }, configurable: true }); }
        catch (e) { /* ignore */ }
      }
    })();
  ` });
  await send('Page.navigate', { url: URL_ });
  for (let i = 0; i < 60; i++) {
    if (await ev(`!!document.querySelector('#groups section.grp')`)) break;
    await sleep(400);
  }
  await sleep(700);
  const stubOk = await ev(`!!(window.chrome && window.chrome.webview && window.__posted)`);
  check('② 替身桥已注入（能抓到页面发给宿主的消息）', stubOk);

  // ── ★ 预览掉帧的诊断通路（schedule 第 6 条）─────────────────────────────
  //   引擎自带采样，每 2s 打一条 `[Perf] frame=…ms | renderPlayer=… updatePlayer=…`；
  //   app.js 里 hook 了 console.log，把它转成 page_log 落进 gui_debug.log。
  //   这里只验「转发这条路通不通」：真跑起来需要 GPU 与 >2s 播放，
  //   headless（--disable-gpu）量不出真帧率，那部分必须主人真机跑。
  const perfProbe = await ev(`(function(){
    const before = window.__posted.length;
    console.log('[Perf] frame=12.34ms | renderPlayer=9.00 updatePlayer=3.00 syncVideo=0.34');
    console.log('这一行不该被转发');
    const added = window.__posted.slice(before).map(function(s){
      try { return JSON.parse(s); } catch (e) { return { __raw: s }; }
    });
    return { added: added, n: added.length };
  })()`);
  const perfMsgs = (perfProbe.added || []).filter((m) => m.type === 'page_log');
  check('★ [Perf] 采样会转发给宿主（真机不开 DevTools 也能定位掉帧）',
        perfMsgs.length === 1 && /^\[Perf\]/.test(perfMsgs[0].step), JSON.stringify(perfProbe.added));
  check('★ 其它 console 输出不转发（不把日志刷爆）', perfProbe.n === 1, `转发 ${perfProbe.n} 条`);

  const r11 = await ev(`(function(){
    const rep = document.getElementById('report').getBoundingClientRect();
    const repY = Math.round(rep.top + rep.height / 2);
    const w = innerWidth, h = innerHeight;
    const pts = { left: [3, repY], right: [w - 3, repY], top: [600, 3], bottom: [600, h - 3],
                  topleft: [3, 3], topright: [w - 3, 3], bottomleft: [3, h - 3], bottomright: [w - 3, h - 3] };
    const out = {};
    for (const k in pts) {
      const [x, y] = pts[k];
      window.__posted.length = 0;
      document.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0, clientX: x, clientY: y }));
      const last = window.__posted[window.__posted.length - 1] || '';
      let got = '';
      try { got = (JSON.parse(last) || {}).edge || ''; } catch (e) { got = 'PARSE_FAIL:' + last; }
      out[k] = got;
    }
    return out;
  })()`);
  const want = ['left', 'right', 'top', 'bottom', 'topleft', 'topright', 'bottomleft', 'bottomright'];
  const badEdges = want.filter((k) => r11[k] !== k);
  check('② 8 条边（含 4 角）都自报正确的 edge 名', badEdges.length === 0,
        badEdges.length ? badEdges.map((k) => `${k}→${r11[k] || '(无)'}`).join(', ') : JSON.stringify(r11));

  const r12 = await ev(`(function(){
    // 最大化时页面应闭嘴（与 Windows 一致：最大化的窗口不给拖边）
    window.__posted.length = 0;
    document.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0, clientX: 3, clientY: 400 }));
    const beforeMax = window.__posted.length;
    // 走替身投递口告知"已最大化"（真实宿主是 PostWebMessageAsString）
    window.__deliver({ type: 'winstate', zoomed: true });
    window.__posted.length = 0;
    document.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0, clientX: 3, clientY: 400 }));
    const afterMax = window.__posted.length;
    window.__deliver({ type: 'winstate', zoomed: false });
    return { beforeMax, afterMax };
  })()`);
  check('② 最大化时不再抢边缘（beforeMax≥1 / afterMax=0）',
        r12.beforeMax >= 1 && r12.afterMax === 0, JSON.stringify(r12));

  // ③ 窗口控件那一条：右键该报 sysmenu（系统菜单由宿主弹），顶栏空白**不该**报
  //    —— 后者属于拖动区，系统（WebView2 非客户区支持）会自己弹它那份，
  //    页面再报一次就会同时出现两个菜单。这条防的就是"打双份"。
  const r13 = await ev(`(function(){
    function rc(el){
      var r = el.getBoundingClientRect();
      var e = new MouseEvent('contextmenu', { bubbles: true, cancelable: true, button: 2,
        clientX: Math.round(r.left + r.width / 2), clientY: Math.round(r.top + r.height / 2) });
      el.dispatchEvent(e);
      return e.defaultPrevented;
    }
    var out = {};
    var btn = document.getElementById('win-max') || document.getElementById('maxBtn');
    var bar = document.getElementById('top') || document.getElementById('titlebar');
    out.hasBtn = !!btn; out.hasBar = !!bar;
    if (btn) { window.__posted.length = 0; out.btnPrevented = rc(btn); out.btn = window.__posted.slice(); }
    if (bar) { window.__posted.length = 0; out.barPrevented = rc(bar); out.bar = window.__posted.slice(); }
    // 顺带验状态广播能不能把按钮字形改对。
    // ★ 2026-09-21 起窗口按钮也换成了 SVG 图标：判据从"字符"改成 <use> 指的图标名。
    //   继续读 textContent 只会拿到空串（图标里没有文本节点），那条断言就成了瞎过。
    var glyphs = {};
    if (btn) {
      var useEl = btn.querySelector('use');
      window.__deliver({ type: 'winstate', zoomed: true });
      glyphs.zoom = useEl ? (useEl.getAttribute('href') || '') : btn.textContent;
      window.__deliver({ type: 'winstate', zoomed: false });
      glyphs.norm = useEl ? (useEl.getAttribute('href') || '') : btn.textContent;
    }
    out.glyphs = glyphs;
    return out;
  })()`);
  check('③ 右键窗口控件 → 页面报 sysmenu（系统菜单交给宿主弹）',
        r13.hasBtn && (r13.btn || []).some((s) => s.indexOf('"sysmenu"') >= 0),
        JSON.stringify({ prevented: r13.btnPrevented, posted: r13.btn }));
  check('③ 右键顶栏空白 → 页面**不**报 sysmenu（避免和系统菜单弹两份）',
        r13.hasBar && !(r13.bar || []).some((s) => s.indexOf('"sysmenu"') >= 0),
        JSON.stringify({ prevented: r13.barPrevented, posted: r13.bar }));
  check('③ winstate 广播让按钮字形跟着变（还原 / 最大化两枚图标）',
        r13.glyphs && r13.glyphs.zoom === '#i-restore' && r13.glyphs.norm === '#i-square',
        JSON.stringify(r13.glyphs));

  // 撤掉替身注入（只影响后续导航；当前文档里已生效的替身留着，不影响收尾检查）
  try { await send('Page.removeScriptToEvaluateOnNewDocument', { identifier: stubScript.identifier }); } catch (_e) { /* ignore */ }

  // ★ Win11 观感层（2026-09-21）：判据一律读"**真的插进去了几支 svg**"，
  //   不是读 data-ico 属性 —— 属性在、图标没插上完全可能（脚本没加载 / 名字拼错都是这样），
  //   只看属性的话那几条会永远绿。
  const icoStat = JSON.parse(await ev(`JSON.stringify({
    sprite: !!document.getElementById('shell-icon-sprite'),
    topIcos: document.querySelectorAll('#top .ico').length,
    tabIcos: document.querySelectorAll('#tabs .tab .ico').length,
    zheadIcos: document.querySelectorAll('.zhead .ico').length,
    playIco: !!document.querySelector('#btn-play .ico'),
    caretIco: !!document.querySelector('#rep-caret .ico'),
    bad: Array.prototype.slice.call(document.querySelectorAll('[data-ico]'))
           .filter(function(e){ return e.dataset.icoDone === 'missing'; })
           .map(function(e){ return e.dataset.ico; })
  })`));
  check('图标 sprite 已注入（#shell-icon-sprite 在文档里）', icoStat.sprite === true);
  check('顶栏按钮换上了 Fluent 图标', icoStat.topIcos >= 3, `topIcos=${icoStat.topIcos}`);
  check('四个预览标签都带图标', icoStat.tabIcos === 4, `tabIcos=${icoStat.tabIcos}`);
  check('窗格标题（来源 / 检查器 / 报告）都带图标', icoStat.zheadIcos >= 3, `zheadIcos=${icoStat.zheadIcos}`);
  check('播放键是图标而不是 ▶ 字符', icoStat.playIco === true);
  check('报告带折叠箭头是 chevron 图标', icoStat.caretIco === true);
  check('没有拼错的图标名（拼错会留 data-ico-done=missing）',
        icoStat.bad.length === 0, icoStat.bad.join(',') || '(无)');

  const badErr = events.filter((s) => !/favicon/i.test(s));
  console.log('（页面日志：' + (events.join('  ／  ') || '无') + '）');
  check('全页无 JS 异常（favicon 404 不算）', badErr.length === 0, badErr.join(' | ') || '(无)');

  console.log('\n==== 汇总 ====');
  console.log(`${results.filter((r) => r.ok).length}/${results.length} 通过`);
  const bad = results.filter((r) => !r.ok);
  if (bad.length) console.log('失败项：\n' + bad.map((b) => ' - ' + b.name + '  ' + b.extra).join('\n'));

  try { await send('Browser.close'); } catch (_e) { /* ignore */ }
  try { child.kill(); } catch (_e) { /* ignore */ }
  process.exit(bad.length ? 1 : 0);
})().catch((e) => {
  console.error('ERR', e && e.stack || e);
  process.exit(2);
});
