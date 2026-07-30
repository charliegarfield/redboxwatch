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
            cur = conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,
                text_hash,http_status,raw_text) VALUES (?,?,?,?,200,'page body')""",
                (cid, "https://example.org/media", "2026-05-29T00:00:00+00:00", f"h{i}"))
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
    cur = conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,text_hash,
        http_status,raw_text)
        VALUES ('H1','https://example.org/media','2026-05-29T00:00:00+00:00','abc',
                200,'page body')""")
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
    assert "Test Candidate" in (out2 / "index.html").read_text()
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
    # Public builds link the extensionless form Pages serves, so crawlers
    # never traverse the /X.html -> /X redirect.
    assert 'href="/"' in nf
    assert 'href="/methodology"' in nf
    assert 'href="/index.html"' not in nf
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
    # Congressional self-styling and parenthesized military retirement tags.
    assert _display_name("ADERHOLT, ROBERT B. REP.") == "Robert B. Aderholt"
    assert _display_name("MARKEY, EDWARD SEN.") == "Edward Markey"
    assert _display_name("PIERCE, MICHAEL DAVID LTC (RET.)") == "Michael David Pierce"
    assert _display_name("CHALIFOUX, THOMAS E. COLONEL JR.") == "Thomas E. Chalifoux Jr."
    # Parenthesized nicknames are names, not titles.
    assert _display_name("FOSTER, G. WILLIAM (BILL)") == "G. William (Bill) Foster"


def test_ledger_name_light_cleanup():
    # The index keeps the official ALL-CAPS 'LAST, FIRST' ledger form but
    # drops filing noise: double commas, self-styled titles, stutters.
    from redbox.publisher import _ledger_name
    assert _ledger_name("LEE, SUMMER") == "LEE, SUMMER"
    assert _ledger_name("BRINK,, BRIDGET") == "BRINK, BRIDGET"
    assert _ledger_name("LANDER, BRAD MR.") == "LANDER, BRAD"
    assert _ledger_name("DUNN, LAURA L. MS. ESQ.") == "DUNN, LAURA L."
    assert _ledger_name("GUTHRIE, S. BRETT BRETT HON.") == "GUTHRIE, S. BRETT"
    # Suffixes stay at the tail where FEC files them, titles between them go.
    assert _ledger_name("JACKSON, JESSE L. JR") == "JACKSON, JESSE L. JR"
    assert _ledger_name("SMITH, RAYMOND EDWARD DR. JR.") == "SMITH, RAYMOND EDWARD JR."
    assert _ledger_name("STEUBE, W. GREGORY III") == "STEUBE, W. GREGORY III"
    # No comma (or a suffix fused into the surname): as filed.
    assert _ledger_name("MADONNA") == "MADONNA"
    assert _ledger_name("ONDER JR, ROBERT FRANK") == "ONDER JR, ROBERT FRANK"
    assert _ledger_name("") == ""


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


def test_new_exhibit_reannounces_feed_item(tmp_path):
    # Approving a second distinct box on an already-published candidate must
    # change the item's guid (feed readers re-announce it), bump its date,
    # count exhibits in the title, and showcase the NEWEST exhibit's page.
    import json as _json
    conn = init_db(tmp_path / "db.sqlite")
    det_id = _seed(conn)
    conn.execute("""INSERT INTO reviews (detection_id,reviewer,action,reviewed_at)
                    VALUES (?,?,?,?)""", (det_id, "editor", "approve", "2026-05-30T00:00:00+00:00"))
    conn.commit()
    out = build_site(conn, tmp_path / "site", approved_only=True,
                     site_url="https://redboxwatch.org/")
    first = _json.loads((out / "feed.json").read_text())["items"][0]

    cur = conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,text_hash,
        http_status,raw_text)
        VALUES ('H1','https://example.org/update.pdf','2026-06-10T00:00:00+00:00','def',
                200,'page body')""")
    cur = conn.execute("""INSERT INTO detections (scan_id,candidate_id,classification,
        confidence,evidence,rationale,model,classified_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (cur.lastrowid, 'H1', 'red_box_guidance', 0.9,
         '[{"quote":"suburban voters need to read","why":"directive"}]',
         'update box', 'claude-haiku-4-5', '2026-06-10T00:00:00+00:00'))
    conn.execute("""INSERT INTO reviews (detection_id,reviewer,action,reviewed_at)
                    VALUES (?,?,?,?)""", (cur.lastrowid, "editor", "approve", "2026-06-11T00:00:00+00:00"))
    conn.commit()
    out2 = build_site(conn, tmp_path / "site2", approved_only=True,
                      site_url="https://redboxwatch.org/")
    second = _json.loads((out2 / "feed.json").read_text())["items"][0]

    assert second["id"] != first["id"]                      # re-announces
    # Approvals span two days, so this genuinely is an update — the title
    # says so instead of the old "(2 exhibits)" jargon.
    assert second["title"].startswith("Updated red-box guidance found for ")
    assert second["date_published"].startswith("2026-06-11")
    assert "update.pdf" in second["content_text"]           # newest showcased
    assert "suburban voters need to read" in second["content_text"]
    # A re-detection of the SAME box (same URL/body, new detection_id) must
    # NOT change the guid — that was the point of dropping detid from it.
    cur = conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,text_hash,
        http_status,raw_text)
        VALUES ('H1','https://example.org/update.pdf','2026-06-20T00:00:00+00:00','def',
                200,'page body')""")
    cur = conn.execute("""INSERT INTO detections (scan_id,candidate_id,classification,
        confidence,evidence,rationale,model,classified_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (cur.lastrowid, 'H1', 'red_box_guidance', 0.92,
         '[{"quote":"suburban voters need to read","why":"directive"}]',
         'same box re-detected', 'claude-haiku-4-5', '2026-06-20T00:00:00+00:00'))
    conn.execute("""INSERT INTO reviews (detection_id,reviewer,action,reviewed_at)
                    VALUES (?,?,?,?)""", (cur.lastrowid, "editor", "approve", "2026-06-21T00:00:00+00:00"))
    conn.commit()
    out3 = build_site(conn, tmp_path / "site3", approved_only=True,
                      site_url="https://redboxwatch.org/")
    third = _json.loads((out3 / "feed.json").read_text())["items"][0]
    assert third["id"] == second["id"]
    conn.close()


