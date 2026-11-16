"""Stub pipeline steps for the eval tests. No network, no keys, no provider SDK.

Every stub calls the real `Gateway` with a fake provider, so cost and latency reach the run
through the real call log. The answers the pipeline would give are canned below, for the four
synthetic tenders in `tests/fixtures/evals/snapshot/`.
"""

from __future__ import annotations

import dataclasses
import itertools
import json
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from evals.run import WEIGHTS_PATH, RunConfig, run_evals
from evals.targets import SearchHit, StepEnv, Targets
from llm.client import Gateway
from llm.log import CallLog
from llm.prompts import PROMPTS_DIR, PromptRef, UntrustedText
from llm.providers.base import ProviderRequest, ProviderResponse, Usage
from pipeline.memo import compute_fit, decide_recommendation
from pipeline.prompt_specs import ALL
from pipeline.schemas import (
    ChatAnswer,
    ExtractionOutput,
    KbEvidence,
    MatchingOutput,
    Memo,
    MemoDeadlines,
    RequirementMatch,
    ScreeningOutput,
    TenderFacts,
)
from tests.fakes import HAIKU, SONNET, FakeProvider, reply

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "evals"
SNAPSHOT = FIXTURES / "snapshot"
DATASETS = FIXTURES / "datasets"

PAGE_CANARY = "PAGINAKANARIE"  # in every page text
CONTACT_CANARY = "Kanariepiet"  # the contact name in contacts.json and on a page
EMAIL_CANARY = "inkoop@kanariestad.example"
# Values that exist only in the notice or in TenderNed: the ground truth.
TRUTH_CANARIES = ("2026-04-20T12:00:00+02:00", "417333", "Gemeente Kanariestad")

NOW = datetime(2026, 11, 15, 10, 0, 0)
COMMIT = "abc1234"
USAGE = Usage(input_tokens=1000, output_tokens=200)
HAIKU_CALL = Decimal("0.0002")  # 1000 x 0.10 + 200 x 0.50, per million
SONNET_CALL = Decimal("0.004")  # 1000 x 2.00 + 200 x 10.00, per million
LATENCY_MS = 250


class Ack(BaseModel):
    ok: bool


class RepeatingProvider(FakeProvider):
    """Answers every request with the same small reply, and records the request."""

    def __init__(self) -> None:
        super().__init__()

    def send(self, request: ProviderRequest) -> ProviderResponse:
        self.requests.append(request)
        return reply({"ok": True}, usage=USAGE, model=request.model)


def ack() -> ProviderResponse:
    return reply({"ok": True}, usage=USAGE)


# --- canned answers -------------------------------------------------------------

QUOTE = "De inschrijver voldoet aan deze eis."


def _cite(document_id: int, page: int) -> dict[str, Any]:
    return {"document_id": document_id, "page": page, "quote": QUOTE}


def _knockout(number: int, category: str, kind: str, document_id: int, page: int) -> dict:
    return {
        "requirement_id": f"KO-{number:02d}",
        "category": category,
        "kind": kind,
        "text": f"Synthetic requirement {number}.",
        "threshold": None,
        "proof": None,
        "proof_when": None,
        "consortium_allowed": None,
        "citation": _cite(document_id, page),
    }


def _lot(title: str, value: str | None, months: int | None, ceiling: str | None = None) -> dict:
    return {
        "lot_identifier": None,
        "title": title,
        "summary": "Synthetic lot.",
        "estimated_value_eur": value,
        "ceiling_value_eur": ceiling,
        "value_includes_vat": None,
        "duration_months": months,
        "extension_months": None,
        "citations": [],
    }


def _extraction(deadline: str, lots: list[dict], knockouts: list[dict]) -> dict[str, Any]:
    return {
        "key_dates": {
            "submission_deadline": {"value": deadline, "citation": _cite(1, 1)},
            "questions_deadline": None,
        },
        "lots": lots,
        "framework": None,
        "knockout_criteria": knockouts,
        "award_basis": None,
        "award_criteria": [],
        "contract_risks": [],
        "documents_read": [],
        "documents_skipped": [],
        "warnings": [],
    }


