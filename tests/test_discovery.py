"""Offline tests for discovery grouping logic (spec §3.1, acceptance #1)."""
from __future__ import annotations

import pytest

from redbox.config import Config
from redbox.discovery import Discovery
from redbox.ratings.fixture import FixtureRatingAdapter


def _cand(cid, name, office, state, district, party, ici=None):
    d = {
        "candidate_id": cid, "name": name, "office": office,
        "state": state, "district": district, "party": party,
    }
    if ici:
        d["incumbent_challenge"] = ici
    return d


class FakeFEC:
    """Stand-in for FECClient returning synthetic candidates + receipts."""

    def __init__(self, candidates, receipts):
        self._candidates = candidates
        self._receipts = receipts

    def candidates(self, *, election_year, office, state=None, **kw):
        return [
            c for c in self._candidates
            if c["office"] == office and (state is None or c["state"] == state)
        ]

    def candidate_totals(self, candidate_id, *, cycle, use_cache=True):
        if candidate_id not in self._receipts:
            return None
        return {"receipts": self._receipts[candidate_id]}

    def get(self, *a, **k):
        return {"results": []}


@pytest.fixture
def cfg():
    return Config(raw={
        "election_year": 2026,
        "receipts_floor": 50000,
        "rating_threshold": ["Tilt", "Lean", "Toss-up"],
    })


@pytest.fixture
def cfg_baseline():
    """cfg with the baseline-coverage rules (populations D/E) enabled."""
    return Config(raw={
        "election_year": 2026,
        "receipts_floor": 50000,
        "rating_threshold": ["Tilt", "Lean", "Toss-up"],
        "include_incumbents": True,
        "include_funded_nominees": True,
    })


def _discovery(cfg, candidates, receipts):
    fec = FakeFEC(candidates, receipts)
    return Discovery(cfg, fec, rating_adapter=FixtureRatingAdapter())


def test_contested_primary_requires_two_funded(cfg):
    candidates = [
        _cand("H1", "A", "H", "OH", "03", "DEM"),
        _cand("H2", "B", "H", "OH", "03", "DEM"),   # same group -> contested
        _cand("H3", "C", "H", "OH", "05", "DEM"),   # lone -> not contested
    ]
    receipts = {"H1": 100000, "H2": 80000, "H3": 250000}
    entries = _discovery(cfg, candidates, receipts).build_universe(offices=["H"])
    ids = {e.candidate.candidate_id for e in entries}
    assert ids == {"H1", "H2"}
    assert all("contested_primary" in e.reasons for e in entries)


def test_receipts_floor_drops_paper_candidates(cfg):
    candidates = [
        _cand("H1", "A", "H", "OH", "03", "DEM"),
        _cand("H2", "B", "H", "OH", "03", "DEM"),   # under floor -> dropped
    ]
    receipts = {"H1": 100000, "H2": 4000}
    # H2 dropped, so H1 is now a lone funded candidate -> not contested.
    entries = _discovery(cfg, candidates, receipts).build_universe(offices=["H"])
    assert entries == []


def test_party_split_is_not_contested(cfg):
    # Two funded candidates, same district, DIFFERENT parties -> not a primary.
    candidates = [
        _cand("H1", "A", "H", "OH", "03", "DEM"),
        _cand("H2", "B", "H", "OH", "03", "REP"),
    ]
    receipts = {"H1": 100000, "H2": 90000}
    entries = _discovery(cfg, candidates, receipts).build_universe(offices=["H"])
    assert entries == []


def test_competitive_overlay_and_both(cfg):
    # NC-01 is Toss-up in the fixture. One funded NC-01 candidate -> overlay only.
    # Add a second NC-01 same-party funded candidate -> contested AND competitive = both.
    candidates = [
        _cand("H1", "A", "H", "NC", "01", "DEM"),
        _cand("H2", "B", "H", "NC", "01", "DEM"),
        _cand("H3", "C", "H", "NY", "17", "DEM"),   # NY-17 is "Likely" -> dropped from overlay
    ]
    receipts = {"H1": 100000, "H2": 90000, "H3": 200000}
    entries = {e.candidate.candidate_id: e for e in
               _discovery(cfg, candidates, receipts).build_universe(offices=["H"])}
    assert set(entries) == {"H1", "H2"}   # H3 not contested, not competitive (Likely)
    for e in entries.values():
        assert e.reasons == {"contested_primary", "competitive_general"}
        assert Discovery.reason_label(e.reasons) == "both"
        assert e.rating == "Toss-up"


