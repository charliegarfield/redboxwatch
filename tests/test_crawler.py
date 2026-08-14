"""Offline tests for crawler enumeration + orchestration (spec §3.3)."""
from __future__ import annotations

from redbox.crawler import Crawler, FetchResult
from redbox.ratelimit import DomainRateLimiter
from redbox.robots import RobotsPolicy


class FakeFetcher:
    """Serve an in-memory site: url -> (html, visible_text, dom_text)."""

    def __init__(self, pages: dict[str, tuple[str, str, str]]):
        self.pages = pages
        self.calls: list[str] = []

    def fetch(self, url: str, *, screenshot: bool = True) -> FetchResult:
        self.calls.append(url)
        key = url.rstrip("/") or url
        html, vis, dom = self.pages.get(key, ("", "", ""))
        return FetchResult(
            url=url, final_url=url, status=200 if key in self.pages else 404,
            content_type="text/html", render_mode="browser",
            html=html, visible_text=vis, dom_text=dom,
            screenshot_png=b"PNG" if screenshot else None,
        )


def _no_robots() -> RobotsPolicy:
    # default respect, but no network: monkeypatch parser to allow-all.
    rp = RobotsPolicy(default="respect")
    rp._cache = {}
    rp._parser = lambda scheme, domain: None  # type: ignore
    return rp


def _crawler(fetcher, depth=2, max_pages=150):
    return Crawler(
        fetcher, robots=_no_robots(), rate_limiter=DomainRateLimiter(0.0),
        common_paths=["/media", "/press"], crawl_depth=depth, max_pages=max_pages,
    )


def test_enumerate_includes_seed_and_common_paths():
    c = _crawler(FakeFetcher({}))
    urls = dict(c.enumerate_urls("https://example.com"))
    assert "https://example.com" in urls
    assert urls["https://example.com/media"] == "common_path"
    assert urls["https://example.com/press"] == "common_path"


def test_canonical_collapses_www_and_slash():
    canon = Crawler._canonical
    assert canon("https://www.example.com/media/") == canon("https://example.com/media")
    assert canon("http://example.com/media") == canon("https://www.example.com/media/")
    assert canon("https://example.com/a") != canon("https://example.com/b")


def test_crawl_does_not_fetch_www_and_nonwww_twice():
    # Home links to BOTH the non-www and www form of /media; only one fetch.
    pages = {
        "https://example.com": (
            '<a href="https://example.com/media">m1</a>'
            '<a href="https://www.example.com/media">m2</a>', "home", "home"),
        "https://example.com/media": ("", "media page", "media page"),
        "https://www.example.com/media": ("", "media page", "media page"),
    }
    fetcher = FakeFetcher(pages)
    c = _crawler(fetcher, depth=2)
    results = list(c.crawl_site("https://example.com"))
    media_fetches = [u for u in fetcher.calls if u.rstrip("/").endswith("/media")]
    assert len(media_fetches) == 1, media_fetches


def test_extract_links_drops_asset_links():
    # Image/media/feed hrefs are never classifiable pages — don't spend a
    # browser cycle (and a page-budget slot) fetching them.
    c = _crawler(FakeFetcher({}))
    html = ('<a href="/headshot.JPG">photo</a>'
            '<a href="/logo.png?v=2">logo</a>'
            '<a href="/feed.xml">rss</a>'
            '<a href="/kit.pdf?dl=1">pdf with query</a>'
            '<a href="/about">real page</a>')
    pages, pdfs = c._extract_links("https://example.com", html)
    assert pages == ["https://example.com/about"]
    assert pdfs == ["https://example.com/kit.pdf?dl=1"]   # query-string PDF caught


def test_extract_links_excludes_offdomain_pdfs_and_pages():
    # A campaign site that links to an off-domain Senate PDF must not yield it —
    # fetching it would misattribute someone else's content to this candidate.
    c = _crawler(FakeFetcher({}))
    html = ('<a href="/kit.pdf">own pdf</a>'
            '<a href="https://www.markey.senate.gov/doc/letter.pdf">senate pdf</a>'
            '<a href="/about">own page</a>'
            '<a href="https://other.com/x">offsite page</a>')
    pages, pdfs = c._extract_links("https://example.com", html)
    assert pdfs == ["https://example.com/kit.pdf"]          # only same-domain PDF
    assert "https://example.com/about" in pages
    assert not any("markey.senate.gov" in u for u in pages + pdfs)
    assert not any("other.com" in u for u in pages + pdfs)