def test_multi_exhibit_debut_is_not_titled_as_update(tmp_path):
    # Candidates routinely debut with several boxes approved in one bulk
    # review session (the item's FIRST feed appearance). That announcement
    # must not call itself "Updated" — only approvals spanning more than one
    # day read as an update.
    import json as _json
    conn = init_db(tmp_path / "db.sqlite")
    det_id = _seed(conn)
    conn.execute("""INSERT INTO reviews (detection_id,reviewer,action,reviewed_at)
                    VALUES (?,?,?,?)""", (det_id, "editor", "approve", "2026-05-30T09:00:00+00:00"))
    cur = conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,text_hash,
        http_status,raw_text)
        VALUES ('H1','https://example.org/other.pdf','2026-05-29T00:00:00+00:00','def',
                200,'page body')""")
    cur = conn.execute("""INSERT INTO detections (scan_id,candidate_id,classification,
        confidence,evidence,rationale,model,classified_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (cur.lastrowid, 'H1', 'red_box_guidance', 0.9,
         '[{"quote":"suburban voters need to read","why":"directive"}]',
         'second box', 'claude-haiku-4-5', '2026-05-29T00:00:00+00:00'))
    conn.execute("""INSERT INTO reviews (detection_id,reviewer,action,reviewed_at)
                    VALUES (?,?,?,?)""", (cur.lastrowid, "editor", "approve", "2026-05-30T11:00:00+00:00"))
    conn.commit()
    out = build_site(conn, tmp_path / "site", approved_only=True,
                     site_url="https://redboxwatch.org/")
    item = _json.loads((out / "feed.json").read_text())["items"][0]
    assert "Updated" not in item["title"]
    assert "exhibits" not in item["title"]
    assert item["title"].endswith("— red-box guidance found")
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
    cur = conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,text_hash,
        http_status,raw_text)
        VALUES ('H4','https://rita.example/media','2026-07-01T00:00:00+00:00','th',
                200,'txt')""")
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
    cur = conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,text_hash,
        http_status,raw_text)
        VALUES ('H5','https://carla.example/media','2026-07-01T00:00:00+00:00','th',
                200,'txt')""")
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
        cur = conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,text_hash,
            http_status,raw_text) VALUES (?,?,?,?,200,'page body')""",
            (cid, "https://x.example/media", "t", "h" + cid))
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


# ---------------------------------------------------------------------------
# Detection lifecycle: re-detections, removals, and multiple red boxes.

def _add_scan_det(conn, cid, url, text_hash, cls="red_box_guidance", conf=0.9,
                  evidence="[]", fetched="2026-06-01T00:00:00+00:00"):
    cur = conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,text_hash,
        http_status,raw_text) VALUES (?,?,?,?,200,'page body')""",
        (cid, url, fetched, text_hash))
    cur = conn.execute("""INSERT INTO detections (scan_id,candidate_id,classification,
        confidence,evidence,rationale,model,classified_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (cur.lastrowid, cid, cls, conf, evidence, "r", "m", fetched))
    return cur.lastrowid


def _review(conn, det_id, action):
    conn.execute("""INSERT INTO reviews (detection_id,reviewer,action,reviewed_at)
        VALUES (?,?,?,?)""", (det_id, "editor", action, "2026-06-02T00:00:00+00:00"))


def test_approved_finding_survives_unreviewed_redetection(tmp_path):
    # The same red box re-detected after a page tweak (new hash, higher
    # confidence, unreviewed) must NOT displace the approved detection: the
    # candidate stays a published finding — not demoted to pending and dropped
    # from the --approved-only build — and the page shows one exhibit, not two.
    conn = init_db(tmp_path / "db.sqlite")
    det_id = _seed(conn)                              # approved box on /media
    _review(conn, det_id, "approve")
    _add_scan_det(conn, "H1", "https://example.org/media", "def", conf=0.99,
                  fetched="2026-06-10T00:00:00+00:00")   # re-detection, pending
    conn.commit()
    out = build_site(conn, tmp_path / "site", approved_only=True)
    cand = (out / "H1.html").read_text()
    assert "FINDING" in (out / "index.html").read_text()
    assert "pending human review" not in cand.lower()
    assert cand.count('<section class="detection">') == 1
    conn.close()


def test_rejected_top_does_not_bury_approved_finding(tmp_path):
    # Reviewer rejects the higher-confidence flag on one page but approves a
    # real box on another: the approved finding must publish (the old ranking
    # picked the rejected detection and dropped the candidate entirely).
    conn = init_db(tmp_path / "db.sqlite")
    conn.execute("""INSERT INTO candidates (candidate_id,name,office,state,district,
        party,cycle,universe_reason,website_url,url_verified,receipts,created_at,updated_at)
        VALUES ('H1','TEST CANDIDATE','H','NY','12','DEM',2026,'contested_primary',
                'https://example.org',1,2000000,'t','t')""")
    false_pos = _add_scan_det(conn, "H1", "https://example.org/about", "a1", conf=0.99)
    real = _add_scan_det(conn, "H1", "https://example.org/media", "b1", conf=0.70,
                         evidence='[{"quote":"suburban women should hear","why":"directive"}]')
    _review(conn, false_pos, "reject")
    _review(conn, real, "approve")
    conn.commit()
    out = build_site(conn, tmp_path / "site", approved_only=True)
    cand = (out / "H1.html").read_text()
    assert "Test Candidate" in (out / "index.html").read_text()
    assert "suburban women should hear" in cand
    assert "example.org/media" in cand
    assert "example.org/about" not in cand            # rejected flag stays out
    conn.close()


def test_two_page_findings_render_both_exhibits(tmp_path):
    # Genuinely distinct red boxes on two pages: both approved detections render
    # as separate exhibits with their own evidence; the feed still carries one
    # item per candidate.
    conn = init_db(tmp_path / "db.sqlite")
    a = _seed(conn)                                   # /media, quote about younger voters
    b = _add_scan_det(conn, "H1", "https://example.org/priorities", "p1", conf=0.85,
                      evidence='[{"quote":"veterans in the district should know","why":"directive"}]')
    _review(conn, a, "approve")
    _review(conn, b, "approve")
    conn.commit()
    out = build_site(conn, tmp_path / "site", approved_only=True,
                     site_url="https://redboxwatch.org/")
    cand = (out / "H1.html").read_text()
    assert cand.count('<section class="detection">') == 2
    assert "younger voters should see" in cand
    assert "veterans in the district should know" in cand
    assert "Additional page with guidance" in cand
    assert (out / "feed.xml").read_text().count("<item>") == 1
    conn.close()


def test_public_build_never_renders_pending_second_exhibit(tmp_path):
    # Candidate has an approved box on one page and an unreviewed flag on
    # another: the public build shows only the approved exhibit — a pending
    # detection is an unpublished allegation even on a published page.
    conn = init_db(tmp_path / "db.sqlite")
    a = _seed(conn)
    _add_scan_det(conn, "H1", "https://example.org/newpage", "n1", conf=0.9,
                  evidence='[{"quote":"NOT YET REVIEWED SPAN","why":"w"}]')
    _review(conn, a, "approve")
    conn.commit()
    out = build_site(conn, tmp_path / "site", approved_only=True)
    cand = (out / "H1.html").read_text()
    assert cand.count('<section class="detection">') == 1
    assert "NOT YET REVIEWED SPAN" not in cand
    # ...but the review build shows it, marked as pending.
    out2 = build_site(conn, tmp_path / "site2")
    cand2 = (out2 / "H1.html").read_text()
    assert cand2.count('<section class="detection">') == 2
    assert "NOT YET REVIEWED SPAN" in cand2
    assert "pending human review" in cand2.lower()
    conn.close()


def test_removed_guidance_gets_dated_note_and_past_tense(tmp_path):
    # "Used to have a red box, now doesn't": the finding stays on the ledger,
    # but the page discloses the removal with the last-checked date and the
    # meta description shifts to past tense.
    conn = init_db(tmp_path / "db.sqlite")
    det_id = _seed(conn)
    _review(conn, det_id, "approve")
    # Later re-scan of the same page: guidance gone.
    _add_scan_det(conn, "H1", "https://example.org/media", "gone1",
                  cls="no_guidance_detected", conf=0.2,
                  fetched="2026-06-15T00:00:00+00:00")
    conn.commit()
    out = build_site(conn, tmp_path / "site", approved_only=True,
                     site_url="https://redboxwatch.org/")
    cand = (out / "H1.html").read_text()
    assert "FINDING" in (out / "index.html").read_text()   # ledger keeps it
    assert "No longer present." in cand
    assert "June 15, 2026" in cand
    assert "carried a red box" in cand                     # past-tense meta desc
    assert "carries a red box" not in cand
    assert "The guidance has since been removed." in cand
    conn.close()


def test_still_live_guidance_has_no_removal_note(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    det_id = _seed(conn)
    _review(conn, det_id, "approve")
    conn.commit()
    out = build_site(conn, tmp_path / "site", approved_only=True,
                     site_url="https://redboxwatch.org/")
    cand = (out / "H1.html").read_text()
    assert "No longer present." not in cand
    assert "carries a red box" in cand
    conn.close()


def test_feed_guid_is_stable_per_candidate(tmp_path):
    # The feed guid must not embed the detection id: a re-approved re-detection
    # of the SAME box (same URL/body, fresh detection row) must not change it,
    # or feed readers would announce the same finding twice. A genuinely NEW
    # exhibit SHOULD change it — test_new_exhibit_reannounces_feed_item.
    import re as _re
    conn = init_db(tmp_path / "db.sqlite")
    det_id = _seed(conn)
    _review(conn, det_id, "approve")
    conn.commit()
    out = build_site(conn, tmp_path / "site", approved_only=True,
                     site_url="https://redboxwatch.org/")
    guid = lambda p: _re.search(r'<guid isPermaLink="false">([^<]+)</guid>',
                                (p / "feed.xml").read_text()).group(1)
    first = guid(out)
    assert first.startswith("H1")
    re_det = _add_scan_det(conn, "H1", "https://example.org/media", "abc",
                           evidence='[{"quote":"younger voters should see","why":"directive"}]')
    _review(conn, re_det, "approve")
    conn.commit()
    out2 = build_site(conn, tmp_path / "site2", approved_only=True,
                      site_url="https://redboxwatch.org/")
    assert guid(out2) == first
    conn.close()


def test_alias_urls_collapse_to_one_exhibit(tmp_path):
    # Catch-all routes serving the SAME body under several URLs (re-detected
    # across separate scan runs, so the pipeline's in-run dedup never saw them
    # together) must render as ONE exhibit with the aliases listed — not as a
    # stack of near-identical red boxes.
    conn = init_db(tmp_path / "db.sqlite")
    a = _seed(conn)                                   # /media, hash 'abc'
    b = _add_scan_det(conn, "H1", "https://example.org/press", "abc",
                      evidence='[{"quote":"younger voters should see","why":"directive"}]')
    c = _add_scan_det(conn, "H1", "https://example.org/media-kit", "abc",
                      evidence='[{"quote":"younger voters should see","why":"directive"}]')
    for det in (a, b, c):
        _review(conn, det, "approve")
    conn.commit()
    out = build_site(conn, tmp_path / "site", approved_only=True)
    cand = (out / "H1.html").read_text()
    assert cand.count('<section class="detection">') == 1
    assert "The same page body is also served at" in cand
    assert "example.org/press" in cand
    assert "example.org/media-kit" in cand
    conn.close()


def test_alias_exhibit_not_gone_while_any_url_still_serves_it(tmp_path):
    # A body that moved off one alias but still lives at another is NOT
    # "no longer present" — the removal note requires every URL to have
    # dropped it.
    conn = init_db(tmp_path / "db.sqlite")
    a = _seed(conn)                                   # /media, hash 'abc'
    b = _add_scan_det(conn, "H1", "https://example.org/press", "abc")
    _review(conn, a, "approve")
    _review(conn, b, "approve")
    # /media re-scanned: body gone there; /press unchecked since detection.
    _add_scan_det(conn, "H1", "https://example.org/media", "clean",
                  cls="no_guidance_detected", conf=0.1,
                  fetched="2026-06-20T00:00:00+00:00")
    conn.commit()
    out = build_site(conn, tmp_path / "site", approved_only=True)
    cand = (out / "H1.html").read_text()
    assert "No longer present." not in cand
    conn.close()


# ---------------------------------------------------------------------------
# Per-exhibit timelines and the review-gated change history.

def test_exhibit_timeline_shows_detection_and_removal(tmp_path):
    # Every exhibit carries a timeline strip; a removal appears there (from the
    # take-down event) as well as in the prose note.
    conn = init_db(tmp_path / "db.sqlite")
    det_id = _seed(conn)                                        # classified 05-29
    _review(conn, det_id, "approve")
    _add_scan_det(conn, "H1", "https://example.org/media", "gone1",
                  cls="no_guidance_detected", conf=0.2,
                  fetched="2026-06-15T00:00:00+00:00")
    conn.execute("""INSERT INTO change_events (candidate_id,url,event_type,
        prev_classification,new_classification,detected_at)
        VALUES ('H1','https://example.org/media','take_down',
                'red_box_guidance','no_guidance_detected','2026-06-15T00:00:00+00:00')""")
    conn.commit()
    out = build_site(conn, tmp_path / "site", approved_only=True)
    cand = (out / "H1.html").read_text()
    assert 'aria-label="Detection timeline"' in cand
    assert "First detected" in cand and "May 29, 2026" in cand
    assert "Guidance removed" in cand and "June 15, 2026" in cand
    # take_down event present -> no duplicate state-derived "No longer present"
    # timeline entry, but the prose note still renders.
    assert cand.count("No longer present") == 1                 # gone-note only
    conn.close()


def test_timeline_distinguishes_page_update_from_guidance_revision(tmp_path):
    # Two 'modified' events: one where the quoted spans survived verbatim in
    # both versions (page updated) and one where the spans changed (revised).
    conn = init_db(tmp_path / "db.sqlite")
    conn.execute("""INSERT INTO candidates (candidate_id,name,office,state,district,
        party,cycle,universe_reason,website_url,url_verified,receipts,created_at,updated_at)
        VALUES ('H1','TEST CANDIDATE','H','NY','12','DEM',2026,'contested_primary',
                'https://example.org',1,2000000,'t','t')""")

    def scan_det(url, h, text, ev, when, conf=0.9):
        cur = conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,
            http_status,raw_text,text_hash) VALUES ('H1',?,?,200,?,?)""",
            (url, when, text, h))
        sid = cur.lastrowid
        conn.execute("""INSERT INTO detections (scan_id,candidate_id,classification,
            confidence,evidence,rationale,model,classified_at)
            VALUES (?,'H1','red_box_guidance',?,?,?,'m',?)""",
            (sid, conf, ev, "r", when))
        return sid

    # Page A: news item changes around identical guidance -> "Page updated".
    a1 = scan_det("https://example.org/media", "a1", "NEWS ONE. voters should see this ad",
                  '[{"quote":"voters should see this ad","why":"w"}]',
                  "2026-06-01T00:00:00+00:00")
    a2 = scan_det("https://example.org/media", "a2", "NEWS TWO. voters should see this ad",
                  '[{"quote":"voters should see this ad","why":"w"}]',
                  "2026-06-10T00:00:00+00:00", conf=0.91)
    conn.execute("""INSERT INTO change_events (candidate_id,url,event_type,prev_scan_id,
        new_scan_id,prev_classification,new_classification,detected_at)
        VALUES ('H1','https://example.org/media','modified',?,?,
                'red_box_guidance','red_box_guidance','2026-06-10T00:00:00+00:00')""",
        (a1, a2))
    # Page B: the guidance itself changes -> "Guidance revised".
    b1 = scan_det("https://example.org/plan", "b1", "seniors should hear about Medicare",
                  '[{"quote":"seniors should hear about Medicare","why":"w"}]',
                  "2026-06-01T00:00:00+00:00")
    b2 = scan_det("https://example.org/plan", "b2", "veterans should hear about the GI bill",
                  '[{"quote":"veterans should hear about the GI bill","why":"w"}]',
                  "2026-06-12T00:00:00+00:00", conf=0.91)
    conn.execute("""INSERT INTO change_events (candidate_id,url,event_type,prev_scan_id,
        new_scan_id,prev_classification,new_classification,detected_at)
        VALUES ('H1','https://example.org/plan','modified',?,?,
                'red_box_guidance','red_box_guidance','2026-06-12T00:00:00+00:00')""",
        (b1, b2))
    for r in conn.execute("SELECT detection_id FROM detections"):
        _review(conn, r["detection_id"], "approve")
    conn.commit()
    out = build_site(conn, tmp_path / "site", approved_only=True)
    cand = (out / "H1.html").read_text()
    assert "Page updated" in cand
    assert "Guidance revised" in cand
    conn.close()


