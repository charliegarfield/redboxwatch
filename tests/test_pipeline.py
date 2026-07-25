"""End-to-end pipeline integration test (crawler -> classifier -> archiver -> DB).

Fully offline: a fake fetcher serves the labeled fixture pages and a fake LLM
classifies by filename-content, so the whole slice is exercised without any live
calls. Verifies persistence, routing, evidence archiving, and the
unverified-URL safety gate.
"""
from __future__ import annotations

from pathlib import Path

from redbox.archiver import Archiver, WaybackClient
from redbox.classifier import Classifier
from redbox.crawler import Crawler, FetchResult
from redbox.db import init_db
from redbox.pipeline import scan_candidate
from redbox.ratelimit import DomainRateLimiter
from redbox.robots import RobotsPolicy

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "pages"


class FixtureFetcher:
    """Serve a tiny site: home links to /media (positive) and /press (negative)."""

    def __init__(self):
        pos = (FIXTURE_DIR / "positive_plainprose.txt").read_text()
        neg = (FIXTURE_DIR / "negative_presskit.txt").read_text()
        self.pages = {
            "https://example.org": ('<a href="/media">m</a><a href="/press">p</a>', "home"),
            "https://example.org/media": ("", pos),
            "https://example.org/press": ("", neg),
        }

    def fetch(self, url, *, screenshot=True):
        key = url.rstrip("/") or url
        html, text = self.pages.get(key, ("", ""))
        return FetchResult(
            url=url, final_url=url, status=200, content_type="text/html",
            render_mode="browser", html=html, visible_text=text, dom_text=text,
            screenshot_png=b"PNG" if screenshot else None,
        )


class KeywordLLM:
    """Classify by presence of directive keywords — no network."""

    def classify_chunk(self, text, *, model):
        directive = any(k in text for k in ("should see", "need to hear", "in their mailboxes"))
        if directive:
            return {"classification": "red_box_guidance", "confidence": 0.95,
                    "evidence": [{"quote": "should see", "why": "directive"}],
                    "rationale": "directive guidance"}
        return {"classification": "no_guidance_detected", "confidence": 0.95,
                "evidence": [], "rationale": "press kit"}


class StubWayback(WaybackClient):
    def save(self, url):
        return ("https://web.archive.org/web/20260529/" + url, None)


def _crawler():
    rp = RobotsPolicy(default="respect")
    rp._parser = lambda scheme, domain: None  # allow-all, no network
    return Crawler(FixtureFetcher(), robots=rp, rate_limiter=DomainRateLimiter(0.0),
                   common_paths=[], crawl_depth=1)


