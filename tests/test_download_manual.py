"""Tests for scripts/download_manual.py: fake transport, fake clock, synthetic list fixture."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from ingest.http import HttpClient, HttpError, HttpResponse

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "download_manual", ROOT / "scripts" / "download_manual.py"
)
dm = importlib.util.module_from_spec(spec)
sys.modules["download_manual"] = dm
spec.loader.exec_module(dm)

FIXTURE = json.loads((ROOT / "tests/fixtures/tenderned/synthetic/documents.json").read_bytes())
LIST_URL = "https://www.tenderned.nl/papi/tenderned-rs-tns/v2/publicaties/100001/documenten"
FILE_BASE = "https://www.tenderned.nl/papi/tenderned-rs-tns/v2/publicaties/100001/documenten/"


class Fake:
    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.urls: list[str] = []
        self.times: list[float] = []
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def transport(self, req: Any, timeout: float, max_bytes: int) -> HttpResponse:
        self.urls.append(req.full_url)
        self.times.append(self.now)
        value = self.routes.get(req.full_url, 404)
        if isinstance(value, int):
            return HttpResponse(value, "", {}, b"")
        if isinstance(value, Exception):
            raise value
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        return HttpResponse(200, "", {}, body)

    def client(self, max_requests: int = 50) -> HttpClient:
        return HttpClient(
            transport=self.transport, sleep=self.sleep, clock=self.clock, max_requests=max_requests
        )


def routes(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        LIST_URL: FIXTURE,
        FILE_BASE + "1001": b"one",
        FILE_BASE + "1002": b"two",
        FILE_BASE + "1003": b"three",
        FILE_BASE + "1004": b"four",
        FILE_BASE + "1006": b"zipbytes",
    }
    base.update(extra)
    return base


def run(fake: Fake, root: Path, max_requests: int = 50) -> dict[str, Any]:
    report: dict[str, Any] = {}
    dm.run_download(fake.client(max_requests), ["100001"], root, report)
    return report["tenders"]["100001"]


def test_downloads_clash_traversal_foreign_host_and_relative_href(tmp_path: Path) -> None:
    fake = Fake(routes())
    entry = run(fake, tmp_path)
    folder = tmp_path / "100001"
    names = sorted(p.name for p in folder.iterdir() if p.name != dm.INDEX_NAME)
    assert names == sorted(
        [
            "Leidraad.pdf",
            "Leidraad (aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa02).pdf",
            "Bestaand.pdf",
            "_._.hidden_evil_name.pdf",
            "Archief.zip",
        ]
    )
    assert (folder / "Archief.zip").read_bytes() == b"zipbytes"  # left packed
    assert entry["listed"] == 6 and entry["downloaded"] == 5
    assert entry["list_items_unusable"] == 1
    assert entry["failed"] == [{"status": None, "reason": "href_refused"}]
    assert not any("evil.example.com" in u for u in fake.urls)
    assert fake.urls[1] == FILE_BASE + "1001"  # relative href resolved


def test_existing_file_on_disk_is_not_overwritten(tmp_path: Path) -> None:
    folder = tmp_path / "100001"
    folder.mkdir()
    (folder / "Leidraad.pdf").write_bytes(b"by hand")
    run(Fake(routes()), tmp_path)
    assert (folder / "Leidraad.pdf").read_bytes() == b"by hand"
    assert (folder / "Leidraad (aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01).pdf").read_bytes() == b"one"
    assert (folder / "Leidraad (aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa02).pdf").read_bytes() == b"two"
    assert not list(folder.glob(".part-*"))


def test_second_run_does_not_overwrite_suffixed_files(tmp_path: Path) -> None:
    folder = tmp_path / "100001"
    folder.mkdir()
    (folder / "Leidraad.pdf").write_bytes(b"by hand")
    (folder / "Leidraad (aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01).pdf").write_bytes(b"old")
    entry = run(Fake(routes()), tmp_path)
    assert (folder / "Leidraad (aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01).pdf").read_bytes() == b"old"
    assert entry["failed"] == [{"status": None, "reason": "name_taken"}] + [
        f for f in entry["failed"] if f["reason"] != "name_taken"
    ]
    assert "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01" not in json.loads(
        (folder / dm.INDEX_NAME).read_text()
    )


def test_duplicate_document_id_is_skipped(tmp_path: Path) -> None:
    doubled = {"documenten": FIXTURE["documenten"][:1] * 2}
    entry = run(Fake(routes(**{LIST_URL: doubled})), tmp_path)
    assert entry["downloaded"] == 1 and entry["skipped"] == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "_._etc_passwd"),
        ("..\\..\\x.pdf", "_._x.pdf"),
        (".hidden.pdf", "hidden.pdf"),
        ("a\x00b\x1f.pdf", "ab.pdf"),
        ("a..b.pdf", "a.b.pdf"),
        ("", "fallback1"),
        ("...", "fallback1"),
        ("Nota van inlichtingen.pdf", "Nota van inlichtingen.pdf"),
    ],
)
def test_sanitise_name(raw: str, expected: str) -> None:
    out = dm.sanitise_name(raw, "fallback1")
    assert out == expected
    assert "/" not in out and "\\" not in out and ".." not in out and not out.startswith(".")


def test_sanitise_name_limits_length() -> None:
    out = dm.sanitise_name("x" * 400 + ".pdf", "id")
    assert len(out.encode()) <= dm.MAX_NAME_BYTES + 4 and out.endswith(".pdf")


def test_404_on_one_file_continues(tmp_path: Path) -> None:
    entry = run(Fake(routes(**{FILE_BASE + "1002": 404})), tmp_path)
    assert {"status": 404, "reason": "http_error"} in entry["failed"]
    assert entry["downloaded"] == 4


def test_503_stops_the_run(tmp_path: Path) -> None:
    fake = Fake(routes(**{FILE_BASE + "1002": 503}))
    with pytest.raises(HttpError):
        run(fake, tmp_path)
    assert FILE_BASE + "1003" not in fake.urls


@pytest.mark.parametrize("bad", [429, HttpError("network error: URLError")])
def test_429_and_network_error_stop(tmp_path: Path, bad: Any) -> None:
    with pytest.raises(HttpError):
        run(Fake(routes(**{FILE_BASE + "1001": bad})), tmp_path)


def test_list_404_continues_to_next_tender(tmp_path: Path) -> None:
    fake = Fake(routes())
    report: dict[str, Any] = {}
    dm.run_download(fake.client(), ["999999", "100001"], tmp_path, report)
    assert report["tenders"]["999999"]["list_failed"]["status"] == 404
    assert report["tenders"]["100001"]["downloaded"] == 5


def test_unknown_first_list_shape_stops(tmp_path: Path) -> None:
    with pytest.raises(dm.ListShapeError):
        run(Fake(routes(**{LIST_URL: {"total": 0}})), tmp_path)


def test_file_over_size_cap_is_a_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dm, "MAX_FILE_BYTES", 4)
    entry = run(Fake(routes()), tmp_path)
    assert {"status": 200, "reason": "over_size_cap"} in entry["failed"]
    assert entry["downloaded"] == 3  # "one", "two", "four" fit; "three" and "zipbytes" do not


def test_budget_is_respected(tmp_path: Path) -> None:
    fake = Fake(routes())
    with pytest.raises(HttpError):
        run(fake, tmp_path, max_requests=3)
    assert len(fake.urls) == 3


def test_two_seconds_between_requests(tmp_path: Path) -> None:
    fake = Fake(routes())
    run(fake, tmp_path)
    gaps = [b - a for a, b in zip(fake.times, fake.times[1:], strict=False)]
    assert gaps and all(g >= 2.0 for g in gaps)


P = "/papi/tenderned-rs-tns/v2/publicaties/"


def test_href_rules() -> None:
    assert (
        dm.resolve_href(P + "1/documenten/2/content")
        == "https://www.tenderned.nl" + P + "1/documenten/2/content"
    )
    assert dm.resolve_href("http://www.tenderned.nl/x") is None
    assert dm.resolve_href("https://ted.europa.eu/x") is None
    assert dm.resolve_href("//evil.example.com/x") is None
    assert dm.resolve_href(P + "1/documenten/zip") is None
    assert dm.resolve_href(P + "1/public-xml") is None


def test_report_is_written(tmp_path: Path) -> None:
    report: dict[str, Any] = {}
    run_entry = Fake(routes())
    dm.run_download(run_entry.client(), ["100001"], tmp_path / "files", report)
    path = tmp_path / "report.json"
    dm.write_report(report, path)
    data = json.loads(path.read_text())
    assert data["tenders"]["100001"]["downloaded"] == 5
    assert "Leidraad" not in path.read_text()


def test_second_run_makes_only_the_list_request_and_writes_nothing(tmp_path: Path) -> None:
    run(Fake(routes()), tmp_path)
    folder = tmp_path / "100001"
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    assert dm.INDEX_NAME in before
    fake = Fake(routes())
    entry = run(fake, tmp_path)
    assert fake.urls == [LIST_URL]
    assert entry["downloaded"] == 0 and entry["skipped"] == 5
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before


def test_index_records_each_file_as_it_is_written(tmp_path: Path) -> None:
    fake = Fake(routes(**{FILE_BASE + "1003": 503}))
    with pytest.raises(HttpError):
        run(fake, tmp_path)
    index = json.loads((tmp_path / "100001" / dm.INDEX_NAME).read_text())
    assert set(index) == {"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa01", "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa02"}


def test_missing_index_is_empty_and_damaged_index_stops_before_any_request(
    tmp_path: Path,
) -> None:
    folder = tmp_path / "100001"
    folder.mkdir()
    assert dm.load_index(folder) == {}
    (folder / dm.INDEX_NAME).write_text("not json")
    with pytest.raises(dm.IndexDamagedError):
        dm.load_index(folder)
    fake = Fake(routes())
    with pytest.raises(dm.IndexDamagedError):
        run(fake, tmp_path)
    assert fake.urls == []


def test_unsafe_document_id_is_unusable_and_never_in_a_path(tmp_path: Path) -> None:
    evil = dict(FIXTURE["documenten"][0], documentId="x/../y")
    ok = dict(FIXTURE["documenten"][1], documentNaam="Leidraad.pdf")
    clash = dict(FIXTURE["documenten"][0], documentId="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa09")
    listing, bad = dm.parse_listing({"documenten": [evil, ok]})
    assert [d.document_id for d in listing] == [ok["documentId"]] and bad == 1
    entry = run(Fake(routes(**{LIST_URL: {"documenten": [clash, evil, ok]}})), tmp_path)
    assert entry["list_items_unusable"] == 1
    for p in tmp_path.rglob("*"):
        assert p.resolve().is_relative_to(tmp_path.resolve())


def test_list_object_key_rules() -> None:
    item = FIXTURE["documenten"][0]
    assert len(dm.parse_listing({"documenten": [item], "other": [1]})[0]) == 1
    assert len(dm.parse_listing({"x": [item]})[0]) == 1
    with pytest.raises(dm.ListShapeError):
        dm.parse_listing({"a": [item], "b": [item]})
    with pytest.raises(dm.ListShapeError):
        dm.parse_listing({"documenten": [{"documentId": "x"}]})
    assert dm.parse_listing({"documenten": []}) == ([], 0)


def test_href_port_path_and_trailing_slash() -> None:
    ok = "/papi/tenderned-rs-tns/v2/publicaties/1/documenten/2/content"
    assert dm.resolve_href(ok) is not None
    assert dm.resolve_href("https://www.tenderned.nl:8443" + ok) is None
    assert dm.resolve_href("/papi/tenderned-rs-tns/v2/publicaties/1/documenten/zip/") is None
    assert dm.resolve_href("/other/path/1") is None
    assert dm.resolve_href(ok + "?x=1") is None
