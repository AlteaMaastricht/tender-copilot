"""No-network tests for scripts/fetch_ted_fixtures.py: contact data must never be written.

A fake client returns canned bytes (a saved fixture with fake <cac:Contact> blocks added) and
a spy records every file write, so these tests prove that nothing containing contact markup
reaches the disk, not only that the final files are clean.
"""

import hashlib
import importlib.util
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "ted"
MARKERS = ("ElectronicMail", "Telephone", "Telefax", "cac:Contact")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
NEW_NUMBER = "123456-2026"  # not a real notice: canned bytes stand in for the download

CONTACT = (
    "<cac:Contact><cbc:Name>Fake Person</cbc:Name>"
    "<cbc:Telephone>+31 00 000 0000</cbc:Telephone>"
    "<cbc:Telefax>+31 00 000 0001</cbc:Telefax>"
    "<cbc:ElectronicMail>fake.person@example.org</cbc:ElectronicMail></cac:Contact>"
)
CONTACT_MULTILINE = (
    "<cac:Contact>\n    <cbc:Name>Fake Two</cbc:Name>\n"
    "    <cbc:ElectronicMail>two@example.org</cbc:ElectronicMail>\n  </cac:Contact>"
)
CONTACT_WITH_ATTRIBUTE = (
    '<cac:Contact id="x"><cbc:ElectronicMail>three@example.org</cbc:ElectronicMail></cac:Contact>'
)


def _load_fetch_script():
    spec = importlib.util.spec_from_file_location(
        "fetch_ted_fixtures", ROOT / "scripts" / "fetch_ted_fixtures.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses looks the module up while it is being executed
    spec.loader.exec_module(module)
    return module


fetch = _load_fetch_script()


def _fixture_text(publication_number: str) -> str:
    return (FIXTURES / f"{publication_number}.xml").read_text(encoding="utf-8")


def inject_contacts(text: str) -> tuple[str, int]:
    """Add three plain contact blocks (one multi-line) to a saved, already redacted notice."""
    assert "<efac:Company>" in text and "</efac:Company>" in text
    text = text.replace("<efac:Company>", "<efac:Company>" + CONTACT + CONTACT_MULTILINE, 1)
    text = text.replace("</efac:Company>", CONTACT + "</efac:Company>", 1)
    return text, 3


class FakeClient:
    """Stands in for the TED client; `get_xml` is the only method add_notice needs."""

    def __init__(self, responses: dict[str, tuple[int, str, bytes]]) -> None:
        self.responses = responses
        self.requests = 0
        self.search_results: dict[str, dict] = {}

    def get_xml(self, number: str) -> tuple[int, str, bytes]:
        self.requests += 1
        return self.responses.get(number, (404, "application/json", b'{"error":"not found"}'))

    def search(self, query: str, limit: int = 50) -> tuple[dict, bytes]:
        self.requests += 1
        return self.search_results[query.split("classification-cpv = ")[1][:2]], b'{"notices": []}'


@pytest.fixture
def out_dir(tmp_path: Path) -> Path:
    folder = tmp_path / "ted"
    folder.mkdir()
    (folder / "manifest.json").write_text(json.dumps({"notices": []}) + "\n", encoding="utf-8")
    return folder


@pytest.fixture
def writes(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Path, str]]:
    """Record (path, text) for every Path.write_bytes / Path.write_text from now on."""
    log: list[tuple[Path, str]] = []
    original_bytes, original_text = Path.write_bytes, Path.write_text

    def spy_bytes(self: Path, data: bytes) -> int:
        log.append((self, data.decode("utf-8", errors="replace")))
        return original_bytes(self, data)

    def spy_text(self: Path, data: str, *args: object, **kwargs: object) -> int:
        log.append((self, data))
        return original_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_bytes", spy_bytes)
    monkeypatch.setattr(Path, "write_text", spy_text)
    return log


def assert_no_contact_data(text: str, where: str) -> None:
    for marker in MARKERS:
        assert marker not in text, f"{where} contains {marker}"
    assert not EMAIL_RE.search(text), f"{where} contains an email-shaped string"


