"""paths.py — 集中管理 data/ 目录、权重名与训练目录（跨平台唯一真相来源）。

所有脚本（web_server / training/* / 测试）统一从这里取路径，
避免各文件散落的 'data/checkpoints' 魔法字符串与 "+ \"/\"" 拼接。
路径与名称也可被环境变量覆盖，便于容器挂载与部署定制。
"""
import os
import sys
from pathlib import Path

# 项目根：本文件在 app/ 下 -> 上一级
APP_DIR = Path(__file__).resolve().parent          # app/
ROOT = APP_DIR.parent                               # 项目根（portable / 仓库根）

# data 根：可用环境变量 ADOFAI_DATA_DIR 覆盖（容器里可指向挂载卷）
DATA_DIR = Path(os.environ.get("ADOFAI_DATA_DIR", str(ROOT / "data")))

CHECKPOINTS_DIR = DATA_DIR / "checkpoints"
PREVIEW_DIR = DATA_DIR / "preview"
TRAIN_LOG = DATA_DIR / "train.log"
DEMUCS_CACHE_DIR = DATA_DIR / "demucs_cache"

# 权重文件名（训练/推理/评测共用的唯一真相）
CKPT_ONSET = "onset_net.pt"
CKPT_VAE = "vae.pt"
CKPT_DDPM = "ddpm.pt"

# 训练数据根：可用环境变量 ADOFAI_TRAIN_DIR 覆盖
TRAIN_DIR_DEFAULT = str(ROOT / "train")


def ensure_dirs():
    """确保 data 相关目录存在（幂等）。"""
    for d in (DATA_DIR, CHECKPOINTS_DIR, PREVIEW_DIR, DEMUCS_CACHE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def venv_python(portable_root, platform=None):
    """返回 venv 解释器路径；Windows 在 Scripts/，POSIX 在 bin/。

    portable_root: 项目根（str 或 Path）。
    platform: 显式指定平台（测试用）；缺省取当前平台。
    """
    platform = platform or sys.platform
    if platform == "win32":
        return os.path.join(str(portable_root), "venv", "Scripts", "python.exe")
    return os.path.join(str(portable_root), "venv", "bin", "python")
