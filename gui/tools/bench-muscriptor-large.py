#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""bench-muscriptor-large.py —— MuScriptor large 档「五种推理方式」实测对比

目的：给"large 到底该怎么跑"一个**可复现的数字**，而不是感觉。
同一段音频、同一份权重、同一台机器，逐个跑完，量三样东西：

    1) 墙钟耗时（含模型加载）；
    2) 峰值显存（跑的时候 0.5s 采样一次 nvidia-smi，减去跑之前的基线）；
    3) 产物指纹（自研 MIDI 解析：音符数 / 末 tick / note_on 序列的 sha1）
       —— 指纹一致才说明换推理方式没有改变模型输出。

模式（都是真跑，不是估算）：
    cpu           原版，-d cpu                     （下限参照）
    cuda_fp32     原版，-d cuda                    （现状：整份 fp32 压满显卡）
    cuda_fp16     原版，-d cuda --dtype float16    （一行开关，权重减半）
    hybrid_auto   mus_hybrid，按可用显存自动切层
    hybrid_24     mus_hybrid，强制前 24 层上 GPU   （显存减半档，看层数-速度关系）
    hybrid_fp16   mus_hybrid，auto + fp16          （应满载 48/48）

用法：
    python bench-muscriptor-large.py --clip <wav> [--only cpu,cuda_fp16] [--json out.json]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # <REPO>
AUDIOSEP = ROOT / "audio-sep"
MUS_PY = AUDIOSEP / "mus_env" / "Scripts" / "python.exe"
HYBRID = AUDIOSEP / "mus_hybrid.py"
WEIGHTS = AUDIOSEP / "models" / "muscriptor" / "large" / "model.safetensors"

_MODES = {
    "cpu":         ("orig",   ["--device", "cpu"]),
    "cuda_fp32":   ("orig",   ["--device", "cuda"]),
    "cuda_fp16":   ("orig",   ["--device", "cuda", "--dtype", "float16"]),
    "hybrid_auto": ("hybrid", ["--gpu-layers", "auto"]),
    "hybrid_24":   ("hybrid", ["--gpu-layers", "24"]),
    "hybrid_48":   ("hybrid", ["--gpu-layers", "48"]),
    "hybrid_0":    ("hybrid", ["--gpu-layers", "0"]),
    "hybrid_fp16": ("hybrid", ["--gpu-layers", "auto", "--gpu-dtype", "float16"]),
}


# ---------------------------------------------------------------------------
# 自研 MIDI 解析（零依赖；提取 note_on 序列做指纹）
# ---------------------------------------------------------------------------


def _vlq(buf: bytes, i: int) -> tuple[int, int]:
    val = 0
    while True:
        b = buf[i]
        i += 1
        val = (val << 7) | (b & 0x7F)
        if not (b & 0x80):
            return val, i


def parse_midi(path: Path) -> dict:
    data = path.read_bytes()
    if data[:4] != b"MThd":
        raise ValueError("不是 MIDI 文件: %s" % path)
    fmt, ntrk, division = struct.unpack(">HHH", data[8:14])
    off = 14
    events = []          # (abs_tick, channel, note, velocity)
    programs = set()
    for _ in range(ntrk):
        if data[off : off + 4] != b"MTrk":
            break
        tlen = struct.unpack(">I", data[off + 4 : off + 8])[0]
        track = data[off + 8 : off + 8 + tlen]
        off += 8 + tlen
        tick, i, running = 0, 0, None
        while i < len(track):
            dt, i = _vlq(track, i)
            tick += dt
            status = track[i]
            if status & 0x80:
                i += 1
                running = status if status < 0xF0 else None
            else:
                status = running
            if status is None:
                break
            if status == 0xFF:
                if i + 1 >= len(track):
                    break
                mlen = track[i + 1]
                i += 2 + mlen
                continue
            if status in (0xF0, 0xF7):
                while i < len(track) and track[i] != 0xF7:
                    i += 1
                i += 1
                continue
            kind, chan = status & 0xF0, status & 0x0F
            if kind == 0xC0:
                programs.add(track[i])
                i += 1
            elif kind in (0x80, 0x90, 0xA0, 0xB0, 0xE0):
                d1, d2 = track[i], track[i + 1]
                i += 2
                if kind == 0x90 and d2 > 0:
                    events.append((tick, chan, d1, d2))
            elif kind == 0xD0:
                i += 1

    sig = hashlib.sha1(
        ";".join("%d,%d,%d,%d" % e for e in sorted(events)).encode()
    ).hexdigest()[:12]
    return {
        "format": fmt,
        "tracks": ntrk,
        "division": division,
        "notes": len(events),
        "last_tick": max((e[0] for e in events), default=0),
        "programs": sorted(programs),
        "sig": sig,
        "bytes": len(data),
    }


# ---------------------------------------------------------------------------
# 显存采样
# ---------------------------------------------------------------------------


def _gpu_used_mib() -> float | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        return float(out.stdout.strip().splitlines()[0])
    except Exception:
        return None


class GpuSampler(threading.Thread):
    def __init__(self, interval=0.5):
        super().__init__(daemon=True)
        self.interval = interval
        self.peak = 0.0
        # 🔴 别叫 self._stop：threading.Thread 自己有 _stop() 方法，同名会把它盖掉，
        #    join() 时抛 "TypeError: 'Event' object is not callable"
        self._stop_evt = threading.Event()

    def run(self):
        while not self._stop_evt.is_set():
            v = _gpu_used_mib()
            if v and v > self.peak:
                self.peak = v
            self._stop_evt.wait(self.interval)

    def stop(self):
        self._stop_evt.set()
        self.join(timeout=3)


# ---------------------------------------------------------------------------
# 跑一个模式
# ---------------------------------------------------------------------------


