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


def test_take_down_recorded_after_unchanged_interim_scan(tmp_path):
    # scan 1: red box detected; scan 2: page unchanged (hash-skip, so no
    # detection row of its own); scan 3: box removed. The diff must resolve
    # scan 2's classification through its text hash and still record the
    # take-down — a scan_id-only lookup read the quiet re-scan as no-guidance
    # and dropped the event entirely.
    conn = init_db(tmp_path / "db.sqlite")
    conn.execute("""INSERT INTO candidates (candidate_id,name,website_url,url_verified,
        created_at,updated_at) VALUES ('H1','T','https://example.org',1,'t','t')""")
    conn.commit()
    candidate = dict(conn.execute("SELECT * FROM candidates").fetchone())
    fetcher = MutableFetcher()
    archiver = Archiver(tmp_path / "a", wayback=StubWayback(), push_wayback=False)
    kw = Classifier(KeywordLLM())

    o1 = scan_candidate(conn, candidate, crawler=_crawler_with(fetcher), classifier=kw, archiver=archiver)
    assert o1.positives == 1
    o2 = scan_candidate(conn, candidate, crawler=_crawler_with(fetcher), classifier=kw, archiver=archiver)
    assert o2.detections == 0 and o2.changes == 0    # unchanged interim scan

    fetcher.media_text = "Press kit: logos, headshots, bios, and media contact info."
    o3 = scan_candidate(conn, candidate, crawler=_crawler_with(fetcher), classifier=kw, archiver=archiver)
    assert o3.take_downs == 1 and o3.changes == 1
    ev = conn.execute("SELECT * FROM change_events WHERE event_type='take_down'").fetchone()
    assert ev["url"] == "https://example.org/media"
    assert ev["prev_classification"] == "red_box_guidance"
    assert ev["new_classification"] == "no_guidance_detected"
    conn.close()


def test_backfill_reconstructs_suppressed_take_down(tmp_path):
    # A DB carrying the pre-fix damage: positive scan (with detection), quiet
    # unchanged re-scan (no detection row), then the box gone — and NO change
    # event recorded. The backfill must replay history and reconstruct exactly
    # the take-down, dated from the revealing scan; a second run inserts nothing.
    from redbox.pipeline import backfill_change_events

    conn = init_db(tmp_path / "db.sqlite")
    conn.execute("""INSERT INTO candidates (candidate_id,name,website_url,url_verified,
        created_at,updated_at) VALUES ('H1','T','https://example.org',1,'t','t')""")

    def scan(url, hash_, text, when, status=200):
        return conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,
            http_status,raw_text,text_hash) VALUES ('H1',?,?,?,?,?)""",
            (url, when, status, text, hash_)).lastrowid

    s1 = scan("https://example.org/media", "boxhash", "younger voters should see", "2026-07-01T00:00:00+00:00")
    conn.execute("""INSERT INTO detections (scan_id,candidate_id,classification,
        confidence,evidence,rationale,model,classified_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (s1, "H1", "red_box_guidance", 0.9, "[]", "r", "m", "2026-07-01T00:00:00+00:00"))
    scan("https://example.org/media", "boxhash", "younger voters should see", "2026-07-08T00:00:00+00:00")
    scan("https://example.org/media", "cleanhash", "press kit and logos", "2026-07-15T00:00:00+00:00")
    conn.commit()

    dry = backfill_change_events(conn, apply=False)["missing"]
    assert [(e["event_type"], e["url"]) for e in dry] == [
        ("take_down", "https://example.org/media")]
    assert dry[0]["prev_classification"] == "red_box_guidance"
    assert dry[0]["detected_at"].startswith("2026-07-15")
    assert conn.execute("SELECT COUNT(*) FROM change_events").fetchone()[0] == 0  # dry run

    backfill_change_events(conn, apply=True)
    assert conn.execute("SELECT COUNT(*) FROM change_events").fetchone()[0] == 1
    again = backfill_change_events(conn, apply=True)            # idempotent
    assert again["missing"] == [] and again["spurious"] == []
    conn.close()


def test_backfill_does_not_duplicate_pipeline_events(tmp_path):
    # History where the live (fixed) pipeline already recorded the events:
    # backfill must find nothing to add.
    from redbox.pipeline import backfill_change_events

    conn = init_db(tmp_path / "db.sqlite")
    conn.execute("""INSERT INTO candidates (candidate_id,name,website_url,url_verified,
        created_at,updated_at) VALUES ('H1','T','https://example.org',1,'t','t')""")
    conn.commit()
    candidate = dict(conn.execute("SELECT * FROM candidates").fetchone())
    fetcher = MutableFetcher()
    archiver = Archiver(tmp_path / "a", wayback=StubWayback(), push_wayback=False)
    kw = Classifier(KeywordLLM())
    scan_candidate(conn, candidate, crawler=_crawler_with(fetcher), classifier=kw, archiver=archiver)
    fetcher.media_text = "Press kit: logos, headshots, bios, and media contact info."
    scan_candidate(conn, candidate, crawler=_crawler_with(fetcher), classifier=kw, archiver=archiver)
    assert conn.execute("SELECT COUNT(*) FROM change_events").fetchone()[0] == 1
    result = backfill_change_events(conn, apply=True)
    assert result["missing"] == [] and result["spurious"] == []
    conn.close()


