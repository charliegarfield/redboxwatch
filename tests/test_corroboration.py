"""Offline tests for Schedule E corroboration (spec §3.6)."""
from __future__ import annotations

from redbox.corroboration import compute, pull_and_store_ie, run
from redbox.db import init_db


class FakeFEC:
    def __init__(self, rows):
        self._rows = rows

    def schedule_e(self, *, candidate_id, cycle, support_oppose=None, use_cache=True):
        for r in self._rows:
            if r.get("candidate_id") == candidate_id:
                yield r


def _ie(cid, amt, date, ind="S", committee="STAND FOR X PAC", cmid="C1", sub="s"):
    return {"candidate_id": cid, "expenditure_amount": amt, "expenditure_date": date,
            "support_oppose_indicator": ind, "committee": {"name": committee},
            "committee_id": cmid, "sub_id": sub}


def _seed_candidate_with_detection(conn, cid="H1", detected="2026-05-20T00:00:00+00:00"):
    conn.execute("""INSERT INTO candidates (candidate_id,name,state,district,party,cycle,
        url_verified,created_at,updated_at) VALUES (?,?,?,?,?,?,1,'t','t')""",
        (cid, "TEST", "NY", "12", "DEM", 2026))
    cur = conn.execute("INSERT INTO scans (candidate_id,url,fetched_at,text_hash) VALUES (?,?,?,?)",
                       (cid, "https://x/media", detected, "h"))
    conn.execute("""INSERT INTO detections (scan_id,candidate_id,classification,confidence,
        evidence,model,classified_at) VALUES (?,?,?,?,?,?,?)""",
        (cur.lastrowid, cid, "red_box_guidance", 0.9, "[]", "claude-haiku-4-5", detected))
    conn.commit()


def test_aggregates_supporting_and_opposing(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    _seed_candidate_with_detection(conn)
    fec = FakeFEC([
        _ie("H1", 100000, "2026-03-01", "S", sub="a"),
        _ie("H1", 250000, "2026-05-26", "S", sub="b"),
        _ie("H1", 50000, "2026-04-10", "O", committee="OTHER PAC", cmid="C2", sub="c"),
    ])
    corr = run(conn, fec, candidate_id="H1", cycle=2026)
    assert corr.supporting_total == 350000
    assert corr.opposing_total == 50000
    assert corr.ie_filing_count == 3
    assert "350,000" in corr.headline


def test_after_detection_cutoff_is_inclusive_of_detection_day(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    # detected on 2026-05-20
    _seed_candidate_with_detection(conn, detected="2026-05-20T08:00:00+00:00")
    fec = FakeFEC([
        _ie("H1", 100000, "2026-05-10", "S", sub="a"),   # before -> excluded
        _ie("H1", 200000, "2026-05-20", "S", sub="b"),   # same day -> included
        _ie("H1", 300000, "2026-05-26", "S", sub="c"),   # after -> included
    ])
    corr = run(conn, fec, candidate_id="H1", cycle=2026)
    assert corr.supporting_total == 600000
    assert corr.supporting_after_detection == 500000      # 200k + 300k


def test_idempotent_repull(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    _seed_candidate_with_detection(conn)
    fec = FakeFEC([_ie("H1", 100000, "2026-03-01", "S", sub="a")])
    pull_and_store_ie(conn, fec, candidate_id="H1", cycle=2026)
    pull_and_store_ie(conn, fec, candidate_id="H1", cycle=2026)   # re-pull
    n = conn.execute("SELECT COUNT(*) FROM ie_filings WHERE candidate_id='H1'").fetchone()[0]
    assert n == 1                                          # no duplicate accumulation


def test_compute_is_scoped_to_cycle(tmp_path):
    # IE filings from two cycles must not be summed together (regression).
    from redbox.corroboration import compute
    conn = init_db(tmp_path / "db.sqlite")
    _seed_candidate_with_detection(conn)
    for cyc, amt, sub in [(2024, 500000, "old"), (2026, 100000, "new")]:
        conn.execute("""INSERT INTO ie_filings (candidate_id,committee_id,committee_name,
            support_oppose_indicator,expenditure_amount,expenditure_date,cycle,transaction_id)
            VALUES ('H1','C1','PAC','S',?,?,?,?)""", (amt, "2026-01-01", cyc, sub))
    conn.commit()
    corr = compute(conn, "H1", cycle=2026)
    assert corr.supporting_total == 100000          # only the 2026 row
    assert corr.ie_filing_count == 1


def test_persists_corroboration_row(tmp_path):
    conn = init_db(tmp_path / "db.sqlite")
    _seed_candidate_with_detection(conn)
    fec = FakeFEC([_ie("H1", 9384881, "2026-05-26", "S", committee="STAND FOR NEW YORK PAC")])
    run(conn, fec, candidate_id="H1", cycle=2026)
    row = conn.execute("SELECT * FROM corroboration WHERE candidate_id='H1'").fetchone()
    assert row["supporting_total"] == 9384881
    assert row["ie_filing_count"] == 1
    assert "STAND FOR NEW YORK PAC" in row["spender_list"]
