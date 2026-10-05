/* avsrcprobe.js —— 预览音源「记住 + 自动接上 + 药丸按就绪收放」的端到端探针
 * ============================================================================
 * 主人 2026-09-21 的两条裁定（原话）：
 *   「直接选好用户之前选择好的，音乐按照这个加载进去，然后就不要显示预览框下面的
 *     这个了。如果没有加载好的话再显示」
 * 拆成三条可测的：
 *   ① 记住：用户挑过的那份音源要写进 localStorage，重进工作台**直接就是它**；
 *   ② 自动接上：一次都没选过时，本工程自带的原曲（`<MIDI 目录>/job/input.wav`）
 *      自动接成预览音源（先探存在再绑，绝不写假路径）；
 *   ③ 药丸：音源**就绪** ⇒ 药丸 + 它那条 30px 空工具条**一起收掉**；
 *      **没就绪**（指定文件为空 / 文件不在了）⇒ 亮成警告样式；改动入口不能丢。
 *
 * 用法：node tools/avsrcprobe.js [url] [chrome.exe]
 * 默认 url = http://127.0.0.1:8896/workbench/index.html（隔离 dev 实例）
 * 用**全新 profile**（先删目录）跑 ⇒ localStorage 天然是空的，才能验「首次进来」那条。
 * ============================================================================
 */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const URL_ = process.argv[2] || 'http://127.0.0.1:8896/workbench/index.html';
const CHROME = process.argv[3] || 'chrome';
const PORT = Number(process.env.AVSRC_PORT || 9336);
const UDD = process.env.AVSRC_UDD || '<REPO>\\output\\.tmp\\avsrc-profile';

// ── 测试素材（都在本机真实存在，别用假路径冒充"存在"）──────────────────────
const MIDI = '<REPO>\\output\\.work\\HyuN - Grin_1789901575\\HyuN - Grin_stems_combined.mid';
const PROJ_WAV = '<REPO>\\output\\.work\\HyuN - Grin_1789901575\\job\\input.wav';
const USER_MP3 = '<PICT>\\HyuN - Grin.mp3';
const GONE_MP3 = '<PICT>\\__avsrcprobe_no_such_song__.mp3';
// 负向用：**不是**我们流水线产物的一份 MIDI（同目录下没有 job/input.wav）
const SAMPLE_MIDI = '<REPO>\\chartgen\\samples\\Automaton_Waltz.mid';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = (p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: PORT, path: p }, (r) => {
    let b = ''; r.on('data', (c) => (b += c)); r.on('end', () => res(b));
  }).on('error', rej);
});
const J = (s) => JSON.stringify(String(s));

