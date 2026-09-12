"""Generates a second born-digital document fixture, sibling to
generate_document_fixtures.py, specifically a genuinely multi-page PDF —
every existing document fixture is single-page, so the multi-page input-
preview rendering path (stacking N pages in the webapp UI) had never been
exercised against a real file before this was built to diagnose a reported
overlap/misalignment bug there.

Produces tests/fixtures/invoice_two_pages.pdf: page 1 mirrors
invoice_born_digital.pdf's shape (title, fields, line items, total); page 2
is a deliberately different shape/size block of text, so the two pages'
real aspect ratios and content layouts differ — the bug this reproduces
only shows up when pages aren't uniform.
"""

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

OUT_PATH = Path(__file__).parent / "invoice_two_pages.pdf"


def build() -> Path:
    c = canvas.Canvas(str(OUT_PATH), pagesize=A4)
    width, height = A4

    c.setFont("Helvetica-Bold", 16)
    c.drawString(72, height - 72, "Lumière Textiles SARL")
    c.setFont("Helvetica", 11)
    y = height - 100
    for line in [
        "Facture N° 2024-A104 — Date: 01/09/2026",
        "Bill to: Anand Fabrics Pvt. Ltd., Ahmedabad",
        "",
        "Description                          Qty   Unit price   Line total",
        "Soie brute, ivoire                   200   14.50 EUR    2900.00 EUR",
        "Coton peigné                         500    3.20 EUR    1600.00 EUR",
        "",
        "Montant total: 4500.00 EUR",
    ]:
        c.drawString(72, y, line)
        y -= 18
    c.showPage()

    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, height - 72, "Terms and conditions")
    c.setFont("Helvetica", 10)
    y = height - 110
    for i in range(1, 21):
        c.drawString(72, y, f"{i}. Clause {i} — payment due within 30 days of invoice date.")
        y -= 16
    c.showPage()

    c.save()
    return OUT_PATH


if __name__ == "__main__":
    path = build()
    print(f"Wrote {path}")
