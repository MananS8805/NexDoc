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
| `ocr-line`                | one visual line of OCR'd text, grouped from EasyOCR's individual text-box detections by `adapters.common.ocr.group_detections_into_lines` | mean of that line's per-detection EasyOCR confidences |
| `scanned-page-unprocessed`| the page needed OCR but the OCR engine wasn't available on this machine | 0.0 |

`ocr-line` confidence is the only place per-region confidence actually
varies within a document, because it's a real signal that varies per line —
never a hardcoded number. `0.0` for an unprocessed page is not "very
unconfident text" — it means no text was extracted at all, honestly.

## OCR engine — EasyOCR (switched from pytesseract/Tesseract), real limitations stated plainly

This adapter used to run OCR through **pytesseract / Tesseract**. It now
uses **EasyOCR** (pure Python + PyTorch, Apache-2.0) instead, via the
shared wrapper in `adapters/common/ocr.py`. This was a real bug fix, not a
style preference — two concrete, confirmed problems with the old path:

1. **Tesseract is a system binary, not a Python package.** `pytesseract`
   is just a thin wrapper around it; installing the actual OCR engine
   requires an OS-level install, which needs admin/elevation on a machine
   that doesn't have it already — a hard, unresolvable blocker in some
   environments, unrelated to anything about the adapter's own code.
   EasyOCR is `pip install easyocr`, full stop — no system binary, no
   elevation.
2. **The old OCR call site never passed a language.** `pytesseract.image_to_data`
   was called with no `lang=` argument anywhere, so every scanned page was
   OCR'd with whatever Tesseract's default language pack was (English)
   regardless of the page's actual script — and most environments (this
   one included) only ever have the `eng` Tesseract pack installed, so a
   non-Latin script would have produced garbage even if Tesseract itself
   *could* read it in principle, given the right pack. This was verified,
   not theorized: `tests/fixtures/note_scanned.pdf` has a Devanagari
   (Hindi) line specifically to exercise this, and it came back as
   `Ae Ot od @ se YT ENT` under the old code path. With EasyOCR's `Reader`
   built for `["en", "hi"]` (see `DEFAULT_LANGUAGES` in
   `adapters/common/ocr.py`), the same fixture, same line, now comes back
   as `माल की जांच के बाद भुगतान होगा|` — real, readable Devanagari (one
   character off: the sentence-final `।` danda was read as a `|` pipe,
   which is a minor per-character confusion, not a script failure).

**Tesseract was removed rather than kept as a fallback.** Two OCR code
paths — two "is it installed" checks, two confidence scales, two sets of
per-language-pack quirks — is real, ongoing complexity for marginal
benefit here: Tesseract's accuracy and script coverage were already the
weaker of the two, so nothing correctness-relevant is lost, and EasyOCR's
only real downsides (a much larger dependency, slower CPU inference — see
below) apply either way once `easyocr`/`torch` are installed at all.

Real, honestly-stated limitations of the new engine:

- **`torch` is a large dependency.** `pip install easyocr` pulls in
  PyTorch and (on Linux) its CUDA wheels even though this adapter only
  ever runs on CPU (`gpu=False`) — expect a multi-hundred-MB to ~1GB+
  install, materially bigger than pytesseract's footprint. Inference
  itself is fully offline at runtime either way; the size cost is at
  install time, not per-request network use.
- **First-run model download.** The first time a given language
  combination (`en`+`hi` by default) is used on a machine, EasyOCR
  downloads its detector (~85MB, `craft_mlt_25k.pth`) and a
  language-appropriate recognition model (~215MB for the Devanagari
  model, which also covers Latin) to `~/.EasyOCR/model/`, cached
  thereafter (~285MB total observed for the default language pair). That
  download took about 50 seconds in this environment; a slower connection
  or a machine with no cached copy of an already-downloaded language
  combination will pay it again. There is no bundled offline installer —
  a machine with zero network access at all cannot complete this one-time
  step, which is a real (if narrower) constraint compared to shipping
  model weights alongside the package.
- **CPU inference is slow, measured directly on this environment (no
  GPU)**: reading a full `note_scanned.pdf` page (1654×2339px, rendered at
  200dpi) took **~49 seconds**; a smaller flowchart-sized image
  (900×700px, `tests/fixtures/flowchart_clean.png`) took **~5.6 seconds**;
  loading the `Reader` itself (after models are already cached) took
  **~5-6 seconds** per process. This is meaningfully slower than
  Tesseract's near-instant CPU OCR, and is the real cost of a
  neural-network-based OCR engine without a GPU. `adapters/common/ocr.py`
  caches the `Reader` per-process and only builds/probes it lazily (the
  first time a page or label actually needs OCR) so a document with no
  scanned pages, or a diagram with no nodes/edges, never pays this cost at
  all — see its `_iter_regions` for both adapters.
- **Only the languages actually loaded get read correctly.** The default
  `Reader` is built for `["en", "hi"]` — English because it's the lingua
  franca of scanned business documents, Hindi/Devanagari because that's
  the second script this repo's own fixtures exercise. A page in a third
  script (e.g. Chinese, Arabic) will still get *a* result — EasyOCR
  doesn't crash on out-of-vocabulary characters — but it will be
  misrecognized the same structural way the old English-only Tesseract
  call misread Hindi, just with a different (and larger — EasyOCR
  supports 80+ languages) escape hatch: pass a different `languages` list
  to `get_reader`/`ocr_engine_status` for a different script mix. This
  isn't wired to a config file or environment variable yet.
- Neither engine has real layout understanding — EasyOCR's *detector*
  finds individual text boxes with no notion of column layout or table
  structure; `group_detections_into_lines` (a real geometric
  vertical-overlap clustering, not a hardcoded line count) recovers
  Tesseract-style "lines" from those boxes for `ocr-line` regions, but
  this is still simple top-to-bottom/left-to-right geometry, not real
  layout analysis.
- Accuracy on handwriting or very degraded scans is still not claimed to
  match a production layout-aware VLM (e.g. PaddleOCR-VL, Marker) — that
  remains out of scope for this prototype, same as it was with Tesseract.

None of this is implied to be production-grade elsewhere in this repo — the
UI prototype's "why it's better than flattening" section only ever shows a
*measured* number for the spreadsheet adapter, and stays illustrative for
documents (see `adapters/spreadsheet/README.md`'s eval section and
`docs/eval-methodology.md`).

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
- **`ocr-engine-available`** — records whether EasyOCR was actually usable
  on this machine when the artifact was built, and how many scanned pages
  (if any) were affected. This is not a property of the PDF — it's a
  property of the environment, tracked the same way a failed validation
  check is tracked independently of confidence (CLAUDE.md's guardrail): a
  document can have all its own checks pass and still have this one fail
  if OCR wasn't available. For a document with no scanned pages at all,
  this check passes with `"not checked"` rather than actually loading
  EasyOCR's model weights — see `adapters/common/ocr.py` and
  `_iter_regions`'s lazy-probe comment for why forcing that load would be
  pure waste for a document that never needed OCR in the first place.

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
  handwriting-specific handling beyond whatever EasyOCR itself does.
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
