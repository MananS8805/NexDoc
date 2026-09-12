"""Diagram adapter: .png/.jpg -> common schema.

Detection + reconstruction, per CLAUDE.md Phase 3 — deliberately NOT a
single raw VLM prompt against the whole image. Shape detection (OpenCV
contours) finds nodes; line detection (Hough transform) finds connectors;
nodes and edges are reconstructed into the schema from that geometry.
Label text (inside nodes, alongside edges) goes through the same OCR path
as the document adapter — this adapter has no separate text-reading logic
of its own for labels.

Public entry points, matching the other adapters' shape:
    parse_diagram(path)   -> generator yielding Region objects, one per
                              detected node then one per detected edge.
    build_artifact(path)  -> dict, the full common-schema artifact.

Honestly scoped (see README): this reliably handles clean, computer-drawn
diagrams like the test fixture — solid rectangles/diamonds, solid-line
connectors, no visual noise. It is not claimed to handle hand-drawn or
photographed real-world diagrams; that would need a fundamentally more
robust detector (trained shape/arrow detection, not classical contour +
Hough), which is out of scope for this prototype.

Node detection: cv2.findContours + approxPolyDP. A closed contour with
~4 vertices and a large enclosed area is a candidate node; whether its
edges are axis-aligned (rectangle) or diagonal (diamond/decision) comes
from the actual angle of each edge relative to horizontal — a real
geometric signal, not a guess.

UML activity-diagram shapes (initial/final nodes, fork/join bars, rounded
activity boxes, swimlanes) are detected via the same contour geometry plus
one new signal fill_ratio alone can't provide: how dark a shape's own
interior is in the source binary image, which tells a solid-filled glyph
apart from a hollow outline of the identical silhouette. See README's "How
detection actually works" for the full breakdown.

Edge detection: node bounding boxes are masked out of the binary image
first, so cv2.HoughLinesP only sees connector strokes, never a node's own
border. Each retained line segment is matched to the two nearest nodes by
endpoint distance (real signal: how close is this endpoint to a node's
boundary), and its direction (from -> to) is inferred from which endpoint
has denser dark-pixel mass nearby — a real proxy for "this is where the
arrowhead is," not an assumption about diagrams flowing top-to-bottom.

Confidence:
    - nodes: driven by how clean the polygon approximation is (exactly 4
      vertices, low corner-angle error) — a real detection-quality signal.
    - edges: driven mainly by how clearly one endpoint shows denser,
      arrowhead-like pixel convergence than the other (see "Edge detection"
      above) — how confidently the *direction* claim can be made — with a
      secondary penalty for a poor endpoint-to-node geometric match.
      Endpoint-to-node distance alone was tried first and rejected: on the
      test fixture every arrow was drawn with a similarly small gap to its
      target node, so that signal came out nearly identical across all
      three edges — the exact "scored the same, not independently verified"
      failure this adapter is supposed to avoid. Edge confidence is capped
      below node confidence by construction either way, because "there is a
      shape here" is a fundamentally more certain claim than "this shape
      connects to that one in this direction" — reconstructed topology, not
      a direct read.
    - label text: EasyOCR's own per-detection confidence, exactly like the
      document adapter — or 0.0 with text=None when the OCR engine isn't
      available.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np

from adapters.common.ocr import ocr_engine_status, read_label
from adapters.common.schema import Artifact, Check, Region, Relationship, Source, sha256_of_file

ADAPTER_NAME = "Diagram adapter"
ADAPTER_VERSION = "0.1.0"

MIN_NODE_AREA = 3000  # px^2 — the primary "this is definitely a real shape" floor
# A hard-coded absolute floor alone has a real, diagnosed failure mode: a UML
# sequence diagram's activation bar can be legitimately real and much
# smaller than a typical flowchart box (measured case: 1600px^2 vs this
# 3000px^2 constant, on tests/fixtures/sequence_diagram_clean.png — see
# adapters/diagram/README.md's "Known limitations"/"Fixed bugs"). Lowering
# MIN_NODE_AREA itself to rescue that one case would risk new noise false-
# positives on every other image, since it's a fixed floor with no
# knowledge of what "normal" looks like in a given picture. Instead,
# NOISE_FLOOR_AREA is a much smaller absolute floor (true single/few-pixel
# noise never clears it), and a second, relative check below rescues a
# candidate between the two floors only when it's a real fraction of the
# size of shapes *this same image* already confirmed are real nodes — a
# per-image-relative signal, not a new one-size-fits-all constant.
NOISE_FLOOR_AREA = 200  # px^2 — below this, never a real shape in any image, just noise
RELATIVE_MIN_AREA_FRACTION = 0.10  # a sub-MIN_NODE_AREA candidate is rescued if area >= this fraction of the median confirmed-node area in the same image
CONFIDENCE_RELATIVE_AREA_PENALTY = 0.10  # subtracted from a relative-rescued node's confidence — "smaller than every other shape found here" is itself a real uncertainty signal
AXIS_ALIGNED_TOLERANCE_DEG = 20  # how far from horizontal/vertical an edge can be and still count as "axis-aligned"
NODE_PADDING = 10  # px, grown around a node's bbox before masking it out for edge detection
HOUGH_MIN_LINE_LENGTH = 40  # px — shorter than this is treated as arrowhead clutter, not a connector shaft
HOUGH_MAX_LINE_GAP = 8
ENDPOINT_TO_NODE_MAX_DIST = 45  # px — how far an edge endpoint can be from a node and still count as touching it
ARROWHEAD_PROBE_RADIUS = 14  # px, neighborhood used to detect which endpoint has the arrowhead

# Circle/ellipse nodes: a true ellipse's contour-area-to-bounding-box-area
# ratio is pi/4 (~0.7854) regardless of aspect ratio (area = pi*a*b, bbox
# area = (2a)(2b) = 4ab) — unlike the rectangle/diamond bands, this one
# constant works whether the shape is a perfect circle or a stretched
# ellipse. It's a pre-filter, not proof: a rounded-corner rectangle could
# land in the same band by coincidence, so cv2.fitEllipse + a real fit-error
# measurement (below) is what actually confirms it.
ELLIPSE_FILL_RATIO_MIN = 0.70
ELLIPSE_FILL_RATIO_MAX = 0.86
ELLIPSE_MIN_CONTOUR_POINTS = 8  # cv2.fitEllipse requires >=5; a few more avoids fitting noise
ELLIPSE_FIT_ERROR_TIGHT = 0.03  # mean normalized radial error at/below this -> top confidence band
ELLIPSE_FIT_ERROR_MAX = 0.12  # mean normalized radial error above this -> not a real ellipse, discarded
NODE_DEDUP_IOU_THRESHOLD = 0.5  # bbox overlap above this -> same physical shape, keep only the larger candidate
# How far in from a node-circle's bbox to crop before OCR. A rectangle's
# flat edge sits well clear of its own label at any padding, but an
# ellipse/circle's boundary curves in close to the bbox near the label —
# cropping the raw bbox reliably fed the curved border into Tesseract's
# layout analysis alongside the label text and it dropped the text entirely
# (measured directly on this adapter's own circle/ellipse fixture: bbox-crop
# OCR returned nothing for "Start"/"Valid?"/"End" even though the text
# renders cleanly; insetting past the curve fixed it). Scoped to node-circle
# only — confirmed by direct test that the existing node-decision (diamond)
# OCR path on flowchart_clean.png is a separate, already-working case this
# change does not touch.
ELLIPSE_LABEL_INSET_X_FRAC = 0.22
ELLIPSE_LABEL_INSET_Y_FRAC = 0.30

# --- UML activity-diagram shapes -------------------------------------------
# A hollow ring and a solid-filled circle of the *same outer boundary* have
# an IDENTICAL fill_ratio (contour area / bbox area) -- fill_ratio can only
# describe the outer edge, not what's inside it, so it cannot tell an
# initial-node dot, a final-node bullseye, or a fork/join bar apart from an
# ordinary hollow shape of the same silhouette. The one signal that can is
# how dark the shape's own INTERIOR is in the source binary image, sampled
# after eroding a few pixels in from the contour boundary so a hollow
# shape's own outline stroke isn't mistaken for solid fill.
SOLID_FILL_MEAN_THRESHOLD = 127.0  # mean binary-image intensity (0-255) inside a contour's eroded interior; at/above this the shape is solid-filled, below it's a hollow outline
SOLID_FILL_ERODE_PX = 5  # shrinks the sampled interior mask inward by this many pixels so a hollow shape's stroke doesn't skew the mean toward "solid"

# A UML initial node is a small solid dot, and a final node's inner dot is
# the same size marker nested inside a larger hollow ring -- both
# categorically smaller than a real activity/decision shape in the same
# diagram. This is a fixed-scale heuristic (like the diagram's other size
# constants), tuned to this adapter's fixture scale (~700-900px canvases);
# see README's "Known limitations" for the honest scaling caveat.
ACTIVITY_MARKER_MAX_AREA = 1500  # px^2 -- ceiling for "this round solid shape is a small initial/final-node dot," not a large filled circle icon
FINAL_NODE_INNER_MAX_AREA_FRACTION = 0.5  # a candidate inner dot must be smaller than this fraction of its surrounding ring's area to count as nested, not a second same-size shape

ROUNDED_RECT_FILL_RATIO_MIN = 0.85  # a rounded rectangle's corners cut only a small area from its bounding box, so it still nearly fills it, like a sharp-cornered rectangle
ROUNDED_RECT_CONFIDENCE = 0.90  # below a crisp 4-vertex rectangle's ceiling (0.97): tolerating >4 vertices for rounding is a softer geometric match

FORK_JOIN_MIN_ASPECT_RATIO = 4.0  # width:height or height:width -- a fork/join bar is a thin, heavily elongated solid rectangle, unlike a normal activity box
FORK_JOIN_CONFIDENCE = 0.94

SWIMLANE_MIN_LENGTH_FRAC = 0.55  # a long straight segment spanning at least this fraction of the image's width or height is treated as a swimlane divider, not a connector
SWIMLANE_HOUGH_MIN_LINE_LENGTH_FRAC = 0.35  # separate, longer minLineLength than connector detection uses, so short connector strokes never get picked up by this pass
SWIMLANE_AXIS_TOLERANCE_DEG = 3  # how far from purely horizontal/vertical a long segment can be and still count as a lane divider (a hand-steady straight line, not a diagonal connector)
SWIMLANE_DEDUP_DIST_PX = 20  # near-parallel, near-collinear long segments within this distance are the same physical divider line


def _load_binary(path: str | Path) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError(f"could not read '{path}' as an image (unsupported format or corrupt file)")
    _, binary = cv2.threshold(img, 127, 255, cv2.THRESH_BINARY_INV)
    return binary


def _edge_angles_deg(pts: np.ndarray) -> list[float]:
    # approxPolyDP has historically returned points shaped (N, 1, 2); reshape
    # defensively to (N, 2) so this doesn't silently break the way the
    # HoughLinesP parsing below did if a future OpenCV version flattens this
    # output too. Confirmed unchanged (still (N, 1, 2)) on OpenCV 5.0.0.
    pts = np.asarray(pts).reshape(-1, 2)
    angles = []
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        angles.append(abs(math.degrees(math.atan2(y1 - y0, x1 - x0))) % 180)
    return angles


def _is_axis_aligned(angles: list[float]) -> bool:
    def near(a, target):
        return min(abs(a - target), abs(a - target - 180)) <= AXIS_ALIGNED_TOLERANCE_DEG

    return all(near(a, 0) or near(a, 90) for a in angles)


def _ellipse_fit_error(contour: np.ndarray, ellipse: tuple) -> float:
    """Mean normalized radial error between a contour's real points and the
    ellipse cv2.fitEllipse fit to them. 0.0 means every point sits exactly
    on the fitted ellipse boundary; larger values mean the contour is
    something else that merely happened to pass the fill-ratio pre-filter
    (e.g. a rounded rectangle). This is the actual confidence signal for
    node-circle, not the fill-ratio band membership, which is only a cheap
    pre-filter to avoid running fitEllipse on obviously-non-round contours.
    """
    (cx, cy), (major, minor), angle_deg = ellipse
    a, b = major / 2, minor / 2
    if a <= 0 or b <= 0:
        return float("inf")
    theta = math.radians(angle_deg)
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    pts = np.asarray(contour).reshape(-1, 2)
    errors = []
    for px, py in pts:
        dx, dy = px - cx, py - cy
        # Rotate into the ellipse's own axis frame, then scale by its two
        # radii — a point exactly on the boundary has radius exactly 1 in
        # this frame regardless of the ellipse's aspect ratio or rotation.
        xr = dx * cos_t + dy * sin_t
        yr = -dx * sin_t + dy * cos_t
        r = math.hypot(xr / a, yr / b)
        errors.append(abs(r - 1.0))
    return sum(errors) / len(errors) if errors else float("inf")


def _ellipse_confidence(mean_fit_error: float) -> float:
    if mean_fit_error <= ELLIPSE_FIT_ERROR_TIGHT:
        return 0.95
    # Linear falloff from the tight-fit ceiling down to a floor as the fit
    # gets progressively worse, capped by ELLIPSE_FIT_ERROR_MAX (contours
    # past that point are rejected before this is ever called).
    span = ELLIPSE_FIT_ERROR_MAX - ELLIPSE_FIT_ERROR_TIGHT
    frac = (mean_fit_error - ELLIPSE_FIT_ERROR_TIGHT) / span if span > 0 else 1.0
    return round(0.95 - 0.20 * frac, 4)


def _mean_interior_intensity(binary: np.ndarray, contour: np.ndarray) -> float:
    """Mean binary-image value strictly inside a contour, after eroding the
    filled mask inward by SOLID_FILL_ERODE_PX -- the one real signal that
    distinguishes a solid-filled shape from a hollow outline of the exact
    same silhouette, which fill_ratio (area / bbox area) cannot: both read
    identically from the outer boundary alone. A solid shape's interior
    stays foreground (255) after erosion; a hollow shape's interior is
    background (0) once the erosion has eaten past its own outline stroke.
    """
    mask = np.zeros(binary.shape, dtype=np.uint8)
    cv2.drawContours(mask, [contour], -1, 255, thickness=cv2.FILLED)
    kernel = np.ones((SOLID_FILL_ERODE_PX, SOLID_FILL_ERODE_PX), np.uint8)
    eroded = cv2.erode(mask, kernel)
    sample_mask = eroded if cv2.countNonZero(eroded) > 0 else mask
    return float(cv2.mean(binary, mask=sample_mask)[0])


def _is_axis_aligned_rect_contour(contour: np.ndarray) -> bool:
    """Whether a contour's minimum-area bounding rectangle is axis-aligned,
    via the same real edge-angle test _is_axis_aligned already uses for a
    clean 4-vertex polygon -- reused here on cv2.minAreaRect's synthetic
    4-corner box instead of the raw contour, so a >4-vertex contour (e.g. a
    rounded rectangle's rounded corners) can still be tested for axis
    alignment without depending on OpenCV's minAreaRect angle convention,
    which has changed across versions.
    """
    box = cv2.boxPoints(cv2.minAreaRect(contour))
    return _is_axis_aligned(_edge_angles_deg(box))


def _bbox_iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = (ax1 - ax0) * (ay1 - ay0)
    area_b = (bx1 - bx0) * (by1 - by0)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def _dedup_nodes(nodes: list[dict]) -> list[dict]:
    """A drawn shape's outline is a thin ring; cv2.findContours (RETR_LIST)
    returns the ring's outer edge and its inner edge as two separate closed
    contours, and both can independently pass shape checks — registering
    the same physical shape twice at nearly the same bbox. Observed and
    real on the circle/ellipse fixture (the outer and inner boundary of a
    drawn circle's stroke are both very close to the pi/4 fill-ratio band
    and both fit an ellipse cleanly). This never fired on the existing
    rectangle/diamond fixture — there, only one of the two contours per
    shape ever survived approxPolyDP's 4-vertex test, so this is a real gap
    the circle path surfaces, not a change to prior behavior. Keep the
    larger (outer, visually more accurate) of any two heavily-overlapping
    candidates.
    """
    # Prefer a clean rectangle/diamond match over a "node-circle" match at
    # (nearly) the same bbox, before falling back to area. A real, confirmed
    # case this matters for: an ordinary rectangle's stroke produces both an
    # outer and inner ring contour (the same duplicate-ring issue already
    # known for circles); if anti-aliasing nudges the *outer* ring's corner
    # count to 5 instead of 4, it can fall through to the ellipse check
    # instead, and — since a squarish shape can coincidentally clear the
    # ellipse fill-ratio band and fit error too — get misclassified as
    # node-circle. The *inner* ring usually still resolves to a clean
    # 4-vertex rectangle. Area-only tie-breaking would keep the larger,
    # wrongly-classified outer/ellipse candidate every time, since a
    # stroke's outer ring is always slightly bigger than its inner one.
    # Reproduced directly on tests/fixtures/sequence_diagram_clean.png's
    # actor boxes before this fix (all three came back "node-circle").
    # Area still breaks ties within the same shape category — that's the
    # genuine ring-duplicate case (two node-circle candidates for one real
    # circle), unaffected by this change.
    # node-initial/node-final are round-family shapes too (a duplicate
    # hole-boundary contour of a UML marker can be misclassified as
    # node-initial by the solid-fill heuristic, exactly the same failure
    # mode as an ordinary ring's duplicate contour above) -- grouping them
    # with node-circle here ensures such a duplicate still loses to its
    # larger, more-accurate sibling rather than surviving as a phantom
    # extra node just because its shape label differs.
    _ROUND_FAMILY_SHAPES = {"node-circle", "node-initial", "node-final"}

    def _dedup_sort_key(n: dict) -> tuple[bool, float]:
        return (n["shape"] in _ROUND_FAMILY_SHAPES, -n["area"])

    kept: list[dict] = []
    for cand in sorted(nodes, key=_dedup_sort_key):
        if any(_bbox_iou(cand["bbox"], k["bbox"]) > NODE_DEDUP_IOU_THRESHOLD for k in kept):
            continue
        kept.append(cand)
    return kept


def _bbox_center(bbox: tuple[int, int, int, int]) -> tuple[float, float]:
    x0, y0, x1, y1 = bbox
    return (x0 + x1) / 2, (y0 + y1) / 2


def _merge_final_node_bullseyes(nodes: list[dict]) -> list[dict]:
    """A UML activity final (end) node is drawn as a bullseye: a hollow ring
    (already detected as node-circle) with a small solid dot concentric
    inside it (already detected as node-initial, since a lone dot looks
    identical to an initial node until you know it's nested). Real
    containment -- the dot's center sits inside the ring's bbox and its area
    is meaningfully smaller -- is the only thing that tells the two apart;
    an initial node that happens to share an image with an unrelated ring
    elsewhere would not pass this (their bboxes wouldn't overlap).
    """
    rings = [n for n in nodes if n["shape"] == "node-circle" and not n.get("solid", False)]
    dots = [n for n in nodes if n["shape"] == "node-initial"]
    consumed_dot_ids = set()

    for ring in rings:
        rx0, ry0, rx1, ry1 = ring["bbox"]
        best_dot = None
        for dot in dots:
            if id(dot) in consumed_dot_ids:
                continue
            if dot["area"] >= FINAL_NODE_INNER_MAX_AREA_FRACTION * ring["area"]:
                continue
            dcx, dcy = _bbox_center(dot["bbox"])
            if not (rx0 <= dcx <= rx1 and ry0 <= dcy <= ry1):
                continue
            if best_dot is None or dot["area"] > best_dot["area"]:
                best_dot = dot
        if best_dot is not None:
            ring["shape"] = "node-final"
            # Same reasoning as node-initial's own exemption: the nested
            # ring+dot containment check just confirmed is a stronger,
            # purpose-built signal than the generic MIN_NODE_AREA floor
            # (tuned for flowchart boxes) is meant to police.
            ring["below_primary_threshold"] = False
            consumed_dot_ids.add(id(best_dot))

    return [n for n in nodes if id(n) not in consumed_dot_ids]


def _detect_nodes(binary: np.ndarray) -> list[dict]:
    contours, hierarchy = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    nodes = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < NOISE_FLOOR_AREA:
            continue
        perimeter = cv2.arcLength(contour, True)
        x, y, w, h = cv2.boundingRect(contour)
        # 0.02*perimeter alone under-serves a thin, elongated shape (e.g. a
        # UML sequence diagram's activation bar, ~15px wide x 300px tall):
        # its perimeter is dominated by the long dimension, so that epsilon
        # can exceed the shape's own short dimension and Douglas-Peucker
        # collapses two long parallel sides down to a single line, losing
        # 2 of the 4 real corners. Capping epsilon relative to min(w, h) too
        # keeps thin shapes' corners intact without changing anything for
        # normally-proportioned shapes, where min(w, h) is large enough that
        # this second term never binds — verified directly: this fixes a
        # real, reproducing 4-vertex-detection failure on
        # tests/fixtures/sequence_diagram_clean.png's activation bars
        # without changing a single node/edge count on flowchart_clean.png
        # or flowchart_with_circle.png.
        epsilon = max(1.0, min(0.02 * perimeter, 0.4 * min(w, h)))
        approx = cv2.approxPolyDP(contour, epsilon, True)
        bbox_area = w * h
        fill_ratio = area / bbox_area if bbox_area else 0

        if len(approx) == 4:
            angles = _edge_angles_deg(approx)
            axis_aligned = _is_axis_aligned(angles)
            aspect_ratio = max(w, h) / max(1, min(w, h))
            # A rectangle's contour area should nearly fill its bounding box; a
            # diamond's fills roughly half of it (its bbox corners are outside
            # the shape). Both are real, distinct, measurable signals.
            if axis_aligned and fill_ratio > 0.85:
                is_solid = _mean_interior_intensity(binary, contour) >= SOLID_FILL_MEAN_THRESHOLD
                if is_solid and aspect_ratio >= FORK_JOIN_MIN_ASPECT_RATIO:
                    # A UML fork/join bar: solid black, heavily elongated,
                    # axis-aligned. Direction (fork vs. join) isn't a shape
                    # property -- it depends on which side has more incoming
                    # vs. outgoing edges, which this adapter doesn't
                    # distinguish; see README's "Known limitations".
                    shape = "fork-join"
                    confidence = FORK_JOIN_CONFIDENCE
                else:
                    shape = "node"
                    confidence = 0.97 if fill_ratio > 0.93 else 0.90
            elif not axis_aligned and 0.35 < fill_ratio < 0.65:
                shape = "node-decision"
                confidence = 0.93 if 0.45 < fill_ratio < 0.55 else 0.85
            else:
                # 4 vertices but neither clean rectangle nor clean diamond —
                # still worth surfacing, honestly flagged as uncertain rather
                # than forced into one bucket.
                shape = "node"
                confidence = 0.6
            nodes.append(
                {
                    "bbox": (x, y, x + w, y + h),
                    "shape": shape,
                    "confidence": confidence,
                    "area": area,
                    "below_primary_threshold": area < MIN_NODE_AREA,
                }
            )
            continue

        # Not a clean 4-vertex polygon. First check whether it's a real
        # circle/ellipse (a tight fitEllipse fit is a stronger, more
        # reliable signal than fill_ratio band membership alone -- a rounded
        # rectangle can land in the same fill-ratio band but fits an ellipse
        # poorly at its flatter sides, which is exactly what disambiguates
        # the two below). Gated on both the fill-ratio pre-filter and a
        # minimum point count so fitEllipse isn't run on tiny/degenerate
        # contours.
        classified = False
        if len(contour) >= ELLIPSE_MIN_CONTOUR_POINTS and ELLIPSE_FILL_RATIO_MIN <= fill_ratio <= ELLIPSE_FILL_RATIO_MAX:
            ellipse = cv2.fitEllipse(contour)
            fit_error = _ellipse_fit_error(contour, ellipse)
            if fit_error <= ELLIPSE_FIT_ERROR_MAX:
                is_solid = _mean_interior_intensity(binary, contour) >= SOLID_FILL_MEAN_THRESHOLD
                # A solid, small round shape is a UML initial-node dot (or,
                # if it later turns out to sit inside a larger hollow ring,
                # a final node's inner dot -- resolved in the merge pass
                # below, after every candidate in the image is known).
                if is_solid and area <= ACTIVITY_MARKER_MAX_AREA:
                    shape = "node-initial"
                    confidence = _ellipse_confidence(fit_error)
                else:
                    shape = "node-circle"
                    confidence = _ellipse_confidence(fit_error)
                # node-initial deliberately skips the MIN_NODE_AREA gate
                # below: its own solid-fill + ellipse-fit signal is a
                # purpose-built confirmation for "this is a real small
                # marker," a stronger claim than the generic area floor
                # (tuned for flowchart boxes, not deliberately tiny UML
                # glyphs) is meant to police.
                nodes.append(
                    {
                        "bbox": (x, y, x + w, y + h),
                        "shape": shape,
                        "confidence": confidence,
                        "area": area,
                        "solid": is_solid,
                        "below_primary_threshold": False if shape == "node-initial" else area < MIN_NODE_AREA,
                    }
                )
                classified = True

        # Still not classified: check for a rounded-rectangle activity node
        # -- a UML activity box's corners add extra approxPolyDP vertices
        # (so it never matches the 4-vertex branch above) without changing
        # how much of its own bounding box it fills, unlike a diamond.
        if not classified and fill_ratio > ROUNDED_RECT_FILL_RATIO_MIN and _is_axis_aligned_rect_contour(contour):
            nodes.append(
                {
                    "bbox": (x, y, x + w, y + h),
                    "shape": "node-activity",
                    "confidence": ROUNDED_RECT_CONFIDENCE,
                    "area": area,
                    "solid": False,
                    "below_primary_threshold": area < MIN_NODE_AREA,
                }
            )
            classified = True

        # else: neither rectangle, diamond, circle/ellipse, nor rounded
        # rectangle — honestly missed, same as any other unrecognized shape
        # (see README's "Known limitations").

    # Resolve bullseye final nodes (ring + nested solid dot) before the
    # relative-rescue pass below, which reasons about "confirmed" node areas
    # -- a dot that's about to be absorbed into its ring shouldn't count as
    # its own separate small candidate.
    nodes = _merge_final_node_bullseyes(nodes)

    # Relative rescue: a candidate below MIN_NODE_AREA is kept only if it's
    # a real fraction of the size of shapes this same image already
    # confirmed are nodes — never based on a fixed constant tuned against a
    # different image. An image with no confirmed nodes at all (median is
    # undefined) rescues nothing, honestly, rather than guessing a
    # reference size out of nowhere.
    confirmed_areas = sorted(n["area"] for n in nodes if not n["below_primary_threshold"])
    median_area = None
    if confirmed_areas:
        mid = len(confirmed_areas) // 2
        median_area = (
            confirmed_areas[mid]
            if len(confirmed_areas) % 2
            else (confirmed_areas[mid - 1] + confirmed_areas[mid]) / 2
        )

    kept: list[dict] = []
    for n in nodes:
        if not n.pop("below_primary_threshold"):
            kept.append(n)
        elif median_area is not None and n["area"] >= RELATIVE_MIN_AREA_FRACTION * median_area:
            n["confidence"] = round(max(0.35, n["confidence"] - CONFIDENCE_RELATIVE_AREA_PENALTY), 4)
            kept.append(n)
        # else: below both the absolute and the relative floor — genuinely
        # too small to be a real shape in this image, honestly missed.
    nodes = kept

    nodes = _dedup_nodes(nodes)
    # Reading order: top-to-bottom, then left-to-right.
    nodes.sort(key=lambda n: (n["bbox"][1], n["bbox"][0]))
    return nodes


def _mask_out_nodes(binary: np.ndarray, nodes: list[dict]) -> np.ndarray:
    masked = binary.copy()
    for node in nodes:
        x0, y0, x1, y1 = node["bbox"]
        x0, y0 = max(0, x0 - NODE_PADDING), max(0, y0 - NODE_PADDING)
        x1, y1 = x1 + NODE_PADDING, y1 + NODE_PADDING
        masked[y0:y1, x0:x1] = 0
    return masked


def _point_to_bbox_dist(px: float, py: float, bbox: tuple[int, int, int, int]) -> float:
    x0, y0, x1, y1 = bbox
    dx = max(x0 - px, 0, px - x1)
    dy = max(y0 - py, 0, py - y1)
    return math.hypot(dx, dy)


def _nearest_node(px: float, py: float, nodes: list[dict]) -> tuple[int | None, float]:
    best_idx, best_dist = None, float("inf")
    for i, node in enumerate(nodes):
        d = _point_to_bbox_dist(px, py, node["bbox"])
        if d < best_dist:
            best_idx, best_dist = i, d
    return best_idx, best_dist


def _dark_pixel_density(binary: np.ndarray, x: float, y: float, radius: int) -> int:
    h, w = binary.shape
    x0, x1 = max(0, int(x - radius)), min(w, int(x + radius))
    y0, y1 = max(0, int(y - radius)), min(h, int(y + radius))
    return int(np.count_nonzero(binary[y0:y1, x0:x1]))


def _detect_edges(binary_full: np.ndarray, binary_masked: np.ndarray, nodes: list[dict]) -> list[dict]:
    lines = cv2.HoughLinesP(
        binary_masked,
        rho=1,
        theta=np.pi / 180,
        threshold=30,
        minLineLength=HOUGH_MIN_LINE_LENGTH,
        maxLineGap=HOUGH_MAX_LINE_GAP,
    )
    if lines is None:
        return []

    # cv2.HoughLinesP's return shape is not consistent across OpenCV
    # versions: historically (N, 1, 4) (each line a [1,4] sub-array), but
    # OpenCV 5.0.0 returns (N, 4) directly. np.asarray(...).reshape(-1)
    # flattens either shape to the same 4 ints, so this works regardless of
    # which shape the installed OpenCV version produces — no version
    # sniffing required.
    #
    # Cluster collinear/overlapping segments that Hough tends to split into
    # near-duplicates, keeping the longest representative of each cluster.
    segments = [tuple(int(v) for v in np.asarray(l).reshape(-1)) for l in lines]
    segments.sort(key=lambda s: -math.hypot(s[2] - s[0], s[3] - s[1]))
    kept: list[tuple[int, int, int, int]] = []
    for seg in segments:
        x1, y1, x2, y2 = seg
        mx, my = (x1 + x2) / 2, (y1 + y2) / 2
        if any(_point_to_bbox_dist(mx, my, (min(k[0], k[2]) - 10, min(k[1], k[3]) - 10, max(k[0], k[2]) + 10, max(k[1], k[3]) + 10)) < 15 for k in kept):
            continue
        kept.append(seg)

    edges = []
    for x1, y1, x2, y2 in kept:
        idx_a, dist_a = _nearest_node(x1, y1, nodes)
        idx_b, dist_b = _nearest_node(x2, y2, nodes)
        if idx_a is None or idx_b is None or idx_a == idx_b:
            continue
        if dist_a > ENDPOINT_TO_NODE_MAX_DIST or dist_b > ENDPOINT_TO_NODE_MAX_DIST:
            continue

        density_a = _dark_pixel_density(binary_full, x1, y1, ARROWHEAD_PROBE_RADIUS)
        density_b = _dark_pixel_density(binary_full, x2, y2, ARROWHEAD_PROBE_RADIUS)
        # The arrowhead end has extra converging strokes beyond the shaft
        # itself, so it lights up more pixels in a small radius than the
        # plain tail end does. Whichever end is denser is "to".
        if density_b >= density_a:
            from_idx, to_idx = idx_a, idx_b
        else:
            from_idx, to_idx = idx_b, idx_a

        # Confidence is driven primarily by how CLEAR that density asymmetry
        # is (a real, per-edge-varying signal — verified against this
        # fixture: 1.18x for the arrow terminating at the decision diamond's
        # pointed vertex, vs 2.5x+ for the two terminating at flat rectangle
        # edges), not by endpoint-to-node distance. That distance turned out
        # to be nearly constant across all three edges on this fixture (every
        # arrow was drawn with a similar ~10px gap to its target node), so
        # using it as the dominant term produced three near-identical
        # confidence values — the exact failure mode to watch for. It's kept
        # as a secondary penalty: a poor geometric match still pulls
        # confidence down even if the density asymmetry looks clean.
        density_ratio = max(density_a, density_b) / max(1, min(density_a, density_b))
        asymmetry_component = 0.5 + 0.15 * min(density_ratio, 3.0)
        max_dist = max(dist_a, dist_b)
        distance_penalty = 0.1 * (max_dist / ENDPOINT_TO_NODE_MAX_DIST)
        confidence = round(min(0.90, max(0.35, asymmetry_component - distance_penalty)), 4)
        edges.append(
            {
                "from_idx": from_idx,
                "to_idx": to_idx,
                "confidence": confidence,
                "midpoint": ((x1 + x2) / 2, (y1 + y2) / 2),
                "bbox": (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)),
            }
        )
    return edges


def _detect_swimlanes(binary: np.ndarray) -> list[dict]:
    """UML swimlanes are drawn as long straight dividers spanning most of
    the canvas -- a fundamentally different real signal from a connector
    (which runs between two specific nodes and is comparatively short) or a
    node's own border (which is masked out for edge detection, but a
    swimlane divider legitimately runs behind/through nodes, so this scans
    the full, unmasked binary image rather than the connector-masked one).
    Uses its own, much longer minLineLength than connector detection so a
    normal-length connector stroke can never be mistaken for a lane
    divider.
    """
    h, w = binary.shape
    min_line_length = int(SWIMLANE_HOUGH_MIN_LINE_LENGTH_FRAC * min(h, w))
    lines = cv2.HoughLinesP(
        binary,
        rho=1,
        theta=np.pi / 180,
        threshold=50,
        minLineLength=min_line_length,
        maxLineGap=10,
    )
    if lines is None:
        return []

    segments = [tuple(int(v) for v in np.asarray(l).reshape(-1)) for l in lines]
    candidates = []
    for x1, y1, x2, y2 in segments:
        length = math.hypot(x2 - x1, y2 - y1)
        angle = abs(math.degrees(math.atan2(y2 - y1, x2 - x1))) % 180
        is_horizontal = angle <= SWIMLANE_AXIS_TOLERANCE_DEG or angle >= 180 - SWIMLANE_AXIS_TOLERANCE_DEG
        is_vertical = abs(angle - 90) <= SWIMLANE_AXIS_TOLERANCE_DEG
        if is_horizontal and length >= SWIMLANE_MIN_LENGTH_FRAC * w:
            orientation = "horizontal"
        elif is_vertical and length >= SWIMLANE_MIN_LENGTH_FRAC * h:
            orientation = "vertical"
        else:
            continue
        length_frac = length / w if orientation == "horizontal" else length / h
        candidates.append(
            {
                "bbox": (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)),
                "orientation": orientation,
                "length": length,
                "confidence": round(min(0.95, 0.5 + 0.45 * min(length_frac, 1.0)), 4),
            }
        )

    # Hough commonly returns several near-duplicate detections for one real
    # thick line; collapse same-orientation candidates whose midpoints sit
    # within SWIMLANE_DEDUP_DIST_PX of each other, keeping the longest.
    candidates.sort(key=lambda c: -c["length"])
    kept: list[dict] = []
    for cand in candidates:
        cx, cy = _bbox_center(cand["bbox"])
        if any(
            k["orientation"] == cand["orientation"] and math.hypot(cx - _bbox_center(k["bbox"])[0], cy - _bbox_center(k["bbox"])[1]) < SWIMLANE_DEDUP_DIST_PX
            for k in kept
        ):
            continue
        kept.append(cand)

    kept.sort(key=lambda c: (c["bbox"][1], c["bbox"][0]))
    return kept


def _ellipse_label_bbox(bbox: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = bbox
    w, h = x1 - x0, y1 - y0
    inset_x, inset_y = int(w * ELLIPSE_LABEL_INSET_X_FRAC), int(h * ELLIPSE_LABEL_INSET_Y_FRAC)
    return (x0 + inset_x, y0 + inset_y, x1 - inset_x, y1 - inset_y)


_NO_LABEL_SHAPES = frozenset({"node-initial", "node-final", "fork-join"})
_ELLIPSE_LABEL_SHAPES = frozenset({"node-circle"})


def _iter_regions(path: str | Path) -> Iterator[Region]:
    binary = _load_binary(path)
    image_bgr = cv2.imread(str(path))
    nodes = _detect_nodes(binary)
    masked = _mask_out_nodes(binary, nodes)
    edges = _detect_edges(binary, masked, nodes)
    swimlanes = _detect_swimlanes(binary)

    # Probed lazily, only if there's at least one node/edge that could carry
    # a label — same reasoning as the document adapter's _iter_regions:
    # ocr_engine_status() loads EasyOCR's model weights (real, multi-second
    # work), which an image with nothing to label shouldn't have to pay for.
    # Swimlanes never carry a label in this adapter (see README's "Known
    # limitations"), so they don't factor into this probe -- and neither do
    # node-initial/node-final/fork-join nodes, which never get an OCR
    # attempt either (see _NO_LABEL_SHAPES below): a diagram made up
    # entirely of those plus no edges has nothing this adapter would ever
    # try to read, so it shouldn't pay for the model load at all.
    has_labelable_node = any(n["shape"] not in _NO_LABEL_SHAPES for n in nodes)
    ocr_ok = ocr_engine_status()[0] if (has_labelable_node or edges) else False
    node_ids = []
    for i, node in enumerate(nodes):
        region_id = f"node-{i + 1}"
        node_ids.append(region_id)
        text, ocr_conf = (None, 0.0)
        # node-initial/node-final/fork-join are pure UML glyphs with no
        # label of their own by convention -- skipping OCR for them isn't
        # a missed case, it's the correct, honest behavior for a shape
        # that was never going to have readable text inside it.
        if ocr_ok and node["shape"] not in _NO_LABEL_SHAPES:
            ocr_bbox = _ellipse_label_bbox(node["bbox"]) if node["shape"] in _ELLIPSE_LABEL_SHAPES else node["bbox"]
            text, ocr_conf = read_label(image_bgr, ocr_bbox)
        yield Region(
            id=region_id,
            type="node",
            shape=node["shape"],
            confidence=node["confidence"],
            text=text,
        )

    for i, lane in enumerate(swimlanes):
        yield Region(
            id=f"swimlane-{i + 1}",
            type="swimlane",
            shape="swimlane-divider",
            confidence=lane["confidence"],
            text=None,
        )

    for i, edge in enumerate(edges):
        region_id = f"edge-{i + 1}"
        text = None
        if ocr_ok:
            mx, my = edge["midpoint"]
            label_bbox = (int(mx - 40), int(my - 20), int(mx + 40), int(my + 20))
            text, _ = read_label(image_bgr, label_bbox)
        yield Region(
            id=region_id,
            type="edge",
            shape="edge",
            confidence=edge["confidence"],
            text=text,
            relationships=[
                Relationship("connects-to", node_ids[edge["from_idx"]]),
                Relationship("connects-to", node_ids[edge["to_idx"]]),
            ],
        )


def parse_diagram(path: str | Path) -> Iterator[Region]:
    """Streaming entry point, matching the other adapters' shape."""
    yield from _iter_regions(path)


def _check_edges_reference_existing_nodes(regions: list[Region]) -> Check:
    node_ids = {r.id for r in regions if r.type == "node"}
    edge_regions = [r for r in regions if r.type == "edge"]
    if not edge_regions:
        return Check("edges-reference-existing-nodes", True, "no edges detected in this diagram")

    bad = []
    for edge in edge_regions:
        targets = [rel.target for rel in edge.relationships if rel.type == "connects-to"]
        if len(targets) != 2 or any(t not in node_ids for t in targets):
            bad.append(edge.id)

    if bad:
        return Check("edges-reference-existing-nodes", False, f"{len(bad)} edge(s) reference a missing node: {', '.join(bad)}")
    return Check(
        "edges-reference-existing-nodes",
        True,
        f"all {len(edge_regions)} edge(s) reference two real node ids",
    )


def _check_ocr_engine_available(regions: list[Region]) -> Check:
    # node-initial/node-final/fork-join are excluded here even though
    # they're type "node" -- OCR is never attempted for them (see
    # _NO_LABEL_SHAPES), so counting them as "could not be OCR'd" when the
    # engine is unavailable would overstate the real impact: none of these
    # shapes would have gotten a label either way.
    label_bearing = [
        r for r in regions if r.type == "edge" or (r.type == "node" and r.shape not in _NO_LABEL_SHAPES)
    ]
    if not label_bearing:
        # No nodes/edges at all means _iter_regions never probed the OCR
        # engine either (see its lazy-probe comment) — don't force an
        # expensive EasyOCR model load here just to answer a question with
        # no bearing on this (empty) image's actual output.
        return Check(
            "ocr-engine-available",
            True,
            "not checked — no nodes or edges were detected to label",
        )
    ok, detail = ocr_engine_status()
    if ok:
        return Check("ocr-engine-available", True, detail)
    return Check(
        "ocr-engine-available",
        False,
        f"OCR engine not available ({detail}) — {len(label_bearing)} node/edge label(s) could not be OCR'd; "
        "shape and topology detection are unaffected, since they don't use OCR",
    )


def run_checks(regions: list[Region]) -> list[Check]:
    return [_check_edges_reference_existing_nodes(regions), _check_ocr_engine_available(regions)]


def build_artifact(path: str | Path) -> dict:
    path = Path(path)
    regions = list(_iter_regions(path))
    checks = run_checks(regions)
    source = Source(path=str(path), hash=sha256_of_file(path), adapter_version=ADAPTER_VERSION)
    artifact = Artifact(
        artifact=path.stem,
        kind="diagram",
        adapter=ADAPTER_NAME,
        source=source,
        regions=regions,
        checks=checks,
    )
    return artifact.to_dict()
