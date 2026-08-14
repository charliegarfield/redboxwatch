"""Publisher / review console — static site from the database (spec §3.7a, §3.8).

Generates a self-contained static site (open ``site/index.html`` in a browser —
no server required) showing the candidate universe and, per candidate, the
detection status with archived evidence.

Legal-safety discipline (spec §3.7a) is enforced here:
- Positives are shown with the exact label "Posted public messaging guidance
  consistent with red-boxing", linked to archived evidence and quoted spans —
  never asserting illegality.
- A positive that has NOT been approved in review is banner-marked
  "PENDING HUMAN REVIEW — not published". Only approved positives read as
  published findings (spec §3.7: the human gate sits on the positive path).
- Negatives read "No public messaging guidance detected as of [date]" — never
  "does not red-box".
- Methodology and corrections pages are generated (spec §3.7a).

This is the local review build (the default). The strict public build
(`--approved-only`) emits approved positives + dated negatives only.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .pipeline import usable_scan_sql
# Shared presentation pieces live in render.py (labels, escaping, fonts, the
# STATUSES table, and the static assets under redbox/assets/). They are
# imported AND re-exported here: reviewweb.py and others import them from
# redbox.publisher, and that surface must keep working.
from .render import (AMBIGUOUS_CONFIRMED_LABEL, AMBIGUOUS_LABEL, CSS,
                     CSS_VERSION, FAVICON_ICO, FAVICON_PNG48, FAVICON_PNG180,
                     FAVICON_SVG, INDEX_JS, POSITIVE_LABEL, STATUSES,
                     StatusSpec, _FONTS, _h, _neg_label)
from .util import STATE_NAMES, now_iso, sha256_text


@dataclass
class CandidateView:
    row: dict
    status: str            # positive_published | positive_pending | ambiguous_pending
                           # | rejected | negative | not_scanned
    label: str
    detection: dict | None
    evidence: list[dict]
    archive: dict | None
    scan_count: int
    last_scanned: str | None
    review: dict | None
    corroboration: dict | None = None
    changes: list[dict] = field(default_factory=list)
    # One entry per distinct page BODY carrying red-box/ambiguous guidance that
    # survived (or awaits) review: {detection, evidence, archive, review,
    # approved, pending, alias_urls, gone_since}. Template-alias URLs serving
    # the same body are collapsed into one exhibit (primary URL + alias_urls).
    # exhibits[0] is always the same detection as ``detection`` when the
    # candidate's status is positive. ``gone_since`` is the date the guidance
    # stopped appearing on every URL that served it (None while live).
    exhibits: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Detection ranking, shared by the per-candidate and per-URL picks (and by the
# Python-side sort that keeps them consistent). Review state first — an
# approved positive IS the published record and outranks any unreviewed
# re-detection; a rejected flag sinks below everything reviewable — then the
# original severity/confidence/id ordering.
_POSITIVE_CLASSES = ("red_box_guidance", "ambiguous")

_RANK_ORDER_SQL = """
    CASE WHEN d.classification IN ('red_box_guidance','ambiguous')
              AND r.action = 'approve' THEN 2
         WHEN d.classification IN ('red_box_guidance','ambiguous')
              AND (r.action IS NULL OR r.action = 'needs_more') THEN 1
         ELSE 0 END DESC,
    CASE d.classification
         WHEN 'red_box_guidance' THEN 2 WHEN 'ambiguous' THEN 1 ELSE 0 END DESC,
    d.confidence DESC, d.detection_id DESC"""

_LATEST_REVIEW_JOIN = """
    LEFT JOIN (SELECT detection_id, action FROM (
                 SELECT detection_id, action, ROW_NUMBER() OVER (
                     PARTITION BY detection_id
                     ORDER BY reviewed_at DESC, review_id DESC) rn
                 FROM reviews) WHERE rn = 1) r ON r.detection_id = d.detection_id"""

_SEVERITY = {"red_box_guidance": 2, "ambiguous": 1}


def _rank_key(d: dict):
    """Python mirror of _RANK_ORDER_SQL (ascending sort -> best first)."""
    positive = d["classification"] in _POSITIVE_CLASSES
    action = d.get("review_action")
    tier = (2 if positive and action == "approve"
            else 1 if positive and action in (None, "needs_more") else 0)
    return (-tier, -_SEVERITY.get(d["classification"], 0),
            -(d.get("confidence") or 0), -(d.get("detection_id") or 0))


def _gather(conn: sqlite3.Connection) -> list[CandidateView]:
    """Assemble one view per candidate using a fixed set of aggregate queries.

    This used to issue ~6 queries *per candidate* (N+1) — fine for one state, but
    ~30k queries for a nationwide universe. Instead we run a constant number of
    set-based queries (counts grouped by candidate; the top detection / latest
    review / archive / corroboration via window functions) and stitch the results
    together in memory keyed by candidate_id, so query count no longer grows with
    the universe size.
    """
    # Scan counts + last-scanned, grouped (one query).
    scan_by_cid: dict[str, tuple[int, str | None]] = {
        r["candidate_id"]: (r["n"], r["last"])
        for r in conn.execute(
            "SELECT candidate_id, COUNT(*) n, MAX(fetched_at) last "
            "FROM scans GROUP BY candidate_id")
    }

    # Representative detection per candidate (one window-function query).
    # Review state ranks ABOVE severity and confidence: an approved finding is
    # the candidate's published state and must not be displaced by a newer,
    # higher-confidence but unreviewed re-detection of the same box (which
    # would flip the status to pending and silently unpublish the finding on
    # the next --approved-only deploy), nor by a rejected false positive.
    # Within a review tier the old ordering holds: red_box > ambiguous > none,
    # then confidence, then detection_id for a deterministic pick.
    top_det_by_cid: dict[str, dict] = {}
    for r in conn.execute(
        f"""SELECT * FROM (
              SELECT d.*, s.url AS page_url, r.action AS review_action,
                     ROW_NUMBER() OVER (
                       PARTITION BY d.candidate_id
                       ORDER BY {_RANK_ORDER_SQL}) AS rn
              FROM detections d JOIN scans s USING(scan_id)
              {_LATEST_REVIEW_JOIN}
           ) WHERE rn = 1"""):
        d = dict(r)
        d.pop("rn", None)
        top_det_by_cid[d["candidate_id"]] = d

    # Every page URL still carrying reviewable guidance (one window query):
    # the top detection per (candidate, url) under the same ranking. A page's
    # approved detection outranks a newer pending re-detection of the same box,
    # so a re-scan never duplicates an exhibit; a URL whose flag was rejected
    # (and never approved) is excluded. This is what lets a candidate with red
    # boxes on TWO pages show both, each with its own archived evidence.
    exhibit_dets_by_cid: dict[str, list[dict]] = {}
    for r in conn.execute(
        f"""SELECT * FROM (
              SELECT d.*, s.url AS page_url, s.text_hash AS page_text_hash,
                     r.action AS review_action,
                     MIN(CASE WHEN d.classification IN ('red_box_guidance','ambiguous')
                              THEN d.classified_at END) OVER (
                       PARTITION BY d.candidate_id, s.url) AS first_detected_at,
                     ROW_NUMBER() OVER (
                       PARTITION BY d.candidate_id, s.url
                       ORDER BY {_RANK_ORDER_SQL}) AS rn
              FROM detections d JOIN scans s USING(scan_id)
              {_LATEST_REVIEW_JOIN}
           ) WHERE rn = 1
             AND classification IN ('red_box_guidance','ambiguous')
             AND (review_action IS NULL OR review_action != 'reject')"""):
        d = dict(r)
        d.pop("rn", None)
        exhibit_dets_by_cid.setdefault(d["candidate_id"], []).append(d)
    # Collapse template aliases: the pipeline dedupes identical bodies within
    # one scan run, but catch-all routes re-detected across runs (/media,
    # /media-kit, /press all serving the same page) accumulate one detection
    # per URL. One exhibit per distinct body — best-ranked URL is primary, the
    # rest are listed as aliases — matches the review console's grouping and
    # keeps a ten-alias site from rendering as ten red boxes.
    for cid_key, dets in exhibit_dets_by_cid.items():
        dets.sort(key=_rank_key)
        by_hash: dict[str, dict] = {}
        for d in dets:
            h = d["page_text_hash"]
            if h in by_hash:
                prim = by_hash[h]
                prim.setdefault("alias_urls", []).append(d["page_url"])
                fd, pfd = d.get("first_detected_at"), prim.get("first_detected_at")
                if fd and (not pfd or fd < pfd):
                    prim["first_detected_at"] = fd
            else:
                by_hash[h] = d
        exhibit_dets_by_cid[cid_key] = list(by_hash.values())

    # Every distinct positive BODY a candidate has ever served (one window
    # query): the best-ranked detection per (candidate, text_hash). Every
    # revision of a box got its own detection AND archive when it was
    # classified, so the full version history already exists on disk — this
    # surfaces it. An exhibit's "earlier versions" resolve from these by URL
    # group in the assembly loop below.
    body_dets: dict[str, list[dict]] = {}
    for r in conn.execute(
        f"""SELECT * FROM (
              SELECT d.*, s.url AS page_url, s.text_hash AS page_text_hash,
                     r.action AS review_action,
                     ROW_NUMBER() OVER (
                       PARTITION BY d.candidate_id, s.text_hash
                       ORDER BY {_RANK_ORDER_SQL}) AS rn
              FROM detections d JOIN scans s USING(scan_id)
              {_LATEST_REVIEW_JOIN}
              WHERE d.classification IN ('red_box_guidance','ambiguous')
           ) WHERE rn = 1"""):
        d = dict(r)
        d.pop("rn", None)
        body_dets.setdefault(d["candidate_id"], []).append(d)
    # When each positive body was FIRST seen (one grouped query): orders the
    # versions and dates each one's lifespan.
    body_first_seen: dict[tuple[str, str], str] = {
        (r["candidate_id"], r["text_hash"]): r["fs"]
        for r in conn.execute(
            """SELECT s.candidate_id, s.text_hash, MIN(s.fetched_at) AS fs
               FROM scans s
               WHERE (s.candidate_id, s.text_hash) IN (
                   SELECT d.candidate_id, sd.text_hash
                   FROM detections d JOIN scans sd USING(scan_id)
                   WHERE d.classification IN ('red_box_guidance','ambiguous'))
               GROUP BY s.candidate_id, s.text_hash""")}

    # Current live state of each exhibit URL (one query): the latest USABLE
    # scan of the URL — error/challenge fetches say nothing about the page, so
    # a 403 bot-block must not read as a removal — with its classification
    # resolved through text_hash (unchanged re-scans carry no detection row of
    # their own — same convention as the pipeline's change diffing). The
    # per-body verdict uses the SAME review-aware ranking as everything else:
    # legacy scans classified one body under several URLs and the classifier
    # sometimes contradicted itself, and an unreviewed no-guidance verdict
    # must not overrule the approved detection of the identical body (that
    # read as a phantom "removal"). A latest usable scan whose body no longer
    # ranks positive means the guidance has come down; the finding stays on
    # the ledger but the page says so, dated.
    gone_by_cid_url: dict[tuple[str, str], str] = {}
    last_usable_scan: dict[tuple[str, str], int] = {}
    for r in conn.execute(
        f"""SELECT l.candidate_id, l.url, l.fetched_at, l.scan_id,
                   top.classification AS current_class
            FROM (SELECT * FROM (
                    SELECT s.*, ROW_NUMBER() OVER (
                        PARTITION BY s.candidate_id, s.url
                        ORDER BY s.scan_id DESC) rn
                    FROM scans s
                    WHERE (s.candidate_id, s.url) IN (
                        SELECT DISTINCT sd.candidate_id, sd.url
                        FROM detections dd JOIN scans sd ON sd.scan_id = dd.scan_id
                        WHERE dd.classification IN ('red_box_guidance','ambiguous'))
                      AND {usable_scan_sql('s')}
                  ) WHERE rn = 1) l
            LEFT JOIN (SELECT * FROM (
                    SELECT d.candidate_id AS tcand, s.text_hash AS thash,
                           d.classification,
                           ROW_NUMBER() OVER (
                             PARTITION BY d.candidate_id, s.text_hash
                             ORDER BY {_RANK_ORDER_SQL}) trn
                    FROM detections d JOIN scans s USING(scan_id)
                    {_LATEST_REVIEW_JOIN}
                  ) WHERE trn = 1) top
              ON top.tcand = l.candidate_id AND top.thash = l.text_hash"""):
        last_usable_scan[(r["candidate_id"], r["url"])] = r["scan_id"]
        if r["current_class"] not in ("red_box_guidance", "ambiguous"):
            gone_by_cid_url[(r["candidate_id"], r["url"])] = (r["fetched_at"] or "")[:10]
    # A change event recorded after the last usable scan is the newer word on
    # the URL's state: a confirmed-disappearance take_down (two consecutive
    # 404s) has no usable scan of its own, and a later put_up supersedes an
    # older gone verdict.
    for r in conn.execute(
        """SELECT candidate_id, url, event_type, new_scan_id, detected_at
           FROM (SELECT *, ROW_NUMBER() OVER (
                     PARTITION BY candidate_id, url ORDER BY change_id DESC) rn
                 FROM change_events) WHERE rn = 1"""):
        key = (r["candidate_id"], r["url"])
        # Events with no new_scan_id (legacy/synthetic rows) can't be ordered
        # against scans; treat them as older than any usable scan.
        if (r["new_scan_id"] or 0) <= last_usable_scan.get(key, 0):
            continue
        if r["event_type"] == "take_down":
            gone_by_cid_url[key] = (r["detected_at"] or "")[:10]
        else:
            gone_by_cid_url.pop(key, None)

    # Latest review / archive per detection (one query each; both small tables).
    review_by_det: dict[int, dict] = {}
    for r in conn.execute(
        """SELECT * FROM (
              SELECT *, ROW_NUMBER() OVER (
                  PARTITION BY detection_id ORDER BY reviewed_at DESC, review_id DESC) rn
              FROM reviews) WHERE rn = 1"""):
        review_by_det[r["detection_id"]] = dict(r)
    archive_by_det: dict[int, dict] = {}
    for r in conn.execute(
        """SELECT * FROM (
              SELECT *, ROW_NUMBER() OVER (
                  PARTITION BY detection_id ORDER BY archived_at DESC, archive_id DESC) rn
              FROM archives WHERE detection_id IS NOT NULL) WHERE rn = 1"""):
        archive_by_det[r["detection_id"]] = dict(r)

    # Latest corroboration per candidate (one window-function query).
    corr_by_cid: dict[str, dict] = {}
    for r in conn.execute(
        """SELECT * FROM (
              SELECT *, ROW_NUMBER() OVER (
                  PARTITION BY candidate_id ORDER BY computed_at DESC, corroboration_id DESC) rn
              FROM corroboration) WHERE rn = 1"""):
        corr_by_cid[r["candidate_id"]] = dict(r)

    # For 'modified' events, judge whether the quoted guidance itself changed
    # or only the page around it (one query over the modified events; the
    # comparison is _guidance_unchanged). "Guidance changed" fires on any
    # text-hash change, so without this a news-item edit on a red-box page
    # reads the same as a rewritten box.
    mod_same: dict[int, bool | None] = {}
    for r in conn.execute(
        """SELECT ce.change_id, ps.raw_text AS prev_text, ns.raw_text AS new_text,
                  (SELECT evidence FROM detections dp WHERE dp.scan_id = ce.prev_scan_id
                   ORDER BY dp.detection_id DESC LIMIT 1) AS prev_ev,
                  (SELECT evidence FROM detections dn WHERE dn.scan_id = ce.new_scan_id
                   ORDER BY dn.detection_id DESC LIMIT 1) AS new_ev
           FROM change_events ce
           LEFT JOIN scans ps ON ps.scan_id = ce.prev_scan_id
           LEFT JOIN scans ns ON ns.scan_id = ce.new_scan_id
           WHERE ce.event_type = 'modified'"""):
        mod_same[r["change_id"]] = _guidance_unchanged(
            r["prev_ev"], r["new_ev"], r["prev_text"], r["new_text"])

    # All change events, bucketed by candidate in memory (one ordered query).
    changes_by_cid: dict[str, list[dict]] = {}
    for r in conn.execute(
        "SELECT * FROM change_events ORDER BY candidate_id, detected_at DESC"):
        ch = dict(r)
        ch["guidance_same"] = mod_same.get(ch["change_id"])
        changes_by_cid.setdefault(ch["candidate_id"], []).append(ch)

    views: list[CandidateView] = []
    # inactive = withdrawn/superseded candidacy (FEC flag or human wrong-race
    # call): kept in the DB for history, not published — EXCEPT records with an
    # approved finding (latest review wins). A red box found and approved while
    # the run was live stays on the ledger even after the candidacy ends;
    # excluding it here would silently unpublish it.
    for c in conn.execute("""
        SELECT * FROM candidates
        WHERE COALESCE(inactive,0)=0
           OR candidate_id IN (
              SELECT d.candidate_id FROM detections d
              JOIN (SELECT detection_id, action, ROW_NUMBER() OVER (
                        PARTITION BY detection_id
                        ORDER BY reviewed_at DESC, review_id DESC) rn
                    FROM reviews) r
                ON r.detection_id = d.detection_id AND r.rn = 1
              WHERE r.action = 'approve')
        ORDER BY state, district, name"""):
        cid = c["candidate_id"]
        scan_count, last = scan_by_cid.get(cid, (0, None))
        det = top_det_by_cid.get(cid)
        review = archive = None
        evidence: list[dict] = []
        if det:
            review = review_by_det.get(det["detection_id"])
            archive = archive_by_det.get(det["detection_id"])
            evidence = _parse_evidence(det)
        exhibits = []
        current_hashes = {x["page_text_hash"] for x in exhibit_dets_by_cid.get(cid, [])}
        for d in exhibit_dets_by_cid.get(cid, []):
            # Gone only when EVERY URL serving this body has stopped carrying
            # it (a page that moved to an alias is still up); dated by the most
            # recent check.
            urls = [d.get("page_url")] + d.get("alias_urls", [])
            gone = [gone_by_cid_url.get((cid, u)) for u in urls]
            gone_since = max(gone) if all(gone) else None
            versions = _exhibit_versions(
                d, urls, current_hashes, body_dets.get(cid, []),
                body_first_seen, archive_by_det, cid)
            exhibits.append({
                "detection": d,
                "evidence": _parse_evidence(d),
                "archive": archive_by_det.get(d["detection_id"]),
                "review": review_by_det.get(d["detection_id"]),
                "approved": d.get("review_action") == "approve",
                "pending": d.get("review_action") != "approve",
                "alias_urls": d.get("alias_urls", []),
                "gone_since": gone_since,
                "timeline": _exhibit_timeline(
                    d, changes_by_cid.get(cid, []), urls, gone_since,
                    revision_days=[v["superseded"] for v in versions
                                   if v.get("superseded")]),
                "versions": versions,
            })
        status, label = _status(det, review, last, scan_count, candidate=dict(c))
        views.append(CandidateView(
            row=dict(c), status=status, label=label,
            detection=det, evidence=evidence, archive=archive,
            scan_count=scan_count, last_scanned=last, review=review,
            corroboration=corr_by_cid.get(cid),
            changes=changes_by_cid.get(cid, []),
            exhibits=exhibits,
        ))
    return views


def _parse_evidence(det: dict) -> list[dict]:
    try:
        return json.loads(det.get("evidence") or "[]")
    except json.JSONDecodeError:
        return []


def _exhibit_versions(det: dict, urls: list[str], current_hashes: set[str],
                      bodies: list[dict], first_seen: dict[tuple[str, str], str],
                      archive_by_det: dict[int, dict], cid: str) -> list[dict]:
    """Earlier versions of one exhibit's guidance, oldest first.

    A version is a distinct positive body previously served on this exhibit's
    URL group: not the current body, not any other exhibit's current body (a
    page that switched to serving a different exhibit's box must not list it
    twice), not rejected by review, and first seen BEFORE the current body.
    Each carries its own detection, archived evidence, lifespan (first seen ->
    superseded by the next version), and a quote-level diff against what
    replaced it. "Guidance revised" stops being a dead-end label: the thing it
    was revised FROM is one click away.
    """
    cur_first = (first_seen.get((cid, det["page_text_hash"]))
                 or det.get("first_detected_at") or "")
    olds = [b for b in bodies
            if b["page_url"] in urls
            and b["page_text_hash"] not in current_hashes
            and b.get("review_action") != "reject"
            and (first_seen.get((cid, b["page_text_hash"]), "") or "~") < cur_first]
    olds.sort(key=lambda b: first_seen.get((cid, b["page_text_hash"]), ""))
    versions: list[dict] = []
    for i, b in enumerate(olds):
        nxt = olds[i + 1] if i + 1 < len(olds) else det
        versions.append({
            "detection": b,
            "evidence": _parse_evidence(b),
            "archive": archive_by_det.get(b["detection_id"]),
            "approved": b.get("review_action") == "approve",
            "first_seen": (first_seen.get((cid, b["page_text_hash"]), "") or "")[:10],
            "superseded": (first_seen.get((cid, nxt["page_text_hash"]), "") or "")[:10],
            # NOTE: quote diffs are computed at RENDER time against the next
            # SURVIVING version — computing them here against the raw
            # successor leaked an unreviewed revision's quotes through the
            # diff line after the public build filtered that revision out.
        })
    return versions


def _quote_diff(old_det: dict, new_det: dict) -> tuple[list[str], list[str]]:
    """Raw quoted spans the NEXT version added / dropped relative to this one.

    Comparison is over normalized spans (same convention as
    _guidance_unchanged) so whitespace/case/ellipsis drift doesn't read as a
    change; display keeps the raw quotes.
    """
    def raw_by_norm(det: dict) -> dict[str, str]:
        out: dict[str, str] = {}
        for q in _parse_evidence(det):
            if q.get("quote"):
                out[_norm_span(q["quote"]).rstrip(".…")] = q["quote"]
        return out

    o, n = raw_by_norm(old_det), raw_by_norm(new_det)
    added = [v for k, v in n.items() if k not in o]
    dropped = [v for k, v in o.items() if k not in n]
    return added, dropped


def _norm_span(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _quote_set(evidence_json: str | None) -> set[str]:
    """Normalized quoted spans from a detection's evidence JSON. Trailing
    ellipses (classifier truncation) are stripped so containment checks work."""
    try:
        ev = json.loads(evidence_json or "[]")
    except json.JSONDecodeError:
        return set()
    return {_norm_span(q.get("quote")).rstrip(".…")
            for q in ev if q.get("quote")}


def _guidance_unchanged(prev_ev, new_ev, prev_text, new_text) -> bool | None:
    """Did a 'modified' page keep the same quoted guidance?

    True  -> the quoted spans are the same on both sides (the page changed
             around them: a news item, a timestamp).
    False -> the guidance spans themselves changed.
    None  -> not enough recorded evidence to say (label stays generic).

    Quote sets can differ merely because the classifier excerpted differently
    on the re-run, so unequal sets fall back to cross-containment: if every
    span quoted on either side appears verbatim in BOTH page texts, the
    guidance is the same.
    """
    pq, nq = _quote_set(prev_ev), _quote_set(new_ev)
    if pq and nq:
        if pq == nq:
            return True
        pt, nt = _norm_span(prev_text), _norm_span(new_text)
        return all(q in nt for q in pq) and all(q in pt for q in nq)
    if nq and prev_text:
        return all(q in _norm_span(prev_text) for q in nq)
    return None


# Timeline entry kinds, in same-day display order: appearance first, then
# content changes, then removal.
_TL_RANK = {"detected": 0, "put_up": 0, "updated": 1, "revised": 2,
            "changed": 2, "take_down": 3, "gone": 3}


def _exhibit_timeline(det: dict, changes: list[dict], urls: list[str],
                      gone_since: str | None,
                      revision_days: list[str] = ()) -> list[tuple[str, str]]:
    """Chronological (day, kind) entries for one exhibit's URL group: first
    detection, then posted / page-updated / guidance-revised / removed events.
    Alias URLs report the same underlying change, so same-day same-kind events
    collapse to one entry. A removal the event log never captured (or that is
    only visible from scan state) still appears, dated from the last check —
    and so does a revision proven by the version history (``revision_days``:
    each earlier version's superseded date) that the event log missed."""
    entries: set[tuple[str, str]] = set()
    for ch in changes:
        if ch.get("url") not in urls:
            continue
        day = (ch.get("detected_at") or "")[:10]
        entries.add((day, _change_key(ch)))
    modified_days = {d for d, k in entries if k in ("updated", "revised", "changed")}
    for day in revision_days:
        if day and day not in modified_days:
            entries.add((day, "revised"))
    first = (det.get("first_detected_at") or det.get("classified_at") or "")[:10]
    if first and (first, "put_up") not in entries:
        entries.add((first, "detected"))
    if gone_since and not any(k == "take_down" for _, k in entries):
        entries.add((gone_since, "gone"))
    return sorted(entries, key=lambda e: (e[0], _TL_RANK.get(e[1], 9)))


def _status(det, review, last, scan_count, candidate=None):
    if not scan_count:
        # Distinguish "no campaign site found" / "blocked by robots" / "not yet
        # scanned" so each gap is visible rather than silent (spec §3.1 honesty).
        # Labels come from the STATUSES table (they double as the candidate
        # page's coverage-gap section heading).
        if candidate is not None and not candidate.get("website_url"):
            key = "no_url"
        elif candidate is not None and candidate.get("scan_status") == "robots_blocked":
            key = "blocked_by_robots"
        elif candidate is not None and candidate.get("scan_status") == "fetch_failed":
            key = "fetch_failed"
        else:
            key = "not_scanned"
        return key, STATUSES[key].row_label
    cls = det["classification"] if det else "no_guidance_detected"
    action = review["action"] if review else None
    if cls == "red_box_guidance":
        if action == "approve":
            return "positive_published", POSITIVE_LABEL
        if action == "reject":
            return "rejected", "Reviewed — not a finding"
        return "positive_pending", POSITIVE_LABEL
    if cls == "ambiguous":
        if action == "approve":
            # A human read the evidence and called it a finding — that judgment
            # publishes, with the classifier's initial hesitation disclosed.
            return "positive_published", AMBIGUOUS_CONFIRMED_LABEL
        if action == "reject":
            return "rejected", "Reviewed — not a finding"
        return "ambiguous_pending", AMBIGUOUS_LABEL
    return "negative", _neg_label(last or now_iso())


# ---------------------------------------------------------------------------
# Default rows per index page. The full universe can be thousands of candidates;
# one giant HTML table is slow to render and impossible to scan. Pages are
# ordered actionable-first (findings/pending lead), so page 1 always holds the
# review-relevant rows regardless of universe size. Override via publish.page_size.
DEFAULT_PAGE_SIZE = 500

# Index ordering: aligned-IE-first. Findings WITH aligned independent
# expenditures lead (richest first — guidance that visibly moved money is the
# story), then findings without IE, then everything else in status-rank order
# (negatives before coverage gaps). Within a band, status rank keeps published
# findings ahead of pending ones, then a stable state/district/name sort.
_STATUS_RANK = {k: s.rank for k, s in STATUSES.items()}

_FINDING_STATUSES = {"positive_published", "positive_pending", "ambiguous_pending"}


def _index_sort_key(v: "CandidateView"):
    c = v.row
    is_finding = v.status in _FINDING_STATUSES
    ie = (float(v.corroboration.get("supporting_total") or 0)
          if (is_finding and v.corroboration) else 0.0)
    band = 0 if ie > 0 else (1 if is_finding else 2)
    return (band, -ie, _STATUS_RANK.get(v.status, 9), c.get("state") or "",
            c.get("district") or "", c.get("name") or "")


def build_site(conn: sqlite3.Connection, out_dir: Path, *, approved_only: bool = False,
               page_size: int = DEFAULT_PAGE_SIZE, site_url: str | None = None) -> Path:
    # site_url is the canonical public origin (e.g. https://redboxwatch.org).
    # When set, pages carry canonical/OpenGraph URLs and the build emits
    # sitemap.xml + robots.txt. Leave unset for local/review builds so pending
    # detections are never described by public URLs.
    if site_url and not approved_only:
        # A site_url build IS a public build: its sitemap enumerates every
        # rendered candidate page at public URLs. Combined with the review
        # (unfiltered) view that would describe pending-detection candidates —
        # unpublished allegations — at public URLs. Only cmd_publish happened
        # to prevent this pairing; enforce it here so no other caller can
        # produce a crawlable review build.
        raise ValueError("site_url requires approved_only")
    global _SITE_URL
    _SITE_URL = (site_url or "").rstrip("/") or None
    out_dir = Path(out_dir)
    (out_dir / "evidence").mkdir(parents=True, exist_ok=True)
    views = _gather(conn)
    # The full universe: counts, the tracked-nationwide total, and the
    # coverage-gap disclosure are computed over this even in public builds —
    # filtering first silently shrank the published universe number and
    # dropped the gap paragraph entirely (an approved-only build counted zero
    # no_url/blocked/failed candidates, contradicting the methodology page).
    all_views = views
    if approved_only:
        # Public build keeps findings, dated negatives, and the no-allegation
        # coverage-gap statuses (their pages ARE the gap disclosure). Pending
        # and rejected detections are unpublished allegations — never render
        # them or their candidates' pages publicly. The allowlist derives from
        # the STATUSES table's is-public flag.
        public = {k for k, s in STATUSES.items() if s.public}
        views = [v for v in views if v.status in public]
        # A pending exhibit is an unpublished allegation even when the
        # candidate has a separate approved finding — never render it. Same
        # per-detection discipline for version history: only earlier versions
        # a human approved appear publicly.
        for v in views:
            v.exhibits = [e for e in v.exhibits if e["approved"]]
            for e in v.exhibits:
                e["versions"] = [ver for ver in e.get("versions", [])
                                 if ver["approved"]]

    # Copy evidence screenshots + archived PDFs into the site, rewrite to
    # relative paths.
    kept_evidence: set[str] = set()
    for v in views:
        for archive in {id(a): a for a in (
                [v.archive] + [e["archive"] for e in v.exhibits]
                + [ver["archive"] for e in v.exhibits
                   for ver in e.get("versions", [])])
                        if a}.values():
            for key, rel_key in (("screenshot_path", "screenshot_rel"),
                                 ("pdf_path", "pdf_rel")):
                if archive.get(key):
                    src = Path(archive[key])
                    if src.exists():
                        dst = out_dir / "evidence" / src.name
                        # Skip the copy when the destination is already this
                        # exact file: copy2 preserves size+mtime, so a match
                        # means an earlier build copied it and the source
                        # hasn't changed — archives are content-addressed and
                        # immutable, and re-copying hundreds of screenshots
                        # per build was pure I/O waste.
                        s = src.stat()
                        if not (dst.exists()
                                and (d := dst.stat()).st_size == s.st_size
                                and d.st_mtime_ns == s.st_mtime_ns):
                            shutil.copy2(src, dst)
                        archive[rel_key] = f"evidence/{src.name}"
                        kept_evidence.add(src.name)
    # Sweep evidence this build no longer references. Without this the
    # directory is append-only across builds, and the screenshot of a since-
    # rejected (or pending, in a public build) detection stays deployed and
    # publicly fetchable long after the page that showed it is gone.
    for p in (out_dir / "evidence").iterdir():
        if p.is_file() and p.name not in kept_evidence:
            p.unlink()

    # Per-candidate pages (natural order is irrelevant; each is standalone).
    for v in views:
        (out_dir / f"{v.row['candidate_id']}.html").write_text(
            _render_candidate(v, public=approved_only))

    # Index, paginated. Counts/coverage are global (over the FULL universe,
    # not the rendered subset); only the table rows are sliced per page.
    counts: dict[str, int] = {}
    for v in all_views:
        counts[v.status] = counts.get(v.status, 0) + 1
    index_views = sorted(views, key=_index_sort_key)
    size = max(1, int(page_size))
    pages = [index_views[i:i + size] for i in range(0, len(index_views), size)] or [[]]
    # Full row set as one fragment; the index JS fetches it on first filter
    # interaction so name/status/state filters search every page, not just the
    # slice the visitor is on. Built BEFORE the pages so each page can embed a
    # content hash and fetch index-data.json?v=<hash>: Pages serves assets
    # with max-age=14400, and a bare fetch left the client-side filter on
    # 4-hour-stale data after a deploy — stale enough to show a since-rejected
    # candidate with a FINDING pill linking to a now-404 page.
    index_data_json = json.dumps(
        {"html": "".join(_index_row(v) for v in index_views)})
    data_version = sha256_text(index_data_json)[:8]
    for pno, page in enumerate(pages, start=1):
        fname = "index.html" if pno == 1 else f"index-{pno}.html"
        (out_dir / fname).write_text(_render_index(
            page, approved_only, counts=counts, all_views=all_views,
            page_no=pno, n_pages=len(pages), data_version=data_version,
            statuses_present={v.status for v in index_views}))
    (out_dir / "index-data.json").write_text(index_data_json)

    (out_dir / "methodology.html").write_text(_render_methodology())
    (out_dir / "corrections.html").write_text(_render_corrections())
    (out_dir / "about.html").write_text(_render_about())
    (out_dir / "404.html").write_text(_render_404())
    (out_dir / "styles.css").write_text(CSS)
    (out_dir / "favicon.svg").write_text(FAVICON_SVG)
    (out_dir / "favicon.png").write_bytes(FAVICON_PNG48)
    (out_dir / "favicon.ico").write_bytes(FAVICON_ICO)
    (out_dir / "apple-touch-icon.png").write_bytes(FAVICON_PNG180)

    if _SITE_URL:
        # Per-page lastmod: candidate pages get the date their content last
        # actually changed (_lastmod); index/static pages regenerate with the
        # universe every build and keep the build date. Stamping every URL
        # with today (the old behavior) made lastmod meaningless to crawlers.
        today = now_iso()[:10]
        entries = ([("index", today)]
                   + [(f"index-{p}", today) for p in range(2, len(pages) + 1)]
                   + [("methodology", today), ("corrections", today),
                      ("about", today)]
                   + [(v.row["candidate_id"], _lastmod(v) or today)
                      for v in index_views])
        urls = "".join(
            f"<url><loc>{_h(_canonical(p))}</loc><lastmod>{d}</lastmod></url>"
            for p, d in entries)
        (out_dir / "sitemap.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{urls}</urlset>\n")
        (out_dir / "robots.txt").write_text(
            f"User-agent: *\nAllow: /\n\nSitemap: {_SITE_URL}/sitemap.xml\n")
        _write_feeds(out_dir, views)
    return out_dir


def _lastmod(v: CandidateView) -> str | None:
    """Sitemap lastmod for one candidate page: the date of the latest event
    that changed the page's rendered content. That is the most recent of
    (a) an exhibit approval — publishes or re-dates a finding, (b) a recorded
    removal (gone_since) — adds the "No longer present" note, and (c) the last
    scan — re-dates the negative's "as of" label and the pages-scanned meta
    row. All are fields CandidateView already carries; None (never scanned)
    falls back to the build date at the call site."""
    dates = [(e["review"] or {}).get("reviewed_at") or "" for e in v.exhibits]
    dates += [e.get("gone_since") or "" for e in v.exhibits]
    dates += [(v.review or {}).get("reviewed_at") or "", v.last_scanned or ""]
    return max(dates, default="")[:10] or None


def _write_feeds(out_dir: Path, views: list[CandidateView]) -> None:
    """feed.xml (RSS 2.0) + feed.json (JSON Feed 1.1) of published findings.

    Public builds only: items need absolute URLs, and review builds must never
    describe pending detections. Newest approval first, capped at the 50 most
    recent — the index stays the full ledger. Neither file is in the sitemap,
    and neither ends in .html, so the stale-page cleanup never touches them.
    """
    def _approved_at(v: CandidateView) -> str:
        # Latest approval across the candidate's exhibits, so an update
        # re-dates (and re-sorts) the item; fall back to the representative
        # detection for degenerate rows.
        dates = [(e["review"] or {}).get("reviewed_at") or ""
                 for e in v.exhibits if e.get("approved")]
        return (max(dates, default="")
                or (v.review or {}).get("reviewed_at")
                or (v.detection or {}).get("classified_at") or "")

    def _parse_iso(ts: str) -> datetime | None:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    findings = sorted((v for v in views if v.status == "positive_published"),
                      key=_approved_at, reverse=True)[:50]
    tagline = ("Newly confirmed red-boxing findings: campaign-site messaging "
               "guidance aimed at super PACs, each linked to archived evidence.")

    items_xml: list[str] = []
    items_json: list[dict] = []
    for v in findings:
        cid = v.row["candidate_id"]
        url = _canonical(cid)
        # One item per candidate, keyed by the SET of approved exhibit BODIES
        # (each exhibit's text_hash, NOT its primary page_url): approving a new
        # or revised distinct box changes the fingerprint and the item
        # re-announces (feed readers/Bluesky see an update), while a
        # re-detection of a known box (fresh detection_id) folds into its
        # exhibit and stays silent — the double-announce the bare-candidate
        # guid was introduced to prevent. The body hash rather than the URL
        # because the primary URL is a rank artifact (final tiebreaker:
        # detection_id DESC): approving a later re-detection of the SAME body
        # on an alias URL flips which URL is primary, and a URL-keyed guid
        # re-announced the unchanged finding on every such flip. Primary/alias
        # churn must be silent; only the body set speaks. (Legacy detections
        # with no text_hash fall back to the URL — still stable per exhibit.)
        # NOTE: switching the sig input from URL to body hash changed every
        # existing guid once; the bsky-poster worker treats a flood of re-keyed
        # ids (known candidate prefix, new '#'-suffix) as a guid-scheme
        # migration and absorbs them without posting.
        approved_ex = [e for e in v.exhibits if e.get("approved")]
        sig = (hashlib.sha1("|".join(sorted(
                   e["detection"].get("page_text_hash")
                   or e["detection"].get("page_url") or ""
                   for e in approved_ex))
               .encode()).hexdigest()[:8] if approved_ex else "0")
        guid = f"{cid}#{sig}"
        n = len(approved_ex)
        # A multi-exhibit item reads as an update only when its approvals span
        # more than one day: candidates routinely DEBUT with several boxes
        # approved in one bulk review session, and that first announcement
        # must not call itself "updated". A later same-day second approval can
        # slip through as a plain title — understating beats overclaiming.
        approval_days = {((e["review"] or {}).get("reviewed_at") or "")[:10]
                         for e in approved_ex}
        name_seat = f"{_display_name(v.row['name'])} ({_seat_compact(v.row)})"
        if n > 1 and len(approval_days) > 1:
            title = f"Updated red-box guidance found for {name_seat}"
        else:
            title = f"{name_seat} — red-box guidance found"
        if n > 1:
            # An update should showcase what's new: the most recently
            # approved exhibit, not the top-ranked (usually oldest) one.
            newest = max(approved_ex, key=lambda e: (
                (e["review"] or {}).get("reviewed_at")
                or e["detection"].get("first_detected_at") or ""))
            page = newest["detection"].get("page_url")
            quote = next((q.get("quote") for q in newest["evidence"]
                          if q.get("quote")), None)
            parts = [v.label]
            if page:
                parts.append(f"Newest guidance page: {page}")
        else:
            parts = [v.label]
            page = (v.detection or {}).get("page_url")
            if page:
                parts.append(f"Guidance page: {page}")
            quote = next((e.get("quote") for e in v.evidence
                          if e.get("quote")), None)
        if quote:
            parts.append(f"Quoted span: “{quote}”")
        desc = " · ".join(parts)
        dt = _parse_iso(_approved_at(v))
        pub = f"<pubDate>{format_datetime(dt)}</pubDate>" if dt else ""
        items_xml.append(
            f"<item><title>{_h(title)}</title><link>{_h(url)}</link>"
            f'<guid isPermaLink="false">{_h(guid)}</guid>{pub}'
            f"<description>{_h(desc)}</description></item>")
        item: dict = {"id": guid, "url": url, "title": title, "content_text": desc}
        if dt:
            item["date_published"] = dt.isoformat()
        items_json.append(item)

    (out_dir / "feed.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom"><channel>'
        f"<title>RedBoxWatch — Findings</title><link>{_h(_SITE_URL + '/')}</link>"
        f'<atom:link href="{_h(_SITE_URL + "/feed.xml")}" rel="self" '
        'type="application/rss+xml"/>'
        f"<description>{_h(tagline)}</description>"
        f"{''.join(items_xml)}</channel></rss>\n")
    (out_dir / "feed.json").write_text(json.dumps({
        "version": "https://jsonfeed.org/version/1.1",
        "title": "RedBoxWatch — Findings",
        "home_page_url": _SITE_URL + "/",
        "feed_url": _SITE_URL + "/feed.json",
        "description": tagline,
        "items": items_json,
    }, ensure_ascii=False, indent=1) + "\n")


def _pager(page_no: int, n_pages: int) -> str:
    """Prev/Next + 'Page X of N' nav, rendered only for a multi-page index."""
    if n_pages <= 1:
        return ""
    def href(p: int) -> str:
        return _href("index") if p == 1 else _href(f"index-{p}")
    prev = (f'<a class="pg-prev" href="{href(page_no - 1)}">← Prev</a>'
            if page_no > 1 else '<span class="pg-prev pg-off">← Prev</span>')
    nxt = (f'<a class="pg-next" href="{href(page_no + 1)}">Next →</a>'
           if page_no < n_pages else '<span class="pg-next pg-off">Next →</span>')
    nums = " ".join(
        (f'<strong>{p}</strong>' if p == page_no else f'<a href="{href(p)}">{p}</a>')
        for p in range(1, n_pages + 1))
    return (f'<nav class="pager">{prev}<span class="pg-nums">{nums}</span>{nxt}'
            f'<span class="pg-of">Page {page_no} of {n_pages}</span></nav>')


# ---------------------------------------------------------------------------
def _pill(status: str) -> tuple[str, str]:
    """(pill text, css class) for a status, from the STATUSES table."""
    spec = STATUSES.get(status)
    return (spec.pill, spec.pill_class) if spec else ("—", "pill-muted")


# ---------------------------------------------------------------------------
# Rendering. Design: "Broadsheet" — editorial investigations-desk aesthetic
# (warm paper, ink, one decisive red; Fraunces / Source Serif 4 / Libre
# Franklin), with the Sunlight state-grid heatmap on the index. The red box
# itself is the brand mark. Chosen from design-concepts/ (2026-07-11).

_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]

