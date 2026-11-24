"""Load the active profile from config.yaml.

The profile is chosen by the RAG_PROFILE environment variable (or the
`profile` argument), falling back to `default_profile`. A few settings can
be overridden from the environment so the same config works on a laptop, in
docker compose and in production:

    RAG_CONFIG         path to config.yaml (default: repo root)
    DATABASE_URL       Postgres connection string (required)
    OLLAMA_BASE_URL    Ollama server for llm/embedding providers "ollama"
    OPENROUTER_API_KEY key for providers "openrouter" (name set by api_key_env)
    LLM_MODEL          replaces the profile's llm.model
    EMBED_MODEL        replaces the profile's embedding.model
    CLIENT_IP_HEADER   replaces server.client_ip_header (read in rag.app)
    TRUSTED_PROXY_HOPS replaces server.trusted_proxy_hops (read in rag.app)
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class ChunkParams:
    target_tokens: int  # pack paragraphs up to about this size
    max_tokens: int  # split any block larger than this
    min_lead_tokens: int  # shorter section lead-ins merge into their first subsection


@dataclass(frozen=True)
class ModelConfig:
    provider: str  # "ollama" or "openrouter"
    model: str
    base_url: str | None = None
    api_key: str | None = None
    options: dict[str, Any] = field(default_factory=dict)  # provider-specific extras


@dataclass(frozen=True)
class Settings:
    profile: str
    llm: ModelConfig
    embedding: ModelConfig
    dimensions: int
    query_instruction: str
    chunking: ChunkParams
    retrieval: dict[str, Any]
    server: dict[str, Any]
    database_url: str | None

    @property
    def table(self) -> str:
        # one table per profile: dimensions and embedding models differ
        return f"chunks_{self.profile}"


OPENROUTER_URL = "https://openrouter.ai/api/v1"


def _model(section: dict[str, Any], keys: set[str], model_env: str) -> ModelConfig:
    provider = section["provider"]
    if provider == "ollama":
        base_url = os.environ.get("OLLAMA_BASE_URL") or section.get("base_url") or "http://localhost:11434"
        api_key = None
    elif provider == "openrouter":
        base_url = section.get("base_url") or OPENROUTER_URL
        api_key = os.environ.get(section.get("api_key_env", "OPENROUTER_API_KEY"))
    else:
        raise ValueError(f"unknown provider {provider!r}")
    options = {k: v for k, v in section.items() if k not in keys}
    model = os.environ.get(model_env) or section["model"]
    return ModelConfig(provider, model, base_url.rstrip("/"), api_key, options)


def load_config(path: Path | None = None) -> dict[str, Any]:
    path = path or Path(os.environ.get("RAG_CONFIG", REPO_ROOT / "config.yaml"))
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_settings(profile: str | None = None) -> Settings:
    config = load_config()
    name = profile or os.environ.get("RAG_PROFILE") or config["default_profile"]
    if name not in config["profiles"]:
        raise SystemExit(f"unknown profile {name!r}; choose from {sorted(config['profiles'])}")
    p = config["profiles"][name]
    common = {"provider", "model", "base_url", "api_key_env"}
    emb = p["embedding"]
    return Settings(
        profile=name,
        llm=_model(p["llm"], common, "LLM_MODEL"),
        embedding=_model(emb, common | {"dimensions", "max_input_tokens", "query_instruction"}, "EMBED_MODEL"),
        dimensions=int(emb["dimensions"]),
        query_instruction=emb.get("query_instruction", ""),
        chunking=ChunkParams(**p["chunking"]),
        retrieval=p.get("retrieval", {}),
        server=p.get("server", {}),
        database_url=os.environ.get("DATABASE_URL"),
    )
