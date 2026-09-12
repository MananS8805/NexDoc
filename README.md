# nexDoc

**nexDoc** is a semantic preprocessing pipeline for enterprise artifacts.
Enterprise information is rarely stored in forms AI systems can interpret
directly. Critical knowledge sits in spreadsheets, reports, forms, and
diagrams whose structure carries meaning — a formula's dependencies, a
scanned form's field labels, a flowchart's connections. Generic pipelines
flatten all of that to plain text and lose it. nexDoc reads each artifact
type in its own terms and converges every adapter's output onto the same
schema, so a downstream system integrates once, not once per file type.

Full design rationale and build order: [`CLAUDE.md`](CLAUDE.md).

## Current status

All three adapters are real, working, and tested against real fixtures.
None of this is a mock or a design intention — everything below is
verifiable by running the commands.

| Adapter | Status | Real, measured eval? |
|---|---|---|
| [Spreadsheet](adapters/spreadsheet/README.md) | Done, deterministic, no model calls | Yes, two evals — deterministic fact-lookup: 0% vs. 100%; real Groq LLM task-accuracy: 17% vs. 100% (see below) |
| [Document](adapters/document/README.md) | Done, hybrid text-layer + OCR | Yes — deterministic fact-lookup: 50% vs. 100% (see [`docs/eval-methodology.md`](docs/eval-methodology.md)) |
| [Diagram](adapters/diagram/README.md) | Done, OpenCV detection + reconstruction | Yes — deterministic fact-lookup: 0% vs. 100% (see [`docs/eval-methodology.md`](docs/eval-methodology.md)) |

Not built, and explicitly out of scope for this stage: auth/roles/reviewer
workflow, a live server, hosting, a database. Output is static files only.