# --- redact_notice ------------------------------------------------------------


def test_redact_notice_removes_every_contact_block() -> None:
    base = _fixture_text("643202-2026")
    injected, count = inject_contacts(base)
    redacted, blocks = fetch.redact_notice("643202-2026", injected.encode("utf-8"))
    assert blocks == count == 3
    assert_no_contact_data(redacted.decode("utf-8"), "redacted notice")
    assert redacted == base.encode("utf-8")  # only the injected blocks were removed
    ET.fromstring(redacted)


# --- add path (one notice) ----------------------------------------------------


def test_add_notice_writes_only_redacted_xml(out_dir: Path, writes: list) -> None:
    base = _fixture_text("643202-2026")
    injected, count = inject_contacts(base)
    client = FakeClient({NEW_NUMBER: (200, "application/xml", injected.encode("utf-8"))})
    writes.clear()

    entry, lots = fetch.add_notice(client, NEW_NUMBER, out_dir=out_dir)

    assert client.requests == 1
    assert lots == 1
    assert entry["contact_blocks_removed"] == count
    assert entry["redacted"] is True
    assert entry["publication_date"] == "2026-09-18+02:00"
    saved = (out_dir / f"{NEW_NUMBER}.xml").read_bytes()
    assert saved == base.encode("utf-8")
    assert hashlib.sha256(saved).hexdigest() == entry["sha256"]
    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["notices"] == [entry]
    assert {p.name for p, _ in writes} == {f"{NEW_NUMBER}.xml", "manifest.json"}
    for path, text in writes:  # every single write, in order: none may carry contact data
        assert_no_contact_data(text, f"write to {path.name}")


def test_add_notice_accepts_enough_lots(out_dir: Path) -> None:
    base = _fixture_text("643202-2026")
    lot = re.search(r"<cac:ProcurementProjectLot>.*?</cac:ProcurementProjectLot>", base, re.DOTALL)
    assert lot
    two_lots = base.replace(lot.group(0), lot.group(0) + lot.group(0), 1)
    client = FakeClient({NEW_NUMBER: (200, "application/xml", two_lots.encode("utf-8"))})
    _, lots = fetch.add_notice(client, NEW_NUMBER, out_dir=out_dir, min_lots=2)
    assert lots == 2


def test_add_notice_stops_on_unredactable_variant(out_dir: Path, writes: list) -> None:
    injected = _fixture_text("643202-2026").replace(
        "<efac:Company>", "<efac:Company>" + CONTACT_WITH_ATTRIBUTE, 1
    )
    client = FakeClient({NEW_NUMBER: (200, "application/xml", injected.encode("utf-8"))})
    writes.clear()
    with pytest.raises(fetch.Stop, match="contact markers remain"):
        fetch.add_notice(client, NEW_NUMBER, out_dir=out_dir)
    assert writes == []  # nothing at all was written
    assert not list(out_dir.glob("*.xml"))
    assert json.loads((out_dir / "manifest.json").read_text(encoding="utf-8")) == {"notices": []}


@pytest.mark.parametrize(
    ("case", "response", "message", "min_lots"),
    [
        ("too few lots", (200, "application/xml", _fixture_text("643202-2026").encode()),
         r"1 lot\(s\), expected at least 2", 2),
        ("change notice", (200, "application/xml", _fixture_text("777400-2026").encode()),
         "change notice", 1),
        ("not found", (404, "application/json", b'{"error":"x"}'), "HTTP 404", 1),
    ],
    ids=lambda v: v if isinstance(v, str) and " " in v else "",
)  # fmt: skip
def test_add_notice_refuses_and_writes_nothing(
    out_dir: Path, writes: list, case: str, response: tuple, message: str, min_lots: int
) -> None:
    client = FakeClient({NEW_NUMBER: response})
    writes.clear()
    with pytest.raises(fetch.Stop, match=message):
        fetch.add_notice(client, NEW_NUMBER, out_dir=out_dir, min_lots=min_lots)
    assert writes == []
    assert not list(out_dir.glob("*.xml"))