# --- Transient fetch failures must not manufacture change events. ------------
# A 403 bot-block, an HTTP 500, or a bot-challenge shell says nothing about
# the page's content; only a usable scan (or a confirmed disappearance —
# two consecutive 404s) may move a URL's recorded state.

class ErrorableFetcher:
    """A one-page site whose /media response (status, text) is scriptable."""

    def __init__(self):
        self.media = (200, "Younger voters should see ads on the go.")  # positive

    def fetch(self, url, *, screenshot=True):
        key = url.rstrip("/") or url
        if key == "https://example.org":
            return FetchResult(url=url, final_url=url, status=200,
                               content_type="text/html", render_mode="browser",
                               html='<a href="/media">m</a>', visible_text="home",
                               dom_text="home", screenshot_png=b"PNG")
        if key == "https://example.org/media":
            status, text = self.media
            return FetchResult(url=url, final_url=url, status=status,
                               content_type="text/html", render_mode="browser",
                               html="" if status >= 400 else "<p>x</p>",
                               visible_text=text, dom_text=text,
                               screenshot_png=b"PNG" if status < 400 else None)
        return FetchResult(url=url, final_url=url, status=404, content_type="text/html",
                           render_mode="browser", html="", visible_text="", dom_text="")


def _seed_candidate(conn):
    conn.execute("""INSERT INTO candidates (candidate_id,name,website_url,url_verified,
        created_at,updated_at) VALUES ('H1','T','https://example.org',1,'t','t')""")
    conn.commit()
    return dict(conn.execute("SELECT * FROM candidates").fetchone())


def _scan(conn, candidate, fetcher, tmp_path):
    return scan_candidate(
        conn, candidate, crawler=_crawler_with(fetcher),
        classifier=Classifier(KeywordLLM()),
        archiver=Archiver(tmp_path / "a", wayback=StubWayback(), push_wayback=False))


def test_bot_block_after_positive_is_not_a_take_down(tmp_path):
    # The exact shape observed live: box detected, then the site starts
    # 403ing the crawler, then serves a bot-challenge shell. Neither is
    # evidence the campaign removed anything — no event may be recorded.
    conn = init_db(tmp_path / "db.sqlite")
    candidate = _seed_candidate(conn)
    fetcher = ErrorableFetcher()

    o1 = _scan(conn, candidate, fetcher, tmp_path)
    assert o1.positives == 1

    fetcher.media = (403, "")
    o2 = _scan(conn, candidate, fetcher, tmp_path)
    assert o2.changes == 0 and o2.take_downs == 0

    fetcher.media = (202, "Just a moment... Checking your browser before accessing.")
    o3 = _scan(conn, candidate, fetcher, tmp_path)
    assert o3.changes == 0 and o3.take_downs == 0

    fetcher.media = (500, "")
    o4 = _scan(conn, candidate, fetcher, tmp_path)
    assert o4.changes == 0
    assert conn.execute("SELECT COUNT(*) FROM change_events").fetchone()[0] == 0
    conn.close()


def test_challenge_page_with_200_is_not_usable(tmp_path):
    # Some challenge shells return 200. The marker check catches short ones.
    conn = init_db(tmp_path / "db.sqlite")
    candidate = _seed_candidate(conn)
    fetcher = ErrorableFetcher()
    _scan(conn, candidate, fetcher, tmp_path)

    fetcher.media = (200, "Verifying you are human. Enable JavaScript and cookies to continue.")
    o2 = _scan(conn, candidate, fetcher, tmp_path)
    assert o2.changes == 0 and o2.take_downs == 0
    conn.close()


def test_second_consecutive_404_confirms_take_down(tmp_path):
    # A page absent (404) on one scan may be a CDN blip; absent on two
    # consecutive scans is a confirmed disappearance and records the
    # take_down, dated at the confirming scan, diffed against the last
    # usable (positive) scan. A later resurrection with the box records a
    # put_up against the take_down event, not the stale positive scan.
    conn = init_db(tmp_path / "db.sqlite")
    candidate = _seed_candidate(conn)
    fetcher = ErrorableFetcher()

    _scan(conn, candidate, fetcher, tmp_path)                    # positive
    fetcher.media = (404, "")
    o2 = _scan(conn, candidate, fetcher, tmp_path)               # first 404: no event
    assert o2.changes == 0
    o3 = _scan(conn, candidate, fetcher, tmp_path)               # second 404: confirmed
    assert o3.take_downs == 1 and o3.changes == 1
    ev = conn.execute("SELECT * FROM change_events WHERE event_type='take_down'").fetchone()
    assert ev["url"] == "https://example.org/media"
    assert ev["prev_classification"] == "red_box_guidance"

    o4 = _scan(conn, candidate, fetcher, tmp_path)               # third 404: state already gone
    assert o4.changes == 0

    fetcher.media = (200, "Younger voters should see ads on the go.")
    o5 = _scan(conn, candidate, fetcher, tmp_path)               # box back up
    assert o5.put_ups == 1
    ev = conn.execute("SELECT * FROM change_events WHERE event_type='put_up'").fetchone()
    assert ev["prev_classification"] == "no_guidance_detected"
    conn.close()


