# -*- coding: utf-8 -*-
"""**自动贴合**（`docs/58`）：给一份「AI 扒的 / 抖动极大的」原始文件做**时值体检**，
并给出一组「能让它好看、像人写的」参数。

为什么需要它（2026-10 实测的一份真文件 `mad_piano_party_timestamps.json`）：

  · 它是 DEMUCS 分轨 + OnsetNet/频谱通量扒出来的，**每个音都带抖动**；
  · `core.denoise.plan()` 的**自动网格识别对 melody/drums 直接失败**
    （`格级 R=0.015 < 0.30`）—— 可那两个音**其实牢牢踩在钢琴那条 90.909ms 砖上**
    （残差中位 **2.40ms**、97.5% 落在 ±25ms 内）；
  · 结果：砖长「自动」出谱**整齐却不在音乐上**（踩拍率只有 **26.6%**）；
    人工给它砖长提示后 ⇒ 踩拍率 **100%**。

所以体检要回答四个问题（**每一步都不许静默**）：

  1. **哪条路能定砖长**？（逐路 `plan()`；能认的报 R/砖长）
  2. **真砖长是多少**？（候选 × 覆盖率 × 与「音间隔中位数」的接近度 —— 防空过/防倍频选错）
  3. **抖多大**？（每路相对真砖长的残差 中位/p90/max，以及 ±10/20/30/50/100ms 各能吸掉多少）
  4. **该填什么参数**？（砖长提示 / 拟合容差 / 去密 merge_ms / 要不要激进拟合 —— 后两个
     靠**真试算**（`fitdirect.build`）比出来，不靠拍脑袋）

★ 本模块**纯函数 + 不改任何默认行为**：只回一份报告，套用与否由用户点按钮。
"""
from __future__ import annotations

import math
import statistics as _stats

from . import denoise as dn_mod
from . import fitdirect as fd_mod
from . import solve as solve_mod
from .onsets import Onset

__all__ = ["diagnose", "detect_brick", "residuals", "TOL_CHOICES", "MERGE_CHOICES"]

#: 量抖动用这几个容差档（报告里逐个给覆盖率）
TOL_CHOICES = (10.0, 20.0, 30.0, 50.0, 100.0)
#: 去密候选（ms）
MERGE_CHOICES = (0.0, 30.0, 60.0, 90.0, 120.0, 150.0)
#: 砖长候选的合法区间（ms）：30ms(=2000bpm) ~ 2s
BRICK_MIN, BRICK_MAX = 25.0, 2000.0
#: 覆盖率判据：到达这个比例就认为「这条砖长解释得动这些音」
COVER_OK = 0.90
#: 目标密度（层/秒）：人能玩的量级
FPS_LO, FPS_HI = 3.0, 6.0


# ------------------------------------------------------------------ 基础
def residuals(ms, brick: float, phase: float | None = None) -> list[float]:
    """`ms` 里每个点到「砖长 `brick`、相位 `phase`」这根网格的**有符号**残差（ms）。"""
    if not ms or brick <= 0:
        return []
    if phase is None:
        phase = float(ms[0])
    out = []
    for x in ms:
        k = round((float(x) - phase) / brick)
        out.append(float(x) - (phase + k * brick))
    return out


def _phase_of(ms, brick: float) -> float:
    """给定砖长，估**最合适的相位**：把每个点折到一个周期里，取圆均值。"""
    if not ms or brick <= 0:
        return 0.0
    ph = float(ms[0])
    acc = 0.0
    for x in ms:
        k = round((float(x) - ph) / brick)
        acc += (float(x) - k * brick) - ph
    return ph + acc / float(len(ms))


def _coverage(ms, brick: float, tol_ms: float) -> tuple[float, float, list[float]]:
    """这条砖长 + 最佳相位下：`(覆盖率, 相位, 残差列表)`。"""
    if len(ms) < 2 or brick <= 0:
        return 0.0, 0.0, []
    ph = _phase_of(ms, brick)
    res = residuals(ms, brick, ph)
    cov = sum(1 for r in res if abs(r) <= tol_ms) / float(len(res))
    return cov, ph, res


