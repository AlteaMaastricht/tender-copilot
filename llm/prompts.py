"""Prompt files and the wrapping of untrusted text.

Prompts live in `prompts/{name}_{version}.md` and are never written inline in code. A prompt
file is a system prompt: its `{{placeholders}}` take trusted values only. Tender text, notice
text and tool results are untrusted. They go into the user turn inside tags, never into the
system prompt.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
GATEWAY_PROMPT_VERSION = "v1"
TAG = "untrusted_data"

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_VERSION = re.compile(r"^v[0-9]+$")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)\s*\}\}")
_TAG_OPENER = re.compile(rf"<(?=/?{TAG})", re.IGNORECASE)
_LABEL_UNSAFE = re.compile(r"[^A-Za-z0-9 _.:\-]")


class PromptError(ValueError):
    """A prompt file is missing, badly named, or its variables do not match."""


@dataclass(frozen=True, slots=True)
class PromptRef:
    name: str  # "screen"
    version: str  # "v1"

    @property
    def filename(self) -> str:
        if not _NAME.match(self.name) or not _VERSION.match(self.version):
            raise PromptError(f"invalid prompt reference: {self.name!r} {self.version!r}")
        return f"{self.name}_{self.version}.md"


@dataclass(frozen=True, slots=True)
class UntrustedText:
    """Text from outside: a notice, a tender document, a tool result."""

    label: str
    text: str


def load_prompt(
    ref: PromptRef, variables: Mapping[str, str], prompts_dir: Path = PROMPTS_DIR
) -> str:
    """Read the prompt file and fill its placeholders. Every variable must be used exactly."""
    path = prompts_dir / ref.filename
    if not path.is_file():
        raise PromptError(f"prompt file not found: {ref.filename}")
    template = path.read_text(encoding="utf-8")
    wanted = set(_PLACEHOLDER.findall(template))
    if missing := wanted - set(variables):
        raise PromptError(f"{ref.filename}: missing variables {sorted(missing)}")
    if unused := set(variables) - wanted:
        raise PromptError(f"{ref.filename}: unused variables {sorted(unused)}")
    # One pass: a placeholder inside a variable's value is not expanded.
    return _PLACEHOLDER.sub(lambda m: variables[m.group(1)], template).strip()


def data_tag(run_id: object) -> str:
    """The tag name for one run. The author of a document cannot know it in advance.

    It is the same for every call of a run, so a conversation replayed across agent steps
    stays byte-identical.
    """
    return f"{TAG}_{hashlib.sha256(str(run_id).encode()).hexdigest()[:16]}"


def wrap_untrusted(items: Sequence[UntrustedText], tag: str) -> str:
    """Put each text in its own tagged block. Tag look-alikes inside the text are defused."""
    blocks = []
    for item in items:
        label = _LABEL_UNSAFE.sub("", item.label)[:80]
        body = _TAG_OPENER.sub("&lt;", item.text)
        blocks.append(f'<{tag} label="{label}">\n{body}\n</{tag}>')
    return "\n\n".join(blocks)


def schema_json(schema: type[BaseModel]) -> str:
    return json.dumps(schema.model_json_schema(), ensure_ascii=False, sort_keys=True)
