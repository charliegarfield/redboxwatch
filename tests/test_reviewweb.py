"""Offline tests for the web review console (spec §3.7 human gate, phase 5).

The app is plain WSGI, so every test drives it by calling the app with a
hand-built environ — no server, no network, no browser.
"""
from __future__ import annotations

import io

from redbox.db import init_db
from redbox.publisher import POSITIVE_LABEL
from redbox.reviewweb import ReviewApp, pending_groups, record_review


# ---------------------------------------------------------------------------
# WSGI test client

def request(app, method, path, *, body=None, cookie=None, query=""):
    """Call the WSGI app directly; returns (status, headers-dict, body-bytes)."""
    body = body or b""
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
    }
    if cookie:
        environ["HTTP_COOKIE"] = cookie
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = headers

    out = b"".join(app(environ, start_response))
    return captured["status"], dict(captured["headers"]), out


def get(app, path, **kw):
    return request(app, "GET", path, **kw)


def post(app, path, form: str, **kw):
    return request(app, "POST", path, body=form.encode(), **kw)


# ---------------------------------------------------------------------------
# Seeding (same shapes as tests/test_publisher.py)

def _seed_candidate(conn, cid="H1", name="TEST CANDIDATE", url_verified=1):
    conn.execute("""INSERT INTO candidates (candidate_id,name,office,state,district,
        party,cycle,universe_reason,website_url,url_source,url_verified,receipts,
        created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (cid, name, "H", "NY", "12", "DEM", 2026, "contested_primary",
         "https://example.org", "search", url_verified, 2000000, "t", "t"))


def _seed_detection(conn, cid="H1", *, url="https://example.org/media",
                    text_hash="abc", classification="red_box_guidance",
                    confidence=0.95, raw_text="younger voters should see this ad"):
    cur = conn.execute("""INSERT INTO scans (candidate_id,url,fetched_at,text_hash,raw_text)
        VALUES (?,?,?,?,?)""",
        (cid, url, "2026-05-29T00:00:00+00:00", text_hash, raw_text))
    sid = cur.lastrowid
    cur = conn.execute("""INSERT INTO detections (scan_id,candidate_id,classification,
        confidence,evidence,rationale,model,classified_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (sid, cid, classification, confidence,
         '[{"quote":"younger voters should see","why":"directive"}]',
         'directive guidance', 'claude-sonnet-4-6', '2026-05-29T00:00:00+00:00'))
    conn.commit()
    return cur.lastrowid


def _app(tmp_path, seed=True, **det_kw):
    db = tmp_path / "db.sqlite"
    conn = init_db(db)
    det_id = None
    if seed:
        _seed_candidate(conn)
        det_id = _seed_detection(conn, **det_kw)
    conn.close()
    return ReviewApp(db), det_id


# ---------------------------------------------------------------------------
# Queue

def test_queue_lists_pending_detection(tmp_path):
    app, det_id = _app(tmp_path)
    status, headers, body = get(app, "/")
    page = body.decode()
    assert status == "200 OK"
    assert "text/html" in headers["Content-Type"]
    assert "TEST CANDIDATE" in page
    assert f"/detection/{det_id}" in page
    assert "0.95" in page
    assert "1 finding(s) pending review" in page


def test_queue_groups_template_aliases(tmp_path):
    db = tmp_path / "db.sqlite"
    conn = init_db(db)
    _seed_candidate(conn)
    _seed_detection(conn, url="https://example.org/media", text_hash="same")
    _seed_detection(conn, url="https://example.org/press", text_hash="same")
    conn.close()
    app = ReviewApp(db)
    _, _, body = get(app, "/")
    page = body.decode()
    # One reviewable finding, two detections, alias tag shown.
    assert "1 finding(s) pending review" in page
    assert "2 detection(s)" in page
    assert "+1 alias" in page


def test_negatives_never_enter_the_queue(tmp_path):
    app, _ = _app(tmp_path, classification="no_guidance_detected")
    _, _, body = get(app, "/")
    assert "Queue clear" in body.decode()


def test_queue_uses_latest_review_not_any_review(tmp_path):
    # needs_more then approve: the newest decision wins, so it is NOT pending.
    db = tmp_path / "db.sqlite"
    conn = init_db(db)
    _seed_candidate(conn)
    det_id = _seed_detection(conn)
    conn.execute("""INSERT INTO reviews (detection_id,action,reviewed_at)
                    VALUES (?,?,?)""", (det_id, "needs_more", "2026-06-01T00:00:00+00:00"))
    conn.execute("""INSERT INTO reviews (detection_id,action,reviewed_at)
                    VALUES (?,?,?)""", (det_id, "approve", "2026-06-02T00:00:00+00:00"))
    conn.commit()
    assert pending_groups(conn) == []
    conn.close()


def test_queue_escapes_candidate_name(tmp_path):
    db = tmp_path / "db.sqlite"
    conn = init_db(db)
    _seed_candidate(conn, name="<script>alert(1)</script>")
    _seed_detection(conn)
    conn.close()
    app = ReviewApp(db)
    _, _, body = get(app, "/")
    assert b"<script>alert(1)</script>" not in body
    assert b"&lt;script&gt;" in body


# ---------------------------------------------------------------------------
# Detail page

def test_detail_shows_label_evidence_and_pending_banner(tmp_path):
    app, det_id = _app(tmp_path)
    _, _, body = get(app, f"/detection/{det_id}")
    page = body.decode()
    assert POSITIVE_LABEL in page                      # exact §3.7a language
    assert "pending human review" in page.lower()
    assert "younger voters should see" in page          # quoted evidence span
    assert "directive guidance" in page                 # rationale
    assert 'name="action"' in page                      # the decision form


def test_detail_warns_on_unverified_url(tmp_path):
    db = tmp_path / "db.sqlite"
    conn = init_db(db)
    _seed_candidate(conn, url_verified=0)
    det_id = _seed_detection(conn)
    conn.close()
    app = ReviewApp(db)
    _, _, body = get(app, f"/detection/{det_id}")
    assert "Unverified attribution" in body.decode()
    # A verified candidate gets no warning.
    app2, det2 = _app(tmp_path / "verified")
    _, _, body2 = get(app2, f"/detection/{det2}")
    assert "Unverified attribution" not in body2.decode()


def test_detail_shows_decided_banner_after_review(tmp_path):
    app, det_id = _app(tmp_path)
    post(app, f"/detection/{det_id}", "action=approve&reviewer=editor")
    _, _, body = get(app, f"/detection/{det_id}")
    page = body.decode()
    assert "Already decided" in page
    assert "Review history" in page


def test_unknown_detection_404s(tmp_path):
    app, _ = _app(tmp_path)
    status, _, _ = get(app, "/detection/999")
    assert status == "404 Not Found"
    status, _, _ = get(app, "/nope")
    assert status == "404 Not Found"


# ---------------------------------------------------------------------------
# Recording decisions

def test_approve_records_review_and_clears_queue(tmp_path):
    app, det_id = _app(tmp_path)
    status, headers, _ = post(app, f"/detection/{det_id}",
                              "action=approve&reviewer=editor&notes=clear+red+box")
    assert status == "303 See Other"
    assert headers["Location"].startswith("/?done=")     # queue is now empty
    conn = init_db(tmp_path / "db.sqlite")
    row = conn.execute("SELECT * FROM reviews WHERE detection_id=?", (det_id,)).fetchone()
    assert row["action"] == "approve"
    assert row["reviewer"] == "editor"
    assert row["notes"] == "clear red box"
    assert pending_groups(conn) == []
    conn.close()
    _, _, body = get(app, "/")
    assert "Queue clear" in body.decode()


def test_group_review_covers_all_aliases(tmp_path):
    db = tmp_path / "db.sqlite"
    conn = init_db(db)
    _seed_candidate(conn)
    d1 = _seed_detection(conn, url="https://example.org/media", text_hash="same")
    d2 = _seed_detection(conn, url="https://example.org/press", text_hash="same")
    conn.close()
    app = ReviewApp(db)
    status, headers, _ = post(app, f"/detection/{d1}", "action=reject&group=1")
    assert status == "303 See Other"
    assert "n=2" in headers["Location"]
    conn = init_db(db)
    actions = {r["detection_id"]: r["action"]
               for r in conn.execute("SELECT detection_id, action FROM reviews")}
    assert actions == {d1: "reject", d2: "reject"}
    conn.close()


def test_without_group_flag_only_one_alias_reviewed(tmp_path):
    db = tmp_path / "db.sqlite"
    conn = init_db(db)
    _seed_candidate(conn)
    d1 = _seed_detection(conn, url="https://example.org/media", text_hash="same")
    _seed_detection(conn, url="https://example.org/press", text_hash="same")
    conn.close()
    app = ReviewApp(db)
    post(app, f"/detection/{d1}", "action=reject")
    conn = init_db(db)
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1
    conn.close()


def test_invalid_action_is_400_and_records_nothing(tmp_path):
    app, det_id = _app(tmp_path)
    status, _, _ = post(app, f"/detection/{det_id}", "action=publish")
    assert status == "400 Bad Request"
    status, _, _ = post(app, f"/detection/{det_id}", "notes=missing+action")
    assert status == "400 Bad Request"
    conn = init_db(tmp_path / "db.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    conn.close()


def test_post_to_unknown_detection_404s(tmp_path):
    app, _ = _app(tmp_path)
    status, _, _ = post(app, "/detection/999", "action=approve")
    assert status == "404 Not Found"


def test_redirects_to_next_pending_then_queue(tmp_path):
    db = tmp_path / "db.sqlite"
    conn = init_db(db)
    _seed_candidate(conn, cid="H1", name="ALPHA")
    _seed_candidate(conn, cid="H2", name="BETA")
    d1 = _seed_detection(conn, cid="H1", text_hash="a", confidence=0.95)
    d2 = _seed_detection(conn, cid="H2", text_hash="b", confidence=0.80)
    conn.close()
    app = ReviewApp(db)
    _, headers, _ = post(app, f"/detection/{d1}", "action=approve")
    assert headers["Location"].startswith(f"/detection/{d2}?done={d1}")
    _, headers, _ = post(app, f"/detection/{d2}", "action=reject")
    assert headers["Location"].startswith("/?done=")


def test_needs_more_does_not_bounce_back_to_itself(tmp_path):
    # needs_more keeps the detection pending; "next" must still move on.
    app, det_id = _app(tmp_path)
    _, headers, _ = post(app, f"/detection/{det_id}", "action=needs_more")
    assert headers["Location"].startswith("/?done=")
    conn = init_db(tmp_path / "db.sqlite")
    assert len(pending_groups(conn)) == 1               # still in the queue
    conn.close()


def test_reviewer_cookie_round_trips(tmp_path):
    app, det_id = _app(tmp_path)
    _, headers, _ = post(app, f"/detection/{det_id}", "action=needs_more&reviewer=charlie")
    assert "reviewer=charlie" in headers["Set-Cookie"]
    _, _, body = get(app, f"/detection/{det_id}", cookie="reviewer=charlie")
    assert 'value="charlie"' in body.decode()


def test_done_banner_confirms_the_decision(tmp_path):
    app, det_id = _app(tmp_path)
    _, _, body = get(app, "/", query=f"done={det_id}&as=approve&n=3")
    page = body.decode()
    assert f"Detection #{det_id} approved" in page
    assert "+2 identical alias page(s)" in page


def test_record_review_rejects_bad_action():
    import pytest
    with pytest.raises(ValueError):
        record_review(None, 1, "publish")


# ---------------------------------------------------------------------------
# Keyboard shortcuts (wiring only — no browser in the offline suite)

def test_detail_has_keyboard_shortcuts(tmp_path):
    app, det_id = _app(tmp_path)
    _, _, body = get(app, f"/detection/{det_id}")
    page = body.decode()
    # The decision keys are scripted and hinted inline.
    assert "acts={a:'approve',r:'reject',n:'needs_more'}" in page
    assert "requestSubmit" in page
    assert "<kbd>a</kbd>" in page and "<kbd>r</kbd>" in page and "<kbd>n</kbd>" in page
    # Shortcuts must not fire while typing in a form field.
    assert "t==='INPUT'||t==='TEXTAREA'" in page
    # No alias group here, so no g hint.
    assert "<kbd>g</kbd>" not in page


def test_detail_group_shortcut_only_with_aliases(tmp_path):
    db = tmp_path / "db.sqlite"
    conn = init_db(db)
    _seed_candidate(conn)
    d1 = _seed_detection(conn, url="https://example.org/media", text_hash="same")
    _seed_detection(conn, url="https://example.org/press", text_hash="same")
    conn.close()
    app = ReviewApp(db)
    _, _, body = get(app, f"/detection/{d1}")
    assert "<kbd>g</kbd>" in body.decode()


def test_queue_has_navigation_shortcuts_only_when_pending(tmp_path):
    app, _ = _app(tmp_path)
    _, _, body = get(app, "/")
    page = body.decode()
    assert "kbd-sel" in page                       # j/k selection script
    assert "<kbd>j</kbd>" in page
    # An empty queue has nothing to navigate — no script, no hint.
    app2, _ = _app(tmp_path / "empty", seed=False)
    _, _, body2 = get(app2, "/")
    assert "kbd-sel" not in body2.decode()


# ---------------------------------------------------------------------------
# Evidence serving

def _seed_archive(conn, det_id, tmp_path, *, with_html=True):
    shot = tmp_path / "shot.webp"
    shot.write_bytes(b"RIFFfakewebp")
    html = tmp_path / "page.html"
    if with_html:
        html.write_text("<html><script>alert(1)</script></html>")
    conn.execute("""INSERT INTO archives (candidate_id,detection_id,url,
        screenshot_path,html_path,archived_at) VALUES (?,?,?,?,?,?)""",
        ("H1", det_id, "https://example.org/media", str(shot),
         str(html) if with_html else None, "2026-05-29T00:00:00+00:00"))
    conn.commit()
    return conn.execute("SELECT MAX(archive_id) FROM archives").fetchone()[0]


def test_screenshot_served_with_image_type(tmp_path):
    db = tmp_path / "db.sqlite"
    conn = init_db(db)
    _seed_candidate(conn)
    det_id = _seed_detection(conn)
    aid = _seed_archive(conn, det_id, tmp_path)
    conn.close()
    app = ReviewApp(db)
    status, headers, body = get(app, f"/evidence/{aid}/screenshot")
    assert status == "200 OK"
    assert headers["Content-Type"] == "image/webp"
    assert body == b"RIFFfakewebp"
    # And the detail page embeds it.
    _, _, page = get(app, f"/detection/{det_id}")
    assert f"/evidence/{aid}/screenshot" in page.decode()


def test_archived_html_is_never_served_as_html(tmp_path):
    # Archived page scripts must not execute in the reviewer's browser.
    db = tmp_path / "db.sqlite"
    conn = init_db(db)
    _seed_candidate(conn)
    det_id = _seed_detection(conn)
    aid = _seed_archive(conn, det_id, tmp_path)
    conn.close()
    app = ReviewApp(db)
    status, headers, _ = get(app, f"/evidence/{aid}/html")
    assert status == "200 OK"
    assert headers["Content-Type"].startswith("text/plain")


def test_missing_artifact_404s(tmp_path):
    db = tmp_path / "db.sqlite"
    conn = init_db(db)
    _seed_candidate(conn)
    det_id = _seed_detection(conn)
    aid = _seed_archive(conn, det_id, tmp_path, with_html=False)
    conn.close()
    app = ReviewApp(db)
    status, _, _ = get(app, "/evidence/999/screenshot")     # no such archive row
    assert status == "404 Not Found"
    status, _, _ = get(app, f"/evidence/{aid}/html")        # column is NULL
    assert status == "404 Not Found"
    status, _, _ = get(app, f"/evidence/{aid}/secret")      # kind not whitelisted
    assert status == "404 Not Found"


def test_styles_served(tmp_path):
    app, _ = _app(tmp_path, seed=False)
    status, headers, body = get(app, "/styles.css")
    assert status == "200 OK"
    assert "text/css" in headers["Content-Type"]
    assert b"review-form" in body


# ---------------------------------------------------------------------------
# Website triage queue (/urls)

def _seed_no_url_candidate(conn, cid="H7", name="NOSITE, NORA", receipts=300000):
    conn.execute("""INSERT INTO candidates (candidate_id,name,office,state,district,
        party,cycle,universe_reason,website_url,url_source,url_verified,receipts,
        created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,NULL,'none',0,?,'t','t')""",
        (cid, name, "H", "NC", "04", "REP", 2026, "contested_general", receipts))
    conn.commit()


def _url_app(tmp_path):
    db = tmp_path / "db.sqlite"
    conn = init_db(db)
    _seed_no_url_candidate(conn)
    conn.close()
    return ReviewApp(db, overrides_path=tmp_path / "websites.json"), db


def test_url_queue_lists_unresolved_candidates(tmp_path):
    app, _ = _url_app(tmp_path)
    status, _, body = get(app, "/urls")
    page = body.decode()
    assert status == "200 OK"
    assert "NOSITE, NORA" in page
    assert "/urls/H7" in page
    assert "1 candidate(s) with no resolved campaign site" in page


def test_url_form_shows_research_links(tmp_path):
    app, _ = _url_app(tmp_path)
    status, _, body = get(app, "/urls/H7")
    page = body.decode()
    assert status == "200 OK"
    assert "google.com/search" in page
    assert "fec.gov/data/candidate/H7/" in page
    assert 'name="outcome" value="found"' in page
    assert 'name="outcome" value="none"' in page


def test_url_found_updates_db_and_overrides_file(tmp_path):
    import json as _json

    from redbox.db import connect
    app, db = _url_app(tmp_path)
    status, headers, _ = post(
        app, "/urls/H7", "outcome=found&url=norafornc.com&reviewer=charlie")
    assert status == "303 See Other"
    assert "done=H7" in headers["Location"] and "as=found" in headers["Location"]
    conn = connect(db)
    row = conn.execute("SELECT website_url, url_source, url_verified "
                       "FROM candidates WHERE candidate_id='H7'").fetchone()
    conn.close()
    # scheme auto-prepended; human URLs land as manual + VERIFIED
    assert row["website_url"] == "https://norafornc.com"
    assert row["url_source"] == "manual"
    assert row["url_verified"] == 1
    overrides = _json.loads((tmp_path / "websites.json").read_text())
    assert overrides["H7"]["url"] == "https://norafornc.com"
    assert overrides["H7"]["verified"] is True
    assert "charlie" in overrides["H7"]["note"]
    # queue is now empty
    _, _, body = get(app, "/urls")
    assert "0 candidate(s) with no resolved campaign site" in body.decode()


def test_url_found_preserves_existing_overrides(tmp_path):
    import json as _json
    app, _ = _url_app(tmp_path)
    (tmp_path / "websites.json").write_text(
        '{"_comment": "keep me", "H1": {"url": "https://old.example", "verified": true}}')
    post(app, "/urls/H7", "outcome=found&url=https://norafornc.com")
    overrides = _json.loads((tmp_path / "websites.json").read_text())
    assert overrides["_comment"] == "keep me"
    assert overrides["H1"]["url"] == "https://old.example"
    assert overrides["H7"]["url"] == "https://norafornc.com"


def test_url_none_marks_human_none_and_leaves_queue(tmp_path):
    from redbox.db import connect
    app, db = _url_app(tmp_path)
    status, headers, _ = post(app, "/urls/H7", "outcome=none&reviewer=charlie")
    assert status == "303 See Other"
    assert "as=none" in headers["Location"]
    conn = connect(db)
    row = conn.execute("SELECT website_url, url_source FROM candidates "
                       "WHERE candidate_id='H7'").fetchone()
    conn.close()
    assert row["website_url"] is None          # nothing invented
    assert row["url_source"] == "human_none"
    _, _, body = get(app, "/urls")
    page = body.decode()
    assert "0 candidate(s) with no resolved campaign site" in page
    assert "1 candidate(s) previously marked" in page


def test_url_invalid_rerenders_form_without_saving(tmp_path):
    from redbox.db import connect
    app, db = _url_app(tmp_path)
    status, _, body = post(app, "/urls/H7", "outcome=found&url=not a url")
    assert status == "200 OK"                  # re-rendered form, not a redirect
    assert "usable http(s) URL" in body.decode()
    conn = connect(db)
    row = conn.execute("SELECT website_url FROM candidates "
                       "WHERE candidate_id='H7'").fetchone()
    conn.close()
    assert row["website_url"] is None
    assert not (tmp_path / "websites.json").exists()


def test_url_post_redirects_to_next_in_queue(tmp_path):
    from redbox.db import init_db as _init
    db = tmp_path / "db.sqlite"
    conn = _init(db)
    _seed_no_url_candidate(conn, cid="H7", receipts=300000)
    _seed_no_url_candidate(conn, cid="H8", name="ALSO, NOSITE", receipts=200000)
    conn.close()
    app = ReviewApp(db, overrides_path=tmp_path / "websites.json")
    _, headers, _ = post(app, "/urls/H7", "outcome=none")
    assert headers["Location"].startswith("/urls/H8")


def test_url_unknown_candidate_404s(tmp_path):
    app, _ = _url_app(tmp_path)
    status, _, _ = get(app, "/urls/NOPE99")
    assert status == "404 Not Found"


def test_url_wrong_race_marks_inactive_and_leaves_queue(tmp_path):
    from redbox.db import connect
    app, db = _url_app(tmp_path)
    status, headers, _ = post(app, "/urls/H7", "outcome=wrong_race")
    assert status == "303 See Other"
    assert "as=wrong_race" in headers["Location"]
    conn = connect(db)
    row = conn.execute("SELECT inactive, website_url FROM candidates "
                       "WHERE candidate_id='H7'").fetchone()
    conn.close()
    assert row["inactive"] == 2                # human call, distinct from FEC's 1
    assert row["website_url"] is None
    _, _, body = get(app, "/urls")
    assert "0 candidate(s) with no resolved campaign site" in body.decode()


def test_url_queue_excludes_inactive(tmp_path):
    from redbox.db import connect
    app, db = _url_app(tmp_path)
    conn = connect(db)
    conn.execute("UPDATE candidates SET inactive=1 WHERE candidate_id='H7'")
    conn.commit()
    conn.close()
    _, _, body = get(app, "/urls")
    assert "NOSITE, NORA" not in body.decode()


# ---------------------------------------------------------------------------
# Cross-origin protection + durable overrides file

def _post_env(app, path, body, extra):
    environ = {
        "REQUEST_METHOD": "POST", "PATH_INFO": path, "QUERY_STRING": "",
        "CONTENT_LENGTH": str(len(body)), "wsgi.input": io.BytesIO(body),
    }
    environ.update(extra)
    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    b"".join(app(environ, start_response))
    return captured["status"]


def test_cross_origin_post_is_rejected(tmp_path):
    # A malicious page in the reviewer's browser can fire a form POST at
    # http://127.0.0.1:<port> without any preflight; loopback binding alone
    # doesn't stop it from approving detections. Origin/Host must be local.
    app, det_id = _app(tmp_path)
    body = b"action=approve&reviewer=evil"
    status = _post_env(app, f"/detection/{det_id}", body,
                       {"HTTP_ORIGIN": "https://evil.example",
                        "HTTP_HOST": "127.0.0.1:8001"})
    assert status.startswith("403")
    import sqlite3 as _sq
    conn = _sq.connect(app.db_path)
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0

    # DNS rebinding: attacker hostname resolving to 127.0.0.1 still carries
    # its own Host header — rejected too.
    status = _post_env(app, f"/detection/{det_id}", body,
                       {"HTTP_HOST": "evil.example"})
    assert status.startswith("403")
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    conn.close()


def test_same_origin_post_is_accepted(tmp_path):
    app, det_id = _app(tmp_path)
    body = b"action=approve&reviewer=me"
    status = _post_env(app, f"/detection/{det_id}", body,
                       {"HTTP_ORIGIN": "http://127.0.0.1:8001",
                        "HTTP_HOST": "127.0.0.1:8001"})
    assert status.startswith("303")
    import sqlite3 as _sq
    conn = _sq.connect(app.db_path)
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1
    conn.close()


def test_overrides_write_is_atomic_and_locked(tmp_path):
    # Concurrent triage POSTs must not lose entries (read-modify-write on a
    # threading server), and no partial file may ever land on disk.
    import threading as _t

    from redbox.db import init_db as _init
    from redbox.reviewweb import record_found_url

    db = tmp_path / "db.sqlite"
    conn = _init(db)
    for i in range(8):
        conn.execute("""INSERT INTO candidates (candidate_id,name,created_at,updated_at)
            VALUES (?,?,'t','t')""", (f"H{i}", f"C{i}"))
    conn.commit()
    conn.close()
    overrides = tmp_path / "websites.json"

    def triage(i):
        c = _init(db)
        record_found_url(c, f"H{i}", f"https://c{i}.example",
                         reviewer="t", overrides_path=overrides)
        c.close()

    threads = [_t.Thread(target=triage, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    import json as _json
    data = _json.loads(overrides.read_text())
    assert len(data) == 8                       # nobody's entry was lost
    assert not list(tmp_path.glob("websites.json.*"))   # no temp litter
