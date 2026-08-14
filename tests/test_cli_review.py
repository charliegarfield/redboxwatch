"""Offline tests for `review` CLI foot-guns.

Approving a detection must require (a) --detection alongside --action, (b) a
detection that actually exists, and (c) a reviewable classification
(red_box_guidance | ambiguous). The publisher's inactive-candidate exemption
keys on action='approve' alone, so a review row on a no_guidance_detected
detection could resurrect an inactive candidate onto the public site.
"""
from __future__ import annotations

import argparse

import redbox.cli as cli
from redbox.config import Config
from redbox.db import init_db


def _seed(conn, cid="H0001", classification="red_box_guidance", detection_id=1):
    """One candidate + scan + detection (FK-complete) with the given class."""
    conn.execute(
        """INSERT INTO candidates (candidate_id,name,office,state,district,
           party,cycle,universe_reason,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (cid, f"CAND {cid}", "H", "NY", "12", "DEM", 2026,
         "contested_primary", "t", "t"))
    conn.execute(
        """INSERT INTO scans (scan_id,candidate_id,url,fetched_at,text_hash)
           VALUES (?,?,?,?,?)""",
        (detection_id, cid, f"https://example.com/media/{detection_id}",
         "t", f"hash{detection_id}"))
    conn.execute(
        """INSERT INTO detections (detection_id,scan_id,candidate_id,
           classification,confidence,classified_at)
           VALUES (?,?,?,?,?,?)""",
        (detection_id, detection_id, cid, classification, 0.9, "t"))
    conn.commit()


def _setup(tmp_path, seed=None):
    """Fresh tmp DB (seeded via ``seed(conn)`` if given); returns (cfg, db)."""
    db = tmp_path / "db.sqlite"
    conn = init_db(db)
    if seed:
        seed(conn)
    conn.close()
    return Config(raw={"database_path": str(db)}, repo_root=tmp_path), db


def _args(**kw):
    base = dict(list=False, detection=None, action=None, group=False,
                reviewer=None, notes=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _reviews(db):
    conn = init_db(db)
    rows = [dict(r) for r in
            conn.execute("SELECT detection_id, action FROM reviews")]
    conn.close()
    return rows


def test_approve_negative_detection_refused(tmp_path, capsys):
    cfg, db = _setup(tmp_path, lambda c: _seed(
        c, classification="no_guidance_detected"))
    rc = cli.cmd_review(_args(detection=1, action="approve"), cfg)
    assert rc == 2
    assert _reviews(db) == []           # no review row inserted
    out = capsys.readouterr().out
    assert "no_guidance_detected" in out


def test_nonexistent_detection_fails_cleanly(tmp_path, capsys):
    # Must not raise (the old code hit a raw sqlite3.IntegrityError and leaked
    # the connection) and must not record anything.
    cfg, db = _setup(tmp_path, _seed)
    rc = cli.cmd_review(_args(detection=999, action="approve"), cfg)
    assert rc == 2
    assert _reviews(db) == []
    assert "999" in capsys.readouterr().out


def test_action_without_detection_errors(tmp_path, capsys):
    # Old behavior: silently printed the pending queue and exited 0, so the
    # operator believed the approval had been recorded.
    cfg, db = _setup(tmp_path, _seed)
    rc = cli.cmd_review(_args(action="approve"), cfg)
    assert rc == 2
    assert _reviews(db) == []
    out = capsys.readouterr().out
    assert "--detection" in out
    assert "pending review" not in out  # error, not the queue listing


def test_approve_genuine_positive_still_works(tmp_path):
    cfg, db = _setup(tmp_path, _seed)   # red_box_guidance
    rc = cli.cmd_review(_args(detection=1, action="approve"), cfg)
    assert rc == 0
    assert _reviews(db) == [{"detection_id": 1, "action": "approve"}]


def test_ambiguous_detection_is_reviewable(tmp_path):
    cfg, db = _setup(tmp_path, lambda c: _seed(c, classification="ambiguous"))
    rc = cli.cmd_review(_args(detection=1, action="reject"), cfg)
    assert rc == 0
    assert _reviews(db) == [{"detection_id": 1, "action": "reject"}]


def test_plain_listing_still_exits_zero(tmp_path, capsys):
    # No --action: listing the queue remains valid (regression guard for the
    # try/finally refactor).
    cfg, _ = _setup(tmp_path, _seed)
    rc = cli.cmd_review(_args(), cfg)
    assert rc == 0
    assert "pending review" in capsys.readouterr().out
