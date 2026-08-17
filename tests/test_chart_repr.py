"""chart_repr 双向表示的往返测试：dense -> adofai 后应保留角度幅度与双向符号。

注意：dense 是帧粒度表示（hop≈5.8ms），每格 onset 热图占 3 帧；
往返合并相邻帧后格数可能变多、角度有 ±2° 的帧量化误差。因此断言
「原角度的每个幅度都能在结果中找到 ±3° 匹配」+ 符号双性，而非精确相等。
"""
import sys
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "app" / "training"))

from chart_repr import adofai_to_dense, dense_to_adofai


def _sample_level():
    return {
        "angleData": [90, -90, 180, -90, 90, -180],
        "settings": {"bpm": 120, "pitch": 100, "offset": 0},
        "actions": [],
        "decorations": [],
    }


def test_roundtrip_preserves_magnitudes_and_signs():
    lvl = _sample_level()
    T = 1024
    d = adofai_to_dense(lvl, T, global_bpm=120.0)
    assert d.shape == (3, T)

    onsets = np.where(d[0] > 0.5)[0]
    assert len(onsets) > 0

    back = dense_to_adofai(d, global_bpm=120.0, onset_frames=onsets)
    assert back is not None

    ad = np.asarray(back["angleData"], dtype=float)
    orig = np.asarray(lvl["angleData"], dtype=float)
    # 每个原角度幅度都能在结果中找到 ±3°（帧量化误差）匹配
    for oa in np.abs(orig):
        assert np.any(np.abs(np.abs(ad) - oa) <= 3.0), f"丢失幅度 {oa}"
    # 双向符号都要保留（L/R 两手都有内容）
    assert np.any(ad < 0)
    assert np.any(ad > 0)
    # offset 应被折叠回合法范围
    assert -3000 <= back["settings"]["offset"] <= 3000


def test_dense_dims():
    lvl = _sample_level()
    d = adofai_to_dense(lvl, 512, global_bpm=120.0)
    assert d.dtype == np.float32
    assert d.shape[0] == 3
    assert d.shape[1] == 512