def test_alias_events_collapse_to_one_timeline_entry(tmp_path):
    # The same site update fires one 'modified' event per alias URL; the
    # exhibit's timeline must show a single entry, not one per URL.
    conn = init_db(tmp_path / "db.sqlite")
    a = _seed(conn)
    b = _add_scan_det(conn, "H1", "https://example.org/press", "abc")
    _review(conn, a, "approve")
    _review(conn, b, "approve")
    for url in ("https://example.org/media", "https://example.org/press"):
        conn.execute("""INSERT INTO change_events (candidate_id,url,event_type,
            prev_classification,new_classification,detected_at)
            VALUES ('H1',?,'modified','red_box_guidance','red_box_guidance',
                    '2026-06-20T00:00:00+00:00')""", (url,))
    conn.commit()
    out = build_site(conn, tmp_path / "site", approved_only=True)
    cand = (out / "H1.html").read_text()
    assert cand.count('class="tl-changed"') == 1
    conn.close()


def test_change_history_section_is_review_build_only(tmp_path):
    # The raw event log can name URLs whose detections are pending/rejected,
    # so it renders in the review console but never on public pages.
    conn = init_db(tmp_path / "db.sqlite")
    det_id = _seed(conn)
    _review(conn, det_id, "approve")
    # A put-up event for a PENDING detection on another page.
    _add_scan_det(conn, "H1", "https://example.org/secret-page", "s1")
    conn.execute("""INSERT INTO change_events (candidate_id,url,event_type,
        prev_classification,new_classification,detected_at)
        VALUES ('H1','https://example.org/secret-page','put_up',
                'no_guidance_detected','red_box_guidance','2026-06-25T00:00:00+00:00')""")
    conn.commit()
    pub = build_site(conn, tmp_path / "site", approved_only=True)
    cand_pub = (pub / "H1.html").read_text()
    assert "Change history" not in cand_pub
    assert "secret-page" not in cand_pub          # pending URL never leaks
    rev = build_site(conn, tmp_path / "site2")
    cand_rev = (rev / "H1.html").read_text()
    assert "Change history" in cand_rev
    assert "review console only" in cand_rev
    assert "secret-page" in cand_rev
    conn.close()


