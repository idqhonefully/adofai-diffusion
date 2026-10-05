/* ddblurprobe.js —— 下拉菜单的「焦点 / 失活」行为实测。
 * ----------------------------------------------------------------------------
 * 起因：主人 2026-09-22 "这个截图都截不出来，只能拍照"。
 * 机制：截图工具（Win+Shift+S / PrtSc）会**抢窗口焦点** ⇒ 壳窗口 deactivate ⇒
 *       触发器 blur ⇒ 菜单在拖选区之前就收掉了（手机拍照不抢焦点，所以拍得到）。
 * 裁定：**页内**焦点转移照旧收；**整个窗口**失活不收（否则这菜单等于截不出来）。
 *
 * 🔴 跑法必须带 `--focus`：
 *     node tools/pageeval.js http://127.0.0.1:8896/ "$(cat tools/ddblurprobe.js)" '' --focus
 *   无头页默认 `document.hasFocus()===false`，那时焦点根本不会移动、blur 也不触发 ——
 *   整条分支走不到，量出来的"没反应"是环境假象（"验兜底分支先确认它走得到"）。
 *   另外合成 `.click()` 也不移焦点，所以这里用 focus()/blur() 显式驱动。
 * ----------------------------------------------------------------------------
 * 判据（每一条都必须过；③ 是本轮修的东西，①⑥ 是防"修过头"),：
 *   ① 触发器拿到焦点时菜单开着           —— 前提（不成立后面全是废话）
 *   ② 页内焦点转移 ⇒ 收起
 *   ③ 窗口失活 ⇒ **不**收（修的就是这条）
 *   ④ ③ 之后 Esc 仍能收
 *   ⑤ 点页内别处仍能收
 *   ⑥ 窗口恢复后再页内失焦 ⇒ 收起（别被修成"永不收"）
 */
var out = { steps: [] };
function step(n, o) { out.steps.push(Object.assign({ name: n }, o)); }
function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }

var ns = document.querySelectorAll('#sidebar .nav');
for (var i = 0; i < ns.length; i++) {
  if (ns[i].getAttribute('data-page') === 'settings') { ns[i].click(); break; }
}
await wait(400);
window.__devHostMsg({ type: 'appinfo', data: { appearance: 'dark', material: 'mica',
  materials: [{ name: 'mica', label: 'Mica' }] } });
await wait(300);

var t = document.getElementById('appearSelect');
var menu = document.getElementById('appearSelect-menu');
function openMenu() { if (menu.hidden) t.click(); }

step('环境', { documentHasFocus: document.hasFocus() });

// ① 前提：触发器拿到焦点、菜单开着
t.focus(); openMenu(); await wait(320);
var pre1 = { activeIsTrigger: document.activeElement === t, menuOpen: !menu.hidden };
step('① 前提：焦点在触发器 + 菜单开着', pre1);

// ② 页内焦点转移 ⇒ 收起
t.blur(); await wait(160);
var pre2 = { menuClosed: menu.hidden };
step('② 页内焦点转移', pre2);

// ③ 窗口失活 ⇒ 不收（本次修的东西）
t.focus(); openMenu(); await wait(320);
var wasOpen = !menu.hidden;
window.dispatchEvent(new Event('blur'));            // 模拟壳窗口 deactivate
t.blur();
await wait(200);
step('③ 窗口失活（截图工具抢焦点）', {
  menuWasOpen: wasOpen, menuStillOpen: !menu.hidden,
  verdict: menu.hidden ? '收起了 ⇒ 截图工具一按菜单就没了' : '仍开着 ⇒ 截得到图' });

// ④ 窗口失活状态下 Esc 仍能收
t.focus();
t.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
await wait(220);
step('④ 失活态下 Esc', { menuClosed: menu.hidden });

// ⑤ 点页内别处仍能收
t.focus(); openMenu(); await wait(320);
document.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
await wait(220);
step('⑤ 点页内别处', { menuClosed: menu.hidden });

// ⑥ 窗口恢复后再页内失焦 ⇒ 收起（防"修成永不收"）
window.dispatchEvent(new Event('focus'));
t.focus(); openMenu(); await wait(320);
var open6 = !menu.hidden;
t.blur(); await wait(200);
step('⑥ 窗口恢复后再页内失焦', {
  menuWasOpen: open6, menuClosed: menu.hidden,
  verdict: menu.hidden ? '收起了 ✓' : '没收起 ✗ 修过头了' });

// —— 判据 ——
out.checks = [
  { name: '① 触发器拿到焦点时菜单是开的（前提）', ok: pre1.activeIsTrigger && pre1.menuOpen },
  { name: '② 页内焦点转移 ⇒ 收起', ok: pre2.menuClosed === true },
  { name: '③ 窗口失活 ⇒ **不**收（截图才截得到）★本轮修的', ok: out.steps[3].menuStillOpen === true },
  { name: '④ 失活态下 Esc 仍能收', ok: out.steps[4].menuClosed === true },
  { name: '⑤ 点页内别处仍能收', ok: out.steps[5].menuClosed === true },
  { name: '⑥ 窗口恢复后页内失焦仍会收（没修过头）', ok: out.steps[6].menuClosed === true },
];
out.summary = { checks: out.checks.length, fail: out.checks.filter(function (c) { return !c.ok; }).length };
return JSON.stringify(out, null, 1);
