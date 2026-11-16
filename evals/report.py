"""Cost, latency and the results file (docs/EVALS.md 4.8 and section 6).

Everything here is computed from the run's `LlmCallRecord` rows. The results file holds
numbers and identifiers only: no document text, no model free text, no contact data.
"""

from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from collections.abc import Collection, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

from evals.metrics import p50_p95
from llm.log import LlmCallRecord

SCHEMA_VERSION = 1
SCREENING_STAGE = "screening"
MEMO_STAGES = frozenset({"extraction_map", "extraction_reduce", "matching", "memo"})
JUDGE_STAGE = "judge"
_MICRO = Decimal("0.000001")


class ResultsError(Exception):
    """The results file exists but is not one this runner wrote."""


def _usd(value: Decimal | None) -> str | None:
    return None if value is None else str(value.quantize(_MICRO, rounding=ROUND_HALF_UP))


def cost_summary(
    records: Sequence[LlmCallRecord],
    *,
    screened: int,
    memo_tenders: Collection[int],
    memos_produced: int,
) -> dict[str, Any]:
    """Cost per 100 screened and per memo. A failed call's billed cost counts."""
    by_stage: dict[str, Decimal] = defaultdict(Decimal)
    for record in records:
        by_stage[record.stage] += record.cost_usd
    screening = by_stage.get(SCREENING_STAGE, Decimal(0))
    memo = sum(
        (r.cost_usd for r in records if r.stage in MEMO_STAGES and r.tender_id in memo_tenders),
        Decimal(0),
    )
    return {
        "total_usd": _usd(sum(by_stage.values(), Decimal(0))),
        "per_100_screened_usd": _usd(100 * screening / screened if screened else None),
        "screened": screened,
        "per_memo_usd": _usd(memo / memos_produced if memos_produced else None),
        "memos_produced": memos_produced,
        "memos_failed": len(memo_tenders) - memos_produced,
        "eval_overhead_usd": _usd(by_stage.get(JUDGE_STAGE, Decimal(0))),
        "by_stage": {stage: _usd(cost) for stage, cost in sorted(by_stage.items())},
    }


def latency_summary(records: Sequence[LlmCallRecord]) -> dict[str, Any]:
    """p50 and p95 per stage: per call, and per tender (the sum of its calls in that stage)."""
    per_call: dict[str, list[float]] = defaultdict(list)
    per_tender: dict[str, dict[int | None, float]] = defaultdict(lambda: defaultdict(float))
    for record in records:
        per_call[record.stage].append(record.latency_ms)
        per_tender[record.stage][record.tender_id] += record.latency_ms
    return {
        stage: {
            "per_call": p50_p95(per_call[stage]),
            "per_tender": p50_p95(list(per_tender[stage].values())),
        }
        for stage in sorted(per_call)
    }


def calls_summary(records: Sequence[LlmCallRecord]) -> dict[str, Any]:
    return {
        "total": len(records),
        "by_status": dict(sorted(Counter(r.status for r in records).items())),
        "validation_retries": sum(r.validation_retries for r in records),
        "network_retries": sum(r.network_retries for r in records),
    }


def rounded(value: Any) -> Any:
    """Floats to six places, all the way down, so the file is stable and readable."""
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, Mapping):
        return {key: rounded(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [rounded(item) for item in value]
    return value


def results_path(results_dir: Path, day: str, commit: str) -> Path:
    return results_dir / f"{day}_{commit}.json"


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def append_run(results_dir: Path, day: str, commit: str, entry: Mapping[str, Any]) -> Path:
    """Add one run to `<date>_<commit>.json`. Earlier runs in the file are kept as they are."""
    path = results_path(results_dir, day, commit)
    if path.is_file():
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            valid = document["schema_version"] == SCHEMA_VERSION and isinstance(
                document["runs"], list
            )
        except (json.JSONDecodeError, KeyError, TypeError):
            valid = False
        if not valid:
            raise ResultsError(f"{path.name} exists and is not a results file of this runner")
    else:
        document = {
            "schema_version": SCHEMA_VERSION,
            "date": day,
            "git_commit": commit,
            "runs": [],
        }
    document["runs"].append(rounded(entry))
    _write_atomic(path, json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    return path


def write_cases(
    results_dir: Path, day: str, commit: str, run_id: str, rows: Sequence[Mapping[str, Any]]
) -> Path:
    """Per-case rows: identifiers and outcomes only."""
    path = results_dir / f"{day}_{commit}_cases" / f"{run_id}.jsonl"
    lines = [json.dumps(rounded(row), ensure_ascii=False, default=str) for row in rows]
    _write_atomic(path, "".join(line + "\n" for line in lines))
    return path
