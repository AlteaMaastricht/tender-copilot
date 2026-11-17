# Plan: chunking, embedding, hybrid search and the KB index

Status: plan, 17 Nov 2026. Nothing in this file is built. Written for an implementer who follows it step by step; every design choice is made here or listed under "Open decisions" for Arthur.

The request named this plan without listing its contents. Its scope here is taken from `ARCHITECTURE.md` 2.1 (`rag/chunk.py`, `rag/embed.py`, `rag/search.py`), 2.2 step 5, 3 (`chunks`, `company_kb`), 4 (`search_tender_docs`, `search_company_kb`), 7 (retrieval eval), `PLAN.md` Phase 1 (chunking, two embedding models, hybrid search, the early retrieval check), and from what the other plans wait for: the snapshot tool backend for the matching, chat and memo evals (`pipeline.md` P12), the `Indexer` and `BackendFactory` of the daily job (`pipeline.md` P13), and the `Searcher` of `DbToolBackend` (`database.md` B8). Tell me if you meant something else.

Read first: `CLAUDE.md`, `docs/ARCHITECTURE.md` 2.1, 2.5, 2.10, 3, 4, 7; `docs/DECISIONS.md` Q5, Q7, Evals; `docs/EVALS.md` 3, 4.3; `docs/plans/pipeline.md` P0, P5, P12, P13; `docs/plans/database.md` B1, B5, B8.

## 0. Scope and what already exists

| Part | Module | Status today |
|---|---|---|
| Embedding models | `rag/embed.py` | missing |
| Chunking | `rag/chunk.py` | missing |
| Lexical ranking (BM25 with stemming) | `rag/lexical.py` | missing |
| Ranking, fusion, result shaping | `rag/search.py` | missing |
| Snapshot index and the retrieval eval target | `rag/snapshot_index.py`, `evals/targets.py` | `search` target is `None` |
| Tool backend over the snapshot (matching, chat, memo evals) | `agent/snapshot_backend.py` | missing |
| Index step of the daily job (`Indexer`) | `rag/indexer.py` | missing |
| KB index (`company_kb`) and the KB source over the database | `rag/kb_index.py` | missing (`rag/kb.py` `FileKb` from pipeline.md P5) |
| Wiring: `BackendFactory`, re-index command | `jobs/daily.py`, `rag/reindex.py` | missing |

Existing code this plan builds on: `agent/tools.py` (tool schemas, `MAX_RESULT_CHARS = 20_000`, `ToolError`), `agent/fakes.py` (the result shapes), `evals/targets.py` (`SearchStep`, `SearchHit`, `SEARCH_MODES`), `evals/metrics.py` (`first_relevant_rank`, hit@5, MRR@10), `evals/snapshot.py` (`TenderDocs`, `SnapshotTender`), `pipeline/inputs.py` (pipeline.md P0: `DocsView`, `is_readable`), `rag/kb.py` (pipeline.md P5: `KbPassage`, `KbSource`, `FileKb`), `ingest/language.py` (documents.md D6 word lists), `db/` (database.md).

### Fixed rules for every step

1. Search is always scoped to one tender (or to the KB). No cross-tender search, no HNSW index (3).
2. Searched tender documents: readable documents that are current, plus every `qa` version; never `notice` documents (2.5, 2.10). Annexes, forms and price sheets are searched (2.5: "indexed for search but not read").
3. A tool result is never truncated in the middle of a passage; whole passages are dropped from the bottom instead, so a quote copied from a result is always a piece of one page.
4. Embedding runs locally (Q5 as proposed, SD2): no tender text leaves the machine for embedding, so it is not a gateway call and is not logged in `llm_calls`. If a hosted embedding model is ever chosen, it gets a gateway entry point first (SD2).
5. Tests use `FakeEmbedder` (deterministic, no model download). A test that needs the real model is marked `model` and skipped unless the model is in the local cache.
6. Done means for every step: `make test` passes and `make lint` is clean.

## 1. Dependencies to approve

The first four go in a new optional extra `embed` (SD3), so CI, the `app` image and the plain `make test` do not install them; snowballstemmer and numpy are main dependencies. Versions are the newest before the lock cutoff (30 Oct 2026) that `uv` can resolve together; the exact set is fixed by `uv lock` and recorded in `DECISIONS.md`. Dates are PyPI upload dates, checked 16 Nov 2026.

