"""Pure metric functions for the evals. The formulas are in docs/EVALS.md section 4.

No I/O and no model calls: every function takes plain values and returns plain values, so
each formula can be tested on its own.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any
from zoneinfo import ZoneInfo

AMSTERDAM = ZoneInfo("Europe/Amsterdam")
Z95 = 1.96

SUITES = ("retrieval", "screening", "extraction", "knockout", "memo", "edge_cases")

# --- basics --------------------------------------------------------------------


def ratio(numerator: float, denominator: float) -> float | None:
    """A ratio, or None when the denominator is zero."""
    return numerator / denominator if denominator else None


def mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def wilson(successes: int, n: int, z: float = Z95) -> tuple[float, float] | None:
    """Wilson score interval for a proportion. None when there are no cases."""
    if n <= 0:
        return None
    p = successes / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    scale = 1 + z * z / n
    return max(0.0, (centre - margin) / scale), min(1.0, (centre + margin) / scale)


def proportion(successes: int, n: int) -> dict[str, Any]:
    """A proportion with its count and Wilson 95% interval, as stored in the results file."""
    interval = wilson(successes, n)
    return {"value": ratio(successes, n), "n": n, "ci95": list(interval) if interval else None}


def percentile(values: Sequence[float], q: float) -> float | None:
    """Nearest rank: the value at position ceil(q * n) of the sorted values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(q * len(ordered))) - 1]


def p50_p95(values: Sequence[float]) -> dict[str, Any]:
    return {"n": len(values), "p50": percentile(values, 0.50), "p95": percentile(values, 0.95)}


# --- screening (4.2) -----------------------------------------------------------

POSITIVE_DECISIONS = frozenset({"shortlist", "unsure"})  # decision Q13: unsure is shortlisted
THRESHOLDS = tuple(round(step / 10, 1) for step in range(1, 10))


@dataclass(frozen=True, slots=True)
class ScreeningRow:
    actual: bool  # the label is "relevant"
    filter_passed: bool
    decision: str | None = None  # None: dropped by the hard filter, or the call failed
    confidence: float | None = None
    error: bool = False


def screening_positive(row: ScreeningRow) -> bool:
    return row.filter_passed and row.decision in POSITIVE_DECISIONS


def f_beta(precision: float | None, recall: float | None, beta: float = 2.0) -> float | None:
    if precision is None or recall is None:
        return None
    weight = beta * beta
    return ratio((1 + weight) * precision * recall, weight * precision + recall)


def _confusion(pairs: Iterable[tuple[bool, bool]]) -> tuple[int, int, int, int]:
    """(predicted, actual) pairs to TP, FP, FN, TN."""
    tp = fp = fn = tn = 0
    for predicted, actual in pairs:
        if predicted and actual:
            tp += 1
        elif predicted:
            fp += 1
        elif actual:
            fn += 1
        else:
            tn += 1
    return tp, fp, fn, tn


def screening_metrics(
    rows: Sequence[ScreeningRow], min_precision: float | None = None
) -> dict[str, Any]:
    tp, fp, fn, tn = _confusion((screening_positive(row), row.actual) for row in rows)
    precision, recall = ratio(tp, tp + fp), ratio(tp, tp + fn)
    by_confidence = []
    for tau in THRESHOLDS:
        counts = _confusion(
            (
                screening_positive(row) and row.confidence is not None and row.confidence >= tau,
                row.actual,
            )
            for row in rows
        )
        by_confidence.append(
            {
                "threshold": tau,
                "precision": ratio(counts[0], counts[0] + counts[1]),
                "recall": ratio(counts[0], counts[0] + counts[2]),
            }
        )
    metrics: dict[str, Any] = {
        "precision": proportion(tp, tp + fp),
        "recall": proportion(tp, tp + fn),
        "f2": f_beta(precision, recall),
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "lost_in_filter": sum(row.actual and not row.filter_passed for row in rows),
        "lost_in_model": sum(
            row.actual and row.filter_passed and not screening_positive(row) for row in rows
        ),
        "errors": sum(row.error for row in rows),
        "by_confidence": by_confidence,
    }
    if min_precision is not None:
        reached = [
            point["recall"]
            for point in by_confidence
            if point["precision"] is not None
            and point["precision"] >= min_precision
            and point["recall"] is not None
        ]
        metrics["recall_at_precision"] = {
            "min_precision": min_precision,
            "recall": max(reached) if reached else None,
        }
    return metrics


# --- retrieval (4.3) -----------------------------------------------------------

RETRIEVAL_K = 10
HIT_K = 5


