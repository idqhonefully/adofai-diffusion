/* iconfontcheck.js —— 校准「系统图标字体在不在」的判据（gearLike）。
 * ============================================================================
 * 背景：shell-ui.js 的 hasNativeIconFont() 决定「设置」图标走哪条路 ——
 *   走系统字形 E713（= 参考图里那只，跟系统一模一样），或退回官方 Fluent path。
 *   判据是"把 E713 画进 canvas，看这团墨是不是齿轮的形状"（gearLike）。
 *
 * 为什么必须有这个脚本：这个判据已经被写错两版、白折腾半天 ——
 *   v1「比 advance 宽度」：E713 在真/假/回退字体下都是 20，恒为 false（死代码）；
 *   v2「齿身圆环命中率 > 70%」：校准发现**真齿轮在 r=0.28S 上只有 58%**，
 *      阈值根本够不着 ⇒ 又是一条死代码。而写成"阈值调低到 50%"更糟：
 *      缺字形时画出来的是**带叉空心方框**（.notdef），它的环命中率能到 48%~65%，
 *      两者区间重叠，怎么调都会误判成 ☒。
 *   ⇒ 结论：**别用标量阈值，用形状判据**（齿轮中心空、方框中心实）。
 *
 * 本脚本干的事：
 *   ① 从 shell-ui.js 源码里把那函数**原样抠出来**（按 marker），保证校准的对象
 *      就是页面在用的那串字节 —— 不是这里的副本；
 *   ② 造两个已知答案的样本喂给它：
 *        样本 A「真齿轮」= 官方 settings_24_regular path 栅格化到同一张画布
 *        样本 B「豆腐框」= 不存在的族名 + E713（Chromium 会画 .notdef）
 *   ③ 要求 A ⇒ true、B ⇒ false，否则算校准失败（退出码非 0）。
 *   ④ 顺带把两个样本落成 PNG，方便用 pngprobe --ascii 亲眼看形状。
 *
 * 用法：node tools/iconfontcheck.js [chrome.exe]
 * ========================================================================== */
const { spawn } = require('child_process');
const http = require('http');
const fs = require('fs');
const path = require('path');

const CHROME = process.argv[2] || 'chrome';
const PORT = 9337;
const UDD = '<REPO>\\output\\.tmp\\iconfontcheck-profile';
const OUTDIR = '<REPO>\\output\\.tmp';
const SHELL_UI = path.join(__dirname, '..', 'shell-ui.js');

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const get = (p) => new Promise((res, rej) => {
  http.get({ host: '127.0.0.1', port: PORT, path: p }, (r) => {
    let b = ''; r.on('data', (c) => (b += c)); r.on('end', () => res(b));
  }).on('error', rej);
});

/* ---- 从 shell-ui.js 里抠出判据本体（按 marker，不按正则猜函数边界）----
 * 🔴 抠不到就直接失败退出：宁可红着，也不能"悄悄用一份过期副本"校准。 */
