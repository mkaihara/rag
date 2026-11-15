"""Chunk the corpus, embed the chunks and load them into Postgres.

    RAG_PROFILE=local DATABASE_URL=... python -m rag.ingest [--data data] [--force]
    RAG_PROFILE=local DATABASE_URL=... python -m rag.ingest --export-cache

Skips the work when the stored index was built from the same chunks with
the same embedding model (compared by fingerprint), so `docker compose up`
only embeds on the first start or after the data or config changed.

Embedding the corpus on a laptop CPU takes about half an hour, so vectors
are also kept in a committed cache, embeddings/<profile>.jsonl.gz, keyed by
model and chunk text. Ingest embeds only chunks missing from the cache.
After changing the corpus or the chunking, rebuild the index and refresh
the cache with --export-cache.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import math
import os
import struct
import sys
import time
from pathlib import Path

from rag import store
from rag.chunker import chunk_corpus
from rag.config import REPO_ROOT, Settings, load_settings
from rag.providers import Embedder, ProviderError

BATCH = {"ollama": 8, "openrouter": 64}
CACHE_DIR = Path(os.environ.get("RAG_EMBED_CACHE", REPO_ROOT / "embeddings"))


def log(msg: str) -> None:
    print(msg, flush=True)


def fingerprint(records: list[dict], settings: Settings) -> str:
    h = hashlib.sha256()
    h.update(json.dumps([settings.embedding.provider, settings.embedding.model, settings.dimensions]).encode())
    for r in records:
        h.update(r["id"].encode())
        h.update(r["embed_text"].encode())
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------
# Embedding cache: one gzipped JSON line per chunk, {"key", "v"}, where v is
# the vector as base64 float16 (half the size, no measurable loss for
# cosine ranking).


def cache_key(settings: Settings, embed_text: str) -> str:
    h = hashlib.sha256(f"{settings.embedding.model}\x00{settings.dimensions}\x00{embed_text}".encode())
    return h.hexdigest()[:24]


def pack(v: list[float]) -> str:
    return base64.b64encode(struct.pack(f"<{len(v)}e", *v)).decode()


def unpack(s: str) -> list[float]:
    raw = base64.b64decode(s)
    v = list(struct.unpack(f"<{len(raw) // 2}e", raw))
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def cache_path(settings: Settings) -> Path:
    return CACHE_DIR / f"{settings.profile}.jsonl.gz"


def load_cache(settings: Settings) -> dict[str, list[float]]:
    path = cache_path(settings)
    if not path.exists():
        return {}
    with gzip.open(path, "rt", encoding="utf-8") as f:
        entries = [json.loads(line) for line in f]
    return {e["key"]: unpack(e["v"]) for e in entries}


def write_cache(settings: Settings, records: list[dict], vectors: list[list[float]]) -> Path:
    path = cache_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    # mtime=0 keeps the file byte-identical when the vectors are unchanged
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:
        for r, v in zip(records, vectors, strict=True):
            gz.write((json.dumps({"key": cache_key(settings, r["embed_text"]), "v": pack(v)}) + "\n").encode())
    return path


# --------------------------------------------------------------------------


def embed_all(embedder: Embedder, records: list[dict], batch: int) -> list[list[float]]:
    vectors: list[list[float]] = []
    start = time.monotonic()
    for i in range(0, len(records), batch):
        texts = [r["embed_text"] for r in records[i : i + batch]]
        for attempt in range(3):
            try:
                vectors += embedder.embed(texts)
                break
            except ProviderError as e:
                if attempt == 2:
                    raise
                log(f"  retrying after error: {e}")
                time.sleep(2 * (attempt + 1))
        done = len(vectors)
        elapsed = time.monotonic() - start
        eta = elapsed / done * (len(records) - done)
        log(f"  embedded {done:4}/{len(records)}  ({elapsed:5.0f}s elapsed, ~{eta:4.0f}s left)")
    return vectors


def export_cache(settings: Settings, records: list[dict], fp: str) -> None:
    """Write the cache from the vectors already stored in Postgres."""
    with store.connect(settings.database_url) as conn:
        if store.stored_fingerprint(conn, settings.profile, settings.table) != fp:
            sys.exit(f"{settings.table} was not built from the current chunks; run rag.ingest first")
        stored = store.all_vectors(conn, settings.table)
    vectors = [stored[r["id"]] for r in records]
    log(f"wrote {len(vectors)} vectors to {write_cache(settings, records, vectors)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--data", default=os.environ.get("RAG_DATA_DIR", "data"))
    ap.add_argument("--profile", help="config.yaml profile (default: $RAG_PROFILE or default_profile)")
    ap.add_argument("--force", action="store_true", help="rebuild even if the index is up to date")
    ap.add_argument("--no-cache", action="store_true", help="ignore the embedding cache and embed everything")
    ap.add_argument("--export-cache", action="store_true", help="write the embedding cache from the stored index")
    args = ap.parse_args()

    settings = load_settings(args.profile)
    if not settings.database_url:
        sys.exit("DATABASE_URL is not set")
    log(f"profile {settings.profile}: embedding with {settings.embedding.model} ({settings.dimensions} dims)")

    records = chunk_corpus(Path(args.data).resolve(), settings.chunking, log=lambda m: None)
    docs = len({r["doc_id"] for r in records})
    log(f"chunked {docs} documents into {len(records)} chunks")
    fp = fingerprint(records, settings)
    if args.export_cache:
        export_cache(settings, records, fp)
        return

    with store.connect(settings.database_url) as conn:
        store.ensure_meta(conn)
        if not args.force and store.stored_fingerprint(conn, settings.profile, settings.table) == fp:
            log(f"index {settings.table} is up to date ({fp}); nothing to do")
            return

        cache = {} if args.no_cache else load_cache(settings)
        keys = [cache_key(settings, r["embed_text"]) for r in records]
        missing = [r for r, k in zip(records, keys, strict=True) if k not in cache]
        log(f"embedding cache: {len(records) - len(missing)} of {len(records)} chunks cached")
        if missing:
            embedder = Embedder(settings.embedding, settings.dimensions)
            fresh = embed_all(embedder, missing, BATCH.get(settings.embedding.provider, 16))
            cache.update({cache_key(settings, r["embed_text"]): v for r, v in zip(missing, fresh, strict=True)})
        vectors = [cache[k] for k in keys]
        store.replace_index(
            conn, settings.table, settings.profile, settings.dimensions, records, vectors, fp, settings.embedding.model
        )
    log(f"loaded {len(records)} chunks into {settings.table} ({fp})")


if __name__ == "__main__":
    main()