const results = [];
function check(name, ok, extra) {
  results.push({ name, ok: !!ok, extra: extra === undefined ? '' : String(extra) });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra !== undefined ? '   ' + extra : ''}`);
}

(async () => {
  for (const p of [MIDI, USER_MP3]) {
    if (!fs.existsSync(p)) { console.error('素材不存在：' + p); process.exit(2); }
  }
  if (fs.existsSync(GONE_MP3)) { console.error('「不存在的那份」居然存在：' + GONE_MP3); process.exit(2); }

  fs.rmSync(UDD, { recursive: true, force: true });      // ★ 全新 profile ⇒ localStorage 为空
  fs.mkdirSync(UDD, { recursive: true });
  const child = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--mute-audio', `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${UDD}`, '--window-size=1560,940', 'about:blank',
  ], { stdio: 'ignore', windowsHide: true });

  let list = [];
  for (let i = 0; i < 80; i++) {
    try { list = JSON.parse(await get('/json/list')); if (list.some((t) => t.type === 'page')) break; } catch (_e) {}
    await sleep(250);
  }
  const page = list.find((t) => t.type === 'page');
  if (!page) throw new Error('没能连上 headless 浏览器的 DevTools');

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
  const ready = () => ev("!!document.querySelector('#groups section.grp') && !!window.__dsh && !!window.__dsh.av");

  async function boot() {
    await send('Page.navigate', { url: URL_ });
    for (let i = 0; i < 80; i++) { try { if (await ready()) return true; } catch (_e) {} await sleep(250); }
    return false;
  }
  /** 走真实链路载入那份 MIDI，并等谱面重算完（`__chartReady` 是 rebuild 里置的）。 */
  async function loadMidi() {
    await ev('window.__chartReady=false');
    await ev(`(async()=>{ await window.__dsh.load(${J(MIDI)}); return true; })()`);
    for (let i = 0; i < 160; i++) {
      if (await ev('window.__chartReady===true')) return true;
      await sleep(500);
    }
    return false;
  }
  /** 改音源后等重算落地（applyPayload 里才会刷药丸显示）。 */
  async function waitChart() {
    for (let i = 0; i < 160; i++) {
      if (await ev('window.__chartReady===true')) return true;
      await sleep(500);
    }
    return false;
  }
  const AV = () => ev('JSON.stringify(window.__dsh.av())').then(JSON.parse);

  await send('Runtime.enable');
  await send('Log.enable');
  await send('Page.enable');

  // ══════════════════════════ A 首次进来：自动接上本工程的原曲 ══════════════════
  console.log('\n--- A 首次进来（全新 profile，用户没选过）---');
  check('A0 页面 + __dsh.av 钩子就绪', await boot());
  check('A0b 全新 profile：localStorage 里没有音源记忆',
        (await ev("(function(){try{return localStorage.getItem('adoc.avpick')}catch(e){return String(e)}})()")) === null);

  const okA = await loadMidi();
  check('A1 载入 MIDI 并重算出谱面', okA);
  let av = await AV();
  check('A2 自动接上了本工程自带原曲（preview_audio_mode=2 且指向 job/input.wav）',
        av.mode === 2 && /job[\\/]input\.wav$/i.test(av.path), JSON.stringify(av).slice(0, 240));
  check('A3 后端真解析出了同一份文件（payload.preview_audio）',
        av.resolved.replace(/\\/g, '/').toLowerCase() === PROJ_WAV.replace(/\\/g, '/').toLowerCase(),
        `resolved=${av.resolved || '(空)'}`);
  check('A4 就绪判定 = true', av.ready === true);
  check('A5 ★ 药丸收起来了（.hidden 且真的不在布局里）',
        av.pill && av.pill.hidden === true && (await ev("(function(){var b=document.querySelector('#btn-av-sum');return !b||b.getBoundingClientRect().height===0})()")),
        JSON.stringify(av.pill));
  check('A6 ★ 它下面那条空工具条也收掉了（.empty 且高 0，别留一条光板）',
        av.bar && av.bar.empty === true && av.bar.h === 0 && av.bar.display === 'none',
        JSON.stringify(av.bar));
  check('A7 这是「首次自动接续」而不是「记忆恢复」', av.pickFromStore === false);
  check('A8 自动接续**不写**记忆（免得下次换歌被上一首的原曲顶住）', av.saved === null,
        'saved=' + String(av.saved));

  // ══════════════════════════ B 没接上：药丸必须亮 ══════════════════════════
  console.log('\n--- B 指定了一份不存在的文件 ⇒ 未就绪，药丸亮 ---');
  await ev('window.__chartReady=false');
  await ev(`window.__dsh.avPickPath(${J(GONE_MP3)})`);
  const okB = await waitChart();
  check('B1 改完音源后重算落地', okB);
  av = await AV();
  check('B2 就绪判定 = false（后端退回合成音，payload.preview_audio 为空）',
        av.ready === false && !av.resolved, JSON.stringify({ ready: av.ready, resolved: av.resolved }));
  check('B3 ★ 药丸亮起来了（.hidden 被摘掉，且在布局里有高度）',
        av.pill && av.pill.hidden === false && (await ev("(function(){var b=document.querySelector('#btn-av-sum');return !!b&&b.getBoundingClientRect().height>0})()")),
        JSON.stringify(av.pill));
  check('B4 药丸是**警告**样式（.warn + 文案写着未就绪）',
        av.pill.warn === true && /音源未就绪/.test(av.pill.txt), av.pill.txt);
  check('B5 ★ 空工具条回来了（未就绪时它得在，药丸才有地方待）',
        av.bar && av.bar.empty === false && av.bar.h > 0,
        JSON.stringify(av.bar));
  check('B6 后端那句「指定的文件不存在」也上屏了（不许静默退回合成音）',
        await ev("/预览音源指定的文件不存在/.test((document.querySelector('#warnings')||{}).textContent||'')"));
  let b7 = '', b7ok = false;
  for (let i = 0; i < 40; i++) {                       // 换音源是异步落地的，等它一句
    b7 = await ev("document.querySelector('#audio-src').textContent + ' | ' + !!document.querySelector('#player').getAttribute('src')");
    if (/合成节拍音/.test(b7)) { b7ok = true; break; }
    await sleep(250);
  }
  check('B7 上一份文件不会赖在播放条上：退回合成音时旧 src 被摘掉（等真按播放再生成）',
        b7ok && /false$/.test(b7), b7);

  // ══════════════════════════ C 用户自己挑一份：要记住 ══════════════════════
  console.log('\n--- C 用户挑一份真文件 ⇒ 就绪 + 记住 ---');
  await ev('window.__chartReady=false');
  await ev(`window.__dsh.avPickPath(${J(USER_MP3)})`);
  const okC = await waitChart();
  check('C1 换音源后重算落地', okC);
  av = await AV();
  check('C2 就绪判定 = true（后端解析出的就是用户挑的那份）',
        av.ready === true && av.resolved.replace(/\\/g, '/').toLowerCase() === USER_MP3.replace(/\\/g, '/').toLowerCase(),
        `resolved=${av.resolved}`);
  check('C3 ★ 药丸又收起来了', av.pill.hidden === true && av.bar.empty === true,
        JSON.stringify({ pill: av.pill.hidden, bar: av.bar }));
  let saved = null;
  try { saved = JSON.parse(av.saved || 'null'); } catch (_e) { saved = null; }
  check('C4 ★ 用户这份选择写进了 localStorage（下次进来直接就是它）',
        saved && Number(saved.mode) === 2 && String(saved.path).replace(/\\/g, '/').toLowerCase() === USER_MP3.replace(/\\/g, '/').toLowerCase(),
        'saved=' + String(av.saved));
  let c5 = false, c5txt = '';
  for (let i = 0; i < 40; i++) {
    c5txt = await ev("document.querySelector('#audio-src').textContent");
    if (/HyuN - Grin\.mp3/.test(c5txt)) { c5 = true; break; }
    await sleep(250);
  }
  check('C5 挑完文件后播放条 <audio> 也换成同一份（另外三个视图用）', c5, c5txt);

  // ══════════════════════════ D 重进工作台：直接就是选好的那份 ══════════════
  console.log('\n--- D 重载页面（等价于重进工作台）⇒ 记忆生效 ---');
  check('D1 页面重载后钩子就绪', await boot());
  av = await AV();
  check('D2 ★ 一进来就是用户上次选的那份（不必再点「选…」）',
        av.pickFromStore === true && av.mode === 2 &&
        av.path.replace(/\\/g, '/').toLowerCase() === USER_MP3.replace(/\\/g, '/').toLowerCase(),
        JSON.stringify({ fromStore: av.pickFromStore, mode: av.mode, path: av.path }));
  check('D3 记忆恢复 ⇒ 不再自动接续工程原曲（尊重用户的选择）',
        !/job[\\/]input\.wav$/i.test(av.path), 'path=' + av.path);
  const okD = await loadMidi();
  check('D4 重载后载入 MIDI 并重算', okD);
  av = await AV();
  check('D5 ★ 音乐按这份选择接上了（就绪 + 药丸仍然收着）',
        av.ready === true && av.pill.hidden === true && av.bar.empty === true,
        JSON.stringify({ ready: av.ready, pill: av.pill.hidden, bar: av.bar.h }));

  // ══════════════════════════ E 就绪规则单测（不动重算/不渲染合成音）═════════
  console.log('\n--- E 就绪规则：自动 / 合成音 / 指定文件空路径 ---');
  const e1 = await ev(`(function(){ window.__dsh.avRaw({mode:0}); return JSON.stringify(window.__dsh.av()); })()`).then(JSON.parse);
  check('E1 自动 ⇒ 就绪、药丸收掉（默认行为没什么可提醒的）',
        e1.ready === true && e1.pill.hidden === true, JSON.stringify(e1.pill));
  const e2 = await ev(`(function(){ window.__dsh.avRaw({mode:1}); return JSON.stringify(window.__dsh.av()); })()`).then(JSON.parse);
  check('E2 合成音 ⇒ 就绪、药丸收掉', e2.ready === true && e2.pill.hidden === true, JSON.stringify(e2.pill));
  const e3 = await ev(`(function(){ window.__dsh.avRaw({mode:2, path:''}); return JSON.stringify(window.__dsh.av()); })()`).then(JSON.parse);
  check('E3 指定文件但没挑（主人截图里那个状态）⇒ 未就绪、药丸亮',
        e3.ready === false && e3.pill.hidden === false && e3.pill.warn === true && e3.bar.empty === false,
        JSON.stringify({ ready: e3.ready, pill: e3.pill, bar: e3.bar }));

  // ══════════════════════════ F 药丸藏了，改音源的入口不能丢 ══════════════════
  console.log('\n--- F 入口还在吗 ---');
  const f1 = await ev(`(function(){
    window.__dsh.avRaw({mode:2, path:${J(USER_MP3)}});        // 回到「就绪 ⇒ 药丸收着」
    const before = document.querySelector('#av-pop').classList.contains('hidden');
    window.__dsh.avPop();                                      // = Ctrl+Shift+A
    const after = document.querySelector('#av-pop').classList.contains('hidden');
    const r = document.querySelector('#av-pop').getBoundingClientRect();
    return JSON.stringify({ before, after, left: Math.round(r.left), top: Math.round(r.top), w: Math.round(r.width) });
  })()`).then(JSON.parse);
  check('F1 ★ 药丸收着时，Ctrl+Shift+A 仍能开「音源与偏移」（浮层从 hidden 变可见）',
        f1.before === true && f1.after === false, JSON.stringify(f1));
  check('F2 浮层落在窗口里（锚点回退到预览框，没飘到窗口外）',
        f1.left >= 0 && f1.top >= 0 && f1.w > 100, JSON.stringify(f1));
  check('F3 浮层里那 4 个控件都在（预览音源 / 原曲文件 / 偏移修正 / 原曲偏移 Δ）',
        await ev(`(function(){
          var k=['in-preview_audio_mode','in-preview_audio_path','in-music_delay_ms','in-preview_audio_offset_ms'];
          return k.every(function(id){ return !!document.getElementById(id); });
        })()`));

  const badErr = events.filter((s) => !/favicon/i.test(s));
  console.log('\n--- H 负向：本工程没有自带原曲 ⇒ 不许乱绑假路径 ---');
  // 场景：全新 profile（记忆清空）打开一份**不是我们流水线产物**的 MIDI
  //（示例曲在 chartgen/samples/ 下，没有 job/input.wav）。
  // 期望：探存在失败 ⇒ 什么都不做，绝不写一个假路径进去（那样后端会刷
  //       「预览音源指定的文件不存在」，看着像坏了）。
  await ev('try{localStorage.clear()}catch(e){}');
  check('H1 重载后回到「没记忆」的初始态', await boot() &&
        (await ev("(function(){try{return localStorage.getItem('adoc.avpick')}catch(e){return 'x'}})()")) === null);
  await ev('window.__chartReady=false');
  await ev(`(async()=>{ await window.__dsh.load(${J(SAMPLE_MIDI)}); return true; })()`);
  check('H2 示例 MIDI 重算落地', await waitChart());
  const h = await AV();
  check('H3 ★ 没有乱绑：路径仍为空、档位仍是默认「自动」',
        String(h.path).trim() === '' && h.mode === 0,
        JSON.stringify({ mode: h.mode, path: h.path }));
  check('H4 自动档 ⇒ 就绪、药丸收着（合成音是设计内行为）',
        h.ready === true && h.pill.hidden === true && h.bar.empty === true, JSON.stringify(h.pill));
  check('H5 ★ 也没留下「指定的文件不存在」这种假警告（探存在失败就该无声无息）',
        !(await ev("(function(){return /预览音源指定的文件不存在/.test((document.querySelector('#warnings')||{}).textContent||'')})()")),
        await ev("((document.querySelector('#warnings')||{}).textContent||'').slice(0,120)"));

  console.log('（页面日志：' + (events.join('  ／  ') || '无') + '）');
  check('G 全页无 JS 异常（favicon 404 不算）', badErr.length === 0, badErr.join(' | ') || '(无)');

  console.log('\n==== 汇总 ====');
  console.log(`${results.filter((r) => r.ok).length}/${results.length} 通过`);
  const bad = results.filter((r) => !r.ok);
  if (bad.length) console.log('失败项：\n' + bad.map((b) => ' - ' + b.name + '  ' + b.extra).join('\n'));

  try { await send('Browser.close'); } catch (_e) {}
  try { child.kill(); } catch (_e) {}
  process.exit(bad.length ? 1 : 0);
})().catch((e) => {
  console.error('ERR', (e && e.stack) || e);
  process.exit(2);
});
