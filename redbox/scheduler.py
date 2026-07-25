"""Scheduler — per-state primary calendar + scan cadence (spec §3.2).

Primaries are spread roughly March–September with no single national date, so we
keep a per-state calendar (``elections`` table) and derive a per-candidate scan
cadence: begin at the filing deadline, scan **daily** in the final ~3 weeks
before that candidate's primary, **weekly** otherwise. Detection-time archiving
and change-diffing (in the pipeline) capture put-up/take-down events.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

CALENDAR_FIXTURE = (
    Path(__file__).resolve().parent.parent / "fixtures" / "primary_calendar_2026.json"
)


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _as_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
def load_calendar(conn: sqlite3.Connection, *, cycle: int,
                  fixture_path: Path | None = None) -> int:
    """Populate the ``elections`` table from a primary-calendar fixture."""
    path = fixture_path or CALENDAR_FIXTURE
    rows = json.loads(path.read_text())
    n = 0
    for r in rows:
        conn.execute(
            """INSERT INTO elections (state, cycle, office, primary_date,
                   filing_deadline, source)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(state, cycle, office) DO UPDATE SET
                   primary_date=excluded.primary_date,
                   filing_deadline=excluded.filing_deadline,
                   source=excluded.source""",
            (r["state"], cycle, "", r.get("primary_date"),
             r.get("filing_deadline"), r.get("source")))
        n += 1
    conn.commit()
    return n


def backfill_primary_dates(conn: sqlite3.Connection, *, cycle: int) -> int:
    """Copy each state's primary date onto its candidates (statewide office='')."""
    cur = conn.execute(
        """UPDATE candidates SET primary_date = (
               SELECT e.primary_date FROM elections e
               WHERE e.state = candidates.state AND e.cycle = ?
                 AND e.office = '' LIMIT 1)
           WHERE cycle = ?""", (cycle, cycle))
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
@dataclass
class DueItem:
    candidate: dict
    interval_days: int
    cadence: str               # daily | weekly | pre-filing
    last_scanned: str | None
    days_to_primary: int | None
    reason: str


def due_candidates(
    conn: sqlite3.Connection, *, today: date | None = None,
    daily_window_days: int = 21, default_interval_days: int = 7,
    require_verified: bool = False,
) -> list[DueItem]:
    """Candidates due for a scan today under the cadence rules.

    A candidate is due when: it has a resolved ``website_url`` (and, if
    ``require_verified`` is set, a human-verified one), today is on/after its
    filing deadline and on/before its primary, and the time since its last scan
    meets/exceeds the current cadence interval (daily within the pre-primary
    window, weekly otherwise).
    """
    today = today or _today()
    out: list[DueItem] = []
    cands = conn.execute(
        "SELECT * FROM candidates WHERE COALESCE(inactive,0)=0").fetchall()
    for c in cands:
        c = dict(c)
        if not c.get("website_url"):
            continue                     # nothing to scan
        if require_verified and not c.get("url_verified"):
            continue
        prim = _as_date(c.get("primary_date"))
        filing = _as_date(_filing_for(conn, c.get("state"), c.get("cycle")))

        # Window gating: don't scan before filing deadline or after the primary.
        if prim and today > prim:
            continue
        if filing and today < filing:
            out_reason = "before filing deadline"
            # still report as scheduled-but-not-started for visibility? skip.
            continue

        if prim:
            days_to = (prim - today).days
            if 0 <= days_to <= daily_window_days:
                interval, cadence = 1, "daily"
            else:
                interval, cadence = default_interval_days, "weekly"
        else:
            days_to, interval, cadence = None, default_interval_days, "weekly"

        last = _last_scan(conn, c["candidate_id"])
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
                reason = f"{elapsed}d since last scan (interval {interval}d)"
        if due:
            out.append(DueItem(candidate=c, interval_days=interval, cadence=cadence,
                               last_scanned=last, days_to_primary=days_to, reason=reason))
    # Soonest primaries first.
    out.sort(key=lambda d: (d.days_to_primary if d.days_to_primary is not None else 9999))
    return out


def _filing_for(conn, state, cycle):
    r = conn.execute(
        "SELECT filing_deadline FROM elections WHERE state=? AND cycle=? AND office='' LIMIT 1",
        (state, cycle)).fetchone()
    return r["filing_deadline"] if r else None


def _last_scan(conn, candidate_id) -> str | None:
    r = conn.execute(
        "SELECT MAX(fetched_at) m FROM scans WHERE candidate_id=?", (candidate_id,)).fetchone()
    return r["m"] if r and r["m"] else None
