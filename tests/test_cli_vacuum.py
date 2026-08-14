"""Offline tests for `vacuum`'s junk 404/410 sweep.

A 404/410 scan of a (candidate, url) pair that never ONCE served usable
content is common-path probe noise and is deleted. A 404 on a URL that EVER
had a usable scan is take-down evidence and must be kept, as must any row
still referenced by detections/archives/change_events (FK safety).
"""
from __future__ import annotations

import argparse

import redbox.cli as cli
from redbox.config import Config
from redbox.db import init_db

REAL = "This is a real campaign media page with plenty of content."
CHALLENGE = "Just a moment... checking your browser before accessing"


def _cand(conn, cid):
    conn.execute(
        """INSERT INTO candidates (candidate_id,name,office,state,district,
           party,cycle,universe_reason,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (cid, f"CAND {cid}", "H", "NY", "12", "DEM", 2026,
         "contested_primary", "t", "t"))


def _scan(conn, cid, url, status, text=""):
    cur = conn.execute(
        """INSERT INTO scans (candidate_id,url,fetched_at,http_status,
           raw_text,text_hash) VALUES (?,?,?,?,?,?)""",
        (cid, url, "t", status, text, "h"))
    return cur.lastrowid


def _setup(tmp_path):
    db = tmp_path / "db.sqlite"
    conn = init_db(db)
    _cand(conn, "A")
    _cand(conn, "B")

    ids = {}
    # A/x: usable 200 then 404 -> the 404 is take-down evidence, KEPT.
    ids["a_x_200"] = _scan(conn, "A", "https://a.example/x", 200, REAL)
    ids["a_x_404"] = _scan(conn, "A", "https://a.example/x", 404)
    # A/y: only ever 404 -> both junk.
    ids["a_y_404a"] = _scan(conn, "A", "https://a.example/y", 404)
    ids["a_y_404b"] = _scan(conn, "A", "https://a.example/y", 404)
    # A/z: only ever 410 -> junk.
    ids["a_z_410"] = _scan(conn, "A", "https://a.example/z", 410)
    # A/w: 200 that was a bot-challenge shell (not usable) then 404 -> the 404
    # is junk; the 200 row itself is not swept (only 404/410 are).
    ids["a_w_200ch"] = _scan(conn, "A", "https://a.example/w", 200, CHALLENGE)
    ids["a_w_404"] = _scan(conn, "A", "https://a.example/w", 404)
    # A/e: 200 with an empty body (not usable) then 404 -> the 404 is junk.
    ids["a_e_200empty"] = _scan(conn, "A", "https://a.example/e", 200, "")
    ids["a_e_404"] = _scan(conn, "A", "https://a.example/e", 404)
    # B/x: SAME url as A's usable page but a different candidate — the pair
    # (B, x) never had usable content, so B's 404 is junk.
    ids["b_x_404"] = _scan(conn, "B", "https://a.example/x", 404)
    # A/d: 404 referenced by a detection row -> KEPT (never break an FK).
    ids["a_d_404"] = _scan(conn, "A", "https://a.example/d", 404)
    conn.execute(
        """INSERT INTO detections (scan_id,candidate_id,classification,
           confidence,classified_at) VALUES (?,?,?,?,?)""",
        (ids["a_d_404"], "A", "no_guidance_detected", 0.9, "t"))
    conn.commit()
    conn.close()
    return Config(raw={"database_path": str(db)}, repo_root=tmp_path), db, ids


def _scan_ids(db):
    conn = init_db(db)
    out = {r[0] for r in conn.execute("SELECT scan_id FROM scans")}
    conn.close()
    return out


JUNK = {"a_y_404a", "a_y_404b", "a_z_410", "a_w_404", "a_e_404", "b_x_404"}


def test_sweep_deletes_only_never_usable_404s(tmp_path):
    cfg, db, ids = _setup(tmp_path)
    rc = cli.cmd_vacuum(argparse.Namespace(dry_run=False), cfg)
    assert rc == 0
    remaining = _scan_ids(db)
    assert remaining == {v for k, v in ids.items() if k not in JUNK}
    # spot-check the load-bearing keeps
    assert ids["a_x_404"] in remaining      # take-down evidence
    assert ids["a_d_404"] in remaining      # referenced by a detection
    assert ids["a_w_200ch"] in remaining    # 200s are never swept


def test_sweep_dry_run_reports_but_deletes_nothing(tmp_path, capsys):
    cfg, db, ids = _setup(tmp_path)
    rc = cli.cmd_vacuum(argparse.Namespace(dry_run=True), cfg)
    assert rc == 0
    assert _scan_ids(db) == set(ids.values())
    out = capsys.readouterr().out
    assert f"{len(JUNK):,}" in out
    assert "Dry run" in out


def test_sweep_idempotent_and_fresh_schema_ok(tmp_path):
    # Fresh-schema DBs (no raw_html column) must still run the sweep, and a
    # second pass finds nothing left to delete.
    cfg, db, ids = _setup(tmp_path)
    assert cli.cmd_vacuum(argparse.Namespace(dry_run=False), cfg) == 0
    before = _scan_ids(db)
    assert cli.cmd_vacuum(argparse.Namespace(dry_run=False), cfg) == 0
    assert _scan_ids(db) == before