# Favicons (the brand's red box) live as real binary files in redbox/assets/
# and are loaded by render.py; see the rationale comment there.


def _pub_date() -> str:
    # Human-facing dates use Eastern time, not UTC: a post-8pm-ET build must
    # not stamp the site (or the "as of" evidence statements) with tomorrow.
    now = datetime.now(ZoneInfo("America/New_York"))
    return f"{_MONTHS[now.month - 1]} {now.day}, {now.year}"


# Canonical public URL (no trailing slash); set per-build by build_site from
# publish.site_url. None for local/review builds.
_SITE_URL: str | None = None


def _canonical(path: str) -> str | None:
    """Absolute extensionless URL for a page ('index' -> the site root).
    Cloudflare Pages serves pretty URLs, so /X.html canonicalizes to /X."""
    if not _SITE_URL:
        return None
    return f"{_SITE_URL}/" if path == "index" else f"{_SITE_URL}/{path}"


def _href(path: str, root: str = "") -> str:
    """Internal link target for a page. Public builds link the extensionless
    form Pages actually serves, so crawlers never traverse (or index) the
    /X.html → /X redirect; site-less builds keep .html so the output still
    browses from file:// and servers without pretty URLs."""
    if not _SITE_URL:
        return f"{root}{path}.html"
    if path == "index":
        return root or "./"
    return f"{root}{path}"


