"""Generate site/static/og-image.png (1200x630) for social-media unfurls.

Layout mirrors the index intro: title + subtitle stacked left, skyline on the
right, beveled statusbar with the domain along the bottom.

Palette: cream canvas with muted-taupe text + skyline (matches the live site).
The bottom statusbar inverts to a dark fill (--text-muted) with cream text — a
deliberate echo of the Win95 chrome titlebar/statusbar contrast on the live
site.

Vertical layout: subtitle's last line bottom-aligns with the skyline's ground
line. Surround padding (cream margins around the content) is shrunk to ~70%
of geometric centre so the block doesn't float.

Typography matches the site's section headings (IBM Plex Mono SemiBold,
uppercase, ~0.09em tracking, --text-muted).

Re-run after palette or copy changes; commit the resulting PNG.
"""

import os
from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKYLINE_SRC = os.path.join(ROOT, "site", "static", "skyline-email.png")
OUT = os.path.join(ROOT, "site", "static", "og-image.png")
FONT_DIR = os.path.join(ROOT, "tools", "fonts")
FONT_SEMIBOLD = os.path.join(FONT_DIR, "IBMPlexMono-SemiBold.ttf")
FONT_REGULAR = os.path.join(FONT_DIR, "IBMPlexMono-Regular.ttf")

W, H = 1200, 630
STATUSBAR_H = 51

CREAM = (251, 250, 246)
TEXT_MUTED = (90, 87, 79)
BEVEL_DARK = (138, 134, 120)
NEAR_BLACK = (35, 33, 28)
WHITE = (255, 255, 255)

# Main canvas
BG = CREAM
FG = TEXT_MUTED              # text colour
BEVEL_OUT_LIGHT = WHITE
BEVEL_OUT_DARK = BEVEL_DARK

# Footer (inverted)
FOOTER_FILL = TEXT_MUTED
FOOTER_TEXT = CREAM
FOOTER_BEVEL_LIGHT = BEVEL_DARK   # lighter than the dark fill
FOOTER_BEVEL_DARK = NEAR_BLACK    # darker than the dark fill

# Content band — surround margins shrunk 30% from prior 100px → 70px
CONTENT_X0 = 70
CONTENT_X1 = W - 70
SKYLINE_W = 400
GUTTER = 60
TEXT_X1 = CONTENT_X1 - SKYLINE_W - GUTTER

TITLE_LINES = ["HEARING", "HEARINGS"]
SUBTITLE_LINES = [
    "SUMMARIES AND TRANSCRIPTS OF",
    "NEW YORK CITY COUNCIL HEARINGS.",
]
DOMAIN = "hearinghearings.nyc"


def bevel_outset(draw, x0, y0, x1, y1, light, dark, width=4):
    for i in range(width):
        draw.line([(x0 + i, y0 + i), (x1 - 1 - i, y0 + i)], fill=light)
        draw.line([(x0 + i, y0 + i), (x0 + i, y1 - 1 - i)], fill=light)
        draw.line([(x0 + i, y1 - 1 - i), (x1 - 1 - i, y1 - 1 - i)], fill=dark)
        draw.line([(x1 - 1 - i, y0 + i), (x1 - 1 - i, y1 - 1 - i)], fill=dark)


def bevel_inset(draw, x0, y0, x1, y1, light, dark):
    draw.line([(x0, y0), (x1 - 1, y0)], fill=dark)
    draw.line([(x0, y0), (x0, y1 - 1)], fill=dark)
    draw.line([(x0 + 1, y1 - 1), (x1 - 1, y1 - 1)], fill=light)
    draw.line([(x1 - 1, y0 + 1), (x1 - 1, y1 - 1)], fill=light)


def tracked_text(draw, xy, text, font, fill, tracking_px):
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        bbox = draw.textbbox((0, 0), ch, font=font)
        x += (bbox[2] - bbox[0]) + tracking_px
    return x


def tracked_width(draw, text, font, tracking_px):
    w = 0
    for ch in text:
        bbox = draw.textbbox((0, 0), ch, font=font)
        w += (bbox[2] - bbox[0]) + tracking_px
    return w - tracking_px


