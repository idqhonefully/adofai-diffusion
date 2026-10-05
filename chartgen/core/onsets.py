"""采音层：把「一条 MIDI 音轨」压成一条单音 onset 序列。

设计原则（对应 docs/03 的难点③）：
  - 只用一条轨（用户指定），不做跨轨融合 —— 避免"采了鼓还是采旋律"的隐性歧义。
  - 同时音必须合并（ADOFAI 一层一按，不能有多押）。
  - 必须能稀疏化（否则 7267 个 note-on 会变成纯刺猬）。
所有参数都暴露给 UI，且每一步都能在预览里立刻看到结果。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .midi import Note, Track


@dataclass
class Onset:
    t_ms: float
    velocity: int
    pitch: int
    n_merged: int = 1
    pitches: tuple[int, ...] = ()
    tick: int = 0              # MIDI tick：用来精确算「它是几分音符」（拍数 = Δtick / PPQ）
    # ★ 这个 onset 由**哪几条源轨**的音并出来的（升序去重）。
    #   `docs/42`：投射到 BDG 时要**按源轨分轨**，全靠它。
    #   为什么是**元组**而不是一个数：一次按键可能是好几条轨同时响（在 `merge_ms` 内聚成一簇）
    #   —— 那种跨轨簇必须看得出来（否则「分轨」时只能瞎归给其中一条）。
    src_tracks: tuple[int, ...] = ()

    # ★ xk base 的**骨架砖**：不是从音乐采来的，而是「大直线」按 `t = φ + k·砖长`
    #   硬铺出来的合成 onset（`docs/47` §3）。下游靠它区分「骨架」与「真音」。
    synth: bool = False

    def __repr__(self) -> str:
        t = ("trk%s" % ",".join(str(x) for x in self.src_tracks)) if self.src_tracks else "-"
        return (f"Onset({self.t_ms:.2f}ms, v={self.velocity}, p={self.pitch}, "
                f"n={self.n_merged}, {t}{', 骨架' if self.synth else ''})")


@dataclass
class OnsetParams:
    merge_ms: float = 30.0        # 间隔小于此值的音合并成一次按键
    merge_anchor: str = "first"   # first=取簇里最早的音 / loudest=取簇里力度最大的音
    min_velocity: int = 1         # 力度过滤
    min_interval_ms: float = 0.0  # 相邻两次按键的最小间隔（稀疏化）
    pitch_lo: int = 0             # 音高范围过滤
    pitch_hi: int = 127
    max_onsets: int = 0           # >0 时按力度保留前 N 个（again 稀疏化）

    def describe(self) -> str:
        return (f"merge={self.merge_ms:g}ms/{self.merge_anchor} vel>={self.min_velocity} "
                f"gap>={self.min_interval_ms:g}ms pitch={self.pitch_lo}-{self.pitch_hi}")


def build_onsets(notes: list[Note], params: OnsetParams) -> list[Onset]:
    """音符列表 -> onset 序列（单轨/多轨都走这里）。"""
    sel = [n for n in notes
           if n.velocity >= params.min_velocity
           and params.pitch_lo <= n.pitch <= params.pitch_hi]
    if not sel:
        return []
    sel.sort(key=lambda n: (n.t_on_ms, -n.velocity))

    # 1) 同时音合并
    clusters: list[list[Note]] = []
    cur: list[Note] = [sel[0]]
    for n in sel[1:]:
        if n.t_on_ms - cur[0].t_on_ms <= params.merge_ms:
            cur.append(n)
        else:
            clusters.append(cur)
            cur = [n]
    clusters.append(cur)

    onsets: list[Onset] = []
    for cl in clusters:
        if params.merge_anchor == "loudest":
            anchor = max(cl, key=lambda n: (n.velocity, -n.t_on_ms))
        else:
            anchor = min(cl, key=lambda n: n.t_on_ms)
        t = anchor.t_on_ms
        onsets.append(Onset(
            t_ms=t,
            velocity=max(n.velocity for n in cl),
            pitch=anchor.pitch,
            n_merged=len(cl),
            pitches=tuple(sorted(n.pitch for n in cl)),
            tick=anchor.t_on_tick,
            # ★ 簇里的音来自哪几条源轨（`Note.track` 本来就填好了，以前只是扔掉了）
            src_tracks=tuple(sorted({int(getattr(n, "track", -1)) for n in cl}
                                    - {-1})),
        ))

    # 2) 最小间隔稀疏化（贪心：保留更早的，丢掉离得太近的）
    if params.min_interval_ms > 0:
        kept: list[Onset] = []
        for o in onsets:
            if kept and o.t_ms - kept[-1].t_ms < params.min_interval_ms:
                # 若后一个明显更重，则替换（保留重音）
                if o.velocity > kept[-1].velocity + 20:
                    kept[-1] = o
                continue
            kept.append(o)
        onsets = kept

    # 3) 按力度截断
    if params.max_onsets and len(onsets) > params.max_onsets:
        keep_idx = sorted(range(len(onsets)),
                          key=lambda i: -onsets[i].velocity)[:params.max_onsets]
        onsets = [onsets[i] for i in sorted(keep_idx)]

    return onsets


def select_tracks(midi, spec: str = "auto", index: int = -1) -> list[Track]:
    """选要采的音轨。

    spec:
      "auto"      第一条「非鼓、有音」的轨（原来的行为）
      "all"       所有非鼓、有音的轨（多音轨采音）
      "all+drums" 所有有音的轨，含鼓
      "melodic"   同 "all"（留个语义别名）
    另外 index >= 0 时直接取该轨。
    """
    if index >= 0:
        return [t for t in midi.tracks if t.index == index]
    trks = [t for t in midi.tracks if t.notes]
    if spec == "all+drums":
        return trks
    mel = [t for t in trks if not t.is_drum_only()]
    if spec in ("all", "melodic"):
        return mel or trks
    return (mel or trks)[:1]


def build_onsets_multi(tracks: list[Track], params: OnsetParams) -> list[Onset]:
    """多音轨采音：把几条轨的音符合起来，再走同一套「同时音合并」。

    这一步的意义：单轨采音会因为「这段时间只有别的声部在响」留下空白音，
    逼着后面的求解器去扭轨道填时间。多轨合起来空白就少了。
    """
    notes: list[Note] = []
    for t in tracks:
        notes.extend(t.notes)
    return build_onsets(notes, params)


def notes_fill_gaps(primary: list[Note], fillers: list[Note],
                    gap_ms: float) -> list[Note]:
    """主轨为主；**只在主轨出现 > gap_ms 空白的地方**把其它轨的音补进来。

    这是「全采」和「只采一条」之间的折中：
      - 全采 → 把伴奏/内声部也采进来，那已经不是任何一条音乐线了
      - 只采 → 别的声部在响的地方变成空白音，求解器只能扭轨道去填
      - 补空白 → 节奏线还是主轨那条，只把「没人响」的地方填上
    """
    base = sorted(primary, key=lambda n: n.t_on_ms)
    if not base:
        return sorted(fillers, key=lambda n: n.t_on_ms)
    if gap_ms <= 0:
        return sorted(base + list(fillers), key=lambda n: (n.t_on_ms, -n.velocity))

    windows: list[tuple[float, float]] = []
    for i in range(len(base) - 1):
        a, b = base[i].t_on_ms, base[i + 1].t_on_ms
        if b - a > gap_ms:
            windows.append((a, b))
    if not windows:
        return base
    fs = sorted(fillers, key=lambda n: n.t_on_ms)
    extra = [n for n in fs if any(a < n.t_on_ms < b for a, b in windows)]
    out = base + extra
    out.sort(key=lambda n: (n.t_on_ms, -n.velocity))
    return out


def build_onsets_fill(primary: Track, fillers: list[Track], params: OnsetParams,
                      gap_ms: float = 600.0) -> list[Onset]:
    """主轨 + 其它轨补空白 -> onset 序列。"""
    other: list[Note] = []
    for t in fillers:
        other.extend(t.notes)
    return build_onsets(notes_fill_gaps(primary.notes, other, gap_ms), params)


def track_summary(trk: Track) -> str:
    if not trk.notes:
        return f"trk{trk.index}  (空)"
    lo = min(n.pitch for n in trk.notes)
    hi = max(n.pitch for n in trk.notes)
    span = max(n.t_off_ms for n in trk.notes) / 1000.0
    tags = []
    if trk.is_drum_only():
        tags.append("鼓轨")
    tags.append(f"ch{','.join(str(c) for c in trk.channels)}")
    return (f"trk{trk.index}  {trk.name or '(无名)':<12} {len(trk.notes):>5} 音  "
            f"音高 {lo}-{hi}  {' '.join(tags)}  跨度 {span:.0f}s")