EXTRACTIONS: dict[int, dict[str, Any]] = {
    # Deadline 30 minutes off (correct), value and duration correct. KO-03 has the wrong
    # category, so the certification label is missed.
    9001: _extraction(
        "2026-04-20T12:30:00",
        [_lot("Dataplatform", "417333", 36)],
        [
            _knockout(1, "financial", "suitability", 1, 2),
            _knockout(2, "reference_project", "suitability", 1, 3),
            _knockout(3, "technical_capacity", "minimum_requirement", 2, 1),
            _knockout(4, "other", "formal", 1, 3),
        ],
    ),
    # Deadline a day late (wrong). Lot 1's value includes VAT. The exclusion ground is missed.
    9002: _extraction(
        "2026-05-12T10:00:00+02:00",
        [
            _lot("Perceel 1 - licenties dashboardsoftware", "181500", 24, "200000"),
            _lot("Perceel 2: implementatie & beheer", "250000", 30, "300000"),
        ],
        [_knockout(1, "insurance", "suitability", 3, 1)],
    ),
}
SCREENINGS: dict[int, tuple[str, float]] = {
    9001: ("shortlist", 0.9),
    9002: ("reject", 0.2),
    9004: ("unsure", 0.4),
}
MATCH_STATUS: dict[tuple[int, str], str] = {(9001, "KO-02"): "met"}  # the label says not_met
HITS: dict[tuple[str, str], list[SearchHit]] = {
    ("Wat zijn de gunningscriteria en hun wegingen?", "hybrid"): [SearchHit(1, 3, 3)],
    ("Wat zijn de gunningscriteria en hun wegingen?", "vector_only"): [
        SearchHit(1, 1, 1),
        SearchHit(1, 2, 3),
    ],
    ("Welke omzeteis geldt er?", "hybrid"): [
        SearchHit(2, 1, 1),
        SearchHit(1, 1, 1),
        SearchHit(1, 2, 2),
    ],
    ("Which insurance is required?", "hybrid"): [SearchHit(3, 2, 2)] * 6 + [SearchHit(3, 1, 1)],
    ("Which insurance is required?", "vector_only"): [SearchHit(3, 1, 1)],
    # Document 99 is not in the snapshot; the other hit is on the wrong page.
    ("Welke uitsluitingsgronden gelden?", "hybrid"): [SearchHit(99, 2, 2), SearchHit(3, 1, 1)],
}
JUDGE_SCORES = {"complete": 4, "clear": 5}
RUBRIC = {
    "version": "v1",
    "criteria": [
        {"id": "complete", "question": "Are all knock-outs listed?", "scale_max": 5, "weight": 3},
        {"id": "clear", "question": "Is the recommendation clear?", "scale_max": 5, "weight": 1},
    ],
}


# --- stub steps -----------------------------------------------------------------


def _call(env: StepEnv, stage: str, prompt: PromptRef, text: str) -> None:
    env.gateway.call(
        task=stage,
        prompt=prompt,
        data=[UntrustedText(stage, text)],
        schema=Ack,
        model=env.model,
        redactor=env.redactor,
        context=env.context,
    )


def screen(notice, filter_result, env: StepEnv) -> ScreeningOutput:
    seen = f"{notice.title} | {notice.buyer_name} | {notice.estimated_value}"
    _call(env, "screening", env.prompts["screen"], seen)
    decision, confidence = SCREENINGS[env.context.tender_id]
    return ScreeningOutput(
        decision=decision,
        reason="Synthetic screening reason.",
        matched_services=[],
        exclusion_flags=["none"],
        confidence=confidence,
    )


