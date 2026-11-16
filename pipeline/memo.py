"""Fit score and recommendation rules (ARCHITECTURE.md 5.4, MEMO_TEMPLATE.md section 3).

Pure code: no model calls, no database. The model scores four fit dimensions and writes text;
the total, the provisional range and the recommendation are decided here.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from llm.redact import Redactor
from pipeline.citations import PageLookup, verify_citations
from pipeline.schemas import (
    UNMATCHED_KINDS,
    FitDimension,
    FitDimensionName,
    FitScore,
    KnockoutCriterion,
    Recommendation,
    RequirementMatch,
)

# Weight in percent and who scores it, in template order (decision Q9).
FIT_WEIGHTS: dict[FitDimensionName, tuple[int, Literal["copilot", "human"]]] = {
    "scope_match": (25, "copilot"),
    "win_probability": (20, "human"),
    "references": (15, "copilot"),
    "commercial": (15, "human"),
    "capacity_timing": (10, "copilot"),
    "contract_risk": (10, "copilot"),
    "strategic_value": (5, "human"),
}
BID_MIN = 70
CONDITIONAL_MIN = 50

# Gaps the company can close itself before the closing date.
CLOSABLE_MITIGATIONS = frozenset(
    {"consortium", "subcontractor", "third_party", "obtain_before_deadline"}
)

Band = Literal["bid", "bid_conditional", "no_bid"]
_SEVERITY: dict[Band, int] = {"bid": 0, "bid_conditional": 1, "no_bid": 2}


@dataclass(frozen=True, slots=True)
class RecommendationResult:
    recommendation: Recommendation
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CheckedRecommendation:
    """The recommendation, with the inputs as checked: every `verified` flag set by code."""

    knockouts: tuple[KnockoutCriterion, ...]
    matches: tuple[RequirementMatch, ...]
    fit: FitScore
    result: RecommendationResult


# --- fit score -----------------------------------------------------------------


def _has_usable_source(dimension: FitDimension) -> bool:
    """KB evidence, or a tender citation that passed the citation check."""
    if dimension.kb_evidence:
        return True
    return dimension.citation is not None and dimension.citation.verified is True


def compute_fit(dimensions: Iterable[FitDimension]) -> FitScore:
    """Total = sum(weight x score) / 5, with the range the total can still reach.

    Dimensions left out are unscored. An unscored dimension counts 1 for the minimum and 5 for
    the maximum. A copilot score with no usable source is dropped to unscored. Weights and
    `scored_by` always come from `FIT_WEIGHTS`, whatever the input says.
    """
    given: dict[str, FitDimension] = {}
    for dimension in dimensions:
        if dimension.name in given:
            raise ValueError(f"fit dimension given twice: {dimension.name}")
        given[dimension.name] = dimension

    result: list[FitDimension] = []
    low = high = 0
    for name, (weight, scored_by) in FIT_WEIGHTS.items():
        dimension = given.get(name) or FitDimension(
            name=name, weight_pct=weight, scored_by=scored_by
        )
        score = dimension.score
        if scored_by == "copilot" and score is not None and not _has_usable_source(dimension):
            score = None
        dimension = dimension.model_copy(
            update={"weight_pct": weight, "scored_by": scored_by, "score": score}
        )
        result.append(dimension)
        points = weight // 5  # every weight is a multiple of 5, so the total is a whole number
        low += points * (score if score is not None else 1)
        high += points * (score if score is not None else 5)

    return FitScore(
        dimensions=result,
        total=low if low == high and all(d.score is not None for d in result) else None,
        provisional_min=low,
        provisional_max=high,
    )


def fit_band(score: int) -> Band:
    if score >= BID_MIN:
        return "bid"
    if score >= CONDITIONAL_MIN:
        return "bid_conditional"
    return "no_bid"


# --- recommendation ------------------------------------------------------------


def _worse(a: Band, b: Band) -> Band:
    return a if _SEVERITY[a] >= _SEVERITY[b] else b


def _add(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def decide_recommendation(
    *,
    knockouts: Sequence[KnockoutCriterion],
    matches: Sequence[RequirementMatch],
    fit: FitScore,
    documents_missing: bool,
) -> RecommendationResult:
    """Apply the knock-out rules, then the fit score. The model has no say in the value.

    Pipeline code calls `recommend`, which runs the citation check first. This function only
    reads the `verified` flags that check has set; None counts as unverified.
    A knock-out is verified when both its own citation and its match's tender citation are.

    Formal knock-outs are ignored: they are actions in the bid, not matched. With only formal
    knock-outs the rules see none, and a match for a formal one has no knock-out to belong to.

    A `not_met` knock-out that applies to named lots only (`applies_to_lots` not empty) gives
    `needs_review` with `lot_specific_knockout`, whatever its mitigation (decision R9).
    """
    if documents_missing:
        return RecommendationResult("needs_review", ("documents_missing",))
    knockouts = [k for k in knockouts if k.kind not in UNMATCHED_KINDS]
    if not knockouts:
        return RecommendationResult("needs_review", ("no_knockouts_found",))

    review: list[str] = []
    knockout_ids = [k.requirement_id for k in knockouts]
    match_ids = [m.requirement_id for m in matches]
    if len(set(knockout_ids)) != len(knockout_ids) or len(set(match_ids)) != len(match_ids):
        _add(review, "duplicate_requirement_id")
    if set(match_ids) - set(knockout_ids):
        _add(review, "match_without_knockout")
    by_id = {m.requirement_id: m for m in matches}

    confirmed_not_met = False
    ceiling: Band = "bid"
    for knockout in knockouts:
        match = by_id.get(knockout.requirement_id)
        if match is None:
            _add(review, "knockout_unmatched")
            continue
        verified = knockout.citation.verified is True and match.tender_citation.verified is True
        if match.status == "not_met" and knockout.applies_to_lots:
            # Interim rule while Q8 (one memo per tender or per lot) is open: the requirement
            # may rule out one lot only, so it never gives no_bid or a cap for the tender.
            _add(review, "lot_specific_knockout")
            if not verified:
                _add(review, "knockout_citation_unverified")
            continue
        if match.status == "not_met" and match.mitigation == "none":
            if verified:
                confirmed_not_met = True
            else:
                _add(review, "knockout_not_met_unverified")
            continue
        if not verified:
            _add(review, "knockout_citation_unverified")
        if match.status == "unclear":
            _add(review, "knockout_unclear")
        elif match.status == "not_met":
            if match.mitigation in CLOSABLE_MITIGATIONS:
                ceiling = "bid_conditional"
            else:  # ask_in_qa: the outcome depends on the buyer's answer
                _add(review, "knockout_awaiting_qa")

    if confirmed_not_met:
        return RecommendationResult("no_bid", ("knockout_not_met",))
    if review:
        return RecommendationResult("needs_review", tuple(review))

    reasons = ["knockout_gap_closable"] if ceiling == "bid_conditional" else []
    low_band, high_band = fit_band(fit.provisional_min), fit_band(fit.provisional_max)
    at_min, at_max = _worse(ceiling, low_band), _worse(ceiling, high_band)
    if at_min == at_max:  # every score in the range gives the same result
        reasons.append(f"fit_{low_band}" if low_band == high_band else "fit_range_50_or_above")
        return RecommendationResult(at_min, tuple(reasons))

    if any(d.score is None and d.scored_by == "human" for d in fit.dimensions):
        reasons.append("awaiting_human_scores")
    if any(d.score is None and d.scored_by == "copilot" for d in fit.dimensions):
        reasons.append("fit_score_incomplete")
    if not {"awaiting_human_scores", "fit_score_incomplete"} & set(reasons):
        reasons.append("fit_score_incomplete")
    return RecommendationResult("needs_review", tuple(reasons))


def recommend(
    *,
    knockouts: Sequence[KnockoutCriterion],
    matches: Sequence[RequirementMatch],
    dimensions: Iterable[FitDimension],
    documents_missing: bool,
    pages: PageLookup,
    redactor: Redactor,
) -> CheckedRecommendation:
    """Check every citation against its stored page, then score the fit and decide.

    This is the entry point for pipeline code. The `verified` flags that the rules read are
    set here, from the page text, whatever the inputs held before. `redactor` is the one used
    for the model calls that produced the quotes.
    """
    checked_knockouts = tuple(verify_citations(k, pages, redactor) for k in knockouts)
    checked_matches = tuple(verify_citations(m, pages, redactor) for m in matches)
    fit = compute_fit(verify_citations(d, pages, redactor) for d in dimensions)
    result = decide_recommendation(
        knockouts=checked_knockouts,
        matches=checked_matches,
        fit=fit,
        documents_missing=documents_missing,
    )
    return CheckedRecommendation(checked_knockouts, checked_matches, fit, result)
