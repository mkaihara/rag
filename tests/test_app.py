import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from rag import app as app_module
from rag.providers import Truncated


def make_request(peer: str, forwarded: str | None = None, fly_client_ip: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    if fly_client_ip is not None:
        headers.append((b"fly-client-ip", fly_client_ip.encode()))
    return Request({"type": "http", "method": "POST", "path": "/api/ask", "headers": headers, "client": (peer, 1234)})


@pytest.fixture(autouse=True)
def server_settings(monkeypatch):
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    monkeypatch.delenv("CLIENT_IP_HEADER", raising=False)
    server = app_module.settings.server
    defaults = {"rate_limit_per_minute": 0, "global_rate_limit_per_minute": 0, "trusted_proxy_hops": 0, "client_ip_header": ""}
    for key, value in defaults.items():
        monkeypatch.setitem(server, key, value)
    app_module._hits.clear()
    yield server
    app_module._hits.clear()


def test_forwarded_header_ignored_without_trusted_proxy():
    assert app_module.client_ip(make_request("10.0.0.5", "1.2.3.4")) == "10.0.0.5"


def test_forwarded_header_uses_entry_added_by_trusted_proxy(server_settings):
    server_settings["trusted_proxy_hops"] = 1
    # the client forged 6.6.6.6; the proxy appended the real address 1.2.3.4
    assert app_module.client_ip(make_request("10.0.0.5", "6.6.6.6, 1.2.3.4")) == "1.2.3.4"
    server_settings["trusted_proxy_hops"] = 2
    assert app_module.client_ip(make_request("10.0.0.5", "6.6.6.6, 1.2.3.4, 10.0.0.9")) == "1.2.3.4"
    assert app_module.client_ip(make_request("10.0.0.5")) == "10.0.0.5"


def test_client_ip_header_is_used_when_configured(server_settings):
    # Fly.io: X-Forwarded-For is "<client>, <app's own IP>"; Fly-Client-IP is the client.
    server_settings["client_ip_header"] = "Fly-Client-IP"
    assert app_module.client_ip(make_request("172.16.0.2", "1.2.3.4, 66.241.125.12", "1.2.3.4")) == "1.2.3.4"
    assert app_module.client_ip(make_request("172.16.0.2", "5.6.7.8, 66.241.125.12", "5.6.7.8")) == "5.6.7.8"
    # missing or empty header: fall back to the TCP peer
    assert app_module.client_ip(make_request("172.16.0.2", "1.2.3.4, 66.241.125.12")) == "172.16.0.2"
    assert app_module.client_ip(make_request("172.16.0.2", fly_client_ip=" ")) == "172.16.0.2"


def test_client_ip_header_env_overrides_config(server_settings, monkeypatch):
    server_settings["client_ip_header"] = "fly-client-ip"
    monkeypatch.setenv("CLIENT_IP_HEADER", "")  # empty turns the header off
    assert app_module.client_ip(make_request("10.0.0.5", fly_client_ip="1.2.3.4")) == "10.0.0.5"
    monkeypatch.setenv("CLIENT_IP_HEADER", "fly-client-ip")
    server_settings["client_ip_header"] = ""
    assert app_module.client_ip(make_request("10.0.0.5", fly_client_ip="1.2.3.4")) == "1.2.3.4"


def test_fly_visitors_get_separate_rate_limits(server_settings):
    # The bug this replaces: with trusted_proxy_hops 1 on Fly.io every visitor
    # resolved to the app's own IP and shared one bucket.
    server_settings["rate_limit_per_minute"] = 2
    server_settings["client_ip_header"] = "fly-client-ip"
    for _ in range(2):
        app_module.check_rate_limit(make_request("172.16.0.2", "1.2.3.4, 66.241.125.12", "1.2.3.4"))
    app_module.check_rate_limit(make_request("172.16.0.2", "5.6.7.8, 66.241.125.12", "5.6.7.8"))
    with pytest.raises(HTTPException) as e:
        app_module.check_rate_limit(make_request("172.16.0.2", "9.9.9.9, 1.2.3.4, 66.241.125.12", "1.2.3.4"))
    assert e.value.status_code == 429


def test_cloud_profile_reads_fly_client_ip():
    from rag.config import load_settings

    server = load_settings("cloud").server
    assert server["client_ip_header"] == "fly-client-ip"
    assert server["trusted_proxy_hops"] == 0


def test_forged_forwarded_header_does_not_bypass_limit(server_settings):
    server_settings["rate_limit_per_minute"] = 2
    for i in range(2):
        app_module.check_rate_limit(make_request("10.0.0.5", f"6.6.6.{i}"))
    with pytest.raises(HTTPException) as e:
        app_module.check_rate_limit(make_request("10.0.0.5", "6.6.6.99"))
    assert e.value.status_code == 429


def test_global_limit_applies_across_clients(server_settings):
    server_settings["rate_limit_per_minute"] = 5
    server_settings["global_rate_limit_per_minute"] = 3
    for i in range(3):
        app_module.check_rate_limit(make_request(f"10.0.0.{i}"))
    with pytest.raises(HTTPException):
        app_module.check_rate_limit(make_request("10.0.0.200"))


def test_tracked_clients_are_bounded(server_settings, monkeypatch):
    server_settings["rate_limit_per_minute"] = 5
    monkeypatch.setattr(app_module, "MAX_TRACKED_CLIENTS", 3)
    clock = iter([0.0, 1.0, 2.0, 3.0, 100.0])
    monkeypatch.setattr(app_module.time, "monotonic", lambda: next(clock))
    for i in range(3):
        app_module.check_rate_limit(make_request(f"10.0.0.{i}"))
    with pytest.raises(HTTPException):  # full, and nobody is idle yet
        app_module.check_rate_limit(make_request("10.0.0.3"))
    app_module.check_rate_limit(make_request("10.0.0.4"))  # a minute later the idle ones are dropped
    assert list(app_module._hits) == ["10.0.0.4"]


def test_errors_are_generic(monkeypatch, caplog):
    async def fail(*args, **kwargs):
        raise RuntimeError("connection to db.secret-host.neon.tech failed")

    monkeypatch.setattr(app_module, "run_in_threadpool", fail)
    response = TestClient(app_module.app).post("/api/ask", json={"question": "What is MQRTR?"})
    event = json.loads(response.text.strip().splitlines()[-1])
    assert event["type"] == "error"
    assert "secret-host" not in event["message"]
    assert "secret-host" in caplog.text


def test_done_event_reports_truncated_answer(monkeypatch):
    async def no_sources(*args, **kwargs):
        return []

    async def cut_short(*args, **kwargs):
        yield "The algorithm"
        yield Truncated()

    monkeypatch.setattr(app_module, "run_in_threadpool", no_sources)
    monkeypatch.setattr(app_module, "stream_chat", cut_short)
    response = TestClient(app_module.app).post("/api/ask", json={"question": "What is MQRTR?"})
    events = [json.loads(line) for line in response.text.strip().splitlines()]
    assert [e["text"] for e in events if e["type"] == "token"] == ["The algorithm"]
    assert events[-1]["type"] == "done" and events[-1]["truncated"] is True


# --------------------------------------------------------------------------
# Hardening: request size limit, security headers, no API docs, cached info


def test_oversized_body_is_refused_before_reading():
    body = json.dumps({"question": "x" * (app_module.MAX_BODY_BYTES + 1)})
    response = TestClient(app_module.app).post("/api/ask", content=body, headers={"Content-Type": "application/json"})
    assert response.status_code == 413


def test_oversized_chunked_body_is_refused():
    def chunks():  # a generator body is sent chunked, without Content-Length
        yield b'{"question": "'
        for _ in range(10):
            yield b"x" * 4096
        yield b'"}'

    response = TestClient(app_module.app).post("/api/ask", content=chunks(), headers={"Content-Type": "application/json"})
    assert response.status_code == 413


def test_rejected_questions_count_toward_the_rate_limit(server_settings):
    server_settings["rate_limit_per_minute"] = 2
    client = TestClient(app_module.app)
    statuses = [client.post("/api/ask", json={"question": "x" * 5000}).status_code for _ in range(3)]
    assert statuses == [400, 400, 429]


def test_security_headers_on_page_and_api(monkeypatch):
    monkeypatch.setattr(app_module, "_info", lambda: {"ok": True})
    app_module._cache.clear()
    client = TestClient(app_module.app)
    for path in ("/", "/static/app.js", "/api/info"):
        headers = client.get(path).headers
        assert "script-src 'self'" in headers["content-security-policy"], path
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
    too_large = client.post("/api/ask", content=b"x" * (app_module.MAX_BODY_BYTES + 1))
    assert "content-security-policy" in too_large.headers


def test_api_docs_are_not_served():
    client = TestClient(app_module.app)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404, path


def test_info_and_health_are_cached(monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "_info", lambda: calls.append("info") or {"profile": "test"})
    monkeypatch.setattr(app_module, "_health", lambda: calls.append("health") or {"ok": True})
    clock = iter([0.0, 1.0, 0.0, 1.0, app_module.INFO_TTL + 1, app_module.HEALTH_TTL + 1])
    monkeypatch.setattr(app_module.time, "monotonic", lambda: next(clock))
    app_module._cache.clear()
    for _ in range(2):
        assert app_module.info() == {"profile": "test"}
        assert app_module.health() == {"ok": True}
    assert calls == ["info", "health"]
    app_module.info()
    app_module.health()
    assert calls == ["info", "health", "info", "health"]
    app_module._cache.clear()
