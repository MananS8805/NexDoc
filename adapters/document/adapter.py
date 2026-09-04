"""Document adapter: .pdf -> common schema.

Hybrid extraction, per CLAUDE.md Phase 2: a born-digital page (one with a
real embedded text layer) is read directly — no OCR, no model call, just
PyMuPDF's own text-layer extraction. A scanned/image-only page has no text
layer at all, so it goes through OCR (pytesseract / Tesseract) instead.
Which path a page takes is decided by one real signal — the length of
`page.get_text()` — never guessed from the file extension or a hardcoded
per-file assumption.

Public entry points, matching adapters/spreadsheet/adapter.py's shape:
    parse_document(path)  -> generator yielding Region objects, one per
                              text block (born-digital) or OCR'd line
                              (scanned), in page then reading order.
    build_artifact(path)  -> dict, the full common-schema artifact.

Region classification (per page):
    born-digital page:
        - the first block of the whole document                 -> "title"
        - a block matching "<description>  <qty>  <price> <cur>
          <line total> <cur>"                                    -> "line-item"
        - a block matching "...total: <amount> <currency>"       -> "total-field"
        - a block containing both "description" and "qty"        -> "table-header"
        - anything else                                          -> "field"
    scanned page:
        - Tesseract available: one region per OCR'd line, grouped by
          Tesseract's own block/paragraph/line numbering           -> "ocr-line"
        - Tesseract not available on this machine: one region
          recording that the page could not be processed           -> "scanned-page-unprocessed"

Confidence:
    - born-digital blocks: a flat 0.98 for every block on that page. The
      real signal here is binary — "this page has an embedded text layer" —
      so there is exactly one honest confidence value to assign, the same
      way the spreadsheet adapter uses a flat 0.99 for literal cells (a
      direct, unambiguous read of an already-encoded value). It is not 0.99
      because PDF text-layer reading order can still be ambiguous in ways a
      stored cell value never is.
    - OCR lines: the mean of Tesseract's own per-word confidence for that
      line, divided by 100. This is the one place per-region confidence
      actually varies within a document, because it is a real signal that
      varies per line — never a hardcoded number.
    - a page Tesseract could not process at all: 0.0. Zero is not "very
      unconfident text" here, it is "no text was extracted, at all."

No LLM cleanup pass touches extracted text anywhere in this adapter, per
CLAUDE.md's guardrail.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Iterator

import fitz  # PyMuPDF
import pytesseract
from langdetect import LangDetectException, detect
from PIL import Image
from pytesseract import Output

from adapters.common.ocr import tesseract_status
from adapters.common.schema import Artifact, Check, Region, Relationship, Source, sha256_of_file

ADAPTER_NAME = "Document adapter"
ADAPTER_VERSION = "0.1.0"

CONFIDENCE_TEXT_LAYER = 0.98
CONFIDENCE_OCR_UNAVAILABLE = 0.0
MIN_TEXT_LAYER_CHARS = 20  # below this, a page is treated as scanned/image-only
MIN_LANG_DETECT_CHARS = 15  # below this, language detection is too unreliable to report

# Structured/tabular region types (a line-item row, a totals line, a table
# header) aren't natural-language prose — they're records that happen to
# embed a product name or a label. Running a statistical language detector
# on "Soie brute, ivoire   200   14.50 EUR   2900.00 EUR" is a category
# error, not a hard case to get right; language tagging is skipped for
# these types rather than reported unreliably. "field"/"title"/"ocr-line"
# (genuine prose or free-text fields) still get detection.
NON_LINGUISTIC_TYPES = {"line-item", "total-field", "table-header"}

LINE_ITEM_RE = re.compile(
    r"^(?P<description>.+?)\s{2,}(?P<qty>\d+)\s+(?P<unit_price>[\d.]+)\s*(?P<cur1>[A-Za-z]{2,4})\s+"
    r"(?P<line_total>[\d.]+)\s*(?P<cur2>[A-Za-z]{2,4})\s*$"
)
TOTAL_RE = re.compile(r"(?:total|montant total|grand total)\s*:?\s*(?P<amount>[\d.]+)\s*[A-Za-z]{2,4}", re.IGNORECASE)
DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")

_NON_LINGUISTIC_RE = re.compile(r"[\d°%€$£/:.\-—()]+")


def _detect_language(text: str) -> str | None:
    """Strips digits, currency/date punctuation, etc. before detecting —
    langdetect is a real statistical n-gram model, and feeding it a string
    that's mostly numbers and currency codes (a date, a price line) makes it
    genuinely misfire, not because the model is broken but because there's
    barely any linguistic signal left once you remove the numbers. This is
    an honest data-cleaning step for the detector's input, not a rule about
    what counts as text — `Region.text` itself is never touched.
    """
    cleaned = _NON_LINGUISTIC_RE.sub(" ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) < MIN_LANG_DETECT_CHARS:
        return None
    try:
        return detect(cleaned)
    except LangDetectException:
        return None


def _classify_born_digital_block(is_first_block_of_document: bool, text: str) -> str:
    stripped = text.strip()
    if is_first_block_of_document:
        return "title"
    if LINE_ITEM_RE.match(stripped):
        return "line-item"
    if TOTAL_RE.search(stripped):
        return "total-field"
    lowered = stripped.lower()
    if "description" in lowered and "qty" in lowered:
        return "table-header"
    return "field"


def _extract_born_digital(page, is_first_page_of_document: bool) -> Iterator[Region]:
    blocks = [b for b in page.get_text("blocks") if b[4].strip()]
    open_line_item_ids: list[str] = []
    for i, (x0, y0, x1, y1, text, block_no, block_type) in enumerate(blocks):
        is_first_block = is_first_page_of_document and i == 0
        region_type = _classify_born_digital_block(is_first_block, text)
        region_id = f"page{page.number + 1}!block{block_no}"
        stripped = text.strip()

        relationships = []
        if region_type == "line-item":
            open_line_item_ids.append(region_id)
        elif region_type == "table-header":
            open_line_item_ids = []
        elif region_type == "total-field":
            relationships = [Relationship("sums-line-items", iid) for iid in open_line_item_ids]
            open_line_item_ids = []

        language = None if region_type in NON_LINGUISTIC_TYPES else _detect_language(stripped)
        yield Region(
            id=region_id,
            type=region_type,
            confidence=CONFIDENCE_TEXT_LAYER,
            text=stripped,
            language=language,
            relationships=relationships,
        )


def _ocr_page(page) -> Iterator[Region]:
    pix = page.get_pixmap(dpi=200)
    image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    data = pytesseract.image_to_data(image, output_type=Output.DICT)

    lines: dict[tuple[int, int, int], dict] = {}
    for i in range(len(data["text"])):
        word = data["text"][i].strip()
        conf = int(data["conf"][i])
        if not word or conf < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        line = lines.setdefault(key, {"words": [], "confs": []})
        line["words"].append(word)
        line["confs"].append(conf)

    for idx, key in enumerate(sorted(lines)):
        line = lines[key]
        text = " ".join(line["words"])
        confidence = (sum(line["confs"]) / len(line["confs"])) / 100
        yield Region(
            id=f"page{page.number + 1}!ocr-line{idx + 1}",
            type="ocr-line",
            confidence=round(confidence, 4),
            text=text,
            language=_detect_language(text),
        )


def _ocr_unavailable_region(page) -> Region:
    return Region(
        id=f"page{page.number + 1}!ocr-unavailable",
        type="scanned-page-unprocessed",
        confidence=CONFIDENCE_OCR_UNAVAILABLE,
        text=None,
    )


def _iter_regions(doc: fitz.Document) -> Iterator[Region]:
    tesseract_ok, _ = tesseract_status()
    for page in doc:
        text_layer = page.get_text().strip()
        if len(text_layer) >= MIN_TEXT_LAYER_CHARS:
            yield from _extract_born_digital(page, is_first_page_of_document=(page.number == 0))
        elif tesseract_ok:
            yield from _ocr_page(page)
        else:
            yield _ocr_unavailable_region(page)


def parse_document(path: str | Path) -> Iterator[Region]:
    """Streaming entry point, matching parse_spreadsheet's shape."""
    doc = fitz.open(path)
    try:
        yield from _iter_regions(doc)
    finally:
        doc.close()