@dataclass(frozen=True, slots=True)
class RankedChunk:
    doc_key: str | None  # None: the chunk's document is not in the snapshot
    page_start: int
    page_end: int


def first_relevant_rank(
    chunks: Sequence[RankedChunk], answers: Mapping[str, Iterable[int]]
) -> int | None:
    """1-based rank of the first chunk that covers an answer page of the same document."""
    pages = {key: set(values) for key, values in answers.items()}
    for rank, chunk in enumerate(chunks, start=1):
        wanted = pages.get(chunk.doc_key) if chunk.doc_key is not None else None
        if wanted and any(chunk.page_start <= page <= chunk.page_end for page in wanted):
            return rank
    return None


def hit_at_k(ranks: Sequence[int | None], k: int = HIT_K) -> dict[str, Any]:
    return proportion(sum(rank is not None and rank <= k for rank in ranks), len(ranks))


def mrr_at_k(ranks: Sequence[int | None], k: int = RETRIEVAL_K) -> float | None:
    return mean([1 / rank if rank is not None and rank <= k else 0.0 for rank in ranks])


# --- field extraction (4.1) ----------------------------------------------------

TITLE_THRESHOLD = 0.80
AMOUNT_TOLERANCE = Decimal("0.01")
DEADLINE_TOLERANCE = timedelta(minutes=60)
DURATION_TOLERANCE_MONTHS = 1
MIN_COVERAGE = 0.40
VAT = Decimal("1.21")

GRADED_FIELDS = (
    "deadline",
    "deadline_date_only",
    "lot_count",
    "lot_titles",
    "estimated_value",
    "framework_max_value",
    "duration_months",
)
# Fields of ARCHITECTURE.md 6.1 that `ingest/ted.py` does not parse yet: no ground truth.
UNGRADED_FIELDS = (
    "award_types",
    "award_weights",
    "selection_types",
    "selection_threshold",
    "is_framework",
    "security_clearance",
)
EXTRACTION_FIELDS = GRADED_FIELDS + UNGRADED_FIELDS
REPORT_ONLY_FIELDS = frozenset({"deadline_date_only"})  # shown, not in the score

_PUNCTUATION = re.compile(r"[^\w\s]")


@dataclass(frozen=True, slots=True)
class LotFacts:
    title: str | None = None
    estimated_value: Decimal | None = None
    duration_months: float | None = None
    ceiling_value: Decimal | None = None


@dataclass(frozen=True, slots=True)
class ExtractionFacts:
    """The graded facts of one tender: once from the notice (truth), once from the model."""

    deadline: datetime | None = None
    deadline_has_time: bool = True  # False: the truth is a date without a time
    lots: tuple[LotFacts, ...] = ()
    lot_count: int | None = None  # overrides len(lots) when set
    estimated_value: Decimal | None = None  # procedure level
    framework_max_value: Decimal | None = None


@dataclass(slots=True)
class TenderGrade:
    units: dict[str, list[float]] = field(default_factory=dict)  # field -> 1.0, 0.0 or a share
    vat_explained: int = 0


def normalise_title(text: str | None) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    return " ".join(_PUNCTUATION.sub(" ", text).split())


def title_similarity(a: str | None, b: str | None) -> float:
    left, right = normalise_title(a), normalise_title(b)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def align_lots(
    truth: Sequence[str | None], predicted: Sequence[str | None], threshold: float = TITLE_THRESHOLD
) -> dict[int, int]:
    """Truth lot index to predicted lot index. One-to-one, best title match first."""
    if len(truth) == 1 and len(predicted) == 1:
        return {0: 0}  # a single lot on both sides needs no title check
    scored = sorted(
        (
            (title_similarity(t, p), i, j)
            for i, t in enumerate(truth)
            for j, p in enumerate(predicted)
        ),
        key=lambda item: (-item[0], item[1], item[2]),
    )
    pairs: dict[int, int] = {}
    used: set[int] = set()
    for score, i, j in scored:
        if score < threshold:
            break
        if i not in pairs and j not in used:
            pairs[i] = j
            used.add(j)
    return pairs


