"""One live call through the LLM gateway, to confirm the Anthropic adapter.

    .venv/bin/python scripts/smoke_llm.py

Exactly one gateway call to claude-haiku-5-5 (the gateway's own validation and network
retries included). The gateway loads APP_ANTHROPIC_API_KEY itself; this script never reads,
prints or logs it. Payload storage is off and the call record is kept in memory only.
The tender text is invented; its email address and phone number are fake.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from llm.client import CallContext, Gateway, GatewayError  # noqa: E402
from llm.log import MemoryCallLog  # noqa: E402
from llm.prompts import PromptRef, UntrustedText  # noqa: E402
from llm.providers.anthropic import AnthropicProvider  # noqa: E402
from llm.providers.base import (  # noqa: E402
    MissingKeyError,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
)
from llm.redact import EMAIL_TOKEN, PHONE_TOKEN, Redactor  # noqa: E402

MODEL = "claude-haiku-5-5"
FAKE_EMAIL = "inkoop@voorbeeldstad.example"
FAKE_PHONE = "06-12345678"
TENDER_TEXT = (
    "De gemeente Voorbeeldstad zoekt een leverancier voor een dataplatform met dashboards "
    "voor de afdeling Sociaal Domein. Vragen kunnen tot 1 december worden gesteld via "
    f"{FAKE_EMAIL} of telefonisch op {FAKE_PHONE}."
)


class SmokeOutput(BaseModel):
    summary: str = Field(max_length=300)
    language: Literal["nl", "en"]


class RecordingProvider:
    """Wraps the real adapter and keeps each request exactly as the gateway hands it over."""

    def __init__(self, inner: AnthropicProvider) -> None:
        self.inner = inner
        self.name = inner.name
        self.requests: list[ProviderRequest] = []
        self.responses: list[ProviderResponse] = []

    def send(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        response = self.inner.send(request)
        self.responses.append(response)
        return response


def sent_text(request: ProviderRequest) -> str:
    parts = [request.system]
    for message in request.messages:
        parts.append(message.text)
        parts.extend(result.content for result in message.tool_results)
    return "\n".join(parts)


def print_redaction(recorder: RecordingProvider) -> None:
    print("\nRedaction check (what left the gateway):")
    print(f"  requests sent: {len(recorder.requests)}")
    for index, request in enumerate(recorder.requests, 1):
        text = sent_text(request)
        print(
            f"  request {index}: fake email present: {FAKE_EMAIL in text}, "
            f"fake phone present: {FAKE_PHONE in text}, "
            f"{EMAIL_TOKEN} present: {EMAIL_TOKEN in text}, "
            f"{PHONE_TOKEN} present: {PHONE_TOKEN in text}"
        )


def print_record(log: MemoryCallLog) -> None:
    if not log.records:
        print("\nNo llm_calls record was written.")
        return
    record = log.records[-1]
    fields = [
        "model", "prompt_name", "prompt_version", "input_tokens", "output_tokens",
        "cached_tokens", "cost_usd", "latency_ms", "status", "stop_reason",
        "validation_retries", "network_retries", "backoff_ms", "error", "payload_path",
    ]  # fmt: skip
    print("\nllm_calls record:")
    for name in fields:
        print(f"  {name}: {getattr(record, name)}")


def main() -> int:
    os.environ.pop("LLM_PAYLOAD_DIR", None)  # payload storage stays off
    recorder = RecordingProvider(AnthropicProvider())
    log = MemoryCallLog()
    gateway = Gateway({"anthropic": recorder}, log, payload_store=None)

    try:
        result = gateway.call(
            task="screening",  # llm_calls.stage has a fixed list; there is no smoke stage
            prompt=PromptRef("smoke", "v1"),
            data=[UntrustedText("tender", TENDER_TEXT)],
            schema=SmokeOutput,
            model=MODEL,
            redactor=Redactor(),
            context=CallContext.new_run(),
            effort="low",
            max_tokens=1024,
        )
    except (GatewayError, ProviderError, MissingKeyError) as exc:
        status = getattr(exc, "status_code", None)
        print(f"Call failed: {type(exc).__name__}, HTTP status: {status}")
        print_redaction(recorder)
        print_record(log)  # the error text in the record has been scrubbed of keys
        return 1

    print("Validated object:")
    print(json.dumps(result.model_dump(), ensure_ascii=False, indent=2))
    print_redaction(recorder)
    print_record(log)
    if recorder.responses:
        last = recorder.responses[-1]
        print(f"\nAPI reported model: {last.model}, request id: {last.request_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
