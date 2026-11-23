# Plan: Postgres schema, migrations and the stores

Status: plan, 17 Nov 2026. Nothing in this file is built. Written for an implementer who follows it step by step; every design choice is made here or listed under "Open decisions" for Arthur.

The request named this plan without listing its contents. Its scope here is taken from `ARCHITECTURE.md` section 3 (the tables), 2.2 (statuses, lock, budget), 2.7 (approvals), 2.8 (privacy and retention), 2.9 (deployment), and from the store protocols that `pipeline.md` (P13) and `documents.md` (D3, D4) define for this plan to implement. Tell me if you meant something else.

Read first: `CLAUDE.md`, `docs/ARCHITECTURE.md` 2.2, 2.7, 2.8, 2.9, 3; `docs/DECISIONS.md` (Q14, Q16, B12, LLM gateway, Evals E4); `docs/plans/pipeline.md` P0, P13, P14; `docs/plans/documents.md` D3, D4, D8, DD2, DD7.

## 0. Scope and what already exists

| Part | Module | Status today |
|---|---|---|
| Connection and settings | `db/engine.py` | missing |
| Schema, as SQL migrations | `db/migrations/0001_init.sql`, `db/migrate.py` | missing |
| Table definitions for queries | `db/tables.py` | missing |
| `llm_calls` sink (replaces `JsonlCallLog` in the daily job) | `db/call_log.py` | missing (`llm/log.py` has the protocol and the JSONL sink) |
| Ingest store (`IngestStore`, documents.md D3) | `db/ingest_store.py` | missing |
| Document sink (`DocumentSink`, documents.md D4) | `db/document_store.py` | missing |
| Job store (`TenderStore`, pipeline.md P13) | `db/tender_store.py` | missing (`jobs/store.py` `MemoryStore` built in pipeline.md P13) |
| Memo rendering for `memos.rendered_md` | `pipeline/memo_render.py` | missing |
| Tool backend over the database (non-search tools) | `db/tool_backend.py` | missing (`agent/fakes.py` is the fake) |
| Retention jobs | `db/retention.py` | missing (`PayloadStore.purge` exists) |
| Compose: `worker` service, migrations at start | `docker-compose.yml`, `Dockerfile` | `db` and `app` only |

Not covered: search queries and the chunk and KB writers (`search.md`); the Streamlit UI and the HubSpot client (later plans; their tables are created here).

### Fixed rules for every step

1. All SQL goes through `db/`. No other package imports `sqlalchemy` or `psycopg`.
2. `contacts` is read in exactly two functions: `TenderStore.contact_rows` (for the Redactor) and `retention.purge_contacts`. A test reads every `.py` file under `db/` and fails on any other SQL that names `contacts`. No view or join includes it.
3. Every write that changes a tender's status and its step output happens in one transaction.
4. `llm_calls` rows are written in their own short transaction (autocommit connection), so a failed tender step never loses the record of what it cost.
5. Tests that need Postgres are marked `db` and run only when `TEST_DATABASE_URL` is set (DB4). Every protocol also has the in-memory implementation, and one shared contract test suite runs against both.
6. Done means for every step: `make test` passes (database tests skipped locally without a database, run in CI) and `make lint` is clean.

## 1. Dependencies to approve

Releases on or before the lock cutoff (30 Oct 2026), so all are older than two weeks. Dates are PyPI upload dates, checked 16 Nov 2026.

| Package | Version | Released | Licence | Needed for |
|---|---|---|---|---|
| sqlalchemy | 2.1.6 | 21 Oct 2026 | MIT | Core table definitions and queries (in the CLAUDE.md stack) |
| psycopg[binary] | 3.3.6 (with psycopg-binary 3.3.6) | 18 Sep 2026 | LGPL-3.0 | Postgres driver |
| pgvector | 0.5.1 | 9 Oct 2026 | MIT | `vector` column type for SQLAlchemy and psycopg |

Considered, not recommended:

| Package | Version | Released | Why not |
|---|---|---|---|
| alembic | 1.20.1 | 22 Oct 2026 | DB2: plain SQL files are enough for one schema and one developer |
| testcontainers | 4.15.0 | 24 Jul 2026 | Needs Docker on the Mac, which is not installed (`NEXT.md` 13); a CI service container does the same without a dependency |

## 2. Open decisions for Arthur

