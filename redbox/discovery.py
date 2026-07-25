"""Discovery — build the candidate universe (spec §3.1).

Three populations, unioned and deduped into ``candidates``:

A. Contested primaries (primary target): funded candidates grouped by
   (office, state, district, party); any group with >=2 funded candidates is a
   contested primary.
B. Competitive-general overlay: candidates in districts a ratings adapter marks
   Tilt/Lean/Toss-up.
C. Contested generals: districts where >=2 candidates from >=2 parties each
   clear the (higher) ``general_receipts_floor``. Pure FEC data — no ratings
   dependence — so it catches funded general-election contests (e.g. a safe
   incumbent vs a $100k+ challenger) that ratings would never call competitive,
   and candidates who sail through uncontested primaries into funded generals.
D. Incumbents (config ``include_incumbents``): every sitting incumbent (FEC
   I flag), even below ``receipts_floor`` and with no funded opposition —
   safe-seat members are who visitors look up, so coverage can't hinge on
   their race being contested.
E. Funded presumptive nominees (config ``include_funded_nominees``): a race's
   sole funded candidate of a party (uncontested primary -> presumptive
   nominee) clearing ``receipts_floor``.

A candidate in both A and B is tagged ``universe_reason = both`` (legacy label);
other combinations are '+'-joined, e.g. ``contested_primary+contested_general``.
"""
from __future__ import annotations

import csv
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Iterable


def _as_date(s: str | None) -> "date | None":
    """Parse an ISO date prefix ('2026-03-03...') -> date, or None."""
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None

from .config import Config
from .fec import FECClient
from .ratings.base import RaceRating, RatingAdapter
from .util import now_iso
from .website import ResolvedURL, WebsiteResolver

# State-affiliate party codes folded into their national party for the
# contested-general cross-party test (MN Democratic-Farmer-Labor, ND
# Democratic-Nonpartisan League). Without this, DFL vs DEM would read as a
# cross-party general contest.
_PARTY_ALIASES = {"DFL": "DEM", "DNL": "DEM"}


def _norm_party(party: str | None) -> str:
    p = (party or "").strip().upper()
    return _PARTY_ALIASES.get(p, p)


@dataclass
class FundedCandidate:
    raw: dict[str, Any]
    receipts: float

    @property
    def candidate_id(self) -> str:
        return self.raw["candidate_id"]

    @property
    def group_key(self) -> tuple[str, str, str, str]:
        return (
            self.raw.get("office", ""),
            self.raw.get("state", "") or "",
            str(self.raw.get("district", "") or ""),
            self.raw.get("party", "") or "",
        )

    @property
    def district_key(self) -> tuple[str, str, str]:
        return (
            self.raw.get("office", ""),
            self.raw.get("state", "") or "",
            str(self.raw.get("district", "") or ""),
        )


@dataclass
class UniverseEntry:
    candidate: FundedCandidate
    reasons: set[str] = field(default_factory=set)
    rating: str | None = None
    url: ResolvedURL | None = None
    # For general/full modes: how this candidate was confirmed the nominee
    # (uncontested | feed:<name> | manual) — surfaced for the review gate so a
    # feed-resolved nominee is visibly less certain than a manual/uncontested one.
    nominee_source: str | None = None


