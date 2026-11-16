"""Which prompt each step uses: file, stage, output schema and trusted variables.

One place ties a prompt file to its schema, so the pipeline modules and the tests cannot
drift apart. Variables are trusted values only: company profile text, or values that code
produces from an integer or a fixed set. Tender text never goes into a variable; it is passed
to the gateway as data.

The company profile is read from `docs/COMPANY.md` by section number until the company
knowledge base exists.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from llm.client import Stage
from llm.prompts import PromptRef
from pipeline.schemas import (
    ChatAnswer,
    ExtractionOutput,
    ExtractionPartial,
    MemoDraft,
    MemoReason,
    RequirementMatch,
    ScreeningOutput,
)

COMPANY_MD = Path(__file__).resolve().parent.parent / "docs" / "COMPANY.md"
_SECTION = re.compile(r"^## (\d+)\. ", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class PromptSpec:
    ref: PromptRef
    stage: Stage
    schema: type[BaseModel]
    variables: tuple[str, ...] = ()
    company_sections: tuple[int, ...] = ()  # sections of COMPANY.md behind `company_profile`


SCREEN = PromptSpec(
    PromptRef("screen", "v1"), "screening", ScreeningOutput, ("company_profile",), (2, 4, 5)
)
EXTRACT_MAP = PromptSpec(PromptRef("extract_map", "v1"), "extraction_map", ExtractionPartial)
EXTRACT_REDUCE = PromptSpec(
    PromptRef("extract_reduce", "v1"), "extraction_reduce", ExtractionOutput
)
MATCH = PromptSpec(PromptRef("match", "v1"), "matching", RequirementMatch, ("tender_id",))
MEMO = PromptSpec(PromptRef("memo", "v1"), "memo", MemoDraft, ("company_profile",), (2, 4, 5, 9))
MEMO_REASON = PromptSpec(
    PromptRef("memo_reason", "v1"), "memo", MemoReason, ("recommendation", "reason_codes")
)
CHAT = PromptSpec(PromptRef("chat", "v1"), "chat", ChatAnswer, ("tender_id",))

ALL: tuple[PromptSpec, ...] = (SCREEN, EXTRACT_MAP, EXTRACT_REDUCE, MATCH, MEMO, MEMO_REASON, CHAT)


def prompt_refs(versions: Mapping[str, str] | None = None) -> dict[str, PromptRef]:
    """Prompt name to the reference a run uses: the registered version, or the one given.

    The evals use this to measure another version of one prompt, for example
    `{"extract_map": "v2"}`. A name that is not registered is an error.
    """
    versions = versions or {}
    if unknown := sorted(set(versions) - {spec.ref.name for spec in ALL}):
        raise ValueError(f"unknown prompt name(s): {', '.join(unknown)}")
    return {
        spec.ref.name: PromptRef(spec.ref.name, versions.get(spec.ref.name, spec.ref.version))
        for spec in ALL
    }


def company_sections(numbers: Sequence[int], path: Path = COMPANY_MD) -> str:
    """The numbered sections of the company profile, in the order asked for."""
    text = path.read_text(encoding="utf-8")
    starts = [(int(found.group(1)), found.start()) for found in _SECTION.finditer(text)]
    sections: dict[int, str] = {}
    for index, (number, start) in enumerate(starts):
        end = starts[index + 1][1] if index + 1 < len(starts) else len(text)
        sections[number] = text[start:end].strip()
    if missing := [number for number in numbers if number not in sections]:
        raise ValueError(f"{path.name} has no section {missing}")
    return "\n\n".join(sections[number] for number in numbers)