def test_recovery_scan_diffs_against_last_usable_scan(tmp_path):
    # positive -> 403 blip -> genuinely clean page: the take_down is recorded
    # at the recovery scan, diffed against the last USABLE scan (the
    # positive), not against the 403.
    conn = init_db(tmp_path / "db.sqlite")
    candidate = _seed_candidate(conn)
    fetcher = ErrorableFetcher()

    _scan(conn, candidate, fetcher, tmp_path)
    fetcher.media = (403, "")
    _scan(conn, candidate, fetcher, tmp_path)
    fetcher.media = (200, "Press kit: logos, headshots, bios, and media contact info.")
    o3 = _scan(conn, candidate, fetcher, tmp_path)
    assert o3.take_downs == 1 and o3.changes == 1
    ev = conn.execute("SELECT * FROM change_events").fetchone()
    assert ev["event_type"] == "take_down"
    assert ev["prev_classification"] == "red_box_guidance"
    conn.close()


def test_identical_content_after_blip_records_no_event(tmp_path):
    # positive -> 403 blip -> same positive content again: nothing changed,
    # so no 'modified' (or any) event may appear.
    conn = init_db(tmp_path / "db.sqlite")
    candidate = _seed_candidate(conn)
    fetcher = ErrorableFetcher()

    _scan(conn, candidate, fetcher, tmp_path)
    fetcher.media = (403, "")
    _scan(conn, candidate, fetcher, tmp_path)
    fetcher.media = (200, "Younger voters should see ads on the go.")
    o3 = _scan(conn, candidate, fetcher, tmp_path)
    assert o3.changes == 0
    assert conn.execute("SELECT COUNT(*) FROM change_events").fetchone()[0] == 0
    conn.close()


def test_put_up_not_recorded_from_error_baseline(tmp_path):
    # First scan errored (500), second finds the box. The box may have been
    # up all along — a first usable sighting is 'detected', not 'put_up'.
    conn = init_db(tmp_path / "db.sqlite")
    candidate = _seed_candidate(conn)
    fetcher = ErrorableFetcher()

    fetcher.media = (500, "")
    _scan(conn, candidate, fetcher, tmp_path)
    fetcher.media = (200, "Younger voters should see ads on the go.")
    o2 = _scan(conn, candidate, fetcher, tmp_path)
    assert o2.positives == 1            # detection still recorded and reviewable
    assert o2.put_ups == 0 and o2.changes == 0
    conn.close()


def test_all_error_site_is_fetch_failed_not_scanned(tmp_path):
    # A parked/expired domain 404ing every page must not publish as a dated
    # clean negative: zero usable pages -> scan_status='fetch_failed'.
    conn = init_db(tmp_path / "db.sqlite")
    candidate = _seed_candidate(conn)

    class AllErrorFetcher:
        def fetch(self, url, *, screenshot=True):
            return FetchResult(url=url, final_url=url, status=404,
                               content_type="text/html", render_mode="browser",
                               html="", visible_text="", dom_text="")

    out = _scan(conn, candidate, AllErrorFetcher(), tmp_path)
    assert out.pages_scanned == 0
    assert out.pages_failed >= 1
    st = conn.execute("SELECT scan_status FROM candidates WHERE candidate_id='H1'").fetchone()[0]
    assert st == "fetch_failed"
    conn.close()


