"""Redaction before model calls. All names, addresses and numbers here are invented."""

from __future__ import annotations

import pytest

from llm.prompts import PromptRef, UntrustedText
from llm.providers.base import Message, ToolCall, ToolResult, ToolSpec
from llm.redact import Redactor, looks_like_person
from pipeline.schemas import ScreeningOutput
from tests.fakes import CONTEXT, HAIKU, FakeProvider, make_gateway, reply

NAMES = ["Jan de Vries", "Sanne van der Berg", "dhr. P. Jansen"]
PLAIN = Redactor()
WITH_NAMES = Redactor(NAMES)


@pytest.mark.parametrize(
    "address",
    ["inkoop@gemeente-voorbeeld.nl", "J.DeVries@Voorbeeld.NL", "jan+tender@sub.voorbeeld.co.uk"],
)
def test_emails_are_replaced(address):
    assert PLAIN.redact(f"Mail naar {address} voor vragen.") == "Mail naar [EMAIL] voor vragen."


@pytest.mark.parametrize(
    "number",
    [
        "06-12345678",
        "0612345678",
        "06 12 34 56 78",
        "020 123 45 67",
        "(020) 123 4567",
        "088-1234567",
        "+31 (0)20 123 4567",
        "+31 6 12345678",
        "+31612345678",
        "0031 20 1234567",
        "06 1234 5678",
        "0570-12 34 56",
        "0570 123 456",
        "(0570) 123456",
        "+31 (0)6 12345678",
        "+31 20 123 45 67",
    ],
)
def test_phone_numbers_are_replaced(number):
    assert PLAIN.redact(f"Bel {number} tijdens kantooruren.") == "Bel [PHONE] tijdens kantooruren."


@pytest.mark.parametrize(
    "text",
    [
        "Score: 0 25 50 75 100 punten",  # a scoring scale
        "Schaal 0 100 200 300 punten",
        "tabel: 0 1 2 3 4 5 6 7 8 9 10",
        "Punten: 00 10 20 30 40 50",
        "Kolommen 05 10 15 20 25 procent",  # a two-digit area code is only ever 06
        "Weging 010 2 3 4 5 6 7 8 punten",  # single digits after the first group
        "IBAN NL91 ABNA 0417 1643 00",
        "IBAN: NL91ABNA 0417 1643 00.",
    ],
)
def test_spaced_numbers_that_are_not_grouped_like_a_phone_number_are_left_alone(text):
    assert PLAIN.redact(text) == text


def test_a_phone_number_next_to_a_scale_is_still_replaced():
    text = "Score 0 25 50 75 100; bel 06 12 34 56 78 of 020-123 45 67."
    assert PLAIN.redact(text) == "Score 0 25 50 75 100; bel [PHONE] of [PHONE]."


def test_two_phone_numbers_side_by_side():
    assert PLAIN.redact("T 020 1234567 M 06 12345678") == "T [PHONE] M [PHONE]"


@pytest.mark.parametrize(
    "text",
    [
        "De waarde is € 1.000.000 exclusief btw.",
        "Geraamde waarde: EUR 250000.",
        "Geraamde waarde: EUR 0250000000.",
        "Sluiting op 2026-11-20 om 12:00 uur.",
        "Sluiting op 01-12-2026 12:00 uur.",
        "Vragen tot 05-01-2027 17:00 uur.",
        "CPV 72000000-5 en 48000000-8.",
        "TED-publicatie 643806-2026.",
        "IBAN NL91ABNA0417164300.",
        "Minimaal 3 referenties in de afgelopen 3 jaar.",
        "Zie artikel 4.2.1 op pagina 14.",
        "Omzet 2024: 4.500.000; 2025: 5.100.000.",
    ],
)
def test_amounts_dates_and_codes_are_left_alone(text):
    assert PLAIN.redact(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "Contactpersoon: Jan de Vries.",
        "Contactpersoon: JAN DE VRIES.",
        "Contactpersoon: Jan de\nVries.",
        "Contactpersoon: J. de Vries.",
        "Contactpersoon: J.de Vries.",
        "Contactpersoon: mevrouw De Vries.",
        "Contactpersoon: dhr. De Vries.",
        "Contactpersoon: de heer J.A. de Vries.",
        "Contactpersoon: Sanne van der Berg.",
        "Contactpersoon: S. van der Berg.",
        "Contactpersoon: P. Jansen.",
        "Contactpersoon: mr. Jansen.",
    ],
)
def test_contact_names_are_replaced(text):
    assert WITH_NAMES.redact(text) == "Contactpersoon: [NAME]."


@pytest.mark.parametrize(
    "text",
    [
        "De firma Vries levert hardware.",  # bare surname, no title
        "Het project Jansen is afgerond.",
        "Berg en dal zijn onderdeel van het werk.",
        "Bij de vries- en koelinstallaties.",
        "De heer Pietersen is projectleider.",  # not in the contacts data
    ],
)
def test_what_name_redaction_does_not_catch(text):
    assert WITH_NAMES.redact(text) == text


