"""Postgres + pgvector storage: one chunks_<profile> table per profile.

Each table has the chunk text and metadata, its embedding (cosine HNSW
index) and a generated tsvector (GIN index) for keyword search. The
index_meta table records which corpus/model fingerprint a table was built
from, so the indexer can skip work when nothing changed.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

META_DDL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS index_meta (
    profile     text PRIMARY KEY,
    fingerprint text NOT NULL,
    embed_model text NOT NULL,
    dimensions  int  NOT NULL,
    chunks      int  NOT NULL,
    built_at    timestamptz NOT NULL DEFAULT now()
);
"""

COLUMNS = ["id", "doc_id", "source", "title", "authors", "section", "section_path", "part", "pages", "reference", "tokens", "text"]
RESULT_COLUMNS = ", ".join(c for c in COLUMNS)


def vector_literal(v: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in v) + "]"


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, row_factory=dict_row)


def ensure_meta(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute(META_DDL)


def stored_fingerprint(conn: psycopg.Connection, profile: str, table: str) -> str | None:
    exists = conn.execute("SELECT to_regclass(%s) AS t", (table,)).fetchone()["t"]
    if not exists:
        return None
    row = conn.execute("SELECT fingerprint FROM index_meta WHERE profile = %s", (profile,)).fetchone()
    return row["fingerprint"] if row else None


def replace_index(
    conn: psycopg.Connection,
    table: str,
    profile: str,
    dims: int,
    records: list[dict[str, Any]],
    vectors: list[list[float]],
    fingerprint: str,
    embed_model: str,
) -> None:
    """Load all chunks into a fresh table and swap it in, in one transaction."""
    t = sql.Identifier(table)
    with conn.transaction():
        conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(t))
        conn.execute(
            sql.SQL(
                """CREATE TABLE {t} (
                    id           text PRIMARY KEY,
                    doc_id       text NOT NULL,
                    source       text NOT NULL,
                    title        text NOT NULL,
                    authors      text[] NOT NULL,
                    section      text NOT NULL,
                    section_path text[] NOT NULL,
                    part         text NOT NULL,
                    pages        int[],
                    reference    text NOT NULL,
                    tokens       int  NOT NULL,
                    text         text NOT NULL,
                    embedding    vector({dims}) NOT NULL,
                    tsv          tsvector GENERATED ALWAYS AS (
                                     to_tsvector('english', title || ' ' || section || ' ' || text)) STORED
                )"""
            ).format(t=t, dims=sql.Literal(dims))
        )
        with conn.cursor() as cur:
            cur.executemany(
                sql.SQL("INSERT INTO {} ({}, embedding) VALUES ({}, %s::vector)").format(
                    t,
                    sql.SQL(", ").join(map(sql.Identifier, COLUMNS)),
                    sql.SQL(", ").join(sql.Placeholder() * len(COLUMNS)),
                ),
                [[r[c] for c in COLUMNS] + [vector_literal(v)] for r, v in zip(records, vectors, strict=True)],
            )
        conn.execute(sql.SQL("CREATE INDEX ON {} USING hnsw (embedding vector_cosine_ops)").format(t))
        conn.execute(sql.SQL("CREATE INDEX ON {} USING gin (tsv)").format(t))
        conn.execute(
            """INSERT INTO index_meta (profile, fingerprint, embed_model, dimensions, chunks)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (profile) DO UPDATE SET fingerprint = EXCLUDED.fingerprint,
                   embed_model = EXCLUDED.embed_model, dimensions = EXCLUDED.dimensions,
                   chunks = EXCLUDED.chunks, built_at = now()""",
            (profile, fingerprint, embed_model, dims, len(records)),
        )


def vector_search(conn: psycopg.Connection, table: str, query_vec: Sequence[float], limit: int) -> list[dict]:
    q = sql.SQL(
        "SELECT {cols}, 1 - (embedding <=> %(v)s::vector) AS score FROM {t} ORDER BY embedding <=> %(v)s::vector LIMIT %(n)s"
    ).format(cols=sql.SQL(RESULT_COLUMNS), t=sql.Identifier(table))
    return conn.execute(q, {"v": vector_literal(query_vec), "n": limit}).fetchall()


def keyword_search(conn: psycopg.Connection, table: str, question: str, limit: int) -> list[dict]:
    # OR the question's terms (plainto_tsquery ANDs them, which is too strict
    # for natural-language questions); ts_rank_cd rewards dense matches.
    q = sql.SQL(
        "SELECT {cols}, ts_rank_cd(tsv, query) AS score FROM {t}, "
        "to_tsquery('english', replace(plainto_tsquery('english', %(q)s)::text, '&', '|')) AS query "
        "WHERE query::text <> '' AND tsv @@ query ORDER BY score DESC LIMIT %(n)s"
    ).format(cols=sql.SQL(RESULT_COLUMNS), t=sql.Identifier(table))
    return conn.execute(q, {"q": question, "n": limit}).fetchall()


def index_status(conn: psycopg.Connection, profile: str) -> dict | None:
    row = conn.execute("SELECT * FROM index_meta WHERE profile = %s", (profile,)).fetchone()
    return json.loads(json.dumps(row, default=str)) if row else None


def all_vectors(conn: psycopg.Connection, table: str) -> dict[str, list[float]]:
    rows = conn.execute(sql.SQL("SELECT id, embedding::text AS v FROM {}").format(sql.Identifier(table))).fetchall()
    return {r["id"]: [float(x) for x in r["v"].strip("[]").split(",")] for r in rows}
