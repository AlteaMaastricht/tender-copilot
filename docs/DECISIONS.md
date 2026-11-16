# Decisions log: Tender Copilot

Each entry records a question from `docs/ARCHITECTURE.md` section 8, the answer, its status and the evidence behind it. Statuses:

- **Decided**: chosen by Arthur. Changing it needs a new entry.
- **Proposed**: default from the plan, not yet confirmed. Confirm or change before Phase 2.
- **Open**: needs a decision or external input.

Never record credentials here. Keep evidence (emails, screenshots, terms pages) in a private folder outside the repo, and link to it by date.

---

## Scope and plan decisions (decided, 13 November 2026)

| # | Decision | Status | Date | Notes |
|---|---|---|---|---|
| S1 | Documents come from TenderNed from day 1. TED provides discovery and eForms fields. Other platforms are flagged `other_platform`. | Decided | 13 Nov 2026 | TED does not host documents. Mercell is about 28% of IT contract notices in the 2025–26 open data, so this limit is material. |
| S2 | Stage 2 is a fixed pipeline (extract, match, memo) with typed outputs. The agent loop is used only for requirement matching and chat. | Decided | 13 Nov 2026 | Easier to evaluate step by step. |
| S3 | Scope is the Netherlands only, with Dutch and English documents. No German retrieval evals. | Decided | 13 Nov 2026 | Replaces the earlier NL + BE + DE plan. Update `COMPANY.md` sections 4 and 5. |
| S4 | AWS managed services are cut. Deployment is one VM with docker compose and cron. | Decided | 13 Nov 2026 | An AWS account is still needed for the deployment story. Keep a billing alarm. |
| S5 | Kept: three-provider comparison, LLM-judge memo eval, per-tender chat. | Decided | 13 Nov 2026 | Chat has no write tools. |
| S6 | Week-1 checkpoint: if the TenderNed fetcher has not downloaded documents for 20 test tenders by Friday 20 November, switch v1 to a manual snapshot. | Decided | 13 Nov 2026 | Record as a README limitation if triggered. |

---

## Open questions from the architecture

### Access and terms

| # | Question | Status | Decision / answer | Evidence |
|---|---|---|---|---|
| Q1 | Can documents be downloaded without an account, and do the terms allow automated download and storage? | Decided: documents accessible; terms confirmation pending | Documents and publication details are accessible anonymously through the TenderNed API. PDF downloads work. Automated download and storage are permitted for this project (confirmation from account holder, 13 Nov 2026). `public-xml` returns 403 with credentials, so the account needs checking. | Probe run 13 Nov 2026 on publications 406911, 406922, 406921, 430496, 430969 and 428144. Probe output is kept outside git. Email to functioneel beheer sent on [date]; reply on [date]. |
| Q2 | May the frozen snapshot (documents and text) be kept in a public repo? | Proposed | Keep the repo public with code, notice IDs, the download script and evaluation labels. Do not commit documents, extracted text, or the probe output. | Documents can name contact persons. Chosen to avoid personal data and copyright issues. |
| Q3 | If fewer than about 100 TenderNed-hosted tenders match in 8 weeks, widen the date range or CPV list? | Decided: threshold met | About 1,170 TenderNed-hosted IT and business-services contract notices from July 2025 to June 2026, about 100 a month. No widening needed. | Counts from the open-data export: 1,174 TenderNed-hosted, 460 Mercell, 28 Tenpu. CPV 79 includes non-IT services, so the relevant count is lower. |

### Models and retrieval

