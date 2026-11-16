"""Citation verifier. All page text is invented; no probe documents, no person names."""

from __future__ import annotations

from typing import get_args

import pytest

from pipeline.citations import CitationCheck, PageText, Reason, verify_citation
from pipeline.schemas import Citation

DOC = 11
PAGE_NO = 14

PAGE_TEXT = (
    "4.2 Geschiktheidseisen\n"
    "De inschrijver dient te beschikken over een geldig ISO 27001-certifi-\n"
    "caat of gelijkwaardig.  De gemiddelde jaaromzet over de laatste drie\n"
    "boekjaren bedraagt ten minste €\u00a01.000.000 exclusief btw.\n"
    "De opdracht betreft ICT-\n"
    "dienstverlening voor de gemeentelijke organisatie.\n"
    "De inschrijver verklaart dat hij \u201eniet in staat van faillissement\u201d verkeert\n"
    "en dat \u2019s nachts geen onderhoud plaatsvindt.\n"
    "De \ufb01nanciële draagkracht en de kwali\ufb01catie worden getoetst.\n"
    "Looptijd: 2027\u20132029, co\u00adordinatie door de opdrachtgever.\n"
    "5 Gunningscriteria ........ 17\n"
    "The tenderer must submit two reference projects completed in the last three years."
)
NEXT_PAGE_TEXT = (
    "4.3 Verzekering\n"
    "De inschrijver heeft een beroepsaansprakelijkheidsverzekering met een dekking\n"
    "van ten minste € 2.500.000 per gebeurtenis."
)

PAGE = PageText(DOC, PAGE_NO, PAGE_TEXT)


def cite(quote: str, *, document_id: int = DOC, page: int = PAGE_NO, verified=None) -> Citation:
    return Citation(document_id=document_id, page=page, quote=quote, verified=verified)


# --- passes --------------------------------------------------------------------

PASSES = {
    "exact": "De opdracht betreft ICT-",
    "line_breaks_and_double_spaces": (
        "caat of gelijkwaardig. De gemiddelde jaaromzet over de laatste drie boekjaren"
    ),
    "extra_whitespace_in_quote": "gemiddelde   jaaromzet\nover de laatste\tdrie boekjaren",
    "non_breaking_space_on_page": "ten minste € 1.000.000 exclusief btw",
    "non_breaking_space_in_quote": "ten minste €\u00a01.000.000 exclusief\u00a0btw",
    "split_word_rejoined": "een geldig ISO 27001-certificaat of gelijkwaardig",
    "split_word_copied_with_line_break": "ISO 27001-certifi-\ncaat of gelijkwaardig",
    "compound_hyphen_at_line_break": "betreft ICT-dienstverlening voor de gemeentelijke",
    "soft_hyphen_on_page": "coordinatie door de opdrachtgever",
    "straight_quotes_for_dutch_low_quotes": 'hij "niet in staat van faillissement" verkeert',
    "curly_quotes": "hij \u201cniet in staat van faillissement\u201d verkeert",
    "straight_apostrophe": "en dat 's nachts geen onderhoud plaatsvindt",
    "ligatures_on_page": "De financiële draagkracht en de kwalificatie worden getoetst",
    "ligature_in_quote": "de kwali\ufb01catie worden getoetst",
    "hyphen_for_en_dash": "Looptijd: 2027-2029",
    "em_dash_for_en_dash": "Looptijd: 2027\u20142029",
    "case_difference": "DE OPDRACHT BETREFT ict-DIENSTVERLENING",
    "decomposed_accent_in_quote": "De financie\u0308le draagkracht",
    "wrapped_in_straight_quotes": '"De financiële draagkracht en de kwalificatie"',
    "wrapped_in_curly_quotes": "\u201cThe tenderer must submit two reference projects\u201d",
    "dot_leaders_that_are_on_the_page": "5 Gunningscriteria ........ 17",
    "english": "two reference projects completed in the last three years",
}


@pytest.mark.parametrize("quote", PASSES.values(), ids=PASSES.keys())
def test_quote_on_the_page_passes(quote):
    assert verify_citation(cite(quote), PAGE) == CitationCheck(True, "ok")


