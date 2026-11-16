"""Gateway behaviour with a fake provider: validation, retries, tools and structure."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from llm.client import (
    MAX_ATTEMPTS,
    ModelRefusal,
    OutputTruncated,
    SchemaValidationError,
    UnknownModelError,
)
from llm.prompts import PromptRef, UntrustedText
from llm.providers.base import ProviderError, ToolCall, ToolSpec, Usage
from llm.redact import Redactor
from pipeline.schemas import ExtractionOutput, RequirementMatch, ScreeningOutput
from tests.fakes import CONTEXT, HAIKU, SCREENING, FakeProvider, http_error, make_gateway, reply
from tests.test_schemas import extraction, match

REPO = Path(__file__).resolve().parent.parent
DATA = [UntrustedText("notice", "Dataplatform voor een gemeente.")]


def screen(gateway, schema=ScreeningOutput, **kwargs):
    return gateway.call(
        task="screening",
        prompt=PromptRef("screen", "v1"),
        variables={"company": "Datalink B.V."},
        data=DATA,
        schema=schema,
        model=HAIKU,
        redactor=Redactor(),
        context=CONTEXT,
        **kwargs,
    )


def run(tmp_path, *script, schema=ScreeningOutput, **kwargs):
    provider = FakeProvider(*script)
    gateway, log = make_gateway(tmp_path, provider, **kwargs)
    return provider, gateway, log, lambda: screen(gateway, schema)


# --- validation ------------------------------------------------------------------


def test_valid_json_returns_the_schema_object(tmp_path):
    provider, _, log, call = run(tmp_path, reply())
    result = call()
    assert isinstance(result, ScreeningOutput) and result.decision == "shortlist"
    assert len(provider.requests) == 1
    assert (log.records[0].status, log.records[0].validation_retries) == ("ok", 0)


@pytest.mark.parametrize(
    "text",
    [
        "```json\n" + json.dumps(SCREENING) + "\n```",
        "```\n" + json.dumps(SCREENING) + "\n```",
        "Here is the result:\n" + json.dumps(SCREENING) + "\nDone.",
    ],
)
def test_json_in_a_fence_or_with_text_around_it_is_accepted(tmp_path, text):
    _, _, _, call = run(tmp_path, reply(text))
    assert call().confidence == 0.8


def test_invalid_then_valid_makes_exactly_one_retry_with_the_error(tmp_path):
    bad = reply({**SCREENING, "decision": "maybe"})
    provider, _, log, call = run(tmp_path, bad, reply())
    assert call().decision == "shortlist"

    first, second = provider.requests
    assert len(first.messages) == 1 and len(second.messages) == 3
    assert second.messages[1].role == "assistant" and second.messages[1].raw == bad.raw
    retry = second.messages[2].text
    assert "decision" in retry and "literal_error" in retry
    assert "maybe" not in retry  # the rejected value is not echoed back
    assert second.system == first.system and second.messages[0] == first.messages[0]

    record = log.records[0]
    assert (record.status, record.validation_retries) == ("ok", 1)
    assert len(log.records) == 1


def test_invalid_twice_raises_after_exactly_two_requests(tmp_path):
    provider, _, log, call = run(tmp_path, reply("not json"), reply({"decision": "x"}), reply())
    with pytest.raises(SchemaValidationError) as caught:
        call()
    assert len(caught.value.errors) == 2
    assert len(provider.requests) == 2 and len(provider.script) == 1
    record = log.records[0]
    assert (record.status, record.validation_retries) == ("validation_failed", 1)
    assert record.error.startswith("SchemaValidationError")


def test_a_model_validator_failure_gets_the_same_single_retry(tmp_path):
    no_evidence = reply(match(status="met", company_evidence=[]))
    provider, _, log, call = run(tmp_path, no_evidence, reply(match()), schema=RequirementMatch)
    assert call().status == "met"
    assert "company_evidence" in provider.requests[1].messages[2].text
    assert log.records[0].validation_retries == 1


def test_a_recursive_schema_works(tmp_path):
    provider, _, _, call = run(tmp_path, reply(extraction()), schema=ExtractionOutput)
    result = call()
    assert result.award_criteria[0].subcriteria[0].name == "Risicodossier"
    assert "AwardCriterion" in provider.requests[0].system


@pytest.mark.parametrize(
    ("stop", "error", "status"),
    [("refusal", ModelRefusal, "refused"), ("max_tokens", OutputTruncated, "truncated")],
)
def test_refusal_and_truncation_are_not_retried(tmp_path, stop, error, status):
    provider, _, log, call = run(tmp_path, reply("{", stop=stop), reply())
    with pytest.raises(error):
        call()
    assert len(provider.requests) == 1
    record = log.records[0]
    assert (record.status, record.stop_reason, record.validation_retries) == (status, stop, 0)
    assert record.output_tokens == 200  # the failed request was still billed and is logged


# --- network retries -------------------------------------------------------------


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 529, None])
def test_a_transient_failure_is_retried(tmp_path, status):
    provider, gateway, log, call = run(tmp_path, http_error(status), reply())
    assert call().decision == "shortlist"
    assert len(provider.requests) == 2 and len(gateway.sleeps) == 1
    assert (log.records[0].status, log.records[0].network_retries) == ("ok", 1)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
def test_a_client_error_is_not_retried(tmp_path, status):
    provider, gateway, log, call = run(tmp_path, http_error(status), reply())
    with pytest.raises(ProviderError):
        call()
    assert len(provider.requests) == 1 and gateway.sleeps == []
    record = log.records[0]
    assert (record.status, record.network_retries) == ("provider_error", 0)
    assert record.error == f"ProviderError {status}: status {status}"


def test_four_failures_raise_after_exactly_four_attempts(tmp_path):
    provider, gateway, log, call = run(tmp_path, *[http_error(503)] * 5)
    with pytest.raises(ProviderError):
        call()
    assert len(provider.requests) == MAX_ATTEMPTS == 4
    assert gateway.sleeps == [1.0, 2.0, 4.0]  # jitter fixed at its maximum in tests
    assert (log.records[0].status, log.records[0].network_retries) == ("provider_error", 3)
    assert log.records[0].cost_usd == 0


def test_backoff_has_jitter_and_stays_under_its_ceiling(tmp_path):
    provider, gateway, _, call = run(tmp_path, *[http_error(500)] * 3, reply(), jitter=lambda: 0.0)
    call()
    assert gateway.sleeps == [0.5, 1.0, 2.0]  # jitter at its minimum: half the ceiling


def test_retry_after_is_honoured_and_capped(tmp_path):
    script = [http_error(429, retry_after=7), http_error(429, retry_after=3600), reply()]
    _, gateway, _, call = run(tmp_path, *script)
    call()
    assert gateway.sleeps == [7, 60.0]


def test_network_retries_do_not_use_up_the_validation_retry(tmp_path):
    script = [http_error(500), reply("not json"), http_error(429), http_error(None), reply()]
    provider, _, log, call = run(tmp_path, *script)
    assert call().decision == "shortlist"
    assert len(provider.requests) == 5
    record = log.records[0]
    assert (record.validation_retries, record.network_retries, record.status) == (1, 3, "ok")


# --- tools and the agent step ----------------------------------------------------

TOOLS = [
    ToolSpec(
        "get_company_facts",
        "Return exact structured facts about the company.",
        {"type": "object", "properties": {"topics": {"type": "array"}}, "required": ["topics"]},
    )
]
CALL = ToolCall("call_1", "get_company_facts", {"topics": ["certifications"]})


def step(gateway, history=(), **kwargs):
    return gateway.step(
        task="matching",
        prompt=PromptRef("match", "v2"),
        data=[UntrustedText("requirement", "KO-01: geldig ISO 27001-certificaat.")],
        history=history,
        tools=TOOLS,
        model=HAIKU,
        redactor=Redactor(),
        context=CONTEXT,
        step_index=kwargs.pop("step_index", 0),
        final_schema=RequirementMatch,
        **kwargs,
    )


def test_tool_definitions_reach_the_provider_unchanged(tmp_path):
    provider = FakeProvider(reply("", tool_calls=(CALL,)))
    gateway, _ = make_gateway(tmp_path, provider)
    step(gateway)
    assert provider.requests[0].tools == tuple(TOOLS)


def test_a_tool_call_is_returned_to_the_caller_and_not_validated(tmp_path):
    response = reply("Ik zoek het certificaat op.", tool_calls=(CALL,))
    provider = FakeProvider(response)
    gateway, log = make_gateway(tmp_path, provider)
    result = step(gateway, step_index=2)

    assert result.output is None and result.tool_calls == (CALL,)
    assert result.assistant.raw == response.raw and result.assistant.provider == "anthropic"
    assert result.usage == Usage(1000, 200) and result.cost_usd > 0
    assert len(provider.requests) == 1  # no validation, so no retry

    record = log.records[0]
    assert (record.stage, record.step_index, record.stop_reason) == ("matching", 2, "tool_use")
    assert record.tool_calls == [
        {"name": "get_company_facts", "arguments": {"topics": ["certifications"]}}
    ]
    assert (record.prompt_name, record.prompt_version) == ("match", "v2")


def test_the_final_answer_is_validated_with_the_one_retry(tmp_path):
    provider = FakeProvider(reply(match(status="unclear", unclear_reason=None)), reply(match()))
    gateway, log = make_gateway(tmp_path, provider)
    result = step(gateway, step_index=3)
    assert isinstance(result.output, RequirementMatch) and result.tool_calls == ()
    assert log.records[0].validation_retries == 1


def test_a_step_without_a_schema_returns_the_text(tmp_path):
    provider = FakeProvider(reply("De deadline is onbekend."))
    gateway, _ = make_gateway(tmp_path, provider)
    result = gateway.step(
        task="chat",
        prompt=PromptRef("match", "v2"),
        data=[UntrustedText("question", "Wat is de deadline?")],
        history=[],
        tools=TOOLS,
        model=HAIKU,
        redactor=Redactor(),
        context=CONTEXT,
        step_index=0,
    )
    assert (result.output, result.text) == (None, "De deadline is onbekend.")
    assert "Output format" not in provider.requests[0].system


def test_the_opening_turn_is_identical_on_every_step(tmp_path):
    provider = FakeProvider(reply("", tool_calls=(CALL,)), reply("", tool_calls=(CALL,)))
    gateway, _ = make_gateway(tmp_path, provider)
    first = step(gateway)
    step(gateway, history=[first.assistant], step_index=1)
    one, two = provider.requests
    assert (one.system, one.messages[0]) == (two.system, two.messages[0])


# --- configuration and structure -------------------------------------------------


def test_an_unpriced_model_is_refused_before_any_request(tmp_path):
    provider = FakeProvider(reply())
    gateway, log = make_gateway(tmp_path, provider)
    with pytest.raises(UnknownModelError, match="no price row"):
        gateway.call(
            task="screening",
            prompt=PromptRef("screen", "v1"),
            variables={"company": "x"},
            data=DATA,
            schema=ScreeningOutput,
            model="mistral-large-latest",
            redactor=Redactor(),
            context=CONTEXT,
        )
    assert provider.requests == [] and log.records == []


def test_redactor_and_context_are_required():
    import inspect

    from llm.client import Gateway

    for method in (Gateway.call, Gateway.step):
        parameters = inspect.signature(method).parameters
        for name in ("redactor", "context", "model", "prompt", "data"):
            assert parameters[name].default is inspect.Parameter.empty
            assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY


def imported_modules(path: Path) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def project_files() -> list[Path]:
    skip = {".venv", "tests", "build", "node_modules"}
    return [p for p in REPO.rglob("*.py") if not skip & set(p.relative_to(REPO).parts)]


def test_provider_sdks_are_imported_only_in_llm_providers():
    sdks = {"anthropic", "mistralai", "openai", "google"}
    for path in project_files():
        if path.is_relative_to(REPO / "llm" / "providers"):
            continue
        assert not sdks & imported_modules(path), path


def test_the_gateway_has_no_write_path():
    forbidden = {"crm", "sqlalchemy", "psycopg", "subprocess", "requests", "urllib", "socket"}
    for path in (REPO / "llm").rglob("*.py"):
        assert not forbidden & imported_modules(path), path
    # The gateway core does not even know the provider SDKs or how to run a tool.
    assert not {"anthropic", "mistralai", "agent"} & imported_modules(REPO / "llm" / "client.py")


def test_no_module_in_llm_imports_from_pipeline():
    # The gateway is the lower layer: pipeline code calls it, never the other way round.
    files = sorted((REPO / "llm").rglob("*.py"))
    assert len(files) >= 8 and REPO / "llm" / "verified.py" in files
    for path in files:
        assert not {"pipeline", "agent", "evals", "ingest"} & imported_modules(path), path
