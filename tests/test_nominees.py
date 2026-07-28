"""Offline tests for nominee resolution (general/full scan modes). No network."""
from __future__ import annotations

import json

from redbox.nominees import (CivicAPIFeed, Nominee, NomineeResolver, RaceCall,
                             crosswalk, names_match, norm_district, norm_party)


def _cand(cid, name, office, state, district, party):
    return {"candidate_id": cid, "name": name, "office": office, "state": state,
            "district": district, "party": party}


# --- normalization -------------------------------------------------------------
def test_norm_party_collapses_variants():
    assert norm_party("Democratic") == norm_party("DEM") == norm_party("D") == "DEM"
    assert norm_party("Republican") == norm_party("GOP") == norm_party("R") == "REP"


def test_norm_district_house_vs_statewide():
    assert norm_district("H", "01") == norm_district("H", "1") == norm_district("H", "TX-01") == "1"
    assert norm_district("S", "00") == norm_district("S", None) == "0"
    assert norm_district("H", "AL") == "0"          # at-large


def test_names_match_handles_formats_and_nicknames():
    assert names_match("PRINCE, YOLANDA R", "Yolanda R. Prince")
    assert names_match("BARTIE, THURMAN BILL", "Thurman Bill Bartie")
    assert names_match("GREEN, ALEXANDER", "Al Green")        # nickname prefix/initial
    assert not names_match("SMITH, JOHN", "Jane Doe")
    assert not names_match("SMITH, JOHN", "John Smithson")    # different last name


# --- crosswalk -----------------------------------------------------------------
def test_crosswalk_matches_within_bucket():
    cands = [
        _cand("H1", "PRINCE, YOLANDA R", "H", "TX", "01", "DEM"),
        _cand("H2", "ALEXANDER, DAX", "H", "TX", "01", "DEM"),
        _cand("H3", "OTHER, PERSON", "H", "TX", "02", "DEM"),   # wrong district
    ]
    call = RaceCall("TX", "H", "1", "DEM", "Yolanda R. Prince")
    assert crosswalk(call, cands) == "H1"


def test_crosswalk_returns_none_when_winner_not_in_funded_set():
    # Winner cleared no FEC receipts floor -> not in the bucket -> honest gap, no guess.
    cands = [_cand("H2", "ALEXANDER, DAX", "H", "TX", "01", "DEM")]
    call = RaceCall("TX", "H", "1", "DEM", "Yolanda R. Prince")
    assert crosswalk(call, cands) is None


def test_crosswalk_disambiguates_same_surname_same_initial():
    # Real case (TX-22 REP): TREVER vs TROY Nehls. The exact first-name match must
    # win over the shared-initial near-match, not collapse to ambiguous-None.
    cands = [
        _cand("N1", "NEHLS, TROY", "H", "TX", "22", "REP"),
        _cand("N2", "NEHLS, TREVER", "H", "TX", "22", "REP"),
    ]
    assert crosswalk(RaceCall("TX", "H", "22", "REP", "Trever Nehls"), cands) == "N2"
    assert crosswalk(RaceCall("TX", "H", "22", "REP", "Troy Nehls"), cands) == "N1"


def test_crosswalk_respects_party_bucket():
    cands = [_cand("R1", "BAUMAN, ADAM", "H", "TX", "16", "REP")]
    # A DEM call must not match the REP candidate even with the same district.
    assert crosswalk(RaceCall("TX", "H", "16", "DEM", "Adam Bauman"), cands) is None
    assert crosswalk(RaceCall("TX", "H", "16", "REP", "Adam Bauman"), cands) == "R1"


# --- resolver layers -----------------------------------------------------------
def test_uncontested_auto_resolves_single_candidate_bucket():
    cands = [_cand("S1", "SMITH, JANE", "S", "NY", "00", "DEM")]
    res = NomineeResolver(2026, overrides_path=None, feed=None).resolve(cands)
    assert res.nominees["S1"].source == "uncontested"
    assert res.nominees["S1"].confidence == "high"
    assert res.unresolved == []


def test_contested_without_feed_is_unresolved():
    cands = [
        _cand("H1", "PRINCE, YOLANDA R", "H", "TX", "01", "DEM"),
        _cand("H2", "ALEXANDER, DAX", "H", "TX", "01", "DEM"),
    ]
    res = NomineeResolver(2026, overrides_path=None, feed=None).resolve(cands)
    assert res.nominees == {}
    assert res.unresolved == [("H", "TX", "1", "DEM")]


