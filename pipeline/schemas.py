"""Typed contracts between pipeline steps (ARCHITECTURE.md section 5).

These are the shapes of `assessments.output` and `memos.content`. `Memo` is the stored shape,
not a model output shape: its `[code]` parts are filled by application code. No schema here
holds contact data (names, emails, phone numbers of persons).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Self, get_args

from pydantic import BaseModel, Field, model_validator

# --- 5 shared ------------------------------------------------------------------


class Citation(BaseModel):
    """A checkable pointer into a tender document."""

    document_id: int
    page: int = Field(ge=1)
    quote: str = Field(min_length=10, max_length=600, description="Verbatim text from that page")
    verified: bool | None = None  # set by code after the citation check, never by the model


class KbEvidence(BaseModel):
    """A pointer into the company knowledge base. Not yet verified in code."""

    kb_id: int
    doc_slug: str
    quote: str = Field(max_length=600)


class KeyDateTime(BaseModel):
    value: datetime
    citation: Citation


class KeyDate(BaseModel):
    value: date
    citation: Citation


class CitedText(BaseModel):
    text: str = Field(max_length=300)
    citation: Citation


Level = Literal["high", "medium", "low"]

# --- 5.1 screening -------------------------------------------------------------

ExclusionFlag = Literal[
    "hardware_supply",
    "staffing_only",
    "security_clearance",
    "onsite_abroad",
    "ops_24_7",
    "pure_custom_software",
    "liability",
    "none",
]
ServiceLine = Literal[
    "data_platform",
    "ai_ml",
    "bi_dashboards",
    "cloud_migration",
    "data_governance",
]


class ScreeningOutput(BaseModel):
    decision: Literal["shortlist", "reject", "unsure"]
    reason: str = Field(max_length=400, description="One or two sentences citing the notice")
    matched_services: list[ServiceLine]
    exclusion_flags: list[ExclusionFlag]
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _none_is_exclusive(self) -> Self:
        if "none" in self.exclusion_flags and len(self.exclusion_flags) > 1:
            raise ValueError("exclusion flag 'none' cannot be combined with other flags")
        return self


# --- 5.2 extraction ------------------------------------------------------------


class KeyDates(BaseModel):
    """Dates read from the documents, each with its own citation."""

    # Graded against TenderNed `sluitingsDatum` only. Never shown in a memo.
    submission_deadline: KeyDateTime | None
    questions_deadline: KeyDateTime | None
    site_visit: KeyDateTime | None = None
    presentation: KeyDateTime | None = None  # presentation, interview or demo
    award_expected: KeyDate | None = None
    standstill_end: KeyDate | None = None  # end of the Alcatel period
    tender_validity: CitedText | None = None  # gestanddoeningstermijn, often a duration
    contract_start: KeyDate | None = None


class LotScope(BaseModel):
    lot_identifier: str | None  # None when the tender has no lots
    title: str
    summary: str = Field(max_length=600)
    estimated_value_eur: Decimal | None
    ceiling_value_eur: Decimal | None  # plafondbedrag
    value_includes_vat: bool | None
    duration_months: int | None
    extension_months: int | None
    citations: list[Citation]


RequirementCategory = Literal[
    "exclusion_ground",
    "financial",
    "insurance",
    "certification",
    "reference_project",
    "staff_qualification",
    "technical_capacity",
    "legal_form",
    "language",
    "security",
    "other",
]
KnockoutKind = Literal["exclusion", "suitability", "minimum_requirement", "formal"]
ProofWhen = Literal["uea", "with_bid", "on_request"]
AwardBasis = Literal["bpkv", "lowest_price", "lowest_cost"]
Framework = Literal["none", "single", "multi"]

KO_ID_PATTERN = r"^KO-\d{2,}$"


class KnockoutCriterion(BaseModel):
    requirement_id: str = Field(pattern=KO_ID_PATTERN)  # "KO-01", assigned in the reduce step
    category: RequirementCategory
    kind: KnockoutKind
    text: str = Field(description="The requirement restated in one sentence")
    threshold: str | None = Field(
        description="Number, date or named standard, e.g. 'EUR 1,000,000 per year'"
    )
    proof: str | None = Field(description="The evidence the buyer asks for")
    proof_when: ProofWhen | None
    applies_to_lots: list[str] = []  # empty means all lots
    consortium_allowed: bool | None  # may be met jointly or via a third party
    disproportionate: bool = False  # looks disproportionate (Gids Proportionaliteit)
    citation: Citation
    overridden_by_qa: bool = False


class AwardCriterion(BaseModel):
    name: str
    kind: Literal["price", "quality", "cost"]
    weight_pct: float | None = Field(ge=0, le=100)
    max_points: float | None
    description: str = Field(max_length=600)
    scoring_method: str | None = Field(description="Scale, panel, page limit or price formula")
    subcriteria: list[AwardCriterion] = []
    citation: Citation


class ExtractionOutput(BaseModel):
    key_dates: KeyDates
    lots: list[LotScope]
    framework: Framework | None
    knockout_criteria: list[KnockoutCriterion]
    award_basis: AwardBasis | None
    award_criteria: list[AwardCriterion]
    contract_risks: list[str] = Field(description="Liability, penalties, IP, exit terms")
    documents_read: list[int]
    documents_skipped: list[int]
    warnings: list[str] = Field(description="E.g. scanned document, token budget reached")


# --- 5.3 requirement matching --------------------------------------------------

MatchStatus = Literal["met", "not_met", "unclear"]
Mitigation = Literal[
    "none",
    "consortium",
    "subcontractor",
    "third_party",  # beroep op derden
    "ask_in_qa",
    "obtain_before_deadline",
]


UnclearReason = Literal[
    "vague_requirement",
    "no_evidence_found",
    "conflicting_documents",
    "step_limit",  # the agent loop used all its steps
    "token_budget",  # the agent loop used its token budget
    "loop_failed",  # too many tool errors, or the model call failed
]


class RequirementMatch(BaseModel):
    requirement_id: str
    status: MatchStatus
    reasoning: str = Field(max_length=500)
    tender_citation: Citation
    company_evidence: list[KbEvidence]
    gap: str | None = Field(description="What is missing, when not_met or unclear")
    mitigation: Mitigation
    unclear_reason: UnclearReason | None

    @model_validator(mode="after")
    def _status_rules(self) -> Self:
        if self.status == "met" and not self.company_evidence:
            raise ValueError("status 'met' needs at least one company_evidence item")
        if self.status == "unclear" and self.unclear_reason is None:
            raise ValueError("status 'unclear' needs an unclear_reason")
        if self.status == "not_met" and not (self.gap and self.gap.strip()):
            raise ValueError("status 'not_met' needs a gap")
        return self


class MatchingOutput(BaseModel):
    matches: list[RequirementMatch]
    award_notes: list[str]  # one short strength or weakness per award criterion


# --- 5.5 chat ------------------------------------------------------------------


class ChatAnswer(BaseModel):
    """The final answer of one chat question (ARCHITECTURE.md 2.6)."""

    answer: str = Field(max_length=2000)
    citations: list[Citation]  # tender documents; verified in code like any citation
    kb_evidence: list[KbEvidence] = []
    in_scope: bool  # False: the question is not about this tender and is declined


# --- 5.4 memo ------------------------------------------------------------------

FitDimensionName = Literal[
    "scope_match",
    "win_probability",
    "references",
    "commercial",
    "capacity_timing",
    "contract_risk",
    "strategic_value",
]
Recommendation = Literal["bid", "bid_conditional", "no_bid", "needs_review"]
DocType = Literal[
    "guideline",
    "requirements",
    "qa",
    "notice",
    "contract",
    "pricing",
    "form",
    "annex",
    "other",
]


class FitDimension(BaseModel):
    name: FitDimensionName
    weight_pct: int = Field(ge=0, le=100)  # set by code from the fixed table
    scored_by: Literal["copilot", "human"]
    score: int | None = Field(default=None, ge=1, le=5)  # None: not scored
    rationale: str | None = Field(default=None, max_length=400)
    citation: Citation | None = None
    kb_evidence: list[KbEvidence] = []

    @model_validator(mode="after")
    def _copilot_score_needs_source(self) -> Self:
        if self.scored_by == "copilot" and self.score is not None:
            if not (self.rationale and self.rationale.strip()):
                raise ValueError("a copilot score needs a rationale")
            if self.citation is None and not self.kb_evidence:
                raise ValueError("a copilot score needs a citation or KB evidence")
        return self


class FitScore(BaseModel):
    """Computed in code (`pipeline/memo.py`) from the seven dimensions."""

    dimensions: list[FitDimension]
    total: int | None = Field(ge=0, le=100)  # None while any dimension is unscored
    provisional_min: int = Field(ge=0, le=100)  # unscored dimensions at 1
    provisional_max: int = Field(ge=0, le=100)  # unscored dimensions at 5

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        names = sorted(d.name for d in self.dimensions)
        if names != sorted(get_args(FitDimensionName)):
            raise ValueError("fit needs each of the seven dimensions exactly once")
        if sum(d.weight_pct for d in self.dimensions) != 100:
            raise ValueError("fit weights must sum to 100")
        if self.provisional_min > self.provisional_max:
            raise ValueError("provisional_min is above provisional_max")
        if self.total is not None and not (
            self.total == self.provisional_min == self.provisional_max
        ):
            raise ValueError("a final total must equal provisional_min and provisional_max")
        return self


class TenderFacts(BaseModel):
    """Snapshot of the `tenders` row, filled by code. Organisation data only."""

    buyer_name: str
    title: str
    tenderned_id: str | None
    tenderned_url: str | None
    main_nature: str | None
    procedure_type: str | None
    eu_procedure: bool | None  # TenderNed nationaalOfEuropeesCode; None without a match
    cpv_codes: list[str]
    documents_access: Literal["tenderned", "other_platform", "login_required", "none"]
    published_at: datetime | None


class MemoDeadlines(BaseModel):
    # From `tenders.deadline_at` (TenderNed API) only; None is shown as "unknown".
    submission_deadline: datetime | None
    questions_deadline: KeyDateTime | None
    qa_publications: list[KeyDate] = []  # Nota van Inlichtingen 1, 2, ...
    site_visit: KeyDateTime | None = None
    presentation: KeyDateTime | None = None
    award_expected: KeyDate | None = None
    standstill_end: KeyDate | None = None
    tender_validity: CitedText | None = None
    contract_start: KeyDate | None = None


class KnockoutRow(BaseModel):
    requirement_id: str = Field(pattern=KO_ID_PATTERN)
    requirement: str
    kind: KnockoutKind
    proof: str | None
    proof_when: ProofWhen | None
    status: MatchStatus
    evidence: list[KbEvidence]
    gap: str | None
    mitigation: Mitigation
    disproportionate: bool = False
    tender_citation: Citation


class AwardRow(BaseModel):
    name: str
    kind: Literal["price", "quality", "cost"]
    weight_pct: float | None = Field(ge=0, le=100)
    max_points: float | None
    scoring_method: str | None
    position: Level | None  # our position; None when there is no basis to judge
    win_theme: str | None
    evidence: list[KbEvidence] = []
    citation: Citation


class RiskRow(BaseModel):
    risk_id: str = Field(pattern=r"^R-\d{2,}$")
    text: str
    category: Literal[
        "contract_terms",
        "liability_penalties",
        "ip",
        "indexation",
        "volume",
        "social_return",
        "sustainability",
        "privacy_security",
        "subcontracting",
        "capacity",
        "other",
    ]
    likelihood: Level
    impact: Level
    mitigation: Literal["price_in", "ask_in_qa", "accept"]
    citation: Citation | None = None  # None for risks on our own side, e.g. capacity


class QaQuestion(BaseModel):
    question_id: str = Field(pattern=r"^Q-\d{2,}$")
    question: str  # always Dutch, whatever the memo language
    purpose: Literal["clarification", "contradiction", "proportionality", "error"]
    linked_to: list[str] = []  # KO-.. and R-.. identifiers
    priority: Level
    citation: Citation | None = None


class ReviewedDocument(BaseModel):
    document_id: int
    tag: str  # the ID used in source tags, e.g. "LEIDRAAD", "NvI-2"
    filename: str
    doc_type: DocType
    published_at: date | None  # TenderNed `datumPublicatie`


class Memo(BaseModel):
    tender_id: int
    language: Literal["nl", "en"]  # code always sets "en" (decision Q10)
    tender: TenderFacts
    summary: str = Field(max_length=1200)
    lots: list[LotScope]
    target_lots: list[str]
    framework: Framework | None
    deadlines: MemoDeadlines
    fit: FitScore
    knockout_table: list[KnockoutRow]
    award_basis: AwardBasis | None
    price_quality_tradeoff: str | None
    award_table: list[AwardRow]
    risks: list[RiskRow]
    questions_for_qa: list[QaQuestion]
    recommendation: Recommendation  # decided in code, never by the model
    recommendation_reason: str = Field(max_length=600)  # one sentence, written by the model
    recommendation_reason_codes: list[str]  # from the recommendation rules
    reasons: list[str] = Field(max_length=3)
    conditions: list[str]  # what must be true, and by when, for bid_conditional
    confidence: Level
    reflects_qa_document_ids: list[int]  # the Nota's van Inlichtingen this memo reflects
    documents_reviewed: list[ReviewedDocument]
    citations: list[Citation]  # de-duplicated union of all citations above, built by code
    limitations: list[str]  # e.g. "documents not accessible", "2 citations unverified"
