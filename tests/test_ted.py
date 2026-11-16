"""Tests for ingest/ted.py, using only the saved fixtures in tests/fixtures/ted/.

Cases the real files lack (placeholder deadline, missing field, a contact block) are made
by editing fixture bytes in memory; no extra fixture files are written.
"""

import dataclasses
import json
import re
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from ingest.ted import (
    TedBuyer,
    TedContact,
    TedLot,
    TedNotice,
    TedParseError,
    find_original,
    group_by_procedure,
    parse_contacts,
    parse_notice,
    parse_notice_file,
)
from llm.redact import Redactor

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


def _manifest() -> dict:
    return json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))


def test_all_fixtures_parse_and_ids_match_file_names(notices: dict[str, TedNotice]) -> None:
    assert len(XML_FILES) == len(_manifest()["notices"]) >= 10
    assert sorted(notices) == sorted(p.stem for p in XML_FILES)


def test_manifest_matches_the_parsed_notices(notices: dict[str, TedNotice]) -> None:
    """The manifest role of every notice must agree with what the parser reads in the XML."""
    for entry in _manifest()["notices"]:
        n = notices[entry["publication_number"]]
        who = entry["publication_number"]
        assert (entry["role"] == "change_notice") == n.is_change, f"{who}: role"
        expected_change = (
            f"{n.changed_notice_id}-{n.changed_notice_version}" if n.is_change else None
        )
        assert entry["changes_notice"] == expected_change, f"{who}: changes_notice"
        assert entry["procedure_id"] == n.procedure_id, f"{who}: procedure_id"
        assert entry["notice_id"] == n.notice_id, f"{who}: notice_id"
        assert entry["main_cpv"] == n.cpv_main, f"{who}: main_cpv"
        assert entry["publication_date"][:10] == n.publication_date.isoformat(), f"{who}: date"


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
    assert (n.country, n.country_raw) == ("NL", "NLD")  # alpha-2 stored, raw value kept
    assert all((b.country, b.country_raw) == ("NL", "NLD") for b in n.buyers)
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


# --- country codes ------------------------------------------------------------


def _set_buyer_country(text: str, code: str) -> str:
    changed, count = re.subn(
        r'(<cbc:IdentificationCode listName="country">)NLD(</cbc:IdentificationCode>)',
        rf"\g<1>{code}\g<2>",
        text,
    )
    assert count >= 1
    return changed


@pytest.mark.parametrize(
    ("raw", "stored"), [("BEL", "BE"), ("DEU", "DE"), ("nld", "NL"), ("NL", "NL"), ("XKX", None)]
)
def test_country_is_stored_as_alpha_2_with_the_raw_value_kept(raw: str, stored: str | None) -> None:
    n = _parse_text(_set_buyer_country(_xml("648029-2026"), raw))
    assert (n.country, n.country_raw) == (stored, raw)
    assert (n.buyers[0].country, n.buyers[0].country_raw) == (stored, raw)


def test_every_fixture_stores_a_two_letter_country(notices: dict[str, TedNotice]) -> None:
    for n in notices.values():
        assert n.country is not None and len(n.country) == 2 and n.country_raw is not None
        for lot in n.lots:
            assert lot.place_country is None or len(lot.place_country) == 2
            assert (lot.place_country is None) == (lot.place_country_raw is None)


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
    for record in (TedNotice, TedBuyer, TedLot):
        names = {f.name for f in dataclasses.fields(record)}
        assert not names & {"contact", "contacts", "contact_name", "email", "phone", "telephone"}
        assert not any("TedContact" in str(f.type) for f in dataclasses.fields(record))


# The saved fixtures have no contact blocks (they are removed at download), so the blocks
# below are added in memory. Every name, address and number is invented.
BUYER_CONTACT = (
    "<cac:Contact><cbc:Name>Sanne van der Berg</cbc:Name>"
    "<cbc:Telephone>+31 00 000 0000</cbc:Telephone>"
    "<cbc:ElectronicMail>s.vanderberg@voorbeeld.example</cbc:ElectronicMail></cac:Contact>"
)
TOUCHPOINT_CONTACT = (
    "<cac:Contact><cbc:Name>Afdeling Inkoop</cbc:Name>"
    "<cbc:ElectronicMail>inkoop@voorbeeld.example</cbc:ElectronicMail></cac:Contact>"
)
EMPTY_CONTACT = "<cac:Contact><cbc:Name> </cbc:Name></cac:Contact>"


def _with_contacts() -> tuple[str, str]:
    """(fixture text, the same text with three contact blocks added)."""
    text = _xml("643202-2026")
    injected = text.replace("</efac:Company>", BUYER_CONTACT + "</efac:Company>", 1)
    injected = injected.replace(
        "</efac:TouchPoint>", TOUCHPOINT_CONTACT + EMPTY_CONTACT + "</efac:TouchPoint>", 1
    )
    assert injected.count("<cac:Contact>") == 3
    return text, injected


