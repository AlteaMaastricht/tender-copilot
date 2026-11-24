"""Save a few TenderNed publication details as test fixtures (plan M0, details only).

Usage:
    python scripts/fetch_tenderned_fixtures.py [409819 408259 406922]

For each publication: GET /publicaties/{id}, and the publication named by a rectification
field when one is set. Anonymous, standard library only, 2 s between requests, at most
6 requests, no retries, no redirects. Nothing else is requested: no document list, no
downloads.

Writes to tests/fixtures/tenderned/:
  - detail_<id>.json (and rectification_<id>.json), every key that the contact guard
    (agent.tools.contact_keys) flags removed, recursively;
  - manifest.json: URL path, status, SHA-256 and the removed key paths (keys, never values).

Prints key names and a few non-personal values. It never prints a removed value.
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
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from ingest.tenderned import BASE_URL, split_contacts  # noqa: E402

OUT_DIR = REPO_ROOT / "tests" / "fixtures" / "tenderned"
HOST = "www.tenderned.nl"
USER_AGENT = "tender-copilot-fixtures/0.1"
DELAY_SECONDS = 2.0
MAX_REQUESTS = 6
MAX_BYTES = 5 * 1024 * 1024
DEFAULT_IDS = ("409819", "408259", "406922")
# Values that are safe to print (no persons).
SHOW = (
    "publicatieId",
    "publicatieDatum",
    "sluitingsDatum",
    "typePublicatie",
    "nationaalOfEuropeesCode",
    "pbNummerTed",
    "publicatieIDLaatsteRectificatie",
)


class Stop(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
_state = {"requests": 0, "last": 0.0}


def get_json(path: str) -> tuple[int, bytes]:
    url = f"{BASE_URL}{path}"
    if not url.startswith(f"https://{HOST}/"):
        raise Stop("refusing to contact another host")
    if _state["requests"] >= MAX_REQUESTS:
        raise Stop(f"request budget of {MAX_REQUESTS} reached")
    wait = DELAY_SECONDS - (time.monotonic() - _state["last"])
    if wait > 0:
        time.sleep(wait)
    _state["last"] = time.monotonic()
    _state["requests"] += 1
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
    )
    try:
        with _OPENER.open(req, timeout=60) as resp:
            return resp.status, resp.read(MAX_BYTES)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        if status == 429 or status >= 500:
            raise Stop(f"HTTP {status} for {path}; stopping (no retries)") from None
        return status, b""
    except (urllib.error.URLError, TimeoutError) as exc:
        raise Stop(f"network error for {path}: {type(exc).__name__}") from None


def key_paths(value: Any, prefix: str = "") -> list[str]:
    """Every key path of a JSON value (keys only; list items share the path `[]`)."""
    if isinstance(value, dict):
        out: list[str] = []
        for key, item in value.items():
            here = f"{prefix}.{key}" if prefix else str(key)
            out.append(here)
            out += key_paths(item, here)
        return out
    if isinstance(value, list):
        return [p for item in value for p in key_paths(item, f"{prefix}[]")]
    return []


def rectification_ids(detail: dict[str, Any], own_id: str) -> list[tuple[str, str]]:
    """(key, id) for top-level keys that mention a rectification and hold another numeric ID."""
    found = []
    for key, value in detail.items():
        if "rectif" in key.lower() and isinstance(value, str | int) and not isinstance(value, bool):
            text = str(value)
            if re.fullmatch(r"\d{1,9}", text) and text != own_id:
                found.append((key, text))
    return found


def save(name: str, clean: dict[str, Any]) -> str:
    data = (json.dumps(clean, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode()
    (OUT_DIR / name).write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def fetch_one(pub_id: str, role: str, manifest: list[dict[str, Any]]) -> dict[str, Any] | None:
    path = f"/publicaties/{pub_id}"
    status, body = get_json(path)
    entry: dict[str, Any] = {"role": role, "path": path, "status": status}
    manifest.append(entry)
    if status != 200:
        print(f"  {path}: HTTP {status}")
        return None
    detail = json.loads(body)
    if not isinstance(detail, dict):
        raise Stop(f"{path}: not a JSON object")
    clean, removed = split_contacts(detail)
    prefix = "rectification" if role == "rectification" else "detail"
    file_name = f"{prefix}_{pub_id}.json"
    entry.update(
        file=file_name,
        sha256=save(file_name, clean),
        removed_key_paths=sorted(item["path"] for item in removed),
    )
    print(f"  {path}: HTTP 200, {len(clean)} top-level keys, {len(removed)} contact keys removed")
    return detail


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("publication_ids", nargs="*", default=list(DEFAULT_IDS))
    ids = parser.parse_args(argv).publication_ids
    for pub_id in ids:
        if not re.fullmatch(r"\d{1,9}", pub_id):
            parser.error(f"not a numeric publication ID: {pub_id!r}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    try:
        for pub_id in ids:
            print(f"Publication {pub_id}")
            detail = fetch_one(pub_id, "detail", manifest)
            if detail is None:
                continue
            print(f"  top-level keys: {', '.join(sorted(detail))}")
            for key in SHOW:
                if key in detail:
                    print(f"  {key}: {str(detail[key])[:60]}")
            for key, other in rectification_ids(detail, pub_id):
                print(f"  rectification key {key} -> {other}")
                if _state["requests"] < MAX_REQUESTS:
                    fetch_one(other, "rectification", manifest)
    finally:
        (OUT_DIR / "manifest.json").write_text(
            json.dumps({"requests": _state["requests"], "files": manifest}, indent=2) + "\n"
        )
    print(f"\nRequests made: {_state['requests']} (delay {DELAY_SECONDS:.0f} s)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Stop as exc:
        print(f"Stopped: {exc}")
        raise SystemExit(1) from None