def extract(docs, env: StepEnv) -> ExtractionOutput:
    if not docs.readable:
        raise RuntimeError("no readable documents")
    # Send everything the step was given, so a test can see that no ground truth is in it.
    everything = json.dumps(dataclasses.asdict(docs), ensure_ascii=False, default=str)
    _call(env, "extraction_map", env.prompts["extract_map"], everything)
    _call(env, "extraction_reduce", env.prompts["extract_reduce"], "merge")
    return ExtractionOutput.model_validate(EXTRACTIONS[docs.tender_id])


def match(docs, extraction: ExtractionOutput, env: StepEnv) -> MatchingOutput:
    matches = []
    for criterion in extraction.knockout_criteria:
        if criterion.kind == "formal":
            continue
        _call(env, "matching", env.prompts["match"], criterion.text)
        matches.append(
            RequirementMatch(
                requirement_id=criterion.requirement_id,
                status=MATCH_STATUS.get((docs.tender_id, criterion.requirement_id), "met"),
                reasoning="Synthetic match.",
                tender_citation=criterion.citation,
                company_evidence=[KbEvidence(kb_id=1, doc_slug="stub", quote="Synthetic.")],
                gap=None,
                mitigation="none",
                unclear_reason=None,
            )
        )
    return MatchingOutput(matches=matches, award_notes=[])


def memo(notice, sluitingsdatum, docs, extraction, matching, env: StepEnv) -> Memo:
    _call(env, "memo", env.prompts["memo"], "draft")
    _call(env, "memo", env.prompts["memo_reason"], "reason")
    knockouts = extraction.knockout_criteria if extraction is not None else []
    fit = compute_fit([])
    decision = decide_recommendation(
        knockouts=knockouts,
        matches=matching.matches if matching is not None else [],
        fit=fit,
        documents_missing=not docs.readable,
    )
    return Memo(
        tender_id=docs.tender_id,
        language="en",
        tender=TenderFacts(
            buyer_name=notice.buyer_name or "",
            title=notice.title or "",
            tenderned_id=str(docs.tender_id),
            tenderned_url=None,
            main_nature=notice.main_nature,
            procedure_type=notice.procedure_type,
            eu_procedure=None,
            cpv_codes=list(notice.cpv_codes),
            documents_access=docs.documents_access,
            published_at=None,
        ),
        summary="Synthetic memo.",
        lots=extraction.lots if extraction is not None else [],
        target_lots=[],
        framework=None,
        deadlines=MemoDeadlines(submission_deadline=None, questions_deadline=None),
        fit=fit,
        knockout_table=[],
        award_basis=None,
        price_quality_tradeoff=None,
        award_table=[],
        risks=[],
        questions_for_qa=[],
        recommendation=decision.recommendation,
        recommendation_reason="Synthetic reason.",
        recommendation_reason_codes=list(decision.reasons),
        reasons=[],
        conditions=[],
        confidence="low",
        reflects_qa_document_ids=[],
        documents_reviewed=[],
        # One citation per knock-out; KO-02's did not pass the citation check.
        citations=[
            k.citation.model_copy(update={"verified": k.requirement_id != "KO-02"})
            for k in knockouts
        ],
        limitations=[] if docs.readable else ["documents not accessible"],
    )


def chat(docs, question: str, env: StepEnv) -> ChatAnswer:
    _call(env, "chat", env.prompts["chat"], question)
    return ChatAnswer(answer="That is not about this tender.", citations=[], in_scope=False)


def judge(memo_: Memo, rubric, env: StepEnv) -> Mapping[str, int]:
    _call(env, "judge", PromptRef("judge", "v1"), memo_.summary)
    return JUDGE_SCORES


def search(docs, question: str, top_k: int, mode: str) -> list[SearchHit]:
    return HITS.get((question, mode), [])


STUBS = Targets(
    search=search, screen=screen, extract=extract, match=match, memo=memo, chat=chat, judge=judge
)


