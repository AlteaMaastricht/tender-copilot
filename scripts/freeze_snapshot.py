"""Freeze the eval snapshot from hand-downloaded documents (plan M6, D9 subset).

Usage:
    python scripts/freeze_snapshot.py --from-manual data/raw/manual --out data/snapshot \
        --version v1-manual-2026-11-26 [--ids data/snapshot_ids.txt] [--limit N]
        [--details-only] [--force] [--max-requests 150]

Tenders come from the ID list (`data/snapshot_ids.txt`), not from the folders that exist.
Network (anonymous GET, https only, no redirects, 2 s apart, no retries, no document
downloads): per tender the TenderNed detail, the rectification when there is one, and the TED
notice XML. Everything fetched is cached under `data/raw/snapshot/<id>/` (contact data
removed before it is written), so a rerun makes only the missing requests.

Documents are read from `<from-manual>/<id>/`. ZIP files (found by content) are unpacked into
the cache folder, never into the manual folder; the archive is a container record, not a
document. Nothing read from a file is imported or run. The report holds counts, file names of
ZIP, unsupported and duplicate files, and no document text or personal names.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import zipfile
import zlib
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from evals.snapshot import MANIFEST, SnapshotError, build_manifest, open_snapshot  # noqa: E402
from ingest.classify import classify_by_rule  # noqa: E402
from ingest.documents import file_format  # noqa: E402
from ingest.extract_text import extract  # noqa: E402
from ingest.http import DEFAULT_MAX_REQUESTS, HttpClient, HttpError  # noqa: E402
from ingest.ted import (  # noqa: E402
    TedParseError,
    fetch_notice_xml,
    normalise_ted_number,
    parse_contacts,
    parse_notice,
    strip_contacts,
)
from ingest.tenderned import (  # noqa: E402
    TED_NUMBER_KEY,
    TenderNedClient,
    contact_names,
    effective_sluitingsdatum,
    rectification_id,
    split_contacts,
)

DATA = REPO_ROOT / "data"
DEFAULT_IDS = DATA / "snapshot_ids.txt"
DEFAULT_OUT = DATA / "snapshot"
DEFAULT_CACHE = DATA / "raw" / "snapshot"
DEFAULT_REPORT = DATA / "snapshot_report.json"

DOC_KEY_LENGTH = 12
MAX_ARCHIVE_FILES = 500
MAX_ARCHIVE_BYTES = 500 * 1024 * 1024
CHUNK = 1 << 20
SKIP_NAMES = frozenset({"__macosx", ".ds_store", "thumbs.db"})
ID_RE = re.compile(r"\d{1,9}")
DRIVE_RE = re.compile(r"[A-Za-z]:")
MISSING_STATUSES = (404, 410, 200)  # 200: an answer that is not the XML notice (HttpError)
ARCHIVE_ERRORS = (
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    RuntimeError,  # encrypted member, or a password is needed
    NotImplementedError,  # compression method not supported
    zlib.error,
    EOFError,
    OSError,
    ValueError,
)


class FreezeError(Exception):
    """A condition that stops the run (the message holds no document text or names)."""


# --- the ID list and the manual folder ----------------------------------------------------


def read_ids(path: Path) -> list[str]:
    """One publication ID per line; blank lines and `#` comments are ignored; no duplicates."""
    ids: list[str] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        text = line.split("#", 1)[0].strip()
        if not text:
            continue
        if not ID_RE.fullmatch(text):
            raise FreezeError(f"{path.name} line {number}: not a publication ID")
        if text not in ids:
            ids.append(text)
    return ids


def _hidden(name: str) -> bool:
    return name.startswith(".") or name.lower() in SKIP_NAMES


def list_files(folder: Path) -> tuple[list[tuple[str, Path]], int]:
    """(relative POSIX path, path) of every regular file, plus how many entries were skipped.

    Symbolic links (files or folders), hidden files (this covers `.download_ids.json`),
    `__MACOSX/`, `.DS_Store` and `Thumbs.db` are skipped.
    """
    found: list[tuple[str, Path]] = []
    skipped = 0
    for current, dirs, files in os.walk(folder, followlinks=False):
        here = Path(current)
        keep = []
        for name in sorted(dirs):
            if _hidden(name) or (here / name).is_symlink():
                skipped += 1
            else:
                keep.append(name)
        dirs[:] = keep
        for name in sorted(files):
            path = here / name
            if _hidden(name) or path.is_symlink() or not path.is_file():
                skipped += 1
            else:
                found.append((path.relative_to(folder).as_posix(), path))
    found.sort()
    return found, skipped


def folder_state(manual: Path, publication_id: str) -> tuple[str, list[tuple[str, Path]], int]:
    """("ok" | "missing_folder" | "empty_folder", files, skipped entries)."""
    folder = manual / publication_id
    if folder.is_symlink() or not folder.is_dir():
        return "missing_folder", [], 0
    files, skipped = list_files(folder)
    return ("ok" if files else "empty_folder"), files, skipped


def unlisted_folders(manual: Path, ids: Sequence[str]) -> list[str]:
    if not manual.is_dir():
        return []
    return sorted(
        entry.name
        for entry in manual.iterdir()
        if entry.is_dir() and not _hidden(entry.name) and entry.name not in ids
    )


# --- the cache: TenderNed detail, rectification, TED notice, contact names -----------------


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _write_json(path: Path, value: Any) -> None:
    _write_atomic(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def _add_names(path: Path, names: Sequence[str]) -> None:
    """Merge contact names into `contacts.json` ({"names": [...]}). Idempotent."""
    known: list[str] = []
    if path.is_file():
        known = [str(name) for name in json.loads(path.read_text(encoding="utf-8"))["names"]]
    merged = known + [name for name in dict.fromkeys(names) if name not in known]
    if not path.is_file() or merged != known:
        _write_json(path, {"names": merged})


@dataclass(slots=True)
class Fetched:
    status: str  # "ok" | "no_ted_notice" | "failed"
    reason: str | None = None
    ted_number: str | None = None
    rectification: bool = False
    closing: str | None = None


def _missing(exc: HttpError) -> bool:
    return exc.status in MISSING_STATUSES


def fetch_tender(client: HttpClient, publication_id: str, cache: Path) -> Fetched:
    """Fill `cache` for one tender. A rerun fetches only what is not cached yet.

    Stops the run (HttpError) on a 429, a 5xx, a network error or the request budget.
    Contact data is split off at once: only stripped files and names reach the disk, and
    the names are written before the file that would make the step count as done.
    """
    tenderned = TenderNedClient(client)
    contacts = cache / "contacts.json"
    detail_path = cache / "tenderned.json"
    rectification_path = cache / "tenderned_rectification.json"
    notice_path = cache / "notice.xml"

    if not detail_path.is_file():
        try:
            raw = tenderned.detail(publication_id)
        except HttpError as exc:
            if exc.status in (404, 410):
                return Fetched("no_ted_notice", "no_tenderned_detail")
            raise
        clean, removed = split_contacts(raw)
        _add_names(contacts, contact_names(removed))
        _write_json(detail_path, clean)
    detail = json.loads(detail_path.read_text(encoding="utf-8"))

    rect_id = rectification_id(detail)
    rectification: dict[str, Any] | None = None
    if rect_id is not None:
        if not rectification_path.is_file():
            try:
                raw = tenderned.detail(rect_id)
            except HttpError as exc:
                if not _missing(exc):
                    raise
            else:
                clean, removed = split_contacts(raw)
                _add_names(contacts, contact_names(removed))
                _write_json(rectification_path, clean)
        if rectification_path.is_file():
            rectification = json.loads(rectification_path.read_text(encoding="utf-8"))

    number = normalise_ted_number(detail.get(TED_NUMBER_KEY))
    result = Fetched(
        "ok",
        ted_number=number,
        rectification=rectification is not None,
        closing=effective_sluitingsdatum(detail, rectification),
    )
    if number is None:
        return Fetched("no_ted_notice", "no_ted_number", None, result.rectification, result.closing)

    if not notice_path.is_file():
        try:
            original = fetch_notice_xml(client, number)
        except HttpError as exc:
            if not _missing(exc):
                raise
            return Fetched("no_ted_notice", "notice_not_found", number, *_tail(result))
        try:
            parse_notice(original)
            names = [row.contact_name for row in parse_contacts(original) if row.contact_name]
            stripped = strip_contacts(original)
        except TedParseError:
            return Fetched("no_ted_notice", "notice_not_parsable", number, *_tail(result))
        _add_names(contacts, names)
        _write_atomic(notice_path, stripped)
    else:
        try:
            parse_notice(notice_path.read_bytes())
        except TedParseError:
            return Fetched("no_ted_notice", "notice_not_parsable", number, *_tail(result))
    _add_names(contacts, [])
    return result


def _tail(result: Fetched) -> tuple[bool, str | None]:
    return result.rectification, result.closing


# --- unpacking ZIP files -------------------------------------------------------------------


@dataclass(slots=True)
class Unpacked:
    status: str  # "unpacked" | "unsupported"
    reason: str | None = None
    members: list[tuple[str, Path]] = field(default_factory=list)  # (member path, file on disk)
    skipped: int = 0  # directories, links, hidden entries
    unsafe: int = 0  # members rejected for their path


def _safe_member(name: str) -> PurePosixPath | None:
    """The member's path when it is relative, inside the archive and has no drive letter."""
    text = name.replace("\\", "/")
    if not name or "\x00" in name or text.startswith("/") or DRIVE_RE.match(text):
        return None
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    return PurePosixPath(*parts)


