// verify_hitsound_fix.js —— 确定性仿真，验证「打拍音不生效」的时序修复。
// 不依赖浏览器：用虚拟时钟 + 虚拟 rAF 复刻 vendor 的两个关键行为：
//   · preSynthesizeHitsoundsWithProgress 开头 `if (!tileStartTimes.length) return`（vendor 85972）
//   · tileStartTimes 只在「渲染循环首帧」后才填好（vendor 84131-84137 update(stats)）
// 证明：旧实现（建完同步喊一次）必然失败；新实现（waitTileStartTimes 轮询）必然成功。

// ---- 虚拟时钟 + 虚拟 rAF ----
let CLOCK = 0;            // ms
let RAF_QUEUE = [];
function raf(cb) { RAF_QUEUE.push(cb); return RAF_QUEUE.length; }
function flushFrames(n, dtMs, prepare) {  // 推进 n 帧，每帧 dtMs；prepare(帧序) 在每帧回调前跑
  for (let i = 0; i < n; i++) {
    if (prepare) prepare(i);
    CLOCK += dtMs;
    const q = RAF_QUEUE; RAF_QUEUE = [];
    for (const cb of q) cb();          // 每帧回调（第一帧后 tileStartTimes 已填好）
  }
}
const sleep = (ms) => new Promise(r => { const t = setTimeout(() => r(), 0); }); // 真异步占位

// ---- 模拟 player（复制 vendor 关键字段/方法）----
function makePlayer() {
  const p = {
    tileStartTimes: [],               // 建完时为空（vendor 真实行为）
    hitsoundManager: {
      _synth: false,
      isEnabled: () => true,
      isSynthesized: () => p.hitsoundManager._synth,
    },
    // vendor 85972 守卫：tileStartTimes 空 → 提前 return，不合成
    async preSynthesizeHitsoundsWithProgress() {
      if (!p.tileStartTimes || p.tileStartTimes.length === 0) {
        console.log('   [vendor] preSynthesize 命中空 tileStartTimes → 提前 return（不合成）');
        return;
      }
      p.hitsoundManager._synth = true;  // 合成成功
      console.log(`   [vendor] 合成成功，tiles=${p.tileStartTimes.length}`);
    },
  };
  return p;
}

// ---- 复制 app.js 的新逻辑 ----
let hitsoundToken = 0;
function waitTileStartTimes(p, ms, token) {
  return new Promise((resolve) => {
    const t0 = CLOCK;
    const step = () => {
      if (token != null && token !== hitsoundToken) return resolve(false);
      if (p.tileStartTimes && p.tileStartTimes.length > 0) return resolve(true);
      if (CLOCK - t0 > ms) return resolve(false);
      raf(step);
    };
    raf(step);
  });
}

async function NEW_synthesize(pv) {
  const token = ++hitsoundToken;
  const p = pv;
  if (!p || !p.preSynthesizeHitsoundsWithProgress) return;
  if (!p.hitsoundManager.isEnabled() || p.hitsoundManager.isSynthesized()) return;
  const ready = await waitTileStartTimes(p, 2000, token);
  if (token !== hitsoundToken) return;
  if (!ready) return;
  await p.preSynthesizeHitsoundsWithProgress();
}

// 旧实现：建完立刻同步喊一次（不轮询）
async function OLD_synthesize(pv) {
  const p = pv;
  if (!p || !p.preSynthesizeHitsoundsWithProgress) return;
  await p.preSynthesizeHitsoundsWithProgress();
}

// ================= 跑测试 =================
(async () => {
  let pass = true;

  // ---- 场景 A：旧实现 ----
  console.log('\n=== 场景 A：旧实现（建完同步喊一次）===');
  {
    const p = makePlayer();
    await OLD_synthesize(p);            // 此刻 tileStartTimes 还空
    flushFrames(2, 16);                 // 首帧后引擎才填好 tileStartTimes
    console.log(`   结果 isSynthesized = ${p.hitsoundManager.isSynthesized()}（应为 false）`);
    if (p.hitsoundManager.isSynthesized()) { console.log('   ❌ 旧实现竟成功了？不符合预期'); pass = false; }
    else console.log('   ✅ 旧实现确实失败（与线上「打拍音不生效」一致）');
  }

  // ---- 场景 B：新实现（轮询等就绪）----
  console.log('\n=== 场景 B：新实现（waitTileStartTimes 轮询）===');
  {
    const p = makePlayer();
    const done = NEW_synthesize(p);     // 异步开始轮询
    // 模拟引擎：首帧（帧0）的 update(stats) 才把 tileStartTimes 填上
    flushFrames(2, 16, (i) => { if (i === 0) p.tileStartTimes = Array.from({length: 50}, (_, k) => k * 100); });
    await done;
    console.log(`   结果 isSynthesized = ${p.hitsoundManager.isSynthesized()}（应为 true）`);
    if (!p.hitsoundManager.isSynthesized()) { console.log('   ❌ 新实现仍失败'); pass = false; }
    else console.log('   ✅ 新实现成功（打拍音会响）');
  }

  // ---- 场景 C：重建竞态（合成途中又重建）----
  console.log('\n=== 场景 C：重建竞态（新实现 token 防脏写）===');
  {
    const p = makePlayer();
    const done = NEW_synthesize(p);
    hitsoundToken++;                    // 模拟 ensurePreview 又建了新预览
    flushFrames(2, 16, (i) => { if (i === 0) p.tileStartTimes = Array.from({length: 50}, (_, k) => k * 100); });
    await done;
    console.log(`   结果 isSynthesized = ${p.hitsoundManager.isSynthesized()}（应为 false，脏结果被丢弃）`);
    if (p.hitsoundManager.isSynthesized()) { console.log('   ❌ 脏写未被拦截'); pass = false; }
    else console.log('   ✅ 重建竞态被 token 正确拦截');
  }

  console.log('\n=================================');
  console.log(pass ? '✅ 全部通过：修复逻辑正确（打拍音会在 tileStartTimes 就绪后合成）'
                   : '❌ 存在失败用例');
  process.exit(pass ? 0 : 1);
})();
