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
| `llm/client.py` | The gateway: provider adapters (Anthropic, OpenAI, one of Gemini/Mistral), redaction, schema validation with one retry, cost and latency logging to `llm_calls`. |
| `pipeline/filter.py` | Hard rules from `COMPANY.md` sections 4 and 5. |
| `pipeline/screen.py` | Stage 1 screening call. |
| `pipeline/extract.py` | Stage 2 extraction (map over sections, then merge). |
| `pipeline/match.py` | Requirement matching; one agent loop per requirement. |
| `pipeline/memo.py` | Memo generation, fit score, citation verification. |
| `agent/loop.py`, `agent/tools.py` | Hand-written loop (max steps, tool errors returned to the model, token budget) and tool implementations. |
| `jobs/daily.py` | Orchestrates steps 1 to 8; takes a Postgres advisory lock so two runs cannot overlap. |
| `app/` | Streamlit: inbox, memo view, chat per tender, approve/reject. |
| `crm/hubspot.py` | Creates the deal from an approved memo. |
| `evals/` | Datasets (JSONL, in git), runners, results. |
| `prompts/` | Versioned prompt files (`screen_v1.md`, `extract_v1.md`, ...). |

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

One call per tender with a small model. Input: redacted notice title, description, lot titles, CPV labels, buyer type, value, and sections 2, 4 and 5 of the company profile. Output: `ScreeningOutput` (section 5.1). No tools, no documents.

### 2.5 Deep analysis (Stage 2)

**Extract.**
- Documents are classified by type, by rule first and by model only when no rule matches (2.10). The current version of each `guideline` and `requirements` document is read in full. Every version of a `qa` document is read, oldest first by `datumPublicatie`, because same-named answer files can be separate answers. `notice` documents are never read. Annexes, forms and price sheets are indexed for search but not read.
- Map: each section of the key documents is sent with the extraction prompt and returns partial results with citations. Reduce: one call merges and de-duplicates into `ExtractionOutput`.
- The Nota van Inlichtingen documents are processed last, in date order, and may override earlier values (a moved deadline, a relaxed requirement); a later answer overrides an earlier one. Overrides are recorded.
- If the key documents exceed the token budget, the tender is marked `needs_review` instead of being truncated silently.

**Match.**
- For each knock-out criterion, one short agent loop (max 6 steps) with the tools in section 4. It ends with a `RequirementMatch`.
- Numeric and date requirements (turnover, insurance cover, certificate validity, number of references) use `get_company_facts`, not vector search.
- Award criteria are not matched as met or not met. They get a short strength note for the memo.

**Memo.**
- The fit score is computed in code from the match results and notice fields (section 5.4). The model writes text, not scores.
- Citation check: for every citation, the quote is normalised (whitespace, hyphenation, case) and searched on the cited page and the pages on either side. Unverified citations are marked in the memo. Any unverified citation on a knock-out sets the recommendation to `needs_review`.

### 2.6 Chat

Per tender. The same agent loop with `get_tender`, `search_tender_docs`, `read_document_pages` and `search_company_kb`. Answers carry citations. Questions outside the tender are declined. Chat has no write tools.

### 2.7 Approval and CRM

1. The user clicks Approve or Reject on a memo; the UI writes an `approvals` row.
2. On approve, `crm/hubspot.py` builds the deal from database fields (title, buyer, deadline, value, fit score, memo link) and creates it. No model output is passed through unchecked, and no contact data is sent.
3. `approvals.crm_status` records `created` or `failed`; a retry button reuses the same row, so one memo can never create two deals.

### 2.8 Privacy

- Contact data lives only in `contacts`. No query used to build model input joins that table.
- The gateway redacts emails, phone numbers and the contact names known from the notice before every call. This is best effort: names that appear only in document body text are not caught, and the README must say so.
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
| recommendation | text | `bid`, `no_bid`, `needs_review` |
| fit_score | int | 0 to 100 |
| citations_total, citations_verified | int | |
| extraction_id, matching_id | fk assessments | inputs used |
| prompt_version, model | text | |
| is_current | bool | |