def test_contradictory_sibling_verdict_is_not_a_removal(tmp_path):
    # Legacy scans classified one body under several URLs, and the classifier
    # sometimes contradicted itself on the identical text. An unreviewed
    # no-guidance verdict on the same body must not overrule the approved
    # detection and fabricate a "No longer present" note (the page was never
    # re-scanned).
    conn = init_db(tmp_path / "db.sqlite")
    det_id = _seed(conn)                              # /media, hash 'abc', approved below
    _review(conn, det_id, "approve")
    # Same body under an alias URL, same day, classified no-guidance (legacy
    # pre-dedup contradiction) — never reviewed.
    _add_scan_det(conn, "H1", "https://example.org/blank", "abc",
                  cls="no_guidance_detected", conf=0.95)
    conn.commit()
    out = build_site(conn, tmp_path / "site", approved_only=True)
    cand = (out / "H1.html").read_text()
    assert "No longer present" not in cand
    assert "carries a red box" in cand or "TEST CANDIDATE" in cand
    conn.close()


def test_error_scan_does_not_mark_exhibit_gone(tmp_path):
    # An approved finding whose page later 403s (bot-block) or serves a bot
    # challenge: the crawler being blocked is not a removal. The exhibit must
    # stay "live" — only a usable scan (or confirmed take_down event) may
    # flip it to gone.
    conn = init_db(tmp_path / "db.sqlite")
    det_id = _seed(conn)
    _review(conn, det_id, "approve")
    conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,text_hash,
        http_status,raw_text)
        VALUES ('H1','https://example.org/media','2026-06-15T00:00:00+00:00',
                'errhash',403,'')""")
    conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,text_hash,
        http_status,raw_text)
        VALUES ('H1','https://example.org/media','2026-06-16T00:00:00+00:00',
                'chal','202','Just a moment... Checking your browser.')""")
    conn.commit()
    out = build_site(conn, tmp_path / "site", approved_only=True,
                     site_url="https://redboxwatch.org/")
    cand = (out / "H1.html").read_text()
    assert "No longer present." not in cand
    assert "carries a red box" in cand
    conn.close()


