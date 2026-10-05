/* xkprobe.js —— ③b 采bpm（xk base）面板取证：
 * 把「我们工作台」和「Adofai-Chart-Generator 0.5.0 + 我的补丁」两份界面分别渲染出来，
 * 逐元素 dump ③b 那一组的真实 DOM，并出特写图。
 *
 *   · 零侵入：只连主人已经起好的网关（8766），不碰 8765/8766 的占用者
 *   · 用法：node tools/xkprobe.js [url...]      默认两份都跑
 * ========================================================================== */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const URLS = process.argv.slice(2).length ? process.argv.slice(2)
  : ['http://127.0.0.1:8766/workbench/index.html',
     'http://127.0.0.1:8766/studio-skin/index.html'];
const CHROME = process.env.XK_CHROME || 'chrome';
const OUT = process.env.XK_OUT || '<REPO>\\output\\logs\\xkprobe';
const PORT = 9337;
const UDD = '<REPO>\\output\\.tmp\\xkprobe-profile';
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

/* 在页面里跑的取证函数：把 ③b 组的每个子节点连样式一起吐出来 */
const DUMP = `(function(){
  const out = { url: location.pathname, groups: [], xk: null, err: [] };
  const txt = (e) => (e.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 120);

  // 所有分组标题
  out.groups = [...document.querySelectorAll('section.grp')]
    .map((s) => txt(s.querySelector('h4')));

  // 找 ③b 那一组（无论它在 #groups 还是 #groups-xk 里）
  let sec = null, host = null;
  for (const s of document.querySelectorAll('section.grp')) {
    const t = txt(s.querySelector('h4'));
    if (/③b|采bpm/.test(t)) { sec = s; host = s.parentElement && s.parentElement.id; break; }
  }
  if (sec) {
    const kids = [...sec.querySelectorAll('*')].filter((e) =>
      e.classList.contains('field') || e.classList.contains('info') ||
      e.classList.contains('hint') || e.classList.contains('regions') ||
      e.tagName === 'BUTTON' || e.tagName === 'SELECT' || e.tagName === 'INPUT' ||
      e.id === 'lbl-xk' || e.id === 'lst-xk');
    out.xk = {
      host: host || '(?)',
      head: txt(sec.querySelector('h4')),
      count: kids.length,
      dom: kids.map((e) => {
        const r = e.getBoundingClientRect();
        return {
          tag: e.tagName.toLowerCase(),
          cls: e.className || '',
          id: e.id || '',
          type: e.type || '',
          text: txt(e),
          html: (e.innerHTML || '').replace(/\\s+/g, ' ').slice(0, 90),
          box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)].join(','),
          vis: !!(r.width && r.height) && getComputedStyle(e).display !== 'none',
        };
      }),
    };
  }
  // 浮窗本体（他那边有 #fl-xk）
  const fl = document.getElementById('fl-xk');
  if (fl) {
    const r = fl.getBoundingClientRect();
    const gx = document.getElementById('groups-xk');
    out.flx = {
      box: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)].join(','),
      disp: getComputedStyle(fl).display,
      open: fl.classList.contains('hidden') ? 'hidden' : 'visible',
      groupsXkHtmlLen: gx ? gx.innerHTML.length : -1,
      groupsXkSecs: gx ? gx.querySelectorAll('section.grp').length : -1,
      headText: txt(fl.querySelector('.fhead')),
    };
  }
  return out;
})()`;