def test_fixtures_without_contact_blocks_give_no_contacts() -> None:
    for path in XML_FILES:
        assert parse_contacts(path.read_bytes()) == ()


def test_contacts_are_parsed_into_their_own_records() -> None:
    text, injected = _with_contacts()
    notice = _parse_text(injected)
    contacts = parse_contacts(injected.encode("utf-8"))

    assert len(contacts) == 2  # the block without a name, email or phone gives no row
    person, department = contacts
    assert isinstance(person, TedContact)
    assert person.contact_name == "Sanne van der Berg"
    assert person.email == "s.vanderberg@voorbeeld.example"
    assert person.phone == "+31 00 000 0000"
    assert person.org_name == notice.buyers[0].name and person.role == "buyer"
    assert department.contact_name == "Afdeling Inkoop" and department.phone is None
    assert department.org_name == "Rechtbank Zeeland en West Brabant"  # the touch point's own

    # The notice record is the same with or without the blocks, and holds none of the data.
    assert notice == _parse_text(text)
    as_text = repr(notice)
    for planted in ("Sanne", "van der Berg", "voorbeeld.example", "+31 00 000 0000"):
        assert planted not in as_text


def test_a_contact_record_does_not_show_personal_data_when_printed() -> None:
    _, injected = _with_contacts()
    shown = repr(parse_contacts(injected.encode("utf-8")))
    assert "role='buyer'" in shown
    for planted in ("Sanne", "Berg", "voorbeeld.example", "+31 00", "Afdeling Inkoop"):
        assert planted not in shown


def test_parse_contacts_rejects_what_parse_notice_rejects() -> None:
    with pytest.raises(TedParseError):
        parse_contacts(b"<not-a-notice/>")
    with pytest.raises(TedParseError):
        parse_contacts(b"not xml")


def test_the_redactor_is_built_from_the_contact_records() -> None:
    _, injected = _with_contacts()
    notice = _parse_text(injected)
    contacts = parse_contacts(injected.encode("utf-8"))
    redactor = Redactor.from_contacts(contacts, organisations=[b.name for b in notice.buyers])

    buyer = notice.buyers[0].name
    text = (
        f"Aanbestedende dienst: {buyer}. Contactpersoon: Sanne van der Berg "
        f"(mevrouw Van der Berg, S. van der Berg). De afdeling inkoop van "
        f"Rechtbank Zeeland en West Brabant beantwoordt vragen."
    )
    assert redactor.redact(text) == (
        f"Aanbestedende dienst: {buyer}. Contactpersoon: [NAME] "
        f"([NAME], [NAME]). De afdeling inkoop van "
        f"Rechtbank Zeeland en West Brabant beantwoordt vragen."
    )
    assert "Berg" not in repr(redactor)


def test_a_contact_name_that_is_the_organisation_itself_is_not_redacted() -> None:
    row = TedContact(
        org_name="Voorbeeld Staete", role="buyer", contact_name="Voorbeeld Staete",
        email=None, phone=None,
    )  # fmt: skip
    text = "Opdrachtgever is Voorbeeld Staete."
    assert Redactor.from_contacts([row]).redact(text) == text
    assert Redactor(["Voorbeeld Staete"]).redact(text) == "Opdrachtgever is [NAME]."


# --- multi-lot notice 688608-2026 (ZonMW, 3 lots) ------------------------------

MULTI_LOT = "688608-2026"
# Lot values and the deadline date were also reported by the TED search API before the
# fetch, so these expectations do not come from the parser alone.
LOT_VALUES = (Decimal("250000"), Decimal("300000"), Decimal("450000"))
LOT_DEADLINE = datetime(2026, 11, 23, 16, 0, tzinfo=UTC)


def test_multi_lot_notice_lots(notices: dict[str, TedNotice]) -> None:
    n = notices[MULTI_LOT]
    assert len(n.lots) == 3
    assert [lot.lot_id for lot in n.lots] == ["LOT-0001", "LOT-0002", "LOT-0003"]
    assert [lot.title for lot in n.lots] == [
        "Perceel 1 - DTP",
        "Perceel 2 - Grafische vormgeving van illustraties en animatie",
        "Perceel 3 - Grafische ontwerp voor technische producten",
    ]
    assert tuple(lot.estimated_value for lot in n.lots) == LOT_VALUES
    assert {lot.currency for lot in n.lots} == {"EUR"}
    assert {lot.cpv_main for lot in n.lots} == {"79800000"}
    assert {(lot.place_country, lot.place_country_raw) for lot in n.lots} == {("NL", "NLD")}
    assert n.estimated_value == sum(LOT_VALUES)  # procedure-level BT-27 is the lot total here
    assert not n.is_change and n.warnings == ()


def test_multi_lot_notice_per_lot_deadlines(notices: dict[str, TedNotice]) -> None:
    n = notices[MULTI_LOT]
    for lot in n.lots:
        assert lot.tender_deadline == LOT_DEADLINE
        assert lot.participation_deadline is None
    assert (n.deadline, n.deadline_kind, n.deadline_status) == (LOT_DEADLINE, "tender", "ok")