| # | Question | Status | Decision / answer | Evidence |
|---|---|---|---|---|
| Q4 | Models for Stage 1 and Stage 2, and the third provider? | Proposed | Stage 1 (screening): Claude Haiku. Stage 2 (extraction, matching, memo): Claude Sonnet. Comparison: OpenAI and Mistral (EU-hosted). | Mistral strengthens the transfers paragraph and is named in the job description. Model IDs and prices to be confirmed in Phase 3. |
| Q5 | Embeddings: local BGE-M3 or hosted API? | Proposed | Local BGE-M3. | Free, multilingual (Dutch and English), no extra data transfer. Dimension 1024 matches the schema. |
| Q6 | Scanned PDFs: flag only, or OCR? | Proposed | Flag only (`extraction_status = scanned`). OCR is a stretch goal. | Saves time in v1. |
| Q7 | Tables: `pymupdf` text, or table extraction? | Open | To decide after testing on the probe PDFs, especially the award-criteria weights. | Test on the Inschrijvingsleidraad from 406922 and the EF16 notices. |

### Product behaviour

| # | Question | Status | Decision / answer | Evidence |
|---|---|---|---|---|
| Q8 | One memo per tender, or per lot? | Open | Value rule (hard filter) checks per lot: lot values first, the procedure value (BT-27) only when no lot has a value, no value at all gives `unknown` with a flag. The memo question (one memo per tender or per lot) is still open. | |
| Q9 | Fit score: formula in code, and weights? | Decided | Formula in code: Score = sum(weight x score) / 5 over the seven dimensions and weights of `MEMO_TEMPLATE.md` section 3. The model scores four dimensions (1 to 5, each with rationale and source); a human scores three. An unscored dimension counts 1 for the minimum and 5 for the maximum. Bands: 70 or more bid, 50 to 69 bid with conditions, under 50 no-bid, applied after the knock-out rules. A range that crosses a band gives `needs_review`. | `ARCHITECTURE.md` 5.4; `pipeline/memo.py`. |
| Q10 | Memo language: Dutch, English, or the tender's language? | Decided | Always English. Questions for the Nota van Inlichtingen stay Dutch. | Decided 13 Nov 2026. |
| Q11 | Tenders above EUR 2M: drop, or keep with "consortium only"? | Decided | Keep, with a 'consortium only' flag (hard filter: outcome pass, flag `consortium_only`). | Matches `COMPANY.md` section 4. |
| Q12 | Tenders per day at Stage 2, and the daily cost cap? | Open | | Set after the Phase 3 cost measurements. |
| Q13 | Should "unsure" screening results always be shortlisted? | Decided | Yes, always. No confidence threshold. `confidence` is used for ranking only. | Stage 1 is tuned for recall. Decided 14 Nov 2026. Revisit only if the screening eval shows that most "unsure" tenders are irrelevant. |

### Privacy and compliance

| # | Question | Status | Decision / answer | Evidence |
|---|---|---|---|---|
| Q14 | Retention for `contacts` and stored model payloads? | Model payloads: Decided. Contacts and documents: Proposed | Model payloads: 30 days, redacted, and stored only when `LLM_PAYLOAD_DIR` is set (off by default). Contacts: deadline plus 6 months. Downloaded documents: deleted after evaluation. | Short periods, easy to defend in the README. Payload part decided 14 Nov 2026. |
| Q15 | Is best-effort redaction of names in document text acceptable? | Proposed | Yes, with a README limitation. Add named-entity detection later if time allows. | Names that appear only in document body text are not caught. |
| Q16 | Demo UI users: one shared login, or named users? | Proposed | One shared login for the demo. `approvals.decided_by` records the username. | Named users are a stretch goal. |

### CRM and deployment

| # | Question | Status | Decision / answer | Evidence |
|---|---|---|---|---|
| Q17 | HubSpot deal fields and stage; record rejected memos? | Open | | |
| Q18 | Hosting provider and region; live deployment or recorded demo? | Proposed | One small EU VM (Frankfurt or Ireland). A recorded demo is the fallback if the live deployment is not ready when applying. | Keeps costs and the AWS setup small. |

---

## Data findings that change the design