def test_same_person_two_ids_is_not_a_phantom_primary(cfg):
    # One person, two FEC ids, identical receipts -> must NOT read as contested.
    candidates = [
        _cand("H4NC08066", "HARRIS, MARK E", "H", "NC", "08", "REP"),
        _cand("H6NC09200", "HARRIS, MARK E", "H", "NC", "08", "REP"),
    ]
    receipts = {"H4NC08066": 749386.0, "H6NC09200": 749386.0}
    entries = _discovery(cfg, candidates, receipts).build_universe(offices=["H"])
    # Collapsed to one person -> lone funded candidate -> not in universe.
    assert entries == []


def test_same_person_different_name_spellings_still_collapse(cfg):
    # The two records of one person can SPELL the name differently (observed:
    # Bob Onder, MO-03). Identical receipts + same surname must still collapse.
    candidates = [
        _cand("H4MO03221", "ONDER, ROBERT FOR JR.", "H", "MO", "03", "REP"),
        _cand("H8MO09146", "ONDER JR, ROBERT FRANK", "H", "MO", "03", "REP"),
    ]
    receipts = {"H4MO03221": 656775.41, "H8MO09146": 656775.41}
    entries = _discovery(cfg, candidates, receipts).build_universe(offices=["H"])
    assert entries == []


def test_dedupe_keeps_incumbent_record_over_higher_sorting_stale_id(cfg_baseline):
    # Real ONDER shape (MO-03): the stale id H8MO09146 sorts lexicographically
    # HIGHER, but H4MO03221 is the FEC-flagged incumbent record. The old
    # greatest-id tiebreak kept the stale one; the incumbent record must win.
    candidates = [
        _cand("H4MO03221", "ONDER, ROBERT FOR JR.", "H", "MO", "03", "REP", ici="I"),
        _cand("H8MO09146", "ONDER JR, ROBERT FRANK", "H", "MO", "03", "REP"),
    ]
    receipts = {"H4MO03221": 656775.41, "H8MO09146": 656775.41}
    entries = _discovery(cfg_baseline, candidates, receipts).build_universe(offices=["H"])
    # Collapsed to ONE entry, and it is the incumbent's record — so the person
    # enters the universe on incumbency instead of vanishing.
    assert [e.candidate.candidate_id for e in entries] == ["H4MO03221"]
    assert "incumbent" in entries[0].reasons


def test_dedupe_no_incumbent_prefers_id_matching_rows_district(cfg_baseline):
    # Real HARRIS ids (NC-08), neither record flagged incumbent: prefer the id
    # whose embedded state+district (H4NC08066 -> NC-08) matches the row's
    # actual race over the one registered for another district (H6NC09200 ->
    # NC-09) — even though the latter sorts higher.
    candidates = [
        _cand("H4NC08066", "HARRIS, MARK E", "H", "NC", "08", "REP"),
        _cand("H6NC09200", "HARRIS, MARK E", "H", "NC", "08", "REP"),
    ]
    receipts = {"H4NC08066": 749386.0, "H6NC09200": 749386.0}
    entries = _discovery(cfg_baseline, candidates, receipts).build_universe(offices=["H"])
    assert [e.candidate.candidate_id for e in entries] == ["H4NC08066"]


def test_dedupe_falls_back_to_greatest_id_when_rules_are_neutral(cfg_baseline):
    # Neither record is the incumbent and BOTH ids embed the row's OH-03 ->
    # the pre-existing greatest-id behavior is preserved as the final fallback.
    candidates = [
        _cand("H0OH03111", "TWICE, TERRY", "H", "OH", "03", "DEM"),
        _cand("H8OH03222", "TWICE, TERRY", "H", "OH", "03", "DEM"),
    ]
    receipts = {"H0OH03111": 250000.0, "H8OH03222": 250000.0}
    entries = _discovery(cfg_baseline, candidates, receipts).build_universe(offices=["H"])
    assert [e.candidate.candidate_id for e in entries] == ["H8OH03222"]


