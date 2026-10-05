/**
 * 全曲预览条（横向可滑动）：把整首歌铺成一条时间轴。
 *
 * ★ 导航模型照搬 `adofaiex/Re_ADOJAS` 的编辑器 Timeline（`src/pages/Editor/EditorPage.tsx`）：
 *   主导航的单位是**格（tile / floor）**，不是毫秒。
 *     · 点/拖 = 「定位到这一格」：先 seek，再按 `getTileIndexAtTime` 反查格号选中
 *     · `←` / `→`  = 上一格 / 下一格（播放中不抢：交给上层做 ±5s）
 *     · `Home` / `End` = 第一格 / 最后一格
 *     · 「播放」= 从**选中的那一格**开始（没选就从 0 开始）
 *     · 角标显示 `#格号 / 总格数`
 *   与之对应的原版 API：`selectTile` / `getTileIndexAtTime` / `getTileTimeMs`
 *   → 在这边就是 `selectFloor` / `floorAt` / `floorTime`。
 *
 * 在它之上，我们**额外**保留两个原版没有的能力（本项目区间采音要用）：
 *     · **Shift + 拖动 = 框选一段** → 交给上层建「区间采音」
 *     · 滚轮缩放 / 中键平移 / 双击复位
 *
 * 画什么（自上而下）：
 *   时间刻度 → 采音点密度条 → 方格密度（层数） → **区间色带** → 选中格 → 播放头
 *
 * 它跟其它视图共用「音频毫秒轴」，所以 `setPlayhead(ms)` 直接画就行。
 * 注意：谱面视图的时间轴是 entryTime 轴，这里是**音频轴**；`entry[]` 就是音频轴上的
 * 每层进入时刻（`tools/_axis_probe.py` 实测：预览音频首按时刻 == entry[1]，差 <1ms）。
 */
const COLOR = {
  bg: '#0e1319', ruler: 'rgba(255,255,255,0.10)', tick: 'rgba(255,255,255,0.28)',
  text: '#8b98a4', onset: 'rgba(61,110,168,0.85)', floor: 'rgba(127,179,213,0.20)',
  play: '#ffd166', sel: 'rgba(47,93,128,0.35)', selEdge: '#5fa8e0',
  band: 'rgba(47,93,128,0.55)', bandSel: 'rgba(95,168,224,0.55)',
  bandEdge: '#5fa8e0', hud: '#8b949e',
  tileCell: 'rgba(126,231,135,0.13)', tileEdge: '#7ee787', tileText: '#b8f0bf',
};
const REGION_HUE = ['#2f5d80', '#8a5a2b', '#3f6b4a', '#6b4a7a', '#7a4a4a'];
// ★ 分段采音的泳道配色（`docs/34` 方案 C）：三个角色各一色，`off` 不画。
//   蓝=主（全采）/ 青=次（只插空）/ 琥珀=双押（只出落点）。
const ROLE_COLOR = {
  main: 'rgba(95,168,224,0.85)',
  sub: 'rgba(110,201,178,0.70)',
  dp: 'rgba(255,209,102,0.80)',
};

export class Overview {
  constructor(canvas) {
    this.cv = canvas;
    this.ctx = canvas.getContext('2d');
    this.hits = [];
    this.entries = [];
    this.total = 1000;
    this.regions = [];
    this.segments = [];            // ★ 分段采音的**泳道**（docs/34 方案 C）
    this.segMode = 'from';
    this.sel = -1;                 // 选中的**区间**下标（区间采音用）
    this.selFloor = null;          // ★ 选中的**格**（Re_ADOJAS 的 selectedTileIndex）
    this.play = 0;
    this.t0 = 0;
    this.span = 1000;
    this._drag = null;
    this._pan = null;
    this._box = null;
    this._bandRect = null;         // draw() 里算好的区间色带矩形（命中测试要用同一份）
    this.onSeek = null;
    this.onRegion = null;
    this.onSelectRegion = null;
    this.onSelectFloor = null;     // (idx) 选中格变化时通知上层
    this.bindMouse();
  }

  setData(payload) {
    const total = Math.max(1, payload.total_ms || 1000);
    const first = this.total;
    this.hits = payload.hit || [];
    this.entries = payload.entry || [];
    this.regions = payload.regions || [];
    this.segments = payload.segments || [];
    this.segMode = payload.segment_mode || 'from';
    this.total = total;
    if (first !== total) { this.t0 = 0; this.span = total; }
    // 换文件/重算后格数会变：越界的选中要收回，否则角标会显示一个不存在的格
    if (this.selFloor !== null
        && (this.selFloor < 0 || this.selFloor >= this.entries.length)) {
      this.selFloor = null;
    }
  }

