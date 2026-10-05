#!/usr/bin/env node
/* asar.mjs —— 零依赖的 Electron asar 读取 / 重打包工具（不联网、不装包、不写 C 盘）
 *
 * 为什么自己写：`@electron/asar` 要 npm 装（会污染环境 / 落 C 盘缓存），
 * 而 asar 的格式其实极简单、且本机实测过（见下）。
 *
 * ── asar 容器格式（本机 2026-09-19 实测反推并验证）──────────────────────────
 *   [0..4)    UInt32LE = 4                 ← 第一个 pickle 的 payload 长度
 *   [4..8)    UInt32LE = S                 ← 第二个 pickle 的总长度
 *   [8..8+S)  第二个 pickle（存 header JSON）:
 *               [0..4)   UInt32LE = payloadSize = align4(4 + L)
 *               [4..8)   UInt32LE = L        ← JSON 字节数
 *               [8..8+L) header JSON (UTF-8)
 *               余下 (payloadSize - 4 - L) 字节为 0 填充（对齐到 4）
 *             ⇒ S = align4(4 + L) + 4
 *   数据区起点 = 8 + S
 *   每个文件的 `offset` 相对数据区起点；**实测原包是零间隙紧凑排列**（gap 全 0，
 *   尾部无冗余字节），所以重打包就是「按 header 顺序首尾相接」。
 *
 * ── header 结构 ─────────────────────────────────────────────────────────
 *   { files: { "<名>": { files: {...} }            ← 目录
 *                     | { size, offset, integrity } ← 文件
 *   } }
 *   offset 为**字符串**；同级名字按字典序排列（原包实测如此）。
 *   integrity = { algorithm:"SHA256", hash:<内容 sha256 的 hex>,
 *                 blockSize:4194304, blocks:[<每 4MB 一块的 sha256 hex>, ...] }
 *   ★ 已实测：hash === sha256(整份文件内容) 的 hex；单块文件 blocks === [hash]。
 *   Electron 只在开了 `EnableEmbeddedAsarIntegrityValidation` fuse 时才校验，
 *   但**照算不误** —— 万一对方打包时开了，改过的包也不会被拒。
 *
 * 用法：
 *   node asar.mjs list    <a.asar>
 *   node asar.mjs tree    <a.asar>
 *   node asar.mjs get     <a.asar> <内部路径> [输出文件]
 *   node asar.mjs extract <a.asar> <目标目录>
 *   node asar.mjs pack    <原 a.asar> <覆盖目录|-> <输出 a.asar>
 *       以「原 asar」为底，用覆盖目录里相对路径同名的文件**替换/新增**，其余原样搬运。
 *       覆盖目录传 `-` 表示空覆盖 ⇒ 输出应与原包**逐字节一致**（自检用）。
 */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';

const BLOCK = 4194304;
const align4 = (n) => n + ((4 - (n % 4)) % 4);

function readAsar(file) {
  const buf = fs.readFileSync(file);
  if (buf.length < 16) throw new Error('不是 asar：文件太小');
  const S = buf.readUInt32LE(4);
  const L = buf.readUInt32LE(12);
  const header = JSON.parse(buf.subarray(16, 16 + L).toString('utf8'));
  return { buf, header, S, L, dataStart: 8 + S };
}

function collect(node, prefix, out) {
  for (const [name, v] of Object.entries(node.files || {})) {
    const p = prefix ? prefix + '/' + name : name;
    if (v.files) collect(v, p, out);
    else out.push({ path: p, size: v.size, offset: Number(v.offset) });
  }
  return out;
}

const entryBuf = (a, e) => a.buf.subarray(a.dataStart + e.offset, a.dataStart + e.offset + e.size);

function integrityOf(buf) {
  const blocks = [];
  for (let i = 0; i < buf.length; i += BLOCK) {
    blocks.push(crypto.createHash('sha256').update(buf.subarray(i, Math.min(i + BLOCK, buf.length))).digest('hex'));
  }
  return {
    algorithm: 'SHA256',
    hash: crypto.createHash('sha256').update(buf).digest('hex'),
    blockSize: BLOCK,
    blocks: blocks.length ? blocks : [crypto.createHash('sha256').update(buf).digest('hex')],
  };
}

