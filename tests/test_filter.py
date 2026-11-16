"""Tests for pipeline/filter.py: the saved fixtures plus synthetic edge cases.

Synthetic cases change a parsed fixture with dataclasses.replace; no new fixture files.
"""

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ingest.parsing import AMSTERDAM
from ingest.ted import TedLot, TedNotice, parse_notice_file
from pipeline.filter import apply_hard_filter

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "ted"
# A Friday, 09:00 in Amsterdam; the earliest publication date in the fixtures.
NOW = datetime(2026, 9, 18, 9, 0, tzinfo=AMSTERDAM)
VALID_DEADLINE = "2026-12-31T12:00:00"  # TenderNed form; well after every publication


@pytest.fixture(scope="module")
def notices() -> dict[str, TedNotice]:
    return {p.stem: parse_notice_file(p) for p in sorted(FIXTURES.glob("*.xml"))}


@pytest.fixture
def base(notices: dict[str, TedNotice]) -> TedNotice:
    """A clean, one-lot fixture (648029: services, CPV 72, EUR 1.845M, published 2026-09-21)."""
    return notices["648029-2026"]


def run(
    notice: TedNotice,
    deadline: str | None = VALID_DEADLINE,
    matched: bool = True,
    now: datetime = NOW,
):
    return apply_hard_filter(notice, sluitingsdatum=deadline, tenderned_matched=matched, now=now)


def lot(lot_id: str, value: str | None, currency: str | None = "EUR", **kw: object) -> TedLot:
    fields = {
        "lot_id": lot_id,
        "title": None,
        "cpv_main": "72000000",
        "cpv_additional": (),
        "estimated_value": Decimal(value) if value is not None else None,
        "currency": currency if value is not None else None,
        "duration_months": None,
        "tender_deadline": None,
        "participation_deadline": None,
        "place_country": "NL",
        "place_country_raw": "NLD",
    }
    return TedLot(**{**fields, **kw})


def with_lots(notice: TedNotice, *lots: TedLot, procedure: str | None = None) -> TedNotice:
    return replace(
        notice,
        lots=lots,
        estimated_value=Decimal(procedure) if procedure else None,
        currency="EUR" if procedure else None,
    )


# --- real fixtures ------------------------------------------------------------

EXPECTED_VALUE = {
    "642990-2026": ("unknown", "missing"),
    "643202-2026": ("unknown", "missing"),
    "643670-2026": ("unknown", "missing"),
    "643994-2026": ("unknown", "missing"),
    "643806-2026": ("pass", "above_2m"),
    "646325-2026": ("pass", "above_2m"),
    "664976-2026": ("pass", "above_2m"),
    "777400-2026": ("pass", "above_2m"),
    "647200-2026": ("pass", "in_range"),
    "648029-2026": ("pass", "in_range"),
    "688608-2026": ("pass", "in_range"),
}


def test_every_fixture_has_an_expectation(notices: dict[str, TedNotice]) -> None:
    assert sorted(notices) == sorted(EXPECTED_VALUE)


@pytest.mark.parametrize("pub", sorted(EXPECTED_VALUE))
def test_fixture_outcomes(notices: dict[str, TedNotice], pub: str) -> None:
    result = run(notices[pub])
    assert result.passed  # no fixture is dropped
    for rule in ("country", "cpv", "deadline", "notice_type"):
        assert result.rules[rule].outcome == "pass", rule
    value = result.rules["value"]
    assert (value.outcome, value.reason) == EXPECTED_VALUE[pub]
    assert ("consortium_only" in result.flags) == (value.reason == "above_2m")
    assert ("unknown:value" in result.flags) == (value.outcome == "unknown")
    assert ("change_notice" in result.flags) == (pub in {"643806-2026", "777400-2026"})
    assert ("nature_supplies" in result.flags) == (pub in {"643670-2026", "643994-2026"})


def test_value_raw_lists_the_values_used(notices: dict[str, TedNotice]) -> None:
    assert run(notices["688608-2026"]).rules["value"].raw == (
        "LOT-0001=250000; LOT-0002=300000; LOT-0003=450000"
    )
    assert run(notices["647200-2026"]).rules["value"].raw == "procedure=250000.0"


