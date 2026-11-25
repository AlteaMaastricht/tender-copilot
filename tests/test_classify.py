import pytest

from ingest.classify import RULES, classify_by_rule


@pytest.mark.parametrize(
    ("name", "category", "fmt", "expected"),
    [
        ("Document.pdf", "ANK", "pdf", "notice"),  # rule 1
        ("Aankondiging opdracht.pdf", None, "pdf", "notice"),  # rule 2
        ("Publicatie EF14.pdf", None, "pdf", "notice"),  # rule 2 regex
        ("Nota van Inlichtingen 1.pdf", None, "pdf", "qa"),  # rule 3
        ("Vragen en antwoorden ronde 2.pdf", None, "pdf", "qa"),
        ("NvI 2.pdf", None, "pdf", "qa"),
        ("Programma van Eisen.pdf", None, "pdf", "requirements"),  # rule 4
        ("PvE definitief.docx", None, "docx", "requirements"),
        ("Inschrijvingsleidraad.pdf", None, "pdf", "guideline"),  # rule 5
        ("Beschrijvend document.pdf", None, "pdf", "guideline"),
        ("Conceptovereenkomst.pdf", None, "pdf", "contract"),  # rule 6
        ("ARVODI-2018.pdf", None, "pdf", "contract"),
        ("Tarieven.pdf", None, "pdf", "pricing"),  # rule 7
        ("Kostenblad.pdf", None, "pdf", "pricing"),
        ("Inschrijfformulier.xlsx", None, "xlsx", "pricing"),  # rule 7 via format
        ("Eigen verklaring.pdf", None, "pdf", "form"),  # rule 8
        ("Gedragsverklaring Aanbesteden.pdf", None, "pdf", "form"),
        ("Bijlage 4 Overzicht.pdf", None, "pdf", "annex"),  # rule 9
        ("Appendix A.pdf", None, "pdf", "annex"),
    ],
)
def test_each_rule_has_a_positive_name(name, category, fmt, expected):
    assert classify_by_rule(name, category, fmt) == expected


def test_all_nine_rules_are_covered():
    # Rules 5 and 8 each appear twice since DD5 amendment 2 (leidraad and UEA moved earlier).
    assert {r.order for r in RULES} == set(range(1, 10))


@pytest.mark.parametrize(
    ("name", "fmt", "expected"),
    [
        ("Nota van inlichtingen bij het Programma van Eisen", "pdf", "qa"),
        ("Bijlage 1 - Programma van eisen", "pdf", "requirements"),
        ("Bijlage 3 Conceptovereenkomst", "pdf", "contract"),
        ("Bijlage 5 UEA", "pdf", "form"),
        ("Bijlage 7 Prijzenblad.xlsx", "xlsx", "pricing"),
    ],
)
def test_order_cases(name, fmt, expected):
    assert classify_by_rule(name, None, fmt) == expected


@pytest.mark.parametrize(
    "name",
    [
        "NOTA VAN INLICHTINGEN 1.PDF",
        "nota van inlichtingen 1",
        "Nota   van\tInlichtingen  1.pdf",
        "  Nota van inlichtingen 1  ",
        "Nota_van__Inlichtingen_1",
        "Nota_ van inlichtingen 1",
    ],
)
def test_case_spacing_and_extension_variants(name):
    assert classify_by_rule(name, None, "pdf") == "qa"


def test_name_without_extension_matches():
    assert classify_by_rule("Bijlage 7 Prijzenblad", None, "xlsx") == "pricing"
    assert classify_by_rule("Selectieleidraad", None, "pdf") == "guideline"


def test_category_wins_over_name():
    assert classify_by_rule("Leidraad.pdf", "ANK", "pdf") == "notice"


def test_notice_name_rule_without_category():
    assert classify_by_rule("Aankondiging van de opdracht", None, "pdf") == "notice"
    assert classify_by_rule("Rooster EF 24.pdf", None, "pdf") == "notice"
    assert classify_by_rule("Beef 2024.pdf", None, "pdf") is None


def test_xlsx_format_needed_for_inschrijf_rule():
    assert classify_by_rule("Inschrijf sheet", None, "xlsx") == "pricing"
    assert classify_by_rule("Inschrijf sheet", None, "pdf") is None


@pytest.mark.parametrize("name", ["Zorgplan.pdf", "Overzicht", "Rapport 2024.docx", ""])
def test_no_match_gives_none(name):
    assert classify_by_rule(name, None, "pdf") is None


@pytest.mark.parametrize(
    ("name", "fmt", "expected"),
    [
        ("TN588373 - EFE3 Aankondiging van een opdracht", "pdf", "notice"),
        ("EFE2 Vooraankondiging", "pdf", "notice"),
        ("Publicatie EF16.pdf", "pdf", "notice"),
        ("Bijlage F Concept Overeenkomst GIBIT 2025 nav NvI 1", "pdf", "contract"),
        ("Bijlage C - Prijsformulier NVI 13-5-2026", "pdf", "pricing"),
        ("Nota van inlichtingen 2026-07-20-1", "pdf", "qa"),
        ("Aanbestedingsdocument", "pdf", "guideline"),
        ("Selectiedocument.pdf", "pdf", "guideline"),
        ("Vraagspecificatie", "pdf", "requirements"),
        ("20260316_NvI_DIL_Communicatiemanager", "pdf", "qa"),
        ("Bijlage_7_Prijzenblad.xlsx", "xlsx", "pricing"),
    ],
)
def test_dd5_amendment(name, fmt, expected):
    assert classify_by_rule(name, None, fmt) == expected


@pytest.mark.parametrize(
    "name",
    [
        "Bijlage 3 - Uniform Europees Aanbestedingsdocument UEA.pdf",
        "uea_596894_20260618143529.pdf",
    ],
)
def test_uea_is_a_form_before_the_guideline_rule(name):
    assert classify_by_rule(name, None, "pdf") == "form"


def test_uea_must_be_a_whole_word():
    assert classify_by_rule("Dueau rapport.pdf", None, "pdf") is None


def test_leidraad_is_checked_before_the_contract_rule():
    assert classify_by_rule("Aanbestedingsleidraad raamovereenkomst", None, "pdf") == "guideline"
    assert classify_by_rule("Concept raamovereenkomst", None, "pdf") == "contract"


def test_uea_does_not_override_qa_or_contract():
    assert classify_by_rule("Nota van inlichtingen UEA", None, "pdf") == "qa"
    assert classify_by_rule("Overeenkomst UEA", None, "pdf") == "contract"
