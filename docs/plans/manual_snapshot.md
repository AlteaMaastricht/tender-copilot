# Plan: the manual snapshot (`freeze_snapshot.py --from-manual`)

Status: plan, 24 Nov 2026. Nothing in this file is built. It cuts `docs/plans/documents.md` down to what the manual snapshot needs by Thu 26 Nov. Everything else in that plan waits until after the Sia application (27 Nov). Read with: `CLAUDE.md`, `docs/DECISIONS.md` "Plan decisions (23 Nov 2026)", `docs/plans/documents.md` D1–D3, D5, D9 and section 4, `docs/EVALS.md` 3.

## 0. Goal and the command

Arthur downloads the documents of each chosen tender by hand into `data/raw/manual/<publication_id>/`. Then:

```
python scripts/freeze_snapshot.py --from-manual data/raw/manual --out data/snapshot --version v1-manual-2026-11-26
```

The command writes the layout of `EVALS.md` 3, and `evals.snapshot.open_snapshot` accepts it. The script still makes network calls, for the TenderNed detail and the TED notice (section 3); it downloads no documents.

**Done means:** `make test` passes, `make lint` is clean, and a live run (Arthur approves it) produces a snapshot of 20 tenders that opens with `open_snapshot`.

## 1. Open questions for Arthur (answer before M0)

| # | Question | Recommendation |
|---|---|---|
| MQ1 | **Which 20 tenders?** DD8 says a tender without a TED notice is replaced by "the next draw (seed 20261113)". That draw needs the selection script, which is outside the repo (`NEXT.md` item 4). | Take the `selected = true` rows in file order and skip each one without a TED notice until 20 have one. Record the skipped IDs and the rule in DECISIONS.md, as an interim reading of DD8 for the manual snapshot. Strict DD8 means committing `scripts/snapshot_select.py` first, which is not possible by Thursday. |
| MQ2 | **How many details to check first?** The script's `--details-only` mode fetches only the TenderNed details and TED notices and reports which tenders have a TED notice. | Run it on the first 40 selected rows (at most 120 requests, about 4 min). That is enough to find 20 tenders before you start downloading by hand. Checking all 150 would also cover `NEXT.md` item 2 (deadline check), but takes about 10 min. |
| MQ3 | **ZIP files.** The TenderNed site often offers "download all" as a ZIP. | Unzip by hand in Finder into the tender's folder; the script reads subfolders. A `.zip` left in the folder is reported as `unsupported`. Safe archive unpacking (D4) waits until after 27 Nov. |
| MQ4 | **Dependencies** (section 2). | Approve the three packages. python-docx and openpyxl each pull in one more package. |
| MQ5 | **Doc changes.** Proposed as diffs, not written without approval: `EVALS.md` 3 gets the optional `tenderned_rectification.json` and the note that manual documents have `published_at = null`. `data/.gitignore` gets `snapshot.tmp/`, `snapshot.prev/` and `snapshot_report.json`. | Approve both. |

## 2. Dependencies to approve

The lock cutoff is `exclude-newer = 2026-10-30`, and every package below was uploaded before it, so each is more than two weeks old. Upload dates were read from PyPI's JSON API on 24 Nov 2026.

| Package | Version | Uploaded | Licence | For |
|---|---|---|---|---|
| pymupdf | 1.28.2 | 6 Aug 2026 | AGPL-3.0 or Artifex commercial (DD1) | PDF text per page, encryption and scan check, creating PDF test fixtures |
| python-docx | 1.2.0 | 16 Jun 2025 | MIT | DOCX paragraphs and tables |
| openpyxl | 3.1.5 | 28 Jun 2024 | MIT | XLSX sheets |
| lxml (transitive, python-docx) | 6.1.3 (newest that resolves) | 2 Sep 2026 | BSD-3-Clause | XML parsing for python-docx |
| et-xmlfile (transitive, openpyxl) | 2.0.0 | 25 Oct 2024 | MIT | Needed by openpyxl |

`typing-extensions` (also needed by python-docx) is already in `uv.lock` (4.16.0). The free PyMuPDF build cannot open DOCX or XLSX; that needs PyMuPDF Pro, a commercial product. So "PyMuPDF for PDF, DOCX and XLSX" means PyMuPDF for PDF and the two MIT libraries for DOCX and XLSX. Not needed now: python-pptx and LibreOffice. PPTX and DOC files are reported as `unsupported`.

## 3. Network calls the script makes