def test_likely_and_solid_excluded_from_overlay(cfg):
    # Lone funded candidate in a "Likely" district -> not in universe at all.
    candidates = [_cand("H3", "C", "H", "NY", "17", "DEM")]
    receipts = {"H3": 200000}
    entries = _discovery(cfg, candidates, receipts).build_universe(offices=["H"])
    assert entries == []


# --- contested-general screen (population C) -----------------------------------
def test_contested_general_cross_party_both_over_floor(cfg):
    # Uncontested primaries, but two cross-party candidates each >= $100k ->
    # contested general; both enter the universe.
    candidates = [
        _cand("H1", "A", "H", "ME", "02", "DEM"),
        _cand("H2", "B", "H", "ME", "02", "REP"),
    ]
    receipts = {"H1": 500000, "H2": 120000}
    entries = {e.candidate.candidate_id: e for e in
               _discovery(cfg, candidates, receipts).build_universe(offices=["H"])}
    assert set(entries) == {"H1", "H2"}
    for e in entries.values():
        assert e.reasons == {"contested_general"}
        assert Discovery.reason_label(e.reasons) == "contested_general"


def test_contested_general_requires_both_over_general_floor(cfg):
    # A funded-but-under-$100k challenger does not make a contested general
    # (and the $500k lone-primary candidate stays out of the universe).
    candidates = [
        _cand("H1", "A", "H", "ME", "02", "DEM"),
        _cand("H2", "B", "H", "ME", "02", "REP"),
    ]
    receipts = {"H1": 500000, "H2": 90000}
    entries = _discovery(cfg, candidates, receipts).build_universe(offices=["H"])
    assert entries == []


def test_contested_general_requires_cross_party(cfg):
    # Two same-party candidates >= $100k are a contested PRIMARY only.
    candidates = [
        _cand("H1", "A", "H", "OH", "03", "DEM"),
        _cand("H2", "B", "H", "OH", "03", "DEM"),
    ]
    receipts = {"H1": 150000, "H2": 150000}
    entries = _discovery(cfg, candidates, receipts).build_universe(offices=["H"])
    assert all(e.reasons == {"contested_primary"} for e in entries)


def test_contested_general_dfl_counts_as_dem(cfg):
    # MN DFL is the Democratic affiliate: DFL vs DEM is NOT cross-party.
    candidates = [
        _cand("H1", "A", "H", "MN", "02", "DFL"),
        _cand("H2", "B", "H", "MN", "02", "DEM"),
    ]
    receipts = {"H1": 200000, "H2": 200000}
    entries = _discovery(cfg, candidates, receipts).build_universe(offices=["H"])
    assert all("contested_general" not in e.reasons for e in entries)


def test_contested_general_combines_with_contested_primary(cfg):
    # Two funded DEMs + one funded REP, all >= $100k: the DEMs are in a contested
    # primary AND a contested general; the REP rides in on the general alone.
    candidates = [
        _cand("H1", "A", "H", "PA", "01", "DEM"),
        _cand("H2", "B", "H", "PA", "01", "DEM"),
        _cand("H3", "C", "H", "PA", "01", "REP"),
    ]
    receipts = {"H1": 150000, "H2": 120000, "H3": 400000}
    entries = {e.candidate.candidate_id: e for e in
               _discovery(cfg, candidates, receipts).build_universe(offices=["H"])}
    assert set(entries) == {"H1", "H2", "H3"}
    assert entries["H1"].reasons == {"contested_primary", "contested_general"}
    assert (Discovery.reason_label(entries["H1"].reasons)
            == "contested_primary+contested_general")
    assert entries["H3"].reasons == {"contested_general"}


def test_fec_inactive_candidacies_dropped(cfg):
    # A withdrawn/superseded record (FEC candidate_inactive — e.g. a House
    # member now running for Senate) must not enter the universe, even if its
    # old district looks contested.
    class InactiveFEC(FakeFEC):
        def inactive_ids(self, candidate_ids, *, use_cache=True):
            return {"H1"} & set(candidate_ids)

    candidates = [
        _cand("H1", "SWITCHER, SAM", "H", "GA", "01", "REP"),
        _cand("H2", "RIVAL, RHEA", "H", "GA", "01", "REP"),
    ]
    receipts = {"H1": 6800000, "H2": 400000}
    fec = InactiveFEC(candidates, receipts)
    d = Discovery(cfg, fec, rating_adapter=FixtureRatingAdapter())
    entries = d.build_universe(offices=["H"])
    # H1 dropped; H2 alone is no longer a contested primary either.
    assert entries == []


