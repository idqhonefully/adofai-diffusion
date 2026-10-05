#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成一段简单混合音频用于冒烟测试(纯标准库, 无需依赖)。"""
import wave
import math
import struct

SR = 44100


def tone(freq, dur, vol=0.3):
    n = int(dur * SR)
    return [vol * math.sin(2 * math.pi * freq * t / SR) for t in range(n)]


def write(path, samples):
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        frames = b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767)) for s in samples
        )
        w.writeframes(frames)


bass = tone(80, 4, vol=0.35)      # 低频 ~ 贝斯
voc = tone(440, 4, vol=0.25)      # 中频 ~ 人声近似
mix = [b + v for b, v in zip(bass, voc)]
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_mix.wav")
write(out, mix)
print("wrote", out)