  setPlayhead(ms) { this.play = ms; }

  fit() { this.t0 = 0; this.span = this.total; }

  x(ms, w) { return (ms - this.t0) / this.span * w; }

  // ------------------------------------------------------------ 分段泳道
  /** 需要画几条泳道：分段里出现过的轨（主/次/双押），去重升序。 */
  laneTracks() {
    const s = new Set();
    for (const sp of this.segments) {
      for (const k of ['main', 'sub', 'dp']) {
        for (const i of (sp[k] || [])) s.add(i);
      }
    }
    return [...s].sort((a, b) => a - b);
  }

  /**
   * 画泳道。★ 只画**分段显式指定过**的角色块：
   *  `null`（继承全局）不画 —— 所以「不画 = 今天的行为」，一眼能看出哪里被改过。
   */
  _drawLanes(ctx, w, bandY, bandH, lanes) {
    const n = lanes.length || 1;
    const laneH = bandH / n;
    ctx.font = '9px Consolas, "Microsoft YaHei UI", monospace';
    for (let li = 0; li < n; li++) {
      const tid = lanes[li];
      const y = bandY + li * laneH;
      // 行底 + 轨号
      ctx.fillStyle = 'rgba(255,255,255,0.035)';
      ctx.fillRect(0, y, w, laneH);
      ctx.fillStyle = 'rgba(139,152,164,0.9)';
      ctx.fillText(`t${tid}`, 2, y + laneH - 2);
      // 角色块
      for (const sp of this.segments) {
        const t0 = sp.start_ms; const t1 = sp.end_ms;
        const x0 = this.x(t0, w); const x1 = this.x(t1, w);
        if (x1 < 0 || x0 > w) continue;
        const a = Math.max(0, x0); const b = Math.min(w, x1);
        if (b - a < 1) continue;
        for (const [k, col] of [['main', ROLE_COLOR.main],
          ['sub', ROLE_COLOR.sub], ['dp', ROLE_COLOR.dp]]) {
          const v = sp[k];
          if (!Array.isArray(v) || !v.includes(tid)) continue;
          ctx.fillStyle = col;
          ctx.fillRect(a, y + 1, b - a, Math.max(1, laneH - 2));
        }
      }
    }
    // 段界竖线（只在显式分段处）
    ctx.save();
    ctx.setLineDash([3, 3]);
    ctx.strokeStyle = 'rgba(255,209,102,0.55)';
    ctx.lineWidth = 1;
    for (const sp of this.segments) {
      if (sp.src !== 'segment') continue;
      const px = Math.round(this.x(sp.start_ms, w)) + 0.5;
      if (px < 0 || px > w) continue;
      ctx.beginPath(); ctx.moveTo(px, bandY); ctx.lineTo(px, bandY + bandH); ctx.stroke();
    }
    ctx.restore();
  }

  ms(x, w) { return this.t0 + x / Math.max(1, w) * this.span; }

  // ------------------------------------------------ 格（floor/tile）导航
  get nFloors() { return this.entries.length; }

  /** 对等 Re_ADOJAS `getTileIndexAtTime`：entryTime <= ms 的最后一格。 */
  floorAt(ms) {
    const e = this.entries;
    if (!e.length) return null;
    let lo = 0; let hi = e.length;
    while (lo < hi) { const m = (lo + hi) >> 1; if (e[m] <= ms) lo = m + 1; else hi = m; }
    return Math.max(0, Math.min(e.length - 1, lo - 1));
  }

  /** 对等 Re_ADOJAS `getTileTimeMs`：第 i 格的进入时刻（ms）。 */
  floorTime(i) {
    if (i === null || i === undefined || i < 0 || i >= this.entries.length) return 0;
    return this.entries[i] || 0;
  }

  /**
   * 选中第 i 格。**只改状态 + 通知上层**，不负责 seek
   * （Re_ADOJAS 里 seek 由发起者做：拖动→`seekTo`+`selectTile`；键盘→`selectTile`）。
   */
  selectFloor(i, { redraw = true, notify = true } = {}) {
    if (i === null || i === undefined || !this.entries.length) {
      this.selFloor = null;
    } else {
      this.selFloor = Math.max(0, Math.min(this.entries.length - 1, Math.round(i)));
    }
    if (notify && this.onSelectFloor) this.onSelectFloor(this.selFloor);
    if (redraw) this.draw();
    return this.selFloor;
  }