def test_backfill_reconciles_spurious_error_events(tmp_path):
    # A DB carrying pre-fix damage of the opposite kind: events diffed
    # against error scans (a false put_up/take_down pair observed live). The
    # reconciliation must flag them as spurious and delete them on --apply.
    from redbox.pipeline import backfill_change_events

    conn = init_db(tmp_path / "db.sqlite")
    conn.execute("""INSERT INTO candidates (candidate_id,name,website_url,url_verified,
        created_at,updated_at) VALUES ('H1','T','https://example.org',1,'t','t')""")

    def scan(hash_, text, when, status=200):
        return conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,
            http_status,raw_text,text_hash)
            VALUES ('H1','https://example.org/media',?,?,?,?)""",
            (when, status, text, hash_)).lastrowid

    s1 = scan("e", "", "2026-07-09T00:00:00+00:00", status=500)
    s2 = scan("boxhash", "younger voters should see", "2026-07-25T00:00:00+00:00")
    conn.execute("""INSERT INTO detections (scan_id,candidate_id,classification,
        confidence,evidence,rationale,model,classified_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (s2, "H1", "red_box_guidance", 0.9, "[]", "r", "m", "2026-07-25T00:00:00+00:00"))
    s3 = scan("e", "", "2026-07-27T00:00:00+00:00", status=403)
    # The events the old logic recorded from those error scans:
    conn.execute("""INSERT INTO change_events (candidate_id,url,event_type,prev_scan_id,
        new_scan_id,prev_classification,new_classification,detected_at)
        VALUES ('H1','https://example.org/media','put_up',?,?,
                'no_guidance_detected','red_box_guidance','2026-07-25T00:00:00+00:00')""",
        (s1, s2))
    conn.execute("""INSERT INTO change_events (candidate_id,url,event_type,prev_scan_id,
        new_scan_id,prev_classification,new_classification,detected_at)
        VALUES ('H1','https://example.org/media','take_down',?,?,
                'red_box_guidance','no_guidance_detected','2026-07-27T00:00:00+00:00')""",
        (s2, s3))
    conn.commit()

    dry = backfill_change_events(conn, apply=False)
    assert dry["missing"] == []
    assert sorted(e["event_type"] for e in dry["spurious"]) == ["put_up", "take_down"]
    assert conn.execute("SELECT COUNT(*) FROM change_events").fetchone()[0] == 2  # dry

    backfill_change_events(conn, apply=True)
    assert conn.execute("SELECT COUNT(*) FROM change_events").fetchone()[0] == 0
    again = backfill_change_events(conn, apply=True)
    assert again["missing"] == [] and again["spurious"] == []
    conn.close()


def test_prev_scan_matches_across_url_forms(tmp_path):
    # Sitemap order / redirects drift the stored URL form between scans
    # (www vs apex, trailing slash). The diff baseline must treat them as
    # one page — an exact-match lookup silently dropped take-down events.
    from redbox.pipeline import _prev_scan

    conn = init_db(tmp_path / "db.sqlite")
    conn.execute("""INSERT INTO candidates (candidate_id,name,website_url,url_verified,
        created_at,updated_at) VALUES ('H1','T','https://x.com',1,'t','t')""")
    conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,http_status,
        raw_text,text_hash) VALUES ('H1','https://www.x.com/media','t',200,'b','h1')""")
    conn.commit()
    for form in ("https://x.com/media", "https://x.com/media/",
                 "http://www.x.com/media/", "https://www.x.com/media"):
        prev = _prev_scan(conn, "H1", form)
        assert prev is not None and prev["text_hash"] == "h1", form
    assert _prev_scan(conn, "H1", "https://x.com/press") is None


def test_scans_record_robots_posture(tmp_path):
    # ROBOTS_POLICY.md: every fetch's robots posture is auditable on its
    # scans row.
    conn = init_db(tmp_path / "db.sqlite")
    candidate = _seed_candidate(conn)
    _scan(conn, candidate, ErrorableFetcher(), tmp_path)
    postures = {r[0] for r in conn.execute("SELECT robots_posture FROM scans")}
    assert postures == {"respect"}
    conn.close()


def test_wallclock_deadline_yields_partial_scan(tmp_path, monkeypatch):
    # A crawl that would serve pages forever must stop at the candidate
    # wall-clock ceiling with a clean partial outcome: pages already scanned
    # stay committed, scan_status is 'scanned', timed_out is flagged.
    conn = init_db(tmp_path / "db.sqlite")
    conn.execute("""INSERT INTO candidates (candidate_id,name,website_url,url_verified,
        created_at,updated_at) VALUES ('H1','T','https://example.org',1,'t','t')""")
    conn.commit()
    candidate = dict(conn.execute("SELECT * FROM candidates").fetchone())

    clock = {"t": 0.0}
    monkeypatch.setattr("redbox.pipeline._monotonic", lambda: clock["t"])

    class _EndlessCrawler:
        def crawl_site(self, url):
            i = 0
            while True:
                i += 1
                clock["t"] += 30.0          # each page "takes" 30s
                yield FetchResult(
                    url=f"https://example.org/p{i}", final_url=f"https://example.org/p{i}",
                    status=200, content_type="text/html", render_mode="browser",
                    html="", visible_text=f"press kit page {i}",
                    dom_text=f"press kit page {i}")

    out = scan_candidate(conn, candidate, crawler=_EndlessCrawler(),
                         classifier=Classifier(KeywordLLM()),
                         archiver=Archiver(tmp_path / "a", push_wayback=False),
                         deadline_seconds=120.0)
    assert out.timed_out is True
    assert 0 < out.pages_scanned <= 5
    assert "wall-clock ceiling" in out.skipped_reason
    row = conn.execute("SELECT scan_status, last_scan_partial FROM candidates "
                       "WHERE candidate_id='H1'").fetchone()
    assert row["scan_status"] == "scanned"   # partial, not failed
    # The truncation is persisted: an operator can tell this 'scanned' was a
    # partial sweep, not complete coverage of the site.
    assert row["last_scan_partial"] == 1
    conn.close()


# --- The prefilter must never override a classified body or a positive
# baseline. ----------------------------------------------------------------
# Bug shape observed: the prefilter short-circuited before the alias cache, so
# a body classified positive under /media was diffed as negative under a
# /donate alias (false take_down, self-contradictory DB); and any
# positive-baseline page could be concluded negative on the prefilter's
# say-so alone, with no model in the loop.

# A body the LLM reads as guidance but that matches NONE of the prefilter's
# lexical signal patterns — the shape that made the alias bug real: the /media
# copy always classifies (media path), while the signal-free /donate copy used
# to be prefiltered instead of reusing the verdict.
SUBTLE_POSITIVE = ("Voters in this district deserve to know that our opponent "
                   "voted against the water bill. Any advertising on this point "
                   "is best kept in plain language.")


class SubtleLLM(CountingLLM):
    """Positive on the subtle body's phrasing, negative otherwise."""

    def classify_chunk(self, text, *, model):
        self.calls.append(text)
        if "deserve to know" in text:
            return {"classification": "red_box_guidance", "confidence": 0.95,
                    "evidence": [{"quote": "deserve to know", "why": "directive"}],
                    "rationale": "subtle guidance"}
        return {"classification": "no_guidance_detected", "confidence": 0.95,
                "evidence": [], "rationale": "nothing"}


