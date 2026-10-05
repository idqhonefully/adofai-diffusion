/* jsdiag.js —— 页面"卡死"时的现场取证：连上 CDP，发 Debugger.pause 把它按停，
 * 把当前调用栈打出来。死循环卡死时，栈里直接就写着它在哪一行转不出去。
 *
 * 为什么不用 pageeval 加 try：主线程被占死时 Runtime.evaluate 根本排不上队，
 * 会永远不返回（本轮就是"探针跑 3 分钟没输出"）。只有调试器的 pause 能插进去。
 *
 * 顺带收集：页面异常（Runtime.exceptionThrown）、console 输出（含报错）、
 * 以及 evaluate 的带超时取值。给 eval 表达式时可顺手看几个值。
 *
 * 用法：node tools/jsdiag.js <url> [表达式]
 */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');

const URL_ = process.argv[2];
const EXPR = process.argv[3] || '';
const CHROME = process.env.ADOFI_CHROME || 'chrome';
const PORT = 9339;
const UDD = '<REPO>\\output\\.tmp\\jsdiag-profile';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = (p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: PORT, path: p }, (r) => {
    let b = ''; r.on('data', (c) => (b += c)); r.on('end', () => res(b));
  }).on('error', rej);
});

const STUB = `
window.__NO_NAVIND = true;   /* ⏱ 二分开关：设了就不初始化侧栏指示条，用来定位卡死 */
window.__SENT = []; window.__LISTENERS = [];
window.chrome = window.chrome || {};
window.chrome.webview = {
  postMessage: function (s) { window.__SENT.push(s); },
  addEventListener: function (t, f) { if (t === 'message') window.__LISTENERS.push(f); }
};
window.dsh = window.dsh || {};
window.__deliver = function (o) {
  var s = typeof o === 'string' ? o : JSON.stringify(o);
  window.__LISTENERS.forEach(function (f) { try { f({ data: s }); } catch (e) {} });
};
`;

(async () => {
  fs.mkdirSync(UDD, { recursive: true });
  const child = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--mute-audio', '--force-device-scale-factor=1',
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${UDD}`,
    '--window-size=1560,940', 'about:blank',
  ], { stdio: 'ignore', windowsHide: true });

  let list = [];
  for (let i = 0; i < 80; i++) {
    try { list = JSON.parse(await get('/json/list')); if (list.some((t) => t.type === 'page')) break; } catch (_e) { }
    await sleep(250);
  }
  const page = list.find((t) => t.type === 'page');
  if (!page) { throw new Error('连不上 headless'); }

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  const waiters = new Map(); let seq = 0;
  const errors = []; const logs = []; let pausedParams = null;

  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && waiters.has(m.id)) {
      const w = waiters.get(m.id); waiters.delete(m.id);
      if (m.error) w.rej(new Error(JSON.stringify(m.error))); else w.res(m.result);
      return;
    }
    if (m.method === 'Runtime.exceptionThrown') {
      const d = (m.params.exceptionDetails || {});
      errors.push((d.exception && d.exception.description) || d.text || '(无描述)');
    }
    if (m.method === 'Runtime.consoleAPICalled') {
      logs.push(m.params.type + ': ' + m.params.args.map((a) => a.value !== undefined ? a.value : (a.description || a.type)).join(' '));
    }
    if (m.method === 'Log.entryAdded') {
      logs.push('[' + m.params.entry.level + '] ' + m.params.entry.text);
    }
    if (m.method === 'Debugger.paused' && !pausedParams) { pausedParams = m.params; }
  });

  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  const send = (method, params) => new Promise((res, rej) => {
    const id = ++seq; waiters.set(id, { res, rej });
    ws.send(JSON.stringify({ id, method, params: params || {} }));
  });

  await send('Runtime.enable');
  await send('Log.enable');
  await send('Debugger.enable');
  await send('Page.enable');
  await send('Page.addScriptToEvaluateOnNewDocument', { source: STUB });
  await send('Emulation.setDeviceMetricsOverride', { width: 1560, height: 940, deviceScaleFactor: 1, mobile: false });

  await send('Page.navigate', { url: URL_ });
  await sleep(3000);

  // ---- 关键一步：把主线程按停，抓栈 ----
  try { await send('Debugger.pause'); } catch (_e) { /* 无所谓 */ }
  for (let i = 0; i < 40 && !pausedParams; i++) { await sleep(250); }
  if (pausedParams) {
    console.log('=== 主线程按停时的调用栈（最上面一帧就是它卡住的地方）===');
    pausedParams.callFrames.slice(0, 12).forEach((f, i) => {
      const u = (f.url || '').split('/').slice(-1)[0] || '(无 url)';
      console.log(`  #${i} ${f.functionName || '(匿名)'}  @ ${u}:${f.location.lineNumber + 1}:${f.location.columnNumber}`);
    });
  } else {
    console.log('=== 没能按停（可能页面没在跑 JS）===');
  }
  /* 🔴🔴 不管抓没抓到，都必须 resume。
     这是本工具**自己踩过**的坑：只在"抓到栈"那条分支里 resume ⇒ 没抓到时页面就一直停在
     暂停状态，之后任何 Runtime.evaluate 都超时 —— 看上去和"页面卡死"一模一样，
     于是我把"页面正常"误判成"页面卡死"，白绕了一大圈（还去查了服务器和 profile 锁）。
     诊断工具本身出错，比没有工具更糟：它会给你一个**看似有理的假证据**。 */
  try { await send('Debugger.resume'); } catch (_e) { /* 没暂停也无妨 */ }

  // ---- 带超时的取值（卡死时它会超时，但异常/日志已经收到）----
  if (EXPR) {
    console.log('');
    console.log('=== 取值 ===');
    const evalP = send('Runtime.evaluate', {
      expression: '(async function(){' + EXPR + '})()', returnByValue: true, awaitPromise: true,
    });
    const r = await Promise.race([evalP, sleep(8000).then(() => 'TIMEOUT')]);
    if (r === 'TIMEOUT') { console.log('取值超时（主线程被占死）'); }
    else if (r.exceptionDetails) { console.log('抛异常：' + ((r.exceptionDetails.exception || {}).description || r.exceptionDetails.text)); }
    else { console.log(typeof r.result.value === 'string' ? r.result.value : JSON.stringify(r.result.value, null, 1)); }
  }

  console.log('');
  console.log('=== 页面异常 (' + errors.length + ') ===');
  errors.slice(0, 10).forEach((e) => console.log('  ' + String(e).split('\n').slice(0, 4).join('\n  ')));
  console.log('=== console (' + logs.length + '，只列最后 12 条) ===');
  logs.slice(-12).forEach((l) => console.log('  ' + l));

  try { ws.close(); } catch (_e) { }
  try { child.kill(); } catch (_e) { }
  process.exit(0);
})().catch((e) => { console.error('【脚本失败】' + (e && e.stack || e)); process.exit(1); });
