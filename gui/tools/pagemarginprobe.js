/* pagemarginprobe.js —— 量「内容面板的外边距 / 圆角 / 切页动画时长」，并出留证图。
 * ============================================================================
 * 背景（2026-09-22 主人原话）：
 *   "你看右边界面距离最边界和按钮距离最边界，能不能把界面距离调的和按钮距离边界的
 *    距离调成一样的，还有那个界面的圆角，能不能看着和程序边界完全重叠。
 *    还有切换到每个界面的时候，他那个上浮动画再慢一点，太快了看着不舒服。"
 * 第十五轮裁定：① 页边四边 8px；② 面板圆角 8px（明显圆角、接近窗口 9px，放弃严格同心）；
 *   ③ 切页上浮幅度 16px / 时长 .7s（原 6px/.6s 动静太小）。
 * 第十六轮纠正：第十五轮我达成"左右对称"的手段是**把侧栏右内边距归零**，等于把按钮框
 *   从 40px 撑宽到 46px —— 主人驳回："**我是让你把界面往边靠，不是让你把按钮往右延长一点**"。
 *   ⇒ 改为**动面板那一侧**：#main 左页边 8px→2px（面板左缘 x=54，按钮框右缘 x=46，距离 8px）。
 *   按钮与侧栏内边距全部还原（左右各 6px，按钮框 40px）。
 *
 * ⭐ 核心道理：左右两侧**不是同一个源** —— 右边直接对窗口边界，左边要先跨过侧栏(52px)、
 *   再退掉按钮框自己的 6px 右留白。所以"肉眼可见的三个 8px"反推出的 padding 是 8/8/8/2，
 *   **判据必须盯"距离"（gap），不能盯 padding 字面值**。这三条都可量数值，不靠眼睛判：
 *   ① 面板右缘距 viewport 右缘 = 8；面板底缘距 viewport 底缘 = 8；**面板左缘距侧栏按钮框右缘 = 8**
 *      （左右对称，原来 14px 不对称）
 *   ② 面板圆角四角均 = 8px（≥4px 即"明显圆角"，不再方）
 *   ③ .page.active 的 animation-duration = .7s（原 .6s；translateY 6px→16px 幅度更大）
 *   ④ **按钮框宽 = 40px**（第十六轮新增：防"再靠撑宽按钮去凑距离"这类改法卷土重来）
 *
 * ⚠ 判据只测"改的这一处"，不重复测别的探针已经盖住的东西。
 * 用法：node tools/pagemarginprobe.js [url] [输出目录]
 *   url 省略时自起静态服务（纯外观，不需要后端；页面自己不带底色，headless 里
 *   必须垫 Mica 基色，否则白底跟主人看到的深色完全两样）。
 * ========================================================================== */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const ROOT = '<REPO>\\gui';
const PORT = 8811;                       // 静态服务（仅在 url 省略时启用）
const CDP = 9351;
const CHROME = process.argv[3] || 'chrome';
const OUT = process.argv[4] || '<REPO>\\output\\logs\\pagemargin';
const BACKDROP = '#0E1116';              // 材质基色（主人那台实测档位）
const USE_URL = process.argv[2] || '';
const DSF = 1.5;                         // 主人那台 150% 缩放
const W = 1100, H = 760;
const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png' };

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = (p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: CDP, path: p }, (r) => {
    let b = ''; r.on('data', (c) => (b += c)); r.on('end', () => res(b));
  }).on('error', rej);
});

