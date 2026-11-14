"""The LLM gateway: the single entry point for every model call.

One call does, in order: load the prompt file, wrap untrusted text as data, redact, send
(with network retries), check the stop reason, validate against the Pydantic schema (one
retry), price, log, return. The gateway never executes a tool and has no write path other
than its own call log: tool calls are handed back to the caller.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, TypeVar, get_args
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError

from llm.log import CallLog, LlmCallRecord, PayloadStore, Status
from llm.prices import PRICES, ModelPrice
from llm.prompts import (
    GATEWAY_PROMPT_VERSION,
    PROMPTS_DIR,
    PromptRef,
    UntrustedText,
    data_tag,
    load_prompt,
    schema_json,
    wrap_untrusted,
)
from llm.providers.base import (
    Message,
    Provider,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    ToolCall,
    ToolSpec,
    Usage,
)
from llm.redact import Redactor

T = TypeVar("T", bound=BaseModel)

Stage = Literal[
    "screening",
    "extraction_map",
    "extraction_reduce",
    "matching",
    "memo",
    "chat",
    "judge",
    "doc_classify",
]

MAX_ATTEMPTS = 4  # one request plus at most three network retries
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 30.0
RETRY_AFTER_CAP_SECONDS = 60.0
VALIDATION_RETRIES = 1
DEFAULT_MAX_TOKENS = 16000
DEFAULT_TIMEOUT_SECONDS = 120.0

KEY_ENV_VARS = ("APP_ANTHROPIC_API_KEY", "MISTRAL_API_KEY", "OPENAI_API_KEY")
PAYLOAD_DIR_ENV = "LLM_PAYLOAD_DIR"
_KEY_SHAPE = re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}")
_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n(.*)\n```$", re.DOTALL)


class GatewayError(Exception):
    """Base class for errors raised by the gateway itself."""


class UnknownModelError(GatewayError):
    """The model has no price row or no configured provider. Nothing was sent."""


class ModelRefusal(GatewayError):
    """The model declined to answer. Not retried."""


class OutputTruncated(GatewayError):
    """The answer hit the token limit. Not retried: the same request would fail again."""


class SchemaValidationError(GatewayError):
    """The answer failed schema validation on the first try and on the one retry."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__(f"model output failed validation {len(errors)} times")
        self.errors = errors


@dataclass(frozen=True, slots=True)
class CallContext:
    """Who is calling. Required on every call so that every call is logged."""

    run_id: UUID
    tender_id: int | None = None
    eval_run_id: int | None = None

    def __post_init__(self) -> None:
        # The tag around untrusted text is derived from the run ID, so the ID must be one a
        # document author cannot predict: a random UUID, never a date, counter or name.
        if not isinstance(self.run_id, UUID) or self.run_id.version != 4:
            raise ValueError("run_id must be a random UUID (uuid4)")

    @classmethod
    def new_run(cls, tender_id: int | None = None, eval_run_id: int | None = None) -> CallContext:
        """A context with a fresh random run ID."""
        return cls(run_id=uuid4(), tender_id=tender_id, eval_run_id=eval_run_id)


@dataclass(frozen=True, slots=True)
class Step[T: BaseModel]:
    """Result of one agent-loop step: tool calls to run, or the final answer."""

    output: T | None  # the validated final answer, when a schema was given
    text: str
    tool_calls: tuple[ToolCall, ...]
    assistant: Message  # append this to the history unchanged
    usage: Usage
    cost_usd: Decimal


@dataclass(slots=True)
class _Tally:
    usage: Usage = Usage()
    cost: Decimal = Decimal(0)
    latency_ms: int = 0
    network_retries: int = 0
    validation_retries: int = 0
    stop_reason: str | None = None