| # | Question | Recommendation | Consequence of each option | Step |
|---|---|---|---|---|
| DB1 | Where does database code live? `CLAUDE.md` lists no such folder. | New package `db/` (add it to the `CLAUDE.md` layout, `pyproject.toml` packages and `ARCHITECTURE.md` 2.1). | **`db/`:** one place for all SQL, easy to enforce rule 1. **Spread over `jobs/`, `ingest/`, `rag/`:** no layout change, but SQL in many places and the contacts rule is harder to check. | B0 |
| DB2 | Migrations: numbered SQL files with a 60-line runner, or Alembic? | Numbered SQL files (`db/migrations/NNNN_name.sql`), applied in order by `db/migrate.py`, recorded in `schema_migrations`. Forward only. | **SQL files:** no dependency; the schema reads like `ARCHITECTURE.md` 3; no automatic down-migrations. **Alembic:** autogenerate and downgrades; one more dependency and a second copy of the schema in Python. | B1, B2 |
| DB3 | SQLAlchemy Core or ORM? | Core: `Table` objects in `db/tables.py` and explicit statements; rows mapped to the existing dataclasses (`TenderRow`, `DocumentRecord`, ...). | **Core:** no second model layer next to the Pydantic schemas; every query is visible. **ORM:** relationships for free, but lazy loading can join `contacts` by accident. | B0 |
| DB4 | How do database tests run without Docker on the Mac? | CI gets a `pgvector/pgvector:pg16` service container and sets `TEST_DATABASE_URL`; locally the `db` tests are skipped unless `TEST_DATABASE_URL` is set. Install Docker Desktop when convenient (`NEXT.md` 13) to run them locally. | **CI service:** green builds prove the SQL; local runs are fast but do not cover SQL until Docker is installed. **Postgres.app plus pgvector built locally:** local coverage without Docker, but a manual install that CI does not mirror. | B0 |
| DB5 | Statuses and other value lists: Postgres enums, or text with a `CHECK` constraint? | Text with `CHECK`. A new value is a one-line migration. | **CHECK:** easy to extend (as `cancelled` and `default` already needed). **Enum:** stricter type, but `ALTER TYPE` in migrations is awkward. | B1 |
| DB6 | Retention of downloaded documents and their text (Q14 says "deleted after evaluation" for documents, Proposed). For live tenders that is undefined. | Raw files, pages and chunks of a tender are deleted 180 days after its closing date (or 365 days after publication without one), unless the tender has an approved memo; snapshot tenders (`in_snapshot`) are never purged by this job. Memos and assessments stay (their quotes are short). | **Recommended:** bounded storage of documents that can hold personal data; approved memos stay auditable. **Keep everything:** simpler; the README must say documents are kept indefinitely. **Delete at closing:** least data, but memos can no longer be re-verified or chatted with after closing. | B9 |
| DB7 | While a tender is re-analysed (a new Nota van Inlichtingen reset it to `shortlisted`), which memo does the inbox show? | The old memo stays `is_current` until the new one is saved; the inbox shows the tender status next to it ("re-analysis pending"). | **Keep old:** the reader always has a memo, clearly marked as being updated. **Hide it:** no stale advice shown, but the tender disappears from the inbox for a day. | B6, B7 |
| DB8 | Do the evals write to Postgres (`eval_runs`, `llm_calls` with `eval_run_id`)? | Not in v1. The tables are created, but the eval runner keeps its JSONL call log and results files (decision of 15 Nov). `cost_today` ignores rows with an `eval_run_id`. | **Not now:** no change to the evals. **Now:** one place for all cost data, but the runner needs a database. | B3 |
| DB9 | Inbox order while `fit_score` is null (open since 14 Nov, "decide with the UI"). | Not decided here. The schema adds an index on `(is_current, fit_score_max desc)` as well as the planned one, so either order is cheap. | n/a | B1 |

Decisions taken in this plan without asking:

- Columns added to `ARCHITECTURE.md` 3, each requested by another plan: `tenders.deadline_has_time` (pipeline.md PD14), `tenders.tenderned_rectification` (documents.md DD2), `tenders.notice_xml_path` (2.1: "XML stored on disk"), `tenders.status` value `cancelled` (documents.md DD7), `documents.download_id` (tie rule), `documents.extraction_notes` (2.10 "a warning is recorded"), `documents.doc_type_source` value `default` (documents.md DD6), a unique `memos (tender_id) where is_current`, and the table `tenderned_publications` (documents.md D3 cache). `ARCHITECTURE.md` 3 is updated after Arthur's approval.
- `chunks.fts` and `company_kb.fts` are generated columns (`GENERATED ALWAYS AS (...) STORED`), choosing `dutch` or `english` by `language` (other or null → `simple`), so no writer can forget them. `search.md` may not use them (its SD1); they cost little.
- Money is `numeric`; times are `timestamptz`; `datumPublicatie` is `date`.

