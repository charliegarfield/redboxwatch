"""Offline tests for the FEC client's resilience (retry on timeout / 429)."""
from __future__ import annotations

import httpx

from redbox import fec as fec_mod
from redbox.fec import FECClient


class _Resp:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {"results": [], "pagination": {}}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


class _FakeHTTP:
    """Scripted responses: each item is either an Exception to raise or a _Resp."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def get(self, url, params=None):
        item = self.script[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def _client(tmp_path, script):
    c = FECClient(api_key="k", cache_dir=tmp_path, min_delay_seconds=0.0)
    c._client = _FakeHTTP(script)
    return c


def test_retries_on_read_timeout(tmp_path, monkeypatch):
    monkeypatch.setattr(fec_mod.time, "sleep", lambda *_: None)  # no real backoff
    c = _client(tmp_path, [
        httpx.ReadTimeout("slow"),                       # 1st: timeout
        httpx.ConnectError("reset"),                     # 2nd: transient
        _Resp(200, {"results": [{"x": 1}], "pagination": {}}),  # 3rd: ok
    ])
    data = c.get("/candidates/", {"q": "x"}, use_cache=False)
    assert data["results"] == [{"x": 1}]
    assert c._client.calls == 3


def test_retries_on_429_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(fec_mod.time, "sleep", lambda *_: None)
    c = _client(tmp_path, [
        _Resp(429, headers={"retry-after": "1"}),
        _Resp(200, {"results": [{"ok": True}], "pagination": {}}),
    ])
    data = c.get("/x/", {"a": 1}, use_cache=False)
    assert data["results"] == [{"ok": True}]
    assert c._client.calls == 2


def test_persistent_timeout_eventually_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(fec_mod.time, "sleep", lambda *_: None)
    c = _client(tmp_path, [httpx.ReadTimeout("slow")] * 5)
    try:
        c.get("/x/", use_cache=False)
        assert False, "expected timeout to propagate after retries"
    except httpx.ReadTimeout:
        pass
    assert c._client.calls == 5
