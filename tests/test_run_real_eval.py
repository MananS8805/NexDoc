"""Tests for eval/run_real_eval.py: the scoring logic and the "no
GROQ_API_KEY configured" skip path. Mocks qa.groq_qa.ask so the full
baseline-vs-structured loop runs against tests/fixtures/budget_test.xlsx
(a real fixture, real flatten/build_artifact calls) without a real network
call — this proves the eval's own plumbing (building both contexts,
calling ask() once per representation per question, scoring, tallying) is
correct independent of what a live model would actually answer.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import eval.run_real_eval as run_real_eval


def test_score_is_case_insensitive_substring_match():
    assert run_real_eval._score("The total is 3900 dollars", "3900") is True
    assert run_real_eval._score("the TOTAL is $3900 exactly", "3900") is True
    assert run_real_eval._score("I don't know", "3900") is False
    assert run_real_eval._score("", "3900") is False


def test_run_skips_cleanly_with_no_api_key(monkeypatch, capsys):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    result = run_real_eval.run()
    assert result is None
    out = capsys.readouterr().out
    assert "SKIPPED" in out
    assert "GROQ_API_KEY" in out


def test_run_scores_mocked_answers_against_qa_dataset(monkeypatch):
    """A fake ask() that always answers correctly for the baseline and
    always answers incorrectly for the structured context should produce
    baseline_correct == n and structured_correct == 0 — proving run()'s
    scoring/tally logic is wired correctly, independent of what a real
    model would say.
    """
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fake_for_this_test_only")

    import json

    qa_path = ROOT / "eval" / "qa_dataset.json"
    questions = json.loads(qa_path.read_text(encoding="utf-8"))
    expect_by_question = {q["question"]: q["expect_substring"] for q in questions}

    def fake_ask(question, context, *, model=None):
        # Distinguish which representation was passed by a marker only the
        # structured JSON contains (a region "id" field), without depending
        # on run_real_eval's internal variable names.
        if '"id":' in context:
            return "I cannot determine that from this representation."
        return f"The answer is {expect_by_question[question]}."

    monkeypatch.setattr(run_real_eval, "ask", fake_ask)

    result = run_real_eval.run()

    assert result is not None
    assert result["n"] == len(questions)
    assert result["baseline_correct"] == len(questions)
    assert result["baseline_pct"] == 100
    assert result["structured_correct"] == 0
    assert result["structured_pct"] == 0
    assert len(result["rows"]) == len(questions)


def test_run_records_api_failure_as_incorrect_not_a_crash(monkeypatch):
    """A GroqQAError from ask() (network failure, rate limit, ...) for one
    call must not crash the whole eval — it's scored as incorrect and the
    eval keeps going, per run_real_eval.py's own contract.
    """
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fake_for_this_test_only")

    from qa.groq_qa import GroqQAError

    def flaky_ask(question, context, *, model=None):
        raise GroqQAError("simulated network failure")

    monkeypatch.setattr(run_real_eval, "ask", flaky_ask)

    result = run_real_eval.run()

    assert result is not None
    assert result["baseline_correct"] == 0
    assert result["structured_correct"] == 0
    assert all(row["baseline_answer"] == "" for row in result["rows"])
