"""Tests for scripts/freeze_snapshot.py: fake transport only, manual folders in tmp_path.

TenderNed details and TED notices come from the saved fixtures; contact data is planted in
memory and must never reach the output or the cache. Nothing here touches the network.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest

from evals.snapshot import SnapshotError, load_tender, open_snapshot
from ingest.http import HttpClient, HttpError, HttpResponse

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "freeze_snapshot", ROOT / "scripts/freeze_snapshot.py"
)
fz = importlib.util.module_from_spec(spec)
sys.modules["freeze_snapshot"] = fz
spec.loader.exec_module(fz)

DOCS = ROOT / "tests" / "fixtures" / "documents"
TENDERNED = ROOT / "tests" / "fixtures" / "tenderned"
TED = ROOT / "tests" / "fixtures" / "ted"
DETAIL_URL = "https://www.tenderned.nl/papi/tenderned-rs-tns/v2/publicaties/{id}"
TED_URL = "https://ted.europa.eu/en/notice/{number}/xml"

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"\+\d{2}[ \d()-]{6,}\d")
TED_NAME = "Kanariepiet Jansen"
TED_EMAIL = "kanariepiet@kanariestad.example"
TED_PHONE = "+31 20 555 0199"
DETAIL_NAME = "Detailkanarie Visser"
DETAIL_EMAIL = "visser@kanariestad.example"
DETAIL_PHONE = "+31 20 555 0123"
CANARIES = (TED_NAME, DETAIL_NAME)
NEW_CLOSING = "2026-05-01T12:00:00"

A, RECT, B, C = "100001", "100002", "100003", "100006"  # A: rectified; B: no TED; C: plain
NO_FOLDER, EMPTY, UNLISTED = "100004", "100005", "100099"

CONTACT_BLOCK = (
    f"<cac:Contact><cbc:Name>{TED_NAME}</cbc:Name><cbc:Telephone>{TED_PHONE}</cbc:Telephone>"
    f"<cbc:ElectronicMail>{TED_EMAIL}</cbc:ElectronicMail></cac:Contact>"
)


def _json(name: str) -> dict[str, Any]:
    return json.loads((TENDERNED / name).read_text(encoding="utf-8"))


def _detail(publication_id: str, ted_number: str | None, rectification: str | None) -> dict:
    detail = _json("detail_409819.json")
    detail["publicatieId"] = int(publication_id)
    detail["pbNummerTed"] = ted_number
    detail["publicatieIDLaatsteRectificatie"] = int(rectification) if rectification else None
    detail["contactpersoon"] = {
        "naam": DETAIL_NAME,
        "email": DETAIL_EMAIL,
        "telefoon": DETAIL_PHONE,
    }
    return detail


def _ted_xml() -> bytes:
    text = (TED / "643202-2026.xml").read_text(encoding="utf-8")
    assert "<efac:Company>" in text
    return text.replace("<efac:Company>", "<efac:Company>" + CONTACT_BLOCK, 1).encode("utf-8")


def make_routes() -> dict[str, tuple[int, str, bytes]]:
    rectification = _json("rectification_413241.json")
    rectification["publicatieId"] = int(RECT)
    rectification["sluitingsDatum"] = NEW_CLOSING
    rectification["publicatieIDLaatsteRectificatie"] = None
    xml = _ted_xml()
    routes = {
        DETAIL_URL.format(id=A): _detail(A, "643202-2026", RECT),
        DETAIL_URL.format(id=RECT): rectification,
        DETAIL_URL.format(id=B): _detail(B, None, None),
        DETAIL_URL.format(id=C): _detail(C, "643202-2026", None),
    }
    out = {url: (200, "application/json", json.dumps(v).encode()) for url, v in routes.items()}
    out[TED_URL.format(number="643202-2026")] = (200, "application/xml", xml)
    return out


class Fake:
    def __init__(self, routes: dict[str, tuple[int, str, bytes]], fail_at: int | None = None):
        self.routes = routes
        self.fail_at = fail_at
        self.urls: list[str] = []
        self.now = 0.0
        self.sleeps: list[float] = []

    def transport(self, req: Any, timeout: float, max_bytes: int) -> HttpResponse:
        self.urls.append(req.full_url)
        if self.fail_at is not None and len(self.urls) == self.fail_at:
            raise HttpError("network error: boom")
        status, content_type, body = self.routes.get(req.full_url, (404, "", b""))
        return HttpResponse(status, content_type, {}, body)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def client(self, max_requests: int = 150) -> HttpClient:
        return HttpClient(
            transport=self.transport,
            sleep=self.sleep,
            clock=lambda: self.now,
            max_requests=max_requests,
        )


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return path


def fixture(name: str) -> bytes:
    return (DOCS / name).read_bytes()


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Path]:
    manual = tmp_path / "manual"
    for publication_id in (A, B, C, EMPTY, UNLISTED):
        (manual / publication_id).mkdir(parents=True)
    a = manual / A
    (a / "two_pages.pdf").write_bytes(fixture("two_pages.pdf"))
    (a / "scan.pdf").write_bytes(fixture("scan.pdf"))
    (a / "Kopie van twee paginas.pdf").write_bytes(fixture("two_pages.pdf"))  # same bytes
    (a / "old.doc").write_bytes(fixture("old.doc"))
    (a / "slides.pptx").write_bytes(fixture("slides.pptx"))
    (a / "sub").mkdir()
    (a / "sub" / "sheet.xlsx").write_bytes(fixture("sheet.xlsx"))
    (a / ".DS_Store").write_bytes(b"x")
    (a / ".download_ids.json").write_text('{"1": "two_pages.pdf"}')
    (a / "__MACOSX").mkdir()
    (a / "__MACOSX" / "junk.pdf").write_bytes(fixture("two_pages.pdf"))
    (a / "Thumbs.db").write_bytes(b"x")
    os.symlink(a / "two_pages.pdf", a / "link.pdf")
    (manual / B / "sections.docx").write_bytes(fixture("sections.docx"))
    (manual / C / "Programma van eisen.docx").write_bytes(fixture("no_headings.docx"))
    (manual / UNLISTED / "x.pdf").write_bytes(fixture("two_pages.pdf"))
    ids = tmp_path / "ids.txt"
    ids.write_text(f"{A}\n{B}\n{NO_FOLDER}\n{EMPTY}\n{C}\n")
    return {"manual": manual, "ids": ids, "tmp": tmp_path}


def options(world: dict[str, Path], name: str = "snap", **extra: Any) -> Any:
    tmp = world["tmp"]
    values: dict[str, Any] = {
        "manual": world["manual"],
        "out": tmp / name,
        "ids_path": world["ids"],
        "version": "v-test",
        "cache_root": tmp / "cache",
        "report_path": tmp / f"{name}_report.json",
    }
    values.update(extra)
    return fz.Options(**values)


def freeze(world: dict[str, Path], fake: Fake | None = None, **extra: Any) -> tuple[dict, Fake]:
    fake = fake or Fake(make_routes())
    report = fz.freeze(options(world, **extra), fake.client())
    return report, fake


def tender_docs(out: Path, publication_id: str) -> dict[str, Any]:
    path = out / "tenders" / publication_id / "documents.json"
    return json.loads(path.read_text(encoding="utf-8"))


# --- tenders, the list, folders ---------------------------------------------------------------


def test_the_snapshot_opens_and_the_rectification_gives_the_closing_date(world) -> None:
    report, _ = freeze(world)
    out = world["tmp"] / "snap"
    snapshot = open_snapshot(out)
    assert snapshot.version == "v-test" and snapshot.kb_version is None
    assert snapshot.tender_ids == (int(A), int(C))
    tender = load_tender(snapshot, int(A))
    assert tender.sluitingsdatum == NEW_CLOSING  # the rectification's value, not the original's
    assert tender.tenderned["sluitingsDatum"] == "2026-03-20T10:00:00"  # original kept
    assert load_tender(snapshot, int(C)).sluitingsdatum == "2026-03-20T10:00:00"
    assert report["tenders"][A]["rectification"] is True
    assert report["rectifications"] == [A]
    assert (out / "tenders" / A / "tenderned_rectification.json").is_file()
    assert (world["tmp"] / "snap_report.json").is_file()


def test_tenders_without_ted_notice_and_listed_problems_are_reported_not_failed(world) -> None:
    report, _ = freeze(world)
    assert report["no_ted_notice"] == [B]
    assert report["tenders"][B]["reason"] == "no_ted_number"
    assert report["missing_folder"] == [NO_FOLDER]
    assert report["empty_folder"] == [EMPTY]
    assert report["not_listed_folders"] == [UNLISTED]
    assert report["failures"] == {}
    out = world["tmp"] / "snap"
    assert not (out / "tenders" / B).exists()
    assert not (out / "tenders" / UNLISTED).exists()
    assert not (out / "tenders" / NO_FOLDER).exists()


def test_limit_caps_the_tenders(world) -> None:
    report, fake = freeze(world, limit=1)
    assert open_snapshot(world["tmp"] / "snap").tender_ids == (int(A),)
    assert C not in report["tenders"] or "status" not in report["tenders"][C]
    assert len(fake.urls) == 3


def test_read_ids(tmp_path: Path) -> None:
    path = tmp_path / "ids.txt"
    path.write_text("# first\n409819\n\n410334  # note\n409819\n")
    assert fz.read_ids(path) == ["409819", "410334"]
    path.write_text("409819\nabc\n")
    with pytest.raises(fz.FreezeError, match="line 2"):
        fz.read_ids(path)


def test_the_committed_id_list_has_the_final_20() -> None:
    ids = fz.read_ids(ROOT / "data" / "snapshot_ids.txt")
    assert len(ids) == 20 and len(set(ids)) == 20
    assert ids[0] == "409819" and ids[-1] == "409553"


# --- documents ---------------------------------------------------------------------------------


def test_documents_are_classified_deduplicated_and_unsupported_ones_listed(world) -> None:
    report, _ = freeze(world)
    out = world["tmp"] / "snap"
    docs = tender_docs(out, A)
    names = [row["filename"] for row in docs["documents"]]
    assert names == sorted(names)
    assert "link.pdf" not in names  # a symbolic link is skipped
    assert not any(n.startswith("__MACOSX") or ".DS_Store" in n or "Thumbs" in n for n in names)
    assert not any(".download_ids" in n for n in names)  # hidden: the download index
    assert ("two_pages.pdf" in names) != ("Kopie van twee paginas.pdf" in names)  # one row
    by_name = {row["filename"]: row for row in docs["documents"]}
    assert by_name["scan.pdf"]["extraction_status"] == "scanned"
    assert by_name["old.doc"]["extraction_status"] == "unsupported"
    assert by_name["slides.pptx"]["extraction_status"] == "unsupported"
    assert by_name["sub/sheet.xlsx"]["extraction_status"] == "ok"
    assert all(len(row["doc_key"]) == 12 for row in docs["documents"])
    assert all(
        row["published_at"] is None and row["language"] is None and row["is_current"]
        for row in docs["documents"]
    )
    pages = out / "tenders" / A / "pages"
    assert not (pages / f"{by_name['scan.pdf']['doc_key']}.jsonl").exists()  # no page file
    assert not (pages / f"{by_name['old.doc']['doc_key']}.jsonl").exists()
    assert (pages / f"{by_name['sub/sheet.xlsx']['doc_key']}.jsonl").is_file()

    entry = report["tenders"][A]
    assert len(entry["duplicates_skipped"]) == 1
    assert {u["extension"] for u in entry["unsupported"]} == {".doc", ".pptx"}
    assert entry["files_read_directly"] == len(docs["documents"])
    assert entry["skipped_entries"] >= 5  # DS_Store, download index, __MACOSX, Thumbs.db, link
    assert report["by_extraction_status"]["scanned"] == 1
    assert report["by_doc_type"]["other"] >= 1
    assert load_tender(open_snapshot(out), int(C)).docs.documents[0].doc_type == "requirements"


def test_document_ids_are_unique_across_tenders_and_stable_over_two_runs(world) -> None:
    freeze(world, name="one")
    report, fake = freeze(world, name="two")
    assert fake.urls == []  # the second run is served from the cache
    ids = []
    for name in ("one", "two"):
        out = world["tmp"] / name
        ids.append(
            [
                (t, d["document_id"], d["doc_key"])
                for t in (A, C)
                for d in tender_docs(out, t)["documents"]
            ]
        )
    assert ids[0] == ids[1]
    flat = [i[1] for i in ids[0]]
    assert len(flat) == len(set(flat)) and flat == sorted(flat)
    assert report["requests"] == 0


def test_a_doc_key_collision_stops_the_run(world, monkeypatch) -> None:
    monkeypatch.setattr(fz, "DOC_KEY_LENGTH", 0)
    with pytest.raises(fz.FreezeError, match="doc_key"):
        freeze(world)
    assert not (world["tmp"] / "snap").exists()


# --- privacy -----------------------------------------------------------------------------------


def _all_files(*roots: Path) -> list[Path]:
    return [p for root in roots for p in root.rglob("*") if p.is_file()]


def test_no_contact_data_in_the_output_the_cache_or_the_report(world) -> None:
    report, _ = freeze(world)
    out, cache = world["tmp"] / "snap", world["tmp"] / "cache"
    assert "<cac:Contact" not in (out / "tenders" / A / "notice.xml").read_text()
    for publication_id in (A, C):
        contacts = json.loads((out / "tenders" / publication_id / "contacts.json").read_text())
        assert list(contacts) == ["names"]
        assert TED_NAME in contacts["names"]
    assert DETAIL_NAME in json.loads((out / "tenders" / A / "contacts.json").read_text())["names"]
    files = [*_all_files(out, cache), world["tmp"] / "snap_report.json"]
    assert files
    for path in files:
        text = path.read_bytes().decode("utf-8", errors="ignore")
        assert not EMAIL_RE.search(text), path.name
        assert not PHONE_RE.search(text), path.name
        assert TED_PHONE not in text and DETAIL_PHONE not in text, path.name
        if path.name != "contacts.json":
            for canary in CANARIES:
                assert canary not in text, path.name
    detail = json.loads((out / "tenders" / A / "tenderned.json").read_text())
    assert "contactpersoon" not in detail
    assert "contactpersoon" not in json.loads(
        (out / "tenders" / A / "tenderned_rectification.json").read_text()
    )
    assert json.dumps(report).count(TED_NAME) == 0


# --- network: cache, resume, budget, details-only ----------------------------------------------


def test_a_rerun_after_a_failure_makes_only_the_missing_requests(world) -> None:
    routes = make_routes()
    failing = Fake(routes, fail_at=3)  # A: detail (1), rectification (2), TED notice (3) fails
    with pytest.raises(HttpError):
        fz.freeze(options(world), failing.client())
    assert not (world["tmp"] / "snap").exists()
    second = Fake(routes)
    fz.freeze(options(world), second.client())
    assert second.urls == [
        TED_URL.format(number="643202-2026"),  # A: the notice only
        DETAIL_URL.format(id=B),
        DETAIL_URL.format(id=C),
        TED_URL.format(number="643202-2026"),
    ]
    third = Fake(routes)
    fz.freeze(options(world, name="again"), third.client())
    assert third.urls == []


def test_requests_are_two_seconds_apart(world) -> None:
    _, fake = freeze(world)
    assert len(fake.urls) == 6
    assert fake.sleeps and all(s == 2.0 for s in fake.sleeps)


def test_the_request_budget_is_respected(world) -> None:
    fake = Fake(make_routes())
    with pytest.raises(HttpError, match="budget"):
        fz.freeze(options(world), fake.client(max_requests=2))
    assert len(fake.urls) == 2
    assert not (world["tmp"] / "snap").exists()


def test_a_missing_ted_notice_is_reported_and_the_run_goes_on(world) -> None:
    routes = make_routes()
    del routes[TED_URL.format(number="643202-2026")]  # the fake answers 404
    with pytest.raises(fz.FreezeError, match="no tender"):
        freeze(world, Fake(routes))
    routes = make_routes()
    routes[TED_URL.format(number="643202-2026")] = (200, "text/html", b"<html></html>")
    with pytest.raises(fz.FreezeError, match="no tender"):
        freeze(world, Fake(routes), name="html")


def test_details_only_writes_nothing_under_out(world) -> None:
    report, fake = freeze(world, details_only=True, version=None)
    assert not (world["tmp"] / "snap").exists()
    assert not (world["tmp"] / "snap.tmp").exists()
    assert report["tenders"][A]["ted_number"] == "643202-2026"
    assert report["tenders"][A]["rectification"] is True
    assert report["tenders"][A]["closing_date"] == NEW_CLOSING
    assert report["tenders"][C]["rectification"] is False
    assert report["tenders"][B]["status"] == "no_ted_notice"
    # details-only does not need folders: the listed IDs without one are checked too
    assert len(fake.urls) == 8


# --- --out: refuse, force, .prev ---------------------------------------------------------------


def test_an_existing_out_is_refused_without_force_and_kept_with_force(world) -> None:
    freeze(world)
    out = world["tmp"] / "snap"
    before = (out / "manifest.json").read_bytes()
    fake = Fake(make_routes())
    with pytest.raises(fz.FreezeError, match="--force"):
        fz.freeze(options(world), fake.client())
    assert fake.urls == [] and (out / "manifest.json").read_bytes() == before

    freeze(world, force=True, version="v-second")
    assert open_snapshot(out).version == "v-second"
    prev = world["tmp"] / "snap.prev"
    assert open_snapshot(prev).version == "v-test"
    with pytest.raises(fz.FreezeError, match="snap.prev"):  # the old copy is never deleted
        freeze(world, force=True, version="v-third")
    assert open_snapshot(prev).version == "v-test"


def test_the_snapshot_is_checked_before_it_gets_its_name(world, monkeypatch) -> None:
    def broken(_root: Path) -> None:
        raise SnapshotError("broken")

    monkeypatch.setattr(fz, "open_snapshot", broken)
    with pytest.raises(SnapshotError):
        freeze(world)
    assert not (world["tmp"] / "snap").exists()
    assert (world["tmp"] / "snap.tmp").is_dir()


# --- ZIP files ---------------------------------------------------------------------------------


@pytest.fixture
def one(tmp_path: Path) -> tuple[Path, Path]:
    """A manual folder (returned) and a work folder."""
    folder = tmp_path / "manual" / "1"
    folder.mkdir(parents=True)
    return folder, tmp_path / "work"


def read(folder: Path, work: Path) -> Any:
    files, _ = fz.list_files(folder)
    return fz.read_documents(files, work)


def test_a_zip_is_unpacked_classified_and_extracted(one) -> None:
    folder, work = one
    make_zip(
        folder / "Aanbestedingsstukken.zip",
        {
            "Programma van eisen.pdf": fixture("two_pages.pdf"),
            "docs/Bijlage 1.docx": fixture("sections.docx"),
            "map/": b"",
            "__MACOSX/._x.pdf": b"junk",
            ".verborgen.pdf": b"junk",
            "Thumbs.db": b"junk",
        },
    )
    before = sorted(p.name for p in folder.rglob("*"))
    docs = read(folder, work)
    assert [r.filename for r in docs.rows] == [
        "Aanbestedingsstukken.zip/Programma van eisen.pdf",
        "Aanbestedingsstukken.zip/docs/Bijlage 1.docx",
    ]
    by_name = {r.filename: r for r in docs.rows}
    pdf = by_name["Aanbestedingsstukken.zip/Programma van eisen.pdf"]
    assert (pdf.file_format, pdf.extraction_status, pdf.doc_type) == ("pdf", "ok", "requirements")
    assert len(pdf.pages) == 2
    word = by_name["Aanbestedingsstukken.zip/docs/Bijlage 1.docx"]
    assert (word.file_format, word.extraction_status, word.doc_type) == ("docx", "ok", "annex")
    assert docs.unpacked == 2 and docs.direct == 0
    assert docs.containers == [
        {
            "filename": "Aanbestedingsstukken.zip",
            "container": True,
            "status": "unpacked",
            "reason": None,
            "members": 2,
        }
    ]
    assert not any(r.filename == "Aanbestedingsstukken.zip" for r in docs.rows)  # not a document
    assert sorted(p.name for p in folder.rglob("*")) == before  # the manual folder is untouched
    assert (work / "Aanbestedingsstukken.zip" / "docs" / "Bijlage 1.docx").is_file()


def test_a_zip_is_found_by_content_not_by_extension(one) -> None:
    folder, work = one
    make_zip(folder / "stukken.bin", {"a.pdf": fixture("two_pages.pdf")})
    (folder / "rapport.zip").write_bytes(fixture("sections.docx"))  # a DOCX named .zip
    docs = read(folder, work)
    assert [c["filename"] for c in docs.containers] == ["stukken.bin"]
    assert sorted(r.filename for r in docs.rows) == ["rapport.zip", "stukken.bin/a.pdf"]
    assert {r.filename: r.file_format for r in docs.rows}["rapport.zip"] == "docx"


def test_unpacked_files_are_deduplicated_against_direct_files(one) -> None:
    folder, work = one
    (folder / "two_pages.pdf").write_bytes(fixture("two_pages.pdf"))
    make_zip(folder / "pack.zip", {"again.pdf": fixture("two_pages.pdf")})
    docs = read(folder, work)
    assert [r.filename for r in docs.rows] == ["two_pages.pdf"]
    assert docs.duplicates == ["pack.zip/again.pdf"]


def test_unsafe_member_paths_are_rejected_and_nothing_is_written_outside(one) -> None:
    folder, work = one
    make_zip(
        folder / "bad.zip",
        {
            "../evil.pdf": b"x",
            "/abs.pdf": b"x",
            "C:/drive.pdf": b"x",
            "..\\back.pdf": b"x",
            "ok/../../up.pdf": b"x",
            "fine.pdf": fixture("two_pages.pdf"),
        },
    )
    docs = read(folder, work)
    assert docs.unsafe_members == 5
    assert [r.filename for r in docs.rows] == ["bad.zip/fine.pdf"]
    assert docs.containers[0]["status"] == "unpacked"
    base = folder.parent.parent
    for name in ("evil.pdf", "abs.pdf", "drive.pdf", "back.pdf", "up.pdf"):
        assert not list(base.rglob(name))
        assert not Path("/abs.pdf").exists()


def test_symlink_members_are_skipped(one) -> None:
    folder, work = one
    with zipfile.ZipFile(folder / "links.zip", "w") as archive:
        link = zipfile.ZipInfo("link.pdf")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "/etc/passwd")
        archive.writestr("real.pdf", fixture("two_pages.pdf"))
    docs = read(folder, work)
    assert [r.filename for r in docs.rows] == ["links.zip/real.pdf"]
    assert docs.skipped_entries == 1


def test_an_archive_over_a_limit_is_unsupported(one, monkeypatch) -> None:
    folder, work = one
    make_zip(folder / "many.zip", {f"{n}.pdf": fixture("two_pages.pdf") for n in range(3)})
    make_zip(folder / "big.zip", {"x.txt": b"a" * 100})
    monkeypatch.setattr(fz, "MAX_ARCHIVE_FILES", 2)
    monkeypatch.setattr(fz, "MAX_ARCHIVE_BYTES", 50)
    docs = read(folder, work)
    states = {c["filename"]: (c["status"], c["reason"]) for c in docs.containers}
    assert states == {
        "big.zip": ("unsupported", "too_large"),
        "many.zip": ("unsupported", "too_many_files"),
    }
    assert docs.rows == []
    assert {u["filename"] for u in docs.unsupported} == {"big.zip", "many.zip"}
    assert not (work / "big.zip").exists() and not (work / "many.zip").exists()


def test_the_size_limit_is_also_enforced_while_reading(one, monkeypatch) -> None:
    folder, work = one
    make_zip(folder / "lie.zip", {"a.txt": b"a" * 40, "b.txt": b"b" * 40})
    real = zipfile.ZipFile.infolist

    def understate(self: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        infos = real(self)
        for info in infos:
            info.file_size = 1  # a header that lies about the size
        return infos

    monkeypatch.setattr(zipfile.ZipFile, "infolist", understate)
    monkeypatch.setattr(fz, "MAX_ARCHIVE_BYTES", 60)
    docs = read(folder, work)
    assert docs.containers[0]["status"] == "unsupported"
    assert docs.containers[0]["reason"] in {"too_large", "unreadable"}
    assert not (work / "lie.zip").exists()


def test_a_nested_zip_is_unpacked_one_level_only(one) -> None:
    folder, work = one
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as archive:
        archive.writestr("deep.pdf", fixture("scan.pdf"))
    make_zip(
        folder / "outer.zip", {"a.pdf": fixture("two_pages.pdf"), "inner.zip": inner.getvalue()}
    )
    docs = read(folder, work)
    assert [r.filename for r in docs.rows] == ["outer.zip/a.pdf"]
    states = {c["filename"]: (c["status"], c["reason"]) for c in docs.containers}
    assert states == {
        "outer.zip": ("unpacked", None),
        "outer.zip/inner.zip": ("unsupported", "nested_zip"),
    }
    assert docs.unsupported == [{"filename": "outer.zip/inner.zip", "extension": ".zip"}]
    assert not list(work.rglob("deep.pdf"))


def test_corrupt_encrypted_and_empty_archives_are_unsupported_not_a_crash(one) -> None:
    folder, work = one
    (folder / "corrupt.zip").write_bytes(b"PK\x03\x04" + b"\x00garbage" * 20)
    good = io.BytesIO()
    with zipfile.ZipFile(good, "w") as archive:
        archive.writestr("a.pdf", fixture("two_pages.pdf"))
    (folder / "cut.zip").write_bytes(good.getvalue()[:-30])  # no central directory
    (folder / "notzip.zip").write_bytes(b"this is not a zip")
    locked = bytearray(make_zip(folder / "locked.zip", {"secret.pdf": b"x"}).read_bytes())
    locked[locked.index(b"PK\x01\x02") + 8] |= 0x1  # the central directory says: encrypted
    (folder / "locked.zip").write_bytes(bytes(locked))
    make_zip(folder / "empty.zip", {})
    docs = read(folder, work)
    states = {c["filename"]: (c["status"], c["reason"]) for c in docs.containers}
    assert states["corrupt.zip"] == ("unsupported", "unreadable")
    assert states["cut.zip"] == ("unsupported", "unreadable")
    assert states["notzip.zip"] == ("unsupported", "unreadable")
    assert states["locked.zip"] == ("unsupported", "encrypted")
    assert states["empty.zip"] == ("unpacked", None)
    assert docs.rows == []
    assert {u["extension"] for u in docs.unsupported} == {".zip"}


def test_zip_records_reach_documents_json_and_the_report(world) -> None:
    make_zip(world["manual"] / C / "pack.zip", {"x/a.pdf": fixture("two_pages.pdf")})
    make_zip(world["manual"] / C / "nested.zip", {"inner.zip": b"PK\x03\x04junk"})
    report, _ = freeze(world)
    out = world["tmp"] / "snap"
    listing = tender_docs(out, C)
    assert [c["filename"] for c in listing["containers"]] == [
        "nested.zip",
        "nested.zip/inner.zip",
        "pack.zip",
    ]
    assert "pack.zip" not in [d["filename"] for d in listing["documents"]]
    assert "pack.zip/x/a.pdf" in [d["filename"] for d in listing["documents"]]
    assert set(listing["documents"][0]) == {
        "document_id",
        "doc_key",
        "filename",
        "doc_type",
        "is_current",
        "published_at",
        "extraction_status",
        "page_kind",
        "language",
    }
    entry = report["tenders"][C]
    assert entry["files_unpacked_from_zips"] == 1
    assert {z["filename"]: z["status"] for z in entry["zips"]} == {
        "pack.zip": "unpacked",
        "nested.zip": "unpacked",
        "nested.zip/inner.zip": "unsupported",
    }
    assert load_tender(open_snapshot(out), int(C)).docs.documents  # readers see documents only


def test_the_hidden_download_index_is_skipped(tmp_path: Path) -> None:
    folder = tmp_path / "1"
    folder.mkdir()
    (folder / ".download_ids.json").write_text("{}")
    (folder / "a.pdf").write_bytes(fixture("two_pages.pdf"))
    files, skipped = fz.list_files(folder)
    assert [name for name, _ in files] == ["a.pdf"] and skipped == 1


# --- command line --------------------------------------------------------------------------------


def test_main_prints_the_report_last_on_stdout_and_library_hints_go_to_stderr(
    world, monkeypatch, capsys
) -> None:
    fake = Fake(make_routes())
    monkeypatch.setattr(fz, "HttpClient", lambda max_requests: fake.client(max_requests))
    original = fz.read_documents

    def noisy(files: Any, work: Path) -> Any:
        print("MuPDF hint on stdout")
        return original(files, work)

    monkeypatch.setattr(fz, "read_documents", noisy)
    code = fz.main(
        [
            "--from-manual", str(world["manual"]),
            "--out", str(world["tmp"] / "cli"),
            "--version", "v-cli",
            "--ids", str(world["ids"]),
            "--cache", str(world["tmp"] / "cli_cache"),
            "--report", str(world["tmp"] / "cli_report.json"),
        ]
    )  # fmt: skip
    captured = capsys.readouterr()
    assert code == 0
    assert "MuPDF hint" in captured.err and "MuPDF hint" not in captured.out
    assert json.loads(captured.out)["tenders_in_snapshot"] == 2
    open_snapshot(world["tmp"] / "cli")


def test_main_returns_1_when_the_network_stops_the_run(world, monkeypatch, capsys) -> None:
    fake = Fake(make_routes(), fail_at=1)
    monkeypatch.setattr(fz, "HttpClient", lambda max_requests: fake.client(max_requests))
    code = fz.main(
        [
            "--from-manual", str(world["manual"]),
            "--out", str(world["tmp"] / "cli"),
            "--version", "v",
            "--ids", str(world["ids"]),
            "--cache", str(world["tmp"] / "c"),
            "--report", str(world["tmp"] / "r.json"),
        ]
    )  # fmt: skip
    assert code == 1
    assert "freeze stopped" in capsys.readouterr().err
    assert not (world["tmp"] / "cli").exists()


def test_the_version_is_required_unless_details_only(world) -> None:
    with pytest.raises(fz.FreezeError, match="--version"):
        fz.freeze(options(world, version=None), Fake(make_routes()).client())


# --- git-ignored paths -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["data/snapshot.tmp/", "data/snapshot.prev/", "data/snapshot_report.json", "data/raw/manual/"],
)
def test_generated_paths_are_git_ignored(path: str) -> None:
    result = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT, check=False)
    assert result.returncode == 0, f"{path} is not git-ignored"


def test_the_id_list_is_not_git_ignored() -> None:
    result = subprocess.run(
        ["git", "check-ignore", "-q", "data/snapshot_ids.txt"], cwd=ROOT, check=False
    )
    assert result.returncode == 1


def test_the_cache_folder_is_git_ignored() -> None:
    result = subprocess.run(
        ["git", "check-ignore", "-q", "data/raw/snapshot/409819/unpacked/x.zip/a.pdf"],
        cwd=ROOT,
        check=False,
    )
    assert result.returncode == 0


def test_xlsb_is_reported_as_unsupported_as_other(one) -> None:
    folder, work = one
    make_zip(folder / "Prijzen.xlsb", {"xl/workbook.bin": b"x"})
    docs = read(folder, work)
    assert [(r.file_format, r.extraction_status) for r in docs.rows] == [("other", "unsupported")]
    assert docs.unsupported == [{"filename": "Prijzen.xlsb", "extension": ".xlsb"}]


def test_office_lock_files_are_skipped_and_reported(one) -> None:
    folder, work = one
    (folder / "~$Bijlage.docx").write_bytes(b"lock")
    (folder / "Programma van eisen.pdf").write_bytes(fixture("two_pages.pdf"))
    make_zip(
        folder / "stukken.zip",
        {
            "~$Prijzen.xlsx": b"lock",
            "map/~$x.docx": b"lock",
            "Bijlage.docx": fixture("sections.docx"),
        },
    )
    docs = read(folder, work)
    assert sorted(r.filename for r in docs.rows) == [
        "Programma van eisen.pdf",
        "stukken.zip/Bijlage.docx",
    ]
    assert all("~$" not in r.filename for r in docs.rows)
    assert sorted(docs.lock_files) == [
        "stukken.zip/map/~$x.docx",
        "stukken.zip/~$Prijzen.xlsx",
        "~$Bijlage.docx",
    ]


def test_zip_members_differing_only_in_case_do_not_overwrite_each_other(one) -> None:
    folder, work = one
    with zipfile.ZipFile(folder / "case.zip", "w") as archive:
        archive.writestr("Bijlage.pdf", fixture("two_pages.pdf"))
        archive.writestr("BIJLAGE.pdf", fixture("sections.docx"))
    unpacked = fz.unpack_zip(folder / "case.zip", work / "case")
    names = sorted(name for name, _ in unpacked.members)
    assert names == ["BIJLAGE (2).pdf", "Bijlage.pdf"]
    assert len({name.lower() for name in names}) == 2
    assert {p.read_bytes() for _, p in unpacked.members} == {
        fixture("two_pages.pdf"),
        fixture("sections.docx"),
    }
