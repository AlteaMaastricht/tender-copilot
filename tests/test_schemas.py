"""Schemas from ARCHITECTURE.md section 5: bounds, literals, validators and round-trips."""

from __future__ import annotations

import inspect
import json
from datetime import date, datetime
from decimal import Decimal
from typing import get_args

import pytest
from pydantic import BaseModel, ValidationError

from agent.tools import contact_keys
from pipeline import schemas
from pipeline.memo import FIT_WEIGHTS, compute_fit
from pipeline.schemas import (
    AwardCriterion,
    AwardPosition,
    Citation,
    CopilotFitName,
    ExtractionOutput,
    ExtractionPartial,
    FitDimension,
    FitDraft,
    FitScore,
    KbEvidence,
    KeyDates,
    KeyDateTime,
    KnockoutCriterion,
    KnockoutFinding,
    MatchingOutput,
    Memo,
    MemoDraft,
    MemoReason,
    RequirementMatch,
    ScreeningOutput,
)

QUOTE = "De inschrijver beschikt over een geldig certificaat."


def citation(**overrides) -> dict:
    return {"document_id": 1, "page": 3, "quote": QUOTE, **overrides}


def kb_evidence() -> dict:
    return {"kb_id": 7, "doc_slug": "certificaat-iso-27001", "quote": "ISO 27001, geldig tot 2028"}


def screening(**overrides) -> dict:
    return {
        "decision": "shortlist",
        "reason": "Data platform for a municipality, within the value range.",
        "matched_services": ["data_platform"],
        "exclusion_flags": ["none"],
        "confidence": 0.8,
        **overrides,
    }


def knockout(**overrides) -> dict:
    return {
        "requirement_id": "KO-01",
        "category": "certification",
        "kind": "suitability",
        "text": "The bidder holds a valid ISO 27001 certificate.",
        "threshold": "ISO 27001",
        "proof": "Copy of the certificate",
        "proof_when": "on_request",
        "consortium_allowed": None,
        "citation": citation(),
        **overrides,
    }


def award(**overrides) -> dict:
    return {
        "name": "Plan van aanpak",
        "kind": "quality",
        "weight_pct": 60,
        "max_points": 600,
        "description": "Quality of the approach.",
        "scoring_method": "Scale 0-10, max 8 pages",
        "citation": citation(),
        **overrides,
    }


def extraction(**overrides) -> dict:
    return {
        "key_dates": {
            "submission_deadline": {"value": "2026-12-15T12:00:00", "citation": citation()},
            "questions_deadline": None,
            "contract_start": {"value": "2027-03-01", "citation": citation()},
        },
        "lots": [
            {
                "lot_identifier": None,
                "title": "Dataplatform",
                "summary": "Build and run a data platform.",
                "estimated_value_eur": "750000.50",
                "ceiling_value_eur": None,
                "value_includes_vat": False,
                "duration_months": 24,
                "extension_months": 24,
                "citations": [citation()],
            }
        ],
        "framework": "none",
        "knockout_criteria": [knockout()],
        "award_basis": "bpkv",
        "award_criteria": [award(subcriteria=[award(name="Risicodossier", weight_pct=20)])],
        "contract_risks": [{"text": "Unlimited liability", "citation": citation()}],
        "documents_read": [1, 2],
        "documents_skipped": [3],
        "warnings": [],
        **overrides,
    }


def match(**overrides) -> dict:
    return {
        "requirement_id": "KO-01",
        "status": "met",
        "reasoning": "The certificate is valid until 2028.",
        "tender_citation": citation(),
        "company_evidence": [kb_evidence()],
        "gap": None,
        "mitigation": "none",
        "unclear_reason": None,
        **overrides,
    }


