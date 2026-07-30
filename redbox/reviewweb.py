"""Local web console for the human review gate (spec §3.7) — phase 5.

A small, dependency-free WSGI app for working the review queue in a browser
instead of the `review` CLI. Serve it with:

    python -m redbox review-web            # http://127.0.0.1:8001/

What it shows and does:
- ``GET /`` — the queue: pending positive/ambiguous detections, template
  aliases (same page body under several URLs) collapsed to one reviewable
  finding, plus the most recent decisions (so a mis-click can be caught and
  re-reviewed — reviews are append-only history; the latest one wins, exactly
  as the publisher interprets them).
- ``GET /detection/<id>`` — everything a reviewer needs on one page: the
  §3.7a label, quoted evidence spans, classifier rationale, the archived
  screenshot / raw HTML / extracted text, Wayback link, IE corroboration,
  an unverified-attribution warning when the URL was auto-resolved, and the
  approve / reject / needs-more form.
- ``POST /detection/<id>`` — record the decision (optionally for all template
  aliases at once, like ``review --group``) and jump to the next pending
  finding.
Keyboard triage: on the queue, ``j``/``k`` move the selection and ``Enter``
(or ``o``) opens it; on a detection, ``a``/``r``/``n`` pick
approve / reject / needs-more, ``g`` toggles the alias-group checkbox,
``Enter`` submits, and ``q`` returns to the queue. Shortcuts are inert while
typing in a form field, and every key is hinted inline with a ``<kbd>`` chip.

- ``GET /evidence/<archive_id>/<kind>`` — serve archived evidence from disk.
  Paths come only from the ``archives`` table (never from the request), and
  archived HTML is deliberately served as ``text/plain`` so a captured page's
  scripts can never execute in the reviewer's browser.
- ``GET /urls`` — the website triage queue: every candidate the resolution
  chain came up empty for, richest first. ``GET /urls/<candidate_id>`` is the
  per-candidate form (research links + paste box); ``POST`` either records a
  human-found URL — written to the DB as ``manual``/verified AND appended to
  ``data/websites.json`` so a re-resolve or fresh DB keeps it — or marks
  the candidate ``human_none`` (checked by a human, no site exists), which
  removes them from the queue without inventing a URL, or marks the record
  ``inactive=2`` ("not running for this seat": a stale FEC record, e.g. a
  House record for someone actually running for Senate — excluded from
  resolve/scan/publish). Newly URL'd candidates have no ``scan_status``, so
  the next ``scan-all`` picks them up.

Pending here means the *latest* review action is absent or ``needs_more``
(window-function query, same convention as the publisher) — so a detection
that was marked needs-more and later approved does not linger in the queue.

The console is a review tool, not a publisher: approving only records a
review row. The public site is still built explicitly with ``publish``.
Bind is 127.0.0.1 only — pending detections are unpublished allegations and
must not be exposed off-machine.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import threading
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, quote, quote_plus, unquote, urlsplit

from .db import connect, init_db
from .publisher import AMBIGUOUS_LABEL, CSS, POSITIVE_LABEL, _FONTS, _h
from .util import now_iso
from .website import OVERRIDES_PATH

REVIEW_ACTIONS = ("approve", "reject", "needs_more")

_ACTION_VERB = {
    "approve": "approved — will publish on the next site build",
    "reject": "rejected — recorded, never published",
    "needs_more": "marked needs-more — stays in the queue",
}

_CLASS_PILL = {
    "red_box_guidance": ("RED BOX", "pill-pending"),
    "ambiguous": ("AMBIGUOUS", "pill-amb"),
}

# ---------------------------------------------------------------------------
# Data access (plain functions over the shared SQLite schema, so they are
# testable without going through WSGI).

# Latest-review-wins pending set. The CLI's LEFT JOIN keeps a detection pending
# if ANY of its reviews is needs_more; here the newest review decides, matching
# how the publisher classifies the same detection.
_PENDING_SQL = """
WITH latest AS (
  SELECT detection_id, action FROM (
    SELECT detection_id, action, ROW_NUMBER() OVER (
        PARTITION BY detection_id ORDER BY reviewed_at DESC, review_id DESC) rn
    FROM reviews) WHERE rn = 1)
SELECT d.detection_id, d.candidate_id, d.classification, d.confidence,
       c.name, c.state, c.district, c.office, c.party, c.url_verified,
       s.url, s.text_hash
FROM detections d
JOIN candidates c USING(candidate_id)
JOIN scans s USING(scan_id)
LEFT JOIN latest r ON r.detection_id = d.detection_id
WHERE d.classification IN ('red_box_guidance','ambiguous')
  AND (r.action IS NULL OR r.action = 'needs_more')
ORDER BY d.confidence DESC, d.detection_id
"""

# All of a detection's template-alias siblings (identical page body for the
# same candidate), itself included — the same grouping `review --group` uses.
_SIBLINGS_SQL = """
SELECT d2.detection_id, s2.url FROM detections d
JOIN scans s USING(scan_id)
JOIN scans s2 ON s2.candidate_id = s.candidate_id AND s2.text_hash = s.text_hash
JOIN detections d2 ON d2.scan_id = s2.scan_id
WHERE d.detection_id = ?
  AND d2.classification IN ('red_box_guidance','ambiguous')
"""

# Positive detections of the SAME page body under a DIFFERENT candidate — the
# multi-committee trap: one person's old and current committees both resolve to
# one site (e.g. a House committee and a leftover Senate committee), the scanner
# hits it once per committee, and the identical red box lands in the queue twice.
# The guidance concerns exactly one race, so exactly one attribution is right;
# without a flag a reviewer approves both and the ledger double-counts. Twins are
# reported whatever their review state — an already-approved twin is precisely
# the case where one of the two decisions needs revisiting.
_CROSS_COMMITTEE_SQL = """
WITH latest AS (
  SELECT detection_id, action FROM (
    SELECT detection_id, action, ROW_NUMBER() OVER (
        PARTITION BY detection_id ORDER BY reviewed_at DESC, review_id DESC) rn
    FROM reviews) WHERE rn = 1)
