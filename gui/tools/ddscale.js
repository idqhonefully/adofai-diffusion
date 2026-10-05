/* Win11 自绘下拉（ShellUI.Dropdown）的**跨页度量对照**探针 —— 一个脚本两个页面通用。
 *
 * 用法（两侧各跑一次，再对比）：
 *   node tools/pageeval.js http://127.0.0.1:8896/workbench/index.html "$(cat tools/ddscale.js)"
 *   node tools/pageeval.js http://127.0.0.1:8896/                      "$(cat tools/ddscale.js)"
 *
 * 它只读、不改任何东西（只在**当前这次加载**里展开折叠面板、点开菜单，然后复原）。
 * 量的是：触发器 / 菜单 / 箭头 / 选项 / 选中条 的盒、字号、内边距、圆角、颜色、间距，
 * 外加驱动这一切的 `--fl-*` · `--ct-*` · `--accent` · `--text` token 实际值。
 *
 * ⚠ 两个坑（都踩过，写在这儿免得下次再踩）：
 *   ① 工作台的折叠类是 **`.grp.collapsed`**，不是 `.sec.collapsed`。选错 ⇒ 触发器量到 0×0，
 *      而菜单挂在 body 上照样有尺寸 ⇒ 会得出一份「触发器 0×0、菜单正常」的鬼数据。
 *   ② 导入页（壳）的 `#matSelect` / `#appearSelect` 是 `<span>` 占位，被 create() **原地换成**
 *      `<button class="dd">`（id 继承）。所以「找不到触发器」多半是**选错页/没切到设置页**，
 *      不是控件没上线。
 */
var out = { page: null, err: null };

function M(el) {
  if (!el) return null;
  var cs = getComputedStyle(el), r = el.getBoundingClientRect();
  return {
    box: [Math.round(r.width), Math.round(r.height)],
    font: cs.fontSize, fam: cs.fontFamily.split(',')[0].replace(/"/g, ''), fw: cs.fontWeight,
    lh: cs.lineHeight, ls: cs.letterSpacing,
    pad: [cs.paddingTop, cs.paddingRight, cs.paddingBottom, cs.paddingLeft].join('/'),
    radius: cs.borderRadius, border: cs.borderTopWidth + ' ' + cs.borderTopColor,
    bg: cs.backgroundColor, color: cs.color,
    gap: cs.gap, shadow: cs.boxShadow,
    cls: el.className
  };
}
function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

// ── 认页：工作台有 in-merge_anchor（合并取点），导入页要切到设置页 ──
var isWb = !!document.querySelector('.wb, #inspector, #merge_anchor') || !!document.getElementById('cv-band');
var trig = null, id = null;

if (isWb) {
  out.page = 'workbench';
  // ⚠ 工作台的折叠类是 `.grp.collapsed`（不是 `.sec.collapsed`）—— 选错就量到 0×0，
  //    而菜单挂在 body 上照样有尺寸 ⇒ 会得出一份「触发器 0×0、菜单正常」的鬼数据。
  document.querySelectorAll('.grp.collapsed, .sec.collapsed').forEach(function (s) { s.classList.remove('collapsed'); });
  await wait(600);
  trig = document.getElementById('in-merge_anchor-dd') || document.getElementById('in-merge_anchor');
  if (trig && trig.tagName !== 'BUTTON') {
    // 还没被接管（说明它不是自绘的）—— 如实报出来
    out.notUpgraded = true;
  }
  id = trig ? trig.id : null;
} else {
  out.page = 'import-settings';
  var ns = document.querySelectorAll('#sidebar .nav');
  for (var i = 0; i < ns.length; i++) {
    if (ns[i].getAttribute('data-page') === 'settings') { ns[i].click(); break; }
  }
  await wait(400);
  try {
    window.__devHostMsg({ type: 'appinfo', data: { appearance: 'dark', material: 'mica',
      materials: [{ name: 'acrylic', label: 'Acrylic' }, { name: 'mica', label: 'Mica' },
                  { name: 'tabbed', label: 'Tabbed' }, { name: 'solid', label: 'Solid' }] } });
  } catch (e) { /* 真壳里不需要喂 */ }
  await wait(300);
  // 材质那个下拉带 hint，深浅那个不带 —— 两个都量
  trig = document.getElementById('matSelect') || document.getElementById('appearSelect');
  id = trig ? trig.id : 'matSelect';
  out.alsoAppear = !!document.getElementById('appearSelect');
  out.fieldsSeen = [].map.call(document.querySelectorAll('#page-settings .dd'), function (e) { return e.id; });
}

if (!trig) { out.err = '没找到触发器（' + (id || '?') + '）'; return JSON.stringify(out, null, 1); }

out.triggerId = id;
await wait(120);
out.trigger_closed = M(trig);

trig.click();
await wait(400);
var menu = document.getElementById(id + '-menu');
if (!menu) { out.err = '点开后没有菜单 ' + id + '-menu'; return JSON.stringify(out, null, 1); }
out.trigger_open = M(trig);

var tr = trig.getBoundingClientRect(), mr = menu.getBoundingClientRect();
out.menu = M(menu);
out.menu_pos = {
  gapBelow: +(mr.top - tr.bottom).toFixed(1),     // 触发器底 → 菜单顶
  rightDelta: +(mr.right - tr.right).toFixed(1),  // 右缘对齐差
  leftDelta: +(mr.left - tr.left).toFixed(1),
  widthDelta: +(mr.width - tr.width).toFixed(1)
};
out.chevron = M(trig.querySelector('.dd-ch'));
var svg = trig.querySelector('.dd-ch svg path, .dd-ch svg');
if (svg) out.chevron_stroke = svg.getAttribute('stroke-width') || svg.getAttribute('stroke');

var opts = [].slice.call(menu.querySelectorAll('.dd-opt'));
out.optCount = opts.length;
out.opt_first = M(opts[0]);
out.opt_last = M(opts[opts.length - 1]);
out.opt_first_tx = M(opts[0] && opts[0].querySelector('.dd-tx'));
out.opt_first_hint = M(opts[0] && opts[0].querySelector('.dd-h'));
out.opt_first_bar = M(opts[0] && opts[0].querySelector('.dd-bar'));
out.opt_gap = opts.length > 1 ? +(opts[1].getBoundingClientRect().top - opts[0].getBoundingClientRect().bottom).toFixed(1) : null;
out.opt_texts = opts.map(function (o) { return o.textContent; });
out.menu_pad_firstTop = +(opts[0].getBoundingClientRect().top - mr.top).toFixed(1);
var ms = getComputedStyle(menu);
out.menu_anim = ms.animationName + ' ' + ms.animationDuration;
out.opt_on_anim = opts[0] ? getComputedStyle(opts[0]).transition : null;

// 顺手把驱动这一切的 token 值读出来（同源就不该有差）
var rs = getComputedStyle(document.documentElement);
out.tokens = {};
['--fl-r-ctl', '--fl-r-flyout', '--fl-flyout', '--fl-flyout-line', '--fl-flyout-shadow',
 '--ct-fill', '--ct-line', '--fl-opt-hover', '--fl-opt-on', '--fl-dur', '--fl-dur-fast',
 '--text', '--text-dim', '--accent'].forEach(function (n) {
  out.tokens[n] = rs.getPropertyValue(n).trim() || '(未定义)';
});
out.theme = document.documentElement.getAttribute('data-theme') || '(未设)';

trig.click();
await wait(250);

return JSON.stringify(out, null, 1);