def _check_totals_match_line_items(regions: list[Region]) -> Check:
    totals = [r for r in regions if r.type == "total-field"]
    if not totals:
        return Check("total-matches-line-items", True, "no total field found in this document")

    by_id = {r.id: r for r in regions}
    parts = []
    all_ok = True
    for total_region in totals:
        m = TOTAL_RE.search(total_region.text or "")
        if not m:
            continue
        total_value = float(m.group("amount"))
        item_ids = [rel.target for rel in total_region.relationships if rel.type == "sums-line-items"]
        item_sum = 0.0
        for iid in item_ids:
            item = by_id.get(iid)
            im = LINE_ITEM_RE.match(item.text.strip()) if item and item.text else None
            if im:
                item_sum += float(im.group("line_total"))
        ok = abs(item_sum - total_value) < 0.01
        all_ok = all_ok and ok
        parts.append(f"{total_region.id}: line items sum={item_sum:.2f}, extracted total={total_value:.2f}")

    return Check("total-matches-line-items", all_ok, "; ".join(parts) if parts else "no parseable total value found")


def _check_dates_parse(regions: list[Region]) -> Check:
    found = []
    for r in regions:
        if not r.text:
            continue
        for d, mo, y in DATE_RE.findall(r.text):
            y4 = y if len(y) == 4 else f"20{y}"
            raw = f"{d}/{mo}/{y4}"
            try:
                datetime.strptime(raw, "%d/%m/%Y")
                found.append((r.id, raw, True))
            except ValueError:
                found.append((r.id, raw, False))

    if not found:
        return Check("date-fields-parse", True, "no date-like fields found")
    all_ok = all(ok for _, _, ok in found)
    detail = "; ".join(f"{rid}: '{raw}' {'valid' if ok else 'INVALID'}" for rid, raw, ok in found)
    return Check("date-fields-parse", all_ok, detail)