def _is_link(info: zipfile.ZipInfo) -> bool:
    return stat.S_ISLNK(info.external_attr >> 16)


def _unique_member(member: PurePosixPath, taken: set[str]) -> PurePosixPath:
    """The member's path, or `name (2).ext` when another member differs from it only in case."""
    candidate, number = member, 1
    while candidate.as_posix().lower() in taken:
        number += 1
        candidate = member.with_name(f"{member.stem} ({number}){member.suffix}")
    taken.add(candidate.as_posix().lower())
    return candidate


def unpack_zip(archive_path: Path, dest: Path) -> Unpacked:
    """Unpack one ZIP into `dest` (a new folder). Never raises for a bad archive.

    Limits (module constants, read at call time): MAX_ARCHIVE_FILES files and
    MAX_ARCHIVE_BYTES unpacked bytes, checked against the declared sizes first and then while
    reading, since a header can lie. A member with an unsafe path is rejected and counted; any
    other problem marks the whole archive unsupported and removes what was written.
    """
    result = Unpacked("unpacked")
    try:
        with zipfile.ZipFile(archive_path) as archive:
            wanted: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
            seen: set[str] = set()
            taken: set[str] = set()  # lower-case paths: macOS folds case when unpacking
            for info in archive.infolist():
                member = _safe_member(info.filename)
                if member is None:
                    result.unsafe += 1
                    continue
                if (
                    info.is_dir()
                    or _is_link(info)
                    or any(_hidden(part) for part in member.parts)
                    or member.as_posix() in seen
                ):
                    result.skipped += 1
                    continue
                seen.add(member.as_posix())
                member = _unique_member(member, taken)
                wanted.append((info, member))
            if any(info.flag_bits & 0x1 for info, _ in wanted):
                return _unsupported(dest, "encrypted", result)
            if len(wanted) > MAX_ARCHIVE_FILES:
                return _unsupported(dest, "too_many_files", result)
            if sum(info.file_size for info, _ in wanted) > MAX_ARCHIVE_BYTES:
                return _unsupported(dest, "too_large", result)

            total = 0
            for info, member in wanted:
                target = dest.joinpath(*member.parts)
                if dest.resolve() not in target.resolve().parents:
                    result.unsafe += 1
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as sink:
                    for block in iter(lambda: source.read(CHUNK), b""):
                        total += len(block)
                        if total > MAX_ARCHIVE_BYTES:
                            return _unsupported(dest, "too_large", result)
                        sink.write(block)
                result.members.append((member.as_posix(), target))
    except ARCHIVE_ERRORS as exc:
        reason = "encrypted" if isinstance(exc, RuntimeError) else "unreadable"
        return _unsupported(dest, reason, result)
    return result


