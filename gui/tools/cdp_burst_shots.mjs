// 临时工具：用 CDP 在真实时间轴上连拍，验证动画 WebP（img / background-image）是否逐帧播放。
// 用法：node _cdp_burst.mjs <pageUrlRegex> <outPrefix>
//   例：node _cdp_burst.mjs _ls_test _cdp      （新样式：Win11 弧）
//       node _cdp_burst.mjs _ls_old  _cdpold    （旧样式：CSS 缺口环）
const PORT = 9333;
const OUT = "<REPO>/gui";
const PAGE_RE = process.argv[2] || "_ls_test";
const PREFIX = process.argv[3] || "_cdp";
const SHOTS = 8, GAP = 900;          // 8 张 × 900ms ≈ 覆盖 7.14s 的一个完整周期

const targets = await (await fetch(`http://127.0.0.1:${PORT}/json`)).json();
const t = targets.find(x => x.type === "page" && new RegExp(PAGE_RE).test(x.url));
if (!t) { console.log("NO PAGE TARGET:", JSON.stringify(targets.map(x => x.url))); process.exit(1); }

const ws = new WebSocket(t.webSocketDebuggerUrl);
let id = 0; const pend = new Map();
ws.addEventListener("message", ev => {
  const m = JSON.parse(ev.data);
  if (m.id && pend.has(m.id)) { pend.get(m.id)(m.result); pend.delete(m.id); }
});
await new Promise(r => ws.addEventListener("open", r));
const send = (method, params = {}) => new Promise(res => {
  const i = ++id; pend.set(i, res); ws.send(JSON.stringify({ id: i, method, params }));
});
await send("Page.enable");
await send("Runtime.enable");

// 页面自证：尺寸 + background 是否真的拿到了图
const ev = await send("Runtime.evaluate", {
  expression: `JSON.stringify((()=>{const q=s=>document.querySelector(s);const im=q("img.imgbox"),bg=q(".bgbox,.oldring"),d=q("#stageList .st.run .dot");const r=e=>{if(!e)return null;const b=e.getBoundingClientRect();return [Math.round(b.width),Math.round(b.height)];};return{imgNatural:im?[im.naturalWidth,im.naturalHeight]:null,imgBox:r(im),bgBox:r(bg),bgImage:bg?getComputedStyle(bg).backgroundImage.slice(0,58):null,runBox:r(d),runBg:d?getComputedStyle(d).backgroundImage.slice(0,58):null,runBorderTop:d?getComputedStyle(d).borderTopColor:null};})())`,
  returnByValue: true
});
console.log("DOM 自查:", ev.result.value);

const fs = await import("node:fs");
for (let i = 0; i < SHOTS; i++) {
  const r = await send("Page.captureScreenshot", { format: "png" });
  const p = `${OUT}/${PREFIX}_${i}.png`;
  fs.writeFileSync(p, Buffer.from(r.data, "base64"));
  console.log(`shot ${i} -> ${p} (${fs.statSync(p).size}B)`);
  if (i < SHOTS - 1) await new Promise(r2 => setTimeout(r2, GAP));
}
ws.close();
