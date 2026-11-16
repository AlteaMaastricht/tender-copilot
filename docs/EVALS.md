# Tender Copilot: Evals

How `evals/run.py` scores the pipeline: the snapshot it reads, the exact formula of every metric, the label file formats, and the results file. Companion to `docs/ARCHITECTURE.md` sections 6 and 7.

## 1. Running

```
make evals                       # dry run: counts calls and estimates cost, calls no model
make evals MAX_COST=5            # live run, stops cleanly at USD 5
make evals MAX_COST=5 STAGE1=<model> STAGE2=<model>
make evals ARGS="--suites screening,knockout --limit 10"
```

Directly:

```
python -m evals.run --stage1-model M1 --stage2-model M2 [--judge-model MJ]
    [--max-cost-usd X | --dry-run]  [--suites a,b]  [--limit N]
    [--prompt extract_map=v2]  [--min-precision P]  [--memo-dir DIR]
    [--snapshot data/snapshot] [--datasets evals/datasets] [--results evals/results]
    [--list-docs PUBLICATION_ID]
```

- The model is a run parameter. Its provider is looked up in `llm/prices.py` and recorded. The three-provider comparison is the same runner run three times.
- `--stage1-model` screens. `--stage2-model` does extraction, matching, memo and chat. `--judge-model` judges memos and should be the same in every compared run.
- `--prompt NAME=VERSION` replaces a version from `pipeline/prompt_specs.py`, for a before/after measurement. The file must exist.
- `--limit N` runs the first N cases of each suite.
- `--list-docs` prints the documents of one tender with their `doc_key`, for labelling.
- A live run without `--max-cost-usd` is refused. There is no default cap.
- Exit codes: 0 finished; 2 usage, snapshot or label error (nothing called, nothing written); 3 stopped by the cost cap (results written).

Every model call goes through `llm/client.py`. The runner has no network or database code.

## 2. What runs today

The evals call the real pipeline steps, listed in `evals/targets.py`. A step that does not exist yet is `None` there, and each eval that needs it reports `not_built` with the module it waits for. On 15 November 2026 every step is missing, so every suite reports `not_built`. The runner, the metrics and the loaders are tested with stub steps and the fake provider.

| Suite | Needs |
|---|---|
| retrieval | `rag/search.py` |
| screening | `pipeline/screen.py` |
| extraction | `pipeline/extract.py` |
| knockout | `pipeline/extract.py`; match accuracy also needs a tool backend over the snapshot |
| memo | extraction, matching, memo assembly; the judge needs your rubric and `prompts/judge_v1.md` |
| edge_cases | screening, extraction, matching, memo; chat cases need a chat step |

Suite statuses: `ok`, `no_labels` (label file missing), `not_built`, `no_rubric` (memo only), `stopped_cost_cap` (scores cover the finished cases only), `not_run` (the cap was reached in an earlier suite).

Suites run in this order: retrieval, screening, extraction, knockout, memo, edge_cases. A step runs once per tender in a run: the extraction of a tender is reused by the knock-out, memo and edge-case suites.

## 3. The frozen snapshot

```
data/snapshot/
  manifest.json        snapshot_version, frozen_at, kb_version, files: [{path, sha256, bytes}]
  tenders/<publication_id>/
    notice.xml         TED eForms XML, read with ingest.ted.parse_notice
    tenderned.json     publication detail; `sluitingsDatum` is read from it
    contacts.json      {"names": [...]}: for the Redactor only
    documents.json     {"documents_access": "...", "documents": [...]}
    pages/<doc_key>.jsonl    {"page_no": 1, "text": "..."} per line
  kb/                  the company KB files the run used
```

- Each entry of `documents` has: `document_id`, `doc_key`, `filename`, `doc_type`, `is_current`, `published_at`, `extraction_status`, `page_kind`, `language`. A scanned or unsupported document has no page file.
- `doc_key` is the first 12 hex characters of the file's SHA-256. Labels use it, so they survive a database rebuild.
- **Snapshot hash:** SHA-256 of the manifest's `files` list as canonical JSON, sorted by path. At start the runner re-hashes every listed file. A changed file, a missing file or a file that is not listed stops the run with exit code 2.
- **Ground truth** is derived in code from `notice.xml` and `tenderned.json` (`evals/snapshot.py`, `ground_truth`). A document-reading step receives only the documents and their page text: no notice fields, no closing date, no contact names.
- **As-of date.** The snapshot tenders closed months ago, so the hard filter is given the day after the tender's publication date as "today": the start of that day in Amsterdam time, since the filter takes a moment. An edge-case label can set its own `as_of`.
- `contacts.json` and `tenderned.json` are optional. Without `tenderned.json` the tender has no TenderNed match.
- Nothing writes this folder yet. The freeze script is a separate task; `evals.snapshot.build_manifest` gives it the manifest.

