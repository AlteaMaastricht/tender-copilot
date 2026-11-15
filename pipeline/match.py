"""Requirement matching: one agent loop per knock-out criterion (ARCHITECTURE.md 2.5).

When the loop stops without an answer (a limit, tool errors, a failed model call), the match
is built here in code as `unclear`. An unclear knock-out makes the recommendation
`needs_review` (`pipeline/memo.py`), so a limit never turns into a bid or a no-bid.
"""

from __future__ import annotations

from agent.loop import MATCH_MAX_STEPS, MATCH_TOKEN_BUDGET, LoopResult, run_loop
from agent.tools import MATCHING_TOOLS, ToolBackend
from llm.client import CallContext, Gateway
from llm.prompts import PromptRef, UntrustedText
from llm.redact import Redactor
from pipeline.prompt_specs import MATCH
from pipeline.schemas import UNMATCHED_KINDS, KnockoutCriterion, RequirementMatch, UnclearReason

MATCH_PROMPT = MATCH.ref
# The message of the RequirementMatch validator for `met` without evidence (decision B8).
_NO_EVIDENCE = "needs at least one company_evidence"

_CAUSES: dict[str, tuple[UnclearReason, str]] = {
    "step_limit": ("step_limit", f"no answer within {MATCH_MAX_STEPS} steps"),
    "token_budget": ("token_budget", f"the budget of {MATCH_TOKEN_BUDGET} tokens was used up"),
    "tool_errors": ("loop_failed", "too many tool errors"),
    "model_error": ("loop_failed", "the model call failed"),
}


def needs_matching(criterion: KnockoutCriterion) -> bool:
    """Formal requirements are actions in the bid; they appear as `action_required` rows."""
    return criterion.kind not in UNMATCHED_KINDS


def match_requirement(
    criterion: KnockoutCriterion,
    *,
    gateway: Gateway,
    backend: ToolBackend,
    model: str,
    redactor: Redactor,
    context: CallContext,
    prompt: PromptRef = MATCH_PROMPT,
) -> RequirementMatch:
    if not needs_matching(criterion):
        raise ValueError(f"{criterion.requirement_id} is formal: it is an action, not matched")
    result = run_loop(
        gateway=gateway,
        task="matching",
        prompt=prompt,
        # The tender ID is the run's own, set by code. The model needs it for tool calls.
        variables={"tender_id": str(context.tender_id)},
        # The criterion was extracted from tender text, so it is passed as untrusted data.
        data=[UntrustedText("requirement", criterion.model_dump_json())],
        tools=MATCHING_TOOLS,
        backend=backend,
        final_schema=RequirementMatch,
        model=model,
        redactor=redactor,
        context=context,
        max_steps=MATCH_MAX_STEPS,
        token_budget=MATCH_TOKEN_BUDGET,
    )
    if result.stop == "final" and result.output is not None:
        # The requirement ID is owned by code, whatever the model wrote.
        return result.output.model_copy(update={"requirement_id": criterion.requirement_id})
    return unclear_match(criterion, result)


def unclear_match(criterion: KnockoutCriterion, result: LoopResult) -> RequirementMatch:
    """The match for a run that ended without an answer. Never a guess."""
    reason, cause = _CAUSES[result.stop]
    if result.stop == "model_error" and _NO_EVIDENCE in result.detail:
        reason, cause = "no_evidence_found", "the model answered 'met' without evidence, twice"
    return RequirementMatch(
        requirement_id=criterion.requirement_id,
        status="unclear",
        reasoning=f"Not assessed: {cause}. A person must check this requirement.",
        tender_citation=criterion.citation,
        company_evidence=[],
        gap=f"Not assessed: {cause}.",
        mitigation="none",
        unclear_reason=reason,
    )
