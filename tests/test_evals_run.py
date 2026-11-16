"""End-to-end tests of evals/run.py on the synthetic mini snapshot, with the fake provider.

The expected numbers are worked out by hand from the canned answers in tests/evals_stubs.py.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from evals.run import (
    MEMO_DIR,
    RESULTS_DIR,
    SNAPSHOT_DIR,
    WEIGHTS_PATH,
    load_weights,
    main,
    run_evals,
)
from evals.suites import SUITE_ORDER
from evals.targets import WAITING_FOR, Targets, default_targets
from tests.evals_stubs import (
    COMMIT,
    CONTACT_CANARY,
    EMAIL_CANARY,
    LATENCY_MS,
    PAGE_CANARY,
    QUOTE,
    STUBS,
    ack,
    copy_datasets,
    make_config,
    make_prompts,
    run,
)
from tests.fakes import HAIKU, SONNET, FakeProvider, http_error

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def full(tmp_path_factory):
    """One full run of all six suites, shared by the tests that only read it."""
    return run(tmp_path_factory.mktemp("full"))


# --- 31: the results file -------------------------------------------------------


def test_the_results_file_is_named_by_date_and_commit(full):
    assert full.code == 0
    assert full.path.name == f"2026-11-15_{COMMIT}.json"
    document = full.document()
    assert (document["schema_version"], document["date"], document["git_commit"]) == (
        1, "2026-11-15", COMMIT,
    )  # fmt: skip
    assert len(document["runs"]) == 1


def test_the_run_records_models_providers_prompts_snapshot_and_labels(full):
    entry = full.last
    params = entry["params"]
    assert (params["stage1_model"], params["stage1_provider"]) == (HAIKU, "anthropic")
    assert (params["stage2_model"], params["stage2_provider"]) == (SONNET, "anthropic")
    assert params["suites"] == list(SUITE_ORDER)
    assert entry["prompt_versions"] == {
        "chat": "v1", "extract_map": "v1", "extract_reduce": "v1", "match": "v1",
        "memo": "v1", "memo_reason": "v1", "screen": "v1",
    }  # fmt: skip
    assert re.fullmatch(r"[0-9a-f]{64}", entry["snapshot"]["sha256"])
    assert entry["labels"]["knockout.jsonl"]["rows"] == 6
    assert (entry["completed"], entry["stop_reason"], entry["git_dirty"]) == (True, None, False)
    assert entry["started_at"] == "2026-11-15T10:00:00"


def test_screening_scores(full):
    # 9001 shortlist (relevant): TP. 9002 reject (not relevant): TN. 9003 relevant but
    # dropped by the hard filter: FN. 9004 unsure (not relevant): FP.
    suite = full.suite("screening")
    values = suite["metrics"]
    assert (suite["status"], suite["n"]) == ("ok", 4)
    assert (values["tp"], values["fp"], values["fn"], values["tn"]) == (1, 1, 1, 1)
    assert values["precision"]["value"] == 0.5
    assert values["recall"]["value"] == 0.5
    assert values["f2"] == 0.5
    assert (values["lost_in_filter"], values["lost_in_model"], values["errors"]) == (1, 0, 0)


def test_retrieval_scores(full):
    values = full.suite("retrieval")["metrics"]
    # Hybrid ranks 1, 3, 7 and none.
    assert values["hybrid"]["hit_at_5"]["value"] == 0.5
    assert values["hybrid"]["mrr_at_10"] == pytest.approx((1 + 1 / 3 + 1 / 7) / 4, abs=1e-6)
    assert values["hybrid"]["by_language"]["nl"]["hit_at_5"]["value"] == pytest.approx(
        2 / 3, abs=1e-6
    )
    assert values["hybrid"]["by_language"]["en"]["hit_at_5"]["value"] == 0.0
    # Vector only: ranks 2, none, 1 and none.
    assert values["vector_only"]["mrr_at_10"] == pytest.approx(0.375)
    assert values["hybrid"]["latency_ms"]["n"] == 4


def test_extraction_scores(full):
    values = full.suite("extraction")["metrics"]
    fields = values["fields"]
    # deadline: 9001 right, 9002 a day late; 9004's truth has no time.
    assert fields["deadline"]["accuracy"] == 0.5
    assert fields["deadline"]["coverage"] == pytest.approx(2 / 3, abs=1e-6)
    assert fields["deadline_date_only"]["accuracy"] == pytest.approx(1 / 3, abs=1e-6)
    # lot_count: right for 9001 and 9002; 9004's extraction failed.
    assert fields["lot_count"]["accuracy"] == pytest.approx(2 / 3, abs=1e-6)
    # estimated_value: 9001 right; 9002 lot 1 includes VAT, lot 2 right.
    assert fields["estimated_value"]["accuracy"] == pytest.approx(2 / 3, abs=1e-6)
    assert values["vat_explained"] == 1
    # One tender in three has these: under 40% coverage, so out of the score.
    for name in ("lot_titles", "framework_max_value", "duration_months"):
        assert fields[name]["accuracy"] == 1.0 and fields[name]["low_coverage"]
    assert values["extraction_score"] == pytest.approx((0.5 + 2 / 3 + 2 / 3) / 3, abs=1e-6)
    assert values["errors"] == 1
    assert "award_weights" in values["fields_without_ground_truth"]
    assert values["notice_deadline_disagreement"]["value"] == 0.0


def test_knockout_scores(full):
    suite = full.suite("knockout")
    values = suite["metrics"]
    assert (suite["status"], suite["n"]) == ("ok", 2)
    # 9001: 2 of 3 found (the certificate was extracted under another category).
    # 9002: 1 of 2 found (the exclusion ground was not extracted).
    assert values["recall"]["value"] == 0.6
    assert values["recall_macro"] == pytest.approx((2 / 3 + 1 / 2) / 2, abs=1e-6)
    assert values["tenders_all_found"]["value"] == 0.0
    assert values["recall_page_only"]["value"] == 0.8
    assert values["recall_formal"]["value"] == 1.0
    assert values["precision"]["value"] == 0.75
    # Three matched labels carry a status; the stub answers "met" where the label says not_met.
    assert values["match_accuracy"]["value"] == pytest.approx(2 / 3, abs=1e-6)
    assert values["false_met_rate"]["value"] == 1.0
    missed = {
        row["publication_id"]: row["missed"] for row in full.cases() if row["suite"] == "knockout"
    }
    assert missed == {9001: ["9001-KO-03"], 9002: ["9002-KO-02"]}


def test_edge_case_scores(full):
    values = full.suite("edge_cases")["metrics"]
    assert (values["passed"], values["failed"], values["invalid_label"]) == (3, 0, 0)
    assert values["pass_rate"]["value"] == 1.0
    statuses = {row["case"]: row["status"] for row in full.cases() if row["suite"] == "edge_cases"}
    assert statuses == {"EC-01": "pass", "EC-02": "pass", "EC-03": "pass"}


def test_cost_per_100_screened_and_per_memo(full):
    cost = full.last["cost"]
    # 3 screening calls at USD 0.0002.
    assert cost["by_stage"]["screening"] == "0.000600"
    assert (cost["screened"], cost["per_100_screened_usd"]) == (3, "0.020000")
    # Memo tenders 9001 and 9002: 2 map, 2 reduce, 4 matching and 4 memo calls at USD 0.004.
    assert (cost["memos_produced"], cost["memos_failed"]) == (2, 0)
    assert cost["per_memo_usd"] == "0.024000"
    # Plus 2 memo calls for edge case 9004 and 1 chat call: 15 Stage 2 calls in all.
    assert cost["total_usd"] == "0.060600"
    assert cost["eval_overhead_usd"] == "0.000000"


def test_latency_p50_and_p95_per_stage(full):
    latency = full.last["latency_ms"]
    assert set(latency) == {
        "screening", "extraction_map", "extraction_reduce", "matching", "memo", "chat",
    }  # fmt: skip
    assert latency["screening"]["per_call"] == {"n": 3, "p50": LATENCY_MS, "p95": LATENCY_MS}
    # Matching: 3 requirements for 9001 and 1 for 9002.
    assert latency["matching"]["per_call"]["n"] == 4
    assert latency["matching"]["per_tender"] == {"n": 2, "p50": LATENCY_MS, "p95": 3 * LATENCY_MS}
    calls = full.last["calls"]
    assert (calls["total"], calls["by_status"]) == (18, {"ok": 18})


def test_the_composite_is_partial_without_a_memo_score_and_the_gate_fails_below_090(full):
    overall = full.last["overall"]
    assert overall["value"] is None  # the memo suite has no rubric
    assert overall["suites_included"] == [
        "knockout",
        "extraction",
        "screening",
        "retrieval",
        "edge_cases",
    ]
    extraction = (0.5 + 2 / 3 + 2 / 3) / 3
    expected = (0.3 * 0.6 + 0.2 * extraction + 0.2 * 0.5 + 0.1 * 0.5 + 0.1 * 1.0) / 0.9
    assert overall["partial"] == pytest.approx(expected, abs=1e-6)
    # The repo's gate is 0.90 and the stubs find 0.6 of the knock-outs.
    assert (overall["gate"], overall["min_knockout_recall"]) == ("fail", 0.9)
    assert full.last["not_recommended"] is True
    assert overall["weights"]["knockout"] == 0.3


def test_without_a_gate_line_the_gate_is_not_set(tmp_path):
    ran = run(tmp_path, suites=("knockout",), weights_path=_weights(tmp_path))
    assert (ran.last["overall"]["gate"], ran.last["overall"]["min_knockout_recall"]) == (
        "not_set",
        None,
    )
    assert ran.last["not_recommended"] is False


def test_the_summary_is_printed(full):
    text = "\n".join(full.out)
    assert "knockout" in text and "score 0.600" in text
    assert f"results: {full.path}" in text


# --- 32, 33, 34: runs as parameters ---------------------------------------------


def test_a_second_run_with_another_model_is_appended_and_the_first_is_unchanged(tmp_path):
    first = run(tmp_path, suites=("screening",))
    before = first.document()["runs"][0]
    second = run(tmp_path, suites=("screening",), stage1_model=SONNET)
    runs = second.document()["runs"]
    assert len(runs) == 2
    assert runs[0] == before
    assert [entry["params"]["stage1_model"] for entry in runs] == [HAIKU, SONNET]
    assert runs[0]["run_id"] != runs[1]["run_id"]
    # Same tenders, other price: the comparison is the same runner run again.
    assert runs[0]["suites"]["screening"]["metrics"] == runs[1]["suites"]["screening"]["metrics"]
    assert (runs[0]["cost"]["total_usd"], runs[1]["cost"]["total_usd"]) == ("0.000600", "0.012000")
    cases = sorted(path.name for path in (tmp_path / "results").glob("*_cases/*.jsonl"))
    assert cases == sorted(f"{entry['run_id']}.jsonl" for entry in runs)


def test_a_file_that_is_not_a_results_file_is_not_overwritten(tmp_path):
    target = tmp_path / "results" / f"2026-11-15_{COMMIT}.json"
    target.parent.mkdir()
    target.write_text('{"something": "else"}')
    ran = run(tmp_path, suites=("retrieval",))
    assert ran.code == 2
    assert target.read_text() == '{"something": "else"}'


def test_a_prompt_version_is_a_run_parameter(tmp_path):
    ran = run(tmp_path, suites=("extraction",), prompt_overrides={"extract_map": "v2"})
    assert ran.code == 0
    assert ran.last["prompt_versions"]["extract_map"] == "v2"
    assert ran.last["prompt_versions"]["extract_reduce"] == "v1"
    assert "second version" in ran.provider.sent_text()  # the v2 file was really used


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"extract_map": "v9"}, "prompt file not found: extract_map_v9.md"),
        ({"summarise": "v1"}, "unknown prompt name"),
        ({"screen": "latest"}, "invalid prompt reference"),
    ],
)
def test_a_missing_prompt_stops_the_run_before_any_call(tmp_path, overrides, message):
    ran = run(tmp_path, prompt_overrides=overrides)
    assert ran.code == 2
    assert message in ran.out[0]
    assert ran.provider.requests == []


@pytest.mark.parametrize("field", ["stage1_model", "stage2_model", "judge_model"])
def test_a_model_without_a_price_row_is_refused_before_any_call(tmp_path, field):
    ran = run(tmp_path, **{field: "mistral-large-4"})
    assert ran.code == 2
    assert "no price row for model 'mistral-large-4'" in ran.out[0]
    assert ran.provider.requests == []
    assert not ran.results_dir.exists()


def test_an_unknown_suite_is_refused(tmp_path):
    ran = run(tmp_path, suites=("screening", "vibes"))
    assert ran.code == 2 and "unknown suite(s): vibes" in ran.out[0]


def test_only_the_selected_suites_run_and_the_limit_cuts_each_one(tmp_path):
    ran = run(tmp_path, suites=("screening", "retrieval"), limit=2)
    assert set(ran.last["suites"]) == {"retrieval", "screening"}
    assert ran.suite("screening")["n"] == 2
    assert ran.suite("retrieval")["n"] == 2
    assert (ran.last["params"]["suites"], ran.last["params"]["limit"]) == (
        ["retrieval", "screening"],
        2,
    )


def test_recall_at_a_fixed_precision_is_reported_on_request(tmp_path):
    ran = run(tmp_path, suites=("screening",), min_precision=0.9)
    # From confidence 0.5 up only 9001 (0.9) is left: precision 1, recall 1 of 2.
    assert ran.suite("screening")["metrics"]["recall_at_precision"] == {
        "min_precision": 0.9, "recall": 0.5,
    }  # fmt: skip


# --- 35, 40: steps that are not built -------------------------------------------


def test_a_missing_step_gives_not_built_and_the_other_suites_still_run(tmp_path):
    targets = Targets(search=STUBS.search, screen=STUBS.screen)
    ran = run(tmp_path, targets=targets)
    assert ran.code == 0
    assert ran.suite("screening")["status"] == "ok"
    assert ran.suite("retrieval")["status"] == "ok"
    assert ran.suite("extraction") == {"status": "not_built", "detail": WAITING_FOR["extract"]}
    assert ran.suite("knockout")["detail"] == "pipeline/extract.py"
    assert "pipeline/extract.py" in ran.suite("memo")["detail"]
    assert "memo assembly" in ran.suite("memo")["detail"]
    assert ran.suite("edge_cases")["status"] == "not_built"
    assert len(ran.provider.requests) == 3  # the three screening calls, nothing else


def test_knockout_recall_runs_without_the_matching_step(tmp_path):
    targets = Targets(extract=STUBS.extract)
    ran = run(tmp_path, targets=targets, suites=("knockout",))
    values = ran.suite("knockout")["metrics"]
    assert values["recall"]["value"] == 0.6
    assert values["match_accuracy"] == {"status": "not_built", "detail": WAITING_FOR["match"]}


def test_today_every_suite_is_not_built_and_the_run_still_writes_a_valid_file(tmp_path):
    assert default_targets() == Targets()  # no pipeline step is wired in yet
    ran = run(tmp_path, targets=None)
    assert ran.code == 0
    assert {name: suite["status"] for name, suite in ran.last["suites"].items()} == dict.fromkeys(
        SUITE_ORDER, "not_built"
    )
    assert ran.suite("screening")["detail"] == "pipeline/screen.py"
    assert ran.suite("retrieval")["detail"] == "rag/search.py"
    assert ran.provider.requests == []
    assert ran.last["cost"]["total_usd"] == "0.000000"
    assert ran.last["overall"]["partial"] is None
    assert ran.cases() == []


# --- 36: the memo slot ----------------------------------------------------------


def test_without_a_rubric_the_memo_suite_is_no_rubric_and_still_counts_citations(full):
    suite = full.suite("memo")
    assert (suite["status"], suite["n"]) == ("no_rubric", 2)
    # Five citations in the two memos; one did not pass the citation check.
    assert suite["metrics"]["verified_citation_share"]["value"] == 0.8
    assert "judge_score" not in suite["metrics"]
    assert "judge" not in full.last["latency_ms"]  # no judge call was made
    hashes = [row["memo_sha256"] for row in full.cases() if row["suite"] == "memo"]
    assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in hashes)


def test_with_a_rubric_and_a_judge_model_the_memos_are_judged(tmp_path):
    datasets = copy_datasets(tmp_path, rubric=True)
    ran = run(tmp_path, datasets_dir=datasets, judge_model=HAIKU)
    suite = ran.suite("memo")
    assert suite["status"] == "ok"
    # Weights 3 and 1, scores 4 and 5 on a scale to 5: (3 x 0.75 + 1 x 1.0) / 4.
    assert suite["metrics"]["judge_score"] == 0.8125
    assert ran.last["cost"]["eval_overhead_usd"] == "0.000400"  # two judge calls on Haiku
    assert ran.last["cost"]["per_memo_usd"] == "0.024000"  # the judge is not in the memo cost
    assert ran.last["labels"]["memo_rubric.json"]["version"] == "v1"
    overall = ran.last["overall"]
    assert overall["value"] == pytest.approx(overall["partial"])
    assert len(overall["suites_included"]) == 6


def test_a_rubric_without_a_judge_model_or_judge_step_is_not_built(tmp_path):
    datasets = copy_datasets(tmp_path, rubric=True)
    no_model = run(tmp_path, datasets_dir=datasets, suites=("memo",))
    assert no_model.suite("memo")["status"] == "not_built"
    assert no_model.suite("memo")["detail"] == "--judge-model"
    targets = Targets(extract=STUBS.extract, match=STUBS.match, memo=STUBS.memo)
    no_step = run(
        tmp_path, datasets_dir=datasets, suites=("memo",), judge_model=HAIKU, targets=targets
    )
    assert no_step.suite("memo")["detail"] == WAITING_FOR["judge"]


def test_hand_grades_are_compared_with_the_judge_on_the_same_memo_text(tmp_path):
    datasets = copy_datasets(tmp_path, rubric=True)
    memo_dir = tmp_path / "memos"
    first = run(
        tmp_path, datasets_dir=datasets, suites=("memo",), judge_model=HAIKU, memo_dir=memo_dir
    )
    digest = next(row["memo_sha256"] for row in first.cases() if row["publication_id"] == 9001)
    saved = memo_dir / first.last["run_id"] / "9001.json"
    assert saved.is_file() and PAGE_CANARY not in first.written_text()
    # Arthur grades that memo: he agrees on one criterion and is one point lower on the other.
    grade = {"publication_id": 9001, "memo_sha256": digest, "scores": {"complete": 4, "clear": 4}}
    (datasets / "memo_grades.jsonl").write_text(json.dumps(grade) + "\n")
    second = run(
        tmp_path, datasets_dir=datasets, suites=("memo",), judge_model=HAIKU, memo_dir=memo_dir
    )
    agreement = second.suite("memo")["metrics"]["judge_agreement"]
    assert (agreement["graded_memos"], agreement["graded_memos_judged"]) == (1, 1)
    assert agreement["by_criterion"]["complete"]["exact_agreement"] == 1.0
    assert agreement["by_criterion"]["clear"]["exact_agreement"] == 0.0
    assert agreement["by_criterion"]["clear"]["within_one"] == 1.0


def test_memos_are_not_saved_unless_a_folder_is_given(tmp_path):
    run(tmp_path, suites=("memo",))
    assert not any("memo" in path.name for path in tmp_path.iterdir() if path.is_dir())


# --- 37: a failing call ---------------------------------------------------------


def test_a_provider_error_on_one_case_is_recorded_and_the_run_continues(tmp_path):
    provider = FakeProvider(http_error(400), ack(), ack())  # 400 is not retried
    ran = run(tmp_path, provider=provider, suites=("screening",))
    assert ran.code == 0
    rows = {row["publication_id"]: row for row in ran.cases()}
    assert rows[9001]["error"] == "ProviderError"
    assert rows[9001]["predicted"] is False  # an errored tender is not shortlisted
    assert rows[9002]["error"] is None and rows[9004]["error"] is None
    values = ran.suite("screening")["metrics"]
    assert (values["errors"], values["fn"], values["tp"]) == (1, 2, 0)
    assert ran.last["calls"]["by_status"] == {"ok": 2, "provider_error": 1}
    assert ran.last["cost"]["screened"] == 3  # the failed tender was sent to the model


def test_a_failed_extraction_fails_its_cases_in_every_suite_that_needs_it(tmp_path):
    def broken(docs, env):
        raise RuntimeError("boom")

    targets = Targets(extract=broken, match=STUBS.match, memo=STUBS.memo)
    ran = run(tmp_path, targets=targets, suites=("extraction", "knockout", "memo"))
    assert ran.code == 0
    assert ran.suite("extraction")["metrics"]["errors"] == 3
    assert ran.suite("knockout")["metrics"]["recall"]["value"] == 0.0
    assert ran.suite("memo")["metrics"]["errors"] == 2
    assert ran.last["cost"]["memos_failed"] == 2
    assert {row["error"] for row in ran.cases()} == {"RuntimeError"}
    assert "boom" not in ran.written_text()  # only the error type is kept


def test_an_edge_case_with_an_unknown_expect_key_is_an_invalid_label(tmp_path):
    datasets = copy_datasets(tmp_path)
    rows = (datasets / "edge_cases.jsonl").read_text().splitlines()
    rows[1] = rows[1].replace('"memo_created": true', '"mood": "good"')
    rows[2] = rows[2].replace("false", "true")  # the stub declines, the label now expects an answer
    (datasets / "edge_cases.jsonl").write_text("\n".join(rows) + "\n")
    values = run(tmp_path, datasets_dir=datasets, suites=("edge_cases",)).suite("edge_cases")[
        "metrics"
    ]
    assert (values["passed"], values["failed"], values["invalid_label"]) == (1, 1, 1)
    assert values["pass_rate"]["value"] == 0.5  # the invalid label is not in the denominator


# --- 38: git --------------------------------------------------------------------


def test_the_commit_and_the_dirty_flag_come_from_the_injected_function(tmp_path):
    prompts_dir = make_prompts(tmp_path)
    out: list[str] = []
    code = run_evals(
        make_config(tmp_path, suites=("retrieval",)),
        targets=STUBS,
        gateway_factory=lambda log: None,  # retrieval never touches the gateway
        prompts_dir=prompts_dir,
        git=lambda: ("fedcba9", True),
        now=lambda: datetime(2026, 12, 1, 8, 30),
        out=out.append,
    )
    assert code == 0
    document = json.loads((tmp_path / "results" / "2026-12-01_fedcba9.json").read_text())
    assert document["git_commit"] == "fedcba9"
    assert document["runs"][0]["git_dirty"] is True


# --- 39: nothing private in the results -----------------------------------------


def test_results_and_case_files_hold_no_document_text_and_no_contact_data(full):
    text = full.written_text()
    assert len(full.cases()) > 10
    for canary in (PAGE_CANARY, CONTACT_CANARY, EMAIL_CANARY, "Synthetic", "Gemeente"):
        assert canary not in text
    printed = "\n".join(full.out)
    assert PAGE_CANARY not in printed and CONTACT_CANARY not in printed


# Results files are committed (DECISIONS.md E7). Every string in them must be a score, cost,
# version, status or identifier: one token without spaces. The only texts with spaces are the
# fixed explanations written by the runner itself.
_TOKEN = re.compile(r"^[\w.:/+\-]{0,80}$")
_RUNNER_TEXTS = {*WAITING_FOR.values(), "the cost cap was reached"}


def _strings(value, found: list[str]) -> list[str]:
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            found.append(key)
            _strings(item, found)
    elif isinstance(value, list):
        for item in value:
            _strings(item, found)
    return found


def _allowed(text: str) -> bool:
    return bool(_TOKEN.match(text)) or all(part in _RUNNER_TEXTS for part in text.split("; "))


def test_a_committed_results_file_holds_only_scores_costs_versions_and_ids(tmp_path):
    datasets = copy_datasets(tmp_path, rubric=True)
    ran = run(tmp_path, datasets_dir=datasets, judge_model=HAIKU, memo_dir=tmp_path / "memos")
    partial = run(tmp_path, targets=Targets(screen=STUBS.screen))  # not_built details
    assert ran.code == partial.code == 0
    found = _strings(ran.document(), [])
    for row in ran.cases() + partial.cases():
        _strings(row, found)
    _strings(partial.document(), found)
    assert len(found) > 300
    bad = sorted({text for text in found if not _allowed(text)})
    assert bad == []
    # No quote, page text, model text or contact data, in any form.
    text = ran.written_text()
    for banned in (QUOTE, PAGE_CANARY, CONTACT_CANARY, EMAIL_CANARY, "Synthetic", "quote"):
        assert banned not in text
    # The memos themselves went to the memo folder, not to the results.
    assert any((tmp_path / "memos").rglob("*.json"))


def test_the_allowlist_catches_document_text():
    assert not _allowed(QUOTE)
    assert not _allowed("Gunningscriteria en wegingen")
    assert _allowed("claude-sonnet-5-5") and _allowed("2026-11-15T10:00:00")
    assert _allowed("0.024000") and _allowed("9001-KO-03") and _allowed(WAITING_FOR["extract"])


def test_labels_that_point_outside_the_snapshot_stop_the_run(tmp_path):
    datasets = copy_datasets(tmp_path)
    with (datasets / "screening.jsonl").open("a") as handle:
        handle.write('{"publication_id": 12345, "label": "relevant"}\n')
    ran = run(tmp_path, datasets_dir=datasets)
    assert ran.code == 2 and "tender 12345 is not in the snapshot" in ran.out[0]

    datasets = copy_datasets(tmp_path / "b")
    text = (datasets / "knockout.jsonl").read_text().replace("a90010000002", "ffffffffffff")
    (datasets / "knockout.jsonl").write_text(text)
    ran = run(tmp_path / "b", datasets_dir=datasets)
    assert ran.code == 2 and "tender 9001 has no document ffffffffffff" in ran.out[0]
    assert ran.provider.requests == []


def test_a_malformed_label_file_stops_the_run(tmp_path):
    datasets = copy_datasets(tmp_path)
    (datasets / "screening.jsonl").write_text('{"publication_id": 9001, "label": "perhaps"}\n')
    ran = run(tmp_path, datasets_dir=datasets)
    assert ran.code == 2 and "screening.jsonl line 1" in ran.out[0]


# --- weights, gate and the command line ------------------------------------------


def test_the_weights_file_in_the_repo_is_valid_and_sets_the_proposed_gate():
    weights, minimum = load_weights(WEIGHTS_PATH)
    assert set(weights) == set(SUITE_ORDER)
    assert sum(weights.values()) == pytest.approx(1.0)
    assert max(weights, key=weights.get) == "knockout"  # weighted heaviest (PLAN.md section 6)
    assert minimum == 0.90  # Proposed, DECISIONS.md E1


# --- git-ignored eval data (DECISIONS.md E4, E7) --------------------------------


def _ignored(path: Path) -> bool:
    """Ask git itself, so the test follows every .gitignore file in the repo."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--no-index", str(path.relative_to(ROOT))],
        cwd=ROOT,
        capture_output=True,
    )
    if result.returncode not in (0, 1):
        pytest.skip("git is not available")
    return result.returncode == 0