## 3. Build steps

Eleven steps (B0–B10). B0–B3 first; B4–B6 in any order after them; B7–B10 last.

### B0. Package, settings, connections, test database

**Files.**

- `db/__init__.py`, `db/engine.py`:
  ```python
  DATABASE_URL_ENV = "DATABASE_URL"            # postgresql+psycopg://user:pass@host:5432/db
  TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"

  def database_url(env: str = DATABASE_URL_ENV) -> str           # read like llm/env.read_key; never printed
  def make_engine(url: str) -> sqlalchemy.Engine                 # pool_pre_ping, application_name tender-copilot
  def autocommit_engine(url: str) -> sqlalchemy.Engine           # for llm_calls (rule 4)
  ```
- `tests/conftest.py`: a `pg_engine` fixture (session scope) that skips unless `TEST_DATABASE_URL` is set, creates a fresh schema `test_<uuid4 hex>`, sets `search_path`, runs all migrations, yields, drops the schema. A `clean_db` fixture truncates every table between tests.
- `pyproject.toml`: the three dependencies (after approval); package `db`; marker `db` registered (`--strict-markers` is on).
- `.github/workflows/ci.yml`: service `postgres` with image `pgvector/pgvector:pg16`, health check, and `TEST_DATABASE_URL` set for the pytest step.

**Tests.** `database_url` raises a clear error naming the variable (not its value) when unset; the URL never appears in the error of a failed connection (connect to a bad host with a password in the URL and check `str(exc)`); with a database: `select 1` works and `CREATE EXTENSION vector` is available.

**Done when.** CI runs the `db` tests against the service container; locally they are skipped with a reason.

---

### B1. Schema migration `0001_init.sql`

**File.** `db/migrations/0001_init.sql`: every table of `ARCHITECTURE.md` section 3 with the conventions (`id bigserial primary key`, `created_at timestamptz not null default now()`), the indexes listed there, and the additions of section 2. `CREATE EXTENSION IF NOT EXISTS vector;` first.

Exact column lists follow `ARCHITECTURE.md` 3. Constraints to write (DB5):

| Table | Constraint |
|---|---|
| `tenders` | `status` in (`new`, `filtered_out`, `screened_out`, `shortlisted`, `docs_ready`, `docs_unavailable`, `extracted`, `matched`, `memo_ready`, `failed`, `cancelled`); `documents_access` in (`tenderned`, `other_platform`, `login_required`, `none`); `failed_step` null or in the eight step names of pipeline.md P13; `attempts >= 0`; `country` matches `^[A-Z]{2}$` when not null; `status = 'cancelled'` implies `is_cancelled` |
| `documents` | `file_format`, `page_kind`, `doc_type`, `doc_type_source` (`rule`, `model`, `default`), `language` (`nl`, `en`, `other`), `extraction_status` lists from 3 and the plans; `sha256 ~ '^[0-9a-f]{64}$'`; archive rows have null `doc_type` |
| `document_pages` | `page_no >= 1` |
| `assessments` | `stage` in (`screening`, `extraction`, `matching`); `decision` only for screening |
| `memos` | `recommendation` in the four values; `fit_score` null or 0–100; `fit_score_min <= fit_score_max`; `citations_verified <= citations_total` |
| `approvals` | `decision` in (`approved`, `rejected`); `crm_status` in (`not_applicable`, `pending`, `created`, `failed`) |
| `llm_calls` | `status` in the five `llm/log.py` values; `stage` in the eight `Stage` values |
| `memo_human_inputs` | each human score null or 1–5 |

New table:

```sql
CREATE TABLE tenderned_publications (
  id bigserial PRIMARY KEY,
  publicatie_id text NOT NULL UNIQUE,
  ted_number text,                       -- normalised <serial>-<year>, null for national publications
  published date,
  seen_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON tenderned_publications (ted_number) WHERE ted_number IS NOT NULL;
```

Generated full-text columns:

```sql
fts tsvector GENERATED ALWAYS AS (
  to_tsvector(CASE language WHEN 'nl' THEN 'dutch'::regconfig
                            WHEN 'en' THEN 'english'::regconfig
                            ELSE 'simple'::regconfig END, content)) STORED
```