def memo(**overrides) -> dict:
    return {
        "tender_id": 42,
        "language": "en",
        "tender": {
            "buyer_name": "Gemeente Voorbeeldstad",
            "title": "Dataplatform",
            "tenderned_id": "999001",
            "tenderned_url": None,
            "main_nature": "services",
            "procedure_type": "open",
            "eu_procedure": None,
            "cpv_codes": ["72000000"],
            "documents_access": "tenderned",
            "published_at": "2026-11-02T09:00:00",
        },
        "summary": "A municipality buys a data platform.",
        "lots": extraction()["lots"],
        "target_lots": [],
        "framework": "none",
        "deadlines": {"submission_deadline": None, "questions_deadline": None},
        "fit": compute_fit([]).model_dump(),
        "knockout_table": [
            {
                "requirement_id": "KO-01",
                "requirement": "Valid ISO 27001 certificate",
                "kind": "suitability",
                "proof": None,
                "proof_when": None,
                "status": "met",
                "evidence": [kb_evidence()],
                "gap": None,
                "mitigation": "none",
                "tender_citation": citation(),
            }
        ],
        "award_basis": "bpkv",
        "price_quality_tradeoff": None,
        "award_table": [
            {
                "name": "Plan van aanpak",
                "kind": "quality",
                "weight_pct": 60,
                "max_points": None,
                "scoring_method": None,
                "position": "medium",
                "win_theme": None,
                "citation": citation(),
            }
        ],
        "risks": [
            {
                "risk_id": "R-01",
                "text": "Unlimited liability",
                "category": "liability_penalties",
                "likelihood": "low",
                "impact": "high",
                "mitigation": "ask_in_qa",
                "citation": citation(),
            }
        ],
        "questions_for_qa": [
            {
                "question_id": "Q-01",
                "question": "Kan de aansprakelijkheid worden beperkt?",
                "purpose": "proportionality",
                "linked_to": ["R-01"],
                "priority": "high",
            }
        ],
        "recommendation": "needs_review",
        "recommendation_reason": "The human fit scores are still missing.",
        "recommendation_reason_codes": ["awaiting_human_scores"],
        "reasons": ["Scope fits", "Certificate present", "Liability is open"],
        "conditions": [],
        "confidence": "medium",
        "reflects_qa_document_ids": [],
        "documents_reviewed": [
            {
                "document_id": 1,
                "tag": "LEIDRAAD",
                "filename": "Inschrijvingsleidraad.pdf",
                "doc_type": "guideline",
                "published_at": "2026-11-02",
            }
        ],
        "citations": [citation()],
        "limitations": [],
        **overrides,
    }


# --- Citation, KbEvidence ------------------------------------------------------


def test_citation_bounds():
    assert Citation(**citation()).verified is None
    Citation(**citation(quote="x" * 10))
    Citation(**citation(quote="x" * 600))
    for bad in ({"page": 0}, {"quote": "x" * 9}, {"quote": "x" * 601}):
        with pytest.raises(ValidationError):
            Citation(**citation(**bad))


def test_kb_evidence_bounds():
    KbEvidence(kb_id=1, doc_slug="profile", quote="")  # section 5 sets no minimum
    with pytest.raises(ValidationError):
        KbEvidence(kb_id=1, doc_slug="profile", quote="x" * 601)


# --- ScreeningOutput -----------------------------------------------------------


def test_screening_valid():
    assert ScreeningOutput(**screening()).decision == "shortlist"
    ScreeningOutput(**screening(exclusion_flags=["liability", "ops_24_7"]))
    ScreeningOutput(**screening(exclusion_flags=[]))


@pytest.mark.parametrize(
    "bad",
    [
        {"decision": "maybe"},
        {"matched_services": ["blockchain"]},
        {"exclusion_flags": ["too_boring"]},
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"reason": "x" * 401},
        {"exclusion_flags": ["none", "liability"]},
    ],
)
def test_screening_rejects(bad):
    with pytest.raises(ValidationError):
        ScreeningOutput(**screening(**bad))


# --- Extraction ----------------------------------------------------------------


def test_knockout_defaults_and_required_nullable():
    first, second = KnockoutCriterion(**knockout()), KnockoutCriterion(**knockout())
    assert first.applies_to_lots == [] and first.overridden_by_qa is False
    assert first.disproportionate is False
    first.applies_to_lots.append("LOT-0001")
    assert second.applies_to_lots == []  # default list is not shared

    KnockoutCriterion(**knockout(threshold=None, proof=None, proof_when=None))
    for field in ("threshold", "proof", "proof_when", "consortium_allowed"):
        data = knockout()
        del data[field]
        with pytest.raises(ValidationError):
            KnockoutCriterion(**data)


