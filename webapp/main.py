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

`webapp/engine.py` adds the speed/streaming layer around that same flow:
`POST /api/analyze/stream` (NDJSON events as the adapters yield regions),
OCR warm-up at startup (`GET /api/engine`), a SHA-256 result cache, the
"X-ray" baselines, and `GET /api/samples` (whitelisted fixtures). Still no
adapter logic of its own.

Two smaller endpoints wire the real Groq-backed task-accuracy Q&A
(`qa/groq_qa.py`) into the same page: `GET /api/ask/status` reports whether
`GROQ_API_KEY` is configured (no network call), and `POST /api/ask` answers
a question about the artifact currently shown, by calling the exact same
`qa.groq_qa.ask()` function `eval/run_real_eval.py` uses — no Groq-calling
logic is duplicated here either.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
import shutil
import sys
import tempfile
import traceback
import zipfile
from pathlib import Path
from typing import Any

from contextlib import asynccontextmanager

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
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

from webapp import engine  # noqa: E402  (needs the sys.path setup above)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Start loading the OCR model in the background so the first scan/diagram
    # a user drops in doesn't pay the multi-second model load. Only runs
    # under a real server (importing this module in tests starts nothing).
    engine.WARMUP.start()
    yield


app = FastAPI(title="nexDoc — local web app", lifespan=lifespan)


@app.middleware("http")
async def no_stale_assets(request, call_next):
    """The UI's HTML/JS/CSS are edited constantly while this runs as a local
    dev tool; `no-cache` makes the browser revalidate (cheap 304s) instead of
    silently serving a stale script."""
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.middleware("http")
async def optional_password(request, call_next):
    """Off by default (local use). When NEXDOC_PASSWORD is set — which a public
    deployment must do, since the app otherwise has no login and its Q&A box
    spends the host's Groq key — every route except /healthz requires HTTP
    Basic auth with that password (any username). Registered after the cache
    middleware so it is the outermost layer."""
    password = os.environ.get("NEXDOC_PASSWORD")
    if password and request.url.path != "/healthz":
        ok = False
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                _user, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
                ok = hmac.compare_digest(given.encode("utf-8"), password.encode("utf-8"))
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="nexDoc", charset="UTF-8"'})
    return await call_next(request)


@app.get("/healthz")
def healthz() -> JSONResponse:
    """Liveness for the platform. Deliberately 200 while the OCR model is still
    warming — spreadsheets and born-digital PDFs work immediately; only OCR
    work waits (see `ocr` for the real state)."""
    return JSONResponse({"ok": True, "ocr": engine.WARMUP.status()["state"]})


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


_build_preview_images = engine.build_preview_images


def _build_doc(kind: str, tmp_path: Path, artifact: dict[str, Any], original_filename: str) -> dict[str, Any]:
    return engine.build_doc(kind, tmp_path, artifact, original_filename)


@app.post("/api/analyze/stream")
async def analyze_stream(file: UploadFile = File(...)) -> StreamingResponse:
    """NDJSON event stream (see webapp/engine.py): start, sniffed, preview,
    geometry (diagrams), region*, done, xray, end — or error."""
    if not file.filename:
        return JSONResponse({"ok": False, "error": "No file was uploaded."}, status_code=400)  # type: ignore[return-value]
    content = await file.read()
    if len(content) > engine.MAX_UPLOAD_BYTES:
        return JSONResponse(  # type: ignore[return-value]
            {"ok": False, "error": f"'{file.filename}' is {len(content) / 1048576:.0f} MB; this local tool accepts files up to {engine.MAX_UPLOAD_BYTES // 1048576} MB."},
            status_code=413,
        )
    return StreamingResponse(
        engine.stream_analysis(file.filename, content),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/api/engine")
def engine_status() -> JSONResponse:
    return JSONResponse({"ocr": engine.WARMUP.status(), "cache": engine.CACHE.stats()})


@app.get("/api/samples")
def samples() -> JSONResponse:
    return JSONResponse({"samples": engine.list_samples()})


@app.get("/api/samples/{sample_id}")
def sample_file(sample_id: str):
    path = engine.sample_path(sample_id)
    if path is None:
        return _error(404, f"Unknown sample '{sample_id}'.")
    return FileResponse(path, filename=path.name)


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
    print(f"\nnexDoc — local web app")
    print(f"Open: http://{host}:{port}\n")
    uvicorn.run("webapp.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
