"""`Citation.verified` belongs to code: a model cannot set it, and the page check always does.

All page text is invented; the contact below is invented too.
"""

from __future__ import annotations

from pydantic import BaseModel

from agent.fakes import FakeBackend
from llm.prompts import PromptRef, UntrustedText
from llm.redact import Redactor
from llm.verified import reset_verified
from pipeline.citations import (
    PageText,
    verify_citation,
    verify_citations,
    verify_redacted_citation,
)
from pipeline.match import match_requirement
from pipeline.memo import FIT_WEIGHTS, recommend
from pipeline.schemas import (
    Citation,
    ExtractionOutput,
    FitDimension,
    KnockoutCriterion,
    RequirementMatch,
)
from tests.fakes import CONTEXT, SONNET, FakeProvider, make_gateway, reply
from tests.test_schemas import citation, extraction, knockout, match

DOC = 11
ON_PAGE = "De inschrijver beschikt over een geldig ISO 27001-certificaat."
INVENTED = "De inschrijver moet een omzet van tien miljard euro hebben."
PAGES = {
    (DOC, 14): PageText(DOC, 14, f"4.2 Geschiktheidseisen\n{ON_PAGE}\nEinde van de pagina."),
    (DOC, 15): PageText(
        DOC,
        15,
        "Vragen stelt u aan mevrouw A. Voorbeeld via a.voorbeeld@gemeente.example\n"
        "of telefonisch via 06-12345678, uiterlijk tien dagen voor de sluiting.",
    ),
}


def lookup(document_id: int, page_no: int) -> PageText | None:
    return PAGES.get((document_id, page_no))


def cite(quote: str = ON_PAGE, *, page: int = 14, verified: bool | None = None) -> Citation:
    return Citation(document_id=DOC, page=page, quote=quote, verified=verified)


def all_citations(value) -> list[Citation]:
    """Every Citation inside a model, found by walking its fields."""
    found: list[Citation] = []

    def walk(item) -> None:
        if isinstance(item, Citation):
            found.append(item)
        elif isinstance(item, BaseModel):
            for name in type(item).model_fields:
                walk(getattr(item, name))
        elif isinstance(item, list | tuple):
            for part in item:
                walk(part)

    walk(value)
    return found


# --- reset on every model output -----------------------------------------------


def flagged_extraction() -> dict:
    """An extraction answer in which the model set `verified: true` on every citation."""
    text = ExtractionOutput.model_validate(extraction()).model_dump_json()
    return ExtractionOutput.model_validate_json(
        text.replace('"verified":null', '"verified":true')
    ).model_dump(mode="json")


def test_reset_verified_clears_every_nested_citation_and_leaves_the_input_alone():
    output = ExtractionOutput.model_validate(flagged_extraction())
    before = all_citations(output)
    assert len(before) >= 6 and all(c.verified is True for c in before)

    cleared = reset_verified(output)
    assert [c.verified for c in all_citations(cleared)] == [None] * len(before)
    assert all(c.verified is True for c in all_citations(output))  # a copy, not a mutation
    assert isinstance(cleared, ExtractionOutput)
    assert cleared.knockout_criteria[0].text == output.knockout_criteria[0].text
    assert reset_verified(cleared) is cleared  # nothing to change, nothing copied


def test_reset_verified_handles_lists_and_false():
    cleared = reset_verified([cite(verified=True), cite(verified=False), cite()])
    assert [c.verified for c in cleared] == [None, None, None]


def test_reset_verified_works_on_any_pydantic_model_not_only_citations():
    class Claim(BaseModel):
        text: str
        verified: bool | None = None

    class Answer(BaseModel):
        claims: list[Claim]
        by_topic: dict[str, Claim]
        verified: bool = True  # even a top-level flag is cleared
        note: str

    answer = Answer(
        claims=[Claim(text="a", verified=True), Claim(text="b")],
        by_topic={"x": Claim(text="c", verified=False)},
        note="kept",
    )
    cleared = reset_verified(answer)
    assert cleared.verified is None and cleared.note == "kept"
    assert [c.verified for c in cleared.claims] == [None, None]
    assert cleared.by_topic["x"].verified is None
    assert cleared.claims[1] is answer.claims[1]  # unchanged parts are reused
    assert answer.verified is True and answer.claims[0].verified is True  # input untouched
    assert reset_verified("plain text") == "plain text"


def test_gateway_call_drops_a_model_set_verified_flag(tmp_path):
    provider = FakeProvider(reply(flagged_extraction()))
    gateway, _ = make_gateway(tmp_path, provider)
    output = gateway.call(
        task="screening",
        prompt=PromptRef("screen", "v1"),
        variables={"company": "Datalink B.V."},
        data=[UntrustedText("notice", "Dataplatform voor een gemeente.")],
        schema=ExtractionOutput,
        model=SONNET,
        redactor=Redactor(),
        context=CONTEXT,
    )
    assert provider.script == []  # the flagged answer was consumed
    citations = all_citations(output)
    assert citations and all(c.verified is None for c in citations)


def test_gateway_step_drops_a_model_set_verified_flag(tmp_path):
    answer = match(tender_citation=citation(verified=True))
    gateway, _ = make_gateway(tmp_path, FakeProvider(reply(answer)))
    step = gateway.step(
        task="matching",
        prompt=PromptRef("match", "v2"),
        data=[UntrustedText("requirement", "ISO 27001")],
        history=(),
        tools=(),
        final_schema=RequirementMatch,
        model=SONNET,
        redactor=Redactor(),
        context=CONTEXT,
        step_index=0,
    )
    assert step.output is not None and step.output.tender_citation.verified is None