def _median_interval(ms) -> float:
    iv = [b - a for a, b in zip(ms, ms[1:]) if b - a > 1.0]
    return float(_stats.median(iv)) if iv else 0.0


# ------------------------------------------------------------------ 找砖长
def detect_brick(streams, *, tol_ms: float = 25.0) -> dict:
    """定**真砖长**（`docs/58` §2）。

    `streams` = `[{"key":…, "label":…, "ms":[…]}, …]`。

    候选来自两处：① 每路 `plan()` 能认出来的砖长（含 ×2 / ÷2）；
    ② 全曲**相邻音间隔的中位数**（含 ×2 / ÷2）—— 后者专治「plan() 一个都认不出」。
    评分 = **跨路覆盖率**（越高越好），并列时取 **最接近间隔中位数**的那一档
    —— 这一条是为了防空过：`90.909` 和 `181.8` 都能认出来，但后者解释不动
    「每 90.9ms 一个音」的旋律（覆盖率会掉一半），会被判负。
    """
    auto: list[dict] = []
    all_ms: list[float] = []
    for s in streams:
        ms = [float(x) for x in (s.get("ms") or [])]
        if len(ms) < 2:
            continue
        all_ms.extend(ms)
        try:
            g = dn_mod.plan(ms)
        except Exception as exc:                      # noqa: BLE001
            auto.append({"key": s.get("key"), "ok": False,
                         "why": "%s: %s" % (type(exc).__name__, exc)[:60]})
            continue
        auto.append({"key": s.get("key"), "label": s.get("label") or s.get("key"),
                     "ok": bool(g.ok), "why": g.reason,
                     "brick_ms": float(g.period_ms) if g.ok else 0.0,
                     "bpm": float(g.bpm) if g.ok else 0.0,
                     "div": int(g.div) if g.ok else 0, "n": len(ms)})

    # 候选分两族（来源不同，可信度不同）：
    #   ① `plan()` 能认出来的砖长（×2 / ÷2）—— 这是**经过精修的**，优先信；
    #   ② 每路各自的「相邻音间隔中位数」（×2 / ÷2）—— 只在①全都不合格时才用它。
    #   ★ 用**每一路各自的**间隔中位数（不是把各路混在一起）—— 混起来会造出一个假的
    #     中位间隔（实测 34.8ms），把砖长带偏。
    auto_c: list[float] = []
    med_c: list[float] = []
    for a in auto:
        if a.get("ok") and a.get("brick_ms", 0) > 0:
            auto_c.extend([a["brick_ms"], a["brick_ms"] * 2.0, a["brick_ms"] / 2.0])
    for s in streams:
        ms = [float(x) for x in (s.get("ms") or [])]
        if len(ms) < 8:
            continue
        med_s = _median_interval(sorted(ms))
        if med_s > 0:
            med_c.extend([med_s, med_s * 2.0, med_s / 2.0])
    med = _median_interval(sorted(all_ms)) if all_ms else 0.0
    _clamp = {round(c, 6) for c in (auto_c + med_c) if BRICK_MIN <= c <= BRICK_MAX}
    if not _clamp:
        return {"ok": False, "why": "候选砖长一个都没有（点太少或全是噪声）",
                "auto": auto, "median_interval_ms": med, "candidates": []}

    def _score(c: float) -> dict:
        # ★★ 判覆盖率的容差必须**相对砖长**（≤ 20%）。容差一旦 ≥ 砖长/2，
        #   「任何细砖」都 100% 覆盖一切（实测 34.8ms 覆盖 1.0）⇒ 判据退化。
        tol_eff = min(float(tol_ms), 0.2 * c)
        covs, tot = [], 0.0
        for s in streams:
            ms = [float(x) for x in (s.get("ms") or [])]
            if len(ms) < 2:
                continue
            cov, _ph, _res = _coverage(ms, c, tol_eff)
            covs.append(cov)
            tot += cov * len(ms)
        n = max(1, sum(len(s.get("ms") or []) for s in streams))
        return {"brick_ms": c, "cover": tot / float(n),
                "min_cover": min(covs) if covs else 0.0,
                "tol_ms": round(tol_eff, 3),
                "src": "auto" if round(c, 6) in {round(x, 6) for x in auto_c}
                else "median"}

    scored = {c: _score(c) for c in sorted(_clamp)}

    # ★★ 判据：**从最粗往细走，第一根「覆盖率 ≥ 85%」的砖长就是它**
    #   （= 音乐上的「基本节奏单位 / 一条直线」：这是**能解释数据的最粗那根**）。
    #   反过来（覆盖率最高就选谁）会选到最细的砖 —— 细砖长会把 travel 整体放大、
    #   逼出一堆 SetSpeed；而门槛从严到宽「逐个试」又会先撞上细砖，同样错。
    #   另外**先信 `plan()` 精修过的那一族**，中位间隔推出来的只在①全不合格时才用。
    best, reliable = None, True
    for fam in ("auto", "median"):
        pool = [x for x in scored.values() if x["src"] == fam]
        for x in sorted(pool, key=lambda y: -y["brick_ms"]):
            if x["cover"] >= COVER_OK:                    # 0.85~0.90 之间也给过
                best = x
                break
            if x["cover"] >= 0.85:
                best = x
                break
        if best:
            break
    if best is None:
        best = max(scored.values(), key=lambda x: x["cover"])
        reliable = False
    elif best["brick_ms"] not in {round(x, 6) for x in auto_c}:
        # 中位间隔推出来的：拿它当 hint 让 `plan()` **精修一次**（更准，也更"有据"）
        dens = max(streams, key=lambda s: len(s.get("ms") or []))
        try:
            g2 = dn_mod.plan([float(x) for x in dens["ms"]], hint=best["brick_ms"])
            if g2.ok and g2.period_ms > 0:
                best = _score(round(float(g2.period_ms), 6))
        except Exception:                              # noqa: BLE001
            pass
    return {"ok": True, "brick_ms": best["brick_ms"], "bpm": 60000.0 / best["brick_ms"],
            "cover": best["cover"], "tol_ms": tol_ms, "reliable": reliable,
            "brick_source": best["src"],
            "from_median": bool(med > 0
                                and abs(math.log2(best["brick_ms"] / med)) < 0.02),
            "median_interval_ms": med, "auto": auto,
            "candidates": sorted(scored.values(), key=lambda x: -x["brick_ms"])[:12]}


