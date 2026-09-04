"""Shared OCR engine wrapper and availability probe, used by any adapter
that may need OCR (currently document and diagram). This is genuinely
shared infrastructure — not spreadsheet-adapter logic — so it lives in
`common` rather than being duplicated per adapter or creating a dependency
between otherwise-independent adapters.

## Why EasyOCR, not pytesseract/Tesseract (history)

This module used to wrap `pytesseract`, a thin binding around the
Tesseract OCR *binary* — a real, separate piece of software `pytesseract`
does not install for you. Two real problems with that, found while using
this exact codebase:

1. Installing the Tesseract binary is an OS-level install. On a machine
   without admin/elevation, that's a hard, unresolvable blocker — not a
   bug, just a fact about where Tesseract's own distribution model puts
   the friction (see the old version of this section, and
   `adapters/document/README.md`'s prior "Environment-specific fact" note,
   for a session that ran into exactly this).
2. `adapters/document/adapter.py`'s OCR call site never passed a `lang=`
   argument to `pytesseract.image_to_data`, so it always OCR'd with
   whichever language pack Tesseract defaults to (English) regardless of
   the page's actual script. Verified directly on this repo's own
   `tests/fixtures/note_scanned.pdf`, whose scanned page deliberately
   includes a Devanagari (Hindi) line: it came back as
   `Ae Ot od @ se YT ENT` — not because Tesseract can't do Devanagari, but
   because this code path never asked it to, and most environments (this
   one included) only have the `eng` Tesseract language pack installed
   anyway.

EasyOCR (pure Python + PyTorch, Apache-2.0) fixes both: no system binary,
so no elevation is needed to install it (`pip install easyocr` is enough);
and its recognition models are selected explicitly by language when you
build a `Reader`, so asking for Devanagari support means actually loading
Devanagari-capable weights instead of hoping an OS language pack happens
to be present. It also runs fully offline/on-CPU at inference time — the
only network access is a one-time model download to a local cache
(`~/.EasyOCR`) the first time a given language combination is used.

Tesseract support was removed rather than kept as a fallback. Two OCR
code paths (two sets of language-availability semantics, two confidence
scales, two "is it installed" checks) is real, ongoing complexity for a
marginal benefit: anyone who already had Tesseract configured loses
nothing they were relying on for correctness (its accuracy and script
coverage were already the weaker of the two), and EasyOCR's only real
downside is a larger dependency (`torch`) and slower CPU inference — a
maintenance trade-off, not a data-loss risk.

## Usage

    from adapters.common.ocr import get_reader, ocr_engine_status

    ok, detail = ocr_engine_status()             # cheap after the first call
    if ok:
        reader = get_reader()                    # cached; loads once per process
        detections = reader.readtext(image_np)   # image_np: a numpy array (H, W, 3)

`detections` is a list of `(bbox_points, text, confidence)` — EasyOCR's
native output shape, very different from pytesseract's `image_to_data`
dict. `group_detections_into_lines` below adapts it into pytesseract-style
"lines" for adapters (like the document adapter) that want one region per
visual line of text; `read_label` wraps the whole thing for a single small
crop (a diagram node/edge label).
"""

from __future__ import annotations

from typing import Iterable, Sequence

# English + Hindi/Devanagari by default: English because it's the lingua
# franca of most scanned business documents, Hindi/Devanagari because
# that's the second script this repo's own test fixtures actually exercise
# (tests/fixtures/note_scanned.pdf, built specifically to test multi-script
# handling — see tests/fixtures/generate_document_fixtures.py). This is a
# deliberately small default, not a claim that only these two scripts
# matter: EasyOCR supports 80+ out of the box, and any adapter/caller can
# pass a different `languages` list to `get_reader`/`ocr_engine_status` to
# cover a different mix. It is not read from an environment variable or
# config file (yet) — that would be a reasonable next step if this needs to
# vary per deployment rather than per call.
DEFAULT_LANGUAGES: list[str] = ["en", "hi"]

# Cache of instantiated easyocr.Reader objects, keyed by the exact language
# list requested. A Reader load is expensive (loads PyTorch model weights
# into memory; several seconds even when the weights are already
# downloaded), so this is built once per distinct language list per
# process and reused — never once per page or per label crop.
_READERS: dict[tuple[str, ...], object] = {}

# Cache of (available, detail) per language-list key, so repeated status
# checks (e.g. once per page, once per node/edge label) don't repeatedly
# retry a reader load that already failed, or redundantly confirm one that
# already succeeded.
_STATUS: dict[tuple[str, ...], tuple[bool, str]] = {}


def _key(languages: Sequence[str] | None) -> tuple[str, ...]:
    return tuple(languages) if languages else tuple(DEFAULT_LANGUAGES)


def get_reader(languages: Sequence[str] | None = None):
    """Returns a cached `easyocr.Reader` for the given language list
    (default `DEFAULT_LANGUAGES`), building and caching it on first use.
    Raises whatever `easyocr.Reader(...)` raises (e.g. a network error on
    the very first run, before models are cached locally, or an unknown
    language code) — callers that want the "never crash, report honestly"
    behavior should go through `ocr_engine_status()` first, exactly like
    the old `tesseract_status()` pattern.
    """
    key = _key(languages)
    if key not in _READERS:
        import easyocr  # imported lazily so importing this module never requires torch to already be loaded

        _READERS[key] = easyocr.Reader(list(key), gpu=False, verbose=False)
    return _READERS[key]