All requests are GETs, made anonymously over HTTPS with no redirects. There is a single global wait of **2 s** before every request, whatever the host. There are no retries: a 429, a 5xx or a network error stops the run. Everything fetched is cached, so a rerun repeats no request (section 5, M5).

| # | Request | When | Per tender |
|---|---|---|---|
| 1 | `https://www.tenderned.nl/papi/tenderned-rs-tns/v2/publicaties/{publication_id}` | every tender | 1 |
| 2 | the same endpoint with the ID in `publicatieIDLaatsteRectificatie` **(confirm in M0)** | when that field is set and differs from the ID | 0–1 |
| 3 | `https://ted.europa.eu/en/notice/{number}/xml`, with `{number}` from `pbNummerTed` **(confirm in M0)**, normalised to `642990-2026` | when the detail has a TED number | 0–1 |

The script never calls the document list, document downloads, `documenten/zip`, `public-xml` or the TED Search API.

Totals:

- `--details-only` on 40 rows: at most 120 requests, about 4 minutes.
- Full run on 20 tenders after the details run: 0 new requests, because everything is cached. On a cold cache it makes at most 60 requests, about 2 minutes.
- M0 probe: 3 to 5 requests.

A request budget (`--max-requests`, default 150) stops the script before more requests are made.

## 4. What the freeze script needs from D1–D3, and what waits

| Step | Built now (minimal) | Waits until after 27 Nov |
|---|---|---|
| D0 | Detail JSON for 3 publications (one with a rectification: 409819 or 408259), contact keys removed, saved as fixtures. This confirms `pbNummerTed`, `publicatieIDLaatsteRectificatie` and `sluitingsDatum`. | Document list, `documenten/zip`, list endpoint and paging, TED cancellation and no-award fixtures, download-ID order |
| D1 `ingest/http.py` | `HttpClient` with `get_json` and `get_bytes`, allowed hosts, https only, no redirects, a global 2 s delay, timeout, size cap, request budget, injectable transport, sleep and clock, and an `HttpError` holding only the status and path | Per-host delays, retries and backoff, `Retry-After`, the Basic-auth fallback, streaming `download()`, `post_json` |
| D2 `ingest/ted.py` | `fetch_notice_xml`, `strip_contacts` (`redact_contacts` and `LEFTOVER_MARKERS` moved from the fixture script; the script itself is left unchanged), `normalise_ted_number` | `search_query`, `search_notices`, `TedSearchHit`, `documents_url` (BT-15), making the fixture script import `strip_contacts`, cancellation fields |
| D3 `ingest/tenderned.py` | Field-name constants from M0, `TenderNedClient.detail`, `rectification_id`, `split_contacts`, `contact_names` (names only), `effective_sluitingsdatum` (DD2) | `list_publications`, matching and its cache, `documents()`, the `documents_access` rules (`other_platform`, `login_required`), `ContactRecord` and `purge_after` (DD9), `IngestedTender`, the ingestor, late matching, the DD11 refresh |
| D5 `ingest/extract_text.py` | PDF, DOCX and XLSX; `unsupported` for everything else | DOC through LibreOffice, PPTX |
| D7 `ingest/classify.py` | The DD5 rule table only (`classify_by_rule`); no match gives `other` with source `default` | The model fallback (DD6, `doc_classify_v1.md`) |
| D9 `scripts/freeze_snapshot.py` | `--from-manual` (required for now), `--details-only`, `--limit`, `--version`, `--force`, `--max-requests` | Document download from the API, `--raw-only`, archives, `--classify-model`, `--kb` |

D4, D6, D8 and D10 all wait.

- **D6 (language detection):** `language` is written as null. `load_tender` accepts that.
- **`kb/`:** there is no `kb/` folder in the repo yet, so the manifest has `kb_version = null`.

Adding language or the KB later means refreezing, which needs no network calls because of the cache.

## 5. Build steps

### M0. Probe: three TenderNed details (Arthur approves the requests) — Tue 24 Nov

- **Files:** new `scripts/fetch_tenderned_fixtures.py`, details only. It follows the pattern of `scripts/fetch_ted_fixtures.py`: standard library, 2 s wait, at most 6 requests, no retries, no redirects, anonymous.
- **Output:**
  - `tests/fixtures/tenderned/detail_<id>.json`, with every key that `agent.tools.contact_keys` flags removed, recursively;
  - `tests/fixtures/tenderned/manifest.json`, holding the URL, status, SHA-256 and removed key paths;
  - printed key names only, never values of contact keys.
