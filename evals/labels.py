"""Read-only loaders for the hand-made label files (formats: docs/EVALS.md section 5).

The files in `evals/datasets/` are Arthur's ground truth. This module opens them for reading
only; nothing in `evals/` creates or changes a file there. A missing file is not an error:
its eval is reported as "no labels" and the run continues.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from evals.metrics import EXTRACTION_FIELDS, Criterion
from llm.redact import Redactor
from pipeline.schemas import KnockoutKind, MatchStatus, RequirementCategory

DOC_KEY_PATTERN = r"^[0-9a-f]{12}$"
# Identifiers and dates are not free text: they are skipped by the contact-data check.
_NOT_FREE_TEXT = frozenset(
    {"doc_key", "label_id", "question_id", "case_id", "as_of", "memo_sha256", "deadline"}
)
_CONTACT_CHECK = Redactor()


class LabelError(ValueError):
    """A label file is malformed. The message names the file and the line, never a value."""


class _Row(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    note: str | None = None  # never scored


class ScreeningLabel(_Row):
    publication_id: int
    label: Literal["relevant", "not_relevant"]
    stratum: Literal["shortlist", "reject", "filtered_out"] | None = None


class AnswerPages(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    doc_key: str = Field(pattern=DOC_KEY_PATTERN)
    pages: list[int] = Field(min_length=1)

    @model_validator(mode="after")
    def _pages_start_at_one(self) -> AnswerPages:
        if any(page < 1 for page in self.pages):
            raise ValueError("pages start at 1")
        return self


class RetrievalLabel(_Row):
    question_id: str = Field(pattern=r"^RQ-\d{2,}$")
    publication_id: int
    question: str = Field(min_length=3)
    language: Literal["nl", "en"]
    answers: list[AnswerPages] = Field(min_length=1)


class Overrides(BaseModel):
    """Corrections to the notice's values, tender level only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    deadline: datetime | None = None
    lot_count: int | None = Field(default=None, ge=1)
    estimated_value: Decimal | None = None
    framework_max_value: Decimal | None = None


class ExtractionLabel(_Row):
    publication_id: int
    skip_fields: list[Literal[EXTRACTION_FIELDS]] = []  # type: ignore[valid-type]
    overrides: Overrides = Overrides()


class KnockoutLabel(_Row):
    publication_id: int
    label_id: str = Field(pattern=r"^\d+-KO-\d{2,}$")
    doc_key: str = Field(pattern=DOC_KEY_PATTERN)
    page: int = Field(ge=1)
    category: RequirementCategory
    kind: KnockoutKind
    text: str = Field(min_length=3)
    threshold: str | None = None
    expected_status: MatchStatus | None = None

    @model_validator(mode="after")
    def _id_names_the_tender(self) -> KnockoutLabel:
        if not self.label_id.startswith(f"{self.publication_id}-"):
            raise ValueError("label_id must start with the publication_id")
        return self


EdgeCaseType = Literal[
    "cancelled",
    "passed_deadline",
    "scanned_pdf",
    "docs_behind_login",
    "off_topic_question",
    "prompt_injection",
]


class EdgeCaseLabel(_Row):
    case_id: str = Field(pattern=r"^EC-\d{2,}$")
    type: EdgeCaseType
    publication_id: int
    as_of: date | None = None
    question: str | None = None  # chat cases only
    # Keys are checked when the case is scored: an unknown key is an invalid label, not a crash.
    expect: dict[str, Any] = Field(min_length=1)


class MemoGrade(_Row):
    publication_id: int
    memo_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scores: dict[str, int] = Field(min_length=1)


class RubricCriterion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    question: str = Field(min_length=3)
    scale_max: int = Field(ge=2)
    weight: float = Field(gt=0)


