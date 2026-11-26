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
    """Rate limits (429) and server errors (5xx) are worth another try."""
    if not isinstance(status, int):
        return False
    if status == 429:
        return True
    return status >= 500


def _headers(cfg: ModelConfig) -> dict[str, str]:
    if cfg.provider != "openrouter":
        return {}
    if not cfg.api_key:
        raise ProviderError("OPENROUTER_API_KEY is not set")
    return {"Authorization": f"Bearer {cfg.api_key}", "X-Title": "Research papers RAG demo"}


def _check(r: httpx.Response) -> None:
    if r.is_error:
        message = f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text[:500]}"
        raise ProviderError(message, _retryable(r.status_code))


def _normalize(v: Sequence[float], dims: int) -> list[float]:
    """Truncate to `dims` (Matryoshka) and L2-normalize for cosine search."""
    v = list(v[:dims])
    if len(v) != dims:
        raise ProviderError(f"embedding has {len(v)} dimensions, config expects {dims}")
    sum_of_squares = 0.0
    for x in v:
        sum_of_squares += x * x
    norm = math.sqrt(sum_of_squares)
    if norm == 0:  # an all-zero vector stays as it is
        norm = 1.0
    normalized = []
    for x in v:
        normalized.append(x / norm)
    return normalized


class Embedder:
    def __init__(self, cfg: ModelConfig, dimensions: int, query_instruction: str = ""):
        self.cfg = cfg
        self.dims = dimensions
        self.instruction = query_instruction
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
            # Each item says which input it belongs to; put them back in input order.
            items = r.json()["data"]

            def input_position(item):
                return item["index"]

            vectors = []
            for item in sorted(items, key=input_position):
                vectors.append(item["embedding"])
        if len(vectors) != len(texts):
            raise ProviderError(f"asked for {len(texts)} embeddings, got {len(vectors)}")
        normalized = []
        for vector in vectors:
            normalized.append(_normalize(vector, self.dims))
        return normalized

    def embed_query(self, question: str) -> list[float]:
        # Instruction-tuned embedders (Qwen3) expect this format for queries.
        if self.instruction:
            text = f"Instruct: {self.instruction}\nQuery:{question}"
        else:
            text = question
        vectors = self.embed([text])
        return vectors[0]


def openrouter_body(cfg: ModelConfig, messages: list[dict[str, str]]) -> dict:
    """Chat request for OpenRouter. With `fallback_models`, OpenRouter tries
    them in order when the primary model errors (outage, overload, rate limit)."""
    body: dict = {"messages": messages, "stream": True}
    fallbacks = cfg.options.get("fallback_models")
    if fallbacks:
        body["models"] = [cfg.model] + list(fallbacks)
    else:
        body["model"] = cfg.model
    for option in ("temperature", "max_tokens"):
        if option in cfg.options:
            body[option] = cfg.options[option]
    effort = cfg.options.get("reasoning_effort")
    if effort:
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
            last_attempt = attempt == retries
            if started or not e.retryable or last_attempt:
                raise
            log.warning("chat request failed before the first token, retrying: %s", e)
            await asyncio.sleep(RETRY_DELAY)


async def _stream_chat_once(cfg: ModelConfig, messages: list[dict[str, str]]) -> AsyncIterator[str | Truncated]:
    opts = cfg.options
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        if cfg.provider == "ollama":
            # Ollama's own option names; options that are not set are left out.
            wanted_options = {
                "num_ctx": opts.get("num_ctx"),
                "temperature": opts.get("temperature"),
                "num_predict": opts.get("max_tokens"),
            }
            ollama_options = {}
            for name, value in wanted_options.items():
                if value is not None:
                    ollama_options[name] = value
            body = {
                "model": cfg.model,
                "messages": messages,
                "stream": True,
                "think": opts.get("think", False),
                "options": ollama_options,
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
                    message = event.get("message", {})
                    piece = message.get("content")
                    if piece:
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
                    data = line[len("data: ") :].strip()
                    if data == "[DONE]":
                        if truncated:
                            yield Truncated()
                        return
                    event = json.loads(data)
                    if "error" in event:  # sent inside a 200 stream, e.g. the provider is overloaded
                        err = event["error"]
                        code = None
                        if isinstance(err, dict):
                            code = err.get("code")
                        raise ProviderError(str(err), _retryable(code))
                    choices = event.get("choices")
                    if not choices:
                        choices = [{}]
                    choice = choices[0]
                    delta = choice.get("delta", {})
                    piece = delta.get("content")
                    if piece:
                        yield piece
                    if choice.get("finish_reason") == "length":
                        truncated = True
