"""The frozen snapshot: the only data an eval run reads (layout: docs/EVALS.md section 3).

    data/snapshot/
      manifest.json
      tenders/<publication_id>/notice.xml, tenderned.json, contacts.json, documents.json,
                               pages/<doc_key>.jsonl
      kb/...

The manifest lists every file with its SHA-256. `open_snapshot` re-hashes all of them and
refuses to run on any difference, so a results file can name exactly the data it was run on.
There is no network or database access here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from evals.metrics import AMSTERDAM, ExtractionFacts, LotFacts
from ingest.parsing import is_placeholder_deadline, parse_date
from ingest.ted import TedNotice, parse_notice
from pipeline.citations import PageText

MANIFEST = "manifest.json"
TENDERS = "tenders"
READABLE = "ok"


class SnapshotError(Exception):
    """The snapshot is missing, incomplete or differs from its manifest."""


@dataclass(frozen=True, slots=True)
class Snapshot:
    root: Path
    version: str
    sha256: str
    frozen_at: str | None
    kb_version: str | None
    tender_ids: tuple[int, ...]

    def summary(self) -> dict[str, Any]:
        return {"version": self.version, "sha256": self.sha256, "tenders": len(self.tender_ids)}


@dataclass(frozen=True, slots=True)
class SnapshotDocument:
    document_id: int
    doc_key: str  # first 12 hex characters of the file's SHA-256
    filename: str
    doc_type: str
    is_current: bool
    published_at: str | None
    extraction_status: str
    page_kind: str | None
    language: str | None
    pages: tuple[PageText, ...]


@dataclass(frozen=True, slots=True)
class TenderDocs:
    """What a document-reading step may see. No notice fields, no closing date, no contacts."""

    tender_id: int
    documents_access: str
    documents: tuple[SnapshotDocument, ...]

    @property
    def readable(self) -> bool:
        """False when the documents are elsewhere, or every one is scanned or unsupported."""
        return self.documents_access == "tenderned" and any(
            doc.extraction_status == READABLE and doc.pages for doc in self.documents
        )

    def doc_key(self, document_id: int) -> str | None:
        return next((d.doc_key for d in self.documents if d.document_id == document_id), None)

    def has_doc_key(self, doc_key: str) -> bool:
        return any(doc.doc_key == doc_key for doc in self.documents)


@dataclass(frozen=True, slots=True)
class SnapshotTender:
    publication_id: int
    notice: TedNotice
    tenderned: Mapping[str, Any] | None  # publication detail; None without a TenderNed match
    contact_names: tuple[str, ...]  # for the Redactor only
    docs: TenderDocs

    @property
    def sluitingsdatum(self) -> str | None:
        value = self.tenderned.get("sluitingsDatum") if self.tenderned else None
        return value if isinstance(value, str) else None


# --- manifest and hash ---------------------------------------------------------


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def files_hash(files: list[dict[str, Any]]) -> str:
    """The snapshot hash: SHA-256 of the file list as canonical JSON, sorted by path."""
    listing = sorted(
        ({"path": f["path"], "sha256": f["sha256"], "bytes": f["bytes"]} for f in files),
        key=lambda item: item["path"],
    )
    text = json.dumps(listing, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _data_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.name != ".DS_Store" and path != root / MANIFEST
    )


def build_manifest(
    root: Path, *, snapshot_version: str, frozen_at: str, kb_version: str | None = None
) -> dict[str, Any]:
    """The manifest for the files now under `root`. Returned, not written."""
    return {
        "snapshot_version": snapshot_version,
        "frozen_at": frozen_at,
        "kb_version": kb_version,
        "files": [
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": file_sha256(path),
                "bytes": path.stat().st_size,
            }
            for path in _data_files(root)
        ],
    }


def open_snapshot(root: Path) -> Snapshot:
    """Check every file against the manifest. Raises SnapshotError on any difference."""
    manifest_path = root / MANIFEST
    if not manifest_path.is_file():
        raise SnapshotError(f"no snapshot at {root}: {MANIFEST} not found")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        files = manifest["files"]
        version = str(manifest["snapshot_version"])
        listed = {str(entry["path"]): entry for entry in files}
    except (json.JSONDecodeError, KeyError, TypeError):
        raise SnapshotError(f"{MANIFEST} is not a valid manifest") from None

    for name, entry in listed.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise SnapshotError(f"{MANIFEST} lists a path outside the snapshot: {name}")
        path = root / relative
        if not path.is_file():
            raise SnapshotError(f"snapshot file missing: {name}")
        if path.stat().st_size != entry["bytes"] or file_sha256(path) != entry["sha256"]:
            raise SnapshotError(f"snapshot file changed since the freeze: {name}")
    for path in _data_files(root):
        name = path.relative_to(root).as_posix()
        if name not in listed:
            raise SnapshotError(f"file not in the manifest: {name}")

    tenders_dir = root / TENDERS
    tender_ids = sorted(
        int(folder.name)
        for folder in (tenders_dir.iterdir() if tenders_dir.is_dir() else ())
        if folder.is_dir() and folder.name.isdigit()
    )
    return Snapshot(
        root=root,
        version=version,
        sha256=files_hash(files),
        frozen_at=manifest.get("frozen_at"),
        kb_version=manifest.get("kb_version"),
        tender_ids=tuple(tender_ids),
    )


# --- loading one tender --------------------------------------------------------


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _pages(path: Path, document_id: int) -> tuple[PageText, ...]:
    if not path.is_file():
        return ()
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return tuple(PageText(document_id, int(row["page_no"]), str(row["text"])) for row in rows)


def load_tender(snapshot: Snapshot, publication_id: int) -> SnapshotTender:
    folder = snapshot.root / TENDERS / str(publication_id)
    if publication_id not in snapshot.tender_ids:
        raise SnapshotError(f"tender {publication_id} is not in the snapshot")
    try:
        notice = parse_notice((folder / "notice.xml").read_bytes())
        listing = _json(folder / "documents.json")
        documents = tuple(
            SnapshotDocument(
                document_id=int(row["document_id"]),
                doc_key=str(row["doc_key"]),
                filename=str(row["filename"]),
                doc_type=str(row["doc_type"]),
                is_current=bool(row["is_current"]),
                published_at=row.get("published_at"),
                extraction_status=str(row["extraction_status"]),
                page_kind=row.get("page_kind"),
                language=row.get("language"),
                pages=_pages(folder / "pages" / f"{row['doc_key']}.jsonl", int(row["document_id"])),
            )
            for row in listing["documents"]
        )
        detail_path, contacts_path = folder / "tenderned.json", folder / "contacts.json"
        detail = _json(detail_path) if detail_path.is_file() else None
        names = _json(contacts_path).get("names", []) if contacts_path.is_file() else []
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SnapshotError(
            f"tender {publication_id} cannot be read: {type(exc).__name__}"
        ) from None
    return SnapshotTender(
        publication_id=publication_id,
        notice=notice,
        tenderned=detail,
        contact_names=tuple(str(name) for name in names),
        docs=TenderDocs(publication_id, str(listing["documents_access"]), documents),
    )


# --- as-of date and ground truth ----------------------------------------------


def as_of_date(notice: TedNotice) -> date:
    """The day the daily job would first see the tender: the day after publication.

    The snapshot is historical, so the real date would fail every tender on its deadline.
    """
    published = notice.publication_date or notice.issue_date
    if published is None:
        raise SnapshotError(f"notice {notice.publication_id} has no publication date")
    return published + timedelta(days=1)


def deadline_truth(raw: str | None, published: date | None) -> tuple[datetime | None, bool]:
    """TenderNed `sluitingsDatum` as (moment, has a time). None for missing or placeholder."""
    text = raw.strip() if raw else ""
    day = parse_date(text) if text else None
    if day is None or is_placeholder_deadline(day, published):
        return None, False
    if len(text) > 10:
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            moment = None
        if moment is not None:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=AMSTERDAM)
            return moment, True
    return datetime.combine(day, time(0), AMSTERDAM), False


def ground_truth(tender: SnapshotTender) -> ExtractionFacts:
    """The graded values of one tender, from the notice and TenderNed. Never model input."""
    notice = tender.notice
    deadline, has_time = deadline_truth(
        tender.sluitingsdatum, notice.publication_date or notice.issue_date
    )

    def euros(value: Any, currency: str | None) -> Any:
        return value if currency == "EUR" else None

    return ExtractionFacts(
        deadline=deadline,
        deadline_has_time=has_time,
        lots=tuple(
            LotFacts(
                title=lot.title,
                estimated_value=euros(lot.estimated_value, lot.currency),
                duration_months=lot.duration_months,
            )
            for lot in notice.lots
        ),
        estimated_value=euros(notice.estimated_value, notice.currency),
        framework_max_value=euros(notice.framework_max_value, notice.currency),
    )