SELECT d2.detection_id, d2.candidate_id, c2.name, c2.office, c2.state,
       c2.district, c2.inactive, s2.url, r.action AS review_action
FROM detections d
JOIN scans s USING(scan_id)
JOIN scans s2 ON s2.text_hash = s.text_hash
             AND s2.candidate_id <> s.candidate_id
JOIN detections d2 ON d2.scan_id = s2.scan_id
JOIN candidates c2 ON c2.candidate_id = d2.candidate_id
LEFT JOIN latest r ON r.detection_id = d2.detection_id
WHERE d.detection_id = ?
  AND d2.classification IN ('red_box_guidance','ambiguous')
ORDER BY d2.candidate_id, d2.detection_id
"""


def cross_committee_twins(conn: sqlite3.Connection, detection_id: int) -> list[dict]:
    """Positive detections of this detection's page body attributed to another
    candidate record, in any review state. Non-empty means the same red box is
    (or was) claimed for more than one race and only one attribution can be
    right."""
    return [dict(r) for r in conn.execute(_CROSS_COMMITTEE_SQL, (detection_id,))]


# text_hash values whose positive detections span more than one candidate_id —
# the queue-level version of the twin check, one query for the whole list.
_MULTI_COMMITTEE_HASHES_SQL = """
SELECT s.text_hash FROM detections d JOIN scans s USING(scan_id)
WHERE d.classification IN ('red_box_guidance','ambiguous')
GROUP BY s.text_hash
HAVING COUNT(DISTINCT d.candidate_id) > 1
"""


def multi_committee_hashes(conn: sqlite3.Connection) -> set[str]:
    """All text_hashes with positive detections under 2+ candidates."""
    return {r["text_hash"] for r in conn.execute(_MULTI_COMMITTEE_HASHES_SQL)}


def pending_groups(conn: sqlite3.Connection) -> list[list[dict]]:
    """Pending detections grouped into findings: one group per distinct
    (candidate, page body), ordered by confidence descending."""
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for r in conn.execute(_PENDING_SQL):
        key = (r["candidate_id"], r["text_hash"])
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(dict(r))
    return [groups[k] for k in order]


def record_review(conn: sqlite3.Connection, detection_id: int, action: str, *,
                  reviewer: str | None = None, notes: str | None = None,
                  group: bool = False) -> list[int]:
    """Insert review row(s) for a detection (and its template aliases when
    ``group``), mirroring ``review --group``. Returns the detection ids reviewed."""
    if action not in REVIEW_ACTIONS:
        raise ValueError(f"action must be one of {REVIEW_ACTIONS}, got {action!r}")
    targets = [detection_id]
    if group:
        siblings = conn.execute(_SIBLINGS_SQL, (detection_id,)).fetchall()
        targets = sorted({r["detection_id"] for r in siblings} | {detection_id})
    ts = now_iso()
    conn.executemany(
        """INSERT INTO reviews (detection_id, reviewer, action, notes, reviewed_at)
           VALUES (?,?,?,?,?)""",
        [(d, reviewer or None, action, notes or None, ts) for d in targets])
    conn.commit()
    return targets


# Candidates the resolution chain found nothing for, richest first (the more a
# campaign has raised, the more likely a findable site exists). 'human_none'
# marks candidates a human already checked — they've left the queue.
_URL_QUEUE_SQL = """
SELECT candidate_id, name, office, state, district, party, receipts, universe_reason
FROM candidates
WHERE (website_url IS NULL OR website_url = '')
  AND COALESCE(url_source, '') != 'human_none'
  AND COALESCE(inactive, 0) = 0