def main():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Outer raised bevel
    bevel_outset(d, 0, 0, W, H, BEVEL_OUT_LIGHT, BEVEL_OUT_DARK, width=4)

    # Auto-fit the (two-line) title to the available text-column width
    title_size = 120
    title_font = None
    while title_size >= 60:
        candidate = ImageFont.truetype(FONT_SEMIBOLD, title_size)
        track = max(2, int(title_size * 0.09))
        widest = max(
            tracked_width(d, line, candidate, track) for line in TITLE_LINES
        )
        if widest <= TEXT_X1 - CONTENT_X0:
            title_font = candidate
            title_track = track
            break
        title_size -= 4
    if title_font is None:
        title_font = ImageFont.truetype(FONT_SEMIBOLD, 60)
        title_track = 5
    title_line_h = int(title_size * 1.08)
    title_block_h = title_line_h * (len(TITLE_LINES) - 1) + title_size

    subtitle_size = 30
    subtitle_font = ImageFont.truetype(FONT_SEMIBOLD, subtitle_size)
    subtitle_track = max(2, int(subtitle_size * 0.09))
    subtitle_line_h = int(subtitle_size * 1.55)

    # Skyline sized first — its height drives the bottom anchor
    sky = Image.open(SKYLINE_SRC).convert("RGBA")
    src_w, src_h = sky.size
    scale = SKYLINE_W / src_w
    int_scale = max(1, round(scale))
    if abs(scale - int_scale) < 0.05:
        new_w = src_w * int_scale
        new_h = src_h * int_scale
    else:
        new_w = SKYLINE_W
        new_h = int(src_h * scale)
    sky_resized = sky.resize((new_w, new_h), Image.NEAREST)

    # Layout: subtitle-bottom and skyline-bottom share an anchor
    title_to_sub_gap = 44
    subtitle_block_h = subtitle_line_h * len(SUBTITLE_LINES)
    text_block_h = title_block_h + title_to_sub_gap + subtitle_block_h
    visual_block_h = max(text_block_h, new_h)

    content_top = 4
    content_bottom = H - STATUSBAR_H - 4
    available = content_bottom - content_top
    # Vertically centre — bigger title carries the visual weight
    top_pad = (available - visual_block_h) // 2
    block_top = content_top + top_pad
    bottom_anchor = block_top + visual_block_h  # subtitle and skyline both end here

    # Skyline anchored to bottom of the visual block
    sky_x = CONTENT_X1 - new_w
    sky_y = bottom_anchor - new_h
    img.paste(sky_resized, (sky_x, sky_y), sky_resized)

    # Subtitle: last line bottom-aligns to bottom_anchor
    sub_first_line_y = bottom_anchor - subtitle_block_h
    for i, line in enumerate(SUBTITLE_LINES):
        tracked_text(
            d,
            (CONTENT_X0, sub_first_line_y + i * subtitle_line_h),
            line,
            subtitle_font,
            FG,
            subtitle_track,
        )

    # Title sits gap above subtitle's first line, lines stacked
    title_first_y = sub_first_line_y - title_to_sub_gap - title_block_h
    for i, line in enumerate(TITLE_LINES):
        tracked_text(
            d,
            (CONTENT_X0, title_first_y + i * title_line_h),
            line,
            title_font,
            FG,
            title_track,
        )

    # Footer (dark fill, cream text) — echoes the Win95 chrome bar
    status_y = H - 4 - STATUSBAR_H
    d.rectangle([4, status_y, W - 5, H - 5], fill=FOOTER_FILL)
    cells = [(4, 320), (320, W - 320), (W - 320, W - 4)]
    for cx0, cx1 in cells:
        bevel_inset(
            d, cx0, status_y + 4, cx1, H - 4,
            light=FOOTER_BEVEL_LIGHT, dark=FOOTER_BEVEL_DARK,
        )

    domain_font = ImageFont.truetype(FONT_REGULAR, 17)
    bbox = d.textbbox((0, 0), DOMAIN, font=domain_font)
    dw = bbox[2] - bbox[0]
    dy = status_y + (STATUSBAR_H - (bbox[3] - bbox[1])) // 2 - bbox[1]
    d.text(((W - dw) // 2, dy), DOMAIN, font=domain_font, fill=FOOTER_TEXT)

    img.save(OUT, optimize=True)
    print(f"Wrote {OUT} ({W}x{H})  title size = {title_size}px")


if __name__ == "__main__":
    main()
