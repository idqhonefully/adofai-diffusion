/**
 * 4K/8K 下落式（对等旧 `ui/falling_view.py`）—— 主要用来「验采音」。
 *
 * 常量照抄：背景纵向渐变 #0b0f14→#141b23；轨道分隔线 rgba(255,255,255,0.094)、
 *   中线 0.18；切分线 0.118；音符高 NOTE_H=20、左右分色（前半轨道 #e8590c /
 *   后半 #2f7fb5，分界 = lanes//2）；判定框按下缩到 0.70 持续 180ms 回弹；
 *   lead = 1500×speed/100（speed=100 时整屏 1500ms）。
 * 旧视图**完全没有交互**（设了焦点策略但没有事件处理），这里也保持无交互。
 */
const LEAD_MS = 1500.0;
const NOTE_H = 20.0;
const PAD = 0.10;
const CAP_PRESS_MS = 180.0;
const CAP_PRESS_MIN = 0.70;
const LEFT_COLOR = '#e8590c';
const RIGHT_COLOR = '#2f7fb5';
const COLOR = {
  bg0: '#0b0f14', bg1: '#141b23',
  laneLine: 'rgba(255,255,255,0.094)', midLine: 'rgba(255,255,255,0.18)',
  beat: 'rgba(255,255,255,0.118)', judge: 'rgba(255,255,255,0.59)',
  capFill: '#1a212a', capEdge: '#56606e', hud: '#8b949e',
};

export class FallingView {
  constructor(canvas) {
    this.cv = canvas;
    this.ctx = canvas.getContext('2d');
    this.bpm = 180;
    this.notes = [];
    this.lanes = 4;
    this.speed = 100;
    this.division = 8;
    this.caps = [];
    this.time = 0;
    this._pressUntil = {};
    this._lastTime = 0;
  }

  setChart(payload, opts) {
    this.bpm = payload.bpm0 || 180;
    this.notes = (payload.falls || []).slice().sort((a, b) => a.press - b.press);
    this.lanes = (opts && opts.lanes) === 8 ? 8 : 4;
    this.speed = Math.max(5.0, (opts && opts.speed) || 100);
    const dv = (opts && opts.division) || 8;
    this.division = [4, 8, 16, 32].includes(dv) ? dv : 8;
    this.caps = payload.cap || [];
    this.time = 0;
    this._lastTime = 0;
    this._pressUntil = {};
  }

  reset() {
    this.time = 0; this._lastTime = 0; this._pressUntil = {};
  }

  setTime(ms) {
    // 跨越 press 时给判定框一个回弹
    const a = Math.min(this._lastTime, ms); const b = Math.max(this._lastTime, ms);
    for (const n of this.notes) {
      if (n.press > a && n.press <= b) {
        this._pressUntil[n.lane] = n.press + CAP_PRESS_MS;
      }
    }
    this._lastTime = ms;
    this.time = ms;
  }

  layout() {
    const w = this.cv.clientWidth || 900; const h = this.cv.clientHeight || 600;
    const capH = Math.max(10, Math.min(44, h * 0.06));
    const gap = Math.max(4, h * 0.025);
    const bottom = Math.max(4, h * 0.015);
    const judgeY = h - bottom - capH - gap;
    // ★ 轨道最宽 150px 并整体居中：旧 UI 是 `laneW = w/lanes`，
    //   宽窗口下一条轨能有 280px，音符变成大长条，观感很差。
    const laneW = Math.min(w / this.lanes, 150);
    const x0 = (w - laneW * this.lanes) / 2;
    return { w, h, capH, gap, bottom, judgeY, laneW, x0 };
  }

  draw() {
    const cv = this.cv; const ctx = this.ctx;
    const dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth; const h = cv.clientHeight;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
      cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const g = ctx.createLinearGradient(0, 0, 0, h);
    g.addColorStop(0, COLOR.bg0); g.addColorStop(1, COLOR.bg1);
    ctx.fillStyle = g; ctx.fillRect(0, 0, w, h);

    const L = this.lanes;
    const { capH, gap, bottom, judgeY, laneW, x0 } = this.layout();
    const lw = laneW;
    const lead = LEAD_MS * 100 / this.speed;
    const left = x0; const right = x0 + lw * L;

    // 轨道
    ctx.lineWidth = 1;
    for (let i = 0; i <= L; i++) {
      const px = x0 + i * lw;
      ctx.strokeStyle = (i === L / 2) ? COLOR.midLine : COLOR.laneLine;
      ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, h); ctx.stroke();
    }
    // 切分线（只画在轨道区，避免整屏横线把画面切碎）
    ctx.strokeStyle = COLOR.beat;
    for (const bt of this.caps) {
      const dy = (bt - this.time) / lead * judgeY;
      if (dy < -2 || dy > h) continue;
      ctx.beginPath(); ctx.moveTo(left, dy); ctx.lineTo(right, dy); ctx.stroke();
    }
    // 音符
    const perHand = Math.max(1, Math.floor(L / 2));
    for (const n of this.notes) {
      const y = judgeY - (n.press - this.time) / lead * judgeY;
      if (y < -NOTE_H * 2 || y > judgeY + NOTE_H * 2) continue;
      const x = x0 + n.lane * lw;
      ctx.fillStyle = n.lane < perHand ? LEFT_COLOR : RIGHT_COLOR;
      const bw = lw * (1 - 2 * PAD);
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(x + lw * PAD, y - NOTE_H / 2, bw, NOTE_H, 4);
      else ctx.rect(x + lw * PAD, y - NOTE_H / 2, bw, NOTE_H);
      ctx.fill();
    }
    // 判定线
    ctx.strokeStyle = COLOR.judge; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(left, judgeY); ctx.lineTo(right, judgeY); ctx.stroke();
    // 判定框
    for (let i = 0; i < L; i++) {
      const until = this._pressUntil[i] || -1;
      const press = this.time <= until;
      const shrink = press ? CAP_PRESS_MIN : 1.0;
      const cw = lw * (1 - 2 * PAD) * shrink;
      const chh = capH * shrink;
      const x = x0 + i * lw + (lw - cw) / 2;
      const y = h - bottom - capH - gap + (capH - chh) / 2;
      ctx.fillStyle = COLOR.capFill; ctx.fillRect(x, y, cw, chh);
      ctx.strokeStyle = COLOR.capEdge; ctx.lineWidth = 1;
      ctx.strokeRect(x, y, cw, chh);
    }
    ctx.font = '12px Consolas, "Microsoft YaHei UI", monospace';
    ctx.fillStyle = COLOR.hud;
    ctx.fillText(`${L}K  流速 ${this.speed}  ${this.division}分切分  `
      + `BPM ${this.bpm.toFixed(2)}   t=${(this.time / 1000).toFixed(2)}s   `
      + `#${this.notes.filter((n) => n.press <= this.time).length}/${this.notes.length}`,
      10, 16);
    ctx.fillStyle = 'rgba(139,148,158,0.75)';
    ctx.fillText('（此视图无交互：旧 UI 也没有）', 10, h - 8);
  }
}
