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
| Q8 | One memo per tender, or per lot? | Open | | |
| Q9 | Fit score: formula in code, and weights? | Open | | |
| Q10 | Memo language: Dutch, English, or the tender's language? | Open | | |
| Q11 | Tenders above EUR 2M: drop, or keep with "consortium only"? | Proposed | Keep, with a "consortium only" flag. | Matches `COMPANY.md` section 4. |
| Q12 | Tenders per day at Stage 2, and the daily cost cap? | Open | | Set after the Phase 3 cost measurements. |
| Q13 | Should "unsure" screening results always be shortlisted? | Proposed | Yes. | Stage 1 is tuned for recall. |

### Privacy and compliance

| # | Question | Status | Decision / answer | Evidence |
|---|---|---|---|---|
| Q14 | Retention for `contacts` and stored model payloads? | Proposed | Contacts: deadline plus 6 months. Model payloads: 30 days. Downloaded documents: deleted after evaluation. | Short periods, easy to defend in the README. |
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

Lock file: `uv.lock` is approved and generated. Commit it with the skeleton, not separately. The Dockerfile still installs with pip and does not use the lock yet.

Docker: not yet tested (Docker is not installed on this Mac). Run `make up` locally before relying on it. CI does not build the image yet.
