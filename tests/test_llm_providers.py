"""Provider adapters: pure mapping on locally built SDK objects. No network, no real keys."""

from __future__ import annotations

import anthropic
import httpx2
import pytest
from mistralai.client import errors as mistral_errors
from mistralai.client import models as mistral_models

from llm.prices import PRICES
from llm.prompts import PromptRef, UntrustedText
from llm.providers import anthropic as anthropic_adapter
from llm.providers import default_providers
from llm.providers import mistral as mistral_adapter
from llm.providers.base import (
    Message,
    MissingKeyError,
    ProviderError,
    ProviderRequest,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
    parse_retry_after,
)
from llm.redact import Redactor
from pipeline.schemas import ScreeningOutput
from tests.fakes import CONTEXT, HAIKU, make_gateway

TOOL = ToolSpec(
    "get_company_facts",
    "Return exact structured facts about the company.",
    {"type": "object", "properties": {"topics": {"type": "array"}}, "required": ["topics"]},
)
CALL = ToolCall("call_1", "get_company_facts", {"topics": ["certifications"]})
RESULTS = (
    ToolResult("call_1", "get_company_facts", "ISO 27001 geldig tot 2028"),
    ToolResult("call_2", "search_company_kb", "geen resultaat", is_error=True),
)
FORBIDDEN_PARAMS = {"temperature", "top_p", "top_k", "tool_choice", "thinking", "stream"}


def request(*messages: Message, **kwargs) -> ProviderRequest:
    return ProviderRequest(
        model=kwargs.pop("model", HAIKU),
        system="You match requirements.",
        messages=messages or (Message("user", text="KO-01"),),
        **kwargs,
    )


def http_response(status: int, url: str, headers: dict | None = None) -> httpx2.Response:
    return httpx2.Response(status, headers=headers, text="{}", request=httpx2.Request("POST", url))


# --- shared helpers ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "seconds"),
    [("7", 7.0), ("0.5", 0.5), (None, None), ("soon", None), ("-3", None)],
)
def test_parse_retry_after(header, seconds):
    assert parse_retry_after(header) == seconds
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None


def test_default_providers_match_the_price_table():
    providers = default_providers()
    assert {name: p.name for name, p in providers.items()} == {
        "anthropic": "anthropic",
        "mistral": "mistral",
    }
    assert {price.provider for price in PRICES.values()} <= set(providers)


@pytest.mark.parametrize(
    ("provider", "variable"),
    [
        (anthropic_adapter.AnthropicProvider(), "APP_ANTHROPIC_API_KEY"),
        (mistral_adapter.MistralProvider(), "MISTRAL_API_KEY"),
    ],
)
def test_a_missing_key_names_the_variable_and_sends_nothing(monkeypatch, provider, variable):
    monkeypatch.delenv(variable, raising=False)
    with pytest.raises(MissingKeyError, match=variable) as caught:
        provider.send(request())
    assert caught.value.variable == variable


def test_only_the_app_key_is_used_and_sdk_retries_are_off(monkeypatch):
    monkeypatch.setenv("APP_ANTHROPIC_API_KEY", "fake-app-key-for-tests")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-other-key")
    client = anthropic_adapter.AnthropicProvider()._get_client()
    assert client.api_key == "fake-app-key-for-tests" and client.max_retries == 0

    monkeypatch.setenv("MISTRAL_API_KEY", "fake-mistral-key-for-tests")
    mistral = mistral_adapter.MistralProvider()._get_client()
    assert not mistral.sdk_configuration.retry_config  # unset: the SDK does not retry


def test_a_missing_key_is_logged_as_a_failure_without_any_key_text(tmp_path, monkeypatch):
    monkeypatch.delenv("APP_ANTHROPIC_API_KEY", raising=False)
    gateway, log = make_gateway(tmp_path, anthropic_adapter.AnthropicProvider())
    with pytest.raises(MissingKeyError):
        gateway.call(
            task="screening",
            prompt=PromptRef("screen", "v1"),
            variables={"company": "Datalink B.V."},
            data=[UntrustedText("notice", "Dataplatform")],
            schema=ScreeningOutput,
            model=HAIKU,
            redactor=Redactor(),
            context=CONTEXT,
        )
    record = log.records[0]
    assert record.status == "provider_error"
    assert record.error == "MissingKeyError: APP_ANTHROPIC_API_KEY is not set; add it to .env"