def _seo_head(title: str, path: str, desc: str, og_image: str | None,
              og_type: str, og_title: str | None = None) -> str:
    tags = []
    if desc:
        tags.append(f'<meta name="description" content="{_h(desc)}">')
    url = _canonical(path)
    if url:
        tags.append(f'<link rel="canonical" href="{_h(url)}">')
    if desc:
        tags += [
            '<meta property="og:site_name" content="RedBoxWatch">',
            f'<meta property="og:title" content="{_h(og_title or title)}">',
            f'<meta property="og:description" content="{_h(desc)}">',
            f'<meta property="og:type" content="{_h(og_type)}">',
        ]
        if url:
            tags.append(f'<meta property="og:url" content="{_h(url)}">')
        if og_image and _SITE_URL:
            tags.append(f'<meta property="og:image" content="{_h(_SITE_URL + "/" + og_image)}">')
            tags.append('<meta name="twitter:card" content="summary_large_image">')
        else:
            tags.append('<meta name="twitter:card" content="summary">')
    # WebSite structured data on the homepage tells Google to show
    # "RedBoxWatch" as the site name in results instead of the bare domain.
    # alternateName keeps pre-rebrand press mentions tied to the same entity.
    if path == "index" and _SITE_URL:
        tags.append('<script type="application/ld+json">' + json.dumps({
            "@context": "https://schema.org",
            "@type": "WebSite",
            "name": "RedBoxWatch",
            "alternateName": "Red Box Watch",
            "url": f"{_SITE_URL}/",
        }) + "</script>")
    return "\n".join(tags)


