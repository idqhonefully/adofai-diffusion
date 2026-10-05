/* volprobe.js —— 验「谱面页的音量滑块」真的作用于**引擎那条链**（不是只看 #player）。
 * ============================================================================
 * 背景（2026-09-26，主人：「这个音量按钮是假的」）：
 *   工作台默认就在「谱面」页，而这一页的声音是**引擎自己播的**：
 *     · 音乐 → `preview.player.music`（内部 `new Audio()`，**不在 DOM 里**，抓不到元素）
 *     · 打拍音 → `hitsoundManager.getGainNode()`（WebAudio GainNode，buffer 已烘死）
 *   原来 `#vol` 只写 `<audio id="player">.volume` ⇒ 在谱面页一点用都没有。
 *
 * 这个探针怎么取证（关键：目标元素拿不到引用）：
 *   ① 在页面脚本跑起来**之前**挂钩 `HTMLMediaElement.prototype.volume` 与
 *      `AudioParam.prototype.value` 的 setter，把每一次赋值记进 `window.__VOLSET`；
 *   ② 用真链路载入一份真谱面（`window.__dsh.load`），等引擎出图；
 *   ③ 清空记录 → 拖一次 `#vol` → 看记录里出现了谁。
 *   期望：`(anon)` 的媒体元素（引擎音乐，无 id）与一个 `audioparam`（打拍音 gain）
 *         都被设成新值 —— 这才叫「接线了」。旧版本只会出现 `#player`。
 *
 * ⚠ 假宿主要补 `window.dsh`：workbench 的 init 里 `await window.dsh.info()`，
 *   缺方法会抛、把 init 从中间截断，`window.__dsh`（宿主自动载入入口）就永远挂不上
 *   —— 表现就是「__dsh 未定义」。（pageeval 的 STUB 只造了空 dsh，所以那边载不了谱。）
 *
 * 用法：node tools/volprobe.js [url] [midPath] [--vol 20]
 *   零侵入：指到隔离实例（默认 8896），不碰主人正在用的 8766。
 * ========================================================================== */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');

const argv = process.argv.slice(2);
const URL_ = argv[0] || 'http://127.0.0.1:8896/workbench/index.html';
const MID = argv[1] || '<REPO>\\output\\.work\\HyuN - Grin_1789901575\\HyuN - Grin_stems_combined.mid';
const VOL = (() => { const i = argv.indexOf('--vol'); return i >= 0 ? Number(argv[i + 1]) : 20; })();
const CHROME = process.env.VP_CHROME || 'chrome';
const PORT = 9342;
const UDD = '<REPO>\\output\\.tmp\\volprobe-profile';

