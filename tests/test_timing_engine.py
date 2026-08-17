"""timing_engine 的 golden 测试：用已知输入验证计时引擎输出（防回归）。

期望值按 ADOFAI 引擎公式手算：
  AngleToTime(angle, bpm) = angle / 180 * (60 / bpm) * 1000 (ms)
引擎几何：tile 出生方向 = 上一格到达方向 + 180°，旋转量取 (到达方向 - 目标) * direction 的最小正角。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from timing_engine import compute_note_times


def test_plain_90deg_at_120bpm():
    # 3 格 90° @120bpm：首格转 90°=250ms，其后每格几何转角 180°=500ms，哨兵结尾转 270°=750ms
    nt = compute_note_times([90, 90, 90], {"bpm": 120, "pitch": 100, "offset": 0}, [])
    times = [x[0] for x in nt]
    assert times == [250.0, 750.0, 1250.0, 2000.0]
    assert all(not h for _, h in nt)


def test_offset_added():
    # offset 只有 add_offset=True 时参与计时
    nt = compute_note_times([90], {"bpm": 120, "pitch": 100, "offset": 500}, [], add_offset=True)
    assert nt[0][0] == 250.0 + 500.0


def test_twirl_flips_direction():
    # Twirl 在第一格翻转 direction：p_angle 从 90° 变 270° -> 250ms 变 750ms
    nt = compute_note_times(
        [90], {"bpm": 120, "pitch": 100, "offset": 0},
        [{"floor": 0, "eventType": "Twirl"}],
    )
    assert nt[0][0] == 750.0


def test_midspin_999_no_time():
    # mid-spin(999) 不增加时间：首格 250ms，999 格仍停在 250ms
    nt = compute_note_times([90, 999], {"bpm": 120, "pitch": 100, "offset": 0}, [])
    times = [x[0] for x in nt]
    assert times[0] == 250.0
    assert times[1] == 250.0


def test_bpm_affects_duration():
    # 120bpm 90°=250ms；90bpm 角速度减 1/3：90/180*(60/90)*1000 = 333.33ms
    nt = compute_note_times([90], {"bpm": 90, "pitch": 100, "offset": 0}, [])
    assert nt[0][0] == 333.3333333333333
