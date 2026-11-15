"""Embedding and chat clients for Ollama (local) and OpenRouter (cloud)."""

from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator, Sequence

import httpx

from rag.config import ModelConfig

# CPU inference is slow: allow long reads, fail fast on connect.
TIMEOUT = httpx.Timeout(connect=10, read=600, write=60, pool=10)


class ProviderError(RuntimeError):
    pass


def _headers(cfg: ModelConfig) -> dict[str, str]:
    if cfg.provider != "openrouter":
        return {}
    if not cfg.api_key:
        raise ProviderError("OPENROUTER_API_KEY is not set")
    return {"Authorization": f"Bearer {cfg.api_key}", "X-Title": "Research papers RAG demo"}


def _check(r: httpx.Response) -> None:
    if r.is_error:
        raise ProviderError(f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text[:500]}")


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


async def stream_chat(cfg: ModelConfig, messages: list[dict[str, str]]) -> AsyncIterator[str]:
    """Yield answer text pieces as the model generates them."""
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
                        return
        else:
            body = {"model": cfg.model, "messages": messages, "stream": True}
            for k in ("temperature", "max_tokens"):
                if k in opts:
                    body[k] = opts[k]
            async with client.stream("POST", f"{cfg.base_url}/chat/completions", headers=_headers(cfg), json=body) as r:
                if r.is_error:
                    await r.aread()
                    _check(r)
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue  # blank lines and ": OPENROUTER PROCESSING" keep-alives
                    data = line[6:].strip()
                    if data == "[DONE]":
                        return
                    event = json.loads(data)
                    if "error" in event:
                        raise ProviderError(str(event["error"]))
                    choices = event.get("choices") or [{}]
                    if piece := choices[0].get("delta", {}).get("content"):
                        yield piece