## 4. Metrics: exact formulas

Notation: `1[x]` is 1 when x holds, else 0. A ratio with a zero denominator is `null`. Every proportion is stored with `n` and a Wilson 95% interval: `(p + z²/2n ± z·sqrt(p(1−p)/n + z²/4n²)) / (1 + z²/n)`, z = 1.96. The interval covers sampling only, not run-to-run variation of the model.

### 4.1 Field extraction

Lot alignment, used by the per-lot fields: `sim(a, b)` is `difflib.SequenceMatcher` ratio on titles after NFKC, casefold, punctuation removed and whitespace collapsed. Pairs are taken in descending `sim`, each lot once, while `sim ≥ 0.80`. If both sides have exactly one lot they are aligned without a title check.

For field f, `E_f` is the set of gradable units: ground truth present, and the field not in the label row's `skip_fields`.

- `accuracy_f = Σ_{u∈E_f} correct_f(u) / |E_f|`. A missing prediction is wrong. A ground-truth lot with no aligned prediction is wrong. A tender whose extraction failed is wrong on all its units.
- `coverage_f = (tenders with at least one unit in E_f) / N`.
- `extraction_score = mean of accuracy_f` over the fields with ground truth and `coverage_f ≥ 0.40`, leaving out `deadline_date_only`.

| Field | Ground truth | `correct` when | Unit | Truth parsed today |
|---|---|---|---|---|
| `deadline` | TenderNed `sluitingsDatum` | same calendar date in Europe/Amsterdam and `|pred − gt| ≤ 60 min`. A missing or placeholder closing date is not graded. A closing date without a time is graded on `deadline_date_only` alone. | tender | yes |
| `deadline_date_only` | same | same calendar date. Reported, not in the score (v1 writes 00:00 when the document gives no time). | tender | yes |
| `lot_count` | BT-137 | counts equal; a tender without lots counts 1 | tender | yes |
| `lot_titles` | BT-21 | per tender `aligned pairs / max(|gt|, |pred|)`; the field value is the mean. Graded only where the notice has two or more lots. | tender | yes |
| `estimated_value` | BT-27 | `|pred − gt| ≤ 0.01 · gt`. Per aligned lot when the notice has lot values, else the procedure value against the sum of the predicted lot values. EUR only. Misses that fit after dividing by 1.21 are counted as `vat_explained`, and stay wrong. | lot or tender | yes |
| `framework_max_value` | BT-271 | the sum of `ceiling_value_eur` within 1% | tender | yes |
| `duration_months` | BT-36 | `|pred − gt| ≤ 1` | aligned lot | yes |
| `award_types` | BT-539 | set of top-level `kind` values equals the notice's set | tender | no |
| `award_weights` | BT-541 | each side scaled to sum 100; for every kind k, `|Σpred_k − Σgt_k| ≤ 1.0` | tender | no |
| `selection_types` | BT-747 | recall: `Σ|S_gt ∩ S_pred| / Σ|S_gt|`, with categories mapped to economic, technical, suitability (mapping to confirm) | type | no |
| `selection_threshold` | BT-750, BT-752 | amount within 1% (`parse_amount` on `threshold`) | criterion | no |
| `is_framework` | BT-765 | `(framework ≠ "none") == gt` | tender | no |
| `security_clearance` | BT-578 | `(any knock-out of category security) == gt` | tender | no |

The six fields marked "no" are listed in the results under `fields_without_ground_truth` and are not scored until `ingest/ted.py` reads them. DPS (BT-766) and submission language (BT-97) are not graded in v1: `ExtractionOutput` has no field for them.

Also reported, not graded: `notice_deadline_disagreement`, the share of tenders where the notice deadline (BT-131) falls on another date than `sluitingsDatum`.

### 4.2 Screening

