"""Pure parsers for messy tender values: amounts, currencies, durations and dates.

Ported from an earlier pandas-based project and rewritten on the standard library.
Every function takes one raw value and returns a clean value or None. None means
"unknown": empty cells, placeholder text and implausible values all map to None.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

# Text that means "no value" in TenderNed exports and API fields (compared lower-case).
PLACEHOLDER_TEXTS = frozenset({"", "none", "nan", "nat", "null", "-", "n.v.t.", "nvt", "onbekend"})

# No Dutch public contract reaches EUR 10 bn; larger figures are keyboard filler.
MAX_PLAUSIBLE_AMOUNT = Decimal("1e10")
# Tender values below this are placeholders (e.g. lowest value 1, highest value 2).
# Applied to tender values only, never to bid values, where small amounts can be real.
MIN_TENDER_VALUE = Decimal("100")

CURRENCIES = frozenset({"EUR", "USD", "GBP", "CHF", "DKK", "SEK", "NOK", "PLN"})

MIN_YEAR = 1995
MAX_YEAR = 2100
MAX_DURATION_MONTHS = 600.0

_PLACEHOLDER_MIN_DIGITS = 8
_ASCENDING_DIGITS = "123456789012345678901234567890"

_AMOUNT_RE = re.compile(r"[-+]?\d[\d.,\s]*")
# A number containing spaces is valid only when every space separates a group of
# exactly three digits: `720 000`, `1 234 567,89`. Decimals may follow the last group.
_SPACED_AMOUNT_RE = re.compile(r"[-+]?\d{1,3}(?: \d{3})+(?:[.,]\d+)?")
_WORD3_RE = re.compile(r"\b[A-Za-z]{3}\b")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_WORD_RE = re.compile(r"[a-z]+")

# Day-first only: no format puts the month first, so 03-04-2026 is 3 April.
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d-%m-%Y",
    "%d/%m/%Y",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
)

_DAY = 12.0 / 365
_WEEK = 12.0 / 52
_UNIT_MONTHS = {
    "jaar": 12.0,
    "jaren": 12.0,
    "year": 12.0,
    "years": 12.0,
    "maand": 1.0,
    "maanden": 1.0,
    "month": 1.0,
    "months": 1.0,
    "week": _WEEK,
    "weken": _WEEK,
    "weeks": _WEEK,
    "dag": _DAY,
    "dagen": _DAY,
    "day": _DAY,
    "days": _DAY,
    # Working days are counted as calendar days, as in the original code.
    "werkdag": _DAY,
    "werkdagen": _DAY,
}


def _clean_text(value: object) -> str | None:
    """Return stripped text, or None for missing values and placeholder text."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    return None if text.lower() in PLACEHOLDER_TEXTS else text


def _is_placeholder_amount(amount: Decimal) -> bool:
    """Detect filler amounts: implausible sizes, repeated digits, 12345678 and similar."""
    if amount >= MAX_PLAUSIBLE_AMOUNT:
        return True
    if amount != amount.to_integral_value():
        return False
    digits = str(int(amount))
    if len(digits) < _PLACEHOLDER_MIN_DIGITS:
        return False
    return len(set(digits)) == 1 or digits in _ASCENDING_DIGITS


def _normalise_number(number: str) -> str:
    """Turn Dutch or English digit grouping into a plain decimal string."""
    if "," in number and "." in number:
        # Whichever separator comes last is the decimal mark.
        if number.rfind(",") > number.rfind("."):
            return number.replace(".", "").replace(",", ".")
        return number.replace(",", "")
    if "," in number:
        decimals = number.split(",")[-1]
        return number.replace(",", "." if len(decimals) <= 2 else "")
    if number.count(".") > 1:
        return number.replace(".", "")
    if number.count(".") == 1 and len(number.split(".")[-1]) == 3:
        # A single dot followed by exactly three digits is a Dutch thousands separator.
        return number.replace(".", "")
    return number


def _valid_amount(amount: Decimal) -> Decimal | None:
    if not amount.is_finite() or amount <= 0 or _is_placeholder_amount(amount):
        return None
    return amount


def parse_amount(value: object) -> Decimal | None:
    """Parse `EUR 720000`, `1.234.567,89`, `234788.49` or a number into a Decimal.

    Spaces are allowed only as thousands separators between three-digit groups
    (`720 000`, `1 234 567,89`); any other space inside a number gives None.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return _valid_amount(value)
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        return _valid_amount(Decimal(str(value)))

    text = _clean_text(value)
    if text is None:
        return None
    match = _AMOUNT_RE.search(text.replace("\xa0", " "))
    if not match:
        return None
    candidate = match.group(0).strip()
    if re.search(r"\s", candidate):
        if not _SPACED_AMOUNT_RE.fullmatch(candidate):
            return None
        candidate = candidate.replace(" ", "")
    number = _normalise_number(candidate)
    try:
        amount = Decimal(number)
    except InvalidOperation:
        return None
    return _valid_amount(amount)


def parse_tender_value(value: object) -> Decimal | None:
    """Parse an estimated or contract value; amounts under EUR 100 are placeholders."""
    amount = parse_amount(value)
    if amount is None or amount < MIN_TENDER_VALUE:
        return None
    return amount


def parse_currency(value: object) -> str | None:
    """Return the first known ISO currency code in the text, upper-cased."""
    text = _clean_text(value)
    if text is None:
        return None
    for word in _WORD3_RE.findall(text):
        code = word.upper()
        if code in CURRENCIES:
            return code
    return None


def parse_duration_months(term: object, unit: object = None) -> float | None:
    """Parse `4 Jaren` or (`36`, `Maanden`) into months; a bare number means months."""
    combined = " ".join(t for t in (_clean_text(term), _clean_text(unit)) if t).lower()
    number = _NUMBER_RE.search(combined)
    if not number:
        return None
    quantity = float(number.group(0).replace(",", "."))
    if quantity <= 0:
        return None

    factor = next(
        (_UNIT_MONTHS[word] for word in _WORD_RE.findall(combined) if word in _UNIT_MONTHS),
        1.0,
    )
    months = quantity * factor
    return months if 0 < months <= MAX_DURATION_MONTHS else None


def parse_date(value: object) -> date | None:
    """Parse an ISO, DD-MM-YYYY or datetime value into a date (day-first, never month-first)."""
    if isinstance(value, datetime):
        parsed: date | None = value.date()
    elif isinstance(value, date):
        parsed = value
    else:
        text = _clean_text(value)
        if text is None:
            return None
        # Drop milliseconds and time-zone offsets: `2026-09-11T14:32:00.677+02:00`.
        text = text[:19]
        parsed = None
        for fmt in _DATE_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt).date()
                break
            except ValueError:
                continue

    if parsed is None or not MIN_YEAR <= parsed.year <= MAX_YEAR:
        return None
    return parsed


# Closing dates that mean "unknown" (ARCHITECTURE.md 2.3, DECISIONS.md data findings).
PLACEHOLDER_DEADLINE = date(2099, 12, 31)
MAX_DEADLINE_YEARS = 5


def _add_years(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # 29 February in a non-leap target year
        return day.replace(year=day.year + years, day=28)


def is_placeholder_deadline(closing: date, published: date | None) -> bool:
    """True for 2099-12-31, or a closing date more than five years after publication."""
    if closing == PLACEHOLDER_DEADLINE:
        return True
    return published is not None and closing > _add_years(published, MAX_DEADLINE_YEARS)
