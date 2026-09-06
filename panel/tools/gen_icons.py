"""
Generate outline-style status icons for the CrowPanel HMI.

Rasterizes at 16x supersample then box-downsamples to 20x20 8-bit alpha,
emitting both editable source PNGs and an LVGL LV_IMG_CF_ALPHA_8BIT C file.
"""
import os
from PIL import Image, ImageDraw

SIZE = 20
S = 16               # supersample factor
W = SIZE * S

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets", "icons")
OUT_C = os.path.join(ROOT, "src", "ui_icons.c")
os.makedirs(ASSETS, exist_ok=True)


def new():
    img = Image.new("L", (W, W), 0)
    return img, ImageDraw.Draw(img)


def s(v):
    return v * S


def box(x0, y0, x1, y1):
    return [s(x0), s(y0), s(x1), s(y1)]


def circle(cx, cy, r):
    return [s(cx - r), s(cy - r), s(cx + r), s(cy + r)]


# ---------------------------------------------------------------- camera
def draw_camera():
    img, d = new()
    # outer silhouette: body + viewfinder bump
    d.rounded_rectangle(box(1.3, 6.0, 18.7, 16.8), radius=s(2.6), fill=255)
    d.rounded_rectangle(box(4.2, 3.4, 8.8, 7.5), radius=s(1.1), fill=255)
    # punch interior (bump punch overlaps body punch so they merge cleanly)
    d.rounded_rectangle(box(2.8, 7.5, 17.2, 15.3), radius=s(1.4), fill=0)
    d.rounded_rectangle(box(5.6, 4.9, 7.4, 8.2), radius=s(0.5), fill=0)
    # lens ring
    d.ellipse(circle(10.0, 11.4, 3.5), fill=255)
    d.ellipse(circle(10.0, 11.4, 2.1), fill=0)
    return img


# ------------------------------------------------------------------- mic
def draw_mic():
    img, d = new()
    # capsule
    d.rounded_rectangle(box(7.4, 1.8, 12.6, 12.2), radius=s(2.6), fill=255)
    d.rounded_rectangle(box(8.9, 3.3, 11.1, 10.7), radius=s(1.1), fill=0)
    # cradle (lower half arc)
    d.arc(circle(10.0, 10.2, 4.6), start=0, end=180, fill=255, width=int(s(1.5)))
    # stem + base
    d.line([s(10.0), s(14.6), s(10.0), s(17.4)], fill=255, width=int(s(1.5)))
    d.line([s(6.6), s(17.9), s(13.4), s(17.9)], fill=255, width=int(s(1.5)))
    # Mute slash. Like the speaker, this icon is only ever drawn to report a
    # mute, so the glyph itself has to say so -- a plain mic reads as "mic".
    # The cradle spans x 5.4..14.6, leaving no room for a cross beside it as
    # the speaker has, so the strike goes through instead.
    #
    # Punched first at a wider stroke to carve a transparent gutter, then
    # redrawn solid inside it: without the gutter the slash merges into the
    # capsule and cradle strokes it crosses and stops reading as a separate
    # mark at 20px.
    slash = [s(3.8), s(3.8), s(16.2), s(16.2)]
    d.line(slash, fill=0, width=int(s(2.9)))
    d.line(slash, fill=255, width=int(s(1.5)))
    return img


# --------------------------------------------------------------- speaker
def _speaker_cone(d, dx=0.0):
    cone = [(3.6, 7.9), (7.2, 7.9), (11.2, 3.7), (11.2, 16.3), (7.2, 12.1), (3.6, 12.1)]
    pts = [(s(x + dx), s(y)) for x, y in cone]
    pts.append(pts[0])
    d.line(pts, fill=255, width=int(s(1.5)), joint="curve")


def draw_speaker():
    """Speaker actively playing: cone + sound wave.

    A wave arc is the right vocabulary for "sound is coming out" -- it was only
    the wrong one for a mute indicator, where it read as "low volume" because
    there was no unmuted variant on screen to contrast it against.
    """
    img, d = new()
    _speaker_cone(d)
    d.arc(circle(11.4, 10.0, 4.8), start=-50, end=50, fill=255, width=int(s(1.5)))
    return img


def draw_speaker_muted():
    """Speaker muted: cone + cross, no wave.

    Cone sits 1px left of the plain variant to open room for the cross.
    """
    img, d = new()
    _speaker_cone(d, dx=-1.0)
    cx, cy, r = 15.0, 10.0, 2.7
    d.line([s(cx - r), s(cy - r), s(cx + r), s(cy + r)], fill=255, width=int(s(1.5)))
    d.line([s(cx - r), s(cy + r), s(cx + r), s(cy - r)], fill=255, width=int(s(1.5)))
    return img


ICONS = [
    ("camera", draw_camera),
    ("mic", draw_mic),
    ("speaker", draw_speaker),
    ("speaker_muted", draw_speaker_muted),
]


def to_c_array(name, img):
    px = list(img.getdata())
    lines = []
    for i in range(0, len(px), 16):
        chunk = ", ".join("0x%02x" % v for v in px[i:i + 16])
        lines.append("    " + chunk + ",")
    body = "\n".join(lines)
    return f"""
static const LV_ATTRIBUTE_MEM_ALIGN uint8_t ui_img_{name}_map[] = {{
{body}
}};

const lv_img_dsc_t ui_img_{name} = {{
    .header.cf = LV_IMG_CF_ALPHA_8BIT,
    .header.always_zero = 0,
    .header.reserved = 0,
    .header.w = {SIZE},
    .header.h = {SIZE},
    .data_size = {SIZE * SIZE},
    .data = ui_img_{name}_map,
}};
"""


parts = []
for name, fn in ICONS:
    big = fn()
    small = big.resize((SIZE, SIZE), Image.BOX)
    small.save(os.path.join(ASSETS, f"{name}.png"))
    parts.append(to_c_array(name, small))
    print(f"{name}: max alpha {max(small.getdata())}, nonzero px "
          f"{sum(1 for v in small.getdata() if v)}")

header = """/**
 * @file ui_icons.c
 * Status-bar icon image assets for the CrowPanel 2.8" HMI.
 *
 * GENERATED FILE - do not hand-edit.
 * Source artwork: assets/icons/ (regenerate with tools/gen_icons.py)
 *
 * Format: LV_IMG_CF_ALPHA_8BIT, 20x20, 400 bytes each.
 * Alpha-only images are blended using the object's `img_recolor` style as a
 * solid colour, with these bytes acting as an antialiased coverage mask, so a
 * single asset serves both the idle and active tint. Callers MUST also set
 * `img_recolor_opa` to LV_OPA_COVER -- LVGL ignores `img_recolor` when the
 * recolor opacity is zero and the icon would silently render black.
 */

#include "ui_icons.h"

#ifndef LV_ATTRIBUTE_MEM_ALIGN
#define LV_ATTRIBUTE_MEM_ALIGN
#endif
"""

with open(OUT_C, "w", encoding="utf-8") as f:
    f.write(header + "".join(parts))
print("wrote", OUT_C)