def test_link_crawl_depth_and_pdf_discovery(monkeypatch):
    home = (
        '<html><body><a href="/media">media</a> '
        '<a href="/about">about</a> '
        '<a href="https://other.com/x">offsite</a></body></html>'
    )
    media = (
        '<html><body><a href="/guidance.pdf">kit</a> '
        '<a href="/deep">deep</a></body></html>'
    )
    pages = {
        "https://example.com": (home, "home", "home"),
        "https://example.com/media": (media, "media page", "media page"),
        "https://example.com/about": ("<html><body>about</body></html>", "about", "about"),
        "https://example.com/deep": ("<html><body>deep</body></html>", "deep", "deep"),
    }
    fetcher = FakeFetcher(pages)
    c = _crawler(fetcher, depth=2)
    # avoid real PDF network fetch
    import redbox.crawler as cm
    monkeypatch.setattr(cm, "fetch_pdf", lambda url, **k: FetchResult(
        url=url, final_url=url, status=200, content_type="application/pdf",
        render_mode="pdf", visible_text="pdf text", dom_text="pdf text"))

    results = list(c.crawl_site("https://example.com"))
    fetched = {r.url.rstrip("/") for r in results}

    assert "https://example.com" in fetched
    assert "https://example.com/media" in fetched
    assert "https://example.com/about" in fetched          # depth-1 link
    assert "https://example.com/guidance.pdf" in fetched   # linked PDF
    assert not any("other.com" in u for u in fetched)      # offsite excluded
    # one of the results is the PDF, extracted as text
    assert any(r.render_mode == "pdf" and r.visible_text == "pdf text" for r in results)


def test_max_pages_cap_still_fetches_media(monkeypatch):
    # Home links to 20 ordinary pages + /media. With max_pages=5, ordinary fetches
    # are capped but /media (high-value) is always fetched.
    links = "".join(f'<a href="/p{i}">p{i}</a>' for i in range(20)) + '<a href="/media">m</a>'
    pages = {"https://example.com": (f"<html><body>{links}</body></html>", "home", "home")}
    for i in range(20):
        pages[f"https://example.com/p{i}"] = ("<html><body>ordinary</body></html>", "x", "x")
    pages["https://example.com/media"] = ("<html><body>media kit</body></html>", "media", "media")
    fetcher = FakeFetcher(pages)
    c = _crawler(fetcher, depth=1, max_pages=5)
    results = list(c.crawl_site("https://example.com"))
    fetched = {r.url.rstrip("/") for r in results}
    # /media survives the cap
    assert "https://example.com/media" in fetched
    # ordinary pages are capped (home + at most 5 ordinary p-pages)
    ordinary = [u for u in fetched if "/p" in u]
    assert len(ordinary) <= 5, ordinary


def test_high_value_pages_have_their_own_budget():
    # A blog living under /news/ used to bypass max_pages entirely (substring
    # high-value match). Now high-value pages draw on their OWN budget of
    # max_pages, so the crawl is hard-bounded at 3x max_pages total.
    links = "".join(f'<a href="/news/post-{i}">n{i}</a>' for i in range(30))
    links += "".join(f'<a href="/p{i}">p{i}</a>' for i in range(30))
    pages = {"https://example.com": (f"<html><body>{links}</body></html>", "home", "home")}
    for i in range(30):
        pages[f"https://example.com/news/post-{i}"] = ("<html><body>post</body></html>", "x", "x")
        pages[f"https://example.com/p{i}"] = ("<html><body>ordinary</body></html>", "x", "x")
    fetcher = FakeFetcher(pages)
    # No common-path probes: those are high-value fetches too and would draw on
    # the same budget, muddying the count this test is about.
    c = Crawler(fetcher, robots=_no_robots(), rate_limiter=DomainRateLimiter(0.0),
                common_paths=[], crawl_depth=1, max_pages=5)
    results = list(c.crawl_site("https://example.com"))
    fetched = [r.url for r in results]
    high = [u for u in fetched if "/news/" in u]
    ordinary = [u for u in fetched if "example.com/p" in u]
    assert len(high) == 5, high                       # capped, not unbounded
    assert len(ordinary) <= 5, ordinary               # existing ordinary cap holds
    assert len(fetched) <= 15                          # hard total bound (3x)
    # High-value pages are still fetched ahead of ordinary link-crawl pages.
    assert fetched.index(high[0]) < fetched.index(ordinary[0])