`embedding vector(1024)` on `chunks` and `company_kb` (nullable until embedded; `search.md` fills it). No HNSW index (3: exact search per tender).

**Tests** (`tests/test_db_schema.py`, `db` marker):

- Every table and index named in `ARCHITECTURE.md` 3 exists (a list in the test, compared with `pg_catalog`).
- Each `CHECK`: one valid insert, one invalid insert raising `IntegrityError` (table-driven).
- Unique `(tenders.procedure_id)`, `(documents.tender_id, sha256)`, `(documents.tender_id, version_group) where is_current`, `(assessments.tender_id, stage) where is_current`, `(memos.tender_id) where is_current`, `(approvals.memo_id)`, `(approvals.crm_deal_id) where not null`: a second row fails.
- `fts` is filled for a Dutch row (`'aanbestedingsleidraad'` stemmed) and for an English one; `simple` for `other`.
- Deleting a tender cascades to `lots`, `contacts`, `documents`, `document_pages`, `chunks`, `assessments`, `memos`, `memo_human_inputs`.

**Done when.** The migration applies to an empty database and the tests pass in CI.

---

### B2. Migration runner and `make migrate`

**Files.** `db/migrate.py`:

```python
MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")

def pending(conn) -> list[Path]
def apply(engine) -> list[str]           # applied names; each file in its own transaction
def main(argv=None) -> int               # python -m db.migrate
```

**Rules.** Table `schema_migrations(name text primary key, sha256 char(64), applied_at timestamptz)`. Files are applied in number order; each in one transaction with a row in `schema_migrations`. A recorded migration whose file hash changed stops the runner (a used migration is never edited, like prompts under R11). A file name that does not match `_NAME` stops it. `pg_advisory_lock` around the whole run so two containers cannot migrate at once. `make migrate` runs `python -m db.migrate`.

**Tests.** Two temporary migration files in `tmp_path`: both applied in order; a second run applies nothing; a changed applied file stops with its name; a failing second file leaves the first applied and the second absent; a badly named file stops before anything runs.

**Done when.** Tests pass; `docker compose run app python -m db.migrate` works (when Docker is available).

---

### B3. `llm_calls` sink and the day's cost

**Files.** `db/call_log.py`:

```python
class PgCallLog:                          # implements llm.log.CallLog
    def __init__(self, engine: Engine) -> None      # an autocommit engine (rule 4)
    def write(self, record: LlmCallRecord) -> None
def cost_today(conn, *, now: datetime | None = None) -> Decimal
```

**Rules.** One insert per record, every field of `LlmCallRecord` to its column (`tool_calls` as jsonb). `write` never raises into the gateway for a database error: it logs the error class to stderr and appends the record to `data/llm_calls.jsonl` as a fallback, because the gateway's `finally` must not hide the step's own error. `cost_today` sums `cost_usd` for `created_at` from the start of today in Europe/Amsterdam (`ingest.parsing.start_of_day`), `eval_run_id is null` (DB8).

**Tests.** A record round-trips (all fields equal after reading back, `Decimal` exact). `cost_today` counts today's rows, not yesterday's (a row at 23:30 UTC on the previous Amsterdam day, during summer and winter time), not eval rows. A database error in `write` (closed engine) does not raise and writes the JSONL fallback in `tmp_path`.

**Done when.** Tests pass; `jobs/daily.py` `main` uses `PgCallLog`.

---

### B4. Ingest store (`db/ingest_store.py`, implements documents.md D3 `IngestStore`)

**Methods and rules.**