class CrossClassAliasFetcher:
    """/media and /donate serve the SAME positive body (catch-all template):
    the boilerplate alias must reuse the media verdict, not be prefiltered."""

    def __init__(self):
        self.pages = {
            "https://example.org": ('<a href="/media">m</a><a href="/donate">d</a>', "home"),
            "https://example.org/media": ("", SUBTLE_POSITIVE),
            "https://example.org/donate": ("", SUBTLE_POSITIVE),
        }

    def fetch(self, url, *, screenshot=True):
        key = url.rstrip("/") or url
        html, text = self.pages.get(key, ("", ""))
        return FetchResult(
            url=url, final_url=url, status=200, content_type="text/html",
            render_mode="browser", html=html, visible_text=text, dom_text=text,
            screenshot_png=b"PNG" if screenshot else None)


def test_boilerplate_alias_of_positive_body_reuses_verdict(tmp_path):
    # One positive body under /media and /donate -> ONE detection; the
    # /donate copy is deduped via the verdict cache (checked before the
    # prefilter), never diffed as negative, and no change event appears.
    conn = init_db(tmp_path / "db.sqlite")
    candidate = _seed_candidate(conn)
    llm = SubtleLLM()
    out = scan_candidate(
        conn, candidate, crawler=_crawler_with(CrossClassAliasFetcher()),
        classifier=Classifier(llm),
        archiver=Archiver(tmp_path / "a", wayback=StubWayback(), push_wayback=False))

    assert out.pages_scanned == 3                  # home + /media + /donate
    assert out.deduped == 1                        # /donate reused the verdict
    assert out.prefiltered == 0                    # NOT short-circuited
    assert out.positives == 1                      # one reviewable finding
    assert conn.execute(
        "SELECT COUNT(*) FROM detections WHERE classification='red_box_guidance'"
    ).fetchone()[0] == 1
    # LLM saw each distinct body once: home + the shared positive body.
    assert len(llm.calls) == 2
    # No take_down (or any event) manufactured for the /donate copy.
    assert conn.execute("SELECT COUNT(*) FROM change_events").fetchone()[0] == 0
    conn.close()


class MutableDonateFetcher:
    """Home links only to /donate (boilerplate URL); its text is scriptable."""

    def __init__(self, donate_text):
        self.donate_text = donate_text

    def fetch(self, url, *, screenshot=True):
        key = url.rstrip("/") or url
        if key == "https://example.org":
            html, text = '<a href="/donate">d</a>', "home"
        elif key == "https://example.org/donate":
            html, text = "", self.donate_text
        else:
            return FetchResult(url=url, final_url=url, status=404, content_type="text/html",
                               render_mode="browser", html="", visible_text="", dom_text="")
        return FetchResult(url=url, final_url=url, status=200, content_type="text/html",
                           render_mode="browser", html=html, visible_text=text, dom_text=text,
                           screenshot_png=b"PNG")


def test_take_down_on_boilerplate_url_requires_model_verdict(tmp_path):
    # A /donate page classified positive later re-serves changed, signal-free
    # text. The prefilter must NOT conclude it (it never decides anything
    # positive-related): the classifier is invoked, and the take_down is
    # recorded only from its no-guidance verdict.
    conn = init_db(tmp_path / "db.sqlite")
    candidate = _seed_candidate(conn)
    fetcher = MutableDonateFetcher("Younger voters should see ads on the go.")
    llm = CountingLLM()
    kw = Classifier(llm)
    archiver = Archiver(tmp_path / "a", wayback=StubWayback(), push_wayback=False)

    o1 = scan_candidate(conn, candidate, crawler=_crawler_with(fetcher),
                        classifier=kw, archiver=archiver)
    assert o1.positives == 1                       # signal text reached the LLM

    fetcher.donate_text = "Chip in $25 today to help us. Donate now."
    o2 = scan_candidate(conn, candidate, crawler=_crawler_with(fetcher),
                        classifier=kw, archiver=archiver)
    # The classifier WAS consulted on the new text (positive baseline forces it)...
    assert any("Chip in" in t for t in llm.calls)
    assert o2.prefiltered == 0
    # ...and its verdict is what records the take_down.
    assert o2.take_downs == 1 and o2.changes == 1
    ev = conn.execute("SELECT * FROM change_events WHERE event_type='take_down'").fetchone()
    assert ev["url"] == "https://example.org/donate"
    assert ev["prev_classification"] == "red_box_guidance"
    assert ev["new_classification"] == "no_guidance_detected"
    conn.close()


