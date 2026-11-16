"""Citation verifier (ARCHITECTURE.md 2.5): is the quote really on the cited page?

Pure code: no model calls, no database, no logging. The quote and the page text get the same
normalisation, then the quote must be one contiguous substring of that single page. There is
no fuzzy matching, so a paraphrase, a changed number or a quote shortened in the middle fails.
The one loosening: when the page or the quote has a hyphen at a line break and the exact
readings fail, hyphens between two letters are ignored on both sides (decision R7).
The result carries a reason code only, never page text.

`Citation.verified` is owned by code. The gateway clears it on every model output
(`llm/verified.py`), and `verify_citations` here sets it from the check.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from llm.redact import Redactor
from llm.verified import map_models, set_verified
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


# The stored page for (document_id, page_no), or None when there is no such row.
PageLookup = Callable[[int, int], PageText | None]


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
_LETTER_HYPHEN = re.compile(r"(?<=[^\W\d_])-(?=[^\W\d_])")  # a hyphen between two letters
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
    Code cannot tell them apart, so both readings are produced. A passage that holds one of
    each fits neither reading; `verify_citation` has a fallback for that.
    """
    cleaned = _clean(text)

    def joined(match: re.Match[str]) -> str:
        following = match.group(1)
        return following if following.islower() else "-" + following

    without_hyphen = _finish(_LINE_BREAK_HYPHEN.sub(joined, cleaned))
    with_hyphen = _finish(_LINE_BREAK_HYPHEN.sub(r"-\1", cleaned))
    return tuple(dict.fromkeys((without_hyphen, with_hyphen)))


def _has_line_break_hyphen(text: str) -> bool:
    return _LINE_BREAK_HYPHEN.search(_clean(text)) is not None


def _without_letter_hyphens(text: str) -> str:
    """Normalised `text` with every hyphen between two letters removed.

    For the fallback comparison: with the hyphens gone from both sides, it no longer matters
    which line-break hyphens split a word and which belong to it. Hyphens next to a digit
    stay, so a number range or a standard number is still compared exactly.
    """
    joined = _LINE_BREAK_HYPHEN.sub(r"-\1", _clean(text))
    return _finish(_LETTER_HYPHEN.sub("", joined))


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
    # Fallback: one passage can hold a split word and a real hyphen, both at a line break,
    # and neither reading above fits both. Used only when a line-break hyphen is in play.
    if _has_line_break_hyphen(page.text) or _has_line_break_hyphen(citation.quote):
        loose = _strip_wrapping_quotes(_without_letter_hyphens(citation.quote))
        if loose in _without_letter_hyphens(page.text):
            return CitationCheck(True, "ok")
    if any(_ELLIPSIS.search(q) for q in quotes):
        return CitationCheck(False, "quote_has_ellipsis")
    return CitationCheck(False, "not_found")


def verify_redacted_citation(
    citation: Citation, page: PageText | None, redactor: Redactor
) -> CitationCheck:
    """Check a quote the model copied from redacted text.

    The model saw the page after redaction, so its quote can hold `[EMAIL]`, `[PHONE]` or
    `[NAME]`. The stored page is redacted with the same `Redactor` first, then checked.
    """
    if page is not None:
        page = replace(page, text=redactor.redact(page.text))
    return verify_citation(citation, page)


# --- the `verified` flag -------------------------------------------------------


def verify_citations[T](output: T, pages: PageLookup, redactor: Redactor) -> T:
    """A copy of `output` with every `Citation.verified` set by the check: True or False.

    Whatever the flag held before is ignored. `redactor` must be the one used for the model
    calls that produced the quotes.
    """

    def check(citation: Citation) -> Citation:
        page = pages(citation.document_id, citation.page)
        return set_verified(citation, verify_redacted_citation(citation, page, redactor).passed)

    return map_models(output, lambda model: check(model) if isinstance(model, Citation) else model)