def test_deadline_comes_from_the_input_never_the_notice(notices: dict[str, TedNotice]) -> None:
    notice = notices["664976-2026"]  # its own eForms deadline is 2026-10-29
    result = run(notice, deadline="2026-11-30T12:00:00")
    assert result.rules["deadline"].raw == "2026-11-30T12:00:00"
    assert run(replace(notice, deadline=None), deadline="2026-11-30T12:00:00") == result
    assert run(notice, deadline=None).rules["deadline"].reason == "missing"


def test_eurojust_country_without_place_of_performance(notices: dict[str, TedNotice]) -> None:
    result = run(notices["646325-2026"])
    assert result.rules["country"].outcome == "pass"
    assert "place_outside_nl" not in result.flags


# --- deadline -----------------------------------------------------------------


def test_placeholder_deadline_is_unknown_not_a_drop(base: TedNotice) -> None:
    result = run(base, deadline="2099-12-31")
    rule = result.rules["deadline"]
    assert (rule.outcome, rule.reason, rule.raw) == ("unknown", "placeholder_date", "2099-12-31")
    assert "unknown:deadline" in result.flags
    assert result.passed


def test_deadline_more_than_five_years_after_publication(base: TedNotice) -> None:
    assert run(base, deadline="2032-01-01").rules["deadline"].reason == "placeholder_date"


@pytest.mark.parametrize(
    ("deadline", "matched", "reason"),
    [(None, True, "missing"), ("", True, "missing"), ("not a date", True, "missing"),
     (VALID_DEADLINE, False, "no_tenderned_match")],
)  # fmt: skip
def test_unknown_deadlines(
    base: TedNotice, deadline: str | None, matched: bool, reason: str
) -> None:
    result = run(base, deadline=deadline, matched=matched)
    assert (result.rules["deadline"].outcome, result.rules["deadline"].reason) == (
        "unknown",
        reason,
    )
    assert result.passed


def test_closed_deadline_fails(base: TedNotice) -> None:
    result = run(base, deadline="2026-09-17")
    assert (result.rules["deadline"].outcome, result.rules["deadline"].reason) == ("fail", "closed")
    assert not result.passed


def at(hour: int, minute: int = 0) -> datetime:
    return NOW.replace(hour=hour, minute=minute)


CLOSED_TODAY = {
    "an_hour_ago": ("2026-09-18T08:00:00", at(9), "closed"),
    "this_very_minute": ("2026-09-18T09:00:00", at(9), "closed"),
    "an_hour_ago_with_offset": ("2026-09-18T08:00:00+02:00", at(9), "closed"),
    "an_hour_ago_in_utc": ("2026-09-18T06:00:00Z", at(9), "closed"),  # 08:00 in Amsterdam
    "dutch_form": ("18-09-2026 08:00", at(9), "closed"),
    "yesterday_late_utc_is_today": ("2026-09-17T22:30:00Z", at(0, 15), "open"),  # 00:30 today
    "later_today": ("2026-09-18T17:00:00", at(9), "open"),
    "later_today_in_utc": ("2026-09-18T08:00:00Z", at(9), "open"),  # 10:00 in Amsterdam
    "date_only_today": ("2026-09-18", at(23, 59), "open"),  # no time: open the whole day
    "date_only_yesterday": ("2026-09-17", at(0, 1), "closed"),
}


@pytest.mark.parametrize(
    ("deadline", "now", "state"), CLOSED_TODAY.values(), ids=CLOSED_TODAY.keys()
)
def test_a_tender_that_closed_earlier_today_is_closed(
    base: TedNotice, deadline: str, now: datetime, state: str
) -> None:
    notice = replace(base, publication_date=date(2026, 8, 3))  # long before: never too_short
    rule = run(notice, deadline=deadline, now=now).rules["deadline"]
    if state == "closed":
        assert (rule.outcome, rule.reason, rule.raw) == ("fail", "closed", deadline)
    else:
        assert (rule.outcome, rule.reason) == ("pass", "ok")


def test_now_is_compared_as_a_moment_whatever_its_zone(base: TedNotice) -> None:
    notice = replace(base, publication_date=date(2026, 8, 3))
    in_utc = datetime(2026, 9, 18, 7, 0, tzinfo=UTC)  # the same moment as NOW
    for now in (NOW, in_utc):
        assert run(notice, deadline="2026-09-18T08:59:00", now=now).rules["deadline"].reason == (
            "closed"
        )
        assert run(notice, deadline="2026-09-18T09:01:00", now=now).rules["deadline"].reason == "ok"


