"""Regression test for a real bug: multi-page document region boxes used to
be percentages relative to a single fixed-aspect-ratio frame stacking all
pages by band (scripts/build_prototype.py's _compute_boxes_document). The
webapp renders each page's real image with `object-fit:contain`, and for
any document with more than one page, a real page's aspect ratio doesn't
match its allotted band inside the frame's fixed 1:1.32 ratio — verified
directly on tests/fixtures/invoice_two_pages.pdf (both pages 595x842pt,
aspect 0.707): the image rendered as a narrow, off-center column while the
box percentages assumed it filled the frame edge-to-edge, visibly
disconnecting every region box from the real page content.

Fixed by making box percentages LOCAL to each page (0-100% of that one
page) plus a `page` field, with each page rendered as its own block at its
own real aspect ratio (webapp/static/app.js's multi-page branch of
renderPreview()) instead of being squeezed into one shared frame.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

TWO_PAGE_FIXTURE = ROOT / "tests" / "fixtures" / "invoice_two_pages.pdf"
ONE_PAGE_FIXTURE = ROOT / "tests" / "fixtures" / "invoice_born_digital.pdf"


def _build_doc(path):
    from adapters.document.adapter import build_artifact
    from build_prototype import build_doc_for

    artifact = build_artifact(path)
    return build_doc_for("document", path, artifact)


def test_multi_page_boxes_are_local_percentages_with_a_page_number():
    doc = _build_doc(TWO_PAGE_FIXTURE)
    boxes = [r["box"] for r in doc["regions"] if r.get("box")]
    assert boxes, "expected at least one region with a box"

    pages_seen = {b["page"] for b in boxes if "page" in b}
    assert pages_seen == {1, 2}, f"expected boxes on both pages, saw {pages_seen}"

    # Local percentages: every box must be a fraction of its OWN page
    # (0-100%), never a value implying it was scaled into a stacked band
    # (the old bug's global scheme kept page 2's boxes at top >= 50%).
    for b in boxes:
        assert 0 <= b["top"] <= 100, f"box top {b['top']} is not a local 0-100% value: {b}"
        assert 0 <= b["top"] + b["height"] <= 100.5, f"box extends past its own page: {b}"

    page1_tops = [b["top"] for b in boxes if b.get("page") == 1]
    page2_tops = [b["top"] for b in boxes if b.get("page") == 2]
    # Both pages have similar real content near their own top — under the
    # old global-stacked bug, page 2's local-looking top values would
    # actually have been shifted into the 50-100% band; here, real local
    # top values for both pages' first lines should land in a comparable,
    # low range close to each page's own top margin.
    assert min(page1_tops) < 20, f"page 1's first box should be near its own top, got {min(page1_tops)}"
    assert min(page2_tops) < 20, f"page 2's first box should be near ITS OWN top (not shifted into a stacked band), got {min(page2_tops)}"


def test_single_page_document_boxes_are_unaffected():
    """A one-page document's local percentage is numerically identical to
    the old global-stacked scheme (band = 100% when there's only one page),
    so this fix must not change anything for the already-working case.
    """
    doc = _build_doc(ONE_PAGE_FIXTURE)
    boxes = [r["box"] for r in doc["regions"] if r.get("box")]
    assert boxes
    for b in boxes:
        assert b.get("page") == 1
        assert 0 <= b["top"] <= 100


def test_ordinal_stack_boxes_attaches_page_when_given():
    """Regression for a real bug found during review: fallback regions with
    no stored bbox (an OCR'd line, a scanned-page-unprocessed marker) still
    carry their real page number in their own id (e.g.
    'page2!ocr-line1' — see adapters/document/adapter.py), but
    _ordinal_stack_boxes used to return boxes with no `page` field at all.
    Every consumer of a region box (webapp/static/app.js, render_html's
    patched preview loop) treats a missing `page` as page 1 — so a
    multi-page document's OCR'd/unavailable regions on page 2+ would have
    silently collapsed onto page 1's block instead of their own. Fixed by
    accepting an explicit `page` and attaching it to every returned box.
    """
    from build_prototype import _ordinal_stack_boxes

    boxes = _ordinal_stack_boxes(["a", "b"], page=2)
    assert set(boxes) == {"a", "b"}
    for box in boxes.values():
        assert box["page"] == 2

    # Omitting `page` must keep the old, page-less behavior for callers
    # that genuinely have no page to group by.
    boxes_no_page = _ordinal_stack_boxes(["a", "b"])
    for box in boxes_no_page.values():
        assert "page" not in box


if __name__ == "__main__":
    test_multi_page_boxes_are_local_percentages_with_a_page_number()
    test_single_page_document_boxes_are_unaffected()
    test_ordinal_stack_boxes_attaches_page_when_given()
    print("all document preview box tests passed")