# --- Anthropic: request ------------------------------------------------------------


def test_anthropic_minimal_request():
    params = anthropic_adapter.build_params(request())
    assert params == {
        "model": HAIKU,
        "max_tokens": 16000,
        "system": "You match requirements.",
        "messages": [{"role": "user", "content": "KO-01"}],
        "timeout": 120.0,
    }
    assert not FORBIDDEN_PARAMS & set(params)


def test_anthropic_tools_and_effort():
    params = anthropic_adapter.build_params(request(tools=(TOOL,), effort="low", max_tokens=512))
    assert params["tools"] == [
        {
            "name": "get_company_facts",
            "description": "Return exact structured facts about the company.",
            "input_schema": TOOL.input_schema,
        }
    ]
    assert params["output_config"] == {"effort": "low"} and params["max_tokens"] == 512
    assert not FORBIDDEN_PARAMS & set(params)


def test_anthropic_replays_its_own_assistant_turn_unchanged():
    blocks = anthropic_message().content
    turn = Message("assistant", text="x", tool_calls=(CALL,), raw=blocks, provider="anthropic")
    params = anthropic_adapter.build_params(request(Message("user", text="KO-01"), turn))
    assert params["messages"][1] == {"role": "assistant", "content": blocks}
    assert params["messages"][1]["content"] is blocks  # thinking block included, untouched


def test_anthropic_rebuilds_an_assistant_turn_from_another_provider():
    turn = Message("assistant", text="Ik zoek op.", tool_calls=(CALL,), raw=object(), provider="x")
    [built] = anthropic_adapter.build_params(request(turn))["messages"]
    assert built == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Ik zoek op."},
            {
                "type": "tool_use",
                "id": "call_1",
                "name": "get_company_facts",
                "input": {"topics": ["certifications"]},
            },
        ],
    }


def test_anthropic_sends_all_tool_results_in_one_user_message():
    [built] = anthropic_adapter.build_params(request(Message("user", tool_results=RESULTS)))[
        "messages"
    ]
    assert built["role"] == "user"
    assert built["content"] == [
        {
            "type": "tool_result",
            "tool_use_id": "call_1",
            "content": "ISO 27001 geldig tot 2028",
            "is_error": False,
        },
        {
            "type": "tool_result",
            "tool_use_id": "call_2",
            "content": "geen resultaat",
            "is_error": True,
        },
    ]


# --- Anthropic: response and errors ------------------------------------------------


def anthropic_message(**overrides) -> anthropic.types.Message:
    data = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": HAIKU,
        "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": "Ik zoek "},
            {"type": "text", "text": "het op."},
            {
                "type": "tool_use",
                "id": "tu_1",
                "name": "get_company_facts",
                "input": {"topics": ["x"]},
            },
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 900,
            "cache_creation_input_tokens": 50,
        },
    }
    return anthropic.types.Message.model_validate({**data, **overrides})


def test_anthropic_response_is_mapped():
    message = anthropic_message()
    response = anthropic_adapter.parse_message(message)
    assert response.text == "Ik zoek het op."
    assert response.tool_calls == (ToolCall("tu_1", "get_company_facts", {"topics": ["x"]}),)
    assert response.stop_reason == "tool_use" and response.model == HAIKU
    assert response.usage == Usage(100, 20, cache_read_tokens=900, cache_write_tokens=50)
    assert response.raw is message.content


def test_anthropic_usage_without_cache_fields():
    usage = {"input_tokens": 10, "output_tokens": 5}
    response = anthropic_adapter.parse_message(anthropic_message(usage=usage))
    assert response.usage == Usage(10, 5)


