"""The cost cap and the dry-run estimate (docs/EVALS.md section 6).

`RunLog` is the call log an eval run hands to the gateway. It adds up what every call was
billed, failed calls included, so the runner can stop before the next case once the cap is
reached.

`estimate_run` counts the calls a run would make and prices them from `llm/prices.py`
without building a gateway, so no model can be called. Its token figures are first guesses,
listed in the output, to be replaced by measured values after the first real run.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from agent.loop import CHAT_MAX_STEPS, CHAT_TOKEN_BUDGET, MATCH_MAX_STEPS, MATCH_TOKEN_BUDGET
from evals.labels import Labels
from evals.metrics import FORMAL
from evals.snapshot import READABLE, Snapshot, SnapshotTender, as_of_date, load_tender
from ingest.parsing import start_of_day
from llm.log import CallLog, LlmCallRecord
from llm.prices import PRICES, ModelPrice
from llm.prompts import PromptRef, schema_json
from llm.providers.base import Usage
from pipeline.filter import apply_hard_filter
from pipeline.prompt_specs import ALL, PromptSpec, company_sections

# --- cost cap ------------------------------------------------------------------


class RunLog:
    """A `CallLog` that keeps the run's records and the money spent so far."""

    def __init__(self, cap: Decimal | None = None, sink: CallLog | None = None) -> None:
        self.cap = cap
        self.records: list[LlmCallRecord] = []
        self.spent = Decimal(0)
        self._sink = sink

    def write(self, record: LlmCallRecord) -> None:
        self.records.append(record)
        self.spent += record.cost_usd
        if self._sink is not None:
            self._sink.write(record)

    @property
    def cap_reached(self) -> bool:
        return self.cap is not None and self.spent >= self.cap


# --- dry-run estimate ----------------------------------------------------------

CHARS_PER_TOKEN = 3  # conservative for Dutch text; no tokenizer dependency
GATEWAY_BLOCK_CHARS = 1_500  # the data rule and schema instructions around every prompt
SCREEN_NOTICE_ALLOWANCE_CHARS = 2_000  # description and CPV labels, not parsed yet
SCREEN_OUTPUT_TOKENS = 300
KEY_DOC_TYPES = frozenset({"guideline", "requirements", "qa"})  # read in full (2.5)
MAP_SECTION_CHARS = 12_000  # until rag/chunk.py gives real sections
MAP_OUTPUT_TOKENS = 1_500
REDUCE_OUTPUT_TOKENS = 3_000
DEFAULT_KNOCKOUTS = 10  # per tender without knock-out labels
MATCH_EXPECTED_STEPS = 3
MATCH_EXPECTED_TOKENS = 20_000  # prompt tokens per requirement, over its steps
MATCH_OUTPUT_TOKENS = 400  # per step
MEMO_CALLS = 2
MEMO_INPUT_TOKENS = 15_000  # per tender, over both calls
MEMO_OUTPUT_TOKENS = 3_000
JUDGE_INPUT_TOKENS = 8_000
JUDGE_OUTPUT_TOKENS = 500
CHAT_EXPECTED_STEPS = 3
CHAT_EXPECTED_TOKENS = 20_000
CHAT_OUTPUT_TOKENS = 500
RETRY_FACTOR = 2  # upper bound: every single call needs its one validation retry

ASSUMPTIONS: dict[str, Any] = {
    "chars_per_token": CHARS_PER_TOKEN,
    "screening": "1 call per tender that passes the hard filter; prompt = prompt file, "
    f"company sections, schema, notice titles + {SCREEN_NOTICE_ALLOWANCE_CHARS} chars; "
    f"output {SCREEN_OUTPUT_TOKENS} tokens",
    "extraction_map": f"1 call per {MAP_SECTION_CHARS} chars of guideline, requirements and "
    f"Q&A text; output {MAP_OUTPUT_TOKENS} tokens",
    "extraction_reduce": f"1 call per tender; input = the map outputs; output "
    f"{REDUCE_OUTPUT_TOKENS} tokens",
    "matching": f"per non-formal knock-out (label count, else {DEFAULT_KNOCKOUTS}): expected "
    f"{MATCH_EXPECTED_STEPS} steps and {MATCH_EXPECTED_TOKENS} prompt tokens; upper bound "
    f"{MATCH_MAX_STEPS} steps and {MATCH_TOKEN_BUDGET} tokens (a run can pass its budget by "
    "one step)",
    "memo": f"{MEMO_CALLS} calls per tender; {MEMO_INPUT_TOKENS} tokens in, "
    f"{MEMO_OUTPUT_TOKENS} out",
    "judge": f"1 call per memo, only with a rubric and a judge model; {JUDGE_INPUT_TOKENS} "
    f"tokens in, {JUDGE_OUTPUT_TOKENS} out",
    "chat": f"per question: expected {CHAT_EXPECTED_STEPS} steps and {CHAT_EXPECTED_TOKENS} "
    f"prompt tokens; upper bound {CHAT_MAX_STEPS} steps and {CHAT_TOKEN_BUDGET} tokens",
    "edge_cases": "every edge-case tender that passes the hard filter is screened and "
    "analysed in full",
    "upper_bound": "every single call needs its one validation retry, and every agent loop "
    "runs to its limits",
}