@pytest.mark.parametrize(
    "bad",
    [
        {"category": "vibes"},
        {"kind": "nice_to_have"},
        {"proof_when": "never"},
        {"requirement_id": "KO-1"},
        {"requirement_id": "ko-01"},
        {"requirement_id": "R-01"},
    ],
)
def test_knockout_rejects(bad):
    with pytest.raises(ValidationError):
        KnockoutCriterion(**knockout(**bad))


def test_knockout_id_accepts_two_or_more_digits():
    KnockoutCriterion(**knockout(requirement_id="KO-01"))
    KnockoutCriterion(**knockout(requirement_id="KO-112"))


def test_award_criterion():
    parsed = AwardCriterion(**award(subcriteria=[award(name="Sub", weight_pct=None)]))
    assert parsed.subcriteria[0].name == "Sub" and parsed.subcriteria[0].subcriteria == []
    for bad in (
        {"weight_pct": 101},
        {"weight_pct": -1},
        {"kind": "luck"},
        {"description": "x" * 601},
    ):
        with pytest.raises(ValidationError):
            AwardCriterion(**award(**bad))


def test_key_dates_each_date_has_its_own_citation():
    dates = KeyDates(**extraction()["key_dates"])
    assert dates.submission_deadline.citation.page == 3
    assert dates.contract_start.value == date(2027, 3, 1)
    assert "citations" not in KeyDates.model_fields
    with pytest.raises(ValidationError):
        KeyDateTime(value="2026-12-15T12:00:00")
    with pytest.raises(ValidationError):
        KeyDates(questions_deadline=None)  # submission_deadline is required, may be null


def test_extraction_round_trip_keeps_decimal_and_datetime():
    parsed = ExtractionOutput(**extraction())
    again = ExtractionOutput.model_validate_json(parsed.model_dump_json())
    assert again == parsed
    assert again.lots[0].estimated_value_eur == Decimal("750000.50")
    assert again.key_dates.submission_deadline.value == datetime(2026, 12, 15, 12, 0)
    assert again.award_criteria[0].subcriteria[0].name == "Risicodossier"


def test_extraction_rejects_unknown_literals():
    for bad in ({"framework": "maybe"}, {"award_basis": "cheapest"}):
        with pytest.raises(ValidationError):
            ExtractionOutput(**extraction(**bad))


# --- Matching ------------------------------------------------------------------


def test_match_valid_cases():
    RequirementMatch(**match())
    RequirementMatch(**match(status="not_met", gap="No certificate", mitigation="third_party"))
    RequirementMatch(
        **match(status="unclear", company_evidence=[], unclear_reason="step_limit", gap=None)
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"status": "met", "company_evidence": []},
        {"status": "unclear", "unclear_reason": None},
        {"status": "not_met", "gap": None},
        {"status": "not_met", "gap": "   "},
        {"status": "partly"},
        {"mitigation": "hope"},
        {"unclear_reason": "tired"},
        {"reasoning": "x" * 501},
    ],
)
def test_match_rejects(bad):
    with pytest.raises(ValidationError):
        RequirementMatch(**match(**bad))


def test_matching_output_round_trip():
    parsed = MatchingOutput(matches=[match()], award_notes=["Strong on approach"])
    assert MatchingOutput.model_validate_json(parsed.model_dump_json()) == parsed


# --- Fit -----------------------------------------------------------------------


def fit_dimension(**overrides) -> dict:
    return {
        "name": "scope_match",
        "weight_pct": 25,
        "scored_by": "copilot",
        "score": 4,
        "rationale": "Core data platform work.",
        "citation": citation(),
        **overrides,
    }


def test_fit_dimension_bounds_and_source_rule():
    FitDimension(**fit_dimension())
    FitDimension(**fit_dimension(citation=None, kb_evidence=[kb_evidence()]))
    FitDimension(**fit_dimension(score=None, rationale=None, citation=None))  # not scored
    FitDimension(name="win_probability", weight_pct=20, scored_by="human", score=3)
    for bad in (
        {"score": 0},
        {"score": 6},
        {"citation": None},
        {"rationale": None},
        {"rationale": " "},
        {"name": "luck"},
    ):
        with pytest.raises(ValidationError):
            FitDimension(**fit_dimension(**bad))