# ------------------------------------------------------------------ 试算打分
def _probe(streams, brick: float, tol_ms: float, merge_ms: float,
           aggressive: bool) -> dict:
    """按一组参数**真试算**一次直拟合，量出「像人」的几个硬指标。

    只算不做别的：不碰 Session、不改默认值。
    """
    ms: list[float] = []
    for s in streams:
        if not s.get("use", True):
            continue
        ms.extend(float(x) for x in (s.get("ms") or []))
    ms.sort()
    if merge_ms > 0 and len(ms) > 1:                   # 去密：同簇合并（取簇内第一个）
        keep = [ms[0]]
        for x in ms[1:]:
            if x - keep[-1] > merge_ms:
                keep.append(x)
        ms = keep
    if len(ms) < 2:
        return {"ok": False, "why": "去密后不足 2 个点"}
    ons = [Onset(t_ms=x, velocity=100, pitch=60) for x in ms]
    p = solve_mod.SolveParams(travel_min=15.0 if aggressive else 20.0)
    try:
        ch, rep = fd_mod.build(ons, p, base_bpm=60000.0 / brick, tier_mode="dp",
                               angle_ladder=aggressive, angle_tol_ms=tol_ms)
    except Exception as exc:                          # noqa: BLE001
        return {"ok": False, "why": "%s: %s" % (type(exc).__name__, exc)[:70]}
    fl = list(ch.floors[1:-1])
    n = max(1, len(fl))
    tt = solve_mod.times_from_chart(ch)
    base = tt[ch.meta["onset_floors"][0]]
    onbeat = sum(1 for t in tt
                 if abs((t - base) / brick - round((t - base) / brick)) <= 0.15)
    dur = max(1e-9, (max(ms) - min(ms)) / 1000.0)
    fps = n / dur
    ss = sum(1 for i in range(1, len(ch.floors))
             if abs(ch.floors[i].bpm - ch.floors[i - 1].bpm) > 1e-9)
    return {
        "ok": True, "n": len(ms), "floors": n, "fps": fps,
        "straight_frac": sum(1 for x in fl if abs(x.travel - 180.0) < 1e-6) / float(n),
        "ladder_frac": sum(1 for x in fl
                           if abs(x.travel / 15.0 - round(x.travel / 15.0)) < 1e-6)
        / float(n),
        "beat_frac": onbeat / float(max(1, len(tt))),
        "hairpin": sum(1 for x in fl if x.travel < 60.0 or x.travel > 300.0),
        "n_setspeed": ss, "twirl": sum(1 for x in fl if getattr(x, "twirl", False)),
        "travel_min": min((x.travel for x in fl), default=180.0),
        "err_max_ms": rep.err_max_ms,
    }


