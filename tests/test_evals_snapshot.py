"""Tests for the frozen snapshot reader (evals/snapshot.py)."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from evals.metrics import AMSTERDAM
from evals.snapshot import (
    SnapshotError,
    as_of_date,
    build_manifest,
    deadline_truth,
    files_hash,
    ground_truth,
    load_tender,
    open_snapshot,
)
from tests.evals_stubs import (
    CONTACT_CANARY,
    EMAIL_CANARY,
    PAGE_CANARY,
    SNAPSHOT,
    TRUTH_CANARIES,
    copy_snapshot,
    make_config,
    run,
)

PAGE_FILE = "tenders/9001/pages/a90010000001.jsonl"


def test_the_fixture_snapshot_matches_its_manifest():
    snapshot = open_snapshot(SNAPSHOT)
    assert snapshot.tender_ids == (9001, 9002, 9003, 9004)
    assert snapshot.version == "test-fixture-1"
    manifest = json.loads((SNAPSHOT / "manifest.json").read_text())
    assert snapshot.sha256 == files_hash(manifest["files"])
    # The committed manifest is what `build_manifest` gives for the committed files.
    rebuilt = build_manifest(SNAPSHOT, snapshot_version="test-fixture-1", frozen_at="2026-11-15")
    assert rebuilt == manifest


def test_the_snapshot_hash_is_stable_and_does_not_depend_on_file_order():
    files = json.loads((SNAPSHOT / "manifest.json").read_text())["files"]
    assert files_hash(files) == files_hash(list(reversed(files)))
    assert open_snapshot(SNAPSHOT).sha256 == open_snapshot(SNAPSHOT).sha256
    assert len(files_hash(files)) == 64


def test_the_hash_changes_when_a_page_file_changes(tmp_path):
    root = copy_snapshot(tmp_path)
    before = open_snapshot(root).sha256
    page = root / PAGE_FILE
    page.write_text(page.read_text() + '{"page_no": 4, "text": "Een nieuwe pagina."}\n')
    manifest = build_manifest(root, snapshot_version="test-fixture-2", frozen_at="2026-11-16")
    (root / "manifest.json").write_text(json.dumps(manifest))
    assert open_snapshot(root).sha256 != before


def _change(root: Path) -> None:
    page = root / PAGE_FILE
    page.write_text(page.read_text().replace("Geschiktheidseisen", "Geschiktheidseisem"))


def _remove(root: Path) -> None:
    (root / PAGE_FILE).unlink()


def _add(root: Path) -> None:
    (root / "tenders" / "9001" / "extra.json").write_text("{}")


def _no_manifest(root: Path) -> None:
    (root / "manifest.json").unlink()


def _escape(root: Path) -> None:
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["files"][0]["path"] = "../outside.json"
    (root / "manifest.json").write_text(json.dumps(manifest))


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (_change, "changed since the freeze"),  # same size, other content
        (_remove, "missing"),
        (_add, "not in the manifest"),
        (_no_manifest, "manifest.json not found"),
        (_escape, "outside the snapshot"),
    ],
)
def test_a_snapshot_that_differs_from_its_manifest_is_refused(tmp_path, damage, message):
    root = copy_snapshot(tmp_path)
    damage(root)
    with pytest.raises(SnapshotError, match=message):
        open_snapshot(root)
    # The runner stops with exit code 2, calls no model and writes no results.
    ran = run(tmp_path, snapshot_dir=root)
    assert ran.code == 2
    assert ran.provider.requests == []
    assert not ran.results_dir.exists()
    assert message in ran.out[0]


def test_a_folder_without_a_snapshot_is_refused(tmp_path):
    ran = run(tmp_path, snapshot_dir=tmp_path / "nowhere")
    assert ran.code == 2
    assert "no snapshot at" in ran.out[0]


def test_loading_a_tender():
    snapshot = open_snapshot(SNAPSHOT)
    tender = load_tender(snapshot, 9001)
    assert tender.notice.publication_id == "9001-2026"
    assert tender.sluitingsdatum == "2026-04-20T12:00:00+02:00"
    assert [doc.doc_key for doc in tender.docs.documents] == ["a90010000001", "a90010000002"]
    assert [len(doc.pages) for doc in tender.docs.documents] == [3, 2]
    assert tender.docs.doc_key(2) == "a90010000002" and tender.docs.doc_key(99) is None
    assert tender.docs.readable
    scanned = load_tender(snapshot, 9004)
    assert not scanned.docs.readable  # its only document is a scan without text
    assert scanned.contact_names == ()
    with pytest.raises(SnapshotError, match="not in the snapshot"):
        load_tender(snapshot, 12345)


def test_the_as_of_date_is_the_day_after_publication():
    snapshot = open_snapshot(SNAPSHOT)
    assert as_of_date(load_tender(snapshot, 9001).notice) == date(2026, 3, 3)
    assert as_of_date(load_tender(snapshot, 9003).notice) == date(2026, 4, 7)


def test_an_edge_case_as_of_date_overrides_the_default(tmp_path):
    # 9003 closes on 10 April. Seen the day after publication it fails as too short; seen on
    # 1 June (the label's `as_of`) it fails as closed. Both are the deadline rule.
    ran = run(tmp_path, suites=("edge_cases",), limit=1)
    case = ran.cases()[0]
    assert (case["case"], case["status"]) == ("EC-01", "pass")
    assert ran.last["params"]["as_of_rule"] == "published_plus_1_day"
    # With the default date the same tender is dropped too, so no screening call is made.
    screening = run(tmp_path, suites=("screening",))
    by_tender = {row["publication_id"]: row for row in screening.cases()}
    assert by_tender[9003]["filter_passed"] is False


def test_deadline_truth():
    published = date(2026, 3, 2)
    moment, has_time = deadline_truth("2026-04-20T12:00:00+02:00", published)
    assert has_time and moment.isoformat() == "2026-04-20T12:00:00+02:00"
    naive, has_time = deadline_truth("2026-04-20T12:00:00", published)
    assert has_time and naive.tzinfo is AMSTERDAM  # read as Amsterdam time
    day, has_time = deadline_truth("2026-06-15", published)
    assert not has_time and day == datetime(2026, 6, 15, tzinfo=AMSTERDAM)
    for unknown in (None, "", "onbekend", "2099-12-31", "2040-01-01"):  # missing or placeholder
        assert deadline_truth(unknown, published) == (None, False)
    # A UTC value is the same moment, shown in Amsterdam time: the 20th, not the 19th.
    utc, has_time = deadline_truth("2026-04-19T22:00:00Z", published)
    assert has_time and utc.isoformat() == "2026-04-20T00:00:00+02:00"
    dutch, has_time = deadline_truth("20-04-2026 12:00", published)
    assert has_time and dutch == moment


def test_the_evals_and_the_hard_filter_read_the_closing_date_the_same_way():
    from ingest.parsing import parse_sluitingsdatum

    published = date(2026, 3, 2)
    for raw in ("2026-04-19T22:00:00Z", "2026-04-20T12:00:00", "2026-06-15", "20-04-2026 12:00"):
        closing = parse_sluitingsdatum(raw)
        assert deadline_truth(raw, published) == (closing.moment, closing.has_time)


def test_ground_truth_comes_from_the_notice_and_tenderned():
    snapshot = open_snapshot(SNAPSHOT)
    truth = ground_truth(load_tender(snapshot, 9002))
    assert truth.deadline.isoformat() == "2026-05-11T10:00:00+02:00"
    assert [lot.estimated_value for lot in truth.lots] == [Decimal("150000"), Decimal("250000")]
    assert [lot.duration_months for lot in truth.lots] == [24.0, 36.0]
    assert truth.estimated_value == Decimal("400000")
    assert truth.framework_max_value == Decimal("500000")


def test_ground_truth_and_contact_data_never_reach_a_model(tmp_path):
    """The extraction stub sends everything it is given. None of it is ground truth."""
    ran = run(tmp_path, suites=("extraction", "knockout", "memo"))
    assert ran.code == 0
    sent = ran.provider.sent_text()
    assert PAGE_CANARY in sent  # the documents themselves were sent
    for canary in TRUTH_CANARIES:
        assert canary not in sent
    # The contact name and the email on page 1 are redacted before the call.
    assert CONTACT_CANARY not in sent
    assert EMAIL_CANARY not in sent
    assert "[NAME]" in sent and "[EMAIL]" in sent


def test_the_snapshot_summary_is_recorded_in_the_results(tmp_path):
    ran = run(tmp_path, suites=("retrieval",))
    assert ran.last["snapshot"] == {
        "version": "test-fixture-1",
        "sha256": open_snapshot(SNAPSHOT).sha256,
        "tenders": 4,
    }
    assert make_config(tmp_path).snapshot_dir == SNAPSHOT


# --- rectifications (DD2) ----------------------------------------------------------------------


def _with_rectification(tmp_path: Path, content: dict | None) -> Path:
    root = copy_snapshot(tmp_path)
    if content is not None:
        path = root / "tenders" / "9002" / "tenderned_rectification.json"
        path.write_text(json.dumps(content))
    manifest = build_manifest(root, snapshot_version="test-fixture-3", frozen_at="2026-11-16")
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root


def test_the_rectification_gives_the_closing_date_and_the_original_is_kept(tmp_path):
    original = load_tender(open_snapshot(SNAPSHOT), 9002)
    assert original.tenderned_rectification is None
    assert original.sluitingsdatum == original.tenderned["sluitingsDatum"]

    root = _with_rectification(tmp_path, {"sluitingsDatum": "2026-05-18T09:30:00"})
    tender = load_tender(open_snapshot(root), 9002)
    assert tender.sluitingsdatum == "2026-05-18T09:30:00"
    assert tender.tenderned["sluitingsDatum"] == original.tenderned["sluitingsDatum"]
    assert ground_truth(tender).deadline.isoformat() == "2026-05-18T09:30:00+02:00"


def test_a_rectification_without_a_closing_date_falls_back_to_the_original(tmp_path):
    original = load_tender(open_snapshot(SNAPSHOT), 9002)
    cases = ({"publicatieId": 1}, {"sluitingsDatum": None}, {"sluitingsDatum": " "})
    for number, content in enumerate(cases):
        root = _with_rectification(tmp_path / str(number), content)
        tender = load_tender(open_snapshot(root), 9002)
        assert tender.sluitingsdatum == original.sluitingsdatum


def test_a_rectification_is_read_even_without_a_tenderned_json(tmp_path):
    original = load_tender(open_snapshot(SNAPSHOT), 9002)
    assert original.tenderned is not None
    root = _with_rectification(tmp_path, {"sluitingsDatum": "2026-05-18T09:30:00"})
    (root / "tenders" / "9002" / "tenderned.json").unlink()
    manifest = build_manifest(root, snapshot_version="test-fixture-3", frozen_at="2026-11-16")
    (root / "manifest.json").write_text(json.dumps(manifest))
    # The rectification still counts: effective_sluitingsdatum reads it on its own.
    assert load_tender(open_snapshot(root), 9002).sluitingsdatum == "2026-05-18T09:30:00"
