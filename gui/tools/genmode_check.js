/*
 * genmode_check.js —— 「采点方式」UI 常驻回归（不依赖浏览器 / WebView2）
 *
 * 为什么要有它：生成页的采点方式（OSN1 / MuScriptor）出过 5 个坑，且都是
 * "看着对、点了才知道" 的类型，界面截图看不出来：
 *   ① 采点方式排在精度档下面（顺序反了）
 *   ② OSN1 没排第一、不是默认
 *   ③ 选 OSN1 只把三档精度变灰，没真收起
 *   ④ 🔴 按钮可用性只在点精度卡时刷新 ⇒ 选 OSN1 必须「先点一次 MuScriptor 精度、
 *      再切回 OSN1」才会亮
 *   ⑤ 覆盖层阶段名写死成 MuScriptor 的 5 步 ⇒ 选 OSN1 也显示"逐轨 MuScriptor 转谱"
 *
 * 做法：直接从 gui/index.html 里**抽出真实函数**（不是复制一份），配最小 DOM 桩跑断言。
 * 这样界面改了实现、工具会跟着测真实代码，不会测到"副本"。
 *
 * 用法： node gui/tools/genmode_check.js
 * 退出码：0 全过 / 1 有失败（可直接进 CI 或 publish 前门禁）
 *
 * ⚠️ index.html 是 CRLF ⇒ 抽取前必须先 .replace(/\r\n/g,"\n")，否则 \n 锚点全不匹配。
 */
const fs = require("fs");
const path = require("path");

const HTML = path.join(__dirname, "..", "index.html");
const raw = fs.readFileSync(HTML, "utf8");
const html = raw.replace(/\r\n/g, "\n");   // 见文件头 CRLF 提醒

let fails = 0, total = 0;
function chk(name, cond, extra) {
  total++;
  console.log((cond ? "  PASS  " : "  FAIL  ") + name + (extra !== undefined ? "   [" + extra + "]" : ""));
  if (!cond) fails++;
}
function section(t) { console.log("\n== " + t + " =="); }

// ---------------------------------------------------------------- 最小 DOM 桩
function mkEl(id, attrs) {
  return {
    id, _attrs: Object.assign({}, attrs || {}), style: {},
    textContent: "", innerHTML: "", disabled: false,
    getAttribute(k) { return this._attrs[k] === undefined ? null : this._attrs[k]; },
    setAttribute(k, v) { this._attrs[k] = v; },
    /* ★ 2026-09-28（之三）：setMode 不再硬切 style.display，改为在 #precisionGrid
       上 toggle("folded", isOsn1)。桩的 classList 必须是**真记账**的（原 no-op 会让
       断言读不到 .folded 状态），所以用 Set 撑一个 add/remove/toggle/contains。 */
    classList: (function () {
      var s = new Set();
      return {
        add: function (c) { s.add(c); },
        remove: function (c) { s.delete(c); },
        toggle: function (c, force) {
          if (force === undefined) {
            if (s.has(c)) { s.delete(c); return false; }
            s.add(c); return true;
          }
          if (force) s.add(c); else s.delete(c);
          return !!force;
        },
        contains: function (c) { return s.has(c); },
      };
    })(),
    addEventListener() {},
  };
}

// #stageList 桩：innerHTML 赋值时按 buildStageList 生成的规则串解析出 .st
function mkStageList() {
  return {
    _stages: [], _html: "",
    get innerHTML() { return this._html; },
    set innerHTML(s) {
      this._html = s;
      this._stages = s.split('<div class="st" data-st="').slice(1).map(p => {
        const cls = new Set();
        return {
          n: parseInt(p, 10),
          text: p.split("</div>")[1] || "",
          det: { textContent: "" },
          getAttribute(k) { return k === "data-st" ? String(this.n) : null; },
          classList: {
            add: c => cls.add(c), remove: (...cs) => cs.forEach(c => cls.delete(c)),
            contains: c => cls.has(c),
          },
        };
      });
    },
  };
}

const els = {};
["modeMus", "modeOsn1", "precisionGrid", "step2Label",
 "precLabel", "genSub", "genChain", "genBtn"].forEach(id => { els[id] = mkEl(id); });
els.modeMus._attrs["data-mode"] = "muscriptor";
els.modeOsn1._attrs["data-mode"] = "osn1";
els.genBtn.disabled = true;
els.stageList = mkStageList();
els.genLog = { innerHTML: "" };
els.overlayTimer = { textContent: "" };
els.overlayText = { textContent: "" };

