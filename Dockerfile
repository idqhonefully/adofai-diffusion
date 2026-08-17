# ============================================================
# ADOFAI Diffusion — Docker 镜像（CPU / GPU 通用）
#
# 构建参数 TORCH_INDEX_URL（可选）：
#   不传（默认）: 用 PyPI 的 torch。Linux 上 PyPI wheel 自带 CUDA 运行时，
#                 有 NVIDIA 卡 + nvidia-container-toolkit 即可用 GPU，否则自动 CPU。
#   强制 CPU 版（镜像更小，约 2GB+）:
#       --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
#   指定 CUDA 版本（如 cu121 / cu124，需驱动支持）:
#       --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu121
#
# 完整用法见 DOCKER.md
# ============================================================
FROM python:3.12-slim

ARG TORCH_INDEX_URL=
ENV TORCH_INDEX_URL=$TORCH_INDEX_URL \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    ADOFAI_VENV_PY=/usr/local/bin/python

# 系统依赖：ffmpeg 供 librosa 解码训练数据里的 ogg/mp3
# （imageio-ffmpeg 自带的二进制 librosa 不会自动使用，必须装系统 ffmpeg）
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# 指定了 torch 源时先单独装 torch；之后 requirements 里的 torch>=2.0 会判为已满足
RUN if [ -n "$TORCH_INDEX_URL" ]; then \
        pip install --no-cache-dir torch --index-url "$TORCH_INDEX_URL"; \
    fi \
    && pip install --no-cache-dir -r requirements.txt

COPY app ./app
RUN mkdir -p /app/data /app/train

# 权重 / demucs 缓存 / 试听文件 / 训练日志（data/）与训练数据（train/）由 compose 挂载
VOLUME ["/app/data", "/app/train"]

EXPOSE 8081

# 容器内必须绑定 0.0.0.0 才能从宿主机访问；--no-browser 关闭自动开浏览器；
# --in-process 让推理/分离在 web_server 进程内常驻运行（模型只加载一次，免冷启动）
CMD ["python", "app/web_server.py", "--host", "0.0.0.0", "--port", "8081", "--no-browser", "--in-process"]