SPECS: dict[str, PromptSpec] = {spec.ref.name: spec for spec in ALL}


@dataclass(slots=True)
class StageEstimate:
    calls: int = 0
    cost: Decimal = Decimal(0)
    calls_upper: int = 0
    cost_upper: Decimal = Decimal(0)


@dataclass(slots=True)
class Estimate:
    prices: Mapping[str, ModelPrice]
    stages: dict[str, StageEstimate] = field(default_factory=dict)

    def add(
        self,
        stage: str,
        model: str,
        *,
        calls: int,
        tokens_in: int,
        tokens_out: int,
        upper_calls: int | None = None,
        upper_in: int | None = None,
    ) -> None:
        """Add calls of one size. The upper bound defaults to one retry of each call."""
        if calls <= 0:
            return
        price = self.prices[model]
        entry = self.stages.setdefault(stage, StageEstimate())
        entry.calls += calls
        entry.cost += calls * price.cost(Usage(input_tokens=tokens_in, output_tokens=tokens_out))
        more = upper_calls if upper_calls is not None else calls * RETRY_FACTOR
        entry.calls_upper += more
        entry.cost_upper += more * price.cost(
            Usage(
                input_tokens=upper_in if upper_in is not None else tokens_in,
                output_tokens=tokens_out,
            )
        )

    @property
    def expected_usd(self) -> Decimal:
        return sum((stage.cost for stage in self.stages.values()), Decimal(0))

    @property
    def upper_bound_usd(self) -> Decimal:
        return sum((stage.cost_upper for stage in self.stages.values()), Decimal(0))

    def to_json(self) -> dict[str, Any]:
        return {
            "expected_usd": str(self.expected_usd),
            "upper_bound_usd": str(self.upper_bound_usd),
            "by_stage": {
                name: {
                    "calls": stage.calls,
                    "expected_usd": str(stage.cost),
                    "calls_upper": stage.calls_upper,
                    "upper_bound_usd": str(stage.cost_upper),
                }
                for name, stage in sorted(self.stages.items())
            },
            "assumptions": ASSUMPTIONS,
        }


def tokens(chars: int) -> int:
    return math.ceil(chars / CHARS_PER_TOKEN)


def prompt_chars(name: str, prompts: Mapping[str, PromptRef], prompts_dir: Path) -> int:
    """Size of one system prompt: the prompt file, its company sections, schema and blocks."""
    spec = SPECS[name]
    text = (prompts_dir / prompts[name].filename).read_text(encoding="utf-8")
    profile = company_sections(spec.company_sections) if spec.company_sections else ""
    return len(text) + len(profile) + len(schema_json(spec.schema)) + GATEWAY_BLOCK_CHARS


def knockout_tenders(labels: Labels) -> list[int]:
    """The tenders with knock-out labels, in file order. Also the memo eval's tenders."""
    return list(dict.fromkeys(row.publication_id for row in labels.knockout.rows))


def _key_document_chars(tender: SnapshotTender) -> int:
    return sum(
        len(page.text)
        for doc in tender.docs.documents
        if doc.doc_type in KEY_DOC_TYPES
        and doc.extraction_status == READABLE
        and (doc.is_current or doc.doc_type == "qa")
        for page in doc.pages
    )


