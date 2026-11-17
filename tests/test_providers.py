import asyncio

import pytest

from rag import providers
from rag.config import ModelConfig
from rag.providers import ProviderError, openrouter_body, stream_chat

MESSAGES = [{"role": "user", "content": "Hi"}]


def cfg(**options) -> ModelConfig:
    return ModelConfig("openrouter", "openai/gpt-6-luna", "https://openrouter.ai/api/v1", "key", options)


def collect(config: ModelConfig) -> list[str]:
    async def run():
        return [piece async for piece in stream_chat(config, MESSAGES)]

    return asyncio.run(run())


@pytest.fixture
def attempts(monkeypatch):
    """Replace the HTTP call with scripted attempts: each is a list of pieces,
    ending with an exception to raise after them, if any."""
    script: list[list] = []
    calls = []

    async def fake_once(config, messages):
        calls.append(1)
        for item in script[len(calls) - 1]:
            if isinstance(item, Exception):
                raise item
            yield item

    monkeypatch.setattr(providers, "_stream_chat_once", fake_once)
    monkeypatch.setattr(providers, "RETRY_DELAY", 0)
    return script, calls


def test_fallback_models_are_sent_as_models_list():
    body = openrouter_body(cfg(fallback_models=["anthropic/claude-haiku-5.5"], max_tokens=10, retries=1), MESSAGES)
    assert body["models"] == ["openai/gpt-6-luna", "anthropic/claude-haiku-5.5"]
    assert "model" not in body
    assert body["max_tokens"] == 10
    assert "retries" not in body and "fallback_models" not in body


def test_without_fallbacks_only_the_model_is_sent():
    body = openrouter_body(cfg(), MESSAGES)
    assert body["model"] == "openai/gpt-6-luna"
    assert "models" not in body


def test_overload_before_first_token_is_retried(attempts):
    script, calls = attempts
    script += [[ProviderError("502 high demand", retryable=True)], ["Hello", " world"]]
    assert collect(cfg(retries=1)) == ["Hello", " world"]
    assert len(calls) == 2


def test_error_after_first_token_is_not_retried(attempts):
    script, calls = attempts
    script += [["Hello", ProviderError("502 high demand", retryable=True)], ["never"]]
    with pytest.raises(ProviderError):
        collect(cfg(retries=1))
    assert len(calls) == 1


def test_non_retryable_error_is_not_retried(attempts):
    script, calls = attempts
    script += [[ProviderError("401 invalid key")], ["never"]]
    with pytest.raises(ProviderError):
        collect(cfg(retries=1))
    assert len(calls) == 1


def test_retries_default_to_none(attempts):
    script, calls = attempts
    script += [[ProviderError("502", retryable=True)], ["never"]]
    with pytest.raises(ProviderError):
        collect(cfg())
    assert len(calls) == 1


@pytest.mark.parametrize("status, expected", [(429, True), (502, True), (500, True), (400, False), (401, False), (None, False)])
def test_retryable_statuses(status, expected):
    assert providers._retryable(status) is expected


def test_error_event_inside_200_stream_is_retryable(monkeypatch):
    """The overload error seen in testing arrives as an SSE event in a 200 response."""
    sent = []

    def handler(request):
        sent.append(request)
        sse = 'data: {"error": {"code": 502, "message": "high demand", "metadata": {"error_type": "provider_unavailable"}}}\n\n'
        return providers.httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    real_client = providers.httpx.AsyncClient
    monkeypatch.setattr(
        providers.httpx, "AsyncClient", lambda **kw: real_client(transport=providers.httpx.MockTransport(handler), **kw)
    )
    monkeypatch.setattr(providers, "RETRY_DELAY", 0)
    with pytest.raises(ProviderError) as exc:
        collect(cfg(retries=1))
    assert exc.value.retryable
    assert len(sent) == 2  # first attempt plus one retry


def test_reasoning_effort_is_sent_and_excluded_from_the_stream():
    body = openrouter_body(cfg(reasoning_effort="low", max_tokens=4000), MESSAGES)
    assert body["reasoning"] == {"effort": "low", "exclude": True}
    assert "reasoning_effort" not in body
    assert "reasoning" not in openrouter_body(cfg(), MESSAGES)


def mock_stream(monkeypatch, body: str, content_type="text/event-stream"):
    def handler(request):
        return providers.httpx.Response(200, text=body, headers={"content-type": content_type})

    real_client = providers.httpx.AsyncClient
    monkeypatch.setattr(
        providers.httpx, "AsyncClient", lambda **kw: real_client(transport=providers.httpx.MockTransport(handler), **kw)
    )


def test_openrouter_length_limit_is_reported(monkeypatch):
    mock_stream(
        monkeypatch,
        'data: {"choices": [{"delta": {"content": "The algorithm"}}]}\n\n'
        'data: {"choices": [{"delta": {"content": " computes"}, "finish_reason": "length"}]}\n\n'
        "data: [DONE]\n\n",
    )
    assert collect(cfg()) == ["The algorithm", " computes", providers.Truncated()]


def test_openrouter_normal_stop_is_not_reported(monkeypatch):
    mock_stream(
        monkeypatch,
        'data: {"choices": [{"delta": {"content": "Done."}, "finish_reason": "stop"}]}\n\ndata: [DONE]\n\n',
    )
    assert collect(cfg()) == ["Done."]


def test_ollama_length_limit_is_reported(monkeypatch):
    mock_stream(
        monkeypatch,
        '{"message": {"content": "Partial"}, "done": false}\n{"done": true, "done_reason": "length"}\n',
        "application/x-ndjson",
    )
    ollama = ModelConfig("ollama", "qwen3.5:4b", "http://ollama:11434", None, {})
    assert collect(ollama) == ["Partial", providers.Truncated()]