def test_pdf_fetches_are_budgeted(monkeypatch):
    # 30 linked PDFs, max_pages=5 -> only 5 PDF fetches (their own budget).
    links = "".join(f'<a href="/doc-{i:02d}.pdf">d{i}</a>' for i in range(30))
    pages = {"https://example.com": (f"<html><body>{links}</body></html>", "home", "home")}
    fetcher = FakeFetcher(pages)
    c = _crawler(fetcher, depth=1, max_pages=5)
    import redbox.crawler as cm
    monkeypatch.setattr(cm, "fetch_pdf", lambda url, **k: FetchResult(
        url=url, final_url=url, status=200, content_type="application/pdf",
        render_mode="pdf", visible_text="pdf text", dom_text="pdf text"))
    results = list(c.crawl_site("https://example.com"))
    pdfs = [r for r in results if r.render_mode == "pdf"]
    assert len(pdfs) == 5, [r.url for r in pdfs]


def test_robots_disallow_blocks_fetch():
    rp = RobotsPolicy(default="respect")
    # pretend robots.txt disallows everything
    class _RP:
        def can_fetch(self, ua, url): return False
        def crawl_delay(self, ua): return None
    rp._cache = {"example.com": _RP()}
    rp._parser = lambda scheme, domain: rp._cache.get(domain)  # type: ignore

    fetcher = FakeFetcher({"https://example.com": ("<html></html>", "x", "x")})
    c = Crawler(fetcher, robots=rp, rate_limiter=DomainRateLimiter(0.0),
                common_paths=[], crawl_depth=1)
    results = list(c.crawl_site("https://example.com"))
    assert results == []
    assert fetcher.calls == []        # never even fetched


def test_classifier_text_unions_hidden_dom():
    r = FetchResult(
        url="u", final_url="u", status=200, content_type="text/html",
        render_mode="browser", visible_text="Visible line",
        dom_text="Visible line\nHidden directive line",
    )
    text = r.classifier_text
    assert "Visible line" in text
    assert "Hidden directive line" in text
    assert r.text_hash  # stable hash present


def test_relative_links_resolve_against_the_page_not_the_seed():
    # href="article-1" on /news/2026/ means /news/2026/article-1. Resolving
    # against the site seed produced site-root 404s and never crawled the
    # real page.
    c = _crawler(None)
    pages, _ = c._extract_links(
        "https://x.example/news/2026/",
        '<a href="article-1">a</a><a href="/about">b</a>',
        site_url="https://x.example")
    assert "https://x.example/news/2026/article-1" in pages
    assert "https://x.example/about" in pages


def test_extract_links_honors_base_href():
    c = _crawler(None)
    pages, _ = c._extract_links(
        "https://x.example/news/2026/",
        '<head><base href="https://x.example/docs/"></head>'
        '<a href="kit">a</a>',
        site_url="https://x.example")
    assert pages == ["https://x.example/docs/kit"]


def test_crawl_resolves_links_from_the_fetched_page(tmp_path):
    # End-to-end: a depth-1 page under /news/ links relatively to a sibling;
    # the crawler must fetch the sibling, not a phantom site-root URL.
    class SiteFetcher:
        fetched = []

        def fetch(self, url, *, screenshot=True):
            self.fetched.append(url)
            html = {
                "https://x.example": '<a href="/news/2026/">news</a>',
                "https://x.example/news/2026": '<a href="article-1">art</a>',
                "https://x.example/news/2026/article-1": "story",
            }.get(url.rstrip("/") or url, "")
            status = 200 if html else 404
            return FetchResult(url=url, final_url=url, status=status,
                               content_type="text/html", render_mode="browser",
                               html=html, visible_text=html, dom_text=html)

    rp = RobotsPolicy(default="respect")
    rp._parser = lambda scheme, domain: None
    crawler = Crawler(SiteFetcher(), robots=rp, rate_limiter=DomainRateLimiter(0.0),
                      common_paths=[], crawl_depth=2)
    crawler.sitemap_urls = lambda base: []
    results = list(crawler.crawl_site("https://x.example"))
    urls = [r.url for r in results]
    assert "https://x.example/news/2026/article-1" in urls
    assert "https://x.example/article-1" not in urls


