"""Citation verifier (ARCHITECTURE.md 2.5): is the quote really on the cited page?

Pure code: no model calls, no database, no logging. The quote and the page text get the same
normalisation, then the quote must be one contiguous substring of that single page. There is
no fuzzy matching, so a paraphrase, a changed number or a quote shortened in the middle fails.
The result carries a reason code only, never page text.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from pipeline.schemas import Citation

Reason = Literal[
    "ok",
    "page_missing",
    "page_mismatch",
    "page_empty",
    "quote_too_short",
    "quote_has_ellipsis",
    "not_found",
]

MIN_ALNUM = 10  # letters or digits in the quote after normalisation


@dataclass(frozen=True, slots=True)
class PageText:
    """One stored `document_pages` row."""

    document_id: int
    page_no: int
    text: str


@dataclass(frozen=True, slots=True)
class CitationCheck:
    passed: bool
    reason: Reason


# Applied before NFKC, which would split some of these into other characters.
_CHAR_MAP = str.maketrans(
    {
        **dict.fromkeys("\u2018\u2019\u201a\u201b\u2032`\u00b4", "'"),
        **dict.fromkeys("\u201c\u201d\u201e\u201f\u00ab\u00bb\u2033", '"'),
        **dict.fromkeys("\u2010\u2011\u2012\u2013\u2014\u2015\u2212", "-"),
    }
)
# Soft hyphen, zero-width characters, BOM, and control characters other than whitespace.
_INVISIBLE = re.compile("[\u00ad\u200b-\u200d\u2060\ufeff\x00-\x08\x0e-\x1f\x7f]")
_LINE_BREAK_HYPHEN = re.compile(r"-[ \t]*(?:\r\n|[\n\r\x0b\x0c\u2028\u2029])[ \t]*(\S)")
_WHITESPACE = re.compile(r"\s+")
_ELLIPSIS = re.compile(r"\.{3,}|(?:\. ){2}\.|[\[(]\s*\.{2}\s*[\])]")
_QUOTE_MARKS = "'\""


def _clean(text: str) -> str:
    text = text.translate(_CHAR_MAP)
    text = unicodedata.normalize("NFKC", text)  # ligatures, NBSP, composed accents
    return _INVISIBLE.sub("", text)


def _finish(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip().casefold()


def _variants(text: str) -> tuple[str, ...]:
    """Normalised forms of `text`, one per reading of a hyphen at a line break.

    "aanbeste-\\ndingsleidraad" is a split word; "ICT-\\ndienstverlening" is a real hyphen.
    Code cannot tell them apart, so both readings are produced.
    """
    cleaned = _clean(text)

    def joined(match: re.Match[str]) -> str:
        following = match.group(1)
        return following if following.islower() else "-" + following

    without_hyphen = _finish(_LINE_BREAK_HYPHEN.sub(joined, cleaned))
    with_hyphen = _finish(_LINE_BREAK_HYPHEN.sub(r"-\1", cleaned))
    return tuple(dict.fromkeys((without_hyphen, with_hyphen)))


def _strip_wrapping_quotes(quote: str) -> str:
    if len(quote) >= 2 and quote[0] in _QUOTE_MARKS and quote[-1] in _QUOTE_MARKS:
        return quote[1:-1].strip()
    return quote


def verify_citation(citation: Citation, page: PageText | None) -> CitationCheck:
    """Check `citation.quote` against the one page it cites.

    `page` is the stored page for (`citation.document_id`, `citation.page`), or None when no
    such row exists. `citation.verified` is ignored and the citation is not changed; the
    caller sets the flag from `passed`.
    """
    quotes = tuple(dict.fromkeys(_strip_wrapping_quotes(q) for q in _variants(citation.quote)))
    if all(sum(ch.isalnum() for ch in q) < MIN_ALNUM for q in quotes):
        return CitationCheck(False, "quote_too_short")
    if page is None:
        return CitationCheck(False, "page_missing")
    if page.document_id != citation.document_id or page.page_no != citation.page:
        return CitationCheck(False, "page_mismatch")
    pages = _variants(page.text)
    if not any(pages):
        return CitationCheck(False, "page_empty")
    if any(q in p for q in quotes for p in pages):
        return CitationCheck(True, "ok")
    if any(_ELLIPSIS.search(q) for q in quotes):
        return CitationCheck(False, "quote_has_ellipsis")
    return CitationCheck(False, "not_found")
