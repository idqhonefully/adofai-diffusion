#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OSN1 7 通道重训流水线 - 只读状态监控服务 (stdlib only).
   绑定 127.0.0.1:8799, 页面每 3s 拉一次 /api/status 自动刷新(不整页刷新, canvas 持续绘制),
   读取日志/缓存目录/GPU, 不修改任何东西.
"""
import http.server, socketserver, json, os, subprocess, sys, re, time

CACHE_DIR   = r"<REPO>\audio-sep\bsr_train_cache"
PRE_LOG    = r"<REPO>\audio-sep\bsr_precompute_7ch.log"
TRAIN_LOG  = r"<REPO>\audio-sep\bsr_train.log"
TARGET     = 264
EPOCHS     = 80
BATCHES    = 979          # 每 epoch 批数(实测)
TOTAL_STEPS = EPOCHS * BATCHES
PORT       = 8799
STEMS      = ["bass", "drums", "other", "vocals", "guitar", "piano", "full"]

BATCH_RE = re.compile(r"\[epoch\s+(\d+)/(\d+)\]\s+batch\s+(\d+)/(\d+)\s+loss=([0-9.]+)")
BEST_RE  = re.compile(r"\[best\]\s+loss=([0-9.]+)")

def run(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=8).stdout.strip()
    except Exception:
        return ""

def gpu():
    out = run(r'nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu --format=csv,noheader')
    if not out:
        return {"mem_used": "?", "mem_total": "?", "util": "?"}
    parts = [p.strip() for p in out.split(',')]
    return {"mem_used": parts[0], "mem_total": parts[1], "util": parts[2]}

def count_cache():
    try:
        return len([f for f in os.listdir(CACHE_DIR) if f.endswith('.npy')])
    except Exception:
        return 0

def tail(path, n=25):
    if not os.path.exists(path):
        return []
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.read().splitlines()
        return lines[-n:]
    except Exception:
        return []

def count_in_file(path, substr):
    if not os.path.exists(path):
        return 0
    try:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            return sum(1 for ln in f if substr in ln)
    except Exception:
        return 0

# ---- 训练曲线解析(带 mtime 缓存) ----
_curve_cache = {"mtime": 0, "data": None}
_rate = {"prev_lines": 0, "prev_t": 0.0, "bps": 0.0}

def train_curve():
    if not os.path.exists(TRAIN_LOG):
        return None
    mtime = os.path.getmtime(TRAIN_LOG)
    if _curve_cache["mtime"] == mtime and _curve_cache["data"] is not None:
        return _curve_cache["data"]
    try:
        with open(TRAIN_LOG, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
    except Exception:
        return None

    cur_epoch = None
    raw = []                 # (x_frac_epoch, loss)
    epoch_losses = {}        # ep -> [loss]
    best_loss = None
    best_epoch = None
    last = None              # (ep, batch, loss)

    for ln in lines:
        m = BATCH_RE.search(ln)
        if m:
            ep = int(m.group(1)); bt = int(m.group(3)); lo = float(m.group(5))
            cur_epoch = ep
            gs = (ep - 1) * BATCHES + (bt - 1)
            x = gs / float(BATCHES) + 1.0      # 1.0 .. 81.0
            raw.append((x, lo))
            epoch_losses.setdefault(ep, []).append(lo)
            last = (ep, bt, lo)
            continue
        m2 = BEST_RE.search(ln)
        if m2:
            bl = float(m2.group(1))
            if best_loss is None or bl < best_loss:
                best_loss = bl
                best_epoch = cur_epoch

    # 降采样原始点(最多 ~500 个, 避免图太密)
    if len(raw) > 500:
        step = (len(raw) + 499) // 500
        raw_ds = raw[::step]
        if raw_ds[-1] != raw[-1]:
            raw_ds.append(raw[-1])
    else:
        raw_ds = raw

    # 每 epoch 均值(趋势/回归曲线)
    epoch_x = sorted(epoch_losses.keys())
    epoch_mean = [sum(epoch_losses[e]) / len(epoch_losses[e]) for e in epoch_x]

    # 速度 / ETA
    now = time.time()
    n_lines = len(lines)
    if _rate["prev_t"] > 0 and n_lines > _rate["prev_lines"]:
        dt = now - _rate["prev_t"]
        dl = n_lines - _rate["prev_lines"]
        if dt > 0 and dl > 0:
            inst = dl / dt
            _rate["bps"] = _rate["bps"] * 0.7 + inst * 0.3 if _rate["bps"] > 0 else inst
    _rate["prev_lines"] = n_lines
    _rate["prev_t"] = now

    done_steps = (last[0] - 1) * BATCHES + last[1] if last else 0
    eta_sec = (TOTAL_STEPS - done_steps) / _rate["bps"] if _rate["bps"] > 0.01 else None

    data = {
        "raw_x": [r[0] for r in raw_ds],
        "raw_y": [r[1] for r in raw_ds],
        "epoch_x": epoch_x,
        "epoch_mean": epoch_mean,
        "current_epoch": last[0] if last else None,
        "current_batch": last[1] if last else None,
        "current_loss": last[2] if last else None,
        "best_loss": best_loss,
        "best_epoch": best_epoch,
        "n_batches": len(raw),
        "bps": round(_rate["bps"], 2),
        "eta_sec": int(eta_sec) if eta_sec else None,
        "epoch_table": [(e, round(epoch_losses[e][-1], 4),
                         round(sum(epoch_losses[e]) / len(epoch_losses[e]), 4))
                        for e in epoch_x[-12:]],   # 最近 12 个 epoch 的 [末批loss, 均值]
    }
    _curve_cache["mtime"] = mtime
    _curve_cache["data"] = data
    return data

def status():
    cache = count_cache()
    g = gpu()
    pre_ok = count_in_file(PRE_LOG, '[ok]')
    pre_fail = count_in_file(PRE_LOG, 'FAIL') + count_in_file(PRE_LOG, 'WATCHDOG')
    train_exists = os.path.exists(TRAIN_LOG)
    cv = train_curve() if train_exists else None

    if cache < TARGET and not train_exists:
        phase = "1/分离(预计算)进行中"
    elif cache >= TARGET and not train_exists:
        phase = "2/分离完成, 等待训练自动启动"
    elif train_exists:
        phase = "3/训练中"
    else:
        phase = "?"

    return {
        "phase": phase,
        "cache_count": cache,
        "target": TARGET,
        "gpu": g,
        "pre_ok": pre_ok, "pre_fail": pre_fail,
        "stems": STEMS,
        "epochs_cfg": EPOCHS,
        "batches_cfg": BATCHES,
        "total_steps": TOTAL_STEPS,
        "train": cv,
        "pre_log_tail": tail(PRE_LOG, 12),
        "train_log_tail": tail(TRAIN_LOG, 12) if train_exists else [],
    }

HTML = """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>OSN1 7ch 重训监控</title>
<style>
 body{background:#1b1d23;color:#e6e6e6;font:13px/1.5 Consolas,monospace;margin:0;padding:10px}
 h1{font-size:15px;margin:0 0 8px;color:#7fd1ff}
 .topbar{display:flex;flex-wrap:wrap;gap:8px 20px;align-items:center;background:#262a33;border:1px solid #333a47;border-radius:8px;padding:9px 12px;margin-bottom:10px}
 .chip{display:flex;flex-direction:column;line-height:1.25}
 .k{color:#9aa4b2;font-size:11px}.v{color:#fff;font-weight:bold}
 .big{font-size:19px;color:#7CFFB2}
 .bar{flex:1;height:10px;background:#333a47;border-radius:5px;overflow:hidden;min-width:160px;margin-top:5px}
 .fill{height:100%;background:linear-gradient(90deg,#4CC2FF,#7CFFB2)}
 .main{display:flex;gap:10px;flex-wrap:wrap}
 .panel{background:#262a33;border:1px solid #333a47;border-radius:8px;padding:10px}
 .col-left{flex:1 1 340px;min-width:300px}
 .col-right{flex:1.3 1 380px;min-width:320px;display:flex;flex-direction:column}
 .grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:7px 14px;margin-top:4px}
 .legend{margin:2px 0 6px;font-size:11px}
 canvas{width:100%;flex:1;min-height:230px;display:block;background:#0e1014;border:1px solid #2a2f3a;border-radius:6px}
 .logs{display:flex;gap:10px;margin-top:10px;flex-wrap:wrap}
 .logbox{flex:1 1 340px;min-width:300px}
 pre{background:#0e1014;border:1px solid #2a2f3a;border-radius:6px;padding:8px;max-height:15vh;overflow:auto;color:#b9c4d4;white-space:pre-wrap;margin:0;font-size:11px}
 .tag{display:inline-block;padding:2px 9px;border-radius:12px;background:#4CC2FF22;color:#7fd1ff;border:1px solid #4CC2ff55;font-size:12px}
 .warn{color:#FF99A4}.ok{color:#7CFFB2}
 table{border-collapse:collapse;margin-top:6px;font-size:11px;width:100%}
 th,td{border:1px solid #333a47;padding:2px 6px;text-align:right}
 th{color:#9aa4b2;font-weight:normal;background:#20242c}
 .dot{display:inline-block;width:10px;height:3px;vertical-align:middle;margin-right:5px}
</style></head><body>
<h1>🎛 OSN1 7 通道重训流水线 · 只读监控</h1>
<div id="app">加载中…</div>
<script>
async function tick(){
  try{
    const r = await fetch('/api/status'); const s = await r.json();
    const pct = Math.round(s.cache_count/s.target*100);
    let h = '';
    // 顶栏(左右横排)
    h += '<div class="topbar"><span class="tag">'+s.phase+'</span>';
    h += '<div class="chip"><div class="k">分离缓存</div><div class="big">'+s.cache_count+'/'+s.target+' ('+pct+'%)</div></div>';
    h += '<div class="chip"><div class="k">GPU 显存</div><div class="v">'+s.gpu.mem_used+' / '+s.gpu.mem_total+'</div></div>';
    h += '<div class="chip"><div class="k">GPU 利用率</div><div class="v">'+s.gpu.util+'</div></div>';
    h += '<div class="chip"><div class="k">分离 完成/失败</div><div class="v"><span class="ok">'+s.pre_ok+'</span> / <span class="'+(s.pre_fail?'warn':'')+'">'+s.pre_fail+'</span></div></div>';
    h += '<div class="chip"><div class="k">输入通道(7)</div><div class="v" style="font-size:11px">'+s.stems.join('·')+'</div></div>';
    h += '</div>';

    const t = s.train;
    if(t){
      h += '<div class="main">';
      // 左列: 训练配置 + 进度 + 表
      h += '<div class="panel col-left"><div class="k">训练配置 / 进度</div>';
      h += '<div class="grid3">';
      h += '<div><div class="k">计划</div><div class="v">'+s.epochs_cfg+' ep × '+s.batches_cfg+' = '+s.total_steps+'</div></div>';
      h += '<div><div class="k">当前 epoch</div><div class="v">'+t.current_epoch+'/'+s.epochs_cfg+'</div></div>';
      h += '<div><div class="k">当前 batch</div><div class="v">'+t.current_batch+'/'+s.batches_cfg+'</div></div>';
      h += '<div><div class="k">当前 loss</div><div class="v">'+fmt(t.current_loss)+'</div></div>';
      h += '<div><div class="k">最优 best</div><div class="v ok">'+fmt(t.best_loss)+' @'+t.best_epoch+'</div></div>';
      h += '<div><div class="k">已训练 batch</div><div class="v">'+t.n_batches+'</div></div>';
      h += '<div><div class="k">速度</div><div class="v">'+t.bps+'/s</div></div>';
      h += '<div><div class="k">预计剩余</div><div class="v">'+eta(t.eta_sec)+'</div></div>';
      h += '<div><div class="k">进度</div><div class="bar"><div class="fill" style="width:'+Math.round(t.current_epoch/s.epochs_cfg*100)+'%"></div></div></div>';
      h += '</div>';
      h += '<div class="k" style="margin-top:8px">最近 epoch 均值 (末批 / 均值)</div><table><tr><th>ep</th>';
      for(const e of t.epoch_table.map(x=>x[0])) h += '<th>'+e+'</th>';
      h += '</tr><tr><td class="k">末批</td>';
      for(const x of t.epoch_table) h += '<td>'+fmt(x[1])+'</td>';
      h += '</tr><tr><td class="k">均值</td>';
      for(const x of t.epoch_table) h += '<td>'+fmt(x[2])+'</td>';
      h += '</tr></table>';
      h += '</div>';
      // 右列: 折线图
      h += '<div class="panel col-right"><div class="legend"><span><i class="dot" style="background:#4CC2FF"></i>每 batch loss</span><span><i class="dot" style="background:#7CFFB2"></i>每 epoch 均值(回归曲线)</span></div>';
      h += '<canvas id="chart" width="760" height="300"></canvas></div>';
      h += '</div>';
    }
    // 日志(左右并排)
    h += '<div class="logs">';
    h += '<div class="panel logbox"><div class="k">训练日志 (最新)</div><pre>'+s.train_log_tail.join('\\n')+'</pre></div>';
    h += '<div class="panel logbox"><div class="k">预计算日志 (最新)</div><pre>'+s.pre_log_tail.join('\\n')+'</pre></div>';
    h += '</div>';
    document.getElementById('app').innerHTML = h;
    document.querySelectorAll('.logbox pre').forEach(function(p){ p.scrollTop = p.scrollHeight; });
    if(t) drawChart(document.getElementById('chart'), t, s.epochs_cfg);
  }catch(e){ document.getElementById('app').innerText='读取失败: '+e; }
}
function fmt(v){ return (v==null)?'—':(typeof v==='number'? v.toFixed(4): v); }
function eta(s){ if(s==null) return '—'; const h=Math.floor(s/3600), m=Math.floor((s%3600)/60); return (h?h+'h':'')+m+'m'; }
function drawChart(c, t, maxEp){
  const W = c.clientWidth || 760, H = c.clientHeight || 300;
  if(c.width !== W) c.width = W;
  if(c.height !== H) c.height = H;
  const ctx = c.getContext('2d'); ctx.clearRect(0,0,W,H);
  const pl=48, pr=12, pt=10, pb=26;
  const x0=pl, x1=W-pr, y0=pt, y1=H-pb;
  const allY = t.raw_y.concat(t.epoch_mean);
  let yMax = Math.max(0.1, Math.max.apply(null, allY));
  yMax = Math.ceil(yMax*10)/10;
  const xMin=1, xMax=Math.max(maxEp, t.current_epoch||maxEp);
  const X=v=> x0 + (v-xMin)/(xMax-xMin)*(x1-x0);
  const Y=v=> y1 - (v-0)/(yMax-0)*(y1-y0);
  ctx.strokeStyle='#2a2f3a'; ctx.fillStyle='#7e8a9a'; ctx.font='11px Consolas,monospace'; ctx.lineWidth=1;
  const yt=4; for(let i=0;i<=yt;i++){ const v=yMax*i/yt; const y=Y(v);
    ctx.beginPath(); ctx.moveTo(x0,y); ctx.lineTo(x1,y); ctx.stroke();
    ctx.fillText(v.toFixed(2), 6, y+3); }
  ctx.textAlign='center';
  for(let e=xMin;e<=xMax;e+=10){ const x=X(e);
    ctx.strokeStyle='#22272f'; ctx.beginPath(); ctx.moveTo(x,y0); ctx.lineTo(x,y1); ctx.stroke();
    ctx.fillStyle='#7e8a9a'; ctx.fillText('ep'+e, x, y1+16); }
  ctx.textAlign='left';
  ctx.strokeStyle='rgba(76,194,255,0.35)'; ctx.lineWidth=1; ctx.beginPath();
  for(let i=0;i<t.raw_x.length;i++){ const x=X(t.raw_x[i]), y=Y(t.raw_y[i]); i?ctx.lineTo(x,y):ctx.moveTo(x,y); }
  ctx.stroke();
  ctx.strokeStyle='#7CFFB2'; ctx.lineWidth=2.4; ctx.beginPath();
  for(let i=0;i<t.epoch_x.length;i++){ const x=X(t.epoch_x[i]), y=Y(t.epoch_mean[i]); i?ctx.lineTo(x,y):ctx.moveTo(x,y); }
  ctx.stroke();
  ctx.fillStyle='#7CFFB2';
  for(let i=0;i<t.epoch_x.length;i++){ const x=X(t.epoch_x[i]), y=Y(t.epoch_mean[i]); ctx.beginPath(); ctx.arc(x,y,2.6,0,7); ctx.fill(); }
  if(t.best_epoch!=null && t.best_loss!=null){ const x=X(t.best_epoch), y=Y(t.best_loss);
    ctx.strokeStyle='#FFD479'; ctx.setLineDash([4,3]); ctx.beginPath(); ctx.moveTo(x0,y); ctx.lineTo(x1,y); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle='#FFD479'; ctx.fillText('best '+t.best_loss.toFixed(4)+' @ep'+t.best_epoch, x0+6, y-5); }
}
tick(); setInterval(tick, 3000);
</script></body></html>"""

class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.startswith('/api/status'):
            self.send_response(200); self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Access-Control-Allow-Origin','*'); self.end_headers()
            self.wfile.write(json.dumps(status(), ensure_ascii=False).encode('utf-8'))
        else:
            self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8')
            self.end_headers(); self.wfile.write(HTML.encode('utf-8'))

class S(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

if __name__ == '__main__':
    print(f"monitor on http://127.0.0.1:{PORT}", flush=True)
    S(('127.0.0.1', PORT), H).serve_forever()
