from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ingest.parsing import (
    AMSTERDAM,
    Closing,
    _clean_text,
    country_alpha2,
    is_placeholder_deadline,
    parse_amount,
    parse_currency,
    parse_date,
    parse_duration_months,
    parse_sluitingsdatum,
    parse_tender_value,
    start_of_day,
)

# --- parse_amount -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("EUR 720000", Decimal("720000")),
        ("1.234.567,89", Decimal("1234567.89")),
        ("EUR 720.000", Decimal("720000")),
        ("234788.49", Decimal("234788.49")),
        ("1,234,567.89", Decimal("1234567.89")),
        ("720000,5", Decimal("720000.5")),
        (720000, Decimal("720000")),
        (234788.49, Decimal("234788.49")),
        (Decimal("50000"), Decimal("50000")),
        ("€ 1.500,00 excl. btw", Decimal("1500.00")),
        ("720 000", Decimal("720000")),
        ("1 234 567,89", Decimal("1234567.89")),
        ("EUR 720 000 excl. btw", Decimal("720000")),
        ("720000 EUR", Decimal("720000")),
        ("720\xa0000", Decimal("720000")),
    ],
)
def test_parse_amount_values(raw: object, expected: Decimal) -> None:
    assert parse_amount(raw) == expected


@pytest.mark.parametrize(
    "raw", ["720000 2026", "72 00", "1.234 567", "720  000", "720\t000", "2026 720000"]
)
def test_parse_amount_rejects_spaces_outside_thousands_groups(raw: str) -> None:
    assert parse_amount(raw) is None


@pytest.mark.parametrize("raw", [12345678, "12345678", 23456789, 99999999, "9999999999999", 1e12])
def test_parse_amount_rejects_placeholder_amounts(raw: object) -> None:
    assert parse_amount(raw) is None


@pytest.mark.parametrize("raw", [15000000, 12345679])
def test_parse_amount_keeps_large_real_amounts(raw: int) -> None:
    assert parse_amount(raw) == Decimal(raw)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "n.v.t.",
        "NVT",
        "onbekend",
        "-",
        float("nan"),
        "geen bedrag",
        0,
        -500,
        True,
        False,
    ],
)
def test_parse_amount_empty_and_invalid(raw: object) -> None:
    assert parse_amount(raw) is None


def test_parse_amount_returns_decimal() -> None:
    assert isinstance(parse_amount("234788.49"), Decimal)


# --- parse_tender_value -------------------------------------------------------


@pytest.mark.parametrize("raw", [1, 2, "99,99"])
def test_parse_tender_value_rejects_tiny_placeholders(raw: object) -> None:
    assert parse_tender_value(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"), [(100, Decimal("100")), ("EUR 720000", Decimal("720000"))]
)
def test_parse_tender_value_keeps_real_values(raw: object, expected: Decimal) -> None:
    assert parse_tender_value(raw) == expected


def test_tiny_amount_rule_applies_to_tender_values_only() -> None:
    assert parse_amount(1) == Decimal("1")


# --- parse_currency -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("EUR 720000", "EUR"),
        ("720000 eur", "EUR"),
        ("BTW 720000 EUR", "EUR"),
        ("720000", None),
        (None, None),
        ("", None),
    ],
)
def test_parse_currency(raw: object, expected: str | None) -> None:
    assert parse_currency(raw) == expected


# --- parse_duration_months ----------------------------------------------------


@pytest.mark.parametrize(
    ("term", "unit", "expected"),
    [
        ("4 Jaren", None, 48),
        ("36", "Maanden", 36),
        ("2", "jaar", 24),
        ("12", None, 12),
        ("10", "werkdagen", pytest.approx(10 * 12 / 365)),
        ("1,5", "jaar", 18),
    ],
)
def test_parse_duration_months(term: object, unit: object, expected: float) -> None:
    assert parse_duration_months(term, unit) == expected


@pytest.mark.parametrize(
    ("term", "unit"), [(None, None), ("0", "jaar"), ("700", None), ("geen", None)]
)
def test_parse_duration_months_invalid(term: object, unit: object) -> None:
    assert parse_duration_months(term, unit) is None


# --- parse_date ---------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "2026-09-11",
        "11-09-2026",
        "11/09/2026",
        "2026-09-11T14:32:00.677",
        "2026-09-11T14:32:00+02:00",
        "2026-09-11 14:32:00",
        datetime(2026, 9, 11, 14, 32),
        date(2026, 9, 11),
    ],
)
def test_parse_date_formats(raw: object) -> None:
    assert parse_date(raw) == date(2026, 9, 11)


def test_parse_date_is_day_first() -> None:
    assert parse_date("03-04-2026") == date(2026, 4, 3)


def test_parse_date_keeps_placeholder_year_for_the_hard_filter() -> None:
    assert parse_date("2099-12-31") == date(2099, 12, 31)


@pytest.mark.parametrize(
    "raw", [None, "", "n.v.t.", "not a date", "31-02-2026", "1990-01-01", float("nan")]
)
def test_parse_date_empty_and_invalid(raw: object) -> None:
    assert parse_date(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-04-19T22:00:00Z", date(2026, 4, 20)),  # midnight in Amsterdam (summer time)
        ("2026-04-19T21:59:59Z", date(2026, 4, 19)),
        ("2026-01-19T23:00:00+00:00", date(2026, 1, 20)),  # winter time: one hour ahead
        ("2026-04-20T01:00:00+08:00", date(2026, 4, 19)),
        ("2026-04-20T00:30:00+02:00", date(2026, 4, 20)),
        (datetime(2026, 4, 19, 22, 0, tzinfo=UTC), date(2026, 4, 20)),
        ("20-04-2026 12:00", date(2026, 4, 20)),
    ],
)
def test_parse_date_gives_the_amsterdam_date_of_a_value_with_an_offset(
    raw: object, expected: date
) -> None:
    assert parse_date(raw) == expected


