// 留证用前置脚本：展开折叠段 → 滚到检查器里的「N（采音基数）」→ 打开它那张下拉。
// 顶栏「密度」那张证明的是"顶栏也换了"，这张证明的是"检查器里那 20 多个字段也换了"。
document.querySelectorAll('.sec.collapsed').forEach(function (s) { s.classList.remove('collapsed'); });
new Promise(function (r) { setTimeout(r, 550); }).then(function () {
  var t = document.getElementById('in-xk_base-dd');
  if (!t) { return 0; }
  t.scrollIntoView({ block: 'center' });
  return new Promise(function (r) { setTimeout(r, 200); }).then(function () { t.click(); return 1; });
});
