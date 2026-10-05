/* wbddprobe.js —— 工作台「原生 <select> → 自绘 Win11 ComboBox」接管的**行为 + 度量**实测。
 * ----------------------------------------------------------------------------
 * 这是给 tools/pageeval.js 用的**表达式**（不是独立脚本）—— 它要跑在页面里面。
 * 太长且含引号/换行，**别写在命令行上**（shell 会吃掉引号，探针会跑出一堆假 FAIL）：
 *
 *   node tools/pageeval.js http://127.0.0.1:8896/workbench/index.html "$(cat tools/wbddprobe.js)"
 *
 * 断言口径（都是"主人眼睛能看到的那一份"）：
 *   · 结构   —— 每个原生 <select> 都配了 1 个 .dd 触发器 + 1 个 .dd-src 隐身原控件；
 *               菜单挂在 body 下（留在行里会被 overflow 裁）；**没有重复 id**
 *               （接管时触发器另取 `原id + '-dd'`，重了会让 `$('#density')` 取到按钮）
 *   · 隐身   —— 原控件 1×1、opacity 0、pointer-events none ⇒ 不会露出浏览器那层原生弹窗
 *   · 度量   —— 触发器 26px 高 / 12.5px 字 / 4px 圆角（= 原 select 实测值，换完尺寸不变）；
 *               顶栏「密度」宽度仍在 58px 上下（换了元素不能把顶栏挤变形）
 *   · 文字   —— 每个触发器的显示文字 == 它对应 select 当前选中项的 textContent
 *   · 打开   —— 点触发器出 .dd-menu，且 aria-expanded=true
 *   · 回写   —— 选一项后 select.value 变了、**change 事件真的派发**（既有 onchange 靠它）；
 *               并且端到端有效：把「密度」选成"宽松"后 <html data-density> 变成 loose
 *   · 反向   —— 关掉「固定双押角度」后，three_press_mode 的触发器要跟着置灰（置灰是
 *               applyDpLock 直接写在原生控件上的，触发器不跟就永远亮着）
 *   · 观察器 —— 新插进 DOM 的 select 会被自动接管；把它移除，它的菜单要从 body 里收走
 *               （不回收的话每次 render 都往 body 攒一个 hidden 菜单）
 *   · 深浅   —— 切浅色后菜单底色/触发器描边跟着换
 *   · 动态行 —— 点「＋ 加区间」「＋ 加分段」造**真行**，行里的小下拉（.dd-xs 档）
 *               必须被接管，且高度与原 select 一致（拿克隆复原对照量）
 *   · 重入   —— 动态行的 onchange 会**整行重建**（把自绘组件自己摘掉）：值要写回、
 *               不能抛异常、菜单不许累积、重建后的新下拉还能用
 */
var out = { checks: [], fail: 0 };
function push(name, ok, detail) {
  out.checks.push({ name: name, ok: !!ok, detail: String(detail) });
  if (!ok) { out.fail++; }
}
function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function cs(el) { return el ? getComputedStyle(el) : null; }
function R(el) { return el ? el.getBoundingClientRect() : null; }
function box(el) { var r = R(el); return r ? (Math.round(r.width) + '×' + Math.round(r.height)) : 'n/a'; }

// 先把折叠段全展开——收起的组里 select 尺寸是 0，量不出东西。
document.querySelectorAll('.sec.collapsed').forEach(function (s) { s.classList.remove('collapsed'); });
await wait(500);

// ═══ ① 结构：全接管 ════════════════════════════════════════════════════════════
var sels = [...document.querySelectorAll('select')];
var upgraded = [...document.querySelectorAll('select[data-dd]')];
var natLeft = sels.filter(function (s) { return !s.dataset.dd; });
var dupIds = (function () {
  var seen = {}, dup = [];
  document.querySelectorAll('[id]').forEach(function (e) { if (seen[e.id]) dup.push(e.id); seen[e.id] = 1; });
  return dup;
})();
push('全部原生 select 都被接管（' + sels.length + ' 个）', sels.length > 0 && natLeft.length === 0,
  '未接管=' + natLeft.map(function (s) { return s.id || s.className || '(无名)'; }).join(',') + ' (共 ' + sels.length + ')');