class Rubric(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    criteria: list[RubricCriterion] = Field(min_length=1)

    def as_criteria(self) -> list[Criterion]:
        return [Criterion(c.id, c.scale_max, c.weight) for c in self.criteria]


@dataclass(frozen=True, slots=True)
class LabelFile[T]:
    name: str
    sha256: str | None  # None: the file does not exist
    rows: tuple[T, ...] = ()

    @property
    def missing(self) -> bool:
        return self.sha256 is None

    def summary(self) -> dict[str, Any] | None:
        """The entry for this file in the results: its hash and row count, or null."""
        return None if self.missing else {"sha256": self.sha256, "rows": len(self.rows)}


def _free_text(value: Any, key: str | None = None) -> Iterator[str]:
    if isinstance(value, str):
        if key not in _NOT_FREE_TEXT:
            yield value
    elif isinstance(value, dict):
        for name, item in value.items():
            yield from _free_text(item, name)
    elif isinstance(value, list):
        for item in value:
            yield from _free_text(item, key)


def _has_contact_data(row: Any) -> bool:
    return any(_CONTACT_CHECK.redact(text) != text for text in _free_text(row))


def _problem(exc: ValidationError) -> str:
    first = exc.errors(include_url=False, include_input=False, include_context=False)[0]
    where = ".".join(str(part) for part in first["loc"])
    return f"{where}: {first['msg']}" if where else first["msg"]


def _read(path: Path) -> tuple[bytes, str] | None:
    """The bytes of a label file and their hash. Opened for reading only."""
    if not path.is_file():
        return None
    data = path.read_bytes()
    return data, hashlib.sha256(data).hexdigest()


def load_jsonl[T: BaseModel](
    datasets_dir: Path, name: str, model: type[T], key: Callable[[T], Any]
) -> LabelFile[T]:
    found = _read(datasets_dir / name)
    if found is None:
        return LabelFile(name, None)
    data, digest = found
    rows: list[T] = []
    seen: set[Any] = set()
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        raise LabelError(f"{name}: not UTF-8") from None
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            raise LabelError(f"{name} line {number}: not valid JSON") from None
        if _has_contact_data(raw):
            raise LabelError(f"{name} line {number}: holds an email address or phone number")
        try:
            row = model.model_validate(raw)
        except ValidationError as exc:
            raise LabelError(f"{name} line {number}: {_problem(exc)}") from None
        if key(row) in seen:
            raise LabelError(f"{name} line {number}: duplicate identifier")
        seen.add(key(row))
        rows.append(row)
    return LabelFile(name, digest, tuple(rows))


@dataclass(frozen=True, slots=True)
class Labels:
    screening: LabelFile[ScreeningLabel]
    retrieval: LabelFile[RetrievalLabel]
    extraction: LabelFile[ExtractionLabel]
    knockout: LabelFile[KnockoutLabel]
    edge_cases: LabelFile[EdgeCaseLabel]
    memo_grades: LabelFile[MemoGrade]
    rubric: Rubric | None
    rubric_sha256: str | None

    def files(self) -> tuple[LabelFile[Any], ...]:
        return (
            self.screening, self.retrieval, self.extraction, self.knockout, self.edge_cases,
            self.memo_grades,
        )  # fmt: skip

    def summary(self) -> dict[str, Any]:
        result = {file.name: file.summary() for file in self.files()}
        result[RUBRIC_FILE] = (
            None
            if self.rubric is None
            else {"sha256": self.rubric_sha256, "version": self.rubric.version}
        )
        return result


RUBRIC_FILE = "memo_rubric.json"


def _load_rubric(datasets_dir: Path) -> tuple[Rubric | None, str | None]:
    found = _read(datasets_dir / RUBRIC_FILE)
    if found is None:
        return None, None
    data, digest = found
    try:
        return Rubric.model_validate_json(data), digest
    except ValidationError as exc:
        raise LabelError(f"{RUBRIC_FILE}: {_problem(exc)}") from None


def load_all(datasets_dir: Path) -> Labels:
    rubric, rubric_sha256 = _load_rubric(datasets_dir)
    return Labels(
        screening=load_jsonl(
            datasets_dir, "screening.jsonl", ScreeningLabel, lambda r: r.publication_id
        ),
        retrieval=load_jsonl(
            datasets_dir, "retrieval.jsonl", RetrievalLabel, lambda r: r.question_id
        ),
        extraction=load_jsonl(
            datasets_dir, "extraction.jsonl", ExtractionLabel, lambda r: r.publication_id
        ),
        knockout=load_jsonl(datasets_dir, "knockout.jsonl", KnockoutLabel, lambda r: r.label_id),
        edge_cases=load_jsonl(datasets_dir, "edge_cases.jsonl", EdgeCaseLabel, lambda r: r.case_id),
        memo_grades=load_jsonl(
            datasets_dir, "memo_grades.jsonl", MemoGrade, lambda r: r.memo_sha256
        ),
        rubric=rubric,
        rubric_sha256=rubric_sha256,
    )