def test_decomposed_accent_on_page_passes():
    page = PageText(DOC, PAGE_NO, "De financie\u0308le draagkracht wordt getoetst.")
    assert verify_citation(cite("De financiële draagkracht"), page).passed


# --- fails ---------------------------------------------------------------------

FAILS = {
    "paraphrase": ("De inschrijver moet een geldig ISO 27001-certificaat hebben", "not_found"),
    "one_word_changed": ("De gemiddelde jaaromzet over de laatste vijf boekjaren", "not_found"),
    "changed_number": ("bedraagt ten minste € 2.000.000 exclusief btw", "not_found"),
    "other_number_format_comma": ("ten minste € 1,000,000 exclusief btw", "not_found"),
    "other_number_format_plain": ("ten minste € 1000000 exclusief btw", "not_found"),
    "missing_accent": ("De financiele draagkracht en de kwalificatie", "not_found"),
    "translation": ("The tenderer must hold a valid ISO 27001 certificate", "not_found"),
    "reordered_words": ("De draagkracht financiële en de kwalificatie", "not_found"),
    "fragments_joined_without_marker": (
        "De opdracht betreft ICT-dienstverlening en dat 's nachts geen onderhoud plaatsvindt",
        "not_found",
    ),
    "fragments_joined_by_three_dots": (
        "De opdracht betreft ICT-dienstverlening ... en dat 's nachts geen onderhoud",
        "quote_has_ellipsis",
    ),
    "fragments_joined_by_ellipsis_character": (
        "De opdracht betreft ICT-dienstverlening \u2026 worden getoetst",
        "quote_has_ellipsis",
    ),
    "fragments_joined_by_bracketed_dots": (
        "De opdracht betreft ICT-dienstverlening [...] worden getoetst",
        "quote_has_ellipsis",
    ),
    "fragments_joined_by_bracketed_ellipsis": (
        "De opdracht betreft ICT-dienstverlening (\u2026) worden getoetst",
        "quote_has_ellipsis",
    ),
    "on_the_next_page_only": ("dekking van ten minste € 2.500.000 per gebeurtenis", "not_found"),
    "across_the_page_break": (
        "completed in the last three years. 4.3 Verzekering De inschrijver heeft",
        "not_found",
    ),
    "whitespace_only": (" " * 12, "quote_too_short"),
    "punctuation_only": ("." * 12, "quote_too_short"),
    "nine_letters": ("ICT-dienst", "quote_too_short"),
    "short_after_stripping_quotes": ('"geldig ISO"', "quote_too_short"),
}


@pytest.mark.parametrize(("quote", "reason"), FAILS.values(), ids=FAILS.keys())
def test_quote_not_on_the_page_fails(quote, reason):
    assert verify_citation(cite(quote), PAGE) == CitationCheck(False, reason)


def test_ten_letters_is_long_enough():
    assert verify_citation(cite("ICT-dienstz"), PAGE).reason == "not_found"
    assert verify_citation(cite("gelijkwaardig"), PAGE).passed


def test_quote_on_the_next_page_passes_only_when_that_page_is_cited():
    quote = "dekking van ten minste € 2.500.000 per gebeurtenis"
    next_page = PageText(DOC, PAGE_NO + 1, NEXT_PAGE_TEXT)
    assert verify_citation(cite(quote, page=PAGE_NO + 1), next_page).passed
    # The right text handed in for the wrong page is refused, not searched.
    assert verify_citation(cite(quote), next_page) == CitationCheck(False, "page_mismatch")


def test_same_page_number_of_another_document_fails():
    other = PageText(DOC + 1, PAGE_NO, PAGE_TEXT)
    check = verify_citation(cite("De opdracht betreft ICT-dienstverlening"), other)
    assert check == CitationCheck(False, "page_mismatch")


def test_missing_page_fails():
    check = verify_citation(cite("De opdracht betreft ICT-dienstverlening"), None)
    assert check == CitationCheck(False, "page_missing")


@pytest.mark.parametrize("text", ["", " \n\t ", "\u00ad\u200b"])
def test_empty_page_fails(text):
    check = verify_citation(
        cite("De opdracht betreft ICT-dienstverlening"), PageText(DOC, PAGE_NO, text)
    )
    assert check == CitationCheck(False, "page_empty")


