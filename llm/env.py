"""Key lookup for the provider adapters.

The process environment wins (docker compose fills it from `.env`). When a variable is not set
there, it is read from the repository's `.env`. Only the requested variable is returned:
nothing is copied into `os.environ`, printed or logged.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def read_key(name: str, env_file: Path | None = None) -> str | None:
    """The value of `name`, from the environment or else from `.env`. None when absent."""
    value = os.environ.get(name)
    if value:
        return value
    path = env_file if env_file is not None else ENV_FILE
    if not path.is_file():
        return None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, found = line.removeprefix("export ").partition("=")
        if key.strip() != name:
            continue
        found = found.strip()
        if len(found) >= 2 and found[0] == found[-1] and found[0] in "\"'":
            found = found[1:-1]
        return found or None
    return None
