"""Dedicated unit tests for the document adapter — region classification per
type against the real born-digital fixture, both real checks' pass
behavior, and the honest scanned-page fallback path (OCR-unavailable vs.
OCR-available). Until now the document adapter (Phase 2) had zero
adapter-specific tests of its own — only indirect coverage via
tests/test_schema_conformance.py and eval/run_document_eval.py — unlike the
diagram adapter's three dedicated test files.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

INVOICE_FIXTURE = ROOT / "tests" / "fixtures" / "invoice_born_digital.pdf"
SCANNED_FIXTURE = ROOT / "tests" / "fixtures" / "note_scanned.pdf"


def _build(path):
    from adapters.document.adapter import build_artifact

    return build_artifact(path)


def _regions_by_id(artifact):
    return {r["id"]: r for r in artifact["regions"]}


def test_region_classification_on_born_digital_invoice():
    artifact = _build(INVOICE_FIXTURE)
    regions = _regions_by_id(artifact)
    assert len(artifact["regions"]) == 7

    assert regions["page1!block0"]["type"] == "title"
    assert regions["page1!block0"]["text"] == "Lumière Textiles SARL"
    assert regions["page1!block3"]["type"] == "table-header"
    assert regions["page1!block4"]["type"] == "line-item"
    assert regions["page1!block5"]["type"] == "line-item"
    assert regions["page1!block6"]["type"] == "total-field"

    # Every born-digital block gets the same confidence — the real signal is
    # binary ("this page has an embedded text layer"), not per-block.
    for region in artifact["regions"]:
        assert region["confidence"] == 0.98


def test_total_field_links_to_its_line_items_via_relationship():
    artifact = _build(INVOICE_FIXTURE)
    regions = _regions_by_id(artifact)
    total = regions["page1!block6"]
    targets = {rel["target"] for rel in total["relationships"] if rel["type"] == "sums-line-items"}
    assert targets == {"page1!block4", "page1!block5"}


def test_total_matches_line_items_check_passes_on_real_numbers():
    artifact = _build(INVOICE_FIXTURE)
    check = next(c for c in artifact["checks"] if c["name"] == "total-matches-line-items")
    assert check["passed"] is True
    assert "4500.00" in check["detail"]


def test_date_fields_parse_check_passes_on_a_real_valid_date():
    artifact = _build(INVOICE_FIXTURE)
    check = next(c for c in artifact["checks"] if c["name"] == "date-fields-parse")
    assert check["passed"] is True
    assert "14/08/2024" in check["detail"]


def test_scanned_page_produces_real_content_or_honestly_degrades():
    """This machine has EasyOCR available (verified elsewhere this session),
    so the real path is exercised: a scanned page with real English+Hindi
    content should produce actual OCR'd regions, not the unprocessed
    fallback. If OCR genuinely isn't available in whatever environment runs
    this test, it must degrade to the honest scanned-page-unprocessed
    region instead of fabricating text — both real outcomes are asserted
    for, rather than assuming which one applies.
    """
    from adapters.common.ocr import ocr_engine_status

    artifact = _build(SCANNED_FIXTURE)
    ocr_available, _ = ocr_engine_status()
    check = next(c for c in artifact["checks"] if c["name"] == "ocr-engine-available")

    if ocr_available:
        assert check["passed"] is True
        assert any(r["type"] == "ocr-line" for r in artifact["regions"])
        assert all(r["text"] is not None for r in artifact["regions"] if r["type"] == "ocr-line")
    else:
        assert check["passed"] is False
        unprocessed = [r for r in artifact["regions"] if r["type"] == "scanned-page-unprocessed"]
        assert unprocessed and all(r["text"] is None and r["confidence"] == 0.0 for r in unprocessed)


if __name__ == "__main__":
    test_region_classification_on_born_digital_invoice()
    test_total_field_links_to_its_line_items_via_relationship()
    test_total_matches_line_items_check_passes_on_real_numbers()
    test_date_fields_parse_check_passes_on_a_real_valid_date()
    test_scanned_page_produces_real_content_or_honestly_degrades()
    print("all document adapter tests passed")