def test_fit_score_needs_the_seven_dimensions_once():
    fit = compute_fit([]).model_dump()
    FitScore(**fit)
    with pytest.raises(ValidationError):
        FitScore(**{**fit, "dimensions": fit["dimensions"][:6]})
    with pytest.raises(ValidationError):
        FitScore(**{**fit, "dimensions": [*fit["dimensions"][:6], fit["dimensions"][0]]})


def test_fit_score_bounds_and_consistency():
    fit = compute_fit([]).model_dump()
    for bad in (
        {"total": -1},
        {"total": 101},
        {"provisional_min": 80, "provisional_max": 60},
        {"total": 50},  # a final total needs min == max == total
    ):
        with pytest.raises(ValidationError):
            FitScore(**{**fit, **bad})


# --- Memo ----------------------------------------------------------------------


def test_memo_round_trip_as_stored_json():
    parsed = Memo(**memo())
    assert Memo.model_validate(parsed.model_dump(mode="json")) == parsed
    assert parsed.deadlines.submission_deadline is None  # shown as "unknown"


@pytest.mark.parametrize("value", ["bid", "bid_conditional", "no_bid", "needs_review"])
def test_memo_accepts_the_four_recommendations(value):
    assert Memo(**memo(recommendation=value)).recommendation == value


@pytest.mark.parametrize(
    "bad",
    [
        {"language": "de"},
        {"recommendation": "maybe"},
        {"summary": "x" * 1201},
        {"recommendation_reason": "x" * 601},
        {"reasons": ["a", "b", "c", "d"]},
        {"confidence": "certain"},
    ],
)
def test_memo_rejects(bad):
    with pytest.raises(ValidationError):
        Memo(**memo(**bad))


def test_a_formal_knockout_row_has_status_action_required_and_only_it_does():
    row = memo()["knockout_table"][0]
    formal = {**row, "kind": "formal", "status": "action_required", "evidence": []}
    assert Memo(**memo(knockout_table=[row, formal])).knockout_table[1].status == (
        "action_required"
    )
    for bad in (
        {**row, "kind": "formal"},  # formal but matched
        {**row, "status": "action_required", "evidence": []},  # not formal
        {**formal, "evidence": [kb_evidence()]},
        {**formal, "mitigation": "consortium"},
    ):
        with pytest.raises(ValidationError):
            Memo(**memo(knockout_table=[bad]))


def test_the_model_can_never_answer_action_required():
    with pytest.raises(ValidationError):
        RequirementMatch(**match(status="action_required"))


def test_memo_row_identifiers():
    data = memo()
    data["risks"][0]["risk_id"] = "R-1"
    with pytest.raises(ValidationError):
        Memo(**data)
    data = memo()
    data["questions_for_qa"][0]["question_id"] = "Q1"
    with pytest.raises(ValidationError):
        Memo(**data)
    data = memo()
    data["knockout_table"][0]["requirement_id"] = "KO-1"
    with pytest.raises(ValidationError):
        Memo(**data)


# --- Whole module --------------------------------------------------------------

