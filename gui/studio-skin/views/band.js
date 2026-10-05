/* ==========================================================================
   band.js —— **段带**（docs/49 方案 A）
   --------------------------------------------------------------------------
   带 = 段色块 + 标记（双押 / **三押**）+ 播放头 + 刻度；**不做泳道**。
   全曲条（views/overview.js）仍是「永远看全曲的小地图」，两者分工：
     带 = 可缩放/可平移的**编辑面**（拖段边界改时间）
     全曲条 = 缩略图（知道自己在哪、框选区间）

   ★★ 三押的视觉记号（用户 2026-10 问的那半）：
     双押 = 细竖线；**三押 = 加粗 + 一道横杠**（双层）。
     三押从 `payload.dp_pairs` 还原：同一「剩余格」下**两块薄格** ⇒ 押数 3。
     —— 不需要后端改任何东西（`dp_pairs` 早就在 payload 里）。

   坐标：一切都在**采音轴**（= payload 的 hit/entry 轴；`docs/24` §5）
   ========================================================================== */

const KIND_COLOR = { audio: '#3a6f96', skeleton: '#8a5cd2', steer: '#2f8f6a',
                     camera: '#c07a2b', ignore: '#5b6874' };

export class Band {
  constructor(canvas, hooks = {}) {
    this.cv = canvas;
    this.ctx = canvas.getContext('2d');
    this.hooks = hooks;              // {onSeek(ms), onEditRanges(), ranges(), dp()}
    this.t0 = 0;                     // 视窗左端（ms，采音轴）
    this.per = 1 / 40;               // px/ms
    this.dur = 60000;
    this.playT = 0;
    this.marks = [];                 // [{t, press}]
    this.hs = [];                    // 换手押上色的那些格 [{t, floor}]（docs/59）
    this.ap = [];                    // 算法轨道调度记号 [{t,floor,kind}]（docs/60）
    this.segs = [];                  // [{t0,t1,label,kind}]
    this.sel = -1;
    this.drag = null;
    this._bind();
  }

  setData(payload) {
    if (!payload) return;
    const hit = payload.hit || [];
    const entry = payload.entry || [];
    this.dur = Math.max(1, (payload.audio_dur_ms || 0)
      || (entry.length ? entry[entry.length - 1] : 60000));
    // —— 段：优先用区间/分段/采bpm 区间（都在采音轴），没有就退回「整曲」一条
    this.segs = [];
    const R = (this.hooks.ranges && this.hooks.ranges()) || {};
    for (const r of (R.regions || [])) {
      this.segs.push({ t0: r.t0, t1: r.t1, label: r.label || '区间采音',
                       kind: 'audio', ref: { kind: 'region', i: r.i } });
    }
    for (const r of (R.xk || [])) {
      this.segs.push({ t0: r.t0, t1: r.t1, label: r.label || '采bpm 骨架',
                       kind: 'skeleton', ref: { kind: 'xk', i: r.i } });
    }
    for (const r of (R.segments || [])) {
      this.segs.push({ t0: r.t0, t1: r.t1, label: r.label || '分段', kind: 'steer',
                       ref: { kind: 'segment', i: r.i } });
    }
    if (!this.segs.length && entry.length) {
      this.segs.push({ t0: 0, t1: this.dur, label: '全曲（未分段）', kind: 'audio',
                       ref: null });
    }
    // —— 标记：双押 / 三押（从 dp_pairs 还原押数）
    this.marks = (this.hooks.dp && this.hooks.dp()) || [];
    if (!this.marks.length && hit.length) {           // 没算过就只画采音点
      this.marks = hit.map((t) => ({ t, press: 1 }));
    }
    // —— ★★ 换手押上色（`docs/59`）：后端把「染了色的那些格」交出来，段带上打记号
    this.hs = (this.hooks.color && this.hooks.color()) || [];
    this.ap = (this.hooks.appear && this.hooks.appear()) || [];
    if (this.per * this.dur > this.cv.clientWidth * 4) this.per = this.cv.clientWidth / this.dur;
    this.draw();
  }

