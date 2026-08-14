"""Offline tests for main() dispatch, parent-parser flags, and small CLI fixes.

Dispatch is now table-driven: every subparser binds its handler via
``set_defaults(func=...)`` and ``main`` calls ``args.func(args, cfg)``.
``main`` looks the handler up as a module global at call time, so
monkeypatching ``cli.cmd_*`` intercepts the dispatch.
"""
from __future__ import annotations

import argparse

import pytest

import redbox.cli as cli
from redbox.config import Config
from redbox.db import init_db
from redbox.ratings.fixture import FixtureRatingAdapter


def _capture(monkeypatch, name):
    """Replace handler ``name`` with a recorder; stub load_config."""
    calls = {}

    def fake(args, cfg):
        calls["args"] = args
        return 0

    monkeypatch.setattr(cli, name, fake)
    monkeypatch.setattr(cli, "load_config", lambda p: Config(raw={}))
    return calls


def test_main_routes_via_func_defaults(monkeypatch):
    calls = _capture(monkeypatch, "cmd_schedule")
    assert cli.main(["schedule", "--today", "2026-08-01"]) == 0
    assert calls["args"].command == "schedule"
    assert calls["args"].today == "2026-08-01"


def test_initdb_dispatch_end_to_end(tmp_path, monkeypatch):
    # One representative command run for real through main().
    db = tmp_path / "db.sqlite"
    monkeypatch.setattr(
        cli, "load_config",
        lambda p: Config(raw={"database_path": str(db)}, repo_root=tmp_path))
    assert cli.main(["initdb"]) == 0
    assert db.exists()


def test_parent_parser_flags_still_parse(monkeypatch):
    # --authorize/--push-wayback now come from a shared parent parser.
    calls = _capture(monkeypatch, "cmd_scan_all")
    assert cli.main(["scan-all", "--authorize", "--push-wayback", "--rescan"]) == 0
    a = calls["args"]
    assert a.authorize and a.push_wayback and a.rescan
    assert a.due is False

    calls = _capture(monkeypatch, "cmd_mark_inactive")
    assert cli.main(["mark-inactive", "--no-cache"]) == 0
    assert calls["args"].no_cache is True


def test_port_defaults_stay_per_command(monkeypatch):
    # review-web defaults to 8001, publish --serve to 8000; a naively shared
    # --port action would let one command's default clobber the other's.
    rw = _capture(monkeypatch, "cmd_review_web")
    assert cli.main(["review-web"]) == 0
    assert rw["args"].port == 8001

    pub = _capture(monkeypatch, "cmd_publish")
    assert cli.main(["publish"]) == 0
    assert pub["args"].port == 8000


# ---------------------------------------------------------------------------
# rating_source: 'none' disables the overlay; missing/null default to fixture.

def test_rating_adapter_source_none_disables():
    assert cli._make_rating_adapter(Config(raw={"rating_source": "none"})) is None


def test_rating_adapter_defaults_to_fixture():
    assert isinstance(cli._make_rating_adapter(Config(raw={})),
                      FixtureRatingAdapter)
    # An explicit YAML null also falls back to the default, not to None.
    assert isinstance(cli._make_rating_adapter(Config(raw={"rating_source": None})),
                      FixtureRatingAdapter)


def test_rating_adapter_unknown_source_raises():
    with pytest.raises(ValueError):
        cli._make_rating_adapter(Config(raw={"rating_source": "cook"}))


# ---------------------------------------------------------------------------
# --district: weball rows are zero-padded, so '1' must become '01' or the
# filter silently matches nothing.

def test_district_normalized_to_weball_padding():
    assert cli._normalize_district("1") == "01"
    assert cli._normalize_district(" 1 ") == "01"
    assert cli._normalize_district("12") == "12"
    assert cli._normalize_district(None) is None


# ---------------------------------------------------------------------------
# mark-primary-losers: --allow-mass-clear wiring + runoff-aware "past" set.

def _losers_cfg(tmp_path):
    db = tmp_path / "db.sqlite"
    init_db(db).close()
    return Config(raw={"database_path": str(db)}, repo_root=tmp_path)


