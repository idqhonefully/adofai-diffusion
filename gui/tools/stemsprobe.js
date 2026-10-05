/* stemsprobe.js —— 分离试听「按歌曲折叠」的**推拉动画**实测（给 tools/pageeval.js 用的表达式）。
 * ----------------------------------------------------------------------------
 * 主人原话："这里展开的时候没有推拉动画"（旧写法是 `display:none ⇄ block` ——
 * display 不参与过渡，只能硬切）。改法是 `grid-template-rows: 0fr ⇄ 1fr`。
 *
 * ⚠ 判据不写"有没有 transition 这条 CSS"（那只能证明我写了字），而是**逐帧采样高度曲线**：
 *   点开之后每一帧读一次折叠体的 getBoundingClientRect().height，
 *   要求曲线满足 ① 起点≈0 ② 终点=内容高 ③ 中间有 ≥5 个**严格在两者之间**的采样
 *   （硬切的话中间那些采样根本不存在 —— 一帧就从 0 跳到终值）。
 *   收起的曲线同样采一遍（推拉是双向的，收起才算"拉回去"）。
 *
 * 数据走**真实宿主消息路径**（window.__devHostMsg → onHostMessage → renderStems），
 * 不是我另写一份 DOM —— 否则"测的"和"给的"不是同一份字节。
 *
 * 用法：
 *   node tools/pageeval.js http://127.0.0.1:8896/ "$(cat tools/stemsprobe.js)"
 */
var out = { steps: [] };
function step(name, obj) { out.steps.push(Object.assign({ name: name }, obj)); }
function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
/* 等到条件成立（或超时）。轮询而不是固定 sleep —— 固定 sleep 是 flake 的来源。 */
function waitFor(fn, ms) {
  var t0 = Date.now();
  return new Promise(function (res) {
    (function poll() {
      var v = false;
      try { v = !!fn(); } catch (_e) { v = false; }
      if (v) { return res(true); }
      if (Date.now() - t0 > ms) { return res(false); }
      setTimeout(poll, 30);
    })();
  });
}
/* 🔴 切页必须**确认切过去了**再量。
   踩过的坑：`nav()` 只要找得到侧栏项就返回 true —— 可那时页面脚本的 click 处理函数
   可能还没绑上，点了个寂寞；于是整页 display:none、所有矩形量到 0，
   判据却报"曲线像硬切"。**工具报错报错在错误的地方，比没有工具更糟。**
   所以这里轮询点、每次核对 .page.active，切不过去就带着原因退出（不再往下量）。 */
async function navStrict(pg) {
  if (!await waitFor(function () { return !!document.querySelector('#sidebar .nav[data-page="' + pg + '"]'); }, 4000)) {
    return '找不到侧栏项 data-page=' + pg;
  }
  if (!await waitFor(function () { return document.readyState === 'complete'; }, 4000)) {
    return '页面 4s 内没到 readyState=complete';
  }
  for (var i = 0; i < 15; i++) {
    var cur = document.querySelector('.page.active');
    if (cur && cur.id === 'page-' + pg) { return 'OK'; }
    document.querySelector('#sidebar .nav[data-page="' + pg + '"]').click();
    await wait(150);
  }
  var last = document.querySelector('.page.active');
  return '点了 15 次也没切到 page-' + pg + '（当前停留 ' + (last ? last.id : '(无 active 页)')
    + '）—— 页面脚本大概没跑起来（去 gui/gui_debug.log 看）';
}

