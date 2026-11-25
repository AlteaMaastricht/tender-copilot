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