class _FakeFeed:
    name = "fake"

    def __init__(self, calls):
        self._calls = calls

    def calls(self, state=None):
        return [c for c in self._calls if c.state == state]


def test_feed_resolves_contested_bucket():
    cands = [
        _cand("H1", "PRINCE, YOLANDA R", "H", "TX", "01", "DEM"),
        _cand("H2", "ALEXANDER, DAX", "H", "TX", "01", "DEM"),
    ]
    feed = _FakeFeed([RaceCall("TX", "H", "1", "DEM", "Yolanda R. Prince", source="fake")])
    res = NomineeResolver(2026, overrides_path=None, feed=feed).resolve(cands, states=["TX"])
    assert set(res.nominees) == {"H1"}
    assert res.nominees["H1"].source == "feed:fake"
    assert res.nominees["H1"].confidence == "medium"
    assert res.unresolved == []


def test_manual_override_wins_over_feed_and_uncontested(tmp_path):
    ov = tmp_path / "2026.json"
    ov.write_text(json.dumps({"H-TX-1-DEM": {"candidate_id": "H2", "name": "Dax Alexander"}}))
    cands = [
        _cand("H1", "PRINCE, YOLANDA R", "H", "TX", "01", "DEM"),
        _cand("H2", "ALEXANDER, DAX", "H", "TX", "01", "DEM"),
    ]
    # Feed would pick H1, but the human override pins H2.
    feed = _FakeFeed([RaceCall("TX", "H", "1", "DEM", "Yolanda R. Prince", source="fake")])
    res = NomineeResolver(2026, overrides_path=ov, feed=feed).resolve(cands, states=["TX"])
    assert set(res.nominees) == {"H2"}
    assert res.nominees["H2"].source == "manual"
    assert res.nominees["H2"].confidence == "verified"


# --- civicAPI feed adapter parsing (injected JSON, no network) -----------------
def _civic_page(races, count=None):
    return {"count": count if count is not None else len(races),
            "offset": 0, "limit": 100, "races": races}


def test_civicapi_feed_emits_called_primary_winners_only():
    races = [
        {"type": "House of Representatives", "province": "TX", "district": "TX-01",
         "election_type": "Primary", "election_date": "2026-05-26T05:00:00.000Z",
         "candidates": [
             {"name": "Yolanda R. Prince", "party": "Democratic", "winner": True},
             {"name": "Dax Alexander", "party": "Democratic", "winner": False}]},
        # Not called yet (no winner) -> skipped.
        {"type": "House of Representatives", "province": "TX", "district": "TX-02",
         "election_type": "Primary", "election_date": "2026-05-26T05:00:00.000Z",
         "candidates": [{"name": "A B", "party": "Democratic", "winner": False}]},
        # General election, not a primary -> skipped.
        {"type": "House of Representatives", "province": "TX", "district": "TX-03",
         "election_type": "General", "election_date": "2026-11-03T05:00:00.000Z",
         "candidates": [{"name": "C D", "party": "Republican", "winner": True}]},
        # Non-federal office -> skipped.
        {"type": "Governor", "province": "TX", "district": None,
         "election_type": "Primary", "election_date": "2026-03-03T05:00:00.000Z",
         "candidates": [{"name": "E F", "party": "Republican", "winner": True}]},
    ]
    feed = CivicAPIFeed(cycle=2026, get=lambda params: _civic_page(races))
    calls = feed.calls(state="TX")
    assert len(calls) == 1
    c = calls[0]
    assert (c.office, c.state, c.district, c.party) == ("H", "TX", "1", "DEM")
    assert c.winner_name == "Yolanda R. Prince"
    assert c.election_date == "2026-05-26"


def test_civicapi_feed_filters_off_cycle_races():
    races = [{"type": "U.S. Senate", "province": "TX", "district": None,
              "election_type": "Primary", "election_date": "2024-03-05T05:00:00.000Z",
              "candidates": [{"name": "Old Winner", "party": "Republican", "winner": True}]}]
    feed = CivicAPIFeed(cycle=2026, get=lambda params: _civic_page(races))
    assert feed.calls(state="TX") == []


