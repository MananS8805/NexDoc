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
Semantic preprocessing pipeline — local web app
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

Unsupported files, corrupt files, and adapter crashes are shown as a plain
error message in the page — never a raw stack trace or a silent failure
(mirroring `run_adapter.py`'s own `_fail` handling).

## What's here

```
webapp/
  main.py            FastAPI app: page route, upload endpoint, ask endpoints
  static/
    index.html         the page shell
    styles.css          the design language from design/preprocessing-pipeline-mock.html,
                         extended with a dropzone/status/error component set
    app.js               tabs/region-list/checks-strip rendering, adapted from
                          the mock's own JS, driven by one fetched result
                          instead of a hardcoded DOCS array
```

### `POST /api/analyze`

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