# --- running --------------------------------------------------------------------


def make_prompts(tmp_path: Path) -> Path:
    """A prompts folder: the real gateway blocks and one-line task prompts."""
    folder = tmp_path / "prompts"
    if folder.is_dir():
        return folder
    folder.mkdir()
    for source in PROMPTS_DIR.glob("gateway_*.md"):
        shutil.copy(source, folder / source.name)
    for spec in ALL:
        (folder / spec.ref.filename).write_text(f"Synthetic prompt for {spec.ref.name}.\n")
    (folder / "extract_map_v2.md").write_text("Synthetic prompt, second version.\n")
    (folder / "judge_v1.md").write_text("Synthetic judge prompt.\n")
    return folder


def gateway_factory(prompts_dir: Path, provider: FakeProvider) -> Callable[[CallLog], Gateway]:
    """A gateway whose every call takes exactly LATENCY_MS on a scripted clock."""
    ticks = itertools.count()

    def build(log: CallLog) -> Gateway:
        return Gateway(
            {"anthropic": provider},
            log,
            prompts_dir=prompts_dir,
            secrets=(),
            sleep=lambda seconds: None,
            clock=lambda: next(ticks) * LATENCY_MS / 1000,
        )

    return build


@dataclass(frozen=True, slots=True)
class Ran:
    code: int
    provider: FakeProvider
    out: list[str]
    results_dir: Path

    @property
    def path(self) -> Path:
        return self.results_dir / f"{NOW.date().isoformat()}_{COMMIT}.json"

    def document(self) -> dict[str, Any]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    @property
    def last(self) -> dict[str, Any]:
        return self.document()["runs"][-1]

    def suite(self, name: str) -> dict[str, Any]:
        return self.last["suites"][name]

    def cases(self) -> list[dict[str, Any]]:
        path = self.results_dir / f"{self.path.stem}_cases" / f"{self.last['run_id']}.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def written_text(self) -> str:
        """Every file under the results folder, as one string."""
        return "\n".join(
            p.read_text(encoding="utf-8")
            for p in sorted(self.results_dir.rglob("*"))
            if p.is_file()
        )


def make_config(tmp_path: Path, **changes: Any) -> RunConfig:
    values: dict[str, Any] = {
        "stage1_model": HAIKU,
        "stage2_model": SONNET,
        "max_cost_usd": Decimal("5"),
        "snapshot_dir": SNAPSHOT,
        "datasets_dir": DATASETS,
        "results_dir": tmp_path / "results",
        "weights_path": WEIGHTS_PATH,
    }
    return RunConfig(**(values | changes))


def run(
    tmp_path: Path,
    *,
    provider: FakeProvider | None = None,
    targets: Targets | None = STUBS,
    **changes: Any,
) -> Ran:
    """One eval run on the fixture snapshot with the fake provider."""
    provider = provider if provider is not None else RepeatingProvider()
    prompts_dir = make_prompts(tmp_path)
    out: list[str] = []
    config = make_config(tmp_path, **changes)
    code = run_evals(
        config,
        targets=targets,
        gateway_factory=gateway_factory(prompts_dir, provider),
        prompts_dir=prompts_dir,
        git=lambda: (COMMIT, False),
        now=lambda: NOW,
        out=out.append,
    )
    return Ran(code, provider, out, config.results_dir)


def copy_datasets(tmp_path: Path, *, rubric: bool = False) -> Path:
    """A private copy of the label fixtures that a test may change."""
    folder = tmp_path / "datasets"
    shutil.copytree(DATASETS, folder)
    if rubric:
        (folder / "memo_rubric.json").write_text(json.dumps(RUBRIC), encoding="utf-8")
    return folder


def copy_snapshot(tmp_path: Path) -> Path:
    folder = tmp_path / "snapshot"
    shutil.copytree(SNAPSHOT, folder)
    return folder
