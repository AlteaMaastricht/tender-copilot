# Tender Copilot: Architecture

Status: proposal, v0.1. Companion to `docs/PLAN.md` and `docs/COMPANY.md`.
Items marked **(confirm)** were not checked against a live source and must be verified in the Phase 0 notebook.

## 1. Design principles

1. **Code decides what code can decide.** Hard rules (country, CPV, deadline, value range) are checked in Python on eForms fields and the TenderNed publication detail. Models only make judgements that need reading.
2. **Pipeline first, agent where it earns its place.** Stage 2 is a fixed sequence with typed outputs. The agent loop runs only for requirement matching and chat, where the model must choose its own searches.
3. **Recall over precision for knock-outs.** Requirements are found by reading the key documents in full, not by top-k retrieval. "Unclear" is a valid answer and goes to a human.
4. **Every claim has a checkable citation.** A quote plus document and page, verified in code against the stored page text.
5. **The model cannot write to the outside world.** The CRM write is application code triggered by an approval row. Tender text is untrusted input.
6. **One LLM gateway.** Every model call goes through one wrapper that redacts, logs, prices and retries.
7. **Everything is re-runnable.** Each step is idempotent and keyed on tender status, so the daily job and the evals can be re-run safely.

## 2. Components and data flow

```
                   ┌────────────── daily job (cron, 06:00 CET) ──────────────┐
TED Search API ──► 1 ingest notices ──► 2 hard filter ──► 3 screen (small model)
                     (eForms + TenderNed) (code)             │ shortlist / unsure
                                                             ▼
TenderNed ───────────────────────────► 4 fetch + parse documents ──► 5 chunk + embed
                                                             │
                                                             ▼
Company KB ──(embedded once)──►  6 extract ──► 7 match (agent loop) ──► 8 memo + citation check
                                                             │
                                                             ▼
                                          Postgres + pgvector (single source of truth)
                                                             │
                        ┌────────────────────────────────────┴───────────────┐
                        ▼                                                    ▼
          Streamlit UI: inbox, memo, chat, approve            evals/run.py on frozen snapshot
                        │ approval row
                        ▼
          crm/hubspot.py: create deal (application code, idempotent)
```

### 2.1 Modules

| Module | Responsibility |
|---|---|
| `ingest/ted.py` | Query the TED Search API for NL notices by CPV prefix and publication date; fetch notice XML; map eForms fields to `tenders`, `lots`, `contacts`. The deadline is not taken from the notice. |
| `ingest/tenderned.py` | At ingest: walk the public publication list for the same date window, fetch each publication detail (`/publicaties/{id}`), link it to the TED notice on `pbNummerTed`, and supply `sluitingsDatum`. For shortlisted tenders: list documents (`/publicaties/{id}/documenten`) and download each through its `links.download.href`. Sets `documents_access` when the tender is hosted elsewhere or needs a login. |
| `ingest/documents.py` | Unpack ZIP archives recursively, hash, apply the name rules, extract text per page from PDF, DOCX, PPTX, XLSX and legacy DOC, detect scanned files and language, track versions. See 2.10. |
| `rag/chunk.py`, `rag/embed.py` | Section-based chunking with a fixed-size fallback; embedding; writes `chunks`. |
| `rag/search.py` | Hybrid search (vector + full-text, reciprocal rank fusion) over `chunks` and `company_kb`. |
| `llm/` | The gateway package, entered only through `llm/client.py` (`Gateway.call`, `Gateway.step`). `redact.py` redaction; `prompts.py` prompt files and wrapping of untrusted text; `prices.py` price table; `log.py` call log and payload store; `providers/` adapters (Anthropic, Mistral; OpenAI later), the only modules that import a provider SDK. Schema validation with one retry; network retries with backoff; cost and latency logged in the `llm_calls` shape. |
| `pipeline/filter.py` | Hard rules from `COMPANY.md` sections 4 and 5. |
| `pipeline/screen.py` | Stage 1 screening call. |
| `pipeline/extract.py` | Stage 2 extraction (map over sections, then merge). |
| `pipeline/match.py` | Requirement matching; one agent loop per requirement. |
| `pipeline/schemas.py` | The Pydantic models of section 5, shared by every step and passed to the gateway for validation. |
| `pipeline/prompt_specs.py` | Which prompt file, stage, schema, trusted variables and company sections each step uses; reads numbered sections from `docs/COMPANY.md`. `prompt_refs` gives the prompt references of one run, with a version replaced on request (used by the evals). |
| `pipeline/citations.py` | Citation verifier: is the quote on the cited page? Pure code, used by the memo step and by chat. |
| `pipeline/memo.py` | Memo generation, fit score, recommendation rules. |
| `agent/loop.py`, `agent/tools.py` | Hand-written loop (max steps, tool errors returned to the model, token budget); tool definitions, argument checks and the contact-data guard. `agent/fakes.py` is a fake tool backend until the database exists. |
| `jobs/daily.py` | Orchestrates steps 1 to 8; takes a Postgres advisory lock so two runs cannot overlap. |
| `app/` | Streamlit: inbox, memo view, chat per tender, approve/reject. |
| `crm/hubspot.py` | Creates the deal from an approved memo. |
| `evals/` | Datasets (JSONL, in git), runners, results. `run.py` CLI; `snapshot.py` frozen snapshot and its hash; `labels.py` read-only label loaders; `metrics.py` formulas; `suites.py` the six evals; `targets.py` the pipeline steps they call; `budget.py` cost cap and dry run; `report.py` cost, latency and the results file. See `docs/EVALS.md`. |
| `prompts/` | Versioned prompt files: `screen_v1`, `extract_map_v1`, `extract_reduce_v1`, `match_v1`, `memo_v1`, `memo_reason_v1`, `chat_v1`, and the gateway blocks. |

### 2.2 Daily job

| Step | Input | Output | Tender status after |
|---|---|---|---|
| 1 Ingest | TED search, last 2 days (overlap on purpose); TenderNed publication list and details for the same window | upsert `tenders` (with `tenderned_id`, `deadline_at`), `lots`, `contacts` | `new` |
| 2 Hard filter | eForms fields, `sluitingsDatum` | `filter_result` JSON on the tender | `filtered_out` or stays `new` |
| 3 Screen | title, description, CPV, buyer type, value, lots | `assessments` (stage `screening`) | `screened_out` or `shortlisted` |
| 4 Fetch documents | TenderNed | `documents`, `document_pages` | `docs_ready` or `docs_unavailable` |
| 5 Chunk and embed | pages | `chunks` | `docs_ready` |
| 6 Extract | key documents, in full | `assessments` (stage `extraction`) | `extracted` |
| 7 Match | requirements + KB | `assessments` (stage `matching`) | `matched` |
| 8 Memo | the three assessments | `memos` | `memo_ready` |

Rules:

- **Upsert key:** the procedure identifier (BT-04), not the notice. A change or cancellation notice updates the same tender row and bumps `notice_version`. A changed `sluitingsDatum` or a new document version resets the status to `shortlisted` so the tender is re-analysed.
- **TenderNed link:** the join key is `pbNummerTed` on the publication detail **(confirm format against the TED publication number)**. With no match, `tenderned_id` and `deadline_at` stay null, the deadline rule is `unknown`, and `documents_access` is `none`. The notice deadline is never used as a fallback.
- **`docs_unavailable`:** the tender still appears in the inbox with a notice-only memo marked "documents not accessible", and a link to the buyer's platform.
- **Failure:** any step failure sets `status = 'failed'` with `error` and the failed step; the next run retries from that step, three attempts at most.
- **"Unsure" screening results are shortlisted.** Stage 1 is tuned for recall.
- **Budget cap:** the job stops Stage 2 when the day's `llm_calls.cost_usd` passes a configured limit.

### 2.3 Hard filter (code, no model)

Applied to eForms fields and the TenderNed publication detail; each rule records pass, fail or unknown. A tender is dropped only on a definite fail. Unknown is not a pass: the tender continues to screening, is flagged in the inbox, and the memo lists it under `limitations`.

| Rule | Source | From COMPANY.md |
|---|---|---|
| Country is NL | buyer country (first buyer); a lot's place of performance outside NL is the flag `place_outside_nl`, not a fail | section 4, 5; decision S3 |
| Main or additional CPV starts with 72, 79, 48, 73, at procedure or lot level | BT-262, BT-263 | section 4 (CPV 73 adds the flag `cpv_73_check_scope`) |
| Notice is a contract notice (`cn-standard`, `cn-social`); award, prior information, VEAT and market-consultation notices fail | notice type | n/a |
| Deadline is in the future and at least 10 working days (Monday to Friday) after publication | `sluitingsDatum` from TenderNed `/publicaties/{id}`, TED publication date | section 5 |
| Closing date is a real date. `2099-12-31`, a date more than five years after publication, or a missing date gives `unknown` | `sluitingsDatum`, publication date | n/a (`DECISIONS.md`, data findings) |
| Estimated value is between EUR 50k and EUR 2M, checked per lot: lot values first, the procedure value only when no lot has one. Passes if any lot is in range; above 2M only gives pass with the flag `consortium_only`; no value gives `unknown` | BT-27 (lot, then procedure) | section 4; Q11 |
| Main nature: `supplies` or `works` gives the flag `nature_supplies`, not a fail (software packages are often classed as supplies) | BT-23 | section 5 (hardware supply) |
| Security clearance not required: **deferred** until the parser reads BT-578 | BT-578 | section 5 |

