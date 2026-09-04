# Eval methodology

## What exists today

One real, measured eval: [`eval/run_eval.py`](../eval/run_eval.py),
scoring the spreadsheet adapter's structured output against a naive
flatten-to-CSV baseline on `tests/fixtures/budget_test.xlsx`. Result:
baseline 0/6, structured 6/6. Documented with its full caveats in
[`adapters/spreadsheet/README.md`](../adapters/spreadsheet/README.md#eval).

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
   failures independent of the data). **This has not been built.** An
   earlier version of `run_eval.py` was written to do this via the
   Anthropic API, but no API key was available in the environment this was
   built in, and CLAUDE.md's guardrail is explicit: never present an
   estimated number as measured. Rather than leave the eval blocked on a
   credential, it was rewritten to measure (1) instead — a real, weaker,
   but honestly-labeled claim.

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

## How to extend this to a real task-accuracy eval later

`eval/qa_dataset.json`'s `check` field (`cell_value`, `cross_sheet_ref`,
`unresolved_reason`) already names what each question is testing
structurally. A task-accuracy eval could reuse this same dataset shape:
for each question, call an LLM with (a) the baseline text and (b) the
structured JSON as context, ask the question, and score the free-text
answer against `expect_substring` — closer to what CLAUDE.md's Phase 1
originally specified. That requires an API key this environment doesn't
have; the moment one is available, `eval/run_eval.py`'s `_ask`-style
pattern (present in an earlier draft of this file, removed rather than
left half-working) is the natural place to add it back, likely gated
behind a flag so the fast, free, deterministic eval stays the default.

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
