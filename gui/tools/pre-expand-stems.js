// 截图前把第一个折叠组点开（验证"点击展开"的外观）
(function () {
  var h = document.querySelector('#stemList .stem-head');
  if (h) h.click();
  return h ? 'clicked' : 'no-head';
})()
