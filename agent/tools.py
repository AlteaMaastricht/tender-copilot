"""The agent's tools (ARCHITECTURE.md section 4). All read-only; there are no write tools.

Each tool has the JSON schema the model sees and a Pydantic model that checks the arguments.
`run_tool` never raises: every problem becomes an error result for the model. The data comes
from a `ToolBackend`; `agent/fakes.py` has a fake one until the database exists.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from llm.providers.base import ToolCall, ToolResult, ToolSpec

MAX_RESULT_CHARS = 20_000
MAX_PAGES_PER_READ = 5
# Contact-data guard. Keys are lower-cased and split into words ("telefoonNummer" ->
# "telefoon", "nummer"; "e-mail" -> "email").
# A key containing one of these anywhere is contact data (English and Dutch):
CONTACT_SUBSTRINGS = (
    "contact", "email", "phone", "telefoon", "mobiel", "mobile", "whatsapp",
    "voornaam", "achternaam", "firstname", "lastname", "surname",
)  # fmt: skip
# A key with one of these as a whole word is contact data (too short to match inside words).
# "first" and "last" are not here: they blocked ordinary keys such as `last_updated` and
# `first_page`. `first_name` and `lastName` are still caught as "firstname" and "lastname".
CONTACT_WORDS = frozenset({"mail", "tel", "telnr", "fax", "gsm", "sms"})
# Under a parent that stands for a person, these name a person and are contact data too:
PERSON_PARENTS = (
    "aanspreekpunt", "persoon", "person", "behandelaar", "inkoper", "projectleider",
    "medewerker", "functionaris", "officer", "inlichtingen", "vertegenwoordiger",
    "representative", "ondertekenaar", "signatory",
)  # fmt: skip
NAME_WORDS = frozenset(
    {"naam", "name", "initialen", "initials", "tussenvoegsel", "aanhef", "first", "last"}
)

TenderDocType = Literal[
    "guideline", "requirements", "qa", "contract", "pricing", "form", "annex", "other"
]
KbDocType = Literal[
    "profile", "certification", "reference", "cv", "policy", "insurance", "financial"
]
FactTopic = Literal[
    "turnover", "insurance", "certifications", "headcount", "references", "legal_status",
    "languages",
]  # fmt: skip


class ToolError(Exception):
    """Raised by a backend with a message that is safe to show to the model."""


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class GetTenderArgs(_Args):
    tender_id: int


class SearchTenderDocsArgs(_Args):
    tender_id: int
    query: str = Field(min_length=2)
    doc_types: list[TenderDocType] | None = None
    top_k: int = Field(default=5, ge=1, le=10)


class ReadDocumentPagesArgs(_Args):
    document_id: int
    page_from: int = Field(ge=1)
    page_to: int = Field(ge=1)

    @model_validator(mode="after")
    def _page_range(self) -> Self:
        if self.page_to < self.page_from:
            raise ValueError("page_to must not be before page_from")
        if self.page_to - self.page_from + 1 > MAX_PAGES_PER_READ:
            raise ValueError(f"at most {MAX_PAGES_PER_READ} pages per call")
        return self


class SearchCompanyKbArgs(_Args):
    query: str = Field(min_length=2)
    doc_types: list[KbDocType] | None = None
    top_k: int = Field(default=5, ge=1, le=10)


class GetCompanyFactsArgs(_Args):
    topics: list[FactTopic] = Field(min_length=1)


class ToolBackend(Protocol):
    """Where the data comes from. Read methods only."""

    def get_tender(self, tender_id: int) -> dict[str, Any]: ...

    def search_tender_docs(
        self, tender_id: int, query: str, doc_types: list[str] | None, top_k: int
    ) -> dict[str, Any]: ...

    def read_document_pages(
        self, tender_id: int, document_id: int, page_from: int, page_to: int
    ) -> dict[str, Any]: ...

    def search_company_kb(
        self, query: str, doc_types: list[str] | None, top_k: int
    ) -> dict[str, Any]: ...

    def get_company_facts(self, topics: list[str]) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class Tool:
    spec: ToolSpec  # what the model sees
    args: type[_Args]  # how the arguments are checked


def _tool(name: str, description: str, args: type[_Args], schema: dict[str, Any]) -> Tool:
    return Tool(ToolSpec(name, description, schema), args)


# Descriptions and schemas are copied from ARCHITECTURE.md section 4; a test compares them.
TOOLS: dict[str, Tool] = {
    "get_tender": _tool(
        "get_tender",
        "Return the structured notice data for one tender: title, buyer organisation, "
        "procedure type, deadline, estimated value, CPV codes and lots. Use this for facts "
        "published in the notice. It does not return document text or contact persons.",
        GetTenderArgs,
        {
            "type": "object",
            "properties": {"tender_id": {"type": "integer"}},
            "required": ["tender_id"],
            "additionalProperties": False,
        },
    ),
    "search_tender_docs": _tool(
        "search_tender_docs",
        "Hybrid search (meaning plus exact words) over the documents of one tender. Returns "
        "passages with chunk_id, document_id, document type, section heading and page range. "
        "Write the query in the language of the documents, usually Dutch. Use exact terms "
        "for clause numbers, e.g. 'artikel 4.2'.",
        SearchTenderDocsArgs,
        {
            "type": "object",
            "properties": {
                "tender_id": {"type": "integer"},
                "query": {"type": "string", "minLength": 2},
                "doc_types": {
                    "type": "array",
                    "items": {
                        "enum": [
                            "guideline",
                            "requirements",
                            "qa",
                            "contract",
                            "pricing",
                            "form",
                            "annex",
                            "other",
                        ]  # fmt: skip
                    },
                    "description": "Optional filter. Omit to search all documents.",
                },
                "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            },
            "required": ["tender_id", "query"],
            "additionalProperties": False,
        },
    ),
    "read_document_pages": _tool(
        "read_document_pages",
        "Return the full text of consecutive pages of one tender document. Use after "
        "search_tender_docs when a passage is cut off or a table continues on the next page. "
        "At most 5 pages per call.",
        ReadDocumentPagesArgs,
        {
            "type": "object",
            "properties": {
                "document_id": {"type": "integer"},
                "page_from": {"type": "integer", "minimum": 1},
                "page_to": {"type": "integer", "minimum": 1},
            },
            "required": ["document_id", "page_from", "page_to"],
            "additionalProperties": False,
        },
    ),
    "search_company_kb": _tool(
        "search_company_kb",
        "Hybrid search over the company's own knowledge base: profile, reference projects, "
        "CVs, certifications and bid policy. Returns passages with kb_id and doc_slug. Use to "
        "find evidence that the company meets a requirement.",
        SearchCompanyKbArgs,
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 2},
                "doc_types": {
                    "type": "array",
                    "items": {
                        "enum": [
                            "profile",
                            "certification",
                            "reference",
                            "cv",
                            "policy",
                            "insurance",
                            "financial",
                        ]  # fmt: skip
                    },
                },
                "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    "get_company_facts": _tool(
        "get_company_facts",
        "Return exact structured facts about the company. Use this instead of search whenever "
        "a requirement states a number, a date or a named certificate: minimum turnover, "
        "insurance cover, certificate validity, headcount, or the number, value and year of "
        "reference projects. Each fact comes with the kb_id, doc_slug and text of the "
        "knowledge-base passage it was taken from; cite that passage as evidence.",
        GetCompanyFactsArgs,
        {
            "type": "object",
            "properties": {
                "topics": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "enum": [
                            "turnover",
                            "insurance",
                            "certifications",
                            "headcount",
                            "references",
                            "legal_status",
                            "languages",
                        ]  # fmt: skip
                    },
                }
            },
            "required": ["topics"],
            "additionalProperties": False,
        },
    ),
}

MATCHING_TOOLS = (
    "get_tender", "search_tender_docs", "read_document_pages", "search_company_kb",
    "get_company_facts",
)  # fmt: skip
CHAT_TOOLS = ("get_tender", "search_tender_docs", "read_document_pages", "search_company_kb")


def _key_words(key: object) -> tuple[str, list[str]]:
    """The key in lower case without separators, and its separate words."""
    text = re.sub(r"([a-z])([A-Z])", r"\1_\2", str(key)).lower().replace("e-mail", "email")
    words = [word for word in re.split(r"[^a-z0-9]+", text) if word]
    return "".join(words), words


def _is_contact_key(key: object, under_person: bool) -> bool:
    joined, words = _key_words(key)
    if any(part in joined for part in CONTACT_SUBSTRINGS) or CONTACT_WORDS & set(words):
        return True
    return under_person and bool(NAME_WORDS & set(words))


def _is_person_key(key: object) -> bool:
    joined, _ = _key_words(key)
    return any(part in joined for part in PERSON_PARENTS)


def contact_keys(value: Any, under_person: bool = False) -> list[str]:
    """Keys at any depth of a result that look like contact data."""
    if isinstance(value, dict):
        found = [str(k) for k in value if _is_contact_key(k, under_person)]
        for key, item in value.items():
            found += contact_keys(item, under_person or _is_person_key(key))
        return found
    if isinstance(value, list | tuple):
        return [key for item in value for key in contact_keys(item, under_person)]
    return []


def _call_backend(backend: ToolBackend, args: _Args, tender_id: int) -> dict[str, Any]:
    """Every tender-scoped call uses the run's own tender, never one the model chose."""
    if isinstance(args, GetTenderArgs):
        return backend.get_tender(tender_id)
    if isinstance(args, SearchTenderDocsArgs):
        return backend.search_tender_docs(tender_id, args.query, args.doc_types, args.top_k)
    if isinstance(args, ReadDocumentPagesArgs):
        return backend.read_document_pages(
            tender_id, args.document_id, args.page_from, args.page_to
        )
    if isinstance(args, SearchCompanyKbArgs):
        return backend.search_company_kb(args.query, args.doc_types, args.top_k)
    if isinstance(args, GetCompanyFactsArgs):
        return backend.get_company_facts(args.topics)
    raise AssertionError(f"no backend call for {type(args).__name__}")