const src = fs.readFileSync(SHELL_UI, 'utf8');
const rawSet = src.match(/var SETTINGS_PATH24 = '([^']+)'/);
const rawFun = src.match(/\/\*#gearLike-start[\s\S]*?\*\/\s*(function gearLike\([\s\S]*?\n  \})/);
if (!rawSet) { throw new Error(SHELL_UI + ' 里找不到 SETTINGS_PATH24'); }
if (!rawFun) { throw new Error(SHELL_UI + ' 里找不到 gearLike（marker /*#gearLike-start*/ 还在吗？）'); }
const SET_PATH = rawSet[1];
const GEAR_FN_SRC = rawFun[1];
console.log('判据来源 ' + SHELL_UI + '（' + GEAR_FN_SRC.length + ' 字节，marker 抠取）');

(async () => {
  fs.mkdirSync(UDD, { recursive: true });
  fs.mkdirSync(OUTDIR, { recursive: true });
  const child = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
    '--disable-extensions', `--remote-debugging-port=${PORT}`, `--user-data-dir=${UDD}`,
    'about:blank',
  ], { stdio: 'ignore', windowsHide: true });

  let list = [];
  for (let i = 0; i < 80; i++) {
    try { list = JSON.parse(await get('/json/list')); if (list.some((t) => t.type === 'page')) break; } catch (_e) { }
    await sleep(250);
  }
  const page = list.find((t) => t.type === 'page');
  if (!page) { throw new Error('连不上 headless'); }
  const ws = new WebSocket(page.webSocketDebuggerUrl);
  const waiters = new Map(); let seq = 0;
  ws.addEventListener('message', (ev) => {
    const m = JSON.parse(ev.data);
    if (m.id && waiters.has(m.id)) {
      const w = waiters.get(m.id); waiters.delete(m.id);
      if (m.error) w.rej(new Error(JSON.stringify(m.error))); else w.res(m.result);
    }
  });
  await new Promise((res, rej) => { ws.addEventListener('open', res); ws.addEventListener('error', rej); });
  const send = (method, params) => new Promise((res, rej) => {
    const id = ++seq; waiters.set(id, { res, rej });
    ws.send(JSON.stringify({ id, method, params: params || {} }));
  });
  const ev = async (e) => {
    const r = await send('Runtime.evaluate', { expression: e, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) {
      throw new Error(r.exceptionDetails.text + ' ' + ((r.exceptionDetails.exception || {}).description || ''));
    }
    return r.result.value;
  };
  await send('Runtime.enable');
  await send('Page.enable');
  await send('Page.navigate', { url: 'about:blank' });
  await sleep(300);

  const out = await ev(`(function(){
    var gearLike = (${GEAR_FN_SRC});
    var SET = ${JSON.stringify(SET_PATH)};
    var S = 48;
    var p2 = new Path2D(SET);
    var k = S / 24;

    // 样本 A：官方齿轮 path 栅格化到 S×S（= "字体在、画出来是齿轮" 的替身）
    var ca = document.createElement('canvas'); ca.width = S; ca.height = S;
    var a = ca.getContext('2d');
    a.fillStyle = '#fff'; a.save(); a.scale(k, k); a.fill(p2); a.restore();
    var da = a.getImageData(0, 0, S, S);

    // 样本 B：缺字形（不存在的族名 + PUA 码点）⇒ Chromium 画 .notdef 带叉方框
    var cb = document.createElement('canvas'); cb.width = S; cb.height = S;
    var b = cb.getContext('2d');
    b.font = S + 'px "__NoSuchFamily__"';
    b.textAlign = 'center'; b.textBaseline = 'middle'; b.fillStyle = '#fff';
    b.fillText('\\uE713', S / 2, S / 2);
    var db = b.getImageData(0, 0, S, S);

    // 样本 C：真·系统字形（本机装了才有效；没装时它跟样本 B 是同一坨）
    var cc = document.createElement('canvas'); cc.width = S; cc.height = S;
    var c = cc.getContext('2d');
    c.font = S + "px 'Segoe Fluent Icons','Segoe MDL2 Assets'";
    c.textAlign = 'center'; c.textBaseline = 'middle'; c.fillStyle = '#fff';
    c.fillText('\\uE713', S / 2, S / 2);
    var dc = c.getImageData(0, 0, S, S);

    function stat(d){
      var minX=S,maxX=-1,minY=S,maxY=-1,ink=0;
      for (var y=0;y<S;y++) for (var x=0;x<S;x++) if (d.data[(y*S+x)*4+3]>40) {
        ink++; if(x<minX)minX=x; if(x>maxX)maxX=x; if(y<minY)minY=y; if(y>maxY)maxY=y;
      }
      if (maxX<0) return {ink:0, 判定口径:'全空'};
      var bw=maxX-minX+1, bh=maxY-minY+1;
      function blk(bx,by,w,h){
        var i=0,t=0;
        for(var yy=by;yy<by+h;yy++) for(var xx=bx;xx<bx+w;xx++){
          if(xx<0||yy<0||xx>=S||yy>=S) continue; t++; if(d.data[(yy*S+xx)*4+3]>40) i++;
        }
        return t? Math.round(i/t*100) : 100;
      }
      var cw=Math.max(2,Math.round(bw*0.20)), ch=Math.max(2,Math.round(bh*0.20));
      var kw=Math.max(1,Math.round(bw*0.15)), kh=Math.max(1,Math.round(bh*0.15));
      return {ink:ink, box:bw+'x'+bh, cover:Math.round(ink/(bw*bh)*100)+'%',
              aspect:Math.round(Math.min(bw,bh)/Math.max(bw,bh)*100)+'%',
              中心墨: blk(minX+Math.round((bw-cw)/2), minY+Math.round((bh-ch)/2), cw, ch)+'%',
              四角墨: [blk(minX,minY,kw,kh), blk(maxX-kw+1,minY,kw,kh),
                      blk(minX,maxY-kh+1,kw,kh), blk(maxX-kw+1,maxY-kh+1,kw,kh)].join('/')+'%'};
    }

    return JSON.stringify({
      S: S,
      样本A_官方齿轮: { 判定: gearLike(da.data, S), 量: stat(da) },
      样本B_缺字形方框: { 判定: gearLike(db.data, S), 量: stat(db) },
      样本C_系统字形E713: { 判定: gearLike(dc.data, S), 量: stat(dc) },
      pngA: ca.toDataURL('image/png'),
      pngB: cb.toDataURL('image/png'),
      pngC: cc.toDataURL('image/png')
    }, null, 1);
  })()`);

  const j = JSON.parse(out);
  console.log('\n画布 ' + j.S + '×' + j.S + '\n');
  for (const k of ['样本A_官方齿轮', '样本B_缺字形方框', '样本C_系统字形E713']) {
    const v = j[k];
    console.log((v.判定 ? '  [齿轮] ' : '  [不是] ') + k + '   量=' + JSON.stringify(v.量));
  }
  for (const [tag, d] of [['gear', j.pngA], ['notdef', j.pngB], ['sysglyph', j.pngC]]) {
    if (typeof d === 'string' && d.indexOf('data:image/png;base64,') === 0) {
      fs.writeFileSync(path.join(OUTDIR, 'iconfontcheck-' + tag + '.png'),
        Buffer.from(d.split(',')[1], 'base64'));
      console.log('  落盘 ' + path.join(OUTDIR, 'iconfontcheck-' + tag + '.png'));
    }
  }

  const okA = j.样本A_官方齿轮.判定 === true;
  const okB = j.样本B_缺字形方框.判定 === false;
  console.log('\n====================================================================');
  console.log((okA ? 'PASS' : 'FAIL') + '  样本A 真齿轮 ⇒ 判据应当为 true');
  console.log((okB ? 'PASS' : 'FAIL') + '  样本B 缺字形方框 ⇒ 判据应当为 false（否则设置图标会变成 ☒）');
  console.log(okA && okB ? '\n校准通过：判据能分开齿轮与豆腐框。'
    : '\n校准失败：判据分不开这两者，别上线 —— 会静默画错图标。');
  console.log('====================================================================');

  try { ws.close(); } catch (_e) { }
  try { child.kill(); } catch (_e) { }
  process.exit(okA && okB ? 0 : 1);
})().catch((e) => { console.error('【失败】' + (e && e.stack || e)); process.exit(1); });