def test_empty_name_list_still_redacts_emails_and_phones():
    text = "Jan de Vries, jan@voorbeeld.nl, 06-12345678"
    assert PLAIN.redact(text) == "Jan de Vries, [EMAIL], [PHONE]"
    assert Redactor(["", "  ", "Bo"]).redact(text) == "Jan de Vries, [EMAIL], [PHONE]"


# --- contact names that are not persons ------------------------------------------

SENTENCE = (
    "De afdeling inkoop van de gemeente Voorbeeldstad beoordeelt de inschrijvingen; "
    "het team aanbestedingen beantwoordt vragen over de inkoop."
)


@pytest.mark.parametrize(
    "name",
    [
        "Inkoop",
        "Afdeling Inkoop",
        "Team Aanbestedingen",
        "Gemeente Voorbeeldstad",
        "Inkoopbureau Midden-Nederland",
        "Servicedesk",
        "Voorbeeld Advies B.V.",
        "Jansen",  # one word, no title: not assumed to be a person
        "Contactpunt 2",
        "t.a.v. inkoop@voorbeeld.example",
        "",
    ],
)
def test_names_that_do_not_look_like_a_person(name):
    assert not looks_like_person(name)
    assert Redactor([name]).redact(SENTENCE) == SENTENCE


@pytest.mark.parametrize(
    "name",
    ["Jan de Vries", "J. de Vries", "dhr. Jansen", "mevrouw S. van der Berg", "Anne-Marie Smit"],
)
def test_names_that_look_like_a_person(name):
    assert looks_like_person(name)


def test_department_and_buyer_names_in_the_contact_list_do_not_remove_tender_text():
    redactor = Redactor(["Inkoop", "Gemeente Voorbeeldstad", "Team Aanbestedingen", "Jan de Vries"])
    assert redactor.redact(SENTENCE) == SENTENCE
    assert redactor.redact("Vragen aan Jan de Vries, afdeling Inkoop.") == (
        "Vragen aan [NAME], afdeling Inkoop."
    )


def test_a_contact_name_equal_to_an_organisation_name_is_skipped():
    text = "Opdrachtgever is Voorbeeld Staete; contactpersoon is Jan de Vries."
    names = ["Voorbeeld Staete", "Jan de Vries"]
    # Shaped like a person's name, so only the organisation list can keep it readable.
    assert Redactor(names).redact(text).startswith("Opdrachtgever is [NAME];")
    redactor = Redactor(names, organisations=["  voorbeeld   STAETE "])
    assert redactor.redact(text) == "Opdrachtgever is Voorbeeld Staete; contactpersoon is [NAME]."


def test_repr_does_not_show_the_names():
    assert "Vries" not in repr(WITH_NAMES) and "Jansen" not in repr(WITH_NAMES)


# --- through the gateway ---------------------------------------------------------

PLANTED = ("jan@voorbeeld.nl", "06-12345678", "Jan de Vries", "J. de Vries")
DIRTY = "Contact: Jan de Vries (J. de Vries), jan@voorbeeld.nl, 06-12345678."


def test_nothing_planted_reaches_the_provider_in_a_call(tmp_path):
    provider = FakeProvider(reply())
    gateway, _ = make_gateway(tmp_path, provider)
    gateway.call(
        task="screening",
        prompt=PromptRef("screen", "v1"),
        variables={"company": f"Datalink B.V. ({DIRTY})"},
        data=[UntrustedText("notice", DIRTY), UntrustedText("lots", DIRTY)],
        schema=ScreeningOutput,
        model=HAIKU,
        redactor=WITH_NAMES,
        context=CONTEXT,
    )
    sent = provider.sent_text()
    assert all(planted not in sent for planted in PLANTED)
    assert sent.count("Contact: [NAME] ([NAME]), [EMAIL], [PHONE].") == 3


def test_tool_results_and_user_turns_are_redacted_and_assistant_turns_untouched(tmp_path):
    call = ToolCall("call_1", "search_tender_docs", {"tender_id": 42, "query": "ISO 27001"})
    assistant = Message(
        "assistant", tool_calls=(call,), raw={"native": DIRTY}, provider="anthropic"
    )
    history = [
        assistant,
        Message("user", tool_results=(ToolResult("call_1", "search_tender_docs", DIRTY),)),
        Message("user", text=f"En wie is {DIRTY}"),
    ]
    provider = FakeProvider(reply("Geen verdere vragen."))
    gateway, _ = make_gateway(tmp_path, provider)
    gateway.step(
        task="chat",
        prompt=PromptRef("match", "v2"),
        data=[UntrustedText("requirement", "ISO 27001 vereist.")],
        history=history,
        tools=[ToolSpec("search_tender_docs", "Search.", {"type": "object"})],
        model=HAIKU,
        redactor=WITH_NAMES,
        context=CONTEXT,
        step_index=1,
    )
    sent = provider.sent_text()
    assert all(planted not in sent for planted in PLANTED)
    # The assistant turn is replayed exactly as the provider returned it.
    assert provider.requests[0].messages[1] is assistant
