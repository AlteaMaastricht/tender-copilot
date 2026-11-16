"""Tests for the cost cap and the dry run (evals/budget.py, evals/run.py)."""

from __future__ import annotations

import json
from decimal import Decimal
from uuid import uuid4

import pytest

from evals.budget import RunLog
from evals.run import run_evals
from llm.log import LlmCallRecord, MemoryCallLog
from tests.evals_stubs import (
    HAIKU_CALL,
    STUBS,
    Ran,
    ack,
    copy_datasets,
    make_config,
    make_prompts,
    run,
)
from tests.fakes import HAIKU, SONNET, FakeProvider, reply


def _record(cost: str, status: str = "ok") -> LlmCallRecord:
    return LlmCallRecord(
        run_id=uuid4(), stage="screening", provider="anthropic", model=HAIKU,
        prompt_name="screen", prompt_version="v1", status=status, cost_usd=Decimal(cost),
    )  # fmt: skip


def test_the_run_log_adds_up_every_call_and_passes_it_on():
    sink = MemoryCallLog()
    log = RunLog(Decimal("0.01"), sink)
    log.write(_record("0.004"))
    assert not log.cap_reached
    log.write(_record("0.006", status="validation_failed"))  # a failed call was billed too
    assert log.cap_reached and log.spent == Decimal("0.010")
    assert len(sink.records) == 2
    assert not RunLog(None).cap_reached  # no cap, as in a test


def test_the_cost_cap_stops_the_run_before_the_next_case(tmp_path):
    # One screening call costs USD 0.0002, so the cap is passed by the first case.
    ran = run(tmp_path, max_cost_usd=Decimal("0.0001"))
    assert ran.code == 3
    assert len(ran.provider.requests) == 1  # the second tender was never sent
    run_entry = ran.last
    assert (run_entry["completed"], run_entry["stop_reason"]) == (False, "cost_cap")
    assert ran.suite("screening")["status"] == "stopped_cost_cap"
    assert ran.suite("screening")["n"] == 1  # scored over the finished case only
    for later in ("extraction", "knockout", "memo", "edge_cases"):
        assert ran.suite(later)["status"] == "not_run"
    assert ran.suite("retrieval")["status"] == "ok"  # it ran first and calls no model
    assert run_entry["cost"]["total_usd"] == "0.000200"
    assert run_entry["overall"]["value"] is None
    assert any("cost cap" in line for line in ran.out)
    assert [row["suite"] for row in ran.cases()].count("screening") == 1


def test_the_cap_is_checked_before_every_case_not_only_between_suites(tmp_path):
    ran = run(tmp_path, suites=("screening",), max_cost_usd=HAIKU_CALL * 2)
    # Tender 9001 and 9002 are screened; the cap is then reached, so 9003 and 9004 are not run.
    assert len(ran.provider.requests) == 2
    assert ran.suite("screening")["n"] == 2
    assert ran.code == 3


def test_a_run_under_the_cap_finishes(tmp_path):
    ran = run(tmp_path, suites=("screening",), max_cost_usd=Decimal("0.0007"))
    assert ran.code == 0
    assert (ran.last["completed"], ran.last["stop_reason"]) == (True, None)
    assert ran.last["params"]["max_cost_usd"] == "0.0007"


def test_the_billed_cost_of_a_failed_call_counts_towards_the_cap(tmp_path):
    truncated = reply("{", stop="max_tokens")  # billed, and raised as OutputTruncated
    ran = run(
        tmp_path,
        provider=FakeProvider(truncated),  # a second request would raise IndexError
        suites=("screening",),
        max_cost_usd=Decimal("0.0001"),
    )
    assert ran.code == 3
    assert len(ran.provider.requests) == 1
    assert ran.last["calls"]["by_status"] == {"truncated": 1}
    assert ran.last["cost"]["total_usd"] == "0.000200"
    assert ran.cases()[0]["error"] == "OutputTruncated"


def test_a_live_run_without_a_cost_cap_is_refused(tmp_path):
    ran = run(tmp_path, max_cost_usd=None)
    assert ran.code == 2
    assert "--max-cost-usd" in ran.out[0]
    assert ran.provider.requests == []
    assert not ran.results_dir.exists()


# --- dry run --------------------------------------------------------------------


def _no_gateway(log):
    raise AssertionError("a dry run must not build a gateway")


def dry_run(tmp_path, **changes) -> tuple[int, dict, list[str]]:
    out: list[str] = []
    prompts_dir = make_prompts(tmp_path)
    config = make_config(tmp_path, dry_run=True, max_cost_usd=None, **changes)
    code = run_evals(
        config, targets=STUBS, gateway_factory=_no_gateway, prompts_dir=prompts_dir, out=out.append
    )
    return code, json.loads(out[1]) if len(out) > 1 else {}, out


