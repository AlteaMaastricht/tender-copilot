"""Run the evals on the frozen snapshot and append the result to `evals/results/`.

    python -m evals.run --stage1-model M1 --stage2-model M2 --max-cost-usd 5
    python -m evals.run --stage1-model M1 --stage2-model M2 --dry-run

The model is a run parameter and its provider follows from `llm/prices.py`, so the
three-provider comparison is this runner run three times. A live run needs a cost cap; a dry
run counts the calls and estimates the cost without building a gateway. Formulas, label
formats and the results file are described in docs/EVALS.md.

Exit codes: 0 finished, 2 usage, snapshot or label error, 3 stopped by the cost cap.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from evals import metrics
from evals.budget import RunLog, estimate_run
from evals.labels import LabelError, Labels, load_all
from evals.report import (
    ResultsError,
    append_run,
    calls_summary,
    cost_summary,
    latency_summary,
    write_cases,
)
from evals.snapshot import Snapshot, SnapshotError, load_tender, open_snapshot
from evals.suites import SUITE_ORDER, Runner, SuiteResult, suite_score
from evals.targets import Targets, default_targets
from llm.client import Gateway
from llm.log import CallLog, JsonlCallLog
from llm.prices import PRICES, ModelPrice
from llm.prompts import PROMPTS_DIR, PromptError, PromptRef
from pipeline.prompt_specs import prompt_refs

ROOT = Path(__file__).resolve().parent.parent
EXIT_OK, EXIT_USAGE, EXIT_COST_CAP = 0, 2, 3
AS_OF_RULE = "published_plus_1_day"
SNAPSHOT_DIR = ROOT / "data" / "snapshot"
DATASETS_DIR = ROOT / "evals" / "datasets"
RESULTS_DIR = ROOT / "evals" / "results"
WEIGHTS_PATH = ROOT / "evals" / "weights.toml"
MEMO_DIR = ROOT / "data" / "eval_memos"  # git-ignored: memos hold tender text


class UsageError(Exception):
    """The run cannot start. Nothing was called and nothing was written."""


@dataclass(frozen=True, slots=True)
class RunConfig:
    stage1_model: str
    stage2_model: str
    judge_model: str | None = None
    max_cost_usd: Decimal | None = None
    dry_run: bool = False
    suites: tuple[str, ...] = SUITE_ORDER
    limit: int | None = None
    prompt_overrides: Mapping[str, str] = field(default_factory=dict)  # name -> version
    min_precision: float | None = None
    snapshot_dir: Path = SNAPSHOT_DIR
    datasets_dir: Path = DATASETS_DIR
    results_dir: Path = RESULTS_DIR
    weights_path: Path = WEIGHTS_PATH
    memo_dir: Path | None = None  # generated memos are saved only when this is given


# --- inputs ---------------------------------------------------------------------


def git_state() -> tuple[str, bool]:
    """Short commit hash and whether the working tree has changes."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()  # fmt: skip
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return "nogit", False
    return commit or "nogit", bool(status.strip())


def load_weights(path: Path) -> tuple[dict[str, float], float | None]:
    """The composite weights and the knock-out recall gate (None: not set)."""
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        weights = {name: float(value) for name, value in document["weights"].items()}
        minimum = document.get("gate", {}).get("min_knockout_recall")
    except (OSError, tomllib.TOMLDecodeError, KeyError, TypeError, ValueError):
        raise UsageError(f"{path.name} cannot be read as weights") from None
    if set(weights) != set(SUITE_ORDER):
        raise UsageError(f"{path.name} must give a weight for each of: {', '.join(SUITE_ORDER)}")
    if abs(sum(weights.values()) - 1) > 1e-9 or any(value < 0 for value in weights.values()):
        raise UsageError(f"the weights in {path.name} must be 0 or more and sum to 1")
    number = isinstance(minimum, int | float) and not isinstance(minimum, bool)
    if minimum is not None and not (number and 0 <= minimum <= 1):
        raise UsageError("min_knockout_recall must be a number between 0 and 1")
    return weights, None if minimum is None else float(minimum)


def resolve_prompts(overrides: Mapping[str, str], prompts_dir: Path) -> dict[str, PromptRef]:
    """Prompt name to the version this run uses. Every file must exist before any call."""
    try:
        prompts = prompt_refs(overrides)  # references are made only in the prompt registry
    except ValueError as exc:
        raise UsageError(str(exc)) from None
    for ref in prompts.values():
        try:
            present = (prompts_dir / ref.filename).is_file()
        except PromptError as exc:
            raise UsageError(str(exc)) from None
        if not present:
            raise UsageError(f"prompt file not found: {ref.filename}")
    return prompts


def check_models(config: RunConfig, prices: Mapping[str, ModelPrice]) -> None:
    """A model without a price row cannot be called (llm/prices.py)."""
    for model in (config.stage1_model, config.stage2_model, config.judge_model):
        if model is not None and model not in prices:
            raise UsageError(f"no price row for model {model!r}; add one to llm/prices.py")