| Date | Finding | Consequence |
|---|---|---|
| 13 Nov 2026 | Closing dates in the open-data spreadsheet do not match the API. Example: 430496 shows 2026-11-20 in the spreadsheet and 2026-09-11 in the API. | The daily job uses the API field `sluitingsDatum`. The spreadsheet is used only for historical analysis. |
| 13 Nov 2026 | The spreadsheet's `Aankondiging gegunde opdracht` rows are award notices, not open tenders. The open-tender type is `Aankondiging opdracht` in the spreadsheet and `Aankondiging van een opdracht` in the API. | Use exact type matching when selecting tenders. |
| 13 Nov 2026 | Some closing dates are placeholders (for example 2099-12-31). | Treat as `unknown` in the hard filter. |
| 13 Nov 2026 | Document formats include PDF, XLSX, PPTX, legacy DOC and nested ZIP. | Extend extraction and record unsupported formats. |
| 13 Nov 2026 | The same document name appears several times, for example several EF16 copies and several Q&A answers. | Keep every version. Mark the newest by `datumPublicatie`. |
| 13 Nov 2026 | Deadline check on 10 snapshot publications: 8 matched. The 2 mismatches go in opposite directions: 409819 spreadsheet 2026-03-09, API 2026-03-20; 408259 spreadsheet 2026-03-06, API 2026-04-28. Earlier example 430496 also went the other way (API earlier). | The spreadsheet closing date is not reliable in either direction. Evaluation labels use the API `sluitingsDatum`. The cause of the 2 mismatches is not yet confirmed; check `publicatieIDLaatsteRectificatie` on both before explaining them. |

## Evaluation snapshot (13 Nov 2026)

| Item | Value | Status |
|---|---|---|
| Candidate pool | 589 TenderNed-hosted candidates from the open-data database: CPV 72 (220), CPV 79 (293), CPV 48 (76). Not committed. | Done |
| Selection | 150 publications: 50 CPV 72, 30 CPV 48, 70 CPV 79 | Done |
| Seed | `20261113` | Done |
| Sampling | Within each CPV group, round-robin across 2026 publication months. The seed decides which months receive the remainder. | Done |
| Output | `data/snapshot_selection.csv` holds all 589 candidates with a `selected` column. `data/snapshot_candidates.csv` is the same table without `selected` (redundant). `data/snapshot_candidates_raw.psv` is the original sqlite output, git-ignored. | Done |
| Placeholder closing dates | 1 row has 2099-12-31, flagged `closing_placeholder = true`, not selected. 6 candidates have an empty closing date; 1 of them (431048) is selected. Its deadline must come from the API. | Open |
| Reproducibility | The selection script is in a scratch folder outside the repo. Commit it as `scripts/snapshot_select.py` before citing the snapshot. | Open: needs approval to commit |
| Deadline check | `data/snapshot_deadline_check.csv`: 10 publications checked against the detail endpoint. 8 matched. The check made 11 detail requests (one extra, for 430496, to confirm the field name). No documents downloaded. | Done. Full check of all 150 pending decision. |

Monthly counts (2026):

| CPV group | Total | Jan | Feb | Mar | Apr | May | Jun |
|---|---|---|---|---|---|---|---|
| 72 | 50 | 9 | 8 | 8 | 8 | 8 | 9 |
| 48 | 30 | 5 | 5 | 5 | 5 | 5 | 5 |
| 79 | 70 | 12 | 12 | 11 | 11 | 12 | 12 |

---

## Review log

| Date | Reviewer | Change |
|---|---|---|
| 13 Nov 2026 | Arthur | Initial log created. Scope decisions S1–S6 recorded. |
| 13 Nov 2026 | Arthur | Snapshot selection recorded: seed 20261113, mix 50/30/70, deadline-check result. Data finding on spreadsheet closing dates added. |
| 13 Nov 2026 | Arthur | Q11 decided; Q8 note for the value rule; hard filter rule scope recorded. |
| 13 Nov 2026 | Arthur | Q9 and Q10 decided. Schema, citation verifier and recommendation decisions B1 to B12 recorded. |
| 14 Nov 2026 | Arthur | `eu_procedure` source decided; confidence rule proposed; KB-evidence gap and inbox order recorded. |
| 14 Nov 2026 | Arthur | LLM gateway decisions recorded; Q14 payload retention decided. |
| 14 Nov 2026 | Arthur | Agent loop decisions recorded: limits, stop reasons, `ChatAnswer`. |
| 14 Nov 2026 | Arthur | Q13 decided. Prompt decisions recorded: seven v1 prompts, two memo calls, KB pre-fetch, model-facing memo schemas, map-step schema. |
| 14 Nov 2026 | Arthur | Formal knock-outs are not matched (`action_required`); `get_company_facts` returns the `kb_id` of each fact. |
| 15 Nov 2026 | Arthur | Eval runner decisions recorded: snapshot as files, composite plus gate, one results file per date and commit, runner built before the pipeline steps. |