- Predicted positive: `apply_hard_filter(...).passed` and `decision ∈ {shortlist, unsure}` (decision Q13). A call that fails after retries is a predicted negative and is counted under `errors`.
- Actual positive: label `relevant`.
- `precision = TP / (TP + FP)`, `recall = TP / (TP + FN)`.
- `F2 = 5·P·R / (4·P + R)`. This is the suite's value in the composite, because Stage 1 is tuned for recall.
- Reported beside them: TP, FP, FN, TN, and FN split into `lost_in_filter` and `lost_in_model`.
- Secondary, for the calibration check: for τ in 0.1 … 0.9, positive when shortlisted or unsure with `confidence ≥ τ`, giving P(τ) and R(τ). With `--min-precision P*`: `recall_at_precision = max R(τ) with P(τ) ≥ P*`.

The 60 labels are sampled half from "shortlist" and half from "reject" (ARCHITECTURE 6.4), so these are sample figures, not population estimates. The optional `stratum` field keeps what is needed to reweight them later.

### 4.3 Retrieval

For question q the label gives answer pages `A_q` as (doc_key, page) pairs. The search step returns chunks `r_1 … r_10` for that tender. `r_i` is relevant when its document matches and `[page_start, page_end]` contains a page of `A_q`. `rank_q` is the smallest such i, or ∞.

- `hit@5 = (1/|Q|) · Σ_q 1[rank_q ≤ 5]`
- `MRR@10 = (1/|Q|) · Σ_q (1/rank_q if rank_q ≤ 10 else 0)`

Reported for `hybrid` and `vector_only`, and per question language. No model calls. The composite uses hybrid `hit@5`.

### 4.4 Knock-out recall

`G_t` is the labelled requirements of tender t; `P_t` the extracted `knockout_criteria`. A prediction p matches a label g when: same document, `|p.citation.page − g.page| ≤ 1`, and `p.category == g.category`. Matching is one-to-one (maximum bipartite matching), so one prediction cannot cover two labels. `M_t` is the number of matched labels.

Formal and non-formal requirements are matched apart. A real knock-out that was extracted as a formal action is never checked against company evidence, so it does not count as found.

- `recall = Σ_t M_t / Σ_t |G_t|` over labels of kind `exclusion`, `suitability`, `minimum_requirement`, against predictions of those kinds. **This is the headline, the composite value and the gate value.**
- `recall_macro = mean_t (M_t / |G_t|)`
- `tenders_all_found = (1/T) · Σ_t 1[M_t == |G_t|]`
- `recall_page_only`: the same labels against all predictions, without the category test. The gap to `recall` shows disagreements on category or kind.
- `recall_formal`: the same rule for kind `formal`, reported apart.
- `precision = Σ_t M_t / Σ_t |P_t|` over non-formal predictions. Reported, not weighted.
- Match accuracy, for matched labels that carry `expected_status`: `match_accuracy = Σ 1[status == expected] / n`, and `false_met_rate = Σ 1[status == met and expected == not_met] / Σ 1[expected == not_met]`.

### 4.5 Memo quality (slot for your rubric)

- The memo eval runs on the tenders that have knock-out labels. Say so if you want a separate list.
- `verified_citation_share = Σ_m citations_verified / Σ_m citations_total`. Code only; computed without a rubric.
- `memo_rubric.json` missing: status `no_rubric`, no judge call. Present, but no `--judge-model` or no judge step: `not_built`.
- For criterion c with weight `w_c`, scale `1 … S_c` and judge score `s_{m,c}`: `memo_score_m = Σ_c w_c · (s_{m,c} − 1)/(S_c − 1) / Σ_c w_c`. `judge_score = mean_m memo_score_m`.
- Judge check on the memos you graded, per criterion: `exact_agreement = Σ 1[judge == hand] / n`, `within_one = Σ 1[|judge − hand| ≤ 1] / n`, `mean_abs_diff = Σ |judge − hand| / n`.
- A hand grade belongs to one memo text, named by its SHA-256. The command line saves generated memos under `--memo-dir` (default `data/eval_memos/`, git-ignored), as `<run_id>/<publication_id>.json`. With the same folder a later run finds the graded memos by hash and judges exactly those texts. A memo holds tender text, so a folder elsewhere must also stay out of git.
- The judge call uses stage `judge` and the prompt file `prompts/judge_v1.md`, written when the rubric exists.

### 4.6 Edge cases