/** 把 [{path, buf}]（已按 path 排好序）装配成 asar 字节 */
function buildAsar(list) {
  // 1) 紧凑排布 + 算 integrity
  let off = 0;
  const entries = list.map((e) => {
    const rec = { size: e.buf.length, offset: String(off), integrity: integrityOf(e.buf) };
    off += e.buf.length;
    return { path: e.path, buf: e.buf, rec };
  });
  // 2) 建 header 树（插入顺序 = 排序后的顺序）
  const root = { files: {} };
  for (const e of entries) {
    const parts = e.path.split('/');
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      if (!node.files[parts[i]]) node.files[parts[i]] = { files: {} };
      node = node.files[parts[i]];
    }
    node.files[parts[parts.length - 1]] = e.rec;
  }
  // 3) 序列化 header pickle
  const json = Buffer.from(JSON.stringify(root), 'utf8');
  const payloadSize = align4(4 + json.length);
  const S = payloadSize + 4;
  const pickle = Buffer.alloc(S);
  pickle.writeUInt32LE(payloadSize, 0);
  pickle.writeUInt32LE(json.length, 4);
  json.copy(pickle, 8);
  const head = Buffer.alloc(8);
  head.writeUInt32LE(4, 0);
  head.writeUInt32LE(S, 4);
  return { head, pickle, data: Buffer.concat(entries.map((e) => e.buf)), fileCount: entries.length };
}

function walkDir(dir, prefix, out) {
  for (const name of fs.readdirSync(dir)) {
    const full = path.join(dir, name);
    const rel = prefix ? prefix + '/' + name : name;
    const st = fs.statSync(full);
    if (st.isDirectory()) walkDir(full, rel, out);
    else out.push({ path: rel.replace(/\\/g, '/'), buf: fs.readFileSync(full) });
  }
  return out;
}

// ────────────────────────────────────────────────────────────────── 主流程
const [, , cmd, arg1, arg2, arg3] = process.argv;

if (cmd === 'list' || cmd === 'tree') {
  const a = readAsar(arg1);
  const list = collect(a.header, '', []);
  if (cmd === 'list') {
    for (const e of list) console.log(String(e.size).padStart(10) + '  ' + e.path);
    console.log('--- ' + list.length + ' 个文件 | 数据区起点 ' + a.dataStart + ' | header S=' + a.S + ' L=' + a.L);
  } else {
    (function rec(node, depth) {
      for (const [name, v] of Object.entries(node.files || {})) {
        if (v.files) { console.log('  '.repeat(depth) + name + '/'); rec(v, depth + 1); }
        else console.log('  '.repeat(depth) + name + '  (' + v.size + ' B)');
      }
    })(a.header, 0);
  }
} else if (cmd === 'get') {
  const a = readAsar(arg1);
  const e = collect(a.header, '', []).find((x) => x.path === arg2);
  if (!e) { console.error('没找到: ' + arg2); process.exit(1); }
  const buf = entryBuf(a, e);
  if (arg3) { fs.writeFileSync(arg3, buf); console.error('已写出 ' + arg3 + ' (' + buf.length + ' B)'); }
  else process.stdout.write(buf);
} else if (cmd === 'extract') {
  const a = readAsar(arg1);
  const list = collect(a.header, '', []);
  for (const e of list) {
    const out = path.join(arg2, e.path);
    fs.mkdirSync(path.dirname(out), { recursive: true });
    fs.writeFileSync(out, entryBuf(a, e));
  }
  console.log('解出 ' + list.length + ' 个文件 → ' + arg2);
} else if (cmd === 'pack') {
  const a = readAsar(arg1);
  const overlay = [];
  if (arg2 !== '-') walkDir(arg2, '', overlay);
  const orig = collect(a.header, '', []);
  const byPath = new Map(orig.map((e) => [e.path, e]));

  // 以原包为底，覆盖目录里的替换/新增
  const merged = new Map();
  for (const e of orig) merged.set(e.path, entryBuf(a, e));
  let replaced = 0, added = 0;
  for (const o of overlay) {
    if (merged.has(o.path)) replaced++; else added++;
    merged.set(o.path, o.buf);
  }
  const list = [...merged.keys()].sort().map((p) => ({ path: p, buf: merged.get(p) }));

  const { head, pickle, data, fileCount } = buildAsar(list);
  const out = Buffer.concat([head, pickle, data]);
  fs.mkdirSync(path.dirname(arg3), { recursive: true });
  fs.writeFileSync(arg3, out);
  console.log(`已写出 ${arg3}`);
  console.log(`  文件 ${fileCount} 个（替换 ${replaced} / 新增 ${added} / 原样 ${fileCount - replaced - added}）`);
  console.log(`  大小 ${out.length} B（原 ${a.buf.length} B，差 ${out.length - a.buf.length}）`);
  console.log(`  S=${pickle.length} 数据区起点=${8 + pickle.length}`);
} else {
  console.error('用法: list | tree | get | extract | pack   （见文件头注释）');
  process.exit(2);
}