def test_fetch_pdf_dripfeed_guard(monkeypatch):
    # A server that drips bytes forever must trip the TOTAL wall-clock guard —
    # httpx's own read timeout resets per chunk and never fires on a drip-feed.
    import httpx
    import pytest

    import redbox.crawler as cm

    class _DripResponse:
        status_code = 200
        url = "https://slow.example/a.pdf"
        headers = {"content-type": "application/pdf"}

        def iter_bytes(self):
            while True:
                yield b"x"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(cm.httpx, "stream", lambda *a, **k: _DripResponse())

    t = {"now": 0.0}

    def _clock():
        t["now"] += 30.0                 # every look at the clock advances 30s
        return t["now"]

    monkeypatch.setattr("time.monotonic", _clock)

    with pytest.raises(httpx.ReadTimeout, match="drip-feed guard"):
        cm.fetch_pdf("https://slow.example/a.pdf", user_agent="t", max_total_seconds=120)


# --- sitemap-enumeration guards --------------------------------------------
# Enumeration runs on the crawl's first next(), before pipeline's per-candidate
# ceiling is ever checked — so sitemap_urls must bound itself (child-count cap,
# total-loc cap, per-fetch byte/deadline guard, whole-pass time budget).

def _index_xml(n: int) -> str:
    return ("<sitemapindex>"
            + "".join(f"<loc>https://x.example/sm-{i}.xml</loc>" for i in range(n))
            + "</sitemapindex>")


def _patch_locations(monkeypatch, cm, urls):
    monkeypatch.setattr(cm.Crawler, "_sitemap_locations",
                        lambda self, base: list(urls))


def test_sitemap_index_child_cap(monkeypatch):
    # An index with 100 children: only SITEMAP_MAX_CHILDREN are ever fetched —
    # each child costs a rate-limit wait + a fetch, so an unbounded index used
    # to pin a worker for hours at 0% CPU.
    import redbox.crawler as cm

    fetched: list[str] = []

    def fake_get(self, url):
        fetched.append(url)
        if "index" in url:
            return _index_xml(100)
        return f"<urlset><loc>https://x.example/p-{len(fetched)}</loc></urlset>"

    monkeypatch.setattr(cm.Crawler, "_get_text", fake_get)
    _patch_locations(monkeypatch, cm, ["https://x.example/sitemap_index.xml"])
    c = _crawler(FakeFetcher({}))
    urls = c.sitemap_urls("https://x.example")
    child_fetches = [u for u in fetched if "/sm-" in u]
    assert len(child_fetches) == cm.SITEMAP_MAX_CHILDREN, child_fetches
    assert len(urls) == cm.SITEMAP_MAX_CHILDREN   # one loc per fetched child


def test_sitemap_total_locs_capped(monkeypatch):
    # A single flat sitemap listing far more URLs than the cap -> truncated.
    import redbox.crawler as cm

    big = ("<urlset>"
           + "".join(f"<loc>https://x.example/p{i}</loc>"
                     for i in range(cm.SITEMAP_MAX_LOCS + 500))
           + "</urlset>")
    monkeypatch.setattr(cm.Crawler, "_get_text", lambda self, url: big)
    _patch_locations(monkeypatch, cm, ["https://x.example/sitemap.xml"])
    c = _crawler(FakeFetcher({}))
    urls = c.sitemap_urls("https://x.example")
    assert len(urls) == cm.SITEMAP_MAX_LOCS


def test_sitemap_loc_cap_stops_child_expansion(monkeypatch):
    # Children carry 1500 locs each: after the loc cap (2000) is reached the
    # remaining children must not even be fetched.
    import redbox.crawler as cm

    fetched: list[str] = []

    def fake_get(self, url):
        fetched.append(url)
        if "index" in url:
            return _index_xml(10)
        n = len(fetched)
        return ("<urlset>"
                + "".join(f"<loc>https://x.example/c{n}-{i}</loc>" for i in range(1500))
                + "</urlset>")

    monkeypatch.setattr(cm.Crawler, "_get_text", fake_get)
    _patch_locations(monkeypatch, cm, ["https://x.example/sitemap_index.xml"])
    c = _crawler(FakeFetcher({}))
    urls = c.sitemap_urls("https://x.example")
    child_fetches = [u for u in fetched if "/sm-" in u]
    assert len(child_fetches) == 2, child_fetches   # 1500 + 1500 >= 2000 -> stop
    assert len(urls) == cm.SITEMAP_MAX_LOCS


