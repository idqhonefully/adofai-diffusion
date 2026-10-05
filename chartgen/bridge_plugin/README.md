# ADO 谱面桥（BDG 插件）

把 **Beat Data Generator** 里当前工程的踩点**实时推给我们**的谱面生成器。

> **只读**。它不写回、不改你的工程、不进撤销栈、不碰宿主源码 —— 就是一个插件目录。

## 装法

`plugins.ts:46-58` 定了两处扫描根：`userData/plugins` **（以及开发模式下的 `<宿主仓库>/plugins`）**。

| 你跑的是 | 放哪 | 具体路径 |
|---|---|---|
| **开发模式**（`npm run dev` / `electron-vite dev`） | 宿主仓库根的 `plugins/`（省事，不用管 userData） | `<你的 BDG 仓库>\plugins\bridge_plugin\` |
| **安装版 / 开发模式**（都一样） | userData | `%APPDATA%\beat-data-generator\plugins\bridge_plugin\` |

> ★★ **2026-10 真机更正（以前这张表写错了）**：**安装版也是 `beat-data-generator`**，
> 不是 `Beat Data Generator`。实测：装了官方 `Beat.Data.Generator-0.2.10-setup.exe`
> 之后，宿主用的仍是 `%APPDATA%\beat-data-generator\`（我按旧表放进
> `Beat Data Generator\plugins\` ⇒ **插件一直不加载**，查了半天）。
>
> 为什么：`userData` = `app.getPath("userData")` = `%APPDATA%\<app.getName()>`，
> 而 `app.getName()` 读的是 **app 自己 package.json 的 `productName`** ——
> 上游那个字段是**空的** ⇒ 回退到 `name`（`beat-data-generator`）。
> `build.productName`（`Beat Data Generator`）只决定**安装包名 / 快捷方式 / exe 名**，
> **不参与** `getName()`。
>
> **最权威的确认**：在宿主里点 **设置 → 打开插件目录**
> （`plugins.ts` 的 `shell.openPath`），打开的就是该放的位置 —— 别照抄路径。
> 稳妥做法：**两个候选目录都放一份**（多放一份没有任何副作用）。

一条命令装（在**本工程**根跑）：

```powershell
# 装到你的 BDG 仓库（开发模式）
python tools\_bdg_install_plugin.py --dev-root "D:\path\to\beat_data_generator"

# 装到 userData（打包版）
python tools\_bdg_install_plugin.py --user

