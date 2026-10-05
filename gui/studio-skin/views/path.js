/**
 * 谱面路径（对等旧 `ui/path_view.py`）—— 主要用来「验打结」。
 *
 * 常量照抄：背景 #0f1319；轨迹线 rgba(255,255,255,0.24) 宽 1；
 *   普通格 #4fc3f7、Twirl 格 #e060c0；圆点半径 r = max(2.5, min(7.0, scale*0.6))，
 *   实际画 0.8r；贴太近判据 **< 1.75**（与 core 的 OVERLAP_R 同口径），只跟前 26 格比；
 *   当前格 #ffd166 空心圈（半径 2.2r，宽 2）。
 * 自适应缩放：scale = clamp(min((w-60)/sw, (h-60)/sh), 0.4, 200)；
 * 手动缩放钳 [0.5, 400]，滚轮因子 1.15。
 * 鼠标：滚轮缩放（Shift+滚轮 切跟随）、左键拖动平移、中键切跟随。
 * （旧视图的 `seekRequested` 声明了但从未 emit —— 这里也**不做**点击跳转，保持行为一致。）
 */
const OVERLAP_R = 1.75;
const RECENT = 26;
const COLOR = {
  bg: '#0f1319', path: 'rgba(255,255,255,0.24)',
  n: '#4fc3f7', t: '#e060c0', bad: '#ff5252', cur: '#ffd166', hud: '#8b949e',
};

export class PathView {
  constructor(canvas) {
    this.cv = canvas;
    this.ctx = canvas.getContext('2d');
    this.floors = [];
    this.entry = [];
    this.scale = 40;
    this.cx = 0; this.cy = 0;
    this.showAll = true;
    this.follow = true;
    this.cur = 0;
    this._drag = null;
    this.bindMouse();
  }

  setData(payload) {
    this.floors = payload.floors || [];
    this.entry = payload.entry || [];
    this.cur = 0;
    this.fit();
  }

  fit() {
    this.showAll = true;
  }

  setPlayhead(ms) {
    if (!this.entry.length) return;
    let lo = 0; let hi = this.entry.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (this.entry[mid] <= ms) lo = mid + 1; else hi = mid;
    }
    this.cur = Math.max(0, lo - 1);
    if (this.follow) {
      const f = this.floors[this.cur];
      if (f) { this.cx = f.x; this.cy = f.y; }
    }
  }

  transform() {
    const w = this.cv.clientWidth || 900; const h = this.cv.clientHeight || 600;
    let sc = this.scale; let cx = this.cx; let cy = this.cy;
    if (this.showAll && this.floors.length) {
      let minX = 1e9; let maxX = -1e9; let minY = 1e9; let maxY = -1e9;
      for (const f of this.floors) {
        if (f.x < minX) minX = f.x; if (f.x > maxX) maxX = f.x;
        if (f.y < minY) minY = f.y; if (f.y > maxY) maxY = f.y;
      }
      const sw = Math.max(1e-6, maxX - minX); const sh = Math.max(1e-6, maxY - minY);
      sc = Math.max(0.4, Math.min(200.0, Math.min((w - 60) / sw, (h - 60) / sh)));
      cx = (minX + maxX) / 2; cy = (minY + maxY) / 2;
    }
    return [sc, w / 2 - cx * sc, h / 2 + cy * sc];
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
    if (!this.floors.length) {
      ctx.font = '13px "Microsoft YaHei UI", sans-serif';
      ctx.fillStyle = COLOR.hud;
      ctx.fillText('（还没有谱面：加载文件后会自动生成）', 16, 26);
      return;
    }
    const [sc, ox, oy] = this.transform();
    const sx = (x) => ox + x * sc;
    const sy = (y) => oy - y * sc;

    // 轨迹
    ctx.strokeStyle = COLOR.path; ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(sx(this.floors[0].x), sy(this.floors[0].y));
    for (const f of this.floors) ctx.lineTo(sx(f.x), sy(f.y));
    ctx.stroke();

    const r = Math.max(2.5, Math.min(7.0, sc * 0.6));
    const step = Math.max(1, Math.floor(this.floors.length / 4000));
    let nTw = 0; let nBad = 0;
    for (let i = 0; i < this.floors.length; i += step) {
      const f = this.floors[i];
      const px = sx(f.x); const py = sy(f.y);
      if (f.twirl) nTw++;
      ctx.fillStyle = f.twirl ? COLOR.t : COLOR.n;
      ctx.beginPath(); ctx.arc(px, py, r * 0.8, 0, Math.PI * 2); ctx.fill();
      // 贴太近（每帧重算，只跟前 26 格比 —— 与旧视图一致）
      let bad = false;
      for (let j = Math.max(0, i - RECENT); j < i; j++) {
        const g = this.floors[j];
        if (Math.hypot(f.x - g.x, f.y - g.y) < OVERLAP_R) { bad = true; break; }
      }
      if (bad) {
        nBad++;
        ctx.strokeStyle = COLOR.bad; ctx.lineWidth = 1.4;
        ctx.beginPath(); ctx.arc(px, py, r, 0, Math.PI * 2); ctx.stroke();
      }
    }
    // 当前格
    const c = this.floors[this.cur];
    if (c) {
      ctx.strokeStyle = COLOR.cur; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(sx(c.x), sy(c.y), 2.2 * r, 0, Math.PI * 2); ctx.stroke();
    }

    ctx.font = '12px Consolas, "Microsoft YaHei UI", monospace';
    ctx.fillStyle = COLOR.hud;
    ctx.fillText(`方块 ${this.floors.length}   Twirl ${nTw}   贴太近 ${nBad}   `
      + `floor #${this.cur + 1}/${this.floors.length}   `
      + `${this.showAll ? '自适应' : '缩放 ' + this.sc.toFixed(1)}   `
      + `${this.follow ? '跟随' : '自由'}`, 10, 16);
    ctx.fillStyle = 'rgba(139,148,158,0.75)';
    ctx.fillText('滚轮=缩放 / Shift+滚轮=切跟随 / 左键拖动=平移 / 中键=切跟随 / 双击画布=自适应', 10, h - 8);
  }

  bindMouse() {
    const cv = this.cv;
    cv.addEventListener('wheel', (e) => {
      e.preventDefault();
      const d = e.deltaY || e.deltaX;
      if (e.shiftKey) { this.follow = !this.follow; } else {
        this.scale = Math.max(0.5, Math.min(400, d < 0 ? this.scale * 1.15 : this.scale / 1.15));
        this.showAll = false;
        this.follow = false;
      }
      this.draw();
    }, { passive: false });
    cv.addEventListener('mousedown', (e) => {
      if (e.button === 1) { this.follow = !this.follow; e.preventDefault(); this.draw(); return; }
      if (e.button === 0) this._drag = { x: e.offsetX, y: e.offsetY };
    });
    cv.addEventListener('mousemove', (e) => {
      if (!this._drag) return;
      const dx = e.offsetX - this._drag.x;
      const dy = e.offsetY - this._drag.y;
      const [sc] = this.transform();
      if (Math.hypot(dx, dy) > 3) {
        this.cx -= dx / sc; this.cy += dy / sc;
        this.showAll = false; this.follow = false;
        this.draw();
      }
      this._drag.x = e.offsetX; this._drag.y = e.offsetY;
    });
    cv.addEventListener('mouseup', () => { this._drag = null; });
    cv.addEventListener('dblclick', () => { this.fit(); this.draw(); });
  }
}