def _layout(title: str, body: str, *, page_class: str = "", active: str = "",
            path: str = "", desc: str = "", og_image: str | None = None,
            og_type: str = "website", og_title: str | None = None,
            root: str = "") -> str:
    # root="/" makes internal links absolute — required for 404.html, which
    # Pages serves at arbitrary depths where relative links would break.
    def nav(page: str, label: str, key: str) -> str:
        cur = ' aria-current="page"' if key == active else ""
        return f'<a href="{_href(page, root)}"{cur}>{label}</a>'
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_h(title)} · RedBoxWatch</title>
{_seo_head(title, path, desc, og_image, og_type, og_title)}
<link rel="icon" href="{root}favicon.svg" type="image/svg+xml">
<link rel="icon" href="{root}favicon.png" type="image/png" sizes="48x48">
<link rel="apple-touch-icon" href="{root}apple-touch-icon.png">
{_FONTS}
{f'<link rel="alternate" type="application/rss+xml" title="RedBoxWatch — Findings" href="{_SITE_URL}/feed.xml">' if _SITE_URL else ''}
<link rel="stylesheet" href="{root}styles.css?v={CSS_VERSION}"></head><body>
<header class="masthead">
  <div class="folio wrap">
    <span>Published {_h(_pub_date())}&ensp;·&ensp;Updated with each build</span>
    <nav>{nav('index', 'Index', 'index')}{nav('methodology', 'Methodology', 'methodology')}{nav('corrections', 'Corrections &amp; Appeals', 'corrections')}{nav('about', 'About', 'about')}</nav>
  </div>
  <div class="nameplate wrap">
    <a class="brand" href="{_href('index', root)}"><span class="redbox"></span>Red<span class="kb">B</span>ox<span class="kw">W</span>atch</a>
    <p class="tagline">A public ledger of red&#8209;boxing &#8212; campaign&#8209;site signals to super PACs</p>
  </div>
  <div class="wrap"><div class="double-rule"></div></div>
</header>
<main class="wrap {page_class}">{body}</main>
<footer class="site-foot"><div class="wrap">
  <div class="double-rule"></div>
  <p class="foot-mark"><span class="redbox"></span></p>
  <p>Detections are gated behind human review before any are treated as findings. Negatives are recorded as dated &#8220;no guidance detected as of&#8221; statements, never as &#8220;does not red-box.&#8221; Red-boxing exploits campaign-finance rules openly; it is <strong>not per se unlawful</strong>. Every published claim links to archived evidence. Generated {_h(now_iso()[:16].replace('T', ' '))} UTC.</p>
  <p class="foot-contact">Press &amp; media: <a class="px-mail" href="#">press&nbsp;[at]&nbsp;redboxwatch&nbsp;[dot]&nbsp;org</a>&ensp;·&ensp;<a href="{_href('corrections', root)}">Corrections &amp; appeals</a>{f'&ensp;·&ensp;<a href="{_SITE_URL}/feed.xml">New-findings RSS</a>' if _SITE_URL else ''}</p>
