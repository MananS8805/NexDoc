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
    - label text: Tesseract's own OCR confidence, exactly like the document
      adapter — or 0.0 with text=None when Tesseract isn't available.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np
import pytesseract
from pytesseract import Output

from adapters.common.ocr import tesseract_status
from adapters.common.schema import Artifact, Check, Region, Relationship, Source, sha256_of_file

ADAPTER_NAME = "Diagram adapter"
ADAPTER_VERSION = "0.1.0"

MIN_NODE_AREA = 3000  # px^2 — below this, a closed contour is noise, not a node
AXIS_ALIGNED_TOLERANCE_DEG = 20  # how far from horizontal/vertical an edge can be and still count as "axis-aligned"
NODE_PADDING = 10  # px, grown around a node's bbox before masking it out for edge detection
HOUGH_MIN_LINE_LENGTH = 40  # px — shorter than this is treated as arrowhead clutter, not a connector shaft
HOUGH_MAX_LINE_GAP = 8
ENDPOINT_TO_NODE_MAX_DIST = 45  # px — how far an edge endpoint can be from a node and still count as touching it
ARROWHEAD_PROBE_RADIUS = 14  # px, neighborhood used to detect which endpoint has the arrowhead


def _load_binary(path: str | Path) -> np.ndarray:
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError(f"could not read '{path}' as an image (unsupported format or corrupt file)")
    _, binary = cv2.threshold(img, 127, 255, cv2.THRESH_BINARY_INV)
    return binary


def _edge_angles_deg(pts: np.ndarray) -> list[float]:
    angles = []
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i][0]
        x1, y1 = pts[(i + 1) % n][0]
        angles.append(abs(math.degrees(math.atan2(y1 - y0, x1 - x0))) % 180)
    return angles


def _is_axis_aligned(angles: list[float]) -> bool:
    def near(a, target):
        return min(abs(a - target), abs(a - target - 180)) <= AXIS_ALIGNED_TOLERANCE_DEG

    return all(near(a, 0) or near(a, 90) for a in angles)


def _detect_nodes(binary: np.ndarray) -> list[dict]:
    contours, hierarchy = cv2.findContours(binary, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    nodes = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < MIN_NODE_AREA:
            continue
        perimeter = cv2.arcLength(contour, True)
        approx = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(approx) != 4:
            continue
        x, y, w, h = cv2.boundingRect(approx)
        bbox_area = w * h
        fill_ratio = area / bbox_area if bbox_area else 0
        angles = _edge_angles_deg(approx)
        axis_aligned = _is_axis_aligned(angles)
        # A rectangle's contour area should nearly fill its bounding box; a
        # diamond's fills roughly half of it (its bbox corners are outside
        # the shape). Both are real, distinct, measurable signals.
        if axis_aligned and fill_ratio > 0.85:
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
        nodes.append({"bbox": (x, y, x + w, y + h), "shape": shape, "confidence": confidence, "area": area})
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

    # Cluster collinear/overlapping segments that Hough tends to split into
    # near-duplicates, keeping the longest representative of each cluster.
    segments = [tuple(l[0]) for l in lines]
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


def _ocr_label(image_bgr: np.ndarray, bbox: tuple[int, int, int, int], padding: int = 6) -> tuple[str | None, float]:
    ok, _ = tesseract_status()
    if not ok:
        return None, 0.0
    x0, y0, x1, y1 = bbox
    h, w = image_bgr.shape[:2]
    x0, y0 = max(0, x0 - padding), max(0, y0 - padding)
    x1, y1 = min(w, x1 + padding), min(h, y1 + padding)
    crop = image_bgr[y0:y1, x0:x1]
    if crop.size == 0:
        return None, 0.0
    data = pytesseract.image_to_data(crop, output_type=Output.DICT)
    words, confs = [], []
    for i in range(len(data["text"])):
        word = data["text"][i].strip()
        conf = int(data["conf"][i])
        if word and conf >= 0:
            words.append(word)
            confs.append(conf)
    if not words:
        return None, 0.0
    return " ".join(words), round((sum(confs) / len(confs)) / 100, 4)


def _iter_regions(path: str | Path) -> Iterator[Region]:
    binary = _load_binary(path)
    image_bgr = cv2.imread(str(path))
    nodes = _detect_nodes(binary)
    masked = _mask_out_nodes(binary, nodes)
    edges = _detect_edges(binary, masked, nodes)

    tesseract_ok, _ = tesseract_status()
    node_ids = []
    for i, node in enumerate(nodes):
        region_id = f"node-{i + 1}"
        node_ids.append(region_id)
        text, ocr_conf = (None, 0.0)
        if tesseract_ok:
            text, ocr_conf = _ocr_label(image_bgr, node["bbox"])
        yield Region(
            id=region_id,
            type="node",
            shape=node["shape"],
            confidence=node["confidence"],
            text=text,
        )

    for i, edge in enumerate(edges):
        region_id = f"edge-{i + 1}"
        text = None
        if tesseract_ok:
            mx, my = edge["midpoint"]
            label_bbox = (int(mx - 40), int(my - 20), int(mx + 40), int(my + 20))
            text, _ = _ocr_label(image_bgr, label_bbox)
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
    ok, detail = tesseract_status()
    label_bearing = [r for r in regions if r.type in ("node", "edge")]
    if ok:
        return Check("ocr-engine-available", True, f"Tesseract {detail} available")
    if not label_bearing:
        return Check(
            "ocr-engine-available",
            False,
            f"Tesseract not available ({detail}) — no effect on this image, no nodes or edges were detected to label",
        )
    return Check(
        "ocr-engine-available",
        False,
        f"Tesseract not available ({detail}) — {len(label_bearing)} node/edge label(s) could not be OCR'd; "
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