The outcome is recorded in `filter_result`, for example `{"deadline": {"outcome": "unknown", "reason": "placeholder_date", "raw": "2099-12-31"}}`. Reasons: `placeholder_date`, `missing`, `no_tenderned_match` (in these three cases `deadline_at` is null), and `closed`, `too_short` and `below_50k` for definite fails.

### 2.4 Screening (Stage 1)

One call per tender with a small model. Input: redacted notice title, description, lot titles, CPV labels, buyer type, value, the flags set by the hard filter, and sections 2, 4 and 5 of the company profile. Output: `ScreeningOutput` (section 5.1). No tools, no documents.

### 2.5 Deep analysis (Stage 2)

**Extract.**
- Documents are classified by type, by rule first and by model only when no rule matches (2.10). The current version of each `guideline` and `requirements` document is read in full. Every version of a `qa` document is read, oldest first by `datumPublicatie`, because same-named answer files can be separate answers. `notice` documents are never read. Annexes, forms and price sheets are indexed for search but not read.
- Map: each section of the key documents is sent with the extraction prompt and returns partial results with citations. Reduce: one call merges and de-duplicates into `ExtractionOutput`.
- The Nota van Inlichtingen documents are processed last, in date order, and may override earlier values (a moved deadline, a relaxed requirement); a later answer overrides an earlier one. Overrides are recorded.
- If the key documents exceed the token budget, the tender is marked `needs_review` instead of being truncated silently.

**Match.**
- For each knock-out criterion, one short agent loop (max 6 steps) with the tools in section 4. It ends with a `RequirementMatch`.
- Formal knock-outs (kind `formal`: sign a form, use a format, submit on time) are actions in the bid, not matched. They appear in the memo's knock-out table with status `action_required` and are ignored by the recommendation rules.
- Numeric and date requirements (turnover, insurance cover, certificate validity, number of references) use `get_company_facts`, not vector search.
- Award criteria are not matched as met or not met. They get a short strength note for the memo.

**Memo.**
- Two model calls: the draft, then the reason for the decided value (section 5.4). Before the draft call, code fetches KB passages (profile, references, certifications, policy; no CVs) and company facts and passes them as data.
- Fit score: the model scores four of the seven dimensions from 1 to 5, each with a rationale and a source. The bid team scores the other three in the UI. Code computes the total and the range it can still reach (section 5.4).
- Recommendation: decided in code from the knock-out results and the fit score (section 5.4). The model writes only the one-sentence reason.
- Citation check (`pipeline/citations.py`): the quote must be one contiguous piece of the text of the cited page. Neighbouring pages are not searched, so a quote that runs over a page break fails; the evals count these as false negatives.
  - Normalised on both sides: Unicode NFKC (ligatures, non-breaking spaces, composed accents), invisible characters, hyphens at line breaks, whitespace, quote marks and apostrophes, dashes, case.
  - Not normalised: accents, punctuation, number formats. There is no fuzzy matching.
  - Always fails: a quote with fewer than 10 letters or digits, a quote shortened in the middle, an empty page.
  - The result is pass or fail with a reason code. Unverified citations are marked in the memo and do not count as a source.

### 2.6 Chat

Per tender. The same agent loop with `get_tender`, `search_tender_docs`, `read_document_pages` and `search_company_kb`. Answers carry citations. Questions outside the tender are declined. Chat has no write tools.

### 2.7 Approval and CRM

1. The user clicks Approve or Reject on a memo; the UI writes an `approvals` row.
2. On approve, `crm/hubspot.py` builds the deal from database fields (title, buyer, deadline, value, fit score, memo link) and creates it. No model output is passed through unchecked, and no contact data is sent.
3. `approvals.crm_status` records `created` or `failed`; a retry button reuses the same row, so one memo can never create two deals.

### 2.8 Privacy

- Contact data lives only in `contacts`. No query used to build model input joins that table.
- The gateway redacts emails, phone numbers and the contact names from `contacts` before every call, in the system prompt, the data, user turns and tool results. Application code passes the names to a `Redactor`; they are used only for local matching. Matched name forms: full name, initial plus surname, surname after a title.
- This is best effort, and the README must say so. Not caught: names that are not in `contacts`, a bare surname without a title, misspelt or OCR-damaged names, obfuscated email addresses, unusual phone formats, and other personal data such as postal addresses. A ten-digit number starting with 0 can be replaced as a phone number by mistake.
- Tender text, notice text and tool results are passed to the gateway as data. It wraps them in tags named per run and never places them in the system prompt.
- A quote copied from redacted text can contain `[EMAIL]`, `[PHONE]` or `[NAME]`. The caller of the citation verifier must redact the stored page text with the same `Redactor` before the check.
- Stored payloads are redacted, off by default (`LLM_PAYLOAD_DIR`), and purged after 30 days.
- Provider keys (`APP_ANTHROPIC_API_KEY`, `MISTRAL_API_KEY`) come from the process environment first, then from `.env`, which `llm/env.py` reads only for the requested variable. Nothing is copied into the environment, printed or logged.
- `contacts` rows carry `purge_after` (deadline plus a retention period).

### 2.9 Deployment

One small EU VM, docker compose with three services: `db` (Postgres 16 + pgvector), `app` (Streamlit behind Caddy for HTTPS and basic auth), `worker` (the daily job, started by host cron). Secrets in `.env` on the VM. Nightly `pg_dump` to disk. Raw documents on a mounted volume, not in the repo. The `worker` image includes LibreOffice (headless) for legacy DOC conversion.

### 2.10 Document handling

**Formats**

| Format | Extraction | `page_no` means | Notes |
|---|---|---|---|
| PDF | Text layer per page | page | No text layer: `scanned` |
| DOCX | Paragraphs and tables | section index | |
| DOC (legacy) | Converted to DOCX with LibreOffice headless, then as DOCX | section index | Conversion failure or timeout: `extraction_status = unsupported`. The original file is kept. |
| PPTX | Text per slide (titles, text boxes, tables) | slide number | |
| XLSX | Each sheet read as a table | sheet index, with the sheet name in `page_label` | Rows and columns are preserved, `has_table = true`. Chunked in row blocks with the header row repeated. Very large sheets are cut at a cell limit and a warning is recorded. |
| ZIP | Unpacked recursively | none | Each contained file is its own `documents` row with `parent_document_id` and `archive_path`. The archive row has `extraction_status = archive`. Limits on depth (5), file count and unpacked size; member paths are sanitised, because archives are untrusted input. |
| Anything else | None | none | `unsupported` |

`Citation.page` holds `page_no` whatever the kind; `documents.page_kind` tells the UI to print "page", "slide", "sheet" or "section". New libraries (`python-pptx`, `openpyxl`) and the LibreOffice binary need approval before they are added.

**Versions**

- Every file in TenderNed's document list is stored. A file is skipped only when its SHA-256 already exists for that tender. The name is never used to de-duplicate.
- `version_group` is the normalised `documentNaam` (lower case, whitespace collapsed).
- Within one tender and `version_group`, the file with the newest `datumPublicatie` has `is_current = true`. `datumPublicatie` is a date without a time, so ties are broken by the higher numeric download ID **(confirm that it rises over time)**.
- Files inside an archive take the archive's `datumPublicatie`.
- `search_tender_docs` searches current versions plus all `qa` versions. Superseded versions stay in the database for audit.

**Classification by rule, before any model call**

Matching is case-insensitive on the normalised `documentNaam`. Rules are tried in this order and the first match wins.

| Order | Condition | `doc_type` |
|---|---|---|
| 1 | `publicatieCategorie.code` is `ANK` (a copy of the notice, such as the EF16 PDFs) | `notice` |
| 2 | Name contains "Nota van inlichtingen" or "Toelichting beantwoording" | `qa` |
| 3 | Name contains "Programma van eisen" or "Bijlage 1 - Programma" | `requirements` |
| 4 | Name contains "Inschrijvingsleidraad" or "Selectieleidraad" | `guideline` |