ORDER BY receipts DESC, candidate_id
"""


def url_queue(conn: sqlite3.Connection) -> list[dict]:
    """Candidates with no resolved website that no human has triaged yet."""
    return [dict(r) for r in conn.execute(_URL_QUEUE_SQL)]


def normalize_url(raw: str) -> str | None:
    """A pasted URL, normalised (https:// default) — or None if unusable."""
    u = (raw or "").strip()
    if not u:
        return None
    if "://" not in u:
        u = "https://" + u
    parts = urlsplit(u)
    if parts.scheme not in ("http", "https") or "." not in parts.netloc:
        return None
    return u


# One lock for the read-modify-write on the overrides file: the console runs
# a THREADING server, and two concurrent triage POSTs interleaving the
# read/write lost the earlier reviewer's URL (last writer wins).
_OVERRIDES_LOCK = threading.Lock()


def record_found_url(conn: sqlite3.Connection, candidate_id: str, url: str, *,
                     reviewer: str | None = None,
                     overrides_path: Path = OVERRIDES_PATH) -> None:
    """A human found the candidate's site: store it as manual/VERIFIED and
    append it to the overrides file, the durable home for human-curated URLs
    (a re-resolve or a rebuilt DB re-reads it; DB-only edits would not survive
    that). The DB write makes the candidate scannable immediately.

    The file write is locked (threading server) and atomic (temp file +
    os.replace): a crash mid-write must never truncate the canonical
    human-curated store."""
    ts = now_iso()
    conn.execute(
        """UPDATE candidates SET website_url=?, url_source='manual',
               url_verified=1, updated_at=? WHERE candidate_id=?""",
        (url, ts, candidate_id))
    conn.commit()
    with _OVERRIDES_LOCK:
        overrides = (json.loads(overrides_path.read_text())
                     if overrides_path.exists() else {})
        overrides[candidate_id] = {
            "url": url, "verified": True,
            "note": f"found by {reviewer or 'reviewer'} via review console {ts[:10]}",
        }
        overrides_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=overrides_path.parent,
                                   prefix=overrides_path.name + ".")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(overrides, indent=2) + "\n")
            os.replace(tmp, overrides_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def record_no_site(conn: sqlite3.Connection, candidate_id: str) -> None:
    """A human looked and found no site: mark the candidate 'human_none' so the
    queue drops them. website_url stays NULL — nothing is invented, and the
    publisher keeps reporting them as a coverage gap."""
    conn.execute(
        """UPDATE candidates SET url_source='human_none', updated_at=?
           WHERE candidate_id=?""", (now_iso(), candidate_id))
    conn.commit()


def record_wrong_race(conn: sqlite3.Connection, candidate_id: str) -> None:
    """A human determined this FEC record isn't a real candidacy for this seat
    (e.g. a House record for someone actually running for Senate). inactive=2
    is the human value — `mark-inactive` refreshes FEC flags (inactive=1) but
    never touches human calls. The row leaves resolve/scan/publish entirely."""
    conn.execute(
        """UPDATE candidates SET inactive=2, updated_at=?
           WHERE candidate_id=?""", (now_iso(), candidate_id))
    conn.commit()


def detection_view(conn: sqlite3.Connection, detection_id: int) -> dict | None:
    """Everything the detail page needs, or None for an unknown detection."""
    det = conn.execute(
        """SELECT d.*, s.url AS page_url, s.raw_text, s.text_hash, s.fetched_at
           FROM detections d JOIN scans s USING(scan_id)
           WHERE d.detection_id = ?""", (detection_id,)).fetchone()
    if not det:
        return None
    det = dict(det)
    cand = dict(conn.execute(
        "SELECT * FROM candidates WHERE candidate_id = ?",
        (det["candidate_id"],)).fetchone())
    archive = conn.execute(
        """SELECT * FROM archives WHERE detection_id = ?
           ORDER BY archived_at DESC, archive_id DESC LIMIT 1""",
        (detection_id,)).fetchone()
    reviews = [dict(r) for r in conn.execute(
        """SELECT * FROM reviews WHERE detection_id = ?
           ORDER BY reviewed_at DESC, review_id DESC""", (detection_id,))]
    corr = conn.execute(
        """SELECT * FROM corroboration WHERE candidate_id = ?
           ORDER BY computed_at DESC, corroboration_id DESC LIMIT 1""",
        (det["candidate_id"],)).fetchone()
    siblings = [dict(r) for r in conn.execute(_SIBLINGS_SQL, (detection_id,))]
    try:
        evidence = json.loads(det.get("evidence") or "[]")
    except json.JSONDecodeError:
        evidence = []
    return {
        "detection": det,
        "candidate": cand,
        "archive": dict(archive) if archive else None,
        "reviews": reviews,
        "corroboration": dict(corr) if corr else None,
        "siblings": siblings,
        "cross_committee": cross_committee_twins(conn, detection_id),
        "evidence": evidence,
    }


def _next_pending(conn: sqlite3.Connection, exclude: set[int]) -> int | None:
    """The next finding to review (its representative detection id), skipping any
    group that contains an excluded id — a needs_more'd detection stays pending,
    and bouncing straight back to it would make the queue feel stuck."""
    for g in pending_groups(conn):
        if not any(d["detection_id"] in exclude for d in g):
            return g[0]["detection_id"]
    return None


# ---------------------------------------------------------------------------
# Rendering. Same visual language as the published site (publisher CSS is
# reused verbatim), so evidence reads identically here and there.

def _page(title: str, body: str) -> str:
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_h(title)} · Review Console · Red-Boxing Tracker</title>
{_FONTS}<link rel="stylesheet" href="/styles.css"></head><body>
<header class="site-head"><div class="wrap">
  <a class="brand" href="/">Red-Boxing&nbsp;Tracker · Review&nbsp;Console</a>
  <nav><a href="/">Queue</a> <a href="/urls">Site&nbsp;URLs</a></nav>
</div></header>
<main class="wrap">{body}</main>
<footer class="site-foot"><div class="wrap">
  <p>Approving records a review; it does not publish anything by itself — build the
  site with <code>python -m redbox publish</code>. Pending detections are
  <strong>unpublished allegations</strong>; this console binds to 127.0.0.1 only.</p>