# 只看路径、不动手
python tools\_bdg_install_plugin.py
```

装完 **重载 / 重启宿主** ⇒ 顶部「插件」菜单里出现 **ADO 谱面桥**。
**卸载 = 删掉那个 `bridge_plugin` 目录。**

### ★ 推荐做法：用 `tools/host.js link`（junction，源码即生效）

上面那个 `_bdg_install_plugin.py` 是 **copy** —— 于是**改了 `renderer.js` 必须重跑
`--force` 才生效**，这是个反复踩的坑。改用 **junction** 之后源码即生效：

```powershell
node tools\host.js status          # 只看现状（装成什么样、启用了哪些）
node tools\host.js link            # 建 junction + 合并启用项（毫秒级、幂等）
node tools\host.js fetch           # 缺宿主才拉（clone + npm install + Electron 233MB）
node tools\host.js dev             # 起宿主（--remote-debugging-port=9222，可用 _cdp.js 遥控）
```

在 `app/` 里也有对应的：

```powershell
npm start            # = host:link && electron .   （start 前自动把链接刷新一遍）
npm run host:link    # 只建链接
npm run host:fetch   # 只拉宿主
npm run host:dev     # 只起宿主
```

**为什么 `link` 挂在 `start` 前面**：它**毫秒级、幂等、且只要 0 退出码**。
宿主没拉、没装、junction 建不了，都只打印 `[!]` 警告、**不挡住我们自己的 app 启动**
（那时桥面板显示「未连接」，其它功能照常）。链接每次跑也顺便自愈「工作区搬过家 ⇒
链接指向旧位置」这种情况。

### ★★ 重要更正：junction **不等于热更新**

`link` 省掉的是「**重装**插件」这一步，**不省「重载宿主页面」**。
宿主的渲染层是 Vite dev server，它**不会**热更新 `root` 之外（junction 出去）的路径。

实测（2026-10 踩到）：改完 `renderer.js` 之后继续往宿主推，宿主跑的**还是旧代码**
（回执里没有 `anchor`/`via_ms`，还在每次新建轨）；`location.reload()` 之后才对。

⇒ 改插件的正确流程：**改源码 → 在宿主里重载（Ctrl+R）→ 继续用**。

顺手补了一个体验问题：**插件现在记住地址就自动重连**（`localStorage['adoc.bridge.url']`
非空就 300ms 后自己 `connect()`）—— 以前每次重载都要人去面板点一下「连接」，
而「重载」恰好是开发中最常做的事。

**为什么要 junction 而不是拷贝**（`plugins.ts:50-58`）：

- `<userData>/plugins` 永远扫；`<宿主仓库>/plugins` **只有 `!app.isPackaged`（dev）**才扫；
- Windows 上 `fs.symlinkSync(src, dst, 'junction')` **不需要管理员**（实测），
  Node 的 `fs` 看它是普通目录，宿主无感；
- ⚠️ junction **只能同盘**。跨盘或建失败会自动**退化成拷贝**并明确说出来
  （那时又得重跑 `link --force`）；
- ⚠️ 同名目录如果是**别人的**插件（`manifest.json` 的 id 对不上），`link` **一根手指都不碰**。

启用那一步写的是 `userData/plugins-state.json`（`plugins.ts:99`：
`isEnabled = state.enabled.includes(id)`，**新插件默认不启用**）——
**合并**而不是覆盖，别的插件和别的字段都留着；文件坏了会先备份成 `.bak` 再重建。

体检：`node tools/_host_test.js`（全在临时目录里跑，不碰真宿主、不碰真 `%APPDATA%`）。

### ★★ 真机验证（junction 这一条是实测的，不是推断的）

```
① node tools/host.js link
     → 把旧拷贝换成 junction：<宿主>\plugins\bridge_plugin → <工程>\bridge_plugin
② node tools/host.js dev    （宿主起来，CDP 9222）
③ CDP 查 __adocBridge       → {"hook":"object","keys":["api","session","panel"]}
     ★ 宿主**认 junction**，插件完整加载
