# nexDoc — Semantic Preprocessing Pipeline

## Problem statement

Enterprise information is rarely stored in forms AI systems can interpret directly.
Critical knowledge is embedded in spreadsheets, reports, forms, diagrams, and
domain-specific artifacts whose structure carries semantic meaning. Generic
pipelines reduce these to plain text, losing relationships and context.

Build reusable preprocessing pipelines that preserve domain semantics while
transforming heterogeneous data into structured, AI-ready representations.

**Expected solution:** a working preprocessing pipeline with semantic
transformation, structured representation generation, measurable downstream
improvement, reusability across similar datasets, documentation, and sample
outputs.

## Current status (be honest about this in every session — this section is
kept up to date; if it looks stale, check `git log` / the actual files
before trusting it)

- Architecture, schema, and UX are designed and validated against a UI mock
  at `/design/preprocessing-pipeline-mock.html`. Read that file before writing
  any backend code — it is the spec, not just a picture.
- **Phases 1-4 are implemented and real.** All three adapters
  (spreadsheet, document, diagram) run against real fixtures, converge on
  the one common schema, and are validated by
  `tests/test_schema_conformance.py`. A real content-sniffing router
  (`adapters/router.py`) and CLI (`run_adapter.py`) route any of the three
  file types to the correct adapter automatically. See `README.md` for the
  full picture and quickstart.
- **All three adapters now have a real, deterministic fact-lookup eval.**
  `eval/run_eval.py` (spreadsheet, `tests/fixtures/budget_test.xlsx`):
  baseline 0/6, structured 6/6. `eval/run_document_eval.py` (document,
  `invoice_born_digital.pdf`): baseline 3/6 (50%), structured 6/6.
  `eval/run_diagram_eval.py` (diagram, `flowchart_clean.png`): baseline
  0/6, structured 6/6. None require an API key or model call. The document
  and diagram numbers are deliberately not stark 0%s the way spreadsheet's
  is — see `docs/eval-methodology.md` for the real, verified reason each
  one is what it is (a born-digital PDF's content mostly survives
  flattening; a diagram's doesn't, because whole-image OCR measurably
  merges/splits labels across shapes). The UI's comparison bars for
  document/diagram are no longer illustrative placeholders — they show
  these real numbers.
- **The spreadsheet adapter additionally has a real, model-backed
  task-accuracy eval.** `eval/run_real_eval.py` calls a real Groq-hosted
  LLM (`qa/groq_qa.py`) with the baseline and structured representations
  and scores its free-text answers against `qa_dataset.json`. Requires
  `GROQ_API_KEY` (see `.env.example`) and is opt-in (a separate script;
  `run_eval.py` stays the fast/free/keyless default). **A real key has
  since been supplied and this has actually been run**: baseline 1/6
  (17%), structured 6/6 (100%) — see `docs/eval-methodology.md` for the
  real result and one genuinely interesting finding from that run (the
  model partially self-reconstructing one blank-formula answer by summing
  visible inputs itself — inconsistently, not "always when possible").
  Document and diagram don't have a task-accuracy eval yet — a named,
  not-yet-built next step, not an oversight. Never present a fabricated or
  estimated number as measured.