def ocr_engine_status(languages: Sequence[str] | None = None) -> tuple[bool, str]:
    """Returns (available, detail), probed once per distinct language list
    per process — cheap enough to not bother caching more aggressively,
    but there's no reason to reload a multi-hundred-MB set of model
    weights more than once for a fact that doesn't change mid-run.

    This actually builds the reader (which, on a completely fresh machine,
    means downloading its model weights) rather than just checking that
    the `easyocr` package imports — "the engine is available" should mean
    "we can actually OCR right now," the same real signal
    `tesseract_status()` used to check via
    `pytesseract.get_tesseract_version()`, not just `import pytesseract`
    succeeding.
    """
    key = _key(languages)
    if key not in _STATUS:
        try:
            get_reader(key)
            _STATUS[key] = (True, f"EasyOCR reader ready (languages: {', '.join(key)}, CPU)")
        except Exception as exc:  # easyocr/torch can raise many different types (network, torch, model-file)
            _STATUS[key] = (False, f"{type(exc).__name__}: {exc}")
    return _STATUS[key]


def _bbox_to_xyxy(bbox_points: Iterable[Iterable[float]]) -> tuple[float, float, float, float]:
    xs = [float(p[0]) for p in bbox_points]
    ys = [float(p[1]) for p in bbox_points]
    return min(xs), min(ys), max(xs), max(ys)


def group_detections_into_lines(
    detections: list[tuple], *, min_overlap_frac: float = 0.3
) -> list[list[tuple]]:
    """Groups EasyOCR's flat list of `(bbox_points, text, confidence)`
    detections into visual lines, the way Tesseract's own block/paragraph/
    line numbering used to for `pytesseract.image_to_data` — EasyOCR's
    detector (CRAFT) finds individual text boxes with no such grouping
    built in, so two words sitting on the same printed line (e.g. "Field
    note" and "inspection amendment" in `note_scanned.pdf`) come back as
    two separate detections unless something groups them back together.

    Two detections are the same line when their vertical extents overlap
    by more than `min_overlap_frac` of the shorter box's height — a
    simple, real geometric signal (not a hardcoded line count), robust to
    the small vertical jitter real OCR boxes have even for text a human
    would call "one line."

    Returns lines ordered top-to-bottom, each line a list of
    `(x0, y0, x1, y1, text, confidence)` tuples ordered left-to-right —
    empty/whitespace-only detections are dropped.
    """
    boxes = []
    for bbox_points, text, conf in detections:
        stripped = (text or "").strip()
        if not stripped:
            continue
        x0, y0, x1, y1 = _bbox_to_xyxy(bbox_points)
        boxes.append((x0, y0, x1, y1, stripped, float(conf)))
    boxes.sort(key=lambda b: (b[1] + b[3]) / 2)  # top-to-bottom by vertical center

    lines: list[list[tuple]] = []
    line_extents: list[tuple[float, float]] = []  # (min_y0, max_y1) covered so far, per line
    for box in boxes:
        x0, y0, x1, y1, _text, _conf = box
        placed = False
        for idx, (ly0, ly1) in enumerate(line_extents):
            overlap = min(y1, ly1) - max(y0, ly0)
            shorter_height = min(y1 - y0, ly1 - ly0)
            if shorter_height > 0 and (overlap / shorter_height) > min_overlap_frac:
                lines[idx].append(box)
                line_extents[idx] = (min(ly0, y0), max(ly1, y1))
                placed = True
                break
        if not placed:
            lines.append([box])
            line_extents.append((y0, y1))

    order = sorted(range(len(lines)), key=lambda i: line_extents[i][0])
    return [sorted(lines[i], key=lambda b: b[0]) for i in order]


def read_label(
    image_bgr, bbox: tuple[int, int, int, int], *, padding: int = 6, languages: Sequence[str] | None = None
) -> tuple[str | None, float]:
    """OCRs one small crop (a diagram node/edge label region) and returns
    `(text, confidence)` — `(None, 0.0)` if nothing was detected, matching
    the old pytesseract-based `_ocr_label`'s contract exactly so
    `adapters/diagram/adapter.py`'s call sites don't need to change shape.

    A label crop is small enough, and usually genuinely one line, that
    (unlike a full page) there's no need to keep lines visually separate —
    every detection in the crop is joined left-to-right/top-to-bottom into
    one string via `group_detections_into_lines`, and confidence is the
    mean of each detection's own EasyOCR confidence — a real per-label
    signal, not a hardcoded value.
    """
    ok, _ = ocr_engine_status(languages)
    if not ok:
        return None, 0.0
    x0, y0, x1, y1 = bbox
    h, w = image_bgr.shape[:2]
    x0, y0 = max(0, x0 - padding), max(0, y0 - padding)
    x1, y1 = min(w, x1 + padding), min(h, y1 + padding)
    crop = image_bgr[y0:y1, x0:x1]
    if crop.size == 0:
        return None, 0.0

    reader = get_reader(languages)
    detections = reader.readtext(crop)
    lines = group_detections_into_lines(detections)
    if not lines:
        return None, 0.0
    words = [b[4] for line in lines for b in line]
    confs = [b[5] for line in lines for b in line]
    return " ".join(words), round(sum(confs) / len(confs), 4)