</div></footer>
<script>(function(){{
  var r=function(s){{return s.split('').reverse().join('')}};
  var e=r('sserp')+String.fromCharCode(64)+r('hctawxobder')+'.'+r('gro');
  var ls=document.querySelectorAll('.px-mail');
  for(var i=0;i<ls.length;i++){{ls[i].href='mailto:'+e;ls[i].textContent=e;}}
}})();</script>
</body></html>"""


# ---------------------------------------------------------------------------
# State-grid heatmap (the classic square-per-state cartogram, pure CSS grid).
_STATE_GRID = {
    "AK": (1, 1), "ME": (1, 11),
    "VT": (2, 10), "NH": (2, 11),
    "WA": (3, 1), "ID": (3, 2), "MT": (3, 3), "ND": (3, 4), "MN": (3, 5),
    "IL": (3, 6), "WI": (3, 7), "MI": (3, 8), "NY": (3, 10), "MA": (3, 11),
    "OR": (4, 1), "NV": (4, 2), "WY": (4, 3), "SD": (4, 4), "IA": (4, 5),
    "IN": (4, 6), "OH": (4, 7), "PA": (4, 8), "NJ": (4, 9), "CT": (4, 10), "RI": (4, 11),
    "CA": (5, 1), "UT": (5, 2), "CO": (5, 3), "NE": (5, 4), "MO": (5, 5),
    "KY": (5, 6), "WV": (5, 7), "VA": (5, 8), "MD": (5, 9), "DE": (5, 10),
    "AZ": (6, 2), "NM": (6, 3), "KS": (6, 4), "AR": (6, 5), "TN": (6, 6),
    "NC": (6, 7), "SC": (6, 8), "DC": (6, 9),
    "OK": (7, 4), "LA": (7, 5), "MS": (7, 6), "AL": (7, 7), "GA": (7, 8),
    "HI": (8, 1), "TX": (8, 4), "FL": (8, 9),
}

_OFFICE_WORD = {"H": "House", "S": "Senate", "P": "President"}
_PARTY_WORD = {"DEM": "Democrat", "REP": "Republican", "DFL": "DFL",
               "IND": "Independent", "LIB": "Libertarian", "GRE": "Green"}


def _heat_class(n: int) -> str:
    if n >= 7:
        return " h4"
    if n >= 4:
        return " h3"
    if n >= 2:
        return " h2"
    if n == 1:
        return " h1"
    return ""


def _statemap_pop(st: str, finds: list["CandidateView"], row: int, col: int) -> str:
    """Hover popup for one heatmap cell: the state's finding candidates, each
    linked to its page. Position classes keep the popup on-page: below the cell
    for the top rows, right-anchored for the right-hand columns."""
    items = []
    for v in sorted(finds, key=lambda v: (v.row.get("office") or "",
                                          v.row.get("district") or "",
                                          v.row.get("name") or "")):
        c = v.row
        seat = _h(_seat_code(c))
        ie = ""
        if v.corroboration and v.corroboration.get("supporting_total"):
            ie = f'<span class="pop-ie">{_h(_money_compact(float(v.corroboration["supporting_total"])))}</span>'
        items.append(
            f'<li><a href="{_h(_href(c["candidate_id"]))}">{_h(_display_name(c.get("name")))}</a>'
            f'<span class="pop-seat">{seat}</span>{ie}</li>')
    pos = ("pop-below" if row <= 3 else "") + (" pop-right" if col >= 8 else "")
    name = STATE_NAMES.get(st, st)
    return f"""<div class="pop {pos.strip()}">
      <p class="pop-head"><span class="redbox"></span>{_h(name)} &#183; {len(finds)} finding{'s' if len(finds) != 1 else ''}</p>
      <ul class="pop-list">{''.join(items)}</ul>
      <p class="pop-foot">Click the square to filter the table</p>
    </div>"""


def _render_statemap(state_finds: dict[str, list["CandidateView"]], *,
                     includes_pending: bool) -> str:
    state_counts = {st: len(vs) for st, vs in state_finds.items()}
    total = sum(state_counts.values())
    if not total:
        return ""
    cells = []
    for st, (r, c) in sorted(_STATE_GRID.items(), key=lambda kv: kv[1]):
        n = state_counts.get(st, 0)
        num = f'<span class="n">{n}</span>' if n else ""
        if n:
            pop = _statemap_pop(st, state_finds[st], r, c)
            cells.append(f'<div class="st{_heat_class(n)} has-pop" style="grid-area:{r}/{c}"'
                         f' tabindex="0" data-state="{_h(st)}">{st}{num}{pop}</div>')
        else:
            cells.append(f'<div class="st" style="grid-area:{r}/{c}">{st}</div>')
    n_states = sum(1 for s, n in state_counts.items() if n and s in _STATE_GRID)
    off_grid = {s: n for s, n in state_counts.items() if n and s not in _STATE_GRID}
    off_note = ""
    if off_grid:
        parts = ", ".join(f"{_h(STATE_NAMES.get(s, s))} {n}" for s, n in sorted(off_grid.items()))
        off_note = f'<p class="map-offgrid">Not shown on the map: {parts}.</p>'
    scope = ("Filled squares mark states where at least one candidate posted public "
             "messaging or media-buy guidance consistent with red-boxing; intensity "
             "scales with the count of findings."
             + (" Pending detections are included in this review build."
                if includes_pending else ""))
    return f"""
    <section class="map-band" aria-label="Findings by state">
      <div class="map-cell">
        <p class="lbl">Findings by state</p>
        <div class="statemap" role="img" aria-label="US state-grid heatmap of red-boxing findings; darker red squares indicate more findings">{''.join(cells)}</div>
      </div>
      <aside class="map-aside">
        <p class="lbl">Reading the map</p>
        <p class="map-note">One square per state. {scope}</p>
        <div class="map-legend">
          <span class="key"><i></i>0</span>
          <span class="key k1"><i></i>1</span>
          <span class="key k2"><i></i>2&#8211;3</span>
          <span class="key k3"><i></i>4&#8211;6</span>
          <span class="key k4"><i></i>7+</span>
        </div>
        {off_note}
        <p class="map-count">{total} finding{'s' if total != 1 else ''} &#183; {n_states} state{'s' if n_states != 1 else ''}</p>
      </aside>
    </section>"""


def _money_compact(x: float) -> str:
    if x >= 1e9:
        return f"${x / 1e9:.1f}B"
    if x >= 1e6:
        return f"${x / 1e6:.1f}M"
    if x >= 1e3:
        return f"${x / 1e3:.0f}K"
    return f"${x:,.0f}"


_NAME_UPPER = {"II", "III", "IV", "V", "VI", "VII"}
_NAME_SUFFIX = {"JR", "SR", "II", "III", "IV", "VI", "VII"}
# Titles/degrees FEC filers append to their own names ('SHAH, AMISH DR.',
# 'BONAMICI, SUZANNE MS.', 'KAPTUR, MARCY HON. M.C.'). Compared after
# stripping dots/parens, only in the trailing zone of the given-name half —
# so 'Do' the Vietnamese name or 'Maj' the Scandinavian name can't be eaten
# (deliberately excluded), and a leading token is never touched.
_NAME_TITLE = {"MR", "MRS", "MS", "MISS", "DR", "HON", "REV", "PROF",
               "SGT", "COL", "COLONEL", "CAPT", "LT", "LTC", "GEN", "RET",
               "REP", "SEN",
               "MD", "PHD", "DDS", "OD", "JD", "ESQ", "MBA", "CPA", "FACS",
               "M.C"}


def _parse_fec_name(name: str) -> tuple[str, list[str], list[str]] | None:
    """Split FEC 'LAST, FIRST ...' into (surname, given tokens, suffixes),
    or None when there's no comma to parse.

    Split on commas (empty segments like 'BRINK,, BRIDGET' are filing noise,
    dropped); surname is the first part, everything after joins as the
    given-name half. From that half's tail, strip self-styled titles/degrees
    ('WHALEN, JEROMIE PATRICK DR.', 'DUNN, NEAL PATRICK MD, FACS', with 'THE'
    swallowed before a title for 'WOMACK, STEPHEN A THE HON') and pull off
    suffixes ('STEUBE, W. GREGORY III', 'SMITH, RAYMOND EDWARD DR. JR.'),
    returned innermost-first. A lone bare 'V' with no title context stays
    put — as a trailing token it's more likely a middle initial ('SMITH, JOHN
    V') — but after a title it reads as a suffix ('MARKERT, GEORGE WASHINGTON
    MR V'). Consecutive duplicate tokens are collapsed ('GUTHRIE, S. BRETT
    BRETT HON.')."""
    parts = [p.strip() for p in (name or "").split(",") if p.strip()]
    if len(parts) < 2:
        return None
    last = parts[0]
    given = " ".join(parts[1:]).split()
    suffixes: list[str] = []
    saw_title = False
    while given:
        # Parens join dots in the strip so '(RET.)' reads as RET; a real
        # parenthesized nickname ('(JACK)') never lands in the title set.
        t = given[-1].strip("().")
        if t in _NAME_TITLE:
            given.pop()
            saw_title = True
            if given and given[-1].rstrip(".") == "THE":
                given.pop()
        elif t in _NAME_SUFFIX or t == "V":
            suffixes.append(given.pop())
        else:
            break
    if suffixes == ["V"] and not saw_title:
        given.append(suffixes.pop())
    # Collapse consecutive duplicate words ('S. BRETT BRETT') — but keep
    # doubled initials ('MORGAN W. W.'), which can be two real names.
    given = [w for i, w in enumerate(given)
             if i == 0 or w != given[i - 1] or len(w.rstrip(".")) <= 1]
    return last, given, suffixes


def _ledger_name(name: str) -> str:
    """Light cleanup for the index ledger: keep the official ALL-CAPS
    'LAST, FIRST' form (it's the point of the home table, and it sorts by
    surname for free) but drop the filing noise a formal ledger shouldn't
    reprint — double commas, self-styled titles/degrees ('LANDER, BRAD MR.'),
    doubled tokens. Suffixes stay at the tail where FEC files them
    ('JACKSON, JESSE L. JR'). No comma: as filed."""
    parsed = _parse_fec_name(name)
    if not parsed:
        return name or ""
    last, given, suffixes = parsed
    rest = " ".join(given + list(reversed(suffixes)))
    return f"{last}, {rest}" if rest else last


def _display_name(name: str) -> str:
    """FEC ALL-CAPS 'LAST, FIRST' -> natural display order and case:
    'MOORE, FELIX BARRY' -> 'Felix Barry Moore'. Display-only — the raw FEC
    string stays the DB/sort/matching key everywhere.

    Parsing quirks live in _parse_fec_name; here the given-name half is
    re-seated before the surname with suffixes after it — a suffix already
    fused into the surname ('ONDER JR, ROBERT FRANK') lands correctly by the
    swap alone. No comma: name passes through as filed.
    Case: capitalize each letter-run (handles hyphens/apostrophes), keep
    roman-numeral suffixes upper, give Mc- surnames their inner cap."""
    parsed = _parse_fec_name(name)
    if parsed:
        last, given, suffixes = parsed
        rebuilt = " ".join(given + [last] + list(reversed(suffixes))).strip()
        if rebuilt:
            name = rebuilt

    def cap_run(m):
        run = m.group(0)
        if run in _NAME_UPPER:
            return run
        if len(run) > 2 and run.startswith("MC"):
            return "Mc" + run[2:].capitalize()
        return run.capitalize()
    return re.sub(r"[A-Za-z]+", cap_run, name or "")


def _ordinal(n: int) -> str:
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def _race_phrase(c: dict) -> str:
    """Plain-English race for prose: "North Carolina's 4th District",
    "U.S. Senate, Texas", "President" — instead of codes like H-NC-04."""
    office, state = c.get("office"), c.get("state") or ""
    sname = STATE_NAMES.get(state, state)
    if office == "P":
        return "President"
    if office == "S":
        return f"U.S. Senate, {sname}"
    try:
        d = int(c.get("district") or 0)
    except (TypeError, ValueError):
        d = 0
    return f"{sname}'s {_ordinal(d)} District" if d else f"{sname} at-large"


def _seat_compact(c: dict) -> str:
    """Short seat for <title> parentheticals: "NC-04", "TX Senate", "President"."""
    office, state = c.get("office"), c.get("state") or ""
    if office == "P":
        return "President"
    if office == "S":
        return f"{state} Senate"
    return f"{state}-{c.get('district')}" if c.get("district") else state


def _seat_code(c: dict) -> str:
    """Ledger-style seat code: "H-NC-04", "S-TX" (no district). Unescaped —
    callers wrap in _h()."""
    code = f"{c.get('office')}-{c.get('state')}"
    return f"{code}-{c.get('district')}" if c.get("district") else code


def _pretty_date(iso: str) -> str:
    """'2026-07-16...' -> 'July 16, 2026' (falls back to the raw date)."""
    try:
        d = date.fromisoformat(iso[:10])
        return f"{d.strftime('%B')} {d.day}, {d.year}"
    except ValueError:
        return iso[:10]


