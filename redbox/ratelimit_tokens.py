"""Thread-safe token-rate limiter for the Anthropic API (tokens-per-minute).

The org has a tokens/min ceiling per model (e.g. 450k input tokens/min on Haiku).
Concurrency limits alone don't respect it: a few concurrent large-page
classifications sustain more than the ceiling and trigger 429s. This bucket
gates *every* classification call to stay under a configured tokens/min budget,
across all scan-all worker threads (it's a process-wide shared instance).

Design: a classic token bucket refilled continuously at `rate` tokens/sec, with
capacity = one minute's budget. ``acquire(n)`` blocks until `n` tokens are
available, then deducts them. Estimation of `n` (input tokens for a request) is
approximate — we use chars/4 plus the fixed prompt — and deliberately a slight
over-estimate so we stay comfortably under the real ceiling.

Anthropic's ceilings are PER MODEL, so :class:`PerModelTokenRateLimiter` holds
one bucket per model (first-pass Haiku and escalation Sonnet no longer share a
single budget). The meter is input-tokens only — output token limits are a
separate, generally non-binding ceiling we don't track here.
"""
from __future__ import annotations

import threading
import time


class TokenRateLimiter:
    def __init__(self, tokens_per_minute: float, *, monotonic=time.monotonic,
                 sleep=time.sleep, initial_fraction: float = 0.0):
        self.capacity = float(tokens_per_minute)
        self.rate = float(tokens_per_minute) / 60.0   # tokens per second
        # Start (nearly) EMPTY, not full. The API ceiling is a sliding-window
        # rate, not a bucket — a full bucket lets a large burst through at
        # startup that exceeds the window. Starting empty paces from call one.
        self._tokens = float(tokens_per_minute) * max(0.0, min(1.0, initial_fraction))
        self._last = monotonic()
        self._lock = threading.Lock()
        self._monotonic = monotonic
        self._sleep = sleep

    def _refill_locked(self) -> None:
        now = self._monotonic()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last = now

    def acquire(self, n: float, *, model: str | None = None) -> None:
        """Block until `n` tokens are available, then consume them.

        A single request larger than the per-minute capacity is clamped to
        capacity (it can never otherwise proceed); it still waits for a full
        bucket, which is the best we can do under the ceiling.

        ``model`` is accepted and ignored so this single-bucket limiter is
        call-compatible with :class:`PerModelTokenRateLimiter`.
        """
        n = min(float(n), self.capacity)
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= n:
                    self._tokens -= n
                    return
                # How long until enough tokens accrue?
                deficit = n - self._tokens
                wait = deficit / self.rate
            # Sleep outside the lock so other threads can refill/observe.
            self._sleep(min(wait, 5.0))


class PerModelTokenRateLimiter:
    """A separate token bucket per model, sharing the ``acquire`` interface.

    Anthropic enforces token/min ceilings PER MODEL. A single shared bucket for
    first-pass (Haiku) and escalation (Sonnet) is wrong both ways: it throttles
    the cheap model to protect the expensive one's budget, yet still lets their
    combined rate violate either model's actual ceiling. Each model gets its own
    bucket here; a call for a model with no configured limit is a no-op (that
    model is simply unmetered).
    """

    def __init__(self, limits: dict[str, float], *, monotonic=time.monotonic,
                 sleep=time.sleep, initial_fraction: float = 0.0):
        # Keep only positive limits; expose the resolved map for display/inspection.
        self.limits = {m: float(v) for m, v in limits.items() if v}
        self._buckets = {
            m: TokenRateLimiter(v, monotonic=monotonic, sleep=sleep,
                                initial_fraction=initial_fraction)
            for m, v in self.limits.items()
        }

    def acquire(self, n: float, *, model: str | None = None) -> None:
        bucket = self._buckets.get(model)
        if bucket is not None:
            bucket.acquire(n)


def estimate_input_tokens(text: str, *, prompt_tokens: int = 700,
                          chars: int | None = None) -> int:
    """Rough input-token estimate for one classification call.

    ~4 chars/token for English; add the fixed system-prompt cost. Intentionally
    a mild over-estimate so the limiter errs toward staying under the ceiling.
    Pass ``chars`` to estimate from a length alone (the cost preflight used to
    allocate a multi-megabyte throwaway string for this).
    """
    n = chars if chars is not None else len(text or "")
    return int(n / 4) + prompt_tokens
