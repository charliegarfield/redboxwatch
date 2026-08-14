"""Offline tests for `mark-inactive`: the FEC sync must own ONLY value 1.

Human calls from the review console (inactive=2) and primary-loser marks
(inactive=3) are never machine-touched — neither downgraded to 1 when the FEC
also flags the candidacy, nor cleared to NULL when it doesn't.
"""
from __future__ import annotations

import argparse

import redbox.cli as cli
from redbox.config import Config
from redbox.db import init_db


class FakeFEC:
    """Stands in for redbox.cli.FECClient; returns a preset inactive set."""

    inactive: set[str] = set()

    def __init__(self, **kwargs):
        pass

    def inactive_ids(self, candidate_ids, *, use_cache=True):
        return set(self.inactive) & set(candidate_ids)

    def close(self):
        pass


def _seed(conn, cid, inactive=None):
    conn.execute(
        """INSERT INTO candidates (candidate_id,name,office,state,district,
           party,cycle,universe_reason,inactive,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (cid, f"CAND {cid}", "H", "NY", "12", "DEM", 2026,
         "contested_primary", inactive, "t", "t"))


def _run(tmp_path, monkeypatch, rows, fec_inactive):
    """Seed a fresh DB with (cid, inactive) rows, run the command, return
    {cid: inactive} after."""
    db = tmp_path / "db.sqlite"
    db.unlink(missing_ok=True)  # each _run starts from a fresh DB
    conn = init_db(db)
    for cid, val in rows:
        _seed(conn, cid, val)
    conn.commit()
    conn.close()

    monkeypatch.setattr(FakeFEC, "inactive", set(fec_inactive))
    monkeypatch.setattr(cli, "FECClient", FakeFEC)
    cfg = Config(raw={"database_path": str(db),
                      "fec_cache_dir": str(tmp_path / "cache")},
                 repo_root=tmp_path)
    rc = cli.cmd_mark_inactive(argparse.Namespace(no_cache=True), cfg)
    assert rc == 0

    conn = init_db(db)
    state = {r[0]: r[1] for r in
             conn.execute("SELECT candidate_id, inactive FROM candidates")}
    conn.close()
    return state


def test_fec_flag_does_not_downgrade_human_call(tmp_path, monkeypatch):
    # inactive=2 (human wrong-race/deceased call) also flagged by the FEC:
    # the human value must survive, not be clobbered down to 1.
    state = _run(tmp_path, monkeypatch,
                 rows=[("H2222", 2)], fec_inactive={"H2222"})
    assert state["H2222"] == 2


def test_fec_flag_does_not_downgrade_primary_loser(tmp_path, monkeypatch):
    state = _run(tmp_path, monkeypatch,
                 rows=[("H3333", 3)], fec_inactive={"H3333"})
    assert state["H3333"] == 3


def test_untouched_row_gets_flagged(tmp_path, monkeypatch):
    state = _run(tmp_path, monkeypatch,
                 rows=[("H0001", None)], fec_inactive={"H0001"})
    assert state["H0001"] == 1


def test_unflagged_fec_row_is_cleared(tmp_path, monkeypatch):
    # A previously FEC-flagged row (inactive=1) the FEC no longer reports:
    # cleared back to NULL. Covers both the empty and non-empty FEC-set paths.
    state = _run(tmp_path, monkeypatch,
                 rows=[("H1111", 1), ("H0001", None)], fec_inactive={"H0001"})
    assert state["H1111"] is None
    assert state["H0001"] == 1

    state = _run(tmp_path, monkeypatch,
                 rows=[("H1111", 1)], fec_inactive=set())
    assert state["H1111"] is None


def test_human_values_never_cleared(tmp_path, monkeypatch):
    # 2/3 rows absent from the FEC set must NOT be resurrected to NULL —
    # neither when the FEC set is empty nor when it names other candidates.
    state = _run(tmp_path, monkeypatch,
                 rows=[("H2222", 2), ("H3333", 3)], fec_inactive=set())
    assert state["H2222"] == 2
    assert state["H3333"] == 3

    state = _run(tmp_path, monkeypatch,
                 rows=[("H2222", 2), ("H3333", 3), ("H0001", None)],
                 fec_inactive={"H0001"})
    assert state["H2222"] == 2
    assert state["H3333"] == 3
    assert state["H0001"] == 1


def test_regression_two_runs_do_not_resurrect_human_call(tmp_path, monkeypatch):
    # The original bug end-to-end: run 1 downgraded 2 -> 1 (FEC also flagged
    # it), run 2 (FEC flag gone) cleared 1 -> NULL, resurrecting the
    # candidate. After the fix the value must stay 2 through both runs.
    db = tmp_path / "sub"
    db.mkdir()
    state = _run(db, monkeypatch, rows=[("H2222", 2)], fec_inactive={"H2222"})
    assert state["H2222"] == 2

    # Re-run against the SAME db with the FEC flag gone.
    monkeypatch.setattr(FakeFEC, "inactive", set())
    cfg = Config(raw={"database_path": str(db / "db.sqlite"),
                      "fec_cache_dir": str(db / "cache")},
                 repo_root=db)
    assert cli.cmd_mark_inactive(argparse.Namespace(no_cache=True), cfg) == 0
    conn = init_db(db / "db.sqlite")
    val = conn.execute("SELECT inactive FROM candidates "
                       "WHERE candidate_id='H2222'").fetchone()[0]
    conn.close()
    assert val == 2
