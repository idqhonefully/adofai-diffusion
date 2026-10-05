#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build the Windows boot-spinner WebP assets used by gui/index.html.

Why a font: Windows' own boot spinner is NOT a hand-drawn dot ring -- it is a run of
glyphs in the boot font, advanced one character per frame. Rendering those exact glyphs
is the only way to get a byte-faithful spinner.

  Win 8/10 style -> loader8.webp / loader8-light.webp : segoe_slboot.ttf, U+E052..U+E0CB
                                   (**122 frames -- the trailing 5 blanks are KEPT on
                                    purpose**, see below)
  Win 11  style -> loader11.webp : swept arc, cropped + accent-tinted from the official
                                   Windows11Loader.gif
  loader.webp (raw white Win11 arc) stays as the build source for loader11.webp.

🔴 Why the blank frames must NOT be trimmed (2026-09-30, 主人 restored them):
  U+E0C7..U+E0CB render with zero ink (verified by rendering each glyph -- ink=0 at both
  120px and 200px). An earlier round dropped them "so it would not blink" -- WRONG: the
  trim makes the last dot hand straight back to the first dot of the next lap, so the loop
  point snaps. Keeping them gives the pause Windows itself has => smooth, correct feel.
  The reference demo the user supplied also uses the full U+E052..E0CB range.

Outputs go to BOTH the dev tree and the portable package tree, because the running app
serves gui/ from the package (<REPO>/gui).

Run:  <REPO>/audio-sep/runtime/python.exe gui/tools/build_win_loader.py

Rendering rules (learned the hard way):
  * Every frame MUST be drawn at the SAME anchor (font metrics, anchor="mm").
    Re-centering each frame by its ink bbox makes the orbiting dots jitter.
  * A single global offset centers the union of all frames; per-frame offsets re-break it.
  * RGB is forced flat (never left as antialiased grey) -> no colour fringing. The flat
    colour is the *accent* itself, so the spinner matches the button it sits next to.
