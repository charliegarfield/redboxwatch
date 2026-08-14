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
from time import monotonic as _monotonic

from . import prefilter
from .archiver import Archiver
from .classifier import Classifier
from .crawler import Crawler, FetchResult
from .util import now_iso


@dataclass
class ScanOutcome:
    candidate_id: str
    pages_scanned: int = 0        # usable pages (HTTP 200 with real content)
    pages_failed: int = 0         # fetched but unusable: error status / empty / bot
                                  # challenge (incl. never-before-seen 404s, which
                                  # are counted here but persist no scans row)
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
    timed_out: bool = False        # candidate wall-clock ceiling hit (partial scan)
    skipped_reason: str | None = None


def _insert_scan(conn: sqlite3.Connection, candidate_id: str, r: FetchResult) -> int:
    # We deliberately do NOT store raw_html here. Full markup is ~50x the size of
    # the extracted text and was never read back; for any page worth preserving,
    # the archiver writes the full HTML to disk (spec §3.4). The DB audit trail is
    # raw_text (what the classifier actually saw) + text_hash (change detection).
    cur = conn.execute(
        """INSERT INTO scans (candidate_id, url, fetched_at, http_status,
               content_type, render_mode, raw_text, text_hash,
               discovered_via, robots_posture)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (candidate_id, r.url, r.fetched_at, r.status, r.content_type,
         r.render_mode, r.classifier_text, r.text_hash,
         r.discovered_via, r.robots_posture),
    )
    # No commit here: scan_candidate batches all of a page's writes (scan +
    # detection + archive + change) into one commit so the SQLite write lock is
    # taken once per page, not 3-4 times. See scan_candidate for the ordering
    # that keeps the (slow) network/LLM work OUT of the write transaction.
    return cur.lastrowid


def _url_variants(url: str) -> list[str]:
    """URL forms the crawler treats as the same page (its _canonical dedup):
    with/without trailing slash, 'www.', and either scheme. Prior scans may be
    stored under any of them — sitemap order and redirects pick the form — and
    an exact-match lookup silently dropped the diff baseline (and with it
    take-down events) whenever the form drifted between scans."""
    from urllib.parse import urlparse, urlunparse
    p = urlparse(url)
    hosts = {p.netloc}
    hosts.add(p.netloc[4:] if p.netloc.startswith("www.") else "www." + p.netloc)
    stripped = p.path.rstrip("/")
    paths = {stripped, stripped + "/"}   # '' and '/' are both root forms
    return [urlunparse((scheme, h, pa, p.params, p.query, ""))
            for scheme in ("https", "http") for h in sorted(hosts)
            for pa in sorted(paths)]


def _canon_url(url: str) -> str:
    """Canonical page key (mirrors Crawler._canonical): host without 'www.',
    no scheme, no trailing slash."""
    from urllib.parse import urlparse
    p = urlparse(url)
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = p.path.rstrip("/") or "/"
    return f"{host}{path}" + (f"?{p.query}" if p.query else "")


def _prev_scan(conn: sqlite3.Connection, candidate_id: str, url: str):
    """Most recent prior scan of this page (any URL form), pre-insert."""
    forms = _url_variants(url)
    return conn.execute(
        f"""SELECT scan_id, text_hash, http_status FROM scans
            WHERE candidate_id=? AND url IN ({','.join('?' * len(forms))})
            ORDER BY scan_id DESC LIMIT 1""",
        (candidate_id, *forms)).fetchone()


def _baseline_state(conn: sqlite3.Connection, candidate_id: str, url: str):
    """Last *concluded* state of a URL: ``(ref_scan_id, classification, text_hash)``.

    The reference is the most recent usable scan — error/challenge fetches
    carry no information — superseded by any change event recorded after it (a
    confirmed-disappearance take_down has no usable scan of its own, so the
    event is the newer word on the URL's state). ``None`` when the URL has
    never been usably seen: there is nothing to diff a first sighting against.
    """
    forms = _url_variants(url)
    marks = ",".join("?" * len(forms))
    u = conn.execute(
        f"""SELECT scan_id, text_hash FROM scans s
            WHERE candidate_id=? AND url IN ({marks}) AND {usable_scan_sql('s')}
            ORDER BY scan_id DESC LIMIT 1""",
        (candidate_id, *forms)).fetchone()
    ev = conn.execute(
        f"""SELECT new_scan_id, new_classification FROM change_events
            WHERE candidate_id=? AND url IN ({marks})
            ORDER BY change_id DESC LIMIT 1""",
        (candidate_id, *forms)).fetchone()
    if u is None and ev is None:
        return None
    if u is None or (ev and ev["new_scan_id"] >= u["scan_id"]):
        return ev["new_scan_id"], ev["new_classification"], None
    return u["scan_id"], _classification_for_scan(conn, u["scan_id"]), u["text_hash"]


def _classification_for_scan(conn: sqlite3.Connection, scan_id: int) -> str:
    """Last known classification for the page content this scan saw.

    Resolved through the scan's text_hash, not its scan_id: hash-unchanged
    re-scans and template-alias pages deliberately carry no detection row of
    their own, so a scan_id-only lookup would read them as no-guidance — and a
    red box removed after a quiet re-scan would diff none->none, silently
    dropping the take-down event. Empty scans have no detection with a
    matching hash and correctly read as no_guidance_detected; prefiltered
    scans read the same, because a page is only prefiltered when its body was
    not classified this run (aliases of a classified body reuse its verdict
    before the prefilter looks) and its baseline is not positive (those
    always get a real model verdict).
    """
    row = conn.execute(
        """SELECT d.classification FROM detections d
           JOIN scans s ON s.scan_id = d.scan_id
           JOIN scans ref ON ref.scan_id = ?
           WHERE s.candidate_id = ref.candidate_id AND s.text_hash = ref.text_hash
           ORDER BY d.detection_id DESC LIMIT 1""",
        (scan_id,)).fetchone()
    return row["classification"] if row else "no_guidance_detected"


_POSITIVE = ("red_box_guidance", "ambiguous")

# Statuses meaning the page itself is absent — as opposed to a server-side
# block (403/429/5xx) or challenge, which says nothing about the page.
_GONE_STATUSES = (404, 410)

# Bot-challenge shells (Cloudflare & co.) render as a short interstitial,
# sometimes with a 2xx status. Only texts this short are marker-checked, so a
# real page that merely mentions a captcha can't be misread as a challenge.
_CHALLENGE_MAX_CHARS = 400
_CHALLENGE_MARKERS = (
    "just a moment", "checking your browser", "verifying you are human",
    "enable javascript and cookies", "captcha", "attention required",
)


def _is_challenge_text(text: str | None) -> bool:
    if not text or len(text) >= _CHALLENGE_MAX_CHARS:
        return False
    low = text.lower()
    return any(m in low for m in _CHALLENGE_MARKERS)


def usable_status(status: int | None) -> bool:
    """Whether the response body is the page's OWN content: HTTP 200 only.

    One predicate feeds BOTH the classify gate and :func:`usable_scan` so they
    cannot disagree: every page the classifier is paid to read can also anchor
    a diff baseline and count as coverage. 202 (Accepted — in the wild almost
    always a bot-challenge/queue shell), 204 (no content) and 3xx (a
    redirect's stub body) are not page content; classifying them bought
    detections that were reviewable but invisible to the diff.
    """
    return status == 200


def usable_scan(status: int | None, text: str | None) -> bool:
    """Whether a fetch says anything about what the page contains.

    Error/interstitial statuses, empty bodies, and bot-challenge shells carry
    no information: diffing them against real content manufactures put_up /
    take_down events out of outages (a 403 bot-block is not the campaign
    taking its red box down). Only usable scans move a URL's recorded state.
    Status-wise this is exactly :func:`usable_status` — the same range the
    classify gate uses.
    """
    stripped = (text or "").strip()
    return (usable_status(status) and bool(stripped)
            and not _is_challenge_text(stripped))


def usable_scan_sql(alias: str) -> str:
    """SQL predicate mirroring :func:`usable_scan` for a ``scans`` alias.

    The ``= 200`` literal mirrors :func:`usable_status`; keep them in sync.
    """
    t = f"TRIM(COALESCE({alias}.raw_text,''))"
    markers = " OR ".join(
        f"LOWER({t}) LIKE '%{m}%'" for m in _CHALLENGE_MARKERS)
    return (f"({alias}.http_status = 200 AND {t} != '' "
            f"AND NOT (LENGTH({t}) < {_CHALLENGE_MAX_CHARS} AND ({markers})))")


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
                   prev_class, new_class, when) -> str | None:
    ctype = _change_type(prev_class, new_class)
    if not ctype:
        return None
    conn.execute(
        """INSERT INTO change_events (candidate_id, url, event_type, prev_scan_id,
               new_scan_id, prev_classification, new_classification, detected_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (candidate_id, url, ctype, prev_scan_id, new_scan_id, prev_class, new_class, when))
    # Committed by scan_candidate as part of the per-page batch (see _insert_scan).
    return ctype


def backfill_change_events(conn: sqlite3.Connection, *, apply: bool = False) -> dict:
    """Reconcile ``change_events`` against a replay of the full scan history.

    Every input survives in the DB — scans keep text_hash/status/fetched_at,
    detections the verdicts — so this replays each (candidate, URL)'s scans in
    order under the current diff rules' *observable effects* (usable-scan
    baselines, hash resolution, confirmed disappearance) and returns:

    - ``missing``:  events the live pipeline would have recorded but didn't
      (e.g. take-downs the pre-2026-07-25 logic dropped after a quiet re-scan);
    - ``spurious``: recorded events the rules would NOT produce (e.g. a 403
      bot-block or HTTP-500 outage diffed as a take_down/put_up by the
      pre-2026-07-28 logic — the crawler being blocked is not a page change);
    - ``mismatched``: events recorded at the right (candidate, URL, revealing
      scan) but with the WRONG content — event_type or either classification
      disagrees with the replay. (The keys-only comparison used to wave these
      through untouched.)

    Note the replay does not re-run the prefilter/dedup machinery — it reads
    their *outputs*: pages the pipeline skipped (prefiltered, hash-unchanged,
    template aliases) carry no detection row of their own, and their
    classification resolves through the candidate's latest verdict for the
    same text_hash — the same resolution ``_classification_for_scan`` uses
    live. The 2026-07 alias-before-prefilter reordering only changes WHICH
    scan carries a body's detection row, an input this replay consumes as
    recorded data, so the two stay in agreement.

    Existing events are keyed by (candidate, URL, revealing scan). Writes —
    inserting missing, deleting spurious, correcting mismatched — happen only
    with ``apply``.
    """
    existing: dict[tuple, dict] = {
        (r["candidate_id"], r["url"], r["new_scan_id"]): dict(r)
        for r in conn.execute("SELECT * FROM change_events")}
    det_by_scan = {r["scan_id"]: r["classification"] for r in conn.execute(
        """SELECT scan_id, classification FROM (
              SELECT scan_id, classification, ROW_NUMBER() OVER (
                  PARTITION BY scan_id ORDER BY detection_id DESC) rn
              FROM detections) WHERE rn = 1""")}
    # Replay in global scan order so hash-resolution sees exactly what the
    # pipeline would have seen "as of" each scan (aliases included). raw_text
    # is only pulled for short bodies (the challenge-marker check needs it);
    # long bodies can't be challenge shells.
    last_by_hash: dict[tuple[str, str], str] = {}
    state: dict[tuple[str, str], dict] = {}
    expected: dict[tuple, dict] = {}
    for s in conn.execute(
        f"""SELECT scan_id, candidate_id, url, fetched_at, text_hash, http_status,
                   LENGTH(TRIM(COALESCE(raw_text,''))) AS text_len,
                   CASE WHEN LENGTH(TRIM(COALESCE(raw_text,''))) < {_CHALLENGE_MAX_CHARS}
                        THEN raw_text END AS short_text
            FROM scans ORDER BY scan_id"""):
        cand, url, h = s["candidate_id"], s["url"], s["text_hash"]
        # Keyed canonically: sitemap order / redirects drift the stored URL
        # form between scans, and per-form state would re-split the history
        # the live pipeline now diffs as one page.
        st = state.setdefault((cand, _canon_url(url)), {"prev_status": None,
                                                        "base": None})
        usable = (s["http_status"] == 200 and s["text_len"] > 0
                  and not _is_challenge_text(s["short_text"]))
        ev = None
        base = st["base"]
        if usable:
            cls = (det_by_scan.get(s["scan_id"])
                   or last_by_hash.get((cand, h), "no_guidance_detected"))
            if base is not None and base["hash"] != h:
                ctype = _change_type(base["class"], cls)
                if ctype:
                    ev = (ctype, base["ref"], base["class"], cls)
            st["base"] = {"ref": s["scan_id"], "class": cls, "hash": h}
        elif (s["http_status"] in _GONE_STATUSES
              and st["prev_status"] in _GONE_STATUSES
              and base is not None and base["class"] in _POSITIVE):
            ev = ("take_down", base["ref"], base["class"], "no_guidance_detected")
            st["base"] = {"ref": s["scan_id"], "class": "no_guidance_detected",
                          "hash": None}
        if s["scan_id"] in det_by_scan:
            last_by_hash[(cand, h)] = det_by_scan[s["scan_id"]]
        st["prev_status"] = s["http_status"]
        if ev:
            expected[(cand, url, s["scan_id"])] = dict(
                candidate_id=cand, url=url, event_type=ev[0],
                prev_scan_id=ev[1], new_scan_id=s["scan_id"],
                prev_classification=ev[2], new_classification=ev[3],
                detected_at=s["fetched_at"])
    missing = [v for k, v in expected.items() if k not in existing]
    spurious = [v for k, v in existing.items() if k not in expected]
    # Same key, wrong content: the event exists where the replay expects one,
    # but records a different transition (e.g. a put_up where the rules say
    # take_down, or classifications from a placeholder verdict).
    mismatched = []
    for k, exp in expected.items():
        got = existing.get(k)
        if got is None:
            continue
        if (got["event_type"] != exp["event_type"]
                or got["prev_classification"] != exp["prev_classification"]
                or got["new_classification"] != exp["new_classification"]):
            mismatched.append({**exp, "change_id": got["change_id"],
                               "recorded_event_type": got["event_type"]})
    if apply:
        if missing:
            conn.executemany(
                """INSERT INTO change_events (candidate_id, url, event_type,
                       prev_scan_id, new_scan_id, prev_classification,
                       new_classification, detected_at)
                   VALUES (:candidate_id, :url, :event_type, :prev_scan_id,
                           :new_scan_id, :prev_classification, :new_classification,
                           :detected_at)""", missing)
        if spurious:
            conn.executemany("DELETE FROM change_events WHERE change_id = ?",
                             [(r["change_id"],) for r in spurious])
        if mismatched:
            conn.executemany(
                """UPDATE change_events SET event_type = :event_type,
                       prev_scan_id = :prev_scan_id,
                       prev_classification = :prev_classification,
                       new_classification = :new_classification,
                       detected_at = :detected_at
                   WHERE change_id = :change_id""", mismatched)
        if missing or spurious or mismatched:
            conn.commit()
    return {"missing": missing, "spurious": spurious, "mismatched": mismatched}


def scan_candidate(
    conn: sqlite3.Connection,
    candidate: dict,
    *,
    crawler: Crawler,
    classifier: Classifier,
    archiver: Archiver,
    require_verified: bool = False,
    deadline_seconds: float | None = None,
) -> ScanOutcome:
    cid = candidate["candidate_id"]
    out = ScanOutcome(candidate_id=cid)
    _started = _monotonic()

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
    # can't be short-circuited by a body first seen on a boilerplate URL) and
    # consulted BEFORE the prefilter, so a body already classified keeps its
    # verdict on every alias regardless of URL class — a positive body seen
    # on /media must not be re-read as no-guidance on a /donate alias.
    verdict_by_hash: dict[str, str] = {}

    for res in crawler.crawl_site(url):
        # Wall-clock ceiling (checked between pages, so every page's writes stay
        # complete): a site that is merely SLOW across many pages must not pin a
        # worker for hours. Pages already scanned are committed; the candidate
        # finishes as a normal partial 'scanned' and re-enters on its next
        # cadence day. Hangs *inside* one fetch are the per-fetch guards' job.
        if deadline_seconds and _monotonic() - _started > deadline_seconds:
            out.timed_out = True
            out.skipped_reason = (f"wall-clock ceiling {deadline_seconds:.0f}s hit "
                                  f"after {out.pages_scanned} pages — rest of site skipped")
            break
        # Look at the previous scan of this URL *before* inserting the new one,
        # so we can diff for put-up/take-down (spec §3.2).
        prev = _prev_scan(conn, cid, res.url)
        # Phantom links: news templates and probed common paths 404 on every
        # scan of every site, forever. A 404/410 for a URL NEVER seen before
        # carries no information and anchors nothing — the confirmed-
        # disappearance rule needs a prior scan row to confirm against — so no
        # scans row is written (the fetch still counts as a failed page in the
        # attempt bookkeeping below). A 404 on a URL WITH a prior scan IS
        # recorded: it's the evidence trail for the two-consecutive-404
        # take_down.
        if prev is None and res.status in _GONE_STATUSES:
            out.pages_failed += 1
            continue
        unchanged = bool(prev and prev["text_hash"] == res.text_hash)
        cur_usable = usable_scan(res.status, res.classifier_text)
        # Baseline for diffing. Needed even when hash-unchanged for the
        # confirmed-disappearance check: consecutive 404s serve the same
        # (empty) body, so the confirming scan is always hash-unchanged.
        need_base = ((cur_usable and not unchanged)
                     or (not cur_usable and res.status in _GONE_STATUSES))
        base = _baseline_state(conn, cid, res.url) if need_base else None

        # --- All slow work (LLM + archive disk/Wayback) happens here, with NO
        # open write transaction, so a worker never holds the single SQLite write
        # lock during a network call. The DB writes are batched into one short
        # transaction per page at the bottom of the loop. ---
        cl = None              # classification result, if the page was classified
        archive_rec = None     # archived evidence (disk written) awaiting its DB row
        new_class = "no_guidance_detected"
        prefiltered = deduped = False
        if not unchanged:
            # No classifiable content: blank text, a failed fetch (status
            # 0/None), or any non-200 status (4xx/5xx errors, 202 challenge/
            # queue shells, 204, 3xx stubs). Skipped without an LLM call —
            # same status range as usable_scan, so a page the classifier
            # reads is always one that can anchor a baseline. (Diffing
            # ignores non-usable scans entirely — see the change-diff block.)
            empty = (not res.classifier_text.strip()
                     or not usable_status(res.status))
            # Alias dedup comes FIRST: a body already classified this scan
            # keeps its verdict under every URL it appears on. Checked before
            # the prefilter so a positive classified on /media can't be
            # re-read as no-guidance on a boilerplate alias like /donate
            # (a self-contradictory scan row and a false take_down).
            dup_class = None if empty else verdict_by_hash.get(res.text_hash)
            if dup_class is not None:
                # Reuse the verdict for change-diffing; no LLM call, no
                # duplicate detection/archive (evidence exists under the
                # first URL).
                deduped = True
                new_class = dup_class
            elif not empty:
                # Cheap pre-filter: skip the LLM on obviously-empty boilerplate
                # pages (donate/privacy/...) with no red-box signal. Media pages
                # and PDFs always classify. A skipped page is treated as
                # no-guidance for diffing — so a page whose last concluded state
                # is positive is NEVER prefiltered: the prefilter's contract is
                # that it decides nothing positive-related, and a take_down may
                # only be concluded by a real model verdict.
                baseline_positive = base is not None and base[1] in _POSITIVE
                prefiltered = (not baseline_positive and not prefilter.decide(
                    res.url, res.classifier_text, res.content_type).scan)
                if not prefiltered:
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
        if cur_usable:
            out.pages_scanned += 1
        else:
            # Recorded for the audit trail but not counted as coverage: a site
            # whose every fetch 404s or bot-challenges must not publish as a
            # dated clean negative (it gets 'fetch_failed' below).
            out.pages_failed += 1
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

        # Change-diff vs the URL's last concluded state. Only a usable scan can
        # move that state; a page that starts 403ing/challenging is the crawler
        # being blocked, not the campaign taking its box down. The one
        # exception: a page absent (404/410) on two consecutive scans is a
        # confirmed disappearance and records the take_down.
        ctype = None
        if base is not None:
            base_ref, base_class, base_hash = base
            if cur_usable and base_hash != res.text_hash:
                ctype = _record_change(
                    conn, candidate_id=cid, url=res.url,
                    prev_scan_id=base_ref, new_scan_id=scan_id,
                    prev_class=base_class, new_class=new_class,
                    when=res.fetched_at)
            elif (not cur_usable
                  and prev is not None and prev["http_status"] in _GONE_STATUSES
                  and base_class in _POSITIVE):
                # res.status is in _GONE_STATUSES too (that's what set
                # need_base) — second consecutive absence confirms removal.
                ctype = _record_change(
                    conn, candidate_id=cid, url=res.url,
                    prev_scan_id=base_ref, new_scan_id=scan_id,
                    prev_class=base_class, new_class="no_guidance_detected",
                    when=res.fetched_at)
        if ctype:
            out.changes += 1
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
    #   scanned        -> at least one USABLE page fetched (HTTP 200, real content)
    #   robots_blocked -> robots.txt disallowed our crawler
    #   fetch_failed   -> site unreachable, empty, or nothing usable (DNS,
    #                     timeout, refused, all-4xx/5xx, bot challenges)
    # This is also what scan-all reads to skip already-attempted candidates on a
    # restart (a fetch_failed can be retried with `scan-all --rescan`).
    if out.robots_blocked:
        status = "robots_blocked"
    elif out.pages_scanned:
        status = "scanned"
    else:
        status = "fetch_failed"
        out.skipped_reason = out.skipped_reason or (
            f"no usable pages ({out.pages_failed} failed fetches)"
            if out.pages_failed else "site unreachable or returned no pages")
    # last_attempt_at is stamped on EVERY concluded attempt — including the
    # zero-page outcomes above, which write no `scans` rows. Without it those
    # candidates have no timestamp anywhere and the scheduler sees them as
    # "never scanned", re-crawling robots-blocked/unreachable sites every day.
    # last_scan_partial records whether the wall-clock ceiling truncated this
    # attempt (1) or it ran to completion (0): a truncated partial finishes as
    # a normal 'scanned', so without the flag an operator can't distinguish it
    # from a complete sweep of the site.
    conn.execute(
        "UPDATE candidates SET scan_status=?, last_attempt_at=?, "
        "last_scan_partial=? WHERE candidate_id=?",
        (status, now_iso(), int(out.timed_out), cid))
    conn.commit()
    return out
