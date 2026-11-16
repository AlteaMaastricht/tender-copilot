"""The v1 prompt files: they load, name their schema, keep their rules and stay out of code.

No live calls. Everything is driven by the registry in `pipeline/prompt_specs.py`, so a new
prompt is covered as soon as it is registered.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path
from typing import get_args
from xml.etree import ElementTree

import pytest

from agent.fakes import PLANTED_CONTACT, FakeBackend
from agent.tools import CHAT_TOOLS, MATCHING_TOOLS, TOOLS
from llm.client import Gateway, Stage
from llm.log import MemoryCallLog
from llm.prompts import PROMPTS_DIR, UntrustedText, load_prompt, schema_json
from llm.redact import Redactor
from pipeline.match import match_requirement
from pipeline.prompt_specs import (
    ALL,
    CHAT,
    EXTRACT_MAP,
    EXTRACT_REDUCE,
    MATCH,
    MEMO,
    MEMO_REASON,
    SCREEN,
    PromptSpec,
    company_sections,
)
from pipeline.schemas import (
    AwardBasis,
    ChatAnswer,
    CopilotFitName,
    ExclusionFlag,
    ExtractionOutput,
    ExtractionPartial,
    FitDimensionName,
    KnockoutCriterion,
    KnockoutKind,
    MatchStatus,
    MemoDraft,
    MemoReason,
    Mitigation,
    ProofWhen,
    Recommendation,
    RequirementMatch,
    ScreeningOutput,
    ServiceLine,
    UnclearReason,
)
from tests.fakes import CONTEXT, SONNET, FakeProvider, reply
from tests.test_schemas import citation, knockout

ROOT = PROMPTS_DIR.parent
PACKAGES = ("agent", "app", "crm", "evals", "ingest", "jobs", "llm", "pipeline", "rag")
DUMMY = {
    "company_profile": "PROFILE-TEXT",
    "tender_id": "42",
    "recommendation": "no_bid",
    "reason_codes": "knockout_not_met",
}
LOOP_TOOLS = {MATCH.ref.name: MATCHING_TOOLS, CHAT.ref.name: CHAT_TOOLS}
EXAMPLE_COUNT = {
    "screen": 6,
    "extract_map": 2,
    "extract_reduce": 1,
    "match": 3,
    "memo": 1,
    "memo_reason": 2,
    "chat": 2,
}
MARKER = "UNTRUSTED-MARKER-TEXT"

_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")
_EXAMPLE = re.compile(r"<example>\n(.*?)\n</example>", re.DOTALL)
_OUTPUT = re.compile(r"<example_output>\n(.*?)\n</example_output>", re.DOTALL)

each_prompt = pytest.mark.parametrize("spec", ALL, ids=lambda spec: spec.ref.name)


def raw(spec: PromptSpec) -> str:
    return (PROMPTS_DIR / spec.ref.filename).read_text(encoding="utf-8")


def variables(spec: PromptSpec) -> dict[str, str]:
    return {name: DUMMY[name] for name in spec.variables}


def rendered(spec: PromptSpec) -> str:
    return load_prompt(spec.ref, variables(spec))


def examples(spec: PromptSpec) -> list[tuple[str, str]]:
    """Each example as (the input shown, the output JSON)."""
    found = []
    for block in _EXAMPLE.findall(raw(spec)):
        [output] = _OUTPUT.findall(block)
        found.append((block[: block.index("<example_output>")], output))
    return found


def named(text: str, value: str) -> bool:
    """The value is written as a literal: in backticks."""
    return f"`{value}`" in text


def holds_citation(spec: PromptSpec) -> bool:
    return "Citation" in spec.schema.model_json_schema().get("$defs", {})


def values_under(node: object, key: str) -> list:
    """Every value stored under `key`, at any depth."""
    if isinstance(node, dict):
        found = [node[key]] if key in node else []
        return found + [v for value in node.values() for v in values_under(value, key)]
    if isinstance(node, list):
        return [v for item in node for v in values_under(item, key)]
    return []


def section_rows(number: int) -> list[list[str]]:
    """The table rows of one COMPANY.md section, without the header."""
    lines = [line for line in company_sections((number,)).splitlines() if line.startswith("|")]
    return [[cell.strip() for cell in line.strip("|").split("|")] for line in lines[2:]]


STAFF_NAMES = [row[0] for row in section_rows(7)]
REFERENCE_CLIENTS = [row[1] for row in section_rows(6)]


# --- Files and variables -------------------------------------------------------


@each_prompt
def test_every_prompt_loads_with_its_declared_variables(spec):
    text = rendered(spec)
    # No placeholder is left. A bare "}}" is fine: the JSON examples close nested objects.
    assert text and "{{" not in text and not _PLACEHOLDER.search(text)
    for name in spec.variables:
        assert DUMMY[name] in text


@each_prompt
def test_placeholders_equal_the_declared_variables(spec):
    assert set(_PLACEHOLDER.findall(raw(spec))) == set(spec.variables)


def test_every_prompt_file_is_registered_a_gateway_block_or_the_smoke_prompt():
    on_disk = {path.name for path in PROMPTS_DIR.glob("*.md")}
    registered = [spec.ref.filename for spec in ALL]
    assert len(set(registered)) == len(registered) == 7
    assert set(registered) <= on_disk
    for name in on_disk:
        assert re.fullmatch(r"[a-z][a-z0-9_]*_v[0-9]+\.md", name), name
    others = on_disk - set(registered)
    assert all(name.startswith("gateway_") or name == "smoke_v1.md" for name in others), others


@each_prompt
def test_each_prompt_names_its_schema(spec):
    assert re.findall(r"^Output schema: `(\w+)`$", raw(spec), re.MULTILINE) == [
        spec.schema.__name__
    ]


def test_the_registry_ties_each_prompt_to_its_stage_and_schema():
    table = {spec.ref.name: (spec.ref.version, spec.stage, spec.schema) for spec in ALL}
    assert table == {
        "screen": ("v1", "screening", ScreeningOutput),
        "extract_map": ("v1", "extraction_map", ExtractionPartial),
        "extract_reduce": ("v1", "extraction_reduce", ExtractionOutput),
        "match": ("v1", "matching", RequirementMatch),
        "memo": ("v1", "memo", MemoDraft),
        "memo_reason": ("v1", "memo", MemoReason),
        "chat": ("v1", "chat", ChatAnswer),
    }
    assert all(spec.stage in get_args(Stage) for spec in ALL)
    assert set(EXAMPLE_COUNT) == set(table)


@each_prompt
def test_each_prompt_works_through_the_gateway(spec, monkeypatch):
    monkeypatch.delenv("LLM_PAYLOAD_DIR", raising=False)
    answer = json.loads(examples(spec)[0][1])
    provider, log = FakeProvider(reply(answer, model=SONNET)), MemoryCallLog()
    gateway = Gateway({"anthropic": provider}, log, secrets=(), sleep=lambda _: None)
    common = {
        "task": spec.stage,
        "prompt": spec.ref,
        "variables": variables(spec),
        "data": [UntrustedText("block", MARKER)],
        "model": SONNET,
        "redactor": Redactor(),
        "context": CONTEXT,
    }
    if spec.ref.name in LOOP_TOOLS:
        tools = [TOOLS[name].spec for name in LOOP_TOOLS[spec.ref.name]]
        output = gateway.step(
            **common, history=[], tools=tools, final_schema=spec.schema, step_index=0
        ).output
    else:
        output = gateway.call(**common, schema=spec.schema)
    assert isinstance(output, spec.schema)

    request = provider.requests[0]
    assert request.system.startswith(rendered(spec))
    assert "## Untrusted material" in request.system and "## Output format" in request.system
    assert schema_json(spec.schema) in request.system
    assert MARKER not in request.system and MARKER in request.messages[0].text
    record = log.records[0]
    assert (record.prompt_name, record.prompt_version, record.stage) == (
        spec.ref.name,
        spec.ref.version,
        spec.stage,
    )


# --- Trust and company sections ------------------------------------------------


def test_company_sections_returns_the_numbered_sections_in_order():
    text = company_sections((2, 4, 5))
    headings = re.findall(r"^## (\d+)\. ", text, re.MULTILINE)
    assert headings == ["2", "4", "5"] and text.startswith("## 2. Services")
    assert company_sections((5, 2)).startswith("## 5. Will not bid on")
    with pytest.raises(ValueError, match="no section"):
        company_sections((2, 99))


def test_screening_gets_sections_2_4_and_5_and_nothing_else():
    profile = company_sections(SCREEN.company_sections)
    assert "Data platforms and data engineering" in profile  # section 2
    assert "Target CPV codes" in profile and "Staffing-only contracts" in profile  # 4 and 5
    assert len(STAFF_NAMES) == 5 and len(REFERENCE_CLIENTS) == 6
    for outside in [*STAFF_NAMES, *REFERENCE_CLIENTS, "hourly rates", "ISO 9001"]:
        assert outside not in profile, outside


def test_only_screening_and_the_memo_get_company_sections():
    assert {spec.ref.name: spec.company_sections for spec in ALL} == {
        "screen": (2, 4, 5),
        "extract_map": (),
        "extract_reduce": (),
        "match": (),
        "memo": (2, 4, 5, 9),
        "memo_reason": (),
        "chat": (),
    }
    for spec in ALL:
        assert ("company_profile" in spec.variables) == bool(spec.company_sections)
    memo_profile = company_sections(MEMO.company_sections)
    assert not [name for name in STAFF_NAMES if name in memo_profile]


def test_variables_are_trusted_values_only():
    allowed = {"company_profile", "tender_id", "recommendation", "reason_codes"}
    assert {name for spec in ALL for name in spec.variables} == allowed
    assert set(DUMMY) == allowed


def test_matching_sends_the_tender_id_as_a_variable_and_the_criterion_as_data(monkeypatch):
    monkeypatch.delenv("LLM_PAYLOAD_DIR", raising=False)
    provider = FakeProvider(reply(json.loads(examples(MATCH)[0][1]), model=SONNET))
    gateway = Gateway({"anthropic": provider}, MemoryCallLog(), secrets=(), sleep=lambda _: None)
    criterion = KnockoutCriterion(**knockout(requirement_id="KO-07", citation=citation()))
    result = match_requirement(
        criterion,
        gateway=gateway,
        backend=FakeBackend(),
        model=SONNET,
        redactor=Redactor(),
        context=CONTEXT,
    )
    assert result.status == "met" and result.requirement_id == "KO-07"
    request = provider.requests[0]
    assert "has ID 42." in request.system and "{{" not in request.system
    assert "KO-07" not in request.system and "KO-07" in request.messages[0].text


@pytest.mark.parametrize("spec", [SCREEN, MEMO], ids=lambda spec: spec.ref.name)
def test_the_real_company_profile_passes_the_redactor_unchanged(spec):
    text = load_prompt(spec.ref, {"company_profile": company_sections(spec.company_sections)})
    assert Redactor().redact(text) == text


# --- Content rules -------------------------------------------------------------


@each_prompt
def test_prompts_that_return_citations_carry_the_citation_rules(spec):
    text = raw(spec)
    if not holds_citation(spec):
        assert spec in (SCREEN, MEMO_REASON)
        return
    for rule in (
        "`document_id`",
        "`page`",
        "`quote`",
        "character for character",
        "at least 10 letters or digits",
        "Leave `verified` out. Code sets it.",
        "Niet gevonden in stukken",
        "Never infer",
    ):
        assert rule in text, rule


def test_prompts_without_citations_say_what_to_do_instead():
    assert "Write no citations" in raw(MEMO_REASON)
    assert "Niet gevonden in stukken" in raw(MEMO_REASON) and "Never infer" in raw(MEMO_REASON)
    assert "State nothing the notice does not say." in raw(SCREEN)


@pytest.mark.parametrize("spec", [EXTRACT_REDUCE, MEMO], ids=lambda spec: spec.ref.name)
def test_steps_that_see_no_page_text_only_copy_citations(spec):
    text = raw(spec)
    assert "You cannot see page text, so you cannot write a new quote." in text
    assert "Code treats a citation that is not an exact copy as unverified." in text


@each_prompt
def test_every_prompt_asks_for_english_output(spec):
    assert "in English" in raw(spec)


def test_the_memo_asks_for_dutch_questions_that_reveal_nothing():
    text = raw(MEMO)
    assert "is always Dutch" in text and "is written in Dutch" in text
    assert "Nota van Inlichtingen" in text
    assert "reveals nothing about Datalink, its solution, its partners or its pricing" in text


def test_screening_names_every_decision_flag_and_service():
    text = raw(SCREEN)
    for value in ("shortlist", "reject", "unsure"):
        assert named(text, value), value
    for value in (*get_args(ExclusionFlag), *get_args(ServiceLine)):
        assert named(text, value), value
    assert "When in doubt, do not reject." in text  # tuned for recall (decision Q13)
    assert "An unsure tender is still read" in text


def test_extraction_names_the_values_of_its_literals():
    text = raw(EXTRACT_MAP)
    for value in (*get_args(KnockoutKind), *get_args(ProofWhen), *get_args(AwardBasis)):
        assert named(text, value), value
    for value in ("single", "multi", "none"):
        assert named(text, value), value
    assert "`KO-01`" in raw(EXTRACT_REDUCE) and "`overridden_by_qa`" in raw(EXTRACT_REDUCE)


def test_matching_names_every_status_mitigation_and_reason():
    text = raw(MATCH)
    for value in (*get_args(MatchStatus), *get_args(Mitigation), *get_args(UnclearReason)):
        assert named(text, value), value
    [code_only] = [line for line in text.splitlines() if "set by code when a run is cut" in line]
    for value in ("step_limit", "token_budget", "loop_failed"):
        assert named(code_only, value)
    assert "Never use them yourself." in code_only


def test_the_memo_scores_the_four_copilot_dimensions_and_no_human_one():
    text = raw(MEMO)
    copilot = get_args(CopilotFitName)
    for value in copilot:
        assert named(text, value), value
    for human in set(get_args(FitDimensionName)) - set(copilot):
        assert not named(text, human) and f'"{human}"' not in text, human
    for value in ("price_in", "ask_in_qa", "accept", "high", "medium", "low"):
        assert named(text, value), value
    assert "do not state a recommendation anywhere" in text


def test_the_reason_prompt_names_the_four_recommendation_values():
    text = raw(MEMO_REASON)
    for value in get_args(Recommendation):
        assert named(text, value), value
    assert "You cannot change it, soften it or argue against it." in text


@each_prompt
def test_no_prompt_names_a_write_tool_or_the_crm(spec):
    text = raw(spec).lower()
    for word in ("propose_crm_deal", "hubspot", "crm", "search_tenders"):
        assert word not in text, word
    mentioned = {name for name in TOOLS if name in text}
    assert mentioned == set(LOOP_TOOLS.get(spec.ref.name, ()))


def test_matching_is_never_given_formal_requirements_and_cites_facts_directly():
    text = raw(MATCH)
    assert "Formal requirements" in text and "They are never sent to you." in text
    assert "A fact from `get_company_facts` needs no extra search." in text
    assert "make one `search_company_kb` call" not in text
    # The 'met' example cites a fact without a search step.
    shown, output = examples(MATCH)[0]
    assert "get_company_facts returned" in shown and "search_company_kb returned" not in shown
    assert json.loads(output)["company_evidence"][0]["kb_id"] == 9101


@pytest.mark.parametrize("spec", [MEMO, MEMO_REASON], ids=lambda spec: spec.ref.name)
def test_the_memo_prompts_know_formal_rows_do_not_count(spec):
    text = raw(spec)
    assert "`action_required`" in text and "formal" in text.lower()


def test_chat_cannot_be_told_to_use_the_company_facts_tool():
    assert "get_company_facts" not in raw(CHAT) and "get_company_facts" in raw(MATCH)


# --- Few-shot examples ---------------------------------------------------------


@each_prompt
def test_each_example_output_validates_against_the_schema(spec):
    found = examples(spec)
    assert len(found) == EXAMPLE_COUNT[spec.ref.name]
    assert raw(spec).count("<example_output>") == len(found) == raw(spec).count("<example>")
    for _, output in found:
        spec.schema.model_validate_json(output)


def test_the_examples_cover_every_decision_and_status():
    decisions = {json.loads(output)["decision"] for _, output in examples(SCREEN)}
    assert decisions == {"shortlist", "reject", "unsure"}
    statuses = {json.loads(output)["status"] for _, output in examples(MATCH)}
    assert statuses == set(get_args(MatchStatus))
    assert [json.loads(output)["in_scope"] for _, output in examples(CHAT)] == [True, False]
    [draft] = [json.loads(output) for _, output in examples(MEMO)]
    assert None in [dimension["score"] for dimension in draft["fit"]]  # shows "no source"


@each_prompt
def test_examples_hold_no_contact_data_and_no_names(spec):
    text = raw(spec)
    names = [*STAFF_NAMES, PLANTED_CONTACT["contact_name"]]
    assert Redactor(names).redact(text) == text
    for known in [*STAFF_NAMES, *REFERENCE_CLIENTS, *PLANTED_CONTACT.values(), "Voorbeeldstad"]:
        assert known not in text, known
    assert not re.search(r"\b(?:Gemeente|Provincie|Waterschap|Universiteit) [A-Z]", text)


def fixture_shingles(size: int = 6) -> set[tuple[str, ...]]:
    """Every run of `size` words in the text of the saved TED notices."""
    shingles: set[tuple[str, ...]] = set()
    for path in sorted((ROOT / "tests" / "fixtures" / "ted").glob("*.xml")):
        for text in ElementTree.parse(path).getroot().itertext():
            words = re.findall(r"\w+", text.lower())
            shingles.update(tuple(words[i : i + size]) for i in range(len(words) - size + 1))
    return shingles


def test_examples_hold_no_text_from_the_real_notice_fixtures():
    shingles = fixture_shingles()
    assert len(shingles) > 1000  # the fixtures were read
    for spec in ALL:
        words = re.findall(r"\w+", raw(spec).lower())
        runs = {tuple(words[i : i + 6]) for i in range(len(words) - 5)}
        assert not runs & shingles, (spec.ref.name, sorted(runs & shingles)[:3])
        assert not re.search(r"\b\d{5,6}-20\d{2}\b", raw(spec))  # a TED publication number


@each_prompt
def test_example_identifiers_are_invented_and_quotes_come_from_the_example_input(spec):
    for shown, output in examples(spec):
        answer = json.loads(output)
        for key in ("document_id", "kb_id"):
            assert all(value >= 9001 for value in values_under(answer, key)), key
        for quote in values_under(answer, "quote"):
            assert quote in shown, quote


# --- No prompt inlined in code -------------------------------------------------


def code_files(*extra: str) -> list[Path]:
    return [p for package in (*PACKAGES, *extra) for p in sorted((ROOT / package).rglob("*.py"))]


def calls_to(tree: ast.AST, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", getattr(node.func, "attr", None)) == name
    ]


def strings(path: Path, *, docstrings: bool) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    skip: set[int] = set()
    if not docstrings:
        owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        for node in ast.walk(tree):
            if isinstance(node, owners) and node.body and isinstance(node.body[0], ast.Expr):
                skip.add(id(node.body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in skip
    ]


def files_that_call(name: str, *extra: str) -> set[str]:
    return {
        path.relative_to(ROOT).as_posix()
        for path in code_files(*extra)
        if calls_to(ast.parse(path.read_text(encoding="utf-8")), name)
    }


def test_the_code_scan_sees_the_production_packages():
    scanned = {path.relative_to(ROOT).as_posix() for path in code_files()}
    assert {"llm/client.py", "pipeline/match.py", "agent/loop.py", "agent/tools.py"} <= scanned


def test_a_request_to_a_model_is_built_only_in_the_gateway():
    assert files_that_call("ProviderRequest") == {"llm/client.py"}


def test_prompt_references_are_made_only_in_the_registry_and_the_gateway():
    assert files_that_call("PromptRef", "scripts") == {
        "pipeline/prompt_specs.py",
        "llm/client.py",
        "scripts/smoke_llm.py",
    }


def test_no_line_of_a_prompt_file_is_repeated_in_code():
    lines = {
        line.strip()
        for path in PROMPTS_DIR.glob("*.md")
        for line in path.read_text(encoding="utf-8").splitlines()
        if len(line.strip()) >= 40
    }
    assert len(lines) > 100
    for path in code_files("scripts"):
        for text in strings(path, docstrings=True):
            copied = [line for line in lines if line in text]
            assert not copied, (path.name, copied[:1])


def test_no_string_in_code_reads_like_an_instruction_to_a_model():
    instruction = re.compile(r"\b(?:You are|You must|Your task|Answer with|Respond with)\b")
    for path in code_files():
        for text in strings(path, docstrings=False):
            assert not instruction.search(text), (path.name, text[:80])


def test_memo_reason_prompt_explains_every_reason_code_the_rules_can_give():
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    rules = (root / "pipeline" / "memo.py").read_text()
    codes = set(
        re.findall(
            r'"((?:knockout|fit|documents|no_knockouts|match|duplicate|awaiting|lot)_[a-z_0-9]+)"',
            rules,
        )
    )
    assert "lot_specific_knockout" in codes and len(codes) >= 12
    prompt = (root / "prompts" / "memo_reason_v1.md").read_text()
    missing = sorted(code for code in codes if f"`{code}`" not in prompt)
    assert missing == [], missing
