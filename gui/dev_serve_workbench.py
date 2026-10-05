"""开发用：把【自有工作台】直接开在普通浏览器里预览（不经过 Mica/WebView2 外壳）。

用途：只想用眼睛看检查器长啥样（比如某个 schema 组有没有渲染出来），
      不想跑完整 audio-sep 前置转写、也不想开原生窗口时用这个。

用法：
    <REPO>\\python313\\python.exe <REPO>\\gui\\dev_serve_workbench.py
然后浏览器打开它打印的地址（默认 http://127.0.0.1:8766/workbench/index.html ）。
关掉本窗口 / Ctrl+C 即停（sidecar 与网关一起收）。

说明：
- 起的是 sidecar(8765) + 同源网关(8766)，与正常启动走的是同一套 backend，
  所以看到的就是真前端；只是没有 `window.chrome.webview`，
  `__dsh_bridge.js` 会在「宿主不在」时静默降级（打开文件/导出等原生动作不可用）。
- 没有载入谱面时，⑤d 演出等依赖层数 n_floors 的控件会显示「层数未知」，属正常。
"""

import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import backend  # noqa: E402


def main():
    print("[dev] 启动 sidecar ...", flush=True)
    backend.start()
    print("[dev] sidecar healthy = %s" % backend.is_healthy(), flush=True)

    port = backend.gateway_start()
    base = backend.gateway_base()
    print("[dev] 网关端口 = %d" % port, flush=True)
    print("[dev] 工作台地址 = %s/workbench/index.html" % base, flush=True)
    print("[dev] （sidecar 8765 / 网关 %d；关闭本窗口即全部停止）" % port, flush=True)

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        print("[dev] 收尾 ...", flush=True)
        backend.gateway_stop()
        try:
            backend.stop()
        except Exception as exc:  # noqa: BLE001
            print("[dev] sidecar 停止异常（忽略）: %r" % (exc,), flush=True)
        print("[dev] 已退出。", flush=True)


if __name__ == "__main__":
    main()