| Package | Version | Released | Licence | Needed for |
|---|---|---|---|---|
| sentence-transformers | 6.2.0 | 21 Oct 2026 | Apache-2.0 | Loading and running BGE-M3 and multilingual-e5 |
| torch (CPU build) | 2.14.1 | 30 Sep 2026 | BSD-3 and others | Runtime for the models. 2.15.0 (28 Oct 2026) is allowed by the cutoff but only 20 days old; 2.14.1 if `sentence-transformers` 6.2.0 accepts it. Installed from the PyTorch CPU index (`[tool.uv.sources]`), otherwise Linux pulls the CUDA build (several GB). |
| transformers | 5.22.0 (or the newest that resolves) | 29 Oct 2026 | Apache-2.0 | Pulled in by sentence-transformers |
| huggingface-hub | 2.4.0 (or the newest that resolves) | 29 Oct 2026 | Apache-2.0 | Model download with a pinned revision |
| snowballstemmer | 3.1.1 | 3 Jun 2026 | BSD-3 | Dutch and English stemming for the lexical ranker (SD1). Small, pure Python. Not in the `embed` extra: a main dependency, because the lexical ranker has no model. |
| numpy | 2.5.4 | 10 Oct 2026 | BSD-3 | Vectors in the ranker. A main dependency, not in the `embed` extra, because the ranker and its tests run without torch. |

Model weights (not Python packages; downloaded once into `MODEL_CACHE_DIR`, git-ignored, a volume in Docker):

| Model | Size | Licence | Revision |
|---|---|---|---|
| `BAAI/bge-m3` | about 2.3 GB | MIT | pinned commit hash, written into `rag/embed.py` at first download and recorded in `DECISIONS.md` |
| `intfloat/multilingual-e5-large` | about 2.2 GB | MIT | pinned likewise; only for the comparison (SD4) |

## 2. Open decisions for Arthur

| # | Question | Recommendation | Consequence of each option | Step |
|---|---|---|---|---|
| SD1 | Where is the hybrid ranking computed? `ARCHITECTURE.md` 3 plans Postgres full-text (`fts`, GIN) plus pgvector. The retrieval eval reads files, not the database (decision of 15 Nov), so a Postgres ranker could not be the one the eval measures. | One ranker in Python for both: load the tender's chunks (a few hundred, rule 1) with their embeddings, then exact cosine plus BM25 with Snowball stemming, fused with reciprocal rank fusion. The database stores chunks and embeddings; its `fts` columns stay (database.md) but are unused. | **Python ranker:** the retrieval eval measures exactly the production code, and evals need no database; one more small dependency (snowballstemmer); loading a tender's chunks per query costs a few milliseconds. `ARCHITECTURE.md` 2.1 and 9 need a line. **Postgres ranker:** uses the planned `fts`/`ts_rank_cd`; the evals would need a second implementation (which would then be what is measured) or a database loaded from the snapshot. | S2, S3 |
| SD2 | Q5 is "Proposed": local BGE-M3, or a hosted embedding API? | Local BGE-M3, as proposed. | **Local:** free, no transfer of tender text, 1024 dimensions as in the schema; about 2.3 GB of weights and a few minutes of CPU per tender on a small VM (measure in S9). **Hosted (for example `mistral-embed`, 1024 dimensions, EU-hosted):** no torch, fast; a new `Gateway.embed` entry point with redaction and `llm_calls` logging (stage `embedding`), a price row, and a data-transfer paragraph in the README. | S0 |
| SD3 | Install the embedding stack as an optional extra? | Yes: `pip install .[embed]` / `uv sync --extra embed` in the worker image and for eval runs with retrieval; not in CI or the `app` image. | **Extra:** CI stays fast and small; the retrieval suite and the index step check for the extra and report `not_built: embedding extra not installed`. **Main dependency:** simpler, but every install pulls torch. | S0 |
| SD4 | Compare two embedding models now (`PLAN.md` Phase 1 step 6), or BGE-M3 only? | Compare once, when the retrieval labels exist: the retrieval suite run with each model (`--embedding-model`), the better hit@5 kept and the reason written to `DECISIONS.md`. | **Compare:** a measured choice for the README; a second download and twice the snapshot embedding time (one-off). **BGE-M3 only:** saves an evening; the choice is not evidenced. | S9 |
| SD5 | Chunk sizes. | Target 1,500 characters (about 400 tokens at 3.7 characters per token for Dutch), at most 2,000, at least 200 (smaller pieces are merged), 200 characters of overlap for fixed windows only. Tunable constants, set from the retrieval eval. | **These:** passages fit 8 per tool result under the result budget; a requirement and its threshold usually stay together. **Larger:** better context, fewer hits per result, more noise per hit. **Smaller:** sharper hits, more cut-off passages (more `read_document_pages` steps). | S1 |

