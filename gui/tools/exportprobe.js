/* exportprobe.js —— 「导出点了没反应」的端到端验证（零 GUI、零侵入）
 *
 * 背景：2026-09-20 主人报「工作台导出点了没反应」。真凶是宿主 native_open_dir()
 *       抛 ctypes TypeError（见 gui/tools/test-nativedir.py），异常被吃 → dsh_reply ok:false
 *       → 前端 Promise reject → 没人接 → unhandledrejection → 界面零反馈。
 *
 * 本探针验两件事：
 *   A. 失败必须**看得见**：dsh 通道缺失 / 宿主 reject 时，界面要出现 toast + 报告带状态行
 *      （旧代码这两条都是静默的 —— 就是主人看到的现象）
 *   B. 把宿主选目录这一步替换成"已选好目录"，点导出要真的把谱面写出来
 *      （证明假故障之外，后半条链路 /api/export 是通的）
 *
 * 用法（先起一个**隔离**的 dev 实例，别碰主人正在用的 8766）：
 *   CHARTGEN_PORT=8895 STUDIO_GATEWAY_PORT=8896 \
 *     python313/python.exe gui/dev_serve_workbench.py &
 *   node gui/tools/exportprobe.js http://127.0.0.1:8896/workbench/index.html
 * ========================================================================== */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const URL_ = process.argv[2] || 'http://127.0.0.1:8896/workbench/index.html';