- **Tests (`tests/test_tenderned_fixtures.py`):**
  - every fixture is in the manifest with a matching hash;
  - `contact_keys` finds nothing in any fixture;
  - no email-shaped or phone-shaped string (reuse `EMAIL_RE` from `tests/test_fixtures_privacy.py`).
- **Done when:** the three field names are confirmed and written as constants at the top of `ingest/tenderned.py`, and DECISIONS.md gets one line (also answers `NEXT.md` item 1 if a rectification shows). **If the detail has no TED number field, stop and ask:** the TED link would then need the TED search, and the plan changes.

### M1. HTTP client (D1 minimal) — Tue 24 Nov

- **Files:** `ingest/http.py`, `tests/test_http.py`.
- **API:**
  - `HttpClient(transport=None, sleep=time.sleep, clock=time.monotonic, delay=2.0, max_requests=150)`;
  - `get_json(url)` and `get_bytes(url, max_bytes=5 MB)`;
  - `requests` (property);
  - `HttpResponse` and `HttpError`;
  - `ALLOWED_HOSTS = {"www.tenderned.nl", "ted.europa.eu"}`.
- **Tests (fake transport):**
  - a disallowed host and an `http://` URL are refused without a request;
  - two requests 0.5 s apart (fake clock) cause a 1.5 s sleep;
  - a 302 raises `HttpError` and is not followed;
  - 404, 429 and 503 raise `HttpError` after a single request each, with no retry;
  - a body over the cap raises;
  - the request budget raises before the request is made;
  - `str(error)` contains only the status and the path.

### M2. TED notice fetch (D2 minimal) — Tue 24 Nov

- **Files:** `ingest/ted.py` (add), `tests/test_ted.py` (add).
- **Functions:** `fetch_notice_xml(client, number) -> bytes` (200 and XML content type, otherwise `HttpError`), `strip_contacts(xml) -> bytes`, `normalise_ted_number(value) -> str | None`.
- **`strip_contacts` rules:** it uses the regex and leftover check moved from the fixture script; leftover markers raise an error; the output must still parse. `parse_contacts` always runs on the original bytes, and only stripped bytes are ever written.
- **Tests:**
  - **Fixtures:** the saved `tests/fixtures/ted/*.xml` files and a synthetic contact block (`Kanariepiet`) inserted in the test.
  - **`strip_contacts`:**
    - removes the block;
    - `parse_notice(stripped) == parse_notice(original)`;
    - a block with attributes raises.
  - **`normalise_ted_number`:** a table of cases with the M0 form, `00642990-2026`, `2026/S 190-642990`, `None`, `"abc"` and `"642990-26"`.
  - **`fetch_notice_xml`:** against a fake transport serving `642990-2026.xml`; an HTML 200 raises.

### M3. TenderNed detail (D3 minimal) — Wed 25 Nov

- **Files:**
  - `ingest/tenderned.py` and `tests/test_tenderned.py`;
  - in `agent/tools.py`, `_is_contact_key` and `_is_person_key` become public, so the two modules share one set of word lists.
- **Functions:**
  - `TenderNedClient(http).detail(publication_id) -> dict`;
  - `rectification_id(detail) -> str | None`;
  - `split_contacts(detail) -> (clean, removed)`;
  - `contact_names(removed) -> list[str]`;
  - `effective_sluitingsdatum(detail, rectification) -> str | None`.
- **Tests (M0 fixtures plus synthetic JSON):**
  - **`split_contacts`:** a planted `contactpersoon: {naam, email, telefoon}` is removed and returned, and `opdrachtgeverNaam` is kept; nested lists are handled; `contact_keys(clean) == []`.
  - **`contact_names`:** returns only the name, never the email or phone.
  - **`effective_sluitingsdatum`:**
    - takes the rectification's value when it has one;
    - falls back to the original when the rectification has none;
    - returns `None` when neither has one.
  - **`rectification_id`:** `None` when the ID equals the publication's own ID.

### M4. Text extraction (D5: PDF, DOCX, XLSX) — Wed 25 Nov

- **Files:**
  - `ingest/extract_text.py`;
  - `ingest/documents.py`, holding only `file_format(name, head)` and `normalise_name(name)` (the D4 versions, so D4 extends this file later);
  - `scripts/make_document_fixtures.py` and its output in `tests/fixtures/documents/`: synthetic Dutch text, no real tender wording, generated once and committed;
  - `tests/test_extract_text.py` and `tests/test_documents_format.py`.