def run_tool(
    call: ToolCall, allowed: Sequence[str], backend: ToolBackend, tender_id: int
) -> ToolResult:
    """Run one tool call. Never raises: any problem is returned as an error result."""

    def error(message: str) -> ToolResult:
        return ToolResult(call.id, call.name[:60], message, is_error=True)

    if call.name not in allowed or call.name not in TOOLS:
        return error(f"Unknown tool. Available tools: {', '.join(allowed)}.")
    try:
        args = TOOLS[call.name].args.model_validate(call.arguments)
    except ValidationError as exc:
        # Field, message and type only: the rejected value is not echoed back.
        problems = exc.errors(include_url=False, include_input=False, include_context=False)
        return error("Invalid arguments: " + json.dumps(problems, default=str))
    if getattr(args, "tender_id", tender_id) != tender_id:
        return error(f"This run can only read tender {tender_id}.")
    try:
        result = _call_backend(backend, args, tender_id)
    except ToolError as exc:
        return error(str(exc))
    except Exception as exc:
        # Class name only: an error message could carry internal details or personal data.
        return error(f"Tool failed: {type(exc).__name__}.")
    if contact_keys(result):
        return error("Tool result withheld: it contained contact data.")
    content = json.dumps(result, ensure_ascii=False, default=str)
    if len(content) > MAX_RESULT_CHARS:
        return error("Result too large. Narrow the request: fewer pages or a lower top_k.")
    return ToolResult(call.id, call.name, content)