def test_inactive_lookup_failure_fails_open(cfg):
    # An API error must not silently shrink the universe.
    class BrokenFEC(FakeFEC):
        def inactive_ids(self, candidate_ids, *, use_cache=True):
            raise RuntimeError("FEC down")

    candidates = [
        _cand("H1", "A", "H", "OH", "03", "DEM"),
        _cand("H2", "B", "H", "OH", "03", "DEM"),
    ]
    receipts = {"H1": 100000, "H2": 80000}
    fec = BrokenFEC(candidates, receipts)
    d = Discovery(cfg, fec, rating_adapter=FixtureRatingAdapter())
    entries = d.build_universe(offices=["H"])
    assert {e.candidate.candidate_id for e in entries} == {"H1", "H2"}


def test_contested_general_disabled_by_zero_floor():
    cfg = Config(raw={
        "election_year": 2026,
        "receipts_floor": 50000,
        "rating_threshold": ["Tilt", "Lean", "Toss-up"],
        "general_receipts_floor": 0,
    })
    candidates = [
        _cand("H1", "A", "H", "ME", "02", "DEM"),
        _cand("H2", "B", "H", "ME", "02", "REP"),
    ]
    receipts = {"H1": 500000, "H2": 120000}
    entries = _discovery(cfg, candidates, receipts).build_universe(offices=["H"])
    assert entries == []


# --- baseline coverage: incumbents (D) + funded presumptive nominees (E) -------
def test_subfloor_incumbent_included(cfg_baseline):
    # A safe-seat incumbent with token receipts and no funded opposition enters
    # on incumbency alone — but NOT as a funded_nominee (below the floor).
    candidates = [_cand("H1", "SAFE, SAM", "H", "OH", "05", "DEM", ici="I")]
    receipts = {"H1": 12000}
    entries = _discovery(cfg_baseline, candidates, receipts).build_universe(offices=["H"])
    assert len(entries) == 1
    assert entries[0].reasons == {"incumbent"}
    assert Discovery.reason_label(entries[0].reasons) == "incumbent"


def test_funded_incumbent_is_also_presumptive_nominee(cfg_baseline):
    candidates = [_cand("H1", "SAFE, SAM", "H", "OH", "05", "DEM", ici="I")]
    receipts = {"H1": 900000}
    entries = _discovery(cfg_baseline, candidates, receipts).build_universe(offices=["H"])
    assert entries[0].reasons == {"incumbent", "funded_nominee"}
    assert Discovery.reason_label(entries[0].reasons) == "incumbent+funded_nominee"


def test_funded_lone_filer_is_presumptive_nominee(cfg_baseline):
    # Lone funded non-incumbent (uncontested primary) -> presumptive nominee.
    candidates = [_cand("H1", "HOPEFUL, HOLLY", "H", "OH", "05", "REP")]
    receipts = {"H1": 60000}
    entries = _discovery(cfg_baseline, candidates, receipts).build_universe(offices=["H"])
    assert entries[0].reasons == {"funded_nominee"}


def test_contested_primary_members_are_not_presumptive_nominees(cfg_baseline):
    # Population E is lone-filer only: contested-primary candidates keep their
    # existing reason and the loser sweep decides who stays after the vote.
    candidates = [
        _cand("H1", "A", "H", "OH", "03", "DEM"),
        _cand("H2", "B", "H", "OH", "03", "DEM"),
    ]
    receipts = {"H1": 100000, "H2": 80000}
    entries = {e.candidate.candidate_id: e for e in
               _discovery(cfg_baseline, candidates, receipts).build_universe(offices=["H"])}
    assert entries["H1"].reasons == {"contested_primary"}
    assert entries["H2"].reasons == {"contested_primary"}