  /** 相对步进（Re_ADOJAS 的 `←` / `→`）：没选中时从播放头所在格起步。 */
  stepFloor(d) {
    if (!this.entries.length) return null;
    const base = this.selFloor === null ? this.floorAt(this.play) : this.selFloor;
    return this.selectFloor(base + d);
  }

  firstFloor() { return this.selectFloor(0); }

  lastFloor() { return this.selectFloor(this.entries.length - 1); }

  clearFloor() {
    if (this.selFloor === null) return;
    this.selFloor = null;
    if (this.onSelectFloor) this.onSelectFloor(null);
    this.draw();
  }

  draw() {
    const cv = this.cv; const ctx = this.ctx;
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth; const h = cv.clientHeight;
    if (!w || !h) return;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
      cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = COLOR.bg; ctx.fillRect(0, 0, w, h);

    const rulerH = 16;
    const hudH = 14;                    // 底部专留一行给说明文字，别压在密度条上
    // ★ 有分段时色带区扩成「泳道」（一条轨一行），否则维持原来的一条色带
    const lanes = this.laneTracks();
    const laneMode = lanes.length > 0;
    const bandH = laneMode
      ? Math.max(16, Math.min(52, h * 0.38))
      : Math.max(12, Math.min(18, h * 0.2));
    const bandY = h - hudH - bandH - 1;
    const plotH = bandY - rulerH - 2;
    this._bandRect = { y: bandY, h: bandH };

    // 时间刻度
    ctx.strokeStyle = COLOR.ruler; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, rulerH); ctx.lineTo(w, rulerH); ctx.stroke();
    const step = this._tickStep();
    ctx.font = '10px Consolas, monospace';
    ctx.fillStyle = COLOR.text;
    for (let t = Math.floor(this.t0 / step) * step; t <= this.t0 + this.span; t += step) {
      const px = this.x(t, w);
      if (px < -1 || px > w + 1) continue;
      ctx.strokeStyle = COLOR.ruler;
      ctx.beginPath(); ctx.moveTo(px, rulerH - 4); ctx.lineTo(px, h); ctx.stroke();
      ctx.fillStyle = COLOR.text;
      ctx.fillText(this._fmt(t), px + 2, 11);
    }

    // ★ 选中格：铺满「本格进入 → 下一格进入」的整段（视觉上就是一格）
    if (this.selFloor !== null && this.entries.length) {
      const i = this.selFloor;
      const a = this.x(this.entries[i], w);
      const nextT = i + 1 < this.entries.length ? this.entries[i + 1]
        : Math.min(this.total, this.entries[i] + 100);
      const b = this.x(nextT, w);
      const x0 = Math.max(0, Math.min(a, b));
      const x1 = Math.min(w, Math.max(a, b, a + 2));   // 至少 2px，极窄时也看得见
      ctx.fillStyle = COLOR.tileCell;
      ctx.fillRect(x0, rulerH + 1, Math.max(2, x1 - x0), bandY - rulerH - 1);
      ctx.strokeStyle = COLOR.tileEdge; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(a + 1, 0); ctx.lineTo(a + 1, bandY); ctx.stroke();
      // 顶部小三角，窄格也认得出是哪一格
      ctx.fillStyle = COLOR.tileEdge;
      ctx.beginPath(); ctx.moveTo(a + 1, 0); ctx.lineTo(a - 3, 7); ctx.lineTo(a + 5, 7);
      ctx.closePath(); ctx.fill();
      if (x1 - x0 > 22) {
        ctx.fillStyle = COLOR.tileText;
        ctx.font = '10px Consolas, "Microsoft YaHei UI", monospace';
        ctx.fillText(`#${i}`, x0 + 5, rulerH + 12);
      }
    }

    // 密度条：每个像素列统计落在里面的采音点
    const cols = new Uint16Array(Math.max(1, Math.round(w)));
    for (const t of this.hits) {
      const px = Math.round(this.x(t, w));
      if (px >= 0 && px < cols.length) cols[px] += 1;
    }
    let cmax = 1;
    for (let i = 0; i < cols.length; i++) if (cols[i] > cmax) cmax = cols[i];
    ctx.fillStyle = COLOR.onset;
    for (let i = 0; i < cols.length; i++) {
      if (!cols[i]) continue;
      const bh = Math.max(2, cols[i] / cmax * plotH);
      ctx.fillRect(i, rulerH + 1 + (plotH - bh), 1, bh);
    }
    // 层位置（细横线）
    ctx.fillStyle = COLOR.floor;
    for (const t of this.entries) {
      const px = this.x(t, w);
      if (px < 0 || px > w) continue;
      ctx.fillRect(px, rulerH + 1, 1, plotH);
    }

