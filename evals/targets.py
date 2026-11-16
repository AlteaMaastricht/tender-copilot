"""The pipeline steps the evals call, as one explicit table.

The evals test the real pipeline code, never a copy of it. A step that is not built yet is
`None` here, and every eval that needs it reports `not_built` and names what it waits for.
Wiring a step in is one line in `default_targets`.

No step ever receives the ground truth or the contact names: a document-reading step gets
`TenderDocs` only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Literal

from evals.labels import Rubric
from evals.snapshot import TenderDocs
from ingest.ted import TedNotice
from llm.client import CallContext, Gateway
from llm.prompts import PromptRef
from llm.redact import Redactor
from pipeline.filter import FilterResult
from pipeline.schemas import (
    ChatAnswer,
    ExtractionOutput,
    MatchingOutput,
    Memo,
    ScreeningOutput,
)

SearchMode = Literal["hybrid", "vector_only"]
SEARCH_MODES: tuple[SearchMode, ...] = ("hybrid", "vector_only")


@dataclass(frozen=True, slots=True)
class StepEnv:
    """What a step needs to call a model: always through the gateway."""

    gateway: Gateway
    model: str
    prompts: Mapping[str, PromptRef]  # prompt name -> the version this run uses
    context: CallContext
    redactor: Redactor


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One retrieved chunk, in rank order."""

    document_id: int
    page_start: int
    page_end: int


SearchStep = Callable[[TenderDocs, str, int, SearchMode], Sequence[SearchHit]]
ScreenStep = Callable[[TedNotice, FilterResult, StepEnv], ScreeningOutput]
ExtractStep = Callable[[TenderDocs, StepEnv], ExtractionOutput]
MatchStep = Callable[[TenderDocs, ExtractionOutput, StepEnv], MatchingOutput]
# Notice, TenderNed closing date, documents, then extraction and matching (None when the
# documents cannot be read: the memo is then a notice-only memo).
MemoStep = Callable[
    [TedNotice, str | None, TenderDocs, ExtractionOutput | None, MatchingOutput | None, StepEnv],
    Memo,
]
ChatStep = Callable[[TenderDocs, str, StepEnv], ChatAnswer | None]  # None: the loop gave up
JudgeStep = Callable[[Memo, Rubric, StepEnv], Mapping[str, int]]  # criterion id -> score


@dataclass(frozen=True, slots=True)
class Targets:
    search: SearchStep | None = None
    screen: ScreenStep | None = None
    extract: ExtractStep | None = None
    match: MatchStep | None = None
    memo: MemoStep | None = None
    chat: ChatStep | None = None
    judge: JudgeStep | None = None

    def missing(self, names: Sequence[str]) -> list[str]:
        return [name for name in names if getattr(self, name) is None]


# What each unbuilt step waits for. Shown in the results next to `not_built`.
WAITING_FOR: dict[str, str] = {
    "search": "rag/search.py",
    "screen": "pipeline/screen.py",
    "extract": "pipeline/extract.py",
    "match": "a tool backend over the snapshot for pipeline/match.py",
    "memo": "memo assembly in pipeline/memo.py",
    "chat": "a chat step with a tool backend over the snapshot",
    "judge": "prompts/judge_v1.md and a judge step",
}
assert set(WAITING_FOR) == {f.name for f in fields(Targets)}


def default_targets() -> Targets:
    """The steps that exist today. None yet: see WAITING_FOR."""
    return Targets()