def _set_lot_end_dates(text: str, end_dates: list[str]) -> str:
    """Give each lot's BT-131 EndDate a new value, in lot order (in memory only)."""
    pattern = re.compile(
        r"(<cac:TenderSubmissionDeadlinePeriod>\s*<cbc:EndDate>)[^<]*(</cbc:EndDate>)"
    )
    assert len(pattern.findall(text)) == len(end_dates)
    values = iter(end_dates)
    return pattern.sub(lambda m: f"{m.group(1)}{next(values)}{m.group(2)}", text)


def test_notice_deadline_is_the_earliest_real_lot_deadline(notices: dict[str, TedNotice]) -> None:
    real = notices[MULTI_LOT]
    assert real.deadline == min(lot.tender_deadline for lot in real.lots)

    # Lots with different deadlines: the earliest one (lot 2) wins.
    n = _parse_text(
        _set_lot_end_dates(
            _xml(MULTI_LOT), ["2026-12-01+00:00", "2026-11-05+00:00", "2026-11-20+00:00"]
        )
    )
    assert n.deadline == datetime(2026, 11, 5, 16, 0, tzinfo=UTC) == n.lots[1].tender_deadline
    assert n.deadline == min(lot.tender_deadline for lot in n.lots)
    assert n.deadline_status == "ok"


def test_placeholder_lot_deadline_is_skipped_for_the_earliest() -> None:
    # The earliest-looking lot carries the 2099-12-31 placeholder: it is ignored,
    # and the earliest REAL deadline (lot 3) wins.
    n = _parse_text(
        _set_lot_end_dates(
            _xml(MULTI_LOT), ["2099-12-31+00:00", "2026-11-25+00:00", "2026-11-20+00:00"]
        )
    )
    assert n.lots[0].tender_deadline is None
    assert n.deadline == datetime(2026, 11, 20, 16, 0, tzinfo=UTC) == n.lots[2].tender_deadline
    assert n.deadline_status == "ok"
    assert "placeholder:deadline" not in n.warnings


def _set_lot_end_times(text: str, end_times: list[str]) -> str:
    """Give each lot's BT-131 EndTime a new value, in lot order (in memory only)."""
    pattern = re.compile(
        r"(<cac:TenderSubmissionDeadlinePeriod>\s*<cbc:EndDate>[^<]*</cbc:EndDate>\s*"
        r"<cbc:EndTime>)[^<]*(</cbc:EndTime>)"
    )
    assert len(pattern.findall(text)) == len(end_times)
    values = iter(end_times)
    return pattern.sub(lambda m: f"{m.group(1)}{next(values)}{m.group(2)}", text)


def test_an_end_time_without_an_offset_takes_the_offset_of_the_end_date() -> None:
    # Lot 1 has no offset on EndTime, lot 2 has one, lot 3 uses Z. Before the fix this mix
    # of naive and aware values raised TypeError in the earliest-deadline comparison.
    text = _set_lot_end_dates(
        _xml(MULTI_LOT), ["2026-11-23+01:00", "2026-11-23+01:00", "2026-11-23+01:00"]
    )
    n = _parse_text(_set_lot_end_times(text, ["12:00:00", "13:00:00+01:00", "10:30:00Z"]))
    assert [lot.tender_deadline for lot in n.lots] == [
        datetime(2026, 11, 23, 12, 0, tzinfo=CET),
        datetime(2026, 11, 23, 13, 0, tzinfo=CET),
        datetime(2026, 11, 23, 10, 30, tzinfo=UTC),
    ]
    assert all(lot.tender_deadline.tzinfo is not None for lot in n.lots)
    assert n.deadline == datetime(2026, 11, 23, 10, 30, tzinfo=UTC)  # 11:30 CET: the earliest
    assert n.deadline_status == "ok"


def test_a_deadline_with_no_offset_at_all_is_read_as_amsterdam_time() -> None:
    text = _set_lot_end_dates(_xml(MULTI_LOT), ["2026-11-23", "2026-07-06", "2026-11-23+01:00"])
    n = _parse_text(_set_lot_end_times(text, ["12:00:00", "12:00:00", "12:00:00"]))
    assert n.lots[0].tender_deadline == datetime(2026, 11, 23, 12, 0, tzinfo=CET)  # winter
    assert n.lots[1].tender_deadline == datetime(2026, 7, 6, 12, 0, tzinfo=CEST)  # summer
    assert n.deadline == n.lots[1].tender_deadline


def test_all_lot_deadlines_placeholder() -> None:
    n = _parse_text(_set_lot_end_dates(_xml(MULTI_LOT), ["2099-12-31+00:00"] * 3))
    assert all(lot.tender_deadline is None for lot in n.lots)
    assert (n.deadline, n.deadline_status) == (None, "placeholder")
    assert "placeholder:deadline" in n.warnings