def _check_ocr_engine_available(regions: list[Region]) -> Check:
    ok, detail = tesseract_status()
    scanned_unprocessed = sum(1 for r in regions if r.type == "scanned-page-unprocessed")
    if ok:
        return Check("ocr-engine-available", True, f"Tesseract {detail} available")
    if scanned_unprocessed:
        return Check(
            "ocr-engine-available",
            False,
            f"Tesseract not available ({detail}) — {scanned_unprocessed} scanned page(s) could not be processed",
        )
    return Check(
        "ocr-engine-available",
        False,
        f"Tesseract not available ({detail}) — no effect on this document, it has no scanned pages",
    )


def run_checks(regions: list[Region]) -> list[Check]:
    return [
        _check_totals_match_line_items(regions),
        _check_dates_parse(regions),
        _check_ocr_engine_available(regions),
    ]


def build_artifact(path: str | Path) -> dict:
    """Runs the full pipeline and returns the common-schema artifact as a dict."""
    path = Path(path)
    doc = fitz.open(path)
    try:
        regions = list(_iter_regions(doc))
    finally:
        doc.close()
    checks = run_checks(regions)
    source = Source(path=str(path), hash=sha256_of_file(path), adapter_version=ADAPTER_VERSION)
    artifact = Artifact(
        artifact=path.stem,
        kind="document",
        adapter=ADAPTER_NAME,
        source=source,
        regions=regions,
        checks=checks,
    )
    return artifact.to_dict()