Indexes: unique `(tender_id, version)`, `(is_current, fit_score desc)` for the inbox.

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
| input_tokens, output_tokens, cached_tokens | int | |
| cost_usd | numeric(10,6) | from a price table in config |
| latency_ms | int | |
| tool_calls | jsonb | names and arguments |
| stop_reason | text | |
| validation_retries | int | |
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
  "description": "Return exact structured facts about the company. Use this instead of search whenever a requirement states a number, a date or a named certificate: minimum turnover, insurance cover, certificate validity, headcount, or the number, value and year of reference projects.",
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

Loop limits: matching 6 steps per requirement, chat 8 steps per question, plus a token budget per loop. When a limit is hit during matching, the result is `unclear` with the reason `step_limit`.

## 5. Pydantic schemas

Shared types first. These are the contracts between steps and the shape of `assessments.output` and `memos.content`.

```python
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from pydantic import BaseModel, Field

class Citation(BaseModel):
    """A checkable pointer into a tender document."""
    document_id: int
    page: int = Field(ge=1)
    quote: str = Field(min_length=10, max_length=600, description="Verbatim text from that page")
    verified: bool | None = None  # set by code after the citation check, never by the model

class KbEvidence(BaseModel):
    """A pointer into the company knowledge base."""
    kb_id: int
    doc_slug: str
    quote: str = Field(max_length=600)
```

### 5.1 Screening output

```python
ExclusionFlag = Literal[
    "hardware_supply", "staffing_only", "security_clearance", "onsite_abroad",
    "ops_24_7", "pure_custom_software", "liability", "none",
]
ServiceLine = Literal[
    "data_platform", "ai_ml", "bi_dashboards", "cloud_migration", "data_governance",
]

class ScreeningOutput(BaseModel):
    decision: Literal["shortlist", "reject", "unsure"]
    reason: str = Field(max_length=400, description="One or two sentences citing the notice")
    matched_services: list[ServiceLine]
    exclusion_flags: list[ExclusionFlag]
    confidence: float = Field(ge=0, le=1)
```

`confidence` is used for ranking only. Its calibration is measured in the screening eval before it is trusted for anything else.

### 5.2 Extraction output

```python
class KeyDates(BaseModel):
    submission_deadline: datetime | None
    questions_deadline: datetime | None
    site_visit: datetime | None = None
    award_expected: date | None = None
    contract_start: date | None = None
    citations: list[Citation]

class LotScope(BaseModel):
    lot_identifier: str | None       # None when the tender has no lots
    title: str
    summary: str = Field(max_length=600)
    estimated_value_eur: Decimal | None
    value_includes_vat: bool | None
    duration_months: int | None
    extension_months: int | None
    citations: list[Citation]

RequirementCategory = Literal[
    "exclusion_ground", "financial", "insurance", "certification", "reference_project",
    "staff_qualification", "technical_capacity", "legal_form", "language",
    "security", "other",
]

class KnockoutCriterion(BaseModel):
    requirement_id: str               # "KO-01", assigned in the reduce step
    category: RequirementCategory
    text: str = Field(description="The requirement restated in one sentence")
    threshold: str | None = Field(description="Number, date or named standard, e.g. 'EUR 1,000,000 per year'")
    applies_to_lots: list[str] = []   # empty means all lots
    consortium_allowed: bool | None   # may be met jointly or via a third party
    citation: Citation
    overridden_by_qa: bool = False

class AwardCriterion(BaseModel):
    name: str
    kind: Literal["price", "quality", "cost"]
    weight_pct: float | None = Field(ge=0, le=100)
    max_points: float | None
    description: str = Field(max_length=600)
    subcriteria: list["AwardCriterion"] = []
    citation: Citation

class ExtractionOutput(BaseModel):
    key_dates: KeyDates
    lots: list[LotScope]
    knockout_criteria: list[KnockoutCriterion]
    award_criteria: list[AwardCriterion]
    contract_risks: list[str] = Field(description="Liability, penalties, IP, exit terms")
    documents_read: list[int]
    documents_skipped: list[int]
    warnings: list[str] = Field(description="E.g. scanned document, token budget reached")
```