/* 逐帧采样：返回 [[t(ms), h(px)], …]，同时采折叠体与整组的盒子高 */
function sampleCurve(ms) {
  return new Promise(function (res) {
    var inner = document.querySelector('#stemList .stem-body > .stem-inner');
    var group = document.querySelector('#stemList .stem-group');
    var t0 = performance.now(), arr = [];
    function tick() {
      var t = performance.now() - t0;
      arr.push([+t.toFixed(0),
        +inner.getBoundingClientRect().height.toFixed(2),
        +group.getBoundingClientRect().height.toFixed(2)]);
      if (t < ms) requestAnimationFrame(tick); else res(arr);
    }
    requestAnimationFrame(tick);
  });
}
/* 曲线里"严格夹在首尾之间"的采样数 —— 硬切 = 0，真动画 = 一把 */
function middles(curve) {
  var h0 = curve[0][1], h1 = curve[curve.length - 1][1];
  var lo = Math.min(h0, h1), hi = Math.max(h0, h1);
  var n = 0;
  for (var i = 1; i < curve.length - 1; i++) {
    if (curve[i][1] > lo + 0.5 && curve[i][1] < hi - 0.5) n++;
  }
  return n;
}

var navRes = await navStrict('separate');
if (navRes !== 'OK') return JSON.stringify({ err: navRes });
await wait(260);

// —— 喂真实宿主消息（结构与 HostApi 回的一致）——
window.__devHostMsg({
  type: 'stems',
  job: '<REPO>/output/.work/FallenEra_1789790176',
  items: [
    { name: 'input_bass.wav', path: 'D:/x/stems/input_bass.wav' },
    { name: 'input_drums.wav', path: 'D:/x/stems/input_drums.wav' },
    { name: 'input_guitar.wav', path: 'D:/x/stems/input_guitar.wav' },
    { name: 'input_instrumental.wav', path: 'D:/x/stems/input_instrumental.wav' },
    { name: 'input_other.wav', path: 'D:/x/stems/input_other.wav' },
    { name: 'input_piano.wav', path: 'D:/x/stems/input_piano.wav' }
  ]
});
await wait(260);

var grp = document.querySelector('#stemList .stem-group');
var head = document.querySelector('#stemList .stem-head');
var body = document.querySelector('#stemList .stem-body');
var inner = body ? body.querySelector('.stem-inner') : null;
var csB = body ? getComputedStyle(body) : null;
var csI = inner ? getComputedStyle(inner) : null;

// —— 1. 结构：能动画的那套写法（不是 display:none / block）——
step('结构', {
  groups: document.querySelectorAll('#stemList .stem-group').length,
  headName: (head.querySelector('.hn') || {}).textContent,
  bodyDisplay: csB ? csB.display : '(无)',
  gridRowsClosed: csB ? csB.gridTemplateRows : '(无)',
  hasInner: !!inner,
  // 负判据：旧写法留下的 display:none 必须没有了（那玩意儿不可动画）
  bodyHiddenByDisplay: csB ? csB.display === 'none' : null,
  rowsInsideInner: inner ? inner.querySelectorAll('.stem-row').length : -1,
  audioInsideInner: inner ? inner.querySelectorAll('audio').length : -1,
  audioAnywhere: document.querySelectorAll('#stemList audio').length
});

// —— 2. 收起态几何：折叠体该被压到 0，整组只有标题那么高 ——
var hHead = head.getBoundingClientRect().height;
var hInnerClosed = inner.getBoundingClientRect().height;
var hGroupClosed = grp.getBoundingClientRect().height;
step('收起态', {
  headH: +hHead.toFixed(2),
  innerH: +hInnerClosed.toFixed(2),
  groupH: +hGroupClosed.toFixed(2),
  groupMinusHead: +(hGroupClosed - hHead).toFixed(2),
  innerPad: csI ? csI.paddingTop + ' ' + csI.paddingRight + ' ' + csI.paddingBottom + ' ' + csI.paddingLeft : '(无)',
  openClass: grp.classList.contains('open')
});