push('每个 select 配到 1 个触发器', document.querySelectorAll('.dd').length === sels.length,
  '触发器=' + document.querySelectorAll('.dd').length + ' select=' + sels.length);
push('原控件带 .dd-src 隐身（不是删掉）',
  document.querySelectorAll('select.dd-src').length === sels.length,
  '.dd-src=' + document.querySelectorAll('select.dd-src').length);
push('★ 没有重复 id（触发器另取 -dd 后缀）', dupIds.length === 0, dupIds.join(',') || '无');
push('菜单都挂在 body 下（挂行里会被 overflow 裁）',
  [...document.querySelectorAll('.dd-menu')].every(function (m) { return m.parentNode === document.body; }),
  '菜单=' + document.querySelectorAll('.dd-menu').length + ' 个');

// ═══ ② 隐身：原控件不可见、不可点 ═════════════════════════════════════════════
var d = document.getElementById('density');
var dcs = cs(d), dr = R(d);
push('原控件 1×1 且 opacity:0（露不出来就不会弹出原生菜单）',
  dr.width <= 2 && dr.height <= 2 && dcs.opacity === '0',
  'rect=' + box(d) + ' opacity=' + dcs.opacity);
push('原控件 pointer-events:none 且脱离文档流',
  dcs.pointerEvents === 'none' && dcs.position === 'absolute',
  'pointer-events=' + dcs.pointerEvents + ' position=' + dcs.position);

// ═══ ③ 度量：换完尺寸不变 ═════════════════════════════════════════════════════
var trg = document.getElementById('density-dd');
var tcs = cs(trg), tr = R(trg);
push('触发器 26px 高（= 原 select 实测高度）', Math.round(tr.height) === 26, 'h=' + Math.round(tr.height));
push('触发器 12.5px 字 / 4px 圆角（= 原 select 实测）',
  tcs.fontSize === '12.5px' && tcs.borderTopLeftRadius === '4px',
  'font=' + tcs.fontSize + ' radius=' + tcs.borderTopLeftRadius);
push('触发器带 .dd-sm 尺度档（菜单跟着同档）',
  trg.classList.contains('dd-sm') && document.getElementById('density-dd-menu').classList.contains('dd-sm'),
  'trigger=' + trg.className + ' menu=' + document.getElementById('density-dd-menu').className);
push('★ 顶栏「密度」宽度没变形（原 58px ±6）', Math.abs(tr.width - 58) <= 6, 'width=' + Math.round(tr.width));

// 检查器字段：铺满右侧（原 select 是 flex:1 1 auto）
var insp = document.getElementById('in-xk_base-dd');
var ics = cs(insp);
push('检查器字段触发器仍是 flex:1 1 auto / min-width:0（占位跟原来一样）',
  ics.flexGrow === '1' && ics.minWidth === '0px',
  'flex=' + ics.flexGrow + ' ' + ics.flexShrink + ' ' + ics.flexBasis + ' min-width=' + ics.minWidth);
push('检查器字段触发器也是 26px 高', Math.round(R(insp).height) === 26, 'h=' + Math.round(R(insp).height));

// ═══ ④ 文字一致：触发器显示 == select 当前选中项 ═══════════════════════════════
var mismatch = [];
upgraded.forEach(function (s) {
  var t = s.id + '-dd';
  var el = document.getElementById(t);
  if (!el) { mismatch.push(s.id + '(无触发器)'); return; }
  var lb = el.querySelector('.dd-lb');
  var want = s.options[s.selectedIndex] ? s.options[s.selectedIndex].textContent : '';
  if (!lb || lb.textContent !== want) { mismatch.push(s.id + ': 显示"' + (lb ? lb.textContent : '') + '" ≠ "' + want + '"'); }
});
push('每个触发器的文字 == 它 select 的选中项文字（' + upgraded.length + ' 个）',
  mismatch.length === 0, mismatch.slice(0, 3).join(' | ') || '全一致');