def test_never_positive_boilerplate_page_still_prefiltered(tmp_path):
    # The cost optimization survives the fix: a boilerplate page whose
    # baseline was never positive keeps skipping the classifier even as its
    # signal-free text churns between scans.
    conn = init_db(tmp_path / "db.sqlite")
    candidate = _seed_candidate(conn)
    fetcher = MutableDonateFetcher("Chip in $25 today to help us. Donate now.")
    llm = CountingLLM()
    kw = Classifier(llm)
    archiver = Archiver(tmp_path / "a", wayback=StubWayback(), push_wayback=False)

    o1 = scan_candidate(conn, candidate, crawler=_crawler_with(fetcher),
                        classifier=kw, archiver=archiver)
    assert o1.prefiltered == 1
    fetcher.donate_text = "Give $10 before the end-of-quarter deadline."
    o2 = scan_candidate(conn, candidate, crawler=_crawler_with(fetcher),
                        classifier=kw, archiver=archiver)
    assert o2.prefiltered == 1
    # The LLM never saw either donate text.
    assert not any("Chip in" in t or "end-of-quarter" in t for t in llm.calls)
    conn.close()


def test_no_deadline_means_no_ceiling(tmp_path):
    # deadline_seconds=None (the default) must not change behavior.
    conn = init_db(tmp_path / "db.sqlite")
    conn.execute("""INSERT INTO candidates (candidate_id,name,website_url,url_verified,
        created_at,updated_at) VALUES ('H1','T','https://example.org',1,'t','t')""")
    conn.commit()
    candidate = dict(conn.execute("SELECT * FROM candidates").fetchone())
    out = scan_candidate(conn, candidate, crawler=_crawler(),
                         classifier=Classifier(KeywordLLM()),
                         archiver=Archiver(tmp_path / "a", push_wayback=False))
    assert out.timed_out is False
    assert out.pages_scanned == 3
    conn.close()


def test_zero_page_scan_stamps_last_attempt_at(tmp_path):
    # A scan concluding with ZERO pages (every fetch fails) writes no `scans`
    # rows, so candidates.last_attempt_at is the ONLY timestamp of the attempt.
    # Without it the scheduler sees the candidate as "never scanned" and
    # re-crawls the dead site every day.
    conn = init_db(tmp_path / "db.sqlite")
    candidate = _seed_candidate(conn)

    class AllErrorFetcher:
        def fetch(self, url, *, screenshot=True):
            return FetchResult(url=url, final_url=url, status=404,
                               content_type="text/html", render_mode="browser",
                               html="", visible_text="", dom_text="")

    out = _scan(conn, candidate, AllErrorFetcher(), tmp_path)
    assert out.pages_scanned == 0
    row = conn.execute("SELECT scan_status, last_attempt_at FROM candidates "
                       "WHERE candidate_id='H1'").fetchone()
    assert row["scan_status"] == "fetch_failed"
    assert row["last_attempt_at"]                 # stamped despite zero pages
    conn.close()


def test_normal_scan_stamps_last_attempt_at(tmp_path):
    # The stamp is written on EVERY concluded attempt, not just failures.
    conn = init_db(tmp_path / "db.sqlite")
    candidate = _seed_candidate(conn)
    out = _scan(conn, candidate, FixtureFetcher(), tmp_path)
    assert out.pages_scanned == 3
    row = conn.execute(
        "SELECT scan_status, last_attempt_at, last_scan_partial FROM candidates "
        "WHERE candidate_id='H1'").fetchone()
    assert row["scan_status"] == "scanned"
    assert row["last_attempt_at"]
    # A run-to-completion sweep is affirmatively marked complete (0, not NULL).
    assert row["last_scan_partial"] == 0
    conn.close()


# --- Phantom 404s (never-seen URLs) must not persist junk scan rows. ---------

def test_phantom_404_writes_no_scan_row(tmp_path):
    # A 404 on a URL with NO prior scan (phantom links from news templates)
    # is pure junk: no scans row, no diffing — but the fetch still counts in
    # the attempt bookkeeping (pages_failed), matching existing semantics.
    conn = init_db(tmp_path / "db.sqlite")
    candidate = _seed_candidate(conn)
    fetcher = ErrorableFetcher()
    fetcher.media = (404, "")
    out = _scan(conn, candidate, fetcher, tmp_path)
    assert out.pages_scanned == 1                 # home only
    assert out.pages_failed == 1                  # the 404 still counted
    assert conn.execute("SELECT COUNT(*) FROM scans").fetchone()[0] == 1  # home only
    # Re-scan: the URL is STILL never-seen, so still nothing persisted.
    _scan(conn, candidate, fetcher, tmp_path)
    assert conn.execute(
        "SELECT COUNT(*) FROM scans WHERE url LIKE '%/media'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM change_events").fetchone()[0] == 0
    conn.close()