- `remember_publications(refs)`: upsert on `publicatie_id`, updating `ted_number`, `published`, `seen_at`.
- `publication_for(ted_number)`: the `publicatie_id` with that number; with several, the lowest numeric ID (the original publication; rectifications are separate publications, documents.md DD2) **(confirm with D0)**.
- `upsert_tender(tender: IngestedTender) -> UpsertOutcome`, in one transaction:
  1. Write the stripped XML to `NOTICE_DIR/<procedure_id hash>/<publication_id>.xml` (`NOTICE_DIR` env, default `data/raw/notices`), set `notice_xml_path`.
  2. Find the row by `procedure_id`. None: insert with `status = 'new'`, `notice_version = int(notice.notice_version or 1)`, `attempts = 0`. Insert `lots` and `contacts` (with `purge_after`).
  3. Existing row, newer notice version (compare `notice_version`, then publication date): update the notice fields, set `notice_id` to the new one and append the old to `superseded_notice_ids`; replace `lots` (`ON CONFLICT (tender_id, lot_identifier) DO UPDATE`); add new contacts (no duplicates on `(tender_id, contact_name, email, phone)`).
  4. Older or equal version: notice fields untouched; TenderNed fields may still be filled (late match).
  5. TenderNed fields: never overwrite a non-null `tenderned_id` with null; store `tenderned_detail`, `tenderned_rectification`, `deadline_at` and `deadline_has_time` from `effective_sluitingsdatum` parsed by `parse_sluitingsdatum` (null when missing, unparseable or a placeholder: `is_placeholder_deadline`), `documents_access`.
  6. `is_cancelled` true: set it and `status = 'cancelled'` (any prior status). It is never set back to false.
  7. `eforms_gt`: the notice deadline (BT-131, BT-1311), lot values, durations, framework maximum, from `TedNotice` (ARCHITECTURE 3 and 6). `raw_notice`: the TED search hit JSON.
  Returns `inserted`, `updated` or `unchanged`.
- `unmatched_tenders(published_after)`: `tenderned_id is null`, not cancelled, `published_at >= published_after`; returns `(tender_id, TedNotice)` pairs (notice parsed from the XML file).
- `attach_tenderned(tender_id, match)`: one transaction: sets the TenderNed fields as in step 5 of `upsert_tender`, inserts `match.contacts`, and when the status is past `shortlisted` (`docs_unavailable`, `memo_ready`, and so on) resets it to `shortlisted` with reason `tenderned_matched`. `new`, `filtered_out` and `screened_out` keep their status.
- `active_tenders(today)`: status in (`docs_ready`, `docs_unavailable`, `extracted`, `matched`, `memo_ready`, `failed`), `documents_access = 'tenderned'`, not cancelled, `deadline_at` null or `>= start_of_day(today)`.
- `known_document_ids(tender_id)`: `tenderned_document_id` values of its `documents`.
- `reset_to_shortlisted(tender_id, reason)`: `status = 'shortlisted'`, `attempts = 0`, `failed_step = null`, `error = 'reanalysis:' || reason`, unless cancelled. Current assessments and the memo stay current until replaced (DB7).
- `purge_contacts(today)`: in `db/retention.py` (B9), called through this store.

**Tests** (`db` marker; plus the shared contract suite against `MemoryIngestStore` from documents.md):

- Insert, then the same notice again → `unchanged`; a version-02 change notice → `updated`, `notice_id` replaced, old one in `superseded_notice_ids`, `notice_version 2`; then version 01 arriving late → `unchanged`.
- Late match: an unmatched tender gets a `tenderned_id`; a later upsert with `tenderned_id = None` keeps it.
- `deadline_at`: `2026-04-20T12:00:00+02:00` → 10:00 UTC, `deadline_has_time` true; `2026-06-15` → start of day Amsterdam, false; `2099-12-31` → null.
- Rectification date wins over the original (via `effective_sluitingsdatum`).
- Cancellation moves `memo_ready` to `cancelled`; a later upsert without the flag keeps it cancelled.
- `reset_to_shortlisted` on a cancelled tender does nothing.
- Contacts are written only to `contacts`; `tenders.tenderned_detail` and the XML file hold no contact key or `cac:Contact` (the ingest strips them, the store asserts it: an unstripped XML raises before writing).
- `active_tenders` excludes closed, cancelled and non-TenderNed tenders.

**Done when.** Tests pass in CI.

---

### B5. Document sink (`db/document_store.py`, implements documents.md D4 `DocumentSink`)

**Methods and rules.**

