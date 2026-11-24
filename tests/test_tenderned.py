"""Tests for ingest/tenderned.py: saved details plus synthetic JSON. No network."""

import json
from pathlib import Path

import pytest

from agent.tools import contact_keys
from ingest.http import HttpClient, HttpResponse
from ingest.tenderned import (
    TenderNedClient,
    contact_names,
    effective_sluitingsdatum,
    rectification_id,
    split_contacts,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tenderned"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# Every name, address and number below is invented.
PLANTED = {
    "aanbestedingNaam": "Test",
    "opdrachtgeverNaam": "Gemeente Voorbeeld",
    "contactpersoon": {
        "naam": "Sanne van der Berg",
        "email": "s.vanderberg@voorbeeld.example",
        "telefoon": "+31 00 000 0000",
    },
    "aanspreekpunt": {"naam": "Piet Kanariepiet", "afdeling": "Inkoop"},
    "lijst": [{"emailadres": "x@voorbeeld.example", "titel": "keep"}],
}


def test_split_contacts_removes_contacts_and_keeps_the_organisation() -> None:
    clean, removed = split_contacts(PLANTED)
    assert clean["opdrachtgeverNaam"] == "Gemeente Voorbeeld"
    assert "contactpersoon" not in clean
    assert clean["aanspreekpunt"] == {"afdeling": "Inkoop"}  # the name under a person key goes
    assert clean["lijst"] == [{"titel": "keep"}]  # nested lists are walked
    assert contact_keys(clean) == []
    assert {entry["path"] for entry in removed} == {
        "contactpersoon",
        "aanspreekpunt.naam",
        "lijst[0].emailadres",
    }
    assert PLANTED["contactpersoon"]["naam"] == "Sanne van der Berg"  # input untouched


def test_split_contacts_leaves_the_saved_details_unchanged() -> None:
    for name in ("detail_409819.json", "rectification_413241.json", "detail_406922.json"):
        detail = _fixture(name)
        clean, removed = split_contacts(detail)
        assert clean == detail and removed == []


def test_contact_names_returns_names_only() -> None:
    _, removed = split_contacts(PLANTED)
    names = contact_names(removed)
    assert names == ["Sanne van der Berg", "Piet Kanariepiet"]
    text = " ".join(names)
    assert "@" not in text and "+31" not in text


def test_contact_names_skips_numbers_and_emails_under_a_whole_contact_key() -> None:
    _, removed = split_contacts({"contactEmail": "a@voorbeeld.example", "telefoon": "0612345678"})
    assert contact_names(removed) == []
    _, removed = split_contacts({"contactpersoon": "Sanne van der Berg"})
    assert contact_names(removed) == ["Sanne van der Berg"]


def test_effective_sluitingsdatum_prefers_the_rectification() -> None:
    original = {"sluitingsDatum": "2026-03-20T10:00:00"}
    assert effective_sluitingsdatum(original, {"sluitingsDatum": "2026-04-01T12:00:00"}) == (
        "2026-04-01T12:00:00"
    )


@pytest.mark.parametrize(
    "rectification", [None, {}, {"sluitingsDatum": None}, {"sluitingsDatum": " "}]
)
def test_effective_sluitingsdatum_falls_back_to_the_original(rectification) -> None:
    original = {"sluitingsDatum": "2026-03-20T10:00:00"}
    assert effective_sluitingsdatum(original, rectification) == "2026-03-20T10:00:00"


def test_effective_sluitingsdatum_none_when_neither_has_one() -> None:
    assert effective_sluitingsdatum({}, {}) is None
    assert effective_sluitingsdatum(None, None) is None


def test_saved_rectifications_keep_the_closing_date_of_the_original() -> None:
    for original, rectified in (
        ("detail_409819.json", "rectification_413241.json"),
        ("detail_408259.json", "rectification_418437.json"),
    ):
        detail, rect = _fixture(original), _fixture(rectified)
        assert effective_sluitingsdatum(detail, rect) == rect["sluitingsDatum"]
        assert rect["sluitingsDatum"] == detail["sluitingsDatum"]  # as saved on 24 Nov 2026


def test_rectification_id() -> None:
    assert rectification_id(_fixture("detail_409819.json")) == "413241"
    assert rectification_id(_fixture("rectification_413241.json")) is None  # its own ID
    assert rectification_id(_fixture("detail_406922.json")) is None  # no field
    assert rectification_id({"publicatieId": 1, "publicatieIDLaatsteRectificatie": 2}) == "2"
    assert rectification_id({"publicatieIDLaatsteRectificatie": "../x"}) is None
    assert rectification_id({"publicatieIDLaatsteRectificatie": True}) is None


def test_client_detail_requests_the_publication_endpoint() -> None:
    urls: list[str] = []

    def transport(req, timeout, max_bytes):
        urls.append(req.full_url)
        return HttpResponse(200, "application/json", {}, b'{"publicatieId": 409819}')

    client = TenderNedClient(HttpClient(transport=transport, sleep=lambda _: None))
    assert client.detail(409819) == {"publicatieId": 409819}
    assert urls == ["https://www.tenderned.nl/papi/tenderned-rs-tns/v2/publicaties/409819"]
    with pytest.raises(ValueError):
        client.detail("../409819")
    assert len(urls) == 1
