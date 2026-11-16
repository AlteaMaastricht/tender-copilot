"""Fit score and recommendation rules (ARCHITECTURE.md 5.4, MEMO_TEMPLATE.md section 3)."""

from __future__ import annotations

import pytest

from pipeline.memo import FIT_WEIGHTS, compute_fit, decide_recommendation, fit_band
from pipeline.schemas import (
    Citation,
    FitDimension,
    FitScore,
    KbEvidence,
    KnockoutCriterion,
    RequirementMatch,
)

COPILOT = [name for name, (_, by) in FIT_WEIGHTS.items() if by == "copilot"]
HUMAN = [name for name, (_, by) in FIT_WEIGHTS.items() if by == "human"]
KB = KbEvidence(kb_id=1, doc_slug="reference-waterschap", quote="Dataplatform, 2024")


def cit(verified: bool | None = True) -> Citation:
    return Citation(
        document_id=1, page=2, quote="De inschrijver voldoet aan de eis.", verified=verified
    )


def dim(name: str, score: int | None, *, citation: Citation | None = None) -> FitDimension:
    weight, scored_by = FIT_WEIGHTS[name]
    if scored_by == "human" or score is None:
        return FitDimension(name=name, weight_pct=weight, scored_by=scored_by, score=score)
    return FitDimension(
        name=name,
        weight_pct=weight,
        scored_by=scored_by,
        score=score,
        rationale="Matches the company profile.",
        citation=citation,
        kb_evidence=[] if citation else [KB],
    )


def fit(copilot: int | None = None, human: int | None = None, **single: int | None) -> FitScore:
    """All copilot dimensions at `copilot`, all human at `human`; keywords override one."""
    scores = dict.fromkeys(COPILOT, copilot) | dict.fromkeys(HUMAN, human) | single
    return compute_fit(dim(name, score) for name, score in scores.items())


def ko(n: int = 1, *, verified: bool | None = True) -> KnockoutCriterion:
    return KnockoutCriterion(
        requirement_id=f"KO-{n:02d}",
        category="certification",
        kind="suitability",
        text="Valid ISO 27001 certificate.",
        threshold=None,
        proof=None,
        proof_when=None,
        consortium_allowed=None,
        citation=cit(verified),
    )


def match(
    n: int = 1, status: str = "met", mitigation: str = "none", *, verified: bool | None = True
) -> RequirementMatch:
    return RequirementMatch(
        requirement_id=f"KO-{n:02d}",
        status=status,
        reasoning="Checked against the knowledge base.",
        tender_citation=cit(verified),
        company_evidence=[KB] if status == "met" else [],
        gap=None if status == "met" else "No certificate on file.",
        mitigation=mitigation,
        unclear_reason="no_evidence_found" if status == "unclear" else None,
    )


def decide(knockouts=None, matches=None, score: FitScore | None = None, missing: bool = False):
    result = decide_recommendation(
        knockouts=[ko()] if knockouts is None else knockouts,
        matches=[match()] if matches is None else matches,
        fit=score or fit(5, 5),
        documents_missing=missing,
    )
    return result.recommendation, result.reasons


# --- fit score -----------------------------------------------------------------


def test_weights_are_the_template_weights():
    assert {name: weight for name, (weight, _) in FIT_WEIGHTS.items()} == {
        "scope_match": 25,
        "win_probability": 20,
        "references": 15,
        "commercial": 15,
        "capacity_timing": 10,
        "contract_risk": 10,
        "strategic_value": 5,
    }
    assert sum(weight for weight, by in FIT_WEIGHTS.values() if by == "copilot") == 60


@pytest.mark.parametrize(("score", "total"), [(5, 100), (1, 20), (3, 60)])
def test_all_dimensions_scored_gives_a_total(score, total):
    result = fit(score, score)
    assert (result.total, result.provisional_min, result.provisional_max) == (total,) * 3


def test_total_is_weighted():
    # 25*5 + 15*4 + 10*3 + 10*2 + 20*1 + 15*2 + 5*3 = 300; 300 / 5 = 60
    result = fit(
        scope_match=5, references=4, capacity_timing=3, contract_risk=2,
        win_probability=1, commercial=2, strategic_value=3,
    )  # fmt: skip
    assert result.total == 60


def test_nothing_scored_gives_the_full_range():
    result = compute_fit([])
    assert (result.total, result.provisional_min, result.provisional_max) == (None, 20, 100)
    assert [d.name for d in result.dimensions] == list(FIT_WEIGHTS)


