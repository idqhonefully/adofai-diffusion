/* visuallagprobe.js —— 验「预览画面整体滞后 N 毫秒」已生效（第8条）。
 *
 * 背景：引擎的渲染时钟 `elapsedTime` 取自 `AudioContext.currentTime` 或
 * `performance.now()`（都是"算到哪儿"的时钟），而扬声器出声还要过输出链路（≈100ms）
 * ⇒ 画面恰好比听到的早一个输出延迟。改造把 `updatePlayer()` 每帧看到的两根时钟
 * **同时往后挪 lag 毫秒**（`gui/studio-skin/app.js` 的 `applyVisualLag()`）。
 *
 * 怎么验才**不算自证**：不看我们的钩子，直接用**墙钟**量。
 *   同一次播放里：`墙钟经过的时间 - player.elapsedTime` 就是"画面落后真实时间多少"。
 *   lag=0 时应 ≈0（只有一点点调度抖动）；lag=100 时应 ≈100。
 *   ⇒ 把两次的差值拿出来比，差 ≈100ms 才算通过。
 *
 * 另外验两个"里程碑"没被偏移污染：
 *   · `musicDelayMs` 必须等于宿主传进去的那个值（没被我们多推 100ms，否则音画差会变 200ms）
 *   · `_musicScheduled` / `audioDriftSynced` 两个一次性标志都得跑完（否则偏移不生效）
 *
 *   · 零侵入：指到隔离实例（默认 8896）
 *   · 用法：node tools/visuallagprobe.js [url] [sampleMid]
 * ========================================================================== */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');

const URL = process.argv[2] || 'http://127.0.0.1:8896/studio-skin/index.html';
const SAMPLE = process.argv[3] || '<REPO>\\chartgen\\samples\\Automaton_Waltz.mid';
const CHROME = process.env.VL_CHROME || 'chrome';
const PORT = 9345;
const UDD = '<REPO>\\output\\.tmp\\visuallag-profile';
const W = 1280, H = 800;
const PROBE_MS = 3000;   // 每次播放观察窗（必须 > 1500ms 的兜底生效门槛）

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

/* 墙钟量：从 startPlay 那一刻起，真实过去多久 vs 播放器认为过去多久 */
const TIMED_PLAY = `(async function(ms){
  const d = window.__dsh;
  const p = d.adofai().player;
  p.stopPlay();
  await new Promise((r) => setTimeout(r, 250));
  const t0 = performance.now();
  d.adofai().startPlay(0);
  await new Promise((r) => setTimeout(r, ms));
  const wall = performance.now() - t0;
  const snap = {
    wall: Math.round(wall * 10) / 10,
    elapsed: Math.round(p.elapsedTime * 10) / 10,
    behind: Math.round((wall - p.elapsedTime) * 10) / 10,
    lag: Number(p.__wbVisualLag) || 0,
    musicDelayMs: p.musicDelayMs,
    musicStartDelayMs: Math.round(p.musicStartDelay * 1000),
    sched: !!p._musicScheduled,
    drift: !!p.audioDriftSynced,
    playing: p.isPlaying,
    hasAudio: !!(p.music && p.music.hasAudio),
    tiles: p.tileCount,
  };
  return snap;
})`;

const HOOK = `(function(){
  const d = window.__dsh || {};
  const p = d.adofai && d.adofai() ? d.adofai().player : null;
  return {
    lag: typeof d.visualLag === 'function' ? d.visualLag() : '(钩子缺失)',
    hooked: p ? !!p.__wbVisualLagHooked : null,
    hasSetter: p ? (typeof p.__wbSetVisualLag === 'function') : null,
    meter: typeof d.hitErrorMeter === 'function' ? d.hitErrorMeter() : '(缺)',
    hitsound: typeof d.hitsound === 'function' ? d.hitsound() : '(缺)',
  };
})()`;

