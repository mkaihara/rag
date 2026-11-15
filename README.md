# Ask the Papers — RAG over math-heavy research papers

Retrieval-augmented question answering over my publications on hardware
algorithms for modular arithmetic (Montgomery multiplication, modular
division, RNS bases) and Pollard's rho method for the elliptic-curve
discrete logarithm problem. Answers cite the passages they come from, and
formulas render as LaTeX.

The same code runs in two configurations, selected with `RAG_PROFILE`:

| | `local` (offline, `docker compose`) | `cloud` (hosted demo) |
|---|---|---|
| LLM | `qwen3.5:4b` on Ollama, CPU | `openai/gpt-6-luna` via OpenRouter |
| Embeddings | `qwen3-embedding:0.6b` on Ollama | `qwen/qwen3-embedding-8b` via OpenRouter |
| Vector store | Postgres + pgvector container | Neon Postgres + pgvector |
| Chunks | ~350 tokens (max 500) | ~700 tokens (max 1000) |

Everything lives in [`config.yaml`](config.yaml).

## Run it locally (no API keys)

Requires Docker with about 8 GB of memory available.

```sh
docker compose up --build
```

Then open <http://localhost:8000>. The first start downloads about 4 GB of
models; the corpus vectors come from the committed embedding cache
(`embeddings/local.jsonl.gz`), so indexing takes seconds instead of the
~30 minutes a CPU needs to embed 584 chunks.

**Expect about 1.5–2.5 minutes per answer on CPU.** Measured in Docker on an
Apple Silicon laptop (10 cores): retrieval under 1 s, then `qwen3.5:4b`
reads the ~3,000-token prompt at ~30 tokens/s before the first word appears,
and writes at ~5 tokens/s.

**Much faster on a Mac with native Ollama** (Docker cannot use the GPU):

```sh
ollama serve   # from https://ollama.com, in another terminal
OLLAMA_BASE_URL=http://host.docker.internal:11434 docker compose up --build
```

Other models: `LLM_MODEL=... docker compose up`. `qwen3.5:2b` is about twice
as fast but noticeably less faithful to the sources in testing.

## How it works

```
data/<doc>/*.md ──► chunker ──► embeddings ──► Postgres (pgvector + full-text)
                                                     │
question ──► embed ──► vector search ─┐              │
         └─────────► keyword search ──┴─► RRF fusion ─► dedupe ─► top-k ─► LLM ─► streamed answer + sources
```

1. **Source conversion.** The papers were written in LaTeX.
   [`scripts/tex_to_md.py`](scripts/tex_to_md.py) converts them with pandoc
   into Markdown with `$...$` math, numbered citations and resolved
   cross-references, so no OCR is involved and formulas stay exact.
2. **Section-aware chunking** ([`rag/chunker.py`](rag/chunker.py)). Chunks
   follow the heading structure and carry the document title, authors,
   section path, pages and a citation string. Reference lists are left out.
   Display math, tables and code are not split, except when a block exceeds
   the profile's maximum: tables then become row groups written as text
   (`Row 12: f=200MHz, area=3.2mm²`) with the caption repeated, and listings
   split at line boundaries without breaking math.
3. **Hybrid retrieval** ([`rag/retrieve.py`](rag/retrieve.py)). Cosine
   search over embeddings plus Postgres full-text search, merged with
   reciprocal rank fusion, so exact identifiers (`MQRTR`, `SD2`, `PS3`) are
   found even when embeddings blur them. Near-duplicate passages (the
   theses reuse the papers' text) are dropped.
4. **Grounded generation.** The LLM answers only from the numbered sources
   and cites them as `[n]`; the page links each citation to its passage.

## Deploy the hosted version

1. Create a Neon project and copy its connection string.
2. Create an OpenRouter key with a spending limit.
3. Build the index from your machine:
   ```sh
   export RAG_PROFILE=cloud
   export DATABASE_URL='postgresql://...neon.tech/rag?sslmode=require'
   export OPENROUTER_API_KEY='sk-or-v1-...'
   uv run python -m rag.ingest --data data
   ```
4. Deploy the `Dockerfile` to any container host (Fly.io, Render, Railway,
   Cloud Run…) with `RAG_PROFILE=cloud`, `DATABASE_URL` and
   `OPENROUTER_API_KEY` set. The app listens on `$PORT` (default 8000) and
   rate-limits each client IP (`server.rate_limit_per_minute`) and all
   clients together (`server.global_rate_limit_per_minute`).
   Client IPs come from `X-Forwarded-For` only as far as
   `server.trusted_proxy_hops` (default 1, one platform proxy) allows; set
   `TRUSTED_PROXY_HOPS` to match your host, or 0 if the container is
   reachable without the proxy. Errors shown on the page are generic, with
   an id to find the details in the app log.

## Development

```sh
uv sync
uv run python scripts/fetch_vendor.py         # page JS/CSS (KaTeX, marked, DOMPurify)
uv run python -m rag.chunker data --profile local   # inspect chunks in index/local/
uv run pytest
# after changing data/ or chunking: rebuild the index, then refresh the cache
docker compose run --rm -v "$PWD/embeddings:/app/embeddings" indexer python -m rag.ingest --export-cache
DATABASE_URL=... uv run uvicorn rag.app:app --reload
```

| Path | Purpose |
|---|---|
| `config.yaml` | Profiles: models, chunk sizes, retrieval, limits |
| `rag/chunker.py` | Section-aware Markdown chunking |
| `rag/ingest.py` | Chunk → embed (via cache) → load into Postgres (skips if unchanged) |
| `embeddings/` | Committed embedding cache (float16, keyed by model + chunk text) |
| `rag/retrieve.py` | Hybrid search, fusion, dedupe, prompt |
| `rag/providers.py` | Ollama and OpenRouter clients |
| `rag/store.py` | pgvector schema and queries |
| `rag/app.py`, `rag/static/` | FastAPI streaming API and the web page |
| `scripts/tex_to_md.py` | One-off LaTeX → Markdown conversion |