const SAMPLE = process.argv[3] || '<REPO>\\chartgen\\samples\\Automaton_Waltz.mid';
const OUTDIR = process.argv[4] || '<REPO>\\output\\.tmp\\exportprobe';
const CHROME = process.env.EXPORT_CHROME || 'chrome';
const WRITER_PY = process.env.EXPORT_WRITER || '<REPO>\\chartgen\\core\\writer.py';
const PORT = 9341;
const UDD = '<REPO>\\output\\.tmp\\exportprobe-profile';
const W = 1560, H = 940;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let PASS = 0, FAIL = 0;
const check = (name, ok, detail) => {
  (ok ? PASS++ : FAIL++);
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? '   [' + detail + ']' : ''}`);
};
const get = (p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: PORT, path: p }, (r) => {
    let b = ''; r.on('data', (c) => (b += c)); r.on('end', () => res(b));
  }).on('error', rej);
});
const esc = (s) => s.replace(/\\/g, '\\\\').replace(/'/g, "\\'");

async function main() {
  // 干净的起点
  try { fs.rmSync(OUTDIR, { recursive: true, force: true }); } catch (_e) {}
  try { fs.rmSync(UDD, { recursive: true, force: true }); } catch (_e) {}

  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--mute-audio', '--force-color-profile=srgb',
    `--remote-debugging-port=${PORT}`, `--user-data-dir=${UDD}`,
    `--window-size=${W},${H}`, 'about:blank',
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
  await send('Runtime.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: 1, mobile: false });

  console.log('='.repeat(78));
  console.log('### 导出链路端到端探针');
  console.log('   页面 ' + URL_);
  console.log('   示例 ' + SAMPLE);
  console.log('   导出目标 ' + OUTDIR);
  console.log('='.repeat(78));

  await send('Page.navigate', { url: URL_ });
  await sleep(1500);
  let n = 0;
  for (let i = 0; i < 60; i++) {
    n = await ev("document.querySelectorAll('section.grp').length");
    if (n > 0) break;
    await sleep(500);
  }
  check('P0 工作台渲染出检查器分组', n > 0, 'groups=' + n);

  // ---------- 载入示例并出谱 ----------
  const loaded = await ev("(async()=>{ try{ await window.__dsh.load('" + esc(SAMPLE) + "'); return 'ok'; }catch(e){ return 'ERR '+(e&&e.message); } })()");
  check('P1 载入示例 MIDI', loaded === 'ok', loaded);

  let ready = false;
  for (let i = 0; i < 90; i++) {
    ready = await ev('!!window.__chartReady');
    if (ready) break;
    await sleep(500);
  }
  const hasChart = await ev('!!(window.__dsh && window.__dsh.state && window.__dsh.status && window.__dsh.status().loaded)');
  check('P2 谱面已生成（__chartReady）', ready === true, 'loaded=' + hasChart);

  const click = () => ev("document.getElementById('btn-export').click(); 1");
  const readUi = () => ev("(function(){var s=document.getElementById('status');var t=document.getElementById('toast');"
    + "return {status:(s?s.textContent:'').replace(/\\s+/g,' ').trim(), toast:(t?t.textContent:''), toastOn:!!(t&&t.classList.contains('on'))};})()");

  // ---------- A. 通道缺失（旧代码：静默） ----------
  events.length = 0;
  await ev("window.__savedDsh = window.dsh; delete window.dsh; 1");
  await click();
  await sleep(400);
  let ui = await readUi();
  check('A1 没有 dsh 通道时，界面有可见反馈（不再静默）',
    /不能弹目录选择框|导出失败/.test(ui.status), 'status=' + JSON.stringify(ui.status));
  check('A1b 同时弹了 toast', ui.toastOn && ui.toast.length > 0, 'toast=' + JSON.stringify(ui.toast));

  // ---------- B. 宿主 reject（= 今天真凶的形状） ----------
  await ev("window.dsh = window.__savedDsh; window.dsh.openDir = function(){ return Promise.reject(new Error('incompatible types, c_wchar_Array_260 instance instead of c_wchar_p instance')); }; 1");
  await click();
  await sleep(500);
  ui = await readUi();
  check('B1 宿主 reject 时界面报出原因（旧代码就是这里一点反应都没有）',
    /导出失败/.test(ui.status) && /incompatible types/.test(ui.status), 'status=' + JSON.stringify(ui.status.slice(0, 90)));
  check('B1b 同时弹了 toast', ui.toastOn, 'toast=' + JSON.stringify(ui.toast.slice(0, 70)));

  // ---------- C. 选好目录 → 真的导出 ----------
  await ev("window.dsh.openDir = function(){ return Promise.resolve('" + esc(OUTDIR) + "'); }; 1");
  await click();
  for (let i = 0; i < 60; i++) {
    ui = await readUi();
    if (/导出到|导出失败|失败/.test(ui.status)) break;
    await sleep(500);
  }
  check('C1 导出成功并回报路径', /导出到/.test(ui.status), 'status=' + JSON.stringify(ui.status.slice(0, 120)));

  // ★ 导出会在所选目录里**再建一个曲目文件夹**（export.song 决定名字），所以要往里找一层
  const walk = (d, acc) => {
    if (!fs.existsSync(d)) return acc;
    for (const e of fs.readdirSync(d, { withFileTypes: true })) {
      const p = path.join(d, e.name);
      if (e.isDirectory()) walk(p, acc); else acc.push(p.slice(OUTDIR.length + 1));
    }
    return acc;
  };
  const files = walk(OUTDIR, []);
  check('C2 磁盘上真的写出了谱面文件（含曲目子目录）', files.some((f) => /\.adofai$/i.test(f)),
    OUTDIR + ' -> ' + JSON.stringify(files));
  console.log('     产物: ' + JSON.stringify(files));

  // ---------- C3/C4. settings 白名单（2026-09-20 主人：多余的删掉） ----------
  //   白名单现从 chartgen/core/writer.py 里抠出来读 ⇒ 探针与实现**同一个源**，不会各说各话。
  const afPath = files.filter((f) => /\.adofai$/i.test(f))
    .map((f) => path.join(OUTDIR, f))
    .sort((a, b) => fs.statSync(b).size - fs.statSync(a).size)[0];
  let keys = [];
  try {
    const raw = JSON.parse(fs.readFileSync(afPath, 'utf8').replace(/^\uFEFF/, ''));
    keys = Object.keys(raw.settings || {});
  } catch (e) { console.log('     读谱面失败: ' + e.message); }
  const wlSrc = fs.readFileSync(WRITER_PY, 'utf8');
  const i0 = wlSrc.indexOf('EXPORT_SETTINGS_KEYS = (');
  const WL = i0 < 0 ? [] : (wlSrc.slice(i0, wlSrc.indexOf('\n)', i0)).match(/"([^"]+)"/g) || [])
    .map((s) => s.slice(1, -1));
  const extra = keys.filter((k) => !WL.includes(k));
  const miss = WL.filter((k) => !keys.includes(k));
  check('C3 导出的 settings 只含白名单里的键', keys.length > 0 && WL.length > 0
    && extra.length === 0 && miss.length === 0,
  `${path.basename(path.dirname(afPath))}/main.adofai | 文件 ${keys.length} 键 vs 白名单 ${WL.length} 键`
    + ` | 多=${JSON.stringify(extra)} 缺=${JSON.stringify(miss)}`);
  check('C4 settings 的键顺序与白名单一致（可 diff）', keys.join(',') === WL.join(','),
    keys.slice(0, 3).join(',') + ' …');
  console.log('     导出的 settings 键: ' + keys.join(', '));

  check('D1 全过程没有未捕获的 Promise 异常', events.filter((e) => /EXC/.test(e)).length === 0,
    events.slice(0, 2).join(' | ') || '0 条');

  console.log('\n==== 通过 ' + PASS + ' / 失败 ' + FAIL + ' ====');
  try { ws.close(); } catch (_e) {}
  chrome.kill();
  return FAIL ? 1 : 0;
}

main().then((c) => process.exit(c)).catch((e) => { console.error('探针自身出错: ' + (e && e.stack || e)); process.exit(2); });