@pytest.mark.parametrize(
    "path",
    [
        SNAPSHOT_DIR / "manifest.json",
        SNAPSHOT_DIR / "tenders" / "412551" / "pages" / "a3f9c2d41b07.jsonl",
        MEMO_DIR / "00000000-0000-4000-8000-000000000001" / "412551.json",
    ],
    ids=["snapshot manifest", "snapshot page text", "eval memo"],
)
def test_the_snapshot_and_the_eval_memos_are_git_ignored(path):
    assert _ignored(path), f"{path.relative_to(ROOT)} is not git-ignored"


def test_the_results_folder_is_not_git_ignored():
    assert not _ignored(RESULTS_DIR / "2026-11-15_abc1234.json")
    assert not _ignored(RESULTS_DIR / "2026-11-15_abc1234_cases" / "run.jsonl")


def test_the_command_line_saves_memos_in_the_ignored_folder_by_default():
    from evals.run import _parser

    assert _parser().parse_args([]).memo_dir == MEMO_DIR


def _weights(tmp_path: Path, gate: str = "", knockout: str = "0.30") -> Path:
    path = tmp_path / "weights.toml"
    path.write_text(
        f"[weights]\nknockout = {knockout}\nextraction = 0.20\nscreening = 0.20\n"
        f"retrieval = 0.10\nmemo = 0.10\nedge_cases = 0.10\n\n[gate]\n{gate}\n"
    )
    return path


