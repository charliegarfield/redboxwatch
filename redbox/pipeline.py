"""End-to-end scan pipeline tying crawler -> classifier -> archiver -> DB.

For one candidate: enumerate + render pages, classify each page's text, write a
``scans`` row and a ``detections`` row, and — for positive/ambiguous detections
— archive evidence (screenshot/HTML/text + optional Wayback) and write an
``archives`` row linked to the detection (spec §3.3–3.5).

Safety gates honored here:
- A candidate with no resolved ``website_url`` is skipped (nothing to scan).
- URL verification is a *signal*, not a blocker by default: any candidate with a
  resolved URL is scanned, and ``url_verified`` is surfaced to the human review /
  publish gate so attribution is confirmed before anything is published. Pass
  ``require_verified=True`` (config ``require_verified_url``) to restore the spec
  §3.1 pre-scan gate.
- Re-classification is skipped when a page's ``text_hash`` is unchanged from a
  prior scan (spec §3.3), while still recording the new scan for take-down/diff
  history.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from . import prefilter
from .archiver import Archiver
from .classifier import Classifier
from .crawler import Crawler, FetchResult
from .util import now_iso


@dataclass
class ScanOutcome:
    candidate_id: str
    pages_scanned: int = 0
    detections: int = 0
    positives: int = 0
    ambiguous: int = 0
    prefiltered: int = 0          # pages skipped by the cheap pre-filter (no LLM call)
    deduped: int = 0              # template-alias pages: body already classified this scan
    archived: int = 0
    changes: int = 0
    take_downs: int = 0
    put_ups: int = 0
    robots_blocked: bool = False   # site's robots.txt disallowed our crawler
    skipped_reason: str | None = None


def _insert_scan(conn: sqlite3.Connection, candidate_id: str, r: FetchResult) -> int:
    # We deliberately do NOT store raw_html here. Full markup is ~50x the size of
    # the extracted text and was never read back; for any page worth preserving,
    # the archiver writes the full HTML to disk (spec §3.4). The DB audit trail is
    # raw_text (what the classifier actually saw) + text_hash (change detection).
    cur = conn.execute(
        """INSERT INTO scans (candidate_id, url, fetched_at, http_status,
               content_type, render_mode, raw_text, text_hash,
               discovered_via)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (candidate_id, r.url, r.fetched_at, r.status, r.content_type,
         r.render_mode, r.classifier_text, r.text_hash,
         r.discovered_via),
    )
    # No commit here: scan_candidate batches all of a page's writes (scan +
    # detection + archive + change) into one commit so the SQLite write lock is
    # taken once per page, not 3-4 times. See scan_candidate for the ordering
    # that keeps the (slow) network/LLM work OUT of the write transaction.
    return cur.lastrowid


def _prev_scan(conn: sqlite3.Connection, candidate_id: str, url: str):
    """Most recent prior scan of this exact URL (before the new one is inserted)."""
    return conn.execute(
        """SELECT scan_id, text_hash FROM scans
           WHERE candidate_id=? AND url=? ORDER BY scan_id DESC LIMIT 1""",
        (candidate_id, url)).fetchone()


def _classification_for_scan(conn: sqlite3.Connection, scan_id: int) -> str:
    row = conn.execute(
        "SELECT classification FROM detections WHERE scan_id=? ORDER BY detection_id DESC LIMIT 1",
        (scan_id,)).fetchone()
    return row["classification"] if row else "no_guidance_detected"


_POSITIVE = ("red_box_guidance", "ambiguous")


def _change_type(prev_class: str, new_class: str) -> str | None:
    prev_pos = prev_class in _POSITIVE
    new_pos = new_class in _POSITIVE
    if prev_pos and not new_pos:
        return "take_down"      # box came down — the key publishable signal (spec §3.2)
    if new_pos and not prev_pos:
        return "put_up"
    if prev_pos and new_pos:
        return "modified"
    return None                  # negative -> negative: not an event


