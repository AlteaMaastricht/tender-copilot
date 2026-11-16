"""The six evals (docs/EVALS.md section 4). Each one runs its cases and scores them.

A suite reads labels, calls pipeline steps from `evals/targets.py` on snapshot tenders, and
hands plain values to the pure functions in `evals/metrics.py`. Before every case it checks
the cost cap. Case rows hold identifiers and outcomes only, never document or model text.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

from evals import metrics
from evals.budget import RunLog, knockout_tenders
from evals.labels import EdgeCaseLabel, Labels
from evals.metrics import (
    ExtractionFacts,
    LotFacts,
    Outcome,
    RankedChunk,
    Requirement,
    ScreeningRow,
    TenderKnockouts,
)
from evals.snapshot import Snapshot, SnapshotTender, as_of_date, ground_truth, load_tender
from evals.targets import SEARCH_MODES, WAITING_FOR, StepEnv, Targets
from ingest.parsing import start_of_day
from llm.client import CallContext, Gateway
from llm.prompts import PromptRef
from llm.redact import Redactor
from pipeline.filter import FilterResult, apply_hard_filter
from pipeline.schemas import ExtractionOutput, MatchingOutput, Memo, ScreeningOutput

SUITE_ORDER = metrics.SUITES  # cheapest first: retrieval makes no model call


@dataclass(frozen=True, slots=True)
class SuiteResult:
    status: str  # ok, no_labels, not_built, no_rubric, stopped_cost_cap, not_run
    n: int = 0
    metrics: dict[str, Any] = field(default_factory=dict)
    detail: str | None = None  # for not_built: what the suite waits for

    def to_json(self) -> dict[str, Any]:
        result: dict[str, Any] = {"status": self.status}
        if self.detail:
            result["detail"] = self.detail
        if self.n or self.metrics:
            result |= {"n": self.n, "metrics": self.metrics}
        return result


class StepFailed(Exception):
    """A pipeline step failed for one case. Holds the error type only, never its text."""


def _name(exc: BaseException) -> str:
    return str(exc) if isinstance(exc, StepFailed) else type(exc).__name__


def predicted_facts(output: ExtractionOutput) -> ExtractionFacts:
    deadline = output.key_dates.submission_deadline
    return ExtractionFacts(
        deadline=deadline.value if deadline is not None else None,
        lots=tuple(
            LotFacts(lot.title, lot.estimated_value_eur, lot.duration_months, lot.ceiling_value_eur)
            for lot in output.lots
        ),
    )


def suite_score(name: str, result: SuiteResult) -> float | None:
    """The one number a suite contributes to the composite. None unless the suite is ok."""
    if result.status != "ok":
        return None
    values = result.metrics
    if name == "retrieval":
        return values["hybrid"]["hit_at_5"]["value"]
    if name == "screening":
        return values["f2"]
    if name == "extraction":
        return values["extraction_score"]
    if name == "knockout":
        return values["recall"]["value"]
    if name == "memo":
        return values.get("judge_score")
    return values["pass_rate"]["value"]


class Runner:
    def __init__(
        self,
        *,
        snapshot: Snapshot,
        labels: Labels,
        targets: Targets,
        gateway: Gateway,
        log: RunLog,
        run_id: UUID,
        stage1_model: str,
        stage2_model: str,
        judge_model: str | None,
        prompts: Mapping[str, PromptRef],
        limit: int | None = None,
        min_precision: float | None = None,
        memo_dir: Path | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.snapshot = snapshot
        self.labels = labels
        self.targets = targets
        self.gateway = gateway
        self.log = log
        self.run_id = run_id
        self.stage1_model = stage1_model
        self.stage2_model = stage2_model
        self.judge_model = judge_model
        self.prompts = prompts
        self.limit = limit
        self.min_precision = min_precision
        self.memo_dir = memo_dir
        self._clock = clock

        self.cases: list[dict[str, Any]] = []
        self.stopped = False  # the cost cap was reached
        self.screened = 0  # tenders sent to the screening model
        self.memo_tenders: set[int] = set()
        self.memos_produced = 0
        self._tenders: dict[int, SnapshotTender] = {}
        self._screenings: dict[
            tuple[int, date], tuple[FilterResult, ScreeningOutput | None, str | None]
        ] = {}
        self._extractions: dict[int, ExtractionOutput | Exception] = {}
        self._matchings: dict[int, MatchingOutput | Exception] = {}
        self._memos: dict[int, Memo | Exception] = {}

    # --- orchestration -------------------------------------------------------------

    def run(self, names: Sequence[str]) -> dict[str, SuiteResult]:
        results: dict[str, SuiteResult] = {}
        for name in SUITE_ORDER:
            if name not in names:
                continue
            if self.stopped:
                results[name] = SuiteResult("not_run", detail="the cost cap was reached")
                continue
            results[name] = getattr(self, f"_suite_{name}")()
        return results

    def _cases[T](self, rows: Sequence[T]) -> Iterator[T]:
        """The cases of a suite. Stops, and marks the run stopped, once the cap is reached."""
        for row in rows[: self.limit] if self.limit is not None else rows:
            if self.log.cap_reached:
                self.stopped = True
                return
            yield row

    def _status(self) -> str:
        return "stopped_cost_cap" if self.stopped else "ok"

    def _not_built(self, needed: Sequence[str]) -> SuiteResult | None:
        missing = self.targets.missing(needed)
        if not missing:
            return None
        return SuiteResult("not_built", detail="; ".join(WAITING_FOR[name] for name in missing))

    def _row(self, suite: str, case: object, publication_id: int, **values: Any) -> None:
        self.cases.append(
            {"suite": suite, "case": case, "publication_id": publication_id, **values}
        )

    # --- snapshot and cached pipeline steps ----------------------------------------

    def _tender(self, publication_id: int) -> SnapshotTender:
        if publication_id not in self._tenders:
            self._tenders[publication_id] = load_tender(self.snapshot, publication_id)
        return self._tenders[publication_id]

    def _env(self, tender: SnapshotTender, model: str) -> StepEnv:
        return StepEnv(
            gateway=self.gateway,
            model=model,
            prompts=self.prompts,
            context=CallContext(run_id=self.run_id, tender_id=tender.publication_id),
            redactor=Redactor(
                tender.contact_names,
                organisations=[b.name for b in tender.notice.buyers if b.name],
            ),
        )

    @staticmethod
    def _cached[T](cache: dict[int, T | Exception], key: int, compute: Callable[[], T]) -> T:
        """Run a step once per tender. A failure is kept too, and raised again as StepFailed."""
        if key not in cache:
            try:
                cache[key] = compute()
            except Exception as exc:  # a failed step fails its cases, not the run
                cache[key] = exc
        value = cache[key]
        if isinstance(value, Exception):
            raise StepFailed(_name(value)) from value
        return value

    def _screen(
        self, tender: SnapshotTender, today: date | None = None
    ) -> tuple[FilterResult, ScreeningOutput | None, str | None]:
        """Hard filter, then the screening call. Returns (filter, output, error type)."""
        today = today or as_of_date(tender.notice)
        key = (tender.publication_id, today)
        if key not in self._screenings:
            result = apply_hard_filter(
                tender.notice,
                sluitingsdatum=tender.sluitingsdatum,
                tenderned_matched=tender.tenderned is not None,
                now=start_of_day(today),
            )
            output, error = None, None
            if result.passed:
                assert self.targets.screen is not None
                self.screened += 1
                try:
                    output = self.targets.screen(
                        tender.notice, result, self._env(tender, self.stage1_model)
                    )
                except Exception as exc:
                    error = type(exc).__name__
            self._screenings[key] = (result, output, error)
        return self._screenings[key]

    def _extract(self, tender: SnapshotTender) -> ExtractionOutput:
        assert self.targets.extract is not None
        step = self.targets.extract
        return self._cached(
            self._extractions,
            tender.publication_id,
            lambda: step(tender.docs, self._env(tender, self.stage2_model)),
        )

    def _match(self, tender: SnapshotTender, extraction: ExtractionOutput) -> MatchingOutput:
        assert self.targets.match is not None
        step = self.targets.match
        return self._cached(
            self._matchings,
            tender.publication_id,
            lambda: step(tender.docs, extraction, self._env(tender, self.stage2_model)),
        )

    def _memo(self, tender: SnapshotTender) -> Memo:
        """Stage 2 for one tender. Without readable documents the memo is notice-only."""
        assert self.targets.memo is not None
        step = self.targets.memo
        extraction = matching = None
        if tender.docs.readable:
            extraction = self._extract(tender)
            matching = self._match(tender, extraction)
        return self._cached(
            self._memos,
            tender.publication_id,
            lambda: step(
                tender.notice,
                tender.sluitingsdatum,
                tender.docs,
                extraction,
                matching,
                self._env(tender, self.stage2_model),
            ),
        )

    # --- retrieval -----------------------------------------------------------------

    def _suite_retrieval(self) -> SuiteResult:
        file = self.labels.retrieval
        if file.missing:
            return SuiteResult("no_labels")
        if blocked := self._not_built(["search"]):
            return blocked
        assert self.targets.search is not None
        ranks: dict[str, list[int | None]] = {mode: [] for mode in SEARCH_MODES}
        by_language: dict[str, dict[str, list[int | None]]] = {mode: {} for mode in SEARCH_MODES}
        latency: dict[str, list[float]] = {mode: [] for mode in SEARCH_MODES}
        count = 0
        for question in self._cases(file.rows):
            count += 1
            docs = self._tender(question.publication_id).docs
            answers: dict[str, set[int]] = {}
            for answer in question.answers:
                answers.setdefault(answer.doc_key, set()).update(answer.pages)
            for mode in SEARCH_MODES:
                rank, error = None, None
                started = self._clock()
                try:
                    hits = self.targets.search(docs, question.question, metrics.RETRIEVAL_K, mode)
                    latency[mode].append((self._clock() - started) * 1000)
                    chunks = [
                        RankedChunk(docs.doc_key(hit.document_id), hit.page_start, hit.page_end)
                        for hit in list(hits)[: metrics.RETRIEVAL_K]
                    ]
                    rank = metrics.first_relevant_rank(chunks, answers)
                except Exception as exc:
                    error = type(exc).__name__
                ranks[mode].append(rank)
                by_language[mode].setdefault(question.language, []).append(rank)
                self._row(
                    "retrieval", question.question_id, question.publication_id,
                    mode=mode, rank=rank, error=error,
                )  # fmt: skip
        values = {
            mode: {
                "hit_at_5": metrics.hit_at_k(ranks[mode]),
                "mrr_at_10": metrics.mrr_at_k(ranks[mode]),
                "by_language": {
                    language: {
                        "hit_at_5": metrics.hit_at_k(found),
                        "mrr_at_10": metrics.mrr_at_k(found),
                    }
                    for language, found in sorted(by_language[mode].items())
                },
                "latency_ms": metrics.p50_p95(latency[mode]),
            }
            for mode in SEARCH_MODES
        }
        return SuiteResult(self._status(), count, values)

    # --- screening -----------------------------------------------------------------

    def _suite_screening(self) -> SuiteResult:
        file = self.labels.screening
        if file.missing:
            return SuiteResult("no_labels")
        if blocked := self._not_built(["screen"]):
            return blocked
        rows: list[ScreeningRow] = []
        for label in self._cases(file.rows):
            result, output, error = self._screen(self._tender(label.publication_id))
            row = ScreeningRow(
                actual=label.label == "relevant",
                filter_passed=result.passed,
                decision=output.decision if output is not None else None,
                confidence=output.confidence if output is not None else None,
                error=error is not None,
            )
            rows.append(row)
            self._row(
                "screening", label.publication_id, label.publication_id,
                actual=row.actual, filter_passed=row.filter_passed, decision=row.decision,
                predicted=metrics.screening_positive(row), error=error,
            )  # fmt: skip
        return SuiteResult(
            self._status(), len(rows), metrics.screening_metrics(rows, self.min_precision)
        )

    # --- extraction ----------------------------------------------------------------

    def _suite_extraction(self) -> SuiteResult:
        file = self.labels.extraction
        if file.missing:
            return SuiteResult("no_labels")
        if blocked := self._not_built(["extract"]):
            return blocked
        units: dict[str, list[list[float]]] = {name: [] for name in metrics.GRADED_FIELDS}
        vat_explained = errors = count = 0
        disagreements: list[bool] = []
        for label in self._cases(file.rows):
            count += 1
            tender = self._tender(label.publication_id)
            truth = ground_truth(tender)
            if truth.deadline is not None and tender.notice.deadline is not None:
                disagreements.append(
                    not metrics.same_local_date(tender.notice.deadline, truth.deadline)
                )
            changes = label.overrides.model_dump(exclude_none=True)
            if "deadline" in changes:
                changes["deadline_has_time"] = True
            if "estimated_value" in changes:  # a corrected total replaces the lot values
                changes["lots"] = tuple(replace(lot, estimated_value=None) for lot in truth.lots)
            truth = replace(truth, **changes)
            predicted, error = None, None
            try:
                predicted = predicted_facts(self._extract(tender))
            except StepFailed as exc:
                error = _name(exc)
                errors += 1
            grade = metrics.grade_extraction(truth, predicted, label.skip_fields)
            vat_explained += grade.vat_explained
            for name in metrics.GRADED_FIELDS:
                units[name].append(grade.units.get(name, []))
            self._row(
                "extraction", label.publication_id, label.publication_id,
                units={name: values for name, values in grade.units.items()}, error=error,
            )  # fmt: skip
        fields = {name: metrics.field_summary(units[name], count) for name in units}
        return SuiteResult(
            self._status(),
            count,
            {
                "extraction_score": metrics.extraction_score(fields),
                "fields": fields,
                "fields_without_ground_truth": list(metrics.UNGRADED_FIELDS),
                "vat_explained": vat_explained,
                "notice_deadline_disagreement": metrics.proportion(
                    sum(disagreements), len(disagreements)
                ),
                "errors": errors,
            },
        )

    # --- knock-out recall ----------------------------------------------------------

    def _suite_knockout(self) -> SuiteResult:
        file = self.labels.knockout
        if file.missing:
            return SuiteResult("no_labels")
        if blocked := self._not_built(["extract"]):
            return blocked
        graded: list[TenderKnockouts] = []
        statuses: list[tuple[str | None, str]] = []
        wants_status = any(row.expected_status is not None for row in file.rows)
        errors = 0
        for publication_id in self._cases(knockout_tenders(self.labels)):
            tender = self._tender(publication_id)
            rows = [row for row in file.rows if row.publication_id == publication_id]
            labels = [
                Requirement(row.label_id, row.doc_key, row.page, row.category, row.kind)
                for row in rows
            ]
            predictions: list[Requirement] = []
            extraction, error = None, None
            try:
                extraction = self._extract(tender)
                predictions = [
                    Requirement(
                        item.requirement_id,
                        tender.docs.doc_key(item.citation.document_id),
                        item.citation.page,
                        item.category,
                        item.kind,
                    )
                    for item in extraction.knockout_criteria
                ]
            except StepFailed as exc:
                error = _name(exc)
                errors += 1
            result = metrics.grade_knockouts(labels, predictions)
            graded.append(result)

            expected = {row.label_id: row.expected_status for row in rows if row.expected_status}
            if expected and extraction is not None and self.targets.match is not None:
                try:
                    matching = self._match(tender, extraction)
                    by_id = {m.requirement_id: m.status for m in matching.matches}
                except StepFailed:
                    by_id = {}
                for label_id, status in expected.items():
                    if label_id in result.pairs:
                        statuses.append((by_id.get(result.pairs[label_id]), status))
            self._row(
                "knockout", publication_id, publication_id,
                labels=result.labels, matched=result.matched, predictions=result.predictions,
                missed=sorted(
                    row.label_id for row in rows
                    if row.kind != metrics.FORMAL and row.label_id not in result.pairs
                ),
                error=error,
            )  # fmt: skip
        values = metrics.knockout_metrics(graded)
        values["errors"] = errors
        if wants_status and self.targets.match is None:
            values["match_accuracy"] = {"status": "not_built", "detail": WAITING_FOR["match"]}
        elif wants_status:
            values |= metrics.match_accuracy(statuses)
        return SuiteResult(self._status(), len(graded), values)

    # --- memo quality --------------------------------------------------------------

    def _suite_memo(self) -> SuiteResult:
        """Runs on the tenders that have knock-out labels (docs/EVALS.md 4.5)."""
        if self.labels.knockout.missing:
            return SuiteResult("no_labels")
        if blocked := self._not_built(["extract", "match", "memo"]):
            return blocked
        rubric = self.labels.rubric
        judging = rubric is not None and self.targets.judge is not None and self.judge_model
        total = verified = errors = count = 0
        scores: list[float] = []
        judged: dict[str, Mapping[str, int]] = {}  # memo hash -> judge scores
        for publication_id in self._cases(knockout_tenders(self.labels)):
            count += 1
            self.memo_tenders.add(publication_id)
            tender = self._tender(publication_id)
            digest, score, error = None, None, None
            try:
                memo = self._memo(tender)
                self.memos_produced += 1
                total += len(memo.citations)
                verified += sum(citation.verified is True for citation in memo.citations)
                digest = self._keep_memo(memo)
                if judging:
                    judged[digest] = self._judge(memo, tender)
                    score = metrics.memo_score(judged[digest], rubric.as_criteria())
                    scores.append(score)
            except Exception as exc:
                error = _name(exc)
                errors += 1
            self._row("memo", publication_id, publication_id,
                      memo_sha256=digest, judge_score=score, error=error)  # fmt: skip

        values: dict[str, Any] = {
            "verified_citation_share": metrics.proportion(verified, total),
            "memos": count - errors,
            "errors": errors,
        }
        if self.stopped:
            return SuiteResult("stopped_cost_cap", count, values)
        if rubric is None:
            return SuiteResult("no_rubric", count, values)
        if not judging:
            waiting = WAITING_FOR["judge"] if self.targets.judge is None else "--judge-model"
            return SuiteResult("not_built", count, values, detail=waiting)
        values["judge_score"] = metrics.mean(scores)
        values["judge_agreement"] = self._judge_agreement(judged)
        return SuiteResult("ok", count, values)

    def _keep_memo(self, memo: Memo) -> str:
        """The memo's hash. The memo itself is saved only when a memo folder was given."""
        data = memo.model_dump_json().encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        if self.memo_dir is not None:
            folder = self.memo_dir / str(self.run_id)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{memo.tender_id}.json").write_bytes(data)
        return digest

    def _judge(self, memo: Memo, tender: SnapshotTender) -> Mapping[str, int]:
        assert self.targets.judge is not None and self.labels.rubric is not None
        assert self.judge_model is not None
        return self.targets.judge(memo, self.labels.rubric, self._env(tender, self.judge_model))

    def _judge_agreement(self, judged: dict[str, Mapping[str, int]]) -> dict[str, Any]:
        """Judge against the hand grades, on exactly the memo texts that were graded."""
        grades = {row.memo_sha256: row for row in self.labels.memo_grades.rows}
        stored = sorted(self.memo_dir.glob("*/*.json")) if self.memo_dir is not None else []
        for path in stored:
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            if digest in grades and digest not in judged and not self.log.cap_reached:
                try:
                    memo = Memo.model_validate_json(data)
                    judged[digest] = self._judge(memo, self._tender(memo.tender_id))
                except Exception:
                    continue  # a stored memo that cannot be judged is left out of the check
        pairs: dict[str, list[tuple[int, int]]] = {}
        for digest, grade in grades.items():
            for criterion, hand in grade.scores.items():
                if digest in judged and criterion in judged[digest]:
                    pairs.setdefault(criterion, []).append((judged[digest][criterion], hand))
        return {
            "graded_memos": len(grades),
            "graded_memos_judged": sum(digest in judged for digest in grades),
            "by_criterion": {name: metrics.agreement(found) for name, found in pairs.items()},
        }

    # --- edge cases ----------------------------------------------------------------

    def _suite_edge_cases(self) -> SuiteResult:
        file = self.labels.edge_cases
        if file.missing:
            return SuiteResult("no_labels")
        needed = ["screen", "extract", "match", "memo"]
        if any(case.question for case in file.rows):
            needed.append("chat")
        if blocked := self._not_built(needed):
            return blocked
        passed = failed = invalid = 0
        for case in self._cases(file.rows):
            before = len(self.log.records)
            try:
                outcome = self._chat_case(case) if case.question else self._pipeline_case(case)
                outcome.model_calls = len(self.log.records) - before
                verdict = metrics.check_expectations(case.expect, outcome)
                status, keys, error = verdict.status, list(verdict.failed), None
            except Exception as exc:
                status, keys, error = "fail", [], _name(exc)
            passed += status == "pass"
            failed += status == "fail"
            invalid += status == "invalid_label"
            self._row("edge_cases", case.case_id, case.publication_id,
                      type=case.type, status=status, failed_keys=keys, error=error)  # fmt: skip
        return SuiteResult(
            self._status(),
            passed + failed + invalid,
            {
                "pass_rate": metrics.proportion(passed, passed + failed),
                "passed": passed,
                "failed": failed,
                "invalid_label": invalid,
            },
        )

    def _pipeline_case(self, case: EdgeCaseLabel) -> Outcome:
        """Filter, screen, then Stage 2 for a shortlisted tender, as the daily job would."""
        tender = self._tender(case.publication_id)
        result, screening, error = self._screen(tender, case.as_of)
        if error is not None:
            raise StepFailed(error)
        outcome = Outcome(
            filter_passed=result.passed,
            filter_failed_rules=tuple(
                name for name, rule in result.rules.items() if rule.outcome == "fail"
            ),
            filter_flags=result.flags,
            screened=result.passed,
        )
        if screening is None:
            return outcome
        outcome.model_text = (screening.reason,)
        if screening.decision == "reject":
            return outcome
        memo = self._memo(tender)
        extraction = self._extractions.get(tender.publication_id)
        outcome.memo_created = True
        outcome.recommendation = memo.recommendation
        outcome.reason_codes = tuple(memo.recommendation_reason_codes)
        outcome.limitations = tuple(memo.limitations)
        if isinstance(extraction, ExtractionOutput):
            outcome.extraction_warnings = tuple(extraction.warnings)
        outcome.model_text += (memo.model_dump_json(),)
        return outcome

    def _chat_case(self, case: EdgeCaseLabel) -> Outcome:
        assert self.targets.chat is not None and case.question is not None
        tender = self._tender(case.publication_id)
        answer = self.targets.chat(tender.docs, case.question, self._env(tender, self.stage2_model))
        if answer is None:
            return Outcome()
        return Outcome(chat_in_scope=answer.in_scope, model_text=(answer.answer,))
