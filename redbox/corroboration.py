"""Corroboration — Schedule E correlation (spec §3.6).

For a candidate with a positive detection, pull independent expenditures from
FEC Schedule E and correlate the spend with the guidance. The headline output is
the sequence: *guidance posted/updated around [date] → $X in aligned independent
expenditures from [committees]*.

Important framing (spec §3.6): Schedule E is **corroboration, not the trigger**.
A filed IE is *late* evidence — the red box goes up before the money to solicit
it — and our "first detected" timestamp is when *we* crawled, which can lag the
actual posting. So we compute and present:
  - the full aligned spend (supporting/opposing totals + per-committee breakdown
    with date ranges) — the real, newsworthy signal, and
  - the strict "supporting IE dated on/after our detection" figure, transparently
    labeled, since detection time may post-date the spend.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from .util import now_iso


@dataclass
class CommitteeSpend:
    committee_id: str | None
    committee_name: str | None
    indicator: str            # S | O
    amount: float
    count: int
    first_date: str | None
    last_date: str | None


@dataclass
class Corroboration:
    candidate_id: str
    detection_id: int | None
    guidance_first_detected: str | None
    supporting_total: float
    opposing_total: float
    supporting_after_detection: float
    ie_filing_count: int
    committees: list[CommitteeSpend] = field(default_factory=list)

    @property
    def headline(self) -> str:
        if self.ie_filing_count == 0:
            return "No independent expenditures on file for this candidate yet."
        names = ", ".join(sorted({c.committee_name or c.committee_id or "?"
                                  for c in self.committees if c.indicator == "S"}))
        return (f"${self.supporting_total:,.0f} in supporting independent "
                f"expenditures aligned with this candidate"
                + (f" from {names}" if names else ""))


# ---------------------------------------------------------------------------
def pull_and_store_ie(conn: sqlite3.Connection, fec, *, candidate_id: str,
                      cycle: int, use_cache: bool = True) -> int:
    """Pull all Schedule E (support + oppose) for a candidate and store fresh.

    Delete-then-insert per candidate makes re-runs idempotent and keeps the
    table in sync with current FEC state.
    """
    conn.execute("DELETE FROM ie_filings WHERE candidate_id=? AND cycle=?",
                 (candidate_id, cycle))
    rows = []
    for r in fec.schedule_e(candidate_id=candidate_id, cycle=cycle, use_cache=use_cache):
        committee = r.get("committee") or {}
        rows.append((
            candidate_id,
            r.get("committee_id"),
            committee.get("name") or r.get("committee_name"),
            r.get("payee_name"),
            r.get("support_oppose_indicator"),
            float(r.get("expenditure_amount") or 0.0),
            r.get("expenditure_date"),
            r.get("dissemination_date"),
            cycle,
            r.get("sub_id") or r.get("transaction_id"),
            json.dumps({k: r.get(k) for k in (
                "support_oppose_indicator", "expenditure_amount", "expenditure_date",
                "dissemination_date", "payee_name", "committee_id", "sub_id")}),
        ))
    conn.executemany(
        """INSERT OR IGNORE INTO ie_filings (candidate_id, committee_id,
               committee_name, payee_name, support_oppose_indicator,
               expenditure_amount, expenditure_date, dissemination_date, cycle,
               transaction_id, raw)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""", rows)
    conn.commit()
    return len(rows)


# Human review gates every published claim, so it gates corroboration too: a
# detection whose LATEST review is 'reject' must not date guidance_first_detected
# (the timeline the publisher prints) or pull FEC data. Latest-review-wins,
# same convention as the publisher and review console.
_NOT_REJECTED_JOIN = """
    LEFT JOIN (SELECT detection_id, action, ROW_NUMBER() OVER (
                   PARTITION BY detection_id
                   ORDER BY reviewed_at DESC, review_id DESC) rn
               FROM reviews) r
      ON r.detection_id = d.detection_id AND r.rn = 1"""


def _earliest_positive(conn: sqlite3.Connection, candidate_id: str):
    return conn.execute(
        f"""SELECT d.detection_id, d.classified_at FROM detections d
            {_NOT_REJECTED_JOIN}
            WHERE d.candidate_id=?
              AND d.classification IN ('red_box_guidance','ambiguous')
              AND COALESCE(r.action,'') != 'reject'
            ORDER BY d.classified_at ASC LIMIT 1""", (candidate_id,)).fetchone()


def compute(conn: sqlite3.Connection, candidate_id: str, *, cycle: int) -> Corroboration:
    """Aggregate stored IE filings (for one cycle) and write a corroboration record."""
    det = _earliest_positive(conn, candidate_id)
    detection_id = det["detection_id"] if det else None
    first_detected = det["classified_at"] if det else None
    # Compare expenditure_date (YYYY-MM-DD) against the detection DAY, so a same
    # day IE counts as on/after detection.
    cutoff = first_detected[:10] if first_detected else None

    agg = conn.execute(
        """SELECT support_oppose_indicator AS ind, committee_id, committee_name,
                  SUM(expenditure_amount) AS amt, COUNT(*) AS n,
                  MIN(expenditure_date) AS first_d, MAX(expenditure_date) AS last_d
           FROM ie_filings WHERE candidate_id=? AND cycle=?
           GROUP BY committee_id, support_oppose_indicator
           ORDER BY amt DESC""", (candidate_id, cycle)).fetchall()
    committees = [CommitteeSpend(
        committee_id=r["committee_id"], committee_name=r["committee_name"],
        indicator=r["ind"] or "?", amount=float(r["amt"] or 0), count=r["n"],
        first_date=r["first_d"], last_date=r["last_d"]) for r in agg]

    supporting_total = sum(c.amount for c in committees if c.indicator == "S")
    opposing_total = sum(c.amount for c in committees if c.indicator == "O")
    total_count = sum(c.count for c in committees)

    after = 0.0
    if cutoff:
        row = conn.execute(
            """SELECT SUM(expenditure_amount) FROM ie_filings
               WHERE candidate_id=? AND cycle=? AND support_oppose_indicator='S'
                 AND expenditure_date >= ?""", (candidate_id, cycle, cutoff)).fetchone()
        after = float(row[0] or 0.0)

    corr = Corroboration(
        candidate_id=candidate_id, detection_id=detection_id,
        guidance_first_detected=first_detected, supporting_total=supporting_total,
        opposing_total=opposing_total, supporting_after_detection=after,
        ie_filing_count=total_count, committees=committees,
    )
    _persist(conn, corr)
    return corr


def _persist(conn: sqlite3.Connection, c: Corroboration) -> None:
    spender_json = json.dumps([{
        "committee_id": s.committee_id, "committee_name": s.committee_name,
        "indicator": s.indicator, "amount": s.amount, "count": s.count,
        "first_date": s.first_date, "last_date": s.last_date} for s in c.committees])
    # One corroboration row per candidate; replace on recompute.
    conn.execute("DELETE FROM corroboration WHERE candidate_id=?", (c.candidate_id,))
    conn.execute(
        """INSERT INTO corroboration (candidate_id, detection_id,
               guidance_first_detected, supporting_ie_total_after, supporting_total,
               opposing_total, ie_filing_count, spender_list, computed_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (c.candidate_id, c.detection_id, c.guidance_first_detected,
         c.supporting_after_detection, c.supporting_total, c.opposing_total,
         c.ie_filing_count, spender_json, now_iso()))
    conn.commit()


def run(conn: sqlite3.Connection, fec, *, candidate_id: str, cycle: int,
        use_cache: bool = True) -> Corroboration:
    """Pull Schedule E then compute corroboration for one candidate."""
    pull_and_store_ie(conn, fec, candidate_id=candidate_id, cycle=cycle, use_cache=use_cache)
    return compute(conn, candidate_id, cycle=cycle)


def candidates_with_positive(conn: sqlite3.Connection) -> list[str]:
    """Candidates with a positive/ambiguous detection not rejected by review."""
    return [r[0] for r in conn.execute(
        f"""SELECT DISTINCT d.candidate_id FROM detections d
            {_NOT_REJECTED_JOIN}
            WHERE d.classification IN ('red_box_guidance','ambiguous')
              AND COALESCE(r.action,'') != 'reject'""").fetchall()]