- A rule match sets `doc_type_source = 'rule'`.
- Only documents that match no rule go to the model (stage `doc_classify`, through the gateway, with the file name and the redacted first page). Those get `doc_type_source = 'model'`.
- `notice` documents are stored but never chunked, read or searched, so the extraction eval is not circular (section 6).
- Archive rows are not classified.

## 3. Postgres schema

Conventions: `id bigserial primary key`, `created_at timestamptz default now()` on every table; omitted below. Embedding dimension is 1024 (BGE-M3 and multilingual-e5-large both fit).

### tenders
One row per procurement procedure (latest notice version).

| Column | Type | Notes |
|---|---|---|
| procedure_id | text, unique | eForms BT-04 |
| notice_id | text, unique | TED publication number of the latest version |
| notice_version | int | bumped on change notices |
| superseded_notice_ids | text[] | earlier publication numbers |
| notice_type | text | contract notice, change, cancellation |
| title, description | text | redacted copies used for model input |
| buyer_name, buyer_type | text | organisation, not a person |
| country | char(2) | |
| cpv_main | text | |
| cpv_codes | text[] | main plus additional |
| main_nature | text | services, supplies, works |
| procedure_type | text | |
| is_framework, is_dps | bool | |
| estimated_value | numeric(14,2), null | often missing |
| currency | char(3) | |
| published_at | timestamptz | |
| deadline_at | timestamptz, null | TenderNed `sluitingsDatum` from `/publicaties/{id}`, read as Europe/Amsterdam time **(confirm)**. Null when missing, unmatched or a placeholder (2.3). Never taken from the notice. |
| documents_url | text | BT-15 |
| documents_access | text | `tenderned`, `other_platform`, `login_required`, `none` |
| tenderned_id | text, null | `publicatieId`, matched at ingest on `pbNummerTed` |
| tenderned_detail | jsonb, null | raw publication detail, the source of `deadline_at` |
| is_cancelled | bool | |
| status | text | see 2.2 |
| failed_step, error | text, null | |
| attempts | int | |
| filter_result | jsonb | per-rule outcome of the hard filter |
| eforms_gt | jsonb | held-out ground truth (section 6); never shown to a model. Also keeps the notice deadline (BT-131, BT-1311), for comparison only |
| raw_notice | jsonb | API response; XML stored on disk |
| in_snapshot | bool | frozen eval set |
| updated_at | timestamptz | |

Indexes: unique `(procedure_id)`, unique `(notice_id)`, `(status)`, `(deadline_at)`, `(published_at desc)`, GIN `(cpv_codes)`, partial `(in_snapshot) where in_snapshot`.

### lots

| Column | Type | Notes |
|---|---|---|
| tender_id | fk tenders, on delete cascade | |
| lot_identifier | text | e.g. `LOT-0001` |
| title, description | text | |
| cpv_codes | text[] | |
| estimated_value | numeric(14,2), null | |
| duration_months | int, null | |
| award_criteria_gt | jsonb | from eForms: type, name, description, weight |
| selection_criteria_gt | jsonb | from eForms: type, name, description, threshold |

Indexes: unique `(tender_id, lot_identifier)`.

### contacts
Personal data. Never joined into model input.

| Column | Type | Notes |
|---|---|---|
| tender_id | fk tenders, on delete cascade | |
| org_name | text | |
| contact_name, email, phone | text, null | |
| role | text | buyer, review body, etc. |
| purge_after | date | retention |

Indexes: `(tender_id)`, `(purge_after)`.

### documents

| Column | Type | Notes |
|---|---|---|
| tender_id | fk tenders, on delete cascade | |
| tenderned_document_id | text, null | `documentId` (32 hex characters; not the download ID) |
| parent_document_id | fk documents, null | the archive this file came from |
| archive_path | text, null | path inside the archive |
| source_url | text | |
| filename | text | |
| sha256 | char(64) | the only de-duplication key |
| mime_type | text | |
| file_format | text | `pdf`, `docx`, `doc`, `pptx`, `xlsx`, `zip`, `other` |
| page_kind | text, null | `page`, `slide`, `sheet`, `section` |
| publication_category | text, null | `publicatieCategorie.code` |
| doc_type | text | `guideline`, `requirements`, `qa` (Nota van Inlichtingen), `notice`, `contract`, `pricing`, `form`, `annex`, `other` |
| doc_type_source | text, null | `rule` or `model`; null for archives |
| version_group | text | normalised document name (2.10) |
| is_current | bool | newest `published_at` in its version group |
| language | text | `nl`, `en`, `other` |
| page_count, text_chars | int | |
| extraction_status | text | `ok`, `scanned`, `unsupported` (includes a failed DOC conversion), `encrypted`, `failed`, `archive` |
| storage_path | text | on the volume |
| published_at | date | from `datumPublicatie` |
| fetched_at | timestamptz | |

Indexes: unique `(tender_id, sha256)`, `(tender_id, doc_type)`, `(parent_document_id)`, `(tender_id, version_group, published_at desc)`, unique `(tender_id, version_group) where is_current`.

### document_pages (addition to the requested list)
Needed for full-document reading and citation verification.

| Column | Type | Notes |
|---|---|---|
| document_id | fk documents, on delete cascade | |
| page_no | int | 1-based; a page, slide, sheet or section, per `documents.page_kind` |
| page_label | text, null | sheet name or slide title |
| text | text | |
| has_table | bool | always true for sheets |

Indexes: unique `(document_id, page_no)`.

### chunks

| Column | Type | Notes |
|---|---|---|
| document_id | fk documents, on delete cascade | |
| tender_id | fk tenders | denormalised for filtering |
| chunk_index | int | |
| section_path | text | e.g. `4 > 4.2 Gunningscriteria` |
| page_start, page_end | int | |
| language | text | |
| content | text | |
| token_count | int | |
| chunk_method | text | `heading` or `fixed` |
| embedding | vector(1024) | |
| embedding_model | text | |
| fts | tsvector | written at ingest with the `dutch` or `english` config by language |

Indexes: unique `(document_id, chunk_index)`, btree `(tender_id)`, GIN `(fts)`.

No HNSW index in v1. Search is always filtered to one tender (a few hundred chunks), where an exact scan is fast and exact, and an approximate index combined with a filter can return fewer than k rows. Add `hnsw (embedding vector_cosine_ops)` only if cross-tender search is built.

### company_kb
Chunks of the company knowledge base, plus structured facts.

| Column | Type | Notes |
|---|---|---|
| doc_slug | text | e.g. `reference-rivierwaarde` |
| doc_type | text | `profile`, `certification`, `reference`, `cv`, `policy`, `insurance`, `financial` |
| title | text | |
| language | text | |
| chunk_index | int | |
| content | text | |
| facts | jsonb, null | typed facts: turnover per year, cover amounts, `valid_until`, reference value and year |
| embedding | vector(1024) | |
| fts | tsvector | |
| kb_version | text | hash of the KB folder |

Indexes: unique `(doc_slug, chunk_index, kb_version)`, GIN `(fts)`, `(doc_type)`.

### assessments
One row per pipeline step output. The JSON is the Pydantic model from section 5.

| Column | Type | Notes |
|---|---|---|
| tender_id | fk tenders, on delete cascade | |
| stage | text | `screening`, `extraction`, `matching` |
| output | jsonb | validated |
| schema_version | text | |
| prompt_version | text | |
| model | text | |
| decision | text, null | screening only: `shortlist`, `reject`, `unsure` |
| confidence | real, null | screening only |
| needs_review | bool | |
| is_current | bool | false for superseded runs |
| run_id | uuid | one daily job or eval run |

Indexes: unique `(tender_id, stage) where is_current`, `(run_id)`, `(stage, decision)`.

### memos

| Column | Type | Notes |
|---|---|---|
| tender_id | fk tenders, on delete cascade | |
| version | int | |
| content | jsonb | `Memo` model |
| rendered_md | text | |
| recommendation | text | `bid`, `bid_conditional`, `no_bid`, `needs_review` |
| fit_score | int, null | 0 to 100; null until all seven dimensions are scored |
| fit_score_min, fit_score_max | int | the range the total can still reach (section 5.4) |
| citations_total, citations_verified | int | |
| extraction_id, matching_id | fk assessments | inputs used |
| prompt_version, model | text | |
| is_current | bool | |

Indexes: unique `(tender_id, version)`, `(is_current, fit_score desc)` for the inbox. With `fit_score` nullable, the inbox order needs a decision (for example by `fit_score_max`); open until the UI is built (`docs/DECISIONS.md`).

### memo_human_inputs (proposed, built with the database on Day 7)
The `[human]` items of the memo template. Keyed on the tender, so the inputs survive a new memo version. Never sent to a model: owner names are personal data of staff.

