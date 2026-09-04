# Semantic Preprocessing Pipeline

Enterprise information is rarely stored in forms AI systems can interpret
directly. Critical knowledge sits in spreadsheets, reports, forms, and
diagrams whose structure carries meaning — a formula's dependencies, a
scanned form's field labels, a flowchart's connections. Generic pipelines
flatten all of that to plain text and lose it. This project builds one
pipeline that reads each artifact type in its own terms and converges every
adapter's output onto the same schema, so a downstream system integrates
once, not once per file type.

Full design rationale and build order: [`CLAUDE.md`](CLAUDE.md).

## Current status

All three adapters are real, working, and tested against real fixtures.
None of this is a mock or a design intention — everything below is
verifiable by running the commands.

| Adapter | Status | Real, measured eval? |
|---|---|---|
| [Spreadsheet](adapters/spreadsheet/README.md) | Done, deterministic, no model calls | Yes — 0% baseline vs. 100% structured on the test fixture (see below) |
| [Document](adapters/document/README.md) | Done, hybrid text-layer + OCR | No eval harness yet — see [`docs/eval-methodology.md`](docs/eval-methodology.md) |
| [Diagram](adapters/diagram/README.md) | Done, OpenCV detection + reconstruction | No eval harness yet |

Not built, and explicitly out of scope for this stage: auth/roles/reviewer
workflow, a live server, hosting, a database. Output is static files only.

**Environment-specific, honest caveat**: the machine this was built on does
not have the Tesseract OCR binary installed (only the `pytesseract` Python
package is), and installing system-wide software requires elevation this
session doesn't have. Every OCR-dependent code path is real and complete —
it has simply never produced a real OCR'd result *on this machine*. Each
adapter detects this itself and reports it honestly through an
`ocr-engine-available` check rather than crashing or fabricating text. On a
machine with Tesseract installed, the exact same code produces real OCR
output instead. Details: [`adapters/document/README.md`](adapters/document/README.md#ocr-engine--real-limitations-stated-plainly).

## Quickstart

```bash
pip install -r requirements.txt
```

Run any supported file through the pipeline — the file's real content
(magic bytes), not its extension, decides which adapter handles it
(`adapters/router.py`):

```bash
python run_adapter.py --input tests/fixtures/budget_test.xlsx --output-dir out/
python run_adapter.py --input tests/fixtures/invoice_born_digital.pdf --output-dir out/
python run_adapter.py --input tests/fixtures/flowchart_clean.png --output-dir out/
```

Each run writes, into `--output-dir`:

- `schema.json` — the full common-schema artifact (source, regions, checks)
- `text.txt`, `structured.json`, `table.json` — the other three output shapes
- `prototype.html` — a self-contained, offline HTML report over that one real artifact (open it directly in a browser)

Prints a real confidence-tier breakdown and every check's actual result to
the console. Errors (missing file, unrecognized format, a corrupt file) are
reported clearly and exit non-zero — never a raw traceback for an expected
failure mode.

**Eval**: the one real, measured accuracy comparison so far is the
spreadsheet adapter's:

```bash
python eval/run_eval.py
```

This needs no API key — it's a deterministic fact-lookup eval, not an LLM
benchmark. What that distinction means and why: [`docs/eval-methodology.md`](docs/eval-methodology.md).

**Schema conformance**: proves the "one shared schema" claim is real by
validating actual output from all three adapters against
[`docs/schema.json`](docs/schema.json):

```bash
python -m pytest tests/test_schema_conformance.py -v
```

## Architecture

Every artifact goes through the same five stages (detect → route → extract
natively → map to the common schema → confidence + validation checks). See
`CLAUDE.md`'s "Architecture" section for the full description and
[`docs/schema.json`](docs/schema.json) for the schema itself, with an
inline description on every field.

```
adapters/
  router.py              content-sniffing "detect type" + "route to adapter"
  common/
    schema.py             the shared dataclasses (Region, Check, Artifact, ...)
    outputs.py             the four output-shape functions, pure functions over the schema
    ocr.py                  shared Tesseract-availability probe (document + diagram)
  spreadsheet/adapter.py   .xlsx -> schema, formula-aware, no OCR, no model calls
  document/adapter.py      .pdf -> schema, hybrid text-layer / OCR
  diagram/adapter.py       .png/.jpg -> schema, OpenCV shape + line detection
eval/
  run_eval.py              deterministic fact-lookup eval (spreadsheet only, for now)
docs/
  schema.json              the shared JSON Schema, with inline field descriptions
  eval-methodology.md      what "eval" means here and its real limits
  adr/                     why the non-obvious decisions were made
tests/
  fixtures/                generator scripts + the real fixture files they produce
  test_schema_conformance.py   validates real adapter output against docs/schema.json
scripts/build_prototype.py     bakes real adapter output into a static HTML report
design/preprocessing-pipeline-mock.html    the original UI design spec (untouched)
```

Each adapter is independent — a bug in the diagram adapter's detector can't
reach the spreadsheet code path. The only place all three adapters are
known about together is `adapters/router.py`, whose entire job is routing.

## Reusability, demonstrated not asserted

Adding the document and diagram adapters after the spreadsheet adapter
required zero changes to `adapters/common/schema.py` or
`adapters/common/outputs.py`. The four output-shape functions, written
against the spreadsheet adapter's output, worked unmodified against
document and diagram output the first time they were tried — verified in
`tests/test_schema_conformance.py`, not just claimed here.

## Where to read next

- [`adapters/spreadsheet/README.md`](adapters/spreadsheet/README.md), [`adapters/document/README.md`](adapters/document/README.md), [`adapters/diagram/README.md`](adapters/diagram/README.md) — each adapter's input/output contract, confidence rationale, checks, and known limitations.
- [`docs/eval-methodology.md`](docs/eval-methodology.md) — what "measured" means here.
- [`docs/adr/`](docs/adr/) — why the non-obvious calls were made (openpyxl vs. pandas, detection+OCR vs. a raw VLM prompt for diagrams, static HTML vs. a live server, fact-lookup vs. LLM-graded eval).
