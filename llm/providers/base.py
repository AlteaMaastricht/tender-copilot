"""Provider-neutral request and response types. No provider SDK is imported here."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

StopReason = Literal["end", "tool_use", "max_tokens", "refusal", "other"]
RETRYABLE_STATUS = frozenset({408, 409, 429})


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class Message:
    """One turn. A user turn holds text or tool results; an assistant turn text or tool calls.

    `raw` is the provider's own assistant payload. It is replayed unchanged, because some
    providers reject a conversation whose earlier assistant turns were edited.
    """

    role: Literal["user", "assistant"]
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    raw: Any = None
    provider: str | None = None  # which provider produced `raw`


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0  # prompt tokens not read from or written to a cache
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )


@dataclass(frozen=True, slots=True)
class ProviderRequest:
    model: str
    system: str
    messages: tuple[Message, ...]
    tools: tuple[ToolSpec, ...] = ()
    max_tokens: int = 16000
    effort: str | None = None
    timeout: float = 120.0


@dataclass(frozen=True, slots=True)
class ProviderResponse:
    text: str
    stop_reason: StopReason
    usage: Usage
    model: str
    tool_calls: tuple[ToolCall, ...] = ()
    request_id: str | None = None
    raw: Any = None


class ProviderError(Exception):
    """Any failure talking to a provider. Adapters map every SDK exception to this."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retryable: bool | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        # No status code means the request never got an answer: connection error or timeout.
        self.retryable = is_retryable_status(status_code) if retryable is None else retryable


class MissingKeyError(RuntimeError):
    """An API key is not set. The message names the variable and never holds a value."""

    def __init__(self, variable: str) -> None:
        super().__init__(f"{variable} is not set; add it to .env")
        self.variable = variable


def parse_retry_after(value: str | None) -> float | None:
    """A `retry-after` header in seconds. Dates and junk give None (use normal backoff)."""
    try:
        seconds = float(value) if value is not None else None
    except ValueError:
        return None
    return seconds if seconds is not None and seconds >= 0 else None


def is_retryable_status(status_code: int | None) -> bool:
    return status_code is None or status_code in RETRYABLE_STATUS or status_code >= 500


class Provider(Protocol):
    name: str

    def send(self, request: ProviderRequest) -> ProviderResponse: ...
