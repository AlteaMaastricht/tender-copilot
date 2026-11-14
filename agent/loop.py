"""The hand-written agent loop, used only for requirement matching and chat.

In plain words:

1. Start with an empty history, zero tokens used and zero tool errors.
2. Ask the model for its next move (`gateway.step`), with the same run ID on every step.
3. Add the step's tokens to the total.
4. No tool calls? Then this is the final answer, already validated by the gateway. Return it.
5. Token budget reached? Stop.
6. Run each tool call. A failing tool gives the model an error result; it never crashes.
7. Three tool errors in one run? Stop.
8. Append the model's turn and the tool results to the history, and go back to 2.
9. Out of steps? Stop.

When the loop stops without a final answer it returns no output. It never guesses. The loop
does not read tool results: its decisions depend only on the model's tool calls and the three
counters, so text inside a result cannot change what the loop does.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from agent.tools import TOOLS, ToolBackend, run_tool
from llm.client import (
    CallContext,
    Gateway,
    ModelRefusal,
    OutputTruncated,
    SchemaValidationError,
    Stage,
)
from llm.prompts import PromptRef, UntrustedText
from llm.providers.base import Message, ProviderError
from llm.redact import Redactor

# Tunable limits (ARCHITECTURE.md section 4). Tokens are prompt plus output, over all steps.
MATCH_MAX_STEPS = 6
MATCH_TOKEN_BUDGET = 60_000
CHAT_MAX_STEPS = 8
CHAT_TOKEN_BUDGET = 80_000
MAX_TOOL_ERRORS = 3

StopReason = Literal["final", "step_limit", "token_budget", "tool_errors", "model_error"]


@dataclass(frozen=True, slots=True)
class LoopResult[T: BaseModel]:
    stop: StopReason
    output: T | None = None  # only when stop == "final"
    detail: str = ""  # for model_error: what failed
    steps: int = 0  # model calls made
    tokens: int = 0
    cost_usd: Decimal = Decimal(0)


def run_loop[T: BaseModel](
    *,
    gateway: Gateway,
    task: Stage,
    prompt: PromptRef,
    data: Sequence[UntrustedText],
    tools: Sequence[str],
    backend: ToolBackend,
    final_schema: type[T],
    model: str,
    redactor: Redactor,
    context: CallContext,
    max_steps: int,
    token_budget: int,
    variables: Mapping[str, str] | None = None,
    effort: str | None = None,
) -> LoopResult[T]:
    if context.tender_id is None:
        raise ValueError("an agent run needs a tender_id")
    specs = [TOOLS[name].spec for name in tools]
    history: list[Message] = []
    tokens = 0
    cost = Decimal(0)
    tool_errors = 0

    for step_index in range(max_steps):
        steps = step_index + 1
        try:
            step = gateway.step(
                task=task,
                prompt=prompt,
                variables=variables,
                data=data,
                history=history,
                tools=specs,
                final_schema=final_schema,
                model=model,
                redactor=redactor,
                context=context,
                step_index=step_index,
                effort=effort,
            )
        except (ModelRefusal, OutputTruncated, SchemaValidationError, ProviderError) as exc:
            # The gateway has logged the failed call. A reply that was billed (truncated,
            # refused, invalid) still counts; a provider error has no usage.
            if not isinstance(exc, ProviderError):
                tokens += exc.usage.prompt_tokens + exc.usage.output_tokens
                cost += exc.cost_usd
            detail = type(exc).__name__
            if isinstance(exc, SchemaValidationError):
                detail += ": " + exc.errors[-1][:500]
            elif isinstance(exc, ProviderError):
                detail += f" {exc.status_code}"
            return LoopResult("model_error", None, detail, steps, tokens, cost)

        tokens += step.usage.prompt_tokens + step.usage.output_tokens
        cost += step.cost_usd
        if not step.tool_calls:
            return LoopResult("final", step.output, "", steps, tokens, cost)
        if tokens >= token_budget:
            return LoopResult("token_budget", None, "", steps, tokens, cost)

        results = [run_tool(call, tools, backend, context.tender_id) for call in step.tool_calls]
        tool_errors += sum(result.is_error for result in results)
        if tool_errors >= MAX_TOOL_ERRORS:
            return LoopResult("tool_errors", None, "", steps, tokens, cost)
        history += [step.assistant, Message("user", tool_results=tuple(results))]

    return LoopResult("step_limit", None, "", max_steps, tokens, cost)
