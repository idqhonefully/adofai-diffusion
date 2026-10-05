/* hitmeterprobe.js —— 验「预览框判定条」已被去掉（引擎内置准度条 HitErrorMeter）。
 *
 * 背景：引擎 `createPlayer()` 会在预览容器 `#cv-adofai` 里挂一张 **zIndex:9998、
 * 覆盖整个画面** 的 canvas 画判定条（准度条），每次命中都会让它 visible=true
 * ⇒ 一播放就横在画面下方、遮挡视野。改造在预览建好后 dispose 它并把字段置 null
 * （`gui/studio-skin/app.js` 的 `killHitErrorMeter()`）。
 *
 * 本探针端到端跑真链路：载谱 → 建预览 → 查字段 → 查 DOM → 重建再查 → 顺带验
 * 「别删过头」（预览还能出格、还能播）。
 *
 *   · 零侵入：指到隔离实例（默认 8896），不碰主人正在用的 8766
 *   · 用法：node tools/hitmeterprobe.js [url] [sampleMid]
 * ========================================================================== */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const URL = process.argv[2] || 'http://127.0.0.1:8896/studio-skin/index.html';
const SAMPLE = process.argv[3] || '<REPO>\\chartgen\\samples\\Automaton_Waltz.mid';
const CHROME = process.env.HM_CHROME || 'chrome';
const OUT = process.env.HM_OUT || '<REPO>\\output\\logs\\hitmeterprobe';
const PORT = 9339;
const UDD = '<REPO>\\output\\.tmp\\hitmeterprobe-profile';
const W = 1560, H = 940;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let PASS = 0, FAIL = 0;
const check = (name, ok, detail) => {
  (ok ? PASS++ : FAIL++);
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '   [' + detail + ']' : ''}`);
};
const note = (s) => console.log('NOTE  ' + s);
const get = (p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: PORT, path: p }, (r) => {
    let b = ''; r.on('data', (c) => (b += c)); r.on('end', () => res(b));
  }).on('error', rej);
});
const esc = (s) => String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'");

/* 页面侧取证：判定条字段 + 舞台上有没有残留的判定条 canvas */
const PROBE = `(function(){
  const host = document.getElementById('cv-adofai');
  const canvases = host ? [...host.querySelectorAll('canvas')] : [];
  const bar = canvases
    .map((c) => ({ z: c.style.zIndex || '', disp: getComputedStyle(c).display,
                   box: [Math.round(c.getBoundingClientRect().width),
                         Math.round(c.getBoundingClientRect().height)].join('x') }))
    .filter((c) => c.z === '9998');
  const d = window.__dsh || {};
  return {
    meter: typeof d.hitErrorMeter === 'function' ? d.hitErrorMeter() : '(钩子缺失)',
    // 引擎的判定条画布特征：zIndex 9998 且铺满容器、pointer-events:none
    barTotal: bar.length,
    barVisible: bar.filter((c) => c.disp !== 'none').length,
    barBox: bar.map((c) => c.box).join(' '),
    canvasCount: canvases.length,
    st: typeof d.adofaiState === 'function' ? d.adofaiState() : null,
    canvasSize: host ? [host.clientWidth, host.clientHeight].join('x') : '(无容器)',
  };
})()`;

(async () => {
  fs.mkdirSync(UDD, { recursive: true });
  fs.mkdirSync(OUT, { recursive: true });
  const child = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--mute-audio', '--force-color-profile=srgb',
    '--autoplay-policy=no-user-gesture-required',
    '--remote-debugging-port=' + PORT, '--user-data-dir=' + UDD,
    '--window-size=' + W + ',' + H, 'about:blank',
  ], { stdio: 'ignore', windowsHide: true });

  let list = [];
  for (let i = 0; i < 80; i++) {
    try { list = JSON.parse(await get('/json/list')); if (list.some((t) => t.type === 'page')) break; }
    catch (_e) { /* 等浏览器 */ }
    await sleep(250);
  }
  const page = list.find((t) => t.type === 'page');
  if (!page) throw new Error('连不上 headless 浏览器');

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  const waiters = new Map();
  let seq = 0;
  const events = [];
  ws.addEventListener('message', (m) => {
    const d = JSON.parse(m.data);
    if (d.id && waiters.has(d.id)) { waiters.get(d.id)(d); waiters.delete(d.id); return; }
    if (d.method === 'Runtime.exceptionThrown') {
      const x = d.params.exceptionDetails || {};
      events.push('EXC ' + ((x.exception && x.exception.description) || x.text));
    }
    if (d.method === 'Log.entryAdded' && d.params.entry.level === 'error') {
      // favicon.ico 的 404 是 headless 固有噪音（页面没放图标），按 URL 过滤掉
      const u = (d.params.entry.url || '');
      if (!/favicon\.ico$/i.test(u)) events.push('LOG ' + d.params.entry.text + '  @' + u);
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
  const shot = async (name, clip, scale) => {
    const p = { format: 'png' };
    if (clip) p.clip = Object.assign({ scale: scale || 1 }, clip);
    const r = await send('Page.captureScreenshot', p);
    const f = path.join(OUT, name + '.png');
    fs.writeFileSync(f, Buffer.from(r.data, 'base64'));
    console.log('SAVED ' + f);
  };

  await send('Page.enable');
  await send('Log.enable');
  await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });

  console.log('='.repeat(78));
  console.log('### ' + URL);
  console.log('    样本：' + SAMPLE + '\n');

  await send('Page.navigate', { url: URL });
  await sleep(1500);
  await ev('try { localStorage.clear(); } catch (_e) {} 1');
  await send('Page.reload', { ignoreCache: true });

  let n = 0;
  for (let i = 0; i < 60; i++) {
    n = await ev("document.querySelectorAll('section.grp').length");
    if (n > 0) break;
    await sleep(500);
  }
  await sleep(1200);
  console.log('schema 分组数 = ' + n);

  // ---------- A. 载入谱面 ----------
  const loaded = await ev("(async()=>{ try{ await window.__dsh.load('" + esc(SAMPLE) + "'); return 'ok'; }catch(e){ return 'ERR '+(e&&e.message); } })()");
  check('A1 载入示例谱面（走界面同款 __dsh.load）', loaded === 'ok', String(loaded));
  let hasChart = false;
  for (let i = 0; i < 90; i++) {
    hasChart = await ev('!!(window.__dsh && window.__dsh.stats && window.__dsh.stats().hasChart)');
    if (hasChart) break;
    await sleep(1000);
  }
  check('A2 谱面出来了（hasChart）', hasChart === true, 'hasChart=' + hasChart);
  if (!hasChart) {
    console.log('\n★ 谱面没出来，后面没法验 —— 先看隔离实例（8896）与 sidecar（8895）是否在跑。');
    console.log('异常 ' + events.length + ' 条：'); events.slice(0, 8).forEach((e) => console.log('   ' + e));
    return;
  }

  // ---------- B. 等预览建起来（判定条就在这一步被掐掉） ----------
  let pr = null, built = false;
  for (let i = 0; i < 90; i++) {
    pr = await ev(PROBE);
    if (pr && pr.meter && pr.meter !== 'no-preview' && pr.meter !== '(钩子缺失)') { built = true; break; }
    await sleep(1000);
  }
  const errTxt = await ev("String((window.__dsh && window.__dsh.adofaiErr) ? window.__dsh.adofaiErr() : '')");
  check('B1 预览引擎建起来了（前提条件）', built === true,
    built ? JSON.stringify(pr) : ('未建起，adofaiErr=' + errTxt));
  if (!built) {
    note('headless 环境建不起 WebGL 预览 ⇒ 本条**未验证**（不是通过、也不是失败）。');
    note('真机复看：打开谱面预览、按播放，画面下方不应再出现那条「Perfect/Good/Bad/Miss」横条。');
    console.log('\n异常 ' + events.length + ' 条：'); events.slice(0, 8).forEach((e) => console.log('   ' + e));
    return;
  }

  console.log('\n--- 建起来后的实测 ---');
  console.log(JSON.stringify(pr));

  // ---------- C. 判定条真没了 ----------
  check('C1 hitErrorMeter 字段已置 null（引擎内部调用全变 no-op）', pr.meter === 'killed', 'meter=' + pr.meter);
  check('C2 预览容器里没有可见的判定条画布（zIndex 9998）', pr.barVisible === 0,
    'zIndex9998 共 ' + pr.barTotal + ' 张 / 可见 ' + pr.barVisible + ' 张  box=' + (pr.barBox || '(无)'));
  check('C3 预览容器本身有尺寸（不是整块被干掉了）', pr.canvasSize !== '0x0', 'canvasSize=' + pr.canvasSize);
  check('C4 预览出格了（别删过头：谱面还在）', !!(pr.st && pr.st.tiles > 0),
    JSON.stringify(pr.st));

  // ---------- D. 播放一下，判定条也不会冒出来 ----------
  await ev("(function(){ try { window.__dsh.adofai().startPlay(0); } catch(_e){} return 1; })()");
  await sleep(2500);
  const pr2 = await ev(PROBE);
  const playing = !!(pr2.st && pr2.st.playing);
  check('D1 预览能播（判定条删掉不影响播放）', playing === true, 'st=' + JSON.stringify(pr2.st));
  check('D2 播放中判定条依然不可见（addHit 没把它弄回来）', pr2.barVisible === 0 && pr2.meter === 'killed',
    'meter=' + pr2.meter + ' barVisible=' + pr2.barVisible);
  await shot('2-playing');
  await ev("(function(){ try { window.__dsh.adofai().stop(); } catch(_e){} return 1; })()");
  await sleep(400);

  // ---------- E. DOM 兜底分支（显式调用才走得到） ----------
  //  引擎重建预览时会 `container.innerHTML = ''` 把容器清空 ⇒ 兜底那段分支
  //  平时**永远走不到**。这里手搓一张「长得像判定条」的 canvas（zIndex 9998）
  //  塞进去，然后用 `__dsh.killMeter()` 显式调一次：它必须被藏掉。
  //  没有这条，兜底就是"写了但没人走过"的死代码。
  await ev(`(function(){
    const host = document.getElementById('cv-adofai');
    const c = document.createElement('canvas');
    c.id = '__fake_hitbar';
    c.style.position = 'absolute'; c.style.zIndex = '9998';
    c.style.width = '60px'; c.style.height = '12px';
    host.appendChild(c);
    return 1;
  })()`);
  const fakeBefore = await ev("(function(){ const c=document.getElementById('__fake_hitbar'); return c ? getComputedStyle(c).display : '(没塞进去)'; })()");
  check('E1 手搓的「假判定条」塞进去了（准备验兜底）', fakeBefore !== '(没塞进去)' && fakeBefore !== 'none', 'display=' + fakeBefore);

  await ev("(function(){ try { window.__dsh.killMeter(); } catch(_e){} return 1; })()");
  const fakeAfter = await ev("(function(){ const c=document.getElementById('__fake_hitbar'); if(!c) return '(被删了)'; return getComputedStyle(c).display; })()");
  check('E2 DOM 兜底生效：残留的 zIndex9998 画布被藏掉', fakeAfter === 'none', 'display=' + fakeAfter);
  await ev("(function(){ const c=document.getElementById('__fake_hitbar'); if(c) c.remove(); return 1; })()");

  // ---------- F. 重建一次：每次重建都掐 ----------
  await ev("(async()=>{ try{ await window.__dsh.reloadPreview(); }catch(_e){} return 1; })()");
  let pr3 = null, rebuilt = false;
  for (let i = 0; i < 90; i++) {
    pr3 = await ev(PROBE);
    if (pr3 && pr3.meter === 'killed' && pr3.st && pr3.st.tiles > 0) { rebuilt = true; break; }
    await sleep(1000);
  }
  check('F1 重建预览后仍是 killed（每次重建都掐，不只是第一次）', rebuilt === true,
    'meter=' + (pr3 && pr3.meter) + ' tiles=' + (pr3 && pr3.st && pr3.st.tiles));
  const pr4 = await ev(PROBE);
  check('F2 重建后也没有可见的判定条画布、别的画布还在（没误伤）',
    pr4.barVisible === 0 && pr4.canvasCount >= 1,
    'barVisible=' + pr4.barVisible + ' canvasCount=' + pr4.canvasCount);
  await shot('3-after-reload');

  // ---------- G. 对照组：证明「引擎默认真的会挂那张条」 ----------
  //  不经过我们的 ensurePreview，直接调 vendor 的 createPreview 到**一个临时容器**里：
  //  那个句柄的 hitErrorMeter 应当是 `alive`。这才是"删掉了一条真实存在的东西"的证据，
  //  否则 C1/C2 只能证明"现在没有"，证明不了"本来是引擎挂上去的"。
  const ctl = await ev(`(async () => {
    try {
      const d = window.__dsh;
      const lj = await d.api.levelJson(d.state);
      if (!lj || !lj.ok) return { err: 'levelJson: ' + ((lj && lj.error) || '失败') };
      const mod = await import('./vendor/adofai-player.js');
      const hostBox = document.getElementById('cv-adofai').getBoundingClientRect();
      const tmp = document.createElement('div');
      tmp.id = '__ctl_host';
      // ★ 对照容器就摆在预览区**原位**（截图用）：这样出的一张图 = 「没做这个改造、
      //   播放时肉眼会看到什么」。放完就拆，页面状态不留痕。
      tmp.style.cssText = 'position:fixed;z-index:5000;background:#0b0d10'
        + ';left:' + Math.round(hostBox.left) + 'px;top:' + Math.round(hostBox.top) + 'px'
        + ';width:' + Math.round(hostBox.width) + 'px;height:' + Math.round(hostBox.height) + 'px';
      document.body.appendChild(tmp);
      const pv = await mod.createPreview(tmp, JSON.stringify(lj.level), null, {
        editorMode: true, trail: false, renderer: 'webgl', hitsound: false, disableTrackTexture: true,
      });
      tmp.__ctl_pv = pv;                 // 截图之后由外面 destroy（不要在这儿拆，先让它画出来）
      const alive = !!(pv && pv.player && pv.player.hitErrorMeter);
      // 引擎默认只有命中时才会把它 visible=true ⇒ 手动喂几次判定，让它真画出来
      if (alive) {
        const hm = pv.player.hitErrorMeter;
        const cfg = (typeof pv.player.judgeConfig === 'function') ? pv.player.judgeConfig() : {};
        for (const e of [2.4, -1.3, 4.1, -3.2, 1.2, 0.5, -2.1, 3.0]) hm.addHit(e, 280, 100, 1, cfg);
        hm.update(16);
      }
      return { alive, ctlReady: true };
    } catch (e) { return { err: String((e && e.message) || e) }; }
  })()`);
  if (ctl && ctl.err) {
    note('对照组没跑成：' + ctl.err + '（headless 环境限制，本条未验证）');
  } else {
    check('G1 对照组：引擎默认**确实**会挂判定条（createPreview 出来的句柄 hitErrorMeter 非空）',
      ctl.alive === true, JSON.stringify(ctl));
    await sleep(600);
    await shot('0-对照-引擎默认有判定条');       // ← 交付说明里的「改造前」对比图
    const ctl2 = await ev(`(function(){
      const tmp = document.getElementById('__ctl_host');
      if (!tmp) return { err: '对照容器没了' };
      const zs = [...tmp.querySelectorAll('canvas')].map((c) => c.style.zIndex || '(空)');
      const host = document.getElementById('cv-adofai');
      return { ctlZs: zs.join(','), ctlHasBar: zs.includes('9998'),
               mineVisible: [...host.querySelectorAll('canvas')].filter((c) => c.style.zIndex === '9998').length };
    })()`);
    check('G2 对照组：容器里那张判定条画布的 zIndex 就是 9998',
      ctl2.ctlHasBar === true, '画布 zIndex=[' + ctl2.ctlZs + ']');
    check('G3 而我们的预览容器里，同类画布 0 张（同样的引擎、不同的处理）',
      ctl2.mineVisible === 0, 'mineVisible=' + ctl2.mineVisible);
    await ev(`(function(){
      const tmp = document.getElementById('__ctl_host');
      if (!tmp) return 1;
      const p = tmp.__ctl_pv; if (p) { try { p.destroy(); } catch(_e){} }
      tmp.remove(); return 1;
    })()`);
  }

  // ---------- H. 出张特写（预览框区域） ----------
  const box = await ev(`(function(){
    const h = document.getElementById('cv-adofai').getBoundingClientRect();
    return { x: Math.round(h.left), y: Math.round(h.top), width: Math.round(h.width), height: Math.round(h.height) };
  })()`);
  if (box && box.width > 0) {
    await shot('1-preview-area', { x: box.x, y: box.y, width: box.width, height: box.height });
  }

  console.log('\n页面异常 ' + events.length + ' 条' + (events.length ? '：' : ''));
  events.slice(0, 8).forEach((e) => console.log('   ' + e));
  console.log('\n' + '='.repeat(78));
  console.log(`结果：通过 ${PASS} / 失败 ${FAIL}`);
  process.exitCode = FAIL ? 1 : 0;
  try { child.kill(); } catch (_e) { /* 忽略 */ }
  ws.close();
})().catch((e) => { console.error('★ 探针自身出错：' + (e && e.stack || e)); process.exitCode = 2; });
