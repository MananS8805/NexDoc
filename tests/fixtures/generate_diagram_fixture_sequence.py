"""Generates a UML-sequence-diagram-shaped test fixture — a diagram grammar
the diagram adapter has never been tested against. Every existing fixture
(flowchart_clean.png, flowchart_with_circle.png) is boxes/diamonds/circles
connected by short, solid, mostly-horizontal-or-vertical arrows. A sequence
diagram is structurally different in ways worth testing empirically rather
than assuming the existing detector generalizes:

  - actor header boxes (ordinary rectangles — should behave like any other
    rectangle node)
  - long vertical DASHED lifelines hanging from each actor down the page
  - thin, tall activation bars (rectangles with a much more extreme aspect
    ratio than anything in the existing fixtures) sitting on a lifeline
  - horizontal message arrows between activation bars, some solid (a call)
    and some DASHED (a return, per UML convention) — real message topology,
    not just decoration

Produces tests/fixtures/sequence_diagram_clean.png: 3 actors (Client,
Server, Database), one activation bar each, 2 solid call arrows and 2
dashed return arrows — deliberately small so a wrong node/edge count is
easy to eyeball against the real drawing.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT_PATH = Path(__file__).parent / "sequence_diagram_clean.png"

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


def _actor_box(draw, cx, top, w, h, label, font):
    x0, y0, x1, y1 = cx - w / 2, top, cx + w / 2, top + h
    draw.rectangle([x0, y0, x1, y1], outline=LINE, width=3, fill=FILL)
    draw.text((cx, (y0 + y1) / 2), label, font=font, fill=LINE, anchor="mm")
    return (x0, y0, x1, y1)


def _dashed_vline(draw, x, y0, y1, dash=10, gap=8):
    y = y0
    while y < y1:
        y_end = min(y + dash, y1)
        draw.line([(x, y), (x, y_end)], fill=LINE, width=2)
        y += dash + gap


def _dashed_hline(draw, x0, x1, y, dash=10, gap=8):
    x = x0
    step = 1 if x1 >= x0 else -1
    while (x < x1 if step > 0 else x > x1):
        x_end = x + step * dash
        x_end = min(x_end, x1) if step > 0 else max(x_end, x1)
        draw.line([(x, y), (x_end, y)], fill=LINE, width=2)
        x += step * (dash + gap)


def _activation_bar(draw, cx, top, bottom, w=16):
    x0, x1 = cx - w / 2, cx + w / 2
    draw.rectangle([x0, top, x1, bottom], outline=LINE, width=3, fill=FILL)
    return (x0, top, x1, bottom)


def _arrowhead(draw, x0, y0, x1, y1, head_len=12):
    import math

    angle = math.atan2(y1 - y0, x1 - x0)
    for a in (angle - 0.4, angle + 0.4):
        hx = x1 - head_len * math.cos(a)
        hy = y1 - head_len * math.sin(a)
        draw.line([(x1, y1), (hx, hy)], fill=LINE, width=3)


def _solid_arrow(draw, x0, y, x1, label, font):
    draw.line([(x0, y), (x1, y)], fill=LINE, width=3)
    _arrowhead(draw, x0, y, x1, y)
    if label:
        mx = (x0 + x1) / 2
        draw.text((mx, y - 10), label, font=font, fill=LINE, anchor="mm")


def _dashed_arrow(draw, x0, y, x1, label, font):
    _dashed_hline(draw, x0, x1, y)
    _arrowhead(draw, x0, y, x1, y)
    if label:
        mx = (x0 + x1) / 2
        draw.text((mx, y - 10), label, font=font, fill=LINE, anchor="mm")


def build() -> Path:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    label_font = _font(20)
    edge_font = _font(16)

    actor_top, actor_h, actor_w = 40, 60, 180
    lifeline_bottom = 620

    client_x, server_x, db_x = 180, 450, 720
    _actor_box(draw, client_x, actor_top, actor_w, actor_h, "Client", label_font)
    _actor_box(draw, server_x, actor_top, actor_w, actor_h, "Server", label_font)
    _actor_box(draw, db_x, actor_top, actor_w, actor_h, "Database", label_font)

    lifeline_top = actor_top + actor_h
    for x in (client_x, server_x, db_x):
        _dashed_vline(draw, x, lifeline_top, lifeline_bottom)

    client_bar = _activation_bar(draw, client_x, 160, 560)
    server_bar = _activation_bar(draw, server_x, 200, 480)
    db_bar = _activation_bar(draw, db_x, 280, 380)

    _solid_arrow(draw, client_bar[2], 200, server_bar[0], "1. request", edge_font)
    _solid_arrow(draw, server_bar[2], 280, db_bar[0], "2. query", edge_font)
    _dashed_arrow(draw, db_bar[0], 380, server_bar[2], "3. result", edge_font)
    _dashed_arrow(draw, server_bar[0], 480, client_bar[2], "4. response", edge_font)

    image.save(OUT_PATH)
    return OUT_PATH


if __name__ == "__main__":
    path = build()
    print(f"Wrote {path}")