class Discovery:
    def __init__(
        self,
        config: Config,
        fec: FECClient,
        rating_adapter: RatingAdapter | None = None,
        resolver: WebsiteResolver | None = None,
        weball_path: str | Path | None = None,
    ) -> None:
        self.cfg = config
        self.fec = fec
        self.ratings = rating_adapter
        self.resolver = resolver or WebsiteResolver(
            fec_client=fec, user_agent=config.user_agent)
        # If a FEC bulk 'weball' file is given (and exists), discovery reads
        # candidates + receipts from it — no per-candidate API calls.
        self.weball_path = Path(weball_path) if weball_path else None

    # ------------------------------------------------------------------
    def _funded_candidates(
        self, *, offices: Iterable[str], states: Iterable[str] | None,
        district: str | None = None,
    ) -> list[FundedCandidate]:
        """Pull candidates and keep only those above the receipts floor."""
        if self.weball_path and self.weball_path.exists():
            return self._funded_from_weball(
                offices=offices, states=states, district=district)
        return self._funded_from_api(
            offices=offices, states=states, district=district)

    def _funded_from_weball(
        self, *, offices: Iterable[str], states: Iterable[str] | None,
        district: str | None = None,
    ) -> list[FundedCandidate]:
        """Funded candidates from the FEC bulk file (no API calls)."""
        from .weball import load_funded

        state_set = {s.upper() for s in states} if states else None
        rows = load_funded(
            self.weball_path, offices=set(offices), states=state_set,
            district=district, receipts_floor=self.cfg.receipts_floor,
            keep_subfloor_incumbents=self.cfg.include_incumbents)
        funded = [FundedCandidate(raw=r.as_candidate(), receipts=r.receipts) for r in rows]
        return self._dedupe_people(funded)

    def _funded_from_api(
        self, *, offices: Iterable[str], states: Iterable[str] | None,
        district: str | None = None,
    ) -> list[FundedCandidate]:
        """Pull candidates and keep only those above the receipts floor (API)."""
        year = self.cfg.election_year
        floor = self.cfg.receipts_floor
        state_list = list(states) if states else [None]
        extra = {"district": district} if district else None
        funded: list[FundedCandidate] = []
        seen: set[str] = set()
        for office in offices:
            for state in state_list:
                for cand in self.fec.candidates(
                    election_year=year, office=office, state=state, extra=extra
                ):
                    cid = cand.get("candidate_id")
                    if not cid or cid in seen:
                        continue
                    seen.add(cid)
                    try:
                        totals = self.fec.candidate_totals(cid, cycle=year)
                    except Exception as e:
                        # One candidate's totals failing (after the client's own
                        # retries) must not abort discovery for everyone else.
                        print(f"  warning: totals lookup failed for {cid} ({e}); skipping")
                        continue
                    receipts = float((totals or {}).get("receipts") or 0.0)
                    is_incumbent = (cand.get("incumbent_challenge") or "").upper() == "I"
                    if receipts >= floor or (self.cfg.include_incumbents and is_incumbent):
                        funded.append(FundedCandidate(raw=cand, receipts=receipts))
        return self._dedupe_people(funded)

    @staticmethod
    def _person_surname(name: str) -> str:
        """Surname for the dedupe person-key, robust to FEC spelling drift.

        One person's two records can format the name differently — observed:
        'ONDER, ROBERT FOR JR.' vs 'ONDER JR, ROBERT FRANK' (MO-03) — so exact
        names can't key a person. Take the part before the comma, drop suffix
        tokens (JR/SR/II/...), keep the first token."""
        last = (name or "").split(",", 1)[0].upper()
        toks = [t.strip(".") for t in last.split()
                if t.strip(".") not in ("JR", "SR", "II", "III", "IV", "V")]
        return toks[0] if toks else last.strip()

    @classmethod
    def _dedupe_people(cls, funded: list[FundedCandidate]) -> list[FundedCandidate]:
        """Collapse one person's multiple FEC candidate_ids into a single entry.

        A person can hold several candidate_ids across cycles/districts that all
        roll up to one principal committee, so their financial totals are
        identical to the dollar. Two such rows in the same group would otherwise
        register as a phantom contested primary (observed: MARK E HARRIS in
        NC-08 appearing as H4NC08066 *and* H6NC09200). We treat
        (surname, office, state, district, party, receipts) as a person key and
        keep one id (the lexicographically greatest, which favours the most
        recent cycle prefix). Identical receipts is the discriminating signal —
        two genuinely distinct same-surname candidates in the same race almost
        never match to the cent (the surname, not the full name, keys the person
        because the same person's records can spell their name differently).
        """
        by_person: dict[tuple, FundedCandidate] = {}
        for fc in funded:
            person_key = (cls._person_surname(fc.raw.get("name", "")),
                          *fc.group_key, round(fc.receipts, 2))
            existing = by_person.get(person_key)
            if existing is None or fc.candidate_id > existing.candidate_id:
                by_person[person_key] = fc
        return list(by_person.values())

    # ------------------------------------------------------------------
    def _contested_primaries(
        self, funded: list[FundedCandidate]
    ) -> set[str]:
        """Return candidate_ids in any (office,state,district,party) group of >=2."""
        groups: dict[tuple, list[FundedCandidate]] = defaultdict(list)
        for fc in funded:
            groups[fc.group_key].append(fc)
        contested: set[str] = set()
        for members in groups.values():
            if len(members) >= 2:
                contested.update(m.candidate_id for m in members)
        return contested

    def _contested_generals(self, funded: list[FundedCandidate]) -> set[str]:
        """Candidate_ids in a contested GENERAL: their (office, state, district)
        has >=2 candidates from >=2 distinct parties, each at/above
        ``general_receipts_floor``. Only the candidates clearing that floor are
        included — a $60k also-ran in a contested-general district doesn't ride
        in on it. Screened within the funded pool, so the effective floor is
        max(receipts_floor, general_receipts_floor). Floor of 0 disables."""
        floor = self.cfg.general_receipts_floor
        if not floor:
            return set()
        groups: dict[tuple, list[FundedCandidate]] = defaultdict(list)
        for fc in funded:
            if fc.receipts >= floor:
                groups[fc.district_key].append(fc)
        contested: set[str] = set()
        for members in groups.values():
            if len(members) >= 2 and len({_norm_party(m.raw.get("party")) for m in members}) >= 2:
                contested.update(m.candidate_id for m in members)
        return contested

    def _office_on_ballot(self, office: str) -> bool:
        """Whether this office holds an election in the configured cycle.
        House and Senate files only carry current-cycle campaigns, but
        presidential committees file continuously — in a midterm cycle a
        P record is never on the November ballot, so the baseline-coverage
        presumptions (populations D/E) must not apply to it."""
        if (office or "").upper() == "P":
            return self.cfg.election_year % 4 == 0
        return True

    def _incumbents(self, funded: list[FundedCandidate]) -> set[str]:
        """Candidate_ids of sitting incumbents (population D). Empty set when
        the ``include_incumbents`` rule is off."""
        if not self.cfg.include_incumbents:
            return set()
        return {
            fc.candidate_id for fc in funded
            if (fc.raw.get("incumbent_challenge") or "").upper() == "I"
            and self._office_on_ballot(fc.raw.get("office", ""))
        }

    def _funded_presumptive_nominees(self, funded: list[FundedCandidate]) -> set[str]:
        """Candidate_ids of each race's sole funded candidate of a party
        (population E): an uncontested primary makes them the presumptive
        nominee, and clearing ``receipts_floor`` (which ``_funded_candidates``
        already applied — except to floor-exempt incumbents, re-checked here)
        makes them worth scanning. Contested-primary winners are already
        covered by population A + the primary-loser sweep."""
        if not self.cfg.include_funded_nominees:
            return set()
        groups: dict[tuple, list[FundedCandidate]] = defaultdict(list)
        for fc in funded:
            groups[fc.group_key].append(fc)
        return {
            members[0].candidate_id for members in groups.values()
            if len(members) == 1 and members[0].receipts >= self.cfg.receipts_floor
            and self._office_on_ballot(members[0].raw.get("office", ""))
        }

    def _competitive_overlay(
        self, funded: list[FundedCandidate]
    ) -> dict[str, str]:
        """Map candidate_id -> rating for candidates in competitive districts."""
        if not self.ratings:
            return {}
        keep = set(self.cfg.rating_threshold)
        # normalise threshold wording the same way the adapter does
        from .ratings.base import _normalize

        keep = {_normalize(k) for k in keep}
        rated = self.ratings.competitive(cycle=self.cfg.election_year, keep=keep)
        by_district: dict[tuple[str, str, str], str] = {}
        for r in rated:
            key = (r.office, r.state, str(r.district or ""))
            by_district[key] = r.rating
            # at-large / statewide may be stored as "00"
            if r.district is None:
                by_district[(r.office, r.state, "00")] = r.rating
        out: dict[str, str] = {}
        for fc in funded:
            rating = by_district.get(fc.district_key)
            if rating:
                out[fc.candidate_id] = rating
        return out

    @staticmethod
    def _race_phase(state: str, today: "date | None",
                    primary_dates: dict[str, str] | None) -> str:
        """'general' if this state's primary is already past, else 'primary'.

        Used by ``full`` mode so each race is scanned for its CURRENT phase —
        states primary on different dates (March–September), so on any given day
        some races are pre-primary (scan the contested field) and others are
        post-primary (scan the nominee). Unknown date -> 'primary' (conservative:
        we don't assume a primary we can't date has happened)."""
        if not today or not primary_dates:
            return "primary"
        d = _as_date(primary_dates.get((state or "").upper()))
        return "general" if (d and today > d) else "primary"

    # ------------------------------------------------------------------
    def build_universe(
        self,
        *,
        offices: Iterable[str] = ("H", "S", "P"),
        states: Iterable[str] | None = None,
        district: str | None = None,
        mode: str = "primary",
        nominee_resolver: Any | None = None,
        today: "date | None" = None,
        primary_dates: dict[str, str] | None = None,
    ) -> list[UniverseEntry]:
        """Build the candidate universe for a scan ``mode`` (spec §3.1).

        - ``primary`` (default): contested primaries (+ competitive-general overlay
          if a ratings adapter is configured) — the original behavior.
        - ``general``: primary WINNERS (nominees) only, resolved via
          ``nominee_resolver`` (uncontested-auto + manual override + results feed).
        - ``full``: per race, ``primary``-style selection while the race is
          pre-primary and ``general``-style (nominee) once its state has voted,
          using ``primary_dates`` (state -> ISO date) and ``today``.
        """
        funded = self._funded_candidates(offices=offices, states=states, district=district)
        # Drop withdrawn/superseded candidacies BEFORE grouping: a phantom
        # record's money must not make its old district look contested and pull
        # real candidates into the universe on its strength.
        funded = self._drop_inactive(funded)
        contested = self._contested_primaries(funded)
        competitive = self._competitive_overlay(funded)
        contested_general = self._contested_generals(funded)
        incumbents = self._incumbents(funded)
        presumptive = self._funded_presumptive_nominees(funded)

        nominees: dict[str, Any] = {}
        if mode in ("general", "full") and nominee_resolver is not None:
            nominees = nominee_resolver.resolve(
                [fc.raw for fc in funded], states=states).nominees

        def primary_reasons(cid: str) -> set[str]:
            r: set[str] = set()
            if cid in contested:
                r.add("contested_primary")
            if cid in competitive:
                r.add("competitive_general")
            if cid in contested_general:
                r.add("contested_general")
            return r

        def baseline_reasons(cid: str) -> set[str]:
            # Populations D/E apply in EVERY mode: an incumbent (or a lone
            # funded filer) is presumptively on the November ballot whether we
            # are scanning the primary field or resolved nominees — and if a
            # primary later removes them, the loser sweep deactivates the row.
            r: set[str] = set()
            if cid in incumbents:
                r.add("incumbent")
            if cid in presumptive:
                r.add("funded_nominee")
            return r

        entries: dict[str, UniverseEntry] = {}
        for fc in funded:
            cid = fc.candidate_id
            nominee = nominees.get(cid)
            if mode == "general":
                reasons = {"nominee"} if nominee else set()
            elif mode == "full":
                if self._race_phase(fc.raw.get("state", ""), today, primary_dates) == "general":
                    reasons = {"nominee"} if nominee else set()
                else:
                    reasons = primary_reasons(cid)
            else:  # primary
                reasons = primary_reasons(cid)
            reasons |= baseline_reasons(cid)
            if not reasons:
                continue
            entry = entries.setdefault(cid, UniverseEntry(candidate=fc))
            entry.reasons |= reasons
            if cid in competitive and "competitive_general" in reasons:
                entry.rating = competitive[cid]
            if nominee and "nominee" in reasons:
                entry.nominee_source = nominee.source
        # NOTE: discovery deliberately does NOT resolve campaign URLs. Resolution
        # (Wikipedia + billable web search) is a separate, restart-safe step run
        # via the `resolve` command, so building the universe stays fast and free
        # and a hung/slow web-search call can't stall discovery. Entries are
        # returned with url=None; `resolve` backfills website_url afterwards.
        return list(entries.values())

    def _drop_inactive(self, funded: list[FundedCandidate]) -> list[FundedCandidate]:
        """Drop candidacies the FEC marks candidate_inactive (withdrawn/
        superseded). The bulk weball file carries no such flag, so a House
        member now running for Senate keeps a funded-looking H record that
        reads as a phantom candidacy (the API discovery path already filters
        these via is_active_candidate). One batched API call per ~100 funded
        candidates, cached like every FEC response. Fail-OPEN: an API error
        must not silently shrink the universe (`mark-inactive` can re-flag
        later); a missing/None FEC client (offline weball runs) skips too."""
        lookup = getattr(self.fec, "inactive_ids", None)
        if not funded or lookup is None:
            return funded
        try:
            inactive = lookup([fc.candidate_id for fc in funded])
        except Exception as e:
            print(f"  warning: FEC inactive-flag lookup failed ({e}); "
                  f"keeping all candidates — run `mark-inactive` later")
            return funded
        if inactive:
            print(f"  dropped {len(inactive)} FEC-inactive candidacy(ies) "
                  f"(withdrawn/superseded records)")
        return [fc for fc in funded if fc.candidate_id not in inactive]

    @staticmethod
    def reason_label(reasons: set[str]) -> str:
        # "both" is the legacy label for exactly A+B; every other combination is
        # '+'-joined in a fixed order so labels are stable across runs.
        if reasons == {"contested_primary", "competitive_general"}:
            return "both"
        if "nominee" in reasons:
            return "nominee"
        order = ("contested_primary", "contested_general", "competitive_general",
                 "incumbent", "funded_nominee")
        return "+".join(r for r in order if r in reasons)

    # ------------------------------------------------------------------
    def persist(self, conn: sqlite3.Connection, entries: list[UniverseEntry]) -> int:
        ts = now_iso()
        rows = []
        for e in entries:
            c = e.candidate.raw
            url = e.url or ResolvedURL(None, "none", False)
            rows.append(
                (
                    c["candidate_id"], c.get("name"), c.get("office"), c.get("state"),
                    str(c.get("district") or ""), c.get("party"), self.cfg.election_year,
                    self.reason_label(e.reasons), None, url.url, url.source,
                    int(url.verified), e.candidate.receipts, e.rating,
                    e.nominee_source, ts, ts,
                )
            )
        conn.executemany(
            """
            INSERT INTO candidates (
                candidate_id, name, office, state, district, party, cycle,
                universe_reason, primary_date, website_url, url_source,
                url_verified, receipts, rating, nominee_source, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(candidate_id) DO UPDATE SET
                name=excluded.name, office=excluded.office, state=excluded.state,
                district=excluded.district, party=excluded.party,
                universe_reason=excluded.universe_reason, receipts=excluded.receipts,
                rating=excluded.rating, nominee_source=excluded.nominee_source,
                updated_at=excluded.updated_at
            -- Deliberately NOT updated on re-discover: website_url / url_source /
            -- url_verified. Those are owned by the `resolve` step; re-running
            -- discover (which no longer resolves) must not wipe a resolved URL
            -- back to NULL. New candidates still get url=NULL on first insert.
            """,
            rows,
        )
        conn.commit()
        return len(rows)

    @staticmethod
    def to_csv(entries: list[UniverseEntry], path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow([
                "candidate_id", "name", "office", "state", "district", "party",
                "universe_reason", "nominee_source", "rating", "receipts",
                "website_url", "url_source", "url_verified",
            ])
            for e in entries:
                c = e.candidate.raw
                url = e.url or ResolvedURL(None, "none", False)
                w.writerow([
                    c["candidate_id"], c.get("name"), c.get("office"), c.get("state"),
                    str(c.get("district") or ""), c.get("party"),
                    Discovery.reason_label(e.reasons), e.nominee_source or "",
                    e.rating or "", f"{e.candidate.receipts:.0f}", url.url or "",
                    url.source, "yes" if url.verified else "no",
                ])
        return path