def test_unscored_human_dimensions_give_a_32_point_range():
    result = fit(copilot=5)
    assert (result.total, result.provisional_min, result.provisional_max) == (None, 68, 100)


def test_one_human_score_narrows_the_range():
    result = fit(copilot=5, win_probability=3)
    assert (result.provisional_min, result.provisional_max) == (76, 92)


def test_unscored_copilot_dimension_widens_the_range():
    result = fit(copilot=4, human=4, scope_match=None)
    # 80 with scope at 4; scope weight 25 gives 5 points per step: 65 at 1, 85 at 5
    assert (result.total, result.provisional_min, result.provisional_max) == (None, 65, 85)


def test_copilot_score_with_only_an_unverified_citation_is_dropped():
    for verified in (False, None):
        dims = [dim(name, 4) for name in FIT_WEIGHTS if name != "scope_match"]
        dims.append(dim("scope_match", 4, citation=cit(verified)))
        result = compute_fit(dims)
        scope = next(d for d in result.dimensions if d.name == "scope_match")
        assert scope.score is None and result.total is None

    dims = [dim(name, 4) for name in FIT_WEIGHTS if name != "scope_match"]
    dims.append(dim("scope_match", 4, citation=cit(True)))
    assert compute_fit(dims).total == 80


def test_weights_and_owner_come_from_code_not_from_the_input():
    forged = FitDimension(name="strategic_value", weight_pct=100, scored_by="human", score=5)
    result = compute_fit([forged])
    strategic = next(d for d in result.dimensions if d.name == "strategic_value")
    assert strategic.weight_pct == 5 and result.provisional_min == 24


def test_duplicate_dimension_is_an_error():
    with pytest.raises(ValueError, match="twice"):
        compute_fit([dim("commercial", 3), dim("commercial", 4)])


@pytest.mark.parametrize(
    ("score", "band"),
    [(100, "bid"), (70, "bid"), (69, "bid_conditional"), (50, "bid_conditional"), (49, "no_bid")],
)
def test_bands(score, band):
    assert fit_band(score) == band


# --- recommendation: fit score with no knock-out problem -----------------------


def test_band_boundaries_with_all_dimensions_scored():
    # 70: all 3 (60) plus scope_match at 5 (+10)
    assert decide(score=fit(3, 3, scope_match=5)) == ("bid", ("fit_bid",))
    # 69: 70 minus one step of strategic_value (weight 5, 1 point)
    at_69 = fit(3, 3, scope_match=5, strategic_value=2)
    assert at_69.total == 69
    assert decide(score=at_69) == ("bid_conditional", ("fit_bid_conditional",))
    # 50: all 3 (60) minus two steps of scope_match (-10)
    at_50 = fit(3, 3, scope_match=1)
    assert at_50.total == 50
    assert decide(score=at_50) == ("bid_conditional", ("fit_bid_conditional",))
    at_49 = fit(3, 3, scope_match=1, strategic_value=2)
    assert at_49.total == 49
    assert decide(score=at_49) == ("no_bid", ("fit_no_bid",))


def test_all_human_scores_missing_always_needs_review():
    for copilot in range(1, 6):
        assert decide(score=fit(copilot=copilot)) == ("needs_review", ("awaiting_human_scores",))


def test_range_inside_one_band_decides_before_all_humans_scored():
    # copilot 5 (60) + win 5 (20) + commercial 5 (15) = 95; strategic_value open: 96..100
    partly = fit(copilot=5, win_probability=5, commercial=5)
    assert (partly.provisional_min, partly.provisional_max) == (96, 100)
    assert decide(score=partly) == ("bid", ("fit_bid",))


def test_recompute_after_human_scores_gives_the_band():
    assert decide(score=fit(copilot=4))[0] == "needs_review"
    assert decide(score=fit(copilot=4, human=4)) == ("bid", ("fit_bid",))
    assert decide(score=fit(copilot=4, human=1)) == ("bid_conditional", ("fit_bid_conditional",))


def test_unscored_copilot_dimension_gives_fit_score_incomplete():
    # range 65..85 crosses the 70 boundary
    result = decide(score=fit(copilot=4, human=4, scope_match=None))
    assert result == ("needs_review", ("fit_score_incomplete",))
    both = decide(score=fit(copilot=4, scope_match=None))
    assert both == ("needs_review", ("awaiting_human_scores", "fit_score_incomplete"))


