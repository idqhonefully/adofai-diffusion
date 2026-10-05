/* importprobe.js —— 导入页（gui/index.html）新增功能的交互自检
 * ============================================================================
 * 覆盖 schedule.txt 的第 1（工作台入口）/ 2（界面材质）/ 3（explain.md）/
 * 9（训练页）/ 10（选型说明）条。
 *
 * 为什么能用 headless 测：
 *   导入页与宿主的通信全走 `chrome.webview.postMessage` / `message` 事件。
 *   这里用 CDP 的 Page.addScriptToEvaluateOnNewDocument **在页面脚本之前**
 *   塞一个假桥，把页面发出的消息全部收进 __SENT；再用 page 里暴露的
 *   `window.__devHostMsg()`（与真宿主消息走**同一个** onHostMessage）回灌。
 *   两边不是各自一份副本 —— 否则就是在测副本。
 *
 * 不碰主人的窗口：只读网关的静态文件，不调用 run_selftest.py（那个会 _free_port
 * 把主人正开着的 8765/8766 进程杀掉）。
 *
 * 用法：node tools/importprobe.js [url] [chrome.exe]
 *   默认 url = http://127.0.0.1:8766/index.html
 * ============================================================================
 */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');

const URL_ = process.argv[2] || 'http://127.0.0.1:8766/index.html';
const CHROME = process.argv[3] || 'chrome';
const PORT = 9334;
const UDD = process.env.IMPORTPROBE_UDD || '<REPO>\\output\\.tmp\\importprobe-profile';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = (p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: PORT, path: p }, (r) => {
    let b = ''; r.on('data', (c) => (b += c)); r.on('end', () => res(b));
  }).on('error', rej);
});

const results = [];
function check(name, ok, extra) {
  results.push({ name, ok: !!ok });
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${extra !== undefined ? '   ' + extra : ''}`);
}

const BRIDGE = `
window.__SENT = [];
window.__HANDLERS = [];
window.__FETCHED = [];
window.chrome = window.chrome || {};
window.chrome.webview = {
  postMessage: function (s) {
    try { window.__SENT.push(JSON.parse(s)); }
    catch (e) { window.__SENT.push({ __raw: String(s) }); }
  },
  addEventListener: function (t, fn) { if (t === 'message') window.__HANDLERS.push(fn); }
};
// ★ 2026-09-28 列表口子改造后：前端改从 chartgen 网关 fetch /api/projects
//   （由 C# Gateway 反代到 sidecar），静态测试服没有真后端 ⇒ 这里把该请求桩掉，
//   ① 不真打网络（避免 404 被"全程没有 JS 运行时异常"当成异常）；
//   ② 返回 ok:false，模拟"离线/后端没给列表"的真实场景，让页面走 projectsFail()
//      分支（显示明确报错文案，而非静默空白）—— 这恰恰是下面两个断言要验的点。
var __origFetch = (window.fetch ? window.fetch.bind(window) : null);
window.fetch = function (input, init) {
  var url = (input && input.url) ? input.url : String(input);
  try { window.__FETCHED.push(url); } catch (e) {}
  if (/api\\/projects/.test(url)) {
    return Promise.resolve(new Response(
      JSON.stringify({ ok: false, error: '测试桩：后端未返回工程列表' }),
      { status: 200, headers: { 'Content-Type': 'application/json' } }
    ));
  }
  return __origFetch ? __origFetch(input, init)
                     : Promise.reject(new Error('no native fetch'));
};
`;

const MIDI_ITEMS = [
  {
    name: 'Demo Song_stems_combined.mid', song: 'Demo Song', path: 'D:\\out\\.work\\Demo Song_1\\Demo Song_stems_combined.mid',
    job: 'D:\\out\\.work\\Demo Song_1', size: '86.0 KB', bytes: 88064, mtime: 1789901575000,
    audio: 'D:\\out\\.work\\Demo Song_1\\job\\input.wav', has_chart: true,
  },
  {
    name: 'No Audio_stems_combined.mid', song: 'No Audio', path: 'D:\\out\\.work\\No Audio_2\\No Audio_stems_combined.mid',
    job: 'D:\\out\\.work\\No Audio_2', size: '12.0 KB', bytes: 12288, mtime: 1789800000000,
    audio: '', has_chart: false,
  },
];

const MD_SAMPLE = [
  '# 标题一',
  '',
  '普通段落，含**粗体**与`行内码`。',
  '',
  '## 小节',
  '',
  '- 第一项',
  '- 第二项',
  '',
  '| 步骤 | 工具 |',
  '|---|---|',
  '| ① | ffmpeg |',
  '| ② | BS-RoFormer |',
  '',
  '```',
  'code block line',
  '```',
  '',
  '> 引用一句',
  '',
  '---',
  '',
  '<script>window.__XSS = 1</script>',
].join('\n');