"""
import os
import struct
from PIL import Image, ImageDraw, ImageFont, ImageChops

HERE = os.path.dirname(os.path.abspath(__file__))
CANDIDATES = [
    os.path.join(HERE, "fonts", "segoe_slboot.ttf"),
    r"<PICT>/Windows11load/segoe_slboot.ttf",
    r"<PICT>/Windows11_LoaderGIF/loader.ttf",
]
FONT = next((p for p in CANDIDATES if os.path.exists(p)), None)

# ---- Win8/10 orbiting-dots spinner (used right next to the 开始生成 button) ----
DEV = r"<REPO>/gui/loader8.webp"
PKG = r"<REPO>/gui/loader8.webp"
DEV_L = r"<REPO>/gui/loader8-light.webp"
PKG_L = r"<REPO>/gui/loader8-light.webp"
START, END = 0xE052, 0xE0CB      # official range; trailing blanks KEPT (see header)
DUR = 33                         # ms/frame (~30fps, matches the reference demo default)
CANVAS, FS, OUT = 160, 120, 128

# ---- Win11 ProgressRing (stage-list "running" dot) ----
WIN11_SRC = r"<REPO>/gui/loader.webp"   # official arc, white (from Windows11Loader.gif)
WIN11_DEV = r"<REPO>/gui/loader11.webp"
WIN11_PKG = r"<REPO>/gui/loader11.webp"
WIN11_OUT = 64            # 16px on screen => 4x, crisp on HiDPI

# 🔴 Tint colours MUST track shell-ui.css's --fl-accent (dark / light blocks).
#    Deep/shallow themes need different blues: #4CC2FF washes out on a light card.
ACCENT_DARK = (76, 194, 255)    # --fl-accent dark  #4CC2FF
ACCENT_LIGHT = (15, 108, 189)   # --fl-accent light #0F6CBD


def make(cp):
    f = ImageFont.truetype(FONT, FS)
    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((CANVAS / 2, CANVAS / 2), chr(cp),
                             font=f, fill=(255, 255, 255, 255), anchor="mm")
    return img


def _write(out, dur, dsts):
    for dst in dsts:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        out[0].save(dst, save_all=True, append_images=out[1:],
                    duration=dur, loop=0, format="WEBP", quality=92, method=6)
        print("wrote", dst, os.path.getsize(dst), "bytes,", len(out), "frames")


def build_dots():
    """U+E052..U+E0CB glyph run -> two tinted animated WebPs (dark / light accent)."""
    frames = [make(cp) for cp in range(START, END + 1)]

    comp = Image.new("L", (CANVAS, CANVAS), 0)
    for fr in frames:
        comp = ImageChops.lighter(comp, fr.getchannel("A"))
    bx = comp.getbbox()
    dx = int(round(CANVAS / 2 - (bx[0] + bx[2]) / 2))
    dy = int(round(CANVAS / 2 - (bx[1] + bx[3]) / 2))
    print(f"dots: {len(frames)} frames, union bbox={bx} global offset=({dx},{dy})")

    outs = {}
    for rgb in (ACCENT_DARK, ACCENT_LIGHT):
        seq = []
        for fr in frames:
            canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
            canvas.paste(fr, (dx, dy), fr)
            small = canvas.resize((OUT, OUT), Image.LANCZOS)
            solid = Image.new("RGBA", small.size, rgb + (255,))
            solid.putalpha(small.getchannel("A"))
            seq.append(solid)
        outs[rgb] = seq

    _write(outs[ACCENT_DARK], DUR, (DEV, PKG))
    _write(outs[ACCENT_LIGHT], DUR, (DEV_L, PKG_L))
    return len(frames)


def build_win11_accent():
    """Official Win11 arc -> cropped to its content box + tinted to the Win11 accent blue.

    Used by #stageList's "running" indicator, where the old look was a CSS ring with
    border-top-color:transparent -- a web-ish fake spinner.

    Why crop: the official GIF leaves ~20% blank margin (arc outer dia = 84/100), so a
    16px container would render a ~13px ring. Cropping to the content square makes the
    arc's outer diameter equal the box, so it lines up exactly with the idle ring (.dot).

    Why tint: Win11's own ProgressRing is accent coloured. RGB is forced flat and only
    alpha is kept -> no grey/colour fringe. Tinted with the DARK accent; on the light
    theme's near-white panel a light blue is still clearly visible.
    """
    src = Image.open(WIN11_SRC)
    # 🔴 force Windows default ~30fps (33ms). Do NOT inherit the source GIF's
    #    60ms/frame (16.7fps) -- that produced the "too slow" per-item spinner
    #    the user complained about. Locking to DUR keeps loader11 in lockstep
    #    with loader8 (the 开始生成 button spinner), which is also 33ms.
    dur = DUR
    frames, comp = [], None
    for i in range(src.n_frames):
        src.seek(i)
        f = src.convert("RGBA")
        frames.append(f)
        a = f.getchannel("A").point(lambda v: 255 if v > 12 else 0)   # threshold: AA edge out
        comp = a if comp is None else ImageChops.lighter(comp, a)
    bx = comp.getbbox()
    side = max(bx[2] - bx[0], bx[3] - bx[1])
    cx0, cy0 = (bx[0] + bx[2]) / 2.0, (bx[1] + bx[3]) / 2.0
    box = (int(round(cx0 - side / 2.0)), int(round(cy0 - side / 2.0)),
           int(round(cx0 + side / 2.0)), int(round(cy0 + side / 2.0)))
    print(f"win11 src: {src.n_frames} frames, {dur}ms, bbox={bx} -> crop {box} side={side}")

    out = []
    for f in frames:
        c = f.crop(box).resize((WIN11_OUT, WIN11_OUT), Image.LANCZOS)
        solid = Image.new("RGBA", c.size, ACCENT_DARK + (255,))
        solid.putalpha(c.getchannel("A"))
        out.append(solid)

    _write(out, dur, (WIN11_DEV, WIN11_PKG))


def verify(dst, nframes, dur):
    """Assert on the CONTAINER, never on PIL's n_frames.

    🔴 PIL reports n_frames = 118 for these files even though the encode is correct:
       libwebp MERGES consecutive identical frames, so the 5 trailing blank frames are
       stored as ONE frame whose duration is 5x33 = 165ms (checked by parsing the ANMF
       chunks). Frame count is therefore NOT an invariant -- total cycle time is, plus
       the fact that the final frame really is fully transparent (the end-of-lap pause
       that makes the loop feel like Windows instead of snapping).
    """
    d = open(dst, "rb").read()
    i, durs = 12, []
    while i + 8 <= len(d):
        fourcc = d[i:i+4]
        size = struct.unpack("<I", d[i+4:i+8])[0]
        if fourcc == b"ANMF":
            durs.append(int.from_bytes(d[i+8+12:i+8+15], "little"))
        i += 8 + size + (size & 1)
    total = sum(durs)
    im = Image.open(dst)
    im.seek(len(durs) - 1)
    last_ink = sum(1 for v in im.getchannel("A").get_flattened_data() if v > 10)
    print(f"  verify {os.path.basename(dst)}: ANMF={len(durs)} total={total}ms "
          f"(nominal {nframes}x{dur}={nframes * dur}ms) last_frame_ink={last_ink}")
    assert total == nframes * dur, "cycle time wrong -> trailing blanks were lost"
    assert last_ink == 0, "last frame is not blank -> the end-of-lap pause is gone"


def main():
    if not FONT:
        raise SystemExit("segoe_slboot.ttf not found in: " + repr(CANDIDATES))
    print("font:", FONT)
    n = build_dots()
    for dst in (DEV, PKG, DEV_L, PKG_L):
        verify(dst, n, DUR)
    build_win11_accent()


if __name__ == "__main__":
    main()