### 5.3 Requirement-match output

```python
class RequirementMatch(BaseModel):
    requirement_id: str
    status: Literal["met", "not_met", "unclear"]
    reasoning: str = Field(max_length=500)
    tender_citation: Citation
    company_evidence: list[KbEvidence]  # must be non-empty when status == "met"
    gap: str | None = Field(description="What is missing, when not_met or unclear")
    mitigation: Literal["none", "consortium", "subcontractor", "ask_in_qa", "obtain_before_deadline"]
    unclear_reason: Literal["vague_requirement", "no_evidence_found", "conflicting_documents", "step_limit"] | None

class MatchingOutput(BaseModel):
    matches: list[RequirementMatch]
    award_notes: list[str]              # one short strength or weakness per award criterion
```

Validation in code: `met` without evidence is rejected and retried once, then downgraded to `unclear`.

### 5.4 Memo

```python
class FitScore(BaseModel):
    """Computed in code, not by the model."""
    total: int = Field(ge=0, le=100)
    knockouts_met: int
    knockouts_not_met: int
    knockouts_unclear: int
    quality_weight_pct: float | None    # company prefers 50% or more
    value_in_range: bool | None
    service_overlap: float = Field(ge=0, le=1)

class KnockoutRow(BaseModel):
    requirement_id: str
    requirement: str
    status: Literal["met", "not_met", "unclear"]
    evidence: str
    tender_citation: Citation

class Memo(BaseModel):
    tender_id: int
    language: Literal["nl", "en"]
    summary: str = Field(max_length=1200)
    key_dates: KeyDates
    fit: FitScore
    knockout_table: list[KnockoutRow]
    award_strategy: str
    risks_and_gaps: list[str]
    questions_for_qa: list[str]         # for the Nota van Inlichtingen
    recommendation: Literal["bid", "no_bid", "needs_review"]
    recommendation_reason: str = Field(max_length=600)
    citations: list[Citation]
    limitations: list[str]              # e.g. "documents not accessible", "2 citations unverified"
```

Recommendation rules applied in code after generation:

- any knock-out `not_met` with mitigation `none`: `no_bid`
- any knock-out `unclear`, any unverified knock-out citation, or documents missing: `needs_review`
- otherwise the model's recommendation stands

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
| Framework agreement or DPS | BT-765, BT-766 | Exact. |
| Security clearance required | BT-578 | Exact. |
| Submission language | BT-97 | Exact. |

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
| screening | 2.3 + 2.4 | hand labels | recall at fixed precision; cost per 100 tenders |
| retrieval | `rag/search.py` | hand-labelled pages | hit@5, MRR; hybrid versus vector-only |
| extraction | 2.5 extract | eForms and TenderNed `sluitingsDatum` (6.1) | accuracy per field, with coverage |
| knockout | 2.5 extract + match | hand annotations | requirement recall; match accuracy |
| memo | 2.5 memo | rubric | judge score, agreement with hand grades, share of verified citations |
| edge_cases | whole pipeline | expected behaviour | pass or fail per case |

Model comparison: three providers on screening and on extraction plus matching, reported as quality, cost and p50/p95 latency from `llm_calls`.

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
9. Fit score: confirm it is a formula in code, and choose the weights.
10. Memo language: Dutch, English, or the language of the tender?
11. Tenders above EUR 2M: drop, or keep with a "consortium only" flag as proposed?
12. How many tenders may go to Stage 2 per day, and what is the daily cost cap?
13. Should "unsure" from screening always be shortlisted, or only above a confidence level?

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
