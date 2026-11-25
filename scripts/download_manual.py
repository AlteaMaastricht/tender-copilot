"""Document download for the manual snapshot (PRE-M4 b): the final 20 tenders only.

Usage:
    python scripts/download_manual.py

Anonymous GETs, one at a time, 2 s apart (ingest.http.HttpClient: https only, allowed hosts,
no redirects, no retries). Per tender: one GET of the document list
(/publicaties/{id}/documenten), then one GET per file through links.download.href (DD3).
Never documenten/zip, public-xml, TED or the detail. Files go to data/raw/manual/<id>/ under
the server's name (documentNaam), sanitised; ZIPs stay packed. Nothing downloaded is opened,
imported or executed. The list JSON is never written to disk. A 429, a 5xx or a network error
stops the run; a 4xx on one file or list is recorded and the run goes on.
Writes data/raw/manual_download_report.json (git-ignored): counts and HTTP statuses only.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from ingest.http import HttpClient, HttpError  # noqa: E402
from ingest.tenderned import BASE_URL  # noqa: E402

OUT_ROOT = REPO_ROOT / "data" / "raw" / "manual"
REPORT_PATH = REPO_ROOT / "data" / "raw" / "manual_download_report.json"
SITE = "https://www.tenderned.nl"
ALLOWED_DOWNLOAD_HOST = "www.tenderned.nl"
MAX_FILE_BYTES = 100 * 1024 * 1024
# 20 list requests plus at most 680 files (about 23 minutes at 2 s). The folder with the most
# files by hand has 34, so 20 tenders of 34 would be 680 + 20 = 700.
HARD_CAP_REQUESTS = 700
MAX_NAME_BYTES = 180

NAME_KEY = "documentNaam"
ID_KEY = "documentId"
DOCUMENT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
LIST_KEY = "documenten"  # preferred array key when the list is an object
DOWNLOAD_PREFIX = "/papi/tenderned-rs-tns/v2/publicaties/"
INDEX_NAME = ".download_ids.json"  # documentId -> file name, per tender folder (DD4)

FINAL_IDS = [
    "409819", "410334", "408865", "413590", "413464",
    "417895", "413124", "423040", "417894", "427033",
    "422544", "430966", "417763", "427081", "409515",
    "413504", "430137", "422800", "417896", "409553",
]  # fmt: skip


class IndexDamagedError(ValueError):
    """A tender's .download_ids.json exists but cannot be read. The run stops."""


class ListShapeError(ValueError):
    """The document list is not a shape we know. Carries top-level key names only."""


@dataclass(frozen=True, slots=True)
class Listing:
    document_id: str
    name: str
    href: str


def parse_listing(payload: Any) -> tuple[list[Listing], int]:
    """(documents, items skipped for lack of an ID or a download link).

    The list is a JSON array, or an object holding one array of objects.
    """
    items: Any = payload
    if isinstance(payload, dict):
        arrays = [v for v in payload.values() if isinstance(v, list)]
        if isinstance(payload.get(LIST_KEY), list):
            items = payload[LIST_KEY]
        elif len(arrays) == 1:
            items = arrays[0]
        else:
            raise ListShapeError(
                f"{len(arrays)} arrays and no {LIST_KEY!r}; keys: {sorted(map(str, payload))[:12]}"
            )
    if not isinstance(items, list):
        raise ListShapeError(f"unexpected list type {type(payload).__name__}")
    found: list[Listing] = []
    skipped = 0
    for item in items:
        if not isinstance(item, dict):
            skipped += 1
            continue
        links = item.get("links")
        download = links.get("download") if isinstance(links, dict) else None
        href = download.get("href") if isinstance(download, dict) else None
        doc_id = item.get(ID_KEY)
        name = item.get(NAME_KEY)
        if (
            not isinstance(href, str)
            or not href
            or isinstance(doc_id, bool)
            or not isinstance(doc_id, str | int)
            or not DOCUMENT_ID_RE.fullmatch(str(doc_id))
        ):
            skipped += 1
            continue
        found.append(Listing(str(doc_id), name if isinstance(name, str) else "", href))
    if items and not found:
        raise ListShapeError("the list has items but none with a documentId and a download link")
    return found, skipped


def resolve_href(href: str) -> str | None:
    """The absolute https URL on www.tenderned.nl, or None if the link must be refused."""
    url = urljoin(SITE + "/", href.strip())
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != ALLOWED_DOWNLOAD_HOST or parts.username:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    if port is not None or parts.query or parts.fragment:
        return None
    path = parts.path.lower()
    if not path.startswith(DOWNLOAD_PREFIX) or ".." in path:
        return None
    if path.rstrip("/").endswith("/zip") or "public-xml" in path:
        return None
    return url


def sanitise_name(name: str, fallback: str) -> str:
    """A safe single file name: no separators, no "..", no control characters, no leading dot."""
    text = re.sub(r"[\x00-\x1f\x7f]", "", name)
    text = text.replace("/", "_").replace("\\", "_").replace(":", "_")
    while ".." in text:
        text = text.replace("..", ".")
    text = text.strip().lstrip(". ").rstrip(" .")
    stem, ext = os.path.splitext(text)
    if len(ext) > 12:
        stem, ext = text, ""
    stem = stem.strip()
    encoded = stem.encode("utf-8")
    if len(encoded) > MAX_NAME_BYTES:
        stem = encoded[:MAX_NAME_BYTES].decode("utf-8", "ignore").rstrip()
    safe_fallback = re.sub(r"[^A-Za-z0-9_-]", "", fallback) or "document"
    return (stem + ext) if stem else safe_fallback