| Column | Type | Notes |
|---|---|---|
| tender_id | fk tenders, unique, on delete cascade | |
| incumbent | text, null | zittende partij, if known |
| fit_win_probability, fit_commercial, fit_strategic_value | smallint, null | 1 to 5; null until scored |
| fit_rationale | jsonb | rationale per human dimension |
| milestones | jsonb | internal go/no-go, red team, sign-off, with dates and owners |
| row_owners | jsonb | owner per knock-out, award and risk row |
| risk_overrides | jsonb | human changes to likelihood, impact and mitigation |
| deadline_realistic | bool, null | |
| price_to_win | text, null | |
| bid_investment | text, null | hours or cost, team |
| updated_by | text | UI username |
| updated_at | timestamptz | |

A change to a human fit score triggers a new fit score and recommendation.

### approvals

| Column | Type | Notes |
|---|---|---|
| memo_id | fk memos | |
| decision | text | `approved`, `rejected` |
| decided_by | text | UI username |
| comment | text, null | |
| decided_at | timestamptz | |
| crm_status | text | `not_applicable`, `pending`, `created`, `failed` |
| crm_deal_id | text, null | |
| crm_error | text, null | |

Indexes: unique `(memo_id)`, unique `(crm_deal_id) where crm_deal_id is not null`.

### llm_calls

| Column | Type | Notes |
|---|---|---|
| run_id | uuid | |
| eval_run_id | fk eval_runs, null | |
| tender_id | fk tenders, null | |
| stage | text | `screening`, `extraction_map`, `extraction_reduce`, `matching`, `memo`, `chat`, `judge`, `doc_classify` |
| provider, model | text | |
| prompt_name, prompt_version | text | |
| step_index | int | position in an agent loop |
| input_tokens, output_tokens, cached_tokens | int | `input_tokens` counts all prompt tokens; `cached_tokens` is the part read from a cache. Summed over a validation retry. |
| cost_usd | numeric(10,6) | from a price table in config |
| latency_ms | int | |
| tool_calls | jsonb | names and arguments |
| stop_reason | text | |
| validation_retries | int | 0 or 1 |
| network_retries | int | retries after 408, 409, 429, 5xx, connection errors and timeouts; at most 3 |
| status | text | `ok`, `validation_failed`, `refused`, `truncated`, `provider_error` |
| error | text, null | |
| payload_path | text, null | redacted request and response on disk, short retention |

Indexes: `(tender_id)`, `(run_id)`, `(eval_run_id)`, `(created_at)`, `(stage, model)`.

### eval_runs

| Column | Type | Notes |
|---|---|---|
| suite | text | `screening`, `retrieval`, `extraction`, `knockout`, `memo`, `edge_cases` |
| git_commit | text | |
| snapshot_version | text | |
| config | jsonb | models, prompt versions, retrieval settings |
| metrics | jsonb | headline numbers |
| results_path | text | per-case JSON in `evals/results/` |
| cost_usd | numeric(10,4) | |
| started_at, finished_at | timestamptz | |

Indexes: `(suite, started_at desc)`, `(git_commit)`.

Eval labels are not in the database. They live in `evals/datasets/*.jsonl` in git, so that they are reviewable and versioned.

## 4. Agent tools

All tools are read-only. Tool errors are returned to the model as a result with `is_error`, not raised. Results never include `contacts` data.

| Tool | Used in | When the model should call it |
|---|---|---|
| `get_tender` | chat, matching | Needs structured facts: deadline, value, lots, procedure |
| `search_tender_docs` | chat, matching | Needs to find where the documents say something |
| `read_document_pages` | chat, matching | A search hit is cut off, or a table or clause runs over pages |
| `search_company_kb` | matching, chat | Needs evidence of experience, references, staff skills |
| `get_company_facts` | matching | The requirement is a number, a date or a certificate |

Removed from the PLAN.md list:

- `propose_crm_deal`: no longer a model tool. The deal is created by application code from the approval row (2.7).
- `search_tenders`: not needed for per-tender chat. Add it only if a cross-tender chat is built.

```json
{
  "name": "get_tender",
  "description": "Return the structured notice data for one tender: title, buyer organisation, procedure type, deadline, estimated value, CPV codes and lots. Use this for facts published in the notice. It does not return document text or contact persons.",
  "input_schema": {
    "type": "object",
    "properties": {
      "tender_id": {"type": "integer"}
    },
    "required": ["tender_id"],
    "additionalProperties": false
  }
}
```

```json
{
  "name": "search_tender_docs",
  "description": "Hybrid search (meaning plus exact words) over the documents of one tender. Returns passages with chunk_id, document_id, document type, section heading and page range. Write the query in the language of the documents, usually Dutch. Use exact terms for clause numbers, e.g. 'artikel 4.2'.",
  "input_schema": {
    "type": "object",
    "properties": {
      "tender_id": {"type": "integer"},
      "query": {"type": "string", "minLength": 2},
      "doc_types": {
        "type": "array",
        "items": {"enum": ["guideline", "requirements", "qa", "contract", "pricing", "form", "annex", "other"]},
        "description": "Optional filter. Omit to search all documents."
      },
      "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}
    },
    "required": ["tender_id", "query"],
    "additionalProperties": false
  }
}
```

```json
{
  "name": "read_document_pages",
  "description": "Return the full text of consecutive pages of one tender document. Use after search_tender_docs when a passage is cut off or a table continues on the next page. At most 5 pages per call.",
  "input_schema": {
    "type": "object",
    "properties": {
      "document_id": {"type": "integer"},
      "page_from": {"type": "integer", "minimum": 1},
      "page_to": {"type": "integer", "minimum": 1}
    },
    "required": ["document_id", "page_from", "page_to"],
    "additionalProperties": false
  }
}
```

```json
{
  "name": "search_company_kb",
  "description": "Hybrid search over the company's own knowledge base: profile, reference projects, CVs, certifications and bid policy. Returns passages with kb_id and doc_slug. Use to find evidence that the company meets a requirement.",
  "input_schema": {
    "type": "object",
    "properties": {
      "query": {"type": "string", "minLength": 2},
      "doc_types": {
        "type": "array",
        "items": {"enum": ["profile", "certification", "reference", "cv", "policy", "insurance", "financial"]}
      },
      "top_k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}
    },
    "required": ["query"],
    "additionalProperties": false
  }
}
```

```json
{
  "name": "get_company_facts",
  "description": "Return exact structured facts about the company. Use this instead of search whenever a requirement states a number, a date or a named certificate: minimum turnover, insurance cover, certificate validity, headcount, or the number, value and year of reference projects. Each fact comes with the kb_id, doc_slug and text of the knowledge-base passage it was taken from; cite that passage as evidence.",
  "input_schema": {
    "type": "object",
    "properties": {
      "topics": {
        "type": "array",
        "minItems": 1,
        "items": {"enum": ["turnover", "insurance", "certifications", "headcount", "references", "legal_status", "languages"]}
      }
    },
    "required": ["topics"],
    "additionalProperties": false
  }
}
```

Loop limits (`agent/loop.py`). The numbers are tunable constants, to be adjusted from the Phase 3 measurements.

| Limit | Matching | Chat | Constant |
|---|---|---|---|
| Steps (model calls) | 6 per requirement | 8 per question | `MATCH_MAX_STEPS`, `CHAT_MAX_STEPS` |
| Token budget (prompt plus output tokens, summed over the steps of one run) | 60,000 | 80,000 | `MATCH_TOKEN_BUDGET`, `CHAT_TOKEN_BUDGET` |
| Tool errors per run | 3 | 3 | `MAX_TOOL_ERRORS` |

- When a limit is hit the loop returns no answer. It never forces one and never guesses. For matching, `pipeline/match.py` then builds an `unclear` match in code (section 5.3), which makes the tender `needs_review`.
- The budget is checked after each step, so a run can pass it by one step. A final answer that arrives over budget is kept.
- Unknown tools, invalid arguments and tool exceptions are returned to the model as error results. The third error in a run stops it.
- A tool call can only read the run's own tender; another `tender_id` or another tender's document is refused.
- A result larger than 20,000 characters is an error, not a truncation.
- A result with a key that looks like contact data (`contact`, `email`, `phone`) is withheld.
- The loop does not read tool results. The gateway wraps them as untrusted data, so text in a result cannot change the tools, the prompt or the limits.

## 5. Pydantic schemas

These are the contracts between steps and the shape of `assessments.output` and `memos.content`. They live in `pipeline/schemas.py`; the code blocks below are that file. No schema holds contact data.

Shared types first.