def estimate_run(
    *,
    snapshot: Snapshot,
    labels: Labels,
    suites: Sequence[str],
    limit: int | None,
    stage1_model: str,
    stage2_model: str,
    judge_model: str | None,
    prompts: Mapping[str, PromptRef],
    prompts_dir: Path,
    prices: Mapping[str, ModelPrice] = PRICES,
) -> Estimate:
    """Count and price the model calls of a run. Calls no model."""
    estimate = Estimate(prices)
    cache: dict[int, SnapshotTender] = {}

    def tender(publication_id: int) -> SnapshotTender:
        if publication_id not in cache:
            cache[publication_id] = load_tender(snapshot, publication_id)
        return cache[publication_id]

    def first[T](rows: Sequence[T]) -> Sequence[T]:
        return rows[:limit] if limit is not None else rows

    def passes(publication_id: int, today: Any = None) -> bool:
        item = tender(publication_id)
        return apply_hard_filter(
            item.notice,
            sluitingsdatum=item.sluitingsdatum,
            tenderned_matched=item.tenderned is not None,
            now=start_of_day(today or as_of_date(item.notice)),
        ).passed

    screened: set[tuple[int, Any]] = set()
    extract: set[int] = set()
    match: set[int] = set()
    memo: set[int] = set()
    judged: set[int] = set()
    questions = 0

    if "screening" in suites:
        screened |= {
            (row.publication_id, None)
            for row in first(labels.screening.rows)
            if passes(row.publication_id)
        }
    if "extraction" in suites:
        extract |= {row.publication_id for row in first(labels.extraction.rows)}
    labelled = first(knockout_tenders(labels))
    if "knockout" in suites:
        extract |= set(labelled)
        match |= {
            row.publication_id
            for row in labels.knockout.rows
            if row.expected_status is not None and row.publication_id in labelled
        }
    if "memo" in suites:
        memo |= set(labelled)
        if labels.rubric is not None and judge_model is not None:
            judged |= set(labelled)
    if "edge_cases" in suites:
        for case in first(labels.edge_cases.rows):
            if case.question:
                questions += 1
            elif passes(case.publication_id, case.as_of):
                screened.add((case.publication_id, case.as_of))
                memo.add(case.publication_id)
    # A memo needs extraction and matching, where the documents can be read.
    readable = {pid for pid in memo if tender(pid).docs.readable}
    extract |= readable
    match |= readable

    screen_prompt = prompt_chars("screen", prompts, prompts_dir)
    for publication_id, _ in sorted(screened, key=str):
        notice = tender(publication_id).notice
        titles = len(notice.title or "") + sum(len(lot.title or "") for lot in notice.lots)
        estimate.add(
            "screening",
            stage1_model,
            calls=1,
            tokens_in=tokens(screen_prompt + titles + SCREEN_NOTICE_ALLOWANCE_CHARS),
            tokens_out=SCREEN_OUTPUT_TOKENS,
        )

    map_prompt = prompt_chars("extract_map", prompts, prompts_dir)
    reduce_prompt = prompt_chars("extract_reduce", prompts, prompts_dir)
    for publication_id in sorted(extract):
        sections = math.ceil(_key_document_chars(tender(publication_id)) / MAP_SECTION_CHARS)
        estimate.add(
            "extraction_map",
            stage2_model,
            calls=sections,
            tokens_in=tokens(map_prompt + MAP_SECTION_CHARS),
            tokens_out=MAP_OUTPUT_TOKENS,
        )
        estimate.add(
            "extraction_reduce",
            stage2_model,
            calls=1 if sections else 0,
            tokens_in=tokens(reduce_prompt) + sections * MAP_OUTPUT_TOKENS,
            tokens_out=REDUCE_OUTPUT_TOKENS,
        )

    for publication_id in sorted(match):
        counted = sum(
            row.publication_id == publication_id and row.kind != FORMAL
            for row in labels.knockout.rows
        )
        requirements = counted or DEFAULT_KNOCKOUTS
        estimate.add(
            "matching",
            stage2_model,
            calls=requirements * MATCH_EXPECTED_STEPS,
            tokens_in=MATCH_EXPECTED_TOKENS // MATCH_EXPECTED_STEPS,
            tokens_out=MATCH_OUTPUT_TOKENS,
            upper_calls=requirements * MATCH_MAX_STEPS,
            upper_in=MATCH_TOKEN_BUDGET // MATCH_MAX_STEPS,
        )

    estimate.add(
        "memo",
        stage2_model,
        calls=len(memo) * MEMO_CALLS,
        tokens_in=MEMO_INPUT_TOKENS // MEMO_CALLS,
        tokens_out=MEMO_OUTPUT_TOKENS // MEMO_CALLS,
    )
    if judge_model is not None:
        estimate.add(
            "judge",
            judge_model,
            calls=len(judged),
            tokens_in=JUDGE_INPUT_TOKENS,
            tokens_out=JUDGE_OUTPUT_TOKENS,
        )
    estimate.add(
        "chat",
        stage2_model,
        calls=questions * CHAT_EXPECTED_STEPS,
        tokens_in=CHAT_EXPECTED_TOKENS // CHAT_EXPECTED_STEPS,
        tokens_out=CHAT_OUTPUT_TOKENS,
        upper_calls=questions * CHAT_MAX_STEPS,
        upper_in=CHAT_TOKEN_BUDGET // CHAT_MAX_STEPS,
    )
    return estimate
