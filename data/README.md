# data/ — 运行时数据目录（模型权重放这里）

这个目录**基本不入库**（`.gitignore` 只放行本 `README.md`），因为里面放的是权重、缓存和预览图。

## 1. 放权重（必做）

仓库**不包含**模型权重，权重在发行版里：

- 下载页：**<https://github.com/idqhonefully/adofai-diffusion/releases>**
  （当前 `v7`，附件 `data.zip`，压缩包 41.5 MB / 解压 44.7 MB）

`data.zip` 内部顶层就是 `checkpoints/`，所以要在**仓库根目录**把它解压到 `data/`：

```bash
# Linux / macOS（没有 curl 就把 curl -L -o data.zip 换成 wget -O data.zip）
mkdir -p data
curl -L -o data.zip https://github.com/idqhonefully/adofai-diffusion/releases/download/v7/data.zip
unzip -o data.zip -d data && rm data.zip
```

```bat
:: Windows（CMD/批处理，在仓库根目录执行；Win10 1803+ 自带的 tar 是 bsdtar，能解 zip）
mkdir data
curl.exe -L -o data.zip https://github.com/idqhonefully/adofai-diffusion/releases/download/v7/data.zip
tar -xf data.zip -C data
del data.zip
```

解压后应该得到这 7 个 `.pt`：

```
data/checkpoints/
├── ddpm.pt               33.9 MB   ← 核心（扩散）
├── onset_net.pt           1.8 MB   ← 核心（踩点）
├── vae.pt                 2.5 MB   ← 核心（风格层）
├── onset_net_melody.pt    1.8 MB   可选（分轨·旋律）
├── onset_net_vocal.pt     1.8 MB   可选（分轨·人声）
├── shape_model.pt         0.4 MB   可选（摆形状）
└── vfx_net.pt             2.8 MB   可选（视觉特效）
```

> ⚠️ 解压到仓库根目录会得到 `<仓库根>/checkpoints/`，那不是 `app/paths.py` 会去找的位置，等于没装。
>
> 旧的 `v1.0` 发行版里是散的 3 个 `.pt`（只含核心模型），新装建议直接用 `v7`。

## 2. 自检

```bash
python -c "import sys; sys.path.insert(0,'app'); from paths import resolve_checkpoint as r; print({f: (str(r(f)) if r(f) else '缺失') for f in ['onset_net.pt','vae.pt','ddpm.pt','onset_net_melody.pt','onset_net_vocal.pt','shape_model.pt','vfx_net.pt']})"
```

七个都打印出路径 = 装齐了；出现 `缺失` 就是没被解析到，回第 1 步重做。这条命令只依赖
`app/paths.py`，不需要装 torch。

## 3. 目录用途

| 路径 | 谁写的 | 说明 |
|---|---|---|
| `checkpoints/` | 你手动解压（出厂权重） | 权重解析的第三优先级；**训练产物不写这里**，写运行时目录 |
| `demucs_cache/` | `app/training/demucs_mel.py` | Demucs 6 通道 mel 缓存（按音频哈希存 `.npy`，跟随 `ADOFAI_DATA_DIR`） |

> 网页端 / GUI 运行时的日志、预览图、训练产物、`vfx_cache` 都写在**运行时目录**
> `%LOCALAPPDATA%\ADOFAI_Diffusion`（Linux/macOS 下为 `~/ADOFAI_Diffusion`），不在 `data/` 里；
> 容器里 compose 已把 `LOCALAPPDATA` / `ADOFAI_DATA_DIR` 指到 `/app/data`。
> `app/paths.py` 里的 `PREVIEW_DIR` / `TRAIN_LOG` 是便携场景的默认值，
> 实际由 `web_server.py` 重定向到运行时目录。
