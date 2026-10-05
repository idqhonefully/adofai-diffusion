"""verify_osn1_long.py — 「多长音频都得能输入进去」专项验证（纯 venv / 零 torch）

针对主人 9-23 的要求：不要固定音频时长。
验证两件事：
  A. OnsetNet ONNX 本身对**任意长度**都一次前向吃下（不再分块/补零）——
     用计数 session 客观记录 run() 的调用次数与每次送进去的真形状。
  B. 整条管线跑真实长歌（202s / 317s）端到端，长度不受 4096 帧限制。

用法：./venv/Scripts/python.exe verify_osn1_long.py [音频路径 ...]
"""
from __future__ import annotations
import os
import sys
import threading
import time

import numpy as np
import librosa

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import osn1_pipeline as P  # noqa: E402

HOP_MS = P.HOP_MS
DEFAULT_FILES = [
    "output/Automaton Waltz - Plum_instrumental.wav",   # 202s
    ".tmp_in/chain_10392_1789722092/input.wav",         # 317s
]
LEGACY_CHUNK = 4096   # 旧版固定窗，用来做对照

fails = []


def check(label, cond, extra=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({extra})" if extra else ""))
    if not cond:
        fails.append(label)


class CountingSess:
    """包一层 session，客观记录 run() 被调了几次、每次真实送入的形状。"""

    def __init__(self, inner):
        self.inner = inner
        self.calls = []
        self.lock = threading.Lock()

    def run(self, out, feed):
        x = feed["mel"]
        with self.lock:
            self.calls.append(tuple(x.shape))
        return self.inner.run(out, feed)


def peak_mem_mb():
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1048576.0
    except Exception:
        return None


def part_a(real_mel_len):
    """直接用一段真长度的 mel 调 run_osn1，确认"一次前向"。"""
    print(f"\n=== A. run_osn1 任意长度一次前向（T={real_mel_len}）===")
    cs = CountingSess(P.get_osn1_session())
    orig = P.get_osn1_session
    P.get_osn1_session = lambda *a, **k: cs
    try:
        for T in (37, 1000, 4096, 12345, real_mel_len):
            cs.calls.clear()
            mel = np.random.RandomState(T % 777).randn(6, 128, T).astype(np.float32)
            t0 = time.time()
            out = P.run_osn1(mel)
            dt = time.time() - t0
            n_call = len(cs.calls)
            shape_ok = tuple(out.shape) == (T,)
            if T <= LEGACY_CHUNK:
                n_ok = n_call == 1
            else:
                # 旧版会切 ceil(T/4096) 次；新版必须 1 次
                n_ok = n_call == 1
            print(f"    T={T:>6} ({T * HOP_MS / 1000:>6.1f}s 音频)  前向次数={n_call}  "
                  f"送入形状={cs.calls[0] if cs.calls else None}  用时 {dt:.2f}s")
            check(f"T={T} 输出长度正确", shape_ok, f"got {out.shape}")
            check(f"T={T} 只前向 1 次（旧版需 {max(1, -(-T // LEGACY_CHUNK))} 次）", n_ok)
    finally:
        P.get_osn1_session = orig


def part_b(path):
    print(f"\n=== B. 长歌端到端：{os.path.basename(path)} ===")
    if not os.path.exists(path):
        print("    （文件不存在，跳过）")
        return
    y, _ = librosa.load(path, sr=22050, mono=True)
    dur = len(y) / 22050.0
    T_expected = len(y) // P.HOP
    del y
    print(f"    时长 {dur:.1f}s  期望 mel 帧数 T≈{T_expected}"
          f"（旧版固定窗 4096 需切 {-(-T_expected // LEGACY_CHUNK)} 段）")
    mb0 = peak_mem_mb()
    cs = CountingSess(P.get_osn1_session())
    orig = P.get_osn1_session
    P.get_osn1_session = lambda *a, **k: cs
    t0 = time.time()
    try:
        r = P.detect(path)
    finally:
        P.get_osn1_session = orig
    dt = time.time() - t0
    mb1 = peak_mem_mb()
    ts = r["timestamps_ms"]
    print(f"    总耗时 {dt:.1f}s（其中 BSR 分离按 {P.BSR_CHUNK} 采样固定块跑，与本项无关）")
    print(f"    OnsetNet 前向 {len(cs.calls)} 次，送入形状 {cs.calls[:2]}"
          f"{' ...' if len(cs.calls) > 2 else ''}")
    if mb0 and mb1:
        print(f"    进程内存 {mb0:.0f}MB -> {mb1:.0f}MB")
    print(f"    peak_prob={float(r['prob'].max()):.3f}  raw={r['n_raw']} "
          f"sil={r['n_sil']} final={r['n_final']}")
    print(f"    末次踩点={ts[-1] if ts else None}ms  （应 < {int(dur * 1000)}ms）")
    check("长歌跑通且出踩点", r["n_final"] > 0, f"final={r['n_final']}")
    check("OnsetNet 整首一次前向（未分块）", len(cs.calls) == 1,
          f"前向 {len(cs.calls)} 次 / 送入 {cs.calls[0] if cs.calls else None}")
    check("踩点未超出音频时长", (not ts) or ts[-1] < dur * 1000 + 200)
    check("覆盖到后段（末次踩点 > 80% 时长）",
          bool(ts) and ts[-1] > dur * 800, f"末次={ts[-1] if ts else 0}ms")
    check("概率序列有限", np.isfinite(r["prob"]).all())


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--a-only"]
    a_only = "--a-only" in sys.argv
    files = args or DEFAULT_FILES
    # A 用第一首真歌的长度做输入尺寸
    real_len = 0
    for f in files:
        if os.path.exists(f):
            import soundfile as sf
            real_len = int(sf.info(f).duration * 22050) // P.HOP
            break
    part_a(real_len or 34867)
    if not a_only:
        for f in files:
            part_b(f)
    print("\nRESULT:", "ALL PASS ✅" if not fails else f"失败 {len(fails)} 项 ❌ {fails}")
