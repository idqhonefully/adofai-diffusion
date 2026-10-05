/** sidecar HTTP 客户端：普通请求 + SSE 事件流。 */

const BASE = (window.dsh && window.dsh.base) || 'http://127.0.0.1:0';

async function req(path, opts = {}) {
  const r = await fetch(BASE + path, opts);
  let j = null;
  try {
    j = await r.json();
  } catch (_e) {
    j = { ok: false, error: `HTTP ${r.status}（响应不是 JSON）` };
  }
  if (!r.ok && j && !j.error) j.error = `HTTP ${r.status}`;
  // ★ 不能叫 status：sidecar 的 rebuild 响应里 `status` 是**状态文案**，
  //   用 HTTP 码覆盖它会把界面上的状态栏变成 "200"。所以另取名字。
  if (j) j.httpStatus = r.status;
  return j;
}

export const api = {
  base: BASE,
  get: (p) => req(p),
  post: (p, body) => req(p, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  }),
  schema: () => req('/api/schema'),
  samples: () => req('/api/samples'),
  health: () => req('/api/health'),
  load: (path) => req('/api/load', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  }),
  cancel: () => req('/api/cancel', { method: 'POST', body: '{}' }),
  derive: (state) => req('/api/derive', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ state }),
  }),
  /** ★ 自动贴合（`docs/58`）：时值体检（纯报告，不改状态） */
  tempoDiag: (state) => req('/api/tempo_diag', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ state }),
  }),
  rebuild: (state) => req('/api/rebuild', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ state }),
  }),
  capTimes: (state) => req('/api/cap_times', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ state }),
  }),
  audio: (state) => req('/api/audio', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ state }),
  }),
  /** 当前谱面 → `.adofai` JSON（喂内嵌的 ADOFAI 播放器）。 */
  levelJson: (state) => req('/api/leveljson', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ state }),
  }),
  exportTo: (state, dir) => req('/api/export', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ state, dir }),
  }),
  /** ★ BDG 桥（`docs/38`）：状态 / 投射 / 收回。 */
  bridge: (full = false) => req(`/api/bridge${full ? '?full=1' : ''}`),
  bridgeImport: (body) => req('/api/bridge/import', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  }),
  bridgeAdopt: () => req('/api/bridge/adopt', { method: 'POST', body: '{}' }),
  bridgeClear: () => req('/api/bridge/clear', { method: 'POST', body: '{}' }),
  /** ★ 收回的**轨道项目**（`docs/45`）：看一眼 / 重新落地 / 清掉。 */
  bridgeBack: () => req('/api/bridge/back'),
  bridgeBackApply: (payload) => req('/api/bridge/back/apply', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload ? { payload } : {}),
  }),
  bridgeBackClear: () => req('/api/bridge/back/clear', { method: 'POST', body: '{}' }),
  /** ★ 分段采音的自动填充：把 BDG 角色轨上的点编译成段落（`docs/38` §10）。 */
  bridgeSegments: () => req('/api/bridge/segments', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: '{}',
  }),
  /** ★ 启动并桥接（`docs/40`）：宿主状态 / 起 / 停 / 重推连接串。 */
  host: () => req('/api/host'),
  hostStart: () => req('/api/host/start', { method: 'POST', body: '{}' }),
  hostStop: () => req('/api/host/stop', { method: 'POST', body: '{}' }),
  hostInject: (body) => req('/api/host/inject', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  }),
  /** 媒体直链（给 <audio src>）。 */
  mediaUrl: (p) => `${BASE}/media?path=${encodeURIComponent(p)}`,
  /** SSE：进度 / 状态。返回关闭函数。 */
  events(onMsg) {
    const es = new EventSource(`${BASE}/api/events`);
    es.onmessage = (ev) => {
      try {
        onMsg(JSON.parse(ev.data));
      } catch (_e) { /* ignore */ }
    };
    es.onerror = () => { /* 自动重连，不打扰 */ };
    return () => es.close();
  },
};