```python
from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Self, get_args
from pydantic import BaseModel, Field, model_validator

class Citation(BaseModel):
    """A checkable pointer into a tender document."""

    document_id: int
    page: int = Field(ge=1)
    quote: str = Field(min_length=10, max_length=600, description="Verbatim text from that page")
    verified: bool | None = None  # set by code after the citation check, never by the model


class KbEvidence(BaseModel):
    """A pointer into the company knowledge base. Not yet verified in code."""

    kb_id: int
    doc_slug: str
    quote: str = Field(max_length=600)


class KeyDateTime(BaseModel):
    value: datetime
    citation: Citation


class KeyDate(BaseModel):
    value: date
    citation: Citation


class CitedText(BaseModel):
    text: str = Field(max_length=300)
    citation: Citation


Level = Literal["high", "medium", "low"]
```

- `Citation.verified` is always overwritten by the citation check (`pipeline/citations.py`). A value set by a model has no effect.
- `KbEvidence` quotes are not verified yet. They will be checked against `company_kb.content` later.

### 5.1 Screening output

```python
ExclusionFlag = Literal[
    "hardware_supply",
    "staffing_only",
    "security_clearance",
    "onsite_abroad",
    "ops_24_7",
    "pure_custom_software",
    "liability",
    "none",
]
ServiceLine = Literal[
    "data_platform",
    "ai_ml",
    "bi_dashboards",
    "cloud_migration",
    "data_governance",
]


class ScreeningOutput(BaseModel):
    decision: Literal["shortlist", "reject", "unsure"]
    reason: str = Field(max_length=400, description="One or two sentences citing the notice")
    matched_services: list[ServiceLine]
    exclusion_flags: list[ExclusionFlag]
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _none_is_exclusive(self) -> Self:
        if "none" in self.exclusion_flags and len(self.exclusion_flags) > 1:
            raise ValueError("exclusion flag 'none' cannot be combined with other flags")
        return self
```

`confidence` is used for ranking only. Its calibration is measured in the screening eval before it is trusted for anything else. The flag `none` cannot be combined with another flag.

### 5.2 Extraction output

```python
class KeyDates(BaseModel):
    """Dates read from the documents, each with its own citation."""

    # Graded against TenderNed `sluitingsDatum` only. Never shown in a memo.
    submission_deadline: KeyDateTime | None
    questions_deadline: KeyDateTime | None
    qa_publications: list[KeyDate] = []  # planned Nota van Inlichtingen dates
    site_visit: KeyDateTime | None = None
    presentation: KeyDateTime | None = None  # presentation, interview or demo
    award_expected: KeyDate | None = None
    standstill_end: KeyDate | None = None  # end of the Alcatel period
    tender_validity: CitedText | None = None  # gestanddoeningstermijn, often a duration
    contract_start: KeyDate | None = None


class LotScope(BaseModel):
    lot_identifier: str | None  # None when the tender has no lots
    title: str
    summary: str = Field(max_length=600)
    estimated_value_eur: Decimal | None
    ceiling_value_eur: Decimal | None  # plafondbedrag
    value_includes_vat: bool | None
    duration_months: int | None
    extension_months: int | None
    citations: list[Citation]


RequirementCategory = Literal[
    "exclusion_ground",
    "financial",
    "insurance",
    "certification",
    "reference_project",
    "staff_qualification",
    "technical_capacity",
    "legal_form",
    "language",
    "security",
    "other",
]
KnockoutKind = Literal["exclusion", "suitability", "minimum_requirement", "formal"]
# Formal requirements are actions in the bid (sign a form, use a format). They are not matched
# against company evidence and are ignored by the recommendation rules.
UNMATCHED_KINDS: frozenset[str] = frozenset({"formal"})
ProofWhen = Literal["uea", "with_bid", "on_request"]
AwardBasis = Literal["bpkv", "lowest_price", "lowest_cost"]
Framework = Literal["none", "single", "multi"]

KO_ID_PATTERN = r"^KO-\d{2,}$"


class KnockoutCriterion(BaseModel):
    requirement_id: str = Field(pattern=KO_ID_PATTERN)  # "KO-01", assigned in the reduce step
    category: RequirementCategory
    kind: KnockoutKind
    text: str = Field(description="The requirement restated in one sentence")
    threshold: str | None = Field(
        description="Number, date or named standard, e.g. 'EUR 1,000,000 per year'"
    )
    proof: str | None = Field(description="The evidence the buyer asks for")
    proof_when: ProofWhen | None
    applies_to_lots: list[str] = []  # empty means all lots
    consortium_allowed: bool | None  # may be met jointly or via a third party
    disproportionate: bool = False  # looks disproportionate (Gids Proportionaliteit)
    citation: Citation
    overridden_by_qa: bool = False


class AwardCriterion(BaseModel):
    name: str
    kind: Literal["price", "quality", "cost"]
    weight_pct: float | None = Field(ge=0, le=100)
    max_points: float | None
    description: str = Field(max_length=600)
    scoring_method: str | None = Field(description="Scale, panel, page limit or price formula")
    subcriteria: list[AwardCriterion] = []
    citation: Citation


class KnockoutFinding(BaseModel):
    """A knock-out as found in one run of pages (map step). No identifier yet."""

    category: RequirementCategory
    kind: KnockoutKind
    text: str = Field(description="The requirement restated in one sentence")
    threshold: str | None = Field(
        description="Number, date or named standard, e.g. 'EUR 1,000,000 per year'"
    )
    proof: str | None = Field(description="The evidence the buyer asks for")
    proof_when: ProofWhen | None
    applies_to_lots: list[str] = []  # empty means all lots
    consortium_allowed: bool | None  # may be met jointly or via a third party
    disproportionate: bool = False  # looks disproportionate (Gids Proportionaliteit)
    citation: Citation


class ExtractionPartial(BaseModel):
    """Output of one map call. Every part may be empty."""

    key_dates: KeyDates
    lots: list[LotScope]
    framework: Framework | None
    knockout_criteria: list[KnockoutFinding]
    award_basis: AwardBasis | None
    award_criteria: list[AwardCriterion]
    contract_risks: list[CitedText]


class ExtractionOutput(BaseModel):
    key_dates: KeyDates
    lots: list[LotScope]
    framework: Framework | None
    knockout_criteria: list[KnockoutCriterion]
    award_basis: AwardBasis | None
    award_criteria: list[AwardCriterion]
    contract_risks: list[CitedText] = Field(description="Liability, penalties, IP, exit terms")
    documents_read: list[int]
    documents_skipped: list[int]
    warnings: list[str] = Field(description="E.g. scanned document, token budget reached")
```

- Each key date carries its own citation.
- `KeyDates.submission_deadline` is kept for grading only (6.1). A memo never shows it.
- Requirement identifiers are `KO-01`, `KO-02`, ... everywhere.
- A map call returns `ExtractionPartial`; the reduce call returns `ExtractionOutput`. A `KnockoutFinding` is a criterion without `requirement_id` and `overridden_by_qa`, which only the reduce step can set.
- After the reduce call, code overwrites `documents_read` and `documents_skipped`, renumbers `requirement_id` in order, adds its own warnings, and marks as unverified any citation that is not one of the partials' citations.
- Each contract risk carries a citation, so that a risk row in the memo has a source.
- `KeyDateTime` cannot hold a date without a time. The v1 prompt writes 00:00 and the reduce step adds a warning.

### 5.3 Requirement-match output

```python
MatchStatus = Literal["met", "not_met", "unclear"]
Mitigation = Literal[
    "none",
    "consortium",
    "subcontractor",
    "third_party",  # beroep op derden
    "ask_in_qa",
    "obtain_before_deadline",
]


UnclearReason = Literal[
    "vague_requirement",
    "no_evidence_found",
    "conflicting_documents",
    "step_limit",  # the agent loop used all its steps
    "token_budget",  # the agent loop used its token budget
    "loop_failed",  # too many tool errors, or the model call failed
]


class RequirementMatch(BaseModel):
    requirement_id: str
    status: MatchStatus
    reasoning: str = Field(max_length=500)
    tender_citation: Citation
    company_evidence: list[KbEvidence]
    gap: str | None = Field(description="What is missing, when not_met or unclear")
    mitigation: Mitigation
    unclear_reason: UnclearReason | None

    @model_validator(mode="after")
    def _status_rules(self) -> Self:
        if self.status == "met" and not self.company_evidence:
            raise ValueError("status 'met' needs at least one company_evidence item")
        if self.status == "unclear" and self.unclear_reason is None:
            raise ValueError("status 'unclear' needs an unclear_reason")
        if self.status == "not_met" and not (self.gap and self.gap.strip()):
            raise ValueError("status 'not_met' needs a gap")
        return self


class MatchingOutput(BaseModel):
    matches: list[RequirementMatch]
    award_notes: list[str]  # one short strength or weakness per award criterion
```

Validation in the schema: `met` needs evidence, `unclear` needs an `unclear_reason`, `not_met` needs a `gap`. A failure is retried once by the gateway. After that, `pipeline/match.py` downgrades a `met` without evidence to `unclear` with the reason `no_evidence_found`.

