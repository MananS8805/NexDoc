# ADR 0001: openpyxl over pandas for the spreadsheet adapter

## Status

Accepted, implemented in [`adapters/spreadsheet/adapter.py`](../../adapters/spreadsheet/adapter.py).

## Context

CLAUDE.md's problem statement is specifically about *not* losing structure
that generic tooling flattens away — for a spreadsheet, that means formulas,
cell/sheet references, and merged-cell layout. `pandas.read_excel` is the
obvious first reach for "read a spreadsheet in Python," and it's a fine
choice if the goal is a dataframe of values.

## Decision

Use `openpyxl` directly, not `pandas`.

`pandas.read_excel` reads *resolved values*, not formulas — by the time a
cell reaches a dataframe, `=SUM(B3:D3)` is already gone, replaced by
whatever value was cached (or `NaN` if there wasn't one). That's exactly
the "flatten first, structure second" failure mode this whole project
exists to avoid. `openpyxl`, used with `data_only=False`, gives direct
access to the formula string, the cell's raw type, and the workbook's
merged-cell ranges — everything the adapter needs to keep formulas as
formulas rather than pre-flattening them before extraction even starts.

## Consequences

- Formula *evaluation* becomes the adapter's own problem, not something a
  library does for free: `adapters/spreadsheet/formula.py`'s small
  evaluator exists specifically because `openpyxl` never computes
  formulas, and `data_only=True` returns `None` for a formula cell that was
  never opened and recalculated by Excel/LibreOffice (verified directly —
  see that README's "Why an evaluator exists at all"). This is a real cost:
  a production version would need a much more complete formula engine.
- In exchange, formula strings, cell/sheet references, and merge structure
  are all real, first-class data the adapter can reason about — which is
  the entire point.
- `pandas` may still be the right tool for a *table export* consumer of
  this adapter's `to_table()` output downstream — that's a different
  concern from how the source file itself is read, and isn't affected by
  this decision.