// ═══ ⑤ 打开：出自绘菜单，不是原生弹窗 ════════════════════════════════════════
var dmenu = document.getElementById('density-dd-menu');
trg.click();
await wait(320);
push('点触发器后自绘菜单出现、aria-expanded=true',
  !dmenu.hidden && trg.getAttribute('aria-expanded') === 'true',
  'hidden=' + dmenu.hidden + ' aria=' + trg.getAttribute('aria-expanded'));
push('菜单里有 3 个选项（紧凑/标准/宽松）', dmenu.querySelectorAll('.dd-opt').length === 3,
  'opts=' + dmenu.querySelectorAll('.dd-opt').length);
push('菜单是 fixed 定位（跟着触发器走、不被裁）', cs(dmenu).position === 'fixed', cs(dmenu).position);

// ═══ ⑥ 回写：写回 select + 派发 change（既有 onchange 靠它）════════════════════
var seen = [];
var onCh = function () { seen.push(d.value); };
d.addEventListener('change', onCh);
var opts = dmenu.querySelectorAll('.dd-opt');
opts[2].dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
opts[2].click();
await wait(300);
d.removeEventListener('change', onCh);
push('选一项后 select.value 被写回', d.value === 'loose', 'select.value=' + d.value);
push('★ change 事件真的派发了（原生 onchange 拿得到）', seen.length === 1 && seen[0] === 'loose',
  'listener 收到=' + JSON.stringify(seen));
push('★ 端到端：顶栏「密度」真的生效（<html data-density> = loose）',
  document.documentElement.dataset.density === 'loose',
  'data-density=' + document.documentElement.dataset.density);
push('选完菜单收起', dmenu.hidden, 'hidden=' + dmenu.hidden);

// ═══ ⑦ 反向同步：置灰要跟着原生控件走 ═════════════════════════════════════════
var dpChk = document.getElementById('in-use_fixed_dp_angle');
var tpTrg = document.getElementById('in-three_press_mode-dd');
var beforeDis = tpTrg ? tpTrg.disabled : null;
if (dpChk) { dpChk.click(); await wait(300); }
push('★ 关掉「固定双押角度」后 three_press_mode 的触发器跟着置灰',
  !!tpTrg && tpTrg.disabled === true, 'before=' + beforeDis + ' after=' + (tpTrg && tpTrg.disabled));
if (dpChk) { dpChk.click(); await wait(300); }
push('再打开后触发器恢复可点', !!tpTrg && tpTrg.disabled === false, 'after2=' + (tpTrg && tpTrg.disabled));

// ═══ ⑧ 观察器：新来的 select 自动接管；移除时菜单收走 ══════════════════════════
var menusBefore = document.querySelectorAll('.dd-menu').length;
var probeSel = document.createElement('select');
probeSel.id = 'probe-dyn-sel';
['甲', '乙'].forEach(function (t) { var o = document.createElement('option'); o.value = t; o.textContent = t; probeSel.appendChild(o); });
document.getElementById('top').appendChild(probeSel);
await wait(250);
push('★ 新插进 DOM 的 select 被自动接管（render 重建的行靠这条）',
  !!probeSel.dataset.dd && !!document.getElementById('probe-dyn-sel-dd'),
  'data-dd=' + probeSel.dataset.dd + ' 触发器=' + !!document.getElementById('probe-dyn-sel-dd'));
probeSel.parentNode.removeChild(probeSel);
await wait(250);
push('★ 移除后它的菜单从 body 收走（否则每 render 攒一个）',
  !document.getElementById('probe-dyn-sel-menu') && document.querySelectorAll('.dd-menu').length === menusBefore,
  '菜单数 ' + menusBefore + ' → ' + document.querySelectorAll('.dd-menu').length);

// ═══ ⑨ 深浅：菜单与触发器跟着主题换 ═══════════════════════════════════════════
trg.click();
await wait(300);
var menuBgDark = cs(dmenu).backgroundColor, trigBgDark = cs(trg).backgroundColor;
trg.click();
await wait(200);
document.documentElement.setAttribute('data-theme', 'light');
await wait(300);
trg.click();
await wait(300);
var menuBgLight = cs(dmenu).backgroundColor, trigBgLight = cs(trg).backgroundColor;
push('切浅色后菜单底色变了', menuBgDark !== menuBgLight,
  menuBgDark + ' → ' + menuBgLight);