def _unsupported(dest: Path, reason: str, partial: Unpacked) -> Unpacked:
    shutil.rmtree(dest, ignore_errors=True)
    return Unpacked("unsupported", reason, [], partial.skipped, partial.unsafe)


# --- documents of one tender ---------------------------------------------------------------


@dataclass(slots=True)
class DocRow:
    filename: str
    sha256: str
    doc_key: str
    doc_type: str
    doc_type_source: str
    file_format: str
    extraction_status: str
    page_kind: str | None
    pages: list[tuple[int, str]]


@dataclass(slots=True)
class TenderDocuments:
    rows: list[DocRow] = field(default_factory=list)
    containers: list[dict[str, Any]] = field(default_factory=list)
    direct: int = 0
    unpacked: int = 0
    duplicates: list[str] = field(default_factory=list)
    unsupported: list[dict[str, str]] = field(default_factory=list)
    skipped_entries: int = 0
    unsafe_members: int = 0
    lock_files: list[str] = field(default_factory=list)  # Office lock files ("~$..."), skipped


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _extension(name: str) -> str:
    return PurePosixPath(name).suffix.lower() or "(none)"


def read_documents(files: Sequence[tuple[str, Path]], work: Path) -> TenderDocuments:
    """Classify and extract every file of one tender; unpack its ZIPs one level into `work`."""
    out = TenderDocuments()
    shutil.rmtree(work, ignore_errors=True)
    seen: dict[str, str] = {}  # sha256 -> first relative name
    pending: list[tuple[str, Path]] = []  # top-level ZIP files

    def handle(relative: str, path: Path, level: int) -> None:
        if PurePosixPath(relative).name.startswith("~$"):
            out.lock_files.append(relative)
            return
        digest = _sha256(path)
        if digest in seen:
            out.duplicates.append(relative)
            return
        seen[digest] = relative
        with path.open("rb") as handle:
            head = handle.read(2048)
        if head.startswith(b"PK\x03\x04"):
            head = path.read_bytes()  # a ZIP is told from DOCX and XLSX by its member names
        fmt = file_format(PurePosixPath(relative).name, head)
        if fmt == "zip":
            if level == 0:
                pending.append((relative, path))  # after the plain files, so those win a tie
            else:
                handle_zip(relative, path, level)
            return
        if level == 0:
            out.direct += 1
        else:
            out.unpacked += 1
        result = extract(path, fmt)
        doc_type = classify_by_rule(PurePosixPath(relative).name, None, fmt)
        if result.status == "unsupported":
            out.unsupported.append({"filename": relative, "extension": _extension(relative)})
        out.rows.append(
            DocRow(
                filename=relative,
                sha256=digest,
                doc_key=digest[:DOC_KEY_LENGTH],
                doc_type=doc_type or "other",
                doc_type_source="rule" if doc_type else "default",
                file_format=fmt,
                extraction_status=result.status,
                page_kind=result.page_kind,
                pages=[(page.page_no, page.text) for page in result.pages],
            )
        )

    def handle_zip(relative: str, path: Path, level: int) -> None:
        record: dict[str, Any] = {"filename": relative, "container": True}
        if level > 0:
            record.update(status="unsupported", reason="nested_zip", members=0)
            out.unsupported.append({"filename": relative, "extension": _extension(relative)})
            out.containers.append(record)
            return
        unpacked = unpack_zip(path, work / relative)
        out.skipped_entries += unpacked.skipped
        out.unsafe_members += unpacked.unsafe
        record.update(status=unpacked.status, reason=unpacked.reason, members=len(unpacked.members))
        out.containers.append(record)
        if unpacked.status == "unsupported":
            out.unsupported.append({"filename": relative, "extension": _extension(relative)})
            return
        for member, member_path in sorted(unpacked.members):
            handle(f"{relative}/{member}", member_path, 1)

    for relative, path in files:
        handle(relative, path, 0)
    for relative, path in pending:
        handle_zip(relative, path, 0)
    out.rows.sort(key=lambda row: row.filename)
    keys: dict[str, str] = {}
    for row in out.rows:
        if row.doc_key in keys and keys[row.doc_key] != row.sha256:
            raise FreezeError("two different files share one doc_key")
        keys[row.doc_key] = row.sha256
    return out