When the agent loop stops without an answer, `pipeline/match.py` builds the match in code: status `unclear`, no evidence, mitigation `none`, and the criterion's own citation.

| Loop stop | `unclear_reason` |
|---|---|
| Step limit | `step_limit` |
| Token budget | `token_budget` |
| Three tool errors | `loop_failed` |
| Model call failed (refusal, truncated output, invalid answer twice, provider error) | `loop_failed` |
| `met` without evidence, twice | `no_evidence_found` |

`award_notes` is empty in v1. Award positions are written in the memo draft (5.4).

`RequirementMatch.status` has no `action_required`: a model never produces it. Formal knock-outs are not matched (`needs_matching` in `pipeline/match.py`); code builds their `KnockoutRow` with status `action_required`, no evidence and mitigation `none`.

### 5.4 Memo

`Memo` is the stored shape (`memos.content`). It covers every `[copilot]` and `[code]` item of `docs/MEMO_TEMPLATE.md`. It is not a model output shape. The model returns `MemoDraft` and `MemoReason` (below, "Model-facing memo schemas") and code builds the `Memo`. `[human]` items are stored in `memo_human_inputs` (section 3).

```python
FitDimensionName = Literal[
    "scope_match",
    "win_probability",
    "references",
    "commercial",
    "capacity_timing",
    "contract_risk",
    "strategic_value",
]
Recommendation = Literal["bid", "bid_conditional", "no_bid", "needs_review"]
DocType = Literal[
    "guideline",
    "requirements",
    "qa",
    "notice",
    "contract",
    "pricing",
    "form",
    "annex",
    "other",
]


class FitDimension(BaseModel):
    name: FitDimensionName
    weight_pct: int = Field(ge=0, le=100)  # set by code from the fixed table
    scored_by: Literal["copilot", "human"]
    score: int | None = Field(default=None, ge=1, le=5)  # None: not scored
    rationale: str | None = Field(default=None, max_length=400)
    citation: Citation | None = None
    kb_evidence: list[KbEvidence] = []

    @model_validator(mode="after")
    def _copilot_score_needs_source(self) -> Self:
        if self.scored_by == "copilot" and self.score is not None:
            if not (self.rationale and self.rationale.strip()):
                raise ValueError("a copilot score needs a rationale")
            if self.citation is None and not self.kb_evidence:
                raise ValueError("a copilot score needs a citation or KB evidence")
        return self


class FitScore(BaseModel):
    """Computed in code (`pipeline/memo.py`) from the seven dimensions."""

    dimensions: list[FitDimension]
    total: int | None = Field(ge=0, le=100)  # None while any dimension is unscored
    provisional_min: int = Field(ge=0, le=100)  # unscored dimensions at 1
    provisional_max: int = Field(ge=0, le=100)  # unscored dimensions at 5

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        names = sorted(d.name for d in self.dimensions)
        if names != sorted(get_args(FitDimensionName)):
            raise ValueError("fit needs each of the seven dimensions exactly once")
        if sum(d.weight_pct for d in self.dimensions) != 100:
            raise ValueError("fit weights must sum to 100")
        if self.provisional_min > self.provisional_max:
            raise ValueError("provisional_min is above provisional_max")
        if self.total is not None and not (
            self.total == self.provisional_min == self.provisional_max
        ):
            raise ValueError("a final total must equal provisional_min and provisional_max")
        return self


class TenderFacts(BaseModel):
    """Snapshot of the `tenders` row, filled by code. Organisation data only."""

    buyer_name: str
    title: str
    tenderned_id: str | None
    tenderned_url: str | None
    main_nature: str | None
    procedure_type: str | None
    eu_procedure: bool | None  # TenderNed nationaalOfEuropeesCode; None without a match
    cpv_codes: list[str]
    documents_access: Literal["tenderned", "other_platform", "login_required", "none"]
    published_at: datetime | None


class MemoDeadlines(BaseModel):
    # From `tenders.deadline_at` (TenderNed API) only; None is shown as "unknown".
    submission_deadline: datetime | None
    questions_deadline: KeyDateTime | None
    qa_publications: list[KeyDate] = []  # Nota van Inlichtingen 1, 2, ...
    site_visit: KeyDateTime | None = None
    presentation: KeyDateTime | None = None
    award_expected: KeyDate | None = None
    standstill_end: KeyDate | None = None
    tender_validity: CitedText | None = None
    contract_start: KeyDate | None = None


KnockoutRowStatus = Literal["met", "not_met", "unclear", "action_required"]


class KnockoutRow(BaseModel):
    requirement_id: str = Field(pattern=KO_ID_PATTERN)
    requirement: str
    kind: KnockoutKind
    proof: str | None
    proof_when: ProofWhen | None
    status: KnockoutRowStatus  # action_required: a formal requirement, never matched
    evidence: list[KbEvidence]
    gap: str | None
    mitigation: Mitigation
    disproportionate: bool = False
    tender_citation: Citation

    @model_validator(mode="after")
    def _formal_rows_are_actions(self) -> Self:
        if (self.kind in UNMATCHED_KINDS) != (self.status == "action_required"):
            raise ValueError("a formal knock-out has status 'action_required', and only it does")
        if self.status == "action_required" and (self.evidence or self.mitigation != "none"):
            raise ValueError("an 'action_required' row has no evidence and mitigation 'none'")
        return self


class AwardRow(BaseModel):
    name: str
    kind: Literal["price", "quality", "cost"]
    weight_pct: float | None = Field(ge=0, le=100)
    max_points: float | None
    scoring_method: str | None
    position: Level | None  # our position; None when there is no basis to judge
    win_theme: str | None
    evidence: list[KbEvidence] = []
    citation: Citation


class RiskRow(BaseModel):
    risk_id: str = Field(pattern=r"^R-\d{2,}$")
    text: str
    category: Literal[
        "contract_terms",
        "liability_penalties",
        "ip",
        "indexation",
        "volume",
        "social_return",
        "sustainability",
        "privacy_security",
        "subcontracting",
        "capacity",
        "other",
    ]
    likelihood: Level
    impact: Level
    mitigation: Literal["price_in", "ask_in_qa", "accept"]
    citation: Citation | None = None  # None for risks on our own side, e.g. capacity


class QaQuestion(BaseModel):
    question_id: str = Field(pattern=r"^Q-\d{2,}$")
    question: str  # always Dutch, whatever the memo language
    purpose: Literal["clarification", "contradiction", "proportionality", "error"]
    linked_to: list[str] = []  # KO-.. and R-.. identifiers
    priority: Level
    citation: Citation | None = None


class ReviewedDocument(BaseModel):
    document_id: int
    tag: str  # the ID used in source tags, e.g. "LEIDRAAD", "NvI-2"
    filename: str
    doc_type: DocType
    published_at: date | None  # TenderNed `datumPublicatie`


class Memo(BaseModel):
    tender_id: int
    language: Literal["nl", "en"]  # code always sets "en" (decision Q10)
    tender: TenderFacts
    summary: str = Field(max_length=1200)
    lots: list[LotScope]
    target_lots: list[str]
    framework: Framework | None
    deadlines: MemoDeadlines
    fit: FitScore
    knockout_table: list[KnockoutRow]
    award_basis: AwardBasis | None
    price_quality_tradeoff: str | None
    award_table: list[AwardRow]
    risks: list[RiskRow]
    questions_for_qa: list[QaQuestion]
    recommendation: Recommendation  # decided in code, never by the model
    recommendation_reason: str = Field(max_length=600)  # one sentence, written by the model
    recommendation_reason_codes: list[str]  # from the recommendation rules
    reasons: list[str] = Field(max_length=3)
    conditions: list[str]  # what must be true, and by when, for bid_conditional
    confidence: Level
    reflects_qa_document_ids: list[int]  # the Nota's van Inlichtingen this memo reflects
    documents_reviewed: list[ReviewedDocument]
    citations: list[Citation]  # de-duplicated union of all citations above, built by code
    limitations: list[str]  # e.g. "documents not accessible", "2 citations unverified"
```

Filled by code, never by the model: `tender`, `deadlines.submission_deadline`, `fit.total` and the provisional range, `recommendation`, `recommendation_reason_codes`, `documents_reviewed`, `citations`, every `verified` flag, and `language` (always `en`, decision Q10; questions for the Nota van Inlichtingen stay Dutch).

**Submission deadline.** Code sets `deadlines.submission_deadline` from `tenders.deadline_at` (TenderNed API). When that is null the memo shows "unknown", never the extracted value. When the extracted and the API value differ, the code `deadline_differs_from_documents` is added to `limitations`.

**Citations.** Code builds `Memo.citations` as the de-duplicated union of every citation in the memo. `memos.citations_total` and `citations_verified` are counted from it.

