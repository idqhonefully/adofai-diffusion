#!/usr/bin/env node
/* asar-vs-deliver.js —— 把「打包进 asar 的文件」与「交付目录的文件」逐条对 sha256。
 *
 * 用途：改了别人的 Electron 成品（重打包 app.asar）之后，证明**跑起来的包里那份字节**
 *       和你交付出去的那份**完全相同** —— 即「测的」和「给的」是同一份字节。
 *
 * 关键点：这个脚本要用**他包内的 Electron**去跑（它自带 asar 支持，是最权威的读取器）：
 *   cd <打了补丁的应用目录>
 *   ELECTRON_RUN_AS_NODE=1 timeout 30 './xx.exe' "D:/…/gui/tools/asar-vs-deliver.js" \
 *       "D:/…/resources/app.asar" "<REPO>/deliver/workbench-v2"
 * ⚠️ 子进程别带 ELECTRON_RUN_AS_NODE 的话，他会当 GUI 启动（沙箱里还起不来）——
 *    这里**故意**用 node 模式跑，就是为了借 Electron 的 asar 读取实现。
 *
 * 用法：node asar-vs-deliver.js <app.asar 路径> [交付目录] [映射...]
 *   映射形式：'asar内路径=交付目录内相对路径'，不给则用下面的默认表。
 */
const fs = require('fs');
const crypto = require('crypto');

const ASAR = process.argv[2];
const DELIVER = process.argv[3] || '<REPO>/deliver/workbench-v2';
const DEFAULT_MAP = [
  ['main.js', 'main-process/main.js'],
  ['renderer/index.html', 'renderer/index.html'],
  ['renderer/app.js', 'renderer/app.js'],
  ['renderer/layout.js', 'renderer/layout.js'],
  ['renderer/skin-workbench.css', 'renderer/skin-workbench.css'],
  ['renderer/workbench-elements.css', 'renderer/workbench-elements.css'],
];

if (!ASAR) {
  console.error('用法: node asar-vs-deliver.js <app.asar> [交付目录] [映射...]');
  process.exit(2);
}

const pairs = process.argv.slice(4).length
  ? process.argv.slice(4).map((s) => { const i = s.indexOf('='); return [s.slice(0, i), s.slice(i + 1)]; })
  : DEFAULT_MAP;

const sha = (p) => crypto.createHash('sha256').update(fs.readFileSync(p)).digest('hex');

let bad = 0;
for (const [inAsar, relDisk] of pairs) {
  let a = null, b = null;
  try { a = sha(ASAR + '/' + inAsar); } catch (e) { a = 'ERR:' + e.code; }
  try { b = sha(DELIVER + '/' + relDisk); } catch (e) { b = 'ERR:' + e.code; }
  const ok = a === b && !String(a).startsWith('ERR');
  bad += ok ? 0 : 1;
  console.log(`${ok ? 'OK  ' : 'DIFF'}  ${inAsar.padEnd(34)} asar=${String(a).slice(0, 16)}  disk=${String(b).slice(0, 16)}`);
}
console.log(bad
  ? `\n❌ ${bad} 个不一致 —— 交付的和跑起来的不是同一份字节`
  : '\n✅ 全部一致：Electron 从 asar 里读出来的字节 == 交付目录里的文件');
process.exit(bad ? 1 : 0);
