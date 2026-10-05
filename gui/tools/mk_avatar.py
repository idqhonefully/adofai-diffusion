#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""贡献者头像生成器（开源版）。

把贡献者头像做成"真·圆形"PNG，供「关于」页的贡献者卡片使用。
- ffmpeg 不在仓库内（见 EXTERNAL_ASSETS.md）：用环境变量 FFMPEG_BIN 指定，
  或放到 audio-sep/ffmpeg/ffmpeg.exe（便携包布局）。
- 源头像用环境变量 AVATAR_SRC 指定目录（默认 <仓库>/pict），文件名见 JOBS。
- 输出写到 <仓库>/gui/，全程不写死任何绝对路径。
"""
import os
import struct
import subprocess
import sys
import zlib

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)                       # .../gui 的上级 = 仓库根
FF = os.environ.get("FFMPEG_BIN") or os.path.join(REPO, "audio-sep", "ffmpeg", "ffmpeg.exe")
SRC_DIR = os.environ.get("AVATAR_SRC") or os.path.join(REPO, "pict")

SIZE = 64

# 源图 -> 输出文件名（两棵树同名）
JOBS = [
    ("LINIX099.jpg",     "avatar-linix.png"),
    ("iDQhonefully.jpg", "avatar-idq.png"),
]

# 输出目录：仓库内的 gui/
OUT_DIRS = [os.path.join(REPO, "gui")]


def load_rgb(path, size):
    vf = "crop='min(iw,ih)':'min(iw,ih)',scale=%d:%d:flags=lanczos" % (size, size)
    p = subprocess.run([FF, "-v", "error", "-i", path, "-vf", vf,
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg 失败: " + p.stderr.decode("utf-8", "replace"))
    buf = np.frombuffer(p.stdout, dtype=np.uint8)
    want = size * size * 3
    if buf.size != want:
        raise RuntimeError("像素数不对: got %d want %d" % (buf.size, want))
    return buf.reshape(size, size, 3)


def circle_alpha(size):
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float64)
    c = size / 2.0
    d = np.hypot(xx + 0.5 - c, yy + 0.5 - c)
    return np.round(np.clip(c - d + 0.5, 0.0, 1.0) * 255.0).astype(np.uint8)


def write_png_rgba(path, rgb, alpha):
    h, w = alpha.shape
    raw = np.empty((h, w * 4), dtype=np.uint8)
    raw[:, 0::4] = rgb[:, :, 0]
    raw[:, 1::4] = rgb[:, :, 1]
    raw[:, 2::4] = rgb[:, :, 2]
    raw[:, 3::4] = alpha
    body = b"".join(b"\x00" + raw[y].tobytes() for y in range(h))
    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    blob = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(body, 9)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(blob)
    return len(blob)


def read_png_rgba(path):
    b = open(path, "rb").read()
    if b[:8] != b"\x89PNG\r\n\x1a\n":
        raise RuntimeError("PNG 签名不对")
    i, idat, w, h, ct = 8, b"", 0, 0, 0
    while i < len(b):
        ln = struct.unpack(">I", b[i:i + 4])[0]
        tag, data = b[i + 4:i + 8], b[i + 8:i + 8 + ln]
        crc = struct.unpack(">I", b[i + 8 + ln:i + 12 + ln])[0]
        if crc != (zlib.crc32(tag + data) & 0xFFFFFFFF):
            raise RuntimeError("chunk %s CRC 校验失败" % tag)
        if tag == b"IHDR":
            w, h, _bd, ct = struct.unpack(">IIBB", data[:10])
        elif tag == b"IDAT":
            idat += data
        i += 12 + ln
    if ct != 6:
        raise RuntimeError("colortype 应为 6(RGBA)，实际 %d" % ct)
    body = zlib.decompress(idat)
    stride = w * 4
    out = np.empty((h, w, 4), dtype=np.uint8)
    for y in range(h):
        off = y * (stride + 1)
        if body[off] != 0:
            raise RuntimeError("第 %d 行 filter byte 非 0" % y)
        out[y] = np.frombuffer(body[off + 1:off + 1 + stride], dtype=np.uint8).reshape(w, 4)
    return out


def main():
    if not os.path.exists(FF):
        sys.exit("找不到 ffmpeg，请设环境变量 FFMPEG_BIN 或放到 audio-sep/ffmpeg/ffmpeg.exe")
    alpha = circle_alpha(SIZE)
    made = []
    for src_name, out_name in JOBS:
        src = os.path.join(SRC_DIR, src_name)
        if not os.path.exists(src):
            sys.exit("找不到头像源文件: " + src + "（用 AVATAR_SRC 指定目录）")
        rgb = load_rgb(src, SIZE)
        made.append((src_name, out_name, rgb))
        print("%-20s -> %-20s %dx%d" % (src_name, out_name, SIZE, SIZE))
    c = SIZE // 2
    assert alpha[c, c] == 255 and alpha[0, 0] == 0 and (alpha > 0).sum() > (alpha < 255).sum()
    for src_name, out_name, rgb in made:
        for out_dir in OUT_DIRS:
            if not os.path.isdir(out_dir):
                print("  ⚠ 跳过不存在的目录: " + out_dir)
                continue
            dst = os.path.join(out_dir, out_name)
            n = write_png_rgba(dst, rgb, alpha)
            img = read_png_rgba(dst)
            assert np.array_equal(img[:, :, 3], alpha)
            assert np.array_equal(img[:, :, :3], rgb)
            print("  %-46s %6d bytes" % (dst, n))
    print("\nALL PASS")


if __name__ == "__main__":
    main()