def check_labels(labels: Labels, snapshot: Snapshot) -> None:
    """Every label must point at a tender, and a document, that the snapshot holds."""
    for file in labels.files():
        for row in file.rows:
            if row.publication_id not in snapshot.tender_ids:
                raise UsageError(f"{file.name}: tender {row.publication_id} is not in the snapshot")
    wanted = [(r.publication_id, r.doc_key, labels.knockout.name) for r in labels.knockout.rows]
    wanted += [
        (row.publication_id, answer.doc_key, labels.retrieval.name)
        for row in labels.retrieval.rows
        for answer in row.answers
    ]
    docs = {pid: load_tender(snapshot, pid).docs for pid in {item[0] for item in wanted}}
    for publication_id, doc_key, name in wanted:
        if not docs[publication_id].has_doc_key(doc_key):
            raise UsageError(f"{name}: tender {publication_id} has no document {doc_key}")


def list_docs(snapshot_dir: Path, publication_id: int, out: Callable[[str], None]) -> None:
    """Print the documents of one tender with their keys, for labelling."""
    tender = load_tender(open_snapshot(snapshot_dir), publication_id)
    out(f"tender {publication_id}: documents_access = {tender.docs.documents_access}")
    for doc in tender.docs.documents:
        out(
            f"  {doc.doc_key}  {doc.doc_type:<12} {doc.extraction_status:<11} "
            f"{len(doc.pages):>4} {doc.page_kind or 'page'}s  {doc.filename}"
        )


# --- the run --------------------------------------------------------------------


def _default_gateway(log: CallLog) -> Gateway:
    from llm.providers import default_providers  # the SDKs load only for a live run

    return Gateway(default_providers(), log)


def run_evals(
    config: RunConfig,
    *,
    targets: Targets | None = None,
    gateway_factory: Callable[[CallLog], Gateway] = _default_gateway,
    sink: CallLog | None = None,
    prices: Mapping[str, ModelPrice] = PRICES,
    prompts_dir: Path = PROMPTS_DIR,
    git: Callable[[], tuple[str, bool]] = git_state,
    now: Callable[[], datetime] = datetime.now,
    out: Callable[[str], None] = print,
) -> int:
    try:
        return _run(
            config,
            targets=default_targets() if targets is None else targets,
            gateway_factory=gateway_factory,
            sink=sink,
            prices=prices,
            prompts_dir=prompts_dir,
            git=git,
            now=now,
            out=out,
        )
    except (UsageError, SnapshotError, LabelError, ResultsError) as exc:
        out(f"evals: {exc}")
        return EXIT_USAGE


def _run(
    config: RunConfig,
    *,
    targets: Targets,
    gateway_factory: Callable[[CallLog], Gateway],
    sink: CallLog | None,
    prices: Mapping[str, ModelPrice],
    prompts_dir: Path,
    git: Callable[[], tuple[str, bool]],
    now: Callable[[], datetime],
    out: Callable[[str], None],
) -> int:
    if unknown := sorted(set(config.suites) - set(SUITE_ORDER)):
        raise UsageError(f"unknown suite(s): {', '.join(unknown)}")
    if not config.dry_run and config.max_cost_usd is None:
        raise UsageError("a live run needs --max-cost-usd; use --dry-run for an estimate")
    check_models(config, prices)
    prompts = resolve_prompts(config.prompt_overrides, prompts_dir)
    weights, minimum_recall = load_weights(config.weights_path)
    snapshot = open_snapshot(config.snapshot_dir)
    labels = load_all(config.datasets_dir)
    check_labels(labels, snapshot)

    if config.dry_run:
        estimate = estimate_run(
            snapshot=snapshot,
            labels=labels,
            suites=config.suites,
            limit=config.limit,
            stage1_model=config.stage1_model,
            stage2_model=config.stage2_model,
            judge_model=config.judge_model,
            prompts=prompts,
            prompts_dir=prompts_dir,
            prices=prices,
        )
        out("dry run: no model is called and no results file is written")
        out(json.dumps(estimate.to_json(), indent=2))
        return EXIT_OK

    started = now()
    run_id = uuid4()
    log = RunLog(config.max_cost_usd, sink)
    runner = Runner(
        snapshot=snapshot,
        labels=labels,
        targets=targets,
        gateway=gateway_factory(log),
        log=log,
        run_id=run_id,
        stage1_model=config.stage1_model,
        stage2_model=config.stage2_model,
        judge_model=config.judge_model,
        prompts=prompts,
        limit=config.limit,
        min_precision=config.min_precision,
        memo_dir=config.memo_dir,
    )
    results = runner.run(config.suites)
    commit, dirty = git()
    day = started.date().isoformat()

    scores = {name: suite_score(name, result) for name, result in results.items()}
    knockout = results.get("knockout")
    overall = metrics.composite(scores, weights) | {
        "weights": weights,
        "gate": metrics.gate(
            scores.get("knockout"), minimum_recall, knockout is not None and knockout.status == "ok"
        ),
        "min_knockout_recall": minimum_recall,
    }
    entry: dict[str, Any] = {
        "run_id": str(run_id),
        "started_at": started.isoformat(timespec="seconds"),
        "finished_at": now().isoformat(timespec="seconds"),
        "completed": not runner.stopped,
        "stop_reason": "cost_cap" if runner.stopped else None,
        "not_recommended": overall["gate"] == "fail",
        "git_dirty": dirty,
        "params": {
            "stage1_model": config.stage1_model,
            "stage1_provider": prices[config.stage1_model].provider,
            "stage2_model": config.stage2_model,
            "stage2_provider": prices[config.stage2_model].provider,
            "judge_model": config.judge_model,
            "max_cost_usd": str(config.max_cost_usd),
            "suites": [name for name in SUITE_ORDER if name in config.suites],
            "limit": config.limit,
            "min_precision": config.min_precision,
            "as_of_rule": AS_OF_RULE,
        },
        "prompt_versions": {name: ref.version for name, ref in sorted(prompts.items())},
        "snapshot": snapshot.summary(),
        "labels": labels.summary(),
        "suites": {name: result.to_json() for name, result in results.items()},
        "overall": overall,
        "cost": cost_summary(
            log.records,
            screened=runner.screened,
            memo_tenders=runner.memo_tenders,
            memos_produced=runner.memos_produced,
        ),
        "latency_ms": latency_summary(log.records),
        "calls": calls_summary(log.records),
    }
    path = append_run(config.results_dir, day, commit, entry)
    write_cases(config.results_dir, day, commit, str(run_id), runner.cases)
    _print_summary(results, entry, path, out)
    return EXIT_COST_CAP if runner.stopped else EXIT_OK


