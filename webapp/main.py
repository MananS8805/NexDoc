"""Local-only FastAPI wrapper around the existing adapter pipeline.

This is deliberately thin. It does not reimplement any adapter logic, and it
does not reopen the scope ADR 0003 closed (see
docs/adr/0003-static-html-over-live-server.md): no accounts, no database, no
reviewer/approval workflow, no hosting story. It exists so a single local
user can drag a file into a browser tab instead of typing a CLI command, and
see the exact same real output `run_adapter.py` produces — nothing here
fabricates or hardcodes a result.

Run it with:

    uvicorn webapp.main:app --reload

or

    python -m webapp.main

Both print the local URL (default http://127.0.0.1:8000) on startup. Only
127.0.0.1 is bound — this is not meant to be reachable from another machine.

Request flow for the main endpoint (`POST /api/analyze`):

    uploaded file -> temp file on disk -> adapters.router.build_artifact_for
    (the exact function run_adapter.py's CLI uses) -> scripts/build_prototype's
    build_doc_for (the exact function that bakes DOCS entries for
    prototype.html) -> that dict, as JSON, back to the browser.

No routing logic, adapter logic, or output-shaping logic is duplicated here;
every real transformation happens in adapters/ and scripts/build_prototype.py.

Two smaller endpoints wire the real Groq-backed task-accuracy Q&A
(`qa/groq_qa.py`) into the same page: `GET /api/ask/status` reports whether
`GROQ_API_KEY` is configured (no network call), and `POST /api/ask` answers
a question about the artifact currently shown, by calling the exact same
`qa.groq_qa.ask()` function `eval/run_real_eval.py` uses — no Groq-calling
logic is duplicated here either.
"""

from __future__ import annotations

