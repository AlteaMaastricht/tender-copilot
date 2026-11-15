"""Requirement matching: a limit or failure becomes an unclear match, never a guess."""

from __future__ import annotations

import pytest

from agent.fakes import FakeBackend
from llm.providers.base import ToolCall, Usage
from llm.redact import Redactor
from pipeline.match import match_requirement
from pipeline.memo import compute_fit, decide_recommendation
from pipeline.schemas import KnockoutCriterion, RequirementMatch
from tests.fakes import CONTEXT, SONNET, FakeProvider, http_error, make_gateway, reply
from tests.test_schemas import citation, knockout, match

SEARCH = ToolCall("c1", "search_tender_docs", {"tender_id": 42, "query": "ISO 27001"})
BAD = ToolCall("bad", "get_tender", {})
BIG = Usage(input_tokens=35_000, output_tokens=100)
NO_EVIDENCE = reply(match(status="met", company_evidence=[]))
CRITERION = KnockoutCriterion(
    **knockout(requirement_id="KO-07", citation=citation(page=14, verified=True))
)


def tools(*calls, usage=None):
    return reply("", tool_calls=calls, usage=usage)


def run(tmp_path, *script) -> tuple[RequirementMatch, FakeProvider]:
    provider = FakeProvider(*script)
    gateway, _ = make_gateway(tmp_path, provider)
    result = match_requirement(
        CRITERION,
        gateway=gateway,
        backend=FakeBackend(),
        model=SONNET,
        redactor=Redactor(),
        context=CONTEXT,
    )
    return result, provider


def test_a_final_answer_is_returned_with_the_criterions_own_id(tmp_path):
    result, provider = run(tmp_path, tools(SEARCH), reply(match(requirement_id="KO-99")))
    assert result.status == "met" and result.requirement_id == "KO-07"
    # The criterion is sent as untrusted data, not as an instruction.
    opening = provider.requests[0].messages[0].text
    assert 'label="requirement"' in opening and "KO-07" in opening
    assert "KO-07" not in provider.requests[0].system
    # The tender ID is the run's own, set by code, so it is a trusted variable.
    assert "of tender 42." in provider.requests[0].system and "tender 42" not in opening


def test_a_formal_requirement_is_never_sent_to_the_model(tmp_path):
    from pipeline.match import needs_matching

    formal = KnockoutCriterion(**knockout(requirement_id="KO-08", kind="formal"))
    provider = FakeProvider()
    gateway, log = make_gateway(tmp_path, provider)
    assert not needs_matching(formal) and needs_matching(CRITERION)
    with pytest.raises(ValueError, match="formal"):
        match_requirement(
            formal,
            gateway=gateway,
            backend=FakeBackend(),
            model=SONNET,
            redactor=Redactor(),
            context=CONTEXT,
        )
    assert provider.requests == [] and log.records == []


STOPPED = {
    "step_limit": ([tools(SEARCH)] * 6, "step_limit", "6 steps"),
    "token_budget": ([tools(SEARCH, usage=BIG)] * 2, "token_budget", "60000 tokens"),
    "tool_errors": ([tools(BAD, BAD, BAD)], "loop_failed", "tool errors"),
    "refusal": ([reply("{", stop="refusal")], "loop_failed", "model call failed"),
    "truncated": ([reply("{", stop="max_tokens")], "loop_failed", "model call failed"),
    "provider_error": ([http_error(400)], "loop_failed", "model call failed"),
    "invalid_twice": ([reply("x"), reply("y")], "loop_failed", "model call failed"),
    "met_without_evidence_twice": ([NO_EVIDENCE, NO_EVIDENCE], "no_evidence_found", "evidence"),
}


@pytest.mark.parametrize(("script", "reason", "cause"), STOPPED.values(), ids=STOPPED.keys())
def test_a_stopped_run_gives_an_unclear_match_built_in_code(tmp_path, script, reason, cause):
    result, _ = run(tmp_path, *script)
    assert isinstance(result, RequirementMatch)
    assert (result.status, result.unclear_reason) == ("unclear", reason)
    assert result.requirement_id == "KO-07"
    assert result.company_evidence == [] and result.mitigation == "none"
    assert result.tender_citation == CRITERION.citation  # the criterion's own citation
    assert result.gap.startswith("Not assessed") and cause in result.gap
    # A valid RequirementMatch: it passes its own validators again.
    assert RequirementMatch.model_validate(result.model_dump()) == result


@pytest.mark.parametrize(("script", "reason", "cause"), STOPPED.values(), ids=STOPPED.keys())
def test_a_stopped_run_makes_the_tender_needs_review(tmp_path, script, reason, cause):
    result, _ = run(tmp_path, *script)
    decision = decide_recommendation(
        knockouts=[CRITERION],
        matches=[result],
        fit=compute_fit([]),
        documents_missing=False,
    )
    assert decision.recommendation == "needs_review"
    assert "knockout_unclear" in decision.reasons