def test_civicapi_feed_keeps_latest_dated_call_per_bucket():
    # Runoff state: same bucket appears as the March primary and the May runoff,
    # with DIFFERENT winners. The latest-dated (runoff) winner must be the one kept.
    races = [
        {"type": "House of Representatives", "province": "TX", "district": "TX-17",
         "election_type": "Primary", "election_date": "2026-03-03T05:00:00.000Z",
         "candidates": [{"name": "Milah Flores", "party": "Democratic", "winner": True}]},
        {"type": "House of Representatives", "province": "TX", "district": "TX-17",
         "election_type": "Primary", "election_date": "2026-05-26T05:00:00.000Z",
         "candidates": [{"name": "Casey Shepard", "party": "Democratic", "winner": True}]},
    ]
    feed = CivicAPIFeed(cycle=2026, get=lambda params: _civic_page(races))
    calls = feed.calls(state="TX")
    assert len(calls) == 1
    assert calls[0].winner_name == "Casey Shepard"
    assert calls[0].election_date == "2026-05-26"


def test_civicapi_feed_paginates():
    # Two full pages then empty; ensure offset advances and all are returned.
    page_a = [{"type": "House of Representatives", "province": "TX", "district": f"TX-{i:02d}",
               "election_type": "Primary", "election_date": "2026-03-03T05:00:00.000Z",
               "candidates": [{"name": f"Win {i}", "party": "Democratic", "winner": True}]}
              for i in range(1, 101)]
    page_b = [{"type": "House of Representatives", "province": "TX", "district": "TX-77",
               "election_type": "Primary", "election_date": "2026-03-03T05:00:00.000Z",
               "candidates": [{"name": "Win 77b", "party": "Republican", "winner": True}]}]
    pages = {0: _civic_page(page_a, count=101), 100: _civic_page(page_b, count=101), 101: _civic_page([])}
    feed = CivicAPIFeed(cycle=2026, page_size=100, get=lambda p: pages[p["offset"]])
    calls = feed.calls(state="TX")
    assert len(calls) == 101


# ---------------------------------------------------------------------------
# flag_primary_losers: mark defeated candidates on an existing universe DB

def _loser_db(tmp_path):
    from redbox.db import init_db
    conn = init_db(tmp_path / "db.sqlite")
    rows = [
        # TX-01 DEM: contested, feed will call Prince the winner -> Kolman lost.
        ("T1", "PRINCE, YOLANDA", "H", "TX", "01", "DEM"),
        ("T2", "KOLMAN, DAVID", "H", "TX", "01", "DEM"),
        # TX-02 REP: contested, feed silent -> unresolved, everyone stays.
        ("T3", "AAA, ANN", "H", "TX", "02", "REP"),
        ("T4", "BBB, BOB", "H", "TX", "02", "REP"),
        # TX-03 DEM: uncontested -> lone member is the nominee, no losers.
        ("T5", "SOLO, SAL", "H", "TX", "03", "DEM"),
        # NY-01 DEM: contested but NY not in scope (primary not past).
        ("N1", "NYA, ANA", "H", "NY", "01", "DEM"),
        ("N2", "NYB, BEN", "H", "NY", "01", "DEM"),
    ]
    conn.executemany(
        """INSERT INTO candidates (candidate_id,name,office,state,district,party,
           cycle,universe_reason,url_verified,receipts,created_at,updated_at)
           VALUES (?,?,?,?,?,?,2026,'contested_primary',0,100000,'t','t')""", rows)
    conn.commit()
    return conn


def test_flag_primary_losers_marks_only_called_races(tmp_path):
    from redbox.nominees import NomineeResolver, flag_primary_losers
    conn = _loser_db(tmp_path)
    feed = _FakeFeed([RaceCall("TX", "H", "1", "DEM", "Yolanda Prince", source="fake")])
    resolver = NomineeResolver(2026, overrides_path=None, feed=feed)
    lost, cleared, unresolved, feed_failed = flag_primary_losers(
        conn, resolver, states={"TX"}, ts="2026-07-16T00:00:00+00:00")
    assert [c["candidate_id"] for c in lost] == ["T2"]     # Kolman lost
    assert cleared == 0
    assert unresolved == 1                                 # TX-02 REP uncalled
    assert feed_failed == set()
    flags = {r[0]: r[1] for r in conn.execute("SELECT candidate_id, inactive FROM candidates")}
    assert flags["T2"] == 3
    # winner, unresolved race, uncontested, and out-of-scope NY all untouched
    assert all(flags[c] is None for c in ("T1", "T3", "T4", "T5", "N1", "N2"))
    conn.close()


