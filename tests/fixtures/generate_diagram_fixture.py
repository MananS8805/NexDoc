"""Generates the diagram-adapter test fixture: a clean, synthetic flowchart
drawn with Pillow — not a real-world hand-drawn or scanned diagram. Per
CLAUDE.md Phase 3, the detector here is honestly scoped to reliably handle
clean, generated diagrams like this one; it is not claimed to work on
messy or hand-drawn ones.

Produces tests/fixtures/flowchart_clean.png: a "vendor onboarding" flow
mirroring the shape the UI mock already established (start -> decision ->
two branches) — one start node (rectangle), one decision node (diamond),
two action nodes (rectangles), and three labeled arrows connecting them.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT_PATH = Path(__file__).parent / "flowchart_clean.png"

WIDTH, HEIGHT = 900, 700
BG = "white"
LINE = (20, 20, 20)
FILL = (255, 255, 255)


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype("C:/Windows/Fonts/arial.ttf", size)


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
    # arrowhead
    import math

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
    label_font = _font(20)
    edge_font = _font(16)

    start_box = _rect_node(draw, 450, 80, 300, 70, "Start: new vendor\nrequest", label_font)
    decision_box = _diamond_node(draw, 450, 260, 340, 130, "Credit check\npasses?", label_font)
    approve_box = _rect_node(draw, 220, 480, 260, 80, "Approve vendor", label_font)
    request_box = _rect_node(draw, 680, 480, 300, 80, "Request additional\ndocuments", label_font)

    _arrow(draw, 450, start_box[3], 450, decision_box[1], None, edge_font)
    _arrow(draw, decision_box[0] + 30, decision_box[3] - 20, approve_box[2] - 40, approve_box[1], "yes", edge_font)
    _arrow(draw, decision_box[2] - 30, decision_box[3] - 20, request_box[0] + 40, request_box[1], "no", edge_font)

    image.save(OUT_PATH)
    return OUT_PATH


if __name__ == "__main__":
    path = build()
    print(f"Wrote {path}")