class Gateway:
    def __init__(
        self,
        providers: Mapping[str, Provider],
        log: CallLog,
        *,
        prices: Mapping[str, ModelPrice] = PRICES,
        prompts_dir: Path = PROMPTS_DIR,
        payload_store: PayloadStore | None = None,
        secrets: Iterable[str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self._providers = dict(providers)
        self._log = log
        self._prices = prices
        self._prompts_dir = prompts_dir
        if payload_store is None and os.environ.get(PAYLOAD_DIR_ENV):
            payload_store = PayloadStore(Path(os.environ[PAYLOAD_DIR_ENV]))
        self._payloads = payload_store
        # Key values are held only so they can be removed from error text before logging.
        if secrets is None:
            secrets = (os.environ.get(name, "") for name in KEY_ENV_VARS)
        self._secrets = tuple(s for s in secrets if len(s) >= 8)
        self._sleep = sleep
        self._clock = clock
        self._jitter = jitter

    # --- public entry points -------------------------------------------------------

    def call(
        self,
        *,
        task: Stage,
        prompt: PromptRef,
        data: Sequence[UntrustedText],
        schema: type[T],
        model: str,
        redactor: Redactor,
        context: CallContext,
        variables: Mapping[str, str] | None = None,
        effort: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> T:
        """One model call. Returns the validated schema object."""
        step = self._run(
            task=task,
            prompt=prompt,
            variables=variables or {},
            data=data,
            history=(),
            tools=(),
            schema=schema,
            model=model,
            redactor=redactor,
            context=context,
            step_index=0,
            effort=effort,
            max_tokens=max_tokens,
            timeout=timeout,
        )
        assert step.output is not None
        return step.output

    def step(
        self,
        *,
        task: Stage,
        prompt: PromptRef,
        data: Sequence[UntrustedText],
        history: Sequence[Message],
        tools: Sequence[ToolSpec],
        model: str,
        redactor: Redactor,
        context: CallContext,
        step_index: int,
        final_schema: type[T] | None = None,
        variables: Mapping[str, str] | None = None,
        effort: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> Step[T]:
        """One step of an agent loop. The caller runs the tools; the gateway never does.

        `history` is every turn after the opening one: the `assistant` message of each
        earlier step, each followed by a user message holding the tool results.
        """
        return self._run(
            task=task,
            prompt=prompt,
            variables=variables or {},
            data=data,
            history=history,
            tools=tools,
            schema=final_schema,
            model=model,
            redactor=redactor,
            context=context,
            step_index=step_index,
            effort=effort,
            max_tokens=max_tokens,
            timeout=timeout,
        )

    # --- the one send path ---------------------------------------------------------

    def _run(
        self,
        *,
        task: str,
        prompt: PromptRef,
        variables: Mapping[str, str],
        data: Sequence[UntrustedText],
        history: Sequence[Message],
        tools: Sequence[ToolSpec],
        schema: type[T] | None,
        model: str,
        redactor: Redactor,
        context: CallContext,
        step_index: int,
        effort: str | None,
        max_tokens: int,
        timeout: float,
    ) -> Step[T]:
        if task not in get_args(Stage):
            raise ValueError(f"unknown task: {task!r}")
        if not data:
            raise ValueError("data must hold at least one UntrustedText")
        price = self._prices.get(model)
        if price is None:
            raise UnknownModelError(f"no price row for model {model!r}; add one to llm/prices.py")
        provider = self._providers.get(price.provider)
        if provider is None:
            raise UnknownModelError(f"no provider configured for {price.provider!r}")

        tag = data_tag(context.run_id)
        system = redactor.redact(self._system_prompt(prompt, variables, schema, tag))
        opening = Message("user", text=redactor.redact(wrap_untrusted(data, tag)))
        messages = [opening, *(self._outbound(m, redactor, tag) for m in history)]

        tally = _Tally()
        status: Status = "ok"
        error: str | None = None
        errors: list[str] = []
        response: ProviderResponse | None = None
        exchanges: list[dict[str, Any]] = []
        try:
            while True:
                request = ProviderRequest(
                    model=model,
                    system=system,
                    messages=tuple(messages),
                    tools=tuple(tools),
                    max_tokens=max_tokens,
                    effort=effort,
                    timeout=timeout,
                )
                response = self._send(provider, request, tally)
                tally.usage += response.usage
                tally.cost += price.cost(response.usage)
                tally.stop_reason = response.stop_reason
                exchanges.append(_exchange(request, response, redactor))

                if response.stop_reason == "refusal":
                    status = "refused"
                    raise ModelRefusal(f"{model} declined the request")
                if response.stop_reason == "max_tokens":
                    status = "truncated"
                    raise OutputTruncated(f"{model} hit the limit of {max_tokens} output tokens")

                assistant = Message(
                    "assistant",
                    text=response.text,
                    tool_calls=response.tool_calls,
                    raw=response.raw,
                    provider=provider.name,
                )
                if response.tool_calls or schema is None:
                    return self._step(None, response, assistant, tally)
                try:
                    output = schema.model_validate_json(_json_text(response.text))
                except ValidationError as exc:
                    errors.append(_validation_errors(exc))
                    if tally.validation_retries >= VALIDATION_RETRIES:
                        status = "validation_failed"
                        raise SchemaValidationError(errors) from None
                    tally.validation_retries += 1
                    retry_text = self._gateway_prompt("gateway_retry", {"errors": errors[-1]})
                    messages += [assistant, Message("user", text=retry_text)]
                    continue
                return self._step(output, response, assistant, tally)
        except ProviderError as exc:
            status = "provider_error"
            code = f" {exc.status_code}" if exc.status_code is not None else ""
            error = self._scrub(f"{type(exc).__name__}{code}: {exc}")
            raise
        except GatewayError as exc:
            error = self._scrub(f"{type(exc).__name__}: {exc}")
            raise
        except Exception as exc:  # e.g. a missing key: never log a failed call as "ok"
            status = "provider_error"
            error = self._scrub(f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._write_log(
                task, prompt, model, provider.name, context, step_index, tally, status, error,
                response, redactor, exchanges,
            )  # fmt: skip

    def _send(
        self, provider: Provider, request: ProviderRequest, tally: _Tally
    ) -> ProviderResponse:
        """Send with network retries. Separate from, and not counted as, the validation retry."""
        for attempt in range(MAX_ATTEMPTS):
            started = self._clock()
            try:
                return provider.send(request)
            except ProviderError as exc:
                if not exc.retryable or attempt == MAX_ATTEMPTS - 1:
                    raise
                tally.network_retries += 1
                if exc.retry_after is not None:
                    delay = min(max(exc.retry_after, 0.0), RETRY_AFTER_CAP_SECONDS)
                else:
                    ceiling = min(BACKOFF_BASE_SECONDS * 2**attempt, BACKOFF_CAP_SECONDS)
                    delay = ceiling * (0.5 + self._jitter() / 2)
                self._sleep(delay)
            finally:
                tally.latency_ms += round((self._clock() - started) * 1000)
        raise AssertionError("unreachable")

    # --- helpers -------------------------------------------------------------------

    def _gateway_prompt(self, name: str, variables: Mapping[str, str]) -> str:
        return load_prompt(PromptRef(name, GATEWAY_PROMPT_VERSION), variables, self._prompts_dir)

    def _system_prompt(
        self, prompt: PromptRef, variables: Mapping[str, str], schema: type[T] | None, tag: str
    ) -> str:
        parts = [
            load_prompt(prompt, variables, self._prompts_dir),
            self._gateway_prompt("gateway_data_rule", {"tag": tag}),
        ]
        if schema is not None:
            parts.append(self._gateway_prompt("gateway_schema", {"schema": schema_json(schema)}))
        return "\n\n".join(parts)

    @staticmethod
    def _outbound(message: Message, redactor: Redactor, tag: str) -> Message:
        """Prepare one history turn for sending."""
        if message.role == "assistant":
            # Sent back unchanged: it was produced from redacted input, and providers reject
            # conversations whose earlier assistant turns were edited.
            return message
        results = tuple(
            replace(
                result,
                content=redactor.redact(
                    wrap_untrusted([UntrustedText(f"tool:{result.name}", result.content)], tag)
                ),
            )
            for result in message.tool_results
        )
        return replace(message, text=redactor.redact(message.text), tool_results=results)

    @staticmethod
    def _step(
        output: T | None, response: ProviderResponse, assistant: Message, tally: _Tally
    ) -> Step[T]:
        return Step(
            output=output,
            text=response.text,
            tool_calls=response.tool_calls,
            assistant=assistant,
            usage=tally.usage,
            cost_usd=tally.cost,
        )

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "[KEY]")
        return _KEY_SHAPE.sub("[KEY]", text)[:300]

    def _write_log(
        self,
        task: str,
        prompt: PromptRef,
        model: str,
        provider_name: str,
        context: CallContext,
        step_index: int,
        tally: _Tally,
        status: Status,
        error: str | None,
        response: ProviderResponse | None,
        redactor: Redactor,
        exchanges: list[dict[str, Any]],
    ) -> None:
        calls = response.tool_calls if response is not None and status == "ok" else ()
        payload_path = None
        if self._payloads is not None and exchanges:
            payload_path = self._payloads.write({"stage": task, "exchanges": exchanges})
        self._log.write(
            LlmCallRecord(
                run_id=context.run_id,
                eval_run_id=context.eval_run_id,
                tender_id=context.tender_id,
                stage=task,
                provider=provider_name,
                model=model,
                prompt_name=prompt.name,
                prompt_version=prompt.version,
                step_index=step_index,
                input_tokens=tally.usage.prompt_tokens,
                output_tokens=tally.usage.output_tokens,
                cached_tokens=tally.usage.cache_read_tokens,
                cost_usd=tally.cost,
                latency_ms=tally.latency_ms,
                tool_calls=[_redacted_call(c, redactor) for c in calls],
                stop_reason=tally.stop_reason,
                validation_retries=tally.validation_retries,
                network_retries=tally.network_retries,
                status=status,
                error=error,
                payload_path=payload_path,
            )
        )


def _json_text(text: str) -> str:
    """The JSON object in a reply: strip a code fence and anything around the braces."""
    text = text.strip()
    if fenced := _FENCE.match(text):
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else text


def _validation_errors(exc: ValidationError) -> str:
    """Location, message and type only: the rejected values are not echoed back."""
    return json.dumps(
        exc.errors(include_url=False, include_input=False, include_context=False),
        ensure_ascii=False,
        default=str,
    )[:4000]


def _redacted_call(call: ToolCall, redactor: Redactor) -> dict[str, Any]:
    arguments = json.loads(redactor.redact(json.dumps(call.arguments, ensure_ascii=False)))
    return {"name": call.name, "arguments": arguments}


def _exchange(
    request: ProviderRequest, response: ProviderResponse, redactor: Redactor
) -> dict[str, Any]:
    """One request and response for the payload store. Everything in it is redacted text."""
    return {
        "model": request.model,
        "system": request.system,
        "messages": [
            {
                "role": m.role,
                "text": m.text,
                "tool_calls": [_redacted_call(c, redactor) for c in m.tool_calls],
                "tool_results": [
                    {"name": r.name, "content": r.content, "is_error": r.is_error}
                    for r in m.tool_results
                ],
            }
            for m in request.messages
        ],
        "response": {
            "text": redactor.redact(response.text),
            "tool_calls": [_redacted_call(c, redactor) for c in response.tool_calls],
            "stop_reason": response.stop_reason,
            "request_id": response.request_id,
        },
    }