def test_unscored_copilot_dimension_inside_one_band_still_decides():
    # contract_risk open (weight 10): 90..98 with everything else at 5
    result = decide(score=fit(copilot=5, human=5, contract_risk=None))
    assert result == ("bid", ("fit_bid",))


# --- recommendation: knock-out rules -------------------------------------------


def test_documents_missing_needs_review_before_anything_else():
    result = decide(matches=[match(status="not_met")], missing=True)
    assert result == ("needs_review", ("documents_missing",))


def test_no_knockouts_found_needs_review():
    assert decide(knockouts=[], matches=[]) == ("needs_review", ("no_knockouts_found",))


def test_not_met_without_mitigation_is_no_bid_whatever_the_fit():
    assert decide(matches=[match(status="not_met")]) == ("no_bid", ("knockout_not_met",))


def test_no_bid_wins_over_unclear_and_unverified_elsewhere():
    knockouts = [ko(1), ko(2), ko(3, verified=False), ko(4)]
    matches = [match(1, "not_met"), match(2, "unclear"), match(3)]  # KO-04 has no match
    assert decide(knockouts, matches) == ("no_bid", ("knockout_not_met",))


@pytest.mark.parametrize(
    ("ko_verified", "match_verified"), [(False, True), (True, False), (None, True), (True, None)]
)
def test_not_met_with_its_own_citation_unverified_needs_review(ko_verified, match_verified):
    result = decide([ko(verified=ko_verified)], [match(status="not_met", verified=match_verified)])
    assert result == ("needs_review", ("knockout_not_met_unverified",))


def test_one_verified_not_met_is_enough_for_no_bid():
    knockouts = [ko(1, verified=False), ko(2)]
    matches = [match(1, "not_met"), match(2, "not_met")]
    assert decide(knockouts, matches) == ("no_bid", ("knockout_not_met",))


def test_unclear_needs_review():
    assert decide(matches=[match(status="unclear")]) == ("needs_review", ("knockout_unclear",))


def test_unverified_citation_on_a_met_knockout_needs_review():
    for knockouts, matches in (([ko(verified=False)], [match()]), ([ko()], [match(verified=None)])):
        assert decide(knockouts, matches) == ("needs_review", ("knockout_citation_unverified",))


def test_knockout_without_a_match_needs_review():
    assert decide([ko(1), ko(2)], [match(1)]) == ("needs_review", ("knockout_unmatched",))


def test_match_without_a_knockout_needs_review():
    assert decide([ko(1)], [match(1), match(2)]) == ("needs_review", ("match_without_knockout",))


def test_duplicate_requirement_id_needs_review():
    assert decide([ko(1), ko(1)], [match(1)]) == ("needs_review", ("duplicate_requirement_id",))


def test_not_met_waiting_for_the_buyers_answer_needs_review():
    result = decide(matches=[match(status="not_met", mitigation="ask_in_qa")])
    assert result == ("needs_review", ("knockout_awaiting_qa",))


def test_several_review_reasons_are_all_reported_once():
    knockouts = [ko(1), ko(2), ko(3)]
    matches = [match(1, "unclear"), match(2, "unclear"), match(3, "not_met", "ask_in_qa")]
    assert decide(knockouts, matches) == (
        "needs_review",
        ("knockout_unclear", "knockout_awaiting_qa"),
    )


# --- recommendation: a knock-out for some lots only (interim, Q8 open) ----------


def lot_ko(n: int = 1, *, verified: bool | None = True) -> KnockoutCriterion:
    return ko(n, verified=verified).model_copy(update={"applies_to_lots": ["LOT-0002"]})


def test_not_met_for_named_lots_needs_review_instead_of_no_bid():
    result = decide([lot_ko()], [match(status="not_met")])
    assert result == ("needs_review", ("lot_specific_knockout",))
    # The same answer for a requirement on every lot is still a no-bid.
    assert decide([ko()], [match(status="not_met")]) == ("no_bid", ("knockout_not_met",))


@pytest.mark.parametrize(
    "mitigation",
    ["consortium", "subcontractor", "third_party", "obtain_before_deadline", "ask_in_qa"],
)
def test_not_met_for_named_lots_needs_review_whatever_the_mitigation(mitigation):
    result = decide([lot_ko()], [match(status="not_met", mitigation=mitigation)])
    assert result == ("needs_review", ("lot_specific_knockout",))