- `has_sha(tender_id, sha256)`, `known_document_ids(tender_id)`.
- `add(tender_id, record, pages) -> int`: one transaction. `parent_key` (the archive's SHA-256) is resolved to `parent_document_id` within the same tender; an unknown parent raises. `INSERT ... ON CONFLICT (tender_id, sha256) DO NOTHING RETURNING id`; on conflict return the existing ID and add no pages. Pages inserted with `(document_id, page_no)` unique. `is_current` is inserted false; `recompute_current` sets it. `page_count`, `text_chars` computed from `pages` (not trusted from the record).
- `recompute_current(tender_id)`: one statement with a window function: per `(tender_id, version_group)`, rank by `published_at desc nulls last, download_id desc nulls last, id desc`; set `is_current = (rank = 1)`. Archive members: their group already contains the archive's group as prefix (documents.md decision), so they rank among themselves. Runs inside one transaction so the partial unique index is never violated mid-way (set all false first, then the winners true).
- `set_access(tender_id, access)`.
- `docs_view(tender_id) -> DbDocs` (used by `TenderStore.docs`): a frozen dataclass satisfying pipeline.md P0 `DocsView` (`published_at` given as an ISO date string, as `DocView` expects); documents ordered by `(published_at, id)`; `pages` loaded lazily per document (a property that queries `document_pages` once and caches), so a tender with 40 annexes does not load every sheet for a memo.

**Tests** (`db` marker plus contract suite against the in-memory sink):

- Duplicate SHA in one tender → one row, second `add` returns the same ID and adds no pages; the same SHA in another tender → two rows.
- Archive with two members → `parent_document_id` set on both. `parent_document_id` is `ON DELETE SET NULL` (only `tender_id` cascades), so deleting the archive row leaves the members with a null parent: test that.
- `recompute_current`: newer date wins; equal dates → higher `download_id`; null dates lose; two groups independent; running it twice changes nothing.
- `docs_view` satisfies `DocsView` (pass it to `pipeline.inputs.documents_missing` and `document_tags`); pages loaded once per document (count queries with a SQLAlchemy event listener).
- A page with a NUL character is rejected by the database: the sink asserts none before insert (defence behind documents.md `clean_text`).

**Done when.** Tests pass in CI.

---

### B6. Job store (`db/tender_store.py`, implements pipeline.md P13 `TenderStore`)

**Methods and rules.**

- `lock()`: a context manager on a dedicated connection: `select pg_try_advisory_lock(hashtext('tender-copilot-daily'))`; yields the boolean; unlocks and closes on exit. Session-level, so a crashed process releases it.
- `tenders(statuses)`: rows in those statuses, never `is_cancelled`, as `TenderRow` (notice parsed from `notice_xml_path`; `screening_confidence` from the current screening assessment; `sluitingsdatum` = the effective raw value).
- `retryable_failures(max_attempts)`: `status = 'failed'`, `attempts < max_attempts`, not cancelled.
- `set_filter(tender_id, result, status)`: `filter_result = result.to_json()`, status, in one statement.
- `set_status(tender_id, status)`: `attempts = 0`, `failed_step = null`, `error = null`; refuses (no-op) when the row is `cancelled` (documents.md D8).
- `record_failure(tender_id, step, error)`: `status = 'failed'`, `failed_step = step`, `error = error[:200]`, `attempts = attempts + 1` when `failed_step` is the same step, else 1; returns the new count. No-op for cancelled.
- `save_assessment(...)`: one transaction: set `is_current = false` on the tender's current row of that stage, insert the new row with `is_current = true`, `output = output.model_dump(mode="json")`, `schema_version` = a constant `SCHEMA_VERSION` in `pipeline/schemas.py` (add it: `"2026-11-14"`), `run_id`. Returns the ID.
- `current_assessment(tender_id, stage)`: `(id, model)` where `model` is validated back into `ScreeningOutput`, `ExtractionOutput` or `MatchingOutput`. A row that no longer validates (schema changed) raises `StaleAssessment`, which the job treats as "redo the step".
- `save_memo(tender_id, result, extraction_id, matching_id)`: one transaction: `version = coalesce(max(version), 0) + 1`; the previous current memo `is_current = false`; insert with `content`, `rendered_md = render_markdown(result.memo)` (B7), `recommendation`, fit columns, citation counts, `prompt_version`, `model`; set the tender `status = 'memo_ready'` in the same transaction.
- `docs(tender_id)`: `DocumentStore.docs_view`.
- `human_scores(tender_id)`: from `memo_human_inputs` into `HumanScores` (scores and the rationale only; never `updated_by`, owners or milestones).
- `cost_today()`: B3.
- `contact_rows(tender_id)`: `select org_name, contact_name from contacts where tender_id = :id` (the only SELECT on `contacts` besides retention; rule 2).

**Tests** (`db` marker; the contract suite from pipeline.md P13 runs against `MemoryStore` and this store):

- Lock: a second `lock()` while the first is held yields false; after the first exits, true again.
- Status and failure counting: failure on `extract` twice → attempts 2; then a failure on `match` → 1; `set_status` resets.
- Cancelled rows are never returned and never changed by `set_status`, `record_failure` or `save_memo` (save raises `TenderCancelled`, which the job treats as a skip).
- Assessments: two saves for one stage → one current; the older one still readable by ID; `StaleAssessment` for a row with an invalid output.
- Memos: versions 1, 2; only the latest current; the tender is `memo_ready` after the save, in the same transaction (a failure in render rolls back both).
- `human_scores` returns no owner or `updated_by` field (check the dataclass fields).
- Rule 2 test: the only SQL text containing `contacts` in `db/` is in `contact_rows` and `retention.purge_contacts` (and the migration).
- The full pipeline.md P14 happy-path test runs once more against this store (database tests only).

**Done when.** Tests pass in CI, and `jobs/daily.py` `main` builds this store from `DATABASE_URL`.

---

### B7. Memo rendering (`pipeline/memo_render.py`)

**Function.**

```python
LIMITATION_TEXT: dict[str, str]          # pipeline.md PD8 codes -> one English sentence
RECOMMENDATION_LABEL = {"bid": "BID", "bid_conditional": "BID ONDER VOORWAARDEN",
                        "no_bid": "NO-BID", "needs_review": "TER BEOORDELING"}

def source_tag(citation: Citation, tags: Mapping[int, str], page_kinds: Mapping[int, str]) -> str
def render_markdown(memo: Memo) -> str
```

**Rules.** Sections 1–8 of `docs/MEMO_TEMPLATE.md` in order, with the tables there. `[human]` cells are left blank (they live in `memo_human_inputs`). Every cited value carries a source tag `[<tag> p.<page>]` (`slide`, `sheet`, `section` instead of `p.` by `page_kind`), built from `documents_reviewed`; an unverified citation is tagged `[<tag> p.<n>, niet geverifieerd]`. The submission deadline is tagged `[TenderNed API]`, or "unknown". A missing value is *Niet gevonden in stukken*. Limitation codes are turned into sentences with `LIMITATION_TEXT`; a code with a `:` suffix keeps the suffix (`filter_unknown:country` → "The country rule could not be checked: country."). The renderer reads only the `Memo`: no database, no contacts. Text from the memo is escaped for Markdown table cells (`|` and newlines).

**Tests.** A full synthetic memo renders with all eight section headings; an unverified citation shows `niet geverifieerd`; `deadline_at` null shows `unknown`; every code in pipeline.md PD8 has a sentence (test fails on a missing one); a cell with `|` does not break the table (count the columns per row).

**Done when.** Tests pass. (The UI may render the `Memo` itself later; `rendered_md` is the stored copy.)

---

### B8. Tool backend over the database (`db/tool_backend.py`)

**Class.**

```python
class DbToolBackend:                     # implements agent.tools.ToolBackend
    def __init__(self, engine: Engine, *, searcher: Searcher, kb: KbSource) -> None
    def get_tender(self, tender_id: int) -> dict[str, Any]
    def search_tender_docs(self, tender_id, query, doc_types, top_k) -> dict[str, Any]   # delegates to searcher (search.md)
    def read_document_pages(self, tender_id, document_id, page_from, page_to) -> dict[str, Any]
    def search_company_kb(self, query, doc_types, top_k) -> dict[str, Any]              # delegates to searcher
    def get_company_facts(self, topics) -> dict[str, Any]                               # kb.facts(topics)
```

**Rules.**

- `get_tender` returns the shape of `agent/fakes.FakeBackend.get_tender`: `tender_id`, `title`, `buyer_name`, `procedure_type`, `deadline_at` (ISO, Amsterdam; `null` when unknown) with `deadline_source: "TenderNed API"`, `deadline_has_time`, `estimated_value`, `currency`, `cpv_codes`, `main_nature`, `lots` (`lot_identifier`, `title`, `estimated_value`, `duration_months`). It never selects `tenderned_detail`, `raw_notice`, `eforms_gt` (ground truth) or anything from `contacts`.
- `read_document_pages`: the document must belong to `tender_id`, else `ToolError("Document <id> is not part of this tender.")`; `notice` documents are refused the same way (they are never read, 2.10); returns `{"document_id", "filename", "doc_type", "page_kind", "pages": [{"page", "label", "text"}]}`; pages out of range are simply absent.
- The tool layer (`agent/tools.run_tool`) still applies the contact-key guard and the 20,000-character limit; the backend does not repeat them.
- `pipeline.md` P13 `BackendFactory` = `lambda tender_id: DbToolBackend(engine, searcher=..., kb=...)`.

**Tests** (`db` marker): `get_tender` keys equal the fake's keys plus `deadline_source`, `deadline_has_time`, `main_nature`; no ground-truth or contact key (`contact_keys(result) == []` and a check that no value equals a planted contact); another tender's document → `ToolError`; a notice document → `ToolError`; pages 3–4 of a 3-page document → page 3 only.

**Done when.** Tests pass in CI.

---

### B9. Retention (`db/retention.py`)

**Functions.**

```python
def purge_contacts(conn, today: date) -> int                       # Q14 / documents.md DD9
def purge_documents(conn, root: Path, today: date) -> int          # DB6
def purge_payloads(store: PayloadStore, today: date) -> list[str]  # existing 30-day rule, called daily
```

**Rules.** `purge_contacts`: delete rows with `purge_after < today`. `purge_documents` (DB6 as recommended): for tenders past the retention date, not `in_snapshot`, without an `approvals` row `approved`: delete their files under `DOCUMENTS_DIR/<tender_id>/` (only files whose name is `<sha256>.<ext>` and that are recorded in `documents.storage_path`; never follow symlinks), then delete `document_pages` and `chunks` rows; keep `documents` rows with `storage_path = null` and `extraction_status` unchanged for audit. All three are called at the end of the daily job.

**Tests.** Contacts past the date deleted, others kept. Documents: a tender closed 200 days ago purged (files gone, pages and chunks gone, document rows kept); one with an approved memo kept; an `in_snapshot` one kept; a symlink in the folder is not followed (the target outside `tmp_path` survives); a file not in `documents.storage_path` is left alone.

**Done when.** Tests pass; the daily job report shows the purge counts.

---

### B10. Compose and deployment wiring

**Changes.** `docker-compose.yml`: add `worker` (same image as `app` plus LibreOffice per documents.md, `command: python -m jobs.daily`, `profiles: ["job"]` so `make up` does not run it, volume for `DOCUMENTS_DIR` and `NOTICE_DIR`). `app` and `worker` run `python -m db.migrate` before their command. `Dockerfile` installs from `uv.lock` (`NEXT.md` 14). A `pg_dump` script `infra/backup.sh` writing `data/backups/<date>.sql.gz` (git-ignored), called by host cron after the job (2.9). `make ingest` runs `docker compose run --rm worker` when Docker is used, else the local command.

**Tests.** None automated beyond a compose file lint (`docker compose config` in CI, when the CI image builds). Manual check (Arthur, when Docker is installed): `make up`, `make migrate`, `make ingest` with `DAILY_STAGE2_CAP_USD` unset runs Stage 1 only.

**Done when.** `docker compose config` is valid; the manual check passes.

## 4. Risks and edge cases

| Source | Risk or edge case | Handled in |
|---|---|---|
| 2.8, CLAUDE.md privacy | `contacts` joined into model input by accident. | Rule 2 test; no ORM relationships (DB3); `DbToolBackend` selects explicit columns |
| 2.8 | Contact data in raw TenderNed JSON or TED XML. | B4 asserts stripped input before writing |
| 2.2 | Two daily runs at once. | B6 advisory lock; B2 migration lock |
| 2.2 | Upsert key is the procedure, not the notice; late or out-of-order notices. | B4 version rules |
| 2.2 | Retry from the failed step, three attempts. | B6 `record_failure` counting per step |
| 2.2 | Budget cap needs today's cost, including failed calls. | B3: every gateway record is written, failed ones too; autocommit |
| 2.10 | `is_current` tie rule and the partial unique index. | B5 one-statement recompute |
| R3 | One closing-date parser; date without time. | B4 `deadline_has_time`; `parse_sluitingsdatum` only |
| R8 | `backoff_ms` column. | B1 `llm_calls` |
| R11-like | A used migration must never change. | B2 hash check |
| B12 | Documents missing. | B5 `docs_view` feeds `documents_missing` unchanged |
| Q14, DB6 | Retention of contacts, payloads, documents. | B9 |
| Q16 | `approvals.decided_by` is a username from one shared login. | Column created; meaning stated in the UI plan |
| DB7 | A stale memo during re-analysis. | Old memo current until replaced; status shown |
| Schema drift | Stored assessments that no longer validate. | `StaleAssessment` → the step is redone |
| Time zones | Cost per "today" across DST changes. | B3 tests in summer and winter time |
| NUL bytes | Postgres rejects `\x00` in text. | B5 assertion behind documents.md `clean_text` |

## 5. What is still open after this plan

- DB1 to DB9.
- The UI and CRM plans (they use `memo_human_inputs` and `approvals`, created here).
- `ARCHITECTURE.md` section 3 must list the added columns and the new table once approved.
