"""Per-domain rate limiting (spec §3.3)."""
from __future__ import annotations

import threading
import time
from urllib.parse import urlparse


class DomainRateLimiter:
    """Enforce a minimum delay between requests to the same domain.

    Thread-safe, so ONE instance can be shared across scan-all workers: all
    state is guarded by a lock, and each waiter *reserves* its send slot under
    the lock before sleeping — concurrent waiters for the same domain queue
    behind each other instead of all sleeping to the same instant and firing
    at once. The sleep itself happens outside the lock, so a long wait on one
    domain never blocks callers hitting other domains.

    ``backoff`` pushes a domain's next-allowed request into the future (used
    by the crawler on 429/503 responses); ``wait`` honors it.

    ``monotonic``/``sleep`` are injectable for deterministic tests (same
    pattern as TokenRateLimiter).
    """

    def __init__(self, min_delay_seconds: float = 2.0, *,
                 monotonic=time.monotonic, sleep=time.sleep) -> None:
        self.min_delay = min_delay_seconds
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        # domain -> scheduled time of that domain's most recent request slot
        self._last: dict[str, float] = {}
        # domain -> absolute monotonic time before which no request may go out
        self._blocked_until: dict[str, float] = {}

    @staticmethod
    def _domain(url: str) -> str:
        return urlparse(url).netloc

    def wait(self, url: str, extra_delay: float | None = None) -> None:
        domain = self._domain(url)
        delay = max(self.min_delay, extra_delay or 0.0)
        with self._lock:
            now = self._monotonic()
            slot = now
            last = self._last.get(domain)
            if last is not None:
                slot = max(slot, last + delay)
            slot = max(slot, self._blocked_until.get(domain, 0.0))
            # Reserve the slot before releasing the lock (see class docstring).
            self._last[domain] = slot
        if slot > now:
            self._sleep(slot - now)

    def backoff(self, url: str, seconds: float) -> None:
        """Push the domain's next-allowed request to at least ``seconds`` from
        now (e.g. after a 429/503). Never *shortens* an existing backoff, so
        concurrent workers can all report rate-limit responses safely."""
        domain = self._domain(url)
        with self._lock:
            until = self._monotonic() + max(0.0, seconds)
            if until > self._blocked_until.get(domain, 0.0):
                self._blocked_until[domain] = until