def to_amsterdam(value: datetime) -> datetime:
    """A naive datetime is read as Europe/Amsterdam time, like TenderNed's closing date."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=AMSTERDAM)
    return value.astimezone(AMSTERDAM)


def same_local_date(predicted: datetime | None, truth: datetime) -> bool:
    return predicted is not None and to_amsterdam(predicted).date() == to_amsterdam(truth).date()


def deadline_correct(predicted: datetime | None, truth: datetime) -> bool:
    if predicted is None or not same_local_date(predicted, truth):
        return False
    return abs(to_amsterdam(predicted) - to_amsterdam(truth)) <= DEADLINE_TOLERANCE


def amount_correct(predicted: Decimal | None, truth: Decimal) -> bool:
    return predicted is not None and abs(predicted - truth) <= AMOUNT_TOLERANCE * abs(truth)


def vat_explained(predicted: Decimal | None, truth: Decimal) -> bool:
    """A miss that fits once 21% VAT is taken off. Counted, and still a miss."""
    if predicted is None or amount_correct(predicted, truth):
        return False
    return amount_correct(predicted / VAT, truth)


def duration_correct(predicted: float | None, truth: float) -> bool:
    return predicted is not None and abs(predicted - truth) <= DURATION_TOLERANCE_MONTHS


def _sum_or_none(values: Iterable[Decimal | None]) -> Decimal | None:
    present = [value for value in values if value is not None]
    return sum(present, Decimal(0)) if present else None


def grade_extraction(
    truth: ExtractionFacts, predicted: ExtractionFacts | None, skip: Iterable[str] = ()
) -> TenderGrade:
    """Grade one tender. `predicted` None (the extraction failed) is wrong on every unit."""
    grade = TenderGrade()
    skipped = set(skip)
    lots = predicted.lots if predicted is not None else ()

    def add(name: str, value: float | bool) -> None:
        if name not in skipped:
            grade.units.setdefault(name, []).append(float(value))

    if truth.deadline is not None:
        guess = predicted.deadline if predicted is not None else None
        add("deadline_date_only", same_local_date(guess, truth.deadline))
        if truth.deadline_has_time:
            add("deadline", deadline_correct(guess, truth.deadline))

    truth_count = truth.lot_count if truth.lot_count is not None else len(truth.lots)
    if truth_count:
        add("lot_count", predicted is not None and max(len(lots), 1) == truth_count)

    pairs = align_lots([lot.title for lot in truth.lots], [lot.title for lot in lots])
    if len(truth.lots) >= 2:  # with one lot there is no title to tell apart
        add("lot_titles", len(pairs) / max(len(truth.lots), len(lots)))

    def record_amount(name: str, guess: Decimal | None, value: Decimal) -> None:
        add(name, amount_correct(guess, value))
        if name not in skipped and vat_explained(guess, value):
            grade.vat_explained += 1

    if any(lot.estimated_value is not None for lot in truth.lots):
        for index, lot in enumerate(truth.lots):
            if lot.estimated_value is not None:
                match = lots[pairs[index]] if index in pairs else None
                record_amount(
                    "estimated_value", match.estimated_value if match else None, lot.estimated_value
                )
    elif truth.estimated_value is not None:
        total = _sum_or_none(lot.estimated_value for lot in lots)
        record_amount("estimated_value", total, truth.estimated_value)

    if truth.framework_max_value is not None:
        ceiling = predicted.framework_max_value if predicted is not None else None
        if ceiling is None:
            ceiling = _sum_or_none(lot.ceiling_value for lot in lots)
        record_amount("framework_max_value", ceiling, truth.framework_max_value)

    for index, lot in enumerate(truth.lots):
        if lot.duration_months is not None:
            match = lots[pairs[index]] if index in pairs else None
            add(
                "duration_months",
                duration_correct(match.duration_months if match else None, lot.duration_months),
            )
    return grade


def field_summary(per_tender: Sequence[Sequence[float]], tenders: int) -> dict[str, Any]:
    """Accuracy and coverage of one field. `per_tender` holds the units of each tender."""
    units = [unit for tender in per_tender for unit in tender]
    coverage = ratio(sum(1 for tender in per_tender if tender), tenders)
    whole = all(unit in (0.0, 1.0) for unit in units)
    interval = wilson(int(sum(units)), len(units)) if whole else None
    return {
        "accuracy": mean(units),
        "n": len(units),
        "ci95": list(interval) if interval else None,
        "coverage": coverage,
        "low_coverage": coverage is None or coverage < MIN_COVERAGE,
    }


def extraction_score(fields: Mapping[str, Mapping[str, Any]]) -> float | None:
    """Mean accuracy over the graded fields with enough coverage."""
    return mean(
        [
            summary["accuracy"]
            for name, summary in fields.items()
            if name not in REPORT_ONLY_FIELDS
            and summary["accuracy"] is not None
            and not summary["low_coverage"]
        ]
    )


# --- knock-out recall (4.4) ----------------------------------------------------

FORMAL = "formal"
PAGE_TOLERANCE = 1


@dataclass(frozen=True, slots=True)
class Requirement:
    """A knock-out as labelled, or as extracted. `ref` is the label ID or requirement ID."""

    ref: str
    doc_key: str | None  # None: the cited document is not in the snapshot
    page: int
    category: str
    kind: str


@dataclass(frozen=True, slots=True)
class TenderKnockouts:
    labels: int  # non-formal labels
    matched: int
    predictions: int  # non-formal predictions
    page_only_matched: int
    formal_labels: int
    formal_matched: int
    pairs: Mapping[str, str]  # label ref -> prediction ref, non-formal only


def maximum_matching(left: int, right: int, allowed: Callable[[int, int], bool]) -> dict[int, int]:
    """Largest one-to-one matching of left items to right items (augmenting paths)."""
    owner: dict[int, int] = {}  # right index -> left index

    def place(i: int, seen: set[int]) -> bool:
        for j in range(right):
            if j in seen or not allowed(i, j):
                continue
            seen.add(j)
            if j not in owner or place(owner[j], seen):
                owner[j] = i
                return True
        return False

    for i in range(left):
        place(i, set())
    return {i: j for j, i in owner.items()}


def _match(
    labels: Sequence[Requirement], predictions: Sequence[Requirement], *, check_category: bool
) -> dict[int, int]:
    def allowed(i: int, j: int) -> bool:
        label, prediction = labels[i], predictions[j]
        return (
            prediction.doc_key is not None
            and label.doc_key == prediction.doc_key
            and abs(label.page - prediction.page) <= PAGE_TOLERANCE
            and (not check_category or label.category == prediction.category)
        )

    return maximum_matching(len(labels), len(predictions), allowed)


def grade_knockouts(
    labels: Sequence[Requirement], predictions: Sequence[Requirement]
) -> TenderKnockouts:
    """Match the labels of one tender to its extracted knock-outs.

    Formal and non-formal requirements are matched apart: a real knock-out extracted as a
    formal action is never checked against company evidence, so it does not count as found.
    """
    real_labels = [item for item in labels if item.kind != FORMAL]
    real_predictions = [item for item in predictions if item.kind != FORMAL]
    formal_labels = [item for item in labels if item.kind == FORMAL]
    formal_predictions = [item for item in predictions if item.kind == FORMAL]
    found = _match(real_labels, real_predictions, check_category=True)
    return TenderKnockouts(
        labels=len(real_labels),
        matched=len(found),
        predictions=len(real_predictions),
        page_only_matched=len(_match(real_labels, list(predictions), check_category=False)),
        formal_labels=len(formal_labels),
        formal_matched=len(_match(formal_labels, formal_predictions, check_category=True)),
        pairs={real_labels[i].ref: real_predictions[j].ref for i, j in found.items()},
    )


def knockout_metrics(tenders: Sequence[TenderKnockouts]) -> dict[str, Any]:
    labels = sum(t.labels for t in tenders)
    matched = sum(t.matched for t in tenders)
    with_labels = [t for t in tenders if t.labels]
    return {
        "recall": proportion(matched, labels),
        "recall_macro": mean([t.matched / t.labels for t in with_labels]),
        "tenders_all_found": proportion(
            sum(t.matched == t.labels for t in with_labels), len(with_labels)
        ),
        "recall_page_only": proportion(sum(t.page_only_matched for t in tenders), labels),
        "recall_formal": proportion(
            sum(t.formal_matched for t in tenders), sum(t.formal_labels for t in tenders)
        ),
        "precision": proportion(matched, sum(t.predictions for t in tenders)),
    }


def match_accuracy(pairs: Sequence[tuple[str | None, str]]) -> dict[str, Any]:
    """`pairs` are (predicted status, expected status). None: the requirement had no match."""
    expected_not_met = [predicted for predicted, expected in pairs if expected == "not_met"]
    return {
        "match_accuracy": proportion(
            sum(predicted == expected for predicted, expected in pairs), len(pairs)
        ),
        "false_met_rate": proportion(
            sum(predicted == "met" for predicted in expected_not_met), len(expected_not_met)
        ),
    }


# --- memo quality (4.5) --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Criterion:
    id: str
    scale_max: int
    weight: float


def memo_score(scores: Mapping[str, int], criteria: Sequence[Criterion]) -> float:
    """Weighted mean of the criterion scores, each scaled from 1..S to 0..1."""
    total = 0.0
    for criterion in criteria:
        score = scores.get(criterion.id)
        if score is None or not 1 <= score <= criterion.scale_max:
            raise ValueError(f"no valid score for criterion {criterion.id!r}")
        total += criterion.weight * (score - 1) / (criterion.scale_max - 1)
    return total / sum(criterion.weight for criterion in criteria)


def agreement(pairs: Sequence[tuple[int, int]]) -> dict[str, Any]:
    """Judge against hand grades. `pairs` are (judge score, hand score) for one criterion."""
    return {
        "n": len(pairs),
        "exact_agreement": ratio(sum(judge == hand for judge, hand in pairs), len(pairs)),
        "within_one": ratio(sum(abs(judge - hand) <= 1 for judge, hand in pairs), len(pairs)),
        "mean_abs_diff": mean([abs(judge - hand) for judge, hand in pairs]),
    }


# --- edge cases (4.6) ----------------------------------------------------------


@dataclass(slots=True)
class Outcome:
    """What the pipeline did with one edge case. `model_text` is checked and never stored."""

    filter_passed: bool | None = None
    filter_failed_rules: tuple[str, ...] = ()
    filter_flags: tuple[str, ...] = ()
    screened: bool = False  # a screening call was made
    memo_created: bool = False
    recommendation: str | None = None
    reason_codes: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    extraction_warnings: tuple[str, ...] = ()
    chat_in_scope: bool | None = None
    model_text: tuple[str, ...] = ()
    model_calls: int = 0


def _is_strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_bool(value: object) -> bool:
    return isinstance(value, bool)


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_string(value: object) -> bool:
    return isinstance(value, str)


def _excluded(outcome: Outcome, banned: Sequence[str]) -> bool:
    text = "\n".join(outcome.model_text).casefold()
    return not any(item.casefold() in text for item in banned)


# key -> (the value has the right type, the expectation holds)
_CHECKS: dict[str, tuple[Callable[[Any], bool], Callable[[Outcome, Any], bool]]] = {
    "filter_passed": (_is_bool, lambda o, v: o.filter_passed is v),
    "filter_failed_rule": (_is_string, lambda o, v: v in o.filter_failed_rules),
    "filter_flags_include": (_is_strings, lambda o, v: set(v) <= set(o.filter_flags)),
    "screened": (_is_bool, lambda o, v: o.screened is v),
    "memo_created": (_is_bool, lambda o, v: o.memo_created is v),
    "recommendation": (_is_string, lambda o, v: o.recommendation == v),
    "reason_codes_include": (_is_strings, lambda o, v: set(v) <= set(o.reason_codes)),
    "limitations_include": (_is_strings, lambda o, v: set(v) <= set(o.limitations)),
    "extraction_warnings_include": (
        _is_strings,
        lambda o, v: all(any(item in warning for warning in o.extraction_warnings) for item in v),
    ),
    "chat_in_scope": (_is_bool, lambda o, v: o.chat_in_scope is v),
    "output_excludes": (_is_strings, _excluded),
    "max_model_calls": (_is_count, lambda o, v: o.model_calls <= v),
}
EXPECT_KEYS = frozenset(_CHECKS)


@dataclass(frozen=True, slots=True)
class Verdict:
    status: str  # "pass", "fail" or "invalid_label"
    failed: tuple[str, ...] = ()  # keys that did not hold, or that are not valid


def check_expectations(expect: Mapping[str, Any], outcome: Outcome) -> Verdict:
    invalid = tuple(
        key for key, value in expect.items() if key not in _CHECKS or not _CHECKS[key][0](value)
    )
    if invalid or not expect:
        return Verdict("invalid_label", invalid)
    failed = tuple(key for key, value in expect.items() if not _CHECKS[key][1](outcome, value))
    return Verdict("fail" if failed else "pass", failed)


# --- composite and gate (4.7) --------------------------------------------------


def composite(scores: Mapping[str, float | None], weights: Mapping[str, float]) -> dict[str, Any]:
    """`value` needs every weighted suite; `partial` is rescaled to the suites that ran."""
    included = [name for name in weights if scores.get(name) is not None]
    weight = sum(weights[name] for name in included)
    total = sum(weights[name] * scores[name] for name in included)  # type: ignore[operator]
    return {
        "value": total if len(included) == len(weights) else None,
        "partial": total / weight if weight else None,
        "suites_included": included,
    }


def gate(recall: float | None, minimum: float | None, evaluated: bool) -> str:
    """The knock-out recall gate: pass, fail, not_set or not_evaluated."""
    if minimum is None:
        return "not_set"
    if not evaluated or recall is None:
        return "not_evaluated"
    return "pass" if recall >= minimum else "fail"
