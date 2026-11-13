"""Probe TenderNed for a few publications: which endpoints exist, and what documents they list.

Usage:
    python scripts/probe_tenderned.py 449344 449101 448977

For each publication ID (three at most) the script:
  1. requests a small set of candidate endpoints, first without and, where that is refused,
     with HTTP Basic auth (TENDERNED_USER / TENDERNED_PASSWORD from .env);
  2. prints the document list (name, type, size);
  3. downloads at most one document into data/raw/probe/<publication id>/.

It answers open question 1 in docs/ARCHITECTURE.md. It is a one-off probe: no database,
no bulk download, a fixed delay between requests, standard library only.
Credentials are sent only to www.tenderned.nl and are never printed.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

HOST = "www.tenderned.nl"
BASE_URL = f"https://{HOST}/papi/tenderned-rs-tns/v2"
REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE_DIR = REPO_ROOT / "data" / "raw" / "probe"

USER_AGENT = "tender-copilot-probe/0.1 (one-off research probe)"
MAX_IDS = 3
DELAY_SECONDS = 2.0
TIMEOUT_SECONDS = 30
MAX_METADATA_BYTES = 5 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024

# (label, path template, read the body?). Only `public-xml` and the per-document `content`
# path are confirmed by public sources; the others are guesses that this probe tests.
ENDPOINTS: list[tuple[str, str, bool]] = [
    ("detail", "/publicaties/{id}", True),
    ("public-xml", "/publicaties/{id}/public-xml", True),
    ("documents", "/publicaties/{id}/documenten", True),
    ("documents-zip", "/publicaties/{id}/documenten/zip", False),  # headers only, never downloaded
]

NAME_KEYS = ("documentNaam", "naam", "bestandsnaam", "fileName", "filename", "name", "titel")
TYPE_KEYS = ("typeDocument", "documentType", "type", "soort", "mimeType", "contentType")
SIZE_KEYS = ("grootte", "bestandsgrootte", "size", "fileSize", "omvang")
ID_KEYS = ("documentId", "id")
DETAIL_KEYS = (
    "aanbestedingNaam",
    "opdrachtgeverNaam",
    "publicatieDatum",
    "sluitingsDatum",
    "typePublicatie",
    "procedure",
)


@dataclass
class Response:
    status: int | None
    content_type: str = ""
    content_length: int | None = None
    content_disposition: str = ""
    location: str = ""
    body: bytes = b""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def refused(self) -> bool:
        return self.status in (401, 403)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not follow redirects, so the auth header can never leave the TenderNed host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
_last_request_at = 0.0


def load_env(path: Path) -> dict[str, str]:
    """Minimal .env reader: KEY=VALUE lines, optional quotes, `#` comments."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def basic_auth_header(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def _polite_wait() -> None:
    global _last_request_at
    wait = DELAY_SECONDS - (time.monotonic() - _last_request_at)
    if wait > 0:
        time.sleep(wait)
    _last_request_at = time.monotonic()


def _open(url: str, auth: str | None, accept: str):
    if not url.startswith(f"https://{HOST}/"):
        raise ValueError("refusing to request a URL outside www.tenderned.nl")
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    if auth:
        headers["Authorization"] = auth
    _polite_wait()
    return _OPENER.open(urllib.request.Request(url, headers=headers), timeout=TIMEOUT_SECONDS)


def _headers_to_response(status: int, headers: Any) -> Response:
    length = headers.get("Content-Length")
    return Response(
        status=status,
        content_type=(headers.get("Content-Type") or "").split(";")[0].strip(),
        content_length=int(length) if length and length.isdigit() else None,
        content_disposition=headers.get("Content-Disposition") or "",
        location=headers.get("Location") or "",
    )


def fetch(url: str, auth: str | None = None, read_body: bool = True) -> Response:
    """GET a URL. Never raises for HTTP or network errors; the error text carries no secrets."""
    accept = "application/json, application/xml;q=0.9, */*;q=0.5"
    try:
        with _open(url, auth, accept) as resp:
            result = _headers_to_response(resp.status, resp.headers)
            if read_body:
                result.body = resp.read(MAX_METADATA_BYTES)
            return result
    except urllib.error.HTTPError as exc:
        result = _headers_to_response(exc.code, exc.headers)
        exc.close()
        return result
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return Response(status=None, error=f"{type(exc).__name__}: {reason}")