def test_get_text_bytes_cap_is_a_failed_fetch(monkeypatch):
    # A sitemap streaming more than SITEMAP_MAX_BYTES is treated as a failed
    # fetch (None) and enumeration continues with the next sitemap location.
    import redbox.crawler as cm

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/xml"}
        encoding = "utf-8"

        def __init__(self, url, body_iter):
            self.url = url
            self._body_iter = body_iter

        def iter_bytes(self):
            return self._body_iter()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _huge():
        chunk = b"<urlset>" + b"x" * 1_000_000
        while True:
            yield chunk

    def _good():
        yield b"<urlset><loc>https://x.example/ok</loc></urlset>"

    def fake_stream(method, url, **kw):
        return _Resp(url, _huge if "huge" in url else _good)

    monkeypatch.setattr(cm.httpx, "stream", fake_stream)
    c = _crawler(FakeFetcher({}))
    assert c._get_text("https://x.example/huge.xml") is None
    _patch_locations(monkeypatch, cm,
                     ["https://x.example/huge.xml", "https://x.example/sitemap.xml"])
    urls = c.sitemap_urls("https://x.example")
    assert urls == ["https://x.example/ok"]


# --- 429/503 backoff + host abandonment -------------------------------------

class StatusFetcher:
    """Fetcher with canned (status, headers) per URL; 200 + html otherwise."""

    def __init__(self, html: dict[str, str],
                 statuses: dict[str, tuple[int, dict[str, str]]]):
        self.html = html
        self.statuses = statuses
        self.calls: list[str] = []

    def fetch(self, url: str, *, screenshot: bool = True) -> FetchResult:
        self.calls.append(url)
        key = url.rstrip("/") or url
        status, headers = self.statuses.get(key, (200, {}))
        html = self.html.get(key, "")
        return FetchResult(
            url=url, final_url=url, status=status, content_type="text/html",
            render_mode="browser", html=html, visible_text=html, dom_text=html,
            headers=headers)


class RecordingLimiter(DomainRateLimiter):
    """No-sleep limiter that records backoff calls."""

    def __init__(self):
        super().__init__(0.0, sleep=lambda s: None)
        self.backoffs: list[tuple[str, float]] = []

    def backoff(self, url: str, seconds: float) -> None:
        self.backoffs.append((url, seconds))
        super().backoff(url, seconds)


def _backoff_crawler(fetcher, limiter):
    c = Crawler(fetcher, robots=_no_robots(), rate_limiter=limiter,
                common_paths=[], crawl_depth=1)
    c.sitemap_urls = lambda base: []          # no network during enumeration
    return c


def test_429_exponential_backoff_without_retry_after():
    # No Retry-After header -> 30s on the first strike, doubling on the next.
    links = '<a href="/p1">1</a><a href="/p2">2</a>'
    fetcher = StatusFetcher(
        {"https://example.com": links},
        {"https://example.com/p1": (429, {}),
         "https://example.com/p2": (429, {})})
    lim = RecordingLimiter()
    c = _backoff_crawler(fetcher, lim)
    results = list(c.crawl_site("https://example.com"))
    assert [s for _, s in lim.backoffs] == [30.0, 60.0]
    # The rate-limited results are still yielded so the scans are recorded.
    assert sum(1 for r in results if r.status == 429) == 2


def test_retry_after_header_is_honored():
    links = '<a href="/p1">1</a>'
    fetcher = StatusFetcher(
        {"https://example.com": links},
        {"https://example.com/p1": (429, {"Retry-After": "120"})})
    lim = RecordingLimiter()
    c = _backoff_crawler(fetcher, lim)
    list(c.crawl_site("https://example.com"))
    assert lim.backoffs == [("https://example.com/p1", 120.0)]


def test_503_counts_as_rate_limit_too():
    links = '<a href="/p1">1</a>'
    fetcher = StatusFetcher(
        {"https://example.com": links},
        {"https://example.com/p1": (503, {})})
    lim = RecordingLimiter()
    list(_backoff_crawler(fetcher, lim).crawl_site("https://example.com"))
    assert [s for _, s in lim.backoffs] == [30.0]


