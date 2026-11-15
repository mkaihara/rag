import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from rag import app as app_module


def make_request(peer: str, forwarded: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded else []
    return Request({"type": "http", "method": "POST", "path": "/api/ask", "headers": headers, "client": (peer, 1234)})


@pytest.fixture(autouse=True)
def server_settings(monkeypatch):
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)
    server = app_module.settings.server
    for key, value in {"rate_limit_per_minute": 0, "global_rate_limit_per_minute": 0, "trusted_proxy_hops": 0}.items():
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
