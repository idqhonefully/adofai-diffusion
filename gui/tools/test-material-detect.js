/* test-material-detect.js —— 验证 main.js 里那段「窗口材质检测」真的判得对。
 *
 * 做法：**从交付版的 main.js 里把函数原文抠出来**（不抄一份，测的就是要发出去的那份代码），
 * 在 Node 里换着条件跑：Win11 → 应该 acrylic；Win10 / --no-material / 非 Windows → none。
 * 注册表那步用的是真实调用，会顺带报出本机「透明效果」开关的真值。
 *
 * 用法：node tools/test-material-detect.js [main.js 路径]
 */
const fs = require('fs');
const { spawnSync } = require('child_process');

const file = process.argv[2] || '<REPO>\\deliver\\workbench-skin\\main-process\\main.js';
const src = fs.readFileSync(file, 'utf8');

function grab(name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('未在 main.js 里找到 ' + name);
  let d = 0, started = false;
  for (let j = i; j < src.length; j++) {
    const c = src[j];
    if (c === '{') { d++; started = true; } else if (c === '}') { d--; if (started && d === 0) return src.slice(i, j + 1); }
  }
  throw new Error('括号不匹配: ' + name);
}

// 只替换 getSystemVersion / platform，其余（spawnSync）保持真实
const real = process;
const mk = (platform, sysver) => {
  const fake = Object.create(real);
  fake.platform = platform;
  fake.getSystemVersion = () => sysver;
  return fake;
};

const build = (fakeProc, noMaterial) => {
  const code = [grab('sysBuild'), grab('transparencyEnabled'), grab('pickMaterial')].join('\n');
  // eslint-disable-next-line no-new-func
  return new Function('process', 'spawnSync', 'os', 'NO_MATERIAL',
    'return (function(){' + code + '\nreturn { sysBuild, transparencyEnabled, pickMaterial };})()'
  )(fakeProc, spawnSync, require('node:os'), noMaterial);
};

let pass = 0, fail = 0;
const check = (name, got, want) => {
  const ok = got === want;
  ok ? pass++ : fail++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}  →  ${got}${ok ? '' : `（应为 ${want}）`}`);
};

console.log('—— 本机真实调用 ——');
const me = build(real, false);
console.log('sysBuild()            =', me.sysBuild());
console.log('transparencyEnabled() =', me.transparencyEnabled(), '(注册表 EnableTransparency)');
console.log('pickMaterial()        =', me.pickMaterial());
console.log('');

// ★ 注册表那条路必须单独证一次：`transparencyEnabled()` 读不到时会**兜底返回 true**，
//   所以只看到 "true" 并不能说明真的读到了值。这里把原始结果打出来对账。
console.log('—— 注册表读取对账（区分"真读到"vs"读不到走了兜底"）——');
const KEY = 'HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Themes\\Personalize';
const raw = spawnSync('reg', ['query', KEY, '/v', 'EnableTransparency'], { encoding: 'utf8', windowsHide: true });
const txt = raw.stdout || '';
const m = /EnableTransparency\s+REG_DWORD\s+0x([0-9a-f]+)/i.exec(txt);
if (m) {
  const val = parseInt(m[1], 16);
  console.log(`注册表真实值 = ${val}  （status=${raw.status}）`);
  check(`transparencyEnabled() 与注册表一致（真值 ${val}）`, me.transparencyEnabled(), val === 1);
} else {
  console.log(`⚠ 注册表没读到（status=${raw.status}），transparencyEnabled() 走了兜底 → true`);
  console.log(`  stderr: ${JSON.stringify((raw.stderr || '').slice(0, 120))}`);
  check('读不到时兜底为 true（与原行为一致，不阻断材质）', me.transparencyEnabled(), true);
}
console.log('');

console.log('—— 条件矩阵（抠的是 main.js 里的原文）——');
check('Win11 build 28000（本机）→ acrylic', build(mk('win32', '10.0.28000'), false).pickMaterial(), 'acrylic');
check('Win11 build 22000（最低门槛）→ acrylic', build(mk('win32', '10.0.22000'), false).pickMaterial(), 'acrylic');
check('Win10 build 19045 → none（没有 Mica/Acrylic）', build(mk('win32', '10.0.19045'), false).pickMaterial(), 'none');
check('Win11 + --no-material → none（开关有效）', build(mk('win32', '10.0.28000'), true).pickMaterial(), 'none');
check('非 Windows → none', build(mk('darwin', '14.0.0'), false).pickMaterial(), 'none');
// 没有 getSystemVersion（纯 Node / 老 Electron）→ 退 os.release()，结果应与本机真实版本一致
const realBuild = Number((require('node:os').release().split('.')[2] || 0));
const wantFB = realBuild >= 22000 ? 'acrylic' : 'none';
check(`getSystemVersion 缺失 → 退 os.release()（本机 build ${realBuild}）`,
  build(mk('win32', ''), false).pickMaterial(), wantFB);
const noVer = Object.create(real); noVer.platform = 'win32'; delete noVer.getSystemVersion;
check('process 完全没有 getSystemVersion → 同样能判', build(noVer, false).pickMaterial(), wantFB);

console.log(`\n==== ${pass} 通过 / ${fail} 失败 ====`);
process.exit(fail ? 1 : 0);