function serve() {
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      let p = decodeURIComponent(req.url.split('?')[0]);
      if (p === '/') p = '/index.html';
      fs.readFile(path.join(ROOT, p), (e, buf) => {
        if (e) { res.writeHead(404); res.end('nf'); return; }
        res.writeHead(200, { 'Content-Type': MIME[path.extname(p)] || 'application/octet-stream' });
        res.end(buf);
      });
    });
    srv.listen(PORT, () => resolve(srv));
  });
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const srv = USE_URL ? null : await serve();
  const url = USE_URL || ('http://127.0.0.1:' + PORT + '/index.html');

  const child = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', '--mute-audio', '--remote-debugging-port=' + CDP,
    '--user-data-dir=<REPO>\\output\\.tmp\\pagemargin-profile',
    '--window-size=' + W + ',' + H, 'about:blank',
  ], { stdio: 'ignore', windowsHide: true });

  let list = [];
  for (let i = 0; i < 80; i++) {
    try { list = JSON.parse(await get('/json/list')); if (list.some((t) => t.type === 'page')) break; }
    catch (_e) { /* 还没起来 */ }
    await sleep(250);
  }
  const page = list.find((t) => t.type === 'page');
  if (!page) throw new Error('没能连上 headless 浏览器的 DevTools');

  const ws = new WebSocket(page.webSocketDebuggerUrl);
  const waiters = new Map();
  let seq = 0;
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && waiters.has(m.id)) { waiters.get(m.id)(m); waiters.delete(m.id); }
  });
  await new Promise((r) => ws.addEventListener('open', r));
  const send = (method, params) => new Promise((res, rej) => {
    const id = ++seq;
    waiters.set(id, (m) => (m.error ? rej(new Error(method + ': ' + m.error.message)) : res(m.result)));
    ws.send(JSON.stringify({ id, method, params: params || {} }));
  });
  const ev = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) throw new Error('page err: ' + JSON.stringify(r.exceptionDetails.exception));
    return r.result && r.result.value;
  };

  await send('Page.enable');
  await send('Emulation.setDeviceMetricsOverride', { width: W, height: H, deviceScaleFactor: DSF, mobile: false });
  await send('Page.navigate', { url });
  for (let i = 0; i < 60; i++) {
    if (await ev('!!document.getElementById("nav-ind")')) break;
    await sleep(300);
  }
  await sleep(1400);

  // 垫底色（页面侧栏/外框都是 transparent，靠 DWM 材质当底；headless 没材质 ⇒ 必须自己垫）
  await ev('document.documentElement.style.background=' + JSON.stringify(BACKDROP) + ';1');
  await sleep(300);

  const geo = await ev(`(function(){
    var R = function(el){ if(!el) return null; var r = el.getBoundingClientRect();
      return { x:+r.left.toFixed(2), y:+r.top.toFixed(2), right:+r.right.toFixed(2),
               bottom:+r.bottom.toFixed(2), w:+r.width.toFixed(2), h:+r.height.toFixed(2) }; };
    var cs = function(el){ return el ? getComputedStyle(el) : null; };
    var sb  = document.getElementById('sidebar');
    var nav = sb ? sb.querySelector('.nav') : null;
    var main= document.getElementById('main');
    var pg  = document.querySelector('#main .page.active');
    // ★ 2026-09-28 第二轮：切页动画从 .page.active 挪到了它的**直接子元素**上
    //   （.page.active > * { animation: fl-rise-in … }），外框（填充/圆角/滚动条）原地钉死。
    //   ⇒ 动画相关判据必须去读第一个直接子节点的 computed style，不能读 .page 自己（那是 none）。
    var pgChild = pg ? pg.querySelector(':scope > *') : null;
    var card= document.querySelector('#main .page.active .card') || document.querySelector('#main .card');
    var cm = cs(main), cp = cs(pg), cc = cs(card), cnav = cs(nav), cpc = cs(pgChild);
    return {
      dpr: devicePixelRatio, inner:[innerWidth, innerHeight],
      mainPad:[cm?cm.paddingTop:null, cm?cm.paddingRight:null, cm?cm.paddingBottom:null, cm?cm.paddingLeft:null],
      sidebarPad:[cs(sb)?cs(sb).paddingTop:null, cs(sb)?cs(sb).paddingRight:null,
                  cs(sb)?cs(sb).paddingBottom:null, cs(sb)?cs(sb).paddingLeft:null],
      sidebar: R(sb), nav1: R(nav), main: R(main), page: R(pg), card: R(card),
      navRadius: cnav?cnav.borderTopLeftRadius:null,
      pageRadius: cp?[cp.borderTopLeftRadius, cp.borderTopRightRadius, cp.borderBottomRightRadius, cp.borderBottomLeftRadius]:null,
      cardRadius: cc?cc.borderTopLeftRadius:null,
      // ★ 动画在子节点上：读子节点 computed style
      animName: cpc?cpc.animationName:null, animDur: cpc?cpc.animationDuration:null,
      animDelay: cpc?cpc.animationDelay:null, animEase: cpc?cpc.animationTimingFunction:null,
      pageOverflowY: cp?cp.overflowY:null
    };
  })()`);
  console.log('GEO ' + JSON.stringify(geo));

  const g = geo, checks = [];
  const push = (name, ok, detail) => checks.push({ name, ok: !!ok, detail });
  const gapR = +(g.inner[0] - g.page.right).toFixed(2);
  const gapB = +(g.inner[1] - g.page.bottom).toFixed(2);
  const gapL = +g.nav1.x.toFixed(2);
  // ★ 第十五轮新增：面板**左缘**到侧栏按钮框**右缘**的距离，必须等于右边距（左右对称）
  const gapLeftBtn = +(g.page.x - g.nav1.right).toFixed(2);

  push('面板右缘距窗口右缘 = 8px（第二轮：原 6px 偏窄）', Math.abs(gapR - 8) < 0.6, 'gapRight=' + gapR);
  push('面板底缘距窗口底缘 = 8px', Math.abs(gapB - 8) < 0.6, 'gapBottom=' + gapB);
  // ★ 主人第二轮的真正判据：右下角"往右"和"往下"必须等距，否则看着就是"不太一样"
  push('右下角等距：右距 == 底距（对称）', Math.abs(gapR - gapB) < 0.6, 'right=' + gapR + ' bottom=' + gapB);
  push('侧栏按钮左缘距窗口左缘 = 6px', Math.abs(gapL - 6) < 0.6, 'gapLeft=' + gapL);
  // ★ 第十五轮：面板到**左侧栏按钮框**的距离 = 面板到右框 = 8px（左右对称）
  push('面板左缘距侧栏按钮框右缘 = 8px（= 右边距，左右对称）', Math.abs(gapLeftBtn - 8) < 0.6, 'gapLeftBtn=' + gapLeftBtn);
  // ★ 第十六轮：主人驳回了"把按钮往右延长"的做法 ⇒ 加一条**按钮框宽度**判据。
  //   侧栏 52px − 左右内边距各 6px ⇒ .nav 框必须是 40px。一旦有人再把侧栏内边距动掉
  //   （比如又去归零右内边距），按钮框会被撑宽，这条就会 FAIL —— 补上第十五轮漏掉的这一环：
  //   当时我只测了"面板到按钮框右缘"这个**距离**，距离本身达标了，可按钮被拉长了，
  //   没有任何一条断言在看"按钮框本身多大"。
  const navW = g.nav1 ? +g.nav1.w.toFixed(2) : null;
  push('侧栏按钮框宽 = 40px（52 − 6×2，未被撑宽）', navW !== null && Math.abs(navW - 40) < 0.6, 'navW=' + navW);
  push('侧栏内边距左右对称（左 = 右）',
    g.sidebarPad[1] === g.sidebarPad[3], 'L=' + g.sidebarPad[3] + ' R=' + g.sidebarPad[1]);
  // ★ 第十六轮：页边**故意不再四边同值**。左右手不同源 —— 右侧直接对窗口边界，左侧要先跨过
  //   侧栏（52px）再退掉按钮框自己的 6px 右留白，所以"能看见的三个 8px"对应的是 #main 左页边 2px。
  //   判据得盯**距离**（上面三条 gap），不能盯 padding 字面值。
  push('#main 页边 = 8px 8px 8px 2px（左 2px 是跨过侧栏后的差额）',
    g.mainPad[0] === '8px' && g.mainPad[1] === '8px' && g.mainPad[2] === '8px' && g.mainPad[3] === '2px',
    JSON.stringify(g.mainPad));
  push('面板圆角 = 8px（四个角；= --rd-win，明显圆角、接近窗口 9px）',
    (g.pageRadius || []).every((v) => v === '8px'), JSON.stringify(g.pageRadius));
  push('面板圆角 ≥ 4px（明显圆角，不再方）',
    parseFloat(g.pageRadius[0]) >= 4, 'page=' + g.pageRadius[0] + ' card=' + g.cardRadius);
  // ★ 2026-09-29 第四轮（最终裁定）：动画在 .page.active 的**直接子节点**上
  //   （.page.active > * { fl-rise-in .34s 100ms }），外框钉死、内容 25vh 上浮。
  //   这 100ms 里**旧页在渐隐**（showPage 挂 .leaving 跑 page-leave .1s，opacity 1→0），
  //   新内容因 `both` fill + 100ms 延迟仍 opacity:0 透明 hold ⇒ 严格顺序、零叠字
  //   （主人原话："延迟 100ms 里不是空白，是上一个页面快速渐隐，然后下一个界面浮上来"）。
  push('外框仍是滚动容器（overflow-y:auto）', g.pageOverflowY === 'auto', 'overflowY=' + g.pageOverflowY);

  // 动画时长用**真实采样**交叉验证：切页后立刻读 getAnimations() + 中途 rect.top。
  // ⚠ 别调 ShellUI.showPage 之类的外部 API —— 那个名字不一定存在，一旦不存在就是
  //   "页根本没换、动画没跑"，但 rect 采样会老老实实报 0 位移 ⇒ 假 FAIL 白查一轮。
  //   直接改 class 最稳（.page.active{display:block} + animation 会随之重启）。
  const sampling = await ev(`(async function(){
    var wait = function(ms){ return new Promise(function(r){ setTimeout(r, ms); }); };
    var pages = document.querySelectorAll('#main .page');
    var cur = document.querySelector('#main .page.active');
    var tgt = null;
    for (var i = 0; i < pages.length; i++) { if (pages[i] !== cur) { tgt = pages[i]; break; } }
    if (!tgt) return { err: '只有一个 page，换不了页' };
    if (cur) cur.classList.remove('active');
    tgt.classList.add('active');
    // ★ 2026-09-28 第二轮：动画在 .page.active 的**直接子节点**上（.page.active > * { fl-rise-in }）。
    //   外框（.page.active 自己）原地钉死，只有子节点在动。
    var ch = tgt.querySelector(':scope > *');
    var an = (ch && ch.getAnimations) ? ch.getAnimations() : [];
    var info = { n: an.length, name: an[0] && an[0].animationName,
                 dur: an[0] && an[0].effect && an[0].effect.getTiming().duration,
                 delay: an[0] && an[0].effect && an[0].effect.getTiming().delay,
                 t0: an[0] && Math.round(an[0].currentTime) };
    var pgR0 = tgt.getBoundingClientRect();          // ★ 外框（该原地钉死）
    var r0 = ch.getBoundingClientRect();             // ★ 内容子节点
    await wait(120);
    var pgR1 = tgt.getBoundingClientRect();
    var r1 = ch.getBoundingClientRect();
    var ct1 = an[0] ? Math.round(an[0].currentTime) : null;
    await wait(800);
    var pgR3 = tgt.getBoundingClientRect();
    var r3 = ch.getBoundingClientRect();
    info.top0 = +r0.top.toFixed(2); info.top120 = +r1.top.toFixed(2); info.top920 = +r3.top.toFixed(2);
    info.pgTop0 = +pgR0.top.toFixed(2); info.pgTop120 = +pgR1.top.toFixed(2); info.pgTop920 = +pgR3.top.toFixed(2);
    info.ct120 = ct1; info.switchedTo = tgt.id;
    info.playState = an[0] ? an[0].playState : null;
    return info;
  })()`);
  console.log('SAMP ' + JSON.stringify(sampling));
  // ★ 子节点从 translateY(25vh) 上浮归位；120ms 时（100ms delay + 20ms 跑）还在中途，残余位移明显
  const midLift = sampling && sampling.top120 != null ? +(sampling.top120 - sampling.top920).toFixed(2) : null;
  const frameDelta = sampling && sampling.pgTop0 != null && sampling.pgTop920 != null ? +(sampling.pgTop0 - sampling.pgTop920).toFixed(2) : null;
  push('切页动画被真实创建（内容子节点 getAnimations 拿到 1 条）', sampling && sampling.n === 1, 'n=' + (sampling && sampling.n));
  push('动画对象自报名字 = fl-rise-in（内容滑、框不动）', sampling && sampling.name === 'fl-rise-in', 'name=' + (sampling && sampling.name));
  push('动画对象自报时长 = 340ms（=0.34s）', sampling && sampling.dur === 340, 'dur=' + (sampling && sampling.dur));
  push('动画对象自报延迟 = 100ms（=0.1s，Win11 手感）', sampling && sampling.delay === 100, 'delay=' + (sampling && sampling.delay));
  push('切页动画真的在跑（中途 rect 仍在位移）', midLift === null ? false : midLift > 0.3,
    '120ms 时上浮残余 = ' + midLift + 'px');
  push('采样终态已归位（内容 rect.top 回到静止值）',
    sampling && sampling.top920 != null && sampling.top0 - sampling.top920 > 3,
    't0=' + (sampling && sampling.top0) + ' → t920=' + (sampling && sampling.top920));
  // ★ 2026-09-28 第二轮核心诉求："框框固定不动"。内容在动、外框 rect.top 必须分毫不差。
  push('外框固定不动（切页前后 frame rect.top 差 < 0.6px）',
    frameDelta === null ? false : Math.abs(frameDelta) < 0.6,
    'frameΔtop=' + frameDelta);

  // ★ 2026-09-29 第四轮（最终裁定）：旧页渐隐 + 新页浮（严格顺序、零叠字）。
  //   必须走真实 showPage（点 nav）才触发 .leaving 渐隐分支，直接改 class 不会触发。
  const xf = await ev(`(async function(){
    var wait = function(ms){ return new Promise(function(r){ setTimeout(r, ms); }); };
    var navs = document.querySelectorAll('#sidebar .nav');
    var actPage = document.querySelector('#main .page.active');
    var t = null;
    for (var i = 0; i < navs.length; i++) {
      var np = navs[i].getAttribute('data-page');
      if (document.getElementById('page-' + np) !== actPage) { t = navs[i]; break; }
    }
    if (!t) return { err: '没有别的页，换不了页' };
    var oldPage = document.querySelector('#main .page.active');
    // 逐帧（16ms）采：旧页(.leaving) opacity + 新页内容(active 子节点) opacity
    var frames = [];
    var ft0 = performance.now();
    var iv = setInterval(function(){
      var tt = performance.now() - ft0;
      var lv = document.querySelector('#main .page.leaving');
      var act = document.querySelector('#main .page.active');
      var kid = act ? act.querySelector(':scope > *') : null;
      var lvOp = lv ? +getComputedStyle(lv).opacity : null;
      var kidOp = kid ? +getComputedStyle(kid).opacity : null;
      frames.push({ t: Math.round(tt), lvOp: lvOp, kidOp: kidOp });
      if (tt > 640) clearInterval(iv);
    }, 16);
    t.click();
    var lv0 = document.querySelector('#main .page.leaving');
    var info = { hasLeaving: !!lv0, leavingIsOld: lv0 ? (lv0 === oldPage) : null,
                 lvDisp: lv0 ? getComputedStyle(lv0).display : null };
    await wait(650);
    info.leavingGone = !document.querySelector('#main .page.leaving');
    // 结论
    var fByT = function(target){ var best = frames[0];
      for (var k=0;k<frames.length;k++){ if(Math.abs(frames[k].t-target)<Math.abs(best.t-target)) best=frames[k]; }
      return best; };
    var f20 = fByT(20), f50 = fByT(50);
    var reached0 = frames.filter(function(f){ return f.lvOp != null && f.lvOp <= 0.05; });
    // 🔴 关键：旧页半透明(0.1~0.9)的那些帧，新内容 opacity 必须仍≈0（严格顺序、不叠字）
    var overlap = frames.filter(function(f){ return f.lvOp != null && f.lvOp > 0.1 && f.lvOp < 0.9
                                             && (f.kidOp == null || f.kidOp > 0.1); });
    info.op20 = f20 ? f20.lvOp : null;
    info.op50 = f50 ? f50.lvOp : null;
    info.goneT = reached0.length ? reached0[0].t : null;
    info.frames = frames.length;
    info.overlapN = overlap.length;
    info.overlapSamples = overlap.slice(0,3);
    return info;
  })()`);
  console.log('XF ' + JSON.stringify(xf));
  push('交叉切换：旧页挂 .leaving 且是上一个页面（渐隐源正确、非空白）',
    xf && xf.leavingIsOld === true && xf.lvDisp !== 'none',
    JSON.stringify({ hasLeaving: xf && xf.hasLeaving, isOld: xf && xf.leavingIsOld, disp: xf && xf.lvDisp }));
  push('点击瞬间旧页仍满不透明（非空白：t≈20ms opacity>0.7）',
    xf && xf.op20 != null && xf.op20 > 0.7, 'op20=' + (xf && xf.op20));
  push('旧页在 ~100ms 内渐隐到透明（goneT ≤ 160ms，正好接新内容浮起）',
    xf && xf.goneT != null && xf.goneT >= 60 && xf.goneT <= 160, 'goneT=' + (xf && xf.goneT) + 'ms');
  push('🔴 零同显帧：旧页半透明时新内容仍透明（严格顺序、不叠字）',
    xf && xf.overlapN === 0,
    'overlapN=' + (xf && xf.overlapN) + '/' + (xf && xf.frames) + ' ' + JSON.stringify(xf && xf.overlapSamples));
  push('渐隐完 .leaving 已摘除（旧页回归 display:none）',
    xf && xf.leavingGone === true, 'gone=' + (xf && xf.leavingGone));

  // 采样把页面切走了 ⇒ 拍留证图之前切回第一页（图要能跟主人截图对照）
  await ev(`(function(){ var pages = document.querySelectorAll('#main .page');
    for (var i = 0; i < pages.length; i++) pages[i].classList.remove('active');
    pages[0].classList.add('active'); return pages[0].id; })()`);
  await sleep(900);

  const shot = async (name, clip, scale) => {
    const p = { format: 'png' };
    if (clip) p.clip = Object.assign({ scale: scale || 1 }, clip);
    const r = await send('Page.captureScreenshot', p);
    const f = path.join(OUT, name + '.png');
    fs.writeFileSync(f, Buffer.from(r.data, 'base64'));
    console.log('SAVED ' + f);
    return f;
  };

  await shot('0-full');
  // 左下角（窗口左缘 + 侧栏按钮 + 面板左下角）—— 放大后"两个圆角有没有对齐"直接可读
  await shot('1-bottom-left', { x: 0, y: H - 130, width: 170, height: 130 }, 4);
  // 右下角（面板右下角 + 窗口右缘）
  await shot('2-bottom-right', { x: W - 170, y: H - 130, width: 170, height: 130 }, 4);
  // ★ 第十六轮新增：**左侧接缝**（侧栏按钮 + 面板左缘同框，×4）—— 专给主人核"按钮有没有被拉长、
  //   面板有没有靠过来"看的。几何断言能说"宽 40px / 距 8px"，但"读起来像不像被拉长"只能靠这条缝的图。
  await shot('3-left-join', { x: 0, y: 88, width: 140, height: 200 }, 4);

  console.log('\n===== CHECKS =====');
  let bad = 0;
  for (const c of checks) { if (!c.ok) bad++; console.log((c.ok ? 'PASS  ' : 'FAIL  ') + c.name + '   [' + c.detail + ']'); }
  console.log(bad === 0 ? '\nALL PASS (' + checks.length + ')' : '\n' + bad + ' FAILED');

  ws.close();
  try { child.kill(); } catch (_e) { /* ignore */ }
  if (srv) try { srv.close(); } catch (_e) { /* ignore */ }
  console.log('DONE');
  process.exit(bad === 0 ? 0 : 2);
})().catch((e) => { console.error('ERR ' + ((e && e.stack) || e)); process.exit(1); });
