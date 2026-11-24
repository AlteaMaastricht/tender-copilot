"""Checks on the saved TenderNed detail fixtures (privacy and manifest)."""

import hashlib
import json
import re
from pathlib import Path

import pytest

from agent.tools import contact_keys
from ingest.tenderned import CLOSING_KEY, ID_KEY, RECTIFICATION_KEY, TED_NUMBER_KEY

FIXTURES = Path(__file__).parent / "fixtures" / "tenderned"
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE_RE = re.compile(r"(?<!\d)(?:\+31|0031|0)[\s\-]?\d(?:[\s\-]?\d){8}(?!\d)")
FILES = sorted(p for p in FIXTURES.glob("*.json") if p.name != "manifest.json")


def test_fixtures_exist() -> None:
    assert len(FILES) >= 3, "the checks below would pass without checking anything"


def test_every_fixture_is_in_the_manifest_with_its_hash() -> None:
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    listed = {entry["file"]: entry["sha256"] for entry in manifest["files"] if "file" in entry}
    assert set(listed) == {p.name for p in FILES}
    for path in FILES:
        assert hashlib.sha256(path.read_bytes()).hexdigest() == listed[path.name], path.name


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_contact_keys(path: Path) -> None:
    assert contact_keys(json.loads(path.read_text(encoding="utf-8"))) == [], path.name


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_no_email_or_phone_shaped_string(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert not EMAIL_RE.search(text), f"{path.name} has an email-shaped string"
    assert not PHONE_RE.search(text), f"{path.name} has a phone-shaped string"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.name)
def test_confirmed_field_names_are_present(path: Path) -> None:
    detail = json.loads(path.read_text(encoding="utf-8"))
    for key in (ID_KEY, TED_NUMBER_KEY, CLOSING_KEY):
        assert key in detail, f"{path.name} lacks {key}"
    assert re.fullmatch(r"\d+-\d{4}", detail[TED_NUMBER_KEY])


def test_rectification_field_points_at_another_publication() -> None:
    detail = json.loads((FIXTURES / "detail_409819.json").read_text(encoding="utf-8"))
    other = json.loads((FIXTURES / "rectification_413241.json").read_text(encoding="utf-8"))
    assert str(detail[RECTIFICATION_KEY]) == str(other[ID_KEY]) != str(detail[ID_KEY])
