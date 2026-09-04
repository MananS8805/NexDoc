"""Real task-accuracy eval: an actual Groq-hosted LLM answers each question
in `qa_dataset.json`, given (a) the flattened CSV baseline and (b) the
structured common-schema JSON, and each free-text answer is scored against
`expect_substring`.

This is the eval `docs/eval-methodology.md` says has never been built —
"task accuracy" (does a model actually answer correctly), as opposed to
`eval/run_eval.py`'s "fact-recoverability" (is the fact present at all, no
model involved). It is a **separate, opt-in script**, not a change to
`run_eval.py`'s default behavior: `python eval/run_eval.py` still runs
instantly, offline, and for free, exactly as before. This script makes real
network calls to Groq, costs a (small) amount of API usage, and is only
useful once a real `GROQ_API_KEY` is configured.

Usage:
    python eval/run_real_eval.py

If GROQ_API_KEY isn't set (see .env.example), this prints a clear
explanation and exits 0 — it never crashes and never fabricates a number
for a mode that didn't actually run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

from adapters.common.outputs import to_structured
from adapters.spreadsheet.adapter import build_artifact
from adapters.spreadsheet.baseline import flatten_to_csv
from qa.groq_qa import DEFAULT_MODEL, GroqQAError, api_key_status, ask

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "budget_test.xlsx"
QA_PATH = Path(__file__).resolve().parent / "qa_dataset.json"


def _score(answer: str, expect_substring: str) -> bool:
    """Case-insensitive substring match — the same semantics `run_eval.py`
    uses (`expect.lower() in value.lower()`), applied here to a model's
    free-text answer instead of a directly-looked-up cell/region value."""
    return expect_substring.lower() in answer.lower()


def run(
    fixture_path: Path = FIXTURE_PATH,
    qa_path: Path = QA_PATH,
    model: str | None = None,
) -> dict | None:
    """Returns the result dict on a real run, or None if skipped because no
    GROQ_API_KEY is configured (checked, never guessed at)."""
    available, detail = api_key_status()
    if not available:
        print("=== Real task-accuracy eval: SKIPPED ===")
        print(detail)
        print(
            "\nThis mode requires a real Groq API key because, unlike "
            "eval/run_eval.py, it actually calls a model rather than doing a "
            "direct code lookup. See docs/eval-methodology.md."
        )
        return None

    questions = json.loads(Path(qa_path).read_text(encoding="utf-8"))
    baseline_context = flatten_to_csv(fixture_path)
    artifact = build_artifact(fixture_path)
    structured_context = json.dumps(to_structured(artifact), indent=2, default=str)

    model = model or DEFAULT_MODEL
    print(f"=== Real task-accuracy eval (model: {model}) ===\n")

    baseline_correct = 0
    structured_correct = 0
    rows = []

    for q in questions:
        question = q["question"]
        expect = q["expect_substring"]

        def _ask(context: str, label: str) -> str:
            try:
                return ask(question, context, model=model)
            except GroqQAError as exc:
                # A real API failure (network, rate limit, bad key discovered
                # mid-run) is reported per-question and scored as incorrect —
                # it does not crash the whole eval or fabricate an answer.
                print(f"  [{label} call failed] {exc}")
                return ""

        baseline_answer = _ask(baseline_context, "baseline")
        structured_answer = _ask(structured_context, "structured")

        baseline_ok = _score(baseline_answer, expect)
        structured_ok = _score(structured_answer, expect)
        baseline_correct += baseline_ok
        structured_correct += structured_ok

        rows.append(
            {
                "id": q["id"],
                "question": question,
                "expect_substring": expect,
                "baseline_answer": baseline_answer,
                "baseline_ok": baseline_ok,
                "structured_answer": structured_answer,
                "structured_ok": structured_ok,
            }
        )
        print(f"[{q['id']}] {question}  (expect substring: {expect!r})")
        print(f"  baseline   ({'PASS' if baseline_ok else 'FAIL'}): {baseline_answer}")
        print(f"  structured ({'PASS' if structured_ok else 'FAIL'}): {structured_answer}")
        print()

    n = len(questions)
    baseline_pct = round(100 * baseline_correct / n)
    structured_pct = round(100 * structured_correct / n)

    print("=== Result (real, measured — a real LLM answered each question) ===")
    print(f"baseline (flattened CSV) task accuracy:      {baseline_correct}/{n} = {baseline_pct}%")
    print(f"structured (common schema JSON) task accuracy: {structured_correct}/{n} = {structured_pct}%")

    return {
        "model": model,
        "n": n,
        "baseline_correct": baseline_correct,
        "baseline_pct": baseline_pct,
        "structured_correct": structured_correct,
        "structured_pct": structured_pct,
        "rows": rows,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_real_eval.py",
        description="Real, model-backed task-accuracy eval (requires GROQ_API_KEY). "
        "See docs/eval-methodology.md.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"Groq model id to use (default: {DEFAULT_MODEL}, or $GROQ_MODEL if set).",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run(model=args.model)  # exits 0 either way — a skip (no key) is not an error