(async () => {
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
      events.push('LOG ' + d.params.entry.text);
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

  for (const url of URLS) {
    const tag = url.includes('workbench') ? 'workbench' : 'studio-skin';
    events.length = 0;
    await send('Page.navigate', { url });
    await sleep(1500);
    await ev('try { localStorage.clear(); } catch (_e) {} 1');
    await send('Page.reload', { ignoreCache: true });
    let n = 0;
    for (let i = 0; i < 60; i++) {
      n = await ev(`document.querySelectorAll('section.grp').length`);
      if (n > 0) break;
      await sleep(500);
    }
    await sleep(1200);

    console.log('\n' + '='.repeat(78));
    console.log('### ' + tag + '   ' + url);
    console.log('分组数 = ' + n);
    const d = await ev(DUMP);
    console.log('全部组标题：');
    (d.groups || []).forEach((t, i) => console.log('   ' + String(i + 1).padStart(2) + '. ' + t));
    console.log('\n③b 组：' + (d.xk ? '找到（宿主 ' + d.xk.host + '）' : '★ 没找到 ★'));
    if (d.xk) {
      console.log('  标题: ' + d.xk.head);
      console.log('  子节点 ' + d.xk.count + ' 个：');
      d.xk.dom.forEach((e) => {
        console.log('   ' + (e.vis ? '·' : '×') + ' <' + e.tag + (e.type ? ':' + e.type : '')
          + (e.id ? ' #' + e.id : '') + (e.cls ? ' .' + e.cls : '') + '>');
        console.log('        box=' + e.box + '  text="' + e.text + '"');
      });
    }
    if (d.flx) {
      console.log('\n#fl-xk 浮窗：' + JSON.stringify(d.flx));
    } else {
      console.log('\n#fl-xk 浮窗：不存在（这份界面没有浮窗）');
    }
    console.log('页面异常 ' + events.length + ' 条' + (events.length ? '：' : ''));
    events.slice(0, 6).forEach((e) => console.log('   ' + e));

    // ── 断言 A：③b 面板该有的东西在不在（"少了这个"就指这里） ──
    const dom = (d.xk && d.xk.dom) || [];
    const byId = (id) => dom.find((e) => e.id === id) || {};
    const infoTxt = String(byId('info-xk_span_help').text || '');
    check(`${tag}: ③b 采bpm（xk base · 大直线）组存在`, !!d.xk,
      d.xk ? '宿主 ' + d.xk.host : '没找到');
    check(`${tag}: 渲染出「采bpm（xk base）」下拉`,
      dom.some((e) => e.id === 'in-xk_base' && e.tag === 'select'));
    check(`${tag}: 渲染出 tbpm 数字框`,
      dom.some((e) => e.id === 'in-xk_tbpm' && e.tag === 'input'));
    // ★ `xk_span_help` 是 info 型字段、`default` 是空串（正文写在 `help` 里）⇒ 只印 `default`
    //   就渲染成一条**空框**。2026-09-19 增量改造：两边都加了「default 空则用 label+help」的
    //   回退（他原版这一行也一并修了）⇒ 这里对两份界面都当真断言，不再打 NOTE 放过。
    check(`${tag}: 「区间外 →」说明行有正文（不是一条空框）`, infoTxt.length >= 8,
      `len=${infoTxt.length} text="${infoTxt.slice(0, 44)}"`);

    // ── 断言 A2：③b **挂在哪儿**（2026-09-19 改造：从「大直线（采bpm）」浮窗搬回右栏检查器） ──
    //   旧况：他原版把 ③b 塞进 `#fl-xk`（默认写死 340×250、`layout.js` applyLayout 每次都把
    //   display 重写成 flex），这一组内容比可视区高 100+px ⇒「＋加区间」和区间列表被折在外面，
    //   主人看到的就是"这块板块没了"。所以这里必须钉住：宿主是 #groups、且不在 .fbody 里。
    const place = await ev(`(function(){
      let sec = null;
      for (const s of document.querySelectorAll('section.grp')) {
        const t = (s.querySelector('h4') || {}).textContent || '';
        if (/③b|采bpm/.test(t)) { sec = s; break; }
      }
      const fl = document.getElementById('fl-xk');
      const gx = document.getElementById('groups-xk');
      const cs = fl ? getComputedStyle(fl) : null;
      const rr = fl ? fl.getBoundingClientRect() : null;
      const btn = sec ? [...sec.querySelectorAll('button')].find((x) => /加区间/.test(x.textContent)) : null;
      return {
        host: (sec && sec.parentElement) ? (sec.parentElement.id || '(无id)') : '(没找到)',
        hasFloat: !!fl,
        flEmpty: !!(fl && fl.classList.contains('fl-empty')),
        flDisplay: cs ? cs.display : '(无浮窗)',
        flBox: rr ? Math.round(rr.width) + 'x' + Math.round(rr.height) : '-',
        gxSecs: gx ? gx.querySelectorAll('section.grp').length : -1,
        inFloat: !!(btn && btn.closest('.fbody')),
      };
    })()`);
    console.log('③b 落位：' + JSON.stringify(place));
    check(`${tag}: ③b 挂在右栏检查器 #groups（不再进 250px 的浮窗滚动体）`,
      place.host === 'groups' && place.inFloat === false,
      `host=${place.host} inFloatBody=${place.inFloat}`);
    check(`${tag}: 空的「大直线（采bpm）」浮窗已隐藏（不留空窗飘着）`,
      !place.hasFloat || (place.flEmpty && place.flDisplay === 'none'),
      `flEmpty=${place.flEmpty} display=${place.flDisplay} box=${place.flBox}`);
    check(`${tag}: 浮窗里已无 ③b 分组（没被渲染两遍）`,
      place.gxSecs <= 0, `#groups-xk 里 section 数=${place.gxSecs}`);

    // ③b 特写（截它所在的那个容器）
    const clip = await ev(`(function(){
      const host = document.getElementById('groups-xk');
      if (host && host.querySelector('section.grp')) {
        const r = host.getBoundingClientRect();
        return { x: Math.max(0, r.left - 6), y: Math.max(0, r.top - 6),
                 width: Math.min(700, r.width + 12), height: Math.min(800, r.height + 12) };
      }
      for (const s of document.querySelectorAll('section.grp')) {
        const t = (s.querySelector('h4') || {}).textContent || '';
        if (/③b|采bpm/.test(t)) {
          const r = s.getBoundingClientRect();
          return { x: Math.max(0, r.left - 6), y: Math.max(0, r.top - 6),
                   width: Math.min(700, r.width + 12), height: Math.min(800, r.height + 12) };
        }
      }
      return null;
    })()`);
    if (clip && clip.width > 10 && clip.height > 10) await shot('xk-' + tag, clip, 2);
    else console.log('（没能定位到 ③b 容器的框，跳过特写）');

    // ── 点一下 ③b 那一组里的「＋ 加区间（播放头）」：真能加出一行可编辑的区间吗？ ──
    //    🔴 必须**限定在 ③b 组内**找按钮：左栏「来源与段」也有一颗「＋ 加区间」，
    //      全局 find 会先抓到它 ⇒ 点了它当然不会动 #lst-xk（探针自己的坑，踩过）。
    const XKB = `(function(){
      let sec = document.getElementById('groups-xk');
      sec = (sec && sec.querySelector('section.grp')) || null;
      if (!sec) {
        for (const s of document.querySelectorAll('section.grp')) {
          const t = (s.querySelector('h4') || {}).textContent || '';
          if (/③b|采bpm/.test(t)) { sec = s; break; }
        }
      }
      if (!sec) return { found: false, why: '没找到 ③b 组' };
      const b = [...sec.querySelectorAll('button')].find((x) => /加区间/.test(x.textContent));
      if (!b) return { found: false, why: '③b 组里没有「＋ 加区间」按钮' };
      // ★ 先滚进视野再量坐标：他的「大直线」浮窗只有 250px 高，按钮常常在可视区**外**，
      //   直接按 rect 点会点到背后的画布上（elementFromPoint 会暴露这点）。
      try { b.scrollIntoView({ block: 'center' }); } catch (_e) { /* 老引擎 */ }
      const q = b.getBoundingClientRect();
      return { found: true, id: b.id || '(无 id)', x: Math.round(q.left + q.width / 2),
               y: Math.round(q.top + q.height / 2),
               before: sec.querySelectorAll('#lst-xk .region').length,
               emptyHint: (sec.querySelector('#lst-xk') || {}).textContent || '' };
    })()`;
    const clicked = await ev(XKB);
    console.log('\n③b 组里的「＋ 加区间（播放头）」按钮：' + JSON.stringify(clicked.found ? { id: clicked.id, before: clicked.before } : clicked));
    console.log('   点前 #lst-xk 文字="' + String(clicked.emptyHint || '').replace(/\s+/g, ' ').trim().slice(0, 90) + '"');
    check(`${tag}: ③b 组里有「＋ 加区间（播放头）」按钮`, !!clicked.found,
      clicked.found ? 'id=' + clicked.id : String(clicked.why || ''));
    check(`${tag}: 空列表时有空状态提示（不是一片空白）`,
      /没有区间/.test(String(clicked.emptyHint || '')),
      String(clicked.emptyHint || '').replace(/\s+/g, ' ').slice(0, 44));
    if (clicked.found) {
      // 用 CDP 真派发鼠标事件（模拟真人点击，能暴露手势相关的问题）
      await send('Input.dispatchMouseEvent', { type: 'mousePressed', x: clicked.x, y: clicked.y, button: 'left', clickCount: 1 });
      await send('Input.dispatchMouseEvent', { type: 'mouseReleased', x: clicked.x, y: clicked.y, button: 'left', clickCount: 1 });
      await sleep(2200);
      // ★ 真鼠标事件没生效时，先分清是「点不到」还是「点了没反应」：
      //   elementFromPoint 告诉我们落点到底是谁；程序化 .click() 再复测一次。
      const probe1 = await ev(`(function(){
        const el = document.elementFromPoint(${clicked.x}, ${clicked.y});
        return el ? el.tagName.toLowerCase() + (el.id ? '#' + el.id : '')
          + (el.className ? '.' + String(el.className).split(' ')[0] : '')
          + ' text=' + JSON.stringify((el.textContent || '').trim().slice(0, 20)) : '(null)';
      })()`);
      console.log('   鼠标落点 elementFromPoint = ' + probe1);
      check(`${tag}: 真鼠标点击落在按钮上（没被别的层挡住）`, /加区间/.test(String(probe1)), probe1);
      const after = await ev(`(function(){
        const box = document.getElementById('lst-xk');
        if (!box) return { box: false };
        const rs = [...box.querySelectorAll('.region')];
        return {
          rows: rs.length,
          text: (box.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 160),
          ctrls: rs.length ? [...rs[0].querySelectorAll('input,select,button')]
            .map((e) => e.tagName.toLowerCase() + (e.type ? ':' + e.type : '')
              + (e.id ? '#' + e.id : '') + (e.className ? '.' + String(e.className).split(' ')[0] : '')) : [],
          status: (document.getElementById('status') || {}).textContent || '',
        };
      })()`);
      // 真鼠标事件没生效时：程序化 b.click() 复测一次 —— 区分「点不到」与「点了没反应」
      let after2 = null;
      if (!after.rows) {
        await ev(`(function(){
          let sec = document.getElementById('groups-xk');
          sec = (sec && sec.querySelector('section.grp')) || null;
          if (!sec) {
            for (const s of document.querySelectorAll('section.grp')) {
              const t = (s.querySelector('h4') || {}).textContent || '';
              if (/③b|采bpm/.test(t)) { sec = s; break; }
            }
          }
          if (!sec) return 0;
          const b = [...sec.querySelectorAll('button')].find((x) => /加区间/.test(x.textContent));
          if (b) b.click();
          return 1;
        })()`);
        await sleep(1800);
        after2 = await ev(`(function(){
          const box = document.getElementById('lst-xk');
          if (!box) return { rows: -1, ctrls: [], status: '' };
          const rs = [...box.querySelectorAll('.region')];
          return {
            rows: rs.length,
            ctrls: rs.length ? [...rs[0].querySelectorAll('input,select,button')]
              .map((e) => e.tagName.toLowerCase() + (e.type ? ':' + e.type : '')
                + (e.className ? '.' + String(e.className).split(' ')[0] : '')) : [],
            status: (document.getElementById('status') || {}).textContent || '',
          };
        })()`);
        console.log('   程序化 b.click() 之后：rows=' + after2.rows
          + '  status="' + String(after2.status || '').slice(0, 50) + '"');
      }
      const eff = (after2 && after2.rows) ? after2 : after;   // 判定用「能达成」的那次
      console.log('点完之后 #lst-xk：rows=' + after.rows + '  ctrls=[' + (after.ctrls || []).join(' ') + ']');
      console.log('   列表文字="' + (after.text || '') + '"');
      console.log('   状态栏="' + (after.status || '').slice(0, 90) + '"');
      console.log('本步页面异常：' + (events.length ? events.slice(-2).join(' | ') : '无'));
      // ── 断言 B：点一下真能加出**可编辑**的区间行 ──
      const cs = (eff.ctrls || []).join(' ');
      check(`${tag}: 点一下能加出区间行`, eff.rows === 1, `rows=${eff.rows}`);
      // ★ 断言按**结构**而不是按类名（他那边单位下拉没有 `.xkunit` 这个类）：
      //   名字输入 + ✕ 删除 + 2 个数字框 + 3 个下拉（单位×2 + N）+ 多押轨输入。
      check(`${tag}: 行内控件齐全（名字/✕/起止×2/单位×2/N/多押轨）`,
        /input:text\.rname/.test(cs) && /\.rdel/.test(cs)
        && (cs.match(/input:number/g) || []).length >= 2
        && (cs.match(/select/g) || []).length >= 3
        && /input:text/.test(cs.replace(/input:text\.rname/, '')), cs);
      check(`${tag}: 加了区间后状态栏有回话`, /已加采bpm 区间/.test(String(eff.status || '')),
        String(eff.status || '').slice(0, 40));
      const excs = events.filter((e) => e.startsWith('EXC'));
      check(`${tag}: 这一步没有 JS 异常`, excs.length === 0, excs.slice(0, 2).join(' | ') || '无');
      const clip2 = await ev(`(function(){
        const box = document.getElementById('lst-xk');
        const sec = box && box.closest('section.grp');
        if (!sec) return null;
        const r = sec.getBoundingClientRect();
        return { x: Math.max(0, r.left - 6), y: Math.max(0, r.top - 6),
                 width: Math.min(700, r.width + 12), height: Math.min(800, r.height + 12) };
      })()`);
      if (clip2 && clip2.width > 10) await shot('xk-' + tag + '-added', clip2, 2);
    } else {
      check(`${tag}: 点一下能加出区间行`, false, '按钮没找到，没法点');
    }
  }

  console.log('\n==== 汇总 ====');
  console.log(`${PASS}/${PASS + FAIL} 通过` + (FAIL ? `（${FAIL} 条失败）` : ''));
  try { child.kill(); } catch (_e) {}
  await sleep(300);
  process.exit(FAIL ? 1 : 0);
})().catch((e) => { console.error('FATAL ' + (e && e.stack || e)); process.exit(1); });
