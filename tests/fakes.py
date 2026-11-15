"""Test doubles for the LLM gateway. No network, no keys, no provider SDK."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import UUID

from llm.client import CallContext, Gateway
from llm.log import MemoryCallLog
from llm.prompts import PROMPTS_DIR
from llm.providers.base import (
    ProviderError,
    ProviderRequest,
    ProviderResponse,
    ToolCall,
    Usage,
)

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
RUN_ID = UUID("00000000-0000-4000-8000-000000000001")
CONTEXT = CallContext(run_id=RUN_ID, tender_id=42)

SCREENING = {
    "decision": "shortlist",
    "reason": "Data platform for a municipality.",
    "matched_services": ["data_platform"],
    "exclusion_flags": ["none"],
    "confidence": 0.8,
}


class FakeProvider:
    """Plays back a script of responses and errors, and records every request."""

    name = "anthropic"

    def __init__(self, *script: ProviderResponse | Exception) -> None:
        self.script = list(script)
        self.requests: list[ProviderRequest] = []

    def send(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def sent_text(self) -> str:
        """Everything that left the gateway, as one string."""
        parts = []
        for request in self.requests:
            parts.append(request.system)
            for message in request.messages:
                parts.append(message.text)
                parts.extend(result.content for result in message.tool_results)
        return "\n".join(parts)


def reply(
    content: dict | str = SCREENING,
    *,
    stop: str = "end",
    tool_calls: tuple[ToolCall, ...] = (),
    usage: Usage | None = None,
    model: str = HAIKU,
) -> ProviderResponse:
    text = content if isinstance(content, str) else json.dumps(content)
    return ProviderResponse(
        text=text,
        stop_reason="tool_use" if tool_calls else stop,
        usage=usage or Usage(input_tokens=1000, output_tokens=200),
        model=model,
        tool_calls=tool_calls,
        request_id="req_test",
        raw={"native": text},
    )


def http_error(status: int | None, *, retry_after: float | None = None) -> ProviderError:
    return ProviderError(f"status {status}", status_code=status, retry_after=retry_after)


def make_prompts_dir(tmp_path: Path) -> Path:
    """A prompts folder with the real gateway blocks and three small task prompts."""
    folder = tmp_path / "prompts"
    folder.mkdir()
    for source in PROMPTS_DIR.glob("gateway_*.md"):
        shutil.copy(source, folder / source.name)
    (folder / "screen_v1.md").write_text("You screen tenders for {{company}}.\n")
    (folder / "match_v1.md").write_text("You match one requirement of tender {{tender_id}}.\n")
    (folder / "match_v2.md").write_text("You match one requirement against company evidence.\n")
    return folder


def make_gateway(tmp_path: Path, provider: FakeProvider, **kwargs) -> tuple[Gateway, MemoryCallLog]:
    log = MemoryCallLog()
    sleeps: list[float] = []
    gateway = Gateway(
        {"anthropic": provider},
        log,
        prompts_dir=make_prompts_dir(tmp_path),
        secrets=kwargs.pop("secrets", ()),
        sleep=kwargs.pop("sleep", sleeps.append),
        jitter=kwargs.pop("jitter", lambda: 1.0),
        **kwargs,
    )
    gateway.sleeps = sleeps  # type: ignore[attr-defined]
    return gateway, log
