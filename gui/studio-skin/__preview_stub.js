/* __preview_stub.js —— 【仅用于浏览器里预览皮肤】Electron 正式运行时请勿加载本文件。
 *
 * Adofai-Chart-Generator的正式版靠 preload.js 用 contextBridge 注入 window.dsh（13 个方法）。
 * 在纯浏览器里跑（比如为了截图 / 对皮肤做视觉回归），没有 preload，
 * 于是这里给一份**最小替身**：补上同样的字段，全部走同一端口（location.origin），
 * 让 api.js 的 BASE 指向网关即可。**不注入任何额外 UI**，页面与 Electron 里一致。
 */
(function () {
  if (window.dsh) return;                       // Electron 里已存在 → 本文件自动失效
  const ok = () => Promise.resolve(null);
  window.dsh = {
    port: Number(location.port || 0),
    base: location.origin,                      // api.js 取它拼 fetch
    openFile: ok, openAudio: ok, openDir: ok, reveal: ok,
    info: () => Promise.resolve({
      version: '0.5.0-preview（浏览器预览）',
      port: Number(location.port || 0),
      electron: 'browser-preview',
    }),
    layoutGet: ok, layoutSet: ok,
    ready: () => {}, reportError: (m) => console.warn('[preview-stub] reportError:', m),
    onMenu: () => {}, onSidecarDown: () => {},
  };
})();
