/* ddprobe.js —— 自绘下拉框（ShellUI.Dropdown / Win11 ComboBox）的**行为 + 外观**实测。
 * ----------------------------------------------------------------------------
 * 这是给 tools/pageeval.js 用的**表达式**（不是独立脚本）—— 它要跑在页面里面。
 * 因为它太长、且含着引号与换行，**别直接写在命令行上**（shell 会把引号吃掉，
 * 我第一版就是这么踩的：表达式被截断、探针跑出一堆假 FAIL）。用：
 *
 *   node tools/pageeval.js http://127.0.0.1:8896/ "$(cat tools/ddprobe.js)"
 *
 * 断言口径（都是"主人眼睛能看到的那一份"）：
 *   · 结构   —— 触发器是 <button class="dd">；菜单挂在 body 下、position:fixed
 *               （留在卡片里会被 overflow 裁掉）；设置页**一个原生 <select> 都不剩**
 *   · 触发器 —— 32px 高、4px 圆角、**没有焦点方框**（outline:none）、chevron 12px
 *   · 展开   —— 菜单可见且 aria-expanded=true；入场动画名 dd-in / .22s / decelerate
 *   · 浮层   —— 8px 圆角、独立表面色、1px 描边、两层柔和投影、5px 内边距、z-index 120
 *   · 选中行 —— 32px 高、左侧 3×16 主色竖条（未选中那条必须透明）
 *   · 收起   —— Esc 走 dd-out 动画后才 hidden（不是瞬间消失）
 *   · 选择   —— 点菜单项后 data-v / 触发器文字 / .on 三处同步
 *   · 深浅   —— 切到浅色后 <html data-theme>、菜单底色、触发器描边一起换
 *   · 外部点击 —— 点页面别处会收起
 *   · 布局   —— 「深浅外观」那一行没被撑坏（历史上被挤成竖排过）
 * 需要底盘数据（材质清单）就先喂一条 appinfo，见 tools/feed-appinfo-dark.json。
 * ⚠ 截图留证要用 capture.js 的 **flat** 模式（见那个文件头部关于 fixed 浮层的说明）。
 */
var out = { steps: [] };
function step(name, obj) { out.steps.push(Object.assign({ name: name }, obj)); }
function cs(el) { return el ? getComputedStyle(el) : null; }
function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function nav(pg) {
  var ns = document.querySelectorAll('#sidebar .nav');
  for (var i = 0; i < ns.length; i++) { if (ns[i].getAttribute('data-page') === pg) { ns[i].click(); return true; } }
  return false;
}

nav('settings');
await wait(400);

// —— 喂一份真宿主的 appinfo（4 档材质，当前选中 mica）——
window.__devHostMsg({
  type: 'appinfo',
  data: {
    appearance: 'dark',
    material: 'mica',
    materials: [
      { name: 'acrylic', label: 'Acrylic' },
      { name: 'mica', label: 'Mica' },
      { name: 'tabbed', label: 'Tabbed' },
      { name: 'solid', label: 'Solid' }
    ]
  }
});
await wait(250);

var trig = document.getElementById('matSelect');
var menu = document.getElementById('matSelect-menu');
var atrig = document.getElementById('appearSelect');
var amenu = document.getElementById('appearSelect-menu');

step('结构', {
  trigTag: trig ? trig.tagName : null,
  trigClass: trig ? trig.className : null,
  trigRole: trig ? trig.getAttribute('role') : null,
  natSelectsInSettings: document.querySelectorAll('#page-settings select').length,
  menuParentIsBody: !!menu && menu.parentNode === document.body,
  menuPosition: cs(menu) ? cs(menu).position : null,
  menuHidden: menu ? menu.hidden : null,
  opts: menu ? menu.querySelectorAll('.dd-opt').length : -1,
  optVals: menu ? Array.prototype.map.call(menu.querySelectorAll('.dd-opt'), function (o) { return o.getAttribute('data-v'); }) : null,
  onVal: (function () { var e = document.querySelector('#matSelect-menu .dd-opt.on'); return e ? e.getAttribute('data-v') : null; })(),
  trigText: trig ? trig.textContent : null,
  trigInlineStyle: trig ? trig.getAttribute('style') : null,
  trigWidth: trig ? Math.round(trig.getBoundingClientRect().width) : -1,
  appearOpts: amenu ? amenu.querySelectorAll('.dd-opt').length : -1,
  appearOn: (function () { var e = document.querySelector('#appearSelect-menu .dd-opt.on'); return e ? e.getAttribute('data-v') : null; })()
});

// —— 触发器外观（静态）：圆角 / 边框 / 高度 / 无焦点方框 ——
var tcs = cs(trig);
step('触发器外观', {
  h: Math.round(trig.getBoundingClientRect().height),
  radius: tcs.borderTopLeftRadius,
  borderTop: tcs.borderTopWidth + ' ' + tcs.borderTopColor,
  outlineW: tcs.outlineWidth, outlineStyle: tcs.outlineStyle,
  cursor: tcs.cursor,
  bg: tcs.backgroundColor,
  chevW: (function () { var c = trig.querySelector('.dd-ch'); return c ? Math.round(c.getBoundingClientRect().width) : -1; })()
});

