"""Fetch a small, reproducible set of TED eForms notices as test fixtures.

Usage:
    python scripts/fetch_ted_fixtures.py [--force]
    python scripts/fetch_ted_fixtures.py --add 688608-2026 --min-lots 2

--add makes exactly one request, after a 2 s wait, and adds one notice to the existing set.

Writes to tests/fixtures/ted/:
  - 9 Dutch contract notices (main CPV 72: 4, 79: 3, 48: 2), published in the 8-week window
  - 1 change notice for a procedure whose original contract notice is also in the set
  - 2 not-found responses (not_found_N.body / not_found_N.status)
  - search_cn_72.json (one raw search response) and manifest.json

Anonymous TED Search API, standard library only, 1 s between requests (TED fair use:
700 requests per minute, 600 downloads per 6 minutes). No retries: 429 or 5xx stops the run.

Privacy (CLAUDE.md): every notice is passed through redact_contacts() before anything is
written, so no <cac:Contact> block (person names, emails, phones) ever reaches the fixtures.

How eForms change notices are found: a change notice is a new notice (new notice ID) of the
same type whose `change-notice-version-identifier` (BT-758) points at the changed notice's
`notice-identifier` (BT-701) plus version, and which shares the `procedure-identifier` (BT-04).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SEARCH_URL = "https://api.ted.europa.eu/v3/notices/search"
XML_URL = "https://ted.europa.eu/en/notice/{number}/xml"
ALLOWED_PREFIXES = ("https://api.ted.europa.eu/", "https://ted.europa.eu/")
USER_AGENT = "tender-copilot-fixtures/0.1"

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "tests" / "fixtures" / "ted"

WINDOW = "(20260918 <> 20261113)"  # 8 weeks up to 13 November 2026
QUOTAS = {"72": 4, "79": 3, "48": 2}  # contract notices, including the change pair's original
NOT_FOUND = ("99999999-2026", "99999998-2026")
DELAY_SECONDS = 1.0
ADD_DELAY_SECONDS = 2.0  # --add mode: one request, preceded by this wait
MAX_REQUESTS = 60
SEARCH_FIELDS = [
    "publication-number",
    "notice-type",
    "publication-date",
    "classification-cpv",
    "buyer-country",
    "procedure-identifier",
    "notice-identifier",
    "notice-version",
    "change-notice-version-identifier",
]

NS = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "efbc": "http://data.europa.eu/p27/eforms-ubl-extension-basic-components/1",
}
CONTRACT_NOTICE_NS = "urn:oasis:names:specification:ubl:schema:xsd:ContractNotice-2"


class Stop(Exception):
    """Abort the run with a clear message; nothing is retried."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


