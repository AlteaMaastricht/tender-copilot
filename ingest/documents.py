"""Document helpers (docs/plans/documents.md D4). So far: format detection and name grouping.

The fetcher itself is added to this file later.
"""

from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from pathlib import PurePath

_OOXML_FOLDERS = (("word/", "docx"), ("ppt/", "pptx"), ("xl/", "xlsx"))
_EXTENSIONS = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".doc": "doc",
    ".pptx": "pptx",
    ".xlsx": "xlsx",
    ".zip": "zip",
}
_DOCUMENT_EXTENSIONS = frozenset(
    {".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xlsm", ".xls", ".zip", ".odt", ".ods",
     ".odp", ".rtf", ".txt", ".csv", ".xml", ".html", ".htm", ".msg", ".eml", ".jpg", ".jpeg",
     ".png", ".tif", ".tiff", ".7z", ".rar"}
)  # fmt: skip
_PDF_WINDOW = 1024  # a PDF header may follow a few junk bytes
_SPACES = re.compile(r"\s+")


def normalise_name(name: str) -> str:
    """The `version_group` of a file name: NFKC, lower case, no extension, one space."""
    text = unicodedata.normalize("NFKC", name).strip()
    suffix = PurePath(text).suffix
    if suffix.lower() in _DOCUMENT_EXTENSIONS:
        text = text[: -len(suffix)]
    return _SPACES.sub(" ", text.lower()).strip()


def _zip_kind(head: bytes) -> str:
    """docx, pptx, xlsx or zip, from the member names (the whole file, or just its head)."""
    try:
        with zipfile.ZipFile(io.BytesIO(head)) as archive:
            names = archive.namelist()
    except (zipfile.BadZipFile, OSError, ValueError):
        # Only the start of the file: member names sit in the local headers.
        names = [m.decode("latin-1") for m in re.findall(rb"(?:word|ppt|xl)/[\w./-]*", head)]
    for folder, kind in _OOXML_FOLDERS:
        if any(n.startswith(folder) for n in names):
            return kind
    return "zip"


def file_format(name: str, head: bytes) -> str:
    """pdf, docx, doc, pptx, xlsx, zip or other: by magic bytes first, then by extension.

    A ZIP is told apart by its member folders, which needs the whole file when the central
    directory is past `head`; a truncated head falls back to the names in the local headers.
    OLE files count as `doc` only when the name ends in `.doc`.
    """
    extension = PurePath(name).suffix.lower()
    if head.startswith(b"PK\x03\x04"):
        return _zip_kind(head)
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "doc" if extension == ".doc" else "other"
    if b"%PDF" in head[:_PDF_WINDOW]:
        return "pdf"
    if extension == ".doc":
        return "other"  # no OLE header: not a Word file we can convert
    return _EXTENSIONS.get(extension, "other")
