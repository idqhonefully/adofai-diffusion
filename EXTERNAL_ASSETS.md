# 外部资产清单（不进仓库）
>其实是下个版本的更新预告
> **口径**：任何**密钥**、任何**可能包含版权内容**的文件，都**直接放外部下载链接**，
> 别打包在仓库。
>
> ⇒ 仓库里**只有本项目的源码 / 文档 / 测试**。下面这些被 `.gitignore` 排除，
> 需要时从对应来源取。

---

## 一、模型权重（体积大 + 版权敏感，一律外部）

| 被排除的路径 | 是什么 | 从哪来 |
| --- | --- | --- |
| `audio-sep/**/*.onnx` | onset / BSR 推理权重（含 `onset_net.onnx`、`AdofaiOrt` 相关） | 自训 / 上游发行包 |
| `chartgen/**` 内的权重 / 语料 | chartgen 的模型与社区谱面语料 | 见 chartgen 上游仓库说明 |
| `output/` | 生成本地产物（含成品谱面与原曲） | 本地产出 |

权重获取方式（本项目约定）：自训权重主源走 ModelScope（钉 `resolve` + commit rev，
分片并发 + Range 下载）；CUDA / ONNX Runtime 走官方源。详见 `NOTICE` / `THIRD-PARTY.md`。

## 二、二进制运行时（不进仓库）

| 路径 | 是什么 | 说明 |
| --- | --- | --- |
| `audio-sep/runtime/` | 嵌入式 Python（自包含解释器 + 依赖） | 体积大；发布时随便携包附带，仓库不含 |
| `audio-sep/ffmpeg/` | ffmpeg 二进制 | LGPL/GPL；随便携包附带或自行安装 |
| `chartgen/` 内的 Electron / Chromium（若其便携包需要） | 桌面壳运行时 | 见 chartgen 上游 |

### 环境变量（不写死路径）

开源版脚本统一读环境变量，避免泄露本机布局：

| 变量 | 默认 | 指向 |
| --- | --- | --- |
| `FFMPEG_BIN` | `<仓库>/audio-sep/ffmpeg/ffmpeg.exe` | ffmpeg 可执行文件（头像裁圆用） |
| `AVATAR_SRC` | `<仓库>/pict` | 贡献者头像源目录（见 `gui/tools/mk_avatar.py`） |
| `ADOFAI_STUDIO_ROOT` | exe 同级 / 向上回溯 | C# 壳定位工程根（见 `app-cs/src/.../Paths.cs`） |

## 三、音频 / MIDI（版权 + 体积，一律外部链接）

`.gitignore` 全局排除 `*.ogg / *.mp3 / *.wav / *.flac / *.mid / *.midi`。
测试用的样例曲 / MIDI 由使用者自行提供，放在仓库外或已被 gitignore 覆盖的位置。

## 四、其它（体积 / 噪音，不是版权问题）

| 路径 | 说明 |
| --- | --- |
| `app-cs/out/`、`app-cs/bin/`、`app-cs/obj/` | C# 构建产物 |
| `gui/output/`、各种 `output/` | 运行 / 生成本地产物 |
| `*.log`、`__pycache__/`、`*.pyc` | 日志与 Python 字节码 |
| `pict/` | 头像源图（含真人照片，不进仓库） |

---

## 五、密钥

**仓库里一个密钥都没有**。任何 token / 私钥都不要写进仓库、也不要写进 `.git/config`。
