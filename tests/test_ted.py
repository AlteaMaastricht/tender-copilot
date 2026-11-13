"""Tests for ingest/ted.py, using only the saved fixtures in tests/fixtures/ted/.

Cases the real files lack (placeholder deadline, missing field, a contact block) are made
by editing fixture bytes in memory; no extra fixture files are written.
"""

import dataclasses
import re
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ingest.ted import (
    TedNotice,
    TedParseError,
    find_original,
    group_by_procedure,
    parse_notice,
    parse_notice_file,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ted"
XML_FILES = sorted(FIXTURES.glob("*.xml"))
CET = timezone(timedelta(hours=1))
CEST = timezone(timedelta(hours=2))


def _xml(publication_id: str) -> str:
    return (FIXTURES / f"{publication_id}.xml").read_text(encoding="utf-8")


def _parse_text(text: str) -> TedNotice:
    return parse_notice(text.encode("utf-8"))


@pytest.fixture(scope="module")
def notices() -> dict[str, TedNotice]:
    return {n.publication_id: n for n in (parse_notice_file(p) for p in XML_FILES)}


# --- parsing all fixtures -----------------------------------------------------


def test_all_fixtures_parse_and_ids_match_file_names(notices: dict[str, TedNotice]) -> None:
    assert len(XML_FILES) == 10
    assert sorted(notices) == sorted(p.stem for p in XML_FILES)


EXPECTED = [
    # id, procedure, cpv_main, buyer starts with, value, deadline date, kind, project_id
    ("642990-2026", "open", "79220000", "Gemeente Bloemendaal", None, date(2026, 11, 17),
     "tender", "PRJ-2600168-HMS 202605"),
    ("643202-2026", "open", "79200000", "HZ / HZ University", None, date(2026, 12, 1),
     "tender", "26.021"),
    ("643670-2026", "open", "48000000", "Gemeente Nijkerk", None, date(2026, 11, 30),
     "tender", None),
    ("643806-2026", "open", "72261000", "Samenwerkingsorganisatie De Wolden", Decimal("6000000"),
     date(2026, 10, 21), "tender", "Z.536549"),
    ("643994-2026", "open", "48000000", "Gemeente Lansingerland", None, date(2026, 11, 16),
     "tender", "D26.00264"),
    ("646325-2026", "open", "72000000", "European Union Agency for Criminal Justice",
     Decimal("13500000"), date(2026, 11, 4), "tender", "EUROJUST/HAG/2026/OP/0005"),
    ("647200-2026", "open", "72300000", "Veiligheidsregio Kennemerland", Decimal("250000"),
     date(2026, 11, 30), "tender", None),
    ("648029-2026", "open", "72222300", "Gemeente Den Helder", Decimal("1845000"),
     date(2026, 11, 20), "tender", "558827"),
    ("664976-2026", "neg-w-call", "79710000", "TenneT TSO", Decimal("30000000"),
     date(2026, 10, 29), "participation", "9974e71c-599e-47f6-b0e3-5b00ee9ab35b"),
    ("777400-2026", "neg-w-call", "79710000", "TenneT TSO", Decimal("30000000"),
     date(2026, 10, 29), "participation", "9974e71c-599e-47f6-b0e3-5b00ee9ab35b"),
]  # fmt: skip


@pytest.mark.parametrize(
    ("pub", "procedure", "cpv", "buyer", "value", "deadline", "kind", "project"),
    EXPECTED,
    ids=[row[0] for row in EXPECTED],
)
def test_notice_fields(
    notices: dict[str, TedNotice],
    pub: str,
    procedure: str,
    cpv: str,
    buyer: str,
    value: Decimal | None,
    deadline: date,
    kind: str,
    project: str | None,
) -> None:
    n = notices[pub]
    assert n.notice_type == "cn-standard"
    assert n.form_type == "competition"
    assert n.procedure_type == procedure
    assert n.cpv_main == cpv
    assert n.buyer_name is not None and n.buyer_name.startswith(buyer)
    assert n.country == "NLD"
    assert n.estimated_value == value
    assert n.deadline is not None and n.deadline.date() == deadline
    assert n.deadline_kind == kind
    assert n.deadline_status == "ok"
    assert len(n.lots) == 1
    assert n.project_id == project


# --- specific fields ----------------------------------------------------------


def test_joint_procurement_has_two_buyers(notices: dict[str, TedNotice]) -> None:
    n = notices["642990-2026"]
    assert [b.org_id for b in n.buyers] == ["ORG-0001", "ORG-0002"]
    assert n.buyer_name == "Gemeente Bloemendaal"
    assert n.lots[0].duration_months == 96


def test_english_notice(notices: dict[str, TedNotice]) -> None:
    n = notices["646325-2026"]
    assert n.language == "ENG"
    assert n.title is not None and n.title.startswith("Provision of ICT services")
    assert n.lots[0].place_country is None
    assert (n.estimated_value, n.currency) == (Decimal("13500000"), "EUR")


def test_framework_value_and_participation_deadline(notices: dict[str, TedNotice]) -> None:
    n = notices["664976-2026"]
    assert n.framework_max_value == Decimal("30000000")
    assert n.lots[0].tender_deadline is None
    assert n.lots[0].participation_deadline == n.deadline
    assert n.deadline == datetime(2026, 10, 29, 11, 0, tzinfo=UTC)


def test_cpv_codes_main_first_without_duplicates(notices: dict[str, TedNotice]) -> None:
    n = notices["643202-2026"]
    assert n.cpv_codes[0] == n.cpv_main
    assert len(n.cpv_codes) == 4
    assert len(set(n.cpv_codes)) == len(n.cpv_codes)


def test_deadlines_are_timezone_aware(notices: dict[str, TedNotice]) -> None:
    assert notices["643806-2026"].deadline == datetime(2026, 10, 21, 23, 59, tzinfo=CEST)
    assert notices["646325-2026"].deadline == datetime(2026, 11, 4, 16, 0, 59, tzinfo=CET)


# --- missing fields -----------------------------------------------------------


@pytest.mark.parametrize("pub", ["642990-2026", "643202-2026", "643670-2026", "643994-2026"])
def test_missing_estimated_value_is_none_with_warning(
    notices: dict[str, TedNotice], pub: str
) -> None:
    assert notices[pub].estimated_value is None
    assert "missing:estimated_value" in notices[pub].warnings


def test_missing_optional_fields_are_none(notices: dict[str, TedNotice]) -> None:
    assert notices["643670-2026"].project_id is None
    assert notices["647200-2026"].project_id is None
    assert notices["664976-2026"].buyers[0].legal_type is None
    assert notices["777400-2026"].buyers[0].legal_type is None


def test_missing_procedure_id_raises() -> None:
    text = re.sub(r"<cbc:ContractFolderID>[^<]*</cbc:ContractFolderID>", "", _xml("643202-2026"))
    with pytest.raises(TedParseError, match="procedure_id"):
        _parse_text(text)


@pytest.mark.parametrize("name", ["not_found_1.body", "search_cn_72.json"])
def test_non_notice_files_raise(name: str) -> None:
    with pytest.raises(TedParseError):
        parse_notice((FIXTURES / name).read_bytes())


# --- placeholder and missing deadlines ----------------------------------------


def _set_tender_end_date(text: str, new_date: str) -> str:
    return re.sub(
        r"(<cac:TenderSubmissionDeadlinePeriod>.*?<cbc:EndDate>)[^<]*(</cbc:EndDate>)",
        rf"\g<1>{new_date}\g<2>",
        text,
        count=1,
        flags=re.DOTALL,
    )


def test_placeholder_deadline_2099() -> None:
    n = _parse_text(_set_tender_end_date(_xml("643202-2026"), "2099-12-31+01:00"))
    assert n.deadline is None
    assert n.deadline_status == "placeholder"
    assert n.deadline_raw is not None and n.deadline_raw.startswith("2099-12-31")
    assert "placeholder:deadline" in n.warnings
    assert n.lots[0].tender_deadline is None


def test_deadline_more_than_five_years_after_publication_is_placeholder() -> None:
    n = _parse_text(_set_tender_end_date(_xml("643202-2026"), "2032-01-01+01:00"))
    assert n.deadline is None
    assert n.deadline_status == "placeholder"


def test_missing_deadline() -> None:
    text = re.sub(
        r"<cac:(TenderSubmissionDeadlinePeriod|ParticipationRequestReceptionPeriod)>.*?"
        r"</cac:\1>",
        "",
        _xml("643202-2026"),
        flags=re.DOTALL,
    )
    n = _parse_text(text)
    assert (n.deadline, n.deadline_kind, n.deadline_status) == (None, None, "missing")
    assert "missing:deadline" in n.warnings


# --- change linking -----------------------------------------------------------


def test_change_notice_points_to_its_original(notices: dict[str, TedNotice]) -> None:
    change, original = notices["777400-2026"], notices["664976-2026"]
    assert change.is_change
    assert change.changed_notice_id == original.notice_id
    assert change.changed_notice_version == "02" == original.notice_version
    assert change.procedure_id == original.procedure_id
    assert change.change_reason == "susp-review"


def test_find_original_and_group_by_procedure(notices: dict[str, TedNotice]) -> None:
    all_notices = list(notices.values())
    change = notices["777400-2026"]
    assert find_original(change, all_notices) is notices["664976-2026"]
    group = group_by_procedure(all_notices)[change.procedure_id]
    assert [n.publication_id for n in group] == ["664976-2026", "777400-2026"]


def test_change_notice_without_original_in_set(notices: dict[str, TedNotice]) -> None:
    n = notices["643806-2026"]
    assert n.is_change
    assert (n.changed_notice_version, n.change_reason) == ("01", "update-add")
    assert find_original(n, list(notices.values())) is None


def test_plain_contract_notice_is_not_a_change(notices: dict[str, TedNotice]) -> None:
    n = notices["642990-2026"]
    assert not n.is_change
    assert n.changed_notice_id is None
    assert find_original(n, list(notices.values())) is None


# --- contacts -----------------------------------------------------------------


def test_contact_blocks_are_ignored() -> None:
    text = _xml("643202-2026")
    contact = (
        "<cac:Contact><cbc:Name>Test Person</cbc:Name>"
        "<cbc:Telephone>+31 00 000 0000</cbc:Telephone>"
        "<cbc:ElectronicMail>test@example.org</cbc:ElectronicMail></cac:Contact>"
    )
    injected = text.replace("<efac:Company>", "<efac:Company>" + contact, 1)
    assert injected != text
    assert _parse_text(injected) == _parse_text(text)


def test_records_have_no_contact_fields() -> None:
    names = {f.name for f in dataclasses.fields(TedNotice)}
    assert not names & {"contact", "contacts", "email", "phone", "telephone"}