@pytest.mark.parametrize(
    ("minimum", "gate", "not_recommended"), [("0.50", "pass", False), ("0.90", "fail", True)]
)
def test_the_gate_compares_knockout_recall_with_the_minimum(
    tmp_path, minimum, gate, not_recommended
):
    weights = _weights(tmp_path, f"min_knockout_recall = {minimum}")
    ran = run(tmp_path, suites=("knockout",), weights_path=weights)  # recall is 0.6
    assert ran.last["overall"]["gate"] == gate
    assert ran.last["overall"]["min_knockout_recall"] == float(minimum)
    assert ran.last["not_recommended"] is not_recommended


def test_the_gate_is_not_evaluated_when_the_knockout_suite_did_not_run(tmp_path):
    weights = _weights(tmp_path, "min_knockout_recall = 0.9")
    ran = run(tmp_path, suites=("screening",), weights_path=weights)
    assert ran.last["overall"]["gate"] == "not_evaluated"
    assert ran.last["not_recommended"] is False


@pytest.mark.parametrize(
    ("gate", "knockout"), [("", "0.40"), ("min_knockout_recall = 1.5", "0.30"), ("", "-0.1")]
)
def test_bad_weights_stop_the_run(tmp_path, gate, knockout):
    ran = run(tmp_path, weights_path=_weights(tmp_path, gate, knockout))
    assert ran.code == 2
    assert ran.provider.requests == []