</div></footer></body></html>"""


def _done_banner(qs: dict[str, list[str]]) -> str:
    """A one-shot confirmation note carried in the redirect query string."""
    try:
        did = int(qs.get("done", [""])[0])
        action = qs.get("as", [""])[0]
        n = int(qs.get("n", ["1"])[0])
    except ValueError:
        return ""
    if action not in REVIEW_ACTIONS:
        return ""
    extra = f" (+{n - 1} identical alias page(s))" if n > 1 else ""
    return (f'<div class="done-banner">Detection #{did} '
            f'{_h(_ACTION_VERB[action])}{extra}.</div>')


def _render_queue(conn: sqlite3.Connection, qs: dict[str, list[str]]) -> str:
    groups = pending_groups(conn)
    n_detections = sum(len(g) for g in groups)
    multi = multi_committee_hashes(conn)
    rows = []
    for g in groups:
        r = g[0]
        pill_txt, pill_cls = _CLASS_PILL.get(r["classification"], ("—", "pill-muted"))
        seat = f"{r['office'] or ''}-{r['state'] or ''}" + (f"-{r['district']}" if r["district"] else "")
        alias = f'<span class="alias-tag">+{len(g) - 1} alias</span>' if len(g) > 1 else ""
        twin = (' <span class="twin-tag">multi-committee</span>'
                if r["text_hash"] in multi else "")
        verified = "" if r["url_verified"] else ' <span class="unverified-tag">unverified URL</span>'
        conf = f"{r['confidence']:.2f}" if r["confidence"] is not None else "—"
        rows.append(f"""<tr>
          <td><a href="/detection/{r['detection_id']}">{_h(r['name'])}</a>{verified}{twin}</td>
          <td>{_h(seat)}</td>
          <td><span class="pill {pill_cls}">{pill_txt}</span></td>
          <td class="num">{conf}</td>
          <td class="qurl">{_h(r['url'])} {alias}</td>
        </tr>""")
    if groups:
        queue = f"""<table class="grid"><thead><tr>
          <th>Candidate</th><th>Seat</th><th>Class</th><th class="num">Conf.</th><th>Page</th>
        </tr></thead><tbody>{''.join(rows)}</tbody></table>
        <p class="kbd-hint">Keyboard: <kbd>j</kbd>/<kbd>k</kbd> select ·
          <kbd>Enter</kbd> open</p>
        <script>{QUEUE_JS}</script>"""
    else:
        queue = '<p class="empty">Queue clear — no detections pending review.</p>'

    decided_rows = []
    for r in conn.execute(
        """SELECT r.detection_id, r.action, r.reviewer, r.reviewed_at, c.name
           FROM reviews r JOIN detections d USING(detection_id)
           JOIN candidates c ON c.candidate_id = d.candidate_id
           ORDER BY r.reviewed_at DESC, r.review_id DESC LIMIT 20"""):
        decided_rows.append(
            f'<li><span class="chg-when">{_h(r["reviewed_at"][:10])}</span>'
            f'<strong>{_h(r["action"])}</strong> — '
            f'<a href="/detection/{r["detection_id"]}">#{r["detection_id"]}</a> '
            f'{_h(r["name"])}'
            + (f' <span class="qurl">by {_h(r["reviewer"])}</span>' if r["reviewer"] else "")
            + '</li>')
    decided = (f"""<section class="changes"><h2>Recent decisions</h2>
      <p class="srcline">Reviews are append-only history; re-review a detection to
      change its outcome (the latest decision wins).</p>
      <ul class="changelog">{''.join(decided_rows)}</ul></section>"""
               if decided_rows else "")

    body = f"""
    {_done_banner(qs)}
    <h1>Review queue</h1>
    <p class="summary">{len(groups)} finding(s) pending review
       ({n_detections} detection(s) incl. template aliases).</p>
    {queue}
    {decided}"""
    return _page("Queue", body)


def _render_detail(view: dict, *, reviewer_default: str, n_pending: int) -> str:
    d, c = view["detection"], view["candidate"]
    label = POSITIVE_LABEL if d["classification"] == "red_box_guidance" else AMBIGUOUS_LABEL
    pill_txt, pill_cls = _CLASS_PILL.get(d["classification"], ("—", "pill-muted"))
    seat = f"{c.get('office') or ''}-{c.get('state') or ''}" + (
        f"-{c.get('district')}" if c.get("district") else "")

    latest = view["reviews"][0] if view["reviews"] else None
    if latest and latest["action"] in ("approve", "reject"):
        decided = (f'<div class="review-banner">Already decided: '
                   f'<strong>{_h(latest["action"])}</strong> by '
                   f'{_h(latest["reviewer"] or "(unnamed)")} on '
                   f'{_h(latest["reviewed_at"][:10])}. Submitting below records a '
                   f'new decision — the latest one wins.</div>')
    else:
        decided = ('<div class="review-banner">This detection is <strong>pending '
                   'human review and is not a published finding.</strong></div>')

    warn = ""
    if not c.get("url_verified"):
        warn = (f'<div class="warn-banner"><strong>Unverified attribution.</strong> '
                f'This site URL was auto-resolved (source: '
                f'{_h(c.get("url_source") or "unknown")}) — confirm the site really '
                f'belongs to this candidate before approving.</div>')

    twins = view.get("cross_committee") or []
    if twins:
        by_cand: dict[str, list[dict]] = {}
        for t in twins:
            by_cand.setdefault(t["candidate_id"], []).append(t)
        items = []
        for tcid, rows in by_cand.items():
            t = rows[0]
            tseat = f"{t['office'] or ''}-{t['state'] or ''}" + (
                f"-{t['district']}" if t["district"] else "")
            dets = ", ".join(
                f'<a href="/detection/{r["detection_id"]}">#{r["detection_id"]}</a>'
                + (f' ({_h(r["review_action"])})' if r["review_action"] else " (pending)")
                for r in rows)
            inactive = " · candidacy inactive" if t["inactive"] else ""
            items.append(f'<li>{_h(t["name"])} — {_h(tseat)} '
                         f'<span class="qurl">({_h(tcid)}{inactive})</span>: {dets}</li>')
        warn += (f'<div class="warn-banner"><strong>Multi-committee page.</strong> '
                 f'This exact page body was also detected under a different '
                 f'committee for what is likely the same campaign site. The '
                 f'guidance applies to one race — approve it under the committee '
                 f'for that race and reject the other attribution(s):'
                 f'<ul class="twin-list">{"".join(items)}</ul></div>')

    meta = f"""<dl class="meta">
      <dt>Seat</dt><dd>{_h(seat)} · {_h(c.get('party'))}</dd>
      <dt>FEC receipts</dt><dd>${float(c.get('receipts') or 0):,.0f}</dd>
      <dt>Campaign site</dt><dd><a href="{_h(c.get('website_url'))}" rel="nofollow noopener">{_h(c.get('website_url'))}</a> ({'verified' if c.get('url_verified') else 'unverified'})</dd>
      <dt>Detected on</dt><dd><a href="{_h(d.get('page_url'))}" rel="nofollow noopener">{_h(d.get('page_url'))}</a></dd>
      <dt>Fetched</dt><dd>{_h((d.get('fetched_at') or '')[:16].replace('T', ' '))} UTC</dd>
      <dt>Classifier</dt><dd>{_h(d.get('model'))}{' (escalated)' if d.get('escalated') else ''} · confidence {float(d.get('confidence') or 0):.2f}</dd>
    </dl>"""

    ev = "".join(
        f'<li><blockquote>{_h(e.get("quote"))}</blockquote>'
        f'<span class="why">{_h(e.get("why"))}</span></li>'
        for e in view["evidence"])
    evidence = (f'<h3>Quoted evidence (verbatim spans from the page)</h3>'
                f'<ul class="evidence">{ev}</ul>' if ev else "")

    shot = ""
    a = view["archive"]
    if a:
        aid = a["archive_id"]
        if a.get("screenshot_path"):
            shot += (f'<a href="/evidence/{aid}/screenshot">'
                     f'<img class="shot" src="/evidence/{aid}/screenshot" '
                     f'alt="Archived screenshot of {_h(d.get("page_url"))}"></a>')
        links = []
        if a.get("wayback_url"):
            links.append(f'<a href="{_h(a["wayback_url"])}" rel="noopener">Wayback snapshot</a>')
        if a.get("html_path"):
            links.append(f'<a href="/evidence/{aid}/html">Raw HTML (source view)</a>')
        if a.get("text_path"):
            links.append(f'<a href="/evidence/{aid}/text">Extracted text</a>')
        if links:
            shot += '<p class="evlinks">' + " · ".join(links) + "</p>"
    else:
        shot = ('<p class="evlinks"><em>No archived evidence row for this detection '
                '— review from the live page and extracted text below.</em></p>')

    rawtext = ""
    txt = d.get("raw_text") or ""
    if txt:
        shown = txt[:20000] + (f"\n… [{len(txt) - 20000:,} more chars truncated]"
                               if len(txt) > 20000 else "")
        rawtext = (f'<details class="rawtext"><summary>Extracted page text the '
                   f'classifier saw ({len(txt):,} chars)</summary>'
                   f'<pre>{_h(shown)}</pre></details>')

    corro = ""
    co = view["corroboration"]
    if co and co.get("ie_filing_count"):
        corro = (f'<section class="corroboration"><h2>IE corroboration</h2>'
                 f'<p class="sequence">FEC Schedule E this cycle: '
                 f'<strong>${float(co.get("supporting_total") or 0):,.0f}</strong> supporting'
                 + (f' · <strong>${float(co.get("opposing_total") or 0):,.0f}</strong> opposing'
                    if co.get("opposing_total") else "")
                 + f' · {_h(co.get("ie_filing_count"))} filings. Corroboration, not the trigger.</p>'
                 f'</section>')

    siblings = view["siblings"]
    aliases = group_box = ""
    if len(siblings) > 1:
        urls = "".join(f'<li>{_h(s["url"])} <span class="qurl">(#{s["detection_id"]})</span></li>'
                       for s in siblings)
        aliases = (f'<details class="aliases"><summary>{len(siblings)} identical alias '
                   f'pages (same page body, different URLs)</summary><ul>{urls}</ul></details>')
        group_box = (f'<label class="group-box"><input type="checkbox" name="group" '
                     f'value="1" checked> Apply to all {len(siblings)} identical alias '
                     f'pages (one judgment, one review record each) <kbd>g</kbd></label>')

    history = ""
    if view["reviews"]:
        items = "".join(
            f'<li><span class="chg-when">{_h(r["reviewed_at"][:10])}</span>'
            f'<strong>{_h(r["action"])}</strong>'
            + (f' by {_h(r["reviewer"])}' if r["reviewer"] else "")
            + (f' — {_h(r["notes"])}' if r["notes"] else "") + "</li>"
            for r in view["reviews"])
        history = (f'<section class="changes"><h2>Review history</h2>'
                   f'<ul class="changelog">{items}</ul></section>')

    form = f"""
    <section class="detection review-form-box">
      <h2>Record decision</h2>
      <form method="post" class="review-form">
        <label class="radio"><input type="radio" name="action" value="approve" required>
          <strong>Approve</strong> — becomes a publishable finding on the next site build <kbd>a</kbd></label>
        <label class="radio"><input type="radio" name="action" value="reject">
          <strong>Reject</strong> — recorded, never published <kbd>r</kbd></label>
        <label class="radio"><input type="radio" name="action" value="needs_more">
          <strong>Needs more</strong> — keep in the queue for re-scan / a closer look <kbd>n</kbd></label>
        {group_box}
        <label class="field">Reviewer
          <input name="reviewer" value="{_h(reviewer_default)}" placeholder="your name/id"></label>
        <label class="field">Notes
          <textarea name="notes" rows="3" placeholder="basis for the decision (optional)"></textarea></label>
        <button type="submit">Record review <kbd>⏎</kbd></button>
      </form>
      <p class="kbd-hint">Keyboard: <kbd>a</kbd>/<kbd>r</kbd>/<kbd>n</kbd> decide ·
        {'<kbd>g</kbd> toggle aliases · ' if group_box else ''}<kbd>Enter</kbd> submit ·
        <kbd>q</kbd> queue</p>
    </section>
    <script>{DETAIL_JS}</script>"""

    body = f"""
    <p class="crumb"><a href="/">← Queue</a> · {n_pending} finding(s) pending</p>
    <div class="cand-head">
      <h1>{_h(c.get('name'))}</h1>
      <span class="pill {pill_cls}">{pill_txt}</span>
    </div>
    {decided}{warn}{meta}
    <section class="detection">
      <h2>{_h(label)}</h2>
      <p class="rationale">{_h(d.get('rationale'))}</p>
      {evidence}
      {shot}
      {aliases}
      {rawtext}
    </section>
    {corro}{form}{history}"""
    return _page(c.get("name") or "Detection", body)


def _seat(c: dict) -> str:
    return f"{c.get('office') or ''}-{c.get('state') or ''}" + (
        f"-{c.get('district')}" if c.get("district") else "")


def _url_done_banner(qs: dict[str, list[str]]) -> str:
    cid = qs.get("done", [""])[0]
    action = qs.get("as", [""])[0]
    verbs = {
        "found": "URL saved (manual, verified) and added to data/websites.json",
        "none": "marked as no findable website",
        "wrong_race": "marked not-running-for-this-seat — excluded from "
                      "resolve/scan/publish",
    }
    if not cid or action not in verbs:
        return ""
    return f'<div class="done-banner">{_h(cid)} {verbs[action]}.</div>'


def _render_url_queue(conn: sqlite3.Connection, qs: dict[str, list[str]]) -> str:
    rows = []
    for c in url_queue(conn):
        rows.append(f"""<tr>
          <td><a href="/urls/{_h(c['candidate_id'])}">{_h(c['name'])}</a></td>
          <td>{_h(_seat(c))}</td>
          <td>{_h(c.get('party'))}</td>
          <td class="num">${float(c.get('receipts') or 0):,.0f}</td>
          <td class="qurl">{_h(c.get('universe_reason'))}</td>
        </tr>""")
    if rows:
        queue = f"""<table class="grid"><thead><tr>
          <th>Candidate</th><th>Seat</th><th>Party</th><th class="num">Receipts</th><th>In universe as</th>
        </tr></thead><tbody>{''.join(rows)}</tbody></table>
        <p class="kbd-hint">Keyboard: <kbd>j</kbd>/<kbd>k</kbd> select ·
          <kbd>Enter</kbd> open</p>
        <script>{QUEUE_JS}</script>"""
    else:
        queue = '<p class="empty">Queue clear — every candidate has a website or a human no-site call.</p>'
    n_none = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE url_source='human_none'").fetchone()[0]
    checked = (f'<p class="srcline">{n_none} candidate(s) previously marked '
               f'no-findable-website by a human.</p>' if n_none else "")
    body = f"""
    {_url_done_banner(qs)}
    <h1>Website triage queue</h1>
    <p class="summary">{len(rows)} candidate(s) with no resolved campaign site,
      richest first. A URL you save is stored as <strong>manual / verified</strong>
      and written to <code>data/websites.json</code>; the next
      <code>scan-all</code> picks the candidate up automatically.</p>
    {queue}
    {checked}"""
    return _page("Site URLs", body)


def _render_url_form(c: dict, *, n_pending: int, reviewer_default: str,
                     error: str | None = None, url_value: str = "",
                     banner: str = "") -> str:
    q = quote_plus(f"{c.get('name')} {c.get('state')} campaign website")
    research = " · ".join([
        f'<a href="https://www.google.com/search?q={q}" rel="noopener">Google</a>',
        f'<a href="https://www.fec.gov/data/candidate/{_h(c["candidate_id"])}/" rel="noopener">FEC filings</a>',
        f'<a href="https://ballotpedia.org/wiki/index.php?search={quote_plus(str(c.get("name")))}" rel="noopener">Ballotpedia</a>',
    ])
    err = f'<div class="warn-banner">{_h(error)}</div>' if error else ""
    meta = f"""<dl class="meta">
      <dt>Seat</dt><dd>{_h(_seat(c))} · {_h(c.get('party'))}</dd>
      <dt>FEC receipts</dt><dd>${float(c.get('receipts') or 0):,.0f}</dd>
      <dt>In universe as</dt><dd>{_h(c.get('universe_reason'))}</dd>
      <dt>Research</dt><dd>{research}</dd>
    </dl>"""
    body = f"""
    {banner}
    <p class="crumb"><a href="/urls">← Site URLs</a> · {n_pending} candidate(s) in the queue</p>
    <div class="cand-head"><h1>{_h(c.get('name'))}</h1></div>
    {err}{meta}
    <section class="detection review-form-box">
      <h2>Record what you found</h2>
      <form method="post" class="review-form">
        <label class="field">Campaign website URL (leave blank if none found)
          <input name="url" value="{_h(url_value)}" placeholder="https://…" autofocus></label>
        <label class="field">Reviewer
          <input name="reviewer" value="{_h(reviewer_default)}" placeholder="your name/id"></label>
        <div class="btn-row">
          <button type="submit" name="outcome" value="found">Save URL</button>
          <button type="submit" name="outcome" value="none" class="btn-secondary"
                  formnovalidate>No website found</button>
          <button type="submit" name="outcome" value="wrong_race" class="btn-secondary"
                  formnovalidate>Not running for this seat</button>
        </div>
      </form>
      <p class="kbd-hint">“Save URL” records it as manual/verified and appends it to
        <code>data/websites.json</code>. “No website found” records a human
        no-site call — the candidate leaves this queue and stays unscanned.
        “Not running for this seat” is for stale FEC records (e.g. a House record
        for someone actually running for Senate) — the record is excluded from
        resolve, scanning, and the published site entirely.</p>
    </section>"""
    return _page(c.get("name") or "Candidate", body)


# Shortcuts stay inert while a form field has focus (the guard on the target
# tag), so typing notes never fires a decision.
QUEUE_JS = """
const rows=[...document.querySelectorAll('table.grid tbody tr')];let sel=-1;
function mark(i){rows[sel]&&rows[sel].classList.remove('kbd-sel');sel=i;
  const r=rows[sel];if(r){r.classList.add('kbd-sel');r.scrollIntoView({block:'nearest'});}}