@dataclass
class Client:
    requests: int = 0
    _last: float = 0.0
    delay: float = DELAY_SECONDS
    wait_first: bool = False  # also wait `delay` before the very first request

    def __post_init__(self) -> None:
        if self.wait_first:
            self._last = time.monotonic()

    def _send(self, req: urllib.request.Request) -> tuple[int, str, bytes]:
        if not req.full_url.startswith(ALLOWED_PREFIXES):
            raise Stop(f"refusing to contact {req.full_url}")
        if self.requests >= MAX_REQUESTS:
            raise Stop(f"request budget of {MAX_REQUESTS} reached")
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.requests += 1
        try:
            with _OPENER.open(req, timeout=60) as resp:
                return resp.status, resp.headers.get("Content-Type", ""), resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()
            status, ctype = exc.code, exc.headers.get("Content-Type", "")
            exc.close()
            if status == 429 or status >= 500:
                raise Stop(f"HTTP {status} from {req.full_url}; stopping (no retries)") from None
            return status, ctype, body
        except (urllib.error.URLError, TimeoutError) as exc:
            raise Stop(f"network error for {req.full_url}: {exc}") from None

    def search(self, query: str, limit: int = 50) -> tuple[dict[str, Any], bytes]:
        payload = {"query": query, "fields": SEARCH_FIELDS, "limit": limit, "page": 1}
        req = urllib.request.Request(
            SEARCH_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        status, _, body = self._send(req)
        if status != 200:
            raise Stop(f"search rejected (HTTP {status}): {body[:500].decode(errors='replace')}")
        data = json.loads(body)
        if data.get("timedOut"):
            raise Stop(f"search timed out: {query}")
        return data, body

    def get_xml(self, number: str) -> tuple[int, str, bytes]:
        req = urllib.request.Request(
            XML_URL.format(number=number), headers={"User-Agent": USER_AGENT}
        )
        return self._send(req)


@dataclass
class Notice:
    number: str
    xml: bytes
    url: str
    status: int
    role: str
    main_cpv: str = ""
    notice_type: str = ""
    procedure_id: str = ""
    notice_id: str = ""
    changed_notice: str = ""
    publication_date: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def parse_notice(number: str, xml: bytes) -> dict[str, str]:
    """Read the identifying fields from an eForms notice; raise Stop if it is not eForms."""
    root = ET.fromstring(xml)
    if not root.tag.startswith("{" + CONTRACT_NOTICE_NS + "}"):
        raise ValueError(f"{number}: root {root.tag} is not an eForms contract notice")

    def text(path: str) -> str:
        node = root.find(path, NS)
        return (node.text or "").strip() if node is not None else ""

    changed = root.find(".//efbc:ChangedNoticeIdentifier", NS)
    return {
        "procedure_id": text("cbc:ContractFolderID"),
        "notice_id": text("cbc:ID[@schemeName='notice-id']"),
        "notice_type": text("cbc:NoticeTypeCode"),
        "main_cpv": text(
            "cac:ProcurementProject/cac:MainCommodityClassification/cbc:ItemClassificationCode"
        ),
        "changed_notice": (changed.text or "").strip() if changed is not None else "",
    }


def role_for(changed_notice: str) -> str:
    """Manifest role from the parsed notice: a notice that changes another (BT-758) is a
    change notice, whatever search found it. A version-1 notice can still be a change."""
    return "change_notice" if changed_notice else "contract_notice"


def fetch_notice(client: Client, number: str) -> Notice | None:
    status, ctype, body = client.get_xml(number)
    if status != 200 or "xml" not in ctype:
        print(f"  skip {number}: HTTP {status}, {ctype or 'no content type'}")
        return None
    try:
        info = parse_notice(number, body)
    except (ET.ParseError, ValueError) as exc:
        print(f"  skip {number}: {exc}")
        return None
    role = role_for(info["changed_notice"])
    return Notice(number, body, XML_URL.format(number=number), status, role, **info)


def base_query(prefix: str) -> str:
    return (
        f"buyer-country = NLD AND notice-type = cn-standard AND classification-cpv = {prefix}* "
        f"AND publication-date = {WINDOW}"
    )


def pub_key(number: str) -> tuple[int, int]:
    serial, year = number.split("-")
    return int(year), int(serial)


def find_change_pair(client: Client) -> tuple[Notice, Notice]:
    """Find a changed contract notice and its change notice, both in the window."""
    query = (
        f"buyer-country = NLD AND notice-type = cn-standard AND publication-date = {WINDOW} "
        "AND classification-cpv IN (72* 79* 48*) AND notice-version != 01"
    )
    data, _ = client.search(query)
    for hit in sorted(data.get("notices", []), key=lambda n: pub_key(n["publication-number"])):
        procedure = hit.get("procedure-identifier")
        if not procedure:
            continue
        related, _ = client.search(f"procedure-identifier = {procedure}", limit=20)
        for other in related.get("notices", []):
            ref = other.get("change-notice-version-identifier") or ""
            if other["publication-number"] == hit["publication-number"]:
                continue
            if not ref.startswith(hit["notice-identifier"]):
                continue
            original = fetch_notice(client, hit["publication-number"])
            change = fetch_notice(client, other["publication-number"])
            if not original or not change:
                continue
            same_procedure = original.procedure_id == change.procedure_id == procedure
            points_back = change.changed_notice.startswith(original.notice_id)
            group = original.main_cpv[:2]
            if same_procedure and points_back and group in QUOTAS:
                original.publication_date = hit.get("publication-date", "")
                change.publication_date = other.get("publication-date", "")
                return original, change
            print(
                f"  pair {hit['publication-number']} / {other['publication-number']} rejected: "
                f"same procedure {same_procedure}, BT-758 points back {points_back}, "
                f"main CPV {original.main_cpv}"
            )
    raise Stop("no change pair found in the window; widening the window is your decision")


CONTACT_BLOCK_RE = re.compile(r"<cac:Contact>.*?</cac:Contact>", re.DOTALL)
# If any of these is still present after redaction, the run stops instead of writing the file.
LEFTOVER_MARKERS = ("ElectronicMail", "Telephone", "Telefax", "cac:Contact")


def redact_contacts(text: str) -> str:
    """Remove every <cac:Contact> block (names, emails, phones; CLAUDE.md privacy rule)."""
    return CONTACT_BLOCK_RE.sub("", text)


def redact_notice(number: str, xml: bytes) -> tuple[bytes, int]:
    """Redact one downloaded notice and return (bytes to write, number of blocks removed).

    Raises Stop if contact markers remain (a variant the regex misses, such as a tag with
    attributes) or if the redacted XML no longer parses. Messages never contain notice text.
    """
    text = xml.decode("utf-8")
    blocks = len(CONTACT_BLOCK_RE.findall(text))
    redacted = redact_contacts(text)
    leftovers = [marker for marker in LEFTOVER_MARKERS if marker in redacted]
    if leftovers:
        raise Stop(f"{number}: contact markers remain after redaction: {', '.join(leftovers)}")
    out = redacted.encode("utf-8")
    try:
        ET.fromstring(out)
    except ET.ParseError as exc:
        raise Stop(f"{number}: redacted XML does not parse: {exc}") from None
    return out, blocks


def write_bytes(path: Path, data: bytes) -> str:
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


PUBLICATION_NUMBER_RE = re.compile(r"\d+-\d{4}")


def count_lots(xml: bytes) -> int:
    return len(ET.fromstring(xml).findall("cac:ProcurementProjectLot", NS))


def read_publication_date(xml: bytes) -> str | None:
    """OPP-012 (confirm), e.g. `2026-10-06+02:00`: the form the manifest already uses."""
    node = ET.fromstring(xml).find(".//efbc:PublicationDate", NS)
    return (node.text or "").strip() or None if node is not None else None


def add_notice(
    client: Client, number: str, out_dir: Path | None = None, min_lots: int = 1
) -> tuple[dict[str, Any], int]:
    """Fetch ONE notice (one request), redact it, save it and record it in the manifest.

    Stops with nothing written if the notice breaks a rule below or cannot be redacted.
    Returns (manifest entry, lot count). `client` only needs a `get_xml(number)` method.
    """
    out_dir = OUT_DIR if out_dir is None else out_dir
    if not PUBLICATION_NUMBER_RE.fullmatch(number):
        raise Stop(f"not a publication number: {number!r}")
    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        raise Stop(f"{manifest_path.name} not found; run the full fetch first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    xml_path = out_dir / f"{number}.xml"
    if xml_path.exists() or any(n["publication_number"] == number for n in manifest["notices"]):
        raise Stop(f"{number} is already a fixture")

    status, ctype, body = client.get_xml(number)
    if status != 200 or "xml" not in ctype:
        raise Stop(f"{number}: HTTP {status}, {ctype or 'no content type'}")
    try:
        info = parse_notice(number, body)
    except (ET.ParseError, ValueError) as exc:
        raise Stop(f"{number}: {exc}") from None
    group = info["main_cpv"][:2]
    if info["notice_type"] != "cn-standard":
        raise Stop(f"{number}: notice type {info['notice_type']!r}, expected cn-standard")
    if group not in QUOTAS:
        raise Stop(f"{number}: main CPV {info['main_cpv']!r} is not in groups {sorted(QUOTAS)}")
    if info["changed_notice"]:
        raise Stop(f"{number} is a change notice (BT-758), not a contract notice")
    lots = count_lots(body)
    if lots < min_lots:
        raise Stop(f"{number}: {lots} lot(s), expected at least {min_lots}")

    # Privacy (CLAUDE.md): redact before anything is written; raises Stop if contact data remains.
    redacted, blocks = redact_notice(number, body)
    digest = write_bytes(xml_path, redacted)
    entry = {
        "publication_number": number,
        "file": f"{number}.xml",
        "role": role_for(info["changed_notice"]),  # always contract_notice: changes are refused
        "source_url": XML_URL.format(number=number),
        "http_status": status,
        "sha256": digest,
        "redacted": True,
        "contact_blocks_removed": blocks,
        "notice_type": info["notice_type"],
        "cpv_group": group,
        "main_cpv": info["main_cpv"],
        "publication_date": read_publication_date(body),
        "procedure_id": info["procedure_id"],
        "notice_id": info["notice_id"],
        "changes_notice": None,
        "fetched_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    manifest["notices"].append(entry)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return entry, lots


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--force", action="store_true", help="overwrite existing fixtures")
    parser.add_argument("--add", metavar="NUMBER", help="fetch ONE more notice (one request)")
    parser.add_argument("--min-lots", type=int, default=1, help="with --add: minimum lot count")
    args = parser.parse_args(argv)

    if args.add:
        client = Client(delay=ADD_DELAY_SECONDS, wait_first=True)
        try:
            entry, lots = add_notice(client, args.add, min_lots=args.min_lots)
        except Stop as exc:
            print(f"STOPPED: {exc} (requests made: {client.requests})")
            return 2
        print(f"Added {entry['publication_number']}: {entry['notice_type']}, ", end="")
        print(f"CPV {entry['main_cpv']}, {lots} lots, published {entry['publication_date']}")
        print(f"  contact blocks removed: {entry['contact_blocks_removed']}")
        print(f"  saved: {(OUT_DIR / entry['file']).relative_to(REPO_ROOT)}")
        print(f"  requests made: {client.requests}")
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if any(OUT_DIR.iterdir()) and not args.force:
        print(f"{OUT_DIR.relative_to(REPO_ROOT)} is not empty; use --force to overwrite")
        return 1

    client = Client()
    fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        print("Change pair")
        original, change = find_change_pair(client)
        notices: list[Notice] = [original, change]
        counts = {prefix: 0 for prefix in QUOTAS}
        counts[original.main_cpv[:2]] += 1
        taken = {original.number, change.number}

        print("Contract notices")
        search_fixture: tuple[str, bytes] | None = None
        for prefix, quota in QUOTAS.items():
            data, raw = client.search(base_query(prefix))
            if search_fixture is None:
                search_fixture = (f"search_cn_{prefix}.json", raw)
            hits = sorted(data.get("notices", []), key=lambda n: pub_key(n["publication-number"]))
            for hit in hits:
                if counts[prefix] >= quota:
                    break
                number = hit["publication-number"]
                if number in taken or str(hit.get("notice-version", "1")).lstrip("0") != "1":
                    continue
                notice = fetch_notice(client, number)
                if not notice:
                    continue
                if not notice.main_cpv.startswith(prefix):
                    print(f"  skip {number}: main CPV {notice.main_cpv} not in group {prefix}")
                    continue
                notice.publication_date = hit.get("publication-date", "")
                notices.append(notice)
                taken.add(number)
                counts[prefix] += 1
            if counts[prefix] < quota:
                raise Stop(f"only {counts[prefix]} of {quota} notices for CPV {prefix}")

        print("Not-found responses")
        not_found = []
        for index, number in enumerate(NOT_FOUND, start=1):
            status, ctype, body = client.get_xml(number)
            if status == 200 and "xml" in ctype:
                raise Stop(f"{number} unexpectedly exists; choose another number")
            not_found.append((index, number, status, ctype, body))

        # Privacy (CLAUDE.md): redact every notice before anything is written to disk.
        redacted = {n.number: redact_notice(n.number, n.xml) for n in notices}
    except Stop as exc:
        print(f"STOPPED: {exc} (requests made: {client.requests})")
        return 2

    if search_fixture is not None:
        (OUT_DIR / search_fixture[0]).write_bytes(search_fixture[1])

    manifest: dict[str, Any] = {
        "fetched_at": fetched_at,
        "window": WINDOW,
        "requests": client.requests,
        "notices": [],
        "not_found": [],
        "change_pair": {"original": original.number, "change": change.number},
    }
    for notice in notices:
        xml, blocks_removed = redacted[notice.number]
        digest = write_bytes(OUT_DIR / f"{notice.number}.xml", xml)
        manifest["notices"].append(
            {
                "publication_number": notice.number,
                "file": f"{notice.number}.xml",
                "role": notice.role,
                "source_url": notice.url,
                "http_status": notice.status,
                "sha256": digest,
                "redacted": True,
                "contact_blocks_removed": blocks_removed,
                "notice_type": notice.notice_type,
                "cpv_group": notice.main_cpv[:2],
                "main_cpv": notice.main_cpv,
                "publication_date": notice.publication_date,
                "procedure_id": notice.procedure_id,
                "notice_id": notice.notice_id,
                "changes_notice": notice.changed_notice or None,
            }
        )
    for index, number, status, ctype, body in not_found:
        digest = write_bytes(OUT_DIR / f"not_found_{index}.body", body)
        (OUT_DIR / f"not_found_{index}.status").write_text(f"{status}\n")
        manifest["not_found"].append(
            {
                "label": f"not_found_{index}",
                "publication_number": number,
                "source_url": XML_URL.format(number=number),
                "http_status": status,
                "content_type": ctype,
                "sha256": digest,
            }
        )
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    print(f"\nSaved to {OUT_DIR.relative_to(REPO_ROOT)}")
    for n in manifest["notices"]:
        print(
            f"  {n['publication_number']:<13} {n['role']:<16} {n['notice_type']:<12} "
            f"CPV {n['main_cpv']:<9} {n['publication_date'][:10]}  procedure {n['procedure_id']}"
        )
    total_blocks = sum(n["contact_blocks_removed"] for n in manifest["notices"])
    print(f"  contact blocks removed: {total_blocks}")
    print(f"  change pair: {change.number} changes {original.number}")
    print(f"    BT-758 {change.changed_notice}")
    for n in manifest["not_found"]:
        print(f"  {n['label']}: {n['publication_number']} -> HTTP {n['http_status']}")
    print(f"  requests made: {client.requests}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
