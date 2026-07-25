"""Tests for the token-rate limiter (deterministic via injected clock/sleep)."""
from __future__ import annotations

from redbox.ratelimit_tokens import (PerModelTokenRateLimiter, TokenRateLimiter,
                                     estimate_input_tokens)


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


def _limiter(tpm, clock, initial_fraction=0.0):
    return TokenRateLimiter(tpm, monotonic=clock.monotonic, sleep=clock.sleep,
                            initial_fraction=initial_fraction)


def test_starts_empty_so_first_call_paces():
    # Default: bucket starts EMPTY -> even the first call waits for refill,
    # preventing a startup burst past the sliding-window API ceiling.
    clk = FakeClock()
    lim = _limiter(60_000, clk)        # 1000 tok/sec, empty
    lim.acquire(30_000)                # must accrue 30k -> ~30s
    assert sum(clk.sleeps) >= 29.0


def test_initial_fraction_allows_some_immediate_headroom():
    clk = FakeClock()
    lim = _limiter(60_000, clk, initial_fraction=1.0)   # opt into full start
    lim.acquire(50_000)
    assert clk.sleeps == []            # full bucket -> no wait


def test_no_startup_burst_then_sustained_rate_bounded():
    clk = FakeClock()
    lim = _limiter(60_000, clk)        # empty start
    # 12 calls of 10k = 120k tokens at 60k/min must take >= ~120s (no free burst).
    for _ in range(12):
        lim.acquire(10_000)
    assert clk.t >= 119.0


def test_oversize_request_is_clamped_to_capacity():
    clk = FakeClock()
    lim = _limiter(60_000, clk, initial_fraction=1.0)
    lim.acquire(100_000)               # bigger than capacity -> clamped, still returns
    # second call must wait for a full refill, not for 100k
    lim.acquire(60_000)
    assert clk.t >= 59.0


def test_per_model_buckets_are_independent():
    # Haiku and Sonnet have separate budgets: draining Haiku's bucket must not
    # make a Sonnet call wait, and vice versa.
    clk = FakeClock()
    lim = PerModelTokenRateLimiter(
        {"haiku": 60_000, "sonnet": 60_000},
        monotonic=clk.monotonic, sleep=clk.sleep)
    lim.acquire(60_000, model="haiku")     # drains the haiku bucket (empty start -> ~60s)
    t_after_haiku = clk.t
    lim.acquire(60_000, model="sonnet")    # sonnet refilled over that same 60s -> no extra wait
    assert clk.t == t_after_haiku


def test_per_model_unconfigured_model_is_unmetered():
    clk = FakeClock()
    lim = PerModelTokenRateLimiter({"haiku": 60_000},
                                   monotonic=clk.monotonic, sleep=clk.sleep)
    lim.acquire(1_000_000, model="some-other-model")   # no bucket -> no wait
    assert clk.sleeps == []


def test_per_model_drops_zero_limits():
    lim = PerModelTokenRateLimiter({"haiku": 60_000, "sonnet": 0})
    assert set(lim.limits) == {"haiku"}        # zero/falsey limits are not metered


def test_single_bucket_accepts_model_kwarg():
    # TokenRateLimiter.acquire must accept (and ignore) model= for interface parity.
    clk = FakeClock()
    lim = _limiter(60_000, clk, initial_fraction=1.0)
    lim.acquire(1_000, model="anything")
    assert clk.sleeps == []


def test_estimate_input_tokens():
    # ~4 chars/token + fixed prompt cost.
    assert estimate_input_tokens("", prompt_tokens=700) == 700
    assert estimate_input_tokens("x" * 4000, prompt_tokens=700) == 1700
