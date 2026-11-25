"""TenderNed details check for the manual snapshot (PRE-M4 a): which IDs have a TED notice.

Usage:
    python scripts/check_manual_details.py

One anonymous GET per ID (details only): /publicaties/{id}. 2 s between requests, at most 40
requests, no retries, no redirects. No rectification, TED, document list or download request.
A 429, a 5xx or a network error stops the run. Contact data is stripped at once and never
written. Writes data/raw/manual_details_check.csv (git-ignored): a summary, one row per ID.
Then prints the final 20: each provisional ID without a TED notice is replaced by the next
unused reserve ID of the same CPV group that has one.
"""

from __future__ import annotations

import csv
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from ingest.http import HttpClient, HttpError  # noqa: E402
from ingest.ted import normalise_ted_number  # noqa: E402
from ingest.tenderned import CLOSING_KEY, TED_NUMBER_KEY, TenderNedClient, split_contacts  # noqa: E402

SELECTION_CSV = REPO_ROOT / "data" / "snapshot_selection.csv"
OUT_CSV = REPO_ROOT / "data" / "raw" / "manual_details_check.csv"
COLUMNS = (
    "publication_id",
    "role",
    "cpv_group",
    "month",
    "status",
    "pb_nummer_ted",
    "has_ted",
    "sluitingsdatum",
)
PROVISIONAL = [
    "409819",
    "410334",
    "408865",
    "413590",
    "413464",
    "417895",
    "413124",
    "423040",
    "417894",
    "427033",
    "422544",
    "430966",
    "417763",
    "427081",
    "409515",
    "413504",
    "430137",
    "422800",
    "417896",
    "409553",
]
RESERVE = [
    "423003",
    "426975",
    "427069",
    "412797",
    "431004",
    "417742",
    "409575",
    "430247",
    "420246",
    "412501",
    "417671",
    "427087",
    "408985",
    "422498",
    "428959",
    "426672",
    "412548",
    "431048",
    "409191",
    "409376",
]


def load_groups(path: Path = SELECTION_CSV) -> dict[str, tuple[str, str]]:
    """publication_id -> (CPV group = first two digits of cpv_main, month = YYYY-MM published)."""
    with path.open(encoding="utf-8", newline="") as handle:
        return {
            row["publication_id"]: (row["cpv_main"][:2], row["published_at"][:7])
            for row in csv.DictReader(handle)
        }


def check_one(client: TenderNedClient, pub_id: str) -> dict[str, Any]:
    """One detail GET. Returns the summary fields only; contact data is dropped at once."""
    try:
        detail = client.detail(pub_id)
    except HttpError as exc:
        # Only a plain 4xx is a row. A 2xx that failed (not JSON, over the cap), a 3xx, a 429,
        # a 5xx or a network error stops the run.
        if exc.status is None or not 400 <= exc.status < 500 or exc.status == 429:
            raise
        return {
            "status": str(exc.status),
            "pb_nummer_ted": "",
            "has_ted": False,
            "sluitingsdatum": "",
        }
    clean, _removed = split_contacts(detail)
    del _removed
    raw = clean.get(TED_NUMBER_KEY)
    has_ted = normalise_ted_number(raw) is not None
    closing = clean.get(CLOSING_KEY)
    return {
        "status": "200",
        "pb_nummer_ted": raw if isinstance(raw, str) else "",
        "has_ted": has_ted,
        "sluitingsdatum": closing if isinstance(closing, str) else "",
    }


def run_check(
    http: HttpClient,
    groups: Mapping[str, tuple[str, str]],
    provisional: Sequence[str] = PROVISIONAL,
    reserve: Sequence[str] = RESERVE,
    rows: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Check every ID once. `rows` is filled as it goes, so a stop keeps the partial result."""
    rows = rows if rows is not None else []
    client = TenderNedClient(http)
    for role, ids in (("provisional", provisional), ("reserve", reserve)):
        for pub_id in ids:
            group, month = groups.get(pub_id, ("", ""))
            row = {"publication_id": pub_id, "role": role, "cpv_group": group, "month": month}
            row.update(check_one(client, pub_id))
            rows.append(row)
    return rows


def final_selection(
    rows: Sequence[Mapping[str, Any]],
    provisional: Sequence[str] = PROVISIONAL,
    reserve: Sequence[str] = RESERVE,
) -> tuple[list[str], list[dict[str, str]], list[str]]:
    """(final IDs, replacements, unfilled IDs).

    Each provisional ID without a TED notice is replaced, in place, by the next unused reserve
    ID (in reserve order) of the same CPV group that has one. The reserve order is already
    round-robin over months, so taking the next one keeps the spread as far as it allows.
    An ID with no replacement is left out of the final list and named in `unfilled`.
    """
    by_id = {str(r["publication_id"]): r for r in rows}
    used: set[str] = set()
    final: list[str] = []
    replacements: list[dict[str, str]] = []
    unfilled: list[str] = []
    for pub_id in provisional:
        row = by_id.get(pub_id)
        if row is not None and row["has_ted"]:
            final.append(pub_id)
            continue
        reason = "no row" if row is None else f"no TED notice (status {row['status']})"
        group = row["cpv_group"] if row is not None else None
        new = next(
            (
                r
                for r in reserve
                if r not in used
                and r in by_id
                and by_id[r]["has_ted"]
                and by_id[r]["cpv_group"] == group
            ),
            None,
        )
        if new is None:
            unfilled.append(pub_id)
            continue
        used.add(new)
        final.append(new)
        replacements.append({"old": pub_id, "new": new, "reason": reason})
    return final, replacements, unfilled


def write_csv(rows: Sequence[Mapping[str, Any]], path: Path = OUT_CSV) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in COLUMNS})


def main() -> int:
    groups = load_groups()
    total = len(PROVISIONAL) + len(RESERVE)
    http = HttpClient(max_requests=total)
    rows: list[dict[str, Any]] = []
    try:
        run_check(http, groups, rows=rows)
    except (HttpError, ValueError) as exc:
        print(f"Stopped after {len(rows)} of {total} IDs: {exc}")
        return 1
    finally:
        write_csv(rows)
        print(f"Requests made: {http.requests}; wrote {OUT_CSV.relative_to(REPO_ROOT)}")
    print("id role cpv month status has_ted pbNummerTed sluitingsdatum")
    for r in rows:
        print(
            r["publication_id"], r["role"], r["cpv_group"], r["month"], r["status"],
            r["has_ted"], r["pb_nummer_ted"], r["sluitingsdatum"],
        )  # fmt: skip
    final, replacements, unfilled = final_selection(rows)
    print(f"\nFinal {len(final)}:")
    for pub_id in final:
        group, month = groups.get(pub_id, ("", ""))
        print(f"  {pub_id} CPV {group} {month}")
    for rep in replacements:
        print(f"Replaced {rep['old']} -> {rep['new']}: {rep['reason']}")
    for pub_id in unfilled:
        print(f"NOT REPLACED {pub_id}: no unused reserve with a TED notice in its CPV group")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