def test_baseline_rules_off_by_default(cfg):
    # Without the config flags, a sub-floor incumbent and a lone funded filer
    # stay out — the pre-2026-07 behavior.
    candidates = [
        _cand("H1", "SAFE, SAM", "H", "OH", "05", "DEM", ici="I"),
        _cand("H2", "HOPEFUL, HOLLY", "H", "OH", "06", "REP"),
    ]
    receipts = {"H1": 12000, "H2": 60000}
    entries = _discovery(cfg, candidates, receipts).build_universe(offices=["H"])
    assert entries == []


def test_baseline_rules_skip_presidential_in_midterm(cfg_baseline):
    # Presidential committees file continuously, but 2026 is a midterm: a lone
    # funded P filer (even flagged incumbent) is not on the November ballot.
    candidates = [_cand("P1", "PERENNIAL, PAT", "P", "00", "00", "IND", ici="I")]
    receipts = {"P1": 900000}
    entries = _discovery(cfg_baseline, candidates, receipts).build_universe(offices=["P"])
    assert entries == []


def test_baseline_rules_apply_to_presidential_in_on_year():
    # In a presidential cycle the same lone funded P filer IS presumptive.
    cfg = Config(raw={
        "election_year": 2028,
        "receipts_floor": 50000,
        "rating_threshold": ["Tilt", "Lean", "Toss-up"],
        "include_incumbents": True,
        "include_funded_nominees": True,
    })
    candidates = [_cand("P1", "PERENNIAL, PAT", "P", "00", "00", "IND")]
    receipts = {"P1": 900000}
    entries = _discovery(cfg, candidates, receipts).build_universe(offices=["P"])
    assert len(entries) == 1
    assert entries[0].reasons == {"funded_nominee"}


def test_incumbent_survives_general_mode_without_nominee_call(cfg_baseline):
    # Baseline coverage is mode-independent: in general mode an incumbent whose
    # race the nominee resolver can't call still enters on incumbency.
    from redbox.nominees import NomineeResolver

    candidates = [
        _cand("H1", "SAFE, SAM", "H", "OH", "05", "DEM", ici="I"),
        _cand("H2", "RIVAL, RHEA", "H", "OH", "05", "DEM"),  # contested, no feed
    ]
    receipts = {"H1": 900000, "H2": 80000}
    resolver = NomineeResolver(2026, overrides_path=None, feed=None)
    entries = _discovery(cfg_baseline, candidates, receipts).build_universe(
        offices=["H"], mode="general", nominee_resolver=resolver)
    assert {e.candidate.candidate_id for e in entries} == {"H1"}
    assert "incumbent" in entries[0].reasons


# --- scan modes: primary / general / full -------------------------------------
def test_general_mode_uncontested_auto_only_without_feed(cfg):
    # general mode = nominees. With no feed, a lone funded candidate of a party is
    # the de-facto nominee (uncontested-auto); a contested race is unresolved and
    # so contributes no nominee.
    from redbox.nominees import NomineeResolver

    candidates = [
        _cand("H1", "A", "H", "OH", "03", "DEM"),
        _cand("H2", "B", "H", "OH", "03", "DEM"),   # contested DEM primary
        _cand("H3", "C", "H", "OH", "05", "DEM"),   # lone -> uncontested nominee
    ]
    receipts = {"H1": 100000, "H2": 80000, "H3": 250000}
    resolver = NomineeResolver(2026, overrides_path=None, feed=None)
    entries = _discovery(cfg, candidates, receipts).build_universe(
        offices=["H"], mode="general", nominee_resolver=resolver)
    assert {e.candidate.candidate_id for e in entries} == {"H3"}
    e = entries[0]
    assert "nominee" in e.reasons and e.nominee_source == "uncontested"
    assert Discovery.reason_label(e.reasons) == "nominee"


def test_general_mode_feed_resolves_contested_winner(cfg):
    from redbox.nominees import NomineeResolver, RaceCall

    class _Feed:
        name = "fake"
        def calls(self, state=None):
            return [RaceCall("OH", "H", "3", "DEM", "Aaron Adams", source="fake")]

    candidates = [
        _cand("H1", "ADAMS, AARON", "H", "OH", "03", "DEM"),   # feed says this one won
        _cand("H2", "BELL, BETH", "H", "OH", "03", "DEM"),
    ]
    receipts = {"H1": 100000, "H2": 80000}
    resolver = NomineeResolver(2026, overrides_path=None, feed=_Feed())
    entries = _discovery(cfg, candidates, receipts).build_universe(
        offices=["H"], mode="general", nominee_resolver=resolver)
    assert {e.candidate.candidate_id for e in entries} == {"H1"}
    assert entries[0].nominee_source == "feed:fake"