global.document = {
  querySelectorAll(sel) {
    if (sel.indexOf("modeMus") >= 0) return [els.modeMus, els.modeOsn1];
    if (sel === "#stageList .st") return els.stageList._stages;
    if (sel.indexOf("#precisionGrid .pcard") >= 0) return [];   // 本工具不测点卡，另有覆盖
    return [];
  },
  querySelector(sel) {
    const mm = sel.match(/data-st="(\d+)"\s*\]\s*\.det/);
    if (mm) {
      const st = els.stageList._stages.find(x => String(x.n) === mm[1]);
      return st ? st.det : null;
    }
    return null;
  },
};
function $(id) { return els[id] || { textContent: "", innerHTML: "" }; }
global.clockStart = function () {};
global.clockStop = function () {};

// ---------------------------------------------------------------- 抽取真实代码
function extract(re, what) {
  const m = html.match(re);
  if (!m) { console.log("EXTRACT FAILED: " + what); process.exit(2); }
  return m[0];
}
const CODE_MODE = extract(
  /var MODE_TEXT = \{[\s\S]*?setMode\("osn1"\);?[^\n]*/, "MODE_TEXT/updateGenBtn/setMode");
const CODE_STAGE = extract(
  /var STAGE_DEFS = \{[\s\S]*?\n  \}\n  function finishStages\(\)\{[\s\S]*?\n  \}/,
  "STAGE_DEFS/buildStageList/resetStages/markStage");

// ---------------------------------------------------------------- ① 采点方式 / 按钮可用性
(function () {
  var AUDIO_PATH = "", PRECISION = "", GEN_MODE = "osn1";
  eval(CODE_MODE);

  section("默认态（抽出并执行真实的 setMode(\"osn1\")）");
  chk("默认采点方式 = osn1", GEN_MODE === "osn1", GEN_MODE);
  chk("未选音乐 → 生成按钮禁用", els.genBtn.disabled === true);
  chk("OSN1 收起精度区（折叠 .folded，不是变灰）",
      els.precisionGrid.classList.contains("folded"), JSON.stringify(els.precisionGrid.classList.contains("folded")));
  chk("第 2 步文案 = 选择采点方式", els.step2Label.textContent === "选择采点方式", els.step2Label.textContent);
  chk("precLabel 说明不吃精度档", /OSN1/.test(els.precLabel.textContent), els.precLabel.textContent);

  section("坑④：选完音乐后 OSN1 应**立刻**可生成（不许先点 MuScriptor 精度）");
  AUDIO_PATH = "D:/x/song.ogg";
  setMode("osn1");
  chk("OSN1 + 已选音乐 → 生成按钮已启用", els.genBtn.disabled === false, "disabled=" + els.genBtn.disabled);

  section("切到 MuScriptor：必须选精度");
  setMode("muscriptor");
  chk("未选精度 → 禁用", els.genBtn.disabled === true);
  chk("精度区展开", !els.precisionGrid.classList.contains("folded"), JSON.stringify(els.precisionGrid.classList.contains("folded")));
  chk("第 2 步文案 = 选择精度", els.step2Label.textContent === "选择精度", els.step2Label.textContent);
  PRECISION = "medium";                 // 模拟点了一张精度卡
  setMode("muscriptor");
  chk("已选精度 → 启用", els.genBtn.disabled === false);
  chk("precLabel 回显 MEDIUM", els.precLabel.textContent === "MEDIUM", els.precLabel.textContent);
  chk("来回切换保留已选精度", (function () {
    setMode("osn1"); setMode("muscriptor");
    return els.precLabel.textContent === "MEDIUM" && els.genBtn.disabled === false;
  })());

  section("文案随模式走（坑⑤的前半：静态说明也得跟着换）");
  setMode("osn1");
  chk("副标题讲 OSN1", /OSN1/.test(els.genSub.textContent));
  chk("链路写 OSN1 多轨采点", /OSN1 多轨采点/.test(els.genChain.innerHTML));
  chk("OSN1 链路**不含** MuScriptor", !/MuScriptor/.test(els.genChain.innerHTML));
  setMode("muscriptor");
  chk("副标题讲转谱", /MuScriptor|转谱/.test(els.genSub.textContent));
  chk("链路写逐轨 MuScriptor 转谱", /逐轨 MuScriptor 转谱/.test(els.genChain.innerHTML));

  section("非法值回退");
  setMode("garbage");
  chk("未知 mode → 回退 osn1", GEN_MODE === "osn1", GEN_MODE);
})();

// ---------------------------------------------------------------- ② 覆盖层阶段名
(function () {
  var GEN_MODE = "osn1", LOG_BUF = [];
  eval(CODE_STAGE);

  section("坑⑤：覆盖层阶段名随采点方式切换");
  GEN_MODE = "osn1";
  resetStages();
  chk("构建 5 步", els.stageList._stages.length === 5, els.stageList._stages.length);
  chk("第 3 步 = OSN1 多轨采点", /OSN1 多轨采点/.test(els.stageList._stages[2].text), els.stageList._stages[2].text.trim());
  chk("第 4 步 = 汇总多轨时间戳 JSON", /汇总多轨时间戳 JSON/.test(els.stageList._stages[3].text));
  chk("**不出现** 逐轨 MuScriptor 转谱", !/MuScriptor/.test(els.stageList._html));
  chk("**不出现** 合并为单文件多轨 MIDI", !/合并为单文件多轨 MIDI/.test(els.stageList._html));

  markStage(2, "OSN1 分离 + 采点", "BSR-ONNX 分离 6 轨 → OnsetNet 多轨踩点");
  chk("第 1 步标 ok", els.stageList._stages[0].classList.contains("ok"));
  chk("第 2 步标 run", els.stageList._stages[1].classList.contains("run"));
  chk("detail 落在**第 2 步**（原来写死只认第 3 步 st3Det）",
      /OnsetNet/.test(els.stageList._stages[1].det.textContent), els.stageList._stages[1].det.textContent);
  chk("第 3 步 detail 仍为空", els.stageList._stages[2].det.textContent === "");

  GEN_MODE = "muscriptor";
  resetStages();
  chk("第 3 步 = 逐轨 MuScriptor 转谱", /逐轨 MuScriptor 转谱/.test(els.stageList._stages[2].text));
  chk("第 4 步 = 合并为单文件多轨 MIDI", /合并为单文件多轨 MIDI/.test(els.stageList._stages[3].text));
  markStage(3, "逐轨转谱 MuScriptor", "2/5  piano（medium 档）");
  chk("detail 落在第 3 步", /piano/.test(els.stageList._stages[2].det.textContent));

  section("越界保护");
  let threw = false;
  try { markStage(0, "x", "y"); markStage(9, "x", "y"); } catch (e) { threw = true; }
  chk("n=0 / n=9 不抛异常", !threw);
})();

// ---------------------------------------------------------------- ③ 回执文案（轨数分母）
const CODE_DONE = extract(/function genDoneText\([\s\S]*?\n  \}/, "genDoneText");
(function () {
  var GEN_MODE_RUN = "osn1";
  eval(CODE_DONE);

  section("坑⑥：回执轨数分母写死 /5 ⇒ OSN1 会显示「6/5 条轨转谱成功」");
  chk("OSN1 = N/6 条轨踩点",
      genDoneText(6, []) === "生成完成（6/6 条轨踩点）", genDoneText(6, []));
  chk("OSN1：一轨 0 点也不套 /5 口径", /\/6 /.test(genDoneText(5, [])), genDoneText(5, []));

  GEN_MODE_RUN = "muscriptor";
  chk("MuScriptor = N/5 条轨转谱成功",
      genDoneText(5, []) === "生成完成（5/5 条轨转谱成功）", genDoneText(5, []));
  chk("失败轨拼进文案", /失败：guitar/.test(genDoneText(4, ["guitar"])), genDoneText(4, ["guitar"]));
  chk("stems/failed 缺省不炸（老消息兼容）",
      genDoneText(undefined, undefined) === "生成完成（0/5 条轨转谱成功）",
      genDoneText(undefined, undefined));

  section("快照：生成期间改模式，不影响这次回执口径");
  chk("genBtn 点击时把 GEN_MODE 快照进 GEN_MODE_RUN",
      /GEN_MODE_RUN = GEN_MODE;/.test(html));
  // 只看**真实赋值语句**，不看注释（注释里会引用旧字面量做反面教材，
  // 用纯字符串匹配会把注释也算进去 ⇒ 上一版就是这么误报的）
  const statusAssigns = [...html.matchAll(/\$\("genStatus"\)\.textContent = ([^;]*);/g)].map(m => m[1]);
  chk("generated 分支走 genDoneText（不再硬编码分母）",
      statusAssigns.some(s => /genDoneText\(/.test(s))
      && !statusAssigns.some(s => /条轨转谱成功/.test(s)),
      statusAssigns.length + " 处赋值");
})();

console.log("\n" + (fails === 0 ? "== ALL PASS (" + total + " checks) ==" : "== FAILED: " + fails + "/" + total + " =="));
process.exit(fails === 0 ? 0 : 1);
