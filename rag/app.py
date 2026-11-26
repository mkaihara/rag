"""Web app: a single page plus a streaming question-answering API.

    uvicorn rag.app:app --host 0.0.0.0 --port 8000

POST /api/ask {"question": "..."} streams newline-delimited JSON events:
    {"type": "sources", "sources": [...]}   retrieved chunks, numbered from 1
    {"type": "token", "text": "..."}         answer text as it is generated
    {"type": "done", "timings": {...}}       retrieval / first-token / total ms
    {"type": "error", "message": "..."}      if anything fails (generic; details in the log)

Behind a hosting platform's proxy, rate limits must see the real client
address: set CLIENT_IP_HEADER (server.client_ip_header) when the platform
writes it into a header of its own, as Fly.io does with Fly-Client-IP, or
TRUSTED_PROXY_HOPS (server.trusted_proxy_hops) to the number of proxies that
append it to X-Forwarded-For.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from collections import deque
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
from rag.providers import Embedder, Truncated, stream_chat
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

    # Only the local models (Ollama) need warming up.
    warmup = None
    if settings.llm.provider == "ollama":
        warmup = asyncio.create_task(warm_up_models())

    yield  # the app serves requests here

    if warmup is not None:
        warmup.cancel()
    pool.close()


async def warm_up_models() -> None:
    """Load both local models and touch all their weights once at startup.

    Ollama memory-maps model files, so the first request otherwise pages the
    weights in from disk while generating; in Docker on a Mac that made the
    first answer several times slower than the following ones.
    """
    started = time.monotonic()
    try:
        embedder = state["embedder"]
        await run_in_threadpool(embedder.embed_query, "warm-up")

        # Generating the first piece of an answer loads the chat model.
        messages = [{"role": "user", "content": "Reply with OK."}]
        async for _first_piece in stream_chat(settings.llm, messages):
            break

        seconds = time.monotonic() - started
        log.warning("models warmed up in %.0fs", seconds)
    except Exception as e:  # the app still works, the first question is just slower
        log.warning("model warm-up failed: %s", e)


# No /docs, /redoc or /openapi.json: the API is for this page only.
app = FastAPI(title="Research papers RAG", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# --------------------------------------------------------------------------
# Request size limit and security headers, as plain ASGI middleware (it
# leaves the streamed answer alone).

MAX_BODY_BYTES = 16 * 1024  # a 1000-character question is at most ~4 KB of JSON

SECURITY_HEADERS = [
    (
        b"content-security-policy",
        b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        b"font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
        b"frame-ancestors 'none'",
    ),  # 'unsafe-inline' styles: KaTeX positions symbols with style attributes
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"strict-transport-security", b"max-age=31536000"),  # browsers ignore it over plain http
]


class BodyTooLarge(HTTPException):
    # An HTTPException, so FastAPI passes it through as a 413 instead of
    # turning it into "error parsing the body" when it surfaces mid-read.
    def __init__(self):
        super().__init__(413, "Request too large")


class Hardening:
    """Refuse request bodies over MAX_BODY_BYTES with 413 before they are read
    into memory, and add SECURITY_HEADERS to every response."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                # Copy the message instead of changing the one we were given.
                headers = list(message.get("headers", []))
                headers.extend(SECURITY_HEADERS)
                message = dict(message)
                message["headers"] = headers
            await send(message)

        async def reject():
            body = b'{"detail":"Request too large"}'
            body_length = str(len(body)).encode()
            response_start = {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json"), (b"content-length", body_length)],
            }
            await send_with_headers(response_start)
            await send({"type": "http.response.body", "body": body})

        # A declared Content-Length is checked before reading anything.
        request_headers = dict(scope["headers"])
        declared_length = request_headers.get(b"content-length")
        if declared_length is not None:
            if not declared_length.isdigit():
                return await reject()
            if int(declared_length) > MAX_BODY_BYTES:
                return await reject()

        # Chunked uploads have no Content-Length: count the bytes as they arrive.
        received = 0

        async def limited_receive():
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > MAX_BODY_BYTES:
                    raise BodyTooLarge()
            return message

        try:
            await self.app(scope, limited_receive, send_with_headers)
        except BodyTooLarge:  # raised while the body is read, before any response
            await reject()