def test_success_resets_rate_limit_streak():
    # p1 429 (strike 1), p2 200 (reset), p3 429 (strike 1 again), p4 429
    # (strike 2) -> backoffs 30, 30, 60 and no abandonment.
    links = "".join(f'<a href="/p{i}">x</a>' for i in range(1, 5))
    fetcher = StatusFetcher(
        {"https://example.com": links,
         "https://example.com/p2": "<html><body>fine</body></html>"},
        {"https://example.com/p1": (429, {}),
         "https://example.com/p3": (429, {}),
         "https://example.com/p4": (429, {})})
    lim = RecordingLimiter()
    c = _backoff_crawler(fetcher, lim)
    list(c.crawl_site("https://example.com"))
    assert [s for _, s in lim.backoffs] == [30.0, 30.0, 60.0]
    assert "https://example.com/p4" in fetcher.calls    # host NOT abandoned


def test_three_consecutive_429s_abandon_host():
    # After 3 rate-limit responses in a row, the host's remaining frontier
    # URLs are skipped for the rest of the crawl.
    links = "".join(f'<a href="/p{i}">x</a>' for i in range(1, 7))
    statuses = {f"https://example.com/p{i}": (429, {}) for i in range(1, 7)}
    fetcher = StatusFetcher({"https://example.com": links}, statuses)
    lim = RecordingLimiter()
    c = _backoff_crawler(fetcher, lim)
    results = list(c.crawl_site("https://example.com"))
    assert fetcher.calls == [
        "https://example.com",
        "https://example.com/p1",
        "https://example.com/p2",
        "https://example.com/p3",
    ]                                      # p4..p6 never fetched
    assert sum(1 for r in results if r.status == 429) == 3
    # The abandoning strike triggers no further backoff call (2 backoffs, not 3).
    assert len(lim.backoffs) == 2


def test_abandoned_host_skips_its_pdfs(monkeypatch):
    # PDFs discovered before the host was abandoned must not be fetched after.
    links = ('<a href="/kit.pdf">k</a>'
             + "".join(f'<a href="/p{i}">x</a>' for i in range(1, 4)))
    statuses = {f"https://example.com/p{i}": (429, {}) for i in range(1, 4)}
    fetcher = StatusFetcher({"https://example.com": links}, statuses)
    import redbox.crawler as cm
    pdf_calls: list[str] = []

    def fake_pdf(url, **k):
        pdf_calls.append(url)
        return FetchResult(url=url, final_url=url, status=200,
                           content_type="application/pdf", render_mode="pdf")

    monkeypatch.setattr(cm, "fetch_pdf", fake_pdf)
    lim = RecordingLimiter()
    list(_backoff_crawler(fetcher, lim).crawl_site("https://example.com"))
    assert pdf_calls == []


def test_retry_after_capped_against_hostile_values():
    # A hostile Retry-After must not pin a worker: capped at the backoff cap.
    from redbox.crawler import RATE_LIMIT_BACKOFF_CAP
    links = '<a href="/p1">1</a>'
    fetcher = StatusFetcher(
        {"https://example.com": links},
        {"https://example.com/p1": (429, {"Retry-After": "86400"})})
    lim = RecordingLimiter()
    list(_backoff_crawler(fetcher, lim).crawl_site("https://example.com"))
    assert lim.backoffs == [("https://example.com/p1", RATE_LIMIT_BACKOFF_CAP)]


# --- sitemap fetches respect robots ------------------------------------------

def test_sitemap_doc_disallowed_by_robots_is_not_fetched(monkeypatch):
    # Sitemap DOCUMENTS are ordinary fetches: a robots-disallowed sitemap URL
    # must be skipped silently (robots.txt itself is always fetchable).
    import redbox.crawler as cm

    fetched: list[str] = []

    def fake_get(self, url):
        fetched.append(url)
        return "<urlset><loc>https://x.example/page</loc></urlset>"

    monkeypatch.setattr(cm.Crawler, "_get_text", fake_get)
    _patch_locations(monkeypatch, cm, ["https://x.example/secret-sitemap.xml",
                                       "https://x.example/sitemap.xml"])

    class _RP:  # disallows anything containing 'secret'
        def can_fetch(self, ua, url):
            return "secret" not in url

        def crawl_delay(self, ua):
            return None

    rp = RobotsPolicy(default="respect")
    rp._cache = {"x.example": _RP()}
    rp._parser = lambda scheme, domain: rp._cache.get(domain)  # type: ignore

    c = Crawler(FakeFetcher({}), robots=rp,
                rate_limiter=DomainRateLimiter(0.0, sleep=lambda s: None),
                common_paths=[], crawl_depth=1)
    urls = c.sitemap_urls("https://x.example")
    assert fetched == ["https://x.example/sitemap.xml"]   # disallowed one skipped
    assert urls == ["https://x.example/page"]


