"""The naive baseline the structured document adapter is measured against.

Reads the same PDF with the same primitive the real adapter uses for a
born-digital page (`page.get_text("blocks")`), but does none of the
adapter's own work: no title/field/table-header/line-item/total-field
classification, no `sums-line-items` relationship inference, no language
detection, and — importantly — no OCR fallback for a scanned page (a page
with no text layer yields nothing at all here, which is the real, worse
failure mode a truly generic "just extract the text" pipeline hits, not a
strawman).

Verified directly (see docs/eval-methodology.md): for a born-digital PDF,
this naive extraction keeps almost every character the structured adapter
also keeps — `page.get_text("blocks")` on tests/fixtures/invoice_born_digital.pdf
is line-for-line identical to what `adapters.common.outputs.to_text()`
produces from the real adapter's regions. That's a real, important
difference from the spreadsheet baseline (which is genuinely blank for
every formula cell): a document baseline does not lose facts by being
flattened, it loses semantic role and structural relationships. The eval
built against this baseline is designed around that honest distinction,
not around forcing a low score.

Each block is emitted under a "# region: <id>" marker using the exact same
`pageN!blockM` id scheme the real adapter uses (see
adapters/document/adapter.py's `_extract_born_digital`) — this mirrors how
`adapters/spreadsheet/baseline.py`'s `flatten_to_csv` keeps the row/column
grid: a structural, not semantic, addressing scheme that lets an eval
question name the same location in both representations.
"""

from __future__ import annotations

from pathlib import Path

import fitz  # PyMuPDF


def flatten_to_text(path: str | Path) -> str:
    """One '# region: pageN!blockM' marker + raw text per PyMuPDF block, in
    page then reading order. A page with no text layer (would need OCR)
    contributes nothing — this baseline never falls back to OCR, unlike the
    real adapter.
    """
    doc = fitz.open(path)
    try:
        out = []
        for page in doc:
            for x0, y0, x1, y1, text, block_no, block_type in page.get_text("blocks"):
                stripped = text.strip()
                if not stripped:
                    continue
                out.append(f"# region: page{page.number + 1}!block{block_no}")
                out.append(stripped)
                out.append("")
        return "\n".join(out)
    finally:
        doc.close()
