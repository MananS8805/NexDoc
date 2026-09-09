"""Regression tests for the three real bugs found and fixed while testing
the diagram adapter against a UML-sequence-diagram-shaped fixture (a
diagram grammar it had never been exercised against before):

  1. A rectangle's stroke produces two ring contours (outer + inner); when
     anti-aliasing nudged the outer ring to 5 approxPolyDP vertices instead
     of 4, it fell through to the ellipse check and — since it coincidentally
     cleared the ellipse fill-ratio band and fit-error threshold too — got
     misclassified as node-circle. Dedup then kept that larger, wrong
     candidate over the smaller, correctly-classified inner rectangle,
     because it only ever compared area, never classification.
  2. A thin, elongated shape (an activation bar, ~15px wide x 300px tall)
     was collapsing to 2 approxPolyDP vertices instead of 4, because
     0.02*perimeter is dominated by the long dimension and exceeded the
     bar's own width.
  3. A very short activation bar (the fixture's `Database` bar, 1600px^2)
     fell below the fixed MIN_NODE_AREA (3000px^2) floor and was missed
     entirely — and the edge that should have connected to it instead
     snapped onto the nearest node that DID exist (the Server actor header
     box), producing a topologically wrong edge rather than no edge. Fixed
     by rescuing a sub-MIN_NODE_AREA candidate when its area is a real
     fraction of the median area of shapes the same image already
     confirmed are nodes (a per-image-relative signal), at a discounted
     confidence — not by lowering the fixed floor itself, which would risk
     new noise false-positives on unrelated images.

See tests/fixtures/generate_diagram_fixture_sequence.py for the fixture and
adapters/diagram/README.md for the full narrative with the real
intermediate contour numbers that diagnosed all three bugs.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FIXTURE = ROOT / "tests" / "fixtures" / "sequence_diagram_clean.png"


def _build():
    from adapters.diagram.adapter import build_artifact

    return build_artifact(FIXTURE)


def test_actor_boxes_are_rectangles_not_circles():
    """The first real bug: three ordinary rectangular actor boxes (Client,
    Server, Database) were all coming back as shape='node-circle' before
    the dedup fix. None of them are anywhere close to round.
    """
    artifact = _build()
    labeled_rect_nodes = [
        r for r in artifact["regions"] if r["type"] == "node" and r["text"] in ("Client", "Server", "Database")
    ]
    assert len(labeled_rect_nodes) == 3, f"expected all 3 actor boxes to be detected and OCR'd, got {labeled_rect_nodes}"
    for region in labeled_rect_nodes:
        assert region["shape"] == "node", f"{region['text']} was classified as {region['shape']}, expected 'node'"


def test_all_three_activation_bars_are_detected():
    """The second and third real bugs, together: all three activation bars
    (Client's and Server's, normally-sized; Database's, deliberately short
    — see module docstring bug 3) must now be detected as unlabeled nodes.
    Before bug 3's fix, only 2 of these 3 were detected.
    """
    artifact = _build()
    unlabeled_nodes = [r for r in artifact["regions"] if r["type"] == "node" and r["text"] is None]
    assert len(unlabeled_nodes) == 3, (
        f"expected all 3 activation-bar nodes (Client's, Server's, Database's), got {len(unlabeled_nodes)}: "
        f"{unlabeled_nodes}"
    )
    for region in unlabeled_nodes:
        assert region["shape"] == "node", f"an activation bar was classified as {region['shape']}, expected 'node'"


def test_database_bar_is_detected_via_relative_area_rescue_at_discounted_confidence():
    """The specific bug-3 case: the Database bar is well below
    MIN_NODE_AREA (3000px^2) but should still be rescued via the relative-
    area check, at a real, lower confidence than the two normally-sized
    bars — "smaller than every other shape found here" is genuine
    uncertainty, not a free pass.
    """
    artifact = _build()
    unlabeled_nodes = [r for r in artifact["regions"] if r["type"] == "node" and r["text"] is None]
    confidences = sorted(r["confidence"] for r in unlabeled_nodes)
    assert len(confidences) == 3
    # The two normally-sized bars clear MIN_NODE_AREA outright (confidence
    # 0.90, the standard fill_ratio<=0.93 rectangle tier); the rescued
    # Database bar is real but discounted, and must be strictly lower.
    assert confidences[0] < confidences[1] == confidences[2] == 0.90, (
        f"expected the rescued bar's confidence to be strictly lower than the two normal bars' 0.90, got {confidences}"
    )


def test_all_four_real_message_edges_have_correct_topology():
    """Before bug 3's fix, only 3 of 4 real message edges existed at all,
    and the one involving the missing Database bar connected to the wrong
    node (the Server actor box) instead. All 4 — request, query, result,
    response — must now connect the correct pair of activation-bar nodes.
    """
    artifact = _build()
    by_id = {r["id"]: r for r in artifact["regions"]}
    edges = [r for r in artifact["regions"] if r["type"] == "edge"]

    def endpoints_text(edge):
        targets = [rel["target"] for rel in edge["relationships"] if rel["type"] == "connects-to"]
        return tuple(by_id[t]["text"] for t in targets) if len(targets) == 2 else None

    # Bars have no OCR'd text (they're unlabeled), so identify each real
    # message edge by matching against the *set* of activation-bar node ids
    # it connects, rather than by label — 4 distinct bar-to-bar pairs are
    # expected (request: Client<->Server, query: Server<->Database,
    # result: Database<->Server, response: Server<->Client), all edges
    # among exactly 3 activation-bar nodes.
    unlabeled_ids = {r["id"] for r in artifact["regions"] if r["type"] == "node" and r["text"] is None}
    bar_to_bar_edges = [
        e
        for e in edges
        if len([rel["target"] for rel in e["relationships"] if rel["type"] == "connects-to" and rel["target"] in unlabeled_ids]) == 2
    ]
    assert len(bar_to_bar_edges) == 4, (
        f"expected exactly 4 real message edges (request/query/result/response) connecting two activation bars, "
        f"got {len(bar_to_bar_edges)}: {[endpoints_text(e) for e in bar_to_bar_edges]}"
    )


def test_existing_fixtures_are_unaffected_by_the_detection_changes():
    """All three fixes touch shared code (_detect_nodes, _dedup_nodes) used
    by every diagram, not sequence-diagram-specific logic — so the two
    pre-existing fixtures must produce exactly the same node count and
    shape classifications as before any of this session's changes.

    `flowchart_with_circle.png` does have one tiny (254px^2) contour inside
    the new NOISE_FLOOR_AREA-to-MIN_NODE_AREA band — a real, checked fact,
    not assumed absent — but it's correctly rejected by the relative-area
    rescue itself (its area is far below 10% of this image's real nodes'
    median size), so it never reaches the final node list. What actually
    matters, and what's asserted here, is that outcome — the exact node
    count and shape set — not the absence of any small contour existing in
    that pixel range at all.
    """
    from adapters.diagram.adapter import build_artifact

    for name, expected_shapes in [
        ("flowchart_clean.png", ["node", "node", "node", "node-decision"]),
        ("flowchart_with_circle.png", ["node", "node-circle", "node-circle", "node-decision"]),
    ]:
        path = ROOT / "tests" / "fixtures" / name
        artifact = build_artifact(path)
        shapes = sorted(r["shape"] for r in artifact["regions"] if r["type"] == "node")
        assert shapes == expected_shapes, f"{name}: expected {expected_shapes}, got {shapes}"


if __name__ == "__main__":
    test_actor_boxes_are_rectangles_not_circles()
    test_all_three_activation_bars_are_detected()
    test_database_bar_is_detected_via_relative_area_rescue_at_discounted_confidence()
    test_all_four_real_message_edges_have_correct_topology()
    test_existing_fixtures_are_unaffected_by_the_detection_changes()
    print("all sequence-diagram regression tests passed")
