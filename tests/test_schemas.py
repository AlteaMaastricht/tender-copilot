"""Schemas from ARCHITECTURE.md section 5: bounds, literals, validators and round-trips."""

from __future__ import annotations

import inspect
from datetime import date, datetime
from decimal import Decimal

import pytest
from pydantic import BaseModel, ValidationError

from pipeline import schemas
from pipeline.memo import compute_fit
from pipeline.schemas import (
    AwardCriterion,
    Citation,
    ExtractionOutput,
    FitDimension,
    FitScore,
    KbEvidence,
    KeyDates,
    KeyDateTime,
    KnockoutCriterion,
    MatchingOutput,
    Memo,
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
        "contract_risks": ["Unlimited liability"],
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