def _losers_args(**kw):
    base = dict(today=None, no_feed=True, allow_mass_clear=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _stub_flag(monkeypatch, seen):
    # cmd_mark_primary_losers imports flag_primary_losers from redbox.nominees
    # at call time, so patching the module attribute intercepts it.
    def fake(conn, resolver, *, states, ts, exclude=(), allow_mass_clear=False):
        seen["states"] = set(states)
        seen["allow_mass_clear"] = allow_mass_clear
        return [], 0, 0, set()

    monkeypatch.setattr("redbox.nominees.flag_primary_losers", fake)


def test_allow_mass_clear_flag_parses(monkeypatch):
    calls = _capture(monkeypatch, "cmd_mark_primary_losers")
    assert cli.main(["mark-primary-losers", "--allow-mass-clear"]) == 0
    assert calls["args"].allow_mass_clear is True

    calls = _capture(monkeypatch, "cmd_mark_primary_losers")
    assert cli.main(["mark-primary-losers"]) == 0
    assert calls["args"].allow_mass_clear is False


def test_allow_mass_clear_reaches_flag_primary_losers(tmp_path, monkeypatch):
    seen = {}
    _stub_flag(monkeypatch, seen)
    cfg = _losers_cfg(tmp_path)
    assert cli.cmd_mark_primary_losers(
        _losers_args(today="2026-07-01", allow_mass_clear=True), cfg) == 0
    assert seen["allow_mass_clear"] is True
    assert cli.cmd_mark_primary_losers(
        _losers_args(today="2026-07-01"), cfg) == 0
    assert seen["allow_mass_clear"] is False


def test_runoff_state_not_past_until_runoff(tmp_path, monkeypatch, capsys):
    # OK: first round 2026-06-16, runoff 2026-08-25 (shipped fixture). Between
    # the rounds OK must NOT be treated as past — first-round results can't
    # say who lost a race that advanced to the runoff. TX (runoff 5/26) is
    # settled by July and stays in.
    seen = {}
    _stub_flag(monkeypatch, seen)
    cfg = _losers_cfg(tmp_path)

    assert cli.cmd_mark_primary_losers(_losers_args(today="2026-07-01"), cfg) == 0
    assert "OK" not in seen["states"]
    assert "TX" in seen["states"]
    out = capsys.readouterr().out
    assert "between first round and runoff" in out and "OK" in out

    # The day after the runoff, OK's races are settled and it joins the set.
    assert cli.cmd_mark_primary_losers(_losers_args(today="2026-08-26"), cfg) == 0
    assert "OK" in seen["states"]


# ---------------------------------------------------------------------------
# backfill-changes: the mismatched bucket is printed alongside missing/spurious.

def test_backfill_changes_prints_mismatched(tmp_path, monkeypatch, capsys):
    db = tmp_path / "db.sqlite"
    conn = init_db(db)
    conn.execute("""INSERT INTO candidates (candidate_id,name,created_at,
        updated_at) VALUES ('H1','Alice Smith','t','t')""")
    conn.commit()
    conn.close()
    result = {"missing": [], "spurious": [], "mismatched": [{
        "candidate_id": "H1", "url": "https://x.example/media",
        "event_type": "take_down", "prev_scan_id": 1, "new_scan_id": 2,
        "prev_classification": "red_box_guidance",
        "new_classification": "no_guidance_detected",
        "detected_at": "2026-07-15T00:00:00+00:00",
        "change_id": 7, "recorded_event_type": "put_up"}]}
    monkeypatch.setattr("redbox.pipeline.backfill_change_events",
                        lambda conn, apply=False: result)
    cfg = Config(raw={"database_path": str(db)}, repo_root=tmp_path)

    assert cli.cmd_backfill_changes(argparse.Namespace(apply=False), cfg) == 0
    out = capsys.readouterr().out
    assert "Mismatched" in out
    assert "put_up -> take_down" in out              # recorded -> expected
    assert "Alice Smith" in out
    assert "https://x.example/media" in out
    assert "0 missing, 0 spurious, 1 mismatched" in out
    assert "--apply" in out                          # dry run points at apply mode
