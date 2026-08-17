"""paths 模块与跨平台 venv 路径选择测试。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import paths


def test_paths_under_project_root():
    assert paths.ROOT == ROOT
    assert paths.APP_DIR == ROOT / "app"
    assert paths.CHECKPOINTS_DIR == paths.DATA_DIR / "checkpoints"
    assert paths.PREVIEW_DIR == paths.DATA_DIR / "preview"
    assert paths.TRAIN_LOG == paths.DATA_DIR / "train.log"
    assert paths.DEMUCS_CACHE_DIR == paths.DATA_DIR / "demucs_cache"


def test_ckpt_names():
    assert paths.CKPT_ONSET == "onset_net.pt"
    assert paths.CKPT_VAE == "vae.pt"
    assert paths.CKPT_DDPM == "ddpm.pt"


def test_venv_python_windows():
    p = paths.venv_python("/app", "win32")
    assert p.endswith("venv/Scripts/python.exe")


def test_venv_python_posix():
    p = paths.venv_python("/app", "linux")
    assert p.endswith("venv/bin/python")


def test_ensure_dirs_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("ADOFAI_DATA_DIR", str(tmp_path / "data"))
    import importlib
    reloaded = importlib.reload(paths)
    assert reloaded.DATA_DIR == tmp_path / "data"
    reloaded.ensure_dirs()
    assert (tmp_path / "data" / "checkpoints").is_dir()
    assert (tmp_path / "data" / "preview").is_dir()
    assert (tmp_path / "data" / "demucs_cache").is_dir()
