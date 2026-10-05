# 第三方组件声明（THIRD-PARTY）

本仓库源码**自带**下列第三方组件 / 依赖。它们各自的许可与版权归各自作者，
**不是**本项目的授权条款。发布 / 分发时请随包保留本文件与 `chartgen/THIRD-PARTY.md`。

> ★ 本文件只列**进仓库**的东西。模型权重、ffmpeg 二进制、Python 运行时等不进仓库，
> 见 `EXTERNAL_ASSETS.md`。

## 运行时与框架

| 组件 | 许可 | 用途 |
| --- | --- | --- |
| **.NET 10 / WPF** | MIT | C# 桌面壳框架 |
| **WebView2** | Microsoft 软件许可（运行时） | 内嵌浏览器承载 `gui/` 前端 |
| **Python** | PSF License | 音频 / 推理管线解释器（不进仓库，见 EXTERNAL_ASSETS.md） |

## 推理与音频

| 组件 | 许可 | 用途 |
| --- | --- | --- |
| **ONNX Runtime** | MIT | 跑 onset / BSR 的 ONNX 模型 |
| **numpy / scipy** | BSD-3-Clause / BSD-2-Clause | 数值 / 信号处理 |
| **librosa / soundfile / audioread** | ISC / BSD-3-Clause | 音频读写字 / 节拍分析 |
| **Demucs** | MIT | 音源分离（作为 BSR 的前处理或对照） |
| **BS-Roformer 权重** | MIT（模型权重） | 7 路音源分离（权重文件不进仓库） |

## 前端 / 预览

| 组件 | 许可 | 用途 |
| --- | --- | --- |
| **adofai-player.js**（vendored，gui/workbench/vendor/） | MIT（上游 Re_ADOJAS） | 谱面预览播放器（three.js） |
| **Chartgen 自带依赖**（chartgen/ 内） | 见 chartgen/THIRD-PARTY.md | chartgen 的 sidecar / core |

> **哪些是必需的**（实测过 import 位置，不是猜的）：
> · **numpy 必需** —— `audio-sep` 推理管线在模块级 `import numpy`；
> · **ONNX Runtime 必需** —— `AdofaiOrt` 推理；
> · **ffmpeg 必需** —— 头像裁圆（`gui/tools/mk_avatar.py`）与音频解码。

详见 `EXTERNAL_ASSETS.md` 与 `NOTICE`。
