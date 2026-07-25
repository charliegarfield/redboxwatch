"""Per-domain rate limiting (spec §3.3)."""
from __future__ import annotations

import time
from urllib.parse import urlparse


class DomainRateLimiter:
    """Enforce a minimum delay between requests to the same domain."""

    def __init__(self, min_delay_seconds: float = 2.0) -> None:
        self.min_delay = min_delay_seconds
        self._last: dict[str, float] = {}

    def wait(self, url: str, extra_delay: float | None = None) -> None:
        domain = urlparse(url).netloc
        delay = max(self.min_delay, extra_delay or 0.0)
        last = self._last.get(domain)
        now = time.monotonic()
        if last is not None:
            elapsed = now - last
            if elapsed < delay:
                time.sleep(delay - elapsed)
        self._last[domain] = time.monotonic()