---

## Dependencies (13 November 2026)

Rule applied: new dependencies need Arthur's approval; releases must be at least two weeks old. The lock file resolves only releases before 30 October 2026 (`[tool.uv] exclude-newer` in `pyproject.toml`).

| Package | Version | Status | Reason |
|---|---|---|---|
| fastapi | 0.162.0 | Approved | App service and `/health` endpoint. |
| uvicorn | 0.54.0 | Approved | ASGI server in Docker. |
| pydantic | 2.14.2 | Approved | Schema validation. Pinned to 2.14.2 (uploaded 29 Oct 2026), not 2.14.3 (uploaded 5 Nov 2026, under two weeks old). |
| pytest | 9.2.0 | Approved (dev) | Test runner. |
| ruff | 0.17.3 | Approved (dev) | Lint and format. |
| httpx | 0.28.1 | Approved (dev) | Test client for FastAPI. Switch to httpx2 deferred to the agent-loop tests. |
| setuptools | 84.2.1 | Approved (build only) | Build backend. |
| opentelemetry-api | 1.45.2 | Approved (transitive) | Pulled in by FastAPI. Not a direct dependency. |
| uv | 0.13.6 | Approved (tool) | Creates `.venv` from `uv.lock`. Installed on Arthur's Mac and in CI. |
| anthropic | 2.3.0 | Approved | Anthropic provider SDK for the LLM gateway, imported only in `llm/providers/anthropic.py`. Uploaded 23 Oct 2026; newest release before the lock cutoff. Sends API version `2026-10-21`. Approved 14 Nov 2026. |
| mistralai | 3.6.0 | Approved | Mistral provider SDK for the LLM gateway, imported only in `llm/providers/mistral.py`. Uploaded 28 Oct 2026; newest release before the lock cutoff. Approved 14 Nov 2026. |

The two SDKs added 13 transitive packages to `uv.lock` (14 Nov 2026), all uploaded before the 30 October 2026 cutoff.

Lock file: `uv.lock` is approved and generated. Commit it with the skeleton, not separately. The Dockerfile still installs with pip and does not use the lock yet.

Docker: not yet tested (Docker is not installed on this Mac). Run `make up` locally before relying on it. CI does not build the image yet.

---

## eForms identifiers (13 Nov 2026)

Every BT and OPP identifier in ingest/ted.py is from memory and unverified. Each is marked “(confirm)” in the code. Verify all of them against the eForms SDK version used by the notices in the Phase 0 notebook, before any eval relies on them.

---

## Hard filter rules (13 Nov 2026)

`pipeline/filter.py` implements five rules: country, CPV, deadline, value and notice type. Main nature (BT-23) is a flag `nature_supplies`, not a fail, because software packages (CPV 48) are often classed as supplies. Security clearance (BT-578) is deferred until the parser reads it. Working days count Monday to Friday; Dutch public holidays are not counted.

| 13 Nov 2026 | public-xml returns 200 with credentials (430496, 430969). The earlier 403 came from swapped user and password in .env. TenderNed returns 403, not 401, for wrong credentials. | Q1: XML access confirmed. Drop the public-xml question from the TenderNed email; keep the terms question. Don’t read a 403 as a permission problem. |

---

## Schemas, citation verifier and recommendation rules (13 Nov 2026)