def _render_index(views: list[CandidateView], approved_only: bool, *,
                  counts: dict[str, int] | None = None,
                  all_views: list[CandidateView] | None = None,
                  page_no: int = 1, n_pages: int = 1,
                  data_version: str = "",
                  statuses_present: set[str] | None = None) -> str:
    # ``views`` is this page's slice; counts/coverage, the stat deck, the state
    # heatmap and the state dropdown are global (over every candidate).
    if counts is None:
        counts = {}
        for v in views:
            counts[v.status] = counts.get(v.status, 0) + 1
    if all_views is None:
        all_views = views
    if statuses_present is None:
        statuses_present = {v.status for v in views}

    mode = ("Public build — approved findings + dated negatives" if approved_only
            else "Review console — includes pending detections, not yet published")
    banner = "" if approved_only else (
        '<div class="review-banner">Review console build. Positive detections shown '
        'here are <strong>pending human review and are not published findings</strong>. '
        'Approve or reject with <code>python -m redbox review</code>.</div>')
    pager = _pager(page_no, n_pages)
    paged_note = (f'<p class="paged-note">Filtering searches all '
                  f'{len(all_views):,} candidates across every page; '
                  'rows are ordered findings-first across pages.</p>'
                  if n_pages > 1 else "")

    # Global aggregates for the stat deck + heatmap. "Finding" statuses include
    # pending in the review build (they are the review-relevant rows), published
    # only in the public build — the labels say which.
    finding_statuses = {"positive_published"} if approved_only else {
        "positive_published", "positive_pending"}
    n_findings = counts.get("positive_published", 0)
    n_pending = counts.get("positive_pending", 0) + counts.get("ambiguous_pending", 0)
    n_neg = counts.get("negative", 0)
    total = len(all_views)
    ie_total = sum(
        float(v.corroboration.get("supporting_total") or 0)
        for v in all_views
        if v.status in finding_statuses and v.corroboration)
    state_finds: dict[str, list[CandidateView]] = {}
    for v in all_views:
        if v.status in finding_statuses and v.row.get("state"):
            state_finds.setdefault(v.row["state"], []).append(v)

    stats = [f'<div class="stat stat-hot"><b>{n_findings:,}</b>'
             f'<span>Findings — reviewed &amp; published</span></div>']
    if n_pending and not approved_only:
        stats.append(f'<div class="stat stat-warm"><b>{n_pending:,}</b>'
                     f'<span>Pending human review — not published</span></div>')
    stats.append(f'<div class="stat"><b>{n_neg:,}</b>'
                 f'<span>None detected, as of the dated scan</span></div>')
    if ie_total:
        stats.append(f'<div class="stat"><b>{_h(_money_compact(ie_total))}</b>'
                     f'<span>Aligned outside money on file</span></div>')
    stats.append(f'<div class="stat"><b>{total:,}</b>'
                 f'<span>Candidates tracked nationwide</span></div>')

    heatmap = _render_statemap(state_finds, includes_pending=not approved_only)

    rows = [_index_row(v) for v in views]

    no_url = counts.get("no_url", 0)
    blocked = counts.get("blocked_by_robots", 0)
    failed = counts.get("fetch_failed", 0)
    coverage = ""
    if no_url or blocked or failed:
        parts = []
        if no_url:
            parts.append(f"{no_url} have no campaign site resolved")
        if blocked:
            parts.append(f"{blocked} block automated access (robots.txt)")
        if failed:
            parts.append(f"{failed} were unreachable (fetch failed)")
        coverage = (f'<p class="coverage"><strong>Coverage gap:</strong> '
                    f'{", ".join(parts)} and were <strong>not scanned</strong> '
                    f'(of {total:,}). For these, no finding means no site was scanned, '
                    f'not that none exists.</p>')

    body = f"""
    <p class="kicker">Campaign Finance <span class="redbox"></span> Red-Boxing <span class="redbox"></span> <span class="k-hot">The Candidate Index</span></p>
    <h1 class="headline">Super PAC instructions, posted in plain sight.</h1>
    <p class="deck">Every funded federal campaign&#8217;s website, read for the advertising it asks outside groups to buy &#8212; and archived before it can disappear.</p>
    <div class="byline-row">
      <span><strong>{_h(mode)}</strong></span>
      <span class="sep">&#9632;</span>
      <span>{total:,} candidates in the universe</span>
      <span class="sep">&#9632;</span>
      <span>Every claim links to archived evidence</span>
    </div>
    {banner}
    <div class="lede-grid">
      <div class="lede-copy">
        <p class="standfirst dropcap">Red-boxing is publicly posted messaging or media-buy guidance whose function is to direct an outside group &#8212; a super PAC &#8212; on what advertising to run: for whom, where, when, and with what message, without a private conversation. Federal rules bar private coordination; posting the guidance publicly is lawful, and the practice exploits that opening openly. It is <strong>not per se unlawful.</strong></p>
        <p>Detections are gated behind human review before any are treated as findings.</p>
      </div>
      <aside class="stat-deck" aria-label="Summary statistics">{''.join(stats)}</aside>
    </div>
    {coverage}
    {heatmap}
    <form class="controls" action="" onsubmit="return false">
      <label for="q">Filter</label>
      <input id="q" type="search" placeholder="Filter by name…" aria-label="Filter by name">
      <select id="f-status" aria-label="Filter by status"><option value="">All statuses</option>{_status_opts(statuses_present)}</select>
      <select id="f-state" aria-label="Filter by state"><option value="">All states</option>{_state_opts(all_views)}</select>
      {paged_note}
    </form>
    <table class="agate"><thead><tr>
      <th data-sort="0">Candidate</th><th data-sort="1">Seat</th><th data-sort="2">Party</th>
      <th data-sort="3">Status</th>
      <th data-sort="4" class="num">Aligned IE</th>
    </tr></thead><tbody id="rows">{''.join(rows)}</tbody></table>
    {pager}
    <script>const PAGED={'true' if n_pages > 1 else 'false'},DATA_V='{data_version}';{INDEX_JS}</script>"""
    desc = (f"RedBoxWatch tracks red-boxing — public messaging and media-buy "
            f"guidance posted on federal campaign websites to signal super PACs. "
            f"{n_findings} human-reviewed findings across {total} candidates, "
            f"every claim linked to archived evidence.")
    return _layout("Candidate Index" if page_no == 1 else f"Candidate Index — page {page_no}",
                   body, page_class="page-index", active="index",
                   path="index" if page_no == 1 else f"index-{page_no}", desc=desc,
                   og_title="RedBoxWatch" if page_no == 1 else None)


def _index_row(v: CandidateView) -> str:
    # The visitor-facing home table stays simple: name, seat, party, status,
    # aligned money. Classifier confidence and page counts are working-notes —
    # they live on the candidate page, not the front door. Names print in the
    # official FEC "LAST, FIRST" form (user's call: the ledger formality is
    # the point, and it sorts by surname for free), lightly cleaned of filing
    # noise by _ledger_name; data-name carries the display form ("Haley
    # Stevens") so the filter matches either name order.
    c = v.row
    pill_txt, pill_cls = _pill(v.status)
    ie = ""
    if v.corroboration and v.corroboration.get("supporting_total"):
        ie = f"${float(v.corroboration['supporting_total']):,.0f}"
    # data-* attributes carry only what INDEX_JS reads (status/state/name);
    # office and party once rode along too — ~70KB of dead markup per build.
    return f"""<tr data-status="{v.status}" data-state="{_h(c.get('state'))}"
        data-name="{_h(_display_name(c.get('name')))}">
      <td class="cand"><a href="{_h(_href(c['candidate_id']))}">{_h(_ledger_name(c.get('name')))}</a></td>
      <td class="seat">{_h(_seat_code(c))}</td>
      <td class="party">{_h(c.get('party'))}</td>
      <td><a class="status {pill_cls}" href="{_h(_href(c['candidate_id']))}">{pill_txt}</a></td>
      <td class="num ie ie-col">{ie}</td>
    </tr>"""


def _state_opts(views):
    states = sorted({v.row.get("state") for v in views if v.row.get("state")})
    return "".join(f'<option value="{_h(s)}">{_h(s)}</option>' for s in states)


def _status_opts(present: set[str]) -> str:
    """Status-filter options, from the STATUSES table — but only for statuses
    actually present in the rendered rows. The hardcoded full list once
    offered "Pending"/"Rejected" on the public build, where those statuses are
    filtered out before rendering: selecting one showed a silently empty
    table on a site whose credibility rests on saying what it publishes."""
    return "".join(
        f'<option value="{k}">{_h(s.filter_label)}</option>'
        for k, s in STATUSES.items() if k in present)


def _render_candidate(v: CandidateView, *, public: bool = False) -> str:
    c = v.row
    pill_txt, pill_cls = _pill(v.status)
    seat = _h(_seat_code(c))
    office = _OFFICE_WORD.get(c.get("office") or "", c.get("office") or "")
    state_name = STATE_NAMES.get(c.get("state") or "", c.get("state") or "")
    party = _PARTY_WORD.get(c.get("party") or "", c.get("party") or "")
    kicker_status = pill_txt.title()

    pending = v.status in ("positive_pending", "ambiguous_pending")
    banner = ('<div class="review-banner">This detection is <strong>pending human review '
              'and is not a published finding.</strong></div>') if pending else ""
    # Inactive rows reach the publisher only when they carry an approved finding
    # (the _gather query drops the rest) — label the ended run rather than
    # silently unpublishing a real, archived red box.
    if c.get("inactive"):
        ended_why = ('This candidate <strong>was defeated in the primary</strong>, so '
                     'the candidacy has ended.'
                     if c.get("inactive") == 3 else
                     'Per FEC records, this is <strong>no longer an active candidacy'
                     '</strong> — withdrawn, superseded (e.g. the candidate now seeks '
                     'a different office), or otherwise ended.')
        banner += (f'<div class="ended-banner">{ended_why} The finding below was '
                   'detected and archived while the campaign was live and remains '
                   'on the ledger as a historical record.</div>')

    meta = f"""<dl class="meta">
      <dt>Seat</dt><dd>{seat}</dd>
      <dt>Party</dt><dd>{_h(c.get('party'))}</dd>
      <dt>Universe reason</dt><dd>{_h(c.get('universe_reason'))}</dd>
      <dt>FEC receipts</dt><dd><a href="https://www.fec.gov/data/candidate/{_h(c['candidate_id'])}/" rel="noopener" title="Financial summary at FEC.gov">${float(c.get('receipts') or 0):,.0f}</a> <span class="src-tag">FEC</span></dd>
      <dt>Campaign site</dt><dd><a href="{_h(c.get('website_url'))}" rel="nofollow noopener">{_h(c.get('website_url'))}</a> ({'verified' if c.get('url_verified') else 'unverified'})</dd>
      <dt>Pages scanned</dt><dd>{v.scan_count}{(' · last ' + _h(v.last_scanned[:10])) if v.last_scanned else ''}</dd>
    </dl>"""

    detail = ""
    if v.exhibits:
        # One section per distinct page still carrying guidance (spec-order:
        # the candidate's representative detection leads; further red boxes on
        # other pages follow as their own exhibits).
        detail = "".join(
            _render_exhibit(e, first=(i == 0),
                            label_override=(v.label if i == 0 else None))
            for i, e in enumerate(v.exhibits))
    elif v.detection and v.status != "negative":
        # No renderable exhibit (e.g. every flag was rejected): show the
        # representative detection under the status label, as before.
        detail = _render_exhibit(
            {"detection": v.detection, "evidence": v.evidence,
             "archive": v.archive, "review": v.review, "approved": False,
             "pending": False, "alias_urls": [], "gone_since": None,
             "timeline": []},
            first=True, label_override=v.label)
    elif v.status == "negative":
        detail = f"""<section class="detection">
          <h2 class="section-head section-head-quiet">{_h(v.label)}</h2>
          <p class="rationale">Across {v.scan_count} pages scanned, no content matching the functional red-box pattern was detected. Absence of a finding is not proof — a box may have been removed, a page uncrawled, or a PDF unparsed.</p>
        </section>"""
    elif (spec := STATUSES.get(v.status)) is not None and spec.gap_body:
        # Coverage-gap statuses (no_url / blocked_by_robots / fetch_failed):
        # one section shape, heading = the status row label, body from the
        # STATUSES table with the resolved site linked in where relevant.
        site = (f'<a href="{_h(c.get("website_url"))}" rel="nofollow noopener">'
                f'{_h(c.get("website_url"))}</a>')
        detail = (f'<section class="detection"><h2 class="section-head section-head-quiet">'
                  f'{_h(v.label)}</h2>'
                  f'<p class="rationale">{spec.gap_body.format(site=site)}</p></section>')

    body = f"""
    <article class="article">
    <p class="crumb"><a href="{_href('index')}">&#8592; Back to the index</a></p>
    <p class="kicker">{_h(kicker_status)} <span class="redbox"></span> {_h(office)} &#183; {_h(state_name)} <span class="redbox"></span> {_h(party)}</p>
    <h1 class="headline headline-cand">{_h(_display_name(c.get('name')))} <span class="finding-tag {pill_cls}">{pill_txt}</span></h1>
    {banner}{meta}{'' if public else _render_changes(v)}{detail}{_render_corroboration(v)}
    </article>"""

    prose = _display_name(c.get("name"))
    seat_short = _seat_compact(c)
    race = _race_phrase(c)
    sup = float(v.corroboration.get("supporting_total") or 0) if v.corroboration else 0
    if v.status == "positive_published":
        # Past tense once every guidance-carrying page has come down: the
        # finding stays on the ledger, but "carries" would overstate the
        # present. The page body says the same via per-exhibit notes.
        gone_all = bool(v.exhibits) and all(e["gone_since"] for e in v.exhibits)
        title = f"Red-Boxing Detected: {prose} ({seat_short})"
        verb = "carried" if gone_all else "carries"
        desc = (f"{prose}'s campaign website {verb} a red box — messaging cues "
                f"that tell super PACs what ads to run."
                + (" The guidance has since been removed." if gone_all else "")
                + (f" {_money_compact(sup)} in aligned outside spending is on "
                   f"file." if sup >= 10_000 else "")
                + " See the archived evidence.")
    elif pending:
        title = f"{prose} ({seat_short}) — Possible Red-Boxing, Pending Review"
        desc = (f"Our screen flagged possible red-boxing on {prose}'s campaign "
                f"website ({race}). A human reviewer has not confirmed it — this "
                f"is not a published finding.")
    elif v.status == "negative":
        asof = _pretty_date(v.last_scanned) if v.last_scanned else ""
        title = f"{prose} ({seat_short}) — No Red-Boxing Detected"
        desc = (f"We scanned {prose}'s campaign website ({race}) and found no "
                f"red-boxing — no messaging cues aimed at super PACs"
                + (f" as of {asof}" if asof else "") + ".")
    elif v.status == "rejected":
        title = f"{prose} ({seat_short}) — Reviewed: Not Red-Boxing"
        desc = (f"A flagged detection on {prose}'s campaign website ({race}) was "
                f"reviewed by a human and rejected. No finding published.")
    else:
        title = f"{prose} ({seat_short}) — Not Scanned"
        desc = (f"{prose} ({race}): campaign website not scanned — "
                f"{v.label.lower()}. This is a coverage gap, not a finding.")
    og_image = (v.archive or {}).get("screenshot_rel") if v.status == "positive_published" else None
    return _layout(title, body, page_class="page-finding", active="index",
                   path=c["candidate_id"], desc=desc, og_image=og_image,
                   og_type="article")