A case passes when every key in its `expect` object holds for the observed outcome. `pass_rate = passed / (passed + failed)`. A case whose pipeline step fails is a failed case. An unknown `expect` key, or a value of the wrong type, makes the case `invalid_label`: it is reported and left out of the denominator.

A case with a `question` runs the chat step only. Any other case runs the hard filter, screening, and for a shortlisted or unsure tender extraction, matching and the memo. A tender whose documents cannot be read gets a notice-only memo.

### 4.7 Composite and gate

- `overall = Σ_e w_e · s_e`, weights summing to 1, with `s_e`: knockout `recall`; extraction `extraction_score`; screening `F2`; retrieval `hit@5` (hybrid); memo `judge_score`; edge cases `pass_rate`.
- The weights are in `evals/weights.toml`: knockout 0.30, extraction 0.20, screening 0.20, retrieval 0.10, memo 0.10, edge_cases 0.10.
- `overall` is `null` unless all six suites are `ok`. `overall_partial = Σ_ok w_e·s_e / Σ_ok w_e`, stored with the suites it covers, so a partial run is never read as a full one.
- Gate: `pass` when `knockout.recall ≥ min_knockout_recall`, else `fail`. `not_set` while the value is absent from `weights.toml`; `not_evaluated` when the knockout suite is not `ok`. A failed gate sets `not_recommended: true` on the run, whatever `overall` is.

### 4.8 Cost and latency

All from the run's `llm_calls` records. A failed call's billed cost counts.

- `cost_per_100_screened = 100 · Σ cost_usd[stage = screening] / (tenders sent to the screening model)`. Tenders dropped by the hard filter cost nothing and are not in the denominator.
- `cost_per_memo = Σ cost_usd[stage ∈ extraction_map, extraction_reduce, matching, memo; memo-suite tenders] / (memos produced)`. A tender that failed part-way stays in the numerator, not the denominator; `memos_failed` is reported.
- Judge cost is `eval_overhead_usd`, outside both.
- Latency per stage, nearest rank on the sorted values: `p_q = x[ceil(q·n) − 1]`, for q = 0.50 and 0.95, with n reported.
  - `per_call`: `latency_ms` of each record (model time, including network retries, excluding backoff sleeps).
  - `per_tender`: the sum of one tender's call latencies in that stage.
  - Retrieval has no model call: wall-clock per query, in the retrieval suite's own metrics.

## 5. Label formats

All files are JSONL, UTF-8, one object per line, in `evals/datasets/`. The runner only reads them, and records the SHA-256 of each file in every result. A missing file is not an error: its eval reports `no_labels`.

- `note` is always optional and never scored.
- Unknown fields are rejected, with the file name and line number.
- No names, emails or phone numbers. The loader rejects a row whose text holds an email address or a phone number.
- `text` fields are your own restatement, not a verbatim quote (decision Q2: no extracted text in git).
- Every `publication_id` must be in the snapshot and every `doc_key` must be a document of that tender; otherwise the run stops before any call.

**`screening.jsonl`**: one row per tender.

| Field | Type | |
|---|---|---|
| `publication_id` | int | TenderNed publication, required, unique |
| `label` | `relevant` or `not_relevant` | required |
| `stratum` | `shortlist`, `reject` or `filtered_out` | optional: how the row was sampled |

```json
{"publication_id": 412551, "label": "relevant", "stratum": "shortlist", "note": "data platform for a province"}
```

**`retrieval.jsonl`**: one row per question.

| Field | Type | |
|---|---|---|
| `question_id` | `RQ-01` … | required, unique |
| `publication_id` | int | required |
| `question` | string | required, in the language a user would ask |
| `language` | `nl` or `en` | required |
| `answers` | list of `{doc_key, pages}` | required; any listed page counts as a hit |

```json
{"question_id": "RQ-01", "publication_id": 412551, "question": "Wat zijn de gunningscriteria en hun wegingen?", "language": "nl", "answers": [{"doc_key": "a3f9c2d41b07", "pages": [14, 15]}]}
```

**`extraction.jsonl`**: one row per tender in the extraction eval. The values come from the notice and TenderNed; this file only selects tenders and records your corrections.

| Field | Type | |
|---|---|---|
| `publication_id` | int | required, unique |
| `skip_fields` | list of field names from 4.1 | optional: do not grade these for this tender |
| `overrides` | object | optional: where the notice is wrong. Keys: `deadline` (date and time), `lot_count`, `estimated_value`, `framework_max_value`. A corrected `estimated_value` is the tender total and replaces the lot values. |

