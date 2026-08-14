"""Nominee resolution — who won each primary (general/full scan modes).

In ``general``/``full`` scan modes we scan primary WINNERS (nominees), not the
whole contested-primary field. There is no FEC field for "won the primary" (the
FEC is a finance regulator, not an election authority), and no results feed
carries FEC candidate IDs — so a nominee is resolved by mapping a race
``(state, office, district, party)`` to one of the FEC-funded candidates in it,
first hit wins:

  1. manual override   -> verified   (data/nominees/<cycle>.json, untracked;
                                      may name a cid OUTSIDE the funded set —
                                      still honored, with a warning and a
                                      ``missing``-flagged synthetic member)
  2. uncontested-auto  -> high       (exactly one funded candidate of that party
                                      in the race == the de-facto nominee)
  3. results feed      -> medium     (a CALLED primary winner crosswalked to the
                                      FEC candidate by state/office/district/party
                                      + fuzzy name; e.g. :class:`CivicAPIFeed`)

The design is feed-agnostic: the resolver works with layers 1–2 alone (no network
dependency), and any object exposing ``calls(state=...) -> list[RaceCall]`` can be
dropped in as layer 3. ``CivicAPIFeed`` is one such adapter.

Why an unverified feed is acceptable here: nominee selection only decides WHO IS
SCANNED. It never publishes. A wrong/missing nominee at worst scans the wrong
candidate (a cheap, honest negative) or misses one — it can NEVER produce a false
published finding, which sits behind the human review gate (spec §3.7). So a feed
is usable as an INPUT even if we wouldn't trust it as an AUTHORITY. Contested
races a feed can't resolve are surfaced as ``unresolved`` rather than guessed.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

from .website import WikipediaResolver  # reuse fec_to_name + honorific handling

OVERRIDES_DIR = Path(__file__).resolve().parent.parent / "data" / "nominees"

_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V"}


# --- normalization: make FEC and feed values comparable in a race bucket -------
# One canonical normalizer shared with discovery grouping — see util.norm_party.
from .util import norm_party  # noqa: F401  (re-exported: tests/callers import it here)


def norm_district(office: str | None, district: Any) -> str:
    """Canonical district for bucketing.

    House -> the zero-stripped number ('01' and '1' collapse; a 'TX-01' style
    prefix is dropped). Senate / President / at-large -> '0' (no district).
    """
    d = str(district if district is not None else "").strip().upper()
    if "-" in d:                       # 'TX-01' -> '01'
        d = d.rsplit("-", 1)[-1]
    if (office or "") in ("S", "P") or d in ("", "00", "0", "AL", "ATLARGE"):
        return "0"
    return d.lstrip("0") or "0"


def _feed_first_last(name: str) -> tuple[str, str]:
    """Split a feed-reported name -> (first, last). Handles 'First M. Last' and
    'Last, First', and drops a trailing Jr/Sr/III suffix from the last name."""
    name = (name or "").strip()
    if "," in name:
        last, _, rest = name.partition(",")
        toks = rest.strip().split()
        return (toks[0].title() if toks else ""), last.strip().title()
    toks = [t for t in name.split() if t.strip(".")]
    if not toks:
        return "", ""
    last = toks[-1]
    if last.strip(".").upper() in _SUFFIXES and len(toks) >= 2:
        last = toks[-2]
    return toks[0].title(), last.title()


def names_match(fec_name: str, feed_name: str) -> bool:
    """True if an FEC 'LAST, FIRST ...' name and a feed 'First Last' name are the
    same person: last name must match; first name matches on prefix or initial
    (handles nicknames / middle-name noise). Within a single race bucket the
    candidate set is tiny, so this is safe without being brittle on formatting."""
    ff, fl = WikipediaResolver.fec_to_name(fec_name)
    gf, gl = _feed_first_last(feed_name)
    if not fl or not gl or fl.lower() != gl.lower():
        return False
    a, b = ff.lower(), gf.lower()
    if not a or not b:
        return True                    # last matched and a first name is missing
    return a.startswith(b) or b.startswith(a) or a[0] == b[0]


# --- data shapes ---------------------------------------------------------------
@dataclass
class RaceCall:
    """A CALLED primary winner from a results feed, pre-crosswalk."""
    state: str
    office: str          # H | S | P
    district: str        # canonical (norm_district)
    party: str           # canonical (norm_party)
    winner_name: str
    source: str = "feed"
    election_date: str | None = None


@dataclass
class Nominee:
    candidate_id: str
    name: str
    state: str
    office: str
    district: str
    party: str
    source: str          # manual | uncontested | feed:<name>
    confidence: str      # verified | high | medium


@dataclass
class NomineeResult:
    nominees: dict[str, Nominee] = field(default_factory=dict)   # candidate_id -> Nominee
    # contested race buckets (>=2 funded candidates) with no resolved nominee:
    unresolved: list[tuple[str, str, str, str]] = field(default_factory=list)
    # states whose feed fetch RAISED (outage), as opposed to answering with no
    # calls — downstream must treat these as "no information", not "no nominee":
    feed_failed_states: set[str] = field(default_factory=set)

    def candidate_ids(self) -> set[str]:
        return set(self.nominees)


def _bucket_key(office: str, state: str, district: Any, party: str) -> tuple[str, str, str, str]:
    return (office or "", (state or "").upper(),
            norm_district(office, district), norm_party(party))


def _first_name_rank(fec_name: str, feed_first: str) -> int:
    """Strength of the first-name agreement: exact(3) > prefix(2) > last-only(1)
    > initial-only(0). Used to disambiguate same-surname candidates in a bucket
    (e.g. TROY vs TREVER NEHLS) so the exact first-name match wins, not a coin
    flip on the shared initial."""
    ff = WikipediaResolver.fec_to_name(fec_name)[0].lower()
    b = (feed_first or "").lower()
    if not ff or not b:
        return 1                       # last name matched, a first name missing
    if ff == b:
        return 3
    if ff.startswith(b) or b.startswith(ff):
        return 2
    return 0                           # shared initial only (weakest)


def crosswalk(call: RaceCall, candidates: Iterable[dict]) -> str | None:
    """Map a called winner to ONE FEC candidate_id within its race bucket.

    Returns None on no match OR a genuinely ambiguous one (two equally-strong
    name matches) — a coverage gap is reported honestly upstream; a wrong
    attribution is not.
    """
    key = (call.office, call.state.upper(), call.district, call.party)
    bucket = [c for c in candidates
              if _bucket_key(c.get("office"), c.get("state"),
                             c.get("district"), c.get("party")) == key]
    feed_first, _ = _feed_first_last(call.winner_name)
    matches = [(_first_name_rank(c.get("name", ""), feed_first), c)
               for c in bucket if names_match(c.get("name", ""), call.winner_name)]
    if not matches:
        return None
    best = max(rank for rank, _ in matches)
    top = [c for rank, c in matches if rank == best]
    return top[0]["candidate_id"] if len(top) == 1 else None


class NomineeResolver:
    def __init__(self, cycle: int, *, overrides_path: Path | None = None,
                 feed: Any | None = None):
        self.cycle = cycle
        self.overrides_path = overrides_path or (OVERRIDES_DIR / f"{cycle}.json")
        self._overrides = self._load_overrides()
        self.feed = feed                       # exposes .calls(state=...) or None

    def _load_overrides(self) -> dict[str, dict[str, Any]]:
        if self.overrides_path and self.overrides_path.exists():
            data = json.loads(self.overrides_path.read_text())
            return {k: v for k, v in data.items() if not k.startswith("_")}
        return {}

    @staticmethod
    def _key_str(key: tuple[str, str, str, str]) -> str:
        return "-".join(key)

    def resolve(self, candidates: list[dict], *,
                states: Iterable[str] | None = None) -> NomineeResult:
        """Resolve nominees among ``candidates`` (FEC-funded universe rows).

        ``states`` (2-letter codes) optionally limits both the candidate scope
        and which states the feed is queried for. Each candidate dict needs
        candidate_id, name, office, state, district, party.
        """
        want = {s.upper() for s in states} if states else None
        buckets: dict[tuple, list[dict]] = {}
        for c in candidates:
            st = (c.get("state") or "").upper()
            if want and st not in want:
                continue
            key = _bucket_key(c.get("office"), st, c.get("district"), c.get("party"))
            buckets.setdefault(key, []).append(c)

        result = NomineeResult()
        resolved: set[tuple] = set()

        def add(c: dict, key: tuple, source: str, confidence: str) -> None:
            office, st, dist, party = key
            result.nominees[c["candidate_id"]] = Nominee(
                candidate_id=c["candidate_id"], name=c.get("name", ""),
                state=st, office=office, district=dist, party=party,
                source=source, confidence=confidence)
            resolved.add(key)

        # 1. manual overrides (verified) — always win. An override may name a
        # cid with NO row in the funded set (below the receipts floor, or a
        # replacement nominee absent from the FEC file entirely): the human
        # call still stands — the bucket resolves and the named cid is the
        # nominee — but the synthetic member is flagged ``missing`` so
        # downstream code can tell it is not a real funded row, and a warning
        # makes the gap visible instead of silent.
        for key, members in buckets.items():
            ov = self._overrides.get(self._key_str(key))
            if not ov or not ov.get("candidate_id"):
                continue
            c = next((m for m in members if m["candidate_id"] == ov["candidate_id"]), None)
            if c is None:               # override names a cid not in the funded set
                c = {"candidate_id": ov["candidate_id"], "name": ov.get("name", ""),
                     "missing": True}
                print(f"WARNING: nominee override {self._key_str(key)} names "
                      f"{ov['candidate_id']} ({ov.get('name') or 'no name'}), "
                      f"which is not in the funded universe — honoring the "
                      f"override, but the nominee has no candidate row.",
                      file=sys.stderr)
            add(c, key, "manual", "verified")

        # 2. uncontested-auto: exactly one funded candidate of that party -> nominee.
        # Skipped in all-candidate-primary states (see TOP_TWO_STATES): everyone
        # shares one primary ballot and the top finishers OVERALL advance,
        # regardless of party — so the per-party nominee model is structurally
        # wrong there (a lone funded independent would be crowned "nominee"
        # despite being eliminated). Those races need verified calls: only a
        # manual override (layer 1) resolves them.
        for key, members in buckets.items():
            if (key not in resolved and len(members) == 1
                    and key[1] not in TOP_TWO_STATES):
                add(members[0], key, "uncontested", "high")

        # 3. results feed for the remaining contested buckets. Only states that
        # still HAVE unresolved contested buckets are queried — a state fully
        # resolved by overrides/uncontested-auto used to cost a full paginated
        # feed round-trip for nothing (~40 wasted fetches per sweep). Top-two
        # states are excluded for the same reason as layer 2: a per-party
        # primary "winner" is not a nominee there.
        if self.feed is not None:
            feed_states = sorted({k[1] for k, m in buckets.items()
                                  if k not in resolved and len(m) >= 2
                                  and k[1] not in TOP_TWO_STATES})
            for st in feed_states:
                try:
                    calls = self.feed.calls(state=st)
                except Exception:
                    result.feed_failed_states.add(st)
                    continue
                for call in calls:
                    key = (call.office, call.state.upper(), call.district, call.party)
                    if (key in resolved or key not in buckets
                            or key[1] in TOP_TWO_STATES):
                        continue
                    cid = crosswalk(call, buckets[key])
                    if cid:
                        c = next(m for m in buckets[key] if m["candidate_id"] == cid)
                        add(c, key, f"feed:{getattr(self.feed, 'name', 'feed')}", "medium")

        result.unresolved = [k for k, m in buckets.items()
                             if k not in resolved and len(m) >= 2]
        return result


# --- flagging primary losers on an existing universe ---------------------------

# All-candidate-primary states: everyone shares one primary ballot and the
# top finishers OVERALL advance, regardless of party — so the per-party
# nominee model is structurally wrong there in both directions (a lone
# independent gets crowned "nominee" despite being eliminated; the runner-up
# in a same-party November matchup gets marked a "loser" despite being on the
# ballot). CA/WA are top-two; AK has been top-FOUR with a ranked-choice
# general since 2022 (the 2024 repeal measure failed), which is the same
# problem with four advancers. Never flag losers in these states; their races
# need verified calls (manual overrides or human review-console judgment).
TOP_TWO_STATES = {"AK", "CA", "WA"}


def flag_primary_losers(conn, resolver: "NomineeResolver", *,
                        states: Iterable[str], ts: str,
                        exclude: Iterable[tuple] = (),
                        allow_mass_clear: bool = False,
                        ) -> tuple[list[dict], int, int, set[str]]:
    """Mark primary losers (candidates.inactive=3) among a DB's active rows.

    For each race bucket in ``states`` where ``resolver`` affirmatively names a
    nominee, every other candidate in the bucket lost their primary. Unresolved
    buckets are untouched — a feed coverage gap must not invent losers. Rows
    already at inactive=3 are re-checked each run: cleared if their race no
    longer resolves (or now resolves to them), so the flag self-heals as feed
    data improves. A state whose feed fetch FAILED (or a run with no feed at
    all) is "no information", not "no nominee": existing marks there are kept
    unless the race affirmatively resolves. Top-two states are excluded
    outright (see TOP_TWO_STATES).

    A manual override may name a nominee with NO ``candidates`` row at all
    (below the receipts floor, or a replacement nominee absent from the FEC
    file). A primary winner belongs in the universe regardless of the floor:
    any such nominee gets a minimal row INSERTed here, derived from the race
    bucket plus the override's name (universe_reason='nominee',
    nominee_source='manual'). An existing row is never overwritten.

    ``exclude`` is a set of ``(office, STATE, district_or_None)`` races whose
    primary has NOT yet happened even though the state's statewide date has
    passed (per-district postponements — e.g. AL CDs moved by proclamation,
    LA's House primary pushed to November). Their candidates are dropped from
    consideration entirely: a feed that still carries a voided earlier result
    must not mark losers in a race that hasn't voted.

    Self-heal safety: clearing is driven by ABSENCE of evidence (a feed that
    answers 200-with-no-calls where it used to call races), so a feed that
    merely goes quiet for a state must not un-mark its settled races en masse.
    If one run would clear more than max(5, 20% of that state's currently
    marked rows), those clears are REFUSED (and the would-be-cleared names
    printed) unless ``allow_mass_clear`` is set; small corrections — the
    normal self-heal — still apply, and every applied clear prints the
    candidate's name.

    Returns (newly_lost_rows, n_cleared, n_unresolved, feed_failed_states)."""
    states = sorted({s.upper() for s in states} - TOP_TWO_STATES)
    if not states:
        return [], 0, 0, set()
    exclude = {(o, (s or "").upper(), d) for o, s, d in exclude}

    def _excluded(c) -> bool:
        for off, st, dd in exclude:
            if (c.get("office") == off and (c.get("state") or "").upper() == st
                    and (dd is None or c.get("district") == dd)):
                return True
        return False

    rows = [dict(r) for r in conn.execute(
        f"""SELECT * FROM candidates
            WHERE COALESCE(inactive,0) IN (0, 3)
              AND state IN ({','.join('?' * len(states))})""", states)]
    rows = [c for c in rows if not _excluded(c)]
    result = resolver.resolve(rows, states=states)
    nominee_ids = result.candidate_ids()
    resolved_keys = {_bucket_key(n.office, n.state, n.district, n.party)
                     for n in result.nominees.values()}
    no_feed_info = set(states) if resolver.feed is None else result.feed_failed_states
    lost: list[dict] = []
    to_clear: list[dict] = []
    for c in rows:
        key = _bucket_key(c.get("office"), c.get("state"),
                          c.get("district"), c.get("party"))
        if key in resolved_keys and c["candidate_id"] not in nominee_ids:
            if c.get("inactive") != 3:
                conn.execute("UPDATE candidates SET inactive=3, updated_at=? "
                             "WHERE candidate_id=?", (ts, c["candidate_id"]))
                lost.append(c)
        elif c.get("inactive") == 3 and (
                key in resolved_keys                      # now resolves to them
                or (c.get("state") or "").upper() not in no_feed_info):
            to_clear.append(c)

    # Mass-clear guard (see docstring): per state, refuse a clear batch bigger
    # than max(5, 20% of the rows currently marked there).
    marked_by_state: dict[str, int] = {}
    for c in rows:
        if c.get("inactive") == 3:
            st = (c.get("state") or "").upper()
            marked_by_state[st] = marked_by_state.get(st, 0) + 1
    clears_by_state: dict[str, list[dict]] = {}
    for c in to_clear:
        clears_by_state.setdefault((c.get("state") or "").upper(), []).append(c)
    cleared = 0
    for st, cands in sorted(clears_by_state.items()):
        limit = max(5, 0.2 * marked_by_state.get(st, 0))
        if len(cands) > limit and not allow_mass_clear:
            print(f"REFUSING to clear {len(cands)} of "
                  f"{marked_by_state.get(st, 0)} lost-primary mark(s) in {st} "
                  f"in one run (limit {limit:.0f}): a feed going quiet is not "
                  f"positive evidence a race un-resolved. Would have cleared:")
            for c in cands:
                print(f"    would clear: {c['candidate_id']} {c.get('name')} "
                      f"({c.get('office')}-{st}-{c.get('district')})")
            print("  (pass allow_mass_clear / an --allow-mass-clear flag to "
                  "apply these clears deliberately)")
            continue
        for c in cands:
            conn.execute("UPDATE candidates SET inactive=NULL, updated_at=? "
                         "WHERE candidate_id=?", (ts, c["candidate_id"]))
            print(f"  cleared lost-primary mark: {c['candidate_id']} "
                  f"{c.get('name')} ({c.get('office')}-{st}-{c.get('district')})")
            cleared += 1
    # Only a manual override can name a nominee outside the funded set (the
    # other layers pick from actual bucket members); make sure each such
    # nominee has a candidates row so the general-mode scan tracks the actual
    # winner, not just their marked losers.
    for n in result.nominees.values():
        if n.source != "manual":
            continue
        if conn.execute("SELECT 1 FROM candidates WHERE candidate_id=?",
                        (n.candidate_id,)).fetchone():
            continue
        conn.execute(
            """INSERT INTO candidates (candidate_id, name, office, state,
                   district, party, cycle, universe_reason, url_source,
                   url_verified, nominee_source, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,'nominee','none',0,'manual',?,?)""",
            (n.candidate_id, n.name, n.office, n.state, n.district, n.party,
             resolver.cycle, ts, ts))
        print(f"inserted nominee {n.candidate_id} ({n.name or 'no name'}) for "
              f"{n.office}-{n.state}-{n.district}-{n.party} "
              f"(universe_reason=nominee): manual override names a candidate "
              f"outside the funded universe")
    conn.commit()
    return lost, cleared, len(result.unresolved), no_feed_info


# --- civicAPI results feed adapter --------------------------------------------
class CivicAPIFeed:
    """Pluggable nominee feed backed by civicAPI (https://civicapi.org).

    civicAPI is free and key-less; a "Primary" race is one race PER PARTY PER
    district, with a ``winner`` boolean on the nominee — which lines up exactly
    with our ``(state, office, district, party)`` bucket. We emit a RaceCall only
    for races that are CALLED (a candidate flagged ``winner``). Coverage and
    timeliness of down-ballot federal primaries should be validated empirically
    (see ``scripts/validate_civicapi.py``) before relying on it; it is an input,
    never an authority (see module docstring). ``get`` is injectable for testing.
    """

    name = "civicapi"
    BASE = "https://civicapi.org/api/v2/race/search"
    # civicAPI race ``type`` -> our office code. Only federal offices are mapped.
    TYPE_TO_OFFICE = {
        "House of Representatives": "H",
        "U.S. House": "H",
        "U.S. Senate": "S",
        "Senate": "S",
        "President": "P",
    }

    def __init__(self, *, cycle: int | None = None,
                 user_agent: str = "RedBoxTracker/0.1", timeout: float = 30.0,
                 page_size: int = 100,
                 get: Callable[[dict], dict] | None = None):
        self.cycle = cycle
        self.user_agent = user_agent
        self.timeout = timeout
        self.page_size = page_size
        self._get = get or self._http_get

    def _http_get(self, params: dict) -> dict:
        r = httpx.get(self.BASE, params=params,
                      headers={"User-Agent": self.user_agent}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # No state has anywhere near this many races; a server that ignores
    # `offset` (or reports a bogus `count`) must not loop forever.
    MAX_PAGES = 50

    def _pages(self, state: str) -> Iterable[dict]:
        """Yield each race object for a state, paginating province=<code>."""
        offset = 0
        for _ in range(self.MAX_PAGES):
            data = self._get({"province": state.upper(), "country": "US",
                              "limit": self.page_size, "offset": offset})
            races = data.get("races", []) or []
            if not races:
                return
            yield from races
            offset += len(races)
            if offset >= int(data.get("count", 0) or 0) and len(races) < self.page_size:
                return
        raise RuntimeError(
            f"civicAPI pagination did not terminate for {state} "
            f"(> {self.MAX_PAGES} pages) — endpoint ignoring offset?")

    def calls(self, state: str | None = None) -> list[RaceCall]:
        if not state:
            return []
        # A runoff state returns separate "Primary" race objects for the initial
        # primary AND the runoff, each flagging a "winner" — and the initial one's
        # winner may just be the top-two finisher heading to the runoff. Keep the
        # LATEST-dated call per race bucket so the runoff nominee wins, not a
        # March top-two finisher. (election_date is YYYY-MM-DD: lexical == chrono.)
        best: dict[tuple[str, str, str, str], RaceCall] = {}
        for race in self._pages(state):
            office = self.TYPE_TO_OFFICE.get(race.get("type", ""))
            if not office or race.get("election_type") != "Primary":
                continue
            date = (race.get("election_date") or "")
            if self.cycle and date[:4] and date[:4] != str(self.cycle):
                continue
            winner = next((c for c in race.get("candidates", []) if c.get("winner")), None)
            if not winner:
                continue               # race not called -> not a nominee yet
            call = RaceCall(
                state=(race.get("province") or state).upper(),
                office=office,
                district=norm_district(office, race.get("district")),
                party=norm_party(winner.get("party")),
                winner_name=winner.get("name", ""),
                source=self.name,
                election_date=date[:10] or None)
            key = (call.office, call.state, call.district, call.party)
            cur = best.get(key)
            if cur is None or (call.election_date or "") > (cur.election_date or ""):
                best[key] = call
        return list(best.values())