**Fit score** (`pipeline/memo.py`, decision Q9). Seven dimensions with the weights of the memo template, section 3:

| Dimension | Weight | Scored by |
|---|---|---|
| `scope_match` | 25 | copilot |
| `win_probability` | 20 | human |
| `references` | 15 | copilot |
| `commercial` | 15 | human |
| `capacity_timing` | 10 | copilot |
| `contract_risk` | 10 | copilot |
| `strategic_value` | 5 | human |

- Total = sum(weight x score) / 5, with scores 1 to 5, so the total runs from 20 to 100.
- A copilot score needs a rationale and a source: KB evidence, or a tender citation that passed the citation check. Without one, code drops the score.
- An unscored dimension, human or copilot, counts 1 for `provisional_min` and 5 for `provisional_max`. `total` is null until all seven are scored.
- When the human scores are entered, the fit score and the recommendation are computed again.

**Recommendation rules** (`decide_recommendation` in `pipeline/memo.py`). The model proposes no value; it writes only `recommendation_reason`. Formal knock-outs are ignored by every rule: a "knock-out" below means one of kind `exclusion`, `suitability` or `minimum_requirement`. A match for a formal knock-out counts as a match without a knock-out. Rules are applied in this order:

1. Documents missing (status `docs_unavailable`, or every document scanned or unsupported): `needs_review`.
2. No knock-out criteria extracted, other than formal ones: `needs_review`.
3. A knock-out `not_met` with mitigation `none`, with both its citations verified: `no_bid`. This wins over every rule below.
4. Any of the following: `needs_review`.
   - a knock-out `unclear`
   - a knock-out `not_met` with mitigation `ask_in_qa` (the outcome depends on the buyer)
   - a knock-out `not_met` with mitigation `none` whose own citation is unverified
   - any other unverified knock-out citation
   - a knock-out without a match, a match without a knock-out, or a duplicate identifier
5. A knock-out `not_met` with mitigation `consortium`, `subcontractor`, `third_party` or `obtain_before_deadline`: the result is capped at `bid_conditional`.
6. Fit score bands: 70 or more `bid`, 50 to 69 `bid_conditional`, under 50 `no_bid`. The result is the worse of the cap and the band. If every score between `provisional_min` and `provisional_max` gives the same result, that result is used. Otherwise `needs_review`, with `awaiting_human_scores` or `fit_score_incomplete`.

A knock-out citation is verified when both `KnockoutCriterion.citation` and `RequirementMatch.tender_citation` passed the check. With all three human scores missing the range is 32 points wide, so a tender with no knock-out problem is always `needs_review` until a human scores it.

**Confidence** (proposed, not built). Code sets `Memo.confidence`, never the model. It is computed after the recommendation and does not feed into it.

| Value | When |
|---|---|
| `low` | Any one of these: documents missing; no knock-outs extracted; any knock-out citation unverified; a quarter or more of `Memo.citations` unverified; an extraction warning about a key document (scanned, token budget reached) |
| `high` | All of these: every non-formal knock-out matched with both citations verified; no knock-out `unclear`; all four copilot fit dimensions scored; every citation in `Memo.citations` verified; no extraction warnings |
| `medium` | Everything else |

The quarter threshold is a first guess, to be set from the evals.

**Model-facing memo schemas.** Two calls, both logged with stage `memo`. These classes are at the end of `pipeline/schemas.py`.

```python
CopilotFitName = Literal["scope_match", "references", "capacity_timing", "contract_risk"]


class FitDraft(BaseModel):
    name: CopilotFitName
    score: int | None = Field(ge=1, le=5)  # None: no source, no guess
    rationale: str | None = Field(max_length=400)
    citation: Citation | None
    kb_evidence: list[KbEvidence] = []

    @model_validator(mode="after")
    def _score_needs_source(self) -> Self:
        if self.score is not None:
            if not (self.rationale and self.rationale.strip()):
                raise ValueError("a score needs a rationale")
            if self.citation is None and not self.kb_evidence:
                raise ValueError("a score needs a citation or KB evidence")
        return self


class AwardPosition(BaseModel):
    criterion_id: str = Field(pattern=r"^AC-\d{2,}$")  # numbered by code in the memo input
    position: Level | None  # None: no basis to judge
    win_theme: str | None = Field(max_length=300)
    evidence: list[KbEvidence] = []


class MemoDraft(BaseModel):
    """Output of the first memo call (`memo_v1`)."""

    summary: str = Field(max_length=1200)
    target_lots: list[str]
    fit: list[FitDraft]
    price_quality_tradeoff: str | None
    award_positions: list[AwardPosition]
    risks: list[RiskRow]
    questions_for_qa: list[QaQuestion]

    @model_validator(mode="after")
    def _four_copilot_dimensions(self) -> Self:
        if sorted(d.name for d in self.fit) != sorted(get_args(CopilotFitName)):
            raise ValueError("fit needs each of the four copilot dimensions exactly once")
        return self


class MemoReason(BaseModel):
    """Output of the second memo call (`memo_reason_v1`), written for the decided value."""

    recommendation_reason: str = Field(max_length=600)
    reasons: list[str] = Field(max_length=3)
    conditions: list[str]
```

- `memo_v1` returns `MemoDraft`. It holds only `[copilot]` items that extraction and matching have not already produced; those are carried over by code, so the memo call cannot alter them. It has no `[code]` item, no `[human]` item and no contact field. The one exception is `Citation.verified`, which is on the shared citation type and is always overwritten.
- Code then checks the draft, computes the fit score and decides the recommendation.
- `memo_reason_v1` receives the decided value and the reason codes as trusted variables and returns `MemoReason`. `conditions` is kept only for `bid_conditional`.
- A draft citation counts only when it equals a verified citation in the memo input. The memo calls see no page text.
- `risk_id` and `question_id` are renumbered by code, and `linked_to` entries without a match are dropped. Award criteria are numbered `AC-01`, ... in the memo input only.
- If the reason call fails, code writes a fixed sentence from the reason codes and adds `reason_not_written` to `limitations`.
- The code that builds the memo input and assembles the `Memo` is not built yet.

| `Memo` field | Source |
|---|---|
| `summary`, `target_lots`, `price_quality_tradeoff`, `risks`, `questions_for_qa` | `MemoDraft` |
| `fit` | `compute_fit` over the four `FitDraft` rows and the human scores |
| `award_table` | `AwardCriterion` rows joined with `AwardPosition` on `criterion_id` |
| `recommendation_reason`, `reasons`, `conditions` | `MemoReason` |
| `lots`, `framework`, `award_basis`, `knockout_table`, `deadlines` | extraction and matching, copied by code (the submission deadline is code) |
| everything else | code |

**EU or national.** `TenderFacts.eu_procedure` comes from TenderNed `nationaalOfEuropeesCode` on the publication detail (`tenders.tenderned_detail`), read when the memo is built; no extra column. Null when there is no TenderNed match. The code values and their mapping to true or false: **(confirm)**.

Not yet defined, and known gaps:

- KB evidence counts as a source for a copilot fit score without being verified (known gap until KB verification is built). Only an unverified tender citation drops a score.
- The inbox order while `fit_score` is null: open until the UI is built.
- A rule for a missing key document (the leidraad) comes after document classification.
- "Working days left until closing" is computed when the memo is shown, not stored.
- One memo per tender or per lot (question 8): `Memo` has no lot field.

### 5.5 Chat answer

```python
class ChatAnswer(BaseModel):
    """The final answer of one chat question (ARCHITECTURE.md 2.6)."""

    answer: str = Field(max_length=2000)
    citations: list[Citation]  # tender documents; verified in code like any citation
    kb_evidence: list[KbEvidence] = []
    in_scope: bool  # False: the question is not about this tender and is declined
```

The final answer of one chat question (2.6). Chat runs the same agent loop as matching, with the four chat tools, 8 steps and its own token budget. The question and earlier turns are passed as data. The caller verifies the citations like any others. On a limit there is no answer.

## 6. eForms fields used as eval ground truth

BT identifiers are from memory **(confirm)** against the eForms SDK version found in real notices. Ground truth is copied to `tenders.eforms_gt` and the `*_gt` columns on `lots` at ingest, and is never given to the model under test. The notice itself is excluded from the retrieval corpus so that the eval is not circular. The submission deadline is the exception: its ground truth is the TenderNed API, not the notice.

### 6.1 Graded: extraction from document text versus the notice

