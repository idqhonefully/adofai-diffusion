/**
 * 钢琴卷帘（对等旧 `ui/piano_roll.py`）。
 *
 * 横轴时间(ms)、纵轴音高。常量照抄：
 *   背景 #12161c；节拍线仅在 view_span < 120000 时画；音高线只在 pitch%12==0 处画；
 *   音符 #3d6ea8 / 边 #6fa8dc；采音点 #ff8a3d 虚线 + 底部三角（半宽 7、高 11.2）；
 *   播放头 #ffd166 宽 2；view_span 钳 [80, 600000]。
 * 鼠标：Ctrl+滚轮 = 音高范围 ±1 半音（钳 0..127）；Shift+滚轮 = 平移 d/1200×span；
 *   普通滚轮 = 以鼠标处时间为锚点缩放（0.8 放大 / 1.25 缩小）；左键拖动平移；
 *   中键 = seek 到该时刻。
 */
const COLOR = {
  bg: '#12161c', gridBeat: 'rgba(255,255,255,0.15)',
  grid: 'rgba(255,255,255,0.06)', note: '#3d6ea8', noteEdge: '#6fa8dc',
  onset: 'rgba(255,138,61,0.35)', onsetTri: '#ff8a3d',
  play: '#ffd166', hud: '#8b949e',
};

export class PianoRoll {
  constructor(canvas) {
    this.cv = canvas;
    this.ctx = canvas.getContext('2d');
    this.notes = [];
    this.onsets = [];
    this.total = 1000;
    this.beats = [];
    this.t0 = 0; this.span = 1000;
    this.plo = 21; this.phi = 108;
    this.play = 0;
    this.hoverMs = null;
    this._drag = null;
    this.bindMouse();
  }

  setData(payload) {
    this.notes = (payload.notes || []).slice();
    this.onsets = (payload.hit || []).slice();
    this.total = Math.max(1000, payload.total_ms || 1000);
    this.beats = payload.beat_grid || [];
    this.t0 = 0;
    this.span = Math.max(80, Math.min(600000, this.total));
    const ps = this.notes.map((n) => n.p);
    if (ps.length) {
      this.plo = Math.max(0, Math.min(...ps) - 2);
      this.phi = Math.min(127, Math.max(...ps) + 2);
    }
  }

  setPlayhead(ms) {
    this.play = ms;
  }

  fitAll() {
    this.t0 = 0;
    this.span = Math.max(80, Math.min(600000, this.total));
  }

  focusTo(ms, spanMs) {
    this.span = Math.max(80, Math.min(600000, spanMs));
    this.t0 = Math.max(0, ms - this.span * 0.35);
  }

  x(ms, w) { return (ms - this.t0) / this.span * w; }

  y(p, h) {
    const lo = this.plo; const hi = Math.max(this.plo + 1, this.phi);
    return h - (p - lo) / (hi - lo) * h;
  }

  draw() {
    const cv = this.cv; const ctx = this.ctx;
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth; const h = cv.clientHeight;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
      cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = COLOR.bg; ctx.fillRect(0, 0, w, h);

    if (this.span < 120000) {
      ctx.strokeStyle = COLOR.gridBeat; ctx.lineWidth = 1;
      for (const b of this.beats) {
        const px = this.x(b, w);
        if (px < 0 || px > w) continue;
        ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, h); ctx.stroke();
      }
    }
    ctx.strokeStyle = COLOR.grid; ctx.lineWidth = 1;
    for (let p = this.plo; p <= this.phi; p++) {
      if (p % 12 !== 0) continue;
      const py = this.y(p, h);
      ctx.beginPath(); ctx.moveTo(0, py); ctx.lineTo(w, py); ctx.stroke();
    }