    // 区间色带
    ctx.font = '11px "Microsoft YaHei UI", sans-serif';
    for (let i = 0; i < this.regions.length; i++) {
      const rg = this.regions[i];
      const x0 = this.x(rg.start_ms, w); const x1 = this.x(rg.end_ms, w);
      if (x1 < 0 || x0 > w) continue;
      const a = Math.max(0, x0); const b = Math.min(w, x1);
      ctx.fillStyle = i === this.sel ? COLOR.bandSel
        : REGION_HUE[i % REGION_HUE.length];
      ctx.fillRect(a, bandY, b - a, bandH);
      ctx.strokeStyle = COLOR.bandEdge; ctx.lineWidth = 1;
      ctx.strokeRect(a + 0.5, bandY + 0.5, b - a - 1, bandH - 1);
      if (b - a > 26) {
        ctx.fillStyle = '#dbe6f0';
        ctx.fillText(rg.label || `区间${i + 1}`, a + 4, bandY + bandH - 4);
      }
    }
    // ★ 分段泳道（`docs/34` 方案 C）：一条轨一行，块的颜色 = 该时刻这条轨的角色。
    //   没画块的地方 = 继承全局 ② 的选择（所以「不画 = 今天的行为」）。
    if (laneMode) this._drawLanes(ctx, w, bandY, bandH, lanes);
    // 框选中的临时选区
    if (this._box) {
      const x0 = this.x(this._box.t0, w); const x1 = this.x(this._box.t1, w);
      ctx.fillStyle = COLOR.sel;
      ctx.fillRect(Math.min(x0, x1), rulerH, Math.abs(x1 - x0), h - rulerH);
      ctx.strokeStyle = COLOR.selEdge; ctx.lineWidth = 1;
      ctx.strokeRect(Math.min(x0, x1) + 0.5, rulerH, Math.abs(x1 - x0), h - rulerH);
      ctx.fillStyle = '#cfe3f5';
      ctx.fillText(`${(this._box.t0 / 1000).toFixed(2)}s → `
        + `${(this._box.t1 / 1000).toFixed(2)}s`, Math.min(x0, x1) + 4, rulerH + 14);
    }