def test_flag_primary_losers_self_heals_when_race_unresolves(tmp_path):
    from redbox.nominees import NomineeResolver, flag_primary_losers
    conn = _loser_db(tmp_path)
    feed = _FakeFeed([RaceCall("TX", "H", "1", "DEM", "Yolanda Prince", source="fake")])
    resolver = NomineeResolver(2026, overrides_path=None, feed=feed)
    flag_primary_losers(conn, resolver, states={"TX"}, ts="t1")
    # Feed data regresses (race no longer called) -> the mark is cleared.
    empty = NomineeResolver(2026, overrides_path=None, feed=_FakeFeed([]))
    lost, cleared, _, _ = flag_primary_losers(conn, empty, states={"TX"}, ts="t2")
    assert lost == [] and cleared == 1
    assert conn.execute("SELECT inactive FROM candidates WHERE candidate_id='T2'").fetchone()[0] is None
    conn.close()


def test_flag_primary_losers_keeps_marks_through_feed_outage(tmp_path):
    # A feed OUTAGE (fetch raises) is "no information", not "race unresolved":
    # marks made from earlier feed data must survive, unlike the genuine
    # regression in the self-heal test above (feed answers with no calls).
    from redbox.nominees import NomineeResolver, flag_primary_losers
    conn = _loser_db(tmp_path)
    feed = _FakeFeed([RaceCall("TX", "H", "1", "DEM", "Yolanda Prince", source="fake")])
    resolver = NomineeResolver(2026, overrides_path=None, feed=feed)
    flag_primary_losers(conn, resolver, states={"TX"}, ts="t1")

    class _DownFeed:
        name = "down"

        def calls(self, state=None):
            raise ConnectionError("origin unreachable")

    down = NomineeResolver(2026, overrides_path=None, feed=_DownFeed())
    lost, cleared, _, feed_failed = flag_primary_losers(
        conn, down, states={"TX"}, ts="t2")
    assert lost == [] and cleared == 0
    assert feed_failed == {"TX"}
    assert conn.execute("SELECT inactive FROM candidates "
                        "WHERE candidate_id='T2'").fetchone()[0] == 3
    conn.close()


def test_flag_primary_losers_no_feed_run_keeps_feed_marks(tmp_path):
    # --no-feed (resolver.feed is None) must not clear marks that only the
    # feed can re-substantiate.
    from redbox.nominees import NomineeResolver, flag_primary_losers
    conn = _loser_db(tmp_path)
    feed = _FakeFeed([RaceCall("TX", "H", "1", "DEM", "Yolanda Prince", source="fake")])
    resolver = NomineeResolver(2026, overrides_path=None, feed=feed)
    flag_primary_losers(conn, resolver, states={"TX"}, ts="t1")

    feedless = NomineeResolver(2026, overrides_path=None, feed=None)
    lost, cleared, _, feed_failed = flag_primary_losers(
        conn, feedless, states={"TX"}, ts="t2")
    assert lost == [] and cleared == 0
    assert feed_failed == {"TX"}
    assert conn.execute("SELECT inactive FROM candidates "
                        "WHERE candidate_id='T2'").fetchone()[0] == 3
    conn.close()


def test_flag_primary_losers_never_touches_top_two_states(tmp_path):
    # CA/WA top-two: the per-party nominee model is structurally wrong (a
    # same-party November runner-up would read as a "loser"), so the sweep
    # must not flag anyone there even with a called feed race.
    from redbox.db import init_db
    from redbox.nominees import NomineeResolver, flag_primary_losers
    conn = init_db(tmp_path / "db.sqlite")
    conn.executemany(
        """INSERT INTO candidates (candidate_id,name,office,state,district,party,
           cycle,universe_reason,url_verified,receipts,created_at,updated_at)
           VALUES (?,?,?,?,?,?,2026,'contested_primary',0,100000,'t','t')""",
        [("C1", "WINNER, WANDA", "H", "CA", "11", "DEM"),
         ("C2", "RUNNERUP, RUTH", "H", "CA", "11", "DEM")])
    conn.commit()
    feed = _FakeFeed([RaceCall("CA", "H", "11", "DEM", "Wanda Winner", source="fake")])
    resolver = NomineeResolver(2026, overrides_path=None, feed=feed)
    lost, cleared, unresolved, _ = flag_primary_losers(
        conn, resolver, states={"CA"}, ts="t")
    assert lost == [] and cleared == 0 and unresolved == 0
    assert conn.execute("SELECT COUNT(*) FROM candidates WHERE inactive=3").fetchone()[0] == 0
    conn.close()


