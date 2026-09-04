# Document adapter

Phase 2 of the semantic preprocessing pipeline (see `/CLAUDE.md`). Converts a
`.pdf` into the common node/edge schema using a hybrid extraction path:
born-digital pages are read directly from their embedded text layer;
scanned/image-only pages go through OCR. No LLM is involved anywhere in
this adapter.

## Input

A path to a `.pdf` file. No other document formats are supported yet
(`.docx`, `.png`/`.jpg` as a standalone image, multi-file batches — out of
scope for Phase 2).

## Output

### `parse_document(path) -> Iterator[Region]`

The streaming entry point, matching `parse_spreadsheet`'s shape. Opens the
PDF and yields one `Region` per text block (born-digital pages) or OCR'd
line (scanned pages), in page order then reading order.

### `build_artifact(path) -> dict`

Runs the full pipeline once and returns the complete common-schema artifact,
ready to pass into `adapters.common.outputs` — the exact same four
output-shape functions used for spreadsheets, unmodified.

## How a page is routed — the real signal

Each page is checked with `page.get_text().strip()` (PyMuPDF). If that
returns at least `MIN_TEXT_LAYER_CHARS` (20) characters, the page has a real
embedded text layer and is read directly — no OCR, no model call. Otherwise
it's treated as scanned/image-only and goes to OCR. This is a measured
property of the page, not a guess based on the file extension or a
per-file flag.

## Region types — born-digital pages

| type            | meaning                                                        | confidence |
|-----------------|-----------------------------------------------------------------|------------|
| `title`         | the first text block of the whole document                      | 0.98 |
| `field`         | a free-text line (name, date, address, etc.)                    | 0.98 |
| `table-header`  | a line containing both "description" and "qty" (case-insensitive) | 0.98 |
| `line-item`     | a line matching `<description>  <qty>  <price> <cur>  <total> <cur>` | 0.98 |
| `total-field`   | a line matching `...total: <amount> <currency>`                 | 0.98 |

Every born-digital block gets the same 0.98 — the real signal being
represented is binary ("this page has an embedded text layer"), so there is
exactly one honest value to assign, the same way the spreadsheet adapter's
literal cells all get a flat 0.99. It's not 0.99 because PDF text-layer
reading order can still be ambiguous in ways a stored cell value never is.

**Known, deliberately narrow scope**: `line-item`/`total-field` detection is
a regex against this exact column layout (`Description   Qty   Unit price
Line total`), not real table-structure detection. It works because the test
fixture was built in a predictable, regex-friendly format — the same way
the spreadsheet fixture's formulas were built to exercise both confidence
tiers deliberately. A real invoice with a different column layout, wrapped
cells, or a scanned table would not be picked up by this regex; a
production version would need actual table detection, not a pattern match.
This is stated here rather than implied to work generally.

## Region types — scanned pages

| type                      | meaning                                                    | confidence |
|---------------------------|-------------------------------------------------------------|------------|
| `ocr-line`                | one line of OCR'd text, grouped by Tesseract's own block/paragraph/line numbering | mean of that line's word-level OCR confidences ÷ 100 |
| `scanned-page-unprocessed`| the page needed OCR but Tesseract wasn't available on this machine | 0.0 |

`ocr-line` confidence is the only place per-region confidence actually
varies within a document, because it's a real signal that varies per line —
never a hardcoded number. `0.0` for an unprocessed page is not "very
unconfident text" — it means no text was extracted at all, honestly.

## OCR engine — real limitations, stated plainly

This adapter uses **pytesseract / Tesseract**, a real, free, locally
installable OCR engine — not a production-grade layout-aware VLM (e.g.
PaddleOCR-VL, Marker). That's a deliberate choice for a prototype, and it
comes with real accuracy limits worth being explicit about:

- Tesseract has no layout understanding — it reads left-to-right,
  top-to-bottom per its own line grouping, with no notion of multi-column
  layout, tables-as-structure, or reading order beyond simple geometry.
- Accuracy on handwriting, low-resolution scans, or unusual fonts is
  meaningfully worse than a modern VLM-based OCR pipeline.