# --- a model-set `true` has no effect on the recommendation --------------------


def criterion(quote: str = ON_PAGE) -> KnockoutCriterion:
    return KnockoutCriterion(
        **knockout(requirement_id="KO-07", citation=citation(document_id=DOC, page=14, quote=quote))
    )


def model_match(tmp_path, quote: str) -> RequirementMatch:
    """A `not_met` answer whose citation the model itself marked as verified."""
    answer = match(
        status="not_met",
        company_evidence=[],
        gap="No certificate on file.",
        tender_citation=citation(document_id=DOC, page=14, quote=quote, verified=True),
    )
    gateway, _ = make_gateway(tmp_path, FakeProvider(reply(answer)))
    return match_requirement(
        criterion(),
        gateway=gateway,
        backend=FakeBackend(),
        model=SONNET,
        redactor=Redactor(),
        context=CONTEXT,
    )


def decide(knockouts, matches, dimensions=()):
    return recommend(
        knockouts=knockouts,
        matches=matches,
        dimensions=dimensions,
        documents_missing=False,
        pages=lookup,
        redactor=Redactor(),
    )


def test_a_model_set_true_on_an_invented_quote_never_gives_no_bid(tmp_path):
    result = model_match(tmp_path, INVENTED)
    assert result.status == "not_met" and result.tender_citation.verified is None

    checked = decide([criterion()], [result])
    assert checked.result.recommendation == "needs_review"
    assert checked.result.reasons == ("knockout_not_met_unverified",)
    assert checked.matches[0].tender_citation.verified is False
    assert checked.knockouts[0].citation.verified is True


def test_the_same_answer_with_a_real_quote_gives_no_bid(tmp_path):
    checked = decide([criterion()], [model_match(tmp_path, ON_PAGE)])
    assert checked.result.recommendation == "no_bid"
    assert checked.matches[0].tender_citation.verified is True


def test_recommend_overwrites_flags_given_by_the_caller():
    forged_knockout = criterion(INVENTED).model_copy(
        update={"citation": cite(INVENTED, verified=True)}
    )
    forged_match = RequirementMatch(
        **match(
            requirement_id="KO-07",
            status="not_met",
            company_evidence=[],
            gap="No certificate on file.",
            tender_citation=cite(INVENTED, verified=True).model_dump(),
        )
    )
    checked = decide([forged_knockout], [forged_match])
    assert checked.result.recommendation == "needs_review"
    assert checked.knockouts[0].citation.verified is False
    assert forged_knockout.citation.verified is True  # the inputs are not changed


def test_recommend_checks_fit_citations_before_scoring():
    weight, scored_by = FIT_WEIGHTS["scope_match"]

    def scope(quote: str) -> FitDimension:
        return FitDimension(
            name="scope_match",
            weight_pct=weight,
            scored_by=scored_by,
            score=5,
            rationale="The scope is a data platform.",
            citation=cite(quote, verified=True),
        )

    met = RequirementMatch(
        **match(requirement_id="KO-07", tender_citation=cite(ON_PAGE).model_dump())
    )
    forged = decide([criterion()], [met], [scope(INVENTED)])
    real = decide([criterion()], [met], [scope(ON_PAGE)])
    assert next(d for d in forged.fit.dimensions if d.name == "scope_match").score is None
    assert next(d for d in real.fit.dimensions if d.name == "scope_match").score == 5


def test_verify_citations_sets_true_or_false_and_a_missing_page_is_false():
    checked = verify_citations(
        [cite(ON_PAGE), cite(INVENTED, verified=True), cite(ON_PAGE, page=99, verified=True)],
        lookup,
        Redactor(),
    )
    assert [c.verified for c in checked] == [True, False, False]


# --- quotes copied from redacted text ------------------------------------------

REDACTOR = Redactor(["A. Voorbeeld"])


def test_a_quote_with_redaction_markers_matches_the_redacted_page():
    quote = "Vragen stelt u aan [NAME] via [EMAIL] of telefonisch via [PHONE], uiterlijk"
    page = PAGES[(DOC, 15)]
    assert verify_citation(cite(quote, page=15), page).reason == "not_found"
    assert verify_redacted_citation(cite(quote, page=15), page, REDACTOR).passed
    assert verify_citations(cite(quote, page=15), lookup, REDACTOR).verified is True


def test_a_quote_holding_the_unredacted_contact_fails():
    # The model never saw this text, so it cannot have copied it from its input.
    quote = "Vragen stelt u aan mevrouw A. Voorbeeld via"
    page = PAGES[(DOC, 15)]
    assert verify_citation(cite(quote, page=15), page).passed
    assert verify_redacted_citation(cite(quote, page=15), page, REDACTOR).reason == "not_found"


def test_redacted_check_without_contact_data_equals_the_plain_check():
    page = PAGES[(DOC, 14)]
    assert verify_redacted_citation(cite(ON_PAGE), page, REDACTOR) == verify_citation(
        cite(ON_PAGE), page
    )
    assert verify_redacted_citation(cite(ON_PAGE), None, REDACTOR).reason == "page_missing"