@pytest.mark.parametrize(
    ("native", "neutral"),
    [
        ("end_turn", "end"),
        ("stop_sequence", "end"),
        ("tool_use", "tool_use"),
        ("max_tokens", "max_tokens"),
        ("refusal", "refusal"),
        ("pause_turn", "other"),
        ("model_context_window_exceeded", "other"),
        (None, "other"),
    ],
)
def test_anthropic_stop_reasons(native, neutral):
    message = anthropic_message(stop_reason=native)
    assert anthropic_adapter.parse_message(message).stop_reason == neutral


ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"


@pytest.mark.parametrize(
    ("error_class", "status", "retryable"),
    [
        (anthropic.BadRequestError, 400, False),
        (anthropic.AuthenticationError, 401, False),
        (anthropic.NotFoundError, 404, False),
        (anthropic.RateLimitError, 429, True),
        (anthropic.InternalServerError, 500, True),
        (anthropic.APIStatusError, 529, True),
    ],
)
def test_anthropic_status_errors(error_class, status, retryable):
    response = http_response(status, ANTHROPIC_URL, {"retry-after": "7"})
    error = anthropic_adapter.to_provider_error(error_class("boom", response=response, body=None))
    assert (error.status_code, error.retryable, error.retry_after) == (status, retryable, 7.0)
    assert str(error) == "boom"


def test_anthropic_connection_errors_and_timeouts_are_retryable():
    http_request = httpx2.Request("POST", ANTHROPIC_URL, headers={"authorization": "Bearer SECRET"})
    for exc in (
        anthropic.APIConnectionError(request=http_request),
        anthropic.APITimeoutError(http_request),
    ):
        error = anthropic_adapter.to_provider_error(exc)
        assert (error.status_code, error.retryable) == (None, True)
        assert "SECRET" not in str(error)


def test_anthropic_other_sdk_errors_are_not_retried():
    error = anthropic_adapter.to_provider_error(anthropic.AnthropicError("bad config"))
    assert (error.status_code, error.retryable) == (None, False)


# --- Mistral: request --------------------------------------------------------------


def test_mistral_minimal_request():
    params = mistral_adapter.build_params(request(model="mistral-model"))
    assert params == {
        "model": "mistral-model",
        "messages": [
            {"role": "system", "content": "You match requirements."},
            {"role": "user", "content": "KO-01"},
        ],
        "max_tokens": 16000,
        "timeout_ms": 120000,
    }
    assert not {"temperature", "top_p", "tool_choice", "retries", "stream"} & set(params)


def test_mistral_tools_and_effort():
    params = mistral_adapter.build_params(request(tools=(TOOL,), effort="high"))
    assert params["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "get_company_facts",
                "description": "Return exact structured facts about the company.",
                "parameters": TOOL.input_schema,
            },
        }
    ]
    assert params["reasoning_effort"] == "high"


def test_mistral_tool_turns():
    own = mistral_response().choices[0].message
    history = (
        Message("user", text="KO-01"),
        Message("assistant", tool_calls=(CALL,), raw=own, provider="mistral"),
        Message("user", tool_results=RESULTS),
        Message(
            "assistant", text="Nog een.", tool_calls=(CALL,), raw=object(), provider="anthropic"
        ),
    )
    messages = mistral_adapter.build_params(request(*history))["messages"]
    assert messages[2] is own  # its own assistant turn, unchanged
    assert messages[3:5] == [
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "get_company_facts",
            "content": "ISO 27001 geldig tot 2028",
        },
        {
            "role": "tool",
            "tool_call_id": "call_2",
            "name": "search_company_kb",
            "content": "[tool error]\ngeen resultaat",
        },
    ]
    assert messages[5] == {
        "role": "assistant",
        "content": "Nog een.",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "get_company_facts",
                    "arguments": '{"topics": ["certifications"]}',
                },
            }
        ],
    }
    assert len(messages) == 6


