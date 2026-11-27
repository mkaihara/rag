# Code review notes, November 2026

**Scope:** `config.yaml` and every module: `rag/app.py`, `retrieve.py`,
`store.py`, `providers.py`, `chunker.py`, `ingest.py`, `evaluate.py` and
`rag/static/app.js`, plus a secrets audit of the history and the deployment.
The notes describe `main` as of 2026-11-27 (commit `0307a28`).

**Method:** I read each module, explained it from memory, then checked every
explanation against the code, the configuration and measurements. The points
I got wrong at first are corrected below, not listed separately.

## Request path (`app.py`)

- **Rate limit first.** Each client IP gets a sliding 60-second window of
  6 questions, and all clients together share a window of 60. The check runs
  before the question is validated, so rejected requests use a slot too.
- **Client address.** On Fly.io it comes from `Fly-Client-IP`, which Fly's
  proxy overwrites with the address of the TCP connection it received.
  Elsewhere, `X-Forwarded-For` is trusted only for the configured number of
  proxy hops; with neither set, the TCP peer is used.
- **No lock needed.** `check_rate_limit()` is synchronous and is called from
  `async def ask()`, with no `await` between reading a window and adding the
  new hit. One process runs one event loop, so no other request runs in
  between. Several processes or machines would each count separately, and
  the global limit would then need shared state.
- **Concurrency.** Retrieval makes blocking database calls, so it runs in a
  thread pool; the LLM answer streams asynchronously as NDJSON events:
  `sources`, then `token`s, then `done` with timings (or `error` with a short
  id).
- **Hardening.** Request bodies over 16 KB are refused with 413. Every response
  carries a Content-Security-Policy that allows scripts from the app's own
  origin only.

## Retrieval (`retrieve.py`, `store.py`, `providers.py`)

- **Query embedding.** Queries, not documents, get the instruction prefix that
  Qwen3-Embedding is trained with; the instruction names this corpus's topics.
- **Dimensions.** Both profiles store 1,024 dimensions. The cloud model
  (`qwen3-embedding-8b`) is 4,096-dimensional natively and is asked for 1,024
  (Matryoshka truncation); the local model (`qwen3-embedding:0.6b`) is 1,024
  natively. `_normalize()` truncates if needed and rescales every vector to
  unit length, which truncation would otherwise break. For unit vectors,
  cosine similarity equals the dot product; the query still uses pgvector's
  cosine operator (`<=>`) with an HNSW `vector_cosine_ops` index.
- **Keyword search.** A generated `tsvector` column with a GIN index; the
  question's terms are ORed, because ANDing every word of a sentence is too
  strict.
- **Fusion.** Reciprocal Rank Fusion over 30 candidates from each search:
  score = Σ 1/(60 + rank). Ranks are used because cosine and `ts_rank_cd`
  scores are on unrelated scales. The constant 60, the usual value from the
  original RRF paper, makes the top ranks nearly equal (1/61 for first, 1/63
  for third), so a passage both searches rank third (2/63) beats one that only
  a single search ranks first (1/61).
- **Duplicate removal.** Passages are compared as sets of 5-word shingles, and
  a passage that shares at least 60% of the smaller set with a better one is
  dropped. The threshold was chosen, not tuned by experiment. On the current
  three-document corpus no pair of chunks reaches it in either profile (the
  highest overlap is 59%, between two chunks of the thesis), so today the step
  removes nothing. It guards against near-identical passages, which the
  earlier, larger corpus had (several versions of the same papers).
- **Prompt.** The top 6 passages go to the LLM with a system prompt that
  allows answers only from the numbered sources, requires `[n]` citations, and
  asks it to say when the sources don't answer.

## Chunking (`chunker.py`)

- Chunks follow the section structure and never split a display formula, a
  table or a code block. A short section introduction joins its first
  subsection. An oversized table becomes groups of text rows with its caption
  repeated; other oversized blocks are split at line boundaries. Reference
  lists are left out.
- Sizes: about 350 tokens (at most 500) locally, where CPU prompt processing
  is the bottleneck, and about 700 (at most 1,000) in the cloud.

## Rendering (`static/app.js`)

Formulas are cut out before Markdown is parsed, or underscores and asterisks
inside them would turn into emphasis. The Markdown HTML is sanitized with
DOMPurify. Only then are KaTeX output and `[n]` citation links inserted, as
DOM nodes and only into text nodes, never spliced into the sanitized HTML
string.

## Secrets

- API keys and the database URL come from environment variables only
  (`api_key_env` in `config.yaml`, `DATABASE_URL`). `.env` files are ignored by
  both git and Docker and were never committed.
- A pattern scan on 2026-11-27 found no credentials. It covered the git
  history of every branch (55 commits), all pull request text, every GitHub
  Actions log and the recent Fly logs. GitHub Secret Protection and push
  protection are enabled on the repository.
- The hosted app connects as `rag_app`, a read-only database role. That is a
  second layer behind parameterized SQL, not the first.

## Evaluation findings

The evaluation has 15 questions: 13 with verbatim evidence quotes, and 2 whose
answer is not in the papers, to test refusals. Results are in
[`eval/results/summary.md`](../eval/results/summary.md).

The local profile ranks the first relevant passage higher (MRR@6 0.77 against
0.71, recall@1 0.58 against 0.42). However, it misses the evidence for q01 and
q13 entirely within its top 6 (recall@6 0.85 against 1.00). With 13 questions,
differences of one or two questions are within noise.

## Known gaps and open questions

- **Rate limits are per process.** That is enough for one machine; more
  instances would need shared state, such as Redis or a database row.
- **Small evaluation.** 13 answerable questions can't separate small
  differences; the plan is 30 or more.
- **Local misses on q01 and q13: cause not established.** The working
  hypothesis is the chunk-size trade-off: smaller chunks match more precisely
  but carry less context. It is untested, and confounded, because the two
  profiles also use different embedding models (0.6B against 8B). To separate
  the two factors, index the local chunking with the cloud embedder and the
  reverse, then rerun `python -m rag.evaluate ask` on both.
- **Duplicate threshold untuned.** It has no effect on the current corpus, so
  it can't be tuned until the corpus contains overlapping documents.
