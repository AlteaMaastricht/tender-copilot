"""Mistral adapter. Written against `mistralai` 3.6.0.

The mapping is three pure functions, so it can be tested without a client or a key. The SDK
retries only when given a retry configuration; none is given, so the gateway owns retries.
No Mistral model has a price row yet, so the gateway cannot call this adapter until one is
added to `llm/prices.py` (DECISIONS.md Q4).
"""

from __future__ import annotations

import json
from typing import Any

from mistralai.client import Mistral, errors, models

from llm.env import read_key
from llm.providers.base import (
    Message,
    MissingKeyError,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    StopReason,
    ToolCall,
    Usage,
    parse_retry_after,
)

NAME = "mistral"
KEY_ENV = "MISTRAL_API_KEY"

_STOP_REASONS: dict[str, StopReason] = {
    "stop": "end",
    "tool_calls": "tool_use",
    "length": "max_tokens",
    "model_length": "max_tokens",
}  # "error" becomes "other"
# The SDK does not wrap network failures: they arrive as exceptions of its HTTP library.
_HTTP_LIBRARIES = frozenset({"httpx2", "httpcore2"})


def build_params(request: ProviderRequest) -> dict[str, Any]:
    """Keyword arguments for `client.chat.complete`."""
    messages: list[Any] = [{"role": "system", "content": request.system}]
    for message in request.messages:
        messages += _messages(message)
    params: dict[str, Any] = {
        "model": request.model,
        "messages": messages,
        "max_tokens": request.max_tokens,
        "timeout_ms": round(request.timeout * 1000),
    }
    if request.tools:
        params["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in request.tools
        ]
    if request.effort is not None:
        params["reasoning_effort"] = request.effort
    return params


def _messages(message: Message) -> list[Any]:
    """One neutral turn can become several Mistral messages: one per tool result."""
    if message.role == "assistant":
        if message.provider == NAME and message.raw is not None:
            return [message.raw]  # the SDK's own AssistantMessage, unchanged
        turn: dict[str, Any] = {"role": "assistant", "content": message.text}
        if message.tool_calls:
            turn["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
                }
                for c in message.tool_calls
            ]
        return [turn]
    out: list[Any] = [
        {
            "role": "tool",
            "tool_call_id": r.tool_call_id,
            "name": r.name,
            # Mistral has no error flag on a tool result, so the content carries it.
            "content": f"[tool error]\n{r.content}" if r.is_error else r.content,
        }
        for r in message.tool_results
    ]
    if message.text or not out:
        out.append({"role": "user", "content": message.text})
    return out


def parse_response(response: models.ChatCompletionResponse) -> ProviderResponse:
    if not response.choices or response.choices[0].message is None:
        raise ProviderError("Mistral returned no message", retryable=False)
    choice = response.choices[0]
    message = choice.message
    content = message.content
    if isinstance(content, str):
        text = content
    else:  # a list of chunks, or nothing
        text = "".join(getattr(chunk, "text", "") or "" for chunk in content or [])
    calls = []
    for index, call in enumerate(message.tool_calls or []):
        arguments = call.function.arguments
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                raise ProviderError(
                    f"tool call {call.function.name!r} had arguments that are not valid JSON",
                    retryable=False,
                ) from None
        if not isinstance(arguments, dict):
            raise ProviderError(
                f"tool call {call.function.name!r} had arguments that are not an object",
                retryable=False,
            )
        # The SDK fills a missing id with the string "null".
        call_id = call.id if call.id and call.id != "null" else f"call_{index}"
        calls.append(ToolCall(call_id, call.function.name, arguments))
    usage = response.usage
    return ProviderResponse(
        text=text,
        tool_calls=tuple(calls),
        stop_reason=_STOP_REASONS.get(str(choice.finish_reason), "other"),
        usage=Usage(
            input_tokens=usage.prompt_tokens or 0, output_tokens=usage.completion_tokens or 0
        ),
        model=response.model,
        request_id=response.id,
        raw=message,
    )


def to_provider_error(exc: Exception) -> ProviderError:
    if isinstance(exc, errors.MistralError):
        return ProviderError(
            exc.message,
            status_code=exc.status_code,
            retry_after=parse_retry_after(exc.headers.get("retry-after")),
        )
    module = type(exc).__module__.split(".")[0]
    if isinstance(exc, errors.NoResponseError) or module in _HTTP_LIBRARIES:
        return ProviderError(f"{type(exc).__name__}: connection failed or timed out")
    return ProviderError(f"{type(exc).__name__}: {exc}", retryable=False)


class MistralProvider:
    name = NAME

    def __init__(self) -> None:
        self._client: Mistral | None = None

    def _get_client(self) -> Mistral:
        if self._client is None:
            key = read_key(KEY_ENV)
            if not key:
                raise MissingKeyError(KEY_ENV)
            self._client = Mistral(api_key=key)  # no retry_config: the SDK does not retry
        return self._client

    def send(self, request: ProviderRequest) -> ProviderResponse:
        client = self._get_client()
        params = build_params(request)
        try:
            response = client.chat.complete(**params)
        except ProviderError:
            raise
        except Exception as exc:
            error = to_provider_error(exc)
            if isinstance(exc, errors.MistralError) or error.retryable:
                # `from None`: the SDK exception holds the request, and with it the auth header.
                raise error from None
            raise  # a bug in our own code, not a provider failure
        return parse_response(response)
