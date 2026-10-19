# Local web app

An interactive, local-only wrapper around the existing adapter pipeline.
Instead of running `run_adapter.py` from the command line and opening the
`prototype.html` it writes, you run one command, a server starts on
`127.0.0.1`, and you drag a file into the browser tab that opens.

This is **not** a hosted service, and it deliberately does not reopen the
scope [`docs/adr/0003-static-html-over-live-server.md`](../docs/adr/0003-static-html-over-live-server.md)
closed:

- no user accounts, no login
- no database, no persistence — the result of your last upload lives only
  in the browser tab's memory; refresh the page and it's gone
- no reviewer/approval workflow
- no deployment/hosting story — it binds to `127.0.0.1` only

Every real transformation still happens in `adapters/` exactly as it does
for the CLI. This app adds a thin FastAPI layer on top of
`adapters.router.build_artifact_for` (the same function `run_adapter.py`
calls) and a plain HTML/CSS/JS frontend with no build step, so
`pip install -r requirements.txt` is still the only setup this repo needs.

## Run it

From the repo root, with the project's dependencies installed:

```bash
pip install -r requirements.txt
uvicorn webapp.main:app --reload
```

or, equivalently:

```bash
python -m webapp.main
```

Either way, the server prints the local URL on startup:

```
nexDoc — local web app
Open: http://127.0.0.1:8000
```

Open that URL, then drag in (or click to choose) one of:

- a `.xlsx` spreadsheet
- a `.pdf` document
- a `.png`/`.jpg` diagram

The file is routed by its real content (not its extension or your file
picker's filter) to the matching adapter, processed once, and the page
renders the real result: the region list (type, confidence, text), a
confidence-tier breakdown, the artifact-level checks with their real
pass/fail and `detail` text, and the same four output shapes
(`text`/`structured`/`json`/`table`) `run_adapter.py` writes to disk.

### What the workspace shows

- **Streaming extraction.** Regions appear as the adapters find them — for a
  diagram, the detected shapes show up on the preview first (dashed ghosts),
  then fill in as OCR reads each label; a five-step tracker (detect → route →
  extract → map → validate) shows real per-stage timings.
- **A parallel, linked viewer.** Left: the actual uploaded file (every page,
  scrollable, zoomable; spreadsheets get a cell grid) with detected regions
  overlaid — or switch to a *Reconstruction* redraw of what was understood.
  Right: the live region stream. Hovering or selecting a region in either
  pane, the reconstruction, the graph or the X-ray highlights it in all of them.
- **Graph tab** — every artifact's relationships (formula-ref, cross-sheet-ref,
  sums-line-items, connects-to) as an interactive graph: diagrams keep their
  real layout, everything else is force-laid-out; hover to isolate a
  neighborhood, drag nodes, **Copy Mermaid** to export the same graph as a
  Mermaid flowchart.
- **X-ray tab** — what a generic "just flatten it" pipeline would have kept
  (the same real baselines the evals use: CSV export, plain text, OCR bag of
  labels) next to what nexDoc kept, with file-specific losses called out
  (e.g. "11 of 11 formula cells come out blank in the flattened CSV").
- **Samples** — one-click runs of the repo's fixtures.
- Light/dark theme, and the OCR engine's warm-up state in the header chip.