def _exhibit_label(e: dict) -> str:
    """Per-exhibit §3.7a label, mirroring _status's label choices."""
    if e["detection"].get("classification") == "ambiguous":
        return AMBIGUOUS_CONFIRMED_LABEL if e["approved"] else AMBIGUOUS_LABEL
    return POSITIVE_LABEL


def _archive_figure(archive: dict | None, *, alt: str, what: str = "",
                    figure_class: str = "exhibit") -> str:
    """The archived-evidence block one archive row renders: the links line
    (archived PDF / Wayback snapshot / raw-HTML note) plus, when a screenshot
    exists, the framed figure. Shared by the current exhibit and each earlier
    version so the two renderings can't drift again (they had: one lazy-loaded
    its image, one omitted the raw-HTML note). Reconciled deliberately:
    images lazy-load in both (all sit below the fold), and the raw-HTML note
    appears in both (every archive preserves the HTML alongside the shot)."""
    if not archive:
        return ""
    bits = []
    if archive.get("pdf_rel"):
        bits.append(f'<a href="{_h(archive["pdf_rel"])}" rel="noopener">Archived PDF (original)</a>')
    if archive.get("wayback_url"):
        bits.append(f'<a href="{_h(archive["wayback_url"])}" rel="noopener">Wayback snapshot</a>')
    if archive.get("html_path"):
        bits.append("Raw HTML preserved")
    caption = " &#183; ".join(bits)
    if archive.get("screenshot_rel"):
        rel = _h(archive["screenshot_rel"])
        cap = (what + ((" &#183; " + caption) if caption else "")) if what else caption
        return f"""<figure class="{figure_class}">
          <a class="exhibit-frame" href="{rel}"><img src="{rel}" alt="{alt}" loading="lazy"></a>
          <figcaption><span class="exhibit-label">Archived at detection</span>{cap}</figcaption>
        </figure>"""
    if caption:
        return f'<p class="evlinks"><span class="exhibit-label">Archived at detection</span>{caption}</p>'
    return ""


def _render_exhibit(e: dict, *, first: bool, label_override: str | None = None) -> str:
    """One detection section: label, source line, rationale, quoted evidence,
    archived exhibit, page text. Repeated per distinct guidance-carrying page —
    a candidate with red boxes on two pages gets two of these."""
    d = e["detection"]
    archive = e["archive"]
    ev = "".join(
        f'<li><blockquote>{_h(q.get("quote"))}</blockquote><span class="why">{_h(q.get("why"))}</span></li>'
        for q in e["evidence"])
    exhibit = ""
    if archive:
        if archive.get("pdf_rel"):
            alt = f"Rendered pages of the archived PDF from {_h(d.get('page_url'))}"
            what = f"Pages rendered from the PDF at {_h(d.get('page_url'))}"
        else:
            alt = f"Archived screenshot of {_h(d.get('page_url'))}"
            what = f"Full-page screenshot of {_h(d.get('page_url'))}"
        exhibit = _archive_figure(archive, alt=alt, what=what)

    also = "" if first else '<p class="exhibit-more">Additional page with guidance</p>'
    timeline = ""
    if e.get("timeline"):
        # Revision entries link into the version history below, so "Guidance
        # revised" shows what it was revised FROM instead of dead-ending.
        vhref = f'#versions-{d["detection_id"]}' if e.get("versions") else None

        def _tl_label(kind: str) -> str:
            lab = _EVENT_LABEL.get(kind, (kind, ""))[0]
            if vhref and kind in ("revised", "changed", "updated"):
                return f'<a href="{vhref}">{lab}</a>'
            return lab

        items = "".join(
            f'<li class="tl-{kind}"><span class="tl-l">{_tl_label(kind)}</span>'
            f'<span class="tl-d">{_h(_pretty_date(day))}</span></li>'
            for day, kind in e["timeline"])
        timeline = f'<ol class="tl" aria-label="Detection timeline">{items}</ol>'
    aliases = ""
    if e.get("alias_urls"):
        shown = e["alias_urls"][:6]
        more = len(e["alias_urls"]) - len(shown)
        links = ", ".join(f'<a href="{_h(u)}" rel="nofollow noopener">{_h(u)}</a>'
                          for u in shown) + (f" and {more} more" if more > 0 else "")
        aliases = (f'<p class="srcline alias-line">The same page body is also served at '
                   f'{links}.</p>')
    pending_tag = ("&ensp;&#183;&ensp;<strong>pending human review — not published</strong>"
                   if e.get("pending") else "")
    gone = ""
    if e.get("gone_since"):
        gone = (f'<div class="gone-note"><strong>No longer present.</strong> When this '
                f'page was last checked, on {_h(_pretty_date(e["gone_since"]))}, this '
                f'guidance was not found. The finding remains on the ledger and the '
                f'archived evidence below is preserved — guidance taken down after '
                f'drawing notice is itself part of the record.</div>')
    label = label_override if label_override is not None else _exhibit_label(e)
    return f"""
    <section class="detection">
      {also}<h2 class="section-head"><span class="redbox"></span>{_h(label)}</h2>
      <p class="srcline">Detected on <a href="{_h(d.get('page_url'))}" rel="nofollow noopener">{_h(d.get('page_url'))}</a>
         &ensp;&#183;&ensp;classifier confidence {float(d.get('confidence') or 0):.2f}&ensp;&#183;&ensp;model {_h(d.get('model'))}{' (escalated)' if d.get('escalated') else ''}{pending_tag}</p>
      {timeline}{aliases}{gone}
      <p class="rationale">{_h(d.get('rationale'))}</p>
      <h3 class="evidence-head"><span class="redbox"></span>Quoted evidence — verbatim spans from the page</h3>
      <ul class="evidence">{ev}</ul>
      {exhibit}
      {_render_page_text(archive)}
      {_render_versions(e)}
    </section>"""


def _render_versions(e: dict) -> str:
    """Collapsed version history for one exhibit: each earlier revision of the
    guidance with its lifespan, quoted spans, what the next revision changed,
    and the SAME archived-evidence treatment the current version gets. Every
    revision was archived at its own detection time, so this is preserved
    primary material, not reconstruction."""
    vers = e.get("versions") or []
    if not vers:
        return ""
    d = e["detection"]
    blocks = []
    for i, ver in enumerate(vers, start=1):
        vd = ver["detection"]
        span = f"Live from {_pretty_date(ver['first_seen'])}" if ver.get("first_seen") else ""
        if ver.get("superseded"):
            span += f" &#8211; revised {_pretty_date(ver['superseded'])}"
        pending = ("" if ver["approved"]
                   else ' <strong class="v-pending">pending human review — not published</strong>')
        quotes = "".join(
            f'<li><blockquote>{_h(q.get("quote"))}</blockquote></li>'
            for q in ver["evidence"] if q.get("quote"))
        # Diff against the next version IN THIS (already review-filtered)
        # list, falling back to the current exhibit — so an unpublished
        # revision's quotes can never leak through the diff line.
        nxt_det = vers[i]["detection"] if i < len(vers) else d
        added, dropped = _quote_diff(vd, nxt_det)
        diffbits = []
        if added:
            diffbits.append("added " + "; ".join(
                f"&#8220;{_h(q)}&#8221;" for q in added[:3]))
        if dropped:
            diffbits.append("dropped " + "; ".join(
                f"&#8220;{_h(q)}&#8221;" for q in dropped[:3]))
        diff = (f'<p class="v-diff">The next revision {"; ".join(diffbits)}.</p>'
                if diffbits else "")
        arch = ver.get("archive") or {}
        shot = _archive_figure(
            arch, alt=f"Archived screenshot of version {i} of {_h(d.get('page_url'))}",
            figure_class="exhibit v-exhibit")
        blocks.append(f"""<div class="version">
        <p class="v-span">Version {i}{(' &#183; ' + span) if span else ''}{pending}</p>
        <ul class="evidence">{quotes}</ul>
        {diff}{shot}
        {_render_page_text(arch)}
      </div>""")
    n = len(vers)
    return f"""<details class="versions" id="versions-{d['detection_id']}">
      <summary>Earlier version{'s' if n != 1 else ''} of this guidance ({n}) — archived</summary>
      <p class="v-note">Each revision below was archived when it was detected; the
      current version is shown above. Newest earlier version last.</p>
      {''.join(blocks)}
    </details>"""


def _render_page_text(archive: dict | None) -> str:
    """Collapsed plain text of the archived page, from the archiver's extracted-
    text file. The screenshot can be obscured by a cookie banner or pop-up, and
    an image is opaque to screen readers — the text is the accessible record."""
    if not (archive and archive.get("text_path")):
        return ""
    try:
        txt = Path(archive["text_path"]).read_text(errors="replace").strip()
    except OSError:
        return ""
    if not txt:
        return ""
    return f"""<details class="pagetext">
      <summary>Plain text of the archived page</summary>
      <pre>{_h(txt)}</pre>
    </details>"""


# One label table per change kind — (short label, long description) — shared
# by the per-exhibit timeline strip (short) and the review-build change log
# (both). 'changed' and 'modified' used to be two names for the same concept
# split across two dicts; _change_key now yields 'changed' everywhere a
# 'modified' event couldn't be refined.
_EVENT_LABEL: dict[str, tuple[str, str]] = {
    "detected": ("First detected", ""),
    "put_up": ("Guidance posted", "Messaging guidance appeared on this page that was not present on the prior scan."),
    # 'modified' refined by whether the quoted spans themselves changed:
    "updated": ("Page updated", "The page's text changed between scans; the quoted guidance spans are identical."),
    "revised": ("Guidance revised", "The quoted guidance spans on this page changed between scans."),
    "changed": ("Guidance changed", "Previously-detected guidance on this page changed between scans."),
    "take_down": ("Guidance removed", "Messaging guidance previously detected on this page was no longer present on re-scan."),
    "gone": ("No longer present", ""),
}


def _change_key(ch: dict) -> str:
    """Event key for labeling: 'modified' refines to updated/revised when the
    quote comparison could tell (guidance_same True/False), and stays the
    generic 'changed' when it couldn't."""
    if ch["event_type"] != "modified":
        return ch["event_type"]
    same = ch.get("guidance_same")
    return "updated" if same else ("revised" if same is False else "changed")