def _print_summary(
    results: Mapping[str, SuiteResult],
    entry: Mapping[str, Any],
    path: Path,
    out: Callable[[str], None],
) -> None:
    for name, result in results.items():
        score = suite_score(name, result)
        shown = "" if score is None else f"  score {score:.3f}"
        detail = f"  ({result.detail})" if result.detail else ""
        out(f"{name:<11} {result.status:<17} n={result.n}{shown}{detail}")
    overall, cost = entry["overall"], entry["cost"]
    out(f"overall {overall['value']}  partial {overall['partial']}  gate {overall['gate']}")
    out(
        f"cost USD {cost['total_usd']}  per 100 screened {cost['per_100_screened_usd']}  "
        f"per memo {cost['per_memo_usd']}"
    )
    if entry["stop_reason"]:
        out("stopped: the cost cap was reached; the scores cover the finished cases only")
    out(f"results: {path}")


# --- command line ----------------------------------------------------------------


def _prompt_override(text: str) -> tuple[str, str]:
    name, _, version = text.partition("=")
    if not name or not version:
        raise argparse.ArgumentTypeError("use NAME=VERSION, for example extract_map=v2")
    return name, version


def _money(text: str) -> Decimal:
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("not an amount") from None
    if not value.is_finite() or value <= 0:
        raise argparse.ArgumentTypeError("the cost cap must be above 0")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m evals.run", description=__doc__.split("\n")[0])
    parser.add_argument("--stage1-model", help="screening model")
    parser.add_argument("--stage2-model", help="extraction, matching, memo and chat model")
    parser.add_argument("--judge-model", help="memo judge; the same for every compared run")
    parser.add_argument("--max-cost-usd", type=_money, help="stop cleanly at this spend")
    parser.add_argument("--dry-run", action="store_true", help="count calls, estimate cost")
    parser.add_argument("--suites", help=f"comma-separated, from: {', '.join(SUITE_ORDER)}")
    parser.add_argument("--limit", type=int, help="first N cases of each suite")
    parser.add_argument(
        "--prompt", action="append", type=_prompt_override, default=[], metavar="NAME=VERSION"
    )
    parser.add_argument("--min-precision", type=float, help="report recall at this precision")
    parser.add_argument("--snapshot", type=Path, default=SNAPSHOT_DIR)
    parser.add_argument("--datasets", type=Path, default=DATASETS_DIR)
    parser.add_argument("--results", type=Path, default=RESULTS_DIR)
    parser.add_argument(
        "--memo-dir", type=Path, default=MEMO_DIR, help="where generated memos are saved"
    )
    parser.add_argument("--list-docs", type=int, metavar="PUBLICATION_ID")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.list_docs is not None:
        try:
            list_docs(args.snapshot, args.list_docs, print)
        except SnapshotError as exc:
            print(f"evals: {exc}")
            return EXIT_USAGE
        return EXIT_OK
    if not args.stage1_model or not args.stage2_model:
        parser.error("--stage1-model and --stage2-model are required")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be 1 or more")
    config = RunConfig(
        stage1_model=args.stage1_model,
        stage2_model=args.stage2_model,
        judge_model=args.judge_model,
        max_cost_usd=args.max_cost_usd,
        dry_run=args.dry_run,
        suites=tuple(args.suites.split(",")) if args.suites else SUITE_ORDER,
        limit=args.limit,
        prompt_overrides=dict(args.prompt),
        min_precision=args.min_precision,
        snapshot_dir=args.snapshot,
        datasets_dir=args.datasets,
        results_dir=args.results,
        memo_dir=args.memo_dir,
    )
    return run_evals(config, sink=None if config.dry_run else JsonlCallLog())


if __name__ == "__main__":
    sys.exit(main())
