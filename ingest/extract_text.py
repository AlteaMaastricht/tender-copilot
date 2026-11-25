"""Text extraction per format (docs/plans/documents.md D5): PDF, DOCX and XLSX.

The extractor never raises for a bad file; it returns status `failed` with the exception
class as a note. Stored text is what the citation check reads, so the only change made to it
is removing NUL characters.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SCANNED_PAGE_MIN_CHARS = 20  # non-whitespace characters for a page to count as having text
SCANNED_SHARE = 0.8  # a PDF is scanned when this share of its pages has no text
SECTION_MAX_CHARS = 6_000  # DOCX sections are split at paragraph boundaries above this
SHEET_MAX_CELLS = 20_000

_HEADING_NAMES = {"heading 1", "heading 2", "kop 1", "kop 2", "title", "titel"}
_HEADING_IDS = {"heading1", "heading2", "kop1", "kop2", "title", "titel"}


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    page_no: int  # 1-based
    page_label: str | None
    text: str
    has_table: bool


@dataclass(frozen=True, slots=True)
class Extraction:
    status: Literal["ok", "scanned", "unsupported", "encrypted", "failed"]
    page_kind: Literal["page", "slide", "sheet", "section"] | None
    pages: tuple[ExtractedPage, ...]
    notes: tuple[str, ...]


def clean_text(text: str) -> str:
    """Remove NUL characters and nothing else."""
    return text.replace("\x00", "")


def _failed(exc: BaseException) -> Extraction:
    return Extraction("failed", None, (), (type(exc).__name__,))


def extract(path: Path, file_format: str) -> Extraction:
    if file_format == "pdf":
        return extract_pdf(path)
    if file_format == "docx":
        return extract_docx(path)
    if file_format == "xlsx":
        return extract_xlsx(path)
    return Extraction("unsupported", None, (), ())


# --- PDF ------------------------------------------------------------------------


def extract_pdf(path: Path) -> Extraction:
    try:
        return _extract_pdf(path)
    except Exception as exc:  # a bad file must never raise
        return _failed(exc)


def _extract_pdf(path: Path) -> Extraction:
    import pymupdf

    with pymupdf.open(path) as doc:
        if doc.needs_pass:
            return Extraction("encrypted", None, (), ())
        pages: list[ExtractedPage] = []
        for index, page in enumerate(doc):
            text = clean_text(page.get_text("text", sort=True))
            try:
                has_table = bool(page.find_tables().tables)
            except Exception:
                has_table = False
            label = clean_text(page.get_label() or "") or None
            pages.append(ExtractedPage(index + 1, label, text, has_table))
    if not pages:
        return Extraction("failed", None, (), ("no_pages",))
    empty = sum(1 for p in pages if len("".join(p.text.split())) < SCANNED_PAGE_MIN_CHARS)
    if empty / len(pages) >= SCANNED_SHARE:
        return Extraction("scanned", "page", tuple(pages), ())
    notes = (f"pages_without_text:{empty}",) if empty else ()
    return Extraction("ok", "page", tuple(pages), notes)


# --- DOCX -----------------------------------------------------------------------


@dataclass(slots=True)
class _Unit:
    text: str
    is_table: bool = False


def extract_docx(path: Path) -> Extraction:
    try:
        return _extract_docx(path)
    except Exception as exc:
        return _failed(exc)


def _is_heading(paragraph) -> bool:
    style = paragraph.style
    if style is None:
        return False
    name = (style.name or "").strip().lower()
    style_id = (style.style_id or "").strip().lower()
    return name in _HEADING_NAMES or style_id in _HEADING_IDS


def _body_units(document) -> Iterator[tuple[str, str | _Unit]]:
    """("heading", text) or ("unit", _Unit) in document order."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            paragraph = Paragraph(child, document)
            text = clean_text(paragraph.text).strip()
            if not text:
                continue
            if _is_heading(paragraph):
                yield "heading", text
            else:
                yield "unit", _Unit(text)
        elif tag == "tbl":
            for row in Table(child, document).rows:
                cells = [clean_text(cell.text).replace("\n", " ").strip() for cell in row.cells]
                if any(cells):
                    yield "unit", _Unit(" | ".join(cells), True)


def _split_units(units: list[_Unit]) -> list[list[_Unit]]:
    """Group units into chunks of at most SECTION_MAX_CHARS, cutting at unit boundaries."""
    chunks: list[list[_Unit]] = []
    current: list[_Unit] = []
    size = 0
    for unit in units:
        pieces = [unit]
        if len(unit.text) > SECTION_MAX_CHARS:  # one huge paragraph: hard cut
            text = unit.text
            pieces = [
                _Unit(text[i : i + SECTION_MAX_CHARS], unit.is_table)
                for i in range(0, len(text), SECTION_MAX_CHARS)
            ]
        for piece in pieces:
            added = len(piece.text) + (1 if current else 0)
            if current and size + added > SECTION_MAX_CHARS:
                chunks.append(current)
                current, size, added = [], 0, len(piece.text)
            current.append(piece)
            size += added
    if current:
        chunks.append(current)
    return chunks


def _extract_docx(path: Path) -> Extraction:
    import docx

    document = docx.Document(str(path))
    sections: list[tuple[str | None, list[_Unit]]] = [(None, [])]
    for kind, item in _body_units(document):
        if kind == "heading":
            sections.append((str(item), [_Unit(str(item))]))
        else:
            assert isinstance(item, _Unit)
            sections[-1][1].append(item)

    pages: list[ExtractedPage] = []
    for label, units in sections:
        for number, chunk in enumerate(_split_units(units)):
            chunk_label = f"{label} (cont.)" if label is not None and number > 0 else label
            pages.append(
                ExtractedPage(
                    len(pages) + 1,
                    chunk_label,
                    "\n".join(u.text for u in chunk),
                    any(u.is_table for u in chunk),
                )
            )
    return Extraction("ok", "section", tuple(pages), ())


# --- XLSX -----------------------------------------------------------------------


def extract_xlsx(path: Path) -> Extraction:
    try:
        return _extract_xlsx(path)
    except Exception as exc:
        return _failed(exc)


def _cell_text(value: object) -> str:
    return "" if value is None else clean_text(str(value))


def _extract_xlsx(path: Path) -> Extraction:
    import openpyxl

    # From bytes: openpyxl refuses file names without an Excel extension.
    workbook = openpyxl.load_workbook(io.BytesIO(path.read_bytes()), read_only=True, data_only=True)
    pages: list[ExtractedPage] = []
    notes: list[str] = []
    try:
        for sheet in workbook.worksheets:
            lines: list[str] = []
            count = 0
            cut = False
            for row in sheet.iter_rows(values_only=True):
                cells = [_cell_text(v) for v in row]
                while cells and not cells[-1]:
                    cells.pop()
                if not cells:
                    continue
                filled = sum(1 for c in cells if c)
                if count + filled > SHEET_MAX_CELLS:
                    cut = True
                    break
                count += filled
                lines.append(" | ".join(cells))
            if cut:
                notes.append(f"sheet_cut:{sheet.title}")
            hidden = sheet.sheet_state != "visible"
            label = f"{sheet.title} (hidden)" if hidden else sheet.title
            pages.append(ExtractedPage(len(pages) + 1, label, "\n".join(lines), True))
    finally:
        workbook.close()
    return Extraction("ok", "sheet", tuple(pages), tuple(notes))