def _record_change(conn, *, candidate_id, url, prev_scan_id, new_scan_id,
                   prev_class, new_class, when) -> bool:
    ctype = _change_type(prev_class, new_class)
    if not ctype:
        return False
    conn.execute(
        """INSERT INTO change_events (candidate_id, url, event_type, prev_scan_id,
               new_scan_id, prev_classification, new_classification, detected_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (candidate_id, url, ctype, prev_scan_id, new_scan_id, prev_class, new_class, when))
    # Committed by scan_candidate as part of the per-page batch (see _insert_scan).
    return True


def scan_candidate(
    conn: sqlite3.Connection,
    candidate: dict,
    *,
    crawler: Crawler,
    classifier: Classifier,
    archiver: Archiver,
    require_verified: bool = False,
) -> ScanOutcome:
    cid = candidate["candidate_id"]
    out = ScanOutcome(candidate_id=cid)

    url = candidate.get("website_url")
    if not url:
        out.skipped_reason = "no resolved website_url"
        return out
    if require_verified and not candidate.get("url_verified"):
        # Strict mode only (require_verified_url): refuse a non-verified URL and
        # route it to review first (spec §3.1).
        out.skipped_reason = "url_verified is false — route to review before scanning"
        return out

    # Template aliasing: many sites serve the SAME body under several URLs
    # (/media, /media-kit, /press catch-all routes). Classify each distinct
    # body once per scan and reuse the verdict for its aliases — one LLM call
    # and ONE reviewable detection per real page, not one per URL. Keyed by
    # text_hash, populated only from pages actually classified (never from
    # prefiltered pages, so the "media paths always classify" recall rule
    # can't be short-circuited by a body first seen on a boilerplate URL).
    verdict_by_hash: dict[str, str] = {}

    for res in crawler.crawl_site(url):
        # Look at the previous scan of this URL *before* inserting the new one,
        # so we can diff for put-up/take-down (spec §3.2).
        prev = _prev_scan(conn, cid, res.url)
        unchanged = bool(prev and prev["text_hash"] == res.text_hash)

        # --- All slow work (LLM + archive disk/Wayback) happens here, with NO
        # open write transaction, so a worker never holds the single SQLite write
        # lock during a network call. The DB writes are batched into one short
        # transaction per page at the bottom of the loop. ---
        cl = None              # classification result, if the page was classified
        archive_rec = None     # archived evidence (disk written) awaiting its DB row
        new_class = "no_guidance_detected"
        prefiltered = deduped = False
        if not unchanged:
            # No usable content: blank text, a failed fetch (status 0/None), or an
            # HTTP error. Treated as "no guidance" for diffing (so a box that 404s
            # reads as a take-down) without an LLM call.
            empty = (not res.classifier_text.strip()
                     or not res.status or res.status >= 400)
            # Cheap pre-filter: skip the LLM on obviously-empty boilerplate pages
            # (donate/privacy/...) with no red-box signal. Media pages and PDFs
            # always classify. A skipped page is treated as no-guidance for diffing.
            prefiltered = (not empty and not prefilter.decide(
                res.url, res.classifier_text, res.content_type).scan)
            if not (empty or prefiltered):
                dup_class = verdict_by_hash.get(res.text_hash)
                if dup_class is not None:
                    # Alias of a body already classified this scan: reuse the
                    # verdict for change-diffing; no LLM call, no duplicate
                    # detection/archive (evidence exists under the first URL).
                    deduped = True
                    new_class = dup_class
                else:
                    try:
                        cl = classifier.classify_text(res.classifier_text)
                    except Exception as e:
                        # Classification failed (after the client's own retries). Persist
                        # NOTHING for this page so it's retried on the next scan rather
                        # than hash-skipped forever, and continue — one page must not
                        # abort the whole candidate's scan.
                        print(f"  warning: classification failed for {res.url} ({e}); "
                              f"will retry on next scan")
                        continue
                    new_class = cl.classification
                    verdict_by_hash[res.text_hash] = new_class
                    if cl.routes_to_review:
                        # Write screenshot/HTML to disk (+ optional Wayback) now, OUTSIDE
                        # the write transaction opened below.
                        archive_rec = archiver.archive(res, candidate_id=cid)

        # --- One short write transaction per page: scan + detection + archive
        # row + change event, then a single commit. ---
        scan_id = _insert_scan(conn, cid, res)
        out.pages_scanned += 1
        if prefiltered:
            out.prefiltered += 1
        if deduped:
            out.deduped += 1
        if cl is not None:
            cur = conn.execute(
                """INSERT INTO detections (scan_id, candidate_id, classification,
                       confidence, evidence, rationale, model, escalated, classified_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (scan_id, cid, cl.classification, cl.confidence,
                 json.dumps(cl.evidence), cl.rationale, cl.model,
                 int(cl.escalated), cl.classified_at))
            detection_id = cur.lastrowid
            out.detections += 1
            if cl.routes_to_review:
                if cl.classification == "red_box_guidance":
                    out.positives += 1
                else:
                    out.ambiguous += 1
                if archive_rec is not None:
                    Archiver.persist(conn, archive_rec, candidate_id=cid,
                                     scan_id=scan_id, detection_id=detection_id,
                                     commit=False)
                    out.archived += 1

        # Change-diff vs the previous scan of this URL (skipped when unchanged).
        if prev is not None and not unchanged:
            prev_class = _classification_for_scan(conn, prev["scan_id"])
            if _record_change(conn, candidate_id=cid, url=res.url,
                               prev_scan_id=prev["scan_id"], new_scan_id=scan_id,
                               prev_class=prev_class, new_class=new_class,
                               when=res.fetched_at):
                out.changes += 1
                ctype = _change_type(prev_class, new_class)
                if ctype == "take_down":
                    out.take_downs += 1
                elif ctype == "put_up":
                    out.put_ups += 1
        conn.commit()   # one write-lock acquisition + fsync per page

    # If nothing was fetched, distinguish "robots.txt blocked us" from "site was
    # empty/unreachable" so the gap is honestly surfaced (not a clean negative).
    if out.pages_scanned == 0:
        try:
            allowed, _ = crawler.robots.can_fetch(url.rstrip("/") + "/")
            if not allowed:
                out.robots_blocked = True
                out.skipped_reason = "robots.txt disallowed our crawler"
        except Exception:
            pass
    # Always record a scan disposition on the candidate so a zero-page outcome is
    # never silently indistinguishable from "never scanned":
    #   scanned        -> at least one page fetched
    #   robots_blocked -> robots.txt disallowed our crawler
    #   fetch_failed   -> site unreachable / empty (DNS, timeout, refused, all 4xx)
    # This is also what scan-all reads to skip already-attempted candidates on a
    # restart (a fetch_failed can be retried with `scan-all --rescan`).
    if out.robots_blocked:
        status = "robots_blocked"
    elif out.pages_scanned:
        status = "scanned"
    else:
        status = "fetch_failed"
        out.skipped_reason = out.skipped_reason or "site unreachable or returned no pages"
    conn.execute("UPDATE candidates SET scan_status=? WHERE candidate_id=?",
                 (status, cid))
    conn.commit()
    return out