def test_mistral_request_is_accepted_by_the_sdk_request_model():
    history = (Message("user", text="KO-01"), Message("user", tool_results=RESULTS))
    params = mistral_adapter.build_params(request(*history, tools=(TOOL,), model="m"))
    params.pop("timeout_ms")
    built = mistral_models.ChatCompletionRequest.model_validate(params)
    assert [m.role for m in built.messages] == ["system", "user", "tool", "tool"]


# --- Mistral: response and errors --------------------------------------------------


def mistral_response(**choice) -> mistral_models.ChatCompletionResponse:
    message = {
        "role": "assistant",
        "content": "Ik zoek het op.",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "get_company_facts", "arguments": '{"topics": ["x"]}'},
            }
        ],
    }
    return mistral_models.ChatCompletionResponse.model_validate(
        {
            "id": "cmpl-1",
            "object": "chat.completion",
            "model": "mistral-model",
            "created": 1,
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": message, **choice}],
        }
    )


def test_mistral_response_is_mapped():
    native = mistral_response()
    response = mistral_adapter.parse_response(native)
    assert response.text == "Ik zoek het op."
    assert response.tool_calls == (ToolCall("c1", "get_company_facts", {"topics": ["x"]}),)
    assert (response.stop_reason, response.model, response.request_id) == (
        "tool_use",
        "mistral-model",
        "cmpl-1",
    )
    assert response.usage == Usage(100, 20)
    assert response.raw is native.choices[0].message


def test_mistral_content_as_chunks_and_arguments_as_object():
    message = {
        "role": "assistant",
        "content": [{"type": "text", "text": "Deel 1. "}, {"type": "text", "text": "Deel 2."}],
        "tool_calls": [{"function": {"name": "f", "arguments": {"a": 1}}}],
    }
    response = mistral_adapter.parse_response(mistral_response(message=message))
    assert response.text == "Deel 1. Deel 2."
    assert response.tool_calls == (ToolCall("call_0", "f", {"a": 1}),)


@pytest.mark.parametrize(
    ("native", "neutral"),
    [
        ("stop", "end"),
        ("tool_calls", "tool_use"),
        ("length", "max_tokens"),
        ("model_length", "max_tokens"),
        ("error", "other"),
    ],
)
def test_mistral_stop_reasons(native, neutral):
    message = {"role": "assistant", "content": "ok"}
    response = mistral_adapter.parse_response(
        mistral_response(finish_reason=native, message=message)
    )
    assert response.stop_reason == neutral and response.tool_calls == ()


@pytest.mark.parametrize("arguments", ['{"topics": ', '["a list"]', '"text"'])
def test_mistral_bad_tool_arguments_are_an_error_not_a_guess(arguments):
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": arguments}}],
    }
    with pytest.raises(ProviderError) as caught:
        mistral_adapter.parse_response(mistral_response(message=message))
    assert caught.value.retryable is False


MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"


@pytest.mark.parametrize(
    ("status", "retryable"), [(400, False), (401, False), (422, False), (429, True), (503, True)]
)
def test_mistral_status_errors(status, retryable):
    response = http_response(status, MISTRAL_URL, {"retry-after": "7"})
    error = mistral_adapter.to_provider_error(mistral_errors.SDKError("boom", response))
    assert (error.status_code, error.retryable, error.retry_after) == (status, retryable, 7.0)


def test_mistral_network_failures_are_retryable():
    http_request = httpx2.Request("POST", MISTRAL_URL, headers={"authorization": "Bearer SECRET"})
    for exc in (
        httpx2.ConnectError("refused", request=http_request),
        httpx2.ReadTimeout("slow", request=http_request),
        mistral_errors.NoResponseError(),
    ):
        error = mistral_adapter.to_provider_error(exc)
        assert (error.status_code, error.retryable) == (None, True)
        assert "SECRET" not in str(error)


def test_mistral_other_errors_are_not_retried():
    error = mistral_adapter.to_provider_error(TypeError("our own bug"))
    assert error.retryable is False
