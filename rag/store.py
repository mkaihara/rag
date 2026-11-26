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
RESULT_COLUMNS = ", ".join(COLUMNS)


def vector_literal(v: Sequence[float]) -> str:
    """The vector as pgvector text, e.g. "[0.1,-0.25,0.03]" (7 significant digits)."""
    numbers = []
    for x in v:
        numbers.append(f"{x:.7g}")
    return "[" + ",".join(numbers) + "]"


def connect(dsn: str) -> psycopg.Connection:
    return psycopg.connect(dsn, row_factory=dict_row)


def ensure_meta(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute(META_DDL)


def stored_fingerprint(conn: psycopg.Connection, profile: str, table: str) -> str | None:
    # to_regclass gives NULL when the table does not exist.
    table_row = conn.execute("SELECT to_regclass(%s) AS t", (table,)).fetchone()
    if not table_row["t"]:
        return None
    row = conn.execute("SELECT fingerprint FROM index_meta WHERE profile = %s", (profile,)).fetchone()
    if not row:
        return None
    return row["fingerprint"]


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
        # INSERT INTO t (id, doc_id, ..., embedding) VALUES (%s, %s, ..., %s::vector)
        column_names = []
        placeholders = []
        for column in COLUMNS:
            column_names.append(sql.Identifier(column))
            placeholders.append(sql.Placeholder())
        insert = sql.SQL("INSERT INTO {} ({}, embedding) VALUES ({}, %s::vector)").format(
            t,
            sql.SQL(", ").join(column_names),
            sql.SQL(", ").join(placeholders),
        )
        # One row of values per chunk: its columns, then its vector.
        values = []
        for record, vector in zip(records, vectors, strict=True):
            row = []
            for column in COLUMNS:
                row.append(record[column])
            row.append(vector_literal(vector))
            values.append(row)
        with conn.cursor() as cur:
            cur.executemany(insert, values)
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
    if not row:
        return None
    # Round-trip through JSON so values such as built_at (a datetime) become strings.
    as_json = json.dumps(row, default=str)
    return json.loads(as_json)


def all_vectors(conn: psycopg.Connection, table: str) -> dict[str, list[float]]:
    query = sql.SQL("SELECT id, embedding::text AS v FROM {}").format(sql.Identifier(table))
    rows = conn.execute(query).fetchall()
    vectors = {}
    for row in rows:
        # pgvector text looks like "[0.1,-0.25,0.03]".
        numbers = row["v"].strip("[]").split(",")
        vector = []
        for number in numbers:
            vector.append(float(number))
        vectors[row["id"]] = vector
    return vectors