    // 播放头
    const px = this.x(this.play, w);
    if (px >= 0 && px <= w) {
      ctx.strokeStyle = COLOR.play; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, h); ctx.stroke();
      ctx.fillStyle = COLOR.play;
      ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px - 4, 6); ctx.lineTo(px + 4, 6);
      ctx.closePath(); ctx.fill();
    }

    // ★ 选中格的箭头**补画在播放头之上**（新 UI：中心区变窄 ⇒ 一格只有 1~2px，
    //   而「拖动定位」会让播放头与选中格的竖线**必然重合** ⇒ 箭头被红色压住就分不清
    //   选的是哪一格。所以箭头最后画：它不会盖住任何尺寸信息，却让选中格永远看得见。）
    if (this.selFloor !== null && this.entries.length) {
      const a = this.x(this.entries[this.selFloor], w);
      ctx.fillStyle = COLOR.tileEdge;
      ctx.beginPath(); ctx.moveTo(a + 1, 0); ctx.lineTo(a - 3, 7); ctx.lineTo(a + 5, 7);
      ctx.closePath(); ctx.fill();
    }

    // 角标（独立一行，不压密度条）
    ctx.fillStyle = COLOR.hud;
    ctx.font = '10px Consolas, "Microsoft YaHei UI", monospace';
    const zoom = this.span < this.total - 1
      ? `  [${(this.t0 / 1000).toFixed(1)}~${((this.t0 + this.span) / 1000).toFixed(1)}s]` : '';
    let hud = `全曲 ${(this.total / 1000).toFixed(1)}s${zoom}`
      + `  采音点 ${this.hits.length}  区间 ${this.regions.length}`;
    if (this.segments.length) {
      hud += `  分段 ${this.segments.length}`
        + (this.segMode === 'until' ? '（到这点为止）' : '（从这点起）');
    }
    if (this.selFloor !== null) {
      hud += `  格 #${this.selFloor}/${Math.max(0, this.entries.length - 1)}`
        + ` @ ${(this.floorTime(this.selFloor) / 1000).toFixed(2)}s`;
    } else if (this.entries.length) {
      hud += `  格 ${this.entries.length}`;
    }
    ctx.fillText(hud, 4, h - 3);
    const tip = '点击/拖动=选格定位 / ←→=逐格 / Home End=首尾 / Shift+拖动=框选 / 滚轮=缩放';
    ctx.textAlign = 'right';
    ctx.fillText(tip, w - 4, h - 3);
    ctx.textAlign = 'left';
  }

  _tickStep() {
    const targets = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300];
    const want = this.span / 8;
    for (const t of targets) if (t * 1000 >= want) return t * 1000;
    return 600000;
  }

  _fmt(ms) {
    const s = ms / 1000;
    const m = Math.floor(s / 60);
    return m > 0 ? `${m}:${String(Math.floor(s - m * 60)).padStart(2, '0')}`
      : `${s.toFixed(0)}s`;
  }

  /** 画布坐标 → 音频毫秒（夹在 [0,total]）。 */
  _evTime(e) {
    const rect = this.cv.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const t = this.ms(mx, this.cv.clientWidth);
    return Math.max(0, Math.min(this.total, t));
  }

  bindMouse() {
    const cv = this.cv;
    cv.addEventListener('wheel', (e) => {
      e.preventDefault();
      const rect = cv.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const d = e.deltaY || e.deltaX;
      if (e.shiftKey) {
        this.t0 = Math.max(0, Math.min(this.total - this.span,
          this.t0 + (d > 0 ? 1 : -1) * this.span * 0.15));
      } else {
        const anchor = this.ms(mx, cv.clientWidth);
        const f = d < 0 ? 1 / 1.35 : 1.35;
        this.span = Math.max(Math.min(this.total, 300), Math.min(this.total, this.span * f));
        this.t0 = Math.max(0, Math.min(this.total - this.span,
          anchor - (mx / Math.max(1, cv.clientWidth)) * this.span));
      }
      this.draw();
    }, { passive: false });

    cv.addEventListener('mousedown', (e) => {
      const t = this._evTime(e);
      if (e.button === 1) {
        this._pan = { x: e.clientX - cv.getBoundingClientRect().left, t0: this.t0 };
        e.preventDefault();
        return;
      }
      if (e.button !== 0) return;
      // Shift+拖动 = 框选区间（本项目扩展，原版没有）
      if (e.shiftKey && this.onRegion) {
        this._box = { t0: t, t1: t };
        this.draw();
        return;
      }
      // 点在色带上 = 选中该区间（保留原行为，不抢导航）
      const br = this._bandRect;
      if (br && e.offsetY >= br.y && this.onSelectRegion) {
        const hit = this.regions.findIndex((r) => t >= r.start_ms && t <= r.end_ms);
        if (hit >= 0) { this.sel = hit; this.onSelectRegion(hit); this.draw(); return; }
      }
      // ★ 原版语义：按下 = 定位 + 选中这一格
      this._drag = true;
      this._pickAt(t);
    });

    cv.addEventListener('mousemove', (e) => {
      const mlocal = e.clientX - cv.getBoundingClientRect().left;
      const t = this._evTime(e);
      if (this._pan) {
        this.t0 = Math.max(0, Math.min(this.total - this.span,
          this._pan.t0 - (mlocal - this._pan.x) * (this.span / Math.max(1, cv.clientWidth))));
        this.draw();
        return;
      }
      if (this._box) {
        this._box.t1 = t;
        if (this._box.t1 < this._box.t0) {
          const s = this._box.t1; this._box.t1 = this._box.t0; this._box.t0 = s;
        }
        this.draw();
        return;
      }
      if (this._drag) this._pickAt(t);
    });

    const endDrag = () => {
      if (this._pan) { this._pan = null; return; }
      if (this._box) {
        const b = this._box;
        this._box = null;
        if (b.t1 - b.t0 >= 60 && this.onRegion) this.onRegion(b.t0, b.t1);
        this.draw();
        return;
      }
      this._drag = null;
    };
    cv.addEventListener('mouseup', endDrag);
    cv.addEventListener('mouseleave', endDrag);
    cv.addEventListener('dblclick', () => { this.fit(); this.draw(); });
  }

  /** 定位 + 选中该处所在的格（拖动中连续调用）。 */
  _pickAt(t) {
    if (this.onSeek) this.onSeek(t);
    this.selectFloor(this.floorAt(t), { notify: true });
  }
}
