from datetime import date, datetime
from decimal import Decimal

import pytest

from ingest.parsing import (
    _clean_text,
    parse_amount,
    parse_currency,
    parse_date,
    parse_duration_months,
    parse_tender_value,
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