def test_404_after_real_scan_still_recorded(tmp_path):
    # A 404 on a URL WITH a prior scan is the confirmed-take-down evidence
    # path (two consecutive 404s) and must keep being persisted.
    conn = init_db(tmp_path / "db.sqlite")
    candidate = _seed_candidate(conn)
    fetcher = ErrorableFetcher()
    _scan(conn, candidate, fetcher, tmp_path)                    # positive, real scan
    fetcher.media = (404, "")
    _scan(conn, candidate, fetcher, tmp_path)                    # first 404: recorded
    assert conn.execute(
        "SELECT COUNT(*) FROM scans WHERE url LIKE '%/media' AND http_status=404"
    ).fetchone()[0] == 1
    o3 = _scan(conn, candidate, fetcher, tmp_path)               # second 404: confirms
    assert o3.take_downs == 1
    conn.close()


# --- Classify gate and usable_scan agree on the status range (200 only). -----
# 202 responses got full LLM calls + detections rows but could never count as
# coverage or anchor a baseline (reviewable but invisible to the diff).

def test_202_page_is_neither_classified_nor_usable(tmp_path):
    from redbox.pipeline import usable_scan

    conn = init_db(tmp_path / "db.sqlite")
    candidate = _seed_candidate(conn)
    fetcher = ErrorableFetcher()
    fetcher.media = (202, "Younger voters should see ads on the go.")
    llm = CountingLLM()
    out = scan_candidate(
        conn, candidate, crawler=_crawler_with(fetcher), classifier=Classifier(llm),
        archiver=Archiver(tmp_path / "a", wayback=StubWayback(), push_wayback=False))
    # The LLM never saw the 202 body — no wasted spend, no detection row.
    assert not any("should see" in t for t in llm.calls)
    assert out.pages_scanned == 1 and out.pages_failed == 1      # not coverage
    media = conn.execute(
        "SELECT scan_id, http_status FROM scans WHERE url LIKE '%/media'").fetchone()
    assert media["http_status"] == 202                           # audit trail kept
    assert conn.execute("SELECT COUNT(*) FROM detections WHERE scan_id=?",
                        (media["scan_id"],)).fetchone()[0] == 0
    assert usable_scan(202, "Younger voters should see ads on the go.") is False
    conn.close()


def test_204_and_302_pages_not_classified_or_usable(tmp_path):
    from redbox.pipeline import usable_scan

    for i, (status, text) in enumerate([(204, ""), (302, "Object moved to here.")]):
        conn = init_db(tmp_path / f"db{i}.sqlite")
        candidate = _seed_candidate(conn)
        fetcher = ErrorableFetcher()
        fetcher.media = (status, text)
        llm = CountingLLM()
        out = scan_candidate(
            conn, candidate, crawler=_crawler_with(fetcher),
            classifier=Classifier(llm),
            archiver=Archiver(tmp_path / f"a{i}", wayback=StubWayback(),
                              push_wayback=False))
        assert out.pages_scanned == 1 and out.pages_failed == 1, status
        assert not any(text and text in t for t in llm.calls), status
        media = conn.execute(
            "SELECT scan_id FROM scans WHERE url LIKE '%/media'").fetchone()
        assert conn.execute("SELECT COUNT(*) FROM detections WHERE scan_id=?",
                            (media["scan_id"],)).fetchone()[0] == 0, status
        assert usable_scan(status, "some perfectly real body text") is False, status
        conn.close()


def test_usable_scan_sql_agrees_with_python(tmp_path):
    # The SQL mirror (used by the publisher and _baseline_state) must match
    # usable_scan row for row, statuses included.
    from redbox.pipeline import usable_scan, usable_scan_sql

    conn = init_db(tmp_path / "db.sqlite")
    conn.execute("""INSERT INTO candidates (candidate_id,name,website_url,url_verified,
        created_at,updated_at) VALUES ('H1','T','https://x.com',1,'t','t')""")
    cases = [(200, "real content here"), (200, ""), (202, "real content here"),
             (204, ""), (302, "Object moved to here."), (404, ""), (500, ""),
             (200, "Just a moment... Checking your browser before accessing.")]
    ids = {}
    for i, (st, txt) in enumerate(cases):
        ids[conn.execute(
            """INSERT INTO scans (candidate_id,url,fetched_at,http_status,
                   raw_text,text_hash) VALUES ('H1',?, 't', ?, ?, ?)""",
            (f"https://x.com/p{i}", st, txt, f"h{i}")).lastrowid] = (st, txt)
    conn.commit()
    sql_ids = {r["scan_id"] for r in conn.execute(
        f"SELECT scan_id FROM scans s WHERE {usable_scan_sql('s')}")}
    for scan_id, (st, txt) in ids.items():
        assert (scan_id in sql_ids) == usable_scan(st, txt), (st, txt)
    conn.close()