Decisions taken while planning `pipeline/schemas.py`, `pipeline/citations.py` and the recommendation rules in `pipeline/memo.py`. All decided by Arthur.

| # | Decision |
|---|---|
| B1 | Schemas live in `pipeline/schemas.py`. The citation verifier lives in `pipeline/citations.py`, because chat needs it too. |
| B2 | Four recommendation values: `bid`, `bid_conditional`, `no_bid`, `needs_review`, with the bands of the memo template. |
| B3 | Code decides the recommendation; the model proposes none and writes only the one-sentence reason. Knock-out rules first, then the fit score over its provisional range. A range across a band gives `needs_review` with `awaiting_human_scores`, and is recomputed when the human scores are entered. |
| B4 | `not_met` with no mitigation gives `no_bid` and wins over `unclear` elsewhere. If that knock-out's own citation is unverified, `needs_review`. A closable gap (`consortium`, `subcontractor`, `third_party`, `obtain_before_deadline`) caps the result at `bid_conditional`; the fit score can still lower it. `ask_in_qa` gives `needs_review`, because the outcome depends on the buyer. |
| B5 | A citation is checked on the cited page only, for v1. Quotes across a page break fail and are counted as false negatives in the evals. |
| B6 | `Memo` is the stored shape. The model-facing draft schema is defined before the memo prompt. |
| B7 | Code overwrites the memo's submission deadline from `tenders.deadline_at`; null shows as "unknown". A difference from the extracted value adds `deadline_differs_from_documents` to `limitations`. |
| B8 | `RequirementMatch` validators: `met` needs evidence, `unclear` needs `unclear_reason`, `not_met` needs `gap`. The downgrade lives in `pipeline/match.py` and uses `no_evidence_found`. |
| B9 | Code builds `Memo.citations` as the de-duplicated union; counts come from it. |
| B10 | A quote needs at least 10 letters or digits after normalisation. |
| B11 | A knock-out with no match gives `needs_review`. |
| B12 | Documents are missing when the status is `docs_unavailable` or every document is scanned or unsupported. A rule for a missing key document comes after document classification (Day 8 to 9). |

Also decided:

- Each key date has its own citation.
- The exclusion flag `none` excludes real flags.
- Requirement identifiers are `KO-01` everywhere.
- KB evidence quotes will be verified against the company KB later.
- A copilot fit dimension with no usable source is unscored and treated as a range, like an unscored human dimension.
- `memos.fit_score` is nullable, with a provisional minimum and maximum.
- `[human]` memo items get their own table (`memo_human_inputs`), built on Day 7.

Added 14 Nov 2026:

| Item | Status | Decision |
|---|---|---|
| `eu_procedure` | Decided | From TenderNed `nationaalOfEuropeesCode` on the publication detail (`tenders.tenderned_detail`). Null when there is no TenderNed match. Code values and their mapping to true or false still to confirm. |
| `Memo.confidence` | Proposed | Set by code after the recommendation, never by the model, and not an input to the recommendation. Rule below. Not built. |
| Inbox order while `fit_score` is null | Open | Decide when the Streamlit UI is built. |

Proposed confidence rule:

| Value | When |
|---|---|
| `low` | Any one of these: documents missing; no knock-outs extracted; any knock-out citation unverified; a quarter or more of `Memo.citations` unverified; an extraction warning about a key document (scanned, token budget reached) |
| `high` | All of these: every non-formal knock-out matched with both citations verified; no knock-out `unclear`; all four copilot fit dimensions scored; every citation in `Memo.citations` verified; no extraction warnings |
| `medium` | Everything else |

The quarter threshold is a first guess, to be set from the evals.

Known gaps:

- KB evidence counts as a source for a copilot fit score without being verified. `compute_fit` drops a score only when its sole source is an unverified tender citation. Stays a gap until KB evidence is verified against `company_kb`.

Still open: the inbox order while `fit_score` is null (until the UI); confirmation of the confidence rule; Q8 (memo per tender or per lot).

---

## LLM gateway (14 Nov 2026)