def test_full_mode_phase_per_state(cfg):
    # Full mode: a state past its primary scans the NOMINEE; a state before its
    # primary scans the CONTESTED field.
    from datetime import date

    from redbox.nominees import NomineeResolver

    candidates = [
        _cand("T1", "TXLONE, PAT", "H", "TX", "01", "DEM"),   # TX past -> nominee (uncontested)
        _cand("N1", "NYA, ANA", "H", "NY", "12", "DEM"),      # NY future -> contested field
        _cand("N2", "NYB, BEN", "H", "NY", "12", "DEM"),
        _cand("N3", "NYLONE, LEE", "H", "NY", "05", "DEM"),   # NY future, lone -> excluded
    ]
    receipts = {"T1": 90000, "N1": 90000, "N2": 90000, "N3": 90000}
    resolver = NomineeResolver(2026, overrides_path=None, feed=None)
    entries = {e.candidate.candidate_id: e for e in _discovery(cfg, candidates, receipts).build_universe(
        offices=["H"], mode="full", nominee_resolver=resolver,
        today=date(2026, 6, 28),
        primary_dates={"TX": "2026-03-03", "NY": "2026-09-01"})}
    assert set(entries) == {"T1", "N1", "N2"}
    assert "nominee" in entries["T1"].reasons             # TX past primary
    assert "contested_primary" in entries["N1"].reasons   # NY pre-primary
    assert "nominee" not in entries["N1"].reasons


def test_primary_mode_is_unchanged_default(cfg):
    # Default mode ignores any nominee machinery and reproduces contested-primary.
    candidates = [
        _cand("H1", "A", "H", "OH", "03", "DEM"),
        _cand("H2", "B", "H", "OH", "03", "DEM"),
        _cand("H3", "C", "H", "OH", "05", "DEM"),
    ]
    receipts = {"H1": 100000, "H2": 80000, "H3": 250000}
    entries = _discovery(cfg, candidates, receipts).build_universe(offices=["H"])  # mode defaults to primary
    assert {e.candidate.candidate_id for e in entries} == {"H1", "H2"}
    assert all(e.nominee_source is None for e in entries)


def test_discover_does_not_resolve_and_rediscover_preserves_url(cfg, tmp_path):
    # Discovery no longer resolves URLs (that's the separate `resolve` step), and
    # re-running discover must NOT wipe a URL that `resolve` already filled in.
    from redbox.db import init_db

    candidates = [
        _cand("H1", "A", "H", "OH", "03", "DEM"),
        _cand("H2", "B", "H", "OH", "03", "DEM"),
    ]
    receipts = {"H1": 100000, "H2": 80000}
    disc = _discovery(cfg, candidates, receipts)
    entries = disc.build_universe(offices=["H"])
    conn = init_db(tmp_path / "db.sqlite")

    disc.persist(conn, entries)
    # discover leaves the URL unresolved
    assert conn.execute("SELECT website_url FROM candidates WHERE candidate_id='H1'").fetchone()[0] is None

    # `resolve` fills it in (simulated)
    conn.execute("UPDATE candidates SET website_url=?, url_source='wikipedia', url_verified=1 "
                 "WHERE candidate_id='H1'", ("https://aforcongress.com",))
    conn.commit()

    # re-running discover (same entries, still url=None) must preserve the resolution
    disc.persist(conn, entries)
    row = conn.execute("SELECT website_url, url_source, url_verified FROM candidates "
                       "WHERE candidate_id='H1'").fetchone()
    assert row[0] == "https://aforcongress.com"
    assert row[1] == "wikipedia"
    assert row[2] == 1
    conn.close()


