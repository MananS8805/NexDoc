"""Deterministic fact-lookup eval: naive text-flatten baseline vs. structured
adapter JSON, over tests/fixtures/invoice_born_digital.pdf.

Mirrors eval/run_eval.py's exact shape and honesty conventions — same
CHECKS-dict pattern, same run(fixture_path, qa_path) -> dict signature and
return shape, no LLM call, no API key. See that file's docstring for the
"is the fact even present" framing this shares.

Important, honest difference from the spreadsheet eval: verified directly
(adapters/document/baseline.py's docstring) that a naive text-flatten of a
born-digital PDF is line-for-line identical to what the real adapter's
to_text() produces for tests/fixtures/invoice_born_digital.pdf. A document
does NOT lose facts by being flattened to text the way a spreadsheet's
formula cells do — so `field_value` questions are expected to pass on
BOTH sides here, not just structured. The real, unbridgeable gap this eval
measures is semantic role and structural relationships: `relationship_target`
and `region_type` questions ask for something a flat text blob has never
had a way to represent, no matter how good the text extraction is.

Usage:
    python eval/run_document_eval.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from adapters.document.adapter import build_artifact
from adapters.document.baseline import flatten_to_text

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "invoice_born_digital.pdf"
QA_PATH = Path(__file__).resolve().parent / "qa_document_dataset.json"

REGION_MARKER_RE = re.compile(r"^# region: (.+)$")


def _parse_baseline_regions(text: str) -> dict[str, str]:
    """Splits flatten_to_text()'s output back into {region_id: text},
    mirroring run_eval.py's _parse_csv_sheets — reads the baseline's own
    marker format rather than re-deriving it independently.
    """
    regions: dict[str, str] = {}
    current_id = None
    current_lines: list[str] = []
    for line in text.split("\n"):
        m = REGION_MARKER_RE.match(line)
        if m:
            if current_id is not None:
                regions[current_id] = "\n".join(current_lines).strip()
            current_id = m.group(1)
            current_lines = []
        else:
            current_lines.append(line)
    if current_id is not None:
        regions[current_id] = "\n".join(current_lines).strip()
    return regions


def _check_field_value(q, baseline_regions, regions_by_id, artifact) -> tuple[bool, str, bool, str]:
    baseline_val = baseline_regions.get(q["ref"], "")
    structured_val = (regions_by_id.get(q["ref"]) or {}).get("text") or ""
    expect = q["expect_substring"]
    return (
        expect.lower() in baseline_val.lower(),
        baseline_val or "(blank)",
        expect.lower() in structured_val.lower(),
        structured_val or "(blank)",
    )


def _check_relationship_target(q, baseline_regions, regions_by_id, artifact) -> tuple[bool, str, bool, str]:
    # Baseline is restricted to only the named region's own text — never the
    # whole flattened blob, which would let an unrelated line item elsewhere
    # on the page give the baseline undeserved credit (the same rule
    # run_eval.py's docstring states for the spreadsheet eval).
    baseline_val = baseline_regions.get(q["ref"], "")
    region = regions_by_id.get(q["ref"]) or {}
    targets = [rel["target"] for rel in region.get("relationships", []) if rel["type"] == "sums-line-items"]
    structured_answer = (
        "; ".join((regions_by_id.get(t) or {}).get("text") or t for t in targets)
        or "(no relationship recorded)"
    )
    expect = q["expect_substring"]
    return (
        expect.lower() in baseline_val.lower(),
        baseline_val or "(blank — this region's own text names no line items)",
        expect.lower() in structured_answer.lower(),
        structured_answer,
    )


def _check_region_type(q, baseline_regions, regions_by_id, artifact) -> tuple[bool, str, bool, str]:
    baseline_val = baseline_regions.get(q["ref"], "")
    structured_val = (regions_by_id.get(q["ref"]) or {}).get("type") or ""
    expect = q["expect_substring"]
    return (
        expect.lower() in baseline_val.lower(),
        baseline_val or "(blank)",
        expect.lower() in structured_val.lower(),
        structured_val or "(no type recorded)",
    )


def _check_check_result(q, baseline_regions, regions_by_id, artifact) -> tuple[bool, str, bool, str]:
    # Plain text has never computed anything — there is no artifact-level
    # validation concept for the baseline to consult, at all, by construction.
    baseline_val = "(no computed check available — plain text has no artifact-level validation)"
    check = next((c for c in artifact["checks"] if c["name"] == q["ref"]), None)
    structured_val = check["detail"] if check else f"(no check named '{q['ref']}' found)"
    expect = q["expect_substring"]
    return (
        expect.lower() in baseline_val.lower(),
        baseline_val,
        expect.lower() in structured_val.lower(),
        structured_val,
    )


CHECKS = {
    "field_value": _check_field_value,
    "relationship_target": _check_relationship_target,
    "region_type": _check_region_type,
    "check_result": _check_check_result,
}


def run(fixture_path: Path = FIXTURE_PATH, qa_path: Path = QA_PATH) -> dict:
    """Runs the deterministic fact-lookup eval against any born-digital PDF
    + question set in this same shape. Defaults to the known fixture, so
    `python eval/run_document_eval.py` with no arguments is the reproducible
    baseline result — parameterized the same way eval/run_eval.py is, so
    run_adapter.py's --eval-questions flag can reuse this for another file.
    """
    questions = json.loads(Path(qa_path).read_text(encoding="utf-8"))
    for q in questions:
        if q.get("check") not in CHECKS:
            raise ValueError(
                f"question '{q.get('id', '?')}' has unknown check type '{q.get('check')}' "
                f"— supported: {sorted(CHECKS)}"
            )

    baseline_regions = _parse_baseline_regions(flatten_to_text(fixture_path))
    artifact = build_artifact(fixture_path)
    regions_by_id = {r["id"]: r for r in artifact["regions"]}

    baseline_correct = 0
    structured_correct = 0
    rows = []

    for q in questions:
        check_fn = CHECKS[q["check"]]
        baseline_ok, baseline_answer, structured_ok, structured_answer = check_fn(
            q, baseline_regions, regions_by_id, artifact
        )
        baseline_correct += baseline_ok
        structured_correct += structured_ok
        rows.append(
            {
                "id": q["id"],
                "question": q["question"],
                "baseline_answer": baseline_answer,
                "baseline_ok": baseline_ok,
                "structured_answer": structured_answer,
                "structured_ok": structured_ok,
            }
        )
        print(f"[{q['id']}] {q['question']}")
        print(f"  baseline   ({'PASS' if baseline_ok else 'FAIL'}): {baseline_answer}")
        print(f"  structured ({'PASS' if structured_ok else 'FAIL'}): {structured_answer}")
        print()

    n = len(questions)
    baseline_pct = round(100 * baseline_correct / n)
    structured_pct = round(100 * structured_correct / n)

    print("=== Result (real, measured — deterministic fact-lookup eval, not illustrative) ===")
    print(f"baseline (flattened text): {baseline_correct}/{n} = {baseline_pct}%")
    print(f"structured (common schema JSON): {structured_correct}/{n} = {structured_pct}%")

    return {
        "n": n,
        "baseline_correct": baseline_correct,
        "baseline_pct": baseline_pct,
        "structured_correct": structured_correct,
        "structured_pct": structured_pct,
        "rows": rows,
    }


if __name__ == "__main__":
    run()
