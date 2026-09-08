# Eval methodology

## What exists today

Two real evals now exist for the spreadsheet adapter:

- [`eval/run_eval.py`](../eval/run_eval.py) — the deterministic
  fact-lookup eval, scoring the spreadsheet adapter's structured output
  against a naive flatten-to-CSV baseline on
  `tests/fixtures/budget_test.xlsx`. Result: baseline 0/6, structured 6/6.
  Documented with its full caveats in
  [`adapters/spreadsheet/README.md`](../adapters/spreadsheet/README.md#eval).
  No API key, no model call, runs in under a second — this stays the
  default eval.
- [`eval/run_real_eval.py`](../eval/run_real_eval.py) — the real
  task-accuracy eval described below, added to close the gap this document
  used to describe as unbuilt. It requires a `GROQ_API_KEY` and is opt-in
  (a separate script, not a flag that changes `run_eval.py`'s default
  behavior).

Documents and diagrams have **no eval harness at all yet**. The UI
prototype's comparison bars for those two kinds are illustrative targets
carried over from the original design mock — not measurements, and stated
as such directly in the prototype's own text.

## The one distinction that matters: what is actually being measured

There are two genuinely different things a "spreadsheet eval" could mean,
and it matters which one you're looking at:

1. **Fact-recoverability** — is the specific fact a question asks about
   even *present*, in some form, in this representation? This is what
   `run_eval.py` measures. It looks up the exact cell or region a question
   names (e.g. "Q1 Plan!E6") in each representation and checks whether the
   expected value is there. No model is involved. It answers "did
   flattening destroy this information," not "can something read this
   representation and reason correctly about it."

2. **Task accuracy** — given this representation, can an LLM (or a human)
   actually answer the question correctly? This requires a real model call,
   scoring free-text answers, and dealing with everything that comes with
   that (prompt sensitivity, partial credit, the model's own reasoning
   failures independent of the data). **This is now built**, in
   [`eval/run_real_eval.py`](../eval/run_real_eval.py) and
   [`qa/groq_qa.py`](../qa/groq_qa.py) — see "The real task-accuracy eval"
   below. An earlier version of `run_eval.py` was written to do this via
   the Anthropic API, but no API key was available in the environment this
   was built in, and CLAUDE.md's guardrail is explicit: never present an
   estimated number as measured. Rather than leave the eval blocked on a
   credential, it was rewritten to measure (1) instead — a real, weaker,
   but honestly-labeled claim. Distinction (1) still stands on its own and
   `run_eval.py` still exists and still runs by default: (2) doesn't
   replace it, it answers the different, stronger question (1) was never
   trying to answer.

**Read every number produced by this repo's eval against distinction (1),
not (2).** "Structured scores 100%, baseline scores 0%" means "every fact
these 6 questions ask about is recoverable from the structured JSON and
none of them are recoverable from the flattened CSV" — a true and
meaningful statement about information loss, and a much narrower one than
"an LLM answers these questions correctly 100% of the time given the
structured JSON."

## Why the spreadsheet baseline is exactly 0%, not "low"

This is a property of the specific test fixture, not a general truth about
flattening spreadsheets. `tests/fixtures/generate_test_workbook.py` builds
its workbook with `openpyxl`, which never computes formulas — so the file
has no cached formula results anywhere. `flatten_to_csv`'s
`data_only=True` read finds nothing to fall back on and every formula cell
comes through blank. All six eval questions happen to target
formula-dependent cells, so the baseline can't get any of them right. A
real Excel-authored file usually *does* have cached values, and a fixture
with more literal (non-formula) data would show the baseline scoring above
0% on the questions that don't depend on a formula. Verified directly (see
[`adapters/spreadsheet/README.md`](../adapters/spreadsheet/README.md#eval))
by opening the same file with `data_only=True` vs `False` and comparing —
not assumed.

## The real task-accuracy eval (`eval/run_real_eval.py`)

This closes the gap the previous section of this document used to
describe as unbuilt. It reuses `eval/qa_dataset.json` exactly as the old
"How to extend this later" section proposed — the `check` field
(`cell_value`, `cross_sheet_ref`, `unresolved_reason`) already named what
each question tests structurally, so no new dataset was needed.

**What it measures.** For each question in `qa_dataset.json`, a real Groq-
hosted LLM (via [`qa/groq_qa.py`](../qa/groq_qa.py)) is asked the question
twice: once given the flattened CSV baseline as context, once given the
spreadsheet's structured common-schema JSON (`to_structured()`'s region
list — see `qa/groq_qa.py`'s module docstring for why that representation
specifically, not `to_text()` or the full `to_json()`). Each free-text
answer is scored against `expect_substring` with the same case-insensitive
substring match `run_eval.py` already uses. This is genuinely distinction
(2) from the section above: a model actually reasoning over the
representation, not a direct code lookup.

**Requirements.** A real `GROQ_API_KEY` (see
[`.env.example`](../.env.example) at the repo root — copy it to `.env` and
fill in a key from https://console.groq.com/keys). Without one,
`run_real_eval.py` prints a clear "SKIPPED" message and exits 0 — it never
crashes and never fabricates a number for a mode that didn't actually run,
per CLAUDE.md's guardrail.

**How to run it:**

```bash
pip install -r requirements.txt   # adds groq + python-dotenv
cp .env.example .env              # then edit .env and set GROQ_API_KEY
python eval/run_real_eval.py
```

It is a separate, opt-in script — `eval/run_eval.py` is unchanged and
still runs instantly, offline, for free, as the default eval.

**Honesty about verification.** The code path was built and is covered by
mocked unit tests
([`tests/test_groq_qa.py`](../tests/test_groq_qa.py),
[`tests/test_run_real_eval.py`](../tests/test_run_real_eval.py)) that
inject a fake Groq client to prove the prompt is built correctly, the
response is parsed correctly, and scoring/tallying is correct — independent
of what a real model says.

**A real key was supplied and this has now actually been run once**, on
`openai/gpt-oss-120b` via Groq. Real, measured result:

```
baseline (flattened CSV) task accuracy:        1/6 = 17%
structured (common schema JSON) task accuracy: 6/6 = 100%
```

Structured hit the same ceiling the deterministic eval (distinction (1))
already found — every fact is recoverable, and the model reliably recovers
it. The baseline scoring *1/6 instead of 0/6* is the interesting part, and
worth understanding rather than treating as noise: q2 asks for
`Actuals!B6` (`=SUM(B3:B5)`), a formula cell that — like every formula cell
in this fixture — comes through blank in the flattened CSV. The model
answered "4980" correctly anyway. It didn't recover a hidden value; the
three literal inputs the formula sums (`B3=1150`, `B4=2950`, `B5=880`) are
still individually visible as plain numbers elsewhere in the same CSV, and
the model added them itself. That's a real capability the deterministic
fact-lookup eval cannot see, by construction — it only checks the exact
cell a question names, never reasons across other cells the way a model
can. It's also exactly the "different, stronger claim" distinction (2)
exists to measure: a model given the baseline can sometimes *partially*
route around missing structure by re-deriving it, something a direct code
lookup never attempts. Notably, this reconstruction wasn't reliable even
within this one run: q1, q3, and q6 ask for the exact same kind of thing
(another row's SUM total, individually visible inputs still present in the
CSV) and the model did *not* reconstruct any of them correctly this time —
so "sometimes" is doing real work in that sentence, not "always when
possible." q4 (a cross-sheet relationship) and q5 (why a formula fails to
resolve) ask for something no amount of re-summation could recover
regardless. All five of those failed, giving the observed 1/6.

## Extending eval to document/diagram

Neither adapter has a `qa_dataset.json`-equivalent yet, because that
requires hand-authoring ground truth: which field of `invoice_born_digital.pdf`
does a question target, or which edge of `flowchart_clean.png`. This is
explicit, deliberate scope-narrowing, not an oversight — `run_adapter.py`
says so directly when pointed at a file with no matching eval:

```
eval: not run. The deterministic eval requires hand-authored ground truth
(which cell/region each question targets, and the expected answer) — that
only exists today for tests/fixtures/budget_test.xlsx. ...
```

Building one for documents would look like `eval/qa_dataset.json` but with
`sheet`/`ref` replaced by a region id from that PDF's real
`page1!blockN` scheme; for diagrams, by a `node-N`/`edge-N` id. The check
functions in `eval/run_eval.py` (`_check_cell_value`, etc.) would need
document/diagram-specific counterparts, since they currently parse
spreadsheet-shaped CSV grids specifically.