def test_robots_blocked_site_is_flagged(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    conn.execute("""INSERT INTO candidates (candidate_id,name,website_url,url_verified,
        created_at,updated_at) VALUES ('H1','T','https://blocked.example',1,'t','t')""")
    conn.commit()
    candidate = dict(conn.execute("SELECT * FROM candidates").fetchone())
    # Robots policy that disallows everything.
    rp = RobotsPolicy(default="respect")
    class _Deny:
        def can_fetch(self, ua, url): return False
        def crawl_delay(self, ua): return None
    rp._cache = {"blocked.example": _Deny()}
    rp._parser = lambda scheme, domain: rp._cache.get(domain)
    crawler = Crawler(FixtureFetcher(), robots=rp, rate_limiter=DomainRateLimiter(0.0),
                      common_paths=[], crawl_depth=1)
    out = scan_candidate(conn, candidate, crawler=crawler,
                         classifier=Classifier(KeywordLLM()),
                         archiver=Archiver(tmp_path / "a", push_wayback=False))
    assert out.pages_scanned == 0
    assert out.robots_blocked is True
    # scan_status persisted on the candidate
    st = conn.execute("SELECT scan_status FROM candidates WHERE candidate_id='H1'").fetchone()[0]
    assert st == "robots_blocked"
    conn.close()


def test_fetch_failed_status_when_site_unreachable(tmp_path):
    # robots allows, but the crawl yields zero pages (DNS/timeout/refused). This
    # must be recorded as 'fetch_failed', NOT left NULL — otherwise an unreachable
    # site is silently indistinguishable from "never scanned".
    conn = init_db(tmp_path / "db.sqlite")
    conn.execute("""INSERT INTO candidates (candidate_id,name,website_url,url_verified,
        created_at,updated_at) VALUES ('H1','T','https://down.example',1,'t','t')""")
    conn.commit()
    candidate = dict(conn.execute("SELECT * FROM candidates").fetchone())

    class _ZeroPageCrawler:
        class _Robots:
            def can_fetch(self, url):
                return (True, None)      # not blocked by robots
        robots = _Robots()
        def crawl_site(self, url):
            return iter(())              # nothing fetched

    out = scan_candidate(conn, candidate, crawler=_ZeroPageCrawler(),
                         classifier=Classifier(KeywordLLM()),
                         archiver=Archiver(tmp_path / "a", push_wayback=False))
    assert out.pages_scanned == 0
    assert out.robots_blocked is False
    st = conn.execute("SELECT scan_status FROM candidates WHERE candidate_id='H1'").fetchone()[0]
    assert st == "fetch_failed"
    conn.close()


def test_end_to_end_scan(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    conn.execute(
        """INSERT INTO candidates (candidate_id, name, website_url, url_verified,
               created_at, updated_at)
           VALUES ('H1','TEST','https://example.org',1,'t','t')""")
    conn.commit()
    candidate = dict(conn.execute("SELECT * FROM candidates").fetchone())

    archiver = Archiver(tmp_path / "artifacts", wayback=StubWayback(), push_wayback=True)
    out = scan_candidate(conn, candidate, crawler=_crawler(),
                         classifier=Classifier(KeywordLLM()), archiver=archiver)

    assert out.pages_scanned == 3            # home + /media + /press
    assert out.positives == 1                # /media
    assert out.archived == 1                 # only the positive archived
    assert out.skipped_reason is None

    # DB reflects the run
    assert conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 3
    pos = conn.execute(
        "SELECT * FROM detections WHERE classification='red_box_guidance'").fetchone()
    assert pos is not None
    arch = conn.execute("SELECT * FROM archives").fetchone()
    assert arch["detection_id"] == pos["detection_id"]
    assert arch["wayback_url"].startswith("https://web.archive.org/web/")
    # evidence file written to disk
    assert Path(arch["text_path"]).exists()
    conn.close()


class AliasFetcher:
    """Template-alias site: /media, /media-kit and /press all serve the SAME
    positive body (catch-all route); /about is a distinct negative page."""

    def __init__(self):
        pos = (FIXTURE_DIR / "positive_plainprose.txt").read_text()
        neg = (FIXTURE_DIR / "negative_presskit.txt").read_text()
        links = ('<a href="/media">a</a><a href="/media-kit">b</a>'
                 '<a href="/press">c</a><a href="/about">d</a>')
        self.pages = {
            "https://example.org": (links, "home"),
            "https://example.org/media": ("", pos),
            "https://example.org/media-kit": ("", pos),
            "https://example.org/press": ("", pos),
            "https://example.org/about": ("", neg),
        }

    def fetch(self, url, *, screenshot=True):
        key = url.rstrip("/") or url
        html, text = self.pages.get(key, ("", ""))
        return FetchResult(
            url=url, final_url=url, status=200, content_type="text/html",
            render_mode="browser", html=html, visible_text=text, dom_text=text,
            screenshot_png=b"PNG" if screenshot else None)


def test_template_aliases_classified_once_one_detection(tmp_path):
    # One body under three URLs -> one LLM verdict, one detection, one archive;
    # the aliases are still recorded as scans (audit trail) and counted deduped.
    conn = init_db(tmp_path / "db.sqlite")
    conn.execute(
        """INSERT INTO candidates (candidate_id, name, website_url, url_verified,
               created_at, updated_at)
           VALUES ('H1','TEST','https://example.org',1,'t','t')""")
    conn.commit()
    candidate = dict(conn.execute("SELECT * FROM candidates").fetchone())

    rp = RobotsPolicy(default="respect")
    rp._parser = lambda scheme, domain: None
    crawler = Crawler(AliasFetcher(), robots=rp, rate_limiter=DomainRateLimiter(0.0),
                      common_paths=[], crawl_depth=1)
    llm = CountingLLM()
    archiver = Archiver(tmp_path / "artifacts", wayback=StubWayback(), push_wayback=False)
    out = scan_candidate(conn, candidate, crawler=crawler,
                         classifier=Classifier(llm), archiver=archiver)

    assert out.pages_scanned == 5                      # home + 3 aliases + about
    assert out.deduped == 2                            # media-kit + press reused
    assert out.positives == 1                          # ONE reviewable finding
    assert out.archived == 1
    # LLM saw each distinct body once: home, positive body, negative body
    assert len(llm.calls) == 3
    # aliases still have scan rows for the audit trail
    assert conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 5
    assert conn.execute(
        "SELECT COUNT(*) FROM detections WHERE classification='red_box_guidance'"
    ).fetchone()[0] == 1
    conn.close()


class PrefilterFetcher:
    """Home links to /media (positive) and /donate (boilerplate, no signal)."""

    def __init__(self):
        pos = (FIXTURE_DIR / "positive_plainprose.txt").read_text()
        self.pages = {
            "https://example.org": ('<a href="/media">m</a><a href="/donate">d</a>', "home"),
            "https://example.org/media": ("", pos),
            "https://example.org/donate": ("", "Chip in $25 today to help us. Donate now."),
        }

    def fetch(self, url, *, screenshot=True):
        key = url.rstrip("/") or url
        html, text = self.pages.get(key, ("", ""))
        return FetchResult(
            url=url, final_url=url, status=200, content_type="text/html",
            render_mode="browser", html=html, visible_text=text, dom_text=text,
            screenshot_png=b"PNG" if screenshot else None)


class CountingLLM(KeywordLLM):
    def __init__(self):
        self.calls = []

    def classify_chunk(self, text, *, model):
        self.calls.append(text)
        return super().classify_chunk(text, model=model)


def test_prefilter_skips_donate_page_but_scans_media(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    conn.execute("""INSERT INTO candidates (candidate_id,name,website_url,url_verified,
        created_at,updated_at) VALUES ('H1','T','https://example.org',1,'t','t')""")
    conn.commit()
    candidate = dict(conn.execute("SELECT * FROM candidates").fetchone())
    llm = CountingLLM()
    rp = RobotsPolicy(default="respect")
    rp._parser = lambda scheme, domain: None
    crawler = Crawler(PrefilterFetcher(), robots=rp, rate_limiter=DomainRateLimiter(0.0),
                      common_paths=[], crawl_depth=1)
    out = scan_candidate(conn, candidate, crawler=crawler,
                         classifier=Classifier(llm),
                         archiver=Archiver(tmp_path / "a", wayback=StubWayback(), push_wayback=False))
    # /donate was skipped by the pre-filter; home + /media classified.
    assert out.prefiltered == 1
    assert out.positives == 1                       # /media still caught
    # The LLM never saw the donate text.
    assert not any("Chip in" in t for t in llm.calls)
    # But /donate still got a scan row (recorded for diffing), just no detection.
    donate = conn.execute(
        "SELECT scan_id FROM scans WHERE url LIKE '%/donate'").fetchone()
    assert donate is not None
    assert conn.execute(
        "SELECT COUNT(*) FROM detections WHERE scan_id=?", (donate["scan_id"],)).fetchone()[0] == 0
    conn.close()


class RaisingLLM:
    """Raises on the /media page (simulating a transient API failure), classifies
    everything else as negative."""

    def classify_chunk(self, text, *, model):
        if "should see" in text:   # only the /media positive fixture has this
            raise RuntimeError("simulated APIConnectionError")
        return {"classification": "no_guidance_detected", "confidence": 0.95,
                "evidence": [], "rationale": "ok"}


def test_classification_failure_skips_page_not_candidate(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    conn.execute("""INSERT INTO candidates (candidate_id,name,website_url,url_verified,
        created_at,updated_at) VALUES ('H1','T','https://example.org',1,'t','t')""")
    conn.commit()
    candidate = dict(conn.execute("SELECT * FROM candidates").fetchone())
    out = scan_candidate(conn, candidate, crawler=_crawler(),
                         classifier=Classifier(RaisingLLM()),
                         archiver=Archiver(tmp_path / "a", push_wayback=False))
    # The failing /media page is dropped; home + /press still scanned; no crash.
    assert out.skipped_reason is None
    assert out.pages_scanned == 2                     # 3 fetched, 1 dropped on failure
    # The dropped page left no scan row (so it's retried next time, not hash-skipped).
    urls = {r["url"] for r in conn.execute("SELECT url FROM scans").fetchall()}
    assert "https://example.org/media" not in urls
    conn.close()


def test_unverified_url_is_scanned_by_default(tmp_path):
    # Verification is a review-time signal, not a pre-scan blocker (default).
    conn = init_db(tmp_path / "db.sqlite")
    conn.execute(
        """INSERT INTO candidates (candidate_id, name, website_url, url_verified,
               created_at, updated_at)
           VALUES ('H2','UNVER','https://example.org',0,'t','t')""")
    conn.commit()
    candidate = dict(conn.execute("SELECT * FROM candidates").fetchone())
    out = scan_candidate(conn, candidate, crawler=_crawler(),
                         classifier=Classifier(KeywordLLM()),
                         archiver=Archiver(tmp_path / "a", wayback=StubWayback(), push_wayback=False))
    assert out.skipped_reason is None
    assert out.pages_scanned == 3            # unverified no longer blocks
    conn.close()


def test_missing_url_is_skipped(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    conn.execute(
        """INSERT INTO candidates (candidate_id, name, website_url, url_verified,
               created_at, updated_at)
           VALUES ('H3','NOURL',NULL,0,'t','t')""")
    conn.commit()
    candidate = dict(conn.execute("SELECT * FROM candidates").fetchone())
    out = scan_candidate(conn, candidate, crawler=_crawler(),
                         classifier=Classifier(KeywordLLM()),
                         archiver=Archiver(tmp_path / "a", push_wayback=False))
    assert out.pages_scanned == 0
    assert "no resolved website_url" in out.skipped_reason
    conn.close()


def test_strict_mode_still_blocks_unverified(tmp_path):
    # require_verified=True (config require_verified_url) restores the §3.1 gate.
    conn = init_db(tmp_path / "db.sqlite")
    conn.execute(
        """INSERT INTO candidates (candidate_id, name, website_url, url_verified,
               created_at, updated_at)
           VALUES ('H4','GUESS','https://guessed.example',0,'t','t')""")
    conn.commit()
    candidate = dict(conn.execute("SELECT * FROM candidates").fetchone())
    out = scan_candidate(conn, candidate, crawler=_crawler(),
                         classifier=Classifier(KeywordLLM()),
                         archiver=Archiver(tmp_path / "a", push_wayback=False),
                         require_verified=True)
    assert out.pages_scanned == 0
    assert "url_verified is false" in out.skipped_reason
    assert conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 0
    conn.close()


class MutableFetcher:
    """A one-page site whose /media content can change between scans."""

    def __init__(self):
        self.media_text = "Younger voters should see ads on the go."  # positive

    def fetch(self, url, *, screenshot=True):
        key = url.rstrip("/") or url
        if key == "https://example.org":
            html, text = '<a href="/media">m</a>', "home"
        elif key == "https://example.org/media":
            html, text = "", self.media_text
        else:
            return FetchResult(url=url, final_url=url, status=404, content_type="text/html",
                               render_mode="browser", html="", visible_text="", dom_text="")
        return FetchResult(url=url, final_url=url, status=200, content_type="text/html",
                           render_mode="browser", html=html, visible_text=text, dom_text=text,
                           screenshot_png=b"PNG")


def _crawler_with(fetcher):
    rp = RobotsPolicy(default="respect")
    rp._parser = lambda scheme, domain: None
    return Crawler(fetcher, robots=rp, rate_limiter=DomainRateLimiter(0.0),
                   common_paths=[], crawl_depth=1)


def test_take_down_is_detected_on_rescan(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    conn.execute("""INSERT INTO candidates (candidate_id,name,website_url,url_verified,
        created_at,updated_at) VALUES ('H1','T','https://example.org',1,'t','t')""")
    conn.commit()
    candidate = dict(conn.execute("SELECT * FROM candidates").fetchone())
    fetcher = MutableFetcher()
    archiver = Archiver(tmp_path / "a", wayback=StubWayback(), push_wayback=False)
    kw = Classifier(KeywordLLM())

    # Scan 1: /media has the red box -> positive.
    o1 = scan_candidate(conn, candidate, crawler=_crawler_with(fetcher), classifier=kw, archiver=archiver)
    assert o1.positives == 1 and o1.changes == 0

    # The box comes down: /media now an innocuous press kit.
    fetcher.media_text = "Press kit: logos, headshots, bios, and media contact info."
    o2 = scan_candidate(conn, candidate, crawler=_crawler_with(fetcher), classifier=kw, archiver=archiver)
    assert o2.take_downs == 1 and o2.changes == 1
    ev = conn.execute("SELECT * FROM change_events WHERE event_type='take_down'").fetchone()
    assert ev["url"] == "https://example.org/media"
    assert ev["prev_classification"] == "red_box_guidance"
    assert ev["new_classification"] == "no_guidance_detected"
    conn.close()


def test_put_up_is_detected_on_rescan(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    conn.execute("""INSERT INTO candidates (candidate_id,name,website_url,url_verified,
        created_at,updated_at) VALUES ('H1','T','https://example.org',1,'t','t')""")
    conn.commit()
    candidate = dict(conn.execute("SELECT * FROM candidates").fetchone())
    fetcher = MutableFetcher()
    fetcher.media_text = "Press kit: logos and headshots."   # starts clean
    archiver = Archiver(tmp_path / "a", wayback=StubWayback(), push_wayback=False)
    kw = Classifier(KeywordLLM())

    scan_candidate(conn, candidate, crawler=_crawler_with(fetcher), classifier=kw, archiver=archiver)
    fetcher.media_text = "Women likely to vote should see this in their mailboxes."  # box goes up
    o2 = scan_candidate(conn, candidate, crawler=_crawler_with(fetcher), classifier=kw, archiver=archiver)
    assert o2.put_ups == 1
    ev = conn.execute("SELECT * FROM change_events WHERE event_type='put_up'").fetchone()
    assert ev["new_classification"] == "red_box_guidance"
    conn.close()


def test_unchanged_hash_skips_reclassification(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    conn.execute(
        """INSERT INTO candidates (candidate_id, name, website_url, url_verified,
               created_at, updated_at)
           VALUES ('H1','TEST','https://example.org',1,'t','t')""")
    conn.commit()
    candidate = dict(conn.execute("SELECT * FROM candidates").fetchone())
    archiver = Archiver(tmp_path / "artifacts", wayback=StubWayback(), push_wayback=True)
    kw = Classifier(KeywordLLM())

    first = scan_candidate(conn, candidate, crawler=_crawler(), classifier=kw, archiver=archiver)
    second = scan_candidate(conn, candidate, crawler=_crawler(), classifier=kw, archiver=archiver)

    assert first.detections == 3
    assert second.detections == 0            # all hashes unchanged -> no re-classify
    assert second.pages_scanned == 3         # but re-scans still recorded (diff history)
    assert conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 6
    conn.close()