// —— 3. 展开：逐帧采样 ——
head.click();
var curveOpen = await sampleCurve(450);
var csB2 = getComputedStyle(body);
step('展开曲线', {
  frames: curveOpen.length,
  firstH: curveOpen[0][1],
  lastH: curveOpen[curveOpen.length - 1][1],
  midCount: middles(curveOpen),
  // 前 4 帧的高度序列，肉眼能看出是"一格一格长起来"的
  head4: curveOpen.slice(0, 4).map(function (s) { return s[1]; }),
  transitionProp: csB2.transitionProperty,
  transitionDur: csB2.transitionDuration,
  easing: csB2.transitionTimingFunction,
  groupGrew: +(curveOpen[curveOpen.length - 1][2] - curveOpen[0][2]).toFixed(2)
});
step('展开态', {
  openClass: grp.classList.contains('open'),
  innerH: +inner.getBoundingClientRect().height.toFixed(2),
  innerPad: getComputedStyle(inner).paddingTop + ' ' + getComputedStyle(inner).paddingBottom,
  borderTop: getComputedStyle(inner).borderTopColor,
  chevRot: getComputedStyle(head.querySelector('.chev')).transform
});

// —— 4. 收起：再采一遍（推拉是双向的）——
head.click();
var curveClose = await sampleCurve(450);
step('收起曲线', {
  frames: curveClose.length,
  firstH: curveClose[0][1],
  lastH: curveClose[curveClose.length - 1][1],
  midCount: middles(curveClose),
  openClass: grp.classList.contains('open'),
  innerAfter: +inner.getBoundingClientRect().height.toFixed(2),
  groupMinusHead: +(grp.getBoundingClientRect().height - head.getBoundingClientRect().height).toFixed(2)
});

// —— 5. 无障碍：系统关动画时不该有过渡 ——
step('减动效', {
  note: '@media (prefers-reduced-motion: reduce) 里把 transition 归零，'
      + '无头里改不了媒体特性，这条只作记录（真实判断在 CSS 里）'
});

/* ── 判据 ────────────────────────────────────────────────────────────────
   只钉"主人能看见的那几件事"，不重复测别的探针已经盖住的（圆角/主题/宿主协议）。 */
function framesToHalf(curve) {
  var last = curve[curve.length - 1][1], first = curve[0][1];
  var half = (last + first) / 2;
  for (var i = 0; i < curve.length; i++) {
    if (Math.abs(curve[i][1] - first) >= Math.abs(half - first)) return i;
  }
  return -1;
}
var fails = [];
if (hInnerClosed > 1.5) fails.push('收起态没压到 0：innerH=' + hInnerClosed);
if (csB && csB.display !== 'grid') fails.push('折叠体不是 grid（display:' + csB.display + '）⇒ 不可动画');
if (middles(curveOpen) < 5) fails.push('展开曲线中间采样只有 ' + middles(curveOpen) + ' 个 ⇒ 像硬切');
if (middles(curveClose) < 3) fails.push('收起曲线中间采样只有 ' + middles(curveClose) + ' 个 ⇒ 像硬切');
// ★ 这条是给"曲线选错"立的路障：快起慢停的曲线在第 1~2 帧就过半，
//   700px 的位移上看着是"啪地弹出"不是推拉。要求至少 4 帧才过半。
var f2hOpen = framesToHalf(curveOpen);
if (f2hOpen >= 0 && f2hOpen < 4) fails.push('展开曲线太急：第 ' + f2hOpen + ' 帧就走过半程');
var f2hClose = framesToHalf(curveClose);
if (f2hClose >= 0 && f2hClose < 3) fails.push('收起曲线太急：第 ' + f2hClose + ' 帧就走过半程');
if (csB2.transitionDuration !== '0.34s') fails.push('时长不是 .34s：' + csB2.transitionDuration);
if (!/grid-template-rows/.test(csB2.transitionProperty)) fails.push('过渡属性不是 grid-template-rows：' + csB2.transitionProperty);
if (inner.querySelectorAll('.stem-row').length !== 6) fails.push('音轨不在 .stem-inner 里');
if (grp.classList.contains('open')) fails.push('收起后 .open 还在');

out.verdict = {
  pass: fails.length === 0,
  fails: fails,
  framesToHalfOpen: f2hOpen,
  framesToHalfClose: f2hClose,
  framesOpen: curveOpen.length,
  framesClose: curveClose.length,
  duration: csB2.transitionDuration,
  easing: csB2.transitionTimingFunction
};

return JSON.stringify(out, null, 1);
