from pathlib import Path

import pytest

from ingest.documents import file_format, normalise_name

FIX = Path(__file__).parent / "fixtures" / "documents"


def head(name: str) -> bytes:
    return (FIX / name).read_bytes()


def test_pdf_named_docx_is_pdf():
    assert file_format("bijlage.docx", head("two_pages.pdf")) == "pdf"


def test_docx_named_zip_is_docx():
    assert file_format("bijlage.zip", head("sections.docx")) == "docx"


def test_no_extension_decided_by_magic():
    assert file_format("bijlage", head("two_pages.pdf")) == "pdf"
    assert file_format("bijlage", head("sheet.xlsx")) == "xlsx"
    assert file_format("bijlage", head("slides.pptx")) == "pptx"


def test_plain_zip_is_zip_whatever_its_name():
    assert file_format("stukken", head("plain.zip")) == "zip"
    assert file_format("stukken.pdf", head("plain.zip")) == "zip"


def test_truncated_zip_head_uses_local_header_names():
    assert file_format("x", head("sections.docx")[:3000]) == "docx"


def test_ole_is_doc_only_with_doc_extension():
    assert file_format("oud.doc", head("old.doc")) == "doc"
    assert file_format("oud.xls", head("old.doc")) == "other"
    assert file_format("oud", head("old.doc")) == "other"


@pytest.mark.parametrize(
    ("name", "expected"),
    [("a.pdf", "pdf"), ("a.DOCX", "docx"), ("a.xlsx", "xlsx"), ("a.txt", "other"), ("a", "other")],
)
def test_extension_fallback(name, expected):
    assert file_format(name, b"") == expected


def test_normalise_name():
    assert normalise_name("Nota van Inlichtingen 1.pdf") == normalise_name(
        "nota van  inlichtingen 1.PDF"
    )
    assert normalise_name("Nota van Inlichtingen 1.pdf") == "nota van inlichtingen 1"
    assert normalise_name("  ﬁche  A.docx ") == "fiche a"
    assert normalise_name("zonder extensie") == "zonder extensie"


def test_zip_holding_a_stored_pdf_is_zip():
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("two_pages.pdf", head("two_pages.pdf"))
    assert file_format("x.pdf", buf.getvalue()) == "zip"


def test_normalise_name_keeps_dotted_numbers():
    assert normalise_name("Bijlage 2.1") != normalise_name("Bijlage 2.2")
    assert normalise_name("Bijlage 2.1.pdf") == "bijlage 2.1"
