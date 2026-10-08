# ADOFAI-Studio

把 **歌曲音频 / MIDI / 时间戳** 变成 **A Dance of Fire and Ice（冰与火之舞）** 的可游玩谱面
（`.adofai`），并能**立刻在浏览器里看见、听见采音对不对**。

- 桌面壳：**C# / .NET 10 + WPF + WebView2**，承载前端并拉起 Python 管线；
- 前端 / 工作台：**Web**（HTML + JS），负责导入、试听、预览、编辑；
- 音频管线：**Python** —— 音源分离（BS-Roformer）→ onset 检测（ONNX）→ 谱面生成；
- `chartgen/`：**联合开发的 fork**（LINIX099/adofai-chart-generator，MIT），提供 sidecar 与核心算法。

**版本** 见 `VERSION` · `CHANGELOG.md`。
许可 **Apache-2.0**（`LICENSE` / `NOTICE` / `THIRD-PARTY.md`）。

> **仓库里只有源码 / 文档 / 测试。** 模型权重、ffmpeg、Python 运行时、打包产物、语料
> 一律排除，逐项说明在 **`EXTERNAL_ASSETS.md`**。
> 最新免配懒人包前往Q群获取 **`1027673321`**。
* * *

## 一、工程结构

```
gui/                   Web 前端 + Python 后端
  index.html           工作台入口（Web）
  backend.py           Python 后端（桥接 C# 壳与 Python 管线）
  gen_cli.py           生成 CLI 入口
  workbench/           预览 / 编辑前端（vendor/adofai-player.js 为第三方播放器）
  studio-skin/         皮肤（C# 不 serve，仅供对照）
  tools/               探针 / 验收 / 头像生成脚本
app-cs/                C# / .NET 桌面壳（WPF + WebView2）
  src/                 源码（含 Paths.cs 工程根定位）
  Directory.Build.props / nuget.config / 启动-C#壳.bat   构建配置
audio-sep/             Python 音频分离 / 推理源码（仅顶层 .py 入库）
chartgen/              联合开发的 fork（LINIX099/adofai-chart-generator，MIT）
  LICENSE / THIRD-PARTY.md / VERSION   其自身许可与说明（保留）
```

**不入库的东西**逐项在 `EXTERNAL_ASSETS.md`：模型权重 / ffmpeg / Python 运行时 /
打包产物 / 语料 / 输出。仓库里**没有密钥**。

* * *

## 二、快速开始（开发者）

### 2.1 前端（Web 工作台）

```
cd gui
python backend.py            # 起本地 HTTP（端口见实现；默认 8766）
# 浏览器打开 http://127.0.0.1:8766/index.html
```

### 2.2 C# 桌面壳 

```
cd app-cs
dotnet publish src/AdofaiStudio.App -c Release \
    --self-contained true -p:PublishSingleFile=true
# 产物为单文件 exe，放到包含 gui/ + audio-sep/ + chartgen/ 的目录即可运行
```

工程根定位（`app-cs/src/.../Paths.cs`）优先级：
**exe 同级目录** → 环境变量 `ADOFAI_STUDIO_ROOT` → 向上回溯 dev 树；
不再写死任何固定盘符路径。

## 三、许可

本程序采用 **Apache License 2.0**（`LICENSE` 全文，`NOTICE` 归属声明）。

- 可以自由使用、修改、再分发（含商用），并且**带专利授权**；
- 分发时请保留 `LICENSE` / `NOTICE` / `THIRD-PARTY.md` / `chartgen/LICENSE`；
- 改过的文件请注明「已修改」（Apache-2.0 §4(b)）。

随包第三方组件**不是** Apache-2.0，各有各的许可（.NET / WebView2 · ONNX Runtime ·
ffmpeg · numpy / scipy / librosa · Demucs / BS-Roformer · adofai-player.js），
全文与索引见 `THIRD-PARTY.md` 与 `chartgen/THIRD-PARTY.md`。

本程序**不包含** ADOFAI 游戏本体、也不分发其美术资源与音频；它只生成 `.adofai`
（纯文本 JSON），由用户在自己的游戏里打开。本项目与 7th Beat Games **无隶属关系**；
「A Dance of Fire and Ice」是其商标。
