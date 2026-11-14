"""Anthropic adapter. Written against `anthropic` 2.3.0 (API version 2026-10-21).

The mapping is three pure functions, so it can be tested without a client or a key. No
sampling parameters are sent (the API rejects them), `tool_choice` stays at the default
`auto` (current models reject forced tool use), and thinking stays at the model default.
SDK retries are off: the gateway owns retries.
"""

from __future__ import annotations

import os
from typing import Any

import anthropic

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

NAME = "anthropic"
KEY_ENV = "APP_ANTHROPIC_API_KEY"

_STOP_REASONS: dict[str, StopReason] = {
    "end_turn": "end",
    "stop_sequence": "end",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
}  # pause_turn and model_context_window_exceeded become "other"


def build_params(request: ProviderRequest) -> dict[str, Any]:
    """Keyword arguments for `client.messages.create`."""
    params: dict[str, Any] = {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "system": request.system,
        "messages": [_message(m) for m in request.messages],
        "timeout": request.timeout,
    }
    if request.tools:
        params["tools"] = [
            {"name": t.name, "description": t.description, "input_schema": t.input_schema}
            for t in request.tools
        ]
    if request.effort is not None:
        params["output_config"] = {"effort": request.effort}
    return params


def _message(message: Message) -> dict[str, Any]:
    if message.role == "assistant":
        if message.provider == NAME and message.raw is not None:
            # The provider's own content blocks, unchanged. Thinking blocks must be replayed
            # exactly, or the API rejects the conversation.
            return {"role": "assistant", "content": message.raw}
        blocks: list[dict[str, Any]] = []
        if message.text:
            blocks.append({"type": "text", "text": message.text})
        blocks += [
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
            for c in message.tool_calls
        ]
        return {"role": "assistant", "content": blocks}
    if not message.tool_results:
        return {"role": "user", "content": message.text}
    # All results of one assistant turn go back in a single user message.
    content: list[dict[str, Any]] = [
        {
            "type": "tool_result",
            "tool_use_id": r.tool_call_id,
            "content": r.content,
            "is_error": r.is_error,
        }
        for r in message.tool_results
    ]
    if message.text:
        content.append({"type": "text", "text": message.text})
    return {"role": "user", "content": content}


def parse_message(message: anthropic.types.Message) -> ProviderResponse:
    text = "".join(block.text for block in message.content if block.type == "text")
    calls = tuple(
        ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
        for block in message.content
        if block.type == "tool_use"
    )
    usage = message.usage
    return ProviderResponse(
        text=text,
        tool_calls=calls,
        stop_reason=_STOP_REASONS.get(message.stop_reason or "", "other"),
        usage=Usage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_input_tokens or 0,
            cache_write_tokens=usage.cache_creation_input_tokens or 0,
        ),
        model=message.model,
        request_id=getattr(message, "_request_id", None),
        raw=message.content,
    )


def to_provider_error(exc: Exception) -> ProviderError:
    if isinstance(exc, anthropic.APIStatusError):
        return ProviderError(
            exc.message,
            status_code=exc.status_code,
            retry_after=parse_retry_after(exc.response.headers.get("retry-after")),
        )
    if isinstance(exc, anthropic.APIConnectionError):  # includes APITimeoutError
        return ProviderError(f"{type(exc).__name__}: connection failed or timed out")
    return ProviderError(f"{type(exc).__name__}: {exc}", retryable=False)


class AnthropicProvider:
    name = NAME

    def __init__(self) -> None:
        self._client: anthropic.Anthropic | None = None

    def _get_client(self) -> anthropic.Anthropic:
        if self._client is None:
            key = os.environ.get(KEY_ENV)
            if not key:
                raise MissingKeyError(KEY_ENV)
            self._client = anthropic.Anthropic(api_key=key, max_retries=0)
        return self._client

    def send(self, request: ProviderRequest) -> ProviderResponse:
        client = self._get_client()
        try:
            message = client.messages.create(**build_params(request))
        except anthropic.AnthropicError as exc:
            # `from None`: the SDK exception holds the request, and with it the auth header.
            raise to_provider_error(exc) from None
        return parse_message(message)
