# Spreadsheet adapter

Phase 1 of the semantic preprocessing pipeline (see `/CLAUDE.md`). Converts a
`.xlsx` workbook into the common node/edge schema without flattening
formulas to values first.

## Input

A path to an `.xlsx` file. No other spreadsheet formats are supported yet
(`.xls`, `.csv`, Google Sheets exports — out of scope for Phase 1).

## Output

### `parse_spreadsheet(path) -> Iterator[Region]`

The streaming entry point. Opens the workbook and yields one `Region` per
non-empty cell, in sheet order then reading order (top-to-bottom,
left-to-right within each sheet). This is what a live-processing UI panel
would consume — each `yield` is one row landing in the panel.

### `build_artifact(path) -> dict`

Runs the full pipeline once and returns the complete common-schema artifact
(`artifact`, `kind`, `adapter`, `source`, `regions`, `checks`) as a plain
dict, ready to pass into `adapters.common.outputs`.

### Region types

| type            | meaning                                                              | confidence |
|------------------|-----------------------------------------------------------------------|------------|
| `title`          | anchor cell of a merged range spanning >1 cell (e.g. a banner title)  | 0.99 |
| `header`         | the first non-empty, non-title row in a sheet                        | 0.99 |
| `label`          | a text cell in a data row (row label, category name, etc.)           | 0.99 |
| `value`          | a literal number in a data row                                       | 0.99 |
| `formula-cell`   | any cell whose value starts with `=`                                 | 0.96 / 0.80 / 0.40 — see below |

Header/title detection is positional, not hardcoded to a specific row
number: the first merged multi-cell range becomes the title, and the first
ordinary row after it (or the first row of the sheet, if there's no merged
title) becomes the header. This generalizes across differently-shaped
sheets without per-workbook configuration.

### Confidence — formula cells

Formula confidence comes from `adapters/spreadsheet/formula.py`'s evaluator,
never from a hardcoded table lookup:

- **0.96** — the formula resolves entirely using cells in its own sheet
  (e.g. `=SUM(B3:D3)`).
- **0.80** — the formula resolves, but only by reaching into another sheet
  (e.g. `=SUM(E3:E5)+'Actuals'!E6`). It's correct, but a rename or deletion
  of the other sheet would silently break it, so it's worth a second look.
- **0.40** — the formula could not be resolved at all: the target sheet
  doesn't exist, a referenced cell in the range isn't numeric, or the
  formula uses a pattern the evaluator doesn't support. Flagged for review
  rather than guessed at.

`Region.text` for a formula cell is always `"<formula> → <value>"` (or
`"<formula> → unresolved (<reason>)"`) — the formula string itself is never
discarded or rewritten, only annotated with what it resolved to.

**Why an evaluator exists at all**: a workbook authored by `openpyxl`
(rather than saved by Excel/LibreOffice) has no cached formula results —
`load_workbook(path, data_only=True)` returns `None` for every formula
cell. Production `.xlsx` files from Excel usually do have a cache, but
relying on it is fragile (it goes stale the moment a cell changes without a
recalculation), so this adapter computes its own resolved value instead.
The evaluator is intentionally small — it handles `SUM(range)`, a
cross-sheet single-cell reference, and `SUM(range) + cross-sheet cell` — and
returns "unresolved" for anything else rather than guessing.

### Checks

Two artifact-level checks run over the whole workbook (not just one sheet),
per `Check{name, passed, detail}`:

- **`cross-sheet-references-resolve`** — for every formula containing a
  cross-sheet reference, does the target sheet actually exist in the
  workbook? Fails (workbook-wide) if even one doesn't.
- **`totals-match-referenced-cells`** — for every formula that is a plain,
  same-sheet `SUM(range)` over cells that are themselves literal numbers,
  independently sum those literals with a bare `sum()` (a separate code
  path from the evaluator) and compare to the evaluator's resolved value.
  Formulas that reach into other formulas or other sheets are out of scope
  for this check — they're already covered by confidence tiering and the
  cross-sheet check above.