EXPECTED_FIELDS = {
    Citation: ["document_id", "page", "quote", "verified"],
    KbEvidence: ["kb_id", "doc_slug", "quote"],
    ScreeningOutput: ["decision", "reason", "matched_services", "exclusion_flags", "confidence"],
    KnockoutCriterion: [
        "requirement_id", "category", "kind", "text", "threshold", "proof", "proof_when",
        "applies_to_lots", "consortium_allowed", "disproportionate", "citation",
        "overridden_by_qa",
    ],
    AwardCriterion: [
        "name", "kind", "weight_pct", "max_points", "description", "scoring_method",
        "subcriteria", "citation",
    ],
    ExtractionOutput: [
        "key_dates", "lots", "framework", "knockout_criteria", "award_basis", "award_criteria",
        "contract_risks", "documents_read", "documents_skipped", "warnings",
    ],
    RequirementMatch: [
        "requirement_id", "status", "reasoning", "tender_citation", "company_evidence", "gap",
        "mitigation", "unclear_reason",
    ],
    MatchingOutput: ["matches", "award_notes"],
    KeyDates: [
        "submission_deadline", "questions_deadline", "qa_publications", "site_visit",
        "presentation", "award_expected", "standstill_end", "tender_validity", "contract_start",
    ],
    KnockoutFinding: [
        "category", "kind", "text", "threshold", "proof", "proof_when", "applies_to_lots",
        "consortium_allowed", "disproportionate", "citation",
    ],
    ExtractionPartial: [
        "key_dates", "lots", "framework", "knockout_criteria", "award_basis", "award_criteria",
        "contract_risks",
    ],
    FitDraft: ["name", "score", "rationale", "citation", "kb_evidence"],
    AwardPosition: ["criterion_id", "position", "win_theme", "evidence"],
    MemoDraft: [
        "summary", "target_lots", "fit", "price_quality_tradeoff", "award_positions", "risks",
        "questions_for_qa",
    ],
    MemoReason: ["recommendation_reason", "reasons", "conditions"],
    FitScore: ["dimensions", "total", "provisional_min", "provisional_max"],
    Memo: [
        "tender_id", "language", "tender", "summary", "lots", "target_lots", "framework",
        "deadlines", "fit", "knockout_table", "award_basis", "price_quality_tradeoff",
        "award_table", "risks", "questions_for_qa", "recommendation", "recommendation_reason",
        "recommendation_reason_codes", "reasons", "conditions", "confidence",
        "reflects_qa_document_ids", "documents_reviewed", "citations", "limitations",
    ],
}  # fmt: skip


@pytest.mark.parametrize("model", EXPECTED_FIELDS, ids=lambda m: m.__name__)
def test_fields_match_architecture_section_5(model):
    assert list(model.model_fields) == EXPECTED_FIELDS[model]


def all_models() -> list[type[BaseModel]]:
    return [
        obj
        for _, obj in inspect.getmembers(schemas, inspect.isclass)
        if issubclass(obj, BaseModel) and obj.__module__ == schemas.__name__
    ]


@pytest.mark.parametrize("model", all_models(), ids=lambda m: m.__name__)
def test_json_schema_builds(model):
    json_schema = model.model_json_schema()  # AwardCriterion is recursive: a top-level $ref
    assert "properties" in json_schema or "$ref" in json_schema


@pytest.mark.parametrize("model", all_models(), ids=lambda m: m.__name__)
def test_no_schema_holds_contact_data(model):
    for name in model.model_fields:
        assert not any(word in name for word in ("contact", "email", "phone", "person"))


# --- Agent-loop additions ------------------------------------------------------


@pytest.mark.parametrize("reason", ["step_limit", "token_budget", "loop_failed"])
def test_unclear_reasons_for_a_stopped_agent_run(reason):
    parsed = RequirementMatch(
        **match(status="unclear", company_evidence=[], unclear_reason=reason, gap="Not assessed.")
    )
    assert parsed.unclear_reason == reason


def test_chat_answer():
    from pipeline.schemas import ChatAnswer

    data = {"answer": "De deadline is onbekend.", "citations": [citation()], "in_scope": True}
    parsed = ChatAnswer(**data)
    assert (
        parsed.kb_evidence == []
        and ChatAnswer.model_validate_json(parsed.model_dump_json()) == parsed
    )
    ChatAnswer(answer="Deze vraag gaat niet over de aanbesteding.", citations=[], in_scope=False)
    for bad in ({"answer": "x" * 2001}, {"in_scope": None}, {"citations": [{"page": 1}]}):
        with pytest.raises(ValidationError):
            ChatAnswer(**{**data, **bad})


# --- Map step ------------------------------------------------------------------


def partial(**overrides) -> dict:
    return {
        "key_dates": {"submission_deadline": None, "questions_deadline": None},
        "lots": [],
        "framework": None,
        "knockout_criteria": [],
        "award_basis": None,
        "award_criteria": [],
        "contract_risks": [],
        **overrides,
    }


def test_an_empty_partial_is_valid():
    parsed = ExtractionPartial(**partial())
    assert parsed.knockout_criteria == [] and parsed.key_dates.qa_publications == []


