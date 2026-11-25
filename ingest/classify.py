"""Document type rules (DD5, documents.md D7). Rules only: no model fallback here."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ingest.documents import normalise_name


@dataclass(frozen=True, slots=True)
class Rule:
    order: int
    doc_type: str
    names: tuple[str, ...] = ()  # substrings of the normalised name, or regexes with "re:"
    category: str | None = None  # publicatieCategorie.code
    formats: tuple[str, ...] = ()  # when given, `format_names` only apply to these formats
    format_names: tuple[str, ...] = ()
    prefixes: tuple[str, ...] = ()  # the normalised name must start with one of these


# Tuple order is the match order. `order` is the DD5 rule number; the DD5 amendment
# (25 Nov 2026) checks rules 6 and 7 before rule 3 (qa); amendment 2 puts the UEA and the
# leidraad part of rule 5 before rule 6.
RULES: tuple[Rule, ...] = (
    Rule(1, "notice", category="ANK"),
    Rule(2, "notice", names=(r"re:\befe? ?\d{1,2}\b",), prefixes=("aankondiging",)),
    # DD5 amendment 2 (25 Nov 2026): every leidraad is checked before the contract rule.
    Rule(
        5,
        "guideline",
        names=(
            "inschrijvingsleidraad",
            "selectieleidraad",
            "aanbestedingsleidraad",
            "leidraad",
        ),
    ),
    Rule(
        6,
        "contract",
        names=(
            "overeenkomst",
            "contract",
            "algemene voorwaarden",
            "inkoopvoorwaarden",
            "arvodi",
            "arbit",
            "ariv",
            "uav-gc",
            "verwerkersovereenkomst",
        ),
    ),
    Rule(
        7,
        "pricing",
        names=("prijs", "prijzen", "tarief", "tarieven", "kostenblad", "inschrijfstaat"),
        formats=("xlsx",),
        format_names=("inschrijf",),
    ),
    Rule(
        3,
        "qa",
        names=(
            "nota van inlichtingen",
            "toelichting beantwoording",
            r"re:\bnvi\b",
            "vragen en antwoorden",
            "vraag en antwoord",
        ),
    ),
    Rule(
        4,
        "requirements",
        names=(
            "programma van eisen",
            "bijlage 1 - programma",
            r"re:\bpve\b",
            "eisen en wensen",
            "vraagspecificatie",
            "bestek",
        ),
    ),
    # The UEA (a whole word) is checked before the other guideline names, so that
    # "aanbestedingsdocument" in its title does not win.
    Rule(8, "form", names=(r"re:\buea\b", "uniform europees")),
    Rule(
        5,
        "guideline",
        names=(
            "offerteaanvraag",
            "uitnodiging tot inschrijving",
            "beschrijvend document",
            "aanbestedingsdocument",
            "selectiedocument",
        ),
    ),
    Rule(
        8,
        "form",
        names=(
            "eigen verklaring",
            "formulier",
            "verklaring",
            "gedragsverklaring",
            "machtiging",
        ),
    ),
    Rule(9, "annex", names=("bijlage", "annex", "appendix")),
)


def _name_hit(pattern: str, name: str) -> bool:
    if pattern.startswith("re:"):
        return re.search(pattern[3:], name) is not None
    return pattern in name


def _matches(rule: Rule, name: str, category: str | None, file_format: str) -> bool:
    if rule.category is not None and category == rule.category:
        return True
    if any(name.startswith(p) for p in rule.prefixes):
        return True
    if any(_name_hit(p, name) for p in rule.names):
        return True
    return file_format in rule.formats and any(p in name for p in rule.format_names)


def classify_by_rule(name: str, category: str | None, file_format: str) -> str | None:
    """The first matching rule's `doc_type`, or None when no rule matches.

    The name is compared as `normalise_name` gives it, so a missing or wrong
    extension, letter case and runs of spaces do not change the result.
    """
    text = " ".join(normalise_name(name).replace("_", " ").split())
    for rule in RULES:
        if _matches(rule, text, category, file_format):
            return rule.doc_type
    return None
