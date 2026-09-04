"""Deterministic fact-lookup eval: naive CSV-flatten baseline vs. structured
adapter JSON, over tests/fixtures/budget_test.xlsx.

For each question in qa_dataset.json, this checks whether the specific fact
being asked about is actually *recoverable* from each representation — by
looking up the exact cell/region the question is about (never scanning the
whole flattened blob, which would let an unrelated sheet name elsewhere in
the CSV give the baseline undeserved credit) and testing for the expected
substring there. No LLM call, no API key, fully reproducible offline.

This is intentionally a lower bar than full LLM comprehension — it answers
"is the fact even present in this representation," not "can a model reason
about it." That's still the core claim CLAUDE.md's problem statement makes
(generic flattening loses relationships), and it's honest, real, and
measured rather than fabricated. An LLM-based eval (asking a model the
question and scoring its free-text answer) can replace or supplement this
later without changing qa_dataset.json's shape — see the "check" field,
which already names what's being tested per question.

Usage:
    python eval/run_eval.py
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Windows terminals often default stdout to a legacy codepage that can't
# encode the "→" characters that appear in formula-cell text; force UTF-8
# rather than let the eval crash on an unrelated console-encoding issue.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from openpyxl.utils.cell import coordinate_from_string, column_index_from_string

from adapters.spreadsheet.adapter import build_artifact
from adapters.spreadsheet.baseline import flatten_to_csv

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "budget_test.xlsx"
QA_PATH = Path(__file__).resolve().parent / "qa_dataset.json"

SHEET_MARKER_RE = re.compile(r"^# sheet: (.+)$")


def _parse_csv_sheets(csv_text: str) -> dict[str, list[list[str]]]:
    """Splits flatten_to_csv()'s output back into per-sheet grids, keyed by
    sheet name. Mirrors baseline.py's own format (a "# sheet: <name>" marker
    line followed by CSV rows) rather than re-deriving it independently.
    """
    sheets: dict[str, list[str]] = {}
    current_sheet = None
    current_lines: list[str] = []
    for line in csv_text.split("\n"):
        m = SHEET_MARKER_RE.match(line)
        if m:
            if current_sheet is not None:
                sheets[current_sheet] = current_lines
            current_sheet = m.group(1)
            current_lines = []
        else:
            current_lines.append(line)
    if current_sheet is not None:
        sheets[current_sheet] = current_lines

    grids = {}
    for sheet_name, lines in sheets.items():
        rows = list(csv.reader(io.StringIO("\n".join(lines))))
        grids[sheet_name] = [r for r in rows if r]
    return grids


def _baseline_cell(grids: dict[str, list[list[str]]], sheet: str, ref: str) -> str:
    col_letter, row = coordinate_from_string(ref)
    col = column_index_from_string(col_letter)
    grid = grids.get(sheet, [])
    if row - 1 >= len(grid):
        return ""
    r = grid[row - 1]
    if col - 1 >= len(r):
        return ""
    return r[col - 1]


def _structured_region(regions_by_id: dict[str, dict], sheet: str, ref: str) -> dict | None:
    return regions_by_id.get(f"{sheet}!{ref}")


def _check_cell_value(q, grids, regions_by_id) -> tuple[bool, str, bool, str]:
    baseline_val = _baseline_cell(grids, q["sheet"], q["ref"])
    region = _structured_region(regions_by_id, q["sheet"], q["ref"])
    structured_val = (region or {}).get("text") or ""
    expect = q["expect_substring"]
    return (
        expect.lower() in baseline_val.lower(),
        baseline_val or "(blank)",
        expect.lower() in structured_val.lower(),
        structured_val or "(blank)",
    )


def _check_cross_sheet_ref(q, grids, regions_by_id) -> tuple[bool, str, bool, str]:
    baseline_val = _baseline_cell(grids, q["sheet"], q["ref"])
    region = _structured_region(regions_by_id, q["sheet"], q["ref"]) or {}
    targets = [rel["target"] for rel in region.get("relationships", []) if rel["type"] == "cross-sheet-ref"]
    structured_answer = ", ".join(t.split("!", 1)[0] for t in targets) or "(no cross-sheet relationship recorded)"
    expect = q["expect_substring"]
    return (
        expect.lower() in baseline_val.lower(),
        baseline_val or "(blank — formula not evaluated, relationship lost)",
        expect.lower() in structured_answer.lower(),
        structured_answer,
    )


def _check_unresolved_reason(q, grids, regions_by_id) -> tuple[bool, str, bool, str]:
    baseline_val = _baseline_cell(grids, q["sheet"], q["ref"])
    region = _structured_region(regions_by_id, q["sheet"], q["ref"])
    structured_val = (region or {}).get("text") or ""
    expect = q["expect_substring"]
    return (
        expect.lower() in baseline_val.lower(),
        baseline_val or "(blank — formula not evaluated, no reason available)",
        expect.lower() in structured_val.lower(),
        structured_val or "(blank)",
    )


CHECKS = {
    "cell_value": _check_cell_value,
    "cross_sheet_ref": _check_cross_sheet_ref,
    "unresolved_reason": _check_unresolved_reason,
}


def run(fixture_path: Path = FIXTURE_PATH, qa_path: Path = QA_PATH) -> dict:
    """Runs the deterministic fact-lookup eval against any .xlsx + question
    set in this same shape (see qa_dataset.json). Defaults to the known
    fixture, so `python eval/run_eval.py` with no arguments behaves exactly
    as before — this parameterization exists so run_adapter.py's
    --eval-questions flag can reuse this same real check logic for a
    different spreadsheet, not to change default behavior.
    """
    questions = json.loads(Path(qa_path).read_text(encoding="utf-8"))
    for q in questions:
        if q.get("check") not in CHECKS:
            raise ValueError(
                f"question '{q.get('id', '?')}' has unknown check type '{q.get('check')}' "
                f"— supported: {sorted(CHECKS)}"
            )
    grids = _parse_csv_sheets(flatten_to_csv(fixture_path))
    artifact = build_artifact(fixture_path)
    regions_by_id = {r["id"]: r for r in artifact["regions"]}

    baseline_correct = 0
    structured_correct = 0
    rows = []

    for q in questions:
        check_fn = CHECKS[q["check"]]
        baseline_ok, baseline_answer, structured_ok, structured_answer = check_fn(q, grids, regions_by_id)
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
    print(f"baseline (flattened CSV): {baseline_correct}/{n} = {baseline_pct}%")
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
