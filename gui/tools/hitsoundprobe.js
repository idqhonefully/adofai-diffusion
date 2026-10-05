/* hitsoundprobe.js —— 验「谱面预览的打拍音」已补上（第7条）。
 *
 * 背景（根因，2026-09-21 实测确认）：
 *   引擎 `createPreview` 的 `hitsound` 选项默认 `false`，而且**引擎自己从不调用**
 *   `preSynthesizeHitsounds()` —— vendor 包里那个方法（以及 `...WithProgress`）
 *   只有定义、**零调用点**。引擎把所有砖块的打拍音预先混成**一条整曲 AudioBuffer**，
 *   播放时用 `startAtOffset(offset)` 整条喂出去；没合成过则 `isSynthesized() === false`，
 *   `startPlay` / `seekTo` / `resume` 里**所有** hitsound 分支被跳过 ⇒ 全程静默。
 *
 * 改造（gui/studio-skin/app.js）：
 *   ① `createPreview(..., { hitsound: true })`
 *   ② 建好后宿主主动喊一次 `synthesizeHitsounds(preview)`
 *
 * 本探针干三件事：
 *   · 验真链路：载谱 → 建预览 → 打拍音已合成（buffer 属性 + 引擎自打日志）
 *   · **对照组**：绕过 `ensurePreview` 直接 `createPreview({hitsound:true})`，
 *     证明「引擎**不会**自己合成」—— 这条是根因的实证，没有它「引擎不自合成」只是推断
 *   · 别搞坏别的：判定条仍是 killed、谱面还在、能播、重建后能重新合成
 *
 *   · 零侵入：指到隔离实例（默认 8896），不碰主人正在用的 8766
 *   · 用法：node tools/hitsoundprobe.js [url] [sampleMid]
 * ========================================================================== */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const URL = process.argv[2] || 'http://127.0.0.1:8896/studio-skin/index.html';
const SAMPLE = process.argv[3] || '<REPO>\\chartgen\\samples\\Automaton_Waltz.mid';
const CHROME = process.env.HS_CHROME || 'chrome';
const OUT = process.env.HS_OUT || '<REPO>\\output\\logs\\hitsoundprobe';
const PORT = 9341;
const UDD = '<REPO>\\output\\.tmp\\hitsoundprobe-profile';
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

/* 页面侧取证：打拍音现状 + 引擎内部的合成结果（不经我们自己的钩子） */
const PROBE = `(function(){
  const d = window.__dsh || {};
  const pv = (typeof d.adofai === 'function') ? d.adofai() : null;
  const p = pv && pv.player;
  const hm = p && p.hitsoundManager;
  let buf = null;
  try {
    const b = hm && hm.synthesizedBuffer;
    if (b) buf = { ch: b.numberOfChannels, len: b.length, sr: b.sampleRate,
                   dur: Math.round(b.duration * 100) / 100 };
  } catch (_e) {}
  const st = (p && p.levelData && p.levelData.settings) || {};
  // 引擎的判定条（回归用）
  const host = document.getElementById('cv-adofai');
  const bars = host ? [...host.querySelectorAll('canvas')]
      .filter((c) => c.style.zIndex === '9998')
      .filter((c) => getComputedStyle(c).display !== 'none').length : -1;
  return {
    info: typeof d.hitsound === 'function' ? d.hitsound() : '(钩子缺失)',
    hasManager: !!hm,
    enabled: hm ? hm.isEnabled() : null,
    synth: hm ? hm.isSynthesized() : null,
    buf: buf,
    setType: st.hitsound || null,
    setVol: st.hitsoundVolume == null ? null : st.hitsoundVolume,
    hits: hm && hm.synthesizedBuffer ? null : null,
    meter: typeof d.hitErrorMeter === 'function' ? d.hitErrorMeter() : '(缺)',
    barVisible: bars,
    tiles: pv ? pv.tileCount : 0,
    dur: pv ? pv.totalDurationMs : 0,
  };
})()`;

/* 对照组：绕过 ensurePreview，直接建一个「只带 hitsound:true」的预览。
 * 预期 isSynthesized() === false —— 证明引擎不会自己合成。 */
