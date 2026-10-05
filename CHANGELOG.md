# Changelog

## 0.1.0 (2026-10-05)

- 首次公开源码快照。
- 工程结构：`gui/`（Web 前端 + Python 后端）、`app-cs/`（C# / .NET 10 + WPF + WebView2 桌面壳）、
  `audio-sep/`（Python 音频分离 / 推理源码）、`chartgen/`（联合开发的修改版 fork，MIT）。
- 品牌统一：界面与文档中的旧品牌名已统一替换为 **Adofai-Chart-Generator**；
  「关于」页贡献者人名 **LINIX099** / **iDQhonefully** 保留。
- 隐私擦洗：源码中所有写死的绝对路径（工程根目录、用户主目录、桌面目录等，含正斜杠 /
  单反斜杠 / 双反斜杠三种写法）替换为占位符 `<REPO>` / `~` / `<PICT>` 或改为相对 / 环境变量。
- 路径定位：`app-cs` 工程根不再写死盘符，改走 exe 同级 / 环境变量 / 向上回溯。
- 许可：顶层 Apache-2.0；`chartgen/` 保留其 MIT `LICENSE` 并随附 `NOTICE` 归属声明。
- 外部资产（权重 / ffmpeg / Python 运行时 / 语料）按 `EXTERNAL_ASSETS.md` 排除，不进仓库。
