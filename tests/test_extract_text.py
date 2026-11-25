import io
from pathlib import Path

import openpyxl

from ingest.extract_text import (
    SECTION_MAX_CHARS,
    SHEET_MAX_CELLS,
    clean_text,
    extract,
    extract_docx,
    extract_pdf,
    extract_xlsx,
)
from pipeline.citations import PageText, verify_citation
from pipeline.schemas import Citation

FIX = Path(__file__).parent / "fixtures" / "documents"


def test_two_pages_exact_text():
    result = extract_pdf(FIX / "two_pages.pdf")
    assert result.status == "ok"
    assert result.page_kind == "page"
    assert [p.page_no for p in result.pages] == [1, 2]
    assert result.pages[0].text == (
        "Dit is de eerste pagina van een synthetisch document over dataplatformen."
    )
    assert result.pages[1].text == (
        "Dit is de tweede pagina met eisen voor beveiliging en rapportage."
    )
    assert result.notes == ()


def test_scan_is_scanned_and_keeps_empty_pages():
    result = extract_pdf(FIX / "scan.pdf")
    assert result.status == "scanned"
    assert len(result.pages) == 3
    assert all(p.text == "" for p in result.pages)


def test_half_scan_notes_pages_without_text():
    result = extract_pdf(FIX / "half_scan.pdf")
    assert result.status == "ok"
    assert result.notes == ("pages_without_text:1",)
    assert result.pages[2].text == ""


def test_encrypted_and_owner_only():
    assert extract_pdf(FIX / "encrypted.pdf").status == "encrypted"
    assert extract_pdf(FIX / "owner_only.pdf").status == "ok"


def test_ligature_kept_and_quote_verifies():
    result = extract_pdf(FIX / "ligature.pdf")
    text = result.pages[0].text
    assert "ﬁ" in text
    assert "bevei-\nliging" in text
    page = PageText(document_id=1, page_no=1, text=text)
    citation = Citation(
        document_id=1,
        page=1,
        quote="De ﬁnanciële afhandeling en de beveiliging van de gegevens.",
    )
    assert verify_citation(citation, page).passed


def test_nul_removed():
    assert clean_text("a\x00b\x00") == "ab"
    assert clean_text("  a\n b ") == "  a\n b "


def test_truncated_pdf_fails_without_raising(tmp_path):
    assert extract_pdf(FIX / "truncated.pdf").status == "failed"
    missing = extract_pdf(tmp_path / "missing.pdf")
    assert missing.status == "failed"
    junk = tmp_path / "junk.pdf"
    junk.write_bytes(b"not a pdf")
    assert extract(junk, "pdf").status == "failed"


def test_docx_sections_order_labels_and_table():
    result = extract_docx(FIX / "sections.docx")
    assert result.status == "ok"
    assert result.page_kind == "section"
    assert [p.page_label for p in result.pages] == [None, "Algemeen", "Gunning"]
    assert result.pages[0].text == "Inleiding zonder kop."
    algemeen = result.pages[1]
    assert "Eis | Waarde\nBeschikbaarheid | 99,9%" in algemeen.text
    assert [p.has_table for p in result.pages] == [False, True, False]
    assert result.pages[2].text.startswith("Gunning\n")


def test_docx_no_headings_gives_unlabelled_sections():
    result = extract_docx(FIX / "no_headings.docx")
    assert result.status == "ok"
    assert all(p.page_label is None for p in result.pages)
    assert "Alinea 3" in result.pages[-1].text


def test_docx_long_section_splits_in_three(tmp_path):
    import docx

    d = docx.Document()
    d.add_heading("Lang", level=1)
    for i in range(30):
        d.add_paragraph(f"{i:02d} " + "x" * 497)  # 30 paragraphs, about 15,000 characters
    path = tmp_path / "long.docx"
    d.save(path)
    result = extract_docx(path)
    assert [p.page_label for p in result.pages] == ["Lang", "Lang (cont.)", "Lang (cont.)"]
    assert all(len(p.text) <= SECTION_MAX_CHARS for p in result.pages)
    assert [p.page_no for p in result.pages] == [1, 2, 3]


def test_docx_broken_fails(tmp_path):
    path = tmp_path / "bad.docx"
    path.write_bytes(b"PK\x03\x04 broken")
    assert extract_docx(path).status == "failed"


def test_xlsx_sheets_hidden_and_formula():
    result = extract_xlsx(FIX / "sheet.xlsx")
    assert result.status == "ok"
    assert result.page_kind == "sheet"
    assert [p.page_label for p in result.pages] == ["Prijzen", "Planning", "Intern (hidden)"]
    assert result.pages[0].text == "Onderdeel | Bedrag\nLicentie | 1200\nTotaal"
    assert result.pages[1].text == "Fase | Start |  | Einde\nStart | januari |  | maart"
    assert all(p.has_table for p in result.pages)
    assert result.notes == ()


def test_xlsx_big_sheet_is_cut(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Groot"
    for r in range(3000):
        ws.append([f"r{r}c{c}" for c in range(10)])  # 30,000 cells
    path = tmp_path / "big.xlsx"
    buffer = io.BytesIO()
    wb.save(buffer)
    path.write_bytes(buffer.getvalue())
    result = extract_xlsx(path)
    assert result.notes == ("sheet_cut:Groot",)
    assert len(result.pages[0].text.splitlines()) == SHEET_MAX_CELLS // 10


def test_xlsx_broken_fails(tmp_path):
    path = tmp_path / "bad.xlsx"
    path.write_bytes(b"nope")
    assert extract_xlsx(path).status == "failed"


def test_other_formats_unsupported():
    for name, fmt in [("old.doc", "doc"), ("slides.pptx", "pptx"), ("plain.zip", "zip")]:
        result = extract(FIX / name, fmt)
        assert result.status == "unsupported"
        assert result.pages == ()


def test_xlsx_without_extension_opens(tmp_path):
    for name in ("bijlage", "x.1", "x.2b"):
        path = tmp_path / name
        path.write_bytes((FIX / "sheet.xlsx").read_bytes())
        result = extract_xlsx(path)
        assert result.status == "ok"
        assert [p.page_label for p in result.pages] == ["Prijzen", "Planning", "Intern (hidden)"]