```json
{"publication_id": 412551, "skip_fields": ["estimated_value"], "overrides": {"lot_count": 2}, "note": "notice value is the framework maximum"}
```

**`knockout.jsonl`**: one row per requirement. A tender is in the eval when it has at least one row; label every requirement of that tender.

| Field | Type | |
|---|---|---|
| `publication_id` | int | required |
| `label_id` | `<publication_id>-KO-01` … | required, unique |
| `doc_key`, `page` | string, int | required: where the requirement is stated |
| `category` | a `RequirementCategory` value from `pipeline/schemas.py` | required |
| `kind` | `exclusion`, `suitability`, `minimum_requirement`, `formal` | required |
| `text` | string | required: the requirement in one sentence, your words |
| `threshold` | string or null | optional |
| `expected_status` | `met`, `not_met`, `unclear` | optional: enables match accuracy |

```json
{"publication_id": 412551, "label_id": "412551-KO-03", "doc_key": "a3f9c2d41b07", "page": 22, "category": "financial", "kind": "suitability", "text": "Minimum annual turnover of EUR 1,000,000 in each of the last three years.", "threshold": "EUR 1,000,000 per year", "expected_status": "met"}
```

**`edge_cases.jsonl`**: one row per case.

| Field | Type | |
|---|---|---|
| `case_id` | `EC-01` … | required, unique |
| `type` | `cancelled`, `passed_deadline`, `scanned_pdf`, `docs_behind_login`, `off_topic_question`, `prompt_injection` | required |
| `publication_id` | int | required |
| `as_of` | date | optional: "today" for the hard filter, instead of the day after publication |
| `question` | string | chat cases only |
| `expect` | object | required, at least one key from the table below |

| `expect` key | Value | Holds when |
|---|---|---|
| `filter_passed` | bool | the hard filter result equals it |
| `filter_failed_rule` | rule name | that rule is a definite fail (`country`, `cpv`, `deadline`, `value`, `notice_type`) |
| `filter_flags_include` | list | every listed flag was set |
| `screened` | bool | a screening call was made, or not |
| `memo_created` | bool | a memo was produced, or not |
| `recommendation` | one of the four values | the memo's recommendation equals it |
| `reason_codes_include` | list | every listed code is among the recommendation reason codes |
| `limitations_include` | list | every listed text is among the memo's limitations |
| `extraction_warnings_include` | list | each listed text is part of an extraction warning |
| `chat_in_scope` | bool | the chat answer's `in_scope` equals it |
| `output_excludes` | list | none of the listed texts appears in a model-written field (case ignored) |
| `max_model_calls` | int | at most this many model calls were made for the case. A result reused from an earlier suite in the same run makes no new call. |

```json
{"case_id": "EC-02", "type": "passed_deadline", "publication_id": 409819, "as_of": "2026-04-01", "expect": {"filter_passed": false, "filter_failed_rule": "deadline", "memo_created": false, "max_model_calls": 0}}
```

**Memo (placeholder shapes, yours to change):**

- `memo_rubric.json`: `{"version": "v1", "criteria": [{"id": "...", "question": "...", "scale_max": 5, "weight": 3}]}`
- `memo_grades.jsonl`: `{"publication_id": 412551, "memo_sha256": "<64 hex>", "scores": {"<criterion id>": 4}}`

## 6. Results file, cost cap, dry run

**`evals/results/<date>_<commit>.json`**: the local run date and the short commit hash. Each run appends one entry to `runs`; earlier entries are not touched. The file is written to a temporary name and then renamed.