def test_a_finding_is_a_criterion_without_the_fields_the_reduce_step_sets():
    only_in_criterion = set(KnockoutCriterion.model_fields) - set(KnockoutFinding.model_fields)
    assert only_in_criterion == {"requirement_id", "overridden_by_qa"}
    assert set(KnockoutFinding.model_fields) < set(KnockoutCriterion.model_fields)
    finding = {k: v for k, v in knockout().items() if k != "requirement_id"}
    parsed = ExtractionPartial(**partial(knockout_criteria=[finding]))
    assert parsed.knockout_criteria[0].threshold == "ISO 27001"
    with pytest.raises(ValidationError):  # a finding has no citation to check
        ExtractionPartial(**partial(knockout_criteria=[{**finding, "citation": None}]))


@pytest.mark.parametrize("model", [ExtractionPartial, ExtractionOutput], ids=lambda m: m.__name__)
def test_a_contract_risk_needs_a_citation(model):
    data = partial() if model is ExtractionPartial else extraction()
    with pytest.raises(ValidationError):
        model(**{**data, "contract_risks": ["Unlimited liability"]})
    cited = {"text": "Unlimited liability", "citation": citation()}
    assert model(**{**data, "contract_risks": [cited]}).contract_risks[0].citation.page == 3


def test_planned_qa_dates_are_part_of_the_key_dates():
    dated = {"value": "2027-02-01", "citation": citation()}
    dates = KeyDates(submission_deadline=None, questions_deadline=None, qa_publications=[dated])
    assert dates.qa_publications[0].value == date(2027, 2, 1)
    with pytest.raises(ValidationError):
        KeyDates(submission_deadline=None, questions_deadline=None, qa_publications=["soon"])


# --- Model-facing memo schemas -------------------------------------------------

COPILOT_DIMENSIONS = get_args(CopilotFitName)

# Where each field of the stored Memo comes from. A new Memo field must be added here.
MEMO_FIELD_SOURCE = {
    "summary": "draft",
    "target_lots": "draft",
    "price_quality_tradeoff": "draft",
    "risks": "draft",
    "questions_for_qa": "draft",
    "fit": "draft_and_code",  # FitDraft rows, then compute_fit
    "award_table": "draft_and_code",  # AwardCriterion rows joined with AwardPosition
    "recommendation_reason": "reason",
    "reasons": "reason",
    "conditions": "reason",
    "lots": "carried",  # from extraction and matching, copied by code
    "framework": "carried",
    "award_basis": "carried",
    "knockout_table": "carried",
    "deadlines": "carried",  # the submission deadline inside it is code
    "tender_id": "code",
    "language": "code",
    "tender": "code",
    "recommendation": "code",
    "recommendation_reason_codes": "code",
    "confidence": "code",
    "reflects_qa_document_ids": "code",
    "documents_reviewed": "code",
    "citations": "code",
    "limitations": "code",
}
# Names of `[code]` items anywhere in the Memo. `verified` is on the shared Citation type.
CODE_ITEMS = {
    name for name, source in MEMO_FIELD_SOURCE.items() if source == "code"
} | {
    "total", "provisional_min", "provisional_max", "weight_pct", "scored_by",
    "submission_deadline", "verified",
}  # fmt: skip
# The columns of `memo_human_inputs` (ARCHITECTURE.md section 3) and the approval fields.
HUMAN_ITEMS = {
    "incumbent", "fit_win_probability", "fit_commercial", "fit_strategic_value",
    "fit_rationale", "milestones", "row_owners", "risk_overrides", "deadline_realistic",
    "price_to_win", "bid_investment", "updated_by", "decided_by", "owner",
}  # fmt: skip


def fit_draft(name: str = "scope_match", **overrides) -> dict:
    return {
        "name": name,
        "score": 4,
        "rationale": "Core service.",
        "citation": citation(),
        "kb_evidence": [],
        **overrides,
    }


def memo_draft(**overrides) -> dict:
    return {
        "summary": "A municipality buys a data platform.",
        "target_lots": [],
        "fit": [fit_draft(name) for name in COPILOT_DIMENSIONS],
        "price_quality_tradeoff": None,
        "award_positions": [
            {"criterion_id": "AC-01", "position": "medium", "win_theme": None, "evidence": []}
        ],
        "risks": memo()["risks"],
        "questions_for_qa": memo()["questions_for_qa"],
        **overrides,
    }


def property_names(model: type[BaseModel]) -> set[str]:
    """Every property name at any depth of the model's JSON schema."""
    names: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            names.update(node.get("properties", {}))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(model.model_json_schema())
    return names


