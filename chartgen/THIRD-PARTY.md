# 第三方组件声明（THIRD-PARTY）

本便携包内**自带**下列第三方组件。它们各自的许可与版权归各自作者，**不是**本项目的
授权条款。发布/分发时请随包保留本文件。

> ★ 本文件只列**打进包里**的东西。作者本人的代码用什么许可，由作者决定 ——
> 那是另一件事，本项目尚未定稿（见 `docs/49` §10 第 6 项）。

## 运行时

| 组件 | 版本 | 许可 | 用途 |
| --- | --- | --- | --- |
| **Python** | 3.14.3（官方 amd64 完整树） | PSF License | sidecar 解释器 |
| **Electron** | 44.0.0 | MIT | 桌面壳 |
| **Chromium** | 随 Electron | BSD-3-Clause 等 | 内嵌浏览器 |

## Python 依赖（只有「音频采音」那一支用得到）

| 组件 | 许可 | 用途 |
| --- | --- | --- |
| **NumPy** | BSD-3-Clause | 数组 / 数值 |
| **SciPy** | BSD-3-Clause | 信号处理 |
| **librosa** | ISC | 音头检测 / 节拍分析 |
| **numba** | BSD-2-Clause | librosa 的 JIT（librosa 依赖它） |
| **soundfile** | BSD-3-Clause | 音频读写（带 libsndfile） |
| **soxr / audioread / lazy_loader / msgpack / pooch / joblib / llvmlite 等** | 各自（BSD/MIT/Apache-2.0/ISC 为主） | librosa 的传递依赖 |

> **哪些是必需的**（实测过 import 位置，不是猜的）：
> · **NumPy 必需** —— `core/denoise.py` / `core/gridfit.py` / `core/synth.py` 都在**模块级**
>   `import numpy`，而 `sidecar/session.py` 又**急切**导入这三个 ⇒ 少了它 sidecar 根本起不来。
>   （`synth` 是「MIDI 渲成音频」那一步：游戏不认 MIDI，没它导出的谱在游戏里没声。）
> · **SciPy / librosa 只在「音频采音」那条路上用，而且是懒加载** ——
>   `core/audio_onsets.py` 模块级只 import numpy，librosa/scipy 是在函数里才 import。
>   ⇒ 缺了它们只是「从音频文件采音」不可用，**MIDI / `.bdg` 两条路照常**（环境自检会明说）。
> 本包把这几样**全带上了**，所以用户不用装 Python、也不用装任何包。

## 前端 / 渲染（**打包产物里含它们的代码，必须附许可**）

| 组件 | 版本 | 许可 | 本程序里的位置 | 许可文本 |
| --- | --- | --- | --- | --- |
| **adofai**（ADOFAI-JS） | 3.3.2 | **BSD-3-Clause** | `renderer/vendor/adofai-player.js`（bundle） | `LICENSES/adofai-BSD-3-Clause.txt` |
| **three.js** | 0.178.0 | **MIT** | 同上（bundle 内含） | `LICENSES/three-MIT.txt` |
| **regenerator-runtime** | — | MIT | 同上（adofai 打过包的那份声明） | `LICENSES/regenerator-runtime-MIT.txt` |
| **adofai_timemodel**（ADOFAI Macro contributors） | — | **MIT** | `vendor/adofai_timemodel/`（原样带 `LICENSE`） | `LICENSES/adofai_timemodel-MIT.txt` |

> ★ **为什么要单独把许可文本收进 `LICENSES/`**：`adofai-player.js` 是个 9.5MB 的
> **打包产物**（rolldown bundle），它把 `adofai` 与 `three.js` **并进了同一个文件**。
> BSD-3-Clause 与 MIT 都要求「以二进制形式再分发时，必须在文档/随附材料里
> **复现版权声明与许可全文**」—— 只写一句「见其仓库」是**不够**的（我们第一版就是那样）。
>
> ★ 另记一条事实：`adofai` 的上游仓库 `Xbodwf/ADOFAI-JS` **没有 LICENSE 文件**
> （2026-10 用 api.github.com 核过），只在 npm 元数据里声明 `BSD-3-Clause`。
> 我们按其声明附上标准文本，并在文件头把「上游未提供版权行」记清楚。

## ★ 没有打进包里的东西（有意为之）

| 组件 | 为什么不带 |
| --- | --- |
| **Beat Data Generator（BDG 宿主）** | **GPL-3.0**，且带 233MB 的 `node_modules`+Electron。本包**不含**它；「BDG 编辑器桥」在没有宿主时**降级为「未连接」**，其余功能不受影响。要用桥请自行安装宿主，再用本程序的「启动并桥接」。 |
| **Essentia.js / 侧载 WASM** | 10MB 级 WASM，做成**可选侧载**，不进主包（`docs/47` §决策 1）。 |
| **示例曲的原始音频** | 只带随 `samples/` 一起的正版/自制素材；用户自己的曲子请自己放。 |
