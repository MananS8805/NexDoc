"""Regression tests for the two real bugs found and fixed while testing the
diagram adapter against a UML-sequence-diagram-shaped fixture (a diagram
grammar it had never been exercised against before):

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

See tests/fixtures/generate_diagram_fixture_sequence.py for the fixture and
adapters/diagram/README.md for the full narrative with the real
intermediate contour numbers that diagnosed both bugs.
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
    """The real bug: three ordinary rectangular actor boxes (Client, Server,
    Database) were all coming back as shape='node-circle' before the dedup
    fix. None of them are anywhere close to round.
    """
    artifact = _build()
    labeled_rect_nodes = [
        r for r in artifact["regions"] if r["type"] == "node" and r["text"] in ("Client", "Server", "Database")
    ]
    assert len(labeled_rect_nodes) == 3, f"expected all 3 actor boxes to be detected and OCR'd, got {labeled_rect_nodes}"
    for region in labeled_rect_nodes:
        assert region["shape"] == "node", f"{region['text']} was classified as {region['shape']}, expected 'node'"


def test_thin_activation_bars_are_detected():
    """The second real bug: thin/tall activation bars were collapsing to 2
    approxPolyDP vertices and being silently dropped entirely. At least the
    two tall ones (Client's and Server's, well above MIN_NODE_AREA even at
    their extreme aspect ratio) must be detected as nodes now.
    """
    artifact = _build()
    unlabeled_nodes = [r for r in artifact["regions"] if r["type"] == "node" and r["text"] is None]
    assert len(unlabeled_nodes) >= 2, (
        f"expected at least 2 unlabeled activation-bar nodes (Client's and Server's), got {len(unlabeled_nodes)}"
    )
    for region in unlabeled_nodes:
        assert region["shape"] == "node", f"an activation bar was classified as {region['shape']}, expected 'node'"


def test_at_least_one_real_message_edge_is_recovered_with_correct_topology():
    """Before the fix, activation bars weren't nodes at all, so every
    message-arrow segment failed its endpoint-to-node distance check and
    zero edges were ever produced. At least the Client<->Server "request"/
    "response" edges (both activation bars are well-detected, unlike
    Database's — see the module docstring and README on that one) must
    connect two *unlabeled* activation-bar nodes, not two actor-header
    boxes and not an actor box to an activation bar.
    """
    artifact = _build()
    by_id = {r["id"]: r for r in artifact["regions"]}
    unlabeled_ids = {r["id"] for r in artifact["regions"] if r["type"] == "node" and r["text"] is None}
    edges = [r for r in artifact["regions"] if r["type"] == "edge"]
    assert edges, "expected at least one real message edge to be recovered"

    bar_to_bar_edges = []
    for edge in edges:
        targets = [rel["target"] for rel in edge["relationships"] if rel["type"] == "connects-to"]
        if len(targets) == 2 and all(t in unlabeled_ids for t in targets):
            bar_to_bar_edges.append(edge)
    assert bar_to_bar_edges, (
        f"expected at least one edge connecting two activation-bar nodes; got edges targeting "
        f"{[[rel['target'] for rel in e['relationships']] for e in edges]}"
    )


def test_existing_fixtures_are_unaffected_by_the_epsilon_and_dedup_changes():
    """The two fixes above touch shared code (_detect_nodes, _dedup_nodes)
    used by every diagram, not sequence-diagram-specific logic — so the
    two pre-existing fixtures must produce exactly the same shape
    classifications as before this change.
    """
    from adapters.diagram.adapter import build_artifact

    flowchart = build_artifact(ROOT / "tests" / "fixtures" / "flowchart_clean.png")
    flowchart_shapes = sorted(r["shape"] for r in flowchart["regions"] if r["type"] == "node")
    assert flowchart_shapes == ["node", "node", "node", "node-decision"]

    circle = build_artifact(ROOT / "tests" / "fixtures" / "flowchart_with_circle.png")
    circle_shapes = sorted(r["shape"] for r in circle["regions"] if r["type"] == "node")
    assert circle_shapes == ["node", "node-circle", "node-circle", "node-decision"]


if __name__ == "__main__":
    test_actor_boxes_are_rectangles_not_circles()
    test_thin_activation_bars_are_detected()
    test_at_least_one_real_message_edge_is_recovered_with_correct_topology()
    test_existing_fixtures_are_unaffected_by_the_epsilon_and_dedup_changes()
    print("all sequence-diagram regression tests passed")