def test_memo_draft_round_trip():
    parsed = MemoDraft(**memo_draft())
    assert MemoDraft.model_validate_json(parsed.model_dump_json()) == parsed
    assert [d.name for d in parsed.fit] == list(COPILOT_DIMENSIONS)


def test_every_memo_field_has_exactly_one_source():
    assert set(MEMO_FIELD_SOURCE) == set(Memo.model_fields)
    by_source = {
        source: {name for name, s in MEMO_FIELD_SOURCE.items() if s == source}
        for source in set(MEMO_FIELD_SOURCE.values())
    }
    draft, reason = set(MemoDraft.model_fields), set(MemoReason.model_fields)
    assert draft == by_source["draft"] | {"fit", "award_positions"}
    assert reason == by_source["reason"]
    assert not (draft | reason) & (by_source["code"] | by_source["carried"])


def test_the_model_facing_memo_schemas_hold_no_code_item():
    # `Citation.verified` is the one exception: always overwritten by the citation check.
    assert property_names(MemoDraft) & CODE_ITEMS == {"verified"}
    assert property_names(MemoReason) & CODE_ITEMS == set()


@pytest.mark.parametrize("model", [MemoDraft, MemoReason], ids=lambda m: m.__name__)
def test_the_model_facing_memo_schemas_hold_no_human_item(model):
    names = property_names(model)
    assert not names & HUMAN_ITEMS
    assert not [name for name in names if "owner" in name or "human" in name]


def test_the_draft_scores_only_the_copilot_fit_dimensions():
    copilot = [name for name, (_, scored_by) in FIT_WEIGHTS.items() if scored_by == "copilot"]
    assert sorted(COPILOT_DIMENSIONS) == sorted(copilot)
    as_json = MemoDraft.model_json_schema()
    assert sorted(as_json["$defs"]["FitDraft"]["properties"]["name"]["enum"]) == sorted(copilot)
    for human in set(FIT_WEIGHTS) - set(copilot):
        assert f'"{human}"' not in json.dumps(as_json)


@pytest.mark.parametrize("model", [MemoDraft, MemoReason], ids=lambda m: m.__name__)
def test_the_model_facing_memo_schemas_hold_no_contact_field(model):
    names = property_names(model)
    assert contact_keys(dict.fromkeys(names)) == []
    assert not [n for n in names if any(w in n for w in ("contact", "email", "phone", "person"))]


def test_fit_draft_needs_a_rationale_and_a_source_for_a_score():
    assert FitDraft(**fit_draft()).score == 4
    unscored = fit_draft(score=None, rationale="Niet gevonden in stukken", citation=None)
    assert FitDraft(**unscored).score is None
    FitDraft(**fit_draft(citation=None, kb_evidence=[kb_evidence()]))
    for bad in (
        {"rationale": None},
        {"rationale": "  "},
        {"citation": None},
        {"score": 0},
        {"score": 6},
        {"name": "win_probability"},
        {"rationale": "x" * 401},
    ):
        with pytest.raises(ValidationError):
            FitDraft(**fit_draft(**bad))


def test_memo_draft_needs_each_copilot_dimension_exactly_once():
    four = [fit_draft(name) for name in COPILOT_DIMENSIONS]
    for bad in (four[:3], [*four, four[0]], [*four[:3], four[0]], []):
        with pytest.raises(ValidationError, match="four copilot dimensions"):
            MemoDraft(**memo_draft(fit=bad))


def test_award_position_and_memo_reason_bounds():
    AwardPosition(criterion_id="AC-12", position=None, win_theme=None)
    for bad in ("AC-1", "KO-01", "ac-01"):
        with pytest.raises(ValidationError):
            AwardPosition(criterion_id=bad, position=None, win_theme=None)
    with pytest.raises(ValidationError):
        AwardPosition(criterion_id="AC-01", position="strong", win_theme=None)

    reason = {"recommendation_reason": "KO-03 is not met.", "reasons": ["a"], "conditions": []}
    assert MemoReason(**reason).conditions == []
    for bad in ({"reasons": ["a", "b", "c", "d"]}, {"recommendation_reason": "x" * 601}):
        with pytest.raises(ValidationError):
            MemoReason(**{**reason, **bad})
