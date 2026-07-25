"""Offline tests for the publisher / review-console site (spec §3.7a, §3.8)."""
from __future__ import annotations

from redbox.db import init_db
from redbox.publisher import POSITIVE_LABEL, build_site


def _seed_universe(conn, n, *, with_detection=0):
    """Seed n bare candidates; the first `with_detection` get a positive detection."""
    for i in range(n):
        cid = f"H{i:04d}"
        conn.execute("""INSERT INTO candidates (candidate_id,name,office,state,district,
            party,cycle,universe_reason,website_url,url_verified,receipts,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, f"CAND {i:04d}", "H", "NY", f"{i % 27:02d}", "DEM", 2026,
             "contested_primary", "https://example.org", 0, 100000 + i, "t", "t"))
        if i < with_detection:
            cur = conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,text_hash)
                VALUES (?,?,?,?)""", (cid, "https://example.org/media",
                                      "2026-05-29T00:00:00+00:00", f"h{i}"))
            sid = cur.lastrowid
            conn.execute("""INSERT INTO detections (scan_id,candidate_id,classification,
                confidence,evidence,rationale,model,classified_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                (sid, cid, "red_box_guidance", 0.9, "[]", "r", "claude-haiku-4-5",
                 "2026-05-29T00:00:00+00:00"))
    conn.commit()


def _seed(conn, classification="red_box_guidance"):
    conn.execute("""INSERT INTO candidates (candidate_id,name,office,state,district,
        party,cycle,universe_reason,website_url,url_verified,receipts,created_at,updated_at)
        VALUES ('H1','TEST CANDIDATE','H','NY','12','DEM',2026,'contested_primary',
                'https://example.org',1,2000000,'t','t')""")
    cur = conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,text_hash)
        VALUES ('H1','https://example.org/media','2026-05-29T00:00:00+00:00','abc')""")
    sid = cur.lastrowid
    cur = conn.execute("""INSERT INTO detections (scan_id,candidate_id,classification,
        confidence,evidence,rationale,model,classified_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (sid, 'H1', classification, 0.95,
         '[{"quote":"younger voters should see","why":"directive"}]',
         'directive guidance', 'claude-haiku-4-5', '2026-05-29T00:00:00+00:00'))
    conn.commit()
    return cur.lastrowid


def test_pending_positive_is_not_published_as_finding(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    _seed(conn)
    out = build_site(conn, tmp_path / "site")
    cand = (out / "H1.html").read_text()
    index = (out / "index.html").read_text()
    # §3.7a label is present, but marked pending — not a published finding.
    assert POSITIVE_LABEL in cand
    assert "pending human review" in cand.lower()
    assert "PENDING REVIEW" in index
    assert "FINDING" not in index.split("PENDING REVIEW")[0][-400:]  # pill isn't "FINDING"
    # evidence quote surfaced
    assert "younger voters should see" in cand
    # required pages exist (spec §3.7a)
    assert (out / "methodology.html").exists()
    assert (out / "corrections.html").exists()
    conn.close()


def test_approved_positive_becomes_published_finding(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    det_id = _seed(conn)
    conn.execute("""INSERT INTO reviews (detection_id,reviewer,action,reviewed_at)
                    VALUES (?,?,?,?)""", (det_id, "editor", "approve", "2026-05-30T00:00:00+00:00"))
    conn.commit()
    out = build_site(conn, tmp_path / "site")
    index = (out / "index.html").read_text()
    assert "FINDING" in index
    # approved-only public build includes it
    out2 = build_site(conn, tmp_path / "site2", approved_only=True)
    assert "TEST CANDIDATE" in (out2 / "index.html").read_text()
    conn.close()


def test_positive_embeds_archived_page_text(tmp_path):
    # The archiver's extracted-text file is embedded in a collapsed <details>
    # on the finding page — the accessible record when the screenshot is
    # obscured (pop-up, cookie banner) or unreadable to a screen reader.
    conn = init_db(tmp_path / "db.sqlite")
    det_id = _seed(conn)
    txt = tmp_path / "page.txt"
    txt.write_text("DONATE\nOur message: younger voters should see this ad & more")
    conn.execute("""INSERT INTO archives (detection_id,candidate_id,url,archived_at,text_path)
                    VALUES (?,?,?,?,?)""",
                 (det_id, "H1", "https://example.org/media",
                  "2026-05-29T00:00:00+00:00", str(txt)))
    conn.commit()
    out = build_site(conn, tmp_path / "site")
    cand = (out / "H1.html").read_text()
    assert "Plain text of the archived page" in cand
    assert "see this ad &amp; more" in cand          # embedded and escaped
    # A missing text file degrades to no block, not a broken build.
    txt.unlink()
    out2 = build_site(conn, tmp_path / "site2")
    assert "Plain text of the archived page" not in (out2 / "H1.html").read_text()


def test_pdf_finding_shows_rendered_exhibit_and_original(tmp_path):
    # A PDF-sourced finding gets the rendered-pages image as its exhibit plus a
    # link to the archived original document, copied into site/evidence/.
    conn = init_db(tmp_path / "db.sqlite")
    det_id = _seed(conn)
    shot = tmp_path / "abc123.webp"
    shot.write_bytes(b"RIFFfakeWEBP")
    pdf = tmp_path / "abc123.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    conn.execute("""INSERT INTO archives (detection_id,candidate_id,url,archived_at,
                    screenshot_path,pdf_path)
                    VALUES (?,?,?,?,?,?)""",
                 (det_id, "H1", "https://example.org/memo.pdf",
                  "2026-05-29T00:00:00+00:00", str(shot), str(pdf)))
    conn.commit()
    out = build_site(conn, tmp_path / "site")
    cand = (out / "H1.html").read_text()
    assert 'src="evidence/abc123.webp"' in cand
    assert 'href="evidence/abc123.pdf"' in cand
    assert "Archived PDF (original)" in cand
    assert "Pages rendered from the PDF" in cand
    assert (out / "evidence" / "abc123.pdf").read_bytes() == b"%PDF-1.4 fake"
    conn.close()


def test_negative_uses_dated_language(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    _seed(conn, classification="no_guidance_detected")
    out = build_site(conn, tmp_path / "site")
    cand = (out / "H1.html").read_text()
    assert "No public messaging guidance detected as of" in cand
    # The phrase may appear only inside the footer disclaimer, which quotes it
    # to disavow it; page content itself must never assert a clearance.
    content = cand.lower().split("<footer", 1)[0]
    assert "does not red-box" not in content
    conn.close()


def test_unresolved_candidate_shows_no_site_status(tmp_path):
    # A candidate with no website_url and no scans is surfaced as a coverage gap.
    conn = init_db(tmp_path / "db.sqlite")
    conn.execute("""INSERT INTO candidates (candidate_id,name,office,state,district,
        party,cycle,universe_reason,website_url,url_source,url_verified,receipts,
        created_at,updated_at)
        VALUES ('H9','NOURL','H','NY','12','DEM',2026,'contested_primary',
                NULL,'none',0,200000,'t','t')""")
    conn.commit()
    out = build_site(conn, tmp_path / "site")
    index = (out / "index.html").read_text()
    cand = (out / "H9.html").read_text()
    assert "NO SITE FOUND" in index
    assert "Coverage gap:" in index           # prominent count
    assert "No campaign site found" in cand
    conn.close()


def test_gather_query_count_is_constant_not_n_plus_1(tmp_path):
    # The publisher must not issue per-candidate queries: query count should be
    # the same for 5 candidates as for 40 (the old code was strictly N+1).
    def _count_build(n):
        conn = init_db(tmp_path / f"db{n}.sqlite")
        _seed_universe(conn, n, with_detection=n // 2)
        stmts = []
        conn.set_trace_callback(lambda s: stmts.append(s))
        build_site(conn, tmp_path / f"site{n}")
        conn.set_trace_callback(None)
        # Only count read queries against the data tables (ignore PRAGMA/BEGIN/etc).
        selects = [s for s in stmts if s.lstrip().upper().startswith("SELECT")]
        conn.close()
        return len(selects)

    small, large = _count_build(5), _count_build(40)
    assert small == large, f"query count grew with universe: {small} -> {large}"


def test_index_paginates_when_over_page_size(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    _seed_universe(conn, 12)
    out = build_site(conn, tmp_path / "site", page_size=5)
    # 12 rows / 5 per page -> 3 pages.
    assert (out / "index.html").exists()
    assert (out / "index-2.html").exists()
    assert (out / "index-3.html").exists()
    assert not (out / "index-4.html").exists()
    idx = (out / "index.html").read_text()
    assert "Page 1 of 3" in idx
    assert 'href="index-2.html"' in idx
    # Each page holds at most page_size data rows.
    assert (out / "index.html").read_text().count("<tr data-status") == 5
    assert (out / "index-3.html").read_text().count("<tr data-status") == 2
    conn.close()


def test_findings_lead_page_one(tmp_path):
    # A single finding among many plain candidates must land on page 1 even though
    # its district would sort it late — actionable rows lead the index.
    conn = init_db(tmp_path / "db.sqlite")
    _seed_universe(conn, 12, with_detection=1)   # H0000 gets the positive
    out = build_site(conn, tmp_path / "site", page_size=5)
    assert "PENDING REVIEW" in (out / "index.html").read_text()
    conn.close()


def test_single_page_has_no_pager(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    _seed_universe(conn, 3)
    out = build_site(conn, tmp_path / "site", page_size=500)
    idx = (out / "index.html").read_text()
    assert "Page 1 of" not in idx
    assert not (out / "index-2.html").exists()
    conn.close()


def test_404_page_built_with_absolute_links(tmp_path):
    # 404.html is served by Pages at arbitrary unknown depths, so its asset
    # and nav links must be absolute; it must never appear in the sitemap.
    conn = init_db(tmp_path / "db.sqlite")
    _seed(conn)
    out = build_site(conn, tmp_path / "site", approved_only=True,
                     site_url="https://redboxwatch.org/")
    nf = (out / "404.html").read_text()
    assert "No page detected" in nf
    assert 'href="/styles.css' in nf
    assert 'href="/index.html"' in nf
    assert "404" not in (out / "sitemap.xml").read_text()
    conn.close()


def test_site_url_emits_seo_files_and_canonicals(tmp_path):
    # With publish.site_url set (public build), the site gets sitemap.xml,
    # robots.txt, and per-page canonical/OpenGraph tags with extensionless URLs.
    conn = init_db(tmp_path / "db.sqlite")
    det_id = _seed(conn)
    conn.execute("""INSERT INTO reviews (detection_id,reviewer,action,reviewed_at)
                    VALUES (?,?,?,?)""", (det_id, "editor", "approve", "2026-05-30T00:00:00+00:00"))
    conn.commit()
    out = build_site(conn, tmp_path / "site", approved_only=True,
                     site_url="https://redboxwatch.org/")
    sitemap = (out / "sitemap.xml").read_text()
    assert "<loc>https://redboxwatch.org/H1</loc>" in sitemap      # extensionless
    assert "<loc>https://redboxwatch.org/</loc>" in sitemap        # index -> root
    assert ".html</loc>" not in sitemap
    robots = (out / "robots.txt").read_text()
    assert "Sitemap: https://redboxwatch.org/sitemap.xml" in robots
    cand = (out / "H1.html").read_text()
    assert '<link rel="canonical" href="https://redboxwatch.org/H1">' in cand
    assert '<meta property="og:type" content="article">' in cand
    assert '<meta name="description"' in cand
    conn.close()


def test_review_build_has_no_seo_footprint(tmp_path):
    # No site_url (review console): no sitemap/robots, no canonical URLs —
    # pending detections must never be described by public URLs.
    conn = init_db(tmp_path / "db.sqlite")
    _seed(conn)
    out = build_site(conn, tmp_path / "site")
    assert not (out / "sitemap.xml").exists()
    assert not (out / "robots.txt").exists()
    assert 'rel="canonical"' not in (out / "H1.html").read_text()
    conn.close()


def test_display_name_natural_order():
    # FEC 'LAST, FIRST [SUFFIX]' renders first-name-first on every display
    # surface; the raw string stays the DB/matching key.
    from redbox.publisher import _display_name
    assert _display_name("LEE, SUMMER") == "Summer Lee"
    assert _display_name("STEUBE, W. GREGORY III") == "W. Gregory Steube III"
    assert _display_name("SMITH, JOHN, JR.") == "John Smith Jr."
    assert _display_name("DE LA CRUZ, MONICA") == "Monica De La Cruz"
    assert _display_name("ONDER JR, ROBERT FRANK") == "Robert Frank Onder Jr"
    assert _display_name("MADONNA") == "Madonna"          # no comma: as filed
    assert _display_name("BRINK,, BRIDGET") == "Bridget Brink"  # FEC double comma
    # Self-styled titles/degrees are stripped from the trailing zone…
    assert _display_name("WHALEN, JEROMIE PATRICK DR.") == "Jeromie Patrick Whalen"
    assert _display_name("BONAMICI, SUZANNE MS.") == "Suzanne Bonamici"
    assert _display_name("LUMMIS, CYNTHIA MARIE MRS.") == "Cynthia Marie Lummis"
    assert _display_name("RUSSELL, RONALD CHARLES MR.") == "Ronald Charles Russell"
    assert _display_name("DUNN, LAURA L. MS. ESQ.") == "Laura L. Dunn"
    assert _display_name("DUNN, NEAL PATRICK MD, FACS") == "Neal Patrick Dunn"
    assert _display_name("KAPTUR, MARCY HON. M.C.") == "Marcy Kaptur"
    assert _display_name("WOMACK, STEPHEN A THE HON") == "Stephen A Womack"
    # …suffixes survive the strip and re-seat after the surname…
    assert _display_name("SMITH, RAYMOND EDWARD DR. JR.") == "Raymond Edward Smith Jr."
    assert _display_name("HARRIS, DIOP JERMAINE MR II") == "Diop Jermaine Harris II"
    assert _display_name("MARKERT, GEORGE WASHINGTON MR V") == "George Washington Markert V"
    # …bare V without title context stays a middle initial…
    assert _display_name("SMITH, JOHN V") == "John V Smith"
    # …stuttered words collapse, doubled initials don't.
    assert _display_name("GUTHRIE, S. BRETT BRETT HON.") == "S. Brett Guthrie"
    assert _display_name("MURPHY, MORGAN W. W.") == "Morgan W. W. Murphy"


def test_public_build_emits_finding_feeds(tmp_path):
    # feed.xml (RSS) + feed.json (JSON Feed) list published findings with
    # absolute links and the approval date; negatives are not items.
    import json as _json
    conn = init_db(tmp_path / "db.sqlite")
    det_id = _seed(conn)
    conn.execute("""INSERT INTO reviews (detection_id,reviewer,action,reviewed_at)
                    VALUES (?,?,?,?)""", (det_id, "editor", "approve", "2026-05-30T00:00:00+00:00"))
    conn.commit()
    out = build_site(conn, tmp_path / "site", approved_only=True,
                     site_url="https://redboxwatch.org/")
    rss = (out / "feed.xml").read_text()
    assert "<link>https://redboxwatch.org/H1</link>" in rss
    assert "red-box guidance found" in rss
    assert "younger voters should see" in rss           # evidence quote carried
    assert "30 May 2026" in rss                         # RFC 822 approval date
    feed = _json.loads((out / "feed.json").read_text())
    assert feed["version"] == "https://jsonfeed.org/version/1.1"
    assert feed["items"][0]["url"] == "https://redboxwatch.org/H1"
    assert feed["items"][0]["date_published"].startswith("2026-05-30")
    # Feeds are for readers, not crawlers: neither belongs in the sitemap.
    assert "feed" not in (out / "sitemap.xml").read_text()
    conn.close()


def test_negative_and_review_builds_have_no_feed_items(tmp_path):
    # A dated negative is not a "new finding"; review builds emit no feeds.
    import json as _json
    conn = init_db(tmp_path / "db.sqlite")
    _seed(conn, classification="no_guidance_detected")
    out = build_site(conn, tmp_path / "site", approved_only=True,
                     site_url="https://redboxwatch.org/")
    assert _json.loads((out / "feed.json").read_text())["items"] == []
    assert "<item>" not in (out / "feed.xml").read_text()
    out2 = build_site(conn, tmp_path / "site2")
    assert not (out2 / "feed.xml").exists()
    assert not (out2 / "feed.json").exists()
    conn.close()


def test_robots_blocked_candidate_shows_blocked_status(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    conn.execute("""INSERT INTO candidates (candidate_id,name,office,state,district,
        party,cycle,universe_reason,website_url,url_source,url_verified,receipts,
        scan_status,created_at,updated_at)
        VALUES ('H8','BLOCKED','H','NY','12','DEM',2026,'contested_primary',
                'https://blocked.example','search',0,200000,'robots_blocked','t','t')""")
    conn.commit()
    out = build_site(conn, tmp_path / "site")
    index = (out / "index.html").read_text()
    cand = (out / "H8.html").read_text()
    assert "BLOCKED (ROBOTS)" in index
    assert "block automated access" in index   # coverage banner mentions it
    assert "Site blocks automated access" in cand
    conn.close()


def test_inactive_candidates_never_published(tmp_path):
    # An FEC-inactive (withdrawn/superseded) or human wrong-race record stays in
    # the DB for history but must not get a candidate page or index row.
    import sqlite3

    from redbox.db import init_db
    from redbox.publisher import build_site

    db = tmp_path / "db.sqlite"
    conn = init_db(db)
    conn.execute("""INSERT INTO candidates (candidate_id,name,office,state,district,
        party,cycle,universe_reason,website_url,url_verified,receipts,inactive,
        created_at,updated_at)
        VALUES ('H1','ACTIVE, ANNIE','H','NY','12','DEM',2026,'contested_primary',
                'https://a.example',1,100000,NULL,'t','t'),
               ('H2','PHANTOM, PETE','H','GA','01','REP',2026,'contested_general',
                'https://p.example',0,6800000,1,'t','t'),
               ('H3','WRONGSEAT, WANDA','H','KY','06','REP',2026,'contested_general',
                NULL,0,8300000,2,'t','t')""")
    conn.commit()
    out = tmp_path / "site"
    build_site(conn, out)
    conn.close()
    index = (out / "index.html").read_text()
    assert "ACTIVE, ANNIE" in index or "Annie" in index
    assert "PHANTOM" not in index and "WRONGSEAT" not in index
    assert not (out / "H2.html").exists()
    assert not (out / "H3.html").exists()
    assert (out / "H1.html").exists()


def test_inactive_candidate_with_approved_finding_stays_published(tmp_path):
    # A candidacy that ends AFTER a finding was approved keeps its page — the
    # ledger must not silently unpublish an approved finding.
    from redbox.db import init_db
    from redbox.publisher import build_site

    db = tmp_path / "db.sqlite"
    conn = init_db(db)
    conn.execute("""INSERT INTO candidates (candidate_id,name,office,state,district,
        party,cycle,universe_reason,website_url,url_verified,receipts,inactive,
        created_at,updated_at)
        VALUES ('H4','RETIRED, RITA','H','CA','26','DEM',2026,'contested_general',
                'https://rita.example',1,900000,1,'t','t')""")
    cur = conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,text_hash,raw_text)
        VALUES ('H4','https://rita.example/media','2026-07-01T00:00:00+00:00','th','txt')""")
    cur = conn.execute("""INSERT INTO detections (scan_id,candidate_id,classification,
        confidence,evidence,rationale,model,classified_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (cur.lastrowid, "H4", "red_box_guidance", 0.95, "[]", "r", "m",
         "2026-07-01T00:00:00+00:00"))
    conn.execute("""INSERT INTO reviews (detection_id,reviewer,action,reviewed_at)
        VALUES (?,?,?,?)""", (cur.lastrowid, "t", "approve", "2026-07-02T00:00:00+00:00"))
    conn.commit()
    out = tmp_path / "site"
    build_site(conn, out)
    conn.close()
    assert (out / "H4.html").exists()
    assert "RETIRED" in (out / "index.html").read_text().upper()
    # ...and the page is labeled as an ended run, not presented as live.
    assert "no longer an active candidacy" in (out / "H4.html").read_text()


def test_approved_ambiguous_publishes_with_confirmed_label(tmp_path):
    # An ambiguous detection a human APPROVED is a publishable finding — labeled
    # with the classifier's initial hesitation, and included in --approved-only.
    from redbox.db import init_db
    from redbox.publisher import build_site

    db = tmp_path / "db.sqlite"
    conn = init_db(db)
    conn.execute("""INSERT INTO candidates (candidate_id,name,office,state,district,
        party,cycle,universe_reason,website_url,url_verified,receipts,
        created_at,updated_at)
        VALUES ('H5','CONFIRMED, CARLA','H','MI','00','DEM',2026,'contested_general',
                'https://carla.example',1,500000,'t','t')""")
    cur = conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,text_hash,raw_text)
        VALUES ('H5','https://carla.example/media','2026-07-01T00:00:00+00:00','th','txt')""")
    cur = conn.execute("""INSERT INTO detections (scan_id,candidate_id,classification,
        confidence,evidence,rationale,model,classified_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (cur.lastrowid, "H5", "ambiguous", 0.62, "[]", "r", "m",
         "2026-07-01T00:00:00+00:00"))
    conn.execute("""INSERT INTO reviews (detection_id,reviewer,action,reviewed_at)
        VALUES (?,?,?,?)""", (cur.lastrowid, "t", "approve", "2026-07-02T00:00:00+00:00"))
    conn.commit()
    out = tmp_path / "site"
    build_site(conn, out, approved_only=True)
    conn.close()
    assert (out / "H5.html").exists()
    page = (out / "H5.html").read_text()
    assert "initially classified ambiguous" in page
    assert "confirmed on human review" in page


def test_index_default_sort_is_aligned_ie_first(tmp_path):
    # Default row order: findings WITH aligned IE (richest first), then findings
    # without IE, then non-findings — the IE column is the default sort story.
    from redbox.db import init_db
    from redbox.publisher import build_site

    conn = init_db(tmp_path / "db.sqlite")

    def seed(cid, name, ie=None, finding=True, approved=True):
        conn.execute("""INSERT INTO candidates (candidate_id,name,office,state,
            district,party,cycle,universe_reason,website_url,url_verified,receipts,
            created_at,updated_at)
            VALUES (?,?,?,?,?,?,2026,'contested_primary','https://x.example',1,
                    100000,'t','t')""", (cid, name, "H", "NY", "01", "DEM"))
        cur = conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,text_hash)
            VALUES (?,?,?,?)""", (cid, "https://x.example/media", "t", "h" + cid))
        cls = "red_box_guidance" if finding else "no_guidance_detected"
        cur = conn.execute("""INSERT INTO detections (scan_id,candidate_id,
            classification,confidence,evidence,rationale,model,classified_at)
            VALUES (?,?,?,?,?,?,?,?)""",
            (cur.lastrowid, cid, cls, 0.9, "[]", "r", "m", "t"))
        if finding and approved:
            conn.execute("""INSERT INTO reviews (detection_id,reviewer,action,
                reviewed_at) VALUES (?,?,?,?)""", (cur.lastrowid, "t", "approve", "t"))
        if ie is not None:
            conn.execute("""INSERT INTO corroboration (candidate_id,
                supporting_total,ie_filing_count,computed_at)
                VALUES (?,?,?,?)""", (cid, ie, 3, "t"))

    seed("H1", "SMALL IE, SAM", ie=100000)
    seed("H2", "BIG IE, BELLA", ie=5000000)
    seed("H3", "NO IE, NORA")                      # finding, no corroboration
    seed("H4", "NEGATIVE, NED", finding=False)     # no finding at all
    conn.commit()
    out = build_site(conn, tmp_path / "site")
    conn.close()
    index = (out / "index.html").read_text()
    order = sorted(["BIG IE, BELLA", "SMALL IE, SAM", "NO IE, NORA", "NEGATIVE, NED"],
                   key=index.find)
    assert order == ["BIG IE, BELLA", "SMALL IE, SAM", "NO IE, NORA", "NEGATIVE, NED"]
