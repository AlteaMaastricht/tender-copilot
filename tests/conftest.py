"""Shared test setup."""

from __future__ import annotations

import pytest

import llm.env


@pytest.fixture(autouse=True)
def no_real_env_file(tmp_path_factory, monkeypatch):
    """No test may read the real `.env`: a key found there could reach a live API."""
    monkeypatch.setattr(llm.env, "ENV_FILE", tmp_path_factory.mktemp("env") / "absent.env")
