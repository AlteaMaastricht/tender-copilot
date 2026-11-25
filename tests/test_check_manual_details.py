"""Tests for scripts/check_manual_details.py with a fake transport. No network."""

import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from ingest.http import HttpClient, HttpError, HttpResponse

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "check_manual_details", ROOT / "scripts" / "check_manual_details.py"
)
cmd = importlib.util.module_from_spec(_spec)
sys.modules["check_manual_details"] = cmd
_spec.loader.exec_module(cmd)

FIXTURES = Path(__file__).parent / "fixtures" / "tenderned"
BASE = json.loads((FIXTURES / "detail_409819.json").read_text(encoding="utf-8"))


def _client(bodies: dict[str, object], sleeps: list[float] | None = None, max_requests: int = 40):
    seen: list[str] = []

    def transport(req, timeout, max_bytes):
        pub = req.full_url.rsplit("/", 1)[1]
        seen.append(req.full_url)
        spec = bodies.get(pub, 404)
        if isinstance(spec, int):
            return HttpResponse(spec, "", {}, b"")
        return HttpResponse(200, "application/json", {}, json.dumps(spec).encode())

    clock = [0.0]
    http = HttpClient(
        transport=transport,
        sleep=(sleeps.append if sleeps is not None else lambda s: None),
        clock=lambda: clock[0],
        max_requests=max_requests,
    )
    return http, seen


def _detail(ted: str | None, **extra) -> dict:
    d = dict(BASE)
    d.pop("pbNummerTed", None)
    if ted is not None:
        d["pbNummerTed"] = ted
    d.update(extra)
    return d


def test_check_rows_and_request_count() -> None:
    bodies = {"1": _detail("60877-2026"), "2": _detail(None), "3": _detail("")}
    http, seen = _client(bodies)
    groups = {
        "1": ("79", "2026-01"),
        "2": ("72", "2026-02"),
        "3": ("48", "2026-03"),
        "4": ("48", "2026-04"),
    }
    rows = cmd.run_check(http, groups, ["1", "2"], ["3", "4"])
    assert len(seen) == http.requests == 4
    assert all(
        u == f"https://www.tenderned.nl/papi/tenderned-rs-tns/v2/publicaties/{u[-1]}" for u in seen
    )
    assert [r["has_ted"] for r in rows] == [True, False, False, False]
    assert rows[0]["pb_nummer_ted"] == "60877-2026"
    assert rows[0]["sluitingsdatum"] == "2026-03-20T10:00:00"
    assert rows[3]["status"] == "404"
    assert rows[0]["cpv_group"] == "79" and rows[1]["month"] == "2026-02"


def test_contacts_are_stripped_and_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "Sanne Geheim"
    bodies = {"1": _detail("60877-2026", contactpersoon={"naam": secret, "email": "a@b.example"})}
    http, _ = _client(bodies)
    seen: list[tuple[dict, list]] = []
    real = cmd.split_contacts

    def spy(detail):
        clean, removed = real(detail)
        seen.append((clean, removed))
        return clean, removed

    monkeypatch.setattr(cmd, "split_contacts", spy)
    rows = cmd.run_check(http, {"1": ("79", "2026-01")}, ["1"], [])
    assert len(seen) == 1
    clean, removed = seen[0]
    assert secret in json.dumps(removed) and secret not in json.dumps(clean)
    assert "a@b.example" not in json.dumps(clean)
    out = tmp_path / "out.csv"
    cmd.write_csv(rows, out)
    text = out.read_text(encoding="utf-8")
    assert secret not in text and "a@b.example" not in text
    assert next(csv.reader(text.splitlines())) == list(cmd.COLUMNS)


def test_200_with_a_non_json_body_stops_the_run() -> None:
    def transport(req, timeout, max_bytes):
        return HttpResponse(200, "text/html", {}, b"<html>not json</html>")

    http = HttpClient(transport=transport, sleep=lambda s: None, clock=lambda: 0.0)
    rows: list = []
    with pytest.raises(HttpError):
        cmd.run_check(http, {}, ["1", "2"], [], rows=rows)
    assert rows == []


def test_budget_is_respected() -> None:
    http, seen = _client({str(i): _detail("1-2026") for i in range(5)}, max_requests=3)
    rows: list = []
    with pytest.raises(HttpError, match="budget"):
        cmd.run_check(http, {}, ["0", "1", "2"], ["3", "4"], rows=rows)
    assert len(seen) == 3 and len(rows) == 3


def test_server_error_stops_but_404_does_not() -> None:
    http, seen = _client({"1": 404, "2": 503, "3": _detail("1-2026")})
    rows: list = []
    with pytest.raises(HttpError):
        cmd.run_check(http, {}, ["1", "2", "3"], [], rows=rows)
    assert len(seen) == 2 and len(rows) == 1 and rows[0]["status"] == "404"


def _row(pid, role, group, month, ted):
    return {
        "publication_id": pid,
        "role": role,
        "cpv_group": group,
        "month": month,
        "status": "200",
        "pb_nummer_ted": "1-2026" if ted else "",
        "has_ted": ted,
        "sluitingsdatum": "",
    }


def test_replacement_same_group_in_order_skipping_reserve_without_ted() -> None:
    rows = [
        _row("p1", "provisional", "79", "2026-01", True),
        _row("p2", "provisional", "72", "2026-02", False),
        _row("p3", "provisional", "79", "2026-03", False),
        _row("p4", "provisional", "79", "2026-04", False),
        _row("r1", "reserve", "48", "2026-01", True),
        _row("r2", "reserve", "79", "2026-02", False),
        _row("r3", "reserve", "79", "2026-03", True),
        _row("r4", "reserve", "72", "2026-04", True),
        _row("r5", "reserve", "79", "2026-05", True),
    ]
    final, reps, unfilled = cmd.final_selection(
        rows, ["p1", "p2", "p3", "p4"], ["r1", "r2", "r3", "r4", "r5"]
    )
    assert final == ["p1", "r4", "r3", "r5"]
    assert [(r["old"], r["new"]) for r in reps] == [("p2", "r4"), ("p3", "r3"), ("p4", "r5")]
    assert unfilled == []


def test_not_enough_reserves_is_reported() -> None:
    rows = [
        _row("p1", "provisional", "79", "2026-01", False),
        _row("p2", "provisional", "79", "2026-02", False),
        _row("r1", "reserve", "79", "2026-03", True),
    ]
    final, reps, unfilled = cmd.final_selection(rows, ["p1", "p2"], ["r1"])
    assert final == ["r1"] and len(reps) == 1 and unfilled == ["p2"]


def test_lists_and_groups_match_the_selection_file() -> None:
    groups = cmd.load_groups()
    assert len(cmd.PROVISIONAL) == len(cmd.RESERVE) == 20
    assert set(cmd.PROVISIONAL + cmd.RESERVE) <= set(groups)
    assert not set(cmd.PROVISIONAL) & set(cmd.RESERVE)