A failed check is tracked independently of confidence — see CLAUDE.md's "A
failed validation check is not the same as low confidence" guardrail. A
region can be 0.96 confidence and still belong to a formula whose check
failed elsewhere in the same artifact.

## Four output shapes

`adapters/common/outputs.py` implements all four as pure functions over
`build_artifact()`'s return value — no separate extraction pass:

- `to_text` — regions flattened in reading order.
- `to_structured` — the region list itself.
- `to_json` — the full artifact dict (source + regions + checks).
- `to_table` — for `kind == "spreadsheet"`, one row per cell:
  `sheet, ref, formula, value, confidence`. `formula`/`value` are split back
  out of the region's `text` field (which the adapter always writes as
  either a plain literal or `"<formula> → <value>"`), not re-parsed from
  the source file.

## Baseline

`adapters/spreadsheet/baseline.py`'s `flatten_to_csv(path)` is the naive
comparison point: one CSV block per sheet via
`load_workbook(path, data_only=True)`. Because the test fixture has no
cached formula values, every formula cell in the baseline comes through
blank, and the cross-sheet relationship disappears entirely — the exact
failure mode CLAUDE.md's problem statement describes.

## Eval

`eval/run_eval.py` runs a deterministic fact-lookup eval: for each question
in `eval/qa_dataset.json`, it looks up the specific cell or region the
question is about (never scans the whole flattened blob — an unrelated
sheet name appearing elsewhere in the CSV must not give the baseline
undeserved credit) and checks whether the expected fact is present there.
No LLM call, no API key, fully reproducible offline:

```
python eval/run_eval.py
```

**Real, measured result on `tests/fixtures/budget_test.xlsx`** (6
questions, run 2026-09-04): baseline (flattened CSV) 0/6 = 0%, structured
(common schema JSON) 6/6 = 100%.

This split is real but stark, and the reason is specific to this fixture:
`generate_test_workbook.py` authors formulas via `openpyxl`, which never
computes them, so `flatten_to_csv`'s `data_only=True` read finds no cached
value and every formula cell comes through blank in the baseline — exactly
the failure mode CLAUDE.md's problem statement describes, not a rigged
comparison. All six questions target formula-dependent cells, which is why
the baseline scores exactly 0 rather than partially credit-able. A fixture
with more literal (non-formula) data would show baseline scoring above 0%
on the questions that don't depend on formulas.

This is intentionally a lower bar than full LLM comprehension — it measures
"is the fact recoverable from this representation at all," not "can a model
reason about it." `qa_dataset.json`'s `check` field (`cell_value`,
`cross_sheet_ref`, `unresolved_reason`) already names what each question
tests, so an LLM-based eval (ask a model the question, score its free-text
answer) can be layered in later without changing the dataset shape.

## Test fixture

`tests/fixtures/generate_test_workbook.py` builds
`tests/fixtures/budget_test.xlsx`: two sheets ("Q1 Plan", "Actuals"), each
with a merged title, a header row, three data rows with in-sheet `SUM`
formulas, and a totals row. `Q1 Plan!E6` reaches into `Actuals!E6`
(resolvable cross-sheet reference — 0.80 confidence). `Q1 Plan!F3`
deliberately references a sheet that doesn't exist (`'Forecast Q4'!B3` —
0.40 confidence, fails the cross-sheet check) so both the pass and fail
paths of that check are exercised, not just the happy path.

## Known limitations (Phase 1 scope, not bugs)

- The formula evaluator only understands `SUM(range)`, a bare cross-sheet
  cell reference, and `SUM(range) + cross-sheet cell`. Anything else (other
  functions, multi-range SUMs, arithmetic beyond a single `+`) resolves as
  "unresolved (unsupported formula pattern)" at 0.40 confidence rather than
  being evaluated incorrectly.
- Header/title detection is positional (first merged range = title, first
  plain row after it = header), not driven by cell styling. A sheet with no
  merged title and no obvious header row will misclassify its first row as
  the header.
- No LLM cleanup pass touches extracted values anywhere in this adapter —
  per CLAUDE.md's guardrail, normalization isn't performed at all in Phase 1,
  so there's nothing to log as a diff yet.