push('切浅色后触发器底色变了', trigBgDark !== trigBgLight, trigBgDark + ' → ' + trigBgLight);
trg.click();
await wait(200);
document.documentElement.setAttribute('data-theme', 'dark');
await wait(200);

// ═══ ⑩ 真实动态行：采bpm 区间 / 演出分段（每次 render 都重建）═════════════════
//   ⑧ 那条用的是我自己造的 select，只证明"观察器扫得到"。
//   这里要的是**真货**：点「＋ 加区间（播放头）」「＋ 加分段」造出真行，
//   看行里的小下拉（.rtime / .rrow 里，走 .dd-xs 档）有没有被接管、度量对不对。
var jsErr = [];
window.addEventListener('error', function (e) { jsErr.push(String(e.message)); });

var xAdd = document.getElementById('btn-xk-add');
var sAdd = document.getElementById('btn-show-add');
push('找得到「＋ 加区间（播放头）」「＋ 加分段」两个按钮', !!xAdd && !!sAdd,
  'xk-add=' + !!xAdd + ' show-add=' + !!sAdd);
if (xAdd) { xAdd.click(); await wait(420); }
if (sAdd) { sAdd.click(); await wait(420); }

/* 触发器是插在原生控件**原来的位置**上（select 自己隐身退到绝对定位）⇒
   它的前一个兄弟就是触发器。用 _dd 引用更稳，但这里刻意走 DOM 关系 ——
   这正是"外面按 `$('#id')` 还能不能拿到"之外的另一半契约。 */
function trgOf(s) { return (s && s._dd) ? s._dd.el : (s ? s.previousElementSibling : null); }

var dynSels = [...document.querySelectorAll('#lst-xk select, #lst-show select')];
push('动态行里造出了 select（采bpm 区间 + 演出分段各一行）', dynSels.length >= 4,
  'n=' + dynSels.length + ' ids=' + dynSels.map(function (s) { return s.id || '(无id)'; }).join(','));
var dynLeft = dynSels.filter(function (s) { return !s.dataset.dd || !trgOf(s); });
push('★ 动态行里的 select 全被自动接管（观察器兜住了重建的行）', dynSels.length > 0 && dynLeft.length === 0,
  '未接管=' + dynLeft.map(function (s) { return s.id || '(无名)'; }).join(',') || '全接管');
push('★ 动态行的触发器带 .dd-xs 档（区间行比检查器密一档）',
  dynSels.length > 0 && dynSels.every(function (s) { var t = trgOf(s); return t && t.classList.contains('dd-xs'); }),
  dynSels.map(function (s) { var t = trgOf(s); return s.id + '=' + (t ? t.className.replace('dd ', '') : 'null'); }).join(' '));

/* 断言"换完不改变这一行的高度"用**克隆对照**：把隐身原控件复制一份、脱掉 .dd-src 放回原位量一次。
   直接量原件没用（被强制成 1×1）；只量触发器又证明不了"跟原来一样"。
   ⚠ 基线选的是**同一行的输入框**（.rtime / .rrow 里的 input），不是"它自己那个原件"：
     `.rrow` 里那个 N 下拉原本是 18px，而同行的 `INPUT.xktrk` 是 20px —— 它自己才是异类，
     跟着输入框（20px）齐平才是这一行该有的观感。拿原件做基线会把"变整齐"误判成"变形"。 */