def fetch_with_fallback(
    url: str, auth: str | None, read_body: bool = True
) -> tuple[Response, Response | None]:
    """Try anonymously; only if that is refused (401/403) and credentials exist, try with auth."""
    anon = fetch(url, None, read_body)
    if anon.refused and auth:
        return anon, fetch(url, auth, read_body)
    return anon, None


def human_size(value: Any) -> str:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return str(value) if value not in (None, "") else "?"
    size = float(value)
    for unit in ("B", "kB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "?"


def first_value(item: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if item.get(key) not in (None, ""):
            return item[key]
    return None


def as_text(value: Any) -> str:
    """Render a scalar, or a TenderNed {code, omschrijving} object, as short text."""
    if isinstance(value, dict):
        return str(value.get("omschrijving") or value.get("code") or "?")
    return "?" if value in (None, "") else str(value)


def find_document_list(payload: Any, depth: int = 0) -> list[dict[str, Any]]:
    """Find the list of document objects in a response whose exact shape is not yet known."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and depth < 3:
        for value in payload.values():
            found = find_document_list(value, depth + 1) if isinstance(value, (list, dict)) else []
            if found:
                return found
    return []


def safe_filename(name: str, fallback: str) -> str:
    name = Path(name.replace("\\", "/")).name
    name = re.sub(r"[^\w.\- ()]", "_", name).strip(" .")
    return name[:150] or fallback


def filename_from_disposition(header: str) -> str:
    match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", header, flags=re.IGNORECASE)
    return urllib.request.unquote(match.group(1)) if match else ""


def describe(resp: Response | None) -> str:
    if resp is None:
        return "-"
    if resp.status is None:
        return resp.error
    text = str(resp.status)
    if resp.location:
        text += f" -> {resp.location}"
    return text


def probe_endpoints(pub_id: str, auth: str | None) -> dict[str, tuple[Response, bool]]:
    """Probe each candidate endpoint. Returns label -> (best response, whether auth was used)."""
    best: dict[str, tuple[Response, bool]] = {}
    print(f"  {'endpoint':<15}{'anonymous':<12}{'with auth':<12}{'content type':<28}size")
    for label, template, read_body in ENDPOINTS:
        url = BASE_URL + template.format(id=pub_id)
        anon, authed = fetch_with_fallback(url, auth, read_body)
        used_auth = authed is not None and authed.ok
        final = authed if used_auth else anon
        best[label] = (final, used_auth)
        size = final.content_length if final.content_length is not None else len(final.body) or None
        print(
            f"  {label:<15}{describe(anon):<12}{describe(authed):<12}"
            f"{final.content_type or '-':<28}{human_size(size) if final.ok else '-'}"
        )
    return best


def print_detail(resp: Response) -> None:
    if not resp.ok or "json" not in resp.content_type:
        return
    try:
        data = json.loads(resp.body)
    except ValueError:
        return
    if not isinstance(data, dict):
        return
    # Only named, non-personal fields are printed; the detail may also hold contact persons.
    for key in DETAIL_KEYS:
        if key in data:
            print(f"  {key}: {as_text(data[key])[:100]}")
    print(f"  detail fields: {', '.join(sorted(data))}")


def print_documents(resp: Response) -> list[dict[str, Any]]:
    if not resp.ok:
        print("  No document list (endpoint not available).")
        return []
    try:
        docs = find_document_list(json.loads(resp.body))
    except ValueError:
        print(f"  Document endpoint did not return JSON ({resp.content_type or 'unknown type'}).")
        return []
    if not docs:
        print("  Document endpoint answered, but no documents were found in the response.")
        return []
    print(f"  {len(docs)} document(s). Field names of the first item: {', '.join(sorted(docs[0]))}")
    for doc in docs:
        name = as_text(first_value(doc, NAME_KEYS))[:70]
        kind = as_text(first_value(doc, TYPE_KEYS))[:24]
        size = human_size(first_value(doc, SIZE_KEYS))
        print(f"    - {name:<70}  {kind:<24}  {size}")
    return docs


def download_one(pub_id: str, docs: list[dict[str, Any]], auth: str | None) -> None:
    """Download the first document that has an ID and is not known to be over the size cap."""
    for doc in docs:
        doc_id = first_value(doc, ID_KEYS)
        size = first_value(doc, SIZE_KEYS)
        too_big = isinstance(size, int) and size > MAX_DOWNLOAD_BYTES
        # documentId is an opaque string (32 hex characters), not a number; allow URL-safe IDs only.
        if doc_id is not None and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(doc_id)) and not too_big:
            break
    else:
        print("  Download skipped: no document with a usable ID under the size cap.")
        return

    # The content path takes a numeric ID found only in links.download.href, not documentId.
    href = ((doc.get("links") or {}).get("download") or {}).get("href", "")
    if re.fullmatch(r"/papi/tenderned-rs-tns/v2/publicaties/\d+/documenten/\d+/content", href):
        url = f"https://{HOST}{href}"
    else:
        url = f"{BASE_URL}/publicaties/{pub_id}/documenten/{doc_id}/content"
    listed_name = as_text(first_value(doc, NAME_KEYS))
    for attempt_auth in (None, auth) if auth else (None,):
        try:
            with _open(url, attempt_auth, "*/*") as resp:
                meta = _headers_to_response(resp.status, resp.headers)
                name = filename_from_disposition(meta.content_disposition) or listed_name
                target = PROBE_DIR / pub_id / safe_filename(name, f"document-{doc_id}")
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with target.open("wb") as handle:
                    while chunk := resp.read(64 * 1024):
                        written += len(chunk)
                        if written > MAX_DOWNLOAD_BYTES:
                            break
                        handle.write(chunk)
                if written > MAX_DOWNLOAD_BYTES:
                    target.unlink(missing_ok=True)
                    print(f"  Download stopped: larger than {human_size(MAX_DOWNLOAD_BYTES)}.")
                    return
                how = "with auth" if attempt_auth else "anonymous"
                print(
                    f"  Downloaded ({how}, {meta.content_type or 'unknown type'}, "
                    f"{human_size(written)}): {target.relative_to(REPO_ROOT)}"
                )
                return
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            if status in (401, 403) and attempt_auth is None and auth:
                continue
            print(f"  Download failed: HTTP {status} for document {doc_id}.")
            return
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"  Download failed: {type(exc).__name__}: {getattr(exc, 'reason', exc)}")
            return
    print(f"  Download refused for document {doc_id} (401/403).")


def parse_args(argv: list[str]) -> list[str]:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("publication_ids", nargs="+", help=f"TenderNed publication IDs (max {MAX_IDS})")
    ids = parser.parse_args(argv).publication_ids
    if len(ids) > MAX_IDS:
        parser.error(f"at most {MAX_IDS} publication IDs; this is a probe, not a bulk download")
    for pub_id in ids:
        if not re.fullmatch(r"\d{1,9}", pub_id):
            parser.error(f"not a numeric publication ID: {pub_id!r}")
    return list(dict.fromkeys(ids))


def main(argv: list[str] | None = None) -> int:
    ids = parse_args(sys.argv[1:] if argv is None else argv)

    env = load_env(REPO_ROOT / ".env")
    user = os.environ.get("TENDERNED_USER") or env.get("TENDERNED_USER", "")
    password = os.environ.get("TENDERNED_PASSWORD") or env.get("TENDERNED_PASSWORD", "")
    auth = basic_auth_header(user, password) if user and password else None
    print(f"Base URL: {BASE_URL}")
    print(f"Credentials: {'loaded from .env' if auth else 'NOT found; probing anonymously only'}")
    print(f"Delay between requests: {DELAY_SECONDS:.0f} s\n")

    for pub_id in ids:
        print(f"Publication {pub_id}")
        results = probe_endpoints(pub_id, auth)
        print_detail(results["detail"][0])
        docs_resp, docs_needed_auth = results["documents"]
        docs = print_documents(docs_resp)
        if docs:
            download_one(pub_id, docs, auth)
        if docs_resp.ok:
            print(f"  Document list needs credentials: {'yes' if docs_needed_auth else 'no'}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