app.add_middleware(Hardening)

# --------------------------------------------------------------------------
# Rate limits: per client and for the whole app, over a sliding one-minute
# window, in memory (one process only).

WINDOW = 60.0
MAX_TRACKED_CLIENTS = 10_000
GLOBAL_KEY = "*"  # not a valid IP address, so no client can collide with it
_hits: dict[str, deque[float]] = {}


def trusted_proxy_hops() -> int:
    # An empty TRUSTED_PROXY_HOPS counts as unset: the config value applies.
    from_env = os.environ.get("TRUSTED_PROXY_HOPS")
    if from_env:
        return int(from_env)
    return int(settings.server.get("trusted_proxy_hops", 0))


def client_ip_header() -> str:
    # An empty CLIENT_IP_HEADER still overrides the config: it turns the header off.
    if "CLIENT_IP_HEADER" in os.environ:
        value = os.environ["CLIENT_IP_HEADER"]
    else:
        value = settings.server.get("client_ip_header")
    if not value:
        return ""
    return value.strip().lower()


def client_ip(request: Request) -> str:
    """The address to rate-limit by.

    By default the TCP peer. Behind a proxy, one of two settings applies:

    - client_ip_header: a header the proxy always overwrites with the client
      address (Fly-Client-IP on Fly.io). Used as is; the peer is the fallback.
    - trusted_proxy_hops: N proxies that each append the address they
      received the request from to X-Forwarded-For, so the Nth entry from the
      right was written by the outermost one. Entries further left come from
      the client and can be forged. Fly.io doesn't fit this rule: it appends
      the app's own IP after the client's, which would put every visitor in
      one rate-limit bucket.
    """
    if request.client:
        peer = request.client.host
    else:
        peer = "unknown"

    header = client_ip_header()
    if header:
        address = request.headers.get(header, "").strip()
        if address:
            return address
        return peer

    hops = trusted_proxy_hops()
    if hops <= 0:
        return peer

    forwarded = []
    for part in request.headers.get("x-forwarded-for", "").split(","):
        address = part.strip()
        if address:
            forwarded.append(address)
    if len(forwarded) < hops:
        return peer
    return forwarded[-hops]


def _window(key: str, now: float) -> deque[float]:
    """The key's request times within the last WINDOW seconds, oldest first."""
    hits = _hits.get(key, deque())
    while hits:
        oldest = hits[0]
        if now - oldest < WINDOW:
            break
        hits.popleft()
    return hits


def _forget_idle(now: float) -> None:
    idle_keys = []
    for key, hits in _hits.items():
        if not hits:
            idle_keys.append(key)
            continue
        newest = hits[-1]
        if now - newest >= WINDOW:
            idle_keys.append(key)
    for key in idle_keys:
        del _hits[key]


BUSY_MESSAGE = "The demo is busy right now. Please try again in a minute."


def check_rate_limit(request: Request) -> None:
    per_client = int(settings.server.get("rate_limit_per_minute", 0))
    overall = int(settings.server.get("global_rate_limit_per_minute", 0))

    # Each check is (key, limit, message). A limit of 0 turns that check off.
    checks = []
    if overall > 0:
        checks.append((GLOBAL_KEY, overall, BUSY_MESSAGE))
    if per_client > 0:
        client_message = f"Rate limit: {per_client} questions per minute. Please wait a moment."
        checks.append((client_ip(request), per_client, client_message))
    if not checks:
        return

    # Refuse if any window is full; nothing is recorded until all checks pass.
    now = time.monotonic()
    windows = []
    for key, limit, message in checks:
        hits = _window(key, now)
        if len(hits) >= limit:
            raise HTTPException(429, message)
        windows.append((key, hits))

    # Bound memory: drop idle clients, and refuse new ones if still full.
    if len(_hits) >= MAX_TRACKED_CLIENTS:
        _forget_idle(now)
        if len(_hits) >= MAX_TRACKED_CLIENTS:
            for key, _hits_of_key in windows:
                is_new_client = key not in _hits
                if is_new_client:
                    raise HTTPException(429, BUSY_MESSAGE)

    for key, hits in windows:
        hits.append(now)
        _hits[key] = hits