Unsupported files, corrupt files, and adapter crashes are shown as a plain
error message in the page — never a raw stack trace or a silent failure
(mirroring `run_adapter.py`'s own `_fail` handling).

## What's here

```
webapp/
  main.py            FastAPI app: routes only (page, analyze, stream, engine, samples, ask)
  engine.py          OCR warm-up, SHA-256 result cache, the streaming analysis
                      pipeline, X-ray baselines, sample whitelist
  static/
    index.html         page shell: landing (hero, how-it-works) + workspace
    styles.css          design system (dark/light), viewer, graph, X-ray
    js/hero.js           dithered cloud sky, condensing nav, theme toggle, reveals
    js/graph.js          semantic-graph model, force layout, SVG render, Mermaid export
    js/app.js            upload + NDJSON stream client, paced event pump,
                          linked panes, reconstruction, downloads, Q&A
```

### `POST /api/analyze/stream` — the endpoint the UI uses

Same input as `/api/analyze` (below), but the response is
`application/x-ndjson`: one JSON event per line, in this order —
`start`, `sniffed` (kind), `preview` (the file's real pages/image),
`geometry` (diagrams only: every detected shape's box, before any OCR),
`note` (e.g. "1 scanned page → OCR on CPU…"), `region`* (one per region, as
the adapter yields it), `done` (the full doc + per-stage timings), `xray`
(the flatten-vs-nexDoc comparison, sent last because the diagram baseline
needs its own OCR pass), `end`. Failures are an `error` event with the same
messages and 400/500 distinction as `/api/analyze`.

The adapters' `build_artifact(path, on_region=...)` hook is what feeds this;
it fires once per region and changes no result.

### `GET /api/engine`, `GET /api/samples`, `GET /api/samples/{id}`

`/api/engine` reports the OCR model's real state (`warming` → `ready` |
`unavailable`, with load seconds) and cache hit/miss counts. `/api/samples`
lists the bundled fixtures; `/api/samples/{id}` serves one — a hard-coded
whitelist, never a path built from the request.

## Performance — what was measured, what changed, what didn't

Measured on the dev machine (12 logical CPUs, CPU-only PyTorch). Everything
except OCR was already fast (spreadsheet / born-digital PDF well under 0.2s);
OCR is what costs seconds, so that is where the work went. Each claim below was
checked with an alternating A/B in one process (or against the real server).

| Change | Measured effect |
|---|---|
| OCR model loaded in a background thread at server start | The 24–47s model load (seen: 24.1, 45.0, 46.7s) no longer lands on the first scan/diagram request |
| Heavy modules (OpenCV, PyMuPDF, openpyxl, adapters) pre-imported in the same thread | First diagram request: 2,441ms unexplained overhead → 77ms |
| Scanned pages OCR'd at 150dpi instead of 200dpi | **1.55× faster** (median 24.1s → 15.5s per page), OCR text identical on the English + Devanagari fixture |
| SHA-256 result cache (in-memory LRU, 12 entries) | Re-running the same file replays instantly instead of re-analyzing (adapters are deterministic) |
| Streaming instead of one blocking response | First visible content (preview, diagram shapes) in ~0.2–0.3s instead of after the full 4–35s run |

**Tried and rejected** (so nobody repeats them):

- Skipping OCR on edge crops with no leftover ink: ~1.03× — empty crops were already cheap; removed as dead weight.
- Running label-crop OCR in parallel threads: ~26% faster at 2 threads, *slower* at 4, and noisy — not adopted.
- Lower scan resolution / smaller EasyOCR canvas: 120dpi and `canvas_size=1280–1600` start corrupting characters ("Signed:" → "Signedः", "16" → "1६"), so 150dpi is the floor that was verified lossless.
- RapidOCR (ONNX, default PP-OCRv4 models): ~1.5× faster on a whole page but *slower* per label crop and it silently dropped the Hindi line — not a drop-in. See [`docs/ocr-engine-options.md`](../docs/ocr-engine-options.md).

Scanned pages remain the slow path (~15–35s/page on CPU). A page can't stream
line-by-line (EasyOCR returns a page at once), so the UI says so up front.

### `POST /api/analyze` (single-response variant, kept for scripts)


Accepts a single uploaded file (`multipart/form-data`, field name `file`).
Writes it to a temp file, calls `adapters.router.build_artifact_for` (the
exact router+adapter dispatch the CLI uses — not reimplemented here), then
calls `scripts/build_prototype.py`'s `build_doc_for` (the exact function
that builds each `DOCS[]` entry for `prototype.html`) to shape the result
for the UI. The temp file is deleted before the response is returned.

Response shape on success:

```json
{ "ok": true, "doc": { "...": "the same shape prototype.html's DOCS entries use" } }
```

`doc.realArtifact` is the untouched common-schema JSON (`source`, `regions`,
`checks`) — the same thing `schema.json` holds for a CLI run. `doc.regions`
carries the four output shapes' worth of information used by the UI, and
`doc.table`/`doc.tableColumns` is exactly `adapters.common.outputs.to_table`'s
result.

On failure (unsupported/corrupt file, or a real adapter error):

```json
{ "ok": false, "error": "a clear, human-readable message" }
```

with `400` for a bad/unsupported input and `500` for the adapter itself
raising — the same distinction `run_adapter.py` draws.

### `GET /api/ask/status`, `POST /api/ask` — real, Groq-backed Q&A

After a file is analyzed, the page shows an "Ask a question" box. This
wires the real task-accuracy Q&A path (`qa/groq_qa.py`,
[`docs/eval-methodology.md`](../docs/eval-methodology.md)) into the UI —
it is not a mock or a canned response.

`GET /api/ask/status` reports `{"available": bool, "detail": str}` by
checking whether `GROQ_API_KEY` is set (no network call). The frontend
calls this once per page load and disables the input with the honest
`detail` message (e.g. "GROQ_API_KEY is not set...") when it's `false`,
rather than letting the user submit a question that's doomed to fail.

`POST /api/ask` takes `{"question": str, "artifact": <the same object as
doc.realArtifact>}`, builds the structured-JSON context
(`adapters.common.outputs.to_structured`) from that artifact, and calls
`qa.groq_qa.ask()` — the exact same function
[`eval/run_real_eval.py`](../eval/run_real_eval.py) uses, so the Groq-
calling logic exists in exactly one place. Response on success:

```json
{ "ok": true, "answer": "a real free-text answer from Groq" }
```

On failure (empty question, or `qa.groq_qa.GroqQAError` — missing/invalid
key, or a real Groq API failure): `{"ok": false, "error": "..."}` with
`400`/`503` respectively — never a raw stack trace.