    // 原始音符
    const hi = Math.max(this.plo + 1, this.phi);
    const nh = Math.max(3.0, h / (hi - this.plo) * 0.85);
    for (const n of this.notes) {
      const px = this.x(n.t, w);
      const pw = Math.max(1.2, this.x(n.t + Math.max(n.d, 1), w) - px);
      if (px + pw < 0 || px > w) continue;
      ctx.fillStyle = COLOR.note;
      ctx.strokeStyle = COLOR.noteEdge;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.rect(px, this.y(n.p, h) - nh / 2, pw, nh);
      ctx.fill(); ctx.stroke();
    }

    // 采音点：虚线 + 底部三角
    ctx.strokeStyle = COLOR.onset;
    ctx.setLineDash([3, 3]);
    ctx.lineWidth = 1;
    for (const t of this.onsets) {
      const px = this.x(t, w);
      if (px < 0 || px > w) continue;
      ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, h); ctx.stroke();
    }
    ctx.setLineDash([]);
    ctx.fillStyle = COLOR.onsetTri;
    for (const t of this.onsets) {
      const px = this.x(t, w);
      if (px < 0 || px > w) continue;
      ctx.beginPath();
      ctx.moveTo(px, h); ctx.lineTo(px - 7, h - 11.2); ctx.lineTo(px + 7, h - 11.2);
      ctx.closePath(); ctx.fill();
    }

    // 播放头
    const px = this.x(this.play, w);
    if (px >= 0 && px <= w) {
      ctx.strokeStyle = COLOR.play; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, h); ctx.stroke();
    }

    ctx.font = '12px Consolas, "Microsoft YaHei UI", monospace';
    ctx.fillStyle = COLOR.hud;
    ctx.fillText(`音符 ${this.notes.length}   采音点 ${this.onsets.length}   `
      + `视窗 ${(this.t0 / 1000).toFixed(1)}→${((this.t0 + this.span) / 1000).toFixed(1)}s   `
      + `音高 ${this.plo}-${this.phi}`, 10, 16);
    if (this.hoverMs !== null) {
      ctx.fillText(`t = ${Math.round(this.hoverMs)} ms`, 10, h - 8);
    }
  }

  bindMouse() {
    const cv = this.cv;
    cv.addEventListener('wheel', (e) => {
      e.preventDefault();
      const d = e.deltaY || e.deltaX;
      if (e.ctrlKey) {
        this.plo = Math.max(0, this.plo - (d < 0 ? 1 : -1));
        this.phi = Math.min(127, this.phi + (d < 0 ? 1 : -1));
      } else if (e.shiftKey) {
        this.t0 = Math.max(0, this.t0 - d / 1200 * this.span);
      } else {
        const rect = cv.getBoundingClientRect();
        const mx = e.clientX - rect.left;
        const anchor = this.t0 + mx / Math.max(1, cv.clientWidth) * this.span;
        this.span = Math.max(80, Math.min(600000, d < 0 ? this.span * 0.8 : this.span * 1.25));
        this.t0 = Math.max(0, anchor - mx / Math.max(1, cv.clientWidth) * this.span);
      }
      this.draw();
    }, { passive: false });

    cv.addEventListener('mousedown', (e) => {
      const rect = cv.getBoundingClientRect();
      if (e.button === 1) {
        const ms = this.t0 + (e.clientX - rect.left) / Math.max(1, cv.clientWidth) * this.span;
        if (this.onSeek) this.onSeek(Math.max(0, ms));
        e.preventDefault();
        return;
      }
      if (e.button === 0) this._drag = { x: e.clientX, t0: this.t0 };
    });
    cv.addEventListener('mousemove', (e) => {
      const rect = cv.getBoundingClientRect();
      this.hoverMs = Math.max(0, this.t0 + (e.clientX - rect.left)
        / Math.max(1, cv.clientWidth) * this.span);
      if (this._drag) {
        const dx = e.clientX - this._drag.x;
        this.t0 = Math.max(0, this._drag.t0 - dx / Math.max(1, cv.clientWidth) * this.span);
      }
      this.draw();
    });
    cv.addEventListener('mouseup', () => { this._drag = null; });
    cv.addEventListener('mouseleave', () => {
      this._drag = null; this.hoverMs = null; this.draw();
    });
  }
}
