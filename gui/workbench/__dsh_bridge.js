/* __dsh_bridge.js —— WebView2 宿主的 window.dsh 替身
 * ============================================================================
 * Adofai-Chart-Generator前端（app.js / api.js）假定自己跑在 Electron 里，它依赖：
 *   · window.dsh.base                → sidecar 的 HTTP 基址（api.js 拿它拼 fetch）
 *   · window.dsh.openFile/openAudio/openDir/reveal/info
 *                                    → 走 Electron IPC 的原生对话框 / 系统动作
 *   · window.dsh.layoutGet/layoutSet  → 存 <userData>/ui-layout.json
 *   · window.dsh.onMenu/onSidecarDown → 原生菜单 / sidecar 掉线事件
 *   · window.dsh.ready/reportError    → 握手
 *
 * 我们用 WebView2 承载它，所以这些能力全部**改用宿主消息通道重新实现**
 * （宿主侧 gui_main.py 的 dsh_call 分支）。
 *
 * 🔴 本文件必须在 app.js（type="module" ⇒ deferred）**之前**同步执行：
 *    api.js 在模块求值时就固化了 BASE = window.dsh.base。
 * ============================================================================
 */
(function () {
  var W = window.chrome && window.chrome.webview;
  var seq = 0;
  var pending = {};
  var menuCbs = [];
  var downCbs = [];
  var matCbs = [];

  function post(obj) {
    try { W.postMessage(JSON.stringify(obj)); } catch (e) { /* 宿主不在 */ }
  }

  function call(name, args) {
    return new Promise(function (resolve, reject) {
      if (!W) { reject(new Error("宿主消息通道不可用")); return; }
      var id = ++seq;
      pending[id] = { res: resolve, rej: reject };
      post({ type: "dsh_call", id: id, name: name, args: args || [] });
      // 防呆：宿主没回（比如对话框被关掉）时别把 Promise 永远挂着
      setTimeout(function () {
        if (pending[id]) {
          delete pending[id];
          reject(new Error("宿主未响应：" + name));
        }
      }, 10 * 60 * 1000);
    });
  }

  if (W) {
    W.addEventListener("message", function (ev) {
      var d = ev.data;
      if (typeof d === "string") { try { d = JSON.parse(d); } catch (e) { return; } }
      if (!d || !d.type) { return; }
      if (d.type === "dsh_reply") {
        var p = pending[d.id];
        if (!p) { return; }
        delete pending[d.id];
        if (d.ok) { p.res(d.result); } else { p.rej(new Error(d.error || "宿主报错")); }
        return;
      }
      if (d.type === "menu") {
        menuCbs.forEach(function (f) { try { f(d.name); } catch (e) { /* ignore */ } });
        return;
      }
      if (d.type === "sidecar_down") {
        downCbs.forEach(function (f) { try { f(d); } catch (e) { /* ignore */ } });
        return;
      }
      if (d.type === "winstate") { RZ_ZOOMED = !!d.zoomed; syncWinGlyph(!!d.zoomed); return; }
      // 宿主换了窗口材质（在设置页切档、或别的窗口调了 setbackdrop）⇒ 广播出来。
      // 页面靠它把材质过场的幕布退掉，不然两页观感对不上（一边有渐变一边是硬切）。
      if (d.type === "material") {
        matCbs.forEach(function (f) { try { f(d); } catch (e) { /* ignore */ } });
        return;
      }
    });
  }

  // 最大化/还原的字形跟着**真实窗口状态**走。
  // 以前只有"点了页面上那个按钮"才会变 —— 用 Win+↑、拖到屏幕边缘分屏、双击标题栏
  // 这些系统途径改变状态时字形就过期了（看着写"最大化"，点下去其实是还原）。
  // 现在宿主每次状态真变化都会广播 winstate，这里负责画对。
  function syncWinGlyph(zoomed) {
    var b = document.getElementById("win-max");
    if (!b) { return; }
    // 换 <use> 指过去的图标，不换字符：文字字形（▢ / ❐）在两个状态间宽度不等，
    // 按钮会跟着抖 2px，而且不同机器的字体里字形还长得不一样。
    var u = b.querySelector("use");
    if (u && window.ShellUI && ShellUI.has("restore")) {
      u.setAttribute("href", zoomed ? "#i-restore" : "#i-square");
    }
    b.title = zoomed ? "还原" : "最大化";
  }

  // ★ 外壳（WebView2 无边框窗口）**边缘缩放**：WebView2 铺满客户区 ⇒ 鼠标永远落在它的
  //   子窗口上 ⇒ 宿主收不到 WM_NCHITTEST（边缘既不变光标也拖不动）。这里由**页面自报**：
  //   mousedown 落在窗口最外 RZ 像素内 → post({type:'resize', edge}) → 宿主
  //   ReleaseCapture + WM_NCLBUTTONDOWN(HTxxx) 走系统原生缩放循环（与顶栏拖窗口的
  //   type:'drag' 是同一条路，机制已验证）。
  //   · 捕获阶段监听：抢在页面自己的 mousedown（拖窗口 / 选字 / 原生拖拽）之前判边。
  //   · 顺手给边缘换光标（用户得看得见"这儿能拖"）。
  //   · 最大化的窗口不给缩放（与 Windows 一致；宿主侧也会再挡一道）。
  var RZ = 6;
  var RZ_ZOOMED = false;
  var RZ_CURSOR = {
    left: "ew-resize", right: "ew-resize", top: "ns-resize", bottom: "ns-resize",
    topleft: "nwse-resize", topright: "nesw-resize",
    bottomleft: "nesw-resize", bottomright: "nwse-resize"
  };

  // 落点是不是压在滚动条上（右栏竖直滚动条就在窗口右缘，不能把它的几像素抢掉）
  function rzOverScrollbar(x, y) {
    var e = document.elementFromPoint(x, y);
    for (var i = 0; i < 6 && e && e.nodeType === 1; i++, e = e.parentElement) {
      var r = e.getBoundingClientRect();
      if (e.scrollHeight > e.clientHeight + 2 && x >= r.left + e.clientLeft + e.clientWidth) { return true; }
      if (e.scrollWidth > e.clientWidth + 2 && y >= r.top + e.clientTop + e.clientHeight) { return true; }
    }
    return false;
  }

  function rzEdge(e) {
    if (!e || RZ_ZOOMED) { return ""; }
    var w = window.innerWidth, h = window.innerHeight;
    var L = e.clientX <= RZ, R = e.clientX >= w - RZ;
    var T = e.clientY <= RZ, B = e.clientY >= h - RZ;
    if (!(L || R || T || B)) { return ""; }
    if ((L || R) && rzOverScrollbar(e.clientX, e.clientY)) { return ""; }
    if (L && T) { return "topleft"; }
    if (R && T) { return "topright"; }
    if (L && B) { return "bottomleft"; }
    if (R && B) { return "bottomright"; }
    if (L) { return "left"; }
    if (R) { return "right"; }
    if (T) { return "top"; }
    return "bottom";
  }

  function initResizeGrips() {
    document.addEventListener("mousedown", function (e) {
      if (e.button !== 0) { return; }
      var edge = rzEdge(e);
      if (!edge) { return; }
      e.preventDefault();
      e.stopPropagation();
      post({ type: "resize", edge: edge });
    }, true);
    document.addEventListener("mousemove", function (e) {
      var edge = rzEdge(e);
      document.documentElement.style.cursor = edge ? RZ_CURSOR[edge] : "";
    }, true);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initResizeGrips);
  } else {
    initResizeGrips();
  }

  // ★ 外壳（WebView2 无边框窗口）给工作台补两样它自己没有的东西：
  //   ① 「← 导入」按钮 —— 回原生导入页；
  //   ② 窗口控制 + 拖拽区 —— Adofai-Chart-Generator页面本身没有标题栏，不补就没法拖窗口/关窗口。
  //   app.js 不重建 #top（只在其上绑已有按钮），所以这里插进去不会被覆盖。
  document.addEventListener("DOMContentLoaded", function () {
    var top = document.getElementById("top");
    if (!top) { return; }

    if (!document.getElementById("btn-back-import")) {
      var b = document.createElement("button");
      b.id = "btn-back-import";
      b.innerHTML = (window.ShellUI && ShellUI.has("back"))
        ? ShellUI.icon("back", "ico-gap") + "导入"
        : "← 导入";
      b.className = "ghost";
      b.style.marginRight = "6px";
      b.title = "返回选歌 / 精度 / 采音导入页";
      b.onclick = function () {
        if (window.dsh && window.dsh.backToImport) window.dsh.backToImport();
      };
      top.insertBefore(b, top.firstChild);
    }

    if (!document.getElementById("win-ctl")) {
      var box = document.createElement("div");
      box.id = "win-ctl";
      box.style.cssText = "display:flex;align-items:center;gap:2px;margin-left:4px;flex:0 0 auto;";
      [["win-min", "minus", "最小化", "winminimize"],
       ["win-max", "square", "最大化", "winmaximize"],
       ["win-close", "close", "关闭", "close"]].forEach(function (d) {
        var c = document.createElement("button");
        c.id = d[0];
        // 走 shell-ui 的图标而不是字符 —— 和页面里其它按钮同一套笔法。
        // 万一 shell-ui.js 没加载就退回原来的字符，按钮不会变成空白（宁可丑，不可丢功能）。
        c.innerHTML = (window.ShellUI && ShellUI.has(d[1]))
          ? ShellUI.icon(d[1])
          : (d[0] === "win-min" ? "—" : d[0] === "win-max" ? "▢" : "✕");
        c.title = d[2];
        c.style.cssText = "width:32px;height:24px;padding:0;border:none;border-radius:6px;"
                        + "background:transparent;color:#9A9AA2;cursor:pointer;font-size:13px;"
                        + "line-height:1;font-family:inherit;";
        c.onmouseenter = function () {
          c.style.background = (d[0] === "win-close") ? "#F85149" : "rgba(255,255,255,0.10)";
          c.style.color = "#fff";
        };
        c.onmouseleave = function () {
          c.style.background = "transparent";
          c.style.color = "#9A9AA2";
        };
        c.onclick = function () { post({ type: d[3] }); };
        box.appendChild(c);
      });
      top.appendChild(box);
      syncWinGlyph(RZ_ZOOMED);   // 用最近一次已知状态先画对，别等宿主再广播

      // 顶栏空白处按下 = 拖窗口（grow 占位块 / 控件之间的空隙都能抓）
      function onShell(e) {
        return !!(e.target.closest && e.target.closest(
          "button, input, select, textarea, a, label, #win-ctl, #btn-layout, #btn-reset-layout"));
      }
      // dbg 字段是给宿主日志用的：一眼看出这一下落在哪个元素上（"点在按钮上却触发标题栏"
      // 这类问题没它就只能靠猜）。宿主收到会打成 `← 页面: drag (target=... btn=...)`。
      function who(e) {
        var t = e.target;
        return "target=" + ((t && (t.id || t.className || t.tagName)) || "?")
             + " btn=" + !!(t && t.closest && t.closest("#win-ctl, button"));
      }
      top.addEventListener("mousedown", function (e) {
        if (e.button !== 0 || onShell(e)) { return; }
        post({ type: "drag", dbg: who(e) });
      });
      top.addEventListener("dblclick", function (e) {
        if (onShell(e)) { return; }
        post({ type: "winmaximize", dbg: who(e) });
      });
      // 右键顶栏 = **系统原生菜单**（还原/移动/大小/最小化/最大化/关闭）。
      // 无边框窗口下 Windows 不会自己弹这个菜单 —— 它不知道顶栏就是标题栏。
      // 🔴 只接管控件区（no-drag）：顶栏空白属于拖动区，系统会自己弹它那份菜单，
      //    这里再弹一次就会同时出现两个。控件区系统不接手，才需要我们补。
      top.addEventListener("contextmenu", function (e) {
        if (!onShell(e)) { return; }
        e.preventDefault();
        post({ type: "sysmenu", dbg: who(e) });
      });
    }
  });

  window.dsh = {
    port: 0,
    // ★ 同源网关：静态前端与 /api 在**同一个端口**上，所以 origin 就是 API 基址，
    //   天然零 CORS（sidecar 自己不发 Access-Control-Allow-Origin）。
    base: location.origin,
    // 暴露底层消息通道，供页面自由发消息给宿主（自检/状态快照/错误上报都用它）
    post: post,

    openFile: function () { return call("openFile"); },
    openAudio: function () { return call("openAudio"); },
    openDir: function (o) { return call("openDir", [o]); },
    reveal: function (p) { return call("reveal", [p]); },
    info: function () { return call("info"); },
    layoutGet: function () { return call("layoutGet"); },
    layoutSet: function (t) { return call("layoutSet", [t]); },
    backToImport: function () { return call("backToImport"); },

    ready: function (i) { post({ type: "dsh_ready", info: i || {} }); },
    reportError: function (m) { post({ type: "dsh_error", msg: String(m) }); },

    onMenu: function (cb) { if (typeof cb === "function") { menuCbs.push(cb); } },
    onSidecarDown: function (cb) { if (typeof cb === "function") { downCbs.push(cb); } },
    // 宿主换窗口材质时回调（shell-ui.js 用它驱动材质过场的退场）
    onMaterial: function (cb) { if (typeof cb === "function") { matCbs.push(cb); } },
  };
})();
