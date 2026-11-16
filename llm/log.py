"""Call log for the gateway, in the shape of the `llm_calls` table (ARCHITECTURE.md section 3).

The database arrives later. Until then `JsonlCallLog` is the sink; the Postgres sink will
implement the same `CallLog.write`. A record never holds a key, a header or prompt text.
Payloads (the redacted request and response) are stored only when a `PayloadStore` is
configured, and are purged after `PAYLOAD_RETENTION_DAYS` (DECISIONS.md Q14).
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

PAYLOAD_RETENTION_DAYS = 30
DEFAULT_LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "llm_calls.jsonl"

Status = Literal["ok", "validation_failed", "refused", "truncated", "provider_error"]
_DATED_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True, slots=True)
class LlmCallRecord:
    run_id: UUID
    stage: str
    provider: str
    model: str
    prompt_name: str
    prompt_version: str
    status: Status
    eval_run_id: int | None = None
    tender_id: int | None = None
    step_index: int = 0
    input_tokens: int = 0  # all prompt tokens, including cached ones
    output_tokens: int = 0
    cached_tokens: int = 0  # the part of input_tokens read from a cache
    cost_usd: Decimal = Decimal(0)
    latency_ms: int = 0  # time spent in the provider calls, over every network attempt
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # names and arguments
    stop_reason: str | None = None
    validation_retries: int = 0
    network_retries: int = 0
    backoff_ms: int = 0  # time waited before network retries; not included in latency_ms
    error: str | None = None
    payload_path: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_json(self) -> dict[str, Any]:
        row = asdict(self)
        row["run_id"] = str(self.run_id)
        row["cost_usd"] = str(self.cost_usd)
        row["created_at"] = self.created_at.isoformat()
        return row


class CallLog(Protocol):
    def write(self, record: LlmCallRecord) -> None: ...


class MemoryCallLog:
    """Keeps records in a list. For tests."""

    def __init__(self) -> None:
        self.records: list[LlmCallRecord] = []

    def write(self, record: LlmCallRecord) -> None:
        self.records.append(record)


class JsonlCallLog:
    """Temporary sink: one JSON object per line. Replaced by the Postgres sink on Day 7."""

    def __init__(self, path: Path = DEFAULT_LOG_PATH) -> None:
        self.path = path

    def write(self, record: LlmCallRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_json(), ensure_ascii=False) + "\n")


class PayloadStore:
    """Writes redacted payloads under `root/YYYY-MM-DD/`. The caller redacts before writing."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def write(self, payload: dict[str, Any], *, today: date | None = None) -> str:
        folder = self.root / (today or datetime.now(UTC).date()).isoformat()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{uuid4().hex}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
        return str(path)

    def purge(
        self, *, older_than_days: int = PAYLOAD_RETENTION_DAYS, today: date | None = None
    ) -> list[str]:
        """Delete dated folders older than the retention period. Returns the names removed."""
        if not self.root.is_dir():
            return []
        cutoff = (today or datetime.now(UTC).date()) - timedelta(days=older_than_days)
        removed = []
        for folder in sorted(self.root.iterdir()):
            # Only folders this class created: a real directory named as a date.
            if not folder.is_dir() or folder.is_symlink() or not _DATED_DIR.match(folder.name):
                continue
            try:
                day = date.fromisoformat(folder.name)
            except ValueError:
                continue
            if day < cutoff:
                shutil.rmtree(folder)
                removed.append(folder.name)
        return removed
