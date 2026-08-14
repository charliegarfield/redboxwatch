"""Tests for the per-domain rate limiter: min-delay, backoff, thread-safety.

Deterministic via an injected clock/sleep (same pattern as the token-limiter
tests) — no real sleeping, no network.
"""
from __future__ import annotations

import threading

from redbox.ratelimit import DomainRateLimiter


class FakeClock:
    """Virtual time: sleep advances the clock; monotonic reads it."""

    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self):
        return self.t

    def sleep(self, dt):
        self.sleeps.append(dt)
        self.t += dt


def _limiter(delay: float, clk: FakeClock) -> DomainRateLimiter:
    return DomainRateLimiter(delay, monotonic=clk.monotonic, sleep=clk.sleep)


def test_first_request_does_not_wait():
    clk = FakeClock()
    lim = _limiter(2.0, clk)
    lim.wait("https://a.example/x")
    assert clk.sleeps == []


def test_min_delay_between_same_domain_requests():
    clk = FakeClock()
    lim = _limiter(2.0, clk)
    lim.wait("https://a.example/x")
    lim.wait("https://a.example/y")           # too soon -> sleeps ~2s
    assert abs(sum(clk.sleeps) - 2.0) < 1e-6


def test_extra_delay_takes_precedence_when_larger():
    clk = FakeClock()
    lim = _limiter(2.0, clk)
    lim.wait("https://a.example/x")
    lim.wait("https://a.example/y", extra_delay=10.0)   # Crawl-delay: 10
    assert abs(sum(clk.sleeps) - 10.0) < 1e-6


def test_backoff_pushes_next_allowed_and_wait_honors_it():
    clk = FakeClock()
    lim = _limiter(0.0, clk)
    lim.wait("https://a.example/x")                 # t=0, no wait
    lim.backoff("https://a.example/x", 30.0)        # next allowed >= t=30
    lim.wait("https://a.example/y")
    assert abs(sum(clk.sleeps) - 30.0) < 1e-6
    # Other domains are unaffected by a.example's backoff.
    before = list(clk.sleeps)
    lim.wait("https://b.example/z")
    assert clk.sleeps == before


def test_backoff_never_shortens_an_existing_backoff():
    clk = FakeClock()
    lim = _limiter(0.0, clk)
    lim.backoff("https://a.example/", 60.0)
    lim.backoff("https://a.example/", 5.0)          # must NOT shrink the 60s
    lim.wait("https://a.example/x")
    assert abs(sum(clk.sleeps) - 60.0) < 1e-6


def test_thread_safety_smoke():
    # One limiter shared across scan-all workers: concurrent wait/backoff
    # calls must not raise. No-op sleep keeps this fast; the point is that
    # dict/state mutation under concurrency is lock-protected.
    lim = DomainRateLimiter(0.0, sleep=lambda s: None)
    errors: list[BaseException] = []

    def hammer(i: int) -> None:
        try:
            for j in range(200):
                url = f"https://d{j % 5}.example/p{i}-{j}"
                lim.wait(url)
                lim.backoff(url, 0.001)
        except BaseException as e:  # pragma: no cover - failure path
            errors.append(e)

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
