"""Proves the "reusable common schema" claim is real, not a design
intention: runs all three adapters against their real test fixtures and
validates each real output artifact against docs/schema.json. If any
adapter drifts from the shared schema, this fails — not a doc that quietly
goes stale.

Run:
    python -m pytest tests/test_schema_conformance.py -v

or directly:
    python tests/test_schema_conformance.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SCHEMA_PATH = ROOT / "docs" / "schema.json"
FIXTURES = {
    "spreadsheet": ROOT / "tests" / "fixtures" / "budget_test.xlsx",
    "document (born-digital)": ROOT / "tests" / "fixtures" / "invoice_born_digital.pdf",
    "document (scanned)": ROOT / "tests" / "fixtures" / "note_scanned.pdf",
    "diagram": ROOT / "tests" / "fixtures" / "flowchart_clean.png",
    "diagram (circle/ellipse shapes)": ROOT / "tests" / "fixtures" / "flowchart_with_circle.png",
    "diagram (sequence-diagram-shaped)": ROOT / "tests" / "fixtures" / "sequence_diagram_clean.png",
}


def _load_schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _build_all_artifacts() -> dict[str, dict]:
    from adapters.router import build_artifact_for

    return {label: build_artifact_for(path)[1] for label, path in FIXTURES.items()}


def test_all_fixtures_produce_schema_conformant_output():
    schema = _load_schema()
    validator = jsonschema.Draft7Validator(schema)

    for label, artifact in _build_all_artifacts().items():
        errors = sorted(validator.iter_errors(artifact), key=lambda e: e.path)
        assert not errors, (
            f"{label}: {len(errors)} schema violation(s) in real adapter output:\n"
            + "\n".join(f"  at {list(e.path)}: {e.message}" for e in errors)
        )


# Relationship types where a dangling target is a real bug. "cross-sheet-ref"
# is deliberately excluded: tests/fixtures/budget_test.xlsx contains a
# reference to a sheet that doesn't exist on purpose (Q1 Plan!F3 ->
# 'Forecast Q4'!B3), so its cross-sheet-ref target is SUPPOSED to dangle —
# that's precisely what the cross-sheet-references-resolve check exists to
# catch and report as failed. Asserting universal resolution here would
# penalize the adapter for correctly representing a broken reference.
MUST_RESOLVE_RELATIONSHIP_TYPES = {"formula-ref", "sums-line-items", "connects-to"}


def test_relationship_targets_resolve_within_artifact():
    """For relationship types that are supposed to always point at a real
    region (internal formula refs, document total<->line-item links, and
    diagram edges), does the target id actually exist in the same
    artifact's regions array? The schema itself can't express "must equal
    another array element's id," so this is checked separately.
    """
    for label, artifact in _build_all_artifacts().items():
        ids = {r["id"] for r in artifact["regions"]}
        for region in artifact["regions"]:
            for rel in region["relationships"]:
                if rel["type"] not in MUST_RESOLVE_RELATIONSHIP_TYPES:
                    continue
                assert rel["target"] in ids, (
                    f"{label}: region '{region['id']}' has a '{rel['type']}' relationship "
                    f"targeting '{rel['target']}', which is not a real region id in this artifact"
                )


def test_broken_cross_sheet_ref_is_caught_by_its_own_check():
    """The one relationship type allowed to dangle (cross-sheet-ref) must
    still be caught by name if it's actually broken — otherwise a dangling
    reference would just be silently invisible instead of being either
    valid or explicitly flagged.
    """
    from adapters.spreadsheet.adapter import build_artifact

    artifact = build_artifact(FIXTURES["spreadsheet"])
    ids = {r["id"] for r in artifact["regions"]}
    dangling = [
        (r["id"], rel["target"])
        for r in artifact["regions"]
        for rel in r["relationships"]
        if rel["type"] == "cross-sheet-ref" and rel["target"] not in ids
    ]
    assert dangling, "expected the fixture's deliberately-broken cross-sheet reference to still be present"

    check = next(c for c in artifact["checks"] if c["name"] == "cross-sheet-references-resolve")
    assert check["passed"] is False, "a dangling cross-sheet-ref exists but the check that should catch it passed"
    for region_id, target in dangling:
        assert target.split("!", 1)[0] in check["detail"], f"check detail doesn't mention the broken target '{target}'"


def test_diagram_edges_have_exactly_two_ordered_connects_to():
    """adapters.common.outputs._table_diagram assumes relationships[0] is
    'from' and relationships[1] is 'to' — this is the contract that assumption
    depends on, checked directly rather than just trusted.
    """
    from adapters.diagram.adapter import build_artifact

    artifact = build_artifact(FIXTURES["diagram"])
    edges = [r for r in artifact["regions"] if r["type"] == "edge"]
    assert edges, "expected at least one edge region in the diagram fixture"
    for edge in edges:
        connects = [rel for rel in edge["relationships"] if rel["type"] == "connects-to"]
        assert len(connects) == 2, f"edge '{edge['id']}' has {len(connects)} connects-to relationships, expected exactly 2"


if __name__ == "__main__":
    schema = _load_schema()
    validator = jsonschema.Draft7Validator(schema)
    artifacts = _build_all_artifacts()

    all_ok = True
    for label, artifact in artifacts.items():
        errors = list(validator.iter_errors(artifact))
        status = "PASS" if not errors else "FAIL"
        print(f"[{status}] {label}: {len(artifact['regions'])} regions, {len(artifact['checks'])} checks")
        for e in errors:
            all_ok = False
            print(f"    at {list(e.path)}: {e.message}")

    ids_ok = True
    for label, artifact in artifacts.items():
        ids = {r["id"] for r in artifact["regions"]}
        for region in artifact["regions"]:
            for rel in region["relationships"]:
                if rel["type"] not in MUST_RESOLVE_RELATIONSHIP_TYPES:
                    continue
                if rel["target"] not in ids:
                    ids_ok = False
                    print(f"[FAIL] {label}: '{region['id']}' -> dangling '{rel['type']}' target '{rel['target']}'")
    if ids_ok:
        print("[PASS] all must-resolve relationship targets resolve within their own artifact")

    try:
        test_broken_cross_sheet_ref_is_caught_by_its_own_check()
        print("[PASS] the fixture's deliberately-broken cross-sheet-ref is caught by its own check")
        broken_check_ok = True
    except AssertionError as e:
        print(f"[FAIL] {e}")
        broken_check_ok = False

    sys.exit(0 if (all_ok and ids_ok and broken_check_ok) else 1)
