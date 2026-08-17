# Docker 部署（CPU / GPU）

> Windows 用户依旧用 `run.bat`，本文件只讲 Docker。两者代码已兼容，互不影响。

## 前置

- Docker Engine 20.10+（含 `docker compose` 插件）
- GPU 版额外需要：NVIDIA 驱动 + [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)

## 快速开始（CPU，也能推理）

```bash
# 在仓库根目录（有 docker-compose.yml 的地方）
docker compose up -d --build
```

浏览器打开 <http://localhost:8081>。

首次 `--build` 会做三件事：
1. 装系统 ffmpeg（librosa 解码 ogg/mp3 训练数据用）
2. 装 Python 依赖（PyPI 的 torch 在 Linux 上自带 CUDA 运行时；没显卡时自动跑 CPU）
3. 预下载 Demucs 分离权重（htdemucs，约 80MB）——**这步必须有网络**，否则镜像里「分解音频」功能会报错

启动时默认带 `--in-process`（常驻模式）：推理/分离直接在 web_server 进程内运行，
torch 与模型只加载一次，多次生成/试听不再重复冷启动。Windows 子进程模式不受影响。

**Demucs 权重现在在运行时首次使用时自动下载（约 80MB）**，不再需要构建时联网。
首次「分解音频」或「试听分离轨」会触发下载，后续走磁盘缓存。

## 数据目录（volume 挂载）

| 宿主机目录 | 容器路径 | 作用 |
|---|---|---|
| `./data` | `/app/data` | 模型权重 `checkpoints/`、Demucs mel 缓存、试听文件、`train.log` |
| `./train` | `/app/train` | 训练数据（每首歌一个文件夹：音频 + `.adofai`） |

## 训练

网页「训练模型」页签 → 数据目录填容器路径 **`/app/train`**（对应宿主机 `./train`）→ 点一键全训。
训练产出的权重落在宿主机 `./data/checkpoints/`（`onset_net.pt` / `vae.pt` / `ddpm.pt`），可随时备份。

## GPU 加速

1. 宿主机装好 nvidia-container-toolkit，`nvidia-smi` 能正常输出。
2. 编辑 `docker-compose.yml`，取消底部 `deploy.resources...` 段注释。
3. （可选）把 `build.args.TORCH_INDEX_URL` 取消注释并设为你的 CUDA 版本（如 `cu121`），构建装对应 torch。
4. 重新构建启动：

```bash
docker compose up -d --build
docker compose logs -f   # 看 torch 是否真正用了 CUDA
```

## 常用命令

```bash
docker compose logs -f      # 实时日志
docker compose restart      # 重启
docker compose down         # 停止（./data ./train 在宿主机，不会丢）
```

## 常见问题

- **生成报「未找到模型运行环境（venv 缺失）」**：旧代码硬编码了 Windows 的 venv 路径；现在已自动识别 Linux `venv/bin/python`。容器内用 `--in-process` 常驻模式，推理/分离不走 venv，不会出现该错误。
- **训练页点了没反应 / 训练子进程**：容器里训练仍走独立子进程（避免长任务卡住网页），但通过 `ADOFAI_VENV_PY=/usr/local/bin/python` 直接复用同一套 Python 环境，无需在容器里另建 venv。
- **「分解音频」报错**：首次使用会自动下载 htdemucs 权重（约 80MB，需联网）。若容器无网络，需预先在有网环境下载：
  ```bash
  docker exec -it adofai-maker python -c "from demucs.pretrained import get_model; get_model('htdemucs')"
  ```
  或改回构建时下载：在 Dockerfile 里加回 `RUN python -c "from demucs.pretrained import get_model; get_model('htdemucs')"`。
- **训练数据目录填什么**：容器里填 `/app/train`；不要填宿主机路径（容器里不存在）。
- **端口冲突**：改 `docker-compose.yml` 里 `"8081:8081"` 冒号左边的宿主机端口。
- **想离线/不联网重建**：先在有网机器上 `docker compose build`，再把镜像 `docker save` / `docker load` 过去（权重已打进镜像）。

## 镜像大小与 torch 版本选择

| 方案 | 构建参数 | 说明 |
|---|---|---|
| 默认（推荐） | 不传 | PyPI torch 自带 CUDA 运行时，CPU/GPU 通吃，镜像较大 |
| 纯 CPU | `TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu` | 镜像明显更小，无 GPU 时首选 |
| 指定 CUDA | `TORCH_INDEX_URL=https://download.pytorch.org/whl/cu121` | 与驱动匹配的精确版本 |