def test_a_dry_run_calls_no_model_and_writes_nothing(tmp_path):
    code, estimate, out = dry_run(tmp_path)
    assert code == 0
    assert out[0].startswith("dry run")
    assert not (tmp_path / "results").exists()
    assert Decimal(estimate["expected_usd"]) > 0
    assert Decimal(estimate["upper_bound_usd"]) > Decimal(estimate["expected_usd"])
    assert estimate["assumptions"]["chars_per_token"] == 3


def test_a_dry_run_counts_the_calls_per_stage(tmp_path):
    stages = dry_run(tmp_path)[1]["by_stage"]
    calls = {name: (stage["calls"], stage["calls_upper"]) for name, stage in stages.items()}
    assert calls == {
        # 9001, 9002 and 9004 pass the hard filter; 9003 does not. One retry each at most.
        "screening": (3, 6),
        # Key documents of 9001 and 9002 each fit one section. 9004 is a scan: no call.
        "extraction_map": (2, 4),
        "extraction_reduce": (2, 4),
        # 3 + 2 non-formal knock-out labels: 3 steps each expected, 6 at most.
        "matching": (15, 30),
        # 9001, 9002 and the edge-case tender 9004: two calls each.
        "memo": (6, 12),
        # One chat question: 3 steps expected, 8 at most.
        "chat": (3, 8),
    }


def test_a_dry_run_prices_the_calls_from_the_price_table(tmp_path):
    stages = dry_run(tmp_path)[1]["by_stage"]
    # Sonnet: USD 2 in and USD 10 out per million tokens.
    # Matching: 15 calls of 6,666 tokens in and 400 out.
    assert Decimal(stages["matching"]["expected_usd"]) == 15 * Decimal("0.017332")
    # Upper bound: 30 calls of 10,000 tokens in and 400 out.
    assert Decimal(stages["matching"]["upper_bound_usd"]) == 30 * Decimal("0.024")
    # Memo: 6 calls of 7,500 tokens in and 1,500 out.
    assert Decimal(stages["memo"]["expected_usd"]) == 6 * Decimal("0.03")
    # Chat: 3 calls of 6,666 tokens in and 500 out.
    assert Decimal(stages["chat"]["expected_usd"]) == 3 * Decimal("0.018332")
    total = sum(Decimal(stage["expected_usd"]) for stage in stages.values())
    assert Decimal(dry_run(tmp_path)[1]["expected_usd"]) == total


def test_a_dry_run_follows_the_suites_the_limit_and_the_judge(tmp_path):
    only_screening = dry_run(tmp_path, suites=("screening",), limit=1)[1]["by_stage"]
    assert {name: stage["calls"] for name, stage in only_screening.items()} == {"screening": 1}
    assert dry_run(tmp_path, suites=("retrieval",))[1]["by_stage"] == {}  # no model call at all

    datasets = copy_datasets(tmp_path, rubric=True)
    judged = dry_run(tmp_path, suites=("memo",), datasets_dir=datasets, judge_model=HAIKU)[1]
    assert judged["by_stage"]["judge"]["calls"] == 2  # one per memo
    # Haiku: 8,000 tokens in at USD 0.10 and 500 out at USD 0.50 per million.
    assert Decimal(judged["by_stage"]["judge"]["expected_usd"]) == 2 * Decimal("0.00105")
    without_judge = dry_run(tmp_path, suites=("memo",), datasets_dir=datasets)[1]
    assert "judge" not in without_judge["by_stage"]


def test_a_dry_run_still_checks_the_snapshot_the_labels_and_the_models(tmp_path):
    code, _, out = dry_run(tmp_path, snapshot_dir=tmp_path / "nowhere")
    assert code == 2 and "no snapshot" in out[0]
    code, _, out = dry_run(tmp_path, stage2_model="not-a-model")
    assert code == 2 and "no price row" in out[0]


@pytest.mark.parametrize("model", [SONNET])
def test_the_estimate_and_a_real_run_agree_on_who_is_screened(tmp_path, model):
    estimate = dry_run(tmp_path, suites=("screening",))[1]
    ran: Ran = run(tmp_path, suites=("screening",), stage1_model=model)
    assert estimate["by_stage"]["screening"]["calls"] == ran.last["cost"]["screened"] == 3
    assert ran.last["calls"]["total"] == 3
    assert ack().usage.input_tokens == 1000
