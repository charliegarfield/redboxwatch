"""robots.txt handling (spec §3.3, ROBOTS_POLICY.md).

Default posture is *respect*; per-domain ``override`` is allowed but must be
explicit in config and is surfaced on each scan record so collection of any page
is auditable.
"""
from __future__ import annotations

from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx


class RobotsPolicy:
    def __init__(
        self,
        *,
        default: str = "respect",
        per_domain: dict[str, str] | None = None,
        user_agent: str = "RedBoxTracker/0.1",
        timeout: float = 15.0,
    ) -> None:
        self.default = default
        self.per_domain = {k.lower(): v for k, v in (per_domain or {}).items()}
        self.user_agent = user_agent
        self.timeout = timeout
        self._cache: dict[str, RobotFileParser | None] = {}

    def posture_for(self, domain: str) -> str:
        return self.per_domain.get(domain.lower(), self.default)

    def _parser(self, scheme: str, domain: str) -> RobotFileParser | None:
        if domain in self._cache:
            return self._cache[domain]
        rp = RobotFileParser()
        url = f"{scheme}://{domain}/robots.txt"
        try:
            resp = httpx.get(
                url, timeout=self.timeout, headers={"User-Agent": self.user_agent},
                follow_redirects=True,
            )
            if resp.status_code >= 400:
                rp = None  # no robots.txt -> allow
            else:
                rp.parse(resp.text.splitlines())
        except httpx.HTTPError:
            rp = None
        self._cache[domain] = rp
        return rp

    def can_fetch(self, url: str) -> tuple[bool, str]:
        """Return (allowed, posture). ``posture`` is 'respect' or 'override'."""
        parsed = urlparse(url)
        domain = parsed.netloc
        posture = self.posture_for(domain)
        if posture == "override":
            return True, "override"
        rp = self._parser(parsed.scheme or "https", domain)
        if rp is None:
            return True, "respect"
        return rp.can_fetch(self.user_agent, url), "respect"

    def crawl_delay(self, url: str) -> float | None:
        parsed = urlparse(url)
        rp = self._cache.get(parsed.netloc)
        if rp is None:
            return None
        try:
            return rp.crawl_delay(self.user_agent)
        except Exception:
            return None
