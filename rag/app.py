"""Web app: a single page plus a streaming question-answering API.

    uvicorn rag.app:app --host 0.0.0.0 --port 8000

POST /api/ask {"question": "..."} streams newline-delimited JSON events:
    {"type": "sources", "sources": [...]}   retrieved chunks, numbered from 1
    {"type": "token", "text": "..."}         answer text as it is generated
    {"type": "done", "timings": {...}}       retrieval / first-token / total ms
    {"type": "error", "message": "..."}      if anything fails
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from pydantic import BaseModel

from rag import store
from rag.config import load_settings
from rag.providers import Embedder, stream_chat
from rag.retrieve import build_messages, retrieve

log = logging.getLogger("rag")
STATIC = Path(__file__).parent / "static"
settings = load_settings()
state: dict = {}


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not set")
    pool = ConnectionPool(
        settings.database_url,
        min_size=1,
        max_size=5,
        kwargs={"row_factory": dict_row},
        check=ConnectionPool.check_connection,  # serverless Postgres drops idle connections
        open=False,
    )
    pool.open(wait=True, timeout=30)
    state["pool"] = pool
    state["embedder"] = Embedder(settings.embedding, settings.dimensions, settings.query_instruction)
    warmup = asyncio.create_task(warm_up_models()) if settings.llm.provider == "ollama" else None
    yield
    if warmup:
        warmup.cancel()
    pool.close()


async def warm_up_models() -> None:
    """Load both local models and touch all their weights once at startup.

    Ollama memory-maps model files, so the first request otherwise pages the
    weights in from disk while generating; in Docker on a Mac that made the
    first answer several times slower than the following ones.
    """
    t0 = time.monotonic()
    try:
        await run_in_threadpool(state["embedder"].embed_query, "warm-up")
        async for _ in stream_chat(settings.llm, [{"role": "user", "content": "Reply with OK."}]):
            break
        log.warning("models warmed up in %.0fs", time.monotonic() - t0)
    except Exception as e:  # the app still works, the first question is just slower
        log.warning("model warm-up failed: %s", e)


app = FastAPI(title="Research papers RAG", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# --------------------------------------------------------------------------
# Per-IP rate limit (sliding one-minute window, in memory: one process only)

_hits: dict[str, deque[float]] = defaultdict(deque)


def client_ip(request: Request) -> str:
    # Behind the hosting platform's proxy, the rightmost X-Forwarded-For entry
    # is the address that proxy saw; earlier entries can be forged by the client.
    forwarded = request.headers.get("x-forwarded-for")
    return forwarded.split(",")[-1].strip() if forwarded else (request.client.host if request.client else "?")


def check_rate_limit(request: Request) -> None:
    limit = int(settings.server.get("rate_limit_per_minute", 0))
    if limit <= 0:
        return
    now, hits = time.monotonic(), _hits[client_ip(request)]
    while hits and now - hits[0] > 60:
        hits.popleft()
    if len(hits) >= limit:
        raise HTTPException(429, f"Rate limit: {limit} questions per minute. Please wait a moment.")
    hits.append(now)


# --------------------------------------------------------------------------
# Routes


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/info")
def info() -> dict:
    with state["pool"].connection() as conn:
        status = store.index_status(conn, settings.profile)
        docs = (
            conn.execute(
                f"SELECT title, authors, count(*) AS chunks FROM {settings.table} "  # table name from config, not user input
                "GROUP BY title, authors ORDER BY title"
            ).fetchall()
            if status
            else []
        )
    return {
        "profile": settings.profile,
        "llm": settings.llm.model,
        "embedding": settings.embedding.model,
        "top_k": settings.retrieval.get("top_k", 6),
        "index": status,
        "documents": docs,
    }


@app.get("/api/health")
def health() -> dict:
    with state["pool"].connection() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


class Question(BaseModel):
    question: str


def _event(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode()


@app.post("/api/ask")
async def ask(q: Question, request: Request) -> StreamingResponse:
    question = q.question.strip()
    max_chars = int(settings.server.get("max_question_chars", 1000))
    if not question:
        raise HTTPException(400, "Empty question")
    if len(question) > max_chars:
        raise HTTPException(400, f"Question too long (max {max_chars} characters)")
    check_rate_limit(request)

    async def events():
        t0 = time.monotonic()
        try:

            def _retrieve():
                with state["pool"].connection() as conn:
                    return retrieve(conn, state["embedder"], settings, question)

            sources = await run_in_threadpool(_retrieve)
            t_retrieval = time.monotonic()
            yield _event(
                {
                    "type": "sources",
                    "sources": [
                        {
                            "n": i,
                            **{k: s[k] for k in ("id", "title", "authors", "section", "pages", "reference", "text")},
                            "similarity": s.get("similarity"),
                        }
                        for i, s in enumerate(sources, 1)
                    ],
                }
            )
            t_first = None
            async for piece in stream_chat(settings.llm, build_messages(question, sources)):
                t_first = t_first or time.monotonic()
                yield _event({"type": "token", "text": piece})
            t_end = time.monotonic()
            ms = lambda a, b: round((b - a) * 1000) if a and b else None  # noqa: E731
            yield _event(
                {
                    "type": "done",
                    "timings": {
                        "retrieval_ms": ms(t0, t_retrieval),
                        "first_token_ms": ms(t0, t_first),
                        "total_ms": ms(t0, t_end),
                    },
                }
            )
        except Exception as e:  # report to the page instead of cutting the stream
            log.exception("ask failed")
            yield _event({"type": "error", "message": str(e)[:500]})

    return StreamingResponse(events(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})
