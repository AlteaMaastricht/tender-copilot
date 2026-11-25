"""Generate the synthetic document fixtures in tests/fixtures/documents/ (plan M4).

Synthetic Dutch text only, no real tender wording. Run once and commit the output:
    .venv/bin/python scripts/make_document_fixtures.py
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import docx
import openpyxl
import pymupdf
from docx.enum.style import WD_STYLE_TYPE

OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "documents"

PAGE_1 = "Dit is de eerste pagina van een synthetisch document over dataplatformen."
PAGE_2 = "Dit is de tweede pagina met eisen voor beveiliging en rapportage."


def _text_pdf(path: Path, pages: list[str | None], **save_kwargs) -> None:
    doc = pymupdf.open()
    for text in pages:
        page = doc.new_page()
        if text is None:  # an image-only page, as a scan would be
            pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
            pix.set_rect(pix.irect, (200, 200, 200))
            page.insert_image(pymupdf.Rect(72, 72, 272, 272), pixmap=pix)
        else:
            page.insert_text((72, 100), text, fontsize=11)
    doc.save(path, **save_kwargs)
    doc.close()


def make_pdfs() -> None:
    _text_pdf(OUT / "two_pages.pdf", [PAGE_1, PAGE_2])
    _text_pdf(OUT / "scan.pdf", [None, None, None])
    _text_pdf(OUT / "half_scan.pdf", [PAGE_1, PAGE_2, None, PAGE_1])
    _text_pdf(
        OUT / "encrypted.pdf",
        [PAGE_1],
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        user_pw="geheim",
        owner_pw="eigenaar",
    )
    _text_pdf(
        OUT / "owner_only.pdf",
        [PAGE_1],
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        user_pw="",
        owner_pw="eigenaar",
        permissions=int(pymupdf.PDF_PERM_PRINT),
    )
    doc = pymupdf.open()
    page = doc.new_page()
    # The built-in base-14 font has no ligature glyph; embed the font so U+FB01 survives.
    page.insert_font(fontname="lig", fontbuffer=pymupdf.Font("tiro").buffer)
    for y, line in (
        (100, "De ﬁnanciële afhandeling en de bevei-"),
        (120, "liging van de gegevens."),
    ):
        page.insert_text((72, y), line, fontname="lig", fontsize=11)
    doc.save(OUT / "ligature.pdf")
    doc.close()
    data = (OUT / "two_pages.pdf").read_bytes()
    (OUT / "truncated.pdf").write_bytes(data[: len(data) // 2])


def make_docx() -> None:
    d = docx.Document()
    kop2 = d.styles.add_style("Kop 2", WD_STYLE_TYPE.PARAGRAPH)
    kop2.base_style = d.styles["Normal"]
    d.add_paragraph("Inleiding zonder kop.")
    d.add_heading("Algemeen", level=1)
    d.add_paragraph("De opdrachtgever zoekt een partner voor datamigratie.")
    table = d.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Eis"
    table.cell(0, 1).text = "Waarde"
    table.cell(1, 0).text = "Beschikbaarheid"
    table.cell(1, 1).text = "99,9%"
    d.add_paragraph("Na de tabel volgt nog een alinea.")
    d.add_paragraph("Gunning", style="Kop 2")
    d.add_paragraph("De gunning vindt plaats op basis van beste prijs-kwaliteit.")
    d.save(OUT / "sections.docx")

    d = docx.Document()
    for i in range(3):
        d.add_paragraph(f"Alinea {i + 1} van een document zonder koppen.")
    d.save(OUT / "no_headings.docx")


def make_xlsx() -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Prijzen"
    ws.append(["Onderdeel", "Bedrag"])
    ws.append(["Licentie", 1200])
    ws.append([None, None])
    ws.append(["Totaal", "=SUM(B2:B2)"])  # formula without a cached value
    ws2 = wb.create_sheet("Planning")
    ws2.append(["Fase", "Start", None, "Einde"])
    ws2.append(["Start", "januari", None, "maart"])
    ws3 = wb.create_sheet("Intern")
    ws3.append(["Verborgen", "notitie"])
    ws3.sheet_state = "hidden"
    wb.save(OUT / "sheet.xlsx")


def make_other() -> None:
    (OUT / "old.doc").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 504)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("ppt/presentation.xml", "<p/>")
    (OUT / "slides.pptx").write_bytes(buf.getvalue())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("leesmij.txt", "synthetisch")
    (OUT / "plain.zip").write_bytes(buf.getvalue())


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    make_pdfs()
    make_docx()
    make_xlsx()
    make_other()


if __name__ == "__main__":
    main()