def test_race_phase_honors_district_overrides():
    # A postponed district stays in its primary phase after the statewide
    # date passes; the rest of the state moves to the general phase.
    from datetime import date

    from redbox.discovery import Discovery

    dates = {"AL": "2026-05-19", "AL:H:01": "2026-08-11", "LA": "2026-05-16",
             "LA:H": "2026-11-03"}
    today = date(2026, 7, 28)
    phase = Discovery._race_phase
    assert phase("AL", "H", "01", today, dates) == "primary"   # postponed CD
    assert phase("AL", "H", "03", today, dates) == "general"   # voted 5/19
    assert phase("AL", "S", "00", today, dates) == "general"
    assert phase("LA", "H", "04", today, dates) == "primary"   # pushed to Nov
    assert phase("LA", "S", "00", today, dates) == "general"
    assert phase("ZZ", "H", "01", today, dates) == "primary"   # unknown state


def test_dfl_and_dem_share_one_primary_bucket(cfg):
    # The FEC codes MN candidates as both DEM and DFL, but they run in the
    # SAME primary. Raw party codes once split them into parallel buckets:
    # each side read as uncontested, and a lone DFL filer facing funded DEM
    # opponents was crowned a sole "funded presumptive nominee" (the live
    # MN-Sen shape: Flanagan/DFL vs Craig/DEM, both heavily funded).
    candidates = [
        _cand("S1", "FLANAGAN, MARGARET", "S", "MN", "00", "DFL"),
        _cand("S2", "CRAIG, ANGIE", "S", "MN", "00", "DEM"),
    ]
    receipts = {"S1": 6_000_000, "S2": 11_900_000}
    entries = {e.candidate.candidate_id: e for e in
               _discovery(cfg, candidates, receipts).build_universe(offices=["S"])}
    assert "contested_primary" in entries["S1"].reasons
    assert "contested_primary" in entries["S2"].reasons
    assert all("funded_nominee" not in e.reasons for e in entries.values())


def test_feed_call_crosswalks_to_dfl_coded_row():
    # A civicAPI race reporting party 'Democratic-Farmer-Labor' must land in
    # the same bucket as an FEC row coded 'DFL' (and as 'DEM' opponents) —
    # two independent normalizers once disagreed and MN races could never
    # crosswalk (the DB side bucketed 'DFL' verbatim).
    from redbox.nominees import CivicAPIFeed, NomineeResolver

    rows = [
        {"candidate_id": "S1", "name": "FLANAGAN, MARGARET", "office": "S",
         "state": "MN", "district": "00", "party": "DFL"},
        {"candidate_id": "S2", "name": "CRAIG, ANGIE", "office": "S",
         "state": "MN", "district": "00", "party": "DEM"},
    ]
    races = [{"type": "Senate", "province": "MN", "district": "MN",
              "election_type": "Primary", "election_date": "2026-08-11T05:00:00.000Z",
              "candidates": [{"name": "Margaret Flanagan",
                              "party": "Democratic-Farmer-Labor", "winner": True}]}]
    feed = CivicAPIFeed(cycle=2026, get=lambda p: {"races": races, "count": 1})
    result = NomineeResolver(2026, overrides_path=None, feed=feed).resolve(
        rows, states={"MN"})
    assert result.candidate_ids() == {"S1"}


def test_reason_label_keeps_baseline_reasons_with_nominee():
    # The old nominee short-circuit dropped incumbent/funded_nominee reasons
    # that were deliberately unioned in. universe_reason is display-only
    # downstream, so the richer label is safe.
    assert Discovery.reason_label({"nominee"}) == "nominee"
    assert Discovery.reason_label({"nominee", "incumbent"}) == "nominee+incumbent"
    assert (Discovery.reason_label({"nominee", "incumbent", "funded_nominee"})
            == "nominee+incumbent+funded_nominee")


# --- orphan report: persist() flags nothing, but reports demoted candidates ----
def _seed_active(conn, cid, name, office, state, district, inactive=None):
    conn.execute(
        """INSERT INTO candidates (candidate_id,name,office,state,district,party,
           cycle,universe_reason,url_verified,receipts,inactive,created_at,updated_at)
           VALUES (?,?,?,?,?,'DEM',2026,'contested_primary',0,100000,?,'t','t')""",
        (cid, name, office, state, district, inactive))
    conn.commit()