```json
{
  "schema_version": 1, "date": "2026-11-15", "git_commit": "9d60a2f",
  "runs": [{
    "run_id": "<uuid4>", "started_at": "...", "finished_at": "...",
    "completed": true, "stop_reason": null, "not_recommended": false, "git_dirty": false,
    "params": {"stage1_model": "...", "stage1_provider": "...", "stage2_model": "...",
               "stage2_provider": "...", "judge_model": null, "max_cost_usd": "5",
               "suites": ["..."], "limit": null, "min_precision": null,
               "as_of_rule": "published_plus_1_day"},
    "prompt_versions": {"screen": "v1", "extract_map": "v1", "...": "..."},
    "snapshot": {"version": "...", "sha256": "...", "tenders": 150},
    "labels": {"screening.jsonl": {"sha256": "...", "rows": 60}, "knockout.jsonl": null},
    "suites": {"screening": {"status": "ok", "n": 60,
                             "metrics": {"precision": {"value": 0.0, "n": 0, "ci95": [0.0, 0.0]}}},
               "knockout": {"status": "no_labels"},
               "extraction": {"status": "not_built", "detail": "pipeline/extract.py"}},
    "overall": {"value": null, "partial": 0.0, "suites_included": ["..."],
                "weights": {"knockout": 0.3}, "gate": "not_set", "min_knockout_recall": null},
    "cost": {"total_usd": "0.000000", "per_100_screened_usd": null, "screened": 0,
             "per_memo_usd": null, "memos_produced": 0, "memos_failed": 0,
             "eval_overhead_usd": "0.000000", "by_stage": {}},
    "latency_ms": {"screening": {"per_call": {"n": 0, "p50": null, "p95": null},
                                 "per_tender": {"n": 0, "p50": null, "p95": null}}},
    "calls": {"total": 0, "by_status": {}, "validation_retries": 0, "network_retries": 0}
  }]
}
```

Per-case rows go to `evals/results/<date>_<commit>_cases/<run_id>.jsonl`. They hold identifiers and outcomes only: no document text, no model free text, no contact data. A failed step is recorded by its error type, never its message.

**Cost cap.** `--max-cost-usd` has no default (decision Q12 is open). The run's call log adds up `cost_usd` of every call, failed calls included. The runner checks the sum before each case. At or over the cap it starts no further case: the current suite is `stopped_cost_cap`, later suites are `not_run`, the results are written with `completed: false` and `stop_reason: "cost_cap"`, and the exit code is 3. The overshoot is at most one case.

**Dry run.** No gateway is built, so no model can be called. The code steps still run: snapshot check, label loading, hard filter. The output is the calls per stage and two cost figures from `llm/prices.py`, `expected_usd` and `upper_bound_usd`, with every assumption used. Nothing is written to `evals/results/`. The estimate covers all selected suites as if their steps were built.

| Stage | Calls counted | Tokens assumed |
|---|---|---|
| screening | 1 per tender that passes the hard filter | prompt file, company sections, schema, notice titles plus 2,000 characters, at 3 characters per token; output 300 |
| extraction_map | `ceil(key-document characters / 12,000)` per tender, until `rag/chunk.py` gives real sections | prompt plus 12,000 characters; output 1,500 |
| extraction_reduce | 1 per tender with readable key documents | the map outputs; output 3,000 |
| matching | per non-formal knock-out (label count, else 10): 3 steps expected | 20,000 prompt tokens per requirement; 400 out per step |
| memo | 2 per tender | 15,000 in, 3,000 out per tender |
| judge | 1 per memo, only with a rubric and a judge model | 8,000 in, 500 out |
| chat | per edge-case question: 3 steps expected | 20,000 prompt tokens per question; 500 out per step |

`upper_bound_usd` assumes that every single call needs its one validation retry and that every agent loop runs to its limits (`MATCH_MAX_STEPS` and `MATCH_TOKEN_BUDGET`, `CHAT_MAX_STEPS` and `CHAT_TOKEN_BUDGET`). A loop can pass its token budget by one step, so this is a planning figure, not a guarantee; the cap is the guarantee. Every edge-case tender that passes the hard filter is counted as screened and analysed in full.

The constants are first guesses, kept in `evals/budget.py`, to be set from the first real run's `input_tokens`.

## 7. Decided and still open

Decided 15 Nov 2026 (`DECISIONS.md`, Evals):

- `min_knockout_recall = 0.90` in `evals/weights.toml`, **Proposed**: to confirm after the first real run.
- The weights of 4.7 and F2 as the screening value are kept.
- No default cost cap. The first real run is about 20 tenders with `MAX_COST=10`.
- `data/snapshot/` and `data/eval_memos/` (the default `--memo-dir`) are git-ignored; a test fails if either is not.
- DPS and submission language are not graded in v1.
- No `--repeats` option now; it is a week-3 option in `docs/NEXT.md`.
- Results files in `evals/results/` are committed. A test checks that they hold only scores, costs, versions and identifiers.

Still open:

1. The cost cap value for the daily job (decision Q12).
2. The eForms identifiers in `ingest/ted.py` are unverified (`DECISIONS.md`); the extraction eval relies on them.
