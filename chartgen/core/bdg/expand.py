# -*- coding: utf-8 -*-
"""`loop` / `parentId` 展开（`docs/36` §11.2，语义已实测确认）。

语义：

```
父点自己                        → 第 0 个点
k = 1 … count                   → 子点，beat = 父.beat + k * interval
k ∈ exclude                     → 跳过（★ 存的就是 k 本身）
```

★★ **上游的快照里子点通常已经显式存在**（`store.refreshChildren` 会真的建 marker）。
   所以这里默认是 **`synth_only`**：**只补缺**，绝不重复生成。
   实测 `electric hornet`：92 母点 / 391 子点 / 4 组带 exclude，全部对得上。

★ 补出来的点带 `synth=True` ⇒ `emit` **不写回**（上游自己会重建子点，
  写回去只会制造重复）。
"""

from __future__ import annotations

from . import aliases as al
from . import model


def _kb(beat: float) -> int:
    """beat 的比较键（浮点误差内视为同拍）。"""
    return int(round(beat * 1e6))


def expected_children(parent) -> list:
    """`[(k, beat), …]` —— 一个主点**应该**有哪些子点。"""
    L = parent.loop
    if L is None:
        return []
    step = float(L.interval or 0.0)
    return [(k, parent.beat + k * step) for k in L.ks()]


def expand(project, rep, synth_only: bool = True) -> list:
    """展开循环。返回**补出来**的点（已 append 进 `project.points`）。"""
    kids = {}
    by_id = {}
    for p in project.points:
        if p.id:
            by_id[p.id] = p
        if p.parent_id:
            kids.setdefault(p.parent_id, []).append(p)

    # ---- 先审 parentId 完整性（实测满分，但上游可能改）--------------
    n_dangling = 0
    for p in project.points:
        if not p.parent_id:
            continue
        owner = by_id.get(p.parent_id)
        if owner is None or owner.loop is None:
            n_dangling += 1
            rep.drop("孤儿子点", {"点": p.id, "母点": p.parent_id})

    n_loops = 0
    n_present = 0
    n_synth = 0
    n_off = 0
    out = []

    for p in list(project.points):
        if p.loop is None:
            continue
        n_loops += 1
        exp = expected_children(p)
        present = kids.get(p.id, [])
        got = {_kb(c.beat) for c in present}

        if len(present) != len(exp):
            rep.warn("循环子点数与规格不符：母点 {} 期望 {} 个、实有 {} 个".format(
                p.id, len(exp), len(present)))

        for k, beat in exp:
            if _kb(beat) in got:
                n_present += 1
                continue
            if synth_only and not p.synth:      # 只补缺
                np_ = model.Point(
                    id=al.SYNTH_ID_PREFIX + str(p.id) + al.SYNTH_ID_SEP + str(k),
                    track_id=p.track_id,
                    beat=beat,
                    time_ms=project.tempo.time_of_beat(beat) if project.tempo else 0.0,
                    attrs={},
                    loop=None,
                    parent_id=p.id,
                    idx=-1,
                    synth=True,
                    raw={},
                )
                project.points.append(np_)
                out.append(np_)
                n_synth += 1
            else:
                n_off += 1
                rep.drop("循环子点缺失", {"母点": p.id, "k": k, "拍位": beat})

    rep.count(n_loops=n_loops, n_children_present=n_present,
              n_children_synth=n_synth, n_children_missing=n_off,
              n_dangling=n_dangling)
    if n_dangling:
        rep.warn("发现 {} 个子点的母点不存在（上游可能改了展开语义）".format(n_dangling))
    return out