(async () => {
  fs.mkdirSync(UDD, { recursive: true });
  const child = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--mute-audio', '--autoplay-policy=no-user-gesture-required',
    '--remote-debugging-port=' + PORT, '--user-data-dir=' + UDD,
    '--window-size=' + W + ',' + H, 'about:blank',
  ], { stdio: 'ignore', windowsHide: true });

  let list = [];
  for (let i = 0; i < 80; i++) {
    try { list = JSON.parse(await get('/json/list')); if (list.some((t) => t.type === 'page')) break; }
    catch (_e) { /* 等 */ }
    await sleep(250);
  }
  const page = list.find((t) => t.type === 'page');
  if (!page) throw new Error('连不上浏览器');
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

  await send('Page.enable'); await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });
  console.log('='.repeat(78));
  console.log('### ' + URL + '\n    样本：' + SAMPLE + '\n');
  await send('Page.navigate', { url: URL });
  await sleep(1500);
  await ev('try { localStorage.clear(); } catch(_e){} 1');
  await send('Page.reload', { ignoreCache: true });
  for (let i = 0; i < 60; i++) {
    if (await ev("document.querySelectorAll('section.grp').length") > 0) break;
    await sleep(500);
  }
  await sleep(1000);

  const loaded = await ev("(async()=>{ try{ await window.__dsh.load('" + esc(SAMPLE) + "'); return 'ok'; }catch(e){ return 'ERR '+(e&&e.message); } })()");
  check('A1 载入示例谱面', loaded === 'ok', String(loaded));
  let hasChart = false;
  for (let i = 0; i < 90; i++) {
    hasChart = await ev('!!(window.__dsh && window.__dsh.stats && window.__dsh.stats().hasChart)');
    if (hasChart) break;
    await sleep(1000);
  }
  check('A2 谱面出来了', hasChart === true, 'hasChart=' + hasChart);
  if (!hasChart) { console.log('★ 谱面没出来，后面对不了。'); return; }

  let h = null;
  for (let i = 0; i < 90; i++) {
    h = await ev(HOOK);
    if (h && h.hooked !== null && h.hooked !== undefined) break;
    await sleep(1000);
  }
  check('B1 滞后改造已挂到 player 上（__wbVisualLagHooked）', h.hooked === true, JSON.stringify(h));
  check('B2 默认滞后量 = 100ms', Number(h.lag) === 100, 'lag=' + h.lag);
  check('B3 提供了运行期调节把手（真机微调用）', h.hasSetter === true, 'hasSetter=' + h.hasSetter);

  // ---------- C. 墙钟对照：lag=100 vs lag=0 ----------
  console.log(`\n--- 每次播放观察 ${PROBE_MS}ms，用墙钟对照 ---`);
  const withLag = await ev(`${TIMED_PLAY}(${PROBE_MS})`);
  console.log('  lag=100 → ' + JSON.stringify(withLag));
  await ev('window.__dsh.setVisualLag(0)');
  const noLag = await ev(`${TIMED_PLAY}(${PROBE_MS})`);
  console.log('  lag=0   → ' + JSON.stringify(noLag));
  await ev('window.__dsh.setVisualLag(100)');
  const again = await ev(`${TIMED_PLAY}(${PROBE_MS})`);
  console.log('  lag=100 → ' + JSON.stringify(again));

  const d1 = withLag.behind - noLag.behind;      // 应 ≈ 100
  const d2 = again.behind - noLag.behind;        // 应 ≈ 100
  console.log(`\n  画面落后量：lag0=${noLag.behind}ms  lag100=${withLag.behind}ms（差 ${d1.toFixed(1)}ms）`);
  check('C1 播放确实跑起来了（不是量了个静止画面）',
    !!(withLag.playing && withLag.elapsed > PROBE_MS * 0.5),
    `playing=${withLag.playing} elapsed=${withLag.elapsed}ms`);
  check('C2 ★ lag=100 时画面比 lag=0 多落后 ≈100ms（墙钟量，±35ms）',
    Math.abs(d1 - 100) <= 35, `Δ=${d1.toFixed(1)}ms`);
  check('C3 可重复（再切回 100ms 仍是 ≈100ms）',
    Math.abs(d2 - 100) <= 35, `Δ=${d2.toFixed(1)}ms`);
  check('C4 lag=0 时基本不落后（≈0，说明偏移是可逆的、不是写死）',
    Math.abs(noLag.behind) <= 35, `behind=${noLag.behind}ms`);

  // ---------- D. 里程碑没被偏移污染（否则音画差会变 200ms） ----------
  check('D1 两个一次性里程碑都跑完了（偏移才有资格生效）',
    withLag.sched === true && withLag.drift === true,
    `_musicScheduled=${withLag.sched} audioDriftSynced=${withLag.drift}`);
  check('D2 ★ musicDelayMs 没被多推 100ms（否则音乐会被排晚、音画差翻倍）',
    withLag.musicDelayMs === noLag.musicDelayMs,
    `lag100=${withLag.musicDelayMs} vs lag0=${noLag.musicDelayMs}`);
  note(`musicStartDelay 实测 ${withLag.musicStartDelayMs}ms（lag 不会改变它 —— 音乐排期用的仍是原始时钟）`);

  // ---------- E. 别搞坏别的 ----------
  const h2 = await ev(HOOK);
  check('E1 判定条仍然是 killed（第8条没把第7条/上一版改造搅坏）', h2.meter === 'killed', 'meter=' + h2.meter);
  check('E2 打拍音仍然是合成好的状态', /synth/.test(String(h2.hitsound)), 'hitsound=' + h2.hitsound);
  await ev("(function(){ try { window.__dsh.adofai().stop(); } catch(_e){} return 1; })()");

  console.log('\n' + '='.repeat(78));
  console.log(`通过 ${PASS} / 失败 ${FAIL}`);
  console.log('异常 ' + events.length + ' 条：'); events.slice(0, 6).forEach((e) => console.log('   ' + e));
  try { child.kill(); } catch (_e) {}
  process.exit(FAIL ? 1 : 0);
})();