def _render_changes(v: CandidateView) -> str:
    """Raw per-URL event log — review builds only. Public pages carry the
    review-gated per-exhibit timelines instead: this list is built from raw
    classifier transitions, so it can name URLs whose detections are pending
    or were rejected, which must never surface on a public page."""
    if not v.changes:
        return ""
    items = []
    for ch in v.changes:
        key = _change_key(ch)
        title, desc = _EVENT_LABEL.get(key, (key, ""))
        cls = "chg-down" if ch["event_type"] == "take_down" else (
            "chg-up" if ch["event_type"] == "put_up" else "chg-mod")
        items.append(
            f'<li class="{cls}"><span class="chg-when">{_h((ch.get("detected_at") or "")[:10])}</span>'
            f'<strong>{_h(title)}</strong> — {_h(desc)} '
            f'<span class="chg-url">{_h(ch.get("url"))}</span></li>')
    return f"""
    <section class="changes">
      <h2 class="section-head"><span class="redbox"></span>Change history <span class="review-only-tag">review console only</span></h2>
      <p class="srcline">Raw put-up / take-down event log across re-scans, all URLs —
         including detections still pending or rejected on review. Public pages show
         only the reviewed, per-exhibit timeline.</p>
      <ul class="changelog">{''.join(items)}</ul>
    </section>"""


def _render_corroboration(v: CandidateView) -> str:
    co = v.corroboration
    if not co or not co.get("ie_filing_count"):
        return ""
    try:
        committees = json.loads(co.get("spender_list") or "[]")
    except json.JSONDecodeError:
        committees = []
    sup = float(co.get("supporting_total") or 0)
    opp = float(co.get("opposing_total") or 0)
    after = float(co.get("supporting_ie_total_after") or 0)
    detected = (co.get("guidance_first_detected") or "")[:10]

    rows = "".join(
        f"""<tr><td><span class="ind ind-{_h(s.get('indicator'))}">{_h(s.get('indicator'))}</span></td>
            <td class="committee">{_h(s.get('committee_name') or s.get('committee_id'))}</td>
            <td class="num ie">${float(s.get('amount') or 0):,.0f}</td>
            <td class="num">{_h(s.get('count'))}</td>
            <td class="range">{_h(s.get('first_date'))} – {_h(s.get('last_date'))}</td></tr>"""
        for s in committees)

    seq = (f'<p class="sequence"><strong>Sequence.</strong> Messaging guidance was '
           f'present on the candidate\'s site when detected (by us) on {_h(detected)}; '
           f'<strong class="money-hot">${sup:,.0f}</strong> in <em>supporting</em> independent expenditures '
           f'is on file for this candidate'
           + (f', and <strong>${opp:,.0f}</strong> opposing' if opp else '')
           + '.</p>') if detected else ""

    caveat = (f'<p class="caveat">Schedule E is corroboration, not the trigger — a filed '
              f'expenditure is <em>late</em> evidence, and our detection timestamp reflects '
              f'when we crawled, which can lag the actual posting. Supporting IE dated on or '
              f'after our detection day: <strong>${after:,.0f}</strong>. Dates are FEC '
              f'expenditure dates.</p>')

    return f"""
    <section class="corroboration">
      <h2 class="section-head"><span class="redbox"></span>Independent-expenditure corroboration</h2>
      <p class="srcline"><a href="https://www.fec.gov/data/independent-expenditures/?candidate_id={_h(v.row['candidate_id'])}" rel="noopener">FEC Schedule E</a>&ensp;&#183;&ensp;{_h(co.get('ie_filing_count'))} filings</p>
      {seq}
      <table class="agate ie-table"><thead><tr><th></th><th>Committee</th>
        <th class="num">Amount</th><th class="num">Filings</th><th>Date range</th></tr></thead>
        <tbody>{rows}</tbody></table>
      <p class="legend"><span class="ind ind-S">S</span>&ensp;Supporting&emsp;<span class="ind ind-O">O</span>&ensp;Opposing</p>
      {caveat}
    </section>"""


def _render_methodology() -> str:
    body = """
    <article class="article">
    <p class="kicker">The Method <span class="redbox"></span> How a red box is found</p>
    <h1 class="headline">Methodology</h1>
    <p class="rationale dropcap">This project detects <em>red-boxing</em> — publicly posted messaging or media-buy
    guidance whose function is to direct an outside group (such as a super PAC) on what
    advertising to run, for whom, where, when, and with what message, without a private
    conversation. Federal rules bar private coordination; posting guidance publicly is
    lawful, and this practice exploits that opening openly. It is <strong>not per se unlawful.</strong></p>
    <h2 class="section-head"><span class="redbox"></span>How a candidate enters the universe</h2>
    <p class="rationale">Candidates are drawn from FEC data: funded candidates (above a receipts floor) in
    contested party primaries, plus an overlay of competitive general-election races. Each
    candidate's official campaign URL is resolved and labeled as either human-verified or
    auto-resolved; <strong>a flagged site's attribution to the candidate is confirmed by a
    human reviewer before any finding is published.</strong> Red-boxing has historically
    been concentrated on the Democratic side; the scan itself covers all funded federal
    candidates regardless of party, and findings reflect what the crawls detect.</p>
    <h2 class="section-head"><span class="redbox"></span>How pages are evaluated</h2>
    <p class="rationale">Sites are crawled with a real browser (visible and hidden page text, plus
    linked PDFs), politely and identifiably. Extracted text is classified by its
    <strong>function, not its styling</strong> — segmented audiences paired with directives;
    channel/timing/geography cues that only make sense as media-buy instructions; prescribed
    themes, contrast framing, or assets to feature; dated "more to follow" cadences. Standard
    press kits, donation/volunteer calls to action, and ordinary issue pages are
    <strong>not</strong> flagged. A decisive test is <strong>audience</strong>: red-boxing is
    guidance whose intended reader is an <em>outside spender</em> (a super PAC) who will run
    paid advertising. Directive language aimed at the campaign's own press shop or at
    journalists (internal press notes), or content written to persuade voters directly, is
    <strong>not</strong> red-boxing even when prescriptive.</p>
    <h2 class="section-head"><span class="redbox"></span>Evidence and the human gate</h2>
    <p class="rationale">Every positive or ambiguous detection is archived at detection time with full-page
    screenshot, raw HTML, extracted text, and a Wayback snapshot, so every claim can
    survive a take-down. <strong>No positive is published as a finding until a human reviewer approves it</strong>,
    having viewed the archived evidence and quoted spans. Negatives are recorded as dated
    "no guidance detected as of [date]" statements, never as "does not red-box."</p>
    </article>"""
    return _layout("Methodology", body, page_class="page-finding", active="methodology",
                   path="methodology",
                   desc=("How RedBoxWatch detects red-boxing: polite browser crawls "
                         "of campaign sites, classification by function rather than "
                         "styling, archived evidence for every claim, and a human "
                         "review gate before anything is published."))


def _render_corrections() -> str:
    body = """
    <article class="article">
    <p class="kicker">Standards <span class="redbox"></span> Accuracy &amp; recourse</p>
    <h1 class="headline">Corrections &amp; appeals</h1>
    <p class="rationale">We aim to be accurate, neutral, and evidence-linked. If you are a candidate or
    representative and believe a detection is mistaken or mischaracterized:</p>
    <ul class="standards">
      <li>Every published item links to the archived screenshot, HTML, and the exact quoted
      spans the classification rests on. Please reference the specific page and quotes.</li>
      <li>Request a correction or appeal by email:
      <a id="cx-mail" href="#">corrections&nbsp;[at]&nbsp;redboxwatch&nbsp;[dot]&nbsp;org</a>.
      Provide the candidate, the page URL, and the basis for the correction.</li>
      <li>Absence of a finding is never proof of absence — and a finding is a statement about
      <em>posted public content</em>, not an assertion of illegality.</li>
    </ul>
    <p class="rationale">Corrections are logged and the affected page updated with a dated note.</p>
    </article>
    <script>(function(){
      // Address is stored as reversed fragments so it never appears in the
      // HTML source in scrapeable form; assembled here for real visitors.
      var r=function(s){return s.split('').reverse().join('')};
      var e=r('snoitcerroc')+String.fromCharCode(64)+r('hctawxobder')+'.'+r('gro');
      var l=document.getElementById('cx-mail');
      l.href='mailto:'+e+'?subject='+encodeURIComponent('Correction / appeal request');
      l.textContent=e;
    })();</script>"""
    return _layout("Corrections & appeals", body, page_class="page-finding", active="corrections",
                   path="corrections",
                   desc=("How to request a correction or appeal a RedBoxWatch "
                         "finding. Every published item links to archived evidence; "
                         "corrections are logged and dated."))


def _render_about() -> str:
    body = f"""
    <article class="article">
    <p class="kicker">About <span class="redbox"></span> Who runs this</p>
    <h1 class="headline">Built by a volunteer who saw the box up close.</h1>
    <p class="rationale">RedBoxWatch is an independent, one-person monitoring project. It crawls the
    public websites of federal candidates nationwide, detects red-boxing &#8212; message
    guidance posted in plain sight for the super PACs that are barred from coordinating
    with campaigns directly &#8212; and publishes the evidence: the archived page, the exact
    quoted spans, and the aligned outside spending that followed.</p>
    <h2 class="section-head"><span class="redbox"></span>Why it exists</h2>
    <p class="rationale">The site is built and maintained by <strong>Charlie Garfield</strong>,
    who spent 2026 as a campaign fellow in one of the most expensive House primaries
    ever run, where he saw the effects of red-boxing firsthand.</p>
    <p class="rationale">Red-box pages are what this site exists to surface. Voters deserve to know exactly how
    involved a candidate is with the outside money in their local race &#8212; not because
    red-boxing is unlawful (it is not), but because it is public, deliberate, and easy to
    miss unless someone points at it.</p>
    <h2 class="section-head"><span class="redbox"></span>Who&#8217;s behind it</h2>
    <p class="rationale">Charlie is a recent graduate of Davidson College with a degree 
    in Computer Science and Sociology. He believes technology can make democracy more
    transparent. RedBoxWatch is unaffiliated with any campaign, party, or PAC. Findings
    currently skew toward Democratic campaigns because the practice historically has; the
    scan covers every funded candidate of every party.</p>
    <h2 class="section-head"><span class="redbox"></span>Open source</h2>
    <p class="rationale">The entire pipeline &#8212; candidate discovery, crawling, classification,
    evidence archiving, and the generator that builds this site &#8212; is open source at
    <a href="https://github.com/charliegarfield/redboxwatch">github.com/charliegarfield/redboxwatch</a>
    (AGPL&#8209;3.0). A site that asks campaigns to work in the open should work in the
    open itself: the methodology can be read, checked, and re-run. Every finding
    published <em>here</em> passed human review before publication; output from other
    deployments of the code is not a finding of RedBoxWatch.</p>
    <h2 class="section-head"><span class="redbox"></span>Contact</h2>
    <p class="rationale">Press and media inquiries:
    <a class="px-mail" href="#">press&nbsp;[at]&nbsp;redboxwatch&nbsp;[dot]&nbsp;org</a>.
    Candidates or representatives disputing a finding:
    <a href="{_href('corrections')}">corrections &amp; appeals</a>.</p>
    </article>"""
    return _layout("About", body, page_class="page-finding", active="about",
                   path="about",
                   desc=("Who runs RedBoxWatch and why: an independent project by "
                         "Charlie Garfield, a former campaign volunteer, tracking "
                         "red-boxing — public campaign-site signals to super PACs — "
                         "across federal races nationwide."))


def _render_404() -> str:
    # Served by Pages (with a real 404 status) for every unknown path — its
    # presence is also what disables the SPA soft-200 fallback to index.html.
    # Voice: the site's own dated-negative methodology, applied to the page.
    body = f"""
    <article class="article notfound">
    <p class="kicker">Error 404 <span class="redbox"></span> A dated negative</p>
    <h1 class="headline">No page detected at this URL.</h1>
    <div class="nf-box" role="presentation"><span>This box intentionally left empty.</span></div>
    <p class="standfirst">We read campaign websites for instructions to super PACs.
    We have read this page with particular care, and can report the strongest
    negative finding in our files: no red box, no media-buy guidance,
    no page at all &#8212; as of {_h(_pub_date())}, and checked against
    archived evidence of nothing.</p>
    <p class="rationale">Our methodology requires us to note that absence of a
    finding is never proof of absence. Here we make our sole exception.</p>
    <p class="rationale">Candidate pages do come and go as races end and
    candidacies close &#8212; if you followed a link here, the campaign may
    simply be over. The <a href="{_href('index', '/')}">Candidate Index</a> is current;
    our <a href="{_href('methodology', '/')}">methodology</a> explains what we track,
    and if you believe this page <em>should</em> exist,
    <a href="{_href('corrections', '/')}">corrections &amp; appeals</a> is that way.</p>
    </article>"""
    return _layout("Page not found", body, page_class="page-finding", root="/")



# INDEX_JS and CSS live in redbox/assets/ (index.js, site.css), loaded by
# render.py and re-exported here.
