"""TenderNed publication details, as far as the manual snapshot needs them (D3, minimal).

Contact data (names, emails, phones of persons) is removed from the detail before anything is
stored; only the names go on, to the Redactor (`contacts.json`). Field names that were not seen
in a saved response are marked "(confirm)".
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from agent.tools import NAME_WORDS, is_contact_key, is_person_key
from ingest.http import HttpClient

BASE_URL = "https://www.tenderned.nl/papi/tenderned-rs-tns/v2"

# Field names of the publication detail, confirmed on saved responses (tests/fixtures/tenderned/,
# 24 Nov 2026): 409819, 408259 (both with a rectification) and 406922.
ID_KEY = "publicatieId"
TED_NUMBER_KEY = "pbNummerTed"  # e.g. "60877-2026": serial and year, no zero padding
RECTIFICATION_KEY = "publicatieIDLaatsteRectificatie"  # absent when there is no rectification
CLOSING_KEY = "sluitingsDatum"  # e.g. "2026-03-20T10:00:00", no offset (Amsterdam time, R3)

# Words that make a removed key a person's name rather than an email or a phone number.
_NAME_PARTS = ("naam", "name", "voornaam", "achternaam", "initialen", "initials", "surname")
_NOT_A_NAME = ("mail", "phone", "telefoon", "tel", "fax", "gsm", "mobiel", "mobile", "sms")


def split_contacts(detail: Mapping[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Remove every key the contact guard flags, at any depth, with its whole value.

    Returns (clean copy, removed entries). A removed entry is {"path", "key", "value"}; the
    value is personal data and must stay inside this process. The organisation name stays.
    """
    removed: list[dict[str, Any]] = []

    def walk(value: Any, path: str, under_person: bool) -> Any:
        if isinstance(value, dict):
            clean: dict[str, Any] = {}
            for key, item in value.items():
                here = f"{path}.{key}" if path else str(key)
                if is_contact_key(key, under_person):
                    removed.append({"path": here, "key": str(key), "value": item})
                else:
                    clean[key] = walk(item, here, under_person or is_person_key(key))
            return clean
        if isinstance(value, list | tuple):
            return [walk(item, f"{path}[{i}]", under_person) for i, item in enumerate(value)]
        return value

    return walk(dict(detail), "", False), removed


def _words(key: str) -> list[str]:
    text = re.sub(r"([a-z])([A-Z])", r"\1_\2", key).lower().replace("e-mail", "email")
    return [word for word in re.split(r"[^a-z0-9]+", text) if word]


def _not_a_name(key: str) -> bool:
    joined = "".join(_words(key))
    return any(part in joined for part in _NOT_A_NAME)


def _name_like(key: str) -> bool:
    if _not_a_name(key):
        return False
    joined = "".join(_words(key))
    return any(part in joined for part in _NAME_PARTS) or bool(NAME_WORDS & set(_words(key)))


def _plain_text_name(text: str) -> bool:
    return "@" not in text and sum(ch.isdigit() for ch in text) < 5 and bool(text.strip())


def contact_names(removed: Sequence[Mapping[str, Any]]) -> list[str]:
    """Person names from removed entries, for the Redactor. Never emails or phone numbers."""
    names: list[str] = []

    def collect(value: Any, key: str, whole_key: bool) -> None:
        if isinstance(value, dict):
            for sub_key, item in value.items():
                collect(item, str(sub_key), False)
        elif isinstance(value, list | tuple):
            for item in value:
                collect(item, key, whole_key)
        elif isinstance(value, str) and (_name_like(key) or whole_key):
            text = value.strip()
            if _plain_text_name(text) and text not in names:
                names.append(text)

    for entry in removed:
        key = str(entry["key"])
        collect(entry["value"], key, whole_key=not _not_a_name(key))
    return names


def effective_sluitingsdatum(
    detail: Mapping[str, Any] | None, rectification: Mapping[str, Any] | None
) -> str | None:
    """The closing date as written: the rectification's when it has one, else the original's."""
    for source in (rectification, detail):
        value = source.get(CLOSING_KEY) if source else None
        if isinstance(value, str) and value.strip():
            return value
    return None


def rectification_id(detail: Mapping[str, Any]) -> str | None:
    """The ID of the publication that rectifies this one, or None (no field, or its own ID)."""
    value = detail.get(RECTIFICATION_KEY)
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    text = str(value).strip()
    own = str(detail.get(ID_KEY, "")).strip()
    return text if re.fullmatch(r"\d{1,9}", text) and text != own else None


class TenderNedClient:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def detail(self, publication_id: int | str) -> dict[str, Any]:
        """GET /publicaties/{id}. The raw detail: run `split_contacts` before storing it."""
        text = str(publication_id)
        if not re.fullmatch(r"\d{1,9}", text):
            raise ValueError("not a numeric publication ID")
        payload = self._http.get_json(f"{BASE_URL}/publicaties/{text}")
        if not isinstance(payload, dict):
            raise ValueError(f"publication {text}: the detail is not a JSON object")
        return payload