document.addEventListener('keydown',e=>{
  const t=e.target.tagName;
  if(t==='INPUT'||t==='TEXTAREA'||t==='SELECT'||e.metaKey||e.ctrlKey||e.altKey)return;
  const k=e.key.toLowerCase();
  if(k==='j'&&rows.length){mark(Math.min(sel+1,rows.length-1));e.preventDefault();}
  else if(k==='k'&&rows.length){mark(Math.max(sel-1,0));e.preventDefault();}
  else if((k==='o'||e.key==='Enter')&&sel>=0){
    const a=rows[sel].querySelector('a');if(a)location.href=a.href;}
});
"""

DETAIL_JS = """
const acts={a:'approve',r:'reject',n:'needs_more'};
document.addEventListener('keydown',e=>{
  const t=e.target.tagName;
  if(t==='INPUT'||t==='TEXTAREA'||t==='SELECT'||e.metaKey||e.ctrlKey||e.altKey)return;
  const k=e.key.toLowerCase();
  if(acts[k]){
    const el=document.querySelector(`input[name=action][value=${acts[k]}]`);
    if(el){el.checked=true;e.preventDefault();}
  }else if(k==='g'){
    const g=document.querySelector('input[name=group]');
    if(g){g.checked=!g.checked;e.preventDefault();}
  }else if(e.key==='Enter'){
    const f=document.querySelector('form.review-form');
    if(f&&f.querySelector('input[name=action]:checked')){f.requestSubmit();e.preventDefault();}
  }else if(k==='q'){location.href='/';}
});
"""

REVIEW_CSS = """
/* review-console additions on top of the published-site stylesheet */
.done-banner{background:#e9f2e6;border:1px solid #b9d3ae;border-left:4px solid #4b7a3f;padding:12px 16px;border-radius:4px;margin:16px 0;font-size:.94rem}
.warn-banner{background:#fbeaea;border:1px solid #e0b4b4;border-left:4px solid var(--pos);padding:12px 16px;border-radius:4px;margin:16px 0;font-size:.94rem}
.alias-tag{background:#eceae4;color:#88847a;border-radius:999px;padding:1px 8px;font-size:.72rem;font-weight:700;margin-left:6px;white-space:nowrap}
.unverified-tag{background:#fbe7c7;color:#8a5a14;border-radius:999px;padding:1px 8px;font-size:.7rem;font-weight:700;margin-left:6px;white-space:nowrap}
.twin-tag{background:#f3e0ee;color:#7d3a6c;border-radius:999px;padding:1px 8px;font-size:.7rem;font-weight:700;margin-left:6px;white-space:nowrap}
.twin-list{margin:8px 0 0 18px;padding:0}
.twin-list li{margin:4px 0}
.qurl{color:#7a766a;font-size:.84rem;word-break:break-all}
.empty{color:#6b675c;font-style:italic}
details.rawtext,details.aliases{margin:14px 0}
details.rawtext pre{max-height:420px;overflow:auto;background:#faf7f2;border:1px solid var(--line);border-radius:6px;padding:12px;font-size:.8rem;white-space:pre-wrap}
details summary{cursor:pointer;color:var(--accent);font-size:.9rem}
.review-form{display:flex;flex-direction:column;gap:12px;max-width:640px}
.review-form label.radio{display:block;padding:10px 12px;border:1px solid var(--line);border-radius:6px;background:#faf9f6;font-size:.94rem;cursor:pointer}
.review-form label.radio:hover{background:#f4f1ea}
.review-form .group-box{font-size:.9rem;color:#3a4654;background:#eef1f4;border-left:3px solid var(--accent);padding:10px 12px;border-radius:4px}
.review-form label.field{display:flex;flex-direction:column;gap:4px;font-size:.86rem;color:#7a766a}
.review-form input[type=text],.review-form input:not([type]),.review-form textarea{padding:8px 10px;border:1px solid var(--line);border-radius:6px;font-size:.92rem;font-family:inherit}
.review-form button{align-self:flex-start;padding:10px 22px;border:none;border-radius:6px;background:#23405a;color:#fff;font-size:.95rem;font-weight:600;cursor:pointer}
.review-form button:hover{background:#2e5375}
kbd{background:#eceae4;border:1px solid #d4d0c4;border-bottom-width:2px;border-radius:4px;padding:0 5px;font-size:.74rem;font-family:"SF Mono",ui-monospace,Menlo,Consolas,monospace;color:#5a564c}
.review-form button kbd{background:rgba(255,255,255,.18);border-color:rgba(255,255,255,.35);color:#fff}
.kbd-hint{color:#7a766a;font-size:.82rem;margin-top:14px}
tr.kbd-sel{outline:2px solid var(--accent);outline-offset:-2px;background:#eef1f4}
.btn-row{display:flex;gap:10px;flex-wrap:wrap}
.review-form .btn-secondary{background:#faf9f6;color:#5a564c;border:1px solid var(--line)}
.review-form .btn-secondary:hover{background:#f4f1ea}
"""


# ---------------------------------------------------------------------------
# WSGI plumbing.

_DETECTION_RE = re.compile(r"^/detection/(\d+)$")
_EVIDENCE_RE = re.compile(r"^/evidence/(\d+)/(screenshot|html|text)$")
_URL_FORM_RE = re.compile(r"^/urls/([A-Za-z0-9]+)$")

# kind -> (archives column, content type). Archived HTML is text/plain ON
# PURPOSE: a captured campaign page's scripts must never run in the console.
_EVIDENCE_KINDS = {
    "screenshot": ("screenshot_path", None),   # type from file suffix
    "html": ("html_path", "text/plain; charset=utf-8"),
    "text": ("text_path", "text/plain; charset=utf-8"),
}
_IMAGE_TYPES = {".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg"}


class ReviewApp:
    """WSGI application for the review console.

    One SQLite connection per request (connections aren't thread-shareable and
    the threading server handles requests concurrently); the schema is ensured
    once at construction so the console works on a fresh database too.
    """

    def __init__(self, db_path: str | Path,
                 overrides_path: str | Path | None = None):
        self.db_path = Path(db_path)
        # Where human-found URLs are durably recorded (data/websites.json
        # unless overridden — tests point this at a temp file).
        self.overrides_path = Path(overrides_path) if overrides_path else OVERRIDES_PATH
        init_db(self.db_path).close()

    def __call__(self, environ, start_response):
        conn = connect(self.db_path)
        try:
            status, headers, body = self._route(environ, conn)
        finally:
            conn.close()
        start_response(status, headers)
        return [body]

    # -- routing -----------------------------------------------------------
    _LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")

    def _route(self, environ, conn) -> tuple[str, list[tuple[str, str]], bytes]:
        method = environ.get("REQUEST_METHOD", "GET")
        path = environ.get("PATH_INFO", "/")
        qs = parse_qs(environ.get("QUERY_STRING", ""))

        if method == "POST":
            # Loopback binding doesn't stop a malicious page in the
            # reviewer's browser from firing a cross-origin form POST at
            # http://127.0.0.1:<port>/ (a "simple request" — no preflight) and
            # approving detections, nor a DNS-rebound hostname from reaching
            # us. Browsers always send Origin on cross-origin POSTs and Host
            # on everything; both must point at loopback. Non-browser clients
            # (tests, curl) send no Origin and are unaffected.
            host = urlsplit("//" + (environ.get("HTTP_HOST") or "")).hostname
            origin = environ.get("HTTP_ORIGIN")
            ohost = urlsplit(origin).hostname if origin else None
            if (host and host.lower() not in self._LOCAL_HOSTS) or \
                    (ohost and ohost.lower() not in self._LOCAL_HOSTS):
                return ("403 Forbidden",
                        [("Content-Type", "text/plain; charset=utf-8")],
                        b"cross-origin POSTs are not accepted")

        if path == "/styles.css":
            return "200 OK", [("Content-Type", "text/css; charset=utf-8")], \
                (CSS + REVIEW_CSS).encode()
        if path == "/" and method == "GET":
            return self._html(_render_queue(conn, qs))

        m = _DETECTION_RE.match(path)
        if m:
            det_id = int(m.group(1))
            if method == "POST":
                return self._post_review(environ, conn, det_id)
            view = detection_view(conn, det_id)
            if not view:
                return self._not_found(f"No detection #{det_id}.")
            reviewer = self._cookie_reviewer(environ)
            return self._html(_render_detail(
                view, reviewer_default=reviewer, n_pending=len(pending_groups(conn))))

        m = _EVIDENCE_RE.match(path)
        if m:
            return self._evidence(conn, int(m.group(1)), m.group(2))

        if path == "/urls" and method == "GET":
            return self._html(_render_url_queue(conn, qs))
        m = _URL_FORM_RE.match(path)
        if m:
            cid = m.group(1)
            row = conn.execute(
                "SELECT * FROM candidates WHERE candidate_id=?", (cid,)).fetchone()
            if not row:
                return self._not_found(f"No candidate {cid}.")
            if method == "POST":
                return self._post_url(environ, conn, dict(row))
            return self._html(_render_url_form(
                dict(row), n_pending=len(url_queue(conn)),
                reviewer_default=self._cookie_reviewer(environ),
                banner=_url_done_banner(qs)))

        return self._not_found("No such page.")

    # -- handlers ----------------------------------------------------------
    def _post_review(self, environ, conn, det_id: int):
        if not detection_view(conn, det_id):
            return self._not_found(f"No detection #{det_id}.")
        try:
            size = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            size = 0
        form = {k: v[0] for k, v in
                parse_qs(environ["wsgi.input"].read(size).decode("utf-8", "replace")).items()}
        action = form.get("action", "")
        if action not in REVIEW_ACTIONS:
            return ("400 Bad Request",
                    [("Content-Type", "text/plain; charset=utf-8")],
                    f"action must be one of {', '.join(REVIEW_ACTIONS)}".encode())
        reviewer = (form.get("reviewer") or "").strip()
        targets = record_review(
            conn, det_id, action, reviewer=reviewer, notes=(form.get("notes") or "").strip(),
            group=form.get("group") == "1")
        nxt = _next_pending(conn, exclude=set(targets))
        dest = f"/detection/{nxt}" if nxt else "/"
        headers = [("Location", f"{dest}?done={det_id}&as={action}&n={len(targets)}")]
        if reviewer:
            # Remember the reviewer across the session so it needn't be retyped.
            headers.append(("Set-Cookie",
                            f"reviewer={quote(reviewer)}; Path=/; SameSite=Lax"))
        return "303 See Other", headers, b""

    def _post_url(self, environ, conn, cand: dict):
        try:
            size = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            size = 0
        form = {k: v[0] for k, v in
                parse_qs(environ["wsgi.input"].read(size).decode("utf-8", "replace")).items()}
        outcome = form.get("outcome", "")
        reviewer = (form.get("reviewer") or "").strip()
        cid = cand["candidate_id"]
        if outcome == "none":
            record_no_site(conn, cid)
        elif outcome == "wrong_race":
            record_wrong_race(conn, cid)
        elif outcome == "found":
            url = normalize_url(form.get("url", ""))
            if not url:
                return self._html(_render_url_form(
                    cand, n_pending=len(url_queue(conn)), reviewer_default=reviewer,
                    error="That doesn't look like a usable http(s) URL — "
                          "check it and try again (or use “No website found”).",
                    url_value=form.get("url", "")))
            record_found_url(conn, cid, url, reviewer=reviewer or None,
                             overrides_path=self.overrides_path)
        else:
            return ("400 Bad Request",
                    [("Content-Type", "text/plain; charset=utf-8")],
                    b"outcome must be 'found', 'none' or 'wrong_race'")
        # Jump straight to the next untriaged candidate, same as the detection
        # flow — with ~240 in the queue, round-tripping via the list would drag.
        remaining = url_queue(conn)
        dest = f"/urls/{remaining[0]['candidate_id']}" if remaining else "/urls"
        headers = [("Location", f"{dest}?done={quote(cid)}&as={outcome}")]
        if reviewer:
            headers.append(("Set-Cookie",
                            f"reviewer={quote(reviewer)}; Path=/; SameSite=Lax"))
        return "303 See Other", headers, b""

    def _evidence(self, conn, archive_id: int, kind: str):
        column, ctype = _EVIDENCE_KINDS[kind]
        row = conn.execute(
            f"SELECT {column} AS p FROM archives WHERE archive_id = ?",
            (archive_id,)).fetchone()
        if not row or not row["p"]:
            return self._not_found("No such archived artifact.")
        path = Path(row["p"])
        if not path.exists():
            return self._not_found(f"Artifact file missing on disk: {path.name}")
        if ctype is None:
            ctype = _IMAGE_TYPES.get(path.suffix.lower(), "application/octet-stream")
        return "200 OK", [("Content-Type", ctype)], path.read_bytes()

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _cookie_reviewer(environ) -> str:
        cookie = SimpleCookie(environ.get("HTTP_COOKIE", ""))
        return unquote(cookie["reviewer"].value) if "reviewer" in cookie else ""

    @staticmethod
    def _html(page: str):
        return "200 OK", [("Content-Type", "text/html; charset=utf-8")], page.encode()

    def _not_found(self, msg: str):
        body = _page("Not found", f"<h1>Not found</h1><p>{_h(msg)}</p>"
                                  f'<p><a href="/">← Back to the queue</a></p>')
        return "404 Not Found", [("Content-Type", "text/html; charset=utf-8")], body.encode()