- **The web app (`webapp/`) streams.** `POST /api/analyze/stream` emits NDJSON
  events as each adapter yields regions (the CLAUDE.md "live processing
  pattern"); the UI shows the real file beside the live stream with linked
  highlighting, a semantic graph of the relationships (Mermaid export), and an
  X-ray of what the flattened baselines lose. `webapp/engine.py` warms the OCR
  model at startup and caches results by content hash. Speed claims are
  A/B-measured (scan OCR at 150dpi: 1.55× faster, identical text); several
  other ideas were measured and rejected — see `webapp/README.md`. Scanned
  pages remain the slow path (~15–35s/page on CPU).
- The web app (`webapp/`) has a real "Ask a question" box wired to
  `qa/groq_qa.py` via `POST /api/ask` — same honest-when-unconfigured
  pattern as the eval script (`GET /api/ask/status` reports whether
  `GROQ_API_KEY` is set before the box is even usable).
- **Not built, and out of scope for now**: auth/roles/reviewer workflow
  (Phase 5), a live server or full API beyond the local `webapp/` (Phase
  4's UI wiring describes one; the CLI's own output stays static HTML per
  run — see `docs/adr/0003-static-html-over-live-server.md`).
- **OCR engine switched from pytesseract/Tesseract to EasyOCR.** The old
  Tesseract-based path had two real, confirmed problems: Tesseract's
  system binary needs an OS-level install that requires elevation some
  environments don't have, and the OCR call site never passed a `lang=`
  argument, so every scanned page was OCR'd with English-only weights
  regardless of script — verified directly on `tests/fixtures/
  note_scanned.pdf`'s deliberately-included Devanagari line, which came
  back as garbage (`Ae Ot od @ se YT ENT`) under the old code. EasyOCR
  (pure Python + PyTorch, no system binary, configured for `["en", "hi"]`
  by default) fixes both — the same fixture's Hindi line now comes back as
  real, readable Devanagari. See `adapters/common/ocr.py`'s module
  docstring and `adapters/document/README.md`'s "OCR engine — EasyOCR"
  section for the full reasoning, real CPU-inference timings measured in
  this environment, and the honest new limitations that come with a
  neural OCR engine instead of Tesseract (a much larger dependency,
  slower CPU inference, a one-time model download). Both the document and
  diagram adapters detect and report OCR availability honestly via the
  same `ocr-engine-available` check as before — the check's semantics and
  name are unchanged, only which engine it describes.

## Architecture — the adapter pattern

Every artifact goes through the same five stages regardless of type:

1. **Detect artifact type** — file extension / content sniffing, routes to an adapter.
2. **Route to adapter** — one adapter per artifact type (document, spreadsheet, diagram, ...).
3. **Native structure extraction** — each adapter reads the artifact in its own
   terms. Never flatten to text as the first step. A spreadsheet's formulas
   stay formulas; a diagram's connections stay connections.
4. **Map to common schema** — every adapter's output converges on the same
   node/edge JSON shape (below), so downstream consumers only integrate once.
5. **Confidence QA & validate** — every node/edge gets its own confidence
   score, plus artifact-level sanity checks (see "Validation" below). Anything
   below threshold, or that fails a sanity check, is flagged for review.

Reusability = adding a new artifact type means writing one new adapter that
targets the common schema. It does not mean touching the schema, the QA layer,
or downstream consumers.

## Common schema

```json
{
  "artifact": "string — human-readable title",
  "kind": "document | spreadsheet | diagram | <new type>",
  "adapter": "string — which adapter produced this, e.g. 'Spreadsheet adapter'",
  "source": { "path": "string", "hash": "sha256:...", "adapter_version": "string" },
  "regions": [
    {
      "id": "string, unique within artifact",
      "type": "string — semantic role (header, formula-cell, node, edge, ...)",
      "shape": "string | null — visual/structural hint (node, node-decision, edge, ...)",
      "language": "string | null — BCP-47 code, null when not applicable",
      "confidence": "number 0-1 — reflects real extraction certainty, never hardcoded",
      "text": "string | null — human-readable content",
      "relationships": [
        { "type": "formula-ref | connects-to | cross-sheet-ref | ...", "target": "region id" }
      ]
    }
  ],
  "checks": [
    { "name": "string", "passed": "boolean", "detail": "string" }
  ]
}
```

Confidence must mean something specific per adapter — e.g. for spreadsheets,
a formula resolving entirely within the same sheet is high confidence; a
formula referencing another sheet or an unresolved external file is flagged.
Do not assign a confidence score that isn't backed by an actual signal from
the parser.

## Output shapes (four views over one schema)

The same `regions` array reshapes into four outputs — implement all four as
pure functions over the schema, not as separate extraction passes:

- **text** — regions flattened in reading order, for search indexing.
- **structured** — the region list itself, typed and confidence-scored, for
  data entry automation.
- **json** — the full schema above including `source` and `checks`, for
  compliance/audit review.
- **table** — reshapes per `kind`: line items for documents-with-tables,
  cell reference/formula/value rows for spreadsheets, an edge list
  (from/to/label/confidence) for diagrams. Empty state ("no tabular data")
  when there's nothing to show — never fabricate rows to fill the view.

## Live processing pattern (for when the UI is wired to real data)

The mock's side panel is the target UX, not decoration — implement the
backend so this is achievable, not just visually simulated:

- As each region finishes processing, it should be emittable as an event
  (e.g. a generator/streaming response), not just available after the whole
  artifact completes. The UI panel streams rows in reading order as they land.
- Each event carries the adapter name ("routed to: Spreadsheet adapter"),
  the region's type/confidence, and enough info to render a table row without
  a second lookup.
- Design the adapter functions as generators/iterators from day one
  (`yield region` per region) so the streaming UI isn't a later retrofit.

## Validation checks (beyond per-region confidence)

Borrowed from how Tally Ira's Docs by Ira handles this for invoices — a
second, cheaper signal alongside model confidence: artifact-level sanity
checks that catch internally inconsistent extractions even when every
individual region looked confident.

- Spreadsheets: does a totals formula's computed value match the sum of the
  cells it claims to reference? Do cross-sheet references resolve to a sheet
  that actually exists in the workbook?
- Documents: does an extracted total match the sum of extracted line items?
  Does an extracted date parse as a valid date?
  Diagrams: does every edge reference two node ids that actually exist in
  the region list?

Store check results in the `checks` array on the schema. A failed check
should escalate to review the same way a low-confidence region does, even if
every individual region cleared the confidence threshold.

## Build order (do not skip ahead)

**Phase 1 — Spreadsheet adapter (start here).**
Deterministic, no model calls, fastest path to something real.
- Parse `.xlsx` with `openpyxl` (keep formulas as formulas — do not evaluate
  them away; capture the formula string, the resolved value if cached, and
  cell/sheet references).
- Preserve merged cells, header rows, and cross-sheet references explicitly.
- Output the common schema JSON above, including `checks`.
- Implement all four output shapes as functions over that JSON.
- Build a naive baseline in parallel: flatten the same file to CSV/plain text.
- Build a small eval: a handful of factual questions about the sheet (e.g.
  "what does cell E5 depend on", "what's the Q1 total") answered by an LLM
  given (a) the flattened baseline and (b) the structured JSON. Score
  accuracy. This produces the first *real* number for the comparison the UI
  mock currently shows as illustrative.
- Document the adapter's interface (input → output contract) so Phase 2 can
  follow the same pattern without guessing.

**Phase 2 — Document adapter.**
Reuse the hybrid OCR architecture already discussed: born-digital PDFs skip
OCR via direct text-layer extraction; scanned/complex pages route through a
layout-aware OCR model. Region-level confidence, script-aware language
tagging, no LLM cleanup pass on extracted values.

**Phase 3 — Diagram adapter.**
Hybrid detection + OCR, not a raw VLM prompt for the whole image — image-only
VLM prompting is known to fail badly on connection topology specifically
(this was measured in prior research, not a guess). Shape/arrow detection
handles topology; OCR handles labels; combine into the node/edge schema.

**Phase 4 — Wire the UI.**
Only after at least Phase 1 is real: replace the hardcoded `DOCS` array in
`/design/preprocessing-pipeline-mock.html` with actual adapter output over a
real API, implement the streaming live-panel behavior for real, and replace
the illustrative comparison bars with real eval results.

**Phase 5 — Auth & roles.**
SSO/OIDC (don't build custom login). Roles: uploader, reviewer (works the
confidence/check-failure queue), admin (tunes thresholds), read-only
(search/analytics consumers). Separate human logins from scoped API keys for
machine consumers. Every reviewer action (approve/correct/reject) is logged
with identity, timestamp, and reason — this is what makes the audit/
compliance story real, not just the confidence score.

**Phase 6 — Documentation & packaging.**
Adapter interface docs, schema docs, example input/output pairs per artifact
type, and a written eval methodology + results — these are named deliverables
in the problem statement, not optional polish.

## Guardrails

- No LLM "cleanup" pass that silently rewrites extracted values — especially
  numbers, formulas, and names. If normalization is needed, log it as a diff,
  never overwrite silently.
- Every output carries provenance: source file, adapter + version, and a
  confidence score per region.
- Don't claim "better than flattening" without the eval harness backing it.
- Keep adapters independent — a bug in the diagram adapter must not touch
  the spreadsheet adapter's code path.
- A failed validation check is not the same as low confidence — track both,
  don't collapse one into the other.

## Theme (for Phase 4)

```css
--paper:#EEF0EA;      --surface:#FBFBF8;
--ink:#16241F;         --ink-soft:#4B5850;   --ink-faint:#8B9691;
--accent:#1E8A72;      --accent-soft:#DCEEE8; --accent-ink:#0E4A3D;  /* verified / high-confidence */
--flag:#C1592E;        --flag-soft:#F4E1D6;   --flag-ink:#7A3418;    /* low-confidence / needs review */
--line:#D9DAD0;         --code-bg:#142119;     --code-text:#DCE8DD;

font-display: 'Space Grotesk' (headings, 500/600)
font-body:    'IBM Plex Sans' (body copy, 400/500)
font-mono:    'IBM Plex Mono' (data, badges, JSON/code panels only)

radius: 6px small / 10px cards / 999px pills (badges only)
spacing scale: 4/8/12/16/24/32/48/64/96px
```

## Design reference

- `/design/preprocessing-pipeline-mock.html` — the target UX and interaction
  pattern: artifact picker, input preview, live streaming panel, four output
  tabs. Treat the hardcoded `DOCS` array in it as sample data showing the
  exact schema shape each adapter must produce.

## First task for this session (historical — completed; kept for context)

Build the Phase 1 spreadsheet adapter:
1. `openpyxl`-based parser producing the common schema JSON (including
   `checks`) from a real `.xlsx` file — start with a small synthetic test
   file with at least one in-sheet formula and one cross-sheet reference, so
   both confidence tiers and at least one validation check are exercised.
2. The four output-shape functions over that JSON.
3. A naive flatten-to-CSV baseline function for the same file.
4. A short script/notebook that runs both through a small Q&A eval and
   prints an accuracy comparison.
5. A README for the adapter documenting its input/output contract.

**Done** — see "Current status" above. Phases 2-4 (document adapter,
diagram adapter, router + CLI + schema conformance) are also done. What's
actually next, if picking this back up: Phase 5 (auth/roles) remains
explicitly out of scope until asked for; a task-accuracy eval for
spreadsheets and a first eval harness for documents/diagrams
(`docs/eval-methodology.md`, "How to extend") are the more likely next
steps within the existing scope.
