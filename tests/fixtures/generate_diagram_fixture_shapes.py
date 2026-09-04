"""Generates a second diagram-adapter test fixture, sibling to
`generate_diagram_fixture.py`, specifically to exercise circle/ellipse node
shapes — a shape type `flowchart_clean.png` never contains.

Produces tests/fixtures/flowchart_with_circle.png: a small "access request"
flow using the shape convention real flowcharts commonly use for terminals
(ellipse/circle) alongside the existing rectangle (process) and diamond
(decision) shapes:

    Start (ellipse, non-1:1 aspect ratio)
      -> Valid? (diamond)
           -- yes --> Approve (rectangle)
           -- no  --> End (circle, 1:1 aspect ratio)

Two distinct round shapes are included on purpose: an ellipse (start) proves
detection isn't accidentally circle-only, and a true circle (end) proves the
same code path handles the aspect-ratio-1 special case too. Drawn with
Pillow at the same high contrast / solid-line style as the existing fixture
— still a clean, computer-generated diagram, not a hand-drawn or scanned
one; see adapters/diagram/README.md's "Honest scope" section for why that
distinction matters.

Note: unlike generate_diagram_fixture.py (which hardcodes a Windows font
path and does not run on this Linux dev environment), this script looks up
a real installed TTF and falls back to Pillow's built-in bitmap font rather
than failing outright.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT_PATH = Path(__file__).parent / "flowchart_with_circle.png"

WIDTH, HEIGHT = 900, 700
BG = "white"
LINE = (20, 20, 20)
FILL = (255, 255, 255)

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "C:/Windows/Fonts/arial.ttf",
]


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def _ellipse_node(draw, cx, cy, w, h, label, font):
    x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
    draw.ellipse([x0, y0, x1, y1], outline=LINE, width=3, fill=FILL)
    draw.text((cx, cy), label, font=font, fill=LINE, anchor="mm")
    return (x0, y0, x1, y1)


def _rect_node(draw, cx, cy, w, h, label, font):
    x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
    draw.rectangle([x0, y0, x1, y1], outline=LINE, width=3, fill=FILL)
    draw.text((cx, cy), label, font=font, fill=LINE, anchor="mm")
    return (x0, y0, x1, y1)


def _diamond_node(draw, cx, cy, w, h, label, font):
    pts = [(cx, cy - h / 2), (cx + w / 2, cy), (cx, cy + h / 2), (cx - w / 2, cy)]
    draw.polygon(pts, outline=LINE, width=3, fill=FILL)
    draw.text((cx, cy), label, font=font, fill=LINE, anchor="mm")
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def _arrow(draw, x0, y0, x1, y1, label, font):
    draw.line([(x0, y0), (x1, y1)], fill=LINE, width=3)
    angle = math.atan2(y1 - y0, x1 - x0)
    head_len = 14
    for a in (angle - 0.4, angle + 0.4):
        hx = x1 - head_len * math.cos(a)
        hy = y1 - head_len * math.sin(a)
        draw.line([(x1, y1), (hx, hy)], fill=LINE, width=3)
    if label:
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        draw.text((mx + 8, my - 8), label, font=font, fill=LINE, anchor="lm")


def build() -> Path:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    label_font = _font(24)
    edge_font = _font(18)

    start_box = _ellipse_node(draw, 450, 100, 260, 110, "Start", label_font)
    decision_box = _diamond_node(draw, 450, 300, 340, 130, "Valid?", label_font)
    approve_box = _rect_node(draw, 220, 520, 260, 80, "Approve", label_font)
    end_box = _ellipse_node(draw, 680, 520, 150, 150, "End", label_font)

    _arrow(draw, 450, start_box[3], 450, decision_box[1], None, edge_font)
    _arrow(draw, decision_box[0] + 30, decision_box[3] - 20, approve_box[2] - 40, approve_box[1], "yes", edge_font)
    _arrow(draw, decision_box[2] - 30, decision_box[3] - 20, end_box[0] + 40, end_box[1], "no", edge_font)

    image.save(OUT_PATH)
    return OUT_PATH


if __name__ == "__main__":
    path = build()
    print(f"Wrote {path}")
