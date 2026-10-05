/* shell-ui.js —— ADOFAI Studio 的 Win11 / Fluent 观感层（图标 + 材质过场）
 * ============================================================================
 * 放在 gui/ 根下，由 gui/index.html 与 gui/workbench/index.html **共同**加载，
 * 必须在页面自己的业务脚本之前跑（app.js 会直接调 window.ShellUI）。
 *
 * 它干三件事：
 *   ① 往文档里塞一份 SVG 图标 sprite（Fluent 描边风格，20×20 视窗、线宽 1.5）；
 *   ② 自动把 [data-ico="名字"] 的元素填上图标，并持续盯住后来才生成的节点；
 *   ③ 提供材质切换的过场动画（MatFade）—— 见 shell-ui.css 里 #shell-mat-veil 的说明。
 *
 * ★ 为什么要 sprite 而不是给每处贴一段 SVG：图标会出现在几十个地方（静态 HTML、
 *   app.js 动态生成的按钮、折叠箭头…），散着写等于同一张图存几十份，改一处漏十处。
 *   sprite 只有一份定义，用 <use href="#i-xxx"> 引用。
 *
 * ★ 为什么基本不用 Segoe Fluent Icons 字体（Win11 自带的图标字体）：
 *   码点在不同 Windows 版本之间有增删，Win10 上更是整个字体都不在，
 *   表现是"图标变豆腐块"——而且没法用页面自己的配色。SVG 没有这些问题。
 *   ⚠ 唯一例外是 settings 那只齿轮：主人拿拯救者工具箱的图一比，要求"一模一样"。
 *     把那几个候选挨个渲染出来做像素比对（tools/iconsheet.js + 比对脚本）后，
 *     确实是系统字形最像 → 那就用字形，并加一道**字体探测**，字形不在时自动退回
 *     SVG（官方 path）。判据在下面 hasNativeIconFont()。
 * ============================================================================
 */