def test_confirmed_take_down_event_marks_exhibit_gone(tmp_path):
    # A confirmed-disappearance take_down (recorded by the pipeline after two
    # consecutive 404s) has no usable scan of its own; the event itself must
    # flip the exhibit to gone, dated at the event.
    conn = init_db(tmp_path / "db.sqlite")
    det_id = _seed(conn)
    _review(conn, det_id, "approve")
    cur = conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,text_hash,
        http_status,raw_text)
        VALUES ('H1','https://example.org/media','2026-06-20T00:00:00+00:00',
                'e404',404,'')""")
    conn.execute("""INSERT INTO change_events (candidate_id,url,event_type,
        prev_scan_id,new_scan_id,prev_classification,new_classification,detected_at)
        VALUES ('H1','https://example.org/media','take_down',1,?,
                'red_box_guidance','no_guidance_detected','2026-06-20T00:00:00+00:00')""",
        (cur.lastrowid,))
    conn.commit()
    out = build_site(conn, tmp_path / "site", approved_only=True,
                     site_url="https://redboxwatch.org/")
    cand = (out / "H1.html").read_text()
    assert "No longer present." in cand
    assert "June 20, 2026" in cand
    conn.close()


def test_build_sweeps_evidence_of_rejected_detections(tmp_path):
    # site/evidence must mirror what THIS build references. A screenshot
    # copied while a detection was approved must disappear from the output
    # directory once the detection is rejected — otherwise the artifact of a
    # rejected allegation stays deployed and publicly fetchable.
    conn = init_db(tmp_path / "db.sqlite")
    det_id = _seed(conn)
    shot = tmp_path / "shot123.webp"
    shot.write_bytes(b"RIFFfakeWEBP")
    conn.execute("""INSERT INTO archives (detection_id,candidate_id,url,archived_at,
        screenshot_path) VALUES (?,?,?,?,?)""",
        (det_id, "H1", "https://example.org/media",
         "2026-05-29T00:00:00+00:00", str(shot)))
    _review(conn, det_id, "approve")
    conn.commit()
    out = build_site(conn, tmp_path / "site", approved_only=True)
    assert (out / "evidence" / "shot123.webp").exists()

    _review(conn, det_id, "reject")
    conn.commit()
    build_site(conn, tmp_path / "site", approved_only=True)
    assert not (out / "evidence" / "shot123.webp").exists()


def test_public_build_discloses_coverage_gap_and_full_universe(tmp_path):
    # The approved-only build must count the whole universe (not the rendered
    # subset) and keep the coverage-gap disclosure; candidates with no
    # resolved site get a public page (it IS the gap disclosure), while a
    # pending detection's candidate stays out entirely.
    conn = init_db(tmp_path / "db.sqlite")
    det_id = _seed(conn)                       # H1: positive
    _review(conn, det_id, "approve")
    conn.execute("""INSERT INTO candidates (candidate_id,name,office,state,district,
        party,cycle,universe_reason,website_url,url_source,url_verified,receipts,
        created_at,updated_at)
        VALUES ('H2','GAP, GRETA','H','OH','03','DEM',2026,'contested_primary',
                NULL,'none',0,300000,'t','t')""")
    conn.execute("""INSERT INTO candidates (candidate_id,name,office,state,district,
        party,cycle,universe_reason,website_url,url_verified,receipts,
        created_at,updated_at)
        VALUES ('H3','PENDING, PAULA','H','PA','07','DEM',2026,'contested_primary',
                'https://paula.example',1,400000,'t','t')""")
    cur = conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,text_hash,
        http_status,raw_text)
        VALUES ('H3','https://paula.example/media','2026-06-01T00:00:00+00:00','ph',
                200,'page body')""")
    conn.execute("""INSERT INTO detections (scan_id,candidate_id,classification,
        confidence,evidence,rationale,model,classified_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (cur.lastrowid, "H3", "red_box_guidance", 0.9, "[]", "r", "m",
         "2026-06-01T00:00:00+00:00"))
    conn.commit()

    out = build_site(conn, tmp_path / "site", approved_only=True)
    index = (out / "index.html").read_text()
    assert "Coverage gap:" in index
    assert "1 have no campaign site resolved" in index
    assert "<b>3</b>" in index                      # full universe, all 3 tracked
    assert (out / "H2.html").exists()               # gap page is public
    assert "No campaign site found" in (out / "H2.html").read_text()
    assert not (out / "H3.html").exists()           # pending stays unpublished
    assert "PENDING, PAULA" not in index
    assert "Pending human review" not in index      # no pending stat publicly
    conn.close()


def test_index_row_fec_name_with_display_data_attribute(tmp_path):
    # The visible cell prints the official FEC "LAST, FIRST" form (the ledger
    # formality is deliberate, and it sorts by surname natively); the display
    # form rides along in data-name so the filter still matches what people
    # type ("haley stevens").
    conn = init_db(tmp_path / "db.sqlite")
    _seed(conn)
    conn.execute("UPDATE candidates SET name='STEVENS, HALEY' WHERE candidate_id='H1'")
    conn.commit()
    out = build_site(conn, tmp_path / "site")
    index = (out / "index.html").read_text()
    assert ">STEVENS, HALEY<" in index               # visible cell text
    assert 'data-name="Haley Stevens"' in index      # filter matches typed form
    # Working-notes columns stay off the home table.
    assert ">Conf.<" not in index and ">Pages<" not in index
    conn.close()


# --- Version history: earlier revisions of a red box are browsable ----------

def _seed_versioned_exhibit(conn, tmp_path):
    """One page (/media) that served three successive guidance bodies, each
    detected and archived at its time: v1 (approved), v2 (never reviewed),
    current v3 (approved)."""
    conn.execute("""INSERT INTO candidates (candidate_id,name,office,state,district,
        party,cycle,universe_reason,website_url,url_verified,receipts,created_at,updated_at)
        VALUES ('H1','REVISED, RITA','H','NY','12','DEM',2026,'contested_primary',
                'https://example.org',1,2000000,'t','t')""")

    def version(hash_, when, quote, shot_name):
        cur = conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,text_hash,
            http_status,raw_text) VALUES ('H1','https://example.org/media',?,?,200,?)""",
            (when, hash_, f"page body {quote}"))
        cur = conn.execute("""INSERT INTO detections (scan_id,candidate_id,classification,
            confidence,evidence,rationale,model,classified_at) VALUES (?,?,?,?,?,?,?,?)""",
            (cur.lastrowid, "H1", "red_box_guidance", 0.9,
             f'[{{"quote":"{quote}","why":"directive"}}]', "r", "m", when))
        det = cur.lastrowid
        shot = tmp_path / shot_name
        shot.write_bytes(b"RIFFfakeWEBP")
        conn.execute("""INSERT INTO archives (detection_id,candidate_id,url,archived_at,
            screenshot_path,wayback_url) VALUES (?,?,?,?,?,?)""",
            (det, "H1", "https://example.org/media", when, str(shot),
             f"https://web.archive.org/web/x/{shot_name}"))
        return det

    d1 = version("v1hash", "2026-06-01T00:00:00+00:00",
                 "younger voters should see this on TV", "v1shot.webp")
    d2 = version("v2hash", "2026-06-15T00:00:00+00:00",
                 "younger voters should see this on TV and mail", "v2shot.webp")
    d3 = version("v3hash", "2026-07-01T00:00:00+00:00",
                 "suburban women need to hear this on CTV", "v3shot.webp")
    _review(conn, d1, "approve")
    _review(conn, d3, "approve")           # v2 never reviewed
    conn.commit()
    return d1, d2, d3