def test_civicapi_pagination_terminates_on_broken_offset():
    # A server that ignores `offset` (same full page forever) must raise
    # rather than loop forever inside mark-primary-losers.
    import pytest

    page = [{"type": "House of Representatives", "province": "TX", "district": "TX-01",
             "election_type": "Primary", "election_date": "2026-03-03T05:00:00.000Z",
             "candidates": [{"name": "W", "party": "Democratic", "winner": True}]}] * 100
    feed = CivicAPIFeed(cycle=2026, page_size=100,
                        get=lambda p: _civic_page(page, count=0))
    with pytest.raises(RuntimeError):
        feed.calls(state="TX")


def test_flag_primary_losers_never_touches_alaska(tmp_path):
    # AK is top-FOUR with a ranked-choice general: several same-party
    # candidates advance, so per-party loser marking is structurally wrong.
    from redbox.db import init_db
    from redbox.nominees import NomineeResolver, flag_primary_losers
    conn = init_db(tmp_path / "db.sqlite")
    conn.executemany(
        """INSERT INTO candidates (candidate_id,name,office,state,district,party,
           cycle,universe_reason,url_verified,receipts,created_at,updated_at)
           VALUES (?,?,?,?,?,?,2026,'contested_primary',0,100000,'t','t')""",
        [("A1", "FIRST, FRAN", "H", "AK", "00", "REP"),
         ("A2", "FOURTH, FAYE", "H", "AK", "00", "REP")])
    conn.commit()
    feed = _FakeFeed([RaceCall("AK", "H", "00", "REP", "Fran First", source="fake")])
    resolver = NomineeResolver(2026, overrides_path=None, feed=feed)
    lost, cleared, unresolved, _ = flag_primary_losers(
        conn, resolver, states={"AK"}, ts="t")
    assert lost == []
    assert conn.execute("SELECT COUNT(*) FROM candidates WHERE inactive=3").fetchone()[0] == 0
    conn.close()


def test_flag_primary_losers_excludes_postponed_races(tmp_path):
    # AL statewide primary has passed but CDs 1,2,6,7 were moved to a later
    # date: a feed still carrying the voided earlier result must not mark
    # losers in the postponed districts, while districts that DID vote are
    # swept normally.
    from redbox.db import init_db
    from redbox.nominees import NomineeResolver, flag_primary_losers
    conn = init_db(tmp_path / "db.sqlite")
    conn.executemany(
        """INSERT INTO candidates (candidate_id,name,office,state,district,party,
           cycle,universe_reason,url_verified,receipts,created_at,updated_at)
           VALUES (?,?,?,?,?,?,2026,'contested_primary',0,100000,'t','t')""",
        [("L1", "POSTPONED, PAT", "H", "AL", "01", "DEM"),
         ("L2", "POSTPONED, PAM", "H", "AL", "01", "DEM"),
         ("L3", "VOTED, VERA", "H", "AL", "03", "DEM"),
         ("L4", "VOTED, VAL", "H", "AL", "03", "DEM")])
    conn.commit()
    feed = _FakeFeed([
        RaceCall("AL", "H", "1", "DEM", "Pat Postponed", source="fake"),  # voided result
        RaceCall("AL", "H", "3", "DEM", "Vera Voted", source="fake"),
    ])
    resolver = NomineeResolver(2026, overrides_path=None, feed=feed)
    lost, cleared, unresolved, _ = flag_primary_losers(
        conn, resolver, states={"AL"}, ts="t",
        exclude={("H", "AL", "01"), ("H", "AL", "02"),
                 ("H", "AL", "06"), ("H", "AL", "07")})
    assert [c["candidate_id"] for c in lost] == ["L4"]
    marks = {r[0] for r in conn.execute(
        "SELECT candidate_id FROM candidates WHERE inactive=3")}
    assert marks == {"L4"}               # nobody in the postponed CD-01
    conn.close()