def with_document_id(name: str, document_id: str) -> str:
    stem, ext = os.path.splitext(name)
    return f"{stem} ({document_id}){ext}"


def _write_new(directory: Path, name: str, data: bytes) -> bool:
    """Write atomically: temp file, then a hard link that never replaces. False if it exists."""
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".part-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        try:
            os.link(tmp, directory / name)
        except FileExistsError:
            return False
        return True
    finally:
        os.unlink(tmp)


def load_index(directory: Path) -> dict[str, str]:
    """documentId -> file name written by an earlier run. Missing = empty; damaged raises."""
    path = directory / INDEX_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise IndexDamagedError(f"{directory.name}: {INDEX_NAME} cannot be read") from None
    if not isinstance(data, dict):
        raise IndexDamagedError(f"{directory.name}: {INDEX_NAME} is not an object")
    return {str(k): str(v) for k, v in data.items()}


def save_index(directory: Path, index: Mapping[str, str]) -> None:
    """Write the index atomically (temp file, then replace)."""
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".part-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(index), handle, indent=1, sort_keys=True)
        os.replace(tmp, directory / INDEX_NAME)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _is_fatal(exc: HttpError) -> bool:
    return exc.status is None or exc.status == 429 or exc.status >= 500


def _failure(exc: HttpError) -> dict[str, Any]:
    reason = "over_size_cap" if str(exc).startswith("response over") else "http_error"
    return {"status": exc.status, "reason": reason}


def process_tender(
    http: HttpClient, pub_id: str, root: Path, entry: dict[str, Any], *, list_seen: bool
) -> bool:
    """Download one tender. Returns True if its list had a known shape. Raises on a stop."""
    directory = root / pub_id
    index = load_index(directory)  # before any request: a damaged index stops the run
    try:
        payload = http.get_json(f"{BASE_URL}/publicaties/{pub_id}/documenten")
    except HttpError as exc:
        if _is_fatal(exc):
            raise
        entry["list_failed"] = _failure(exc)
        return False
    try:
        listings, bad = parse_listing(payload)
    except ListShapeError:
        entry["list_failed"] = {"status": 200, "reason": "unknown_list_shape"}
        if not list_seen:
            raise
        return False
    entry["listed"] = len(listings)
    entry["list_items_unusable"] = bad
    directory.mkdir(parents=True, exist_ok=True)
    taken: set[str] = set()  # lower-case names written in this run
    for doc in listings:
        if doc.document_id in index:  # DD4: known documentId, written by this or an earlier run
            entry["skipped"] += 1
            continue
        url = resolve_href(doc.href)
        if url is None:
            entry["failed"].append({"status": None, "reason": "href_refused"})
            continue
        try:
            response = http.get_bytes(url, max_bytes=MAX_FILE_BYTES)
        except HttpError as exc:
            if _is_fatal(exc):
                raise
            entry["failed"].append(_failure(exc))
            continue
        name = sanitise_name(doc.name, doc.document_id)
        if name.lower() in taken or (directory / name).exists():
            name = with_document_id(name, doc.document_id)
        if name.lower() in taken or not _write_new(directory, name, response.body):
            entry["failed"].append({"status": None, "reason": "name_taken"})
            continue
        taken.add(name.lower())
        index[doc.document_id] = name
        save_index(directory, index)
        entry["downloaded"] += 1
    return True


def run_download(
    http: HttpClient,
    ids: Sequence[str],
    root: Path,
    report: dict[str, Any],
) -> dict[str, Any]:
    """Download every tender in turn. `report` is filled as it goes, so a stop keeps the result."""
    tenders: dict[str, Any] = report.setdefault("tenders", {})
    list_seen = False
    for pub_id in ids:
        entry: dict[str, Any] = {"listed": 0, "downloaded": 0, "skipped": 0, "failed": []}
        tenders[pub_id] = entry
        if process_tender(http, pub_id, root, entry, list_seen=list_seen):
            list_seen = True
    return report


def write_report(report: Mapping[str, Any], path: Path = REPORT_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    http = HttpClient(max_requests=HARD_CAP_REQUESTS)
    report: dict[str, Any] = {"hard_cap_requests": HARD_CAP_REQUESTS, "stopped": None}
    code = 0
    try:
        run_download(http, FINAL_IDS, OUT_ROOT, report)
    except (HttpError, ListShapeError, IndexDamagedError) as exc:
        report["stopped"] = str(exc)
        print(f"Stopped: {exc}")
        code = 1
    finally:
        report["requests"] = http.requests
        write_report(report)
    print(f"Requests made: {http.requests}; wrote {REPORT_PATH.relative_to(REPO_ROOT)}")
    print("id listed downloaded skipped failed")
    for pub_id, e in report["tenders"].items():
        print(
            pub_id,
            e["listed"],
            e["downloaded"],
            e["skipped"],
            len(e["failed"]),
            e.get("list_failed", ""),
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
