"""Hybrid retrieval and prompt assembly.

1. Vector search (cosine, pgvector HNSW) and keyword search (Postgres
   full-text) each return `candidates` chunks.
2. Reciprocal Rank Fusion merges the two rankings. Keyword search catches
   exact identifiers the embedding may blur (MQRTR, SD2, PS3, Cell SPU).
3. Near-duplicates are dropped: papers often repeat passages (between
   versions, or a thesis reusing a paper), and two copies of one passage
   would waste the small local context.
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
    """Reciprocal Rank Fusion: a row scores 1 / (RRF_K + rank) in every
    ranking it appears in (rank from 1), and the scores are added up."""
    scores: dict[str, float] = {}
    rows: dict[str, dict[str, Any]] = {}
    for ranking in rankings:
        for position, row in enumerate(ranking):
            row_id = row["id"]
            rank = position + 1
            previous_score = scores.get(row_id, 0.0)
            scores[row_id] = previous_score + 1.0 / (RRF_K + rank)
            if row_id not in rows:  # keep the first copy of the row
                rows[row_id] = row

    # Highest score first; rows with equal scores keep their first-seen order.
    ordered_ids = sorted(scores, key=scores.get, reverse=True)
    fused = []
    for row_id in ordered_ids:
        fused_row = dict(rows[row_id])
        fused_row["rrf"] = scores[row_id]
        fused.append(fused_row)
    return fused


def shingles(text: str) -> set[tuple[str, ...]]:
    """The set of all runs of SHINGLE consecutive words in the text."""
    words = re.findall(r"\w+", text.lower())
    if len(words) < SHINGLE:
        return {tuple(words)}
    result = set()
    for start in range(len(words) - SHINGLE + 1):
        result.add(tuple(words[start : start + SHINGLE]))
    return result


def dedupe(rows: list[dict[str, Any]], threshold: float, limit: int) -> list[dict[str, Any]]:
    """Keep rows in order, skipping any whose shingles mostly overlap a kept row."""
    kept_rows: list[dict[str, Any]] = []
    kept_shingles: list[set] = []
    for row in rows:
        row_shingles = shingles(row["text"])
        is_duplicate = False
        for other in kept_shingles:
            # Shared shingles as a share of the smaller set (0 to 1).
            shared = len(row_shingles & other)
            smaller = max(1, min(len(row_shingles), len(other)))
            if shared / smaller >= threshold:
                is_duplicate = True
                break
        if is_duplicate:
            continue
        kept_rows.append(row)
        kept_shingles.append(row_shingles)
        if len(kept_rows) == limit:
            break
    return kept_rows


def retrieve(conn: psycopg.Connection, embedder: Embedder, settings: Settings, question: str) -> list[dict[str, Any]]:
    cfg = settings.retrieval
    candidates = int(cfg.get("candidates", 30))
    threshold = float(cfg.get("dedupe_threshold", 0.6))
    top_k = int(cfg.get("top_k", 6))

    question_vector = embedder.embed_query(question)
    by_vector = store.vector_search(conn, settings.table, question_vector, candidates)
    rankings = [by_vector]
    if cfg.get("hybrid", True):
        by_keyword = store.keyword_search(conn, settings.table, question, candidates)
        rankings.append(by_keyword)
    fused = rrf(*rankings)

    # Show the cosine similarity of rows the vector search found (None for
    # rows only the keyword search found).
    similarity_by_id = {}
    for row in by_vector:
        similarity_by_id[row["id"]] = float(row["score"])
    for row in fused:
        row["similarity"] = similarity_by_id.get(row["id"])

    return dedupe(fused, threshold, top_k)


SYSTEM_PROMPT = """You are a research assistant answering questions about the publications of \
Marcelo E. Kaihara and his co-authors on hardware algorithms for modular arithmetic \
(Montgomery, bipartite and binary/Euclidean modular multiplication and division) and on \
the security of RSA and elliptic curve cryptography (integer factorization, Pollard's rho \
method, computations on clusters and PlayStation 3 consoles).

Rules:
- Answer only from the numbered sources provided. If they do not contain the answer, \
say so plainly instead of guessing.
- Cite the sources you use with their numbers in square brackets, e.g. [1] or [2][4], \
right after the claim they support.
- Write mathematics in LaTeX: $...$ inline and $$...$$ for display equations.
- Tables in the sources may appear as lines like "Row 3: A=..., B=..."; read them as table rows.
- Be concise and precise. Prefer the exact numbers, notation and terminology of the papers."""


def build_messages(question: str, sources: list[dict[str, Any]]) -> list[dict[str, str]]:
    blocks = []
    for n, source in enumerate(sources, start=1):
        blocks.append(f"[{n}] {source['reference']}\n{source['text']}")
    separator = "\n\n---\n\n"
    user = "Sources:\n\n" + separator.join(blocks) + separator + f"Question: {question}"
    system_message = {"role": "system", "content": SYSTEM_PROMPT}
    user_message = {"role": "user", "content": user}
    return [system_message, user_message]
