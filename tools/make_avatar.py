"""Draws candidate profile pictures for the LINE account.

Deliberately NOT the hospital's logo or name-mark: this is an unofficial personal tool,
and a profile picture that looks official would invite exactly the trust it has not
earned. The visual language borrowed instead is the queue display itself.

    PYTHONPATH=tools/pylibs python3 tools/make_avatar.py           # contact sheet
    PYTHONPATH=tools/pylibs python3 tools/make_avatar.py 2         # render variant 2 alone
"""

import os
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT = os.path.join(ROOT, "tools", "NotoSansTC-Bold.otf")
S = 640                                   # LINE renders the avatar as a circle


def centred(draw, text, font, cx, cy, fill):
    l, t, r, b = draw.textbbox((0, 0), text, font=font)
    draw.text((cx - (r - l) / 2 - l, cy - (b - t) / 2 - t), text, font=font, fill=fill)


def variant_1(img, draw):
    """Call-board: dark panel, glowing number - what you stare at in a waiting room."""
    draw.rectangle([0, 0, S, S], fill="#12213F")
    draw.rounded_rectangle([88, 150, S - 88, 400], radius=28, fill="#0A1730",
                           outline="#26406F", width=5)
    centred(draw, "88", ImageFont.truetype(FONT, 168), S // 2, 274, "#4DD07A")
    centred(draw, "看診進度", ImageFont.truetype(FONT, 76), S // 2, 492, "#FFFFFF")


def variant_2(img, draw):
    """Ticket stub - the paper slip the number is printed on."""
    draw.rectangle([0, 0, S, S], fill="#2E6BE6")
    draw.rounded_rectangle([96, 128, S - 96, S - 128], radius=32, fill="#FFFFFF")
    centred(draw, "看診號", ImageFont.truetype(FONT, 58), S // 2, 214, "#6B7280")
    centred(draw, "88", ImageFont.truetype(FONT, 190), S // 2, 348, "#2E6BE6")
    draw.rounded_rectangle([170, 442, S - 170, 452], radius=5, fill="#E5E7EB")


def variant_3(img, draw):
    """Bell on a clock face - the notification, not the lookup."""
    draw.rectangle([0, 0, S, S], fill="#F4F7FC")
    cx = cy = S // 2
    draw.ellipse([cx - 210, cy - 210, cx + 210, cy + 210], fill="#2E6BE6")
    draw.ellipse([cx - 168, cy - 168, cx + 168, cy + 168], fill="#FFFFFF")
    w = 78
    dome_h, body_h, rim_h, gap, clap = w, 78, 26, 12, 40
    total = dome_h + body_h + rim_h + gap + clap
    top = cy - total // 2
    draw.pieslice([cx - w, top, cx + w, top + 2 * dome_h], 180, 360, fill="#2E6BE6")
    draw.rectangle([cx - w, top + dome_h, cx + w, top + dome_h + body_h], fill="#2E6BE6")
    rim_y = top + dome_h + body_h
    draw.rounded_rectangle([cx - w - 22, rim_y, cx + w + 22, rim_y + rim_h],
                           radius=13, fill="#2E6BE6")
    draw.ellipse([cx - clap // 2, rim_y + rim_h + gap,
                  cx + clap // 2, rim_y + rim_h + gap + clap], fill="#2E6BE6")


VARIANTS = [variant_1, variant_2, variant_3]


def render(i):
    img = Image.new("RGB", (S, S), "#FFFFFF")
    VARIANTS[i](img, ImageDraw.Draw(img))
    return img


def main(argv):
    if argv and argv[0].isdigit():
        n = int(argv[0])
        out = os.path.join(ROOT, "tools", "avatar.png")
        render(n - 1).save(out, "PNG", optimize=True)
        return print("wrote %s (variant %d) %dx%d" % (out, n, S, S))

    # Contact sheet, with each option shown circle-cropped the way LINE will display it.
    pad, label_h = 40, 70
    sheet = Image.new("RGB", (S * 3 + pad * 4, S + pad * 2 + label_h), "#FFFFFF")
    d = ImageDraw.Draw(sheet)
    f = ImageFont.truetype(FONT, 44)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, S, S], fill=255)
    for i in range(3):
        x = pad + i * (S + pad)
        sheet.paste(render(i), (x, pad), mask)
        centred(d, "選項 %d" % (i + 1), f, x + S // 2, pad + S + label_h // 2, "#1F2328")
    out = os.path.join(ROOT, "tools", "avatar-options.png")
    sheet.save(out, "PNG", optimize=True)
    print("wrote %s" % out)


if __name__ == "__main__":
    main(sys.argv[1:])
