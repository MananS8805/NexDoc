"""Generates the two document-adapter test fixtures used to exercise the
hybrid born-digital / scanned extraction path (see CLAUDE.md Phase 2).

Produces:
  tests/fixtures/invoice_born_digital.pdf
      A real, born-digital PDF built with reportlab: an actual text layer,
      no images. Two-language content (English + French, matching the UI
      mock's original invoice sample) and two line items with a grand total,
      formatted predictably enough for a deterministic line-item parser to
      read back — so the adapter's totals-vs-line-items check has something
      real to verify, the same way the spreadsheet fixture's SUM formulas do.

  tests/fixtures/note_scanned.pdf
      An image-only PDF: text is rendered onto a raster image (via Pillow)
      and that image is the *entire* page content — no text layer at all.
      This is what a real scanned page looks like to a PDF reader: it must
      go through OCR, there is nothing to extract directly. Two languages
      (English + Hindi, via Windows' bundled Nirmala.ttc, which covers
      Devanagari) so the adapter's language tagging has more than one script
      to actually distinguish.
"""

from __future__ import annotations

from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT_DIR = Path(__file__).parent
BORN_DIGITAL_PATH = OUT_DIR / "invoice_born_digital.pdf"
SCANNED_PATH = OUT_DIR / "note_scanned.pdf"

WINDOWS_FONTS = Path("C:/Windows/Fonts")


def build_born_digital_invoice() -> Path:
    c = canvas.Canvas(str(BORN_DIGITAL_PATH), pagesize=A4)
    width, height = A4

    c.setFont("Helvetica-Bold", 16)
    c.drawString(72, height - 72, "Lumière Textiles SARL")

    c.setFont("Helvetica", 11)
    y = height - 100
    lines = [
        "Facture N° 2024-A103 — Date: 14/08/2024",
        "Bill to: Anand Fabrics Pvt. Ltd., Ahmedabad",
        "",
        "Description                          Qty   Unit price   Line total",
        "Soie brute, ivoire                   200   14.50 EUR    2900.00 EUR",
        "Coton peigné                         500    3.20 EUR    1600.00 EUR",
        "",
        "Montant total: 4500.00 EUR",
    ]
    for line in lines:
        c.drawString(72, y, line)
        y -= 18

    c.showPage()
    c.save()
    return BORN_DIGITAL_PATH


def _load_font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont:
    for name in candidates:
        path = WINDOWS_FONTS / name
        if path.exists():
            return ImageFont.truetype(str(path), size)
    raise FileNotFoundError(f"none of {candidates} found under {WINDOWS_FONTS}")


def build_scanned_note() -> Path:
    # Render onto a raster image first — this is the entire "document" as
    # far as a PDF reader is concerned; no text object is ever created.
    img_w, img_h = 1240, 1754  # ~A4 at 150dpi
    image = Image.new("RGB", (img_w, img_h), "white")
    draw = ImageDraw.Draw(image)

    latin_font = _load_font(["arial.ttf"], 32)
    devanagari_font = _load_font(["Nirmala.ttc"], 36)

    draw.text((80, 80), "Field note — inspection amendment", font=latin_font, fill="black")
    draw.text((80, 160), "Signed: R. Anand — Date: 16/08/2024", font=latin_font, fill="black")
    draw.text((80, 260), "माल की जांच के बाद भुगतान होगा।", font=devanagari_font, fill="black")
    draw.text((80, 330), "(Payment will be made after goods inspection.)", font=latin_font, fill="black")

    doc = fitz.open()
    page = doc.new_page(width=img_w * 72 / 150, height=img_h * 72 / 150)
    tmp_img_path = OUT_DIR / "_note_scanned_tmp.png"
    image.save(tmp_img_path)
    page.insert_image(page.rect, filename=str(tmp_img_path))
    doc.save(SCANNED_PATH)
    doc.close()
    tmp_img_path.unlink()
    return SCANNED_PATH


if __name__ == "__main__":
    p1 = build_born_digital_invoice()
    p2 = build_scanned_note()
    print(f"Wrote {p1}")
    print(f"Wrote {p2}")