// —— 打开：菜单可见 + 入场动画 + 浮层外观 ——
trig.click();
await wait(30);
var mcsOpen = cs(menu);
step('打开瞬间', {
  hidden: menu.hidden,
  ariaExpanded: trig.getAttribute('aria-expanded'),
  trigHasOpen: trig.classList.contains('open'),
  animName: mcsOpen.animationName,
  animDur: mcsOpen.animationDuration,
  animEase: mcsOpen.animationTimingFunction
});
await wait(320);
var mcs = cs(menu);
var onOpt = document.querySelector('#matSelect-menu .dd-opt.on');
var bar = onOpt ? onOpt.querySelector('.dd-bar') : null;
var hoverOpt = menu.querySelector('.dd-opt:not(.on)');
step('打开完成', {
  hidden: menu.hidden,
  radius: mcs.borderTopLeftRadius,
  bg: mcs.backgroundColor,
  border: mcs.borderTopWidth + ' ' + mcs.borderTopColor,
  shadow: mcs.boxShadow,
  pad: mcs.paddingTop + ' ' + mcs.paddingRight + ' ' + mcs.paddingBottom + ' ' + mcs.paddingLeft,
  maxH: mcs.maxHeight,
  zIndex: mcs.zIndex,
  rectW: Math.round(menu.getBoundingClientRect().width),
  trigW: Math.round(trig.getBoundingClientRect().width),
  optH: onOpt ? Math.round(onOpt.getBoundingClientRect().height) : -1,
  optRadius: onOpt ? cs(onOpt).borderTopLeftRadius : null,
  onBg: onOpt ? cs(onOpt).backgroundColor : null,
  onBarBg: bar ? cs(bar).backgroundColor : null,
  onBarW: bar ? Math.round(bar.getBoundingClientRect().width) : -1,
  onBarH: bar ? Math.round(bar.getBoundingClientRect().height) : -1,
  otherBarBg: hoverOpt ? cs(hoverOpt.querySelector('.dd-bar')).backgroundColor : null,
  focusedEl: document.activeElement ? (document.activeElement.id || document.activeElement.tagName) : null
});

// —— 键盘：Esc 收起（收起也要有动画）——
document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
trig.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
await wait(30);
step('Esc 收起中', {
  ariaExpanded: trig.getAttribute('aria-expanded'),
  closing: menu.classList.contains('closing'),
  animName: cs(menu).animationName
});
await wait(200);
step('Esc 收起后', { hidden: menu.hidden, closing: menu.classList.contains('closing') });

// —— 选一项：点第 3 项（tabbed）——
trig.click();
await wait(300);
var opts = menu.querySelectorAll('.dd-opt');
opts[2].dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
opts[2].click();
await wait(250);
step('选择 tabbed 之后', {
  onVal: (function () { var e = document.querySelector('#matSelect-menu .dd-opt.on'); return e ? e.getAttribute('data-v') : null; })(),
  dataV: trig.getAttribute('data-v'),
  trigText: trig.textContent,
  menuHidden: menu.hidden,
  themeStill: document.documentElement.getAttribute('data-theme')
});

// —— 深浅下拉：切浅色 ——
atrig.click();
await wait(300);
step('深浅菜单', {
  hidden: amenu.hidden,
  opts: amenu.querySelectorAll('.dd-opt').length,
  texts: Array.prototype.map.call(amenu.querySelectorAll('.dd-opt .dd-tx'), function (e) { return e.textContent; }),
  bg: cs(amenu).backgroundColor,
  radius: cs(amenu).borderTopLeftRadius,
  rectX: Math.round(amenu.getBoundingClientRect().left),
  rectW: Math.round(amenu.getBoundingClientRect().width),
  trigX: Math.round(atrig.getBoundingClientRect().left),
  trigW: Math.round(atrig.getBoundingClientRect().width)
});
var lightOpt = amenu.querySelectorAll('.dd-opt')[1];
lightOpt.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
lightOpt.click();
await wait(400);
step('切浅色后', {
  theme: document.documentElement.getAttribute('data-theme'),
  appearOn: (function () { var e = document.querySelector('#appearSelect-menu .dd-opt.on'); return e ? e.getAttribute('data-v') : null; })(),
  desc: (document.getElementById('appearDesc') || {}).textContent,
  menuBgLight: cs(amenu).backgroundColor,
  trigBgLight: cs(atrig).backgroundColor,
  trigBorderLight: cs(atrig).borderTopColor,
  menuBarLight: (function () { var b = document.querySelector('#appearSelect-menu .dd-opt.on .dd-bar'); return b ? cs(b).backgroundColor : null; })(),
  natSelects: document.querySelectorAll('#page-settings select').length
});

// —— 点外面收掉 ——
atrig.click();
await wait(260);
var openBefore = !amenu.hidden;
document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
await wait(200);
step('点外面', { openBefore: openBefore, hiddenAfter: amenu.hidden });

// —— 布局：深浅那一行没被撑坏（历史坑：标签被挤成竖排）——
var row = document.querySelector('#page-settings .appear-row');
var tx = row ? row.querySelector('.t') : null;
step('布局', {
  rowH: row ? Math.round(row.getBoundingClientRect().height) : -1,
  labelW: tx ? Math.round(tx.getBoundingClientRect().width) : -1,
  labelH: tx ? Math.round(tx.getBoundingClientRect().height) : -1,
  ddW: Math.round(atrig.getBoundingClientRect().width)
});

return JSON.stringify(out, null, 1);
