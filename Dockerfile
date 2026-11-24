# One image for the indexer (python -m rag.ingest) and the web app (default).
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    TIKTOKEN_CACHE_DIR=/opt/tiktoken \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Dependencies first (cached unless pyproject/uv.lock change).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Bake in what would otherwise be downloaded at runtime, so the container
# works without internet: the tokenizer and the page's JS/CSS libraries.
RUN python -c "import tiktoken; tiktoken.get_encoding('o200k_base')"
COPY scripts/fetch_vendor.py scripts/
RUN python scripts/fetch_vendor.py

COPY config.yaml ./
COPY embeddings ./embeddings
COPY rag ./rag

RUN useradd --system --no-create-home app
USER app

EXPOSE 8000
# Hosting platforms pass the port in $PORT. The app picks the client address
# itself (server.client_ip_header / trusted_proxy_hops), so uvicorn's own
# X-Forwarded-For handling is off.
CMD ["sh", "-c", "exec uvicorn rag.app:app --host 0.0.0.0 --port ${PORT:-8000} --no-proxy-headers"]