Decisions taken in this plan without asking:

- Reciprocal rank fusion with `k = 60`; each ranker contributes its top 50; ties broken by vector rank, then `chunk_id`.
- The text embedded for a chunk is `"<tag or file name> > <section_path>\n<content>"`; `chunks.content` stores only `<content>`, so what a tool returns is page text.
- `token_count` is counted with the embedding model's tokenizer at index time; chunk boundaries use characters, so chunking needs no model.
- Embeddings are L2-normalised; cosine similarity is a dot product.
- A tool result holds at most `RESULT_TEXT_BUDGET = 15_000` characters of passage text (rule 3), below the loop's 20,000-character limit including JSON.
- In production `kb_id` is `company_kb.id`; in evals it is the `FileKb` position (pipeline.md P5). Both are consistent within one run; a memo stores the `kb_id` of its environment.

## 3. Build steps

Ten steps (S0–S9). S0–S4 give the retrieval eval; S5 unblocks the matching, chat and memo evals; S6–S8 the daily job.

### S0. Embedders (`rag/embed.py`)

**Functions.**

```python
MODEL_CACHE_ENV = "MODEL_CACHE_DIR"          # default data/models (git-ignored)
EMBEDDING_DIM = 1024

@dataclass(frozen=True, slots=True)
class ModelSpec:
    name: str                    # "BAAI/bge-m3"
    revision: str                # pinned commit hash
    query_prefix: str            # "" for bge-m3, "query: " for e5
    passage_prefix: str          # "" for bge-m3, "passage: " for e5
    max_seq_length: int

MODELS: dict[str, ModelSpec]     # "bge-m3", "multilingual-e5-large"
DEFAULT_MODEL = "bge-m3"

class Embedder(Protocol):
    name: str                    # stored in chunks.embedding_model, e.g. "bge-m3@<revision[:8]>"
    def embed_passages(self, texts: Sequence[str]) -> np.ndarray: ...   # (n, 1024) float32, normalised
    def embed_query(self, text: str) -> np.ndarray: ...                 # (1024,)
    def count_tokens(self, text: str) -> int: ...

class EmbeddingUnavailable(RuntimeError): ...    # the extra is not installed or the model not cached

def load_embedder(key: str = DEFAULT_MODEL, *, cache_dir: Path | None = None,
                  allow_download: bool = False, batch_size: int = 16) -> Embedder
class FakeEmbedder:              # tests: hash of each lower-cased word -> a fixed random unit vector, summed and normalised
    name = "fake"
```

**Rules.** `load_embedder` imports `sentence_transformers` inside the function (an `ImportError` becomes `EmbeddingUnavailable("install the embed extra")`). The model is loaded from `cache_dir` with the pinned `revision`; `allow_download=False` by default, so a missing model raises instead of fetching 2 GB silently. A command `python -m rag.embed --download bge-m3` downloads it explicitly. CPU only (`device="cpu"`). Prefixes are added inside `embed_passages` and `embed_query`. The `FakeEmbedder` makes texts sharing words similar, which is enough to test ranking.

**Tests** (`tests/test_embed.py`): `FakeEmbedder` vectors have norm 1 and dimension 1024; two texts sharing words are closer than unrelated ones; `load_embedder` without the extra raises `EmbeddingUnavailable` (simulate with `monkeypatch.setitem(sys.modules, "sentence_transformers", None)`); a missing model with `allow_download=False` raises. Marked `model`: the real BGE-M3 embeds two Dutch sentences, and "gunningscriteria" is closer to "Gunningscriteria en wegingen" than to "Inleiding".

