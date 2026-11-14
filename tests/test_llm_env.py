"""Key lookup: environment first, then `.env`. Fake values only."""

from __future__ import annotations

import os

import pytest

import llm.env
from llm.env import read_key
from llm.prompts import PromptRef, UntrustedText
from llm.providers.base import ProviderError
from llm.redact import Redactor
from pipeline.schemas import ScreeningOutput
from tests.fakes import CONTEXT, HAIKU, FakeProvider, make_gateway

VAR = "APP_ANTHROPIC_API_KEY"
FAKE = "fake-key-from-dotenv-0123456789"


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text(
        "# provider keys\n"
        f"{VAR}={FAKE}\n"
        "export MISTRAL_API_KEY='quoted-fake-value-123'\n"
        'OPENAI_API_KEY="double-quoted-fake"\n'
        "EMPTY_KEY=\n"
        "not a key line\n"
    )
    monkeypatch.setattr(llm.env, "ENV_FILE", path)
    for name in (VAR, "MISTRAL_API_KEY", "OPENAI_API_KEY", "EMPTY_KEY"):
        monkeypatch.delenv(name, raising=False)
    return path


def test_the_tests_never_see_the_real_env_file():
    assert llm.env.ENV_FILE.name == "absent.env" and not llm.env.ENV_FILE.exists()


def test_falls_back_to_env_file(env_file):
    assert read_key(VAR) == FAKE
    assert read_key("MISTRAL_API_KEY") == "quoted-fake-value-123"
    assert read_key("OPENAI_API_KEY") == "double-quoted-fake"


def test_the_environment_wins(env_file, monkeypatch):
    monkeypatch.setenv(VAR, "from-the-environment")
    assert read_key(VAR) == "from-the-environment"


@pytest.mark.parametrize("name", ["EMPTY_KEY", "NOT_THERE", "not a key line"])
def test_missing_or_empty_gives_none(env_file, name):
    assert read_key(name) is None


def test_missing_file_gives_none(tmp_path, monkeypatch):
    monkeypatch.delenv(VAR, raising=False)
    assert read_key(VAR, tmp_path / "nope.env") is None


def test_only_the_requested_variable_is_read_and_the_environment_is_unchanged(env_file):
    before = dict(os.environ)
    read_key(VAR)
    assert dict(os.environ) == before


def test_a_key_from_the_env_file_is_scrubbed_from_logged_errors(env_file, tmp_path):
    from llm.client import Gateway
    from llm.log import MemoryCallLog
    from tests.fakes import make_prompts_dir

    log = MemoryCallLog()
    error = ProviderError(f"401 invalid key {FAKE}", status_code=401)
    gateway = Gateway(
        {"anthropic": FakeProvider(error)}, log, prompts_dir=make_prompts_dir(tmp_path)
    )
    with pytest.raises(ProviderError):
        gateway.call(
            task="screening",
            prompt=PromptRef("screen", "v1"),
            variables={"company": "Datalink B.V."},
            data=[UntrustedText("notice", "Dataplatform")],
            schema=ScreeningOutput,
            model=HAIKU,
            redactor=Redactor(),
            context=CONTEXT,
        )
    assert log.records[0].error == "ProviderError 401: 401 invalid key [KEY]"
    assert make_gateway  # fixture helpers stay importable