④ CDP 调 session.connect(我们的 ws://…:18865/ws?token=…)
     → sidecar 侧 connected=True authed=True
     ★ 链接进去的插件能跟我们的 sidecar 完成握手
⑤ CDP 重复注册角色轨类型：
     → {"ok":false,"reason":"track type \"dev.adocharter.bdg-bridge:main\" already registered"}
     ★ 四条角色轨类型**确实注册成功**，且宿主按 <api.id>:main 加了前缀（同 docs/38 §3.6）
⑥ 在 app/ 里跑 pnpm start
     → [OK] 插件已链接（junction 有效） / [OK] 插件已启用
     → [sidecar] 就绪 307ms / [renderer] ready {"port":59769,"fields":57}
     ★ 用户那条命令真的能一路走通
```

## 用法

1. 在我们这边开侧车，界面/日志里会打印一行：

   ```
   [sidecar] BDG 桥: ws://127.0.0.1:8765/ws?token=XXXXXXXX
   ```

   （也可以 `GET /api/bridge` 拿，返回 `{url, token, state}`。）

2. 在 BDG 里 `Alt+Shift+B` 打开桥面板，把那串地址粘进输入框 → 点「连接」。
   地址会记在 `localStorage["adoc.bridge.url"]`，下次自动填。

3. 状态行会变成 `● 已连接`；之后每次改动（**去抖 300ms**）自动推一次全量快照。

## 面板

```
● 已连接
[ ws://127.0.0.1:8765/ws?token=…        ]
[ 连接 ] [ 立即推一次 ]
[ ⤴ 返回数据到谱面生成器 ]          ← 新增（docs/45）
[ ⇣ 导入时间戳文件… ]                ← 新增（docs/45 §7）
发送 37 条 · 已同步 35 次
最后：已同步 6 轨 / 866 点 / 变速 2（重复拍 232）
12:01:03  握手成功（对端接受 v1）
…
```

## ★ 「返回数据到谱面生成器」（`docs/45`）

用户口径：「**工程要能回到我们的工具里面**；此时我们的工具使用的音轨**就是 BDG 里面
带时值数据的音轨**。」面板上那颗蓝按钮（菜单里也有一项，快捷键 `Alt+Shift+R`）就是它：

* 它把**有我们投送标记**（`attrs.adbIdx` / `adbRole`）的轨**整条**收回来 ——
  **一条 BDG 轨 = 我们那边的一条音轨项目**（不是按泳道键拼）；
* 用户**自己新建的轨不搬**（那是他的素材，不是我们的谱）；
* 轨上**用户新加的点**（没有标记）照搬，并单独计数；
* 每个点带 `ms`（宿主算好的 `timeMs`）+ `beat` + `src_tracks`（跨轨簇的全部源轨号）；
* 还带上宿主当前的**时序锚**（`baseBpm`/`offsetMs`）—— 我们在那边会拿它跟自己比，
  对不上（= 你在 BDG 里改过 BPM/offset）会**明确报出来**。

发出去的是 `type:"tracks"`。我们那边收到后：

* 界面「② 主轨」列表**就变成这些轨**（音轨 = 带时值数据的那些轨）；
* 重算时**不再从 MIDI 采音**，直接用收回来的时值；
* 双押泳道单独收好，再投射时原样还给它。

> 没有我们的轨时它会**明确说一句**并拒绝（不假装成功）。

## ★ 「导入时间戳」（`docs/45` §7）

用户那条箭头的**前半段**：「毫秒时间戳 → BDG」。面板上「⇣ 导入时间戳文件…」
（菜单里也有，并注册成宿主的**导入器**）：

1. `api.system.pickFile` + `readText` 读一份文本 —— 认 **一行一个毫秒数** /
   **CSV（取每行最后一个数）** / `mm:ss.xxx` / **JSON 数组或 `{timestamps:[…]}`**；
2. 发 `{type:"grid", times:[…]}` **问我们**要网格 —— 吸附的数学在
   `core.denoise`（Python）这一侧，插件只负责「读文件 + 画点」，两边不各写一份；
3. `setBaseBpm` + `setOffset`（★ 锚必须早于拍位）→ `addTrack` → 每个时间戳
   `beatOfTime(ms)` → `addMarker` + 打上我们的标记（`adbIdx/adbRun/adbRole/adbTrack`）；
4. 回读比对：被吸附挪了就如实报，并提示把顶栏分母设成 `1/div`。

⇒ 导进来的点**就是**「带时值数据的轨道」⇒ 按上面的「⤴ 返回数据到谱面生成器」
能整条收回我们那边。**没连上我们**时不假装成功：按宿主当前锚硬摆 + 明说可能被挪。

★ 键名是 `times` 不是 `ts`：`Session.send()` 会给信封打 `{v, seq, ts}`，
用 `ts` 装数组会被发送时刻冲掉（插件单测抓到过一次）。


## 它推什么

| 消息 | 何时 | 内容 |
|---|---|---|
| `hello` | 连上就发 | 协议版本 + 能力表（`caps`） |
| `project` | 工程变（去抖 300ms）/ 对端 `pull` / 点按钮 | `api.project.snapshot()` **全量** |
| `selection` | 选区变（节流 80ms） | `{kind, id, markerIds}` |
| `playhead` | 播放头动（节流 120ms） | `{beat}`（已换算成拍） |
| `audio` | 连上 / 工程变 | `api.system.audioPath()` + 音频名/md5 |
| `tracks` | **点「返回数据到谱面生成器」** | 我们的**轨道项目**（见上一节） |
| `grid` | **点「导入时间戳文件…」之后** | `{times:[ms…]}` 问我们要 bpm/相位/分母 |

`project` 里**每个点都自带宿主算的 `timeMs`**，所以我们不需要自己复算时间轴；
我们那边会拿它跟自己的 tempo 表对照（实测 866 点**逐点一致**）。

## 它注册了什么

**4 条角色轨**（侧栏 `＋` 可建；宿主会给 id 加上插件前缀）：

| 轨 | 最终 `type` | 我们那边认成 |
|---|---|---|
| 主轨 | `dev.adocharter.bdg-bridge:main` | `main` |
| 次轨 | `…:sub` | `sub` |
| 双押轨 | `…:dp` | `dp` |
| 关轨 | `…:off` | `off`（整轨不导） |

⇒ **角色写在轨道上，位置写在点位上** = 我们「分段采音」的角色泳道（`docs/34` 方案 C）。
我们只认冒号后面的 **localId**，所以插件 id 改了我们也不会失效。

另外注册了：面板、动作（显示/隐藏、重连并推）、快捷键 `Alt+Shift+B`。

## 为什么不用 main.js

宿主的 CSP 是 `connect-src 'self' ws:`（`src/renderer/index.html`）
⇒ `renderer.js` 里**直接 `new WebSocket("ws://127.0.0.1:…")` 就是放行的**，
不需要绕 `main.js`。少一层就少一处能坏的地方。

## 它**不**做的（v0.4 口径）

- ❌ **不写回**。宿主的 `api.project.edit.moveMarker(id, beat)` **没有 `force` 参数**，
  写回去会被吸附吃掉时序（`docs/35` §6.2 有实测表：1/4 档 866 点全被挪、23 点超 25ms 预算）。
  要写回得让宿主把 `store.moveMarker` 的 `force` 暴露到插件 API —— 那是**他**的改动，我们不做。
- ❌ 不读音频、不解析工程文件 —— 我们只收快照。
- ❌ 不碰宿主源码；卸载 = 删目录。

## ★ 四条角色轨的**第二个用途**：给分段采音当「打点器」

最初这四条轨只有一个用途 —— 承载**角色**（投送时按角色分轨、收回时看有没有
被拖到别的轨）。现在，**轨上的点**也被用起来了：每个点是
「**从这一刻起，这条轨是什么角色**」。

```
BDG 里摆点                        我们那边
─────────────────────            ──────────────────────────────────
[主轨]轨  ·   ·   ·      →      点「从 BDG 角色轨生成」
[次轨]轨          ·              ⇒ 分段采音（docs/39）：
[双押]轨  ·                          段1 @10.0s 主=[1]
[关轨]轨              ·              段2 @20.0s 主=[1] 次=[2]
                                     段3 @30.0s 主=[1] 次=[]
```

**摆点就是画块** —— 不用在我们这边拖泳道；在 BDG 里按拍位打点更准
（那里有网格、有播放头、能听）。

三条口径：

1. **只认我们插件的四条角色轨**。内置踩点轨（`type: "beat"`）上的点**不算**，
   宿主 `dev.bdg.adofai-export:bpm/twirl` 控制轨上的点**也不算**（那些是**变速**）。
   所以别拿普通轨当角色轨，要选 `ADO·主轨` 那四种轨类型。
2. **累计快照**：某个角色只在 t=20s 出现过，那么 t=10s 那条规则里这个角色是
   `null`（= 继续继承我们这边 ② 的全局选择），而**不是**被清空。
3. **净效果没变的点会被省掉**：同一条轨同一个角色打两次 ⇒ 只出一条规则。
   （所以「点数」和「段数」不相等是正常的，面板两个都会报。）

> 插件本身**没有为此改一行** —— 照旧只推快照。编译发生在我们这侧
> （`core.segments.from_roles`），符合「往我这边加东西，别改他那边」的口径。

## 验证到什么程度

| | 状态 |
|---|---|
| 插件离线单测（假 window/document/WebSocket） | ✅ `node tools/_bdg_plugin_test.js` → 28 项 |
| 插件发出去的**线格式**被我们真服务端吃一遍 | ✅ `python tests/test_bridge.py` K 组（跨语言复验） |
| ★★ **真在 BDG 编辑器里跑通** | ✅ **已验证**（2026-10，proxy 拉下宿主 + `npm install` + 跑起来 + CDP 遥控） |

### ★★ 真机验证记录（可复现）

```
① 宿主：vendor/beat_data_generator（proxy clone）+ npm install + node node_modules/electron/install.js
② 插件：python tools\_bdg_install_plugin.py --dev-root <宿主仓库>       # 落在 plugins/bridge_plugin
③ 启用：%(APPDATA)%\beat-data-generator\plugins-state.json = {"enabled":["dev.adocharter.bdg-bridge"]}
        （★ isEnabled = state.enabled.includes(id)，**新插件默认不启用**，plugins.ts:99）
④ 带调试端口起：npx electron-vite dev -- --remote-debugging-port=9222
⑤ 我们 sidecar：python -m sidecar.server --port 18865 --root .
⑥ 遥控：node tools/_cdp.js --file tools/_probe_points.js
```

实测结果：

```
侧栏 ＋ 菜单：  踩点轨（节拍）| 主轨 | 次轨 | 双押轨 | 关轨          ← ★ 我们的 4 条角色轨
快照类型：      ["beat","dev.adocharter.bdg-bridge:main","…:sub","…:dp","…:off"]
面板：          ● 已连接 · 发送 4 条 · 已同步 2 次 · 最后：已同步 5 轨 / 9 点
我们侧解析：    role=main  主轨   3 点 beats=[0,16,64]
                role=sub   次轨   2 点 beats=[32,48]
                role=dp    双押轨 3 点 beats=[40,44,56]
                role=off   关轨   1 点 beats=[8]
                parser=snapshot  points=9
```

⇒ **「BDG 当我们的泳道编辑器」不是设想了，是通了。**

### 踩过的坑（别再踩）

| 坑 | 真面目 |
|---|---|
| `MainWindowHandle` 拿到的是**欢迎窗**（660×520） | 宿主动起来有**两个**窗口；主窗是 1360×860。用 `tools/_shotmain.ps1`（按面积挑最大窗口） |
| 660×520 那个窗**永远全黑** | 那是 `welcome.html`（`index.ts:733`）；黑的是它的 `backgroundColor`，**不影响主窗** |
| `PrintWindow` 抓 Electron 窗口全黑 | Chromium GPU 合成 ⇒ 改用「临时置顶 + 整屏截图」（`tools/_shotmain.ps1`） |
| 端口 **8765 被别的进程占了** | 本机有 PID 占用它；换 `--port 18865` |
| `.ps1` 里写中文 ⇒ 解析报“缺少引号” | Windows PowerShell 5.1 按 **GBK** 读脚本 ⇒ 脚本里**只用 ASCII** |
| 用 PowerShell `Set-Content/-replace` 改 UTF-8 源码 | 会把文件写成**非法 UTF-8**（本项目已知事故）；改文件一律用编辑器工具 |

### 远程遥控工具（不用手点，脚本化验证）

```powershell
node tools\_cdp.js --list                      # 列出宿主里所有页面
node tools\_cdp.js "document.body.innerText"   # 在宿主主窗里求值
node tools\_cdp.js --file tools\_probe_popup.js # 表达式写在文件里（免引号地狱）
node tools\_cdp.js --keys "Alt+Shift+B"        # 发组合键
node tools\_cdp.js --mouse "272,194"           # 真实鼠标点击
```

插件还开了个调试钩子：`window.__adocBridge = {api, session, panel}`，
面板里点不出来时可以直接 `__adocBridge.session.pushProjectNow()`。