# --- parse_sluitingsdatum -----------------------------------------------------

NOON = datetime(2026, 4, 20, 12, 0, tzinfo=AMSTERDAM)


@pytest.mark.parametrize(
    "raw",
    [
        "2026-04-20T12:00:00",
        "2026-04-20T12:00:00+02:00",
        "2026-04-20T10:00:00Z",
        "2026-04-20T10:00:00.000+00:00",
        "2026-04-20 12:00:00",
        "20-04-2026 12:00",
        "20-04-2026 12:00:00",
        "20/04/2026 12:00",
        "  2026-04-20T12:00:00  ",
    ],
)
def test_sluitingsdatum_with_a_time_is_one_moment_in_amsterdam(raw: str) -> None:
    closing = parse_sluitingsdatum(raw)
    assert closing == Closing(NOON, True)
    assert closing.moment.utcoffset() == timedelta(hours=2) and closing.day == date(2026, 4, 20)


@pytest.mark.parametrize("raw", ["2026-04-20", "20-04-2026", "20/04/2026"])
def test_sluitingsdatum_without_a_time_is_the_start_of_that_day(raw: str) -> None:
    assert parse_sluitingsdatum(raw) == Closing(start_of_day(date(2026, 4, 20)), False)


def test_sluitingsdatum_in_utc_can_fall_on_the_next_amsterdam_day() -> None:
    closing = parse_sluitingsdatum("2026-04-19T22:00:00Z")
    assert closing is not None and closing.day == date(2026, 4, 20)
    assert closing.moment == datetime(2026, 4, 19, 22, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "raw", [None, "", "onbekend", "not a date", "31-02-2026", "1990-01-01", "04-20-2026", 20260420]
)
def test_sluitingsdatum_empty_and_invalid(raw: object) -> None:
    assert parse_sluitingsdatum(raw) is None


def test_sluitingsdatum_keeps_the_placeholder_date_for_the_hard_filter() -> None:
    closing = parse_sluitingsdatum("2099-12-31")
    assert closing is not None and is_placeholder_deadline(closing.day, date(2026, 3, 2))


def test_closing_is_closed() -> None:
    timed = parse_sluitingsdatum("2026-04-20T12:00:00")
    assert timed is not None
    assert not timed.is_closed(NOON - timedelta(minutes=1))
    assert timed.is_closed(NOON) and timed.is_closed(NOON + timedelta(minutes=1))
    # The same moments given in another zone.
    tokyo = timezone(timedelta(hours=9))
    assert timed.is_closed(datetime(2026, 4, 20, 19, 0, tzinfo=tokyo))
    assert not timed.is_closed(datetime(2026, 4, 20, 18, 59, tzinfo=tokyo))

    day_only = parse_sluitingsdatum("2026-04-20")
    assert day_only is not None
    assert not day_only.is_closed(datetime(2026, 4, 20, 23, 59, tzinfo=AMSTERDAM))
    assert day_only.is_closed(datetime(2026, 4, 21, 0, 0, tzinfo=AMSTERDAM))
    with pytest.raises(ValueError, match="timezone-aware"):
        timed.is_closed(datetime(2026, 4, 20, 13, 0))


# --- _clean_text --------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  abc  ", "abc"),
        ("N.v.t.", None),
        ("-", None),
        (None, None),
        (float("nan"), None),
        (123, "123"),
    ],
)
def test_clean_text(raw: object, expected: str | None) -> None:
    assert _clean_text(raw) == expected


# --- is_placeholder_deadline --------------------------------------------------


@pytest.mark.parametrize(
    ("closing", "published", "expected"),
    [
        (date(2099, 12, 31), date(2026, 9, 18), True),
        (date(2099, 12, 31), None, True),
        (date(2031, 9, 18), date(2026, 9, 18), False),  # exactly five years
        (date(2031, 9, 19), date(2026, 9, 18), True),  # five years and one day
        (date(2026, 11, 17), date(2026, 9, 18), False),
        (date(2033, 3, 1), date(2028, 2, 29), True),  # leap day publication
        (date(2040, 1, 1), None, False),  # no publication date: only 2099-12-31 counts
    ],
)
def test_is_placeholder_deadline(closing: date, published: date | None, expected: bool) -> None:
    assert is_placeholder_deadline(closing, published) is expected


# --- country_alpha2 -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("NLD", "NL"), ("nld", "NL"), (" NLD ", "NL"), ("NL", "NL"), ("nl", "NL"),
     ("BEL", "BE"), ("DEU", "DE"), ("GRC", "GR"), ("GBR", "GB"), ("BES", "BQ")],
)  # fmt: skip
def test_country_alpha2(raw: str, expected: str) -> None:
    assert country_alpha2(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "onbekend", "XKX", "Nederland", "N1", "NLDX", 528])
def test_country_alpha2_unknown_gives_none(raw: object) -> None:
    assert country_alpha2(raw) is None


def test_only_the_dutch_code_maps_to_nl() -> None:
    from ingest.parsing import _ALPHA3_TO_ALPHA2

    assert [a3 for a3, a2 in _ALPHA3_TO_ALPHA2.items() if a2 == "NL"] == ["NLD"]
    assert len(set(_ALPHA3_TO_ALPHA2.values())) == len(_ALPHA3_TO_ALPHA2)  # no code used twice
    assert all(len(a3) == 3 and len(a2) == 2 for a3, a2 in _ALPHA3_TO_ALPHA2.items())
