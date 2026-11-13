"""Hard filter (ARCHITECTURE.md 2.3): plain-Python rules on a parsed TED notice.

No model calls, no network. Each rule gives pass, fail or unknown with a reason; a tender is
dropped only on a definite fail. Unknown is not a pass: the tender continues with a flag.

The deadline is TenderNed `sluitingsDatum`, passed in by the caller. The notice's own
deadline (`TedNotice.deadline`) is eForms ground truth only and is never read here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Literal

from ingest.parsing import is_placeholder_deadline, parse_date
from ingest.ted import TedNotice

Outcome = Literal["pass", "fail", "unknown"]

NL_CODES = frozenset({"NLD", "NL"})
CPV_PREFIXES = ("72", "79", "48", "73")
CONTRACT_NOTICE_TYPES = frozenset({"cn-standard", "cn-social"})
NON_CONTRACT_PREFIXES = ("can-", "pin-")  # award notices, prior information notices
NON_CONTRACT_TYPES = frozenset({"veat", "pmc", "cn-desg"})
MIN_VALUE = Decimal("50000")
MAX_VALUE = Decimal("2000000")  # above: kept with the flag consortium_only (Q11)
MIN_WORKING_DAYS = 10


@dataclass(frozen=True, slots=True)
class RuleResult:
    outcome: Outcome
    reason: str
    raw: str | None = None


@dataclass(frozen=True, slots=True)
class FilterResult:
    passed: bool  # False only when at least one rule is a definite fail
    rules: dict[str, RuleResult]
    flags: tuple[str, ...]

    def to_json(self) -> dict[str, dict[str, str | None]]:
        """The `tenders.filter_result` shape from ARCHITECTURE.md 2.3."""
        return {
            name: {"outcome": rule.outcome, "reason": rule.reason, "raw": rule.raw}
            for name, rule in self.rules.items()
        }


# --- rules --------------------------------------------------------------------


def _country(notice: TedNotice) -> RuleResult:
    country = notice.country
    if not country:
        return RuleResult("unknown", "missing")
    if country.upper() in NL_CODES:
        return RuleResult("pass", "nl", country)
    return RuleResult("fail", "not_nl", country)


def _cpv(notice: TedNotice, flags: list[str]) -> RuleResult:
    lot_codes = [
        code for lot in notice.lots for code in (lot.cpv_main, *lot.cpv_additional) if code
    ]
    codes = list(dict.fromkeys([*notice.cpv_codes, *lot_codes]))
    if not codes:
        return RuleResult("unknown", "missing")
    matching = [code for code in codes if code.startswith(CPV_PREFIXES)]
    if not matching:
        return RuleResult("fail", "no_target_cpv", ",".join(codes))
    if all(code.startswith("73") for code in matching):
        flags.append("cpv_73_check_scope")  # COMPANY.md: 73 only for AI/data work
    return RuleResult("pass", "target_cpv", ",".join(matching))


def _working_days_after(start: date, end: date) -> int:
    """Monday-to-Friday days after `start` up to and including `end` (no public holidays)."""
    days, day = 0, start + timedelta(days=1)
    while day <= end:
        days += day.weekday() < 5
        day += timedelta(days=1)
    return days


def _deadline(
    notice: TedNotice, sluitingsdatum: str | None, tenderned_matched: bool, today: date
) -> RuleResult:
    if not tenderned_matched:
        return RuleResult("unknown", "no_tenderned_match")
    raw = sluitingsdatum.strip() if sluitingsdatum else None
    if not raw:
        return RuleResult("unknown", "missing")
    closing = parse_date(raw)
    if closing is None:
        return RuleResult("unknown", "missing", raw)  # unparseable: raw kept for review
    published = notice.publication_date or notice.issue_date
    if is_placeholder_deadline(closing, published):
        return RuleResult("unknown", "placeholder_date", raw)
    if closing < today:
        return RuleResult("fail", "closed", raw)
    if published is None:
        return RuleResult("unknown", "no_publication_date", raw)
    if _working_days_after(published, closing) < MIN_WORKING_DAYS:
        return RuleResult("fail", "too_short", raw)
    return RuleResult("pass", "ok", raw)


def _eur(value: Decimal | None, currency: str | None) -> Decimal | None:
    return value if value is not None and currency == "EUR" else None


def _value(notice: TedNotice, flags: list[str]) -> RuleResult:
    """Per lot (Q8 applied to the filter): lot values first, the procedure value only when
    no lot has one. Pass if any value is in range; above 2M only: pass + consortium_only."""
    lots = [(lot.lot_id or f"lot-{i}", _eur(lot.estimated_value, lot.currency))
            for i, lot in enumerate(notice.lots, start=1)]  # fmt: skip
    basis = [(lot_id, value) for lot_id, value in lots if value is not None]
    some_missing = len(basis) < len(lots)
    if not basis:
        procedure = _eur(notice.estimated_value, notice.currency)
        if procedure is None:
            return RuleResult("unknown", "missing")
        basis, some_missing = [("procedure", procedure)], False

    raw = "; ".join(f"{name}={value}" for name, value in basis)
    in_range = [name for name, v in basis if MIN_VALUE <= v <= MAX_VALUE]
    above = [name for name, v in basis if v > MAX_VALUE]
    below = [name for name, v in basis if v < MIN_VALUE]
    if in_range:
        if above or below:
            flags.append("lots_out_of_range")
            return RuleResult("pass", f"in_range; out_of_range={','.join(above + below)}", raw)
        return RuleResult("pass", "in_range", raw)
    if above:
        flags.append("consortium_only")
        return RuleResult("pass", "above_2m", raw)
    if some_missing:
        return RuleResult("unknown", "partly_missing", raw)  # a lot without a value may fit
    return RuleResult("fail", "below_50k", raw)


def _notice_type(notice: TedNotice) -> RuleResult:
    kind = notice.notice_type
    if kind in CONTRACT_NOTICE_TYPES:
        return RuleResult("pass", "contract_notice", kind)
    if kind.startswith(NON_CONTRACT_PREFIXES) or kind in NON_CONTRACT_TYPES:
        return RuleResult("fail", "not_contract_notice", kind)
    return RuleResult("unknown", "unrecognised_type", kind)


# --- entry point --------------------------------------------------------------


def apply_hard_filter(
    notice: TedNotice,
    *,
    sluitingsdatum: str | None,
    tenderned_matched: bool,
    today: date,
) -> FilterResult:
    """Apply the five hard rules. `sluitingsdatum` is the raw TenderNed value."""
    flags: list[str] = []
    rules = {
        "country": _country(notice),
        "cpv": _cpv(notice, flags),
        "deadline": _deadline(notice, sluitingsdatum, tenderned_matched, today),
        "value": _value(notice, flags),
        "notice_type": _notice_type(notice),
    }
    unknown_flags = [f"unknown:{name}" for name, rule in rules.items() if rule.outcome == "unknown"]
    if notice.main_nature and notice.main_nature != "services":
        flags.append("nature_supplies")  # BT-23: a flag, not a fail (DECISIONS.md)
    if any(lot.place_country and lot.place_country.upper() not in NL_CODES for lot in notice.lots):
        flags.append("place_outside_nl")
    if notice.is_change:
        flags.append("change_notice")
    return FilterResult(
        passed=not any(rule.outcome == "fail" for rule in rules.values()),
        rules=rules,
        flags=tuple(unknown_flags + flags),
    )