(function () {
  'use strict';

  var VIEWBOX = '0 0 20 20';

  /* 微软官方 Fluent 图标 settings_24_regular 的 path（逐字节取自
     fluentui-system-icons 仓库，不是手抄 —— 3KB 的 path 手抄必错）。
     24 视窗缩到 20 视窗用 scale(20/24)=0.8333，中心仍在 (10,10)。 */
  var SETTINGS_PATH24 = 'M12.0122 2.25C12.7462 2.25846 13.4773 2.34326 14.1937 2.50304C14.5064 2.57279 14.7403 2.83351 14.7758 3.15196L14.946 4.67881C15.0231 5.37986 15.615 5.91084 16.3206 5.91158C16.5103 5.91188 16.6979 5.87238 16.8732 5.79483L18.2738 5.17956C18.5651 5.05159 18.9055 5.12136 19.1229 5.35362C20.1351 6.43464 20.8889 7.73115 21.3277 9.14558C21.4223 9.45058 21.3134 9.78203 21.0564 9.9715L19.8149 10.8866C19.4607 11.1468 19.2516 11.56 19.2516 11.9995C19.2516 12.4389 19.4607 12.8521 19.8157 13.1129L21.0582 14.0283C21.3153 14.2177 21.4243 14.5492 21.3297 14.8543C20.8911 16.2685 20.1377 17.5649 19.1261 18.6461C18.9089 18.8783 18.5688 18.9483 18.2775 18.8206L16.8712 18.2045C16.4688 18.0284 16.0068 18.0542 15.6265 18.274C15.2463 18.4937 14.9933 18.8812 14.945 19.3177L14.7759 20.8444C14.741 21.1592 14.5122 21.4182 14.204 21.4915C12.7556 21.8361 11.2465 21.8361 9.79803 21.4915C9.48991 21.4182 9.26105 21.1592 9.22618 20.8444L9.05736 19.32C9.00777 18.8843 8.75434 18.498 8.37442 18.279C7.99451 18.06 7.5332 18.0343 7.1322 18.2094L5.72557 18.8256C5.43422 18.9533 5.09403 18.8833 4.87678 18.6509C3.86462 17.5685 3.11119 16.2705 2.6732 14.8548C2.57886 14.5499 2.68786 14.2186 2.94485 14.0293L4.18818 13.1133C4.54232 12.8531 4.75147 12.4399 4.75147 12.0005C4.75147 11.561 4.54232 11.1478 4.18771 10.8873L2.94516 9.97285C2.6878 9.78345 2.5787 9.45178 2.67337 9.14658C3.11212 7.73215 3.86594 6.43564 4.87813 5.35462C5.09559 5.12236 5.43594 5.05259 5.72724 5.18056L7.12762 5.79572C7.53056 5.97256 7.9938 5.94585 8.37577 5.72269C8.75609 5.50209 9.00929 5.11422 9.05817 4.67764L9.22824 3.15196C9.26376 2.83335 9.49786 2.57254 9.8108 2.50294C10.5281 2.34342 11.26 2.25865 12.0122 2.25ZM12.0124 3.7499C11.5583 3.75524 11.1056 3.79443 10.6578 3.86702L10.5489 4.84418C10.4471 5.75368 9.92003 6.56102 9.13042 7.01903C8.33597 7.48317 7.36736 7.53903 6.52458 7.16917L5.62629 6.77456C5.05436 7.46873 4.59914 8.25135 4.27852 9.09168L5.07632 9.67879C5.81513 10.2216 6.25147 11.0837 6.25147 12.0005C6.25147 12.9172 5.81513 13.7793 5.0771 14.3215L4.27805 14.9102C4.59839 15.752 5.05368 16.5361 5.626 17.2316L6.53113 16.8351C7.36923 16.4692 8.33124 16.5227 9.12353 16.9794C9.91581 17.4361 10.4443 18.2417 10.548 19.1526L10.657 20.1365C11.5466 20.2878 12.4555 20.2878 13.3451 20.1365L13.4541 19.1527C13.5549 18.2421 14.0828 17.4337 14.876 16.9753C15.6692 16.5168 16.6332 16.463 17.4728 16.8305L18.3772 17.2267C18.949 16.5323 19.4041 15.7495 19.7247 14.909L18.9267 14.3211C18.1879 13.7783 17.7516 12.9162 17.7516 11.9995C17.7516 11.0827 18.1879 10.2206 18.9258 9.67847L19.7227 9.09109C19.4021 8.25061 18.9468 7.46784 18.3748 6.77356L17.4783 7.16737C17.113 7.32901 16.7178 7.4122 16.3187 7.41158C14.849 7.41004 13.6155 6.30355 13.4551 4.84383L13.3462 3.8667C12.9007 3.7942 12.4526 3.75512 12.0124 3.7499ZM11.9997 8.24995C14.0708 8.24995 15.7497 9.92888 15.7497 12C15.7497 14.071 14.0708 15.75 11.9997 15.75C9.92863 15.75 8.2497 14.071 8.2497 12C8.2497 9.92888 9.92863 8.24995 11.9997 8.24995ZM11.9997 9.74995C10.7571 9.74995 9.7497 10.7573 9.7497 12C9.7497 13.2426 10.7571 14.25 11.9997 14.25C13.2423 14.25 14.2497 13.2426 14.2497 12C14.2497 10.7573 13.2423 9.74995 11.9997 9.74995Z';

  /* ---------------------------------------------------------------------------
   * 图标本体。每个都是"内层 SVG 内容"，外层 <svg class="ico"> 由 icon() 包。
   * 笔法统一：只画描边（stroke），不写死颜色 —— 颜色靠 CSS 的 currentColor 继承，
   * 所以同一份图标在 hover / 选中 / 警告态下自动跟着文字变色。
   * 需要实心的极少数（状态圆点）用 style 内联写死 fill，因为 <use> 的影子树
   * 里没法用页面 CSS 的选择器点名。
   * ------------------------------------------------------------------------- */
  /* E713 = Segoe Fluent Icons / Segoe MDL2 Assets 里的「设置」齿轮字形（codepoint） */
  var SETTINGS_GLYPH = '\uE713';

  var ICONS = {
    /* 双联音符 —— 生成谱面 */
    music: '<path d="M8 14.6V5.9L16.1 4v8.6"/>' +
           '<circle cx="5.9" cy="14.6" r="2.1"/><circle cx="14.1" cy="12.6" r="2.1"/>',

    /* 版面（左窄右宽）—— 工作台 */
    panel: '<rect x="2.9" y="4.4" width="14.2" height="11.2" rx="1.6"/>' +
           '<path d="M8.1 4.4v11.2"/>',

    /* 三层叠片 —— 分离试听 */
    layers: '<path d="M10 2.9 17.1 6.7 10 10.5 2.9 6.7z"/>' +
            '<path d="M3.3 10.3 10 13.9l6.7-3.6"/><path d="M3.3 13.7 10 17.3l6.7-3.6"/>',

    /* 时钟 —— 历史记录 */
    clock: '<circle cx="10" cy="10" r="6.6"/><path d="M10 6.4V10l2.6 1.5"/>',

    /* 闪电 —— 训练采点模型 */
    flash: '<path d="M11.4 2.8 5.2 10.9h4L8.6 17.2l6.2-8.1h-4z"/>',

    /* 设置（齿轮）——★ 2026-09-21 **不自绘，用官方的**：
       把候选一只只渲染出来跟主人的参考图做像素比对（见 tools/iconsheet.js），
       结论是系统字体 'Segoe Fluent Icons' 的 E713 最像（MAE 0.103），官方
       settings_24_regular 次之（0.119）；我手画的两版是 0.28 / 0.34 —— 手画确实不行。
       所以 icon('settings') 优先吐字体字形（那才是 Win11 应用画出来的那只齿轮），
       字体不在（Win10）时退回本组的官方 path。这里存的就是兜底那份。 */
    settings: '<g transform="scale(0.8333)"><path d="' + SETTINGS_PATH24 +
              '" style="fill:currentColor;stroke:none"/></g>',

    /* 信息圈 —— 关于 */
    info: '<circle cx="10" cy="10" r="6.6"/><path d="M10 9.3v3.9"/><path d="M10 6.6v.01"/>',

    /* 播放 / 暂停 */
    play: '<path d="M7.1 5.1 15 10 7.1 14.9z"/>',
    pause: '<path d="M7.8 4.9v10.2M12.2 4.9v10.2"/>',

    /* 展开箭头（默认朝下；收起态靠 CSS 转 -90° 变朝右，见 shell-ui.css） */
    chevron: '<path d="M5.4 7.9 10 12.5 14.6 7.9"/>',

    /* 关闭 / 删除 */
    close: '<path d="M5.4 5.4 14.6 14.6M14.6 5.4 5.4 14.6"/>',

    /* 刷新 / 重新生成 / 重试 */
    refresh: '<path d="M16.6 10a6.6 6.6 0 1 1-1.9-4.6"/><path d="M14.7 1.6v3.8h3.8"/>',

    /* 警告三角 */
    warning: '<path d="M10 3.5 17.4 16.4H2.6z"/><path d="M10 8v3.5"/><path d="M10 14v.01"/>',

    /* 四角星 —— 生成 / 新建 */
    sparkle: '<path d="M10 2.6 11.75 7.35 16.5 9.1 11.75 10.85 10 15.6 8.25 10.85 3.5 9.1 8.25 7.35z"/>',

    /* 文件夹 */
    folder: '<path d="M2.9 6.1a1.2 1.2 0 0 1 1.2-1.2h3.1l1.6 1.9h6.1a1.2 1.2 0 0 1 1.2 1.2v6.9a1.2 1.2 0 0 1-1.2 1.2H4.1a1.2 1.2 0 0 1-1.2-1.2z"/>',

    /* 导出（上箭头 + 托盘） */
    export: '<path d="M10 13.2V3.6"/><path d="M6.4 7.2 10 3.6l3.6 3.6"/>' +
            '<path d="M3.9 12.4v3.1a1.2 1.2 0 0 0 1.2 1.2h9.8a1.2 1.2 0 0 0 1.2-1.2v-3.1"/>',

    /* 五角星 —— 谱面预览 */
    star: '<path d="M10 2.9 11.7 7.65 16.75 7.81 12.76 10.9 14.17 15.74 10 12.9 5.83 15.74 7.24 10.9 3.25 7.81 8.3 7.65z"/>',

    /* 网格 —— 钢琴卷帘 */
    grid: '<rect x="2.9" y="3.9" width="14.2" height="12.2" rx="1.4"/>' +
          '<path d="M2.9 8h14.2M2.9 12.1h14.2M7.65 8v8.1M12.35 8v8.1"/>',

    /* 曲线 + 端点 —— 谱面路径 */
    path: '<path d="M4.4 15.6C4.4 8.6 8.6 5.2 15.6 5.2"/>' +
          '<circle cx="4.4" cy="15.6" r="1.5"/><circle cx="15.6" cy="5.2" r="1.5"/>',

    /* 4 条下落轨道 —— 4K 下落式 */
    falling: '<rect x="3.2" y="3.2" width="13.6" height="13.6" rx="1.6"/>' +
             '<path d="M6.6 3.2v13.6M10 3.2v13.6M13.4 3.2v13.6"/>' +
             '<rect x="3.9" y="12.2" width="2" height="2.6" rx=".5" style="fill:currentColor;stroke:none"/>',

    /* 左箭头 —— 返回 */
    back: '<path d="M16.2 10H4.2"/><path d="M9.4 5.2 4.6 10l4.8 4.8"/>',

    /* 实心圆点 —— 连接 / 健康状态 */
    dot: '<circle cx="10" cy="10" r="3.6" style="fill:currentColor;stroke:none"/>',

    /* 对勾 */
    check: '<path d="M4.6 10.4 8.2 14 15.4 6.4"/>',

    /* 加号 */
    plus: '<path d="M10 4.4v11.2M4.4 10h11.2"/>',

    /* 垃圾桶 —— 清理 */
    trash: '<path d="M3.9 5.6h12.2"/>' +
           '<path d="M8.2 5.6V4.4a1.1 1.1 0 0 1 1.1-1.1h1.4a1.1 1.1 0 0 1 1.1 1.1v1.2"/>' +
           '<path d="M5.6 5.6 6.4 16a1.1 1.1 0 0 0 1.1 1h5a1.1 1.1 0 0 0 1.1-1l.8-10.4"/>',

    /* 链环 —— 桥接 */
    link: '<path d="M8.4 11.6a3.4 3.4 0 0 0 4.9 0l2.6-2.6a3.5 3.5 0 0 0-4.9-4.9l-1.3 1.3"/>' +
          '<path d="M11.6 8.4a3.4 3.4 0 0 0-4.9 0l-2.6 2.6a3.5 3.5 0 0 0 4.9 4.9l1.3-1.3"/>',

    /* 窗口控制三件套 */
    minus: '<path d="M4.6 10h10.8"/>',
    square: '<rect x="4.6" y="4.6" width="10.8" height="10.8" rx="1.6"/>',
    restore: '<rect x="3.4" y="7.2" width="9.4" height="9.4" rx="1.6"/>' +
             '<path d="M6.6 7.2V5.6a1.6 1.6 0 0 1 1.6-1.6h7.2a1.6 1.6 0 0 1 1.6 1.6v7.2a1.6 1.6 0 0 1-1.6 1.6h-1.6"/>',

    /* 三横杠 —— 侧栏折叠开关（任务管理器式）。纯描边，沿用 .ico 的 stroke 画法。 */
    menu: '<path d="M3.5 6h13M3.5 10h13M3.5 14h13"/>'
  };

  // ------------------------------------------------------------------ sprite

  var SPRITE_ID = 'shell-icon-sprite';

  function buildSprite() {
    if (document.getElementById(SPRITE_ID)) { return; }
    var host = document.body || document.documentElement;
    if (!host) { return; }
    var parts = [];
    for (var k in ICONS) {
      if (Object.prototype.hasOwnProperty.call(ICONS, k)) {
        parts.push('<g id="i-' + k + '">' + ICONS[k] + '</g>');
      }
    }
    var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.id = SPRITE_ID;
    svg.setAttribute('aria-hidden', 'true');
    // 用 display:none 会让 <use> 在某些老引擎下拿不到几何，改用"零尺寸 + 不可见"
    svg.setAttribute('style', 'position:absolute;width:0;height:0;overflow:hidden;pointer-events:none');
    svg.innerHTML = '<defs>' + parts.join('') + '</defs>';
    host.appendChild(svg);
  }

  /** 生成一个图标元素（HTML 字符串）。name 不存在时返回空串，绝不吐"半个 svg"。 */
  function icon(name, extraClass) {
    if (!Object.prototype.hasOwnProperty.call(ICONS, name)) { return ''; }
    var cls = 'ico' + (extraClass ? ' ' + extraClass : '');
    // 「设置」优先用系统字形 —— 有字形时它才是 Win11 应用画出来的那只齿轮（见 ICONS.settings）
    if (name === 'settings' && hasNativeIconFont()) {
      return '<span class="' + cls + ' ico-glyph" aria-hidden="true">' + SETTINGS_GLYPH + '</span>';
    }
    return '<svg class="' + cls + '" viewBox="' + VIEWBOX + '" aria-hidden="true">' +
           '<use href="#i-' + name + '"></use></svg>';
  }

  function has(name) {
    return Object.prototype.hasOwnProperty.call(ICONS, name);
  }

  /* 系统图标字体在不在？—— **必须靠画出来看形状，不能靠量宽度**。
   *
   * 踩过的坑（都栽在"量宽度"这条路上，别再回去）：
   *   ① 同一码点换族名比 advance：E713 在真字体 / 假字体 / 回退字体下 advance 都是 20
   *      （= font-size），**零区分力**，判据恒为 false ⇒ 字形分支永远不激活（等于死代码）；
   *   ② 退一步拿拉丁字母做对照（'A'）：Segoe Fluent Icons 里没有拉丁字母，
   *      量 'A' 量到的是**回退字体**的读数（真字体 12.16、不存在的族名也是 12.16），
   *      照样零区分力。
   *
   * ⚠ 而且这个分支**必须由"字体真在"来把关**，因为字体不在时画出来的不是空白，
   *   而是一只**带叉的空心方框**（.notdef，墨迹占半个字身，比画错齿轮难看得多）。
   *   实测（tools/iconfontcheck.js 落盘的两张图，用 --ascii 看过）：
   *     · 缺字形 ⇒ ▭ 里一个 ✕，**方框中心被叉填满**
   *     · 真齿轮 ⇒ 中心是一只圆孔，**中心是空的**
   *   这一条"中心空 / 中心实"是稳的，且不依赖字号——所以判据就建在它上面。
   *
   * ⚠ 不要再用"环命中率 > 70%"那种阈值：校准过（同上脚本），
   *   真齿轮在 r=0.28S 上只有 58%、在 r=0.44S 上只有 3%，
   *   任何固定阈值不是把真齿轮挡在外面、就是把方框放进来。 */
  var iconFontOK = null;
  function hasNativeIconFont() {
    if (iconFontOK !== null) { return iconFontOK; }
    iconFontOK = false;
    try {
      var S = 48, cv = document.createElement('canvas');
      cv.width = S; cv.height = S;
      var c = cv.getContext('2d');
      if (!c || !c.getImageData) { return iconFontOK; }
      c.font = S + "px 'Segoe Fluent Icons','Segoe MDL2 Assets'";
      c.textAlign = 'center';
      c.textBaseline = 'middle';
      c.fillStyle = '#fff';
      c.fillText(SETTINGS_GLYPH, S / 2, S / 2);
      iconFontOK = gearLike(c.getImageData(0, 0, S, S).data, S);
    } catch (e) { iconFontOK = false; }
    // 给探针留个观测口：importprobe 靠它区分"走的是字形还是 SVG 兜底"
    try { window.__dshIconFont = iconFontOK; } catch (e2) { /* 无所谓 */ }
    return iconFontOK;
  }

  /*#gearLike-start ------------------------------------------------------------
   * gearLike(data, S) —— 纯几何判据："这一团墨是不是一只齿轮（而不是豆腐框）"。
   *
   * 全程只用**墨迹自己算出来的包围盒**做尺度基准，不写死像素半径 ——
   * 换字号、换字体度量都不会失效（写死半径那版就是这么翻车的）。
   *
   * 三条同时成立才算齿轮（每条的实测取值见 tools/iconfontcheck.js 的输出）：
   *   ① 有墨，且墨占包围盒的比例在 12%~70% 之间
   *        实测：齿轮 41% / 豆腐框 50% / 空白 0% / 实心块 100% —— 只挡两头；
   *   ② 包围盒近似方形（短边/长边 ≥ 0.75）：齿轮 95% / 豆腐框 83% —— 只挡怪东西；
   *   ③ ★ 中心是空的（包围盒正中 20% 方块里墨 ≤ 35%）
   *        实测：齿轮 19%（光栅）与 9%（系统字形） / 豆腐框 88%（✕ 正好穿过中心）；
   *   ④ ★ 包围盒四个角是空的（每角 15% 方块里墨 ≤ 30%）
   *        齿轮是圆的，四角天然没墨；豆腐框是矩形描边，四角必然有墨。
   *      ③④ 互为冗余，任何一条单独都能把豆腐框挡下。
   *
   * 取证与校准：tools/iconfontcheck.js —— 它把这张判据**从本文件源码里抠出来**
   * （按下面这对 marker），分别喂给"官方齿轮 path 的栅格""缺字形的豆腐框"
   * （还顺带跑一遍本机真实的 E713），要求前者 true、豆腐框 false 才算通过。
   * 改了这里就去跑那个脚本，别只凭感觉。 */
  function gearLike(d, S) {
    var x, y, minX = S, maxX = -1, minY = S, maxY = -1, ink = 0;
    for (y = 0; y < S; y++) {
      for (x = 0; x < S; x++) {
        if (d[(y * S + x) * 4 + 3] > 40) {
          ink++;
          if (x < minX) { minX = x; }
          if (x > maxX) { maxX = x; }
          if (y < minY) { minY = y; }
          if (y > maxY) { maxY = y; }
        }
      }
    }
    if (maxX < 0 || maxY < 0) { return false; }               // ① 一点墨都没有
    var bw = maxX - minX + 1, bh = maxY - minY + 1;
    var cover = ink / (bw * bh);
    if (cover < 0.12 || cover > 0.70) { return false; }        // ①
    if (Math.min(bw, bh) / Math.max(bw, bh) < 0.75) { return false; }  // ②

    /* 取一块方块里的着墨比例（坐标已按包围盒夹取） */
    function blockRatio(bx, by, bwd, bht) {
      var inked = 0, tot = 0;
      for (var yy = by; yy < by + bht; yy++) {
        for (var xx = bx; xx < bx + bwd; xx++) {
          if (xx < 0 || yy < 0 || xx >= S || yy >= S) { continue; }
          tot++;
          if (d[(yy * S + xx) * 4 + 3] > 40) { inked++; }
        }
      }
      return tot ? inked / tot : 1;      // 取不到样就当"有墨"，宁可退回 SVG 兜底
    }

    // ③ 中心
    var cw = Math.max(2, Math.round(bw * 0.20)), ch = Math.max(2, Math.round(bh * 0.20));
    var center = blockRatio(minX + Math.round((bw - cw) / 2), minY + Math.round((bh - ch) / 2), cw, ch);
    if (center > 0.35) { return false; }

    // ④ 四角
    var kw = Math.max(1, Math.round(bw * 0.15)), kh = Math.max(1, Math.round(bh * 0.15));
    var corners = [
      blockRatio(minX, minY, kw, kh),
      blockRatio(maxX - kw + 1, minY, kw, kh),
      blockRatio(minX, maxY - kh + 1, kw, kh),
      blockRatio(maxX - kw + 1, maxY - kh + 1, kw, kh)
    ];
    for (var i = 0; i < 4; i++) { if (corners[i] > 0.30) { return false; } }

    return true;
  }
  /*#gearLike-end ------------------------------------------------------------ */

  // --------------------------------------------------------------- 声明式填充

  /**
   * 把 [data-ico] 的元素补上图标（插在最前面）。
   * 已经补过的打 data-ico-done 标记，不会重复插 —— 因为 MutationObserver 会
   * 反复路过同一个节点，没有这个标记就会越插越多。
   */
  function fillOne(el) {
    if (!el || el.nodeType !== 1) { return; }
    if (el.dataset.icoDone) { return; }
    var name = el.dataset.ico;
    if (!name) { return; }
    var html = icon(name);
    if (!html) {
      // 名字写错时**留个证据**：不然只是"图标没出来"，看不出是拼错了还是 sprite 没建
      console.warn('[shell-ui] 没有这个图标：' + name);
      el.dataset.icoDone = 'missing';
      return;
    }
    el.insertAdjacentHTML('afterbegin', html);
    el.dataset.icoDone = '1';
  }

  function scan(root) {
    if (!root || root.nodeType !== 1) { return; }
    fillOne(root);
    var list = root.querySelectorAll('[data-ico]');
    for (var i = 0; i < list.length; i++) { fillOne(list[i]); }
  }

  function watch() {
    if (!window.MutationObserver || !document.body) { return; }
    var queued = false;
    var obs = new MutationObserver(function (records) {
      // 图标是"加在节点开头"的，本身也会引发 mutation ⇒ 必须防重入，
      // 否则 observer 追着自己的写入跑，卡死页面。
      if (queued) { return; }
      queued = true;
      requestAnimationFrame(function () {
        queued = false;
        for (var i = 0; i < records.length; i++) {
          var added = records[i].addedNodes;
          for (var j = 0; j < added.length; j++) {
            var n = added[j];
            if (n.nodeType === 1 && n.id !== SPRITE_ID) { scan(n); }
          }
        }
      });
    });
    obs.observe(document.body, { childList: true, subtree: true });
  }

  /**
   * 把一个元素的标签换成「图标 + 文本」。
   * 给 app.js 这类**动态生成**按钮的地方用 —— 它们没法靠 [data-ico] 声明
   * （属性是 JS 拼字符串时才知道的），也没法用 textContent（那会把图标冲掉）。
   */
  function label(el, name, text) {
    if (!el) { return; }
    var html = name ? icon(name) : '';
    if (html) { html = html.replace('class="ico"', 'class="ico ico-gap"'); }
    el.innerHTML = html + escapeHtml(text == null ? '' : String(text));
  }

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  // ------------------------------------------------------------- 材质过场动画

  /* 时序（三拍）：
   *   ① 点下去 → 幕布**快**暗（100ms，加速曲线）—— 这一下把"材质瞬间跳变"盖住；
   *   ② 90ms 后执行真正的切换动作（宿主收到消息、DwmSetWindowAttribute 同步生效）；
   *   ③ 宿主回话（或兜底超时）→ 幕布**慢**亮（340ms，减速曲线）。
   * 用户看到的就是"暗一下，亮起来已经是新材质"，而不是底色的硬切。           */
  var VEIL_ID = 'shell-mat-veil';
  var SWITCH_DELAY = 90;    // 暗到峰值再切，跳变才藏在黑里
  var WATCHDOG = 1600;      // 宿主万一不回话，别让幕布永远挂着（页面就点不动了）

  var veilEl = null;
  var tSwitch = null;
  var tWatch = null;

  function veil() {
    if (veilEl && veilEl.isConnected) { return veilEl; }
    veilEl = document.getElementById(VEIL_ID);
    if (!veilEl) {
      // body 还没出来（脚本在 <head> 跑）就先不建，等 DOMContentLoaded 那轮
      if (!document.body) { return null; }
      veilEl = document.createElement('div');
      veilEl.id = VEIL_ID;
      document.body.appendChild(veilEl);
    }
    return veilEl;
  }

  /**
   * 跑一次材质切换过场。
   * @param {Function} switchFn 真正执行切换的动作（发光消息 / 调接口都行）
   *                             它在幕布最暗的那一刻被调用。
   */
  function run(switchFn) {
    var v = veil();
    if (v) { v.classList.add('on'); }
    clearTimeout(tSwitch);
    clearTimeout(tWatch);

    tSwitch = setTimeout(function () {
      try { if (typeof switchFn === 'function') { switchFn(); } }
      catch (e) { console.error('[shell-ui] 材质切换动作抛错：', e); }
    }, v ? SWITCH_DELAY : 0);

    tWatch = setTimeout(settle, WATCHDOG);
  }

  /** 切换已完成 ⇒ 开始退场（稍等一拍，让宿主的重绘先落地）。 */
  function settle() {
    clearTimeout(tWatch);
    tWatch = null;
    var v = veil();
    if (!v) { return; }
    setTimeout(function () { v.classList.remove('on'); }, 60);
  }

  // ------------------------------------------------ 侧栏选中指示条（#nav-ind）

  /* 一根会在各项之间**滑动**的条。
   * 为什么必须做成独立元素、不能用 .nav::before：
   *   伪元素挂在各自项上，"切换"只能表达成"旧的消失、新的出现"（各自动 opacity），
   *   做不出"同一个东西从这项连续走到那项"。主人 2026-09-21 21:37 说
   *   "小竖条的切换到动画怎么没了" —— 上一版正是 ::before + 150ms 淡入淡出，
   *   看着就是"啪"地跳一下，位移感为零。
   * 做法：一个元素 + `transform:translateY` 过渡 ⇒ 位移交给合成器，
   *   并且**会跟着系统"动画效果"开关一起被关掉**（CSS 过渡的天然好处）。
   *
   * 两个坑：
   *   ① 首帧不能动画 —— 页面刚开时它还在 (0,0)，不按住过渡就会从侧栏顶部滑下来。
   *      做法：先 `transition:none` 定位，强制回流，下一帧再放开。
   *   ② 切页不只"点侧栏"一条路（还有按钮跳转、恢复上次页、探针模拟点击）⇒
   *      用 MutationObserver 盯 `.nav` 的 class 变化，而不是在点击处理里调一次，
   *      这样多一条切页路径也不会漏（也不会出现"某条路切了、条没跟着走"）。
   */
  var navIndEl = null;
  var navBooted = false;
  var navObs = null;
  /* ===== 指示条的动作参数（2026-09-22 主人裁定：不是"划过去"）=====
     他原话："左侧那个条条切换的时候不是划过去，你知道 Windows11 里面是
     缩小一截再直接弹开到另一个选中界面的前面吗"。
     ⇒ 两段，**中间没有任何位移**：
          ① 在旧位置压扁到 SQUASH（SHRINK ms，accelerate 曲线：离场用加速）
          ② 跳到新位置并弹回 1（POP ms，decelerate 曲线：进场用减速）
     ⚠ 判"是弹还是滑"要采样 **rect 高度**（滑动时恒为 22，弹开时中间掉到 6 上下），
       只看 top 两种都会变 —— 这也是探针里那条断言的写法。 */
  var IND_SHRINK = 110;
  var IND_POP = 240;
  var IND_SQUASH = 0.28;
  var IND_ACC = 'cubic-bezier(.7,0,1,.5)';    /* Fluent accelerate */
  var IND_DEC = 'cubic-bezier(.1,.9,.2,1)';   /* Fluent decelerate */
  var REDUCE = false;
  try {
    REDUCE = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  } catch (e) { /* 不支持就当没开 */ }
  var navCur = null;        /* 上一次的选中项（DOM 节点）—— 换了节点才算"真换页" */
  var navY = 0;             /* 上一次落定的 Y（弹开时要从"旧位置"起手） */
  var navReady = false;     /* 首帧不演动作，直接落位 */
  var navPhase = 0;         /* 1 = 压扁阶段（这期间别的重定位别来抢） */
  var navTimer = null;
  /* 🔴 自激护栏。
     syncNavInd 会写 #nav-ind 的 style；它同时又是 MutationObserver 的回调。
     一旦"写"落在被观察的属性上，就成了「回调 → 写 → 再回调」的死循环，
     主线程被占死 —— 页面还有救的感觉都没有：连 CDP 的 Runtime.evaluate 都排不上队，
     探针只会"跑三分钟没输出"。本轮就是这么栽的（栈停在 navAccent）。
     所以留一道闸：同一毫秒内被调超过 60 次就自己断开观察。
     少一个"自动跟随"不要紧（点侧栏、按钮跳转这些路径仍会手动调 syncNavInd），
     把页面卡死才是真的完蛋。 */
  var navStamp = -1;
  var navHits = 0;
  var navGuardTripped = false;

  function navAccent(nav) {
    var cs = getComputedStyle(nav);
    return cs.getPropertyValue('--nav-accent').trim() ||
           cs.getPropertyValue('--accent').trim() || '#4CC2FF';
  }

  /* 把条摆到 (y, scale)。⚠ 只用 transform —— 不碰 left/top，也不碰 class
     （见下面 bootNavInd 那段：改 class 会和 MutationObserver 构成自激）。 */
  function navPlace(y, scale) {
    navIndEl.style.transform = 'translateY(' + y + 'px) scaleY(' + scale + ')';
  }

  function syncNavInd() {
    var now = Date.now();
    if (now === navStamp) {
      if (++navHits > 60 && !navGuardTripped) {
        navGuardTripped = true;
        if (navObs) { try { navObs.disconnect(); } catch (e) { /* 无所谓 */ } }
        try { window.__navIndGuard = 'tripped'; } catch (e2) { /* 无所谓 */ }
      }
    } else { navStamp = now; navHits = 0; }
    var sb = document.getElementById('sidebar');
    if (!sb) { return; }
    if (!navIndEl) { navIndEl = document.getElementById('nav-ind'); }
    if (!navIndEl) { return; }
    var nav = sb.querySelector('.nav.active');
    if (!nav) { navIndEl.style.opacity = '0'; return; }
    /* Y 用"项中心 − 条半高"算，不写死像素：项高会随字号/字体度量变（图标里有一个
       是字体字形），写死就会出现"7 项里只有某项对得上"这种鬼问题。 */
    var sr = sb.getBoundingClientRect();
    var nr = nav.getBoundingClientRect();
    var h = navIndEl.offsetHeight || 22;
    var y = Math.round(nr.top - sr.top + (nr.height - h) / 2);
    /* ★ X 钉在**项的框的左缘**，不是侧栏的左缘（主人 22:03："这个条跑出来了，
       不应该在那个框框里吗"）。侧栏有 8px 横向内边距，写死 0 会让条掉进那条内边距里、
       跟它指示的圆角框脱开。这里按选中项的 offsetLeft 写，内边距怎么改都不会错位。
       ⚠ 写的是内联 style —— MutationObserver 只过滤 class，不会自激（见下面 bootNavInd）。 */
    navIndEl.style.left = (nav.offsetLeft || 0) + 'px';
    navIndEl.style.background = navAccent(nav);
    /* 🔴 显隐走**内联 style**，不走 className —— 见 bootNavInd() 里那段：
       用 class 会跟下面那个 MutationObserver 构成自激（改类→观察回调→再改类），
       页面主线程被占死。加/去一个 class 看着无害，但这里它自己就是触发器。 */
    navIndEl.style.opacity = '1';

    // ---- 位置：分三种情形处理（见文件上方 IND_* 那段注释）----
    if (!navReady) {                       // 首帧：不演动作，直接落在终点
      navReady = true;
      navIndEl.style.transition = 'none';
      navPlace(y, 1);
      navY = y; navCur = nav; navPhase = 0;
      return;
    }
    if (navCur === nav) {                  // 同一项（resize / 字体就绪 / 观察者重复回调）
      if (navPhase === 0) {                //   ⇒ 直接落位；正在演动作时别去抢
        navPlace(y, 1);
        navY = y;
      }
      return;
    }
    navCur = nav;                          // 真的换页了
    if (REDUCE) {                          // 系统里关掉了动画 ⇒ 不给动作
      navIndEl.style.transition = 'none';
      navPlace(y, 1);
      navY = y;
      return;
    }
    if (navTimer) { clearTimeout(navTimer); navTimer = null; }
    navPhase = 1;
    navIndEl.style.transition = 'transform ' + IND_SHRINK + 'ms ' + IND_ACC +
                                ', background ' + IND_POP + 'ms ' + IND_DEC;
    navPlace(navY, IND_SQUASH);            // ① 在旧位置压扁
    navTimer = setTimeout(function () {
      navTimer = null;
      navIndEl.style.transition = 'none';
      navPlace(y, IND_SQUASH);             // ② 压扁状态下**直接跳**到新位置
      void navIndEl.offsetHeight;          //    强制回流：让"跳过去"这一帧先落定，
                                           //    否则下一句会连同位移一起补间 ⇒ 又变成"划过去"
      navIndEl.style.transition = 'transform ' + IND_POP + 'ms ' + IND_DEC +
                                  ', background ' + IND_POP + 'ms ' + IND_DEC;
      navPlace(y, 1);                      // ③ 在新位置弹开
      navY = y; navPhase = 0;
    }, IND_SHRINK + 8);
  }

  function bootNavInd() {
    var sb = document.getElementById('sidebar');
    if (!sb || navBooted) { return; }
    navIndEl = document.getElementById('nav-ind');
    if (!navIndEl) { return; }   // 没有侧栏指示条的页面（如工作台）直接跳过
    navBooted = true;
    navIndEl.style.transition = 'none';   // ① 首帧按住过渡
    syncNavInd();
    void navIndEl.offsetHeight;           // 强制回流，让上面的 transform 先落定
    requestAnimationFrame(function () {
      navIndEl.style.transition = '';
      syncNavInd();                       // 字体铺开后项高可能微变，再量一次
    });
    /* ★ 只观察 class —— 千万别把 syncNavInd 会写的东西（style）也纳进来：
         过滤掉 style 之后，"同步动作"对观察者就是不可见的，自激从结构上不成立。
         上面那道 navGuard 只是第二层保险（万一将来有人往 syncNavInd 里加了改类）。 */
    navObs = new MutationObserver(syncNavInd);
    navObs.observe(sb, { subtree: true, attributes: true, attributeFilter: ['class'] });
    /* ★ 2026-09-28：主题切换时指示条颜色要跟着换。
      题目：深色档主色改成 Win11 蓝 #4CC2FF 后，深/浅两档的 --nav-accent **不再相同**
      （浅色档保留原来的七色）。而指示条的颜色是这儿用内联 style 写死的 —— 只改
      <html data-theme> 不会让它重算，来回切主题时它会停在上一个主题的颜色上。
      所以再挂一个只盯 <html data-theme> 的观察者，主题一变就重新取色。
      ⚠ 与上面同规矩：只观察属性、不观察 style；syncNavInd 只写 #nav-ind 的 style、
        不碰 <html> 的任何属性 ⇒ 结构上不会自激。 */
    if (window.MutationObserver) {
      try {
        new MutationObserver(syncNavInd).observe(document.documentElement,
          { attributes: true, attributeFilter: ['data-theme'] });
      } catch (e) { /* 无所谓 */ }
    }
    window.addEventListener('resize', syncNavInd);
    /* 图标里有一个是**字体字形**（settings），字体就绪前后行高可能差一点 ⇒ 再对一次 */
    if (document.fonts && document.fonts.ready && document.fonts.ready.then) {
      document.fonts.ready.then(syncNavInd)['catch'](function () { /* 无所谓 */ });
    }
  }

  // ------------------------------------------------------------------ 启动

  function boot() {
    buildSprite();
    scan(document.body);
    watch();
    veil();
    wireReveal();
    subscribeMaterial();
    /* ⏱ 诊断开关：调试脚本在页面里预设 window.__NO_NAVIND=true 就跳过指示条，
       用来二分定位"页面卡死到底是不是它"。正常访问没人设这个标志，行为不变。 */
    if (!window.__NO_NAVIND) { bootNavInd(); }
  }

  /* Win11 的 Reveal 高亮要把指针坐标喂给 CSS（--rx / --ry）。
     只绑一次委托，别给每个元素各挂一个监听。 */
  function wireReveal() {
    if (!document.body) { return; }
    document.body.addEventListener('pointermove', function (e) {
      var host = e.target && e.target.closest && e.target.closest('.shell-reveal');
      if (!host) { return; }
      var r = host.getBoundingClientRect();
      host.style.setProperty('--rx', (e.clientX - r.left) + 'px');
      host.style.setProperty('--ry', (e.clientY - r.top) + 'px');
    }, { passive: true });
  }

  /* 宿主换材质时（比如从设置页切完回到工作台），让当前这一页也走一遍过场，
     免得两页观感不一致。桥不支持 onMaterial 时静默跳过。 */
  function subscribeMaterial() {
    if (!window.dsh || typeof window.dsh.onMaterial !== 'function') { return; }
    window.dsh.onMaterial(function () {
      var v = veil();
      if (v) { v.classList.add('on'); }
      setTimeout(settle, 40);
    });
  }

  // ══════════════════════════════════════════════════════════════════════════
  // 自绘下拉框（Win11 ComboBox）—— 外观见 shell-ui.css ⑦
  // --------------------------------------------------------------------------
  // 为什么不用 <select>：它展开时那层弹窗由 Chromium 自己画、**不在 DOM 里**，
  // CSS 碰不到 —— 底色/行高/圆角/高亮永远是浏览器默认值；触发器上还会多一圈
  // 浏览器画的焦点矩形。主人 2026-09-22 的截图就是这两样。
  //
  // 用法：
  //   var dd = ShellUI.Dropdown.create({ mount: 占位元素, id: 'x', onChange: fn });
  //   dd.setOptions([{ value, label, hint }]);   // hint = 名字后面那截浅色说明
  //   dd.setValue('mica');
  // create() 会把占位元素**原地换掉**：新触发器沿用它的 id 与行内 style，
  // 所以页面里 `$('x')` 照旧拿得到，调用方的宽度约束也不用搬家。
  //
  // 两个可选开关：
  //   size      —— 'sm' / 'xs'：加到触发器与菜单上的尺度档（CSS 里定义度量）。
  //   keepMount —— **保留**占位元素（默认是删掉）。给"外面还有代码按 id 取它、读 .value、
  //                挂 .onchange"的场合用：此时原控件留着当数据源（加上 .dd-src 隐身），
  //                触发器另取一个 id（`原id + '-dd'`），免得同一个 id 出现两次、
  //                让人 `$('#x')` 取到触发器而不是原控件。
  // ══════════════════════════════════════════════════════════════════════════
  var DD_SEQ = 0;
  var DD_CHEV = '<svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5"' +
    ' stroke-linecap="round" stroke-linejoin="round"><path d="M5.6 8.2 10 12.6l4.4-4.4"/></svg>';
  // 与 CSS 的 --fl-dur(.22s) / --fl-dur-xfast(.1s) 对齐，只用来判断"什么时候算收完了"；
  // 观感本身完全由 CSS 决定。探针要算等待时间请读实例上的 dd.anim（别把数抄一份到断言里）。
  var DD_DUR = { open: 220, close: 100 };
  var ddOpened = null;          // 当前打开的实例（Win11 同时只允许一个菜单）

  /* ★ 2026-09-22 主人："这个截图都截不出来，只能拍照"。
     根因：触发器 blur 就收菜单 —— 而**截图工具（Win+Shift+S / PrtSc）会抢窗口焦点**，
     壳窗口一 deactivate，blur 就触发，菜单在你拖选区之前就没了（手机拍照不抢焦点，所以拍得到）。
     修法：把两件事分开 ——
       · **页内**焦点转移（Tab 走掉、点到别的控件）⇒ 照旧收；
       · **整个窗口**失活（截图/Alt+Tab/点任务栏）⇒ **不收**，回来还能接着看（也才截得到图）。
     窗口态做成模块级单例：35 个触发器各挂一对监听纯属浪费，也没必要。
     ⚠ 这里**不能**用 `document.hasFocus()`：无头页恒为 false（见 pageeval 头部说明），
       那样写等于"永远不收"，而且 headless 里根本验不了。用真正的 window blur/focus 事件。 */
  var ddWinActive = true;
  /* 🔴 **不能**写 `addEventListener('blur', fn, true)`（捕获阶段）：捕获要从 window 一路
     下沉到目标，所以**触发器自己 blur 时也会命中这个窗口监听** ⇒ 第一次页内失焦就把窗口
     标成"失活"，菜单从此再不收（实测：改成捕获后 ②⑥ 两条都 FAIL）。
     用默认的冒泡阶段：元素 blur 不冒泡 ⇒ 只有 window **自己**的 blur 才会触发它。
     （事件派发在 window 本身时，挂在其上的监听与阶段无关，照样会跑。） */
  window.addEventListener('blur', function () { ddWinActive = false; });
  window.addEventListener('focus', function () { ddWinActive = true; });

  function Dropdown(opts) {
    var mount = opts.mount;
    if (!mount || !mount.parentNode) { return null; }
    var self = this;
    /* 🔴 keepMount 时**不能**沿用 mount 的 id：触发器插在它前面，两个同 id 的话
       `document.getElementById` 取到的是触发器（文档序在前）⇒ 外面 `$('#density').value` 变成
       undefined、挂上去的 .onchange 也再不会触发。所以触发器另起一个 id。 */
    var id = opts.id || (mount.id ? (opts.keepMount ? mount.id + '-dd' : mount.id)
                                  : ('dd' + (++DD_SEQ)));
    var sizeMod = opts.size ? (' dd-' + opts.size) : '';
    var items = [];             // [{ value, label, hint, el }]
    var val = '';
    var act = -1;               // 键盘"当前行"下标
    var hintEl = null, actEl = null, closeTimer = 0, typeBuf = '', typeTimer = 0;

    // ── 触发器：占位元素原地换掉，id / 行内 style 原样继承 ──
    var trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.id = id;
    trigger.className = 'dd' + sizeMod + (opts.className ? ' ' + opts.className : '');
    var inlineStyle = mount.getAttribute('style');
    if (inlineStyle) { trigger.setAttribute('style', inlineStyle); }
    trigger.setAttribute('role', 'combobox');
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.innerHTML = '<span class="dd-lb"></span><span class="dd-ch">' + DD_CHEV + '</span>';
    var lb = trigger.querySelector('.dd-lb');

    // ── 菜单：挂 body + fixed，才不会被卡片的 overflow 裁掉 ──
    var menu = document.createElement('div');
    menu.className = 'dd-menu' + sizeMod;
    menu.id = id + '-menu';
    menu.setAttribute('role', 'listbox');
    menu.hidden = true;
    if (opts.labelledBy) { menu.setAttribute('aria-label', opts.labelledBy); }

    mount.parentNode.insertBefore(trigger, mount);
    if (opts.keepMount) {
      /* 原控件留着当数据源：值 / 选项 / id / 外部监听全都不动，只是看不见、也进不了键盘序。
         ⚠ 别用 display:none —— 外面有代码按它量 offsetWidth / clientHeight，一隐藏就全归零。
         `<label for>` 跟着搬到触发器上（不然点标签不再聚焦，读屏也丢了名字）。 */
      mount.classList.add('dd-src');
      mount.setAttribute('aria-hidden', 'true');
      mount.tabIndex = -1;
      if (mount.id) {
        var lab = document.querySelector('label[for="' + mount.id.replace(/["\\]/g, '\\$&') + '"]');
        if (lab) { lab.setAttribute('for', id); }
      }
    } else {
      mount.parentNode.removeChild(mount);
    }
    document.body.appendChild(menu);

    function isOpen() { return !menu.hidden; }

    /* 落位。顺序要紧：**先**把 min-width 设成触发器宽度，**再**量自己的尺寸 ——
       反过来的话量到的是"还没撑开"的宽度，贴着右缘时会算歪。 */
    function place() {
      var r = trigger.getBoundingClientRect();
      if (!r.width) { return false; }              // 页面被切走了（display:none）
      menu.style.minWidth = Math.round(Math.min(r.width, 380)) + 'px';
      var mw = menu.offsetWidth, mh = menu.offsetHeight;
      var vw = window.innerWidth, vh = window.innerHeight;
      var below = vh - r.bottom - 8;
      var up = (below < mh) && (r.top - 8 > below); // 下面放不下就翻到上方（Win11 同款）
      var x = Math.max(8, Math.min(r.left, vw - mw - 8));
      var y = up ? (r.top - mh - 4) : (r.bottom + 4);
      y = Math.max(8, Math.min(y, vh - mh - 8));
      menu.classList.toggle('up', up);
      menu.style.left = Math.round(x) + 'px';
      menu.style.top = Math.round(y) + 'px';
      return true;
    }

    function scrollAct() {
      if (!actEl) { return; }
      var top = actEl.offsetTop, bot = top + actEl.offsetHeight;
      if (top < menu.scrollTop) { menu.scrollTop = Math.max(0, top - 5); }
      else if (bot > menu.scrollTop + menu.clientHeight) {
        menu.scrollTop = bot - menu.clientHeight + 5;
      }
    }
    function paintAct() {
      if (actEl) { actEl.classList.remove('act'); }
      actEl = (act >= 0 && items[act]) ? items[act].el : null;
      if (actEl) { actEl.classList.add('act'); scrollAct(); }
    }
    function move(step) {
      if (!items.length) { return; }
      act = (act + step + items.length) % items.length;
      paintAct();
    }

    // ── 文档级监听：点外面 / Esc 之外的路径（滚动、缩放）都收掉或跟着走 ──
    var raf = 0;
    function onDocDown(e) {
      if (trigger.contains(e.target) || menu.contains(e.target)) { return; }
      close(false);
    }
    function onDocScroll() {
      if (raf) { return; }
      raf = requestAnimationFrame(function () {
        raf = 0;
        if (!place()) { close(false); }           // 触发器被切走/滚出可视区 ⇒ 收
      });
    }
    function bindDoc() {
      document.addEventListener('mousedown', onDocDown, true);
      window.addEventListener('resize', onDocScroll, true);
      document.addEventListener('scroll', onDocScroll, true);
    }
    function unbindDoc() {
      document.removeEventListener('mousedown', onDocDown, true);
      window.removeEventListener('resize', onDocScroll, true);
      document.removeEventListener('scroll', onDocScroll, true);
    }

    function open() {
      if (!items.length) { return false; }        // 空清单不弹（Win11 也不会弹个空框）
      if (isOpen()) { return true; }
      if (ddOpened && ddOpened !== self) { ddOpened.close(); }
      ddOpened = self;
      clearTimeout(closeTimer);
      menu.classList.remove('closing');
      menu.hidden = false;
      trigger.classList.add('open');
      trigger.setAttribute('aria-expanded', 'true');
      act = -1;
      for (var i = 0; i < items.length; i++) {
        if (items[i].value === val) { act = i; break; }
      }
      if (act < 0) { act = 0; }
      paintAct();
      place();
      place();                                     // 滚动条出现后宽度可能变，复量一次
      bindDoc();
      return true;
    }

    function close(refocus) {
      if (!isOpen()) { return; }
      trigger.classList.remove('open');
      trigger.setAttribute('aria-expanded', 'false');
      if (ddOpened === self) { ddOpened = null; }
      if (actEl) { actEl.classList.remove('act'); actEl = null; }
      unbindDoc();
      if (raf) { cancelAnimationFrame(raf); raf = 0; }
      menu.classList.add('closing');                // 收起也走动画（accelerate 100ms）
      clearTimeout(closeTimer);
      closeTimer = setTimeout(function () {
        menu.hidden = true;
        menu.classList.remove('closing');
      }, DD_DUR.close);
      if (refocus) {
        try { trigger.focus({ preventScroll: true }); } catch (e) { trigger.focus(); }
      }
    }

    function setValue(v) {
      var changed = (v !== val);
      val = v;
      var found = null;
      for (var i = 0; i < items.length; i++) {
        var on = (items[i].value === v);
        items[i].el.classList.toggle('on', on);
        items[i].el.setAttribute('aria-selected', on ? 'true' : 'false');
        if (on) { found = items[i]; }
      }
      // 触发器上那行：名字 + 一截浅色说明（说明在菜单里是独立小字，这里也保持同色规则）
      if (hintEl) { hintEl.remove(); hintEl = null; }
      lb.textContent = found ? found.label : (v == null ? '' : String(v));
      if (found && found.hint) {
        hintEl = document.createElement('span');
        hintEl.className = 'dd-h';
        hintEl.textContent = ' — ' + found.hint;
        lb.appendChild(hintEl);
      }
      trigger.setAttribute('data-v', v == null ? '' : String(v));
      return changed;
    }

    function pick(i) {
      var it = items[i];
      if (!it) { return; }
      var changed = setValue(it.value);
      close(true);
      if (changed && typeof opts.onChange === 'function') { opts.onChange(it.value, it); }
    }

    function setOptions(list) {
      items = [];
      actEl = null; act = -1;
      menu.innerHTML = '';
      (list || []).forEach(function (o) {
        var el = document.createElement('div');
        el.className = 'dd-opt';
        el.setAttribute('role', 'option');
        el.setAttribute('data-v', o.value == null ? '' : String(o.value));
        el.innerHTML = '<span class="dd-bar"></span><span class="dd-tx"></span>';
        var tx = el.querySelector('.dd-tx');
        tx.textContent = o.label == null ? String(o.value) : o.label;
        if (o.hint) {
          var h = document.createElement('span');
          h.className = 'dd-h';
          h.textContent = ' — ' + o.hint;
          tx.appendChild(h);
        }
        // mousedown 阻止默认：不让焦点从触发器跑到菜单上（Win11 里焦点一直在 ComboBox）
        el.addEventListener('mousedown', function (e) { e.preventDefault(); });
        el.addEventListener('click', function () { pick(items.indexOf(item)); });
        el.addEventListener('mouseenter', function () {
          act = items.indexOf(item); paintAct();
        });
        var item = { value: o.value, label: o.label, hint: o.hint, el: el };
        items.push(item);
        menu.appendChild(el);
      });
      setValue(val);
      return items.length;
    }

    function typeAhead(ch) {
      clearTimeout(typeTimer);
      typeBuf += ch.toLowerCase();
      typeTimer = setTimeout(function () { typeBuf = ''; }, 600);
      for (var i = 0; i < items.length; i++) {
        var from = (i + act) % items.length;
        var it = items[from];
        if (String(it.label || '').toLowerCase().indexOf(typeBuf) === 0) {
          act = from; paintAct(); return;
        }
      }
    }

    function onKey(e) {
      var k = e.key;
      if (!isOpen()) {
        if (k === 'ArrowDown' || k === 'ArrowUp' || k === 'Enter' || k === ' ' || k === 'Spacebar') {
          if (open()) { e.preventDefault(); }
        }
        return;
      }
      if (k === 'Escape' || k === 'Esc') { e.preventDefault(); close(true); return; }
      if (k === 'Tab') { close(false); return; }
      if (k === 'ArrowDown') { e.preventDefault(); move(1); return; }
      if (k === 'ArrowUp') { e.preventDefault(); move(-1); return; }
      if (k === 'Home') { e.preventDefault(); act = 0; paintAct(); return; }
      if (k === 'End') { e.preventDefault(); act = items.length - 1; paintAct(); return; }
      if (k === 'Enter' || k === ' ' || k === 'Spacebar') { e.preventDefault(); pick(act); return; }
      if (k && k.length === 1) { typeAhead(k); }
    }

    trigger.addEventListener('click', function () { isOpen() ? close(true) : open(); });
    trigger.addEventListener('keydown', onKey);
    trigger.addEventListener('blur', function () {
      // 焦点离开触发器 ⇒ 收。⚠ **但"整个窗口失活"不算**（截图工具/Alt+Tab 会走到这里，
      // 那时收掉菜单就等于"这菜单截不出来"）。判据见 ddWinActive 的注释。
      setTimeout(function () {
        if (isOpen() && document.activeElement !== trigger && ddWinActive) { close(false); }
      }, 0);
    });

    self.id = id;
    self.el = trigger;
    self.menu = menu;
    self.anim = DD_DUR;
    self.setOptions = setOptions;
    self.setValue = setValue;
    self.options = function () {
      return items.map(function (it) { return { value: it.value, label: it.label, hint: it.hint }; });
    };
    self.value = function () { return val; };
    self.text = function () { return lb.textContent; };
    self.open = open;
    self.close = close;
    self.isOpen = isOpen;
    self.destroy = function () {
      close(false);
      unbindDoc();
      clearTimeout(closeTimer); clearTimeout(typeTimer);
      if (raf) { cancelAnimationFrame(raf); }
      menu.remove();
      trigger.remove();
    };
    return self;
  }

  window.ShellUI = {
    icon: icon,
    has: has,
    scan: scan,
    label: label,
    /* 自绘下拉框（Win11 ComboBox）：见本文件上方 Dropdown 与 shell-ui.css ⑦。
       anim 是只读的动作参数，探针拿它算"等到什么时候"，免得把时长抄进断言。 */
    Dropdown: {
      /* 🔴 必须先判再 `new`：构造里的 `return null`（mount 已脱离文档那种）会被 `new` **吞掉** ——
         构造函数返回非对象时，`new` 忽略它、照旧把**半成品 this** 交出去。那个对象上没有
         setOptions/setValue，调用方 `dd.setOptions(...)` 就炸 `is not a function`，
         而且报错点离真正的原因（挂载点不在文档里）十万八千里。
         （2026-09-22 真踩：工作台观察器给了一批**已脱离**的 select，抛的就是这个。） */
      create: function (o) {
        o = o || {};
        if (!o.mount || !o.mount.parentNode) { return null; }
        return new Dropdown(o);
      },
      anim: DD_DUR,
      openCount: function () { return ddOpened ? 1 : 0; }
    },
    reveal: function (el) { if (el && el.classList) { el.classList.add('shell-reveal'); } },
    MatFade: { run: run, settle: settle, veil: veil },
    /* 侧栏指示条的手动同步口。页面自己的切页路径下不必调（有 MutationObserver 兜着），
       探针用来做"换一项之后条会不会跟着走"的实测。 */
    navInd: syncNavInd,
    /* 动作参数（只读）：探针拿它算"该等到什么时候"，免得把时长抄一份在断言里 ——
       CSS/JS 里改了时长而断言没改，就会变成"动画明明是对的却红"。 */
    navAnim: {
      shrink: IND_SHRINK,
      pop: IND_POP,
      squash: IND_SQUASH,
      reduce: function () { return REDUCE; }
    }
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();