def test_the_command_line_runs_a_dry_run_on_the_fixtures(capsys):
    arguments = [
        "--stage1-model", HAIKU, "--stage2-model", SONNET, "--dry-run",
        "--snapshot", str(ROOT / "tests/fixtures/evals/snapshot"),
        "--datasets", str(ROOT / "tests/fixtures/evals/datasets"),
    ]  # fmt: skip
    assert main(arguments) == 0
    printed = capsys.readouterr().out
    assert printed.startswith("dry run")
    assert '"matching"' in printed


def test_the_command_line_lists_the_documents_of_a_tender(capsys):
    snapshot = str(ROOT / "tests/fixtures/evals/snapshot")
    assert main(["--list-docs", "9001", "--snapshot", snapshot]) == 0
    printed = capsys.readouterr().out
    assert "a90010000001  guideline" in printed and "Programma van eisen.pdf" in printed
    assert main(["--list-docs", "4242", "--snapshot", snapshot]) == 2


def test_the_command_line_refuses_a_live_run_without_a_cap(capsys, tmp_path):
    arguments = [
        "--stage1-model", HAIKU, "--stage2-model", SONNET,
        "--snapshot", str(ROOT / "tests/fixtures/evals/snapshot"),
        "--datasets", str(ROOT / "tests/fixtures/evals/datasets"),
        "--results", str(tmp_path / "results"),
    ]  # fmt: skip
    assert main(arguments) == 2
    assert "--max-cost-usd" in capsys.readouterr().out
    assert not (tmp_path / "results").exists()
    with pytest.raises(SystemExit):
        main([*arguments, "--max-cost-usd", "0"])


def test_make_evals_runs_the_runner_and_is_a_dry_run_without_a_cap():
    makefile = (ROOT / "Makefile").read_text()
    assert "-m evals.run" in makefile
    assert "$(if $(MAX_COST),--max-cost-usd $(MAX_COST),--dry-run)" in makefile