const CTRL_BUILD = `(async function(){
  const d = window.__dsh;
  const lj = await d.api.levelJson(d.state);
  if (!lj || !lj.ok) return { err: 'levelJson: ' + ((lj && lj.error) || '?') };
  const m = await import('./vendor/adofai-player.js');
  const host = document.createElement('div');
  host.id = '__ctrl_host';
  host.style.cssText = 'position:absolute;left:-9999px;top:0;width:640px;height:360px';
  document.body.appendChild(host);
  const pv = await m.createPreview(host, JSON.stringify(lj.level), null,
    { hitsound: true, editorMode: true, trail: true, renderer: 'webgl' });
  window.__ctrlPv = pv;
  const hm = pv.player.hitsoundManager;
  return { built: true, enabled: hm.isEnabled(), synth: hm.isSynthesized(),
           hasMethod: typeof pv.player.preSynthesizeHitsoundsWithProgress === 'function' };
})()`;

/* 对照组第二步：显式喊一次合成，应变成 true */
const CTRL_SYNTH = `(async function(){
  const pv = window.__ctrlPv;
  if (!pv) return { err: 'no ctrl preview' };
  const hm = pv.player.hitsoundManager;
  const before = hm.isSynthesized();
  await pv.player.preSynthesizeHitsoundsWithProgress();
  const b = hm.synthesizedBuffer;
  return { before: before, after: hm.isSynthesized(),
           buf: b ? { ch: b.numberOfChannels, len: b.length, sr: b.sampleRate,
                      dur: Math.round(b.duration * 100) / 100 } : null };
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
  const console_ = [];
  ws.addEventListener('message', (m) => {
    const d = JSON.parse(m.data);
    if (d.id && waiters.has(d.id)) { waiters.get(d.id)(d); waiters.delete(d.id); return; }
    if (d.method === 'Runtime.consoleAPICalled') {
      // 引擎自己的日志是"它真的走过合成那段"的第一手证据
      const txt = (d.params.args || []).map((a) => String(a.value == null ? a.description : a.value)).join(' ');
      if (/HitsoundManager|hitsound/i.test(txt)) console_.push(txt);
    }
    if (d.method === 'Runtime.exceptionThrown') {
      const x = d.params.exceptionDetails || {};
      events.push('EXC ' + ((x.exception && x.exception.description) || x.text));
    }
    if (d.method === 'Log.entryAdded' && d.params.entry.level === 'error') {
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

  // ---------- B. 等预览建起来 ----------
  let pr = null, built = false;
  for (let i = 0; i < 90; i++) {
    pr = await ev(PROBE);
    if (pr && pr.hasManager) { built = true; break; }
    await sleep(1000);
  }
  const errTxt = await ev("String((window.__dsh && window.__dsh.adofaiErr) ? window.__dsh.adofaiErr() : '')");
  check('B1 预览引擎建起来了（前提条件）', built === true,
    built ? JSON.stringify(pr) : ('未建起，adofaiErr=' + errTxt));
  if (!built) {
    note('headless 环境建不起 WebGL 预览 ⇒ 本条**未验证**（不是通过、也不是失败）。');
    console.log('\n异常 ' + events.length + ' 条：'); events.slice(0, 8).forEach((e) => console.log('   ' + e));
    return;
  }
  // B2：等自动合成落地。宿主那句 `synthesizeHitsounds()` 是**异步**的
  //     （内部 await 引擎的 preSynthesize...），必须在断言前等一等 ——
  //     否则读到的是"还没合成"的中间态，会把好代码误报成失败。
  for (let i = 0; i < 40; i++) {
    if (pr && pr.synth === true) break;
    await sleep(500);
    pr = await ev(PROBE);
  }
  console.log('\n--- 建起来后的实测 ---');
  console.log(JSON.stringify(pr));
  console.log('--- 引擎自打日志 ---');
  console_.forEach((l) => console.log('  ' + l));
  console.log('');

  // ---------- C. 打拍音真的就绪了 ----------
  check('C1 开关是开的（setHitsoundEnabled(true) 已生效）', pr.enabled === true, 'enabled=' + pr.enabled);
  check('C2 打拍音已合成（isSynthesized）', pr.synth === true, 'synth=' + pr.synth);
  check('C3 合成 buffer 真的存在且有内容', !!(pr.buf && pr.buf.len > 0),
    pr.buf ? `ch=${pr.buf.ch} sr=${pr.buf.sr} len=${pr.buf.len} dur=${pr.buf.dur}s` : '(无 buffer)');
  check('C4 音色/音量取自谱面 settings（不是我们硬编的）',
    pr.setType === 'Kick' && Number(pr.setVol) === 25,
    `settings.hitsound=${pr.setType} hitsoundVolume=${pr.setVol}`);
  check('C5 buffer 时长 ≥ 谱面时长（整曲一条 buffer）',
    !!(pr.buf && pr.dur > 0 && pr.buf.dur * 1000 >= pr.dur),
    `buffer ${pr.buf && pr.buf.dur}s vs 谱面 ${Math.round(pr.dur / 1000)}s`);
  // ⚠ 引擎那条日志的**函数名带 WithProgress 后缀**（`preSynthesizeHitsoundsWithProgress: ...`），
  //   别按不带后缀的名字写正则 —— 会匹配不上，把"拿到了证据"误报成"没抓到"。
  const engLog = console_.find((l) => /preSynthesizeHitsounds(WithProgress)?:/.test(l));
  check('C6 引擎自己打了「preSynthesizeHitsounds[WithProgress]: N groups, M default hits」日志',
    !!engLog, engLog || '(没抓到)');

  // ---------- D. 对照组：引擎**不会**自己合成（根因实证） ----------
  console.log('\n--- 对照组（绕过 ensurePreview，直接 createPreview hitsound:true） ---');
  const ctrl = await ev(CTRL_BUILD);
  check('D1 对照预览建起来了', !!(ctrl && ctrl.built), JSON.stringify(ctrl));
  if (ctrl && ctrl.built) {
    check('D2 对照预览：开关默认就是开的', ctrl.enabled === true, 'enabled=' + ctrl.enabled);
    check('D3 ★ 对照预览：**没合成**（证明引擎不会自动合成，必须宿主喊）',
      ctrl.synth === false, 'synth=' + ctrl.synth);
    check('D4 合成方法是引擎暴露的公开方法（存在）', ctrl.hasMethod === true, 'hasMethod=' + ctrl.hasMethod);
    const cs = await ev(CTRL_SYNTH);
    check('D5 显式喊一次合成后 ⇒ 有 buffer（且之前没有）',
      !!(cs && cs.before === false && cs.after === true && cs.buf && cs.buf.len > 0),
      JSON.stringify(cs));
  }
  await ev("(function(){ try { window.__ctrlPv.destroy(); } catch(_e){} const h=document.getElementById('__ctrl_host'); if(h) h.remove(); return 1; })()");

  // ---------- E. 运行期开关（buffer 不用重合成） ----------
  await ev("window.__dsh.setHitsound(false)");
  await sleep(300);
  const off = await ev(PROBE);
  check('E1 关掉后 isEnabled=false', off.enabled === false, 'enabled=' + off.enabled);
  check('E2 关掉后 buffer 还在（只是不播，不用重合成）', off.synth === true, 'synth=' + off.synth);
  await ev("window.__dsh.setHitsound(true)");
  await sleep(300);
  const on = await ev(PROBE);
  check('E3 再打开 ⇒ 立刻恢复（buffer 长度不变）', on.enabled === true && on.buf && off.buf && on.buf.len === off.buf.len,
    `enabled=${on.enabled} len ${off.buf && off.buf.len} → ${on.buf && on.buf.len}`);

  // ---------- F. 别搞坏别的（回归） ----------
  await ev("(function(){ try { window.__dsh.adofai().startPlay(0); } catch(_e){} return 1; })()");
  await sleep(2500);
  const pr2 = await ev(PROBE);
  check('F1 谱面预览照常能播', !!(pr2 && pr2.tiles > 0), `tiles=${pr2 && pr2.tiles}`);
  check('F2 判定条仍然是 killed（第7条没把上一版改造搅坏）',
    pr2.meter === 'killed' && pr2.barVisible === 0,
    `meter=${pr2.meter} barVisible=${pr2.barVisible}`);
  await ev("(function(){ try { window.__dsh.adofai().stop(); } catch(_e){} return 1; })()");
  await sleep(400);

  // ---------- G. 重建预览 ⇒ 必须重新合成 ----------
  await ev("window.__dsh.reloadPreview()");
  let rebuilt = null;
  for (let i = 0; i < 60; i++) {
    rebuilt = await ev(PROBE);
    if (rebuilt && rebuilt.hasManager && rebuilt.synth) break;
    await sleep(1000);
  }
  check('G1 重建预览后，打拍音重新合成好了（换谱面/换音源走的就是这条路）',
    !!(rebuilt && rebuilt.synth === true && rebuilt.buf && rebuilt.buf.len > 0),
    rebuilt ? `synth=${rebuilt.synth} buf=${rebuilt.buf && rebuilt.buf.len}` : '(读不到)');

  console.log('\n' + '='.repeat(78));
  console.log(`通过 ${PASS} / 失败 ${FAIL}`);
  console.log('异常 ' + events.length + ' 条：'); events.slice(0, 8).forEach((e) => console.log('   ' + e));
  try { child.kill(); } catch (_e) {}
  process.exit(FAIL ? 1 : 0);
})();
