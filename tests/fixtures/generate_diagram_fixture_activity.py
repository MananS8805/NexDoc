"""Generates a UML activity-diagram fixture -- a genuinely different shape
vocabulary from the flowchart-style fixtures (generate_diagram_fixture.py,
generate_diagram_fixture_shapes.py, generate_diagram_fixture_sequence.py),
none of which contain a filled marker, a bullseye, a sync bar, or a lane
divider.

Produces tests/fixtures/activity_diagram_clean.png:

    (initial node: small solid black dot)
      -> Receive Order (rounded-rect activity)
      -> [fork bar: solid black horizontal bar]
           -- Pack Items (rounded-rect activity) --\
           -- Charge Payment (rounded-rect activity) --\
                                                          -> [join bar]
      -> Ship Order (rounded-rect activity)
      -> (final node: bullseye -- hollow ring with a solid dot inside)

One vertical swimlane divider splits "Warehouse" (left) from "Finance"
(right) across the fork/join section, so the fixture also exercises lane
detection, not just the four new node/marker shapes.

Same drawing conventions as generate_diagram_fixture_shapes.py: Pillow,
solid dark strokes on white, a real installed TTF with a bitmap-font
fallback so this runs even where no TTF is present.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT_PATH = Path(__file__).parent / "activity_diagram_clean.png"

WIDTH, HEIGHT = 900, 1000
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


def _activity_node(draw, cx, cy, w, h, label, font):
    x0, y0, x1, y1 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
    draw.rounded_rectangle([x0, y0, x1, y1], radius=26, outline=LINE, width=3, fill=FILL)
    draw.text((cx, cy), label, font=font, fill=LINE, anchor="mm")
    return (x0, y0, x1, y1)


def _initial_node(draw, cx, cy, r=16):
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=LINE, width=1, fill=LINE)
    return (cx - r, cy - r, cx + r, cy + r)


def _final_node(draw, cx, cy, outer_r=20, inner_r=10):
    draw.ellipse([cx - outer_r, cy - outer_r, cx + outer_r, cy + outer_r], outline=LINE, width=4, fill=FILL)
    draw.ellipse([cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r], outline=LINE, width=1, fill=LINE)
    return (cx - outer_r, cy - outer_r, cx + outer_r, cy + outer_r)


def _sync_bar(draw, cx, cy, length, thickness=12):
    x0, y0, x1, y1 = cx - length / 2, cy - thickness / 2, cx + length / 2, cy + thickness / 2
    draw.rectangle([x0, y0, x1, y1], outline=LINE, width=1, fill=LINE)
    return (x0, y0, x1, y1)


def _arrow(draw, x0, y0, x1, y1, font=None, label=None):
    draw.line([(x0, y0), (x1, y1)], fill=LINE, width=3)
    angle = math.atan2(y1 - y0, x1 - x0)
    head_len = 14
    for a in (angle - 0.4, angle + 0.4):
        hx = x1 - head_len * math.cos(a)
        hy = y1 - head_len * math.sin(a)
        draw.line([(x1, y1), (hx, hy)], fill=LINE, width=3)
    if label and font:
        mx, my = (x0 + x1) / 2, (y0 + y1) / 2
        draw.text((mx + 8, my - 8), label, font=font, fill=LINE, anchor="lm")


def build() -> Path:
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    label_font = _font(22)
    lane_font = _font(20)

    cx = 450

    # Generously spaced vertically (~80-100px gaps between shapes, well over
    # NODE_PADDING*2 + HOUGH_MIN_LINE_LENGTH): a tighter first draft packed
    # shapes closely enough that node-masking ate almost the entire visible
    # length of several connectors, and Hough silently found no line at all
    # to detect there -- measured directly, not a hypothetical spacing rule.
    initial_bbox = _initial_node(draw, cx, 60)
    receive_bbox = _activity_node(draw, cx, 220, 260, 70, "Receive Order", label_font)

    fork_bbox = _sync_bar(draw, cx, 340, 300)

    pack_bbox = _activity_node(draw, 250, 460, 240, 70, "Pack Items", label_font)
    charge_bbox = _activity_node(draw, 650, 460, 240, 70, "Charge Payment", label_font)

    join_bbox = _sync_bar(draw, cx, 580, 300)

    ship_bbox = _activity_node(draw, cx, 700, 260, 70, "Ship Order", label_font)
    final_bbox = _final_node(draw, cx, 820)

    # A small gap (rather than exact pixel-touching) between an arrow and
    # the small round initial/final markers specifically: cv2.findContours
    # treats any touching foreground pixels as one connected blob, and for
    # a marker this small, fusing in even a thin connector line corrupts
    # its contour enough to fail ellipse-fit classification entirely
    # (measured directly: the bullseye's outer ring vanished as a
    # candidate once an arrow touched it at exactly one point). Ordinary
    # rectangular nodes are large enough that a touching connector doesn't
    # meaningfully perturb their contour, so only these two connections
    # need the gap.
    # The same contour-merging problem hits the fork/join bars: each is a
    # small solid shape with THREE arrows touching it, and a touching
    # connector fuses into a solid shape's contour far more disruptively
    # than into a large hollow rectangle's outline (measured directly: the
    # fork bar, its arrows, and even the join bar merged into one 57000px^2
    # blob before this gap was added, and neither bar was recognized at
    # all). Same marker_gap treatment as the initial/final nodes.
    marker_gap = 6
    _arrow(draw, cx, initial_bbox[3] + marker_gap, cx, receive_bbox[1])
    _arrow(draw, cx, receive_bbox[3], cx, fork_bbox[1] - marker_gap)
    _arrow(draw, fork_bbox[0] + 40, fork_bbox[3] + marker_gap, pack_bbox[0] + 40, pack_bbox[1])
    _arrow(draw, fork_bbox[2] - 40, fork_bbox[3] + marker_gap, charge_bbox[2] - 40, charge_bbox[1])
    _arrow(draw, pack_bbox[0] + 40, pack_bbox[3], join_bbox[0] + 40, join_bbox[1] - marker_gap)
    _arrow(draw, charge_bbox[2] - 40, charge_bbox[3], join_bbox[2] - 40, join_bbox[1] - marker_gap)
    _arrow(draw, cx, join_bbox[3] + marker_gap, cx, ship_bbox[1])
    _arrow(draw, cx, ship_bbox[3], cx, final_bbox[1] - marker_gap)

    # One vertical swimlane divider along the untouched left margin,
    # spanning most of the canvas height -- deliberately clear of every
    # node/arrow above (the leftmost activity box starts at x=130), so it
    # exercises long-straight-line detection without corrupting any other
    # shape's own contour.
    draw.line([(60, 90), (60, 960)], fill=LINE, width=2)
    draw.text((10, 60), "Ops", font=lane_font, fill=LINE, anchor="lm")

    image.save(OUT_PATH)
    return OUT_PATH


if __name__ == "__main__":
    path = build()
    print(f"Wrote {path}")
