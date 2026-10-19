"""Engine glue for the nexDoc web app — everything that makes the live UI fast
and streamable, kept out of `main.py` so that file stays a thin router.

Nothing here reimplements adapter logic. It wraps the same
`adapters.router.build_artifact_for` and `scripts/build_prototype.build_doc_for`
the CLI uses and adds four things around them:

1. **OCR warm-up.** EasyOCR's model load is the single biggest fixed cost in
   the whole pipeline (measured: ~30s cold on the dev machine; every adapter
   step besides OCR is well under a second). The server starts loading it in a
   background thread at startup so the first scanned page/diagram a user drops
   in doesn't pay for it, and `/api/engine` reports honest warm-up state.
2. **A content-addressed result cache.** Keyed by SHA-256 of the uploaded
   bytes, so re-running the same file (or a built-in sample) is instant. The
   adapters are deterministic, so a hit is exactly what a fresh run would give.
3. **Streaming analysis** (`stream_analysis`). The adapters already yield one
   region at a time (CLAUDE.md's "live processing pattern"); this surfaces
   that as NDJSON events — sniffed kind, a preview of the actual file, detected
   shape geometry (diagrams, before OCR), each region as it lands, then the
   full document — so the UI renders progressively instead of after a spinner.
4. **X-ray baselines.** The real naive "flatten it" baselines the evals already
   use, packaged so the UI can show what a generic pipeline would have kept.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import math
import os
import queue
import re
import shutil
import tempfile
import threading
import time
import traceback
import zipfile
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"

# Uploads are read fully into memory; this is a local single-user tool, so a
# generous cap is enough to keep a mistaken drop (a 4GB video) from hanging it.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# One analysis at a time: the OCR reader is a single shared PyTorch model and
# two concurrent jobs would just slow each other down on a CPU-only machine.
ANALYSIS_LOCK = threading.Lock()


# --------------------------------------------------------------------------- CPU budget


def detect_cpu_limit() -> int | None:
    """The number of CPUs this process may actually use, *only when something
    constrains it below the host's core count* — else None (leave PyTorch's
    own default alone, so a plain local run is unchanged).

    Why this exists: PyTorch sizes its thread pool from the host's core count.
    In a container limited to, say, 2 CPUs on a 16-core host that means 16
    threads fighting over 2 CPUs of quota — the kernel throttles them and OCR
    gets markedly slower than on the same 2 CPUs used sensibly. Reads, in
    order: NEXDOC_TORCH_THREADS (explicit override), CPU affinity, cgroup v2
    `cpu.max`, cgroup v1 `cfs_quota_us/cfs_period_us`.
    """
    env = os.environ.get("NEXDOC_TORCH_THREADS", "").strip()
    if env.isdigit() and int(env) > 0:
        return int(env)
    host = os.cpu_count() or 1
    limit = host
    try:
        limit = min(limit, len(os.sched_getaffinity(0)))  # Linux only
    except AttributeError:
        pass
    try:  # cgroup v2: "max 100000" or "200000 100000"
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()[:2]
        if quota != "max":
            limit = min(limit, max(1, math.ceil(int(quota) / int(period))))
    except (OSError, ValueError):
        try:  # cgroup v1
            quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
            period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
            if quota > 0 and period > 0:
                limit = min(limit, max(1, math.ceil(quota / period)))
        except (OSError, ValueError):
            pass
    return limit if limit < host else None


# --------------------------------------------------------------------------- OCR warm-up


class OcrWarmup:
    """Loads the shared EasyOCR reader once, in the background, and reports
    real state (never a guess): idle -> warming -> ready | unavailable."""

    def __init__(self) -> None:
        self.state = "idle"
        self.detail = ""
        self.started_at: float | None = None
        self.seconds: float | None = None
        self.threads: int | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self.state != "idle":
                return
            self.state = "warming"
            self.started_at = time.perf_counter()
        threading.Thread(target=self._run, name="ocr-warmup", daemon=True).start()

    @staticmethod
    def _preimport() -> None:
        """Import the heavy-but-fast modules (OpenCV, PyMuPDF, openpyxl, Pillow,
        the adapters) now, so the first request doesn't spend ~1-2s importing
        them. Measured: a cold first diagram analysis took ~2.4s longer in
        total than its own stage timings accounted for — this was it."""
        try:
            import cv2, fitz, numpy, openpyxl  # noqa: F401
            from PIL import Image  # noqa: F401
            import adapters.router, adapters.spreadsheet.adapter, adapters.document.adapter, adapters.diagram.adapter  # noqa: F401
            import build_prototype  # noqa: F401
        except Exception:
            traceback.print_exc()

    def _run(self) -> None:
        self._preimport()
        try:
            limit = detect_cpu_limit()
            if limit:
                import torch

                torch.set_num_threads(limit)
                self.threads = limit
            from adapters.common.ocr import ocr_engine_status

            ok, detail = ocr_engine_status()
        except Exception as exc:  # torch/easyocr import failures (e.g. a broken DLL) must never crash the server
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        self.seconds = round(time.perf_counter() - (self.started_at or time.perf_counter()), 2)
        self.detail = detail
        self.state = "ready" if ok else "unavailable"

    def status(self) -> dict[str, Any]:
        elapsed = None
        if self.state == "warming" and self.started_at is not None:
            elapsed = round(time.perf_counter() - self.started_at, 1)
        return {"state": self.state, "detail": self.detail, "loadSeconds": self.seconds, "elapsedSeconds": elapsed, "torchThreads": self.threads}


WARMUP = OcrWarmup()


# --------------------------------------------------------------------------- result cache


class ResultCache:
    """Tiny in-memory LRU keyed by file SHA-256. Process-local, never written
    to disk — consistent with ADR 0003's "no persistence" stance."""

    def __init__(self, maxsize: int = 12) -> None:
        self.maxsize = maxsize
        self._items: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                self.misses += 1
                return None
            self._items.move_to_end(key)
            self.hits += 1
            return item

    def put(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._items[key] = value
            self._items.move_to_end(key)
            while len(self._items) > self.maxsize:
                self._items.popitem(last=False)

    def update(self, key: str, **fields: Any) -> None:
        with self._lock:
            if key in self._items:
                self._items[key].update(fields)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {"entries": len(self._items), "hits": self.hits, "misses": self.misses}


CACHE = ResultCache()


# --------------------------------------------------------------------------- previews / docs


def _png_data_url(png_bytes: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png_bytes).decode("ascii")


def build_preview_images(kind: str, tmp_path: Path) -> list[dict[str, Any]] | None:
    """Real pixels from the uploaded file: one image for a diagram, one per
    page (150dpi) for a document, None for a spreadsheet (no bitmap of
    itself; the UI draws the cell grid instead)."""
    if kind == "diagram":
        from PIL import Image

        with Image.open(tmp_path) as img:
            img = img.convert("RGB")
            width, height = img.size
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return [{"dataUrl": _png_data_url(buf.getvalue()), "width": width, "height": height, "pageLabel": None}]

    if kind == "document":
        import fitz

        pages = []
        pdf = fitz.open(tmp_path)
        try:
            for page in pdf:
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
            pdf.close()
        return pages

    return None


def build_doc(kind: str, tmp_path: Path, artifact: dict[str, Any], original_filename: str) -> dict[str, Any]:
    """Enriches the real artifact into the UI-facing doc shape via the exact
    function the static prototype uses (`build_doc_for`); falls back to the
    four plain output shapes with no positions if box re-derivation fails."""
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


# --------------------------------------------------------------------------- X-ray


def _sheet_grids(csv_text: str) -> dict[str, list[list[str]]]:
    grids: dict[str, list[list[str]]] = {}
    current: list[list[str]] | None = None
    block: list[str] = []

    def flush() -> None:
        if current is not None:
            current.extend(csv.reader(io.StringIO("\n".join(block))))

    for line in csv_text.splitlines():
        if line.startswith("# sheet: "):
            flush()
            block = []
            name = line[len("# sheet: ") :]
            current = grids.setdefault(name, [])
        else:
            block.append(line)
    flush()
    return grids


def build_xray(kind: str, tmp_path: Path, artifact: dict[str, Any]) -> dict[str, Any]:
    """What a generic "just flatten it" pipeline keeps vs. what nexDoc keeps,
    from the same real baselines the evals use (`adapters/*/baseline.py`).

    The "flattened" column of the stats is "not representable", not a
    measured score: plain text/CSV has no slot for typed regions,
    relationships, per-region confidence or validation checks. The lines in
    `lost` are the measured, file-specific part.
    """
    regions = artifact["regions"]
    relationships = sum(len(r.get("relationships") or []) for r in regions)
    lost: list[str] = []

    if kind == "spreadsheet":
        from adapters.spreadsheet.baseline import flatten_to_csv

        flat = flatten_to_csv(tmp_path)
        label = "CSV export of every sheet — what a generic pipeline sees"
        grids = _sheet_grids(flat)
        formulas = [r for r in regions if r["type"] == "formula-cell"]
        blank = 0
        for r in formulas:
            sheet, _, ref = r["id"].partition("!")
            m = re.match(r"^([A-Za-z]+)(\d+)$", ref)
            if not m:
                continue
            col = 0
            for ch in m.group(1).upper():
                col = col * 26 + (ord(ch) - 64)
            row = int(m.group(2))
            grid = grids.get(sheet, [])
            value = grid[row - 1][col - 1] if row - 1 < len(grid) and col - 1 < len(grid[row - 1]) else ""
            if value == "":
                blank += 1
        if formulas:
            lost.append(f"{blank} of {len(formulas)} formula cells come out blank in the flattened CSV — the formula and its value are both gone.")
        xs = sum(1 for r in regions for rel in (r.get("relationships") or []) if rel["type"] == "cross-sheet-ref")
        if xs:
            lost.append(f"{xs} cross-sheet reference{'s' if xs != 1 else ''} vanish entirely — CSV has no notion of another sheet.")
        unresolved = sum(1 for r in formulas if "unresolved" in (r.get("text") or ""))
        if unresolved:
            lost.append(f"{unresolved} broken reference{'s' if unresolved != 1 else ''} flagged by nexDoc would be invisible in the flat export.")
    elif kind == "document":
        from adapters.document.baseline import flatten_to_text

        flat = flatten_to_text(tmp_path)
        label = "Plain text extraction (no OCR fallback) — what a generic pipeline sees"
        ocr_lines = [r for r in regions if r["type"] == "ocr-line"]
        if ocr_lines and not flat.strip():
            lost.append(f"This is a scan: flat text extraction recovers 0 characters, while nexDoc OCR'd {len(ocr_lines)} line{'s' if len(ocr_lines) != 1 else ''}.")
        sums = sum(1 for r in regions for rel in (r.get("relationships") or []) if rel["type"] == "sums-line-items")
        if sums:
            lost.append(f"{sums} line-item → total link{'s' if sums != 1 else ''} are lost — flat text can't say which numbers add up to the total.")
        types = {r["type"] for r in regions}
        if len(types) > 1:
            lost.append(f"{len(types)} semantic roles ({', '.join(sorted(types))}) collapse into undifferentiated text blocks.")
        langs = {r.get("language") for r in regions if r.get("language")}
        if len(langs) > 1:
            lost.append(f"Language tags ({', '.join(sorted(langs))}) are dropped.")
    else:  # diagram
        from adapters.diagram.baseline import flatten_labels

        flat = flatten_labels(tmp_path)
        label = "Whole-image OCR bag of labels — what a generic pipeline sees"
        edges = [r for r in regions if r["type"] == "edge"]
        if edges:
            lost.append(f"{len(edges)} connection{'s' if len(edges) != 1 else ''} (who points to whom, and which way) exist only as geometry — a flat label list has none of them.")
        shapes: dict[str, int] = {}
        for r in regions:
            if r["type"] == "node" and r.get("shape"):
                shapes[r["shape"]] = shapes.get(r["shape"], 0) + 1
        if shapes:
            lost.append("Shape semantics are dropped: " + ", ".join(f"{n}× {s}" for s, n in sorted(shapes.items())) + ".")
        if not flat.strip():
            lost.append("The OCR engine produced no flattened text here at all.")

    stats = [
        {"label": "Typed regions", "nexdoc": len(regions)},
        {"label": "Relationships", "nexdoc": relationships},
        {"label": "Per-region confidence", "nexdoc": len(regions)},
        {"label": "Validation checks", "nexdoc": len(artifact["checks"])},
    ]
    limit = 5000
    return {
        "flattenedLabel": label,
        "flattened": flat if len(flat) <= limit else flat[:limit] + "\n… (truncated)",
        "stats": stats,
        "lost": lost,
    }


# --------------------------------------------------------------------------- samples


SAMPLES = [
    ("budget", "Q1 budget workbook", "spreadsheet", "budget_test.xlsx", "Two sheets · cross-sheet formulas · one deliberately broken reference"),
    ("invoice", "Born-digital invoice", "document", "invoice_born_digital.pdf", "Text layer · line items · total check"),
    ("contract", "Two-page document", "document", "invoice_two_pages.pdf", "Multi-page scrolling viewer"),
    ("scan", "Scanned note · EN + हिन्दी", "document", "note_scanned.pdf", "OCR path · Devanagari script"),
    ("flow", "Vendor onboarding flowchart", "diagram", "flowchart_clean.png", "Decision diamond · directed edges"),
    ("uml", "UML activity diagram", "diagram", "activity_diagram_clean.png", "Fork/join · final node · swimlane"),
    ("sequence", "UML sequence diagram", "diagram", "sequence_diagram_clean.png", "Actors · activation bars · messages"),
]


def list_samples() -> list[dict[str, Any]]:
    out = []
    for sid, label, kind, filename, blurb in SAMPLES:
        path = FIXTURES / filename
        if path.exists():
            out.append({"id": sid, "label": label, "kind": kind, "filename": filename, "blurb": blurb, "bytes": path.stat().st_size})
    return out


def sample_path(sample_id: str) -> Path | None:
    """Whitelist lookup only — never builds a path from user input."""
    for sid, _label, _kind, filename, _blurb in SAMPLES:
        if sid == sample_id and (FIXTURES / filename).exists():
            return FIXTURES / filename
    return None


# --------------------------------------------------------------------------- streaming analysis


def _line(event: str, **payload: Any) -> str:
    return json.dumps({"event": event, **payload}, ensure_ascii=False, default=str) + "\n"


def _region_payload(kind: str, region_dict: dict[str, Any]) -> dict[str, Any]:
    from build_prototype import _type_label_spreadsheet

    if kind == "spreadsheet":
        label = _type_label_spreadsheet(region_dict)
    else:
        label = f"{region_dict['id']} · {region_dict['type']}" + (f" ({region_dict['shape']})" if region_dict.get("shape") else "")
    return {**region_dict, "typeLabel": label, "lang": region_dict.get("language")}


def _replay_cached(entry: dict[str, Any], filename: str) -> Iterator[str]:
    doc = entry["doc"]
    yield _line("sniffed", kind=doc["kind"], ms=0)
    yield _line("preview", images=entry.get("images") or [], ms=0)
    for i, region in enumerate(doc["regions"]):
        yield _line("region", index=i, region=region, box=region.get("box"), tMs=0)
    yield _line("done", doc={**doc, "pageLabel": f"{doc['kind']} · {filename}"}, timings={**entry["timings"], "cached": True})
    if entry.get("xray"):
        yield _line("xray", xray=entry["xray"])


def stream_analysis(filename: str, content: bytes) -> Iterator[str]:
    """NDJSON event stream for one uploaded file. See module docstring."""
    sha = hashlib.sha256(content).hexdigest()
    yield _line("start", filename=filename, size=len(content), sha256=sha[:16], engine=WARMUP.status())

    cached = CACHE.get(sha)
    if cached is not None:
        yield from _replay_cached(cached, filename)
        yield _line("end")
        return

    events: queue.Queue = queue.Queue()
    DONE = object()

    def worker() -> None:
        safe_name = Path(filename).name or "upload"
        tmp_dir = Path(tempfile.mkdtemp(prefix="nexdoc-upload-"))
        tmp_path = tmp_dir / safe_name
        t_start = time.perf_counter()

        def ms_since(t: float) -> int:
            return int((time.perf_counter() - t) * 1000)

        xray_job = None
        try:
            if not content:
                events.put(_line("error", message="The uploaded file is empty.", status=400))
                return
            tmp_path.write_bytes(content)

            if not ANALYSIS_LOCK.acquire(blocking=False):
                events.put(_line("queued"))
                ANALYSIS_LOCK.acquire()
            try:
                from adapters.router import UnsupportedArtifactError, build_artifact_for, sniff_kind

                t0 = time.perf_counter()
                try:
                    kind = sniff_kind(tmp_path)
                except UnsupportedArtifactError as exc:
                    events.put(_line("error", message=str(exc), status=400))
                    return
                t_sniff = ms_since(t0)
                events.put(_line("sniffed", kind=kind, ms=t_sniff))

                t0 = time.perf_counter()
                try:
                    images = build_preview_images(kind, tmp_path)
                except Exception:
                    traceback.print_exc()
                    images = None
                t_preview = ms_since(t0)
                events.put(_line("preview", images=images or [], ms=t_preview))

                geometry: dict[str, Any] = {}
                if kind == "diagram":
                    try:
                        from build_prototype import compute_all_diagram_boxes

                        geometry = compute_all_diagram_boxes(tmp_path)
                        events.put(_line("geometry", boxes=geometry))
                    except Exception:
                        traceback.print_exc()

                if kind == "document":
                    # A scanned page can't stream line-by-line (OCR returns a whole
                    # page at once and is slow on CPU), so say so up front instead
                    # of leaving the user watching an unexplained spinner.
                    try:
                        import fitz
                        from adapters.document.adapter import MIN_TEXT_LAYER_CHARS

                        pdf = fitz.open(tmp_path)
                        try:
                            scanned = sum(1 for pg in pdf if len(pg.get_text().strip()) < MIN_TEXT_LAYER_CHARS)
                        finally:
                            pdf.close()
                        if scanned:
                            events.put(
                                _line(
                                    "note",
                                    message=f"{scanned} scanned page{'s' if scanned != 1 else ''} → OCR on CPU, roughly 15–35s per page",
                                )
                            )
                    except Exception:
                        traceback.print_exc()

                counter = {"i": 0}
                t_extract0 = time.perf_counter()

                def on_region(region: Any) -> None:
                    rd = region.to_dict()
                    payload = _region_payload(kind, rd)
                    box = geometry.get(rd["id"])
                    if box is None and kind == "document" and re.match(r"^page\d+!block\d+$", rd["id"]):
                        try:
                            from build_prototype import _compute_boxes_document

                            box = _compute_boxes_document(tmp_path, [rd]).get(rd["id"])
                        except Exception:
                            box = None
                    events.put(_line("region", index=counter["i"], region=payload, box=box, tMs=ms_since(t_extract0)))
                    counter["i"] += 1

                try:
                    _kind, artifact = build_artifact_for(tmp_path, on_region=on_region)
                except zipfile.BadZipFile as exc:
                    events.put(_line("error", message=f"'{filename}' could not be opened as an .xlsx workbook: {exc}", status=400))
                    return
                except Exception as exc:
                    traceback.print_exc()
                    events.put(
                        _line(
                            "error",
                            message=(
                                f"the {kind} adapter failed while processing '{filename}': {type(exc).__name__}: {exc}\n"
                                "This is reported as a clean error rather than a raw traceback, but it is a real "
                                "failure — not a recognized-but-unresolvable pattern (those degrade to a "
                                "low-confidence region instead of raising)."
                            ),
                            status=500,
                        )
                    )
                    return
                t_extract = ms_since(t_extract0)

                t0 = time.perf_counter()
                doc = build_doc(kind, tmp_path, artifact, filename)
                t_map = ms_since(t0)
                timings = {
                    "sniffMs": t_sniff,
                    "previewMs": t_preview,
                    "extractMs": t_extract,
                    "mapMs": t_map,
                    "totalMs": ms_since(t_start),
                    "cached": False,
                    "ocrEngine": WARMUP.status()["state"],
                }
                events.put(_line("done", doc=doc, timings=timings))
                entry = {"doc": doc, "images": images or [], "timings": timings, "xray": None}
                CACHE.put(sha, entry)

                xray_job = (kind, artifact)
            finally:
                ANALYSIS_LOCK.release()

            # X-ray runs after the result is already on screen AND after the
            # analysis lock is released: the diagram baseline needs a
            # whole-image OCR pass (seconds) that shouldn't block the next
            # upload from starting.
            try:
                xray = build_xray(xray_job[0], tmp_path, xray_job[1])
                CACHE.update(sha, xray=xray)
                events.put(_line("xray", xray=xray))
            except Exception:
                traceback.print_exc()
                events.put(_line("xray", xray=None))
        except Exception as exc:  # last-resort guard so the stream always terminates
            traceback.print_exc()
            events.put(_line("error", message=f"{type(exc).__name__}: {exc}", status=500))
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            events.put(DONE)

    threading.Thread(target=worker, name="nexdoc-analysis", daemon=True).start()
    while True:
        item = events.get()
        if item is DONE:
            break
        yield item
    yield _line("end")
