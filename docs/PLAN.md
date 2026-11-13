# Tender Copilot: Project Plan

## 1. Goal

An LLM agent for an invented Dutch IT/data consultancy (see `docs/COMPANY.md`). Every morning it:

1. pulls new public tenders from TED (and later TenderNed)
2. screens them against the company profile with a cheap model
3. reads the full documents of the shortlisted tenders
4. checks each requirement against the company's evidence: met / not met / unclear
5. writes a bid/no-bid memo with citations (document and page)
6. after a human approves, creates a deal in the CRM (HubSpot)

Users can also chat with any tender ("What are the award criteria and their weights?").

**Target:** about 4 weeks of focused work (2-3 weeks with AI-assisted coding). Apply to the role after week 2, with the repo public and a working demo.

**Working rule:** AI coding tools may write most of the code. The agent loop, the eval labels and the design decisions must be mine, and I must be able to defend every one of them.

## 2. Architecture

```
TED Search API ─┐                                  ┌─ Streamlit UI (inbox, memo, chat, approve)
TenderNed docs ─┼─► Ingestion ─► Postgres + pgvector ─► Agent ─┤
Company KB ─────┘   (daily job)   (tenders, docs,      (tools)   └─ HubSpot CRM (deal on approval)
                                   chunks, memos, logs)
```

**Two-stage design (main talking point)**
- Stage 1: deterministic CPV-code filter plus a small, cheap model screens every tender.
- Stage 2: a stronger model does deep analysis only on the shortlist.

This is the "right level of sophistication at the right cost" the job asks for, backed by measured numbers.

### Agent tools

| Tool | Does |
|---|---|
| `search_tenders(cpv, country, date_from, keywords)` | Query the local tender DB |
| `get_tender(notice_id)` | Structured notice data: buyer, deadline, value, lots, procedure |
| `search_tender_docs(query, notice_id, lang)` | Hybrid search over tender documents |
| `search_company_kb(query)` | Search the company's capabilities, CVs, references, certifications |
| `propose_crm_deal(notice_id, memo_id)` | Prepares a deal; written only after a human approves |

## 3. Phase 0: Groundwork (3-4 days)

### Learn
- Public procurement basics: procedure types (open, restricted, negotiated), EU thresholds, CPV codes, knock-out criteria (exclusion grounds, minimum turnover, certifications, reference projects), award criteria and weights (Dutch BPKV / EMVI), the Nota van Inlichtingen (Q&A round). PIANOo explains all of this.
- TED data: the Search API can be used anonymously; notices download as XML/HTML/PDF in any EU language; since late 2022 notices use the **eForms** XML standard. Learn the main fields, because they become eval ground truth.
- Fair use: TED limits are 600 downloads per IP in under 6 minutes and 700 requests per minute. Read TED's legal notice and TenderNed's terms of use before bulk downloading.
- Tooling: Git, Docker compose, Postgres (including full-text search).

### Do
1. Create the repo `tender-copilot` with folders `ingest/`, `rag/`, `agent/`, `app/`, `prompts/`, `evals/`, `infra/`, `docs/`.
2. Docker compose with Postgres + pgvector.
3. Explore in a notebook: query TED for Dutch notices with CPV 72*, 79*, 48* from the last 8 weeks; inspect the eForms XML; find out how TenderNed publications link to documents.
4. Write the invented client company's knowledge base (about 15 documents, a few in Dutch).
5. Write a one-page memo template: what a good bid/no-bid memo contains.

**Done when:** 50 real notices are in Postgres and the company knowledge base exists.

## 4. Phase 1: Ingestion and retrieval (week 1)

### Learn
- Text extraction from PDF and DOCX (`pymupdf`, `python-docx`); detecting scanned PDFs (no text layer)
- Language detection
- Section-based chunking: split by headings and clauses so "4.2 Gunningscriteria" stays together
- Multilingual embeddings: compare two models (e.g. BGE-M3 vs multilingual-e5)
- pgvector: `vector` column, cosine distance, HNSW index
- Hybrid search: vector search plus Postgres full-text search (Dutch dictionary), merged with reciprocal rank fusion

### Do
1. `ingest/ted.py`: daily TED pull. Parse eForms fields into `tenders` (buyer, title, CPV codes, estimated value, deadline, procedure, lots) and `contacts` (kept separate: personal data).
2. `ingest/documents.py`: download tender documents, extract text with page numbers. Flag scanned files and documents behind a login instead of failing.
3. `rag/chunk.py` and `rag/embed.py`: chunk, attach metadata (notice, document, section heading, page, language), embed, index.
4. Embed the company knowledge base the same way, in its own table.
5. Freeze a snapshot of about 150 tenders with documents. All evals run on this snapshot.
6. Early retrieval check: 15 questions with known answer locations; measure hit@5 for both embedding models; keep the better one and write down why.

**Done when:** `search_tender_docs("gunningscriteria", notice_id)` returns the right section with its page number.

## 5. Phase 2: Agent and memo (week 2)

### Learn
- Tool use / function calling in the Anthropic and OpenAI SDKs; the agent loop (write it by hand, about 150 lines, with max steps and error handling)
- Structured outputs with Pydantic schemas and validation retries
- Prompt design: system prompts, tool descriptions, few-shot examples
- Human-in-the-loop patterns