# --- writing the snapshot ------------------------------------------------------------------


def _write_snapshot(
    tmp: Path, tenders: dict[int, tuple[Path, TenderDocuments]], version: str, now: datetime
) -> None:
    shutil.rmtree(tmp, ignore_errors=True)
    document_id = 0
    for publication_id in sorted(tenders):
        cache, docs = tenders[publication_id]
        folder = tmp / "tenders" / str(publication_id)
        folder.mkdir(parents=True)
        for name in ("notice.xml", "tenderned.json", "tenderned_rectification.json"):
            if (cache / name).is_file():
                shutil.copyfile(cache / name, folder / name)
        names = json.loads((cache / "contacts.json").read_text(encoding="utf-8"))["names"]
        _write_json(folder / "contacts.json", {"names": names})
        rows: list[dict[str, Any]] = []
        for row in docs.rows:
            document_id += 1
            rows.append(
                {
                    "document_id": document_id,
                    "doc_key": row.doc_key,
                    "filename": row.filename,
                    "doc_type": row.doc_type,
                    "is_current": True,
                    "published_at": None,
                    "extraction_status": row.extraction_status,
                    "page_kind": row.page_kind,
                    "language": None,
                }
            )
            if row.extraction_status == "ok":
                lines = [
                    json.dumps({"page_no": number, "text": text}, ensure_ascii=False)
                    for number, text in row.pages
                ]
                pages = folder / "pages" / f"{row.doc_key}.jsonl"
                _write_atomic(pages, ("\n".join(lines) + "\n").encode("utf-8"))
        _write_json(
            folder / "documents.json",
            {
                "documents_access": "tenderned" if rows or docs.containers else "none",
                "documents": rows,
                "containers": sorted(docs.containers, key=lambda item: item["filename"]),
            },
        )
    manifest = build_manifest(
        tmp, snapshot_version=version, frozen_at=now.isoformat(timespec="seconds"), kb_version=None
    )
    _write_json(tmp / MANIFEST, manifest)
    open_snapshot(tmp)  # must succeed before the folder gets its final name