let PASS = 0, FAIL = 0;
const check = (name, ok, detail) => {
  ok ? PASS++ : FAIL++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '   [' + detail + ']' : ''}`);
};
const note = (s) => console.log('NOTE  ' + s);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const esc = (s) => String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'");

/* ⚠ 假宿主必须**回复** dsh_call：workbench 的 __dsh_bridge.js 自己实现 window.dsh，
 *   但每个方法都是「post 出去 → 等宿主回 dsh_reply」（超时 10 分钟）。
 *   只收集、不应答 ⇒ init 里 `await dsh.info()` 永久挂住，init 从中间截断，
 *   `window.__dsh`（宿主自动载入入口）就永远挂不上 —— 表现就是「__dsh 未定义」。 */
const STUB = `
window.__SENT = []; window.__LISTENERS = []; window.__VOLSET = []; window.__ERRS = [];
window.addEventListener('error', function (e) { window.__ERRS.push('' + (e.message || e.error)); });
window.addEventListener('unhandledrejection', function (e) {
  window.__ERRS.push('rej: ' + ((e.reason && e.reason.message) || e.reason));
});
window.chrome = window.chrome || {};
window.chrome.webview = {
  postMessage: function (s) {
    window.__SENT.push(s);
    try {
      var m = JSON.parse(s);
      if (m && m.type === 'dsh_call') {
        var result = (m.name === 'info') ? { version: 'volprobe', port: 0 } : null;
        setTimeout(function () {
          window.__deliver({ type: 'dsh_reply', id: m.id, ok: true, result: result });
        }, 0);
      }
    } catch (e) {}
  },
  addEventListener: function (t, f) { if (t === 'message') window.__LISTENERS.push(f); }
};
window.__deliver = function (o) {
  var s = typeof o === 'string' ? o : JSON.stringify(o);
  window.__LISTENERS.forEach(function (f) { try { f({ data: s }); } catch (e) {} });
};
// 音量取证钩子
(function () {
  try {
    var d = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'volume');
    Object.defineProperty(HTMLMediaElement.prototype, 'volume', {
      configurable: true,
      get: function () { return d.get.call(this); },
      set: function (v) {
        try { window.__VOLSET.push({ kind: 'media', id: this.id || '(anon)',
              v: Math.round(v * 1000) / 1000, src: String(this.currentSrc || this.src || '').slice(-22) }); } catch (e) {}
        d.set.call(this, v);
      }
    });
  } catch (e) { window.__VOLSET.push({ kind: 'hook-err', id: 'media:' + e.message }); }
  try {
    var p = Object.getOwnPropertyDescriptor(AudioParam.prototype, 'value');
    Object.defineProperty(AudioParam.prototype, 'value', {
      configurable: true,
      get: function () { return p.get.call(this); },
      set: function (v) {
        try { window.__VOLSET.push({ kind: 'audioparam', id: '(gain)',
              v: Math.round(Number(v) * 1000) / 1000 }); } catch (e) {}
        p.set.call(this, v);
      }
    });
  } catch (e) { window.__VOLSET.push({ kind: 'hook-err', id: 'param:' + e.message }); }
})();
`;

const get = (p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: PORT, path: p }, (r) => {
    let b = ''; r.on('data', (c) => (b += c)); r.on('end', () => res(b));
  }).on('error', rej);
});

(async () => {
  if (!fs.existsSync(MID)) { console.log('✗ 谱面不存在：' + MID); process.exit(2); }
  fs.mkdirSync(UDD, { recursive: true });
  const child = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--autoplay-policy=no-user-gesture-required',
    '--force-device-scale-factor=1',
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${UDD}`,
    '--window-size=1560,940', 'about:blank',
  ], { stdio: 'ignore', windowsHide: true });

  let list = [];
  for (let i = 0; i < 80; i++) {
    try { list = JSON.parse(await get('/json/list')); if (list.some((t) => t.type === 'page')) break; } catch (_e) {}
    await sleep(250);
  }
  const page = list.find((t) => t.type === 'page');
  if (!page) throw new Error('连不上 headless');

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  const waiters = new Map(); let seq = 0;
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && waiters.has(m.id)) {
      const w = waiters.get(m.id); waiters.delete(m.id);
      m.error ? w.rej(new Error(JSON.stringify(m.error))) : w.res(m.result);
    }
  });
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  const send = (method, params) => new Promise((res, rej) => {
    const id = ++seq; waiters.set(id, { res, rej });
    ws.send(JSON.stringify({ id, method, params: params || {} }));
  });
  const ev = async (expr) => {
    const r = await send('Runtime.evaluate', {
      expression: '(async function(){' + expr + '})()', returnByValue: true, awaitPromise: true,
    });
    if (r.exceptionDetails) throw new Error((r.exceptionDetails.exception || {}).description);
    return r.result.value;
  };
  const js = async (expr) => JSON.parse(await ev('return JSON.stringify(' + expr + ')'));

  await send('Runtime.enable');
  await send('Page.enable');
  await send('Page.addScriptToEvaluateOnNewDocument', { source: STUB });
  await send('Emulation.setDeviceMetricsOverride', { width: 1560, height: 940, deviceScaleFactor: 1, mobile: false });
  await send('Page.navigate', { url: URL_ });
  await sleep(3500);

  // ---- 0. 前置：init 是否跑完（__dsh 挂上）+ 是否真有 WebGL（否则引擎根本不会建）
  const pre = await js(`({ dsh: typeof (window.__dsh && window.__dsh.load),
      webgl: !!(document.createElement('canvas').getContext('webgl2')),
      hookErr: window.__VOLSET.filter(function (x) { return x.kind === 'hook-err'; }),
      errs: window.__ERRS })`);
  check('页面 init 跑完（window.__dsh.load 已挂）', pre.dsh === 'function', 'typeof=' + pre.dsh);
  check('WebGL 可用（引擎才建得起来）', pre.webgl === true);
  check('取证钩子安装无异常', pre.hookErr.length === 0, JSON.stringify(pre.hookErr));
  if (pre.errs && pre.errs.length) note('init 期间捕获到错误：' + JSON.stringify(pre.errs));
  if (pre.dsh !== 'function') { console.log('\n✗ 前置不成立，后续无意义，中止。'); try { child.kill(); } catch (_e) {} process.exit(1); }

  // ---- 1. 用**真链路**载入谱面（与宿主自动载入同一条路）
  note('载入谱面：' + MID);
  await ev(`window.__dsh.load('${esc(MID)}'); return 1;`);
  let built = false, lastPh = '';
  for (let i = 0; i < 90; i++) {
    await sleep(1000);
    const st = await js(`({ canv: document.querySelectorAll('#cv-adofai canvas').length,
        ph: ((document.querySelector('#ph-text') || {}).textContent || ''),
        hidden: !!(document.querySelector('#ph-note') || {}).classList && document.querySelector('#ph-note').classList.contains('hidden') })`);
    lastPh = st.ph;
    if (st.canv > 0) { built = true; note(`引擎出图：canvas=${st.canv}（等了 ${i + 1}s）`); break; }
  }
  check('谱面页出图（引擎 Player 已建）', built, built ? '' : ('未出图，舞台提示="' + lastPh + '"'));
  if (!built) { console.log('\n✗ 出图失败，量不到引擎链。'); try { child.kill(); } catch (_e) {} process.exit(1); }

  await sleep(1500);   // 让 loadMusic / 打拍音合成把各自的初值设完

  // ---- 2. 正对照：拖一次音量滑块，看谁被设了
  const before = await js(`({ vol: document.querySelector('#vol').value, pv: window.player.volume })`);
  await ev(`window.__VOLSET.length = 0; return 1;`);
  await ev(`(function () { var v = document.querySelector('#vol');
      v.value = '${VOL}'; v.dispatchEvent(new Event('input', { bubbles: true })); return 1; })()`);
  await sleep(700);
  const sets = await js(`window.__VOLSET`);
  const want = VOL / 100;

  console.log('\n--- 拖动后记录到的音量赋值 ---');
  for (const s of sets) console.log(`   ${s.kind}  id=${s.id}  v=${s.v}${s.src ? '  src=…' + s.src : ''}`);

  const mediaAnon = sets.filter((s) => s.kind === 'media' && s.id === '(anon)' && s.v === want);
  const mediaPlay = sets.filter((s) => s.kind === 'media' && s.id === 'player' && s.v === want);
  const paramHit = sets.filter((s) => s.kind === 'audioparam' && s.v === want);

  check('引擎音乐被设成滑块值（无 id 的 Audio 元素）', mediaAnon.length > 0,
    mediaAnon.length ? 'v=' + want : '★ 没被设 —— 引擎那条链还是断的');
  check('打拍音增益被设成滑块值（AudioParam/gainNode）', paramHit.length > 0,
    paramHit.length ? 'v=' + want : '★ 没被设');
  check('原 <audio id="player"> 行为不退化', mediaPlay.length > 0 && before.pv !== undefined,
    'before=' + before.pv);

  console.log(`\n合计：PASS ${PASS} / FAIL ${FAIL}`);
  try { ws.close(); } catch (_e) {}
  try { child.kill(); } catch (_e) {}
  process.exit(FAIL ? 1 : 0);
})().catch((e) => { console.error('【失败】' + (e && e.stack || e)); process.exit(1); });