def test_exhibit_lists_earlier_versions_with_archives(tmp_path):
    # Review build: both earlier versions render, each with its own quotes,
    # lifespan, archived screenshot, and a diff line against its successor;
    # the never-reviewed one is marked pending. The timeline's revision
    # entries link into the version block.
    conn = init_db(tmp_path / "db.sqlite")
    _seed_versioned_exhibit(conn, tmp_path)
    out = build_site(conn, tmp_path / "site")
    cand = (out / "H1.html").read_text()
    assert "Earlier versions of this guidance (2)" in cand
    assert "younger voters should see this on TV" in cand         # v1 quote
    assert "younger voters should see this on TV and mail" in cand  # v2 quote
    assert 'src="evidence/v1shot.webp"' in cand                   # v1 archive copied
    assert 'src="evidence/v2shot.webp"' in cand
    assert (out / "evidence" / "v1shot.webp").exists()
    assert "The next revision added" in cand                      # quote diff
    assert "pending human review" in cand                         # v2 marked
    assert 'href="#versions-' in cand                             # timeline anchor
    conn.close()


def test_public_build_shows_only_approved_versions(tmp_path):
    # Public: v1 (approved) appears; v2 (never reviewed) is an unpublished
    # allegation — no quote, no screenshot in the output directory.
    conn = init_db(tmp_path / "db.sqlite")
    _seed_versioned_exhibit(conn, tmp_path)
    out = build_site(conn, tmp_path / "site", approved_only=True)
    cand = (out / "H1.html").read_text()
    assert "Earlier version of this guidance (1)" in cand
    assert "younger voters should see this on TV" in cand
    assert "on TV and mail" not in cand
    assert (out / "evidence" / "v1shot.webp").exists()
    assert not (out / "evidence" / "v2shot.webp").exists()        # swept/not copied
    conn.close()


def test_single_version_exhibit_has_no_version_block(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    det_id = _seed(conn)
    _review(conn, det_id, "approve")
    conn.commit()
    out = build_site(conn, tmp_path / "site", approved_only=True)
    assert "Earlier version" not in (out / "H1.html").read_text()
    conn.close()