def build_cmd(mode: str, clip: Path, out: Path) -> list[str]:
    kind, extra = _MODES[mode]
    base = ["transcribe", str(clip), "-o", str(out), "--model", str(WEIGHTS)]
    if kind == "orig":
        return [str(MUS_PY), "-m", "muscriptor"] + base + extra
    return [str(MUS_PY), str(HYBRID)] + extra + base


def run_mode(mode: str, clip: Path, outdir: Path, timeout: int) -> dict:
    out = outdir / ("%s.mid" % mode)
    log = outdir / ("%s.log" % mode)
    if out.exists():
        out.unlink()
    cmd = build_cmd(mode, clip, out)

    base_mib = _gpu_used_mib() or 0.0
    sampler = GpuSampler()
    sampler.start()
    t0 = time.perf_counter()
    timed_out = False
    marks: dict[str, float] = {}
    # 逐行加 [+秒] 前缀：这样日志里能直接看出"加载模型"花了多久、"解码"花了多久
    with open(log, "w", encoding="utf-8") as fh:
        fh.write("$ " + " ".join(cmd) + "\n")
        fh.flush()
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        # 看门狗：读管道是阻塞的，只在 proc.wait() 上加 timeout 没用 —— 必须到点就 kill
        def _kill():
            nonlocal timed_out
            timed_out = True
            try:
                proc.kill()
            except Exception:
                pass

        watchdog = threading.Timer(timeout, _kill)
        watchdog.start()
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                el = time.perf_counter() - t0
                low = line.strip()
                if low.startswith("Loading model"):
                    marks.setdefault("load_start", el)
                elif low.startswith("Transcribing"):
                    marks.setdefault("load_end", el)
                fh.write("[+%7.1fs] %s" % (el, line))
                fh.flush()
            proc.wait()
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            watchdog.cancel()
    wall = time.perf_counter() - t0
    sampler.stop()

    res = {
        "mode": mode,
        "cmd": " ".join(cmd),
        "wall_s": round(wall, 1),
        "peak_gpu_mib": round(sampler.peak, 0),
        "peak_delta_mib": round(max(0.0, sampler.peak - base_mib), 0),
        "timeout": timed_out,
        "log": str(log),
    }
    if "load_start" in marks and "load_end" in marks:
        res["load_s"] = round(marks["load_end"] - marks["load_start"], 1)
        res["decode_s"] = round(wall - marks["load_end"], 1)
    else:
        res["load_s"] = None
        res["decode_s"] = None

    text = log.read_text(encoding="utf-8", errors="replace")
    gen = []
    for line in text.splitlines():
        if "generate total:" in line:
            try:
                gen.append(float(line.split("generate total:")[1].strip().rstrip("s")))
            except ValueError:
                pass
    res["chunks"] = len(gen)
    res["generate_s"] = round(sum(gen), 1)
    for line in text.splitlines():
        if "[mus_hybrid]" in line or "Traceback" in line or "Error" in line:
            res.setdefault("notes_log", []).append(line.strip())

    if out.exists():
        try:
            res["midi"] = parse_midi(out)
        except Exception as exc:  # noqa: BLE001
            res["midi_error"] = "%s: %s" % (type(exc).__name__, exc)
    else:
        res["midi"] = None
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clip", required=True)
    ap.add_argument("--outdir", default=str(AUDIOSEP / "bench"))
    ap.add_argument("--only", default="")
    ap.add_argument("--timeout", type=int, default=7200)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    clip = Path(args.clip)
    if not clip.is_file():
        print("找不到音频: %s" % clip, file=sys.stderr)
        return 2
    if not MUS_PY.is_file() or not WEIGHTS.is_file():
        print("环境不对：%s / %s" % (MUS_PY, WEIGHTS), file=sys.stderr)
        return 2
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    modes = [m.strip() for m in args.only.split(",") if m.strip()] or list(_MODES)
    results = []
    t_all = time.perf_counter()
    for m in modes:
        if m not in _MODES:
            print("跳过未知模式 %s" % m, file=sys.stderr)
            continue
        print("\n=== %s ===" % m, flush=True)
        r = run_mode(m, clip, outdir, args.timeout)
        results.append(r)
        mid = r.get("midi") or {}
        print(
            "  wall %.1fs (load %s / decode %s) | peak GPU %.0f MiB (Δ%.0f) "
            "| chunks %d gen %.1fs | notes %s sig %s"
            % (
                r["wall_s"], r["load_s"], r["decode_s"],
                r["peak_gpu_mib"], r["peak_delta_mib"],
                r["chunks"], r["generate_s"],
                mid.get("notes", "-"), mid.get("sig", "-"),
            ),
            flush=True,
        )

    total = time.perf_counter() - t_all
    print("\n========== 汇总（音频 %s） ==========" % clip.name)
    hdr = "%-12s %8s %8s %8s %8s %7s %7s %8s" % (
        "mode", "wall(s)", "load(s)", "decode(s)", "peakMiB", "chunks", "notes", "sig")
    print(hdr)
    print("-" * len(hdr))
    for r in results:
        mid = r.get("midi") or {}
        print("%-12s %8.1f %8s %8s %8.0f %7d %7s %8s" % (
            r["mode"], r["wall_s"],
            "-" if r["load_s"] is None else "%.1f" % r["load_s"],
            "-" if r["decode_s"] is None else "%.1f" % r["decode_s"],
            r["peak_gpu_mib"], r["chunks"], mid.get("notes", "-"), mid.get("sig", "-")))
    print("总耗时 %.1f min" % (total / 60))

    payload = {"clip": str(clip), "results": results, "total_s": round(total, 1)}
    if args.json:
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print("JSON → %s" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