def test_add_notice_refuses_an_existing_fixture(out_dir: Path, writes: list) -> None:
    (out_dir / "manifest.json").write_text(
        json.dumps({"notices": [{"publication_number": NEW_NUMBER}]}), encoding="utf-8"
    )
    client = FakeClient({})
    writes.clear()
    with pytest.raises(fetch.Stop, match="already a fixture"):
        fetch.add_notice(client, NEW_NUMBER, out_dir=out_dir)
    assert client.requests == 0 and writes == []


def test_add_notice_rejects_a_bad_number_without_any_request(out_dir: Path) -> None:
    client = FakeClient({})
    with pytest.raises(fetch.Stop, match="not a publication number"):
        fetch.add_notice(client, "../../etc/passwd", out_dir=out_dir)
    assert client.requests == 0


# --- bulk path (main) ---------------------------------------------------------

CHANGE_PAIR = ("664976-2026", "777400-2026")
# CPV group -> publication numbers, matching QUOTAS (the change pair's original counts as a 79)
CONTRACT_NOTICES = {
    "72": ["643806-2026", "646325-2026", "647200-2026", "648029-2026"],
    "79": ["642990-2026", "643202-2026"],
    "48": ["643670-2026", "643994-2026"],
}


def test_bulk_main_writes_only_redacted_fixtures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, writes: list
) -> None:
    all_numbers = [*CHANGE_PAIR, *(n for numbers in CONTRACT_NOTICES.values() for n in numbers)]
    injected = {n: inject_contacts(_fixture_text(n))[0] for n in all_numbers}
    responses = {n: (200, "application/xml", text.encode("utf-8")) for n, text in injected.items()}

    client = FakeClient(responses)
    client.search_results = {
        prefix: {"notices": [{"publication-number": n, "notice-version": 1} for n in numbers]}
        for prefix, numbers in CONTRACT_NOTICES.items()
    }
    monkeypatch.setattr(fetch, "Client", lambda: client)
    monkeypatch.setattr(
        fetch,
        "find_change_pair",
        lambda c: (
            fetch.fetch_notice(c, CHANGE_PAIR[0], "contract_notice"),
            fetch.fetch_notice(c, CHANGE_PAIR[1], "change_notice"),
        ),
    )
    monkeypatch.setattr(fetch, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(fetch, "OUT_DIR", tmp_path / "ted")
    writes.clear()

    assert fetch.main([]) == 0

    for path, text in writes:
        assert_no_contact_data(text, f"write to {path.name}")
    manifest = json.loads((tmp_path / "ted" / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["notices"]) == 10
    for entry in manifest["notices"]:
        saved = (tmp_path / "ted" / entry["file"]).read_bytes()
        assert saved == (FIXTURES / entry["file"]).read_bytes()  # identical to the real fixture
        assert entry["contact_blocks_removed"] == 3
        assert hashlib.sha256(saved).hexdigest() == entry["sha256"]


# --- the 2-second rule --------------------------------------------------------


class _FakeResponse:
    status = 200
    headers = {"Content-Type": "application/xml"}

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self) -> bytes:
        return b"<x/>"


class _FakeOpener:
    def open(self, req: object, timeout: int) -> _FakeResponse:
        return _FakeResponse()


def test_add_mode_delay_is_at_least_two_seconds() -> None:
    assert fetch.ADD_DELAY_SECONDS >= 2.0


def test_add_mode_client_waits_before_its_first_request(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(fetch.time, "sleep", sleeps.append)
    monkeypatch.setattr(fetch, "_OPENER", _FakeOpener())
    client = fetch.Client(delay=fetch.ADD_DELAY_SECONDS, wait_first=True)
    client.get_xml(NEW_NUMBER)
    assert client.requests == 1
    assert len(sleeps) == 1 and sleeps[0] >= fetch.ADD_DELAY_SECONDS - 0.1


def test_default_client_does_not_wait_before_its_first_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(fetch.time, "sleep", sleeps.append)
    monkeypatch.setattr(fetch, "_OPENER", _FakeOpener())
    fetch.Client().get_xml(NEW_NUMBER)
    assert sleeps == []
