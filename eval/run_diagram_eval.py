"""Deterministic fact-lookup eval: naive whole-image-OCR label baseline vs.
structured adapter JSON, over tests/fixtures/flowchart_clean.png.

Mirrors eval/run_eval.py's exact shape and honesty conventions — same
CHECKS-dict pattern, same run(fixture_path, qa_path) -> dict signature and
return shape, no LLM call, no API key.

Unlike the spreadsheet and document evals, `label_value` here is checked
against the WHOLE flattened label blob, not one addressed region — the
naive baseline (adapters/diagram/baseline.py) has no concept of "this
specific node's label," only an unordered bag of OCR'd strings, so there is
no narrower address to look up. This is not a loosened rule to inflate the
baseline's score: verified directly, running EasyOCR over the whole image
at once (rather than per-shape cropped, the way the real adapter does it)
measurably groups text differently than shape-aware extraction — text from
adjacent nodes gets merged onto the same "line," and a multi-word label
sometimes splits across lines instead of staying contiguous. Both effects
are visible in a real run; see the printed baseline answers.

`topology_target` and `check_result` fail on the baseline by construction,
not by a substring search happening to miss: a flat bag of OCR'd labels has
zero connectivity data and zero computed validation, for any label, ever.
This is a cleaner, more absolute illustration of CLAUDE.md's claim about
diagram topology than the spreadsheet eval's cross-sheet-ref check (whose
baseline failure is a coincidence of an unevaluated formula, not a
structural impossibility).

Usage:
    python eval/run_diagram_eval.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from adapters.diagram.adapter import build_artifact
from adapters.diagram.baseline import flatten_labels

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "flowchart_clean.png"
QA_PATH = Path(__file__).resolve().parent / "qa_diagram_dataset.json"

NO_TOPOLOGY = "(no topology recorded — this representation is a flat bag of OCR'd labels, not a graph)"
NO_CHECKS = "(no computed check available — a flat label bag has no artifact-level validation)"


def _check_label_value(q, baseline_blob, regions_by_id, artifact) -> tuple[bool, str, bool, str]:
    structured_val = (regions_by_id.get(q["ref"]) or {}).get("text") or ""
    expect = q["expect_substring"]
    return (
        expect.lower() in baseline_blob.lower(),
        baseline_blob or "(no labels recovered)",
        expect.lower() in structured_val.lower(),
        structured_val or "(blank)",
    )


def _check_topology_target(q, baseline_blob, regions_by_id, artifact) -> tuple[bool, str, bool, str]:
    edge = regions_by_id.get(q["ref"]) or {}
    targets = [rel["target"] for rel in edge.get("relationships", []) if rel["type"] == "connects-to"]
    # relationships[0] is "from", relationships[1] is "to" (adapters/diagram/adapter.py's
    # construction order, relied on by adapters.common.outputs._table_diagram too).
    # A question can ask about either end — "what happens next" wants "to",
    # "what leads into this" wants "from" — so this is explicit per question
    # (q["direction"], defaulting to "to") rather than a fixed index.
    direction = q.get("direction", "to")
    idx = 0 if direction == "from" else 1
    target_id = targets[idx] if len(targets) > idx else None
    structured_val = (regions_by_id.get(target_id) or {}).get("text") or "(no target node recorded)"
    expect = q["expect_substring"]
    return (
        expect.lower() in NO_TOPOLOGY.lower(),
        NO_TOPOLOGY,
        expect.lower() in structured_val.lower(),
        structured_val,
    )


def _check_check_result(q, baseline_blob, regions_by_id, artifact) -> tuple[bool, str, bool, str]:
    check = next((c for c in artifact["checks"] if c["name"] == q["ref"]), None)
    structured_val = check["detail"] if check else f"(no check named '{q['ref']}' found)"
    expect = q["expect_substring"]
    return (
        expect.lower() in NO_CHECKS.lower(),
        NO_CHECKS,
        expect.lower() in structured_val.lower(),
        structured_val,
    )


CHECKS = {
    "label_value": _check_label_value,
    "topology_target": _check_topology_target,
    "check_result": _check_check_result,
}


def run(fixture_path: Path = FIXTURE_PATH, qa_path: Path = QA_PATH) -> dict:
    """Runs the deterministic fact-lookup eval against any diagram image +
    question set in this same shape. Defaults to the known fixture, so
    `python eval/run_diagram_eval.py` with no arguments is the reproducible
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

    baseline_blob = flatten_labels(fixture_path)
    artifact = build_artifact(fixture_path)
    regions_by_id = {r["id"]: r for r in artifact["regions"]}

    baseline_correct = 0
    structured_correct = 0
    rows = []

    for q in questions:
        check_fn = CHECKS[q["check"]]
        baseline_ok, baseline_answer, structured_ok, structured_answer = check_fn(
            q, baseline_blob, regions_by_id, artifact
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
    print(f"baseline (flattened OCR labels): {baseline_correct}/{n} = {baseline_pct}%")
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
