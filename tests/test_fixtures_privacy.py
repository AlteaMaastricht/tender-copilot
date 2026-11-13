"""Privacy checks for the saved TED fixtures (CLAUDE.md: contact data never leaves `contacts`).

Failure messages name the file and the rule only, never the matched text.
"""

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "ted"
FORBIDDEN_TEXT = ("ElectronicMail", "Telephone", "Telefax", "cac:Contact")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
MIN_XML_FIXTURES = 10


def _load_fetch_script():
    spec = importlib.util.spec_from_file_location(
        "fetch_ted_fixtures", ROOT / "scripts" / "fetch_ted_fixtures.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses looks the module up while it is being executed
    spec.loader.exec_module(module)
    return module


fetch_script = _load_fetch_script()
FIXTURE_FILES = sorted(p for p in FIXTURES.iterdir() if p.is_file()) if FIXTURES.is_dir() else []


def test_fixture_folder_is_not_empty() -> None:
    xml_files = [p for p in FIXTURE_FILES if p.suffix == ".xml"]
    assert len(xml_files) >= MIN_XML_FIXTURES, "privacy checks would pass without checking anything"


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize("marker", FORBIDDEN_TEXT)
def test_fixture_has_no_contact_markup(path: Path, marker: str) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    assert marker not in text, f"{path.name} contains {marker}"


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.name)
def test_fixture_has_no_email_shaped_string(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    assert not EMAIL_RE.search(text), f"{path.name} contains an email-shaped string"


def test_redact_contacts_is_non_greedy() -> None:
    text = "<a><cac:Contact>one</cac:Contact>KEEP<cac:Contact>two</cac:Contact></a>"
    assert fetch_script.redact_contacts(text) == "<a>KEEP</a>"


def test_redact_contacts_matches_across_newlines() -> None:
    text = "<a>\n  <cac:Contact>\n    <cbc:Name>x</cbc:Name>\n  </cac:Contact>\n</a>"
    assert fetch_script.redact_contacts(text) == "<a>\n  \n</a>"