def test_a_lot_specific_knockout_that_is_met_or_unclear_follows_the_normal_rules():
    assert decide([lot_ko()], [match()]) == ("bid", ("fit_bid",))
    assert decide([lot_ko()], [match(status="unclear")]) == ("needs_review", ("knockout_unclear",))


def test_a_tender_wide_no_bid_still_wins_over_a_lot_specific_knockout():
    knockouts = [lot_ko(1), ko(2)]
    matches = [match(1, "not_met"), match(2, "not_met")]
    assert decide(knockouts, matches) == ("no_bid", ("knockout_not_met",))


def test_an_unverified_lot_specific_knockout_reports_both_reasons():
    result = decide([lot_ko(verified=False)], [match(status="not_met")])
    assert result == ("needs_review", ("lot_specific_knockout", "knockout_citation_unverified"))


def test_lot_specific_and_other_review_reasons_are_listed_together():
    knockouts = [lot_ko(1), ko(2)]
    matches = [match(1, "not_met"), match(2, "unclear")]
    assert decide(knockouts, matches) == (
        "needs_review",
        ("lot_specific_knockout", "knockout_unclear"),
    )


# --- recommendation: closable gap as a ceiling ---------------------------------

CLOSABLE = ["consortium", "subcontractor", "third_party", "obtain_before_deadline"]


@pytest.mark.parametrize("mitigation", CLOSABLE)
def test_closable_gap_caps_a_bid_at_bid_conditional(mitigation):
    result = decide(matches=[match(status="not_met", mitigation=mitigation)], score=fit(5, 5))
    assert result == ("bid_conditional", ("knockout_gap_closable", "fit_bid"))


def test_closable_gap_with_fit_below_50_is_no_bid():
    result = decide(matches=[match(status="not_met", mitigation="consortium")], score=fit(2, 2))
    assert result == ("no_bid", ("knockout_gap_closable", "fit_no_bid"))


def test_closable_gap_decides_without_human_scores_when_range_is_50_or_above():
    # copilot 4 gives 48 + 8..40 = 56..88: bid or bid_conditional, both capped
    result = decide(matches=[match(status="not_met", mitigation="third_party")], score=fit(4))
    assert result == ("bid_conditional", ("knockout_gap_closable", "fit_range_50_or_above"))


def test_closable_gap_with_range_across_50_needs_review():
    # copilot 3 gives 36 + 8..40 = 44..76
    result = decide(matches=[match(status="not_met", mitigation="subcontractor")], score=fit(3))
    assert result == ("needs_review", ("knockout_gap_closable", "awaiting_human_scores"))


def test_closable_gap_does_not_hide_an_unclear_knockout():
    matches = [match(1, "not_met", "consortium"), match(2, "unclear")]
    assert decide([ko(1), ko(2)], matches) == ("needs_review", ("knockout_unclear",))


def test_all_met_and_verified_follows_the_fit_band():
    knockouts, matches = [ko(1), ko(2)], [match(1), match(2)]
    assert decide(knockouts, matches, fit(5, 5)) == ("bid", ("fit_bid",))
    assert decide(knockouts, matches, fit(3, 3)) == ("bid_conditional", ("fit_bid_conditional",))
    assert decide(knockouts, matches, fit(2, 2)) == ("no_bid", ("fit_no_bid",))


# --- formal knock-outs ---------------------------------------------------------


def formal(n: int, *, verified: bool | None = True) -> KnockoutCriterion:
    return ko(n, verified=verified).model_copy(update={"kind": "formal"})


def test_a_formal_knockout_without_a_match_is_ignored():
    assert decide([ko(1), formal(2)], [match(1)]) == ("bid", ("fit_bid",))


def test_a_formal_knockout_with_an_unverified_citation_is_ignored():
    assert decide([ko(1), formal(2, verified=None)], [match(1)]) == ("bid", ("fit_bid",))


def test_only_formal_knockouts_count_as_none_found():
    assert decide([formal(1), formal(2)], []) == ("needs_review", ("no_knockouts_found",))


def test_a_match_for_a_formal_knockout_has_no_knockout_to_belong_to():
    result = decide([ko(1), formal(2)], [match(1), match(2)])
    assert result == ("needs_review", ("match_without_knockout",))


def test_a_formal_knockout_cannot_hide_a_confirmed_gap():
    matches = [match(1, "not_met")]
    assert decide([ko(1), formal(2)], matches) == ("no_bid", ("knockout_not_met",))
