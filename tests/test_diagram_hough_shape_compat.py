"""Regression test for an OpenCV-version-dependent crash in
adapters/diagram/adapter.py's _detect_edges.

cv2.HoughLinesP's return shape is not consistent across OpenCV versions:
historically (N, 1, 4) — each line a [1, 4] sub-array — but OpenCV 5.0.0
returns (N, 4) directly. The old code did `tuple(l[0])` for each line `l`,
which assumed the (N, 1, 4) shape; on (N, 4) input, `l` is itself the
4-length array, so `l[0]` is a bare numpy scalar and `tuple(l[0])` raised
`TypeError: 'numpy.int32' object is not iterable`.

This test doesn't depend on which OpenCV version is actually installed —
it monkeypatches cv2.HoughLinesP directly so both shapes are exercised
regardless of the real library's current behavior, which is what would
have caught this bug before it ever reached a real OpenCV upgrade.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters.diagram import adapter as diagram_adapter

# Two nodes far enough apart, positioned so a single straight line's
# endpoints land exactly on each node's bbox corner (distance 0, well
# within ENDPOINT_TO_NODE_MAX_DIST) — this exercises the full matching path
# in _detect_edges, not just the line-parsing line in isolation.
_NODES = [
    {"bbox": (0, 0, 20, 20), "shape": "node", "confidence": 0.9, "area": 400},
    {"bbox": (180, 180, 200, 200), "shape": "node", "confidence": 0.9, "area": 400},
]
_LINE = (10, 10, 190, 190)  # x1, y1, x2, y2 — one endpoint touches each node's bbox


def _fake_binaries():
    # Real arrays (not mocks) since _dark_pixel_density does real
    # np.count_nonzero slicing on them.
    size = 220
    binary_full = np.zeros((size, size), dtype=np.uint8)
    binary_masked = np.zeros((size, size), dtype=np.uint8)
    return binary_full, binary_masked


@pytest.mark.parametrize(
    "fake_lines",
    [
        pytest.param(np.array([[list(_LINE)]], dtype=np.int32), id="legacy-shape-N-1-4"),
        pytest.param(np.array([list(_LINE)], dtype=np.int32), id="current-shape-N-4"),
    ],
)
def test_detect_edges_handles_both_houghlinesp_shapes(monkeypatch, fake_lines):
    assert fake_lines.shape in {(1, 1, 4), (1, 4)}  # sanity on the fixture itself

    monkeypatch.setattr(diagram_adapter.cv2, "HoughLinesP", lambda *a, **k: fake_lines)

    binary_full, binary_masked = _fake_binaries()
    edges = diagram_adapter._detect_edges(binary_full, binary_masked, _NODES)

    assert len(edges) == 1, f"expected exactly one edge to be reconstructed from the fake line, got {edges}"
    edge = edges[0]
    assert {edge["from_idx"], edge["to_idx"]} == {0, 1}
    assert edge["bbox"] == (10, 10, 190, 190)


def test_detect_edges_no_lines_still_returns_empty_list(monkeypatch):
    """cv2.HoughLinesP returns None (not an empty array) when it finds
    nothing — make sure that path is untouched by the shape fix.
    """
    monkeypatch.setattr(diagram_adapter.cv2, "HoughLinesP", lambda *a, **k: None)
    binary_full, binary_masked = _fake_binaries()
    assert diagram_adapter._detect_edges(binary_full, binary_masked, _NODES) == []