| Field | Source | Tolerance and caveats |
|---|---|---|
| Submission deadline | TenderNed `sluitingsDatum` on `/publicaties/{id}` (not eForms) | Same calendar date; time within 1 hour. Use the latest publication, since a rectification can move it. Tenders whose deadline rule is `unknown` are excluded. BT-131 and BT-1311 are kept in `eforms_gt`; the rate at which they disagree with the API is reported, not graded. |
| Number of lots and lot titles | BT-137, BT-21 at lot level | Count exact; titles by fuzzy match. |
| Estimated value | BT-27, BT-271 (framework maximum) | Within 1%. Only scored where present. Note VAT ambiguity. |
| Contract duration | BT-36, BT-536, BT-537 | Within 1 month. |
| Award criteria: type | BT-539 | Exact on price, quality, cost. |
| Award criteria: weight | BT-541 (and its weight, fixed and threshold variants) | Within 1 percentage point after normalising to 100. Top-level criteria only. |
| Selection criteria: type present | BT-747 | Set comparison per type (economic, technical, suitability). This is a recall check for knock-out detection, not full ground truth. |
| Selection criteria: threshold | BT-750, BT-752 | Only scored where the notice gives a concrete value. |
| Framework agreement | BT-765 | Exact. |
| Security clearance required | BT-578 | Exact. |

DPS (BT-766) and submission language (BT-97) are not graded in v1: `ExtractionOutput` has no field for them (decision of 15 Nov 2026).

Many buyers fill award and selection criteria with "see tender documents". Such rows are excluded by a simple rule (short text or a reference to the documents), and the Phase 0 notebook must report coverage per field over the snapshot. A field with coverage under about 40% is dropped from this table.

### 6.2 Used as pipeline input, not graded

These rarely appear in document text, so extracting them would test nothing: CPV codes (BT-262, BT-263), procedure type (BT-105), main nature (BT-23), buyer name and type (BT-500, BT-11), place of performance, notice type (BT-02), procedure identifier (BT-04), documents URL (BT-15), change and cancellation metadata.

### 6.3 Never used for model input or grading

Contact point, email and phone (BT-502, BT-506, BT-503). Stored in `contacts` only.

### 6.4 What still needs hand labels

- Relevance: 60 tenders, sampled half from Stage 1 "shortlist" and half from "reject", so that positives are not rare.
- Knock-out criteria: 15 tenders, every requirement with its page. This is the only real ground truth for knock-out recall.
- Retrieval: 30 questions with the page that answers them (page-level, so the labels survive a change of chunking).
- Memo rubric grades: 10 memos, also used to check the LLM judge.

### 6.5 Data-quality notes

| Date | Finding | Consequence |
|---|---|---|
| 13 Nov 2026 | Closing dates in the TenderNed open-data spreadsheet do not match the API. Publication 430496 shows 2026-11-20 in the spreadsheet and 2026-09-11 in the API. | The API field `sluitingsDatum` is the only deadline source for the daily job and the evals. The spreadsheet is used for historical counts only. |
| 13 Nov 2026 | Some closing dates are placeholders, such as 2099-12-31. | Treated as `unknown` in the hard filter (2.3) and excluded from deadline grading. |

## 7. Evals mapped to the architecture

| Suite | Tests | Ground truth | Headline metric |
|---|---|---|---|
| screening | 2.3 + 2.4 | hand labels | precision and recall with "unsure" counted as shortlisted (Q13); recall at a fixed precision as a secondary number; cost per 100 screened |
| retrieval | `rag/search.py` | hand-labelled pages | hit@5, MRR; hybrid versus vector-only |
| extraction | 2.5 extract | eForms and TenderNed `sluitingsDatum` (6.1) | accuracy per field, with coverage |
| knockout | 2.5 extract + match | hand annotations | requirement recall; match accuracy |
| memo | 2.5 memo | rubric | judge score, agreement with hand grades, share of verified citations |
| edge_cases | whole pipeline | expected behaviour | pass or fail per case |

Model comparison: three providers on screening and on extraction plus matching, reported as quality, cost and p50/p95 latency from `llm_calls`.

Formulas, label formats, the snapshot file layout and the results file are in `docs/EVALS.md`.

## 8. Open questions to decide

**Access and terms**
1. TenderNed: are documents downloadable without an account, through a public API or only through the website, and do the terms of use allow automated download and storage? This decides whether the design works at all. Check in the first two days.
2. May the frozen snapshot (documents and text) be kept in a public repo, or only the notice IDs plus a download script?
3. If fewer than about 100 TenderNed-hosted tenders with accessible documents match the profile in 8 weeks, do you widen the date range or the CPV list?

**Models and retrieval**
4. Which models for Stage 1 and Stage 2, and which third provider (Gemini or Mistral)? An EU-hosted option would strengthen the transfers paragraph.
5. Embeddings: local BGE-M3 (free, slower on a laptop and a small VM) or a hosted API (cost, and one more data transfer)?
6. Scanned PDFs: flag only, or add OCR (Tesseract with Dutch) as a stretch goal?
7. Tables: accept `pymupdf` text, or use table extraction for pages flagged `has_table`? Award-criteria weights depend on this.

**Product behaviour**
8. One memo per tender, or one per lot? The schema assumes per tender with lot detail inside.
9. Fit score: decided, see section 5.4 and `docs/DECISIONS.md`.
10. Memo language: decided, always English. Questions for the Nota van Inlichtingen stay Dutch.
11. Tenders above EUR 2M: drop, or keep with a "consortium only" flag as proposed?
12. How many tenders may go to Stage 2 per day, and what is the daily cost cap?
13. "Unsure" from screening: decided, always shortlisted.

**Privacy and compliance**
14. Retention period for `contacts` and for stored model payloads.
15. Is best-effort redaction of names in document text acceptable, or do you want a named-entity step?
16. Who are the UI users for the demo: one shared login, or named users so that `approvals.decided_by` means something?

**CRM and deployment**
17. HubSpot deal fields and pipeline stage; whether a rejected memo should also be recorded in the CRM.
18. Hosting: which provider and region for the VM, and whether a live deployment is needed at all before applying, or a recorded demo is enough.

## 9. Differences from PLAN.md

| PLAN.md | This document | Why |
|---|---|---|
| TED first, TenderNed later | TenderNed document fetch from day 1 | TED does not host documents |
| Tenders keyed by notice | Keyed by procedure, with versions | Change and cancellation notices |
| Knock-outs found by search | Key documents read in full | Recall |
| `propose_crm_deal` as an agent tool | Application code after approval | Untrusted document text must not reach a write path |
| HNSW index | Exact search per tender | Small filtered sets; approximate index adds nothing |
| eForms as ground truth for all fields | Split into graded, input-only and never-used | Several fields are not in the documents or are often empty |
| NL plus some DE in retrieval eval | NL only, Dutch and English | Decision taken |
| AWS with RDS and EventBridge | One VM, compose, cron | Decision taken |
| One agent with five tools | Pipeline plus agent loop for matching and chat | Decision taken |
| (not in plan) | Citation verification, status machine, cost cap, `document_pages`, `get_company_facts`, `read_document_pages` | Gaps found in review |
| Deadline parsed from eForms | Deadline from TenderNed `sluitingsDatum` | Spreadsheet and API disagree; one source |
| TenderNed used for documents only | TenderNed detail also fetched at ingest, joined on `pbNummerTed` | The hard filter needs the deadline |
| PDF and DOCX extraction | Also PPTX, XLSX, legacy DOC, nested ZIP | Formats found in the probe |
| (not in plan) | Document versions, de-duplication by SHA-256 only | Same name appears several times |
| (not in plan) | Name rules before the model classifier; `notice` type | Cheaper, and keeps notice copies out of the corpus |
| (not in plan) | Placeholder closing dates treated as unknown | 2099-12-31 found in the data |
| Recommendation: bid, no-bid, needs review | Four values, with `bid_conditional`; decided in code | Matches the memo template |
| Fit score (no formula) | Seven weighted dimensions, four scored by the model and three by a human, with a provisional range | Decision Q9 |
| (not in plan) | `pipeline/schemas.py`, `pipeline/citations.py`, `memo_human_inputs` | Shared contracts; chat also verifies citations; human items need storage |
| One memo call | Two memo calls: a draft, then the reason for the value that code decided | The reason must explain a value the model does not choose |
| (not in plan) | `pipeline/prompt_specs.py`; map-step schema `ExtractionPartial`; contract risks with citations | One place ties prompt to schema; the map step had no schema; risks need a source |
| Retrieval ground truth is the chunk holding the answer | The page holding the answer | Page labels survive a change of chunking (6.4) |
| Evals read the snapshot from the database (`in_snapshot`) | Evals read files in `data/snapshot/`, checked against a manifest, until the database exists | The runner and the labelling can start before Day 7; the hash names the exact data |
| One results file per run | `evals/results/<date>_<commit>.json` holds a list of runs | Three provider runs share a date and a commit |
| Knock-out recall "weighted heaviest" | A composite score with knock-out recall as the largest weight, plus a minimum-recall gate | Decision of 15 Nov 2026 (`docs/EVALS.md` 4.7) |
