# Build log

One entry per plan step that passed review. Written by the /build supervisor.

## docs/plans/manual_snapshot.md

### Pre-M4 (a) and (b): details check and document download (24 Nov 2026)
- Approved by Arthur in the `/build ... M4-M6` arguments; not plan steps.
- Files: `scripts/check_manual_details.py`, `tests/test_check_manual_details.py`, `scripts/download_manual.py`, `tests/test_download_manual.py`, `tests/fixtures/tenderned/synthetic/documents.json`.
- Network: 40 detail GETs; then 20 document-list GETs and 346 file GETs. No failures.
- Result: final 20 = the provisional 20, no replacements. Recorded in DECISIONS.md.
- Not separately reviewed by the reviewer agent.
- Open: deletion of the 8 non-final folders in `data/raw/manual/` (permission denied to the supervisor).

### M4: text extraction (25 Nov 2026), PASS after 1 fix round
- Files: `ingest/documents.py`, `ingest/extract_text.py`, `scripts/make_document_fixtures.py`, `tests/fixtures/documents/` (14 files), `tests/test_extract_text.py`, `tests/test_documents_format.py`.
- Tests: 1397/1397; lint clean.
- Fix round 1: XLSX opened from bytes (names without `.xlsx`), ZIP/OLE headers checked before `%PDF`, `normalise_name` strips known extensions only.
- Open (low): DOCX drops nested-table, `w:sdt`, `w:ins` and text-box text without a note (4 of 127 real files lose >10%); broad `except` also catches import errors, `find_tables` failure is silent; NUL test covers only `clean_text`; heading styles match exactly, not "starts with" as D5 says. pymupdf prints a hint line to stdout.

### Pre-M4 (a)+(b) review (25 Nov 2026), PASS after 2 fix rounds
- Files: `scripts/check_manual_details.py`, `scripts/download_manual.py`, `tests/test_check_manual_details.py`, `tests/test_download_manual.py`, `tests/fixtures/tenderned/synthetic/documents.json`.
- Tests: 1441/1441; lint clean.
- Fixes: rerun idempotent through a per-folder `.download_ids.json` (DD4); `documentId` must match `[A-Za-z0-9_-]{1,64}`; damaged index stops the run; `name_taken` recorded; stricter `resolve_href`; non-JSON 200 in the details check stops the run; contact test can fail.
- Open (low): the 24 Nov download has no index, so a rerun downloads everything again; the name length plus ID suffix can exceed 255 bytes (uncaught `OSError`); a `name_taken` file is downloaded again on each rerun; docstring vs. behaviour for 3xx/non-JSON lists; no paging check on the list; `resolve_href` misses percent-encoded `..`; the href prefix was not checked against live hrefs.
- The 8 non-final folders were deleted by Arthur (25 Nov).

### M5: classification rules (25 Nov 2026), PASS (DD5 amendment, then 1 fix round)
- Files: `ingest/classify.py`, `tests/test_classify.py`.
- Tests: 1454/1454; lint clean.
- Escalated on 24 Nov: the D7 table mislabelled real names. Arthur approved the DD5 amendment on 25 Nov. Fix round 1: whitespace collapsed again after `_` becomes a space.
- Open (labels the amended table gives as written): UEA files give `guideline` (`aanbestedingsdocument` before `uea`); "Aanbestedingsleidraad raamovereenkomst …" gives `contract`; some "… na NvI" files without a contract or pricing word stay `qa`; `verklaring` catches an auditor's statement; `gibit`, "Rectificatie", "Aanvullende mededelingen", "staat van ontleding …", "Inschrijvingsbiljet" and "Invulformat kwaliteit" have no fitting rule; substrings such as `contract` in `contractmanager`. Archive files must not be passed to `classify_by_rule` (M6).
