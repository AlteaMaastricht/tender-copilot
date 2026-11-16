"""Redaction of personal contact data before any text is sent to a model (ARCHITECTURE.md 2.8).

Best effort. It replaces email addresses, phone numbers and the contact names it is given.
It cannot catch: names that are not in the contacts data, a bare surname without a title,
misspelt or OCR-damaged names, obfuscated addresses ("jan [at] gemeente punt nl"), unusual
phone formats, or other personal data such as postal addresses. A ten-digit number that
starts with 0 and is written without spaces, or grouped like a phone number, can be replaced
as a phone number by mistake.

Two rules keep tender content readable (decision R2):
- Digits separated by spaces are a phone number only when grouped like one. A scoring scale
  ("0 25 50 75 100") or the tail of a spaced IBAN is left alone.
- A contact "name" that is the buyer's own name, a department ("Inkoop", "Team
  Aanbestedingen") or a single word without a title is not treated as a person. Replacing it
  would remove ordinary words from every document of the tender.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Protocol, Self

EMAIL_TOKEN = "[EMAIL]"
PHONE_TOKEN = "[PHONE]"
NAME_TOKEN = "[NAME]"

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")

# A digit, with at most two separator characters before it.
_DIGIT = r"(?:[ ()\-]{0,2}\d)"
_PHONE = re.compile(
    rf"(?<![\w+])(?:\+|00){_DIGIT}{{9,13}}(?!\d)"  # +31 (0)20 123 4567, 0031 6 12345678
    rf"|(?<![\w€+\-])\(?0{_DIGIT}{{9}}(?!\d)"  # 06-12345678, (020) 123 45 67: ten digits
)
_DATE_SHAPE = re.compile(r"^\(?0\d\)?[ \-]\d{2}[ \-](?:19|20)\d{2}")  # 01-12-2026 12:00
_MONEY_BEFORE = re.compile(r"(?:€|EUR)\s*$", re.IGNORECASE)
_IBAN_BEFORE = re.compile(r"[A-Z]{2}\d{2} ?[A-Z]{4} ?$")  # NL91 ABNA 0417 1643 00
_DIGIT_GROUP = re.compile(r"\d+")

_TITLES = (
    "de heer", "mevrouw", "dhr", "mevr", "mw", "mr", "drs", "ir", "ing", "dr", "prof",
    "mrs", "ms",
)  # fmt: skip
_TITLE = r"(?:" + "|".join(t.replace(" ", r"\s+") for t in _TITLES) + r")\.?"
_PARTICLES = frozenset(
    {"van", "de", "der", "den", "het", "'t", "te", "ter", "ten", "op", "in", "la", "le", "von"}
)
_LEADING_TITLE = re.compile(rf"^(?:{_TITLE}\s+)+", re.IGNORECASE)

# A contact "name" with one of these in it is a department or an organisation, not a person.
_ORG_STEMS = (
    "aanbested", "afdeling", "bedrijf", "beheer", "bureau", "centrum", "contract",
    "department", "desk", "dienst", "gemeente", "hogeschool", "info", "inkoop", "ministerie",
    "office", "organisatie", "postbus", "procurement", "provincie", "purchasing",
    "secretariaat", "service", "stichting", "team", "tender", "universiteit", "waterschap",
)  # fmt: skip
_LEGAL_FORMS = frozenset({"bv", "nv", "vof", "cv"})
_NOT_IN_A_NAME = re.compile(r"[\d@/&:;()]")


def _key(text: str) -> str:
    return " ".join(text.casefold().split())


def looks_like_person(name: str) -> bool:
    """Is this contact name shaped like a person's name?

    False for a department or organisation ("Afdeling Inkoop", "Gemeente Voorbeeldstad B.V.")
    and for a single word without a title: a bare word is more often a department than a
    surname, and a bare surname is not redacted anyway (see the module docstring).
    """
    stripped = name.strip()
    bare = _LEADING_TITLE.sub("", stripped)
    tokens = bare.split()
    if not tokens or _NOT_IN_A_NAME.search(bare):
        return False
    words = [t.casefold().replace(".", "") for t in tokens]
    if any(w in _LEGAL_FORMS or any(stem in w for stem in _ORG_STEMS) for w in words):
        return False
    if len(tokens) == 1:
        return bare != stripped  # "dhr. Jansen" is a person; "Jansen" alone is not assumed
    return len(tokens) <= 6


def _token_pattern(token: str) -> str:
    bare = token.rstrip(".")
    if len(bare) == 1 or token.endswith("."):
        return re.escape(bare) + r"\.?"  # an initial, with or without its dot
    return re.escape(bare)


def _name_patterns(name: str) -> list[str]:
    tokens = _LEADING_TITLE.sub("", name.strip()).split()
    if not tokens or sum(len(t.rstrip(".")) for t in tokens) < 3:
        return []
    patterns = [r"\s+".join(_token_pattern(t) for t in tokens)]
    if len(tokens) == 1:
        return patterns

    # Surname: from the first particle ("de Vries", "van der Berg"), else the last token.
    start = next(
        (i for i, t in enumerate(tokens[1:], 1) if t.lower() in _PARTICLES), len(tokens) - 1
    )
    surname = r"\s+".join(re.escape(t) for t in tokens[start:])
    initial = re.escape(tokens[0][0])
    patterns.append(rf"{initial}(?:\.\s*|\s+)(?:[A-Za-z]\.\s*){{0,3}}{surname}")
    patterns.append(rf"{_TITLE}\s+(?:[A-Za-z]\.\s*){{0,4}}{surname}")
    return patterns


class ContactRow(Protocol):
    """The two `contacts` columns the Redactor reads (`ingest.ted.TedContact` has them)."""

    @property
    def org_name(self) -> str | None: ...

    @property
    def contact_name(self) -> str | None: ...


class Redactor:
    """Replaces emails, phone numbers and the given contact names.

    The names are used only for matching inside this process. They are never sent anywhere
    and are kept out of `repr`, so a logged Redactor does not leak them.

    `organisations` are names that must stay readable (the buyer, its departments). A contact
    name equal to one of them, or not shaped like a person's name, is skipped.
    """

    def __init__(self, names: Iterable[str] = (), *, organisations: Iterable[str] = ()) -> None:
        kept = {_key(organisation) for organisation in organisations}
        patterns = {
            p
            for name in names
            if _key(name) not in kept and looks_like_person(name)
            for p in _name_patterns(name)
        }
        ordered = sorted(patterns, key=len, reverse=True)
        self._name_count = len(ordered)
        self._names = (
            re.compile(r"(?<!\w)(?:" + "|".join(ordered) + r")(?!\w)", re.IGNORECASE)
            if ordered
            else None
        )

    @classmethod
    def from_contacts(
        cls, contacts: Iterable[ContactRow], *, organisations: Iterable[str] = ()
    ) -> Self:
        """The Redactor for one tender, built from its `contacts` rows.

        Contact names are redacted; the organisation names of the rows, and any names given
        in `organisations` (the buyers), stay readable.
        """
        rows = tuple(contacts)
        return cls(
            (row.contact_name for row in rows if row.contact_name),
            organisations=(*organisations, *(row.org_name for row in rows if row.org_name)),
        )

    def __repr__(self) -> str:
        return f"Redactor(name_patterns={self._name_count})"

    def redact(self, text: str) -> str:
        text = _EMAIL.sub(EMAIL_TOKEN, text)
        if self._names is not None:
            text = self._names.sub(NAME_TOKEN, text)
        return _PHONE.sub(_phone_or_original, text)


def _phone_grouping(found: str) -> bool:
    """Are the digits grouped the way a phone number is written?

    National (starts with 0): one block of ten digits, or an area code of 2 to 4 digits
    ("06", "020", "0570") followed by groups of two or more digits. A two-digit area code is
    always "06". International: "+" or "00" joined to the country code, then groups of which
    at most two are a single digit ("+31 (0)6 12345678").
    """
    groups = _DIGIT_GROUP.findall(found)
    if found.startswith("+") or groups[0].startswith("00"):
        if not found.startswith("+") and len(groups[0]) < 3:  # "00 31 ...": not a prefix
            return False
        return sum(len(group) == 1 for group in groups) <= 2
    if len(groups) == 1:
        return True
    area, rest = groups[0], groups[1:]
    if not 2 <= len(area) <= 4 or (len(area) == 2 and area != "06"):
        return False
    return all(len(group) >= 2 for group in rest)


def _phone_or_original(match: re.Match[str]) -> str:
    found = match.group(0)
    if _DATE_SHAPE.match(found) or not _phone_grouping(found):
        return found
    before = match.string[max(0, match.start() - 12) : match.start()]
    if _MONEY_BEFORE.search(before) or _IBAN_BEFORE.search(before):
        return found
    return PHONE_TOKEN