import base64
import json
import shutil
import sys
import tempfile
import traceback
import zipfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Same sys.path setup run_adapter.py does, so `scripts/build_prototype.py`
# (not a package — no __init__.py) can be imported the same way the CLI
# imports it, without moving or duplicating it into webapp/.
for p in (str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

app = FastAPI(title="Semantic preprocessing pipeline — local web app")


def _error(status_code: int, message: str) -> JSONResponse:
    """Mirrors run_adapter.py's `_fail`: a clear, honest message — never a
    raw stack trace for an expected failure mode."""
    return JSONResponse({"ok": False, "error": message}, status_code=status_code)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)) -> JSONResponse:
    if not file.filename:
        return _error(400, "No file was uploaded.")

    # Write the upload to a real temp file so the exact same file-path-based
    # functions the CLI uses (content sniffing, adapters, box re-derivation)
    # can run unmodified against it. A temp *directory* holding a file with
    # the original name (rather than a single randomly-named temp file) so
    # the adapter's title (path.stem, per adapters/*/adapter.py) and the
    # schema's `source.path` reflect the file the user actually uploaded,
    # not an opaque generated name — cleaned up in `finally` regardless.
    safe_name = Path(file.filename).name or "upload"
    tmp_dir = Path(tempfile.mkdtemp(prefix="nexdoc-upload-"))
    tmp_path = tmp_dir / safe_name
    try:
        content = await file.read()
        tmp_path.write_bytes(content)

        if not content:
            return _error(400, "The uploaded file is empty.")

        from adapters.router import UnsupportedArtifactError, build_artifact_for, sniff_kind

        try:
            kind = sniff_kind(tmp_path)
        except FileNotFoundError as exc:  # pragma: no cover — we just wrote this file
            return _error(500, str(exc))
        except UnsupportedArtifactError as exc:
            return _error(400, str(exc))

        try:
            _detected_kind, artifact = build_artifact_for(tmp_path)
        except zipfile.BadZipFile as exc:
            return _error(400, f"'{file.filename}' could not be opened as an .xlsx workbook: {exc}")
        except Exception as exc:  # adapter-boundary safety net — see run_adapter.py's identical guard
            traceback.print_exc()
            return _error(
                500,
                f"the {kind} adapter failed while processing '{file.filename}': "
                f"{type(exc).__name__}: {exc}\n"
                "This is reported as a clean error rather than a raw traceback, but it is a real "
                "failure — not a recognized-but-unresolvable pattern (those degrade to a "
                "low-confidence region instead of raising).",
            )

        doc = _build_doc(kind, tmp_path, artifact, file.filename)
        try:
            doc["previewImages"] = _build_preview_images(kind, tmp_path)
        except Exception:
            # A real preview image is a display nicety, not the actual
            # result — if rendering it fails for some reason (a corrupt
            # page mid-document, an unusual image codec), the analysis
            # itself already succeeded and should still be returned rather
            # than turning a working request into a 500.
            traceback.print_exc()
            doc["previewImages"] = None
        return JSONResponse({"ok": True, "doc": doc})
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _png_data_url(png_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


def _build_preview_images(kind: str, tmp_path: Path) -> list[dict[str, Any]] | None:
    """Real pixels from the file the user actually uploaded — not the
    abstract confidence-colored box overlay `renderPreview()` already drew
    (that stays; this is additive). Returns a list of
    `{dataUrl, width, height, pageLabel}` — one entry for a diagram's single
    image, one per page for a document — or `None` when there's no bitmap
    representation to show (a spreadsheet has no "picture of itself"; the
    existing cell-grid preview already covers that case honestly).

    Runs against `tmp_path` before the caller's `finally: shutil.rmtree`
    deletes it — this endpoint still writes nothing to disk beyond that
    same temp file every other step here already uses, and the images are
    embedded directly in the JSON response rather than served from a
    second endpoint, so nothing persists server-side after the response is
    sent.
    """
    if kind == "diagram":
        from PIL import Image

        with Image.open(tmp_path) as img:
            img = img.convert("RGB")
            width, height = img.size
            import io

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return [{"dataUrl": _png_data_url(buf.getvalue()), "width": width, "height": height, "pageLabel": None}]

    if kind == "document":
        import fitz

        pages = []
        doc = fitz.open(tmp_path)
        try:
            for page in doc:
                # 150dpi: readable text, small enough to embed several pages
                # directly in one JSON response without the payload getting
                # unreasonable — this is a preview image, not an archival
                # scan, so it doesn't need to match the OCR pass's own
                # working resolution (adapters/document/adapter.py renders
                # at 200dpi for that separate purpose).
                pix = page.get_pixmap(dpi=150)
                pages.append(
                    {
                        "dataUrl": _png_data_url(pix.tobytes("png")),
                        "width": pix.width,
                        "height": pix.height,
                        "pageLabel": f"Page {page.number + 1}",
                    }
                )
        finally:
            doc.close()
        return pages

    return None  # spreadsheet: no bitmap representation; frontend keeps the cell-grid preview


def _build_doc(kind: str, tmp_path: Path, artifact: dict[str, Any], original_filename: str) -> dict[str, Any]:
    """Enriches the real artifact into the same UI-facing shape
    prototype.html's DOCS entries use (region typeLabel + box, table
    columns, checks, the full schema for the json tab) by calling the exact
    function that produces those entries today — `build_doc_for` in
    scripts/build_prototype.py — so the live app and the static report stay
    driven by one real rendering path, not two.

    `build_doc_for` re-derives per-region screen positions ("box") by
    re-reading the same temp file (PyMuPDF for PDFs, OpenCV for images,
    openpyxl for spreadsheets) — the same re-derivation run_adapter.py's CLI
    already relies on for every real file it's pointed at, not something new
    for this endpoint. If that re-derivation itself fails for some reason,
    fall back to the artifact's four output shapes with no position data —
    the frontend renders everything except the box-overlay preview in that
    case, rather than the whole request failing over a display nicety.
    """
    from build_prototype import build_doc_for

    try:
        doc = build_doc_for(kind, tmp_path, artifact)
    except Exception:
        traceback.print_exc()
        from adapters.common.outputs import to_table

        table = to_table(artifact)
        failed = [c for c in artifact["checks"] if not c["passed"]]
        check_summary = f"{len(failed)} check{'s' if len(failed) != 1 else ''} flagged" if failed else "all checks passed"
        doc = {
            "id": Path(original_filename).stem,
            "kind": artifact["kind"],
            "adapter": artifact["adapter"],
            "title": artifact["artifact"],
            "meta": f"{len(artifact['regions'])} regions · {check_summary}",
            "pageLabel": f"{artifact['kind']} · {original_filename}",
            "regions": [
                {**r, "typeLabel": f"{r['id']} · {r['type']}", "lang": r.get("language"), "box": None}
                for r in artifact["regions"]
            ],
            "tableColumns": table["columns"],
            "table": table["rows"],
            "tableEmptyReason": table.get("empty_reason"),
            "checks": artifact["checks"],
            "realArtifact": artifact,
        }
    doc["pageLabel"] = f"{artifact['kind']} · {original_filename}"
    return doc


class AskRequest(BaseModel):
    question: str
    artifact: dict[str, Any]


@app.get("/api/ask/status")
def ask_status() -> JSONResponse:
    """Lets the frontend know, before the user types anything, whether the
    "Ask a question" box can actually work — the same honest-up-front
    pattern the document/diagram adapters use for Tesseract availability.
    No network call: this only checks whether GROQ_API_KEY is set.
    """
    from qa.groq_qa import api_key_status

    available, detail = api_key_status()
    return JSONResponse({"available": available, "detail": detail})


@app.post("/api/ask")
def ask(request: AskRequest) -> JSONResponse:
    """Answers a natural-language question about the artifact the user just
    analyzed, using the exact same `qa.groq_qa.ask()` the real
    task-accuracy eval (`eval/run_real_eval.py`) uses — no Groq-calling
    logic is duplicated here. The artifact's structured region list
    (`adapters.common.outputs.to_structured`) is the context handed to the
    model; see qa/groq_qa.py's module docstring for why that representation
    specifically.
    """
    if not request.question.strip():
        return _error(400, "Question is empty.")

    from adapters.common.outputs import to_structured
    from qa.groq_qa import GroqQAError, ask as ask_groq

    try:
        context = json.dumps(to_structured(request.artifact), indent=2, default=str)
    except Exception as exc:
        return _error(400, f"Could not read the artifact sent with this question: {exc}")

    try:
        answer = ask_groq(request.question, context)
    except GroqQAError as exc:
        # Missing/invalid key or a real Groq API failure — reported clearly,
        # never a raw stack trace and never a fabricated answer.
        return _error(503, str(exc))

    return JSONResponse({"ok": True, "answer": answer})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:
    import uvicorn

    host, port = "127.0.0.1", 8000
    print(f"\nSemantic preprocessing pipeline — local web app")
    print(f"Open: http://{host}:{port}\n")
    uvicorn.run("webapp.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