def test_a_utc_closing_time_counts_on_its_amsterdam_date(base: TedNotice) -> None:
    # 2 Oct 22:00 UTC is 3 Oct 00:00 in Amsterdam: 11 working days after Fri 18 Sep, not 10.
    notice = replace(base, publication_date=date(2026, 9, 18))
    assert run(notice, deadline="2026-10-01T21:59:00Z").rules["deadline"].reason == "too_short"
    assert run(notice, deadline="2026-10-01T22:00:00Z").rules["deadline"].reason == "ok"


def test_a_naive_now_is_refused(base: TedNotice) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        run(base, now=datetime(2026, 9, 18, 9, 0))


@pytest.mark.parametrize(
    ("deadline", "outcome"),
    [("2026-10-02", "pass"), ("2026-10-01", "fail")],  # 10 and 9 working days after Fri 18 Sep
)
def test_ten_working_days(base: TedNotice, deadline: str, outcome: str) -> None:
    notice = replace(base, publication_date=date(2026, 9, 18))
    rule = run(notice, deadline=deadline).rules["deadline"]
    assert rule.outcome == outcome
    if outcome == "fail":
        assert rule.reason == "too_short"


# --- value (per lot) ----------------------------------------------------------


def test_no_value_anywhere_is_unknown(base: TedNotice) -> None:
    result = run(with_lots(base, lot("LOT-1", None)))
    assert (result.rules["value"].outcome, result.rules["value"].reason) == ("unknown", "missing")
    assert "unknown:value" in result.flags
    assert result.passed


def test_lot_values_come_before_the_procedure_value(base: TedNotice) -> None:
    notice = with_lots(
        base, lot("LOT-1", "1000000"), lot("LOT-2", "1000000"), lot("LOT-3", "1000000"),
        procedure="3000000",
    )  # fmt: skip
    result = run(notice)
    assert (result.rules["value"].outcome, result.rules["value"].reason) == ("pass", "in_range")
    assert "consortium_only" not in result.flags


def test_value_above_2m_is_kept_with_consortium_flag(base: TedNotice) -> None:
    result = run(with_lots(base, lot("LOT-1", "2000001")))
    assert (result.rules["value"].outcome, result.rules["value"].reason) == ("pass", "above_2m")
    assert "consortium_only" in result.flags
    assert result.passed


@pytest.mark.parametrize(
    ("value", "outcome"), [("49999", "fail"), ("50000", "pass"), ("2000000", "pass")]
)
def test_value_bounds(base: TedNotice, value: str, outcome: str) -> None:
    rule = run(with_lots(base, lot("LOT-1", value))).rules["value"]
    assert rule.outcome == outcome
    if outcome == "fail":
        assert rule.reason == "below_50k"


def test_mixed_lots(base: TedNotice) -> None:
    mixed = run(with_lots(base, lot("LOT-1", "500000"), lot("LOT-2", "3000000")))
    assert mixed.rules["value"].outcome == "pass"
    assert "LOT-2" in mixed.rules["value"].reason and "lots_out_of_range" in mixed.flags

    small_and_missing = run(with_lots(base, lot("LOT-1", "20000"), lot("LOT-2", None)))
    assert small_and_missing.rules["value"].outcome == "unknown"  # the lot without a value may fit

    all_small = run(with_lots(base, lot("LOT-1", "20000"), lot("LOT-2", "30000")))
    assert (all_small.rules["value"].outcome, all_small.rules["value"].reason) == (
        "fail",
        "below_50k",
    )


def test_procedure_value_fallback_and_currency(base: TedNotice) -> None:
    fallback = run(with_lots(base, lot("LOT-1", None), procedure="250000"))
    assert (fallback.rules["value"].outcome, fallback.rules["value"].raw) == (
        "pass",
        "procedure=250000",
    )
    foreign = run(with_lots(base, lot("LOT-1", "500000", currency="USD")))
    assert foreign.rules["value"].outcome == "unknown"


# --- notice type, country, CPV ------------------------------------------------


@pytest.mark.parametrize(
    ("notice_type", "outcome"),
    [("can-standard", "fail"), ("pin-only", "fail"), ("pin-cfc-standard", "fail"),
     ("veat", "fail"), ("cn-social", "pass"), ("something-new", "unknown")],
)  # fmt: skip
def test_notice_type(base: TedNotice, notice_type: str, outcome: str) -> None:
    result = run(replace(base, notice_type=notice_type))
    assert result.rules["notice_type"].outcome == outcome
    assert result.passed == (outcome != "fail")