**Done when.** Tests pass; `pyproject.toml` has the `embed` extra and the CPU torch source; `MODEL_CACHE_DIR` is git-ignored (add `data/models/` to `data/.gitignore`, with a test like E4's).

---

### S1. Chunking (`rag/chunk.py`)

**Functions.**

```python
TARGET_CHARS = 1_500
MAX_CHARS = 2_000
MIN_CHARS = 200
FIXED_OVERLAP_CHARS = 200
EDGE_LINES = 2                   # lines at the top and bottom of a page checked for running headers
REPEAT_SHARE = 0.5

@dataclass(frozen=True, slots=True)
class ChunkDraft:
    document_id: int
    chunk_index: int
    section_path: str            # "4 Selectie > 4.2 Geschiktheidseisen", "" when none
    page_start: int
    page_end: int
    language: str | None
    content: str
    chunk_method: Literal["heading", "fixed"]

HEADING = re.compile(...)        # see rules
def running_lines(pages: Sequence[PageText]) -> set[str]
def headings(lines) -> list[tuple[int, str, int]]       # (line index, text, depth)
def chunk_document(doc: DocView) -> list[ChunkDraft]
def chunk_tender(docs: DocsView) -> list[ChunkDraft]    # every searchable document (rule 2)
```

**Rules.**

- `chunk_tender` chunks every document with `is_readable(doc)` (pipeline.md P0) that is current or `qa`. Superseded versions are not chunked; when a new version arrives the old chunks stay in the database but are filtered out at query time (S6).
- By `page_kind`:
  - `page` (PDF) and `section` (DOCX): heading-based.
    1. Drop running headers and footers: a line among the first or last `EDGE_LINES` non-empty lines of a page that occurs (after collapsing digits to `#`, so "Pagina 3 van 40" matches "Pagina 4 van 40") on at least `REPEAT_SHARE` of the pages. Only edge lines are dropped, so the rest of each page stays one contiguous piece of the stored text.
    2. A heading is a line of at most 120 characters matching one of: a clause number `^\d{1,2}(\.\d{1,2}){0,3}\.?\s+\S` (depth = number of parts), `^(hoofdstuk|chapter|paragraaf|bijlage|annex|artikel|article)\s+\S` (depth 1, `artikel` depth 2), and not ending in a sentence full stop after more than 8 words (that is a numbered sentence, not a heading).
    3. A section runs from one heading to the next, across pages; `page_start`/`page_end` are the pages of its first and last line. `section_path` joins the open headings by depth with ` > `.
    4. A section over `MAX_CHARS` is split at blank lines, then at sentence ends (`. ` followed by a capital), into pieces near `TARGET_CHARS`; each piece keeps the `section_path`. A piece never contains a page break in the middle of a sentence it cannot keep: pieces end at line ends.
    5. Sections under `MIN_CHARS` are merged with the following section that has the same parent, while the result stays under `TARGET_CHARS`.
    6. A document with fewer than two headings: fixed windows of `TARGET_CHARS` with `FIXED_OVERLAP_CHARS` overlap, cut at line ends, `chunk_method = "fixed"`.
  - `slide`: one chunk per slide; consecutive slides under `MIN_CHARS` merged; `section_path` = the slide's first line.
  - `sheet`: the first non-empty line of the sheet is the header; chunks are blocks of whole rows up to `TARGET_CHARS`, each starting with the header line (2.10: "header row repeated"); `section_path = "sheet <page_no>"`; `chunk_method = "fixed"`.
- `chunk_index` runs 0, 1, ... per document in reading order. `language` = the document's.
- `content` is always made of whole lines of one page or of consecutive pages joined with `\n` (so a quote inside one page's part is a substring of that page; a quote that spans the join fails the check, as designed: B5).

**Tests** (`tests/test_chunk.py`, synthetic page texts in the test):

- A three-page document with headings `1 Inleiding`, `2 Procedure`, `2.1 Planning`, `2.2 Vragen`, `3 Gunning` gives five chunks (or fewer after merging small ones) with paths `2 Procedure > 2.1 Planning`; a section spanning pages 2–3 has `page_start 2`, `page_end 3`.
- Running footer "Aanbestedingsleidraad – Pagina 3 van 12" on every page is removed; the same sentence appearing once in the body is kept.
- "1. De inschrijver levert binnen tien werkdagen na gunning een plan van aanpak aan." is not a heading.
- A 5,000-character section splits into pieces between 200 and 2,000 characters, each with the same path.
- No headings → fixed windows with 200 characters overlap.
- Slides; a sheet of 300 rows → several chunks, each starting with the header line.
- Every chunk's text, split at the page joins, is a substring of its page's stored text (property-style loop over all fixtures).
- A notice document and a superseded guideline produce no chunks; a superseded `qa` version does.
- Fixture snapshot tender 9001: chunks exist for both documents; none for 9004 (scanned).

**Done when.** Tests pass. No model needed.

---

### S2. Lexical ranker (`rag/lexical.py`)

**Functions.**

```python
K1 = 1.2
B = 0.75
TOKEN = re.compile(r"\d+(?:[.,]\d+)+|[^\W_]+", re.UNICODE)   # keeps "4.2" and "1.000.000" whole

def terms(text: str, language: str | None) -> list[str]
def query_terms(query: str) -> list[str]
class Bm25:
    def __init__(self, docs: Sequence[Sequence[str]]) -> None
    def scores(self, query: Sequence[str]) -> list[float]
```

**Rules.** `terms`: NFKC, case-folded tokens; stop words from `ingest/language.py` (`NL_WORDS`, `EN_WORDS`) removed; each remaining token gives itself and its Snowball stem in the document's language (`dutch` for `nl`, `english` for `en`, none otherwise). `query_terms`: each token gives itself, its Dutch stem and its English stem (the query language is not known; a Dutch or English question must both hit Dutch documents). Clause numbers and amounts are never stemmed or split. BM25 standard formula with `K1`, `B`, IDF `ln(1 + (N - n + 0.5)/(n + 0.5))`.

**Tests** (`tests/test_lexical.py`): `terms("Gunningscriteria en wegingen", "nl")` contains `gunningscriteria` and its stem, not `en`; `"artikel 4.2"` keeps `4.2`; `EUR 1.000.000` keeps `1.000.000`; a query "gunningscriterium" matches a document with "gunningscriteria" through the stem; BM25 ranks the document with the rare term first; a document repeating a term 50 times does not beat a short exact one by more than the saturation allows (check `K1` effect); empty query → all zeros.

**Done when.** Tests pass.

---

### S3. Ranking, fusion and result shaping (`rag/search.py`)

**Functions.**

```python
RRF_K = 60
CANDIDATES = 50
RESULT_TEXT_BUDGET = 15_000
Mode = Literal["hybrid", "vector_only"]

@dataclass(frozen=True, slots=True)
class IndexedChunk:
    chunk_id: int
    document_id: int
    doc_type: str
    tag: str | None
    section_path: str
    page_start: int
    page_end: int
    content: str
    embedding: np.ndarray        # normalised
    terms: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class Ranked:
    chunk: IndexedChunk
    score: float
    vector_rank: int | None
    lexical_rank: int | None

class ChunkSource(Protocol):
    def tender_chunks(self, tender_id: int) -> Sequence[IndexedChunk]: ...
    def kb_chunks(self) -> Sequence[IndexedKb]: ...       # IndexedKb: kb_id, doc_slug, doc_type, text, embedding, terms

def rank(query: str, chunks: Sequence[IndexedChunk], embedder: Embedder, *, mode: Mode,
         top_k: int, doc_types: Sequence[str] | None = None) -> list[Ranked]
def fit_budget(hits: Sequence[Ranked], budget: int = RESULT_TEXT_BUDGET) -> list[Ranked]

class Searcher:
    def __init__(self, source: ChunkSource, embedder: Embedder) -> None
    def search_tender(self, tender_id, query, doc_types, top_k, mode: Mode = "hybrid") -> dict[str, Any]
    def search_kb(self, query, doc_types, top_k, mode: Mode = "hybrid") -> dict[str, Any]
```

**Rules.**

- `rank`: filter by `doc_types` first. Vector ranking: dot product with `embed_query(query)`, top `CANDIDATES`. Lexical ranking: BM25 over `terms`, top `CANDIDATES` with a score above 0. Hybrid: RRF score `Σ 1/(RRF_K + rank)` over the rankers that list the chunk; `vector_only`: the vector ranking alone. Sort by score descending, then vector rank, then `chunk_id`. Return `top_k`.
- `fit_budget`: keep hits in order while the sum of `content` lengths stays within the budget; always keep the first hit even when it alone is over (it cannot be, with `MAX_CHARS = 2,000`).
- `search_tender` returns the shape of `agent/fakes.FakeBackend.search_tender_docs`: `{"passages": [{"chunk_id", "document_id", "doc_type", "section", "page_start", "page_end", "text"}]}` (`section` = `section_path`, or the tag when empty). `search_kb`: `{"passages": [{"kb_id", "doc_slug", "doc_type", "text"}]}`.
- No result is an empty list, not an error (the model then rephrases).
- The Searcher keeps no state between calls except an optional per-tender cache of `tender_chunks` (cleared per daily run or eval run).

**Tests** (`tests/test_search.py`, `FakeEmbedder`, synthetic chunks):

- A query sharing rare words with one chunk ranks it first in both modes.
- An exact clause query `"artikel 4.2"` finds the chunk containing `4.2` in hybrid mode even when the fake vectors rank it low (the case hybrid search exists for, `PLAN.md` interview checklist); in `vector_only` it may not.
- RRF arithmetic on a hand-made two-ranker example; tie-breaking order.
- `doc_types=["qa"]` returns only qa chunks.
- Budget: ten 2,000-character hits → seven returned; the JSON of a full result stays under `agent.tools.MAX_RESULT_CHARS` (run it through `run_tool` with a stub backend).
- Result keys equal `FakeBackend`'s; `contact_keys` finds none.

**Done when.** Tests pass.

---

### S4. Snapshot index and the retrieval eval (`rag/snapshot_index.py`)

**Functions.**

```python
EMBED_CACHE_DIR = "data/embeddings"      # git-ignored

class SnapshotChunkSource:                # implements ChunkSource over the frozen snapshot
    def __init__(self, snapshot: Snapshot, embedder: Embedder, kb: FileKb | None,
                 cache_dir: Path = Path(EMBED_CACHE_DIR)) -> None
def snapshot_search(source: SnapshotChunkSource) -> SearchStep   # for evals/targets.py
```

**Rules.**

- `tender_chunks(publication_id)`: `chunk_tender(load_tender(...).docs)`; `chunk_id = document_id * 10_000 + chunk_index`; embeddings from the cache file `<cache_dir>/<embedder.name>/<sha256 of the embedded text>.npy`, computed and written when missing. The cache is keyed by text and model, so a chunking change re-embeds only changed chunks.
- `snapshot_search(source)` returns `step(docs, question, top_k, mode) -> list[SearchHit]`: `rank(...)` over `source.tender_chunks(docs.tender_id)`, mapped to `SearchHit(document_id, page_start, page_end)`.
- `evals/run.py`: new option `--embedding-model` (default `bge-m3`), recorded in `params`. When the retrieval suite is selected and not a dry run, the runner builds the embedder (`load_embedder`, no download); `EmbeddingUnavailable` makes the retrieval suite `not_built` with the message, the other suites run. `evals/targets.py` `default_targets()` gets `search` when an embedder is given (signature: `default_targets(embedder=None, snapshot=None)`).
- The dry run is unchanged (retrieval makes no model call and no cost).

**Tests** (`tests/test_snapshot_index.py`, fixture snapshot, `FakeEmbedder`, `tmp_path` cache):

- Chunks for 9001; the cache holds one file per distinct chunk text; a second source over the same snapshot reads from the cache (count `embed_passages` calls: zero).
- `snapshot_search` on 9001 with the question "Gunningscriteria en wegingen" returns document 1 page 3 first; the existing retrieval metrics compute hit@5 = 1 for that synthetic label (a test label in `tests/fixtures/evals/`, never `evals/datasets/`).
- The runner with `--suites retrieval` and the fake embedder injected: suite `ok`, both modes reported, `embedding_model` in `params`. Without the extra: `not_built`.
- No page text and no question text in the case rows (existing E7 check still passes).

**Done when.** Tests pass; `make evals ARGS="--suites retrieval"` runs live once the labels exist (no cost; Arthur runs it).

---

### S5. Snapshot tool backend and the remaining eval targets (`agent/snapshot_backend.py`)

**Class.**

```python
class SnapshotToolBackend:               # implements agent.tools.ToolBackend for one snapshot tender
    def __init__(self, tender: SnapshotTender, searcher: Searcher, kb: FileKb) -> None
```

**Rules.**

- `get_tender`: pipeline.md PD7 as recommended: built from `tender.notice` and `tender.tenderned` with the same keys as `DbToolBackend.get_tender` (database.md B8), the deadline from `effective_sluitingsdatum` with `deadline_source: "TenderNed API"`. Never `eforms_gt`-only fields, never contacts.
- `search_tender_docs`: `searcher.search_tender(tender.publication_id, ...)`; `read_document_pages`: from `tender.docs` pages, refusing documents of other tenders and `notice` documents with `ToolError`, same result shape as B8; `search_company_kb`: `searcher.search_kb`; `get_company_facts`: `kb.facts(topics)`.
- `evals/targets.py` (completes pipeline.md P12): `match = lambda notice, detail, docs, extraction, env: match_all(extraction, backend=SnapshotToolBackend(...), env=env)`; `chat = ... answer_question(question, [], backend=..., pages=page_lookup(docs), env=env).answer`; `memo` wired as in P12 with `kb=FileKb` of `data/snapshot/kb/`. All three need the embedder; without it they are `not_built` with the reason.
- `SnapshotChunkSource.kb_chunks()` embeds the `FileKb` passages (CVs included: matching and chat may cite staff qualifications; the memo pre-fetch excludes CVs on its own).

**Tests** (`tests/test_snapshot_backend.py`): the tool backend contract tests that `agent/fakes.FakeBackend` passes (`tests/test_agent_tools.py`) run against `SnapshotToolBackend` on tender 9001 (parametrise them); another tender's document refused; a notice document refused; `get_tender` holds no contact canary and no `Kanariepiet`; the eval runner with stubs replaced by the real `match` target and a scripted provider runs the knock-out suite with `match_accuracy` computed.

**Done when.** Tests pass; with labels, KB and the embedder in place, `make evals MAX_COST=10` runs all six suites (or reports `no_labels`/`no_rubric` where Arthur's files are missing).

---

### S6. Index step of the daily job (`rag/indexer.py`)

**Functions.**

```python
class DbChunkSource:                      # implements ChunkSource over Postgres (db/ queries)
class DbIndexer:                          # implements pipeline.md P13 `Indexer`
    def __init__(self, engine: Engine, embedder: Embedder) -> None
    def index(self, tender_id: int) -> None
```

**Rules.**

- `index`: for each searchable document of the tender (rule 2) that has no chunks with `embedding_model = embedder.name`: `chunk_document`, `embed_passages` in batches of 16, insert into `chunks` (`token_count` from `embedder.count_tokens`, `language` from the document) in one transaction per document. Existing chunks of another `embedding_model` for that document are deleted in the same transaction. Idempotent: a second call does nothing.
- `DbChunkSource.tender_chunks`: one query joining `chunks` and `documents`: `documents.tender_id = :id`, `doc_type <> 'notice'`, (`is_current` or `doc_type = 'qa'`), `embedding_model = :model`, `embedding is not null`; `terms` computed in Python from `content` and `language`; the tag from `pipeline.inputs.document_tags` over the tender's `docs_view`. All SQL lives in `db/` (database.md rule 1): add `db/chunk_store.py` with `insert_chunks`, `delete_chunks`, `has_chunks`, `tender_chunks_rows`.
- The daily job's step 5 calls `index` for `docs_ready` tenders without chunks; a failure is a step failure (`failed_step = "index"`).

**Tests** (`db` marker and in-memory variants): index a tender with two documents → chunks with embeddings and `embedding_model`; second call → no new rows (count); a superseded guideline's chunks are not returned by `tender_chunks`; a qa version is; a notice document is never chunked; changing the embedder name re-indexes; a failure in embedding rolls back that document's chunks.

**Done when.** Tests pass in CI.

---

### S7. KB index and the database KB source (`rag/kb_index.py`)

**Functions.**

```python
def index_kb(engine: Engine, kb: FileKb, embedder: Embedder) -> str   # returns kb_version
class DbKb:                                # implements KbSource over company_kb for one kb_version
    def __init__(self, engine: Engine, kb_version: str) -> None
```

**Rules.**

- `index_kb`: if rows for `kb.kb_version` and `embedder.name` exist, return; else insert one `company_kb` row per passage (`doc_slug`, `doc_type`, `title`, `language`, `chunk_index`, `content`, `embedding`, `kb_version`), with `facts` = the list of the passage's facts (`topic`, `value`) from the front matter. One transaction.
- The daily job calls `index_kb` at start with the `kb/` folder; the current KB is the folder's `kb_version`. Older versions stay (unique key includes `kb_version`) so old memos' `kb_id`s still resolve.
- `DbKb.passages` and `DbKb.facts` return the same shapes as `FileKb`, with `kb_id = company_kb.id`. `DbChunkSource.kb_chunks` reads the same rows.

**Tests** (`db` marker): indexing twice inserts once; a changed file gives a new version with new rows, old rows kept; `DbKb.facts` equals `FileKb.facts` for the same folder except `kb_id` values (compare with the IDs mapped); CV passages are present in `kb_chunks` and absent from `prefetch_kb(DbKb)` (pipeline.md P5).

**Done when.** Tests pass in CI.

---

### S8. Daily-job wiring and the re-index command

**Changes.**

- `jobs/daily.py` `main`: `embedder = load_embedder()` (the worker image has the model in its cache volume; missing → exit 2 with the download command); `searcher = Searcher(DbChunkSource(engine, embedder), embedder)`; `backends = lambda tender_id: DbToolBackend(engine, searcher=searcher, kb=DbKb(engine, kb_version))`; `indexer = DbIndexer(engine, embedder)`; `kb = DbKb(...)` for the memo pre-fetch.
- New `python -m rag.reindex [--tender ID | --all] [--model KEY]`: re-chunks and re-embeds (after a chunking or model change); prints counts only.

**Tests.** The pipeline.md P14 happy-path test with `DbIndexer`, `DbChunkSource` and `FakeEmbedder` (`db` marker): the match loop's `search_tender_docs` call returns passages from the indexed fixture documents.

**Done when.** Tests pass. Manual check (`PLAN.md` Phase 1 "done when"): on a real tender, `search_tender_docs("gunningscriteria")` returns the award-criteria section with its page.

---

### S9. Measurement and the model choice (Arthur runs; no code)

1. Retrieval labels (`evals/datasets/retrieval.jsonl`, Arthur's) for 30 questions.
2. `make evals ARGS="--suites retrieval --embedding-model bge-m3"` and the same with `multilingual-e5-large` (SD4). Both modes reported per run.
3. Record in `DECISIONS.md`: hit@5 and MRR@10 per model and mode, the per-language split, the indexing time per tender on the VM, the chosen model and why. Pin its revision.
4. If hybrid does not beat `vector_only`, say so in the README; that is a result, not a failure.

## 4. Risks and edge cases

| Source | Risk or edge case | Handled in |
|---|---|---|
| 2.5, 2.10 | Notice copies in the corpus make the extraction eval circular. | Rule 2; S1 and S6 tests |
| 2.10 | Superseded versions must stay for audit but not be searched; every qa version is searched. | S1 (not chunked), S6 query filter |
| 3 | Approximate index with a filter can return fewer than k rows. | No HNSW; exact scan per tender (rule 1) |
| B5 | A quote across a page break fails the check. | S1 keeps page joins at line ends; documented false negatives |
| Tool limit | A result over 20,000 characters is an error. | S3 `fit_budget` |
| Clause queries | "artikel 4.2" must hit exactly. | S2 tokeniser keeps `4.2`; S3 hybrid test |
| Cross-language | An English question over Dutch documents. | BGE-M3 is multilingual; S2 stems the query in both languages |
| XLSX | Sheets lose the header when chunked. | S1 header repeated |
| Running headers | Footers like "Pagina 3 van 40" pollute every chunk. | S1 edge-line removal, contiguous text kept |
| Model download | 2 GB fetched silently in a job or CI. | S0 `allow_download=False`; explicit download command |
| Install size | torch CUDA wheels in Docker. | CPU index; `embed` extra (SD3) |
| Eval leakage | Retrieval must not see ground truth; chunks come from document pages only. | S4 uses `TenderDocs` only |
| Privacy | KB CVs hold (fictional) staff names; tender pages hold real names. | Contacts are never chunk content sources beyond page text; the gateway redacts tool results; `contact_keys` guard on result keys |
| Run-to-run | Embeddings are deterministic on CPU; the evals cache them. | S4 cache keyed by model and text |

## 5. What is still open after this plan

- SD1 to SD5.
- Q7 (tables) may later change how sheet and table pages are chunked.
- The retrieval labels and the model choice (S9) are Arthur's.
