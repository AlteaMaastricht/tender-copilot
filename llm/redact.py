"""Redaction of personal contact data before any text is sent to a model (ARCHITECTURE.md 2.8).

Best effort. It replaces email addresses, phone numbers and the contact names it is given.
It cannot catch: names that are not in the contacts data, a bare surname without a title,
misspelt or OCR-damaged names, obfuscated addresses ("jan [at] gemeente punt nl"), unusual
phone formats, or other personal data such as postal addresses. A ten-digit number that
starts with 0 can be replaced as a phone number by mistake.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

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

_TITLES = (
    "de heer", "mevrouw", "dhr", "mevr", "mw", "mr", "drs", "ir", "ing", "dr", "prof",
    "mrs", "ms",
)  # fmt: skip
_TITLE = r"(?:" + "|".join(t.replace(" ", r"\s+") for t in _TITLES) + r")\.?"
_PARTICLES = frozenset(
    {"van", "de", "der", "den", "het", "'t", "te", "ter", "ten", "op", "in", "la", "le", "von"}
)
_LEADING_TITLE = re.compile(rf"^(?:{_TITLE}\s+)+", re.IGNORECASE)


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


class Redactor:
    """Replaces emails, phone numbers and the given contact names.

    The names are used only for matching inside this process. They are never sent anywhere
    and are kept out of `repr`, so a logged Redactor does not leak them.
    """

    def __init__(self, names: Iterable[str] = ()) -> None:
        patterns = {p for name in names for p in _name_patterns(name)}
        ordered = sorted(patterns, key=len, reverse=True)
        self._name_count = len(ordered)
        self._names = (
            re.compile(r"(?<!\w)(?:" + "|".join(ordered) + r")(?!\w)", re.IGNORECASE)
            if ordered
            else None
        )

    def __repr__(self) -> str:
        return f"Redactor(name_patterns={self._name_count})"

    def redact(self, text: str) -> str:
        text = _EMAIL.sub(EMAIL_TOKEN, text)
        if self._names is not None:
            text = self._names.sub(NAME_TOKEN, text)
        return _PHONE.sub(_phone_or_original, text)


def _phone_or_original(match: re.Match[str]) -> str:
    found = match.group(0)
    if _DATE_SHAPE.match(found):
        return found
    if _MONEY_BEFORE.search(match.string[max(0, match.start() - 6) : match.start()]):
        return found
    return PHONE_TOKEN