def test_sitemap_index_children_respect_robots(monkeypatch):
    import redbox.crawler as cm

    fetched: list[str] = []

    def fake_get(self, url):
        fetched.append(url)
        if "index" in url:
            return ("<sitemapindex>"
                    "<loc>https://x.example/secret-child.xml</loc>"
                    "<loc>https://x.example/child.xml</loc>"
                    "</sitemapindex>")
        return "<urlset><loc>https://x.example/page</loc></urlset>"

    monkeypatch.setattr(cm.Crawler, "_get_text", fake_get)
    _patch_locations(monkeypatch, cm, ["https://x.example/sitemap_index.xml"])

    class _RP:
        def can_fetch(self, ua, url):
            return "secret" not in url

        def crawl_delay(self, ua):
            return None

    rp = RobotsPolicy(default="respect")
    rp._cache = {"x.example": _RP()}
    rp._parser = lambda scheme, domain: rp._cache.get(domain)  # type: ignore

    c = Crawler(FakeFetcher({}), robots=rp,
                rate_limiter=DomainRateLimiter(0.0, sleep=lambda s: None),
                common_paths=[], crawl_depth=1)
    urls = c.sitemap_urls("https://x.example")
    assert "https://x.example/secret-child.xml" not in fetched
    assert "https://x.example/child.xml" in fetched
    assert urls == ["https://x.example/page"]


# --- Playwright teardown guard ----------------------------------------------

def test_playwright_teardown_runs_all_steps_when_context_close_raises():
    # A failing context.close() must not leak the browser or the Playwright
    # driver (2026-07 scale audit): all three teardown steps always run.
    from redbox.crawler import PlaywrightFetcher

    calls: list[str] = []

    class _BoomContext:
        def close(self):
            calls.append("context")
            raise RuntimeError("boom")

    class _Browser:
        def close(self):
            calls.append("browser")

    class _PW:
        def stop(self):
            calls.append("pw")

    f = PlaywrightFetcher()
    f._context, f._browser, f._pw = _BoomContext(), _Browser(), _PW()
    f.__exit__(None, None, None)              # must not raise
    assert calls == ["context", "browser", "pw"]
    assert f._context is None and f._browser is None and f._pw is None


def test_playwright_teardown_survives_browser_close_failure_too():
    from redbox.crawler import PlaywrightFetcher

    calls: list[str] = []

    class _Context:
        def close(self):
            calls.append("context")

    class _BoomBrowser:
        def close(self):
            calls.append("browser")
            raise RuntimeError("boom")

    class _PW:
        def stop(self):
            calls.append("pw")

    f = PlaywrightFetcher()
    f._context, f._browser, f._pw = _Context(), _BoomBrowser(), _PW()
    f.__exit__(None, None, None)
    assert calls == ["context", "browser", "pw"]


def test_sitemap_time_budget_stops_child_expansion(monkeypatch):
    # Fake clock: every look at it advances 50s, so the 120s whole-pass budget
    # expires after the first child fetch — the other 9 are never fetched.
    import redbox.crawler as cm

    fetched: list[str] = []

    def fake_get(self, url):
        fetched.append(url)
        if "index" in url:
            return _index_xml(10)
        return f"<urlset><loc>https://x.example/p-{len(fetched)}</loc></urlset>"

    monkeypatch.setattr(cm.Crawler, "_get_text", fake_get)
    _patch_locations(monkeypatch, cm, ["https://x.example/sitemap_index.xml"])

    t = {"now": 0.0}

    def _clock():
        t["now"] += 50.0
        return t["now"]

    monkeypatch.setattr("time.monotonic", _clock)
    c = _crawler(FakeFetcher({}))
    urls = c.sitemap_urls("https://x.example")
    child_fetches = [u for u in fetched if "/sm-" in u]
    assert 1 <= len(child_fetches) < 10, child_fetches   # stopped early, mid-index
    assert urls                                          # what was collected survives