# --------------------------------------------------------------------------
# Routes


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


# /api/info and /api/health answer from a short-lived cache, so requests in
# a loop cannot keep the serverless database awake or hold its connections.
INFO_TTL = 300.0
HEALTH_TTL = 10.0
_cache: dict[str, tuple[float, dict]] = {}


def cached(key: str, ttl: float, compute) -> dict:
    now = time.monotonic()
    entry = _cache.get(key)
    if entry:
        stored_at, stored_value = entry
        if now - stored_at < ttl:
            return stored_value
    value = compute()
    _cache[key] = (now, value)
    return value


@app.get("/api/info")
def info() -> dict:
    return cached("info", INFO_TTL, _info)


def _info() -> dict:
    with state["pool"].connection() as conn:
        status = store.index_status(conn, settings.profile)
        docs = []
        if status:  # the index exists, so its table can be read
            # The table name comes from the config, not from user input.
            query = f"SELECT title, authors, count(*) AS chunks FROM {settings.table} GROUP BY title, authors ORDER BY title"
            docs = conn.execute(query).fetchall()
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
    return cached("health", HEALTH_TTL, _health)


def _health() -> dict:
    with state["pool"].connection() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


class Question(BaseModel):
    question: str


def _event(obj: dict) -> bytes:
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    return line.encode()


# The chunk fields sent to the page for each source.
SOURCE_FIELDS = ("id", "title", "authors", "section", "pages", "reference", "text")


@app.post("/api/ask")
async def ask(q: Question, request: Request) -> StreamingResponse:
    check_rate_limit(request)  # first, so rejected questions count too
    question = q.question.strip()
    max_chars = int(settings.server.get("max_question_chars", 1000))
    if not question:
        raise HTTPException(400, "Empty question")
    if len(question) > max_chars:
        raise HTTPException(400, f"Question too long (max {max_chars} characters)")

    async def events():
        t0 = time.monotonic()
        try:

            def _retrieve():
                with state["pool"].connection() as conn:
                    return retrieve(conn, state["embedder"], settings, question)

            # 1. Retrieve the chunks and send them, numbered from 1.
            sources = await run_in_threadpool(_retrieve)
            t_retrieval = time.monotonic()
            numbered_sources = []
            for n, source in enumerate(sources, start=1):
                item = {"n": n}
                for field in SOURCE_FIELDS:
                    item[field] = source[field]
                item["similarity"] = source.get("similarity")
                numbered_sources.append(item)
            yield _event({"type": "sources", "sources": numbered_sources})

            # 2. Stream the answer as the model writes it.
            t_first = None
            truncated = False
            messages = build_messages(question, sources)
            async for piece in stream_chat(settings.llm, messages):
                if isinstance(piece, Truncated):
                    truncated = True
                    log.warning("answer stopped at the token limit (llm.max_tokens)")
                    continue
                if not t_first:
                    t_first = time.monotonic()
                yield _event({"type": "token", "text": piece})
            t_end = time.monotonic()

            # 3. Report the timings. A missing time (no token arrived) is None.
            def ms(start, end):
                if not start or not end:
                    return None
                return round((end - start) * 1000)

            timings = {
                "retrieval_ms": ms(t0, t_retrieval),
                "first_token_ms": ms(t0, t_first),
                "total_ms": ms(t0, t_end),
            }
            yield _event({"type": "done", "truncated": truncated, "timings": timings})
        except Exception:  # report to the page instead of cutting the stream
            # Details (provider responses, database hosts) stay in the server log.
            error_id = secrets.token_hex(4)
            log.exception("ask failed [error %s]", error_id)
            message = f"Something went wrong while answering (error {error_id}). Please try again."
            yield _event({"type": "error", "message": message})

    return StreamingResponse(events(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})