**OCR engine: EasyOCR, not Tesseract.** The document and diagram adapters'
OCR path used to run through pytesseract/Tesseract, which had two real,
confirmed problems — installing Tesseract's system binary needs elevation
some environments don't have, and the old code never passed a language
argument, so non-English scripts (verified on this repo's own Hindi test
fixture) came back as garbage. Both are fixed by switching to EasyOCR
(pure Python + PyTorch, no system binary, configured for English + Hindi
by default, real per-detection confidence, fully offline at inference
time after a one-time model download). This does trade Tesseract's
near-instant CPU OCR for EasyOCR's much slower one — real, measured CPU
timings are in `adapters/document/README.md`. Each adapter still detects
and reports OCR availability honestly through the same
`ocr-engine-available` check as before, never crashing or fabricating
text. Details: [`adapters/document/README.md`](adapters/document/README.md#ocr-engine--easyocr-switched-from-pytesseracttesseract-real-limitations-stated-plainly).

## Quickstart

**Prerequisites**: Python 3.11+ (developed and verified on 3.13.2). No
external services or accounts are required for the core pipeline — only
the optional Groq-backed Q&A feature (below) needs an API key.

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt
```

**Known environment pitfall (Windows especially)**: if more than one
Python distribution is installed (e.g. an Anaconda install alongside a
standalone python.org install), `pip install` and `python` can silently
resolve to different environments than the one you activated. A broken or
mismatched `torch` install in the *wrong* one surfaces as EasyOCR failing
with `OSError: [WinError 1114] A dynamic link library (DLL) initialization
routine failed ... torch\lib\c10.dll` — this is an environment problem, not
a bug in this repo. Confirm which interpreter is actually active with
`python -c "import sys; print(sys.executable)"` before troubleshooting
further; shape/topology detection in the document and diagram adapters
still works even when OCR can't load, since neither depends on `torch`.

**Verify the install** by running the test suite (expect all passing;
EasyOCR's first run downloads its models, ~285MB, and takes noticeably
longer than subsequent runs):

```bash
python -m pytest tests/ -q
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

**Eval**: each of the three adapters has its own deterministic, measured
accuracy comparison, none needing an API key:

```bash
python eval/run_eval.py               # spreadsheet: 0% vs. 100%
python eval/run_document_eval.py      # document: 50% vs. 100%
python eval/run_diagram_eval.py       # diagram: 0% vs. 100%
```

These are fact-lookup evals, not LLM benchmarks — see
[`docs/eval-methodology.md`](docs/eval-methodology.md) for exactly what
each one measures and the real, verified reason document's split isn't
stark like the other two (a born-digital PDF's content mostly survives
flattening; a diagram's and a spreadsheet's formulas don't).

A separate, opt-in real task-accuracy eval also exists for spreadsheet —
`eval/run_real_eval.py` actually calls a Groq-hosted LLM and scores its
answers, and requires a real `GROQ_API_KEY` (copy `.env.example` to `.env`
first). Run once with a real key: baseline 1/6 (17%), structured 6/6
(100%) — see the same doc for what the distinction between the two kinds
of eval means, and one genuinely interesting real finding from that run
(the model partially reconstructing one blank formula result by summing
its visible inputs itself — something the deterministic eval can't see by
construction). Document and diagram don't have a task-accuracy eval yet.

## Sample files for a demo or review

Real, checked-in fixtures — not placeholders — one per artifact type (plus
two diagram variants, since the diagram adapter now covers two distinct
diagram grammars):

| File | Type | What it shows |
|---|---|---|
| [`tests/fixtures/budget_test.xlsx`](tests/fixtures/budget_test.xlsx) | Spreadsheet | Two sheets, merged-cell titles, in-sheet and cross-sheet formulas (including one deliberately broken reference), formula confidence tiering |
| [`tests/fixtures/invoice_born_digital.pdf`](tests/fixtures/invoice_born_digital.pdf) | Document | A born-digital invoice — title/field/line-item/total-field region classification, no OCR needed |
| [`tests/fixtures/flowchart_clean.png`](tests/fixtures/flowchart_clean.png) | Diagram (flowchart) | Rectangle/diamond node + arrow detection, direction inference, OCR'd labels |
| [`tests/fixtures/activity_diagram_clean.png`](tests/fixtures/activity_diagram_clean.png) | Diagram (UML activity) | Initial/final nodes, fork/join bars, rounded activity boxes, a swimlane divider |

Run any of them through the CLI (writes a self-contained `prototype.html`
you can open directly in a browser — see Quickstart above), or drag them
into the local web app (below) to see the same result interactively. A
scanned, OCR-dependent document is also available at
[`tests/fixtures/note_scanned.pdf`](tests/fixtures/note_scanned.pdf) if
you want to demo the OCR path specifically (includes a Hindi line, to show
non-English script support).

**Local web app**: the same pipeline, wrapped in a local-only interactive UI
so you can drag a file in instead of typing a command:

```bash
uvicorn webapp.main:app --reload
```

See [`webapp/README.md`](webapp/README.md) for what it does and doesn't add
(no accounts, no database, no hosting — see
[`docs/adr/0003-static-html-over-live-server.md`](docs/adr/0003-static-html-over-live-server.md)
for why that stays out of scope).

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
    ocr.py                  shared EasyOCR reader + availability probe (document + diagram)
  spreadsheet/adapter.py   .xlsx -> schema, formula-aware, no OCR, no model calls
  document/adapter.py      .pdf -> schema, hybrid text-layer / OCR
  diagram/adapter.py       .png/.jpg -> schema, OpenCV shape + line detection
eval/
  run_eval.py              deterministic fact-lookup eval — spreadsheet
  run_document_eval.py     deterministic fact-lookup eval — document
  run_diagram_eval.py      deterministic fact-lookup eval — diagram
  run_real_eval.py         real, Groq-backed task-accuracy eval — spreadsheet only (needs GROQ_API_KEY, opt-in)
qa/
  groq_qa.py               shared Groq Q&A call, used by run_real_eval.py and webapp's /api/ask
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