| Topic | Decision |
|---|---|
| Structured output | The JSON schema goes into the instructions and the answer is validated in code, with exactly one retry that returns the validation errors. No native structured output in v1, so every provider and every schema (including the recursive `AwardCriterion`) uses the same mechanism. Native structured output is a candidate for the measured improvement in Phase 3. |
| Name redaction | Full name, initial plus surname, and surname after a title. A bare surname is not redacted, because surnames can be ordinary words. |
| Payloads | Off by default; redacted; 30 days (Q14). |
| Refusals | No server-side fallback. A refusal is logged with status `refused` and raised, so every logged call was served by the model that was asked for. |
| Network retries | Done by the gateway, not the SDKs: at most 3, exponential backoff with jitter, `retry-after` honoured up to 60 s. Separate from the validation retry. |
| Models | Q4's "Claude Haiku" and "Claude Sonnet" are read as `claude-haiku-5-5` and `claude-sonnet-5-5`. A model without a price row cannot be called. |
| Prices | From platform.claude.com/docs/en/about-claude/pricing, read 14 Nov 2026. Haiku 5.5 charges a higher rate for a prompt over 100,000 tokens. |
| Keys | `APP_ANTHROPIC_API_KEY` and `MISTRAL_API_KEY`, from the environment only. |
| Run ID | Every call carries a run ID that must be a random UUID (`uuid4`), never a date, a counter or a name-based UUID. The tag around untrusted text is derived from it, so a document author cannot predict the tag. `CallContext` rejects any other run ID; `CallContext.new_run()` creates one. |
| Sampling | The current API rejects `temperature`, so runs cannot be made deterministic that way. Evals must allow for run-to-run variation. |

Open:

- Mistral model IDs and prices are unconfirmed (Q4, Phase 3). Read on 14 Nov 2026: Mistral Large 4 at USD 0.50 in and 1.50 out; Mistral Small 4 at 0.15 and 0.60, order not labelled.
- Q15 (best-effort name redaction) is still Proposed; the gateway follows it.
- Gateway prompt blocks live in `prompts/gateway_*_v1.md`. Their version is not logged per call; only the task prompt's is.

---

## Agent loop (14 Nov 2026)

| Topic | Decision |
|---|---|
| Token budget | 60,000 tokens per matching run and 80,000 per chat question, counted as prompt plus output tokens over all steps. Tunable constants in `agent/loop.py`; to be adjusted from the Phase 3 measurements. |
| Steps | 6 per requirement, 8 per chat question. |
| Tool errors | Returned to the model as error results. The third error in a run stops it. |
| Stop reasons | `RequirementMatch.unclear_reason` gains `token_budget` and `loop_failed`. A run that stops without an answer becomes an `unclear` match built in code, so the tender is `needs_review`. |
| Matching wrapper | `pipeline/match.py` runs one loop per knock-out and owns the `met`-without-evidence downgrade (B8). |
| Chat | Reuses the same loop with four tools and a `ChatAnswer` (answer, citations, KB evidence, `in_scope`). |
| Tools | The five read tools of `ARCHITECTURE.md` section 4, with fake implementations (`agent/fakes.py`) until the database exists. A call can only read the run's own tender. No tool may return contact data; a guard (English and Dutch key names, including a name under a person-like parent) and tests enforce it. |

Candidate for the measured improvement in Phase 3: prompt caching for agent steps. Every step resends the system prompt, the tool definitions and the whole conversation, so most prompt tokens of steps 2 to 6 are repeats. The gateway sends no cache settings yet; the log already has `cached_tokens` to measure the effect.

Open:

- A real model's resistance to injected instructions is untested; the tests only show that the loop gives such text no power. It belongs to the `edge_cases` eval suite.

Whether a real model ignores injected instructions is tested in the Phase 3 edge cases.

---

## Prompts (14 Nov 2026)

Seven v1 prompt files in `prompts/`, tied to their stage, schema and trusted variables in `pipeline/prompt_specs.py`. No prompt has been run against a model yet.