### Do
1. Stage 1 screening: CPV filter, then a small model returns `{relevant, reason, confidence}`.
2. Stage 2 extraction into a schema: key dates, scope and lots, value, knock-out criteria (each with quote and page), award criteria with weights, required certifications and references.
3. Requirement matching: for each knock-out criterion, search the company KB and label met / not met / unclear, with evidence from both sides.
4. Memo generation: summary, deadlines, fit score, knock-out table, award-criteria strategy, risks and gaps, questions for the Nota van Inlichtingen, recommendation (bid / no-bid / needs review), citations.
5. Privacy: strip contact names, emails and phone numbers before any text goes to an LLM (eForms lists contacts in structured fields; add a regex for emails and phone numbers).
6. Streamlit UI: inbox ranked by fit, memo view, chat per tender, Approve button that creates the HubSpot deal.
7. HubSpot: free CRM and a private app token. Create the deal (deadline, value, fit score, memo link) only after approval.
8. Logging table: every LLM call with model, prompt version, tokens, cost, latency, tools called.

**Done when:** the daily run produces a ranked inbox and correct memos for 3 tenders checked by hand. **Apply to the role now.**

## 6. Phase 3: Evals and model comparison (week 3)

### Learn
- Extraction metrics: exact match, tolerance for dates and amounts
- Precision and recall, and why recall matters most for knock-out criteria
- Hit@k and MRR
- LLM-as-judge: rubrics, known biases, checking the judge against my own grades

### Eval set

| Eval | Size | Ground truth | Metric |
|---|---|---|---|
| Field extraction from document text | 50 tenders | eForms structured fields (deadline, value, CPV, buyer, lots, procedure) | Accuracy per field |
| Relevance screening | 60 tenders | My labels for the company | Precision / recall |
| Document Q&A retrieval (NL + some DE) | 30 questions | The chunk holding the answer | Hit@5, MRR |
| Knock-out detection | 15 tenders | My hand annotations | Recall (weighted heaviest) |
| Memo quality | 15 memos | Rubric from the memo template | LLM judge + 10 checked by hand |
| Edge cases | 10 | Cancelled tender, passed deadline, scanned PDF, docs behind login, off-topic question | Handled correctly |

Key trick: the agent extracts from the document text and is graded against the XML fields, giving ground truth without hand labelling.

### Do
1. `evals/run.py`: runs everything on the frozen snapshot, saves `evals/results/<date>_<commit>.json`.
2. Model comparison on both stages with 3 models (Claude, OpenAI, Gemini or Mistral): quality per eval, cost per 100 tenders screened, cost per memo, p50/p95 latency. End with a one-sentence recommendation.
3. Improve one thing and measure it (chunking, hybrid search, or the extraction prompt `extract_v1.md` to `v2`). Show before/after numbers in the README.

**Done when:** one results table I could show a client.

## 7. Phase 4: Deploy, document, present (week 4)

### Learn
- AWS basics: a VM (EC2 or Lightsail), managed Postgres (RDS supports pgvector), security groups, secrets manager, a scheduled job (cron or EventBridge)
- Use an EU region (Frankfurt or Ireland); set a billing alarm first

### Do
1. Deploy the compose stack on one small VM with HTTPS, simple authentication on the UI, rate limiting and the daily ingestion job.
2. README: pitch and 2-minute demo video, architecture diagram, local setup, eval and cost tables, design decisions, limitations.
3. GDPR and AI Act section:
   - Personal data: contact persons in notices, and users' logs. State lawful basis (legitimate interest), data minimisation (contacts stored separately, never sent to the LLM), retention period.
   - International transfers: sending text to US model providers; reference the providers' data-processing terms and EU options.
   - AI Act: decision support with a human making the bid decision; not an Annex III high-risk category. Add a transparency notice in the UI; note the AI-literacy duty (Art. 4) for staff using it. Verify the current text and deadlines (the Digital Omnibus changed them).
   - Licences: TED and TenderNed reuse terms; each model's licence or terms.
4. Update the CV: this project first, rewrite the profile line, add only tools actually used.

## 8. Week-by-week summary

| Week | Outcome |
|---|---|
| 0 (3-4 days) | Procurement basics, repo, 50 notices in the DB, invented company KB |
| 1 | Ingestion, chunking, hybrid search, frozen snapshot, first hit@5 |
| 2 | Agent, extraction, requirement matching, memo, UI, HubSpot; apply |
| 3 | Full eval set, 3-model comparison, one measured improvement |
| 4 | AWS deployment, README with compliance section, demo video, CV update |

## 9. Things only I do

- Write this plan, the memo template and the eval rubric
- Label the ground truth (60 relevance labels, 15 knock-out annotations, 30 retrieval questions)
- Read at least 10 memos carefully
- Verify TED/TenderNed terms and the GDPR / AI Act text
- Keep API keys out of chats and the repo (`.env` only; name the Anthropic key `APP_ANTHROPIC_API_KEY` so Claude Code does not bill the API instead of the subscription)
- Rebuild the agent loop from memory once

## 10. Interview checklist

I must be able to explain:
- Why two stages, and what it saves in money
- How tender documents are chunked, and why not by fixed tokens
- Why hybrid search beats pure vector search for "Article 4.2" style queries
- How ground truth was obtained without labelling (eForms fields)
- Why knock-out recall matters more than precision
- Where the system still fails: scanned PDFs, documents behind logins, vague requirements
- What personal data is held and how it is protected
- Why a human approves before anything goes into the CRM
- What I would change to deploy it for a real client

## 11. Open risks

- TenderNed document access and API terms need checking; start with TED only and add TenderNed later.
- Tender PDFs are messy (scans, tables, documents behind logins).
- Pro-plan usage limits: one feature per session, Plan mode first, keep CLAUDE.md short.
