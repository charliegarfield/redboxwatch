"""Offline tests for the FEC client's resilience (retry on timeout / 429 / 5xx)
and cache corruption recovery."""
from __future__ import annotations

import httpx
import pytest

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


def test_retries_on_5xx_then_succeeds(tmp_path, monkeypatch):
    # A 500/502 from openFEC is as transient as a connection reset and must be
    # retried with the same backoff, not raised on first sight.
    monkeypatch.setattr(fec_mod.time, "sleep", lambda *_: None)
    c = _client(tmp_path, [
        _Resp(500),
        _Resp(502),
        _Resp(200, {"results": [{"ok": True}], "pagination": {}}),
    ])
    data = c.get("/x/", {"a": 1}, use_cache=False)
    assert data["results"] == [{"ok": True}]
    assert c._client.calls == 3


def test_persistent_5xx_eventually_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(fec_mod.time, "sleep", lambda *_: None)
    c = _client(tmp_path, [_Resp(500)] * 5)
    with pytest.raises(httpx.HTTPStatusError):
        c.get("/x/", use_cache=False)
    assert c._client.calls == 5


def test_4xx_is_not_retried(tmp_path, monkeypatch):
    # Client errors (bad request, auth) are NOT transient — no retry burn.
    monkeypatch.setattr(fec_mod.time, "sleep", lambda *_: None)
    c = _client(tmp_path, [_Resp(404)] * 5)
    with pytest.raises(httpx.HTTPStatusError):
        c.get("/x/", use_cache=False)
    assert c._client.calls == 1


def test_corrupt_cache_treated_as_miss_and_repaired(tmp_path, monkeypatch):
    # A truncated cache file (interrupted write from the pre-atomic era) must
    # read as a MISS — refetch, then overwrite with good JSON — not raise
    # JSONDecodeError forever.
    monkeypatch.setattr(fec_mod.time, "sleep", lambda *_: None)
    c = _client(tmp_path, [_Resp(200, {"results": [{"ok": 1}], "pagination": {}})])
    cache = c._cache_path("/x/", {"a": 1})
    cache.write_text('{"results": [{"trunc')          # simulated torn write
    data = c.get("/x/", {"a": 1})                     # default use_cache=True
    assert data["results"] == [{"ok": 1}]
    assert c._client.calls == 1
    # Cache is repaired: the next read is served from disk, no new HTTP call.
    again = c.get("/x/", {"a": 1})
    assert again["results"] == [{"ok": 1}]
    assert c._client.calls == 1


def test_cache_write_is_atomic_no_stray_tempfiles(tmp_path, monkeypatch):
    # The tempfile is either promoted via os.replace or unlinked — a normal
    # write leaves exactly the final cache file behind.
    monkeypatch.setattr(fec_mod.time, "sleep", lambda *_: None)
    c = _client(tmp_path, [_Resp(200, {"results": [], "pagination": {}})])
    c.get("/y/", {"b": 2})
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".cache-")]
    assert leftovers == []
    assert c._cache_path("/y/", {"b": 2}).exists()
