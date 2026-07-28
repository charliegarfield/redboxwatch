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
        # Keys AND values normalized: an operator writing `example.com:
        # Override` means the site whose candidates resolve to
        # www.example.com too — an exact-netloc lookup made the deliberate,
        # logged override a silent no-op for www/port/case variants.
        self.per_domain = {self._norm_host(k): (v or "").strip().lower()
                           for k, v in (per_domain or {}).items()}
        self.user_agent = user_agent
        self.timeout = timeout
        self._cache: dict[str, RobotFileParser | None] = {}

    @staticmethod
    def _norm_host(host: str) -> str:
        h = (host or "").strip().lower().split(":", 1)[0]
        return h[4:] if h.startswith("www.") else h

    def posture_for(self, domain: str) -> str:
        return self.per_domain.get(self._norm_host(domain), self.default)

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
            if resp.status_code >= 500:
                # Host under stress: standard crawler practice is temporarily
                # disallow, not open season. Cached for the process lifetime,
                # so the site reads robots_blocked this run and is retried on
                # the next (`scan-all --rescan` also re-attempts it).
                rp.parse(["User-agent: *", "Disallow: /"])
            elif resp.status_code >= 400:
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