def _install(tmp: Path, out: Path, force: bool) -> None:
    """Rename `tmp` to `out`. An existing `out` moves to `<out>.prev` (only with force)."""
    if out.exists():
        if not force:
            raise FreezeError(f"{out.name} already exists; use --force to replace it")
        if any(out.iterdir()):
            os.rename(out, _prev(out))
        else:
            out.rmdir()
    os.rename(tmp, out)


def _prev(out: Path) -> Path:
    return out.with_name(out.name + ".prev")


def _tmp(out: Path) -> Path:
    return out.with_name(out.name + ".tmp")


def _check_out(out: Path, force: bool) -> None:
    if out.exists() and any(out.iterdir()):
        if not force:
            raise FreezeError(f"{out.name} already exists; use --force to replace it")
        if _prev(out).exists():
            raise FreezeError(f"{_prev(out).name} already exists; move it away first")


# --- the run -------------------------------------------------------------------------------


@dataclass(slots=True)
class Options:
    manual: Path
    out: Path
    ids_path: Path
    version: str | None
    limit: int | None = None
    details_only: bool = False
    force: bool = False
    cache_root: Path = DEFAULT_CACHE
    report_path: Path = DEFAULT_REPORT


def freeze(
    options: Options,
    client: HttpClient,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Run the freeze and return the report. Raises FreezeError, HttpError or SnapshotError."""
    if not options.details_only:
        if not options.version:
            raise FreezeError("--version is required unless --details-only is given")
        _check_out(options.out, options.force)
    ids = read_ids(options.ids_path)
    if not ids:
        raise FreezeError("the ID list is empty")

    report: dict[str, Any] = {
        "snapshot_version": options.version,
        "listed": len(ids),
        "tenders": {},
        "missing_folder": [],
        "empty_folder": [],
        "no_ted_notice": [],
        "not_listed_folders": unlisted_folders(options.manual, ids),
        "rectifications": [],
        "failures": {},
    }
    states: dict[str, tuple[str, list[tuple[str, Path]], int]] = {}
    chosen: list[str] = []
    for publication_id in ids:
        state, files, skipped = folder_state(options.manual, publication_id)
        states[publication_id] = (state, files, skipped)
        if state != "ok":
            report[state].append(publication_id)
            report["tenders"][publication_id] = {"status": state}
            if not options.details_only:
                continue
        if options.limit is None or len(chosen) < options.limit:
            chosen.append(publication_id)

    included: dict[int, tuple[Path, TenderDocuments]] = {}
    for publication_id in chosen:
        cache = options.cache_root / publication_id
        entry = report["tenders"].setdefault(publication_id, {})
        try:
            fetched = fetch_tender(client, publication_id, cache)
        except (TedParseError, ValueError) as exc:
            _count_failure(report, exc)
            entry.update(status="failed", error=type(exc).__name__)
            continue
        entry.update(
            ted_number=fetched.ted_number,
            rectification=fetched.rectification,
            closing_date=fetched.closing,
        )
        if fetched.rectification:
            report["rectifications"].append(publication_id)
        if fetched.status != "ok":
            entry.update(status="no_ted_notice", reason=fetched.reason)
            report["no_ted_notice"].append(publication_id)
            continue
        if options.details_only:
            entry.setdefault("status", "details_ok")
            continue
        _, files, skipped = states[publication_id]
        docs = read_documents(files, cache / "unpacked")
        docs.skipped_entries += skipped
        included[int(publication_id)] = (cache, docs)
        entry.update(
            status="included",
            documents=len(docs.rows),
            files_read_directly=docs.direct,
            zips=[
                {k: c[k] for k in ("filename", "status", "reason", "members")}
                for c in docs.containers
            ],
            files_unpacked_from_zips=docs.unpacked,
            duplicates_skipped=docs.duplicates,
            unsupported=docs.unsupported,
            skipped_entries=docs.skipped_entries,
            unsafe_zip_members=docs.unsafe_members,
            office_lock_files_skipped=docs.lock_files,
        )

    report["requests"] = client.requests
    if options.details_only:
        _write_json(options.report_path, report)
        return report
    if not included:
        raise FreezeError("no tender could be included; nothing was written")

    report.update(_counts(included))
    tmp = _tmp(options.out)
    _write_snapshot(tmp, included, options.version or "", now())
    _install(tmp, options.out, options.force)
    _write_json(options.report_path, report)
    return report


def _count_failure(report: dict[str, Any], exc: BaseException) -> None:
    failures = report["failures"]
    failures[type(exc).__name__] = failures.get(type(exc).__name__, 0) + 1


def _counts(included: dict[int, tuple[Path, TenderDocuments]]) -> dict[str, Any]:
    rows = [row for _, docs in included.values() for row in docs.rows]
    return {
        "tenders_in_snapshot": len(included),
        "documents": len(rows),
        "by_doc_type": dict(sorted(Counter(r.doc_type for r in rows).items())),
        "by_doc_type_source": dict(sorted(Counter(r.doc_type_source for r in rows).items())),
        "by_extraction_status": dict(sorted(Counter(r.extraction_status for r in rows).items())),
        "by_file_format": dict(sorted(Counter(r.file_format for r in rows).items())),
    }


@contextlib.contextmanager
def _stdout_to_stderr() -> Iterator[None]:
    """Library hints (pymupdf prints some to stdout) go to stderr, so the report stays clean."""
    with contextlib.redirect_stdout(sys.stderr):
        yield


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--from-manual", type=Path, required=True, metavar="DIR")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--version", help="snapshot_version; required unless --details-only")
    parser.add_argument("--ids", type=Path, default=DEFAULT_IDS)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--details-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE, help="fetched files")
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args(argv)
    options = Options(
        manual=args.from_manual,
        out=args.out,
        ids_path=args.ids,
        version=args.version,
        limit=args.limit,
        details_only=args.details_only,
        force=args.force,
        cache_root=args.cache,
        report_path=args.report,
    )
    client = HttpClient(max_requests=args.max_requests)
    try:
        with _stdout_to_stderr():
            report = freeze(options, client)
    except (FreezeError, HttpError, SnapshotError, OSError) as exc:
        print(f"freeze stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"requests made: {client.requests}", file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))  # last, after any library hints
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
