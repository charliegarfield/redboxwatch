"""Crawler — enumerate and extract candidate-page content (spec §3.3).

Responsibilities:
- enumerate candidate pages: sitemap.xml, common paths, depth<=2 same-domain
  link crawl, and linked PDFs;
- render with a headless browser (Playwright/Chromium): visible text AND DOM
  text, because content can sit in hidden/unlinked divs;
- extract text from discovered PDFs;
- content-hash extracted text to skip re-classification when unchanged;
- be a compliant crawler: honest UA, per-domain rate limit, configurable robots.

The fetching backend is abstracted behind :class:`Fetcher` so the enumeration /
orchestration logic is unit-testable without a live browser.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Iterator, Protocol
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from . import prefilter
from .pdf import extract_pdf_text
from .ratelimit import DomainRateLimiter
from .robots import RobotsPolicy
from .util import now_iso, sha256_text


@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    content_type: str
    render_mode: str                 # browser | http | pdf
    html: str = ""                   # rendered DOM HTML (html pages)
    visible_text: str = ""           # innerText (what a visitor sees)
    dom_text: str = ""               # all text incl. hidden/unlinked nodes
    screenshot_png: bytes | None = None
    pdf_bytes: bytes | None = None   # raw document (pdf pages) for archiving
    discovered_via: str = ""
    # 'respect' | 'override' — how robots.txt was applied to this fetch.
    # Persisted on the scans row so collection of any page is auditable
    # (ROBOTS_POLICY.md).
    robots_posture: str | None = None
    fetched_at: str = field(default_factory=now_iso)

    @property
    def classifier_text(self) -> str:
        """Text handed to the classifier: union of visible + hidden DOM text.

        Hidden/unlinked content is part of the detection target, so we include
        DOM text even when it isn't visibly rendered. Visible text comes first
        (it's what a human sees); any DOM-only remainder is appended.
        """
        vis = (self.visible_text or "").strip()
        dom = (self.dom_text or "").strip()
        if dom and dom not in vis:
            extra = dom if not vis else _dom_only_remainder(vis, dom)
            return (vis + "\n\n[hidden/DOM-only content]\n" + extra).strip() if extra else vis
        return vis or dom

    @property
    def text_hash(self) -> str:
        return sha256_text(self.classifier_text)


def _dom_only_remainder(visible: str, dom: str) -> str:
    """Lines present in DOM text but not in the visible text."""
    vis_lines = {ln.strip() for ln in visible.splitlines() if ln.strip()}
    extra = [ln for ln in dom.splitlines() if ln.strip() and ln.strip() not in vis_lines]
    return "\n".join(extra)


class Fetcher(Protocol):
    def fetch(self, url: str, *, screenshot: bool = True) -> FetchResult: ...


# Collect text from every text node, skipping code/markup elements whose text is
# never page content. Walks the DOM with a TreeWalker (incl. hidden nodes), so a
# red box in a display:none div is still captured — but <script>/<style> noise is
# not. Returns newline-joined non-empty text chunks.
_DOM_TEXT_JS = r"""() => {
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','SVG','CANVAS']);
  const out = [];
  const walker = document.createTreeWalker(
    document.body || document.documentElement, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        let p = node.parentElement;
        while (p) {
          if (SKIP.has(p.tagName)) return NodeFilter.FILTER_REJECT;
          p = p.parentElement;
        }
        const t = node.nodeValue && node.nodeValue.trim();
        return t ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
      }
    });
  let n;
  while ((n = walker.nextNode())) out.push(n.nodeValue.trim());
  return out.join('\n');
}"""


# ---------------------------------------------------------------------------
class PlaywrightFetcher:
    """Headless-Chromium fetcher. Use as a context manager."""

    def __init__(self, user_agent: str = "RedBoxTracker/0.1", timeout_ms: int = 30000,
                 settle_ms: int = 1500):
        self.user_agent = user_agent
        self.timeout_ms = timeout_ms
        # Bounded JS-hydration settle after DOMContentLoaded. Server-rendered
        # sites already have their text; this only matters for JS/SPA pages.
        self.settle_ms = settle_ms
        self._pw = None
        self._browser = None
        self._context = None

    def __enter__(self) -> "PlaywrightFetcher":
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._context = self._browser.new_context(user_agent=self.user_agent)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._context:
            self._context.close()
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    def fetch(self, url: str, *, screenshot: bool = True) -> FetchResult:
        page = self._context.new_page()
        try:
            # Wait only for DOMContentLoaded, NOT 'load': the text we classify is
            # in the DOM at DOMContentLoaded (~1.5s), whereas 'load' blocks on
            # every image/font/ad/tracker resource (~8-15s on campaign sites) we
            # don't use. Then a short, bounded settle lets JS/SPA pages hydrate.
            # ('networkidle' is unusable here — tracker beacons mean the network
            # never goes idle, so it always burns the full timeout.)
            resp = page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
            status = resp.status if resp else 0
            ctype = (resp.headers.get("content-type", "") if resp else "")
            # Error page: no settle wait, extraction, or screenshot — the pipeline
            # treats status>=400 as no-content regardless of what the error page
            # says, and ~50% of fetches on a real run are 404s (news-template
            # sites emit phantom same-domain article links), so skipping the
            # render tail here saves several seconds on each of them.
            if not status or status >= 400:
                return FetchResult(url=url, final_url=page.url, status=status,
                                   content_type=ctype, render_mode="browser")
            if self.settle_ms:
                page.wait_for_timeout(self.settle_ms)
            html = page.content()
            visible = page.evaluate("() => document.body ? document.body.innerText : ''")
            # Hidden/unlinked DOM text is part of the detection target (red boxes
            # can sit in hidden divs), but document.body.textContent also slurps
            # <script>/<style> content — embedded JS config, CSS-in-JS, framework
            # state — which is never a red box and can be 100x the real text. We
            # walk text nodes, skipping SCRIPT/STYLE/NOSCRIPT/TEMPLATE, to keep
            # hidden *content* while dropping code/markup noise.
            dom = page.evaluate(_DOM_TEXT_JS)
            shot = page.screenshot(full_page=True) if screenshot else None
            return FetchResult(
                url=url, final_url=page.url, status=status, content_type=ctype,
                render_mode="browser", html=html, visible_text=visible or "",
                dom_text=dom or "", screenshot_png=shot,
            )
        finally:
            page.close()


# ---------------------------------------------------------------------------
def fetch_pdf(url: str, *, user_agent: str, timeout: float = 30.0,
              max_total_seconds: float = 120.0,
              max_bytes: int = 50_000_000) -> FetchResult:
    """Fetch a PDF over HTTP and extract its text (no browser render).

    Streamed with a TOTAL wall-clock deadline: httpx's read timeout resets on
    every chunk, so a drip-feeding server (one byte every few seconds) can hold
    a worker thread forever without ever tripping it — the documented scan-all
    hang signature. ``max_bytes`` caps runaway bodies for the same reason.
    """
    import time as _time

    deadline = _time.monotonic() + max_total_seconds
    chunks: list[bytes] = []
    size = 0
    with httpx.stream("GET", url, headers={"User-Agent": user_agent},
                      follow_redirects=True, timeout=timeout) as resp:
        status, final_url = resp.status_code, str(resp.url)
        content_type = resp.headers.get("content-type", "application/pdf")
        ok = status < 400
        if ok:
            for chunk in resp.iter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if _time.monotonic() > deadline:
                    raise httpx.ReadTimeout(
                        f"drip-feed guard: {url} still streaming after "
                        f"{max_total_seconds:.0f}s ({size} bytes)")
                if size > max_bytes:
                    raise httpx.ReadTimeout(
                        f"size guard: {url} exceeded {max_bytes} bytes")
    content = b"".join(chunks)
    text = extract_pdf_text(content) if ok else ""
    return FetchResult(
        url=url, final_url=final_url, status=status,
        content_type=content_type,
        render_mode="pdf", visible_text=text, dom_text=text,
        pdf_bytes=content if ok else None,
    )


# ---------------------------------------------------------------------------
class Crawler:
    def __init__(
        self,
        fetcher: Fetcher,
        *,
        robots: RobotsPolicy,
        rate_limiter: DomainRateLimiter,
        common_paths: Iterable[str],
        crawl_depth: int = 2,
        user_agent: str = "RedBoxTracker/0.1",
        max_pages: int = 150,
    ) -> None:
        self.fetcher = fetcher
        self.robots = robots
        self.rate = rate_limiter
        self.common_paths = list(common_paths)
        self.crawl_depth = crawl_depth
        self.user_agent = user_agent
        # Per-class fetch budget, so one sprawling site (blog archive, calendar,
        # pagination) can't dominate the crawl. Ordinary pages, high-value pages
        # (media-kit-style), and PDFs each get their OWN budget of ``max_pages``
        # fetches — high-value pages are prioritized but no longer unbounded
        # (a blog living under /news/ used to bypass the cap entirely), so a
        # site can never cost more than 3x max_pages fetches in total.
        self.max_pages = max_pages

    # Paths fetched ahead of (and budgeted separately from) ordinary pages —
    # red boxes live here. Single source shared with the prefilter's
    # always-classify list (prefilter.HIGH_VALUE_PATHS).
    _HIGH_VALUE = prefilter.HIGH_VALUE_PATHS

    def _is_high_value(self, url: str) -> bool:
        u = url.lower()
        return u.endswith(".pdf") or any(h in u for h in self._HIGH_VALUE)

    # --- enumeration -------------------------------------------------
    @staticmethod
    def _canonical(url: str) -> str:
        """Dedup key for a URL: lowercase host, drop a leading 'www.', strip the
        fragment and trailing slash. So http(s)://host and www.host, and /x and
        /x/, collapse to one — preventing the same page being scanned (and
        flagged) twice under cosmetically different URLs."""
        p = urlparse(url.split("#")[0])
        host = p.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        path = p.path.rstrip("/") or "/"
        return f"{host}{path}" + (f"?{p.query}" if p.query else "")

    @staticmethod
    def _same_domain(a: str, b: str) -> bool:
        na, nb = urlparse(a).netloc, urlparse(b).netloc
        return na.replace("www.", "") == nb.replace("www.", "")

    def _sitemap_locations(self, base_url: str) -> list[str]:
        """Candidate sitemap URLs: robots.txt Sitemap: directives + common names."""
        locs: list[str] = []
        try:
            # Throttled like every other request to the host — enumeration
            # used to fire 1 + N sitemap fetches with no delay at all.
            self.rate.wait(base_url)
            r = httpx.get(urljoin(base_url, "/robots.txt"),
                          headers={"User-Agent": self.user_agent},
                          follow_redirects=True, timeout=15.0)
            if r.status_code < 400:
                for line in r.text.splitlines():
                    if line.lower().startswith("sitemap:"):
                        locs.append(line.split(":", 1)[1].strip())
        except httpx.HTTPError:
            pass
        for name in ("/sitemap.xml", "/sitemap_index.xml"):
            locs.append(urljoin(base_url, name))
        # de-dupe preserving order
        seen: set[str] = set()
        return [u for u in locs if not (u in seen or seen.add(u))]

    @staticmethod
    def _extract_locs(xml_text: str) -> tuple[list[str], bool]:
        """Regex-extract <loc> values and whether this is a sitemap index.

        Real-world sitemaps (Yoast etc.) are often not strictly well-formed for
        Python's XML parser — image namespaces, stray tokens. A <loc> regex is
        bulletproof and avoids dropping an entire sitemap over one bad byte.
        """
        locs = [m.strip() for m in re.findall(r"<loc>\s*(.*?)\s*</loc>",
                                              xml_text, re.IGNORECASE | re.DOTALL)]
        is_index = "<sitemapindex" in xml_text.lower()
        return locs, is_index

    def _get_text(self, url: str) -> str | None:
        try:
            self.rate.wait(url)
            r = httpx.get(url, headers={"User-Agent": self.user_agent},
                          follow_redirects=True, timeout=20.0)
            return r.text if r.status_code < 400 and r.text.strip() else None
        except httpx.HTTPError:
            return None

    def sitemap_urls(self, base_url: str) -> list[str]:
        """Collect page URLs from any sitemap (incl. one level of sitemap-index).

        Tries robots.txt-declared sitemaps plus /sitemap.xml and
        /sitemap_index.xml (WordPress/Yoast). Sitemap-index files are expanded
        one level into their child sitemaps. Parsing is regex-based for
        robustness against slightly-malformed real-world sitemaps.
        """
        out: list[str] = []
        for sm in self._sitemap_locations(base_url):
            text = self._get_text(sm)
            if not text:
                continue
            locs, is_index = self._extract_locs(text)
            if is_index:
                for child in locs:
                    child_text = self._get_text(child)
                    if not child_text:
                        continue
                    child_locs, _ = self._extract_locs(child_text)
                    out.extend(child_locs)
            else:
                out.extend(locs)
        # de-dupe preserving order
        seen: set[str] = set()
        return [u for u in out if u and not (u in seen or seen.add(u))]

    def enumerate_urls(self, base_url: str) -> list[tuple[str, str]]:
        """Return [(url, discovered_via)] for a candidate site, deduped.

        Keyed by canonical form so www/non-www and slash variants collapse to a
        single entry (the sitemap often lists the www form of a page also reached
        as a common path)."""
        seen: dict[str, tuple[str, str]] = {}

        def add(u: str, via: str) -> None:
            u = u.split("#")[0].rstrip("/") or u
            key = self._canonical(u)
            if u and key not in seen and self._same_domain(base_url, u):
                seen[key] = (u, via)

        add(base_url, "seed")
        for path in self.common_paths:
            add(urljoin(base_url, path), "common_path")
        for u in self.sitemap_urls(base_url):
            add(u, "sitemap")
        return list(seen.values())

    # Link hrefs that are never classifiable pages — images, media, styles,
    # archives, feeds. Fetching one costs a full browser cycle for zero text.
    _ASSET_EXT = (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
                  ".css", ".js", ".mjs", ".mp3", ".mp4", ".mov", ".avi", ".webm",
                  ".zip", ".gz", ".tar", ".dmg", ".exe", ".ics", ".xml", ".rss",
                  ".json", ".woff", ".woff2", ".ttf", ".eot")

    def _extract_links(self, page_url: str, html: str, *,
                       site_url: str | None = None) -> tuple[list[str], list[str]]:
        """Return (same-domain page links, same-domain PDF links) from rendered HTML.

        Relative hrefs resolve against ``page_url`` — the page they appear on
        (honoring a ``<base href>`` tag) — NOT the site seed: a bare
        ``href="article-1"`` on ``/news/2026/`` means ``/news/2026/article-1``,
        and resolving it against the seed produced site-root 404s that burned
        page budget while the real pages were never crawled.

        Both pages AND PDFs must be on ``site_url``'s domain (defaults to
        ``page_url``): a campaign site often links to off-domain documents (a
        Senator's .gov letter, a House committee PDF), and fetching those
        would misattribute someone else's content to this candidate. Asset
        links (images/media/feeds) are dropped — never page content.
        """
        site_url = site_url or page_url
        soup = BeautifulSoup(html, "lxml")
        base_tag = soup.find("base", href=True)
        resolve_base = urljoin(page_url, base_tag["href"]) if base_tag else page_url
        pages, pdfs = [], []
        for a in soup.find_all("a", href=True):
            href = urljoin(resolve_base, a["href"]).split("#")[0]
            if not self._same_domain(site_url, href):
                continue
            path = urlparse(href).path.lower()
            if path.endswith(".pdf"):
                pdfs.append(href)
            elif path.endswith(self._ASSET_EXT):
                continue
            else:
                # Keep the site's own form (trailing slash included): dedup is
                # _canonical()'s job, and stripping the slash here broke
                # relative resolution on the fetched page ('article-1' on
                # /news/2026 resolves to /news/article-1).
                pages.append(href)
        return pages, pdfs

    # --- orchestration ----------------------------------------------
    def crawl_site(self, base_url: str) -> Iterator[FetchResult]:
        """Enumerate + link-crawl (depth<=N) + fetch pages and linked PDFs.

        Bounded by ``max_pages`` PER CLASS: ordinary pages, high-value pages
        (media-kit-style), and PDFs each get their own budget of ``max_pages``
        fetches (total <= 3x). High-value pages are fetched first so they never
        lose their budget to a giant blog, and a giant blog under a high-value
        prefix (/news/...) can no longer make the crawl unbounded — the worst
        case per site is a hard 3x ``max_pages`` fetches.

        Streams: yields one FetchResult per page/PDF as it is fetched instead of
        building the whole list. This matters at scale — each FetchResult can
        carry a full-page screenshot (megabytes), so a 150-page site processed
        one page at a time peaks at ~one page of memory per worker rather than
        ~150 (x ``--workers``). ``scan_candidate`` fully processes and releases
        each page before the next is fetched. Ordering is unchanged: pages first
        (high-value first, bounded by ``max_pages``), then discovered PDFs.
        Callers that need a list should wrap in ``list(...)``.
        """
        visited: set[str] = set()
        pdf_links: set[str] = set()
        ordinary_fetched = high_fetched = 0

        # frontier: (url, via, depth). High-value URLs sort first so they're
        # fetched before the cap is reached.
        initial = self.enumerate_urls(base_url)
        initial.sort(key=lambda uv: 0 if self._is_high_value(uv[0]) else 1)
        frontier = [(u, via, 0) for u, via in initial]
        while frontier:
            # Both page budgets spent: stop crawling (PDFs are fetched below).
            if ordinary_fetched >= self.max_pages and high_fetched >= self.max_pages:
                break
            url, via, depth = frontier.pop(0)
            norm = self._canonical(url)
            if norm in visited:
                continue
            high = self._is_high_value(url)
            # Each class draws on its own budget of max_pages fetches.
            if (high_fetched if high else ordinary_fetched) >= self.max_pages:
                continue
            visited.add(norm)

            allowed, posture = self.robots.can_fetch(url)
            if not allowed:
                continue
            self.rate.wait(url, self.robots.crawl_delay(url))
            try:
                res = self.fetcher.fetch(url)
            except Exception:
                continue
            res.discovered_via = via
            res.robots_posture = posture
            if high:
                high_fetched += 1
            else:
                ordinary_fetched += 1

            # Extract links and enqueue BEFORE yielding: once yielded, the caller
            # processes (and we want to release) this page, so we read res.html
            # while we still have it.
            if res.html and depth < self.crawl_depth:
                # Resolve against the fetched page (post-redirect), keep the
                # same-domain test anchored to the site seed.
                pages, pdfs = self._extract_links(res.final_url or url, res.html,
                                                  site_url=base_url)
                pdf_links.update(pdfs)
                # Enqueue high-value links first so they survive the cap.
                for p in sorted(pages, key=lambda x: 0 if self._is_high_value(x) else 1):
                    if self._canonical(p) not in visited:
                        frontier.append((p, "link_crawl", depth + 1))
            yield res

        # fetch discovered PDFs (text only) — their own budget of max_pages, so
        # a PDF-heavy site is bounded too and a bloated page crawl can't starve
        # PDF fetches (each class draws on a separate budget).
        pdfs_fetched = 0
        for pdf in sorted(pdf_links):
            if pdfs_fetched >= self.max_pages:
                break
            if self._canonical(pdf) in visited:
                continue
            visited.add(self._canonical(pdf))
            allowed, pdf_posture = self.robots.can_fetch(pdf)
            if not allowed:
                continue
            # Same politeness as page fetches: honor the host's Crawl-delay.
            self.rate.wait(pdf, self.robots.crawl_delay(pdf))
            try:
                res = fetch_pdf(pdf, user_agent=self.user_agent)
            except Exception:
                continue
            res.discovered_via = "pdf_link"
            res.robots_posture = pdf_posture
            pdfs_fetched += 1
            yield res
