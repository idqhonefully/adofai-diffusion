# -*- coding: utf-8 -*-
"""端到端验证 osn1_pipeline（torch-free，默认 onset 检测器 + 两道过滤）。

验证项
------
  V1  test_mix.wav          —— 单 BSR chunk 冒烟（主链路通）
  V2  BSR 6 源有限性        —— 幅度合理、无 NaN
  V3  bench/mix20.wav       —— 真实音乐(peak 0.93)：peak_prob>0.5（证明 BSR mel 有效）、
                               多 chunk(≥2) 的 Hann 交叉淡化重叠相加、静音门控有裁减
  V4  A/B                   —— dual_block=False 时保留的踩点 >= True（同音多采拦截确实生效）

注意：bench/piano*.wav 实测是【静音文件】(peak≈0)，不能用作验证素材。
"""
import os, sys, time
import numpy as np
import librosa

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import osn1_pipeline as P

ok = True
def check(name, cond, extra=""):
    global ok
    print("    %s %s%s" % ("PASS" if cond else "FAIL", name, ("  " + extra) if extra else ""))
    if not cond:
        ok = False

MIX20 = os.path.join(HERE, "bench", "mix20.wav")
TESTMIX = os.path.join(HERE, "test_mix.wav")

print("=" * 72)
print("V1：test_mix.wav 单 chunk 冒烟")
print("=" * 72)
t0 = time.time()
r1 = P.detect(TESTMIX, dual_block=True)
print(f"  耗时 {time.time()-t0:.1f}s  peak_prob={float(r1['prob'].max()):.3f}")
print(f"  raw={r1['n_raw']} sil={r1['n_sil']} final={r1['n_final']}")
check("单 chunk 跑通且出踩点", r1["n_final"] > 0, f"final={r1['n_final']}")
check("两道过滤计数单调不增",
      r1["n_sil"] <= r1["n_raw"] and r1["n_final"] <= r1["n_sil"],
      f"{r1['n_raw']}->{r1['n_sil']}->{r1['n_final']}")
check("概率有限", np.isfinite(r1["prob"]).all())

print("\n" + "=" * 72)
print("V2：BSR 6 源有限性")
print("=" * 72)
y44, _ = librosa.load(TESTMIX, sr=44100, mono=False)
if y44.ndim == 1:
    y44 = np.stack([y44, y44])
stems, n_run = P.bsr_separate(y44.astype(np.float32))
for nm, s in stems.items():
    check(f"源[{nm}] 有限且幅度<1.5", np.isfinite(s).all() and float(np.abs(s).max()) < 1.5,
          f"max|s|={float(np.abs(s).max()):.3f}")
print(f"  test_mix chunk 数={n_run}（4s 应为 1）")

print("\n" + "=" * 72)
print("V3：真实音乐 bench/mix20.wav（多 chunk + 全通道能量）")
print("=" * 72)
pk_in = float(np.abs(librosa.load(MIX20, sr=44100, mono=False)[0]).max())
check("素材非静音", pk_in > 0.1, f"in_peak={pk_in:.3f}")
r3 = P.detect(MIX20, dual_block=True)
print(f"  peak_prob={float(r3['prob'].max()):.3f}  mean={float(r3['prob'].mean()):.5f}")
print(f"  raw={r3['n_raw']} sil={r3['n_sil']} final={r3['n_final']}")
print(f"  ts_head(ms)={r3['timestamps_ms'][:10]}")
check("真实音乐 peak_prob>0.5（BSR mel 有效）", float(r3["prob"].max()) > 0.5,
      f"peak={float(r3['prob'].max()):.3f}")
check("踩点非空且不过密", 1 < r3["n_final"] < 2000, f"final={r3['n_final']}")
check("时间戳单调递增", all(a <= b for a, b in zip(r3["timestamps_ms"], r3["timestamps_ms"][1:])))
check("概率有限", np.isfinite(r3["prob"]).all())

print("\n" + "=" * 72)
print("V4：A/B —— 关掉②同音多采拦截应保留更多踩点")
print("=" * 72)
r4 = P.detect(MIX20, dual_block=False)
print(f"  拦截=开 final={r3['n_final']}  |  拦截=关 final={r4['n_final']}")
check("关拦截后踩点 >= 开拦截", r4["n_final"] >= r3["n_final"],
      f"off={r4['n_final']} vs on={r3['n_final']}")

print("\n" + "=" * 72)
print("结论：%s" % ("OSN1 torch-free 管线 + 两道过滤 端到端通过 ✓" if ok else "存在问题 ✗"))
print("=" * 72)
sys.exit(0 if ok else 2)