def test_page_label_is_not_searched():
    # Only `text` is given to the verifier; a sheet name or slide title is not part of it.
    page = PageText(DOC, PAGE_NO, "Totaal 100 punten")
    assert not verify_citation(cite("Beoordelingsmatrix kwaliteit"), page).passed


# --- behaviour -----------------------------------------------------------------


def test_input_verified_flag_is_ignored_and_citation_unchanged():
    citation = cite("Deze zin staat nergens in het document.", verified=True)
    before = citation.model_dump()
    assert verify_citation(citation, PAGE) == CitationCheck(False, "not_found")
    assert citation.model_dump() == before

    citation = cite("De opdracht betreft ICT-dienstverlening", verified=False)
    assert verify_citation(citation, PAGE).passed
    assert citation.verified is False  # the caller sets the flag, not the verifier


def test_result_holds_no_page_text():
    check = verify_citation(cite("Deze zin staat nergens in het document."), PAGE)
    assert CitationCheck.__slots__ == ("passed", "reason")
    assert check.reason in get_args(Reason)


def test_every_reason_code_is_reached():
    reached = {reason for _, reason in FAILS.values()}
    reached |= {"ok", "page_missing", "page_mismatch", "page_empty"}  # tested above
    assert reached == set(get_args(Reason))


# --- split words and real hyphens in one quote ---------------------------------

MIXED_PAGE = PageText(
    DOC,
    PAGE_NO,
    "1.1 Inleiding\n"
    "De aanbeste-\n"
    "dingsleidraad beschrijft de ICT-\n"
    "dienstverlening en het niet-ontvankelijk verklaren van inschrijvingen.\n"
    "De looptijd is 2027-2029 en de norm is ISO 27001-\n"
    "gecertificeerd.",
)
NO_BREAK_PAGE = PageText(
    DOC, PAGE_NO, "De inschrijving wordt niet-ontvankelijk verklaard bij een te late indiening."
)

MIXED_PASSES = {
    "split_word_then_real_hyphen": "De aanbestedingsleidraad beschrijft de ICT-dienstverlening",
    "copied_with_both_line_breaks": (
        "De aanbeste-\ndingsleidraad beschrijft de ICT-\ndienstverlening"
    ),
    "real_hyphen_dropped_by_the_model": "aanbestedingsleidraad beschrijft de ICTdienstverlening",
    "runs_on_past_an_ordinary_hyphen": (
        "aanbestedingsleidraad beschrijft de ICT-dienstverlening en het niet-ontvankelijk"
    ),
    "hyphen_after_a_digit_at_a_line_break": "de norm is ISO 27001-gecertificeerd",
}


@pytest.mark.parametrize("quote", MIXED_PASSES.values(), ids=MIXED_PASSES.keys())
def test_a_quote_over_a_split_word_and_a_real_hyphen_passes(quote):
    assert verify_citation(cite(quote), MIXED_PAGE) == CitationCheck(True, "ok")


def test_each_half_passed_before_and_still_does():
    assert verify_citation(cite("De aanbestedingsleidraad beschrijft"), MIXED_PAGE).passed
    assert verify_citation(cite("beschrijft de ICT-dienstverlening"), MIXED_PAGE).passed


MIXED_FAILS = {
    "another_word": "De aanbestedingsleidraad omschrijft de ICT-dienstverlening",
    "changed_year_range": "aanbestedingsleidraad beschrijft de ICT-dienstverlening 2027-2028",
    "number_range_without_its_hyphen": "De looptijd is 20272029 en de norm",
    "words_joined_across_a_space": "De aanbestedingsleidraadbeschrijft de ICT-dienstverlening",
}


@pytest.mark.parametrize("quote", MIXED_FAILS.values(), ids=MIXED_FAILS.keys())
def test_the_fallback_is_not_fuzzy_matching(quote):
    assert verify_citation(cite(quote), MIXED_PAGE) == CitationCheck(False, "not_found")


def test_the_fallback_needs_a_hyphen_at_a_line_break():
    # No line break is involved, so a dropped hyphen is a real difference and fails.
    assert verify_citation(cite("wordt niet-ontvankelijk verklaard"), NO_BREAK_PAGE).passed
    check = verify_citation(cite("wordt nietontvankelijk verklaard"), NO_BREAK_PAGE)
    assert check == CitationCheck(False, "not_found")
