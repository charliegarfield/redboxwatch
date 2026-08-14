"""Scheduler — per-state primary calendar + scan cadence (spec §3.2).

Primaries are spread roughly March–September with no single national date, so we
keep a per-state calendar (``elections`` table) and derive a per-candidate scan
cadence: begin at the filing deadline, scan **daily** in the final ~3 weeks
before that candidate's primary, **weekly** otherwise. After the primary,
surviving (active) candidates stay on cadence through the general election —
weekly, then daily in the final ~3 weeks before election day — since red-box
guidance often goes up (or changes) for the general race. Detection-time
archiving and change-diffing (in the pipeline) capture put-up/take-down events.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .util import as_date as _as_date

CALENDAR_FIXTURE = (
    Path(__file__).resolve().parent.parent / "fixtures" / "primary_calendar_2026.json"
)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def general_election_date(cycle: int) -> date:
    """Federal general election day: first Tuesday after the first Monday in
    November of the cycle year (2 U.S.C. §7)."""
    nov1 = date(cycle, 11, 1)
    first_monday = nov1 + timedelta(days=(0 - nov1.weekday()) % 7)
    return first_monday + timedelta(days=1)


# ---------------------------------------------------------------------------
def load_calendar(conn: sqlite3.Connection, *, cycle: int,
                  fixture_path: Path | None = None) -> int:
    """Populate the ``elections`` table from a primary-calendar fixture.

    Each fixture entry writes the statewide row (office='') plus, for any
    ``overrides`` (races whose primary was moved off the statewide date —
    e.g. AL CDs rescheduled by proclamation, LA's House primary pushed to
    November), office-scoped rows keyed 'H' (whole office) or 'H:01'
    (single district). Backfill resolves most-specific-first.

    An entry's optional ``runoff_date`` is persisted alongside the primary
    date: in a runoff state, first-round results can't settle who lost a race
    that advanced to the runoff, so consumers (mark-primary-losers) treat the
    race as undecided until the RUNOFF date has passed.
    """
    path = fixture_path or CALENDAR_FIXTURE
    rows = json.loads(path.read_text())

    def upsert(state, office, primary, filing, source, runoff):
        conn.execute(
            """INSERT INTO elections (state, cycle, office, primary_date,
                   filing_deadline, source, runoff_date)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(state, cycle, office) DO UPDATE SET
                   primary_date=excluded.primary_date,
                   filing_deadline=excluded.filing_deadline,
                   source=excluded.source,
                   runoff_date=excluded.runoff_date""",
            (state, cycle, office, primary, filing, source, runoff))

    n = 0
    for r in rows:
        upsert(r["state"], "", r.get("primary_date"),
               r.get("filing_deadline"), r.get("source"), r.get("runoff_date"))
        n += 1
        for o in r.get("overrides", []):
            for dd in (o.get("districts") or [None]):
                office_key = o["office"] + (f":{dd}" if dd else "")
                # NOT inherited from the statewide entry: a postponed race
                # runs on its own timetable (e.g. AL CDs voting 8/11 are not
                # settled/unsettled by the statewide 6/16 runoff).
                upsert(r["state"], office_key, o.get("primary_date"),
                       o.get("filing_deadline") or r.get("filing_deadline"),
                       r.get("source"), o.get("runoff_date"))
                n += 1
    conn.commit()
    return n


def backfill_primary_dates(conn: sqlite3.Connection, *, cycle: int) -> int:
    """Copy primary dates onto candidates, most-specific election row first.

    Precedence per candidate: district override ('H:01') > office override
    ('H') > statewide (''). Only candidates whose state HAS a calendar row
    are touched: an
    unconditional UPDATE would NULL out any hand-set or previously backfilled
    date for a state missing from the fixture, silently degrading those
    candidates to the no-primary-date cadence. The count returned is of rows
    actually updated.
    """
    cur = conn.execute(
        """UPDATE candidates SET primary_date = COALESCE(
               (SELECT e.primary_date FROM elections e
                WHERE e.state = candidates.state AND e.cycle = ?1
                  AND e.office = candidates.office || ':' || candidates.district),
               (SELECT e.primary_date FROM elections e
                WHERE e.state = candidates.state AND e.cycle = ?1
                  AND e.office = candidates.office),
               (SELECT e.primary_date FROM elections e
                WHERE e.state = candidates.state AND e.cycle = ?1
                  AND e.office = ''))
           WHERE cycle = ?1
             AND EXISTS (SELECT 1 FROM elections e
                         WHERE e.state = candidates.state AND e.cycle = ?1
                           AND e.primary_date IS NOT NULL
                           AND e.office IN ('', candidates.office,
                                            candidates.office || ':' || candidates.district))""",
        (cycle,))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
@dataclass
class DueItem:
    candidate: dict
    cadence: str               # daily | weekly
    days_to_primary: int | None
    reason: str
    days_to_general: int | None = None   # set when past the primary (general phase)


def due_candidates(
    conn: sqlite3.Connection, *, today: date | None = None,
    daily_window_days: int = 21, default_interval_days: int = 7,
    require_verified: bool = False,
) -> list[DueItem]:
    """Candidates due for a scan today under the cadence rules.

    A candidate is due when: it has a resolved ``website_url`` (and, if
    ``require_verified`` is set, a human-verified one), today falls between
    its filing deadline and the general election, and the time since its last
    scan *attempt* — the MAX of the newest ``scans.fetched_at`` and
    ``candidates.last_attempt_at``, so zero-page outcomes (robots_blocked,
    fetch_failed) still count — meets/exceeds the current cadence interval:
    daily inside the final
    ``daily_window_days`` before that candidate's primary or the general,
    weekly otherwise. Past the primary (losers are marked inactive) and when
    the primary date is unknown, cadence keys off the general election.
    """
    today = today or _today()
    out: list[DueItem] = []
    cands = conn.execute(
        "SELECT * FROM candidates WHERE COALESCE(inactive,0)=0").fetchall()
    # Filing deadlines, one SELECT up front instead of one per candidate.
    # Deliberately statewide (office=''): the filing deadline only gates when
    # scanning STARTS, and candidates in a postponed race are campaigning (and
    # red-boxing) on the statewide timetable regardless of their new date.
    filings = {(r["state"], r["cycle"]): r["filing_deadline"]
               for r in conn.execute(
                   "SELECT state, cycle, filing_deadline FROM elections "
                   "WHERE office=''")}
    for c in cands:
        c = dict(c)
        if not c.get("website_url"):
            continue                     # nothing to scan
        if require_verified and not c.get("url_verified"):
            continue
        prim = _as_date(c.get("primary_date"))
        filing = _as_date(filings.get((c.get("state"), c.get("cycle"))))
        general = general_election_date(c.get("cycle") or today.year)

        # Window gating: don't scan before the filing deadline or after the
        # general election. Candidates still active past their primary are
        # presumed advancers (losers get marked inactive) and stay on cadence.
        if today > general:
            continue
        if filing and today < filing:
            continue

        days_to_gen: int | None = None
        if prim and today <= prim:
            days_to = (prim - today).days
            if days_to <= daily_window_days:
                interval, cadence = 1, "daily"
            else:
                interval, cadence = default_interval_days, "weekly"
        else:
            # Past the primary — or the primary date is unknown (state missing
            # from the calendar, or the candidate was discovered after the
            # last backfill). Either way key cadence off the general: a flat
            # weekly that never escalated left unknown-date candidates
            # unprioritized on election eve.
            days_to = None
            days_to_gen = (general - today).days
            if days_to_gen <= daily_window_days:
                interval, cadence = 1, "daily"
            else:
                interval, cadence = default_interval_days, "weekly"

        # Last activity = the later of the newest fetched page and the last
        # concluded attempt (candidates.last_attempt_at). Zero-page outcomes
        # (robots_blocked / fetch_failed) write no `scans` rows, so without the
        # attempt stamp they'd look never-scanned and be due every day — a
        # robots-politeness problem for blocked domains, not just noise. Both
        # are ISO-8601 UTC strings, so lexicographic max is chronological.
        last = max(filter(None, (_last_scan(conn, c["candidate_id"]),
                                 c.get("last_attempt_at"))), default=None)
        last_date = _as_date(last)
        if last is None:
            due, reason = True, "never scanned"
        elif last_date is None:
            due, reason = True, "last scan date unparseable"
        else:
            elapsed = (today - last_date).days
            if elapsed < 0:
                # Scan timestamp is ahead of `today` (UTC vs local, or a backtest
                # date) — effectively just scanned; not due.
                due, reason = False, "scanned on/after today"
            else:
                due = elapsed >= interval
                reason = f"{elapsed}d since last attempt (interval {interval}d)"
        if due:
            out.append(DueItem(candidate=c, cadence=cadence,
                               days_to_primary=days_to, reason=reason,
                               days_to_general=days_to_gen))
    # Soonest next election (primary or general) first.
    def _next_election(d: DueItem) -> int:
        if d.days_to_primary is not None:
            return d.days_to_primary
        if d.days_to_general is not None:
            return d.days_to_general
        return 9999
    out.sort(key=_next_election)
    return out


def _last_scan(conn, candidate_id) -> str | None:
    r = conn.execute(
        "SELECT MAX(fetched_at) m FROM scans WHERE candidate_id=?", (candidate_id,)).fetchone()
    return r["m"] if r and r["m"] else None
