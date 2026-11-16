"""Tests for the label loaders (evals/labels.py). The loaders only ever read."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evals.labels import (
    EdgeCaseLabel,
    ExtractionLabel,
    KnockoutLabel,
    LabelError,
    RetrievalLabel,
    ScreeningLabel,
    load_all,
    load_jsonl,
)
from tests.evals_stubs import DATASETS, copy_datasets, run

# The example rows of docs/EVALS.md section 5, one per format.
DOC_EXAMPLES = [
    (
        ScreeningLabel,
        '{"publication_id": 412551, "label": "relevant", "stratum": "shortlist", '
        '"note": "data platform for a province"}',
    ),
    (
        RetrievalLabel,
        '{"question_id": "RQ-01", "publication_id": 412551, "question": "Wat zijn de '
        'gunningscriteria en hun wegingen?", "language": "nl", "answers": [{"doc_key": '
        '"a3f9c2d41b07", "pages": [14, 15]}]}',
    ),
    (
        ExtractionLabel,
        '{"publication_id": 412551, "skip_fields": ["estimated_value"], "overrides": '
        '{"lot_count": 2}, "note": "notice value is the framework maximum"}',
    ),
    (
        KnockoutLabel,
        '{"publication_id": 412551, "label_id": "412551-KO-03", "doc_key": "a3f9c2d41b07", '
        '"page": 22, "category": "financial", "kind": "suitability", "text": "Minimum annual '
        'turnover of EUR 1,000,000 in each of the last three years.", "threshold": '
        '"EUR 1,000,000 per year", "expected_status": "met"}',
    ),
    (
        EdgeCaseLabel,
        '{"case_id": "EC-02", "type": "passed_deadline", "publication_id": 409819, "as_of": '
        '"2026-04-01", "expect": {"filter_passed": false, "filter_failed_rule": "deadline", '
        '"memo_created": false, "max_model_calls": 0}}',
    ),
]


def write(folder: Path, name: str, *rows: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text("".join(row + "\n" for row in rows), encoding="utf-8")
    return folder


def screening(folder: Path):
    return load_jsonl(folder, "screening.jsonl", ScreeningLabel, lambda r: r.publication_id)


def test_each_of_the_five_formats_loads():
    labels = load_all(DATASETS)
    assert [len(file.rows) for file in labels.files()[:5]] == [4, 4, 3, 6, 3]
    assert labels.knockout.rows[0].expected_status == "met"
    assert labels.extraction.rows[2].overrides.lot_count == 1
    assert labels.edge_cases.rows[0].as_of.isoformat() == "2026-06-01"
    assert labels.memo_grades.missing and labels.rubric is None  # the memo slot is empty


@pytest.mark.parametrize(("model", "line"), DOC_EXAMPLES, ids=lambda v: getattr(v, "__name__", ""))
def test_the_example_rows_in_the_format_doc_are_valid(model, line):
    row = model.model_validate(json.loads(line))
    assert row.publication_id in (412551, 409819)
    # And the document really shows this row.
    doc = (Path(__file__).resolve().parents[1] / "docs" / "EVALS.md").read_text(encoding="utf-8")
    assert line in doc


@pytest.mark.parametrize(
    ("row", "problem"),
    [
        ('{"publication_id": 9001, "label": "relevant", "colour": "red"}', "colour"),
        ('{"publication_id": 9001, "label": "maybe"}', "label"),
        ('{"publication_id": "abc", "label": "relevant"}', "publication_id"),
        ('{"label": "relevant"}', "publication_id"),
        ("{not json", "not valid JSON"),
    ],
)
def test_a_bad_row_is_rejected_with_the_file_name_and_line_number(tmp_path, row, problem):
    folder = write(
        tmp_path, "screening.jsonl", '{"publication_id": 9002, "label": "relevant"}', row
    )
    with pytest.raises(LabelError) as error:
        screening(folder)
    assert "screening.jsonl line 2" in str(error.value)
    assert problem in str(error.value)


def test_a_duplicate_identifier_is_rejected(tmp_path):
    line = '{"publication_id": 9001, "label": "relevant"}'
    with pytest.raises(LabelError, match="screening.jsonl line 3: duplicate identifier"):
        screening(write(tmp_path, "screening.jsonl", line, "", line))  # blank lines are skipped


@pytest.mark.parametrize(
    "row",
    [
        '{"publication_id": 9001, "label_id": "9002-KO-01", "doc_key": "a90010000001", "page": 1, '
        '"category": "financial", "kind": "suitability", "text": "Turnover."}',  # other tender
        '{"publication_id": 9001, "label_id": "9001-KO-01", "doc_key": "Leidraad.pdf", "page": 1, '
        '"category": "financial", "kind": "suitability", "text": "Turnover."}',  # not a doc key
        '{"publication_id": 9001, "label_id": "9001-KO-01", "doc_key": "a90010000001", "page": 0, '
        '"category": "financial", "kind": "suitability", "text": "Turnover."}',  # page 0
        '{"publication_id": 9001, "label_id": "9001-KO-01", "doc_key": "a90010000001", "page": 1, '
        '"category": "money", "kind": "suitability", "text": "Turnover."}',  # unknown category
        '{"publication_id": 9001, "label_id": "9001-KO-01", "doc_key": "a90010000001", "page": 1, '
        '"category": "financial", "kind": "suitability", "text": "T.", "expected_status": '
        '"action_required"}',  # a model can never produce it
    ],
)
def test_knockout_rows_are_checked(tmp_path, row):
    with pytest.raises(LabelError, match="knockout.jsonl line 1"):
        load_all(write(tmp_path, "knockout.jsonl", row))


def test_extraction_overrides_and_skip_fields_are_checked(tmp_path):
    bad_skip = '{"publication_id": 9001, "skip_fields": ["colour"]}'
    with pytest.raises(LabelError, match="extraction.jsonl line 1: skip_fields"):
        load_all(write(tmp_path / "a", "extraction.jsonl", bad_skip))
    bad_override = '{"publication_id": 9001, "overrides": {"award_types": ["price"]}}'
    with pytest.raises(LabelError, match="extraction.jsonl line 1: overrides"):
        load_all(write(tmp_path / "b", "extraction.jsonl", bad_override))


def test_an_unknown_expect_key_loads_and_is_left_to_the_scoring(tmp_path):
    row = '{"case_id": "EC-01", "type": "cancelled", "publication_id": 9001, "expect": {"mood": 1}}'
    labels = load_all(write(tmp_path, "edge_cases.jsonl", row))
    assert labels.edge_cases.rows[0].expect == {"mood": 1}
    empty = '{"case_id": "EC-01", "type": "cancelled", "publication_id": 9001, "expect": {}}'
    with pytest.raises(LabelError, match="expect"):
        load_all(write(tmp_path / "b", "edge_cases.jsonl", empty))


def test_a_missing_file_is_no_labels_not_an_error(tmp_path):
    labels = load_all(tmp_path)  # an empty folder
    assert all(file.missing and file.rows == () for file in labels.files())
    assert labels.summary()["screening.jsonl"] is None


def test_a_run_reports_no_labels_and_continues_with_the_other_suites(tmp_path):
    datasets = copy_datasets(tmp_path)
    (datasets / "retrieval.jsonl").unlink()
    (datasets / "knockout.jsonl").unlink()
    ran = run(tmp_path, datasets_dir=datasets)
    assert ran.code == 0
    assert ran.suite("retrieval") == {"status": "no_labels"}
    assert ran.suite("knockout") == {"status": "no_labels"}
    assert ran.suite("memo") == {"status": "no_labels"}  # the memo eval uses the knock-out tenders
    assert ran.suite("screening")["status"] == "ok"
    assert ran.suite("extraction")["status"] == "ok"
    assert ran.last["labels"]["retrieval.jsonl"] is None
    assert ran.last["overall"]["value"] is None


def test_the_label_hash_is_the_sha256_of_the_file_and_changes_with_it(tmp_path):
    line = '{"publication_id": 9001, "label": "relevant"}'
    folder = write(tmp_path, "screening.jsonl", line)
    first = screening(folder)
    assert first.sha256 == hashlib.sha256((folder / "screening.jsonl").read_bytes()).hexdigest()
    assert first.summary() == {"sha256": first.sha256, "rows": 1}
    write(tmp_path, "screening.jsonl", line.replace("relevant", "not_relevant"))
    assert screening(folder).sha256 != first.sha256


def test_the_run_records_the_hash_of_every_label_file(tmp_path):
    recorded = run(tmp_path, suites=("screening",)).last["labels"]
    for name in ("screening", "retrieval", "extraction", "knockout", "edge_cases"):
        expected = hashlib.sha256((DATASETS / f"{name}.jsonl").read_bytes()).hexdigest()
        assert recorded[f"{name}.jsonl"]["sha256"] == expected
    assert recorded["memo_rubric.json"] is None


@pytest.mark.parametrize(
    "note",
    ["mail jan@gemeente-voorbeeld.example", "bel 06-12345678", "tel. +31 20 123 4567"],
)
def test_a_row_with_an_email_address_or_phone_number_is_rejected(tmp_path, note):
    row = json.dumps({"publication_id": 9001, "label": "relevant", "note": note})
    with pytest.raises(LabelError) as error:
        screening(write(tmp_path, "screening.jsonl", row))
    assert "screening.jsonl line 1" in str(error.value)
    assert note.split()[-1] not in str(error.value)  # the message never repeats the value


def test_amounts_dates_and_identifiers_are_not_mistaken_for_phone_numbers(tmp_path):
    row = json.dumps(
        {
            "publication_id": 9001,
            "label_id": "9001-KO-01",
            "doc_key": "0612345678ab",
            "page": 1,
            "category": "financial",
            "kind": "suitability",
            "text": "Turnover of EUR 1,000,000 per year since 01-01-2024.",
            "threshold": "EUR 1,000,000",
        }  # fmt: skip
    )
    assert len(load_all(write(tmp_path, "knockout.jsonl", row)).knockout.rows) == 1


def _listing(folder: Path) -> dict[str, str]:
    return {
        path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    }


def test_a_full_run_leaves_the_datasets_folder_byte_identical(tmp_path):
    datasets = copy_datasets(tmp_path, rubric=True)
    before = _listing(datasets)
    assert run(tmp_path, datasets_dir=datasets, judge_model="claude-haiku-5-5").code == 0
    assert _listing(datasets) == before


def test_nothing_in_evals_opens_a_file_for_writing_under_the_datasets_folder():
    source = (Path(__file__).resolve().parents[1] / "evals" / "labels.py").read_text()
    for writer in ("write_text", "write_bytes", ".open(", "unlink", "mkdir", "os.replace"):
        assert writer not in source, f"evals/labels.py uses {writer}"