- **Rules:** as D5, but for PDF, DOCX and XLSX only:
  - **PDF:** `get_text("text", sort=True)` per page; `needs_pass` gives `encrypted`; the scanned rule is 20 characters per page and 80% of pages.
  - **DOCX:** sections split at the headings `Heading 1/2`, `Kop 1/2` and `Title/Titel`, tables written as rows joined with ` | `, sections cut at 6,000 characters.
  - **XLSX:** one page per sheet, hidden sheets labelled, the cell cap of 20,000 with the note `sheet_cut:<name>`.
  - **Every format:** `clean_text` removes NUL only. A broken file gives `failed`; the extractor never raises for a bad file.
- **Fixtures:**
  - **PDF:** `two_pages.pdf`, `scan.pdf`, `half_scan.pdf`, `encrypted.pdf`, `owner_only.pdf`, `ligature.pdf`, `truncated.pdf`;
  - **DOCX:** `sections.docx` (`Heading 1`, `Kop 2`, a table), `no_headings.docx`;
  - **XLSX:** `sheet.xlsx` (two sheets plus a hidden one, a formula without a cached value);
  - **other formats:** `old.doc` (OLE header bytes only), `slides.pptx` (a minimal ZIP with a `ppt/` folder).
- **Tests:**
  - **PDF:**
    - exact page texts in `two_pages.pdf`;
    - `scan.pdf` is `scanned` with empty pages kept;
    - `half_scan.pdf` gets the note `pages_without_text:1`;
    - `encrypted.pdf` gives `encrypted`, while `owner_only.pdf` gives `ok`;
    - in `ligature.pdf` the ligature is kept and `pipeline.citations.verify_citation` passes on the quote without the hyphen;
    - NUL characters are removed;
    - `truncated.pdf` gives `failed` and does not raise.
  - **DOCX:** section order, labels and table rows; a 15,000-character section splits into three.
  - **XLSX:** a 30,000-cell `big.xlsx`, built inside the test, is cut with the note.
  - **Other formats:** `.doc` and `.pptx` give `unsupported`.
  - **`file_format`:** a PDF named `.docx` gives `pdf`; a DOCX named `.zip` gives `docx`.

### M5. Classification rules (D7 table only) — Wed 25 Nov

- **Files:** `ingest/classify.py` and `tests/test_classify.py`.
- **Function:** `classify_by_rule(name, category, file_format) -> str | None`, using the DD5 table of `documents.md` D7. Manual files have no `publicatieCategorie`, so rule 1 never fires here and rule 2 (the name check for `aankondiging` and `EF\d\d`) keeps notice copies out.
- **Tests:**
  - one positive name for each rule;
  - the five order cases of D7;
  - case and spacing variants match the same rule;
  - a name that matches no rule gives `None`, which the script writes as `other` with source `default`.

### M6. Freeze script with `--from-manual` (D9 subset) — Thu 26 Nov

- **Files:**
  - `scripts/freeze_snapshot.py`;
  - `tests/test_freeze_snapshot.py`, which loads the script with `importlib` like `tests/test_fixtures_privacy.py`;
  - `evals/snapshot.py`: `SnapshotTender.tenderned_rectification` and `sluitingsdatum` through `effective_sluitingsdatum` (DD2);
  - `tests/test_evals_snapshot.py` (add);
  - `data/.gitignore` (MQ5).
