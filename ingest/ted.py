"""Parse saved TED eForms contract notices into typed records.

Pure functions on XML bytes: no network calls, standard library only.

Notes:
- `TedNotice.deadline` is the notice's own deadline (BT-131 (confirm), else BT-1311 (confirm)). Per
  ARCHITECTURE.md 2.2 and 6.1 it is eForms ground truth only and never `deadline_at`,
  which comes from TenderNed `sluitingsDatum`.
- Contact points (`cac:Contact`) are never read; the records hold no personal data.
- A change notice is linked to its original by procedure ID (BT-04 (confirm)) plus the changed
  notice identifier (BT-758 (confirm)), never by title or buyer.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from ingest.parsing import (
    PLACEHOLDER_DEADLINE,
    is_placeholder_deadline,
    parse_currency,
    parse_duration_months,
    parse_tender_value,
)

NS = {
    "cbc": "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2",
    "cac": "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2",
    "ext": "urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2",
    "efac": "http://data.europa.eu/p27/eforms-ubl-extension-aggregate-components/1",
    "efbc": "http://data.europa.eu/p27/eforms-ubl-extension-basic-components/1",
    "efext": "http://data.europa.eu/p27/eforms-ubl-extensions/1",
}
CONTRACT_NOTICE_TAG = (
    "{urn:oasis:names:specification:ubl:schema:xsd:ContractNotice-2}ContractNotice"
)
EXT = "ext:UBLExtensions/ext:UBLExtension/ext:ExtensionContent/efext:EformsExtension"

_PUBLICATION_ID_RE = re.compile(r"(\d+)-(\d{4})")
_CHANGED_NOTICE_RE = re.compile(r"(.+)-(\d{2})")

DeadlineKind = Literal["tender", "participation"]
DeadlineStatus = Literal["ok", "missing", "placeholder"]


class TedParseError(ValueError):
    """The input is not a usable eForms contract notice."""


@dataclass(frozen=True, slots=True)
class TedBuyer:
    org_id: str  # e.g. ORG-0001
    name: str | None  # BT-500 (confirm)
    country: str | None  # BT-514 (confirm), ISO alpha-3 as in eForms
    legal_type: str | None  # BT-11 (confirm)


@dataclass(frozen=True, slots=True)
class TedLot:
    lot_id: str  # BT-137 (confirm)
    title: str | None  # BT-21 (confirm) (lot)
    cpv_main: str | None  # BT-262 (confirm) (lot)
    cpv_additional: tuple[str, ...]  # BT-263 (confirm) (lot)
    estimated_value: Decimal | None  # BT-27 (confirm) (lot)
    currency: str | None
    duration_months: float | None  # BT-36 (confirm)
    tender_deadline: datetime | None  # BT-131 (confirm); None if missing or a placeholder
    participation_deadline: datetime | None  # BT-1311 (confirm); None if missing or a placeholder
    place_country: str | None  # BT-5141 (confirm)


@dataclass(frozen=True, slots=True)
class TedNotice:
    publication_id: str  # OPP-010 (confirm), e.g. 642990-2026
    notice_id: str  # BT-701 (confirm)
    notice_version: str | None  # BT-757 (confirm), e.g. "01"
    procedure_id: str  # BT-04 (confirm): the upsert and linking key
    project_id: str | None  # BT-22 (confirm)
    notice_type: str  # BT-02 (confirm), e.g. cn-standard
    form_type: str | None  # BT-03 (confirm), e.g. competition
    notice_subtype: str | None  # OPP-070 (confirm)
    language: str | None  # BT-702 (confirm), e.g. NLD
    issue_date: date | None  # BT-05 (confirm)
    publication_date: date | None  # OPP-012 (confirm)
    title: str | None  # BT-21 (confirm)
    buyers: tuple[TedBuyer, ...]
    buyer_name: str | None  # first buyer
    country: str | None  # first buyer's country, ISO alpha-3
    main_nature: str | None  # BT-23 (confirm)
    procedure_type: str | None  # BT-105 (confirm)
    cpv_main: str | None  # BT-262 (confirm)
    cpv_codes: tuple[str, ...]  # BT-262 (confirm) + BT-263 (confirm), main first, no duplicates
    estimated_value: Decimal | None  # BT-27 (confirm)
    framework_max_value: Decimal | None  # BT-271 (confirm)
    currency: str | None
    deadline: datetime | None  # eForms deadline: ground truth only, never deadline_at
    deadline_kind: DeadlineKind | None
    deadline_status: DeadlineStatus
    deadline_raw: str | None  # original text, kept also for placeholders
    lots: tuple[TedLot, ...]
    changed_notice_id: str | None  # BT-758 (confirm), without the version suffix
    changed_notice_version: str | None
    change_reason: str | None  # BT-140 (confirm)
    warnings: tuple[str, ...]

    @property
    def is_change(self) -> bool:
        return self.changed_notice_id is not None


# --- small readers ------------------------------------------------------------


def _text(node: ET.Element | None) -> str | None:
    if node is None or node.text is None:
        return None
    return node.text.strip() or None


def _find_text(parent: ET.Element | None, path: str) -> str | None:
    return None if parent is None else _text(parent.find(path, NS))


def _pick_language(nodes: list[ET.Element], language: str | None) -> str | None:
    """Text of the node in the notice language, else the first non-empty one."""
    texts = [(node.get("languageID"), _text(node)) for node in nodes]
    texts = [(lang, text) for lang, text in texts if text]
    for lang, text in texts:
        if lang == language:
            return text
    return texts[0][1] if texts else None


def _eforms_date(text: str | None) -> date | None:
    """`2026-09-18+02:00` -> date(2026, 9, 18)."""
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _eforms_datetime(date_text: str | None, time_text: str | None) -> datetime | None:
    """Combine EndDate `2026-11-17+01:00` and EndTime `12:00:00+01:00` into an aware datetime."""
    day = _eforms_date(date_text)
    if day is None:
        return None
    time_part = time_text or f"00:00:00{(date_text or '')[10:]}"
    try:
        return datetime.fromisoformat(f"{day.isoformat()}T{time_part}")
    except ValueError:
        return None


def _amount(node: ET.Element | None) -> tuple[Decimal | None, str | None]:
    """eForms amounts are machine decimals: read exactly, then apply the placeholder rules."""
    text = _text(node)
    if node is None or text is None:
        return None, None
    try:
        value = parse_tender_value(Decimal(text))
    except InvalidOperation:
        value = None
    return value, parse_currency(node.get("currencyID"))


def _cpvs(project: ET.Element | None) -> tuple[str | None, tuple[str, ...]]:
    main = _find_text(project, "cac:MainCommodityClassification/cbc:ItemClassificationCode")
    extra: list[str] = []
    if project is not None:
        path = "cac:AdditionalCommodityClassification/cbc:ItemClassificationCode"
        extra = [t for n in project.findall(path, NS) if (t := _text(n))]
    additional = tuple(code for code in dict.fromkeys(extra) if code != main)
    return main, additional


def _normalise_publication_id(raw: str) -> str:
    """`00642990-2026` -> `642990-2026`, the form TED search and the fixtures use."""
    match = _PUBLICATION_ID_RE.fullmatch(raw)
    if not match:
        return raw
    return f"{int(match.group(1))}-{match.group(2)}"


def _publication_key(publication_id: str) -> tuple[int, int]:
    match = _PUBLICATION_ID_RE.fullmatch(publication_id)
    return (int(match.group(2)), int(match.group(1))) if match else (0, 0)


# --- deadlines ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _DeadlineCandidate:
    kind: DeadlineKind
    value: datetime | None  # None when unparseable or a placeholder
    raw: str
    placeholder: bool


def _deadline(
    period: ET.Element | None, kind: DeadlineKind, published: date | None
) -> _DeadlineCandidate | None:
    if period is None:
        return None
    date_text = _find_text(period, "cbc:EndDate")
    if date_text is None:
        return None
    time_text = _find_text(period, "cbc:EndTime")
    raw = f"{date_text} {time_text}" if time_text else date_text
    value = _eforms_datetime(date_text, time_text)
    placeholder = value is not None and (
        is_placeholder_deadline(value.date(), published)
        if published is not None
        else value.date() == PLACEHOLDER_DEADLINE
    )
    return _DeadlineCandidate(kind, None if placeholder else value, raw, placeholder)


def _notice_deadline(
    candidates: list[_DeadlineCandidate], warnings: list[str]
) -> tuple[datetime | None, DeadlineKind | None, DeadlineStatus, str | None]:
    """Earliest real BT-131 (confirm) over all lots, else the earliest real BT-1311 (confirm)."""
    for kind in ("tender", "participation"):
        real = [c for c in candidates if c.kind == kind and c.value is not None]
        if real:
            best = min(real, key=lambda c: c.value)
            return best.value, best.kind, "ok", best.raw
    placeholders = [c for c in candidates if c.placeholder]
    if placeholders:
        warnings.append("placeholder:deadline")
        first = placeholders[0]
        return None, first.kind, "placeholder", first.raw
    if candidates:
        warnings.append("unparseable:deadline")
        return None, candidates[0].kind, "missing", candidates[0].raw
    warnings.append("missing:deadline")
    return None, None, "missing", None


# --- lots and buyers ----------------------------------------------------------


def _parse_lot(
    lot: ET.Element, language: str | None, published: date | None
) -> tuple[TedLot, list[_DeadlineCandidate]]:
    project = lot.find("cac:ProcurementProject", NS)
    process = lot.find("cac:TenderingProcess", NS)
    cpv_main, cpv_additional = _cpvs(project)
    value, currency = _amount(
        None
        if project is None
        else project.find("cac:RequestedTenderTotal/cbc:EstimatedOverallContractAmount", NS)
    )
    duration = (
        None if project is None else project.find("cac:PlannedPeriod/cbc:DurationMeasure", NS)
    )
    deadlines = [
        d
        for d in (
            _deadline(
                None if process is None else process.find("cac:TenderSubmissionDeadlinePeriod", NS),
                "tender",
                published,
            ),
            _deadline(
                None
                if process is None
                else process.find("cac:ParticipationRequestReceptionPeriod", NS),
                "participation",
                published,
            ),
        )
        if d is not None
    ]
    by_kind = {d.kind: d.value for d in deadlines}
    record = TedLot(
        lot_id=_find_text(lot, "cbc:ID") or "",
        title=_pick_language([] if project is None else project.findall("cbc:Name", NS), language),
        cpv_main=cpv_main,
        cpv_additional=cpv_additional,
        estimated_value=value,
        currency=currency,
        duration_months=(
            parse_duration_months(_text(duration), duration.get("unitCode"))
            if duration is not None
            else None
        ),
        tender_deadline=by_kind.get("tender"),
        participation_deadline=by_kind.get("participation"),
        place_country=_find_text(
            project, "cac:RealizedLocation/cac:Address/cac:Country/cbc:IdentificationCode"
        ),
    )
    return record, deadlines


def _parse_buyers(root: ET.Element, language: str | None) -> tuple[TedBuyer, ...]:
    companies: dict[str, ET.Element] = {}
    for company in root.findall(f"{EXT}/efac:Organizations/efac:Organization/efac:Company", NS):
        org_id = _find_text(company, "cac:PartyIdentification/cbc:ID")
        if org_id:
            companies[org_id] = company
    buyers = []
    for party in root.findall("cac:ContractingParty", NS):
        org_id = _find_text(party, "cac:Party/cac:PartyIdentification/cbc:ID")
        if not org_id:
            continue
        company = companies.get(org_id)
        buyers.append(
            TedBuyer(
                org_id=org_id,
                name=_pick_language(
                    [] if company is None else company.findall("cac:PartyName/cbc:Name", NS),
                    language,
                ),
                country=_find_text(company, "cac:PostalAddress/cac:Country/cbc:IdentificationCode"),
                legal_type=_find_text(party, "cac:ContractingPartyType/cbc:PartyTypeCode"),
            )
        )
    return tuple(buyers)


# --- public API ---------------------------------------------------------------


def parse_notice(xml: bytes) -> TedNotice:
    """Parse one eForms contract notice. Raises TedParseError if it cannot be keyed."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise TedParseError(f"not XML: {exc}") from None
    if root.tag != CONTRACT_NOTICE_TAG:
        raise TedParseError(f"not an eForms contract notice: root {root.tag}")

    raw_publication_id = _find_text(root, f"{EXT}/efac:Publication/efbc:NoticePublicationID")
    notice_id = _find_text(root, "cbc:ID[@schemeName='notice-id']")
    procedure_id = _find_text(root, "cbc:ContractFolderID")
    type_node = root.find("cbc:NoticeTypeCode", NS)
    notice_type = _text(type_node)
    required = {
        "publication_id": raw_publication_id,
        "notice_id": notice_id,
        "procedure_id": procedure_id,
        "notice_type": notice_type,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise TedParseError(f"missing required field(s): {', '.join(missing)}")
    assert raw_publication_id and notice_id and procedure_id and notice_type  # for type checkers

    warnings: list[str] = []
    language = _find_text(root, "cbc:NoticeLanguageCode")
    issue_date = _eforms_date(_find_text(root, "cbc:IssueDate"))
    publication_date = _eforms_date(
        _find_text(root, f"{EXT}/efac:Publication/efbc:PublicationDate")
    )
    project = root.find("cac:ProcurementProject", NS)
    cpv_main, cpv_additional = _cpvs(project)
    total = None if project is None else project.find("cac:RequestedTenderTotal", NS)
    value, currency = _amount(
        None if total is None else total.find("cbc:EstimatedOverallContractAmount", NS)
    )
    framework_max, framework_currency = _amount(
        None if total is None else total.find(f"{EXT}/efbc:FrameworkMaximumAmount", NS)
    )

    published = publication_date or issue_date
    lots: list[TedLot] = []
    candidates: list[_DeadlineCandidate] = []
    for lot_node in root.findall("cac:ProcurementProjectLot", NS):
        lot, lot_deadlines = _parse_lot(lot_node, language, published)
        lots.append(lot)
        candidates.extend(lot_deadlines)
    deadline, deadline_kind, deadline_status, deadline_raw = _notice_deadline(candidates, warnings)

    buyers = _parse_buyers(root, language)
    title = _pick_language([] if project is None else project.findall("cbc:Name", NS), language)
    procedure_type = _find_text(root, "cac:TenderingProcess/cbc:ProcedureCode")

    changed = _find_text(root, f"{EXT}/efac:Changes/efbc:ChangedNoticeIdentifier")
    changed_match = _CHANGED_NOTICE_RE.fullmatch(changed) if changed else None

    for name, present in (
        ("title", title),
        ("buyer", buyers),
        ("procedure_type", procedure_type),
        ("cpv_main", cpv_main),
        ("estimated_value", value),
        ("lots", lots),
    ):
        if not present:
            warnings.append(f"missing:{name}")

    return TedNotice(
        publication_id=_normalise_publication_id(raw_publication_id),
        notice_id=notice_id,
        notice_version=_find_text(root, "cbc:VersionID"),
        procedure_id=procedure_id,
        project_id=_find_text(project, "cbc:ID"),
        notice_type=notice_type,
        form_type=type_node.get("listName") if type_node is not None else None,
        notice_subtype=_find_text(root, f"{EXT}/efac:NoticeSubType/cbc:SubTypeCode"),
        language=language,
        issue_date=issue_date,
        publication_date=publication_date,
        title=title,
        buyers=buyers,
        buyer_name=buyers[0].name if buyers else None,
        country=buyers[0].country if buyers else None,
        main_nature=_find_text(project, "cbc:ProcurementTypeCode"),
        procedure_type=procedure_type,
        cpv_main=cpv_main,
        cpv_codes=tuple(dict.fromkeys(c for c in (cpv_main, *cpv_additional) if c)),
        estimated_value=value,
        framework_max_value=framework_max,
        currency=currency or framework_currency,
        deadline=deadline,
        deadline_kind=deadline_kind,
        deadline_status=deadline_status,
        deadline_raw=deadline_raw,
        lots=tuple(lots),
        changed_notice_id=changed_match.group(1) if changed_match else changed,
        changed_notice_version=changed_match.group(2) if changed_match else None,
        change_reason=_find_text(root, f"{EXT}/efac:Changes/efac:ChangeReason/cbc:ReasonCode"),
        warnings=tuple(warnings),
    )


def parse_notice_file(path: str | Path) -> TedNotice:
    return parse_notice(Path(path).read_bytes())


def group_by_procedure(notices: Iterable[TedNotice]) -> dict[str, list[TedNotice]]:
    """Notices per procedure ID (BT-04 (confirm)), each list in publication order."""
    groups: dict[str, list[TedNotice]] = {}
    for notice in notices:
        groups.setdefault(notice.procedure_id, []).append(notice)
    for group in groups.values():
        group.sort(
            key=lambda n: (
                n.publication_date or n.issue_date or date.min,
                _publication_key(n.publication_id),
            )
        )
    return groups


def find_original(change: TedNotice, notices: Iterable[TedNotice]) -> TedNotice | None:
    """The notice a change notice changes: same procedure ID and notice ID from BT-758 (confirm)."""
    if not change.is_change:
        return None
    candidates = [
        n
        for n in notices
        if n.publication_id != change.publication_id
        and n.procedure_id == change.procedure_id
        and n.notice_id == change.changed_notice_id
    ]
    for candidate in candidates:
        if candidate.notice_version == change.changed_notice_version:
            return candidate
    return candidates[0] if candidates else None