def test_country(base: TedNotice) -> None:
    dutch = run(base).rules["country"]
    assert (base.country, base.country_raw) == ("NL", "NLD")
    assert (dutch.outcome, dutch.reason, dutch.raw) == ("pass", "nl", "NLD")  # raw as received

    belgian = run(replace(base, country="BE", country_raw="BEL"))
    assert (belgian.rules["country"].outcome, belgian.passed) == ("fail", False)
    assert belgian.rules["country"].raw == "BEL"
    missing = run(replace(base, country=None, country_raw=None))
    assert (missing.rules["country"].outcome, missing.rules["country"].reason) == (
        "unknown",
        "missing",
    )
    abroad = run(
        with_lots(base, lot("LOT-1", "500000", place_country="DE", place_country_raw="DEU"))
    )
    assert abroad.rules["country"].outcome == "pass"
    assert "place_outside_nl" in abroad.flags and abroad.passed


@pytest.mark.parametrize("raw", ["XKX", "NLD", "Nederland"])
def test_a_country_code_the_parser_could_not_map_is_unknown_never_a_fail(
    base: TedNotice, raw: str
) -> None:
    # Only the stored alpha-2 code decides. Without one the country is not known.
    result = run(replace(base, country=None, country_raw=raw))
    rule = result.rules["country"]
    assert (rule.outcome, rule.reason, rule.raw) == ("unknown", "unrecognised_code", raw)
    assert result.passed and "unknown:country" in result.flags


def test_an_unmapped_buyer_country_in_a_notice_is_unknown(notices: dict[str, TedNotice]) -> None:
    from ingest.ted import parse_notice

    xml = (FIXTURES / "648029-2026.xml").read_text(encoding="utf-8")
    changed = xml.replace('listName="country">NLD<', 'listName="country">XKX<')
    assert changed != xml
    result = run(parse_notice(changed.encode("utf-8")))
    assert result.rules["country"].outcome == "unknown" and result.passed
    belgian = run(parse_notice(xml.replace('">NLD<', '">BEL<').encode("utf-8")))
    assert belgian.rules["country"].reason == "not_nl" and not belgian.passed


def test_place_of_performance_with_an_unmapped_code_is_flagged(base: TedNotice) -> None:
    elsewhere = run(
        with_lots(base, lot("LOT-1", "500000", place_country=None, place_country_raw="XKX"))
    )
    nowhere = run(
        with_lots(base, lot("LOT-1", "500000", place_country=None, place_country_raw=None))
    )
    assert "place_outside_nl" in elsewhere.flags and "place_outside_nl" not in nowhere.flags


def test_cpv(base: TedNotice) -> None:
    def cpv_result(codes: tuple[str, ...], lot_cpv: str | None = None):
        return run(
            replace(with_lots(base, lot("LOT-1", "500000", cpv_main=lot_cpv)), cpv_codes=codes)
        )

    works = cpv_result(("45000000",))
    assert (works.rules["cpv"].outcome, works.passed) == ("fail", False)
    research = cpv_result(("73000000",))
    assert research.rules["cpv"].outcome == "pass" and "cpv_73_check_scope" in research.flags
    assert cpv_result(("45000000",), lot_cpv="72000000").rules["cpv"].outcome == "pass"
    assert cpv_result(()).rules["cpv"].outcome == "unknown"


# --- result -------------------------------------------------------------------


def test_unknowns_do_not_drop_and_one_fail_does(base: TedNotice) -> None:
    many_unknown = run(
        replace(with_lots(base, lot("LOT-1", None)), country=None, country_raw=None, cpv_codes=()),
        matched=False,
    )
    assert many_unknown.passed
    assert {"unknown:country", "unknown:deadline", "unknown:value"} <= set(many_unknown.flags)
    assert not run(replace(base, notice_type="can-standard")).passed


def test_to_json_matches_the_architecture_example(base: TedNotice) -> None:
    data = run(base, deadline="2099-12-31").to_json()
    assert data["deadline"] == {
        "outcome": "unknown",
        "reason": "placeholder_date",
        "raw": "2099-12-31",
    }
    assert set(data) == {"country", "cpv", "deadline", "value", "notice_type"}