# --- Unparseable classifier output skips the page like a transport error. ----

def test_unparseable_model_output_skips_page_not_candidate(tmp_path):
    # An unparseable response used to manufacture ambiguous/0.0 — a real
    # detections row, an archive, and a put_up event outliving the provider
    # hiccup. Now it must skip the page exactly like a transport failure:
    # nothing persisted, page retried next scan, candidate scan continues.
    from redbox.classifier import ClassifierParseError

    class ParseFailLLM:
        def classify_chunk(self, text, *, model):
            if "should see" in text:   # only the /media positive fixture
                raise ClassifierParseError("simulated unparseable model output")
            return {"classification": "no_guidance_detected", "confidence": 0.95,
                    "evidence": [], "rationale": "ok"}

    conn = init_db(tmp_path / "db.sqlite")
    candidate = _seed_candidate(conn)
    out = scan_candidate(conn, candidate, crawler=_crawler(),
                         classifier=Classifier(ParseFailLLM()),
                         archiver=Archiver(tmp_path / "a", push_wayback=False))
    assert out.skipped_reason is None
    assert out.pages_scanned == 2                    # home + /press; /media dropped
    # Nothing concluded for the page: no scan row (so not hash-skipped — it
    # is retried next run), no detection, no archive, no change event.
    urls = {r["url"] for r in conn.execute("SELECT url FROM scans")}
    assert "https://example.org/media" not in urls
    assert conn.execute(
        "SELECT COUNT(*) FROM detections WHERE classification IN "
        "('ambiguous','red_box_guidance')").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM archives").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM change_events").fetchone()[0] == 0
    conn.close()


def test_parseable_output_behavior_unchanged_after_parse_fix(tmp_path):
    # Sanity guard for the parse-error change: a normal scan still produces
    # the detection + archive exactly as before.
    conn = init_db(tmp_path / "db.sqlite")
    candidate = _seed_candidate(conn)
    out = _scan(conn, candidate, ErrorableFetcher(), tmp_path)
    assert out.positives == 1 and out.archived == 1
    conn.close()


# --- Backfill detects and corrects WRONG events, not just missing/spurious. --

def test_backfill_reports_and_corrects_mismatched_event(tmp_path):
    # An event recorded at the right (candidate, URL, revealing scan) but with
    # the wrong content — here a put_up where the replay says take_down — must
    # land in the 'mismatched' bucket and be corrected in place on apply.
    from redbox.pipeline import backfill_change_events

    conn = init_db(tmp_path / "db.sqlite")
    conn.execute("""INSERT INTO candidates (candidate_id,name,website_url,url_verified,
        created_at,updated_at) VALUES ('H1','T','https://example.org',1,'t','t')""")

    def scan(hash_, text, when):
        return conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,
            http_status,raw_text,text_hash)
            VALUES ('H1','https://example.org/media',?,200,?,?)""",
            (when, text, hash_)).lastrowid

    s1 = scan("boxhash", "younger voters should see", "2026-07-01T00:00:00+00:00")
    conn.execute("""INSERT INTO detections (scan_id,candidate_id,classification,
        confidence,evidence,rationale,model,classified_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (s1, "H1", "red_box_guidance", 0.9, "[]", "r", "m", "2026-07-01T00:00:00+00:00"))
    s2 = scan("cleanhash", "press kit and logos", "2026-07-15T00:00:00+00:00")
    # The deliberately WRONG event at the correct key:
    conn.execute("""INSERT INTO change_events (candidate_id,url,event_type,prev_scan_id,
        new_scan_id,prev_classification,new_classification,detected_at)
        VALUES ('H1','https://example.org/media','put_up',?,?,
                'no_guidance_detected','red_box_guidance','2026-07-15T00:00:00+00:00')""",
        (s1, s2))
    conn.commit()

    dry = backfill_change_events(conn, apply=False)
    assert dry["missing"] == [] and dry["spurious"] == []
    assert len(dry["mismatched"]) == 1
    m = dry["mismatched"][0]
    assert m["event_type"] == "take_down"            # what the replay expects
    assert m["recorded_event_type"] == "put_up"      # what the DB wrongly holds
    # Dry run: the wrong row is untouched.
    assert conn.execute("SELECT event_type FROM change_events").fetchone()[0] == "put_up"

    backfill_change_events(conn, apply=True)
    assert conn.execute("SELECT COUNT(*) FROM change_events").fetchone()[0] == 1
    row = conn.execute("SELECT * FROM change_events").fetchone()
    assert row["event_type"] == "take_down"
    assert row["prev_classification"] == "red_box_guidance"
    assert row["new_classification"] == "no_guidance_detected"
    assert row["prev_scan_id"] == s1 and row["new_scan_id"] == s2

    again = backfill_change_events(conn, apply=True)             # idempotent
    assert (again["missing"] == [] and again["spurious"] == []
            and again["mismatched"] == [])
    conn.close()
