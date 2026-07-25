"""Offline tests for the scheduler cadence + calendar (spec §3.2)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from redbox.db import init_db
from redbox.scheduler import backfill_primary_dates, due_candidates, load_calendar


def _cal(tmp_path):
    p = tmp_path / "cal.json"
    p.write_text(json.dumps([
        {"state": "NY", "primary_date": "2026-06-23", "filing_deadline": "2026-04-06"},
        {"state": "NC", "primary_date": "2026-03-03", "filing_deadline": "2025-12-19"},
    ]))
    return p


def _cand(conn, cid, state, verified=1, last_scan=None):
    conn.execute("""INSERT INTO candidates (candidate_id,name,state,district,party,cycle,
        url_verified,website_url,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,'t','t')""",
        (cid, cid, state, "12", "DEM", 2026, verified, "https://x"))
    if last_scan:
        conn.execute("INSERT INTO scans (candidate_id,url,fetched_at,text_hash) VALUES (?,?,?,?)",
                     (cid, "https://x", last_scan, "h"))
    conn.commit()


def test_calendar_load_and_backfill(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    _cand(conn, "H1", "NY")
    assert load_calendar(conn, cycle=2026, fixture_path=_cal(tmp_path)) == 2
    backfill_primary_dates(conn, cycle=2026)
    row = conn.execute("SELECT primary_date FROM candidates WHERE candidate_id='H1'").fetchone()
    assert row["primary_date"] == "2026-06-23"


def test_daily_window_vs_weekly(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    _cand(conn, "H1", "NY", last_scan="2026-06-08T00:00:00+00:00")
    load_calendar(conn, cycle=2026, fixture_path=_cal(tmp_path))
    backfill_primary_dates(conn, cycle=2026)

    # 2026-06-10: 13 days to primary -> daily; 2d since scan >= 1 -> due
    due = due_candidates(conn, today=date(2026, 6, 10))
    assert len(due) == 1 and due[0].cadence == "daily"

    # 2026-05-20: 34 days to primary -> weekly; 0 prior scans near -> recompute
    # last scan 06-08 is in the future relative to 05-20, but elapsed negative -> not due
    due2 = due_candidates(conn, today=date(2026, 5, 20))
    assert due2 == [] or due2[0].cadence == "weekly"


def test_after_primary_excluded_unverified_scheduled_by_default(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    _cand(conn, "PAST", "NC")            # primary 2026-03-03, already over
    _cand(conn, "UNVER", "NY", verified=0)
    _cand(conn, "OK", "NY")
    load_calendar(conn, cycle=2026, fixture_path=_cal(tmp_path))
    backfill_primary_dates(conn, cycle=2026)
    due = due_candidates(conn, today=date(2026, 6, 10))
    ids = {d.candidate["candidate_id"] for d in due}
    assert "OK" in ids               # has URL, before primary -> due
    assert "UNVER" in ids            # unverified is scheduled by default now
    assert "PAST" not in ids         # primary already passed


def test_strict_mode_excludes_unverified(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    _cand(conn, "UNVER", "NY", verified=0)
    _cand(conn, "OK", "NY")
    load_calendar(conn, cycle=2026, fixture_path=_cal(tmp_path))
    backfill_primary_dates(conn, cycle=2026)
    due = due_candidates(conn, today=date(2026, 6, 10), require_verified=True)
    ids = {d.candidate["candidate_id"] for d in due}
    assert ids == {"OK"}             # strict mode: only the verified one


def test_candidate_without_url_is_not_scheduled(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    conn.execute("""INSERT INTO candidates (candidate_id,name,state,district,party,cycle,
        url_verified,website_url,created_at,updated_at)
        VALUES ('NOURL','NOURL','NY','12','DEM',2026,0,NULL,'t','t')""")
    conn.commit()
    load_calendar(conn, cycle=2026, fixture_path=_cal(tmp_path))
    backfill_primary_dates(conn, cycle=2026)
    due = due_candidates(conn, today=date(2026, 6, 10))
    assert due == []                 # no URL -> nothing to scan


def test_before_filing_deadline_excluded(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    _cand(conn, "H1", "NY")          # filing deadline 2026-04-06
    load_calendar(conn, cycle=2026, fixture_path=_cal(tmp_path))
    backfill_primary_dates(conn, cycle=2026)
    due = due_candidates(conn, today=date(2026, 3, 1))   # before filing
    assert due == []