  setPlayhead(ms) { this.playT = ms; this.draw(); }

  size() {
    const dpr = devicePixelRatio || 1;
    const r = this.cv.parentNode.getBoundingClientRect();
    this.cv.width = Math.max(1, r.width * dpr);
    this.cv.height = Math.max(1, r.height * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.W = r.width; this.H = r.height;
    this.draw();
  }

  // 采音轴 ms ⇄ 像素
  X(t) { return ((t - this.t0) * this.per); }
  T(x) { return this.t0 + x / this.per; }

  draw() {
    const c = this.ctx, W = this.W || this.cv.clientWidth, H = this.H || this.cv.clientHeight;
    if (!c) return;
    c.clearRect(0, 0, W, H);
    c.font = '11px Consolas, monospace';
    const SEGY = 6, SEGH = 26;
    // 段色块
    this.segs.forEach((s, i) => {
      const x0 = this.X(s.t0), x1 = this.X(s.t1);
      if (x1 < 0 || x0 > W) return;
      c.fillStyle = (KIND_COLOR[s.kind] || '#3a6f96') + (i === this.sel ? '' : '99');
      c.fillRect(x0, SEGY, Math.max(2, x1 - x0), SEGH);
      c.strokeStyle = i === this.sel ? '#eaf4ff' : '#0b0f14';
      c.lineWidth = i === this.sel ? 2 : 1;
      c.strokeRect(x0 + .5, SEGY + .5, Math.max(2, x1 - x0) - 1, SEGH - 1);
      c.fillStyle = '#eaf4ff';
      c.fillText(s.label, x0 + 5, SEGY + 17);
      // 左右把手（可拖边界）
      c.fillStyle = '#eaf4ff';
      c.fillRect(x0, SEGY, 3, SEGH); c.fillRect(x1 - 3, SEGY, 3, SEGH);
    });
    // 刻度
    const step = this.per > 0.06 ? 1000 : this.per > 0.02 ? 5000 : 10000;
    for (let t = Math.floor(this.t0 / step) * step; t < this.t0 + W / this.per; t += step) {
      const x = this.X(t);
      c.strokeStyle = '#1b232c';
      c.beginPath(); c.moveTo(x, 34); c.lineTo(x, H); c.stroke();
      c.fillStyle = '#5b6874';
      c.fillText((t / 1000).toFixed(0) + 's', x + 3, 46);
    }
    // 标记行
    const my = H - 20;
    c.fillStyle = '#0d1218'; c.fillRect(0, my - 12, W, 26);
    c.fillStyle = '#6f7b87'; c.fillText('标记', 6, my + 4);
    for (const m of this.marks) {
      const x = this.X(m.t);
      if (x < -6 || x > W) continue;
      if (m.press >= 3) {                    // ★ 三押：加粗 + 一道横杠
        c.fillStyle = '#eaf4ff';
        c.fillRect(x - 1, my - 9, 3, 18);
        c.fillRect(x - 4, my - 3, 9, 2);
      } else if (m.press === 2) {             // 双押
        c.fillStyle = '#d29922';
        c.fillRect(x - 1, my - 6, 2, 12);
      } else {                                // 采音点
        c.fillStyle = '#3a6f96';
        c.fillRect(x, my - 3, 1, 6);
      }
    }
    // 播放头
    const px = this.X(this.playT);
    c.strokeStyle = '#f85149'; c.lineWidth = 1.5;
    c.beginPath(); c.moveTo(px, 0); c.lineTo(px, H); c.stroke();
    // ★★ 换手押上色（`docs/59`）：在标记行**上方**单独一条 「霓虹」 记号 ——
    //   白边 + 黑芯，视觉上就是游戏里那对颜色（Glow 黑底白边 + Neon）。
    for (const m of this.hs) {
      const x = this.X(m.t);
      if (x < -6 || x > W) continue;
      c.fillStyle = '#ffffff'; c.fillRect(x - 3, my - 20, 7, 7);
      c.fillStyle = '#000000'; c.fillRect(x - 2, my - 19, 5, 5);
      c.strokeStyle = '#9aa7b4'; c.lineWidth = 1;
      c.strokeRect(x - 3.5, my - 20.5, 8, 8);
    }
    // ★★ 算法轨道调度（`docs/60`）：更上面一条 —— 涟漪环（向上三角）/ 半径切换（菱形）
    for (const a of this.ap) {
      const x = this.X(a.t);
      if (x < -8 || x > W) continue;
      if (a.kind === 'ripple') {
        c.fillStyle = '#7ee787';
        c.beginPath();
        c.moveTo(x, my - 30); c.lineTo(x - 4, my - 22); c.lineTo(x + 4, my - 22);
        c.closePath(); c.fill();
      } else {
        c.fillStyle = a.scale >= 200 ? '#ffa657' : '#58a6ff';
        c.beginPath();
        c.moveTo(x, my - 30); c.lineTo(x - 3.5, my - 26); c.lineTo(x, my - 22);
        c.lineTo(x + 3.5, my - 26); c.closePath(); c.fill();
      }
    }
  }

  _bind() {
    const cv = this.cv;
    cv.addEventListener('wheel', (e) => {
      e.preventDefault();
      const t = this.T(e.offsetX), k = e.deltaY < 0 ? 1.12 : 1 / 1.12;
      this.per = Math.max(0.002, Math.min(2, this.per * k));
      this.t0 = t - (e.offsetX / this.per);
      this.draw();
    }, { passive: false });

    cv.addEventListener('pointerdown', (e) => {
      if (this.hooks.frozen && this.hooks.frozen()) return;
      const hx = this.hitHandle(e.offsetX, e.offsetY);
      if (e.button === 1) {                        // 中键 = 平移
        const x0 = e.clientX, t0 = this.t0;
        this.drag = { pan: true };
        const mv = (ev) => { this.t0 = t0 - (ev.clientX - x0) / this.per; this.draw(); };
        const up = () => { this.drag = null;
          window.removeEventListener('pointermove', mv);
          window.removeEventListener('pointerup', up); };
        window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up);
        return;
      }
      if (hx) {                                    // 拖段边界 = 改时间
        const s = hx.seg, edge = hx.edge, t0 = this.t0, per = this.per;
        const s0 = s.t0, s1 = s.t1;
        const mv = (ev) => {
          const dt = (ev.clientX - e.clientX) / per;
          if (edge === 'l') s.t0 = Math.max(0, Math.min(s1 - 200, s0 + dt));
          else s.t1 = Math.min(this.dur, Math.max(s.t0 + 200, s1 + dt));
          this.draw();
        };
        const up = () => {
          this.hooks.onEditRanges && this.hooks.onEditRanges(s.ref, s.t0, s.t1);
          this.drag = null;
          window.removeEventListener('pointermove', mv);
          window.removeEventListener('pointerup', up);
        };
        window.addEventListener('pointermove', mv); window.addEventListener('pointerup', up);
        return;
      }
      const s = this.segs.find((g) => e.offsetX >= this.X(g.t0) - 3
        && e.offsetX <= this.X(g.t1) + 3 && e.offsetY < 34);
      if (s) { this.sel = this.segs.indexOf(s); this.draw();
        this.hooks.onSelectRange && this.hooks.onSelectRange(s.ref); return; }
      // 空白 = 定位播放头（并交给 hooks 去 seek）
      const t = Math.max(0, this.T(e.offsetX));
      this.playT = t; this.draw();
      this.hooks.onSeek && this.hooks.onSeek(t);
    });
  }

  hitHandle(x, y) {
    if (y < 4 || y > 34) return null;
    for (const s of this.segs) {
      if (Math.abs(x - this.X(s.t0)) <= 4) return { seg: s, edge: 'l' };
      if (Math.abs(x - this.X(s.t1)) <= 4) return { seg: s, edge: 'r' };
    }
    return null;
  }

  /** 全曲适配（双击 / 初始化） */
  fitAll() { this.t0 = 0; this.per = Math.max(0.002, this.cv.clientWidth / this.dur); this.draw(); }
}
