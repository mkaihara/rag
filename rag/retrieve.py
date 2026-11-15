"""Hybrid retrieval and prompt assembly.

1. Vector search (cosine, pgvector HNSW) and keyword search (Postgres
   full-text) each return `candidates` chunks.
2. Reciprocal Rank Fusion merges the two rankings. Keyword search catches
   exact identifiers the embedding may blur (MQRTR, SD2, PS3, Cell SPU).
3. Near-duplicates are dropped: the theses reuse text from the papers, and
   two copies of one passage would waste the small local context.
"""

from __future__ import annotations

import re
from typing import Any

import psycopg

from rag import store
from rag.config import Settings
from rag.providers import Embedder

RRF_K = 60
SHINGLE = 5  # words per shingle for duplicate detection


def rrf(*rankings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scores: dict[str, float] = {}
    rows: dict[str, dict[str, Any]] = {}
    for ranking in rankings:
        for rank, row in enumerate(ranking):
            scores[row["id"]] = scores.get(row["id"], 0.0) + 1.0 / (RRF_K + rank + 1)
            rows.setdefault(row["id"], row)
    ordered = sorted(scores, key=scores.get, reverse=True)
    return [{**rows[i], "rrf": scores[i]} for i in ordered]


def shingles(text: str) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", text.lower())
    if len(words) < SHINGLE:
        return {tuple(words)}
    return {tuple(words[i : i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}


def dedupe(rows: list[dict[str, Any]], threshold: float, limit: int) -> list[dict[str, Any]]:
    """Keep rows in order, skipping any whose shingles mostly overlap a kept row."""
    kept: list[tuple[dict[str, Any], set]] = []
    for row in rows:
        sh = shingles(row["text"])
        if any(len(sh & other) / max(1, min(len(sh), len(other))) >= threshold for _, other in kept):
            continue
        kept.append((row, sh))
        if len(kept) == limit:
            break
    return [r for r, _ in kept]


def retrieve(conn: psycopg.Connection, embedder: Embedder, settings: Settings, question: str) -> list[dict[str, Any]]:
    cfg = settings.retrieval
    n = int(cfg.get("candidates", 30))
    vec = embedder.embed_query(question)
    by_vector = store.vector_search(conn, settings.table, vec, n)
    rankings = [by_vector]
    if cfg.get("hybrid", True):
        rankings.append(store.keyword_search(conn, settings.table, question, n))
    fused = rrf(*rankings)
    vector_score = {r["id"]: float(r["score"]) for r in by_vector}
    for r in fused:
        r["similarity"] = vector_score.get(r["id"])
    return dedupe(fused, float(cfg.get("dedupe_threshold", 0.6)), int(cfg.get("top_k", 6)))


SYSTEM_PROMPT = """You are a research assistant answering questions about the publications of \
Marcelo E. Kaihara and his co-authors on hardware algorithms for modular arithmetic \
(Montgomery multiplication, modular division, RNS bases) and on Pollard's rho method \
for elliptic curve discrete logarithms.

Rules:
- Answer only from the numbered sources provided. If they do not contain the answer, \
say so plainly instead of guessing.
- Cite the sources you use with their numbers in square brackets, e.g. [1] or [2][4], \
right after the claim they support.
- Write mathematics in LaTeX: $...$ inline and $$...$$ for display equations.
- Tables in the sources may appear as lines like "Row 3: A=..., B=..."; read them as table rows.
- Be concise and precise. Prefer the exact numbers, notation and terminology of the papers."""


def build_messages(question: str, sources: list[dict[str, Any]]) -> list[dict[str, str]]:
    blocks = [f"[{i}] {s['reference']}\n{s['text']}" for i, s in enumerate(sources, 1)]
    user = "Sources:\n\n" + "\n\n---\n\n".join(blocks) + f"\n\n---\n\nQuestion: {question}"
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]
