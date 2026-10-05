# -*- coding: utf-8 -*-
"""「启动并桥接」：从我们的 UI 一键把 BDG 宿主拉起来并接上桥。

用户在侧栏点一下，这里做四件事：

    ① `node tools/host.js link`      把 `bridge_plugin/` 链接进宿主的插件目录（毫秒级）
    ② `node tools/host.js start`     **不阻塞**地起宿主（`electron-vite dev` + CDP 9222）
    ③ 等 CDP 端口通 + 等插件的 `__adocBridge` 出现
    ④ `node tools/_cdp.js "<js>"`    把我们的 `ws://…/ws?token=…` 写进宿主的
                                     `localStorage['adoc.bridge.url']` 并调 `session.connect()`

## 为什么第 ④ 步要绕 CDP

宿主插件是**只读、无文件系统权限**的 renderer 插件，它拿不到我们的端口（我们每次启动
端口都可能不同）。原设计是「把连接串贴进 BDG 面板」—— 能用，但不是一键。

而我们**自己起的**宿主带了 `--remote-debugging-port=9222`，那个调试通道是我们开的
⇒ 从那儿把地址推进去是顺理成章的。这条链路在真机上验过（见 `docs/40`）。

**它不碰宿主源码**，只写宿主的 `localStorage`（等价于点一下插件面板里的「连接」）。

## 找不到路时要说什么

* 宿主没装 ⇒ 明确说「先跑 host:fetch」，**不假装成功**；
* CDP 没起来（比如宿主是别人手动起的、没带调试端口）⇒ 明确说「请手动把连接串贴进面板」，
  并把连接串返回给 UI（UI 上本来就有复制按钮）；
* 插件没加载 ⇒ 说「插件可能没启用」，并给出 `host:link` 的提示。
"""

from __future__ import annotations

import json
import os
import subprocess
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(_HERE)
NODE = "node"
HOST_JS = os.path.join("tools", "host.js")
CDP_JS = os.path.join("tools", "_cdp.js")
CDP_PORT = 9222
LS_KEY = "adoc.bridge.url"

# ② 起宿主后等它到 CDP 通，给足超时（vite 首次会做依赖预打包，实测 ~20s）
WAIT_CDP_S = 60.0
# ③ CDP 通了之后等插件加载出来（renderer 就绪 + 插件 activate）
WAIT_PLUGIN_S = 25.0
# 轮询间隔（测试会把它调小以验「真的会重试」）
RETRY_S = 1.5


