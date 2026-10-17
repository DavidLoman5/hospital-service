"""Draws the rich-menu image. One-off tool - the bot itself never imports this.

Pillow lives unpacked in tools/pylibs (not installed system-wide) and the CJK font sits
next to it, so nothing here leaks into the bot's stdlib-only runtime.

    PYTHONPATH=tools/pylibs python3 tools/make_richmenu.py

LINE's full rich menu is 2500x1686: a 3x2 grid of big cells, each with one word large
enough to read without glasses. No emoji: there is no emoji font on this machine, so every glyph is either from Noto Sans
TC or drawn with primitives.
"""

import os
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT = os.path.join(ROOT, "tools", "NotoSansTC-Bold.otf")
OUT = os.path.join(ROOT, "tools", "richmenu.png")

W, H = 2500, 1686
COLS, ROWS = 3, 2
CELLS = [
    ("查進度", "現在看到幾號", "#1D4ED8"),
    ("我的提醒", "看全部、取消", "#1B7F3B"),
    ("回診", "記下回診日期", "#C2410C"),
    ("領藥", "慢性處方箋", "#7C3AED"),
    ("換醫院", "選要查的醫院", "#0E7490"),
    ("說明", "怎麼使用", "#4B5563"),
]
BG, INK, MUTED, DIVIDER = "#FFFFFF", "#1F2328", "#6B7280", "#E5E7EB"


def centred(draw, text, font, cx, y, fill):
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text((cx - (right - left) / 2 - left, y - top), text, font=font, fill=fill)


def icon_progress(draw, cx, cy, r, colour):
    """A clock face - 'what number is it up to right now'."""
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour)
    # Hands at roughly 12 and 4: two lines at right angles read as a corner, not a clock.
    draw.line([cx, cy, cx, cy - int(r * 0.55)], fill="#FFFFFF", width=13)
    draw.line([cx, cy, cx + int(r * 0.34), cy + int(r * 0.30)], fill="#FFFFFF", width=13)
    draw.ellipse([cx - 10, cy - 10, cx + 10, cy + 10], fill="#FFFFFF")


def icon_bell(draw, cx, cy, r, colour):
    """A bell, drawn from primitives.

    Every dimension is a fraction of r and the total height is checked against the disc:
    the first version's rim and clapper spilled out of the bottom of the circle.
    """
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour)
    w = int(r * 0.40)                    # half-width of the bell body
    dome_h, body_h, rim_h, gap, clap = w, int(r * 0.40), 14, 6, 22
    total = dome_h + body_h + rim_h + gap + clap
    top = cy - total // 2
    draw.pieslice([cx - w, top, cx + w, top + 2 * dome_h], 180, 360, fill="#FFFFFF")
    draw.rectangle([cx - w, top + dome_h, cx + w, top + dome_h + body_h], fill="#FFFFFF")
    rim_y = top + dome_h + body_h
    draw.rounded_rectangle([cx - w - 11, rim_y, cx + w + 11, rim_y + rim_h],
                           radius=7, fill="#FFFFFF")
    draw.ellipse([cx - clap // 2, rim_y + rim_h + gap,
                  cx + clap // 2, rim_y + rim_h + gap + clap], fill="#FFFFFF")


def icon_help(draw, cx, cy, r, colour, font):
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour)
    centred(draw, "?", font, cx, cy - int(r * 0.62), "#FFFFFF")


def icon_calendar(draw, cx, cy, r, colour):
    """A page with two binding rings and a grid of days."""
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour)
    w, h = int(r * 0.95), int(r * 0.85)
    left, top = cx - w // 2, cy - h // 2 + 6
    draw.rounded_rectangle([left, top, left + w, top + h], radius=10, fill="#FFFFFF")
    draw.rectangle([left, top + 18, left + w, top + 34], fill=colour)
    for dx in (int(w * 0.28), int(w * 0.72)):
        draw.rounded_rectangle([left + dx - 6, top - 14, left + dx + 6, top + 14],
                               radius=6, fill="#FFFFFF")
    cell = w // 4
    for row in range(2):
        for col in range(3):
            x = left + cell // 2 + col * cell + 6
            y = top + 48 + row * 26
            draw.rectangle([x, y, x + 18, y + 14], fill=colour)


def icon_pill(draw, cx, cy, r, colour):
    """A capsule on the diagonal, half filled."""
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour)
    pill = Image.new("RGBA", (2 * r, 2 * r), (0, 0, 0, 0))
    pd = ImageDraw.Draw(pill)
    pw, ph = int(r * 1.3), int(r * 0.52)
    x0, y0 = r - pw // 2, r - ph // 2
    pd.rounded_rectangle([x0, y0, x0 + pw, y0 + ph], radius=ph // 2, fill="#FFFFFF")
    pd.rounded_rectangle([x0 + 8, y0 + 8, x0 + pw // 2, y0 + ph - 8],
                         radius=(ph - 16) // 2, fill=colour)
    pill = pill.rotate(40, resample=Image.BICUBIC)
    draw._image.paste(pill, (cx - r, cy - r), pill)


def icon_hospital(draw, cx, cy, r, colour):
    """A plus sign - the sign on every hospital."""
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=colour)
    a, b = int(r * 0.52), int(r * 0.17)
    draw.rounded_rectangle([cx - b, cy - a, cx + b, cy + a], radius=6, fill="#FFFFFF")
    draw.rounded_rectangle([cx - a, cy - b, cx + a, cy + b], radius=6, fill="#FFFFFF")


def main():
    if not os.path.exists(FONT):
        sys.exit("missing font: %s" % FONT)
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    f_title = ImageFont.truetype(FONT, 132)
    f_sub = ImageFont.truetype(FONT, 58)
    f_qmark = ImageFont.truetype(FONT, 200)

    cell_w, cell_h = W // COLS, H // ROWS
    icon_r = 112
    icons = (icon_progress, icon_bell, icon_calendar, icon_pill, icon_hospital,
             lambda d, x, y, r, c: icon_help(d, x, y, r, c, f_qmark))

    for col in range(1, COLS):
        draw.line([cell_w * col, 60, cell_w * col, H - 60], fill=DIVIDER, width=4)
    draw.line([60, cell_h, W - 60, cell_h], fill=DIVIDER, width=4)

    for i, (title, sub, colour) in enumerate(CELLS):
        row, col = divmod(i, COLS)
        cx = cell_w * col + cell_w // 2
        top = cell_h * row
        icons[i](draw, cx, top + 290, icon_r, colour)
        centred(draw, title, f_title, cx, top + 460, INK)
        centred(draw, sub, f_sub, cx, top + 640, MUTED)

    img.save(OUT, "PNG", optimize=True)
    print("wrote %s  %dx%d  %.0f KB" % (OUT, W, H, os.path.getsize(OUT) / 1024))


if __name__ == "__main__":
    main()