var sizeMismatch = [], cloneDev = [];
dynSels.forEach(function (s) {
  var t = trgOf(s);
  if (!t) return;
  var row = s.parentNode;
  var sib = row ? row.querySelector('input:not([type=hidden])') : null;
  var th = t.getBoundingClientRect().height;
  if (sib) {
    var sh = sib.getBoundingClientRect().height;
    if (Math.abs(th - sh) > 1.5) { sizeMismatch.push((s.id || '(无id)') + ': 触发器=' + th.toFixed(1) + ' 同行输入框=' + sh.toFixed(1)); }
  }
  var clone = s.cloneNode(true);
  clone.classList.remove('dd-src');
  clone.removeAttribute('data-dd');
  clone.style.cssText = 'position:static;width:auto;height:auto;opacity:1;pointer-events:auto;';
  s.parentNode.insertBefore(clone, s);
  var oh = clone.getBoundingClientRect().height;
  clone.parentNode.removeChild(clone);
  if (Math.abs(oh - th) > 1.5) { cloneDev.push((s.id || '(无id)') + ': 原=' + oh.toFixed(1) + ' 现=' + th.toFixed(1)); }
});
push('★ 动态行触发器高度 == 同一行的输入框（行高没被撑变，±1.5px）',
  sizeMismatch.length === 0, sizeMismatch.slice(0, 3).join(' | ') || '全部齐平');
push('原控件高度对照（记录用：偏差允许存在，因为 .dd-xs 跟的是"同行输入框"而非旧 select）',
  true, cloneDev.length ? cloneDev.join(' | ') : '与旧 select 高度也一致');

// ═══ ⑪ 重入：在动态行里选一项 ⇒ onchange 里**整行重建**（含触发器自己）══════════
//   这是接管方案最危险的一处：自绘组件在"选中"的流程里同步派发 change，
//   而那条 change 会把组件连同宿主一起从 DOM 里摘掉。要看的是——
//   写回成不成、菜单有没有泄漏、新行里的下拉还能不能正常用。
var nsel = dynSels.filter(function (s) { return s.options.length === 3; })[0];   // 这一段 N（2k/4k/8k）
var ntrg = trgOf(nsel);
var menusBeforePick = document.querySelectorAll('.dd-menu').length;
var errBefore = jsErr.length;
ntrg.click();
await wait(340);
var nmenu = document.getElementById(ntrg.id + '-menu');
var o8 = nmenu ? [...nmenu.querySelectorAll('.dd-opt')].filter(function (o) { return /8/.test(o.textContent); })[0] : null;
if (o8) { o8.dispatchEvent(new MouseEvent('mousedown', { bubbles: true })); o8.click(); }
await wait(520);

var nsel2 = [...document.querySelectorAll('#lst-xk select')].filter(function (s) { return s.options.length === 3; })[0];
var ntrg2 = trgOf(nsel2);
push('★ 动态行里选一项：值写回 + 整行重建成功（重入没把页面搞坏）',
  !!nsel2 && nsel2.value === '8' && !!ntrg2 && ntrg2 !== ntrg,
  'value=' + (nsel2 && nsel2.value) + ' 新触发器=' + (ntrg2 ? ntrg2.id : 'null') + ' ≠ 旧 ' + ntrg.id);
push('★ 重建期间没抛 JS 异常（自绘组件在自己的流程里被移除）', jsErr.length === errBefore,
  jsErr.slice(errBefore).join(' | ') || '无异常');
push('菜单没有累积（旧的随行一起收走、新的补上）',
  document.querySelectorAll('.dd-menu').length === menusBeforePick,
  menusBeforePick + ' → ' + document.querySelectorAll('.dd-menu').length);

if (ntrg2) {
  ntrg2.click();
  await wait(340);
  var m2 = document.getElementById(ntrg2.id + '-menu');
  push('★ 重建后的新下拉照旧能打开（destroy 没把新实例带坏）', !!m2 && !m2.hidden,
    'hidden=' + (m2 ? m2.hidden : 'null') + ' opts=' + (m2 ? m2.querySelectorAll('.dd-opt').length : -1));
  if (m2 && !m2.hidden) { ntrg2.click(); await wait(220); }
}

out.summary = { selects: sels.length, upgraded: upgraded.length, triggers: document.querySelectorAll('.dd').length,
  dynSelects: dynSels.length, menus: document.querySelectorAll('.dd-menu').length,
  checks: out.checks.length, fail: out.fail };
return JSON.stringify(out, null, 1);
