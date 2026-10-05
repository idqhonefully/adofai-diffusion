(function () {
  try { if (typeof gotoStep === 'function') { gotoStep(2); return 'gotoStep(2)'; } } catch (e) {}
  var items = document.querySelectorAll('.stepbar .step, .step');
  for (var i = 0; i < items.length; i++) {
    var t = (items[i].textContent || '');
    if (t.indexOf('采点') >= 0) { items[i].click(); return 'clicked ' + i; }
  }
  return 'no-step';
})()
