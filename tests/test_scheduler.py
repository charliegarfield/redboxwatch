"""Offline tests for the scheduler cadence + calendar (spec §3.2)."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from redbox.db import init_db
from redbox.scheduler import (backfill_primary_dates, due_candidates,
                              general_election_date, load_calendar)


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


def test_after_primary_stays_on_general_cadence(tmp_path):
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
    assert "PAST" in ids             # post-primary: general phase, still scanned
    past = next(d for d in due if d.candidate["candidate_id"] == "PAST")
    assert past.cadence == "weekly"  # general is >21 days out on 06-10
    assert past.days_to_primary is None
    assert past.days_to_general == (date(2026, 11, 3) - date(2026, 6, 10)).days


def test_general_election_date():
    assert general_election_date(2026) == date(2026, 11, 3)
    assert general_election_date(2024) == date(2024, 11, 5)
    assert general_election_date(2020) == date(2020, 11, 3)


def test_daily_window_before_general_and_post_election_cutoff(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    _cand(conn, "GEN", "NC", last_scan="2026-10-18T00:00:00+00:00")
    load_calendar(conn, cycle=2026, fixture_path=_cal(tmp_path))
    backfill_primary_dates(conn, cycle=2026)

    # 2026-10-20: 14 days to the general -> daily cadence, 2d elapsed -> due
    due = due_candidates(conn, today=date(2026, 10, 20))
    assert len(due) == 1 and due[0].cadence == "daily"
    assert due[0].days_to_general == 14

    # after election day the cycle is over -> nothing due
    assert due_candidates(conn, today=date(2026, 11, 4)) == []


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


def test_unknown_primary_date_uses_general_cadence(tmp_path):
    # A candidate whose state is absent from the calendar (or discovered after
    # the last backfill) must key cadence off the general election, not sit on
    # a flat weekly that never escalates and sorts dead-last.
    conn = init_db(tmp_path / "db.sqlite")
    _cand(conn, "NODATE", "ZZ")          # no calendar row -> primary_date NULL
    load_calendar(conn, cycle=2026, fixture_path=_cal(tmp_path))
    backfill_primary_dates(conn, cycle=2026)
    assert conn.execute("SELECT primary_date FROM candidates "
                        "WHERE candidate_id='NODATE'").fetchone()[0] is None

    # 3 days before the 2026-11-03 general: daily, prioritized by general date.
    due = due_candidates(conn, today=date(2026, 10, 31))
    assert len(due) == 1
    assert due[0].cadence == "daily"
    assert due[0].days_to_general == 3

    # Well before the window: weekly, but still carrying days_to_general so
    # the priority sort places it by real election distance, not 9999.
    due2 = due_candidates(conn, today=date(2026, 9, 1))
    assert due2[0].cadence == "weekly"
    assert due2[0].days_to_general == (date(2026, 11, 3) - date(2026, 9, 1)).days


def test_backfill_preserves_dates_for_states_missing_from_calendar(tmp_path):
    # An unconditional backfill NULLed hand-set dates for any state without a
    # calendar row and reported every candidate as "backfilled".
    conn = init_db(tmp_path / "db.sqlite")
    _cand(conn, "H1", "NY")
    _cand(conn, "H2", "ZZ")
    conn.execute("UPDATE candidates SET primary_date='2026-09-01' "
                 "WHERE candidate_id='H2'")
    conn.commit()
    load_calendar(conn, cycle=2026, fixture_path=_cal(tmp_path))
    n = backfill_primary_dates(conn, cycle=2026)
    assert n == 1                        # only H1 (NY has a calendar row)
    rows = {r["candidate_id"]: r["primary_date"] for r in
            conn.execute("SELECT candidate_id, primary_date FROM candidates")}
    assert rows == {"H1": "2026-06-23", "H2": "2026-09-01"}


def _cal_with_overrides(tmp_path):
    p = tmp_path / "cal_o.json"
    p.write_text(json.dumps([
        {"state": "AL", "primary_date": "2026-05-19", "filing_deadline": "2026-01-23",
         "overrides": [{"office": "H", "districts": ["01", "02"],
                        "primary_date": "2026-08-11"}]},
        {"state": "LA", "primary_date": "2026-05-16", "filing_deadline": "2026-01-14",
         "overrides": [{"office": "H", "primary_date": "2026-11-03"}]},
    ]))
    return p


def test_backfill_uses_most_specific_election_row(tmp_path):
    # District override ('H:01') > office override ('H') > statewide ('').
    conn = init_db(tmp_path / "db.sqlite")
    for cid, st, office, dd in [("A1", "AL", "H", "01"), ("A3", "AL", "H", "03"),
                                ("A_S", "AL", "S", "00"),
                                ("LH", "LA", "H", "04"), ("LS", "LA", "S", "00")]:
        conn.execute("""INSERT INTO candidates (candidate_id,name,office,state,district,
            party,cycle,url_verified,website_url,created_at,updated_at)
            VALUES (?,?,?,?,?,?,2026,1,'https://x','t','t')""",
            (cid, cid, office, st, dd, "DEM"))
    conn.commit()
    load_calendar(conn, cycle=2026, fixture_path=_cal_with_overrides(tmp_path))
    backfill_primary_dates(conn, cycle=2026)
    got = {r["candidate_id"]: r["primary_date"] for r in
           conn.execute("SELECT candidate_id, primary_date FROM candidates")}
    assert got == {
        "A1": "2026-08-11",     # postponed district
        "A3": "2026-05-19",     # statewide date
        "A_S": "2026-05-19",    # Senate: statewide
        "LH": "2026-11-03",     # LA House office-wide override
        "LS": "2026-05-16",     # LA Senate: statewide
    }