- Language support depends on which Tesseract language packs are installed
  (`tessdata`); a language pack that isn't installed will produce garbled
  or empty output for that script, not a clear error, unless separately
  checked for.

None of this is implied to be production-grade elsewhere in this repo — the
UI prototype's "why it's better than flattening" section only ever shows a
*measured* number for the spreadsheet adapter, and stays illustrative for
documents (see `adapters/spreadsheet/README.md`'s eval section and
`docs/eval-methodology.md`).

**Environment-specific fact, not an adapter limitation**: the machine this
adapter was built and tested on does not have the Tesseract binary
installed, and installing system-wide software requires elevation this
session doesn't have (a `choco install` attempt failed on a permissions
lock; downloading and running a portable installer `.exe` from the web is
explicitly out of bounds regardless of authorization). `pytesseract` is
installed and the OCR code path is real and complete — it has simply never
been exercised end-to-end with an actual OCR result on this machine. The
adapter detects this itself (`_tesseract_status()`, checked once per
process via `pytesseract.get_tesseract_version()`) and reports it honestly
through the `ocr-engine-available` check rather than crashing or
fabricating OCR text. On a machine with Tesseract installed, the exact same
code produces real `ocr-line` regions with real per-line confidence instead
of the `scanned-page-unprocessed` fallback — nothing else about the adapter
needs to change.

## Checks

Three artifact-level checks, per `Check{name, passed, detail}`:

- **`total-matches-line-items`** — for every `total-field` region, sum the
  `line-item` regions it's linked to (via a `sums-line-items` relationship,
  set when the total is parsed) and compare to the total's own extracted
  value. Passes trivially with an explicit reason when the document has no
  total field at all.
- **`date-fields-parse`** — every `DD/MM/YYYY`-shaped substring found in any
  region's text is parsed with `datetime.strptime`; fails if any date-like
  string doesn't parse as a real calendar date. Passes trivially when no
  date-like text is found.
- **`ocr-engine-available`** — records whether Tesseract was actually
  reachable on this machine when the artifact was built, and how many
  scanned pages (if any) were affected. This is not a property of the PDF —
  it's a property of the environment, tracked the same way a failed
  validation check is tracked independently of confidence (CLAUDE.md's
  guardrail): a document can have all its own checks pass and still have
  this one fail if OCR wasn't available.

## Known limitations (Phase 2 scope, not bugs)

- Line-item / total-field detection is a regex over one specific column
  layout — see "Region types — born-digital pages" above.
- Language detection (`langdetect`, a real statistical n-gram model) is
  skipped entirely for structured/tabular region types (`line-item`,
  `total-field`, `table-header`) — tagging the "language" of a data row is
  a category error, not a hard case to get right. For genuine free-text
  fields, `langdetect` is still unreliable on short strings even after
  stripping numbers and punctuation (e.g. "Facture N° 2024-A103 — Date:
  14/08/2024" can still misclassify) — this is a real, known weakness of
  short-string statistical language ID, not something this adapter works
  around by guessing.
- No table-structure detection, no multi-column layout handling, no
  handwriting-specific handling beyond whatever Tesseract itself does.
- No LLM cleanup pass touches extracted text anywhere in this adapter, per
  CLAUDE.md's guardrail.

## Test fixtures

`tests/fixtures/generate_document_fixtures.py` builds:

- **`invoice_born_digital.pdf`** — a real born-digital PDF (reportlab), no
  images, French + English text, two line items and a grand total in a
  predictable column format. Verified: `page.get_text()` on this file
  returns the actual accented text correctly (`Lumière`, `N°`, `Coton
  peigné`) — an earlier version of this fixture appeared to mangle those
  characters, which turned out to be a Windows console (`cp1252`) print
  artifact, not a real extraction bug; confirmed by writing the extracted
  text to a UTF-8 file instead of printing it.
- **`note_scanned.pdf`** — a genuinely image-only PDF: text is rendered onto
  a Pillow raster image (English + Hindi, via Windows' bundled
  `Nirmala.ttc` for Devanagari) and that image is the entire page — no text
  object exists in the PDF at all. Confirmed via `page.get_text()` (0
  characters) and `page.get_images()` (1 image).
