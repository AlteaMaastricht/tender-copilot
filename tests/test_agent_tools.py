"""Tool registry, argument checks, tender binding and the contact-data guard."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from agent.fakes import INJECTION, OTHER_TENDER_ID, PLANTED_CONTACT, TENDER_ID, FakeBackend
from agent.tools import (
    CHAT_TOOLS,
    MATCHING_TOOLS,
    MAX_RESULT_CHARS,
    TOOLS,
    ToolBackend,
    contact_keys,
    run_tool,
)
from llm.providers.base import ToolCall

REPO = Path(__file__).resolve().parent.parent

# Arguments that make each tool return as much as it can.
BROAD_CALLS = [
    ("get_tender", {"tender_id": TENDER_ID}),
    ("search_tender_docs", {"tender_id": TENDER_ID, "query": "de een het van", "top_k": 10}),
    ("read_document_pages", {"document_id": 1, "page_from": 1, "page_to": 5}),
    ("search_company_kb", {"query": "de een het van datalink dataplatform", "top_k": 10}),
    (
        "get_company_facts",
        {"topics": ["turnover", "insurance", "certifications", "headcount", "references",
                    "legal_status", "languages"]},
    ),
]  # fmt: skip


def call(name: str, arguments: dict, backend=None, allowed=MATCHING_TOOLS):
    return run_tool(ToolCall("c1", name, arguments), allowed, backend or FakeBackend(), TENDER_ID)


# --- registry ----------------------------------------------------------------------


def test_registry_holds_exactly_the_five_read_tools():
    assert (
        set(TOOLS)
        == set(MATCHING_TOOLS)
        == {
            "get_tender",
            "search_tender_docs",
            "read_document_pages",
            "search_company_kb",
            "get_company_facts",
        }
    )
    assert set(CHAT_TOOLS) == set(MATCHING_TOOLS) - {"get_company_facts"}
    assert "propose_crm_deal" not in TOOLS and "search_tenders" not in TOOLS
    for name in TOOLS:
        assert name.split("_")[0] in {"get", "search", "read"}
        assert TOOLS[name].spec.name == name


def test_backend_has_only_read_methods():
    methods = {name for name in vars(ToolBackend) if not name.startswith("_")}
    assert methods == set(TOOLS)


def test_tool_definitions_equal_architecture_section_4():
    text = (REPO / "docs" / "ARCHITECTURE.md").read_text()
    section = text[text.index("## 4. Agent tools") : text.index("## 5. Pydantic schemas")]
    documented = [json.loads(block) for block in re.findall(r"```json\n(.*?)```", section, re.S)]
    assert [tool["name"] for tool in documented] == list(MATCHING_TOOLS)
    for tool in documented:
        spec = TOOLS[tool["name"]].spec
        assert spec.description == tool["description"]
        assert spec.input_schema == tool["input_schema"]


@pytest.mark.parametrize("name", MATCHING_TOOLS)
def test_argument_model_matches_the_json_schema(name):
    tool = TOOLS[name]
    schema, fields = tool.spec.input_schema, tool.args.model_fields
    assert set(schema["properties"]) == set(fields)
    assert set(schema["required"]) == {n for n, f in fields.items() if f.is_required()}
    assert schema["additionalProperties"] is False
    assert tool.args.model_config["extra"] == "forbid"


# --- fake implementations ----------------------------------------------------------


def test_each_fake_tool_returns_the_documented_shape():
    results = {name: json.loads(call(name, arguments).content) for name, arguments in BROAD_CALLS}

    tender = results["get_tender"]
    assert {"title", "buyer_name", "procedure_type", "deadline_at", "estimated_value",
            "cpv_codes", "lots"} <= set(tender)  # fmt: skip

    passage = results["search_tender_docs"]["passages"][0]
    assert {"chunk_id", "document_id", "doc_type", "section", "page_start", "page_end",
            "text"} <= set(passage)  # fmt: skip

    pages = results["read_document_pages"]["pages"]
    assert [page["page"] for page in pages] == [1, 2, 3] and INJECTION in pages[2]["text"]

    kb = results["search_company_kb"]["passages"][0]
    assert {"kb_id", "doc_slug", "text"} <= set(kb)

    facts = results["get_company_facts"]
    assert facts["turnover"]["value"]["2024"] == 4500000 and facts["headcount"]["value"] == 38


def test_every_fact_names_the_kb_passage_it_was_taken_from():
    backend = FakeBackend()
    passages = {entry["kb_id"]: entry for entry in backend.kb}
    result = json.loads(call(*BROAD_CALLS[-1], backend=backend).content)
    assert set(result) == set(BROAD_CALLS[-1][1]["topics"])
    for topic, facts in result.items():
        for fact in facts if isinstance(facts, list) else [facts]:
            assert set(fact) == {"value", "kb_id", "doc_slug", "text"}, topic
            source = passages[fact["kb_id"]]  # a real KB passage, so it can be cited
            assert (fact["doc_slug"], fact["text"]) == (source["doc_slug"], source["text"])
    assert "kb_id" in TOOLS["get_company_facts"].spec.description


def test_defaults_and_filters():
    default = json.loads(
        call("search_tender_docs", {"tender_id": TENDER_ID, "query": "de"}).content
    )
    assert len(default["passages"]) <= 5
    none = call("search_tender_docs", {"tender_id": TENDER_ID, "query": "de", "doc_types": ["qa"]})
    assert json.loads(none.content) == {"passages": []}
    one = call("get_company_facts", {"topics": ["headcount"]})
    assert list(json.loads(one.content)) == ["headcount"]
    assert json.loads(one.content)["headcount"]["value"] == 38


# --- tender binding ----------------------------------------------------------------


def test_another_tenders_id_is_refused():
    backend = FakeBackend()
    for name in ("get_tender", "search_tender_docs"):
        arguments = {"tender_id": OTHER_TENDER_ID, "query": "geheim"}
        if name == "get_tender":
            del arguments["query"]
        result = call(name, arguments, backend)
        assert result.is_error and f"only read tender {TENDER_ID}" in result.content
    assert backend.calls == []


def test_another_tenders_document_is_refused():
    result = call("read_document_pages", {"document_id": 9, "page_from": 1, "page_to": 1})
    assert result.is_error and "not part of this tender" in result.content
    assert "Geheim" not in result.content
    missing = call("read_document_pages", {"document_id": 999, "page_from": 1, "page_to": 1})
    assert missing.is_error


def test_a_tool_outside_the_allowed_set_is_unknown():
    result = call("get_company_facts", {"topics": ["headcount"]}, allowed=CHAT_TOOLS)
    assert result.is_error and "Unknown tool" in result.content
    assert "get_company_facts" not in result.content


def test_a_long_invented_tool_name_is_cut_in_the_result():
    result = call("x" * 500, {})
    assert result.is_error and len(result.name) == 60


def test_an_oversized_result_is_an_error_not_a_truncation():
    class HugeBackend(FakeBackend):
        def get_tender(self, tender_id):
            return {"title": "x" * (MAX_RESULT_CHARS + 1)}

    result = call("get_tender", {"tender_id": TENDER_ID}, HugeBackend())
    assert result.is_error and "too large" in result.content and "xxxx" not in result.content


# --- contact-data guard ------------------------------------------------------------


def test_the_fake_dataset_really_holds_the_planted_contact():
    backend = FakeBackend()
    assert backend.contacts[0]["contact_name"] == PLANTED_CONTACT["contact_name"]
    assert set(PLANTED_CONTACT) == {"contact_name", "email", "phone"}


@pytest.mark.parametrize(("name", "arguments"), BROAD_CALLS, ids=[c[0] for c in BROAD_CALLS])
def test_no_tool_returns_contact_data(name, arguments):
    """Fails if any tool, called as broadly as it allows, returns contact data."""
    result = call(name, arguments)
    assert result.is_error is False
    for planted in PLANTED_CONTACT.values():
        assert planted not in result.content
    assert contact_keys(json.loads(result.content)) == []


@pytest.mark.parametrize(
    "leak",
    [
        {"title": "Dataplatform", "contact_name": PLANTED_CONTACT["contact_name"]},
        {"title": "Dataplatform", "contacts": [dict(PLANTED_CONTACT)]},
        {"buyer": {"name": "Gemeente", "Email": PLANTED_CONTACT["email"]}},
        {"lots": [{"title": "Lot 1", "buyer_phone": PLANTED_CONTACT["phone"]}]},
        {"contact_point": {"org": "Gemeente"}},
        {"passages": [{"text": "ok", "meta": {"telephone": PLANTED_CONTACT["phone"]}}]},
    ],
)
def test_a_backend_that_leaks_contact_data_is_blocked(leak):
    class LeakyBackend(FakeBackend):
        def get_tender(self, tender_id):
            return leak

    result = call("get_tender", {"tender_id": TENDER_ID}, LeakyBackend())
    assert result.is_error is True
    assert result.content == "Tool result withheld: it contained contact data."
    for planted in PLANTED_CONTACT.values():
        assert planted not in result.content


def test_contact_keys_ignores_ordinary_keys():
    assert contact_keys({"title": "x", "lots": [{"cpv_codes": ["72"]}], "text": "bel ons"}) == []
    assert contact_keys({"a": [{"b": {"Contact_Name": 1}}]}) == ["Contact_Name"]


# --- structure ---------------------------------------------------------------------


def test_agent_package_has_no_write_path_and_no_sdk():
    forbidden = {"crm", "sqlalchemy", "psycopg", "subprocess", "anthropic", "mistralai",
                 "openai", "requests", "urllib", "socket"}  # fmt: skip
    for path in (REPO / "agent").rglob("*.py"):
        found: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                found |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module.split(".")[0])
        assert not forbidden & found, path


# --- Dutch and English contact keys ------------------------------------------------


@pytest.mark.parametrize(
    "result",
    [
        {"telefoon": "06-12345678"},
        {"telefoonnummer": "06-12345678"},
        {"TelefoonNummer": "06-12345678"},
        {"tel": "06-12345678"},
        {"tel_nr": "06-12345678"},
        {"telnr": "06-12345678"},
        {"mobiel": "06-12345678"},
        {"mobielNummer": "06-12345678"},
        {"gsm": "06-12345678"},
        {"fax": "020-1234567"},
        {"e-mail": "a@b.example"},
        {"E-mailadres": "a@b.example"},
        {"emailadres": "a@b.example"},
        {"e_mail": "a@b.example"},
        {"mail": "a@b.example"},
        {"contactpersoon": "Jan de Vries"},
        {"contactPersoon": "Jan de Vries"},
        {"contactgegevens": {"plaats": "Utrecht"}},
        {"voornaam": "Jan"},
        {"achternaam": "de Vries"},
        {"first_name": "Jan"},
        {"lastName": "de Vries"},
        {"surname": "de Vries"},
        {"aanspreekpunt": {"naam": "Jan de Vries"}},
        {"aanspreekpunt": {"Naam": "Jan de Vries", "organisatie": "Gemeente"}},
        {"behandelaar": {"name": "Jan de Vries"}},
        {"inkoper": [{"naam": "Jan de Vries"}]},
        {"projectleider": {"gegevens": {"naam": "Jan de Vries"}}},
        {"inlichtingen": {"initialen": "J.", "tussenvoegsel": "de"}},
        {"buyer": {"contact_person": {"naam": "Jan"}}},
        {"lots": [{"title": "Perceel 1", "aanspreekpunt": {"volledige_naam": "Jan"}}]},
    ],
)
def test_dutch_and_english_contact_keys_are_blocked(result):
    assert contact_keys(result), result

    class LeakyBackend(FakeBackend):
        def get_tender(self, tender_id):
            return result

    blocked = call("get_tender", {"tender_id": TENDER_ID}, LeakyBackend())
    assert blocked.is_error is True
    assert blocked.content == "Tool result withheld: it contained contact data."
    assert "Vries" not in blocked.content and "06-" not in blocked.content


@pytest.mark.parametrize(
    "result",
    [
        {"naam": "Inschrijvingsleidraad.pdf"},  # a document name, no person parent
        {"buyer_name": "Gemeente Voorbeeldstad"},
        {"organisatie": {"naam": "Gemeente Voorbeeldstad"}},
        {"lots": [{"naam": "Perceel 1", "titel": "Dataplatform"}]},
        {"title": "Hotelovernachtingen en catering"},  # 'tel' inside a word
        {"titel": "Mailingservice", "contract_value": 100},  # 'contract' is not 'contact'
        {"mailing_lijst": "nee"},  # 'mailing' is not the word 'mail'
        {"lastig": True, "lasten": 3},  # 'last' only as a whole word
        {"section": "4.2 Telefonische bereikbaarheid"},  # text values are not checked here
    ],
)
def test_ordinary_keys_are_not_blocked(result):
    assert contact_keys(result) == []