- **Behaviour:**
  1. **Select tenders.** Read the `selected = true` rows of `data/snapshot_selection.csv`. With `--from-manual DIR`, the tenders are the selected rows that have a folder `DIR/<publication_id>/` holding at least one file; `--limit N` caps the number. A folder whose ID is not selected is reported and skipped.
  2. **Fetch and cache.** For each tender, fetch the detail and, if set, the rectification. Strip contact keys at once. Fetch the TED XML and run `parse_contacts` on the original. Write the cache to `data/raw/snapshot/<id>/`, with stripped files and names only, so no email or phone is ever written to disk:
     - `tenderned.json`;
     - `tenderned_rectification.json`;
     - `notice.xml`;
     - `contacts.json`.
     On a rerun, a cached file is never fetched again. If there is no TED number, or the XML is missing or gives a `TedParseError`, the tender is reported as `no_ted_notice` and skipped.
  3. **Stop early with `--details-only`.** The run stops here and prints, for each tender, its TED number, whether it has a rectification, and the effective closing date (MQ2).
  4. **Read the documents.** Walk `DIR/<id>/` recursively. Skip symbolic links, hidden files, `__MACOSX/`, `.DS_Store` and `Thumbs.db`. Read each file into memory and take its SHA-256; a second file with the same hash in the same tender is skipped and reported. Downloaded content is never imported or run, and no path is built from file content.
  5. **Build each document row:**
     - `file_format` is decided from the file's magic bytes;
     - `extract` is run on the file;
     - `doc_type` is decided by name, using the last part of the path;
     - `filename` is the path relative to the tender's folder;
     - `published_at` is null;
     - `is_current` is true (one file per name in a folder);
     - `language` is null;
     - `documents_access` is `tenderned` when the tender has files, otherwise `none`.
  6. **Write the snapshot to `<out>.tmp`**, in the layout of `EVALS.md` 3:
     - `notice.xml`, `tenderned.json`, `tenderned_rectification.json` when there is one, and `contacts.json` (`{"names": [...]}`);
     - `documents.json`, with `document_id` a snapshot-wide sequence in the order (publication ID, relative path);
     - `doc_key`, the first 12 hex characters of the SHA-256; a collision inside one tender stops the run;
     - `pages/<doc_key>.jsonl`, for documents with status `ok` only.
     Then build the manifest with `build_manifest(..., kb_version=None)`. `open_snapshot(<out>.tmp)` must succeed before the folder is renamed to `<out>`. An `<out>` that already has a manifest is refused unless `--force` is given; with `--force` the old snapshot is moved to `<out>.prev`, never deleted.
  7. **Write the report** to `data/snapshot_report.json`, which is printed too. It holds counts by `extraction_status`, `file_format`, `doc_type` and `doc_type_source`, plus `no_ted_notice`, `unsupported` (with the file extension), rectifications, duplicates, requests made, and failures by error type. It never holds document text or names.
- **Tests (fake transport over the M0 fixtures and the TED fixtures; manual folders in `tmp_path` filled with copies of the M4 fixtures):**
  - two tenders, one with a rectification and one without a TED number: the output opens with `open_snapshot`, the second is `no_ted_notice` and absent, and the effective closing date comes from the rectification;
  - `document_id` is unique across tenders and stable over two runs; `doc_key` has length 12; a forced collision stops the run;
  - a scanned PDF has no page file; `.zip`, `.doc` and `.pptx` are `unsupported`; duplicate bytes produce one row;
  - **privacy:**
    - `notice.xml` has no `cac:Contact`;
    - `contacts.json` holds only `names`;
    - no email-shaped or phone-shaped string anywhere in the output or the cache;
    - the planted canary appears only in `contacts.json`;
  - a rerun after a fake transport raises at request N makes only the missing requests, and a second full run makes 0;
  - `--details-only` writes nothing under `--out`;
  - an existing `--out` without `--force` is refused and left unchanged; with `--force` the old folder is in `.prev`;
  - the request budget is respected;
  - a symbolic link in the manual folder is skipped;
  - E4 style: `git check-ignore` passes for `data/snapshot.tmp/`, `data/snapshot.prev/`, `data/snapshot_report.json` and `data/raw/manual/`.

## 6. Schedule to Thursday

| Day | Work | Arthur |
|---|---|---|
| Tue 24 Nov | Answer MQ1–MQ5; M0 (after approval), M1, M2 | Approves the dependencies and the M0 requests |
| Wed 25 Nov | M3, M4, M5; M6 steps 1 to 3, then the `--details-only` run (after approval) | Downloads the 20 tenders' documents by hand once the list is known |
| Thu 26 Nov | The rest of M6; the full `--from-manual` run; the report is recorded in DECISIONS.md | Approves the run, checks the report |

Fallback if M0 shows no TED number in the detail: stop and ask (M0). If time runs out on Thursday, drop the DOCX section split and write one section per document (the tests for it move to after 27 Nov), and keep PDF and XLSX complete.

## 7. Verification

- After every step, `make test` passes and `make lint` is clean.
- Live runs need Arthur's approval each time:
  - `--details-only` on the MQ2 rows; the report shows a TED number for at least 20 of them;
  - the full `--from-manual` run.
- After the run:
  - `python -c "from evals.snapshot import open_snapshot; ..."` opens the snapshot and loads every tender with `load_tender`;
  - `make evals` (a dry run, no cost) reads the snapshot and counts the calls;
  - `git status` shows nothing under `data/`.
- No commit until Arthur asks.

## 8. What stays open after this plan

- Everything in the "waits" column of section 4. The automated fetcher is built after 27 Nov and before 11 Dec.
- DD8 in its strict form (MQ1), and committing `scripts/snapshot_select.py`.
- Q7 (tables) and D6 (language). Both need a refreeze, which needs no network calls.
