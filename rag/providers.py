"""Embedding and chat clients for Ollama (local) and OpenRouter (cloud)."""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import AsyncIterator, Sequence

import httpx

from rag.config import ModelConfig

# CPU inference is slow: allow long reads, fail fast on connect.
TIMEOUT = httpx.Timeout(connect=10, read=600, write=60, pool=10)
RETRY_DELAY = 1.0  # seconds before retrying a chat request that failed before its first token

log = logging.getLogger("rag")


class ProviderError(RuntimeError):
    def __init__(self, message: str, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable  # overload or outage on the provider's side, worth another try


class Truncated:
    """Yielded by stream_chat after the last piece when the answer stopped at
    the token limit (finish_reason / done_reason "length")."""

    def __eq__(self, other) -> bool:
        return isinstance(other, Truncated)


def _retryable(status) -> bool:
    return isinstance(status, int) and (status == 429 or status >= 500)


def _headers(cfg: ModelConfig) -> dict[str, str]:
    if cfg.provider != "openrouter":
        return {}
    if not cfg.api_key:
        raise ProviderError("OPENROUTER_API_KEY is not set")
    return {"Authorization": f"Bearer {cfg.api_key}", "X-Title": "Research papers RAG demo"}


def _check(r: httpx.Response) -> None:
    if r.is_error:
        raise ProviderError(f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text[:500]}", _retryable(r.status_code))


def _normalize(v: Sequence[float], dims: int) -> list[float]:
    """Truncate to `dims` (Matryoshka) and L2-normalize for cosine search."""
    v = list(v[:dims])
    if len(v) != dims:
        raise ProviderError(f"embedding has {len(v)} dimensions, config expects {dims}")
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


class Embedder:
    def __init__(self, cfg: ModelConfig, dimensions: int, query_instruction: str = ""):
        self.cfg, self.dims, self.instruction = cfg, dimensions, query_instruction
        self.client = httpx.Client(timeout=TIMEOUT)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.cfg.provider == "ollama":
            r = self.client.post(
                f"{self.cfg.base_url}/api/embed",
                json={"model": self.cfg.model, "input": texts, "truncate": True},
            )
            _check(r)
            vectors = r.json()["embeddings"]
        else:
            r = self.client.post(
                f"{self.cfg.base_url}/embeddings",
                headers=_headers(self.cfg),
                json={"model": self.cfg.model, "input": texts, "dimensions": self.dims},
            )
            _check(r)
            data = sorted(r.json()["data"], key=lambda d: d["index"])
            vectors = [d["embedding"] for d in data]
        if len(vectors) != len(texts):
            raise ProviderError(f"asked for {len(texts)} embeddings, got {len(vectors)}")
        return [_normalize(v, self.dims) for v in vectors]

    def embed_query(self, question: str) -> list[float]:
        text = f"Instruct: {self.instruction}\nQuery:{question}" if self.instruction else question
        return self.embed([text])[0]


def openrouter_body(cfg: ModelConfig, messages: list[dict[str, str]]) -> dict:
    """Chat request for OpenRouter. With `fallback_models`, OpenRouter tries
    them in order when the primary model errors (outage, overload, rate limit)."""
    body: dict = {"messages": messages, "stream": True}
    if fallbacks := cfg.options.get("fallback_models"):
        body["models"] = [cfg.model, *fallbacks]
    else:
        body["model"] = cfg.model
    for k in ("temperature", "max_tokens"):
        if k in cfg.options:
            body[k] = cfg.options[k]
    if effort := cfg.options.get("reasoning_effort"):
        # Hidden reasoning counts against max_tokens; keep it small and out of the stream.
        body["reasoning"] = {"effort": effort, "exclude": True}
    return body


async def stream_chat(cfg: ModelConfig, messages: list[dict[str, str]]) -> AsyncIterator[str | Truncated]:
    """Yield answer text pieces as the model generates them, then a
    Truncated() marker if the answer was cut at the token limit.

    A retryable provider error before the first piece is retried up to
    `retries` times (llm option, default 0). Once text has been yielded the
    error is raised, so the page never shows two half-answers.
    """
    retries = int(cfg.options.get("retries", 0))
    for attempt in range(retries + 1):
        started = False
        try:
            async for piece in _stream_chat_once(cfg, messages):
                started = True
                yield piece
            return
        except ProviderError as e:
            if started or not e.retryable or attempt == retries:
                raise
            log.warning("chat request failed before the first token, retrying: %s", e)
            await asyncio.sleep(RETRY_DELAY)


async def _stream_chat_once(cfg: ModelConfig, messages: list[dict[str, str]]) -> AsyncIterator[str | Truncated]:
    opts = cfg.options
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        if cfg.provider == "ollama":
            body = {
                "model": cfg.model,
                "messages": messages,
                "stream": True,
                "think": opts.get("think", False),
                "options": {
                    k: v
                    for k, v in {
                        "num_ctx": opts.get("num_ctx"),
                        "temperature": opts.get("temperature"),
                        "num_predict": opts.get("max_tokens"),
                    }.items()
                    if v is not None
                },
            }
            async with client.stream("POST", f"{cfg.base_url}/api/chat", json=body) as r:
                if r.is_error:
                    await r.aread()
                    _check(r)
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    if "error" in event:
                        raise ProviderError(event["error"])
                    if piece := event.get("message", {}).get("content"):
                        yield piece
                    if event.get("done"):
                        if event.get("done_reason") == "length":
                            yield Truncated()
                        return
        else:
            body = openrouter_body(cfg, messages)
            truncated = False
            async with client.stream("POST", f"{cfg.base_url}/chat/completions", headers=_headers(cfg), json=body) as r:
                if r.is_error:
                    await r.aread()
                    _check(r)
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue  # blank lines and ": OPENROUTER PROCESSING" keep-alives
                    data = line[6:].strip()
                    if data == "[DONE]":
                        if truncated:
                            yield Truncated()
                        return
                    event = json.loads(data)
                    if "error" in event:  # sent inside a 200 stream, e.g. the provider is overloaded
                        err = event["error"]
                        code = err.get("code") if isinstance(err, dict) else None
                        raise ProviderError(str(err), _retryable(code))
                    choices = event.get("choices") or [{}]
                    if piece := choices[0].get("delta", {}).get("content"):
                        yield piece
                    if choices[0].get("finish_reason") == "length":
                        truncated = True
