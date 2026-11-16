"""Tests for the metric formulas in evals/metrics.py and evals/report.py (docs/EVALS.md 4)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from evals import metrics
from evals.metrics import (
    ExtractionFacts,
    LotFacts,
    Outcome,
    RankedChunk,
    Requirement,
    ScreeningRow,
)
from evals.report import calls_summary, cost_summary, latency_summary
from llm.log import LlmCallRecord

CEST = timezone(timedelta(hours=2))
CET = timezone(timedelta(hours=1))

# --- 1, 2: screening and the Wilson interval -----------------------------------


def row(actual, passed=True, decision=None, confidence=None, error=False) -> ScreeningRow:
    return ScreeningRow(actual, passed, decision, confidence, error)


def test_precision_recall_and_f2_from_known_counts():
    rows = [
        row(True, decision="shortlist", confidence=0.9),  # TP
        row(True, decision="unsure", confidence=0.4),  # TP: unsure is shortlisted (Q13)
        row(False, decision="shortlist", confidence=0.6),  # FP
        row(True, decision="reject", confidence=0.1),  # FN, lost in the model
        row(True, passed=False),  # FN, lost in the hard filter
        row(False, decision="reject", confidence=0.2),  # TN
        row(False, passed=False),  # TN
    ]
    result = metrics.screening_metrics(rows)
    assert (result["tp"], result["fp"], result["fn"], result["tn"]) == (2, 1, 2, 2)
    assert result["precision"]["value"] == pytest.approx(2 / 3)
    assert result["recall"]["value"] == pytest.approx(2 / 4)
    # F2 = 5PR / (4P + R)
    assert result["f2"] == pytest.approx(5 * (2 / 3) * 0.5 / (4 * (2 / 3) + 0.5))
    assert (result["lost_in_filter"], result["lost_in_model"]) == (1, 1)


def test_a_passed_filter_with_a_reject_is_negative_and_a_failed_filter_is_always_negative():
    assert metrics.screening_positive(row(True, decision="unsure"))
    assert not metrics.screening_positive(row(True, decision="reject"))
    assert not metrics.screening_positive(row(True, passed=False, decision="shortlist"))
    assert not metrics.screening_positive(row(True, decision=None, error=True))


def test_zero_denominators_give_null():
    result = metrics.screening_metrics([row(False, decision="reject")])
    assert result["precision"]["value"] is None
    assert result["recall"]["value"] is None
    assert result["f2"] is None
    assert metrics.ratio(1, 0) is None
    assert metrics.proportion(0, 0) == {"value": None, "n": 0, "ci95": None}


def test_a_failed_call_is_a_predicted_negative_and_is_counted():
    result = metrics.screening_metrics([row(True, decision=None, error=True)])
    assert (result["fn"], result["errors"]) == (1, 1)


def test_recall_at_a_fixed_precision_uses_the_confidence_thresholds():
    rows = [
        row(True, decision="shortlist", confidence=0.9),
        row(True, decision="unsure", confidence=0.4),
        row(False, decision="shortlist", confidence=0.6),
    ]
    result = metrics.screening_metrics(rows, min_precision=0.9)
    by_tau = {point["threshold"]: point for point in result["by_confidence"]}
    assert by_tau[0.5]["precision"] == pytest.approx(0.5)  # 0.9 and 0.6 are in
    assert by_tau[0.7]["precision"] == pytest.approx(1.0)  # only 0.9 is in
    assert by_tau[0.7]["recall"] == pytest.approx(0.5)
    assert result["recall_at_precision"] == {"min_precision": 0.9, "recall": pytest.approx(0.5)}
    assert "recall_at_precision" not in metrics.screening_metrics(rows)


def test_wilson_interval_against_hand_computed_values():
    # 8 of 10, z = 1.96: centre 0.992080, margin 0.312278, scale 1.384160.
    low, high = metrics.wilson(8, 10)
    assert low == pytest.approx(0.49016, abs=1e-4)
    assert high == pytest.approx(0.94335, abs=1e-4)
    assert metrics.wilson(0, 5)[0] == 0.0
    assert metrics.wilson(5, 5)[1] == 1.0
    assert metrics.wilson(0, 0) is None


# --- 3, 4: retrieval ------------------------------------------------------------


def test_hit_at_5_and_mrr_at_10():
    ranks = [1, 3, 7, None]
    assert metrics.hit_at_k(ranks)["value"] == pytest.approx(2 / 4)
    assert metrics.mrr_at_k(ranks) == pytest.approx((1 + 1 / 3 + 1 / 7 + 0) / 4)
    assert metrics.mrr_at_k([11]) == 0.0  # beyond rank 10 counts nothing
    assert metrics.mrr_at_k([]) is None


def test_a_chunk_is_relevant_when_its_page_range_covers_an_answer_page_of_the_same_document():
    answers = {"aaa": [14, 15]}
    assert metrics.first_relevant_rank([RankedChunk("aaa", 13, 14)], answers) == 1
    assert metrics.first_relevant_rank([RankedChunk("aaa", 16, 18)], answers) is None
    # The same page of another document is not a hit, nor is a document outside the snapshot.
    chunks = [RankedChunk("bbb", 14, 14), RankedChunk(None, 14, 14), RankedChunk("aaa", 15, 15)]
    assert metrics.first_relevant_rank(chunks, answers) == 3


# --- 5 to 9: field extraction ---------------------------------------------------


def test_deadline_tolerance_is_60_minutes_on_the_same_calendar_date():
    truth = datetime(2026, 4, 20, 12, 0, tzinfo=CEST)
    assert metrics.deadline_correct(truth + timedelta(minutes=59), truth)
    assert metrics.deadline_correct(truth - timedelta(minutes=60), truth)
    assert not metrics.deadline_correct(truth + timedelta(minutes=61), truth)
    assert not metrics.deadline_correct(None, truth)


def test_a_date_without_a_time_passes_the_date_check_and_fails_the_deadline():
    truth = datetime(2026, 4, 20, 12, 0, tzinfo=CEST)
    date_only = datetime(2026, 4, 20, 0, 0)  # what extraction v1 writes without a time
    assert metrics.same_local_date(date_only, truth)
    assert not metrics.deadline_correct(date_only, truth)
    # Close in time but on the next calendar day: wrong.
    late = datetime(2026, 4, 20, 23, 50, tzinfo=CEST)
    assert not metrics.deadline_correct(late + timedelta(minutes=20), late)


def test_deadline_is_compared_in_amsterdam_time_across_the_summer_time_change():
    truth = datetime(2026, 3, 29, 12, 0, tzinfo=CEST)  # summer time starts that morning
    assert metrics.deadline_correct(datetime(2026, 3, 29, 12, 0), truth)  # naive: Amsterdam
    assert metrics.deadline_correct(datetime(2026, 3, 29, 11, 0, tzinfo=CET), truth)
    # 12:01 winter time is 13:01 summer time: 61 minutes late.
    assert not metrics.deadline_correct(datetime(2026, 3, 29, 12, 1, tzinfo=CET), truth)


def test_amounts_are_correct_within_one_percent():
    truth = Decimal("100000")
    assert metrics.amount_correct(Decimal("101000"), truth)  # exactly 1.00%
    assert metrics.amount_correct(Decimal("99000"), truth)
    assert not metrics.amount_correct(Decimal("101010"), truth)  # 1.01%
    assert not metrics.amount_correct(None, truth)
    assert metrics.vat_explained(Decimal("121000"), truth)
    assert not metrics.vat_explained(Decimal("101000"), truth)  # correct, so not a miss
    assert not metrics.vat_explained(Decimal("130000"), truth)


def test_a_missing_prediction_is_wrong_and_missing_truth_lowers_coverage_only():
    with_value = ExtractionFacts(lots=(LotFacts("A"),), estimated_value=Decimal("100000"))
    without_value = ExtractionFacts(lots=(LotFacts("A"),))
    wrong = metrics.grade_extraction(with_value, ExtractionFacts(lots=(LotFacts("A"),)))
    assert wrong.units["estimated_value"] == [0.0]  # no predicted value
    failed = metrics.grade_extraction(with_value, None)  # the extraction itself failed
    assert failed.units["estimated_value"] == [0.0]
    assert failed.units["lot_count"] == [0.0]
    ungraded = metrics.grade_extraction(without_value, None)
    assert "estimated_value" not in ungraded.units

    per_tender = [wrong.units["estimated_value"], ungraded.units.get("estimated_value", [])]
    summary = metrics.field_summary(per_tender, tenders=2)
    assert summary["accuracy"] == 0.0  # one unit, and it is wrong
    assert summary["coverage"] == 0.5  # one of two tenders has the truth


def test_values_are_graded_per_lot_and_vat_misses_are_counted():
    truth = ExtractionFacts(
        lots=(LotFacts("Perceel 1: licenties", Decimal("150000")),
              LotFacts("Perceel 2: beheer", Decimal("250000"))),
        estimated_value=Decimal("400000"),
        framework_max_value=Decimal("500000"),
    )  # fmt: skip
    predicted = ExtractionFacts(
        lots=(
            LotFacts("Perceel 2 - beheer", Decimal("250000"), None, Decimal("300000")),
            LotFacts("Perceel 1 - licenties", Decimal("181500"), None, Decimal("200000")),
        ),
    )
    grade = metrics.grade_extraction(truth, predicted)
    assert grade.units["estimated_value"] == [0.0, 1.0]  # in the order of the truth lots
    assert grade.vat_explained == 1  # 181500 / 1.21 = 150000
    assert grade.units["framework_max_value"] == [1.0]  # 300000 + 200000
    assert grade.units["lot_titles"] == [1.0]
    assert grade.units["lot_count"] == [1.0]


def test_the_procedure_value_is_compared_with_the_sum_of_the_predicted_lots():
    truth = ExtractionFacts(lots=(LotFacts("A"), LotFacts("B")), estimated_value=Decimal("300"))
    predicted = ExtractionFacts(lots=(LotFacts("A", Decimal("100")), LotFacts("B", Decimal("200"))))
    assert metrics.grade_extraction(truth, predicted).units["estimated_value"] == [1.0]


def test_duration_is_correct_within_one_month():
    truth = ExtractionFacts(lots=(LotFacts("A", duration_months=36.0),))
    for months, expected in ((35, 1.0), (37, 1.0), (38, 0.0), (None, 0.0)):
        predicted = ExtractionFacts(lots=(LotFacts("A", duration_months=months),))
        assert metrics.grade_extraction(truth, predicted).units["duration_months"] == [expected]


def test_skipped_fields_are_not_graded():
    truth = ExtractionFacts(
        deadline=datetime(2026, 4, 20, 12, 0, tzinfo=CEST),
        lots=(LotFacts("A", duration_months=12.0),),
    )
    grade = metrics.grade_extraction(truth, None, skip=["duration_months", "deadline"])
    assert set(grade.units) == {"deadline_date_only", "lot_count"}


def test_a_truth_date_without_a_time_is_graded_on_the_date_only():
    truth = ExtractionFacts(deadline=datetime(2026, 6, 15), deadline_has_time=False)
    grade = metrics.grade_extraction(truth, ExtractionFacts(deadline=datetime(2026, 6, 15, 14)))
    assert grade.units == {"deadline_date_only": [1.0]}


def test_lot_alignment_is_one_to_one_and_needs_a_similar_title():
    truth = ["Perceel 1: Licenties", "Perceel 2: Beheer", "Perceel 3: Opleiding"]
    predicted = ["perceel 2 - beheer", "PERCEEL 1 licenties", "Iets heel anders"]
    assert metrics.align_lots(truth, predicted) == {0: 1, 1: 0}  # lot 3 has no match
    # Two truth lots cannot both take the one similar prediction.
    assert len(metrics.align_lots(["Beheer", "Beheer"], ["Beheer"])) == 1
    assert metrics.title_similarity("Perceel 1: Licenties", "perceel 1 - licenties") == 1.0
    assert metrics.title_similarity("Licenties", None) == 0.0


def test_a_single_lot_on_both_sides_is_aligned_without_a_title_check():
    assert metrics.align_lots(["Dataplatform voor de gemeente"], ["Iets anders"]) == {0: 0}
    assert metrics.align_lots(["Dataplatform"], ["Iets anders", "Nog iets"]) == {}


def test_a_field_under_40_percent_coverage_is_flagged_and_left_out_of_the_score():
    fields = {
        "deadline": metrics.field_summary([[1.0], [0.0], [1.0], [1.0]], tenders=4),
        "duration_months": metrics.field_summary([[0.0], [], [], []], tenders=4),  # 25%
        "deadline_date_only": metrics.field_summary([[0.0], [0.0], [0.0], [0.0]], tenders=4),
    }
    assert not fields["deadline"]["low_coverage"]
    assert fields["duration_months"]["low_coverage"]
    # Only `deadline` counts: the low-coverage field and the report-only field are left out.
    assert metrics.extraction_score(fields) == pytest.approx(0.75)
    assert metrics.extraction_score({}) is None


# --- 10, 11: knock-outs ---------------------------------------------------------


def req(ref, page, category="financial", kind="suitability", doc="aaa") -> Requirement:
    return Requirement(ref, doc, page, category, kind)


def test_a_knockout_matches_on_document_page_within_one_and_category():
    label = [req("L1", 22)]
    assert metrics.grade_knockouts(label, [req("P1", 23)]).matched == 1
    assert metrics.grade_knockouts(label, [req("P1", 24)]).matched == 0  # two pages away
    assert metrics.grade_knockouts(label, [req("P1", 22, doc="bbb")]).matched == 0
    assert metrics.grade_knockouts(label, [req("P1", 22, doc=None)]).matched == 0
    other = metrics.grade_knockouts(label, [req("P1", 22, category="insurance")])
    assert (other.matched, other.page_only_matched) == (0, 1)


def test_one_prediction_cannot_cover_two_labels():
    labels = [req("L1", 10), req("L2", 10)]
    one = metrics.grade_knockouts(labels, [req("P1", 10)])
    assert (one.labels, one.matched) == (2, 1)
    # The matching is maximal: P1 could take either label, P2 only L2 (page 11 is two from 9).
    labels = [req("L1", 9), req("L2", 10)]
    both = metrics.grade_knockouts(labels, [req("P1", 10), req("P2", 11)])
    assert both.matched == 2
    assert both.pairs == {"L1": "P1", "L2": "P2"}


def test_formal_requirements_are_matched_and_reported_apart():
    labels = [req("L1", 5), req("L2", 6, category="other", kind="formal")]
    # The real knock-out was extracted as a formal action: it is not checked, so not found.
    as_formal = metrics.grade_knockouts(labels, [req("P1", 5, kind="formal")])
    assert (as_formal.labels, as_formal.matched, as_formal.page_only_matched) == (1, 0, 1)
    found = metrics.grade_knockouts(labels, [req("P1", 5), req("P2", 6, "other", "formal")])
    assert (found.matched, found.formal_labels, found.formal_matched) == (1, 1, 1)
    assert found.predictions == 1  # formal predictions are not in the precision


def test_knockout_recall_micro_macro_and_all_found():
    tenders = [
        metrics.grade_knockouts([req("A1", 1), req("A2", 5), req("A3", 9)], [req("P", 1)]),
        metrics.grade_knockouts([req("B1", 1)], [req("P", 1), req("Q", 7)]),
    ]
    result = metrics.knockout_metrics(tenders)
    assert result["recall"]["value"] == pytest.approx(2 / 4)  # over requirements
    assert result["recall_macro"] == pytest.approx((1 / 3 + 1) / 2)  # mean over tenders
    assert result["tenders_all_found"]["value"] == pytest.approx(1 / 2)
    assert result["precision"]["value"] == pytest.approx(2 / 3)
    assert result["recall_formal"]["value"] is None  # no formal labels


def test_match_accuracy_and_the_false_met_rate():
    pairs = [("met", "met"), ("met", "not_met"), ("not_met", "not_met"), (None, "unclear")]
    result = metrics.match_accuracy(pairs)
    assert result["match_accuracy"]["value"] == pytest.approx(2 / 4)
    assert result["false_met_rate"]["value"] == pytest.approx(1 / 2)
    assert metrics.match_accuracy([])["false_met_rate"]["value"] is None


# --- 12, 13: latency and cost ---------------------------------------------------


def test_percentiles_use_the_nearest_rank():
    assert metrics.percentile([7.0], 0.50) == 7.0
    assert metrics.percentile([7.0], 0.95) == 7.0
    twenty = [float(n) for n in range(20, 0, -1)]  # unsorted on purpose
    assert metrics.percentile(twenty, 0.50) == 10.0  # ceil(0.50 x 20) = 10th
    assert metrics.percentile(twenty, 0.95) == 19.0  # ceil(0.95 x 20) = 19th
    hundred = [float(n) for n in range(1, 101)]
    assert (metrics.percentile(hundred, 0.50), metrics.percentile(hundred, 0.95)) == (50.0, 95.0)
    assert metrics.percentile([], 0.5) is None


def record(
    stage: str, tender_id: int | None, cost: str, latency: int = 100, **extra
) -> LlmCallRecord:
    return LlmCallRecord(
        run_id=uuid4(), stage=stage, provider="anthropic", model="m", prompt_name="p",
        prompt_version="v1", status=extra.pop("status", "ok"), tender_id=tender_id,
        cost_usd=Decimal(cost), latency_ms=latency, **extra,
    )  # fmt: skip


def test_cost_per_100_screened_and_per_memo():
    records = [
        record("screening", 1, "0.001"),
        record("screening", 2, "0.002", status="validation_failed"),  # a failed call is billed
        record("extraction_map", 1, "0.10"),
        record("extraction_reduce", 1, "0.05"),
        record("matching", 1, "0.20"),
        record("memo", 1, "0.03"),
        record("extraction_map", 2, "0.40"),  # tender 2 failed after this: no memo
        record("matching", 3, "9.00"),  # not a memo-suite tender
        record("chat", 1, "0.50"),  # not a memo stage
        record("judge", 1, "0.07"),  # eval overhead, in neither figure
    ]
    cost = cost_summary(records, screened=2, memo_tenders={1, 2}, memos_produced=1)
    assert cost["per_100_screened_usd"] == "0.150000"  # 100 x 0.003 / 2
    # Tender 2's cost stays in the numerator; only the one memo is in the denominator.
    assert cost["per_memo_usd"] == "0.780000"
    assert cost["memos_failed"] == 1
    assert cost["eval_overhead_usd"] == "0.070000"
    assert cost["total_usd"] == "10.353000"
    assert cost["by_stage"]["screening"] == "0.003000"


def test_cost_figures_are_null_without_a_denominator():
    cost = cost_summary([], screened=0, memo_tenders=set(), memos_produced=0)
    assert cost["per_100_screened_usd"] is None
    assert cost["per_memo_usd"] is None
    assert cost["total_usd"] == "0.000000"


def test_latency_per_call_and_per_tender_and_the_call_counts():
    records = [
        record("matching", 1, "0", 100),
        record("matching", 1, "0", 300, validation_retries=1),
        record("matching", 2, "0", 200, network_retries=2, status="provider_error"),
    ]
    latency = latency_summary(records)["matching"]
    assert latency["per_call"] == {"n": 3, "p50": 200, "p95": 300}
    assert latency["per_tender"] == {"n": 2, "p50": 200, "p95": 400}  # 400 for tender 1
    calls = calls_summary(records)
    assert calls == {
        "total": 3,
        "by_status": {"ok": 2, "provider_error": 1},
        "validation_retries": 1,
        "network_retries": 2,
    }


# --- 14, 15: composite and gate -------------------------------------------------

WEIGHTS = {
    "knockout": 0.30, "extraction": 0.20, "screening": 0.20,
    "retrieval": 0.10, "memo": 0.10, "edge_cases": 0.10,
}  # fmt: skip


def test_the_composite_is_the_weighted_sum_of_the_six_scores():
    scores = {
        "knockout": 0.8, "extraction": 0.5, "screening": 0.6,
        "retrieval": 0.9, "memo": 0.7, "edge_cases": 1.0,
    }  # fmt: skip
    result = metrics.composite(scores, WEIGHTS)
    expected = 0.3 * 0.8 + 0.2 * 0.5 + 0.2 * 0.6 + 0.1 * 0.9 + 0.1 * 0.7 + 0.1 * 1.0
    assert result["value"] == pytest.approx(expected)
    assert result["partial"] == pytest.approx(expected)
    assert set(result["suites_included"]) == set(WEIGHTS)


def test_the_composite_is_null_when_a_suite_is_missing_and_the_partial_names_its_suites():
    result = metrics.composite({"knockout": 0.8, "screening": 0.6, "memo": None}, WEIGHTS)
    assert result["value"] is None
    assert result["partial"] == pytest.approx((0.3 * 0.8 + 0.2 * 0.6) / 0.5)
    assert result["suites_included"] == ["knockout", "screening"]
    assert metrics.composite({}, WEIGHTS) == {"value": None, "partial": None, "suites_included": []}


def test_the_knockout_recall_gate():
    assert metrics.gate(0.90, 0.90, evaluated=True) == "pass"
    assert metrics.gate(0.89, 0.90, evaluated=True) == "fail"
    assert metrics.gate(0.99, None, evaluated=True) == "not_set"
    assert metrics.gate(None, 0.90, evaluated=False) == "not_evaluated"
    assert metrics.gate(0.99, 0.90, evaluated=False) == "not_evaluated"


# --- 16: edge cases, and the memo slot ------------------------------------------

OUTCOME = Outcome(
    filter_passed=False,
    filter_failed_rules=("deadline",),
    filter_flags=("unknown:value", "consortium_only"),
    screened=False,
    memo_created=True,
    recommendation="needs_review",
    reason_codes=("documents_missing",),
    limitations=("documents not accessible",),
    extraction_warnings=("document 5 is scanned",),
    chat_in_scope=False,
    model_text=("The tender is about a data platform.",),
    model_calls=2,
)


@pytest.mark.parametrize(
    ("key", "holds", "fails"),
    [
        ("filter_passed", False, True),
        ("filter_failed_rule", "deadline", "value"),
        ("filter_flags_include", ["consortium_only"], ["nature_supplies"]),
        ("screened", False, True),
        ("memo_created", True, False),
        ("recommendation", "needs_review", "bid"),
        ("reason_codes_include", ["documents_missing"], ["knockout_not_met"]),
        ("limitations_include", ["documents not accessible"], ["2 citations unverified"]),
        ("extraction_warnings_include", ["scanned"], ["token budget"]),
        ("chat_in_scope", False, True),
        ("output_excludes", ["HACKED"], ["DATA PLATFORM"]),  # compared without case
        ("max_model_calls", 2, 1),
    ],
)
def test_each_expect_key(key, holds, fails):
    assert metrics.check_expectations({key: holds}, OUTCOME).status == "pass"
    verdict = metrics.check_expectations({key: fails}, OUTCOME)
    assert (verdict.status, verdict.failed) == ("fail", (key,))


def test_every_expect_key_is_covered_above():
    covered = {case[0] for case in test_each_expect_key.pytestmark[0].args[1]}
    assert covered == metrics.EXPECT_KEYS


def test_a_case_passes_only_when_every_key_holds():
    expect = {"filter_passed": False, "memo_created": False}
    assert metrics.check_expectations(expect, OUTCOME) == metrics.Verdict("fail", ("memo_created",))


def test_an_unknown_key_or_a_wrong_value_type_is_an_invalid_label_not_a_failure():
    unknown = metrics.check_expectations({"filter_passed": False, "mood": "good"}, OUTCOME)
    assert (unknown.status, unknown.failed) == ("invalid_label", ("mood",))
    assert metrics.check_expectations({"filter_passed": "no"}, OUTCOME).status == "invalid_label"
    assert metrics.check_expectations({"max_model_calls": True}, OUTCOME).status == "invalid_label"
    assert metrics.check_expectations({}, OUTCOME).status == "invalid_label"


def test_memo_score_scales_each_criterion_to_0_1_and_weights_it():
    criteria = [metrics.Criterion("complete", 5, 3), metrics.Criterion("clear", 5, 1)]
    # (3 x (4-1)/4 + 1 x (5-1)/4) / 4
    assert metrics.memo_score({"complete": 4, "clear": 5}, criteria) == pytest.approx(0.8125)
    assert metrics.memo_score({"complete": 1, "clear": 1}, criteria) == 0.0
    with pytest.raises(ValueError, match="complete"):
        metrics.memo_score({"clear": 5}, criteria)
    with pytest.raises(ValueError, match="clear"):
        metrics.memo_score({"complete": 4, "clear": 6}, criteria)


def test_judge_agreement_with_hand_grades():
    result = metrics.agreement([(4, 4), (5, 4), (2, 5)])
    assert result["exact_agreement"] == pytest.approx(1 / 3)
    assert result["within_one"] == pytest.approx(2 / 3)
    assert result["mean_abs_diff"] == pytest.approx(4 / 3)
    assert metrics.agreement([])["exact_agreement"] is None