def human_score(m: dict, *, want_setspeed_frac: float = 0.25) -> float:
    """「像人」的打分（`docs/58` §4）—— 权重写死并在此说明，避免"拍脑袋排序"。

    踩拍 30 · 直线 20 · 15° 格 15 · 密度 15 · 发卡 10 · 换档 10
    （**踩拍第一**：整齐但不踩拍的谱面是废的 —— 那份真文件就是这么被认出来的）
    """
    if not m.get("ok"):
        return -1.0
    n = max(1, m["floors"])
    if FPS_LO <= m["fps"] <= FPS_HI:
        den = 1.0
    else:
        den = max(0.0, 1.0 - abs(m["fps"] - (FPS_LO + FPS_HI) / 2.0)
                  / ((FPS_HI - FPS_LO) / 2.0 + 3.0))
    ss_frac = m["n_setspeed"] / float(n)
    return (30.0 * m["beat_frac"] + 20.0 * m["straight_frac"]
            + 15.0 * m["ladder_frac"] + 15.0 * den
            + 10.0 * (1.0 - min(1.0, m["hairpin"] / float(n) * 5.0))
            + 10.0 * (1.0 - min(1.0, ss_frac / max(1e-9, want_setspeed_frac))))


# ------------------------------------------------------------------ 体检
def diagnose(streams, *, do_probe: bool = True) -> dict:
    """完整体检 → 一份可 JSON 化的报告（界面那张卡直接吃它）。

    `streams` = `[{"key": "0", "label": "旋律·melody", "ms": [...]}, …]`；
    报告里 `suggest` 就是「要填的那几个参数」，另有 `why` 说明每一项的依据。
    """
    streams = [s for s in streams if s.get("ms")]
    if not streams:
        return {"ok": False, "why": "没有任何音轨数据（先载入一个文件）"}
    total = sum(len(s["ms"]) for s in streams)
    if total < 8:
        return {"ok": False, "why": "总共不到 8 个音头，定不出时值（先把来源换一份更全的）"}

    det = detect_brick(streams, tol_ms=max(TOL_CHOICES) / 4.0)
    if not det.get("ok"):
        return {"ok": False, "why": det.get("why") or "定不出砖长", "auto": det.get("auto")}
    brick = float(det["brick_ms"])

    # ---- 每路：相对真砖长的残差 + 覆盖率 + 碎音率
    rows = []
    for s in streams:
        ms = sorted(float(x) for x in s["ms"])
        cov, ph, res = _coverage(ms, brick, max(TOL_CHOICES))
        absr = sorted(abs(r) for r in res) or [0.0]
        iv = [b - a for a, b in zip(ms, ms[1:])]
        rows.append({
            "key": s.get("key"), "label": s.get("label") or s.get("key"),
            "n": len(ms),
            "ms_lo": round(ms[0], 1), "ms_hi": round(ms[-1], 1),
            "median_ms": round(_stats.median(absr), 2),
            "p90_ms": round(absr[min(len(absr) - 1, int(0.9 * len(absr)))], 2),
            "max_ms": round(absr[-1], 2),
            "cover": {("%.0f" % t): round(_coverage(ms, brick, t)[0], 4)
                      for t in TOL_CHOICES},
            "short30": sum(1 for x in iv if x < 30.0),
            "short60": sum(1 for x in iv if x < 60.0),
            "median_interval_ms": round(_median_interval(ms), 1),
        })

    # ---- 建议容差：最小的「覆盖 ≥ 95%」那档
    all_ms: list[float] = []
    for s in streams:
        all_ms.extend(float(x) for x in s["ms"])
    tol = TOL_CHOICES[-1]
    for t in TOL_CHOICES:
        cov_all, _ph, _r = _coverage(all_ms, brick, t)
        if cov_all >= 0.95:
            tol = t
            break
    cover_at_tol = _coverage(all_ms, brick, tol)[0]

    # ---- 建议主轨：**在「跟得上砖长」的路里挑点最多的那一路**。
    #   ★ 不能只按覆盖率挑 —— 鼓那类「点少、间隔大」的轨覆盖率常常略高一点，
    #     但它是**节奏/伴奏**轨，拿它当主轨出来的谱面又稀又散（实测踩拍只有 77%）。
    #     人写谱主轨 = 音最多的那条旋律线。
    ok_rows = [r for r in rows
               if r["cover"][("%.0f" % tol)] >= COVER_OK] or rows
    best = max(ok_rows, key=lambda r: (r["n"], r["cover"][("%.0f" % tol)]))
    others = [r for r in rows if r["key"] != best["key"]]
    noisy = [r["label"] for r in others
             if r["cover"][("%.0f" % tol)] < COVER_OK or r["median_ms"] > tol]
    suggestive_dp = [r["label"] for r in others
                     if r["label"] and ("鼓" in str(r["label"])
                                        or "drum" in str(r["label"]).lower())]

    # ---- 建议去密：最小 merge 让密度落进 3~6 层/秒
    dur = (max(all_ms) - min(all_ms)) / 1000.0 if all_ms else 0.0
    merge = 0.0
    dens = {}
    for mg in MERGE_CHOICES:
        keep = _thin(sorted(all_ms), mg)
        fps = (len(keep) / dur) if dur > 0 else 0.0
        dens[("%.0f" % mg)] = round(fps, 2)
        if merge == 0.0 and fps <= FPS_HI:
            merge = mg
    if merge == 0.0 and dens.get("0") and float(dens["0"]) > FPS_HI:
        merge = MERGE_CHOICES[-1]

    # ---- 试算：激进 vs 不激进（只对建议主轨）
    probe = {}
    if do_probe:
        use = [dict(s, use=(s.get("key") == best["key"])) for s in streams]
        for tag, agg in (("off", False), ("on", True)):
            probe[tag] = _probe(use, brick, tol, merge, agg)
            probe[tag]["score"] = human_score(probe[tag])
        probe["agg_better"] = bool(probe.get("on", {}).get("score", -1)
                                   > probe.get("off", {}).get("score", -1) + 0.5)
    agg_ok = bool(probe.get("agg_better"))

    why = []
    if not det.get("reliable", True):
        why.append("⚠ **不可靠**：没有任何一根砖长能解释 ≥85% 的点"
                   "（多半是各路节奏互不相容 / 点太少）—— "
                   "建议里给的是「覆盖率最高」的那根，请**人工核对**再套用")
    if det.get("from_median"):
        why.append("砖长取自全曲**音间隔中位数**（自动网格识别认不出来）")
    else:
        src = [a.get("label") or a.get("key") for a in det.get("auto") or []
               if a.get("ok")]
        why.append("砖长取自**自动网格识别**能认出的那一路：%s" % ("/".join(map(str, src)) or "?"))
    auto_fail = [str(a.get("key")) for a in det.get("auto") or [] if not a.get("ok")]
    if auto_fail:
        why.append("★ 这些路**自动认不出网格**（AI 扒的常见症状）：%s —— "
                   "所以「砖长」这一栏必须给它提示，否则整齐也不踩拍"
                   % "/".join(auto_fail))
    why.append("容差建议 %.0fms：全曲 %.1f%% 的点落在真砖长的 ±%.0fms 内"
               % (tol, cover_at_tol * 100.0, tol))
    why.append("去密建议 %.0fms：密度从 %.1f 层/秒降到约 %.1f 层/秒（人能玩的量级 3~6）"
               % (merge, float(dens.get("0") or 0.0), float(dens.get("%.0f" % merge) or 0.0)))
    if probe:
        why.append("激进拟合：%s（%s）"
                   % ("**建议开**" if agg_ok else "**建议不开**",
                      "踩拍 %.1f%%→%.1f%%、直线 %.1f%%→%.1f%%、Twirl %d→%d"
                      % (probe["off"]["beat_frac"] * 100, probe["on"]["beat_frac"] * 100,
                         probe["off"]["straight_frac"] * 100,
                         probe["on"]["straight_frac"] * 100,
                         probe["off"]["twirl"], probe["on"]["twirl"])
                      if probe.get("off", {}).get("ok")
                      and probe.get("on", {}).get("ok") else "试算没跑成"))
    if noisy:
        why.append("这 %d 路没跟上真砖长（点少或更抖），**别默认勾**：%s"
                   % (len(noisy), "/".join(noisy)))
    if suggestive_dp:
        why.append("有鼓轨（%s）：**不建议自动当双押轨**——它是频谱通量扒的、多而抖，"
                   "实测会插出成百上千个双押；想要重拍感请用「分段」手动挑"
                   % "/".join(suggestive_dp))

    return {
        "ok": True,
        "brick_ms": round(brick, 4), "bpm": round(60000.0 / brick, 3),
        "brick_cover": round(det["cover"], 4),
        "brick_reliable": bool(det.get("reliable", True)),
        "brick_source": det.get("brick_source") or "auto",
        "auto": det.get("auto") or [], "candidates": det.get("candidates") or [],
        "streams": rows, "tol_choices": list(TOL_CHOICES),
        "density_by_merge": dens,
        "probe": probe,
        "suggest": {
            "fit_mode": "direct",
            "denoise_on": True,
            "denoise_hint_ms": round(brick, 4),
            "fit_tol_ms": tol,
            "aggressive_fit": agg_ok,
            "merge_ms": merge,
            "tracks_checked": [int(best["key"])] if str(best["key"]).isdigit() else [],
            "dp_checked": [],
            "sub_checked": [],
            "main_label": best["label"], "main_cover": best["cover"][("%.0f" % tol)],
            "keep_others_off": [r["label"] for r in others],
        },
        "why": why,
        "main_key": best["key"],
    }


def _thin(ms, merge_ms: float) -> list[float]:
    """去密：同一簇（间隔 < merge_ms）只留**第一个**（与 `merge_ms` 采音同口径）。"""
    if merge_ms <= 0 or len(ms) < 2:
        return list(ms)
    keep = [ms[0]]
    for x in ms[1:]:
        if x - keep[-1] > merge_ms:
            keep.append(x)
    return keep
