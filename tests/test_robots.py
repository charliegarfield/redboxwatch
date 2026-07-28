"""Offline tests for robots.txt policy enforcement (ROBOTS_POLICY.md)."""
from __future__ import annotations

from redbox.robots import RobotsPolicy


def _policy(**kw):
    return RobotsPolicy(default="respect", **kw)


def _stub_robots(policy, text: str | None):
    """Make the policy 'fetch' the given robots.txt body without network."""
    from urllib.robotparser import RobotFileParser

    def parser(scheme, domain):
        if domain in policy._cache:
            return policy._cache[domain]
        rp = None
        if text is not None:
            rp = RobotFileParser()
            rp.parse(text.splitlines())
        policy._cache[domain] = rp
        return rp

    policy._parser = parser


def test_override_matches_www_port_and_case_variants():
    # An operator writing `example.com: Override` means the site — including
    # the www. form the candidate's URL actually resolves to. Exact-netloc
    # matching made the deliberate override a silent no-op.
    p = _policy(per_domain={"Example.com": "Override"})
    for url in ("https://example.com/x", "https://www.example.com/x",
                "https://WWW.EXAMPLE.COM/x", "https://example.com:443/x"):
        allowed, posture = p.can_fetch(url)
        assert (allowed, posture) == (True, "override"), url


def test_www_keyed_override_matches_apex():
    p = _policy(per_domain={"www.example.com": "override"})
    assert p.posture_for("example.com") == "override"


def test_respect_disallow_blocks():
    p = _policy()
    _stub_robots(p, "User-agent: *\nDisallow: /private/")
    assert p.can_fetch("https://x.com/private/page") == (False, "respect")
    assert p.can_fetch("https://x.com/public") == (True, "respect")


def test_missing_robots_allows():
    p = _policy()
    _stub_robots(p, None)          # 404 -> no robots.txt
    assert p.can_fetch("https://x.com/anything") == (True, "respect")


def test_5xx_robots_temporarily_disallows(monkeypatch):
    # A host answering 503 for robots.txt is under stress: standard practice
    # is temporarily-disallow, not open season on up to 3x max_pages fetches.
    import httpx

    p = _policy()

    def fake_get(url, **kw):
        return httpx.Response(503, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    allowed, posture = p.can_fetch("https://stressed.example/page")
    assert allowed is False


def test_crawl_delay_surfaces_from_cache():
    p = _policy()
    _stub_robots(p, "User-agent: *\nCrawl-delay: 7")
    p.can_fetch("https://x.com/a")             # populates the cache
    assert p.crawl_delay("https://x.com/a") == 7
