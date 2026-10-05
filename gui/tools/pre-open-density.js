// 留证用前置脚本：展开折叠段 + 打开顶栏「密度」下拉（capture.js 的 PRE 参数，被 Runtime.evaluate 求值）。
// ⚠ 这里**没有** pageeval 那层 async 包装，所以不能用顶层 await —— 用 Promise 链，
//   且最后一句必须是这个 Promise（capture.js 开了 awaitPromise 会等它）。
document.querySelectorAll('.sec.collapsed').forEach(function (s) { s.classList.remove('collapsed'); });
new Promise(function (r) { setTimeout(r, 500); }).then(function () {
  var t = document.getElementById('density-dd');
  if (t) { t.click(); }
  return 1;
});