(async () => {
  fs.mkdirSync(UDD, { recursive: true });
  const child = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--mute-audio', '--disable-http-cache',
    `--remote-debugging-port=${PORT}`,
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
      // 带上 url：不带就只能看到 "404 (File not found)"，认不出是哪个资源（本轮就是 favicon）
      events.push('LOG ' + m.params.entry.text + ' @' + (m.params.entry.url || '?'));
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
  const feed = (obj) => ev(`window.__devHostMsg(${JSON.stringify(JSON.stringify(obj))})`);
  const probe = async () => JSON.parse(await ev('window.__domProbe()'));
  const sent = () => ev('JSON.stringify(window.__SENT)').then((s) => JSON.parse(s));
  const clearSent = () => ev('window.__SENT.length = 0');
  const clickNav = (p) => ev(`(function(){var n=document.querySelector('.nav[data-page="${p}"]');if(!n)return 'NO_NAV';n.click();return 'OK';})()`);
  const clickEl = (sel) => ev(`(function(){var e=document.querySelector(${JSON.stringify(sel)});if(!e)return 'NO_EL';e.click();return 'OK';})()`);

  await send('Runtime.enable');
  await send('Log.enable');
  await send('Page.enable');
  // ★ 假桥必须在页面脚本之前注入 —— 否则 post() 那一刻 webview 还不存在
  await send('Page.addScriptToEvaluateOnNewDocument', { source: BRIDGE });
  const navRes = await send('Page.navigate', { url: URL_ });
  if (navRes && navRes.errorText) console.log('导航返回错误：' + navRes.errorText);

  let ready = false;
  for (let i = 0; i < 60; i++) {
    try { ready = await ev('!!window.__domProbe && !!document.querySelector("#sidebar .nav")'); } catch (_e) { ready = false; }
    if (ready) break;
    await sleep(200);
  }
  if (!ready) {
    // 别硬着头皮往下跑 —— 后面 30 条断言会集体假失败，掩盖真原因
    let diag = '?';
    try {
      diag = await ev(`JSON.stringify({
        href: location.href, title: document.title,
        bodyLen: document.body ? document.body.innerHTML.length : -1,
        probe: typeof window.__domProbe, sent: typeof window.__SENT,
        navCount: document.querySelectorAll('#sidebar .nav').length
      })`);
    } catch (e) { diag = 'EV_ERR:' + e.message; }
    console.log('页面没建起来。诊断：' + diag);
    console.log('事件：' + JSON.stringify(events.slice(0, 6)));
    console.log(`0 通过 / 1 失败`);
    try { child.kill(); } catch (_e) { /* 无所谓 */ }
    process.exit(1);
  }

  console.log('—— 前提：假桥在页面脚本前注入成功 ——');
  const boot = await sent();
  check('★ 页面启动就发出了 get_schema（桥抢在页面脚本前就位）',
    boot.some((x) => x.type === 'get_schema'), JSON.stringify(boot.slice(0, 3)));
  check('★ 桥收得到消息（__SENT 有内容）', boot.length > 0, `${boot.length} 条`);
  check('页面没有 __raw（说明 post 出去的都是合法 JSON）',
    !boot.some((x) => x.__raw !== undefined));

  console.log('');
  console.log('—— 侧边栏结构（第 9 条要加回「训练采点模型」）——');
  let p = await probe();
  check('侧边栏共 7 项', p.navCount === 7, `navCount=${p.navCount}`);

  // ★ Win11 观感层（2026-09-21）：侧栏每项都要换上 Fluent 线性图标（原来是彩色圆点）。
  //   判据读"真的插进去了几支 svg"，**不是**读 data-ico 属性 —— 属性在、图标没插上是
  //   完全可能的（脚本没加载 / 名字拼错都是这样），只看属性的话那条路永远绿。
  const icoStat = JSON.parse(await ev(`JSON.stringify({
    navIcos: document.querySelectorAll('#sidebar .nav .ico').length,
    navDots: document.querySelectorAll('#sidebar .nav .dot').length,
    sprite: !!document.getElementById('shell-icon-sprite'),
    bad: Array.prototype.slice.call(document.querySelectorAll('[data-ico]'))
           .filter(function(e){ return e.dataset.icoDone === 'missing'; })
           .map(function(e){ return e.dataset.ico; })
  })`));
  check('图标 sprite 已注入（#shell-icon-sprite）', icoStat.sprite === true);
  check('侧栏 7 项都换上了 Fluent 图标（svg 真的插进去了）', icoStat.navIcos === 7, `navIcos=${icoStat.navIcos}`);
  check('旧的彩色圆点已撤掉', icoStat.navDots === 0, `navDots=${icoStat.navDots}`);
  check('没有拼错的图标名（拼错会留 data-ico-done=missing）',
    icoStat.bad.length === 0, icoStat.bad.join(',') || '(无)');
  const navPages = await ev('JSON.stringify([...document.querySelectorAll("#sidebar .nav")].map(n=>n.getAttribute("data-page")))');
  const navs = JSON.parse(navPages);
  check('含 train 页（训练采点模型）', navs.includes('train'), navs.join(','));
  check('仍保留 about 页', navs.includes('about'));

  // ★ 2026-09-21 换形态：侧栏 224px 横排 → 窄图标栏；★ 2026-09-22 第十六轮收到 **52px、
  //   「图标在左、文字在右」一行一项**（此前一度是 88px 竖排 —— 已经改回来了）。
  //   这一段必须量**几何**，不能只看"元素在不在"：横排和竖排在 DOM 上长得一模一样，
  //   只有 flex-direction / 图标与文字的左右关系能区分。少了它，哪天 CSS 里某个
  //   align-items 被改回别的排法，所有结构化断言依然全绿、没人发现。
  const navGeo = JSON.parse(await ev(`(function(){
    var sb = document.getElementById('sidebar');
    var n  = document.querySelector('#sidebar .nav[data-page="settings"]');
    var ic = n.querySelector('.ico');
    var cs = getComputedStyle(n);
    var r  = n.getBoundingClientRect();
    var ri = ic.getBoundingClientRect();
    // 文字用 Range 单独量：n.getBoundingClientRect() 会把图标也算进去，判不出左右。
    // ⚠ 2026-09-22：标签文字搬进了 <span class="lbl">（不再是 nav 的直接文本子节点），
    //   上一版按 childNode 找文本节点会拿到 null ⇒ 两条断言假红。
    //   先找 .lbl，找不到再退回直接文本节点（两种结构都认）。 */
    var lt = n.querySelector('.lbl');
    if (!lt) {
      for (var i = 0; i < n.childNodes.length; i++) {
        var c = n.childNodes[i];
        if (c.nodeType === 3 && c.textContent.trim()) { lt = c; break; }
      }
    }
    var rt = null;
    if (lt) { var rg = document.createRange(); rg.selectNodeContents(lt); rt = rg.getBoundingClientRect(); }
    return JSON.stringify({
      sideW: Math.round(sb.getBoundingClientRect().width),
      flexDir: cs.flexDirection,
      icoRight: Math.round(ri.right),
      icoTop: Math.round(ri.top),
      icoH: Math.round(ri.height),
      txtLeft: rt ? Math.round(rt.left) : null,
      txtMidY: rt ? Math.round(rt.top + rt.height / 2) : null,
      icoMidY: Math.round(ri.top + ri.height / 2),
      icoMidX: Math.round(ri.left + ri.width / 2),
      navMidX: Math.round(r.left + r.width / 2)
    });
  })()`));
  check('★ 侧栏是窄图标栏（52px，不再是 224px 横排）', navGeo.sideW === 52, `sideW=${navGeo.sideW}`);
  check('★ 侧栏项是横排一行（flex-direction: row）', navGeo.flexDir === 'row', navGeo.flexDir);
  check('★ 图标在文字**左边**（ico.right ≤ 文字 left）',
    navGeo.txtLeft !== null && navGeo.icoRight <= navGeo.txtLeft,
    `icoRight=${navGeo.icoRight} txtLeft=${navGeo.txtLeft}`);
  check('★ 图标与文字竖直对齐（中线差 ≤ 1px）',
    navGeo.txtMidY !== null && Math.abs(navGeo.icoMidY - navGeo.txtMidY) <= 1,
    `icoMidY=${navGeo.icoMidY} txtMidY=${navGeo.txtMidY}`);
  check('★ 图标水平居中（图标中线 = 项目中线，±1px）',
    Math.abs(navGeo.icoMidX - navGeo.navMidX) <= 1,
    `icoMidX=${navGeo.icoMidX} navMidX=${navGeo.navMidX}`);
  check('★ 窄栏里图标够大（≥20px，否则缩在中间一小点）', navGeo.icoH >= 20, `icoH=${navGeo.icoH}`);

  // ★ 2026-09-22 第十六轮：侧栏收到 52px 之后，品牌那行小字（.brand/.btext）**已整个删掉** ——
  //   它当初的存在意义是"88px 竖排栏顶部那块空白"，栏一变窄/排法一改就没地方了。
  //   注意这是**删除类**改动 ⇒ 没有正标记，只能靠负判据钉住（数量必须为 0）。
  const brandLeft = JSON.parse(await ev(`(function(){
    var b = document.querySelector('#sidebar .brand');
    var t = document.querySelector('#sidebar .btext');
    return JSON.stringify({ brand: !!b, btext: !!t });
  })()`));
  check('★ 侧栏已无品牌小字（.brand/.btext 随窄栏改版一起删掉）',
    brandLeft.brand === false && brandLeft.btext === false, JSON.stringify(brandLeft));

  // ★ 内容区「框里套框」（拯救者工具箱同款）：#main 的 padding 是页边、.page.active 是外框、
  //   .card 是内框。这里量三条**必须同时成立**的性质：
  //     ① 外框四周留了页边（gap>0）—— 贴边就会退化成上一轮那种"标题栏深、内容区浅"的错位感；
  //     ② 外框有提亮（它得是一层可见的底，不然谈不上"框"）；
  //     ③ 卡片比外框更亮 —— 层级反了就成了"框里挖了个洞"，比不套框还难看。
  const frameGeo = JSON.parse(await ev(`(function(){
    var main = document.getElementById('main');
    var pg   = document.querySelector('.page.active');
    var card = pg.querySelector('.card');
    function alpha(s){ var m = s.match(/[\\d.]+/g) || []; return m.length >= 4 ? Number(m[3]) : 1; }
    var csPg = getComputedStyle(pg), csCard = getComputedStyle(card);
    var rm = main.getBoundingClientRect(), rp = pg.getBoundingClientRect();
    return JSON.stringify({
      gapTop:  Math.round(rp.top  - rm.top),
      gapLeft: Math.round(rp.left - rm.left),
      gapRight: Math.round(rm.right - rp.right),
      gapBottom: Math.round(rm.bottom - rp.bottom),
      pageBg:  alpha(csPg.backgroundColor),
      cardBg:  alpha(csCard.backgroundColor),
      radius:  Math.round(parseFloat(csPg.borderTopLeftRadius) || 0),
      cardInside: pg.contains(card)
    });
  })()`));
  /* ★ 2026-09-22 第十六轮改了口径：主人要"界面往边靠"，所以 #main 的页边从四边 8px
     变成 `8px 8px 8px 2px` —— **左边只剩 2px**（面板到侧栏按钮框正好 8px = 右边距）。
     判据跟着改：① 四边都必须 > 0（贴边会退化成"两块颜色没对齐"的错位感）；
     ② 左边那个 2px 是**刻意值**，单列一条钉住 —— 免得哪天被"顺手调回 8"。
     同一批的左右对称判据在 pagemarginprobe.js（那边量的是面板到按钮框的距离）。 */
  check('★ 外框四边都留了页边（不贴窗口边 ⇒ 读作"浮着的卡"，不是两块颜色没对齐）',
    frameGeo.gapTop > 0 && frameGeo.gapLeft > 0 &&
    frameGeo.gapRight > 0 && frameGeo.gapBottom > 0,
    `上=${frameGeo.gapTop} 左=${frameGeo.gapLeft} 右=${frameGeo.gapRight} 下=${frameGeo.gapBottom}`);
  check('★ 左侧页边 = 2px（第十六轮"界面往边靠"的落点；改回 8 就不对称了）',
    frameGeo.gapLeft === 2, `gapLeft=${frameGeo.gapLeft}`);
  check('★ 外框是一层可见的底（提亮 > 0.02）', frameGeo.pageBg > 0.02, `pageBg=${frameGeo.pageBg}`);
  check('★ 卡片比外框更亮（层级没反）',
    frameGeo.cardBg > frameGeo.pageBg,
    `cardBg=${frameGeo.cardBg} pageBg=${frameGeo.pageBg}`);
  check('★ 外框有圆角（≥8px）', frameGeo.radius >= 8, `radius=${frameGeo.radius}`);
  check('★ 卡片确实在外框**里面**（不是并列的兄弟节点）', frameGeo.cardInside === true);

  // ★ 2026-09-21（本轮）主人指着三张截图提的四件事，这里量页面侧能证的那几件：
  //   ① 「右下角那个 ? 不要了」→ 那是**宿主**钉上去的（见 MainWindow.xaml.cs 的 ③），
  //     页面这边只能验"页面自己不再造这么个浮动入口"（本文件下面有断言）；
  //   ② 「不要画这根线」→ 侧栏 border-right + 外框描边，两条都要为 0；
  //   ③ 品牌格换成真·应用图标 → 必须是 <img> 且**真的解码出来了**（src 写错时图上空白，
  //     而 DOM/类名/属性全都没变，只看结构永远发现不了 —— 所以要读 naturalWidth）；
  //   ④ 设置图标跟参考图一模一样 → 用系统字形 E713（不可用时才退回官方 SVG）。
  const seamGeo = JSON.parse(await ev(`(function(){
    var sb = document.getElementById('sidebar');
    var pg = document.querySelector('.page.active');
    var csSb = getComputedStyle(sb), csPg = getComputedStyle(pg);
    function w(v){ return Math.round(parseFloat(v) || 0); }
    var logo = sb.querySelector('.blogo');
    var ic = document.querySelector('#sidebar .nav[data-page="settings"] .ico');
    var ink = -1;
    try {
      var c = document.createElement('canvas').getContext('2d');
      c.font = '20px "Segoe Fluent Icons"';
      var m = c.measureText('\\uE713');
      ink = Math.round((m.actualBoundingBoxAscent + m.actualBoundingBoxDescent) * 10) / 10;
    } catch (e) { }
    return JSON.stringify({
      sideBR: w(csSb.borderRightWidth),
      pgB: [w(csPg.borderTopWidth), w(csPg.borderRightWidth), w(csPg.borderBottomWidth), w(csPg.borderLeftWidth)],
      pgShadow: csPg.boxShadow === 'none' ? '' : csPg.boxShadow,
      logoTag: logo ? logo.tagName : null,
      logoLoaded: !!(logo && logo.naturalWidth > 0),
      logoW: logo ? Math.round(logo.getBoundingClientRect().width) : 0,
      logoSrc: logo ? (logo.getAttribute('src') || '') : '',
      /* ★ 2026-09-21 晚新增：「外框是固定显示区，界面在里面滚」。
         光看 CSS 里写了 overflow:auto 不算数 —— 要**逼出"该滚谁"**：
         临时塞一块比视口还高的占位（塞完就删），然后看
           · #main 会不会滚（滚了就说明整块外框跟着内容跑）
           · .page.active 会不会滚（这才是对的）
           · **滚起来之后外框自己的 rect 有没有动**（外框固定 = 一动都不动）
           · 外框下边缘是否仍在视口内（页边那 16px 露得出来）
         注意判据取的是"滚过之后 rect 位移"，不是"有没有 scrollTop" —— 后者对
         overflow:hidden 的容器也可能被 JS 赋值成功，说明不了任何事。
         ⚠ 本段整块是包在 ev() 的**模板字面量**里的：注释里也不能出现反引号
           （写一个字面量 backtick 就会提前收口，报成"missing ) after argument list"）。 */
      scrollHost: (function () {
        var main = document.getElementById('main');
        var csM = getComputedStyle(main), csP = getComputedStyle(pg);
        var csM2 = main.getBoundingClientRect();
        var padT = parseFloat(csM.paddingTop) || 0;
        var padB = parseFloat(csM.paddingBottom) || 0;
        var padV = padT + padB;
        var probe = document.createElement('div');
        probe.id = '__scrollProbe';
        probe.style.cssText = 'height:' + (innerHeight + 600) + 'px';
        pg.appendChild(probe);
        var mainScrolls = main.scrollHeight > main.clientHeight + 2;
        var pageScrolls = pg.scrollHeight > pg.clientHeight + 2;
        var before = pg.getBoundingClientRect();
        pg.scrollTop = 400;
        var after = pg.getBoundingClientRect();
        var moved = Math.round(Math.abs(after.top - before.top));
        var scrolled = Math.round(pg.scrollTop);
        pg.scrollTop = 0;
        pg.removeChild(probe);
        return {
          mainOverflow: csM.overflowY, pageOverflow: csP.overflowY,
          mainScrolls: mainScrolls, pageScrolls: pageScrolls,
          frameMoved: moved, scrolled: scrolled,
          frameH: Math.round(before.height), viewportH: innerHeight,
          marginBottom: Math.round(innerHeight - before.bottom),
          padB: Math.round(padB), padV: Math.round(padV),
          expectH: Math.round(innerHeight - csM2.top - padV)
        };
      })(),
      /* ★ 2026-09-21 晚改版：竖条从「每项一个 .nav::before」改成「一根独立元素 #nav-ind」，
         因为它得有**从一项滑到另一项的位移**，伪元素做不到（挂在各自己身上）。
         所以这里只量"图标有没有被挤歪"；竖条本身见下面 navInd。 */
      navBars: (function () {
        var out = [];
        document.querySelectorAll('#sidebar .nav').forEach(function (n) {
          var ico = n.querySelector('.ico');
          var rn = n.getBoundingClientRect(), ri = ico ? ico.getBoundingClientRect() : null;
          out.push({
            page: n.getAttribute('data-page'),
            active: n.classList.contains('active'),
            dx: ri ? Math.round((ri.left + ri.width / 2) - (rn.left + rn.width / 2)) : null
          });
        });
        return out;
      })(),
      /* 选中指示条（#nav-ind）。量五件事：
         ① 只有一根、且落在**选中项**上（中心对齐 ±1px）；
         ② 尺寸/圆角/绝对定位对；
         ③ 颜色 = 该项的 --nav-accent；
         ④ ★ **过渡里必须有 transform 且时长非 0** —— 这就是"切换动画还在不在"的直接判据
            （主人 21:37 反馈的正是它没了：上一版只能 opacity 淡入淡出）；
         ⑤ 换一项之后它的 translateY 真的变了（位移逻辑不是写死的）。
         ⚠ 比 rect 更可靠的是读**内联 style.transform**：过渡进行中 getBoundingClientRect
           读到的是动画中间值，拿它判"有没有变"会随机翻车。 */
      navInd: (function () {
        var ind = document.getElementById('nav-ind');
        var sb = document.getElementById('sidebar');
        if (!ind || !sb) { return null; }
        var act = sb.querySelector('.nav.active');
        var cs = getComputedStyle(ind);
        var r = ind.getBoundingClientRect(), ar = act ? act.getBoundingClientRect() : null;
        var hex2rgb = function (h) {
          var m = /^#?([0-9a-f]{6})$/i.exec(String(h).trim());
          if (!m) { return null; }
          var n = parseInt(m[1], 16);
          return 'rgb(' + ((n >> 16) & 255) + ', ' + ((n >> 8) & 255) + ', ' + (n & 255) + ')';
        };
        var accent = act ? getComputedStyle(act).getPropertyValue('--nav-accent').trim() : '';
        var dur = (cs.transitionDuration || '').split(',').some(function (d) { return parseFloat(d) > 0; });
        /* ⚠ 这里**不再**做"挪一下 active 看 transform 变不变"的同步位移实测（原来是 ⑤）。
           原因：新的动作是**两段异步**的（压扁 → 跳 → 弹开），同步读内联 transform 会
           读到"压扁到一半"那个中间态（scaleY 0.28），于是既判不出"落点对不对"，
           也会把"必须还原"判红 —— 那是拿探针的取样时机否定功能本身。
           ⇒ 位移与落点改到下面 navAnim 里用**轨迹**验（采到落定后的 top，
             再和新选中项的中心比），那才是这件事的真实判据。 */
        return {
          exists: true, opacity: Number(cs.opacity),
          w: Math.round(r.width), h: Math.round(r.height), pos: cs.position,
          radius: cs.borderTopLeftRadius,
          bg: cs.backgroundColor, accent: accent, accentRgb: hex2rgb(accent),
          transitionProperty: cs.transitionProperty, transitionDuration: cs.transitionDuration,
          hasTransformTransition: cs.transitionProperty.indexOf('transform') >= 0 && dur,
          centerOffset: ar ? Math.round((r.top + r.height / 2) - (ar.top + ar.height / 2)) : null,
          /* ★ 2026-09-21 22:03：主人说"这个条跑出来了，不应该在那个框框里吗" ——
             条的左缘必须落在**选中项的框**里（对齐框的左缘），不能跑到侧栏的横向内边距中。
             leftInset = 0 表示左缘与框的左缘齐平；< 0 就是"跑出去了"。 */
          leftInset: ar ? Math.round(r.left - ar.left) : null,
          inBox: ar ? (r.left >= ar.left - 1 && r.right <= ar.right + 1) : null,
          activeLeft: ar ? Math.round(ar.left) : null,
          activePage: act ? act.getAttribute('data-page') : null,
          /* 负断言：.nav 上不该再有伪元素条（当初"每项一条灰的"就是它） */
          pseudo: (function () {
            var n = sb.querySelector('.nav');
            if (!n) { return null; }
            var c = getComputedStyle(n, '::before');
            return { content: c.content, w: c.width, bg: c.backgroundColor };
          })()
        };
      })(),
      /* hover 时也不该冒出灰条（hover ≠ 选中）。
         :hover 没法用脚本派发事件触发（派发的 mouseover 不点亮 :hover），
         所以直接查样式表：不许存在给 .nav:hover::before 上背景的规则。 */
      hoverBarRules: (function () {
        var hits = [];
        try {
          Array.prototype.forEach.call(document.styleSheets, function (ss) {
            Array.prototype.forEach.call(ss.cssRules || [], function (r) {
              var sel = r.selectorText || '';
              if (sel.indexOf('.nav:hover') >= 0 && sel.indexOf('before') >= 0) {
                hits.push(sel + '{' + (r.style ? r.style.cssText : '') + '}');
              }
            });
          });
        } catch (e) { hits.push('(样式表读不到)'); }
        return hits;
      })(),
      iconTag: ic ? ic.tagName.toLowerCase() : null,
      iconGlyph: (ic && ic.textContent) ? ic.textContent.codePointAt(0) : null,
      glyphInk: ink,
      /* 字体探测的读数。★ 这里**读的是页面自己的决定**（window.__dshIconFont，
         由 shell-ui.js 的 hasNativeIconFont() 写出来），不是探针自己再算一遍 ——
         同一份判据在两处各写一份，测的就是副本而不是被测对象了。
         另附探针**独立**量出来的同一套形状指标，用来对账（见下）。
         ⚠ 别再退回量 advance 宽度那套：E713 在真字体/假字体/回退字体下 advance
           都是 20（=font-size），零区分力（本轮踩过）。
         ⚠ 也别再印"某半径上的环命中率"：那版判据已废（真齿轮在该半径上只有 58%，
           而缺字形的豆腐框有 21%~65%，区间重叠、怎么调都误判）。现在判据是**形状**：
           齿轮"中心空、四角空"；豆腐框"中心被 ✕ 填满、四角有边框墨"。
           这里就照这四项印，跟页面判据同一套口径，数字才对得上。 */
      probeW: (function () {
        var m = null;
        try {
          var S = 48, cv = document.createElement('canvas');
          cv.width = S; cv.height = S;
          var c = cv.getContext('2d');
          c.font = S + "px 'Segoe Fluent Icons','Segoe MDL2 Assets'";
          c.textAlign = 'center'; c.textBaseline = 'middle';
          c.fillStyle = '#fff';
          c.fillText('\\uE713', S / 2, S / 2);
          var d = c.getImageData(0, 0, S, S).data;
          var minX = S, maxX = -1, minY = S, maxY = -1, ink = 0;
          for (var y = 0; y < S; y++) {
            for (var x = 0; x < S; x++) {
              if (d[(y * S + x) * 4 + 3] > 40) {
                ink++;
                if (x < minX) { minX = x; } if (x > maxX) { maxX = x; }
                if (y < minY) { minY = y; } if (y > maxY) { maxY = y; }
              }
            }
          }
          if (maxX >= 0) {
            var bw = maxX - minX + 1, bh = maxY - minY + 1;
            function blk(bx, by, w, h) {
              var i = 0, t = 0;
              for (var yy = by; yy < by + h; yy++) {
                for (var xx = bx; xx < bx + w; xx++) {
                  if (xx < 0 || yy < 0 || xx >= S || yy >= S) { continue; }
                  t++; if (d[(yy * S + xx) * 4 + 3] > 40) { i++; }
                }
              }
              return t ? Math.round(i / t * 100) : 100;
            }
            var cw = Math.max(2, Math.round(bw * 0.20)), ch = Math.max(2, Math.round(bh * 0.20));
            var kw = Math.max(1, Math.round(bw * 0.15)), kh = Math.max(1, Math.round(bh * 0.15));
            m = {
              覆盖: Math.round(ink / (bw * bh) * 100) + '%',
              宽高比: Math.round(Math.min(bw, bh) / Math.max(bw, bh) * 100) + '%',
              中心墨: blk(minX + Math.round((bw - cw) / 2), minY + Math.round((bh - ch) / 2), cw, ch) + '%',
              四角墨: [blk(minX, minY, kw, kh), blk(maxX - kw + 1, minY, kw, kh),
                      blk(minX, maxY - kh + 1, kw, kh),
                      blk(maxX - kw + 1, maxY - kh + 1, kw, kh)].join('/') + '%'
            };
          }
        } catch (e) { m = 'err:' + e; }
        return {
          页面判定: (typeof window.__dshIconFont === 'boolean') ? window.__dshIconFont : '(未执行)',
          探针独立量到的形状: m,
          判据: '齿轮=中心空(<=35%)且四角空(<=30%)；豆腐框=中心被叉填满、四角有墨',
        };
      })(),
      /* 兜底分支用的那条 path 的开头（用来区分"官方齿轮"和"我手画的那版"） */
      svgPathHead: (function () {
        var g = document.getElementById('i-settings');
        var p = g && g.querySelector('path');
        return p ? (p.getAttribute('d') || '').slice(0, 22) : null;
      })(),
      /* 页面自己有没有造"钉在视口角上的**小**浮动入口"：
         判据必须带尺寸上限 —— 材质幕布 #shell-mat-veil 是 position:fixed;inset:0
         （整屏覆盖、pointer-events:none），不带尺寸过滤会把它算成"右下角有个浮动件"，
         得到一条永远为红的假断言。 */
      floatBR: Array.prototype.slice.call(document.querySelectorAll('body *')).filter(function (e) {
        var cs = getComputedStyle(e);
        if (cs.position !== 'fixed' || cs.display === 'none' || cs.visibility === 'hidden') { return false; }
        var r = e.getBoundingClientRect();
        return r.width > 0 && r.width <= 80 && r.height <= 80 &&
               r.right > innerWidth - 40 && r.bottom > innerHeight - 40;
      }).map(function (e) { return e.id || e.className || e.tagName; })
    });
  })()`));
  check('★ 侧栏不再画那根竖线（border-right = 0）', seamGeo.sideBR === 0, `sideBR=${seamGeo.sideBR}`);
  check('★ 外框不再画描边（四边全 0；分层靠填充差 + 顶边高光）',
    seamGeo.pgB.every(function (v) { return v === 0; }), '四边=' + seamGeo.pgB.join(','));
  check('★ 外框保留了顶边高光（参考图有那一条，实测 Δ7 左右）',
    /inset/.test(seamGeo.pgShadow), seamGeo.pgShadow || '(无 shadow)');
  /* ★ 2026-09-22 第十六轮：侧栏品牌格（`.blogo` / `.brand`）已随窄栏改版整个删掉，
     所以这里从"图标有没有真的解码出来"改成**负判据**（删除类改动没有正标记）。
     注意：外框圆角 ≥8px 那条也已过时 —— 现在是 8px（--rd-win 同档），留着照样过。 */
  check('★ 侧栏已无品牌格（.blogo 随窄栏改版删掉）',
    seamGeo.logoTag === null, `tag=${seamGeo.logoTag}`);
  //   ④ 设置图标跟参考图一模一样 → 走系统字形 E713；字体不在时才退回官方 Fluent path。
  //   两条分支都要验，而且**都要能证明不是"我手画的那版"**：字形分支读码点；
  //   SVG 分支读 sprite 里那条 path 的开头（手画那版开头是 "M15 10h1.9…"，官方是 "M12.0122 2.25…"）。
  //   ★ 哪条分支生效由 shell-ui.js 自己判（canvas 画一圈看墨迹，见那边的长注释），
  //     探针只读它的结论 + 独立对账，不重复实现判据。
  const settingsOK = seamGeo.iconTag === 'span'
    ? seamGeo.iconGlyph === 0xE713
    : (typeof seamGeo.svgPathHead === 'string' && seamGeo.svgPathHead.indexOf('M12.0122 2.25') === 0);
  check('★ 设置图标＝官方那只齿轮（字形 E713 优先，字体不在则退回官方 path；两者都不是手画的）',
    settingsOK,
    `tag=${seamGeo.iconTag} 码点=${seamGeo.iconGlyph === null ? '-' : '0x' + seamGeo.iconGlyph.toString(16)}` +
    ` path头=${seamGeo.svgPathHead === null ? '-' : seamGeo.svgPathHead}` +
    ` 探测=${JSON.stringify(seamGeo.probeW)}`);
  check('★ 页面自己没有在右下角钉任何小的浮动入口（「?」那种）',
    seamGeo.floatBR.length === 0, seamGeo.floatBR.join(',') || '(无)');

  // ---- 本轮第二批（主人 21:02 的三张截图）-----------------------------------
  //   ⑤ 「外框是显示区域，他是定在这不动的，界面在里面滚动」
  //   ⑥ 「每一个左边的小竖条没了」→ 那条 3px 指示条要回来，而且不能把图标挤歪
  const sh = seamGeo.scrollHost;
  check('★ 滚动发生在外框内部（#main 不滚、.page.active 滚）',
    sh.mainOverflow === 'hidden' && sh.pageOverflow === 'auto' &&
    sh.mainScrolls === false && sh.pageScrolls === true,
    `#main=${sh.mainOverflow}/滚=${sh.mainScrolls}  .page=${sh.pageOverflow}/滚=${sh.pageScrolls}`);
  check('★ 滚起来的时候外框自己一动不动（定住的是它，动的是里面）',
    sh.frameMoved === 0 && sh.scrolled > 200,
    `内容滚了 ${sh.scrolled}px，外框位移 ${sh.frameMoved}px`);
  /* ⚠ 期望值**不再写死**（原来是 `innerHeight - 42 - 32`，页边一改就假红）：
     改成读 #main 的实际 padding 与位置算出来。判据本身没变 —— 框要正好占满
     "页边留完之后的剩余空间"：被压扁（比如谁给 .page 写了固定高度）就会对不上。
     🔴 页边只判 `> 4`，**不判"等于 padding-bottom"**：这里视口是 importprobe 用
        Emulation 覆盖出来的（847），实测会出现 2px 的亚像素取整差（框高那条仍然
        逐像素吻合）。断言不去钉一个自己解释不了的值 —— 页边"真的露出来了"
        由下限兜住，够了。 */
  check('★ 外框正好占满一屏（不缩、也不顶出视口；页边留得住）',
    Math.abs(sh.frameH - sh.expectH) <= 3 && sh.marginBottom > 4,
    `框高=${sh.frameH} 期望≈${sh.expectH} 视口=${sh.viewportH} 下页边=${sh.marginBottom}(padding-bottom=${sh.padB})`);

  /* ★ "切换动画"的过程采样 —— 主人 2026-09-22 那句话的验收：
     「左侧那个条条切换的时候不是划过去，Windows11 里面是缩小一截再直接弹开到
       另一个选中界面的前面」。
     单独跑一次取值（要等几百毫秒，而且**必须跑在 async 上下文里**；seamGeo 那个大表达式
     是同步的，await 塞进去会报 "await is not defined"）。
     🔴 采的是 **[top, height, left] 三个量**：判"弹还是滑"只能看 **height**
        （滑动时恒为 22px；弹开时中间掉到 6px 上下）。上一版只采 top，
        于是"缩小再弹开"和"平移过去"在读数上长得一样 —— 那等于没测。
     ⚠ 用 rAF 在页面里采，不要在外面一帧一帧发 CDP 取值：往返延迟会把 110ms 的
       压扁阶段整个跳过去（采不到最低点，就会得到假的"没压扁"）。 */
  const navAnim = await ev(`(async function () {
    var ind = document.getElementById('nav-ind');
    var sb = document.getElementById('sidebar');
    if (!ind || !sb) { return null; }
    var api = window.ShellUI && window.ShellUI.navInd;
    var meta = (window.ShellUI && window.ShellUI.navAnim) || {};
    var act = sb.querySelector('.nav.active'), other = null;
    var navs = sb.querySelectorAll('.nav');
    for (var i = 0; i < navs.length; i++) {
      if (!navs[i].classList.contains('active')) { other = navs[i]; break; }
    }
    if (!api || !act || !other) { return null; }
    // 系统里关掉了动画 ⇒ 不该有动作，如实报出来（别硬测出个"通过"）
    if (meta.reduce && meta.reduce()) { return JSON.stringify({ reduce: true, frames: 0 }); }
    var wait = function (ms) { return new Promise(function (res) { setTimeout(res, ms); }); };
    var total = (meta.shrink || 110) + (meta.pop || 240) + 420;
    var h0r = ind.getBoundingClientRect();
    var h0 = h0r.height, top0 = h0r.top;
    var trace = [], t0 = performance.now();
    var inlBefore = ind.style.transform;   // 落定态：translateY(旧Y) scaleY(1)
    act.classList.remove('active'); other.classList.add('active'); api();
    /* ★ 确定性锚点：切换那一瞬间**同步**读内联值。
       navPlace 是同步写 style.transform 的，而阶段①就是"在旧位置设成 scaleY(SQUASH)、
       过渡 SHRINK ms" —— 这两个值不依赖"采样有没有采到"，比下面的 rect 最低点可靠得多。
       （rect 最低点要靠运气：见下面 wait(8) 那段注释。） */
    var inl0 = ind.style.transform;
    var trans0 = ind.style.transition;
    while (performance.now() - t0 < total) {
      var r = ind.getBoundingClientRect();
      trace.push([Math.round(performance.now() - t0), Math.round(r.top * 10) / 10,
                  Math.round(r.height * 10) / 10, Math.round(r.left * 10) / 10]);
      /* 🔴 采样间隔一定要**密**。压扁曲线的缓冲段很长（cubic-bezier(.7,0,1,.5)）：
         110ms 里前 100ms 高度只从 22 掉到 19，最后 20ms 才砸到 6，加上弹开起步很快，
         "低于半高"的窗口总共只有 ~25–50ms 宽。
         第一个版本用 wait(24)：整段只落进 1 个采样点，setTimeout 稍微被压一下就整天擦肩
         而过 —— 实测同一份代码 importprobe 里红、连做 12 次的专项里 12/12 绿。
         那不是"动画时有时无"，是**测法在赌运气**。8ms 之后低点稳定采得到。 */
      await wait(8);
    }
    var hEnd = ind.getBoundingClientRect().height;
    var inlEnd = ind.style.transform;
    var hs = trace.map(function (s) { return s[2]; });
    var xs = trace.map(function (s) { return s[3]; });
    var tops = trace.map(function (s) { return s[1]; });
    var jump = 0;
    for (var k = 1; k < tops.length; k++) { jump = Math.max(jump, Math.abs(tops[k] - tops[k - 1])); }
    /* 落点对不对：把它和**新选中项的中心**比（都在视口坐标系里）。
       ⚠ 这条替代了原来那个"同步读内联 transform"的位移实测 —— 两段式动画下同步读只会
         读到压扁到一半的中间态，判不出落点。 */
    var orr = other.getBoundingClientRect();
    var expectTop = orr.top + (orr.height - hEnd) / 2;
    var topEnd = ind.getBoundingClientRect().top;
    other.classList.remove('active'); act.classList.add('active'); api();
    await wait(500);
    var topBack = ind.getBoundingClientRect().top;
    var lows = 0;
    for (var q = 0; q < hs.length; q++) { if (hs[q] < h0 * 0.6) { lows++; } }
    /* 从内联 transform 里抠出 Y 与 scaleY。⚠ 别拿 rect.top 去和 translateY 比：
       translateY 是**相对侧栏**的，rect.top 是视口坐标，两者差一个侧栏原点（实测 ~2px），
       比了会冤枉。要比就比"切换前的内联值"和"切换瞬间的内联值" —— 同坐标系。
       ⚠ 也**不要**在这里写正则字面量：本段是塞在 node 模板字面量里的，转义括号的反斜杠
       会被模板字面量吃掉，正则静默变成别的意思（不报错，只是匹配不上）。
       ⚠ 本段里连注释都**不能出现反引号** —— 它会把外层模板字面量提前闭合。 */
    var pickNum = function (s, key) {
      s = String(s || '');
      var i = s.indexOf(key + '(');
      if (i < 0) { return null; }
      var rest = s.slice(i + key.length + 1);
      var j = rest.indexOf(')');
      if (j < 0) { return null; }
      var v = parseFloat(rest.slice(0, j));
      return isNaN(v) ? null : v;
    };
    var pickY = function (s) { return pickNum(s, 'translateY'); };
    var pickS = function (s) { return pickNum(s, 'scaleY'); };
    return JSON.stringify({
      page: other.getAttribute('data-page'),
      h0: Math.round(h0 * 10) / 10, hEnd: Math.round(hEnd * 10) / 10,
      minH: Math.round(Math.min.apply(null, hs) * 10) / 10,
      lowSamples: lows, inl0: inl0, inlBefore: inlBefore,
      trans0: trans0, inlEnd: inlEnd,
      pinlOldY: pickY(inlBefore), pinl0Y: pickY(inl0),
      pinl0Scale: pickS(inl0), pinlEndScale: pickS(inlEnd),
      shrink: meta.shrink || 110, squash: meta.squash || 0.28,
      xSpread: Math.round((Math.max.apply(null, xs) - Math.min.apply(null, xs)) * 10) / 10,
      topJump: Math.round(jump * 10) / 10, topStart: Math.round(top0 * 10) / 10,
      topEnd: Math.round(topEnd * 10) / 10, expectTop: Math.round(expectTop * 10) / 10,
      posErr: Math.round(Math.abs(topEnd - expectTop) * 10) / 10,
      topBack: Math.round(topBack * 10) / 10,
      backErr: Math.round(Math.abs(topBack - top0) * 10) / 10,
      frames: trace.length, reduce: false, trace: trace
    });
  })()`);
  const anim = navAnim ? JSON.parse(navAnim) : null;

  const bars = seamGeo.navBars;
  const ni = seamGeo.navInd;
  //   ⑦ 「没有被选中的前面不要显示灰色的竖条，被选择的才需要显示」→ 竖条只属于选中项。
  //   ⑦b「小竖条的切换到动画怎么没了」→ 它还得是一根**会滑动**的条（一根独立元素 +
  //      transform 过渡），而不是"旧项淡出、新项淡入"。伪元素天生做不到位移 ⇒ 下面第 1 条
  //      会检查 #nav-ind 存在且 .nav 上**没有**残留的伪元素条（防又改回去）。
  check('★ 指示条是一根独立元素（唯一一条），.nav 上没留伪元素条',
    !!ni && ni.exists && !!ni.pseudo && (ni.pseudo.content === 'none' || ni.pseudo.w === '0px'),
    ni ? `#nav-ind 在  .nav::before 的 content=${ni.pseudo && ni.pseudo.content} w=${ni.pseudo && ni.pseudo.w}` : '(没有 #nav-ind)');
  check('★★ 指示条与选中项中心对齐（±1px）',
    !!ni && ni.centerOffset !== null && Math.abs(ni.centerOffset) <= 1,
    ni ? `选中=${ni.activePage} 中心偏差=${ni.centerOffset}px` : '(无)');
  check('★ 3px 圆角条、绝对定位、颜色取该项强调色',
    !!ni && ni.w === 3 && ni.h >= 12 && ni.pos === 'absolute' && ni.opacity === 1 &&
    parseFloat(ni.radius) >= 1 && !!ni.bg && ni.bg === ni.accentRgb,
    ni ? `${ni.w}x${ni.h}/${ni.pos} 圆角=${ni.radius} 不透明=${ni.opacity} 条=${ni.bg} 该项目强调色=${ni.accent}→${ni.accentRgb}` : '(无)');
  /* ★ 22:03 主人复核："这个条跑出来了，不应该在那个框框里吗" ——
     条要**在选中项那个圆角框里**，左缘与框的左缘齐平（leftInset=0）。
     退化成 left:0（侧栏左缘）时 leftInset 会变成 −8 ⇒ 这条会红，把回归钉住。 */
  check('★★ 指示条落在选中项的框内（左缘与框左缘齐平，没掉进侧栏内边距里）',
    !!ni && ni.leftInset === 0 && ni.inBox === true,
    ni ? `框左缘=${ni.activeLeft}px 条左缘偏移=${ni.leftInset}px 落在框内=${ni.inBox}` : '(无)');
  // ⚠ 这条在 2026-09-22 换了口径：CSS 里那条 transform 过渡现在只是**兜底**
  //   （首帧 / resize / 字体就绪用），换页那两段由 JS 逐段写内联 transition。
  //   两种都算"有 transform 过渡"，所以判据本身不用改 —— 但别把它的存在
  //   误会成"还在滑过去"，真正的判据是下面那三条动画轨迹。
  check('★ transform 过渡还在（换页那两段由 JS 逐段接管，这条是兜底那一段）',
    !!ni && ni.hasTransformTransition === true,
    ni ? `transition=${ni.transitionProperty} / ${ni.transitionDuration}` : '(无)');
  check('★★ 换一项之后它真的跟着走（位置是按选中项算的，不是写死的）',
    !!anim && !anim.reduce && anim.posErr <= 1,
    anim ? `落在 ${anim.topEnd}px（「${anim.page}」中心 ${anim.expectTop}px，偏差 ${anim.posErr}px）` : '(没跑成)');
  /* ★★ 2026-09-22 主人裁定：「左侧那个条条切换的时候不是划过去，
     Windows11 里面是**缩小一截再直接弹开**到另一个选中界面的前面」。
     这三条就是"不是划过去"的判据 ——
     ⚠ **关键是采样 rect 的高度**：滑动时高度恒为 22px，弹开时中间会掉到 6px 上下；
       只看 top 两种都会变，分不出来（上一版就是只采 top，于是"弹开"和"滑动"看不出区别）。 */
  check('★★★ 换页时它先被压扁（过程中高度掉到一半以下 ⇒ 不是平移过去）',
    !!anim && !anim.reduce && anim.h0 > 8 && anim.minH < anim.h0 * 0.6,
    anim ? `原高 ${anim.h0}px → 过程中最低 ${anim.minH}px（${((anim.minH / anim.h0) * 100).toFixed(0)}%），` +
           `低于半高的采样点 ${anim.lowSamples}/${anim.frames} 个`
         : '(没跑成)');
  /* ★★ 确定性锚点：不看渲染、只看**切换那一刻被同步写下的内联值**。
     它证明的是"机制"（在旧位置设成压扁态、过渡 SHRINK ms），不受采样运气影响。
     上面那条 rect 最低点证明的是"观感"（真的画出了矮的一帧）；两条一起才完整 ——
     只留 rect 最低点会偶发假红，只留内联值证不了它真被画出来。 */
  check('★★★ 换位置那一刻它被同步设成「旧位置 + 压扁」态（机制层面的判据，不赌采样）',
    !!anim && !anim.reduce && anim.pinlOldY === anim.pinl0Y &&
    anim.pinl0Scale === anim.squash,
    anim ? `切换瞬间内联 transform = ${anim.inl0}（切换前 = ${anim.inlBefore}）；` +
           `旧 Y=${anim.pinlOldY} 新 Y=${anim.pinl0Y} 压扁系数=${anim.pinl0Scale}（应为 ${anim.squash}）`
         : '(没跑成)');
  check('★★★ 压扁那一段是**带过渡**的（不是瞬间闪一下）',
    !!anim && !anim.reduce && String(anim.trans0 || '').indexOf(anim.shrink + 'ms') >= 0,
    anim ? `切换瞬间内联 transition = ${anim.trans0}` : '(没跑成)');
  check('★★★ 弹开之后回到原高（是"缩一下再弹开"，不是缩完就不管了）',
    !!anim && !anim.reduce && Math.abs(anim.hEnd - anim.h0) <= 1 &&
    anim.pinlEndScale === 1,
    anim ? `结束高度 ${anim.hEnd}px（原 ${anim.h0}px）；收尾内联 transform = ${anim.inlEnd}`
         : '(没跑成)');
  check('★★★ 全程没有横向位移（真的没有"划过去"）',
    !!anim && !anim.reduce && anim.xSpread <= 0.5,
    anim ? `采样 ${anim.frames} 帧，x 的极差 ${anim.xSpread}px` : '(没跑成)');
  check('★ 动画确实被采到了（帧数够，不是"没动过"被当成通过）',
    !!anim && anim.frames >= 8,
    anim ? `${anim.frames} 帧；轨迹片段 ${JSON.stringify((anim.trace || []).slice(0, 6))}` : '(没跑成)');
  check('★ 采样结束后它回到原来的项上（探针没把页面留在半路）',
    !!anim && !anim.reduce && anim.backErr <= 1,
    anim ? `还原后 ${anim.topBack}px vs 原来 ${anim.topStart}px，偏差 ${anim.backErr}px` : '(没跑成)');
  check('★ 换页那一下 top 是**一次跳跃**（不是一帧一帧挪过去 —— 这也是"不是划过去"的旁证）',
    !!anim && !anim.reduce && anim.topJump > 8,
    anim ? `单帧最大位移 ${anim.topJump}px（平移过去的话每帧只有 ~9px 且总量连续）` : '(没跑成)');
  check('★ hover 也不冒灰条（竖条是"当前在哪页"的指示，不是装饰）',
    seamGeo.hoverBarRules.length === 0,
    seamGeo.hoverBarRules.join(' ') || '(样式表里没有这类规则)');
  check('★ 指示条不占布局 ⇒ 图标仍然居中（当年它当 flex 子项时把图标顶歪过）',
    bars.every((b) => b.dx !== null && Math.abs(b.dx) <= 1),
    bars.map((b) => `${b.page}:Δ${b.dx}`).join(' '));

  console.log('');
  console.log('—— ② 设置：界面材质四档（schedule 第 2 条）——');
  await clearSent();
  await clickNav('settings');
  let s = await sent();
  check('点「设置」会向宿主问 appinfo', s.some((x) => x.type === 'appinfo'));
  await feed({
    type: 'appinfo',
    data: {
      app: 'ADOFAI Studio', studio_dir: '<REPO>', output_dir: '<REPO>\\output',
      audio_sep_dir: '<REPO>\\audio-sep', chartgen_dir: '<REPO>\\chartgen',
      gateway: 'http://127.0.0.1:8766', sidecar: '127.0.0.1:8765', py_ver: '3.13.12',
      material: 'acrylic',
      // ★ 2026-09-22：深色 / 浅色由宿主给两样 ——
      //   appearance  = 当前档
      //   appearances = 全局档位清单（带 label，页面不自己编名字）
      //   ⚠ materials[].appearances 那个 per-material 字段**已删**（同日第二轮）：
      //     明暗是全局的，不归某一档材质管 —— 浅色并不只属于云母/实色。
      appearance: 'dark',
      appearances: [
        { name: 'dark', label: '深色', dark: true, base_hex: '#0E1116' },
        { name: 'light', label: '浅色', dark: false, base_hex: '#F3F3F3' },
      ],
      materials: [
        { name: 'acrylic', label: 'Acrylic', kind: 3, opaque: false },
        { name: 'mica', label: 'Mica', kind: 2, opaque: false },
        { name: 'tabbed', label: 'Tabbed', kind: 4, opaque: false },
        { name: 'solid', label: 'Solid', kind: 1, opaque: true },
      ],
    },
  });
  p = await probe();
  // ★ 2026-09-21 主人裁定删「透明」档；2026-09-22 要求加「标签页」档 ⇒ 断言跟着改成 4 项。
  //   同日第三轮：原生 <select> → 自绘 ComboBox ⇒ 这里从"数卡片"改成"数菜单项"。
  //   改设计必须同步改断言，否则这里会一直"红着"，久了就没人看了。
  check('下拉里渲染出 4 个材质项（删「透明」+ 加「标签页」）', p.matOptions === 4, `matOptions=${p.matOptions}`);
  check('四档名字与顺序正确（无 transparency，有 tabbed）',
    JSON.stringify(p.matNames) === JSON.stringify(['acrylic', 'mica', 'tabbed', 'solid']),
    JSON.stringify(p.matNames));
  check('当前材质（亚克力）是选中态', p.matSel === 'acrylic', `matSel=${p.matSel}`);
  /* ★★ 2026-09-22 第四轮：主人一张截图 + 两句话 ——「把这些关键字全换成英语的，要不然太土了。
     纯色不要写最省电，写界面更纯净」。三条判据，一条正、一条负、一条结构：
       · 正：四个显示名就是英文（Acrylic/Mica/Tabbed/Solid）——名字来自宿主 label，
             前端只负责显示 ⇒ 这条同时钉住了宿主载荷与渲染两侧。
       · 负：设置页里**再也搜不到**旧中文档位名（亚克力/云母/标签页/纯色）与「最省电」。
             删除类改动没有正标记，只能靠"旧串不在"。
       · 结构：每项**恰好一段说明**（`.dd-h` 数 == 1）。说明是组件按 `' — ' + hint` 拼进
             `.dd-tx` 的，调用方若把说明同时写进 label 和 hint 就会读成 2 —— 一屏里同一句话
             出现两遍。⚠ 这条是我差点错杀产品换来的：我自己的对照探针直接读 `tx.textContent`，
             把子元素 `.dd-h` 的文本也算进名字里，于是误报"说明重复"。**量 DOM 要挑对口径。** */
  check('★ 四个档位名是英文（Acrylic / Mica / Tabbed / Solid）',
    JSON.stringify(p.matLabels) === JSON.stringify(['Acrylic', 'Mica', 'Tabbed', 'Solid']),
    JSON.stringify(p.matLabels));
  {
    // ⚠ 读数在自检对象上（`p.settingsText`），不是本作用域的裸变量 —— 我第一版写成裸的，
    //   探针当场 ReferenceError 挂掉，后面几十条断言一条都没跑（"探针自身出错"比 FAIL 更毒：
    //   它让你以为"没红就是绿的"）。取一次存变量，顺带别对同一个串跑三遍正则。
    const settingsText = p.settingsText || '';
    const oldHits = /亚克力|云母|标签页|纯色|最省电/.exec(settingsText);
    check('★★ 设置页里搜不到旧中文档位名与「最省电」（负判据）',
      !!settingsText && !oldHits,
      oldHits ? ('命中: ' + oldHits[0]) : ('干净（正文 ' + settingsText.length + ' 字）'));
  }
  check('★ 每项恰好一段说明（说明没被 label + hint 写两遍）',
    !!p.matHintCounts && p.matHintCounts.length === 4 && p.matHintCounts.every(function (n) { return n === 1; }),
    JSON.stringify(p.matHintCounts));
  /* ★★ 2026-09-22 第三轮：主人给了一张截图 ——「原生下拉那层系统灰 + 触发器上一圈白框」。
     根因是 <select> 的弹窗由 Chromium 自己画、不在 DOM 里，CSS 碰不到。
     下面两条是这次改动的**正标记**（删除类改动没有正标记，只能靠"新东西在不在"）：
       · 触发器必须是自绘的 <button class="dd">（不是 <select>）
       · 菜单必须是挂在 body 下、position:fixed 的独立浮层（留在卡片里会被 overflow 裁掉）
     外加一条负判据：设置页里**一个原生 <select> 都不能剩** —— 剩一个，那层灰就回来。 */
  check('★ 触发器是自绘 ComboBox（<button class="dd">，不是 <select>）', p.ddIsButton === true, `ddIsButton=${p.ddIsButton}`);
  check('★ 菜单是 body 下的 fixed 独立浮层（不会被卡片 overflow 裁掉）', p.ddFloats === true, `ddFloats=${p.ddFloats}`);
  check('★★ 设置页里没有原生 <select> 了（有的话那层系统灰弹窗就回来）',
    p.natSelects === 0, `natSelects=${p.natSelects}`);
  /* ★★ 同日的两条"不要"，继续用负判据钉住（卡片 / 预览框 / 胶囊都不该回来了）：
       卡片 .mcard、预览框 .sw、深浅胶囊 .dlight —— 容器已经整个删掉，数量必须是 0。 */
  check('★★ 没有材质卡片了（主人要去掉的 .mcard）', p.matCards === 0, `matCards=${p.matCards}`);
  check('★★ 没有材质预览框（主人要去掉的 .sw）', p.matSwatches === 0, `matSwatches=${p.matSwatches}`);
  check('★★ 没有深/浅胶囊了（.dlight / 已换成下拉）',
    p.matChips === 0 && p.appearPills === 0, `matChips=${p.matChips} appearPills=${p.appearPills}`);

  await clearSent();
  // 自绘下拉：先点触发器展开，等入场动画（Fluent 浮层 .22s）跑完，再点菜单里的「云母」。
  await clickEl('#matSelect');
  await sleep(280);
  const menuOpen = await ev(`(function(){
    var m = document.getElementById('matSelect-menu');
    return m && !m.hidden ? 'open' : 'closed';
  })()`);
  check('★ 点触发器能把菜单展开（自绘浮层真的会弹）', menuOpen === 'open', `menu=${menuOpen}`);
  await clickEl('#matSelect-menu .dd-opt[data-v="mica"]');

  // ★ 过场动画（2026-09-21 新增，主人报「材质切换生硬」）：
  //   点下去的**那一瞬间不该发** setmaterial —— 幕布要先快暗下来（90ms），
  //   材质那一跳才藏得住。所以这条断言查的是"还没发"，不是"发了"；
  //   反过来写就永远绿，等于没测。
  const justNow = await sent();
  check('点下去的瞬间先不发切换（幕布要先暗下来盖住跳变）',
    !justNow.some((x) => x.type === 'setmaterial'), JSON.stringify(justNow));
  const veilState = await ev(`(function(){
    var v = document.getElementById('shell-mat-veil');
    return v ? (v.classList.contains('on') ? 'on' : 'off') : 'missing';
  })()`);
  check('过场幕布已挂上并压暗（#shell-mat-veil.on）', veilState === 'on', veilState);

  await new Promise((r) => setTimeout(r, 240));
  s = await sent();
  const setmat = s.filter((x) => x.type === 'setmaterial');
  check('约 90ms 后才真发 setmaterial(name=mica)',
    setmat.length === 1 && setmat[0].name === 'mica', JSON.stringify(s));

  // 🔴 这条是补的：卡片高亮必须**当场**跟着点击动，不能只等宿主的 material 回发。
  //    回发一旦丢一条（实测发生过），UI 就永远停在上一档 —— 表现出来是
  //    "我明明点了亚克力，界面显示的还是云母"，看着像设置没生效。
  p = await probe();
  check('点完立刻高亮到「云母」，不等宿主回发', p.matSel === 'mica', `matSel=${p.matSel}`);
  await feed({ type: 'material', name: 'mica', kind: 2, opaque: false });
  p = await probe();
  check('宿主确认后高亮切到云母', p.matSel === 'mica', `matSel=${p.matSel}`);
  // 宿主回信 ⇒ 幕布开始退场（60ms 后摘掉 .on，再走 340ms 减速曲线淡出）。
  // 等它退完再查，免得查到"正在退场中"这个中间态。
  await new Promise((r) => setTimeout(r, 280));
  const veilAfter = await ev(`(function(){
    var v = document.getElementById('shell-mat-veil');
    return v ? (v.classList.contains('on') ? 'on' : 'off') : 'missing';
  })()`);
  check('宿主确认后幕布退场（不再压暗页面）', veilAfter === 'off', veilAfter);
  const matStatus = await ev('document.getElementById("matStatus").textContent');
  check('状态行给出反馈', /已切换为/.test(matStatus), matStatus);

  /* ==================================================================
     ②b 深色 / 浅色（2026-09-22。主人两轮的话：
         第一轮 "点到云母和实色的时候都跳出来一个深色和浅色的选项"；
         第二轮 "不要这么显示了，我看到亚克力也有白色版本，所以直接新开一个位置
                 显示深色浅色切换就行" + "浅色模式的时候看不到控件的背景和框框了"）
     验四层，缺一层都只是"看着像做了"：
       ① 深浅那一行在**材质下面另起一行**且占位正常，卡片里一个都没有（负判据在 ② 那段）；
       ② 它**始终可见**，跟"选哪档材质"无关（切到亚克力也照旧在）；
       ③ 点浅色会发 setappearance、不会顺手发 setmaterial（点穿）；
       ④ **CSS 真的换了一套** —— 读 <html> 属性 + 计算出来的 token 值，
          不读页面那个 JS 变量（变量与属性脱节时，主人眼睛看到的才算数）。
     ================================================================== */
  console.log('');
  console.log('—— ②b 深色 / 浅色（2026-09-22）——');
  p = await probe();
  check('★★ 深浅这一行**另起一行**且占位正常（主人说的"新开一个位置"）',
    !!p.appearBtns && p.appearBtns.length === 2 && p.appearRowH > 0,
    `选项=${JSON.stringify(p.appearBtns)} 行高=${p.appearRowH}px`);
  check('两个选项就是「深色 / 浅色」',
    JSON.stringify(p.appearBtns) === JSON.stringify(['深色', '浅色']), JSON.stringify(p.appearBtns));
  // ★ 主人给的理由本身就是判据："亚克力也有白色版本" ⇒ 切到亚克力，这一行必须还在。
  //   第三轮改成自绘下拉后，点选项多一步"先展开"。
  await clickEl('#matSelect');
  await sleep(280);
  await clickEl('#matSelect-menu .dd-opt[data-v="acrylic"]');
  await feed({ type: 'material', name: 'acrylic', kind: 3, opaque: false });
  await sleep(260);
  p = await probe();
  check('★★ 切到「亚克力」这一行照旧在（明暗与材质解耦 —— 这正是"亚克力也有浅色版"）',
    p.matSel === 'acrylic' && !!p.appearBtns && p.appearBtns.length === 2 && p.appearRowH > 0,
    `材质=${p.matSel} 选项=${JSON.stringify(p.appearBtns)} 行高=${p.appearRowH}px`);
  check('★ 只切材质不会把明暗掰回去（应当还是深色）', p.theme === 'dark', `theme=${p.theme}`);

  await clearSent();
  await clickEl('#appearSelect');
  await sleep(280);
  const aMenuOpen = await ev(`(function(){
    var m = document.getElementById('appearSelect-menu');
    return m && !m.hidden ? 'open' : 'closed';
  })()`);
  check('★ 深浅下拉也能展开', aMenuOpen === 'open', `menu=${aMenuOpen}`);
  const hitChip = await clickEl('#appearSelect-menu .dd-opt[data-v="light"]');
  check('找得到「浅色」那一项', hitChip === 'OK', hitChip);
  check('点下去的瞬间先不发 setappearance（幕布要先暗下来盖住整片配色跳变）',
    !(await sent()).some((x) => x.type === 'setappearance'), JSON.stringify(await sent()));
  await sleep(240);
  s = await sent();
  const setapp = s.filter((x) => x.type === 'setappearance');
  check('约 90ms 后才真发 setappearance(name=light)',
    setapp.length === 1 && setapp[0].name === 'light', JSON.stringify(s));
  // 🔴 点穿检查：上一版胶囊长在卡片里，卡片自己也有 click，漏了 stopPropagation 就会
  //    顺手再发一条 setmaterial（用户看到"点一下浅色，材质也闪了一次"）。
  //    现在胶囊已搬出卡片，结构上点不穿 —— 但断言留着，防以后又被挪回卡片里。
  check('★ 点浅色没有点穿（不该顺带发 setmaterial）',
    !s.some((x) => x.type === 'setmaterial'), JSON.stringify(s));

  const lum = (css) => {
    const m = /^#?([0-9a-f]{6})$/i.exec(String(css).trim());
    if (!m) return null;
    const n = parseInt(m[1], 16);
    return (0.2126 * ((n >> 16) & 255) + 0.7152 * ((n >> 8) & 255) + 0.0722 * (n & 255)) / 255;
  };
  /* rgba() 解析 + "是不是黑系"。⚠ 用数值比较而不是字符串比较：
     Chromium 对 alpha 的打印格式会在 0.0605 / 0.061 之间变，字符串比会假红。 */
  const rgba = (css) => {
    const m = /^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+))?\s*\)$/.exec(String(css).trim());
    if (!m) return null;
    return { r: +m[1], g: +m[2], b: +m[3], a: m[4] === undefined ? 1 : +m[4] };
  };
  const isBlack = (c) => !!c && (c.r + c.g + c.b) / 3 < 60;
  /* 🔴 容差必须 ≥ 0.005：Chromium 把计算出来的颜色**量化/序列化成 2 位小数**，
     `rgba(255,255,255,.0605)` 读回来是 `rgba(255, 255, 255, 0.06)`、
     `.069` 读回来是 `0.07`。用 0.001 的容差会拿"打印格式"当成"数值被改了"。
     （这一条是踩过的：第一版断言就是被 0.07 与 0.069 的 0.001 差判红的。） */
  const nearCol = (css, r, g, b, a) => {
    const c = rgba(css);
    return !!c && c.r === r && c.g === g && c.b === b && Math.abs(c.a - a) < 0.008;
  };
  const readPal = () => ev(`(function(){
    var cs = getComputedStyle(document.documentElement);
    var g = function(n){ return cs.getPropertyValue(n).trim(); };
    return JSON.stringify({
      theme: document.documentElement.getAttribute('data-theme'),
      text: g('--text'), dim: g('--text-dim'), frame: g('--fill-frame'), card: g('--fill-card'),
      line: g('--border'), hover: g('--fill-hover'), veil: g('--shell-veil-color'),
      bodyColor: getComputedStyle(document.body).color,
      ls: (function(){ try{ return localStorage.getItem('dsh.appearance'); }catch(e){ return 'ERR'; } })()
    });
  })()`).then(JSON.parse);

  p = await probe();
  let pal = await readPal();
  check('★ <html data-theme> 真的翻到 light（属性才是驱动 CSS 的那一份）',
    p.theme === 'light' && pal.theme === 'light', `probe=${p.theme} html=${pal.theme}`);
  check('选中态标在「浅色」上', p.appearSel === 'light', `appearSel=${p.appearSel}`);
  check('★★ 浅色下正文是**深色**字（不是浅底配浅字）',
    lum(pal.text) !== null && lum(pal.text) < 0.35 && lum(pal.bodyColor) < 0.45,
    `--text=${pal.text} lum=${lum(pal.text) && lum(pal.text).toFixed(3)} body=${pal.bodyColor}`);
  check('★ 叠层也跟着换（浅色下 hover/描边要叠黑，白色叠层在白底上等于没有）',
    /^rgba\(0,\s*0,\s*0/.test(pal.line) && /^rgba\(0,\s*0,\s*0/.test(pal.hover),
    `--border=${pal.line} --fill-hover=${pal.hover}`);
  check('材质切换幕布在浅色下也换成浅色（黑幕在浅色主题里像闪了一下黑）',
    /^#F3F3F3$/i.test(pal.veil), `--shell-veil-color=${pal.veil}`);
  check('★ 值写进了 localStorage（下一次导航靠它摆正首帧，否则会闪一下）',
    pal.ls === 'light', `dsh.appearance=${pal.ls}`);

  /* ★★★ 2026-09-22 主人第三句："浅色模式的时候看不到控件的背景和框框了"。
     这类判据只能读**计算样式**：壳里的 shell-ui.css 是后加载的，它把 button / input /
     .card 的填充与描边整组盖掉了 —— 只 grep index.html 会得出"明明有 --border 啊"的假结论。
     两条判据分别对应他说的两个词：
       ① "框框" ⇒ 描边必须是**黑系且 alpha 够**（浅底上白描边 = 没画）
       ② "背景" ⇒ 填充必须是**压暗**（黑 alpha）。白叠白会收敛到白，控件就"消失"了 ——
          这正是当时的 bug：卡片已是近白，控件再叠白还是近白，差 1~2 阶。 */
  const ctl = p.ctlColors || {};
  const bl = rgba(ctl.btn && ctl.btn.line), pl2 = rgba(ctl.dd && ctl.dd.line);
  check('★★★ 浅色下按钮 / 下拉触发器有**看得见的框框**（描边是黑系、alpha ≥ 0.10）',
    isBlack(bl) && bl.a >= 0.10 && isBlack(pl2) && pl2.a >= 0.10,
    `按钮描边=${ctl.btn && ctl.btn.line} 下拉描边=${ctl.dd && ctl.dd.line}`);
  const bb = rgba(ctl.btn && ctl.btn.bg), pb = rgba(ctl.dd && ctl.dd.bg);
  check('★★★ 浅色下按钮 / 下拉触发器有**看得见的背景**（填充走"压暗"的黑 alpha，不是白叠白）',
    isBlack(bb) && bb.a >= 0.02 && isBlack(pb) && pb.a >= 0.02,
    `按钮底=${ctl.btn && ctl.btn.bg} 下拉底=${ctl.dd && ctl.dd.bg}`);
  const cl = rgba(ctl.card && ctl.card.line);
  check('★★ 浅色下卡片也有框框（不是一张没边的白纸）',
    isBlack(cl) && cl.a >= 0.06, `卡片描边=${ctl.card && ctl.card.line}`);

  // 再切回深色：既验"能切回去"，也把深色那套 token 钉住 ——
  // 加浅色主题不该顺手改到深色（主人已经在用深色，改了就是"你动了没让你动的地方"）。
  await clickEl('#appearSelect');
  await sleep(280);
  await clickEl('#appearSelect-menu .dd-opt[data-v="dark"]');
  await sleep(300);
  pal = await readPal();
  p = await probe();
  check('切回深色后属性回到 dark', pal.theme === 'dark', `theme=${pal.theme}`);
  check('★★ 深色那套 token 与加浅色主题之前逐字节相同（深色没被改坏）',
    lum(pal.text) > 0.7 && pal.frame === 'rgba(255,255,255,0.04)' && pal.line === 'rgba(255,255,255,0.10)',
    `--text=${pal.text} --fill-frame=${pal.frame} --border=${pal.line}`);
  check('深色下不设幕布色 ⇒ 走 shell-ui.css 里那个默认深色（深色没被改）',
    pal.veil === '', `--shell-veil-color=${pal.veil || '(未设 → 默认 #0b0e13)'}`);
  /* ★★ 同上，但这次钉的是**控件那组 token**（本轮把它们从字面量抽成了变量）：
     抽变量的风险正是"顺手改了深色的数值"，所以要求三个数与原字面量**逐值相同**：
       button 底 rgba(255,255,255,.0605) / button 描边 .069 / 卡片描边 .075 */
  const dc = p.ctlColors || {};
  check('★★ 深色控件色与抽 token 之前逐值相同（抽变量没顺手改深色）',
    nearCol(dc.btn && dc.btn.bg, 255, 255, 255, 0.0605) &&
    nearCol(dc.btn && dc.btn.line, 255, 255, 255, 0.069) &&
    nearCol(dc.card && dc.card.line, 255, 255, 255, 0.075),
    `按钮底=${dc.btn && dc.btn.bg} 按钮描边=${dc.btn && dc.btn.line} 卡片描边=${dc.card && dc.card.line}`);

  console.log('');
  console.log('—— ① 工作台入口：无历史拦死 / 有历史才能进（schedule 第 1 条）——');
  await clearSent();
  await clickNav('workbench');
  s = await sent();
  // ★ 2026-09-28 列表口子改造：前端不再向 C# 宿主 postMessage listmidis，
  //   改为直接 fetch chartgen 网关的 /api/projects（由 C# Gateway 反代到 sidecar）。
  //   断言跟着改成"真的发起了对 /api/projects 的请求"，与新的取数契约一致。
  const fetchedApi = await ev('(window.__FETCHED||[]).some(function(u){ return String(u).indexOf("/api/projects") >= 0; })');
  check('点「工作台」会向 chartgen 网关请求工程列表（fetch /api/projects）', fetchedApi);

  // ★ 新契约下"后端拿不到列表"走 projectsFail()，文案是"工程列表取得失败：…"
  //   （含"无法连接后端"），不再是旧的"重新双击启动"。判据跟着改：只要报错文案
  //   确实显示了、且不是静默空白，就算过关（与 2026-09-27 那次"不留空白"诉求一致）。
  await sleep(2800);
  p = await probe();
  check('★ 列表取得失败时不留空白（明确报错文案）',
    p.wbEmptyShown === true && /工程列表取得失败|无法连接后端/.test(p.wbEmptyText), p.wbEmptyText);

  await feed({ type: 'midis', items: [], count: 0, work_dir: '<REPO>\\output\\.work' });
  p = await probe();
  check('★ 无历史：显示拦阻卡', p.wbEmptyShown === true);  check('★ 无历史：不显示选择列表', p.wbPickShown === false || p.wbPickShown === null);
  check('★ 无历史：「前往工作台」按钮禁用（进不去主界面）', p.wbBtnDisabled === true);
  check('★ 无历史：文案就是主人要的那句',
    /暂未对音乐采点或无历史记录，请生成后再试吧/.test(p.wbEmptyText), p.wbEmptyText);
  const goGen = await ev('!!document.getElementById("wbGoGen")');
  check('拦阻卡里给了「去生成谱面」的出口', goGen === true);

  await feed({ type: 'midis', items: MIDI_ITEMS, count: 2, work_dir: 'D:\\out\\.work' });
  p = await probe();
  check('★ 有历史：隐藏拦阻卡、显示列表', p.wbEmptyShown === false && p.wbPickShown === true);
  check('★ 列出全部生成过的 MIDI（本例 2 条）', p.midiRows === 2, `midiRows=${p.midiRows}`);
  check('刚进列表时还没选中，按钮仍禁用', p.wbBtnDisabled === true);
  const pickTip = await ev('document.getElementById("wbInfo").textContent');
  check('提示"请点选一条"', /请点选/.test(pickTip), pickTip);

  await clickEl('#midiList .item[data-i="0"]');
  p = await probe();
  check('★ 点一行后该项选中', p.midiSel === 0, `midiSel=${p.midiSel}`);
  check('★ 选中后「前往工作台」可用', p.wbBtnDisabled === false);
  check('提示里报出选中的是哪一首', /已选/.test(p.wbInfo) && p.wbInfo.includes('Demo Song'), p.wbInfo);
  check('★ 有整曲音乐时会说明"自动载入音乐"', /自动载入这次用的音乐/.test(p.wbInfo), p.wbInfo);

  await clearSent();
  await clickEl('#openWbBtn');
  // ★ 2026-09-28：openWbBtn 现在先播 200ms 左滑离场动画、再 post，所以等一下再读消息。
  await new Promise((r) => setTimeout(r, 350));
  s = await sent();
  const nav = s.filter((x) => x.type === 'open_in_workbench');
  check('★ 点「前往工作台」带上选中的 MIDI 路径',
    nav.length === 1 && nav[0].path === MIDI_ITEMS[0].path, JSON.stringify(s));

  await clickEl('#midiList .item[data-i="1"]');
  p = await probe();
  check('换选另一条（无音乐的那条）', p.midiSel === 1);
  check('无整曲音乐时如实提示"进去后手动选音源"',
    /手动选音源/.test(p.wbInfo), p.wbInfo);

  console.log('');
  console.log('—— ③ 关于页：explain.md（schedule 第 3 条）——');
  await clearSent();
  await clickNav('about');
  s = await sent();
  check('点「关于」会同时要 appinfo 与 getexplain',
    s.some((x) => x.type === 'appinfo') && s.some((x) => x.type === 'getexplain'),
    JSON.stringify(s.map((x) => x.type)));

  await feed({
    type: 'explain', ok: false, path: '',
    candidates: ['<REPO>\\gui\\explain.md', '<REPO>\\explain.md'],
  });
  p = await probe();
  check('★ 没有 explain.md 时显示缺件提示', p.explainMissingShown === true);
  check('★ 缺件时把"该放哪儿"写清楚（首选路径）',
    p.explainPath === '<REPO>\\gui\\explain.md', p.explainPath);
  check('缺件时给「按此路径新建模板」与「打开所在目录」两个按钮',
    (await ev('!!document.getElementById("mkExplainBtn") && !!document.getElementById("openExplainDir")')) === true);
  // ★ 2026-09-26 主人裁定（拿截图质问）：关于页**不许**再写
  //   "读取程序目录下的 explain.md，按 Markdown 渲染。当前路径：D:\…" —— 那是给开发者看的说明 + 本机绝对路径。
  //   下面两条是**负对照**：谁把它改回来，这两条必红（且与"有没有 explain.md"无关，缺件态也成立）。
  check('★ 关于页没有解释实现的那行提示（.hint 应为空）', p.aboutHint === '', JSON.stringify(p.aboutHint));
  check('★ DOM 里已无 #explainPath 节点',
    (await ev('!!document.getElementById("explainPath")')) === false);

  await clearSent();
  await clickEl('#mkExplainBtn');
  s = await sent();
  const mk = s.filter((x) => x.type === 'mkexplain');
  check('点「新建模板」会发 mkexplain 且带上目标路径',
    mk.length === 1 && mk[0].path === '<REPO>\\gui\\explain.md', JSON.stringify(s));
  // ⚠ 顺序要紧：清空必须在 feed **之前**。第一版写成 feed→clearSent→sleep→读，
  //   结果把"写入成功会自动重新读取"的那条 getexplain 亲手清掉了，白报一次失败。
  await clearSent();
  await feed({ type: 'explain_written', ok: true, path: '<REPO>\\gui\\explain.md' });
  await sleep(150);
  const reRead = await sent();
  check('写入成功后自动重新读取（getexplain）', reRead.some((x) => x.type === 'getexplain'),
    JSON.stringify(reRead.map((x) => x.type)));

  await feed({ type: 'explain', ok: true, path: '<REPO>\\gui\\explain.md', text: MD_SAMPLE, size: '1.2 KB' });
  p = await probe();
  check('★ 有文件时隐藏缺件提示', p.explainMissingShown === false);
  // ★ 正常态整页也不许出现"当前路径 / 按 Markdown 渲染"（缺件态那句"放到这个路径"是有用的指引，不算）
  check('★ 正常态整页文案不含"当前路径"/"按 Markdown 渲染"',
    (await ev('(function(){var t=document.getElementById("page-about").textContent||"";'
      + 'return t.indexOf("当前路径")<0 && t.indexOf("按 Markdown")<0;})()')) === true);
  // ★ 2026-09-26 追加（主人第二次截图裁定「这句也不要的」）：关于页标题底下原来还有一行副标题 ——
  //   "ADOFAI Studio —— Adofai-Chart-Generator 引擎 + 我们的 Mica 原生壳。"
  //   同属"自我署名 / 给开发者看"的口径（还写着"我们的"，而这是要发给别人的包）⇒ 整行删掉。
  //   下面这条是**负对照**：谁把那句写回来，立刻红。
  //   ⚠ 表达式必须是**单行**（CDP Runtime.evaluate 送多行模板串会 SyntaxError）。
  check('★ 关于页没有自我署名的副标题（无「Mica 原生壳」/「我们的」）',
    (await ev('(function(){var t=document.getElementById("page-about").textContent||"";'
      + 'return t.indexOf("Mica 原生壳")<0 && t.indexOf("我们的")<0;})()')) === true,
    await ev('(document.getElementById("page-about").textContent||"").slice(0,100)'));
  // 同一句话还抄在**主壳的「?」帮助弹窗**正文第一行（`IB_ABOUT`）里，2026-09-26 一并清掉；
  // 这里点开弹窗把正文读出来做负对照。点完关掉，免得后面的断言看到一层遮罩。
  await clickEl('#ibAbout');
  await sleep(80);
  const ibTxt = await ev('(document.getElementById("ibAboutBody")||{}).textContent || ""');
  check('★ 帮助弹窗正文不再自我署名（无「Mica 原生壳」/「我们自研」）',
    ibTxt.length > 0 && ibTxt.indexOf('Mica 原生壳') < 0 && ibTxt.indexOf('我们自研') < 0,
    ibTxt.slice(0, 60));
  await clickEl('#ibAboutClose');
  await sleep(60);
  const stTxt = await ev('(document.getElementById("explainStatus")||{}).textContent || ""');
  check('★ 正常态状态行是中文短句（不带 1.2 KB 的 Markdown）',
    stTxt.indexOf('已载入') >= 0 && stTxt.indexOf('KB') < 0, JSON.stringify(stTxt));
  check('★ Markdown 真的渲染成元素（h1/段落）', p.aboutMdHasH1 === true);
  const md = JSON.parse(await ev(`JSON.stringify((function(){
    var b = document.getElementById('aboutMd');
    return {
      h1: b.querySelectorAll('h1').length,
      h2: b.querySelectorAll('h2').length,
      li: b.querySelectorAll('li').length,
      strong: b.querySelectorAll('strong,b').length,
      code: b.querySelectorAll('code').length,
      pre: b.querySelectorAll('pre').length,
      quote: b.querySelectorAll('blockquote').length,
      hr: b.querySelectorAll('hr').length,
      th: b.querySelectorAll('th').length,
      td: b.querySelectorAll('td').length,
      raw: b.textContent.indexOf('**') >= 0,
      scripts: b.querySelectorAll('script').length,
      xss: !!window.__XSS
    };
  })())`));
  check('渲染出 h1', md.h1 === 1, JSON.stringify(md));
  check('渲染出 h2', md.h2 >= 1);
  check('渲染出列表项（2 条）', md.li === 2, `li=${md.li}`);
  check('渲染出表格（1 表头 + 4 单元格）', md.th === 2 && md.td === 4, `th=${md.th} td=${md.td}`);
  check('渲染出代码块与行内码', md.pre === 1 && md.code >= 2, `pre=${md.pre} code=${md.code}`);
  check('渲染出引用与分隔线', md.quote === 1 && md.hr === 1);
  check('** 没有被原样留在页面上（说明确实解析过）', md.raw === false);
  check('★ md 里的 <script> 被转义，没被执行也没进 DOM',
    md.scripts === 0 && md.xss === false, JSON.stringify({ scripts: md.scripts, xss: md.xss }));

  console.log('');
  console.log('—— ⑨ 训练采点模型页（schedule 第 9 条）——');
  await clearSent();
  await clickNav('train');
  s = await sent();
  check('点「训练采点模型」会向宿主问体检信息（traininfo）', s.some((x) => x.type === 'traininfo'));
  await feed({
    type: 'traininfo',
    data: {
      script: '', script_ok: false,
      script_candidates: ['<REPO>\\audio-sep\\train_onset.py'],
      data_dir: '', audio_count: 0,
      data_candidates: ['<REPO>\\dataset\\melody'],
      weights: [], py: '<REPO>\\audio-sep\\runtime\\python.exe',
      gpu: { ok: true, name: 'NVIDIA GeForce RTX 4070 Laptop GPU', vram: '8188 MiB' },
    },
  });
  p = await probe();
  check('体检表有 6 行（脚本/显卡/权重/数据集/样本数/Python）', p.trainRows === 6, `trainRows=${p.trainRows}`);
  check('显卡信息如实显示', /RTX 4070/.test(p.trainText) && /8188 MiB/.test(p.trainText), p.trainText);
  check('★ 没有训练脚本时如实说明（不假装能用）', /没有训练代码/.test(p.trainText), p.trainText);
  check('告诉主人该把脚本放哪', /train_onset\.py/.test(p.trainText), p.trainText);
  const trainBtns = await ev('JSON.stringify([...document.querySelectorAll("#trainCard button")].map(b=>b.textContent))');
  check('★ 不放"开始训练"按钮（没有脚本时不摆假按钮，训练也绝不由界面拉起）',
    !/开始训练/.test(trainBtns), trainBtns);

  await feed({
    type: 'traininfo',
    data: {
      script: '<REPO>\\audio-sep\\train_onset.py', script_ok: true,
      script_candidates: ['<REPO>\\audio-sep\\train_onset.py'],
      data_dir: '<REPO>\\dataset\\melody', audio_count: 100,
      data_candidates: [], weights: ['a.pt', 'b.pt'],
      py: 'python.exe', gpu: { ok: true, name: 'RTX 4070', vram: '8188 MiB' },
    },
  });
  p = await probe();
  check('脚本就位后文案改成"由你亲自拍板"', /亲自拍板/.test(p.trainText), p.trainText);
  check('脚本就位后显示权重数量', /2 个/.test(p.trainText), p.trainText);

  console.log('');
  console.log('—— ⑩ 生成页：三种模型怎么选 ——');
  await clickNav('generate');
  p = await probe();
  check('生成页有「怎么选」说明区', p.pickHelp === true);
  check('说明区默认是收起的（不占版面）', p.pickHelpOpen === false, `open=${p.pickHelpOpen}`);
  await clickEl('#pickHelpHead');
  p = await probe();
  check('点标题能展开', p.pickHelpOpen === true);
  check('三种方式都写到了（osn1 / osn2 / midi）',
    /osn1/.test(p.pickHelpText) && /osn2/.test(p.pickHelpText) && /midi/.test(p.pickHelpText), p.pickHelpText.slice(0, 90));
  // ★ 2026-09-26 同步设计：这一条原来钉死"接通中"三个字，但页面上的标签早就改成了
  //   「功能开发中，敬请期待」（osn1=已接通 / midi=当前可用 / osn2=开发中）⇒ 断言成了常红。
  //   改成按**结构**判：只有真接通的通路才准带 .tag.ok，未接通的必须写"开发中"。
  // ⚠ 这条表达式**必须写成单行**：多行模板字符串送进 CDP 会被页面当语法错误拒掉
  //   （实测报 "missing ) after argument list"）。
  const PICK_TAGS_JS = 'JSON.stringify(Array.prototype.map.call('
    + 'document.querySelectorAll("#pickHelp .exp-pad > div"),'
    + ' function (r) { var t = r.querySelector(".tag"); if (!t) { return null; }'
    + ' return { name: ((r.querySelector("b") || {}).textContent || "").trim(),'
    + ' tag: t.textContent, ok: t.classList.contains("ok") }; })'
    + '.filter(function (x) { return !!x; }))';
  const pickTags = JSON.parse(await ev(PICK_TAGS_JS));
  const taggedOk = pickTags.filter((r) => r.ok).map((r) => r.name).join(' | ');
  const notYet = pickTags.filter((r) => !r.ok);
  check('★ 已接通的两条带 ok 标记（osn1 / midi）',
    /osn1/.test(taggedOk) && /midi/.test(taggedOk), taggedOk);
  check('★ 未接通的那条如实写"开发中"、且不带 ok 标记（不夸大）',
    notYet.length === 1 && /开发中|接通中/.test(notYet[0].tag), JSON.stringify(notYet));
  check('midi 通路标为当前可用', /当前可用/.test(p.pickHelpText));
  check('写明精度门槛（2 GB / 4 GB / 8 GB）',
    /2 GB/.test(p.pickHelpText) && /4 GB/.test(p.pickHelpText) && /8 GB/.test(p.pickHelpText),
    p.pickHelpText.slice(-160));

  console.log('');
  console.log('—— 页面异常 ——');
  // 只放过浏览器自己会请求的 favicon（页面没写 <link rel=icon>，必然 404，与我们无关）
  const real = events.filter((e) => !/favicon/i.test(e));
  check('全程没有 JS 运行时异常', real.length === 0, real.slice(0, 3).join(' | '));

  const pass = results.filter((r) => r.ok).length;
  console.log('');
  console.log('='.repeat(68));
  console.log(`${pass} 通过 / ${results.length - pass} 失败`);
  if (results.some((r) => !r.ok)) {
    console.log('失败项：');
    results.filter((r) => !r.ok).forEach((r) => console.log('  - ' + r.name));
  }
  console.log('='.repeat(68));
  try { child.kill(); } catch (_e) { /* 无所谓 */ }
  process.exit(results.some((r) => !r.ok) ? 1 : 0);
})().catch((e) => {
  console.error('探针自身出错：', e && e.stack || e);
  process.exit(2);
});