def test_persist_reports_orphans_scoped_to_run(cfg, tmp_path, capsys):
    from redbox.db import init_db
    conn = init_db(tmp_path / "db.sqlite")
    _seed_active(conn, "H_ORPH", "HENRY, JON", "H", "NC", "06")     # left the file
    _seed_active(conn, "H_NY", "OUTOF, SCOPE", "H", "NY", "01")     # other state
    _seed_active(conn, "H_INACT", "GONE, GARY", "H", "NC", "07", inactive=2)
    candidates = [
        _cand("H1", "A", "H", "NC", "08", "DEM"),
        _cand("H2", "B", "H", "NC", "08", "DEM"),
    ]
    receipts = {"H1": 100000, "H2": 80000}
    disc = _discovery(cfg, candidates, receipts)
    entries = disc.build_universe(offices=["H"], states=["NC"])
    disc.persist(conn, entries)
    out = capsys.readouterr().out
    assert "H_ORPH HENRY, JON (NC-06)" in out
    assert "no longer in the FEC universe" in out
    assert "H_NY" not in out                # out of the run's state scope
    assert "H_INACT" not in out             # already inactive -> not an orphan
    # Report-only: nothing was flagged or modified.
    assert conn.execute("SELECT inactive FROM candidates "
                        "WHERE candidate_id='H_ORPH'").fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM candidates "
                        "WHERE COALESCE(inactive,0)=0").fetchone()[0] == 4
    conn.close()


def test_persist_reports_orphans_unscoped_run(cfg, tmp_path, capsys):
    from redbox.db import init_db
    conn = init_db(tmp_path / "db.sqlite")
    _seed_active(conn, "H_ORPH", "HENRY, JON", "H", "TN", "06")
    _seed_active(conn, "S_ORPH", "SENATE, SUE", "S", "TN", "00")
    candidates = [
        _cand("H1", "A", "H", "NC", "08", "DEM"),
        _cand("H2", "B", "H", "NC", "08", "DEM"),
    ]
    receipts = {"H1": 100000, "H2": 80000}
    disc = _discovery(cfg, candidates, receipts)
    entries = disc.build_universe(offices=["H", "S"])   # no state scope
    disc.persist(conn, entries)
    out = capsys.readouterr().out
    assert "H_ORPH HENRY, JON (TN-06)" in out
    assert "S_ORPH SENATE, SUE (TN-Sen)" in out
    conn.close()


def test_persist_no_orphan_report_when_universe_covers_everyone(cfg, tmp_path, capsys):
    from redbox.db import init_db
    conn = init_db(tmp_path / "db.sqlite")
    candidates = [
        _cand("H1", "A", "H", "NC", "08", "DEM"),
        _cand("H2", "B", "H", "NC", "08", "DEM"),
    ]
    receipts = {"H1": 100000, "H2": 80000}
    disc = _discovery(cfg, candidates, receipts)
    entries = disc.build_universe(offices=["H"])
    disc.persist(conn, entries)          # first run: nothing pre-existing
    capsys.readouterr()
    disc.persist(conn, entries)          # re-persist: same universe -> silent
    assert "orphan" not in capsys.readouterr().out
    conn.close()


def test_presidential_filers_excluded_from_all_populations_in_midterm(cfg_baseline):
    # Presidential committees file continuously, so cumulative receipts read
    # as current-cycle money — in a midterm there IS no presidential race,
    # and P records must not enter ANY population (contested_primary /
    # contested_general once let them in: every P filer shares one
    # state='00' bucket, so two junk-party filers read as a contested
    # cross-party general).
    candidates = [
        _cand("P1", "HOPEFUL, HARRY", "P", "00", "00", "REP"),
        _cand("P2", "WISHFUL, WANDA", "P", "00", "00", "REP"),
        _cand("P3", "NOVEL, NED", "P", "00", "00", "NNE"),
        _cand("H1", "REAL, RITA", "H", "TX", "01", "DEM", ici="I"),
    ]
    receipts = {"P1": 5_000_000, "P2": 2_000_000, "P3": 25_000_000, "H1": 500_000}
    entries = {e.candidate.candidate_id: e for e in
               _discovery(cfg_baseline, candidates, receipts).build_universe(
                   offices=["H", "S", "P"])}
    assert set(entries) == {"H1"}