| Topic | Decision |
|---|---|
| Files | `screen_v1`, `extract_map_v1`, `extract_reduce_v1`, `match_v1`, `memo_v1`, `memo_reason_v1`, `chat_v1`. |
| Trusted and untrusted | A prompt variable holds only company profile text or a value that code produces (`tender_id`, `recommendation`, `reason_codes`). Tender text, notice text, extraction results and tool results are always passed as data. |
| Company sections | Screening gets `COMPANY.md` sections 2, 4 and 5. The memo gets 2, 4, 5 and 9. Matching and chat reach sections 3, 6 and 7 only through the KB tools. Extraction gets none. |
| Screening (Q13) | Tuned for recall: `reject` only when the notice itself shows a will-not-bid item as the main subject, or no service line can apply. `unsure` is always shortlisted. The prompt defines `confidence` as the likelihood that Datalink would want to read the documents; ranking only. |
| Screening input | The flags of the hard filter are passed as data, so the model does not re-check country, CPV, deadline or value. |
| Memo calls | Two. `memo_v1` returns `MemoDraft`. Code decides the recommendation. `memo_reason_v1` gets the decided value and reason codes and returns `MemoReason`. |
| Memo draft | Only `[copilot]` items that extraction and matching have not already produced. Those are carried over by code. No `[code]` or `[human]` item and no contact field; `Citation.verified` is the one exception. |
| KB for the memo | Code pre-fetches KB passages (no CVs) and company facts and passes them as data. `MatchingOutput.award_notes` stays empty in v1; award positions are in the draft. |
| Citations | Only the map step and the two agent loops write new quotes. The reduce step and the memo calls copy citations from their input; any other citation is unverified. |
| Language | English output. Questions for the Nota van Inlichtingen are Dutch. Chat answers in English, also to a Dutch question. |
| Examples | Synthetic only: no organisation or person names, no real tender wording, IDs from 9001. Tests check this against the TED fixtures. |
| Map step | New schema `ExtractionPartial` with `KnockoutFinding`. After the reduce call, code overwrites `documents_read`, `documents_skipped` and the requirement IDs. |
| Schema changes | `contract_risks` is `list[CitedText]`, so a risk has a source. `KeyDates.qa_publications` added. |
| Formal knock-outs | **Decided 14 Nov 2026.** Knock-outs of kind `formal` are not matched: `match_requirement` refuses them and `match_v1` is never sent one. They appear in the knock-out table with status `action_required` (`KnockoutRow` only; a model can never produce it), no evidence and mitigation `none`. `decide_recommendation` ignores them: only formal knock-outs counts as `no_knockouts_found`, and a match for a formal knock-out counts as `match_without_knockout`. This narrows B11 to non-formal knock-outs. Reason: a formal requirement has no company evidence, so it was always `unclear` and made every tender `needs_review`. |
| Facts carry their source | **Decided 14 Nov 2026.** `get_company_facts` returns each fact as `value` plus the `kb_id`, `doc_slug` and `text` of the KB passage it was taken from (a list of such facts for certifications and references). The tool description says so (`ARCHITECTURE.md` section 4). `match_v1` cites the fact directly; the extra `search_company_kb` step is removed. The database backend must keep this shape: a fact without a `kb_id` cannot be cited. |

Open:

- `contract` documents are not read in full (section 2.5), so liability and penalty clauses reach the memo only when the leidraad repeats them.
- `KeyDateTime` cannot hold a date without a time; v1 writes 00:00 with a warning.
- CPV labels for the screening input need a lookup table.
- The code that builds each prompt's input (`pipeline/screen.py`, `pipeline/extract.py`, the memo calls and assembly, the KB pre-fetch) is not built.

---

## Evals (15 Nov 2026)

`evals/run.py`, its metrics, the label formats and the results file. Details and exact formulas are in `docs/EVALS.md`. No eval has been run against a model yet.

Decided by Arthur on 15 Nov 2026:

| Topic | Decision |
|---|---|
| Snapshot | The runner reads files in `data/snapshot/`, checked against a manifest with a SHA-256 per file. Not the database. The snapshot hash and the hash of each label file go into every result. |
| Knock-out recall "weighted heaviest" | Both: a composite score in which knock-out recall has the largest weight, and a gate with a minimum knock-out recall. A failed gate marks the run not recommended whatever the composite is. |
| Results file | `evals/results/<date>_<commit>.json` holds a list of runs. A run is appended; earlier runs are never changed. |
| Build order | Runner, metrics and loaders are built now. An eval whose pipeline step does not exist reports `not_built` and names the module it waits for. |
| Tests | Synthetic mini snapshot and labels under `tests/fixtures/evals/`, stub steps and the fake provider. No live call. |

Proposed (in the code, to confirm):

| Topic | Proposal |
|---|---|
| Composite weights | Knock-out 0.30, extraction 0.20, screening 0.20, retrieval 0.10, memo 0.10, edge cases 0.10, in `evals/weights.toml`. |
| Screening value in the composite | F2, because Stage 1 is tuned for recall. Precision and recall are the headline. Recall at a fixed precision is reported only with `--min-precision`. |
| As-of date | The hard filter gets the day after publication as "today", because every snapshot tender has closed. An edge-case label can set its own date. |
| Knock-out match rule | Same document, page within 1, same category, one-to-one. Formal and non-formal requirements are matched apart: a real knock-out extracted as a formal action does not count as found. |
| Lot titles | Graded only where the notice has two or more lots. |
| Memo eval tenders | The tenders that have knock-out labels. |
| Generated memos | Saved under `--memo-dir`; the command line defaults to `data/eval_memos/` (git-ignored, E4). |
| Extraction overrides | Tender level only: deadline, lot count, estimated value, framework maximum. |
| Dry run | 3 characters per token and fixed token figures per stage, listed in `docs/EVALS.md` section 6. To be replaced by measured values after the first real run. |
| Makefile defaults | `STAGE1 = claude-haiku-5-5`, `STAGE2 = claude-sonnet-5-5`, following Q4. `make evals` without `MAX_COST` is a dry run. |

Answers to the open eval questions, 15 Nov 2026 (Arthur):

| # | Topic | Status | Decision |
|---|---|---|---|
| E1 | Knock-out recall gate | Proposed | `min_knockout_recall = 0.90` in `evals/weights.toml`. To confirm after the first real run. |
| E2 | Composite weights and screening value | Decided | Keep the weights above and F2 as the screening value. |
| E3 | Cost cap | Decided (Q12 stays open) | No default cap; a live run without `--max-cost-usd` is refused. The first real run is about 20 tenders with `MAX_COST=10`. Q12 (the daily job's cap) stays open. |
| E4 | Git-ignored eval data | Decided | `data/snapshot/` and `data/eval_memos/` are in `data/.gitignore`. `data/eval_memos/` is now the default `--memo-dir` of the command line. A test fails if either path is not ignored, or if `evals/results/` is. |
| E5 | DPS and submission language | Decided | Dropped from `ARCHITECTURE.md` 6.1 for v1. `ExtractionOutput` has no field for them. |
| E6 | Run-to-run variation | Decided | No `--repeats` now. Listed in `docs/NEXT.md` as a week-3 option. The Wilson interval covers sampling only. |
| E7 | Results files | Decided | `evals/results/` is committed. A test checks that a results file and its case rows hold only scores, costs, versions and identifiers: no document text and no quotes. |

Open:

- Q12: the daily job's cost cap.
- Six fields of `ARCHITECTURE.md` 6.1 have no ground truth until `ingest/ted.py` reads them.
- Only an Anthropic run is possible today: Mistral has no price row and OpenAI has no adapter.
- Nothing writes `data/snapshot/` yet. The freeze script is a separate task.

Code change outside `evals/`: `pipeline/prompt_specs.py` gained `prompt_refs`, so that a run with another prompt version still makes its prompt references in the registry (the existing test on `PromptRef` calls stays as it is).