class HostCtl:
    def __init__(self, root: str = ROOT, log=None):
        self.root = root
        self.log = log or (lambda msg: None)
        self.last_error = ""

    # ------------------------------------------------------------ 跑 node
    def _node(self, args: list, timeout: float = 30.0) -> tuple:
        """跑一个 node 子命令 → `(code, stdout, stderr)`。

        ★ 不套 shell：argv 直接给，连接串里的 `?` / `&` 就不会被解释。
        """
        cmd = [NODE, HOST_JS] + list(args)
        try:
            p = subprocess.run(cmd, cwd=self.root, capture_output=True, timeout=timeout,
                               env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        except FileNotFoundError:
            return 127, "", "找不到 node"
        except subprocess.TimeoutExpired:
            return 124, "", "超时 %.0fs" % timeout
        out = (p.stdout or b"").decode("utf-8", "replace")
        err = (p.stderr or b"").decode("utf-8", "replace")
        return p.returncode, out, err

    def _cdp(self, expr: str, target: str = "main", timeout: float = 25.0) -> tuple:
        try:
            p = subprocess.run([NODE, CDP_JS, expr, "--target", target],
                               cwd=self.root, capture_output=True, timeout=timeout,
                               env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        except subprocess.TimeoutExpired:
            return 124, "", "CDP 超时"
        return (p.returncode, (p.stdout or b"").decode("utf-8", "replace"),
                (p.stderr or b"").decode("utf-8", "replace"))

    # ------------------------------------------------------------ 状态
    def tool_present(self) -> bool:
        """`tools/host.js` 在不在。

        ★★ 打包版**不带**它（`extraResources` 里没有 `tools/`）—— 因为宿主联动依赖
        「npm / 仓库 / 600MB 下载」，这些对拿到 zip 的人来说**都不存在**。
        所以这里必须先问一句：不在就别去 `npm run host:fetch`，
        那是**开发者提示**，漏到用户界面上就是死胡同（真机踩过）。
        """
        return os.path.exists(os.path.join(self.root, HOST_JS))

    def state(self, timeout: float = 20.0) -> dict:
        """`host.js state` → dict（拿不到就给一份「什么都不知道」的）。"""
        if not self.tool_present():
            return {"present": False, "deps": False, "pid": 0, "pid_alive": False,
                    "cdp_up": False, "port": CDP_PORT, "tool": False,
                    "reason": "packaged", "error": ""}
        code, out, err = self._node(["state"], timeout=timeout)
        for line in reversed((out or "").strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    j = json.loads(line)
                    j["tool"] = True
                    return j
                except ValueError:
                    continue
        self.last_error = err.strip() or ("host.js state 退出码 %d" % code)
        return {"present": False, "deps": False, "pid": 0, "pid_alive": False,
                "cdp_up": False, "port": CDP_PORT, "tool": True,
                "error": self.last_error}

    def link(self) -> dict:
        code, out, err = self._node(["link"], timeout=30.0)
        notes = [ln for ln in (out or "").splitlines() if ln.strip().startswith(("[!", "[OK]"))]
        return {"ok": code == 0, "notes": notes, "out": (out or "").strip()}

    def start_host(self) -> dict:
        code, out, err = self._node(["start"], timeout=30.0)
        r = {}
        for line in (out or "").splitlines():
            if line.strip().startswith("{"):
                try:
                    r = json.loads(line.strip())
                except ValueError:
                    pass
        return r if r else {"ok": code == 0, "errors": [err.strip()]}

    def stop_host(self) -> dict:
        code, out, err = self._node(["stop"], timeout=40.0)
        r = {}
        for line in (out or "").splitlines():
            if line.strip().startswith("{"):
                try:
                    r = json.loads(line.strip())
                except ValueError:
                    pass
        return r if r else {"ok": code == 0}

    # ------------------------------------------------------------ 注入
    def _inject_expr(self, url: str) -> str:
        """把地址写进宿主的 localStorage 并点一下「连接」。

        ★ 用 `json.dumps` 生成字符串字面量，免得连接串里的引号/反斜杠把它顶破。

        ★★ 2026-10 修：以前是「**只要连着**就返回 already」——
        插件连的是**别的** sidecar 时（换过一次端口、或有人手动填过连接串、
        或调试时临时起过一个 sidecar），`启动并桥接` 会**假装成功却什么都不做**：
        宿主那边桥看着是通的，我们 app 这边 `state.connected` 永远是 false，
        于是「投射到编辑器」直接不干活。真机踩到过一次。
        现在按**地址**认：地址不同就强制断开重连（`session.connect()` 本身会关掉旧 ws）。
        """
        ju = json.dumps(url)
        jk = json.dumps(LS_KEY)
        return (
            "(() => {"
            " const U = %s, K = %s;"
            " const b = window.__adocBridge;"
            " if (!b || !b.session) return JSON.stringify({ok:false, why:'插件没加载'});"
            " const was = String(b.session.url || '');"
            " const same = was === U;"
            " try { localStorage.setItem(K, U); } catch (e) {}"
            " b.session.url = U;"
            " if (b.session.connected && same) return JSON.stringify({ok:true, already:true});"
            " const repointed = !same && !!was;"
            " try { b.session.connect(); } catch (e) {"
            "   return JSON.stringify({ok:false, why:String(e && e.message || e), repointed:repointed});"
            " }"
            " return JSON.stringify({ok:true, already:false, repointed:repointed, from:was});"
            "})()" % (ju, jk)
        )

    def inject_url(self, url: str) -> dict:
        """让宿主机里的插件连上我们。返回 `{ok, already?, why?}`。"""
        code, out, err = self._cdp(self._inject_expr(url))
        for line in (out or "").splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except ValueError:
                    continue
        return {"ok": False, "why": (err.strip() or out.strip() or
                                     "拿不到 CDP 应答（宿主没带 --remote-debugging-port？）")}

    # ------------------------------------------------------------ 主流程
    def start_and_bridge(self, bridge_url: str, progress=None,
                         should_cancel=None) -> dict:
        """★ UI 那个按钮背后就是它。**每一步都汇报**，失败一定给原因。"""
        # ★★ 没有联动工具（打包版）⇒ **说人话**，别把 `npm run host:fetch` 甩给用户
        if not self.tool_present():
            self.log("没有 tools/host.js（便携版不带宿主联动工具）")
            return {
                "ok": False, "stage": "tool", "notes": [],
                "error": ("这个便携版**不含** BDG 宿主（它是 GPL-3.0 的独立软件，"
                          "没有随包分发）。要用编辑器联动：\n"
                          "  1) 自己装一份 Beat Data Generator\n"
                          "  2) 把本包 `resources\\bridge_plugin` 整个文件夹放进它的"
                          "插件目录（BDG 里「插件 → 打开插件文件夹」）\n"
                          "  3) 重新打开那一栏，把上面的连接串贴进「ADO 谱面桥」的地址栏"),
            }

        def step(frac, msg):
            self.log(msg)
            if progress:
                progress(frac, msg)

        def cancelled():
            return bool(should_cancel and should_cancel())

        st = self.state()
        if not st.get("present"):
            return {"ok": False, "stage": "host",
                    "error": "还没装 BDG 宿主 —— 先跑 `npm run host:fetch`"
                             "（需要代理 127.0.0.1:7897，约 600MB）",
                    "state": st}
        if not st.get("deps"):
            return {"ok": False, "stage": "deps",
                    "error": "宿主的依赖没装好（缺 electron-vite / Electron）—— 先跑 `npm run host:fetch`",
                    "state": st}

        # ① 链接插件（幂等、毫秒级）
        step(0.05, "检查插件链接…")
        lk = self.link()
        if lk.get("notes"):
            self.log("  ".join(lk["notes"]))

        # ② 起宿主（已经在跑就直接用）
        if st.get("cdp_up") or st.get("pid_alive"):
            step(0.20, "宿主已经在跑，直接用")
            started = {"ok": True, "already": True, "pid": st.get("pid")}
        else:
            step(0.15, "启动 BDG 宿主（首次预打包可能要 ~20s）…")
            started = self.start_host()
            if not started.get("ok"):
                return {"ok": False, "stage": "start",
                        "error": started.get("error") or "宿主启动失败",
                        "state": st}

        # ③ 等 CDP 通
        t0 = time.time()
        cdp_ok = False
        while time.time() - t0 < WAIT_CDP_S:
            if cancelled():
                return {"ok": False, "stage": "cancelled", "error": "已取消"}
            if (self.state(timeout=10.0) or {}).get("cdp_up"):
                cdp_ok = True
                break
            step(min(0.60, 0.20 + 0.40 * (time.time() - t0) / WAIT_CDP_S),
                 "等宿主起来…（%.0fs）" % (time.time() - t0))
            time.sleep(RETRY_S)
        if not cdp_ok:
            return {"ok": False, "stage": "cdp",
                    "error": "宿主起来了但调试端口 %d 不通 ⇒ 请手动把连接串贴进 "
                             "BDG 的插件面板（Alt+Shift+B）" % CDP_PORT,
                    "url": bridge_url, "pid": started.get("pid")}

        # ④ 等插件出现并把地址推进去
        step(0.70, "等插件加载…")
        t1 = time.time()
        last = {}
        while time.time() - t1 < WAIT_PLUGIN_S:
            if cancelled():
                return {"ok": False, "stage": "cancelled", "error": "已取消"}
            last = self.inject_url(bridge_url)
            if last.get("ok"):
                step(1.0, "已桥接" if not last.get("already") else "桥已经在连了")
                return {"ok": True, "pid": started.get("pid"),
                        "already": bool(last.get("already")), "cdp_port": CDP_PORT}
            time.sleep(RETRY_S)
        return {"ok": False, "stage": "inject",
                "error": "宿主起来了但没等到插件（%s）⇒ 在 BDG 里确认插件已启用，"
                         "或手动把连接串贴进面板" % (last.get("why") or "未知"),
                "url": bridge_url, "pid": started.get("pid")}

    def stop_and_unbridge(self) -> dict:
        r = self.stop_host()
        return {"ok": bool(r.get("ok")), "stopped": bool(r.get("stopped")),
                "pid": r.get("pid"), "note": r.get("note") or ""}
