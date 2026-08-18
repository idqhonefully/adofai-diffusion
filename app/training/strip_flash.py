"""
strip_flash.py — 清理已生成 .adofai 里的 Flash（解决快闪刺眼，2026-08-14）。

用法:
  python strip_flash.py <input.adofai> [--mode off|sparse] [--gap 1.5] [--max 24] [--inplace]

  --mode off    (默认) 移除全部 Flash，写出 <input>.noflash.adofai（不破坏原文件）
  --mode sparse 按时间降频（相邻 Flash 至少 --gap 秒，整首不超过 --max 个），保留稀疏闪光
  --inplace     直接覆盖原文件（谨慎，会改原谱面）

示例:
  # 把 Desktop 上某谱面的闪光全部去掉，生成 .noflash.adofai 副本
  python strip_flash.py "D:/Users/Windows/Desktop/mad_piano_party.adofai"
  # 只把闪光降到稀疏（间隔2秒、最多20个）
  python strip_flash.py song.adofai --mode sparse --gap 2.0 --max 20 --inplace

依赖: chart_repr.compute_note_times（仅 numpy，不加载 torch/demucs，启动快）。
"""
from __future__ import annotations
import os
import sys
import json
import argparse

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from chart_repr import compute_note_times  # 仅依赖 numpy

# 默认降频参数（与 apply_vfx.py 对齐）
DEF_GAP = 1.5
DEF_MAX = 24


def _ftime_of(a, nt):
    try:
        fl = int(a.get("floor", 1)) - 1
    except Exception:
        return 0.0
    if 0 <= fl < len(nt):
        t = nt[fl]
        if isinstance(t, (list, tuple)):
            try:
                return float(t[0])
            except Exception:
                return 0.0
        try:
            return float(t)
        except Exception:
            return 0.0
    return 0.0


def filter_flash(actions, nt, mode="off", gap=DEF_GAP, max_count=DEF_MAX):
    """原地修改 actions，移除/降频 Flash。返回 (保留数, 移除数)。"""
    if mode == "off":
        keep = [a for a in actions if a.get("eventType") != "Flash"]
        removed = len(actions) - len(keep)
        actions[:] = keep
        return 0, removed
    # sparse
    flash_items = [(i, a) for i, a in enumerate(actions) if a.get("eventType") == "Flash"]
    if not flash_items:
        return 0, 0
    flash_items.sort(key=lambda x: _ftime_of(x[1], nt))
    last_keep = -1e9
    keep_idx = set()
    count = 0
    for i, a in flash_items:
        t = _ftime_of(a, nt)
        if (t - last_keep) >= gap and count < max_count:
            keep_idx.add(i)
            last_keep = t
            count += 1
    new_actions = [a for i, a in enumerate(actions)
                   if a.get("eventType") != "Flash" or i in keep_idx]
    removed = len(actions) - len(new_actions)
    actions[:] = new_actions
    return count, removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("--mode", choices=["off", "sparse"], default="off")
    ap.add_argument("--gap", type=float, default=DEF_GAP)
    ap.add_argument("--max", type=int, default=DEF_MAX, dest="max_count")
    ap.add_argument("--inplace", action="store_true")
    a = ap.parse_args()

    if not os.path.exists(a.input):
        print(f"[strip] 找不到文件: {a.input}")
        sys.exit(1)

    level = json.load(open(a.input, encoding="utf-8"))
    angle_data = level.get("angleData", []) or []
    settings = level.get("settings", {}) or {}
    actions = level.get("actions", []) or []

    try:
        nt = compute_note_times(angle_data, settings, actions, add_offset=True)
    except Exception as e:
        print(f"[strip] 计算方块时间失败({e})，无法按时间过滤，仅按全移除处理")
        nt = []

    n_before = sum(1 for x in actions if x.get("eventType") == "Flash")
    keep, removed = filter_flash(actions, nt, mode=a.mode, gap=a.gap, max_count=a.max_count)
    level["actions"] = actions

    if a.inplace:
        out = a.input
        json.dump(level, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"[strip] 已就地更新 {out}：Flash {n_before} -> 保留 {keep}（移除 {removed}）")
    else:
        base, ext = os.path.splitext(a.input)
        tag = "noflash" if a.mode == "off" else "sparseflash"
        out = f"{base}.{tag}{ext}"
        json.dump(level, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"[strip] 写出 {out}：Flash {n_before} -> 保留 {keep}（移除 {removed}）")


if __name__ == "__main__":
    main()
