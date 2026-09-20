# ADOFAI Diffusion — CPU 推理镜像（模型路线全部支持；GPU 版把 pip 装的
# torch/torchaudio 换成 cu 索引版本即可，基础镜像不用动）。
#
# 构建：  docker build -t adofai-diffusion .
# 运行：  docker run -d -p 8420:8420 \
#             -v ./data:/app/data \
#             -v ./train_data:/app/train_data \
#             adofai-diffusion
#   浏览器打开 http://localhost:8420
#   （环境变量 ADOFAI_DATA_DIR/ADOFAI_TRAIN_DIR/TORCH_HOME/LOCALAPPDATA
#     已在下面 ENV 里设好，docker run 不用重复传；docker-compose.yml 也已同步。）
#
# 说明：
# - 模型权重 *.pt 不打进镜像：从 GitHub Releases 下载 data.zip（当前 v7）后放到
#   宿主机 ./data/checkpoints/ 再挂载进来（下载与解压见 README「一、快速开始」第 0 步）。
# - beat_this 的 ONSET 权重（约 8 MB）随仓库提供并拷进镜像（torch_hub/）；
#   demucs 权重在 pip 包内，无需下载。
# - 无 GPU 时直接跑，代码会自动退回 CPU（慢但可用）。
# - 容器里 venv 不存在，web_server.py 自动退回系统解释器，推理子进程同样兼容。
FROM python:3.13-slim

# ffmpeg：音频解码/格式转换依赖
RUN sed -i 's|http://deb.debian.org|http://mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true; \
    apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TORCH_HOME=/app/torch_hub \
    ADOFAI_DATA_DIR=/app/data \
    ADOFAI_TRAIN_DIR=/app/train_data \
    LOCALAPPDATA=/app/data

# 不建 venv：依赖直接装进系统 site-packages。
# web_server.py 检测 venv/bin/python 不存在时自动退回 sys.executable，
# 推理/训练子进程同样用系统解释器，无需任何改动。
WORKDIR /app
COPY requirements.txt ./
# requirements.txt 里 Windows 专属包（comtypes）带了 sys_platform 标记，
# Linux 容器里 pip 会自动跳过，直接装即可。
RUN pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 源码：app 包（web_server + training + webui 全在其中，入口即 app/web_server.py）
COPY app/ ./app/

# 数据/缓存目录（运行时用 -v 挂载覆盖，持久化模型与缓存）
RUN mkdir -p /app/data/demucs_cache /app/data/checkpoints /app/train_data

# beat_this 权重（约 8 MB，已随仓库提供）：拷进镜像，首跑免联网
COPY torch_hub/ /app/torch_hub/

EXPOSE 8420
CMD ["python", "app/web_server.py", "--host", "0.0.0.0", "--port", "8420", "--no-browser"]
