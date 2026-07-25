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

This is the local review build (`--include-pending`, default). A strict public
build (`--approved-only`) would emit approved positives + dated negatives only.
"""
from __future__ import annotations

import html
import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .util import now_iso, sha256_text

POSITIVE_LABEL = "Posted public messaging guidance consistent with red-boxing"
AMBIGUOUS_LABEL = "Possible messaging guidance — under review"
# An ambiguous detection a human reviewer then approved: publishable, with the
# classifier's initial hesitation disclosed rather than hidden.
AMBIGUOUS_CONFIRMED_LABEL = ("Posted public messaging guidance consistent with "
                             "red-boxing — initially classified ambiguous by the "
                             "automated screen; confirmed on human review")


def _neg_label(date: str) -> str:
    return f"No public messaging guidance detected as of {date[:10]}"


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


# ---------------------------------------------------------------------------
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

    # Highest-severity detection per candidate (one window-function query). Same
    # ordering as before: red_box > ambiguous > none, then confidence; the final
    # detection_id tiebreak makes the pick deterministic.
    top_det_by_cid: dict[str, dict] = {}
    for r in conn.execute(
        """SELECT * FROM (
              SELECT d.*, s.url AS page_url,
                     ROW_NUMBER() OVER (
                       PARTITION BY d.candidate_id
                       ORDER BY CASE d.classification
                           WHEN 'red_box_guidance' THEN 2 WHEN 'ambiguous' THEN 1 ELSE 0 END DESC,
                           d.confidence DESC, d.detection_id DESC) AS rn
              FROM detections d JOIN scans s USING(scan_id)
           ) WHERE rn = 1"""):
        d = dict(r)
        d.pop("rn", None)
        top_det_by_cid[d["candidate_id"]] = d

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

    # All change events, bucketed by candidate in memory (one ordered query).
    changes_by_cid: dict[str, list[dict]] = {}
    for r in conn.execute(
        "SELECT * FROM change_events ORDER BY candidate_id, detected_at DESC"):
        changes_by_cid.setdefault(r["candidate_id"], []).append(dict(r))

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
            try:
                evidence = json.loads(det.get("evidence") or "[]")
            except json.JSONDecodeError:
                evidence = []
        status, label = _status(det, review, last, scan_count, candidate=dict(c))
        views.append(CandidateView(
            row=dict(c), status=status, label=label,
            detection=det, evidence=evidence, archive=archive,
            scan_count=scan_count, last_scanned=last, review=review,
            corroboration=corr_by_cid.get(cid),
            changes=changes_by_cid.get(cid, []),
        ))
    return views


def _status(det, review, last, scan_count, candidate=None):
    if not scan_count:
        # Distinguish "no campaign site found" / "blocked by robots" / "not yet
        # scanned" so each gap is visible rather than silent (spec §3.1 honesty).
        if candidate is not None and not candidate.get("website_url"):
            return "no_url", "No campaign site found — not scanned"
        if candidate is not None and candidate.get("scan_status") == "robots_blocked":
            return "blocked_by_robots", "Site blocks automated access — not scanned"
        if candidate is not None and candidate.get("scan_status") == "fetch_failed":
            return "fetch_failed", "Site unreachable — fetch failed"
        return "not_scanned", "Not yet scanned"
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
_STATUS_RANK = {
    "positive_published": 0, "positive_pending": 1, "ambiguous_pending": 2,
    "rejected": 3, "negative": 4, "fetch_failed": 5,
    "blocked_by_robots": 6, "no_url": 7, "not_scanned": 8,
}

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
    global _SITE_URL
    _SITE_URL = (site_url or "").rstrip("/") or None
    out_dir = Path(out_dir)
    (out_dir / "evidence").mkdir(parents=True, exist_ok=True)
    views = _gather(conn)
    if approved_only:
        views = [v for v in views if v.status in ("positive_published", "negative")]

    # Copy evidence screenshots + archived PDFs into the site, rewrite to
    # relative paths.
    for v in views:
        if not v.archive:
            continue
        for key, rel_key in (("screenshot_path", "screenshot_rel"),
                             ("pdf_path", "pdf_rel")):
            if v.archive.get(key):
                src = Path(v.archive[key])
                if src.exists():
                    dst = out_dir / "evidence" / src.name
                    shutil.copy2(src, dst)
                    v.archive[rel_key] = f"evidence/{src.name}"

    # Per-candidate pages (natural order is irrelevant; each is standalone).
    for v in views:
        (out_dir / f"{v.row['candidate_id']}.html").write_text(_render_candidate(v))

    # Index, paginated. Counts/coverage are global (over every candidate); only
    # the table rows are sliced per page.
    counts: dict[str, int] = {}
    for v in views:
        counts[v.status] = counts.get(v.status, 0) + 1
    index_views = sorted(views, key=_index_sort_key)
    size = max(1, int(page_size))
    pages = [index_views[i:i + size] for i in range(0, len(index_views), size)] or [[]]
    for pno, page in enumerate(pages, start=1):
        fname = "index.html" if pno == 1 else f"index-{pno}.html"
        (out_dir / fname).write_text(_render_index(
            page, approved_only, counts=counts, all_views=index_views,
            page_no=pno, n_pages=len(pages)))
    # Full row set as one fragment; the index JS fetches it on first filter
    # interaction so name/status/state filters search every page, not just the
    # slice the visitor is on.
    (out_dir / "index-data.json").write_text(json.dumps(
        {"html": "".join(_index_row(v) for v in index_views)}))

    (out_dir / "methodology.html").write_text(_render_methodology())
    (out_dir / "corrections.html").write_text(_render_corrections())
    (out_dir / "about.html").write_text(_render_about())
    (out_dir / "404.html").write_text(_render_404())
    (out_dir / "styles.css").write_text(CSS)
    import base64
    (out_dir / "favicon.svg").write_text(_FAVICON_SVG)
    (out_dir / "favicon.png").write_bytes(base64.b64decode(_FAVICON_PNG48))
    (out_dir / "favicon.ico").write_bytes(base64.b64decode(_FAVICON_ICO))
    (out_dir / "apple-touch-icon.png").write_bytes(base64.b64decode(_FAVICON_PNG180))

    if _SITE_URL:
        paths = (["index"] + [f"index-{p}" for p in range(2, len(pages) + 1)]
                 + ["methodology", "corrections", "about"]
                 + [v.row["candidate_id"] for v in index_views])
        today = now_iso()[:10]
        urls = "".join(
            f"<url><loc>{_h(_canonical(p))}</loc><lastmod>{today}</lastmod></url>"
            for p in paths)
        (out_dir / "sitemap.xml").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"{urls}</urlset>\n")
        (out_dir / "robots.txt").write_text(
            f"User-agent: *\nAllow: /\n\nSitemap: {_SITE_URL}/sitemap.xml\n")
        _write_feeds(out_dir, views)
    return out_dir


def _write_feeds(out_dir: Path, views: list[CandidateView]) -> None:
    """feed.xml (RSS 2.0) + feed.json (JSON Feed 1.1) of published findings.

    Public builds only: items need absolute URLs, and review builds must never
    describe pending detections. Newest approval first, capped at the 50 most
    recent — the index stays the full ledger. Neither file is in the sitemap,
    and neither ends in .html, so the stale-page cleanup never touches them.
    """
    from datetime import datetime, timezone
    from email.utils import format_datetime

    def _approved_at(v: CandidateView) -> str:
        return ((v.review or {}).get("reviewed_at")
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
        title = (f"{_display_name(v.row['name'])} ({_seat_compact(v.row)}) "
                 "— red-box guidance found")
        parts = [v.label]
        page = (v.detection or {}).get("page_url")
        if page:
            parts.append(f"Guidance page: {page}")
        quote = next((e.get("quote") for e in v.evidence if e.get("quote")), None)
        if quote:
            parts.append(f"Quoted span: “{quote}”")
        desc = " · ".join(parts)
        dt = _parse_iso(_approved_at(v))
        pub = f"<pubDate>{format_datetime(dt)}</pubDate>" if dt else ""
        guid = f"{cid}/{(v.detection or {}).get('detection_id', 0)}"
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
        f"<title>Red Box Watch — Findings</title><link>{_h(_SITE_URL + '/')}</link>"
        f'<atom:link href="{_h(_SITE_URL + "/feed.xml")}" rel="self" '
        'type="application/rss+xml"/>'
        f"<description>{_h(tagline)}</description>"
        f"{''.join(items_xml)}</channel></rss>\n")
    (out_dir / "feed.json").write_text(json.dumps({
        "version": "https://jsonfeed.org/version/1.1",
        "title": "Red Box Watch — Findings",
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
        return "index.html" if p == 1 else f"index-{p}.html"
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
STATUS_PILL = {
    "positive_published": ("FINDING", "pill-pos"),
    "positive_pending": ("PENDING REVIEW", "pill-pending"),
    "ambiguous_pending": ("PENDING REVIEW", "pill-amb"),
    "rejected": ("NOT A FINDING", "pill-neg"),
    "negative": ("NONE DETECTED", "pill-neg"),
    "not_scanned": ("NOT SCANNED", "pill-muted"),
    "no_url": ("NO SITE FOUND", "pill-muted"),
    "blocked_by_robots": ("BLOCKED (ROBOTS)", "pill-muted"),
    "fetch_failed": ("FETCH FAILED", "pill-muted"),
}


def _h(s) -> str:
    return html.escape(str(s if s is not None else ""))


# ---------------------------------------------------------------------------
# Rendering. Design: "Broadsheet" — editorial investigations-desk aesthetic
# (warm paper, ink, one decisive red; Fraunces / Source Serif 4 / Libre
# Franklin), with the Sunlight state-grid heatmap on the index. The red box
# itself is the brand mark. Chosen from design-concepts/ (2026-07-11).

_FONTS = """<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,300..900;1,9..144,300..900&family=Libre+Franklin:ital,wght@0,300..700;1,300..700&family=Source+Serif+4:ital,opsz,wght@0,8..60,300..700;1,8..60,300..700&display=swap" rel="stylesheet">"""

_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"]

# Favicon: the brand's red box as the tab icon, at ~62% of the canvas so it
# doesn't dwarf neighboring tab icons. SVG for modern browsers; PNG fallbacks
# (48px transparent-padded — Google wants ≥48px for search results — and
# 180px apple-touch on paper; iOS blackens transparency) because Safari
# ignores SVG favicons. A 16/32/48 favicon.ico sits at the root unlinked for
# crawlers that request /favicon.ico directly (Cloudflare Pages otherwise
# soft-404s it with the HTML fallback).
_FAVICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
                '<rect x="3" y="3" width="10" height="10" fill="#b93425"/></svg>')
_FAVICON_PNG48 = ("iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAYAAABXAvmHAAAAWElEQVR42u3YsQ0AIQwA"
                  "sQSxChuwPNP9r4BEAQhfnQIrqcjRW9xcicsDAAAAAFiqTs59m96XTggAAAAAAAAAAAAA"
                  "AAAAAAAAAAAAIN79nU4bAAAAAAA4sR847gJ3+WSMHgAAAABJRU5ErkJggg==")
_FAVICON_ICO = ("AAABAAMAEBAAAAAAIABoAAAANgAAACAgAAAAACAAiwAAAJ4AAAAwMAAAAAAgALAAAAAp"
                "AQAAiVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAL0lEQVR4nGPcaaLK"
                "QAlgokg3NQxgQeP/J1IfI9VcwDRqAMNoGDBgpER4Chs6CQkA4rQCN+chrFoAAAAASUVO"
                "RK5CYIKJUE5HDQoaCgAAAA1JSERSAAAAIAAAACAIBgAAAHN6evQAAABSSURBVHic7ZbR"
                "CYAwFMTSo6u4gcs7nS4ggiAc2GSBhpf76Dj2jSapvo4CmID5sI/z470NR3hHKBMFMEGZ"
                "KIAJykQBTLB6gvn2D/e7C0QBVk9wAXeKAlfsKSb7AAAAAElFTkSuQmCCiVBORw0KGgoA"
                "AAANSUhEUgAAADAAAAAwCAYAAABXAvmHAAAAd0lEQVR4nOXYsQ2AMAADwc+LVdiA5Zku"
                "rIBEER6udmHJncd57JRJnMRJnMRJnMRJnMRtN3OTNcbnF5A4iZM4iZM4iZM4iZM4iZM4"
                "iZM4iZM4iZM4iZM4iZM4iZM4iZM4iZM4iZM4+ck7PXgpiZM4iZM4iZM4iXN1gacuOO4C"
                "d7DEROMAAAAASUVORK5CYII=")
_FAVICON_PNG180 = ("iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAIAAACyr5FlAAABp0lEQVR42u3SMQ0AIBAE"
                   "wTeDA8xji4pQvQEqQvEkk6yCu4m9pnQsTCA4BIfgEByCQ3AIDsEhOASHBIfgEByCQ3AI"
                   "DsEhOASH4JDgEByCQ3AIDsEhOASH4BAcEhyCQ2VxjN70MDgEh+AQHHDAAQcccMABBxxw"
                   "wAEHHHDAAYfgEByCAw444IADDjjggAMOOOCAAw444BAcgkNwwAEHHHDAAQcccMABBxxw"
                   "wAEHHIJDcAgOj8IBBxxwwAEHHHDAAQcccMABBxxwCA7BITjggAMOOOCAAw444IADDjjg"
                   "gAMOwSE4BAcccMABBxxwwAEHHHDAAQcccMAhOASH4PAoHHDAAQcccMABBxxwwAEHHHDA"
                   "AYfgEByCAw444IADDjjggAMOOOCAAw444BAcgkNwwAEHHHDAAQcccMABBxxwwAEHHIJD"
                   "cAgOd8IBBxxwwAEHHHDAAQcccMABBxxwCA7BITjggAMOOOCAAw444ICjFg79GByCQ3AI"
                   "DsEhOASH4BAcgkNwSHAIDsEhOASH4BAcgkNwCA4JDsEhOASH4BAcgkNwCA7BIcEhOHRR"
                   "Avk9tm3h8owWAAAAAElFTkSuQmCC")


def _pub_date() -> str:
    iso = now_iso()
    return f"{_MONTHS[int(iso[5:7]) - 1]} {int(iso[8:10])}, {iso[:4]}"


# Canonical public URL (no trailing slash); set per-build by build_site from
# publish.site_url. None for local/review builds.
_SITE_URL: str | None = None


def _canonical(path: str) -> str | None:
    """Absolute extensionless URL for a page ('index' -> the site root).
    Cloudflare Pages serves pretty URLs, so /X.html canonicalizes to /X."""
    if not _SITE_URL:
        return None
    return f"{_SITE_URL}/" if path == "index" else f"{_SITE_URL}/{path}"


def _seo_head(title: str, path: str, desc: str, og_image: str | None,
              og_type: str) -> str:
    tags = []
    if desc:
        tags.append(f'<meta name="description" content="{_h(desc)}">')
    url = _canonical(path)
    if url:
        tags.append(f'<link rel="canonical" href="{_h(url)}">')
    if desc:
        tags += [
            '<meta property="og:site_name" content="Red Box Watch">',
            f'<meta property="og:title" content="{_h(title)}">',
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
    # "Red Box Watch" as the site name in results instead of the bare domain.
    if path == "index" and _SITE_URL:
        tags.append('<script type="application/ld+json">' + json.dumps({
            "@context": "https://schema.org",
            "@type": "WebSite",
            "name": "Red Box Watch",
            "alternateName": "RedBoxWatch",
            "url": f"{_SITE_URL}/",
        }) + "</script>")
    return "\n".join(tags)


def _css_version() -> str:
    """Content hash for cache-busting the stylesheet URL. Pages serves
    styles.css with max-age=14400, so without this a deploy leaves visitors
    on 4-hour-stale CSS while the HTML is already new."""
    return sha256_text(CSS)[:8]


def _layout(title: str, body: str, *, page_class: str = "", active: str = "",
            path: str = "", desc: str = "", og_image: str | None = None,
            og_type: str = "website", root: str = "") -> str:
    # root="/" makes internal links absolute — required for 404.html, which
    # Pages serves at arbitrary depths where relative links would break.
    def nav(href: str, label: str, key: str) -> str:
        cur = ' aria-current="page"' if key == active else ""
        return f'<a href="{root}{href}"{cur}>{label}</a>'
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_h(title)} · Red Box Watch</title>
{_seo_head(title, path, desc, og_image, og_type)}
<link rel="icon" href="{root}favicon.svg" type="image/svg+xml">
<link rel="icon" href="{root}favicon.png" type="image/png" sizes="48x48">
<link rel="apple-touch-icon" href="{root}apple-touch-icon.png">
{_FONTS}
{f'<link rel="alternate" type="application/rss+xml" title="Red Box Watch — Findings" href="{_SITE_URL}/feed.xml">' if _SITE_URL else ''}
<link rel="stylesheet" href="{root}styles.css?v={_css_version()}"></head><body>
<header class="masthead">
  <div class="folio wrap">
    <span>Published {_h(_pub_date())}&ensp;·&ensp;Updated with each build</span>
    <nav>{nav('index.html', 'Index', 'index')}{nav('methodology.html', 'Methodology', 'methodology')}{nav('corrections.html', 'Corrections &amp; Appeals', 'corrections')}{nav('about.html', 'About', 'about')}</nav>
  </div>
  <div class="nameplate wrap">
    <a class="brand" href="{root}index.html"><span class="redbox"></span>Red&nbsp;Box&nbsp;Watch</a>
    <p class="tagline">A public ledger of red&#8209;boxing &#8212; campaign&#8209;site signals to super PACs</p>
  </div>
  <div class="wrap"><div class="double-rule"></div></div>
</header>
<main class="wrap {page_class}">{body}</main>
<footer class="site-foot"><div class="wrap">
  <div class="double-rule"></div>
  <p class="foot-mark"><span class="redbox"></span></p>
  <p>Detections are gated behind human review before any are treated as findings. Negatives are recorded as dated &#8220;no guidance detected as of&#8221; statements, never as &#8220;does not red-box.&#8221; Red-boxing exploits campaign-finance rules openly; it is <strong>not per se unlawful</strong>. Every published claim links to archived evidence. Generated {_h(now_iso()[:16].replace('T', ' '))} UTC.</p>
  <p class="foot-contact">Press &amp; media: <a class="px-mail" href="#">press&nbsp;[at]&nbsp;redboxwatch&nbsp;[dot]&nbsp;org</a>&ensp;·&ensp;<a href="{root}corrections.html">Corrections &amp; appeals</a>{f'&ensp;·&ensp;<a href="{_SITE_URL}/feed.xml">New-findings RSS</a>' if _SITE_URL else ''}</p>
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
    "IL": (3, 6), "WI": (3, 7), "MI": (3, 9), "NY": (3, 10), "MA": (3, 11),
    "OR": (4, 1), "NV": (4, 2), "WY": (4, 3), "SD": (4, 4), "IA": (4, 5),
    "IN": (4, 6), "OH": (4, 7), "PA": (4, 8), "NJ": (4, 9), "CT": (4, 10), "RI": (4, 11),
    "CA": (5, 1), "UT": (5, 2), "CO": (5, 3), "NE": (5, 4), "MO": (5, 5),
    "KY": (5, 6), "WV": (5, 7), "VA": (5, 8), "MD": (5, 9), "DE": (5, 10),
    "AZ": (6, 2), "NM": (6, 3), "KS": (6, 4), "AR": (6, 5), "TN": (6, 6),
    "NC": (6, 7), "SC": (6, 8), "DC": (6, 9),
    "OK": (7, 4), "LA": (7, 5), "MS": (7, 6), "AL": (7, 7), "GA": (7, 8),
    "HI": (8, 1), "TX": (8, 4), "FL": (8, 9),
}

STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
    "AS": "American Samoa", "GU": "Guam", "MP": "Northern Mariana Islands",
    "PR": "Puerto Rico", "VI": "U.S. Virgin Islands",
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
        seat = f"{_h(c.get('office'))}-{_h(c.get('state'))}" + (
            f"-{_h(c.get('district'))}" if c.get("district") else "")
        ie = ""
        if v.corroboration and v.corroboration.get("supporting_total"):
            ie = f'<span class="pop-ie">{_h(_money_compact(float(v.corroboration["supporting_total"])))}</span>'
        items.append(
            f'<li><a href="{_h(c["candidate_id"])}.html">{_h(_display_name(c.get("name")))}</a>'
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
# stripping trailing dots, only in the trailing zone of the given-name half —
# so 'Do' the Vietnamese name or 'Maj' the Scandinavian name can't be eaten
# (deliberately excluded), and a leading token is never touched.
_NAME_TITLE = {"MR", "MRS", "MS", "MISS", "DR", "HON", "REV", "PROF",
               "SGT", "COL", "CAPT", "LT", "GEN",
               "MD", "PHD", "DDS", "OD", "JD", "ESQ", "MBA", "CPA", "FACS",
               "M.C"}


def _display_name(name: str) -> str:
    """FEC ALL-CAPS 'LAST, FIRST' -> natural display order and case:
    'MOORE, FELIX BARRY' -> 'Felix Barry Moore'. Display-only — the raw FEC
    string stays the DB/sort/matching key everywhere.

    Reorder: split on commas (empty segments like 'BRINK,, BRIDGET' are filing
    noise, dropped); surname is the first part, everything after joins as the
    given-name half. From that half's tail, strip self-styled titles/degrees
    ('WHALEN, JEROMIE PATRICK DR.', 'DUNN, NEAL PATRICK MD, FACS', with 'THE'
    swallowed before a title for 'WOMACK, STEPHEN A THE HON') and re-seat
    suffixes after the surname ('STEUBE, W. GREGORY III', 'SMITH, RAYMOND
    EDWARD DR. JR.'). A lone bare 'V' with no title context stays put — as a
    trailing token it's more likely a middle initial ('SMITH, JOHN V') — but
    after a title it reads as a suffix ('MARKERT, GEORGE WASHINGTON MR V').
    Consecutive duplicate tokens are collapsed ('GUTHRIE, S. BRETT BRETT
    HON.'). Suffixes already fused into the surname ('ONDER JR, ROBERT FRANK')
    land correctly by the swap alone. No comma: name passes through as filed.
    Case: capitalize each letter-run (handles hyphens/apostrophes), keep
    roman-numeral suffixes upper, give Mc- surnames their inner cap."""
    parts = [p.strip() for p in (name or "").split(",") if p.strip()]
    if len(parts) >= 2:
        last = parts[0]
        given = " ".join(parts[1:]).split()
        suffixes: list[str] = []
        saw_title = False
        while given:
            t = given[-1].rstrip(".")
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
    import re as _re
    return _re.sub(r"[A-Za-z]+", cap_run, name or "")


# Honorifics, ranks, and degrees the FEC sometimes appends to the given name
# ("SANFORD, MARSHALL HON", "ROCHFORD, ROBERT CAPT") — dropped in prose.
# Generational suffixes move after the surname.
_NAME_HONORIFICS = {"HON", "MR", "MRS", "MS", "MISS", "DR", "REV",
                    "CAPT", "CAPTAIN", "COL", "COLONEL", "MAJ", "MAJOR",
                    "SGT", "LT", "GEN", "CDR", "ADM",
                    "MD", "PHD", "ESQ", "JD", "DDS", "DO", "RN"}
_NAME_SUFFIXES = {"JR", "SR", "II", "III", "IV", "V", "VI", "VII"}


def _name_prose(name: str) -> str:
    """FEC 'LAST, FIRST MIDDLE [HON]' -> 'First Middle Last' for titles and
    meta descriptions, where ledger-style 'Last, First' reads as jargon."""
    disp = _display_name(name)
    if "," not in disp:
        return disp
    last, _, rest = disp.partition(",")
    # Some FEC names carry a second comma before the suffix ("CARL, JERRY LEE, JR").
    toks = [t for t in rest.replace(",", " ").split()
            if t.upper().rstrip(".") not in _NAME_HONORIFICS]
    suffix = [t for t in toks if t.upper().rstrip(".") in _NAME_SUFFIXES]
    given = [t for t in toks if t not in suffix]
    return " ".join(given + [last.strip()] + suffix).strip()


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


def _pretty_date(iso: str) -> str:
    """'2026-07-16...' -> 'July 16, 2026' (falls back to the raw date)."""
    from datetime import date
    try:
        d = date.fromisoformat(iso[:10])
        return f"{d.strftime('%B')} {d.day}, {d.year}"
    except ValueError:
        return iso[:10]


def _render_index(views: list[CandidateView], approved_only: bool, *,
                  counts: dict[str, int] | None = None,
                  all_views: list[CandidateView] | None = None,
                  page_no: int = 1, n_pages: int = 1) -> str:
    # ``views`` is this page's slice; counts/coverage, the stat deck, the state
    # heatmap and the state dropdown are global (over every candidate).
    if counts is None:
        counts = {}
        for v in views:
            counts[v.status] = counts.get(v.status, 0) + 1
    if all_views is None:
        all_views = views

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
      <select id="f-status" aria-label="Filter by status"><option value="">All statuses</option>
        <option value="positive_published">Findings</option>
        <option value="positive_pending">Pending (red-box)</option>
        <option value="ambiguous_pending">Pending (ambiguous)</option>
        <option value="negative">None detected</option>
        <option value="rejected">Not a finding</option>
        <option value="not_scanned">Not scanned</option>
        <option value="no_url">No site found</option>
        <option value="blocked_by_robots">Blocked by robots</option>
        <option value="fetch_failed">Fetch failed</option></select>
      <select id="f-state" aria-label="Filter by state"><option value="">All states</option>{_state_opts(all_views)}</select>
      {paged_note}
    </form>
    <table class="agate"><thead><tr>
      <th data-sort="0">Candidate</th><th data-sort="1">Seat</th><th data-sort="2">Party</th>
      <th data-sort="3">Status</th><th data-sort="4" class="num">Conf.</th>
      <th data-sort="5" class="num">Aligned IE</th><th data-sort="6" class="num">Pages</th>
    </tr></thead><tbody id="rows">{''.join(rows)}</tbody></table>
    {pager}
    <script>const PAGED={'true' if n_pages > 1 else 'false'};{INDEX_JS}</script>"""
    desc = (f"Red Box Watch tracks red-boxing — public messaging and media-buy "
            f"guidance posted on federal campaign websites to signal super PACs. "
            f"{n_findings} human-reviewed findings across {total} candidates, "
            f"every claim linked to archived evidence.")
    return _layout("Candidate Index" if page_no == 1 else f"Candidate Index — page {page_no}",
                   body, page_class="page-index", active="index",
                   path="index" if page_no == 1 else f"index-{page_no}", desc=desc)


def _index_row(v: CandidateView) -> str:
    c = v.row
    pill_txt, pill_cls = STATUS_PILL.get(v.status, ("—", "pill-muted"))
    conf = f"{v.detection['confidence']:.2f}" if v.detection and v.detection.get("confidence") is not None else "—"
    ie = ""
    if v.corroboration and v.corroboration.get("supporting_total"):
        ie = f"${float(v.corroboration['supporting_total']):,.0f}"
    return f"""<tr data-status="{v.status}" data-state="{_h(c.get('state'))}"
        data-office="{_h(c.get('office'))}" data-party="{_h(c.get('party'))}">
      <td class="cand"><a href="{_h(c['candidate_id'])}.html">{_h(c.get('name'))}</a></td>
      <td class="seat">{_h(c.get('office'))}-{_h(c.get('state'))}{('-' + _h(c.get('district'))) if c.get('district') else ''}</td>
      <td class="party">{_h(c.get('party'))}</td>
      <td><span class="status {pill_cls}">{pill_txt}</span></td>
      <td class="num conf">{conf}</td>
      <td class="num ie ie-col">{ie}</td>
      <td class="num pages">{v.scan_count}</td>
    </tr>"""


def _state_opts(views):
    states = sorted({v.row.get("state") for v in views if v.row.get("state")})
    return "".join(f'<option value="{_h(s)}">{_h(s)}</option>' for s in states)


def _render_candidate(v: CandidateView) -> str:
    c = v.row
    pill_txt, pill_cls = STATUS_PILL.get(v.status, ("—", "pill-muted"))
    seat = f"{_h(c.get('office'))}-{_h(c.get('state'))}" + (f"-{_h(c.get('district'))}" if c.get("district") else "")
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
    if v.detection and v.status != "negative":
        d = v.detection
        ev = "".join(
            f'<li><blockquote>{_h(e.get("quote"))}</blockquote><span class="why">{_h(e.get("why"))}</span></li>'
            for e in v.evidence)
        exhibit = ""
        if v.archive:
            is_pdf = bool(v.archive.get("pdf_rel"))
            cap_bits = []
            if is_pdf:
                cap_bits.append(f'<a href="{_h(v.archive["pdf_rel"])}" rel="noopener">Archived PDF (original)</a>')
            if v.archive.get("wayback_url"):
                cap_bits.append(f'<a href="{_h(v.archive["wayback_url"])}" rel="noopener">Wayback snapshot</a>')
            if v.archive.get("html_path"):
                cap_bits.append("Raw HTML preserved")
            caption = " &#183; ".join(cap_bits)
            if v.archive.get("screenshot_rel"):
                rel = _h(v.archive["screenshot_rel"])
                if is_pdf:
                    alt = f"Rendered pages of the archived PDF from {_h(d.get('page_url'))}"
                    what = f"Pages rendered from the PDF at {_h(d.get('page_url'))}"
                else:
                    alt = f"Archived screenshot of {_h(d.get('page_url'))}"
                    what = f"Full-page screenshot of {_h(d.get('page_url'))}"
                exhibit = f"""<figure class="exhibit">
              <a class="exhibit-frame" href="{rel}"><img src="{rel}" alt="{alt}"></a>
              <figcaption><span class="exhibit-label">Archived at detection</span>{what}{(' &#183; ' + caption) if caption else ''}</figcaption>
            </figure>"""
            elif caption:
                exhibit = f'<p class="evlinks"><span class="exhibit-label">Archived at detection</span>{caption}</p>'
        detail = f"""
        <section class="detection">
          <h2 class="section-head"><span class="redbox"></span>{_h(v.label)}</h2>
          <p class="srcline">Detected on <a href="{_h(d.get('page_url'))}" rel="nofollow noopener">{_h(d.get('page_url'))}</a>
             &ensp;&#183;&ensp;classifier confidence {float(d.get('confidence') or 0):.2f}&ensp;&#183;&ensp;model {_h(d.get('model'))}{' (escalated)' if d.get('escalated') else ''}</p>
          <p class="rationale">{_h(d.get('rationale'))}</p>
          <h3 class="evidence-head"><span class="redbox"></span>Quoted evidence — verbatim spans from the page</h3>
          <ul class="evidence">{ev}</ul>
          {exhibit}
          {_render_page_text(v)}
        </section>"""
    elif v.status == "negative":
        detail = f"""<section class="detection">
          <h2 class="section-head section-head-quiet">{_h(v.label)}</h2>
          <p class="rationale">Across {v.scan_count} pages scanned, no content matching the functional red-box pattern was detected. Absence of a finding is not proof — a box may have been removed, a page uncrawled, or a PDF unparsed.</p>
        </section>"""
    elif v.status == "no_url":
        detail = ('<section class="detection"><h2 class="section-head section-head-quiet">'
                  'No campaign site found — not scanned</h2>'
                  '<p class="rationale">No official campaign website could be resolved for '
                  'this candidate (manual override, Wikipedia, FEC committee, and web search '
                  'all returned nothing), so no pages were scanned. This is a coverage gap, '
                  'not a finding of any kind — it does not indicate the presence or absence '
                  'of red-boxing.</p></section>')
    elif v.status == "blocked_by_robots":
        detail = (f'<section class="detection"><h2 class="section-head section-head-quiet">'
                  f'Site blocks automated access — not scanned</h2>'
                  f'<p class="rationale">The candidate\'s site '
                  f'(<a href="{_h(c.get("website_url"))}" rel="nofollow noopener">{_h(c.get("website_url"))}</a>) '
                  f'disallows our crawler via robots.txt, so no pages were scanned. The pages '
                  f'are public (a browser or major search engine can read them), but we respect '
                  f'robots by default. This is a coverage gap, not a finding — and a candidate '
                  f'site that blocks automated access can be added to the per-site override list '
                  f'after review.</p></section>')
    elif v.status == "fetch_failed":
        detail = (f'<section class="detection"><h2 class="section-head section-head-quiet">'
                  f'Site unreachable — fetch failed</h2>'
                  f'<p class="rationale">We could not fetch any page from the resolved '
                  f'site (<a href="{_h(c.get("website_url"))}" rel="nofollow noopener">{_h(c.get("website_url"))}</a>) '
                  f'— it may be down, parked, moved, or the resolved URL may be wrong. '
                  f'No pages were scanned. This is a coverage gap, not a finding; the '
                  f'candidate can be re-scanned (the resolved URL is worth re-checking).'
                  f'</p></section>')

    body = f"""
    <article class="article">
    <p class="crumb"><a href="index.html">&#8592; Back to the index</a></p>
    <p class="kicker">{_h(kicker_status)} <span class="redbox"></span> {_h(office)} &#183; {_h(state_name)} <span class="redbox"></span> {_h(party)}</p>
    <h1 class="headline headline-cand">{_h(_display_name(c.get('name')))} <span class="finding-tag {pill_cls}">{pill_txt}</span></h1>
    {banner}{meta}{_render_changes(v)}{detail}{_render_corroboration(v)}
    </article>"""

    prose = _name_prose(c.get("name"))
    seat_short = _seat_compact(c)
    race = _race_phrase(c)
    sup = float(v.corroboration.get("supporting_total") or 0) if v.corroboration else 0
    if v.status == "positive_published":
        title = f"Red-Boxing Detected: {prose} ({seat_short})"
        desc = (f"{prose}'s campaign website carries a red box — messaging cues "
                f"that tell super PACs what ads to run."
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


def _render_page_text(v: CandidateView) -> str:
    """Collapsed plain text of the archived page, from the archiver's extracted-
    text file. The screenshot can be obscured by a cookie banner or pop-up, and
    an image is opaque to screen readers — the text is the accessible record."""
    if not (v.archive and v.archive.get("text_path")):
        return ""
    try:
        txt = Path(v.archive["text_path"]).read_text(errors="replace").strip()
    except OSError:
        return ""
    if not txt:
        return ""
    return f"""<details class="pagetext">
      <summary>Plain text of the archived page</summary>
      <pre>{_h(txt)}</pre>
    </details>"""


_CHANGE_LABEL = {
    "take_down": ("Guidance removed", "Messaging guidance previously detected on this page was no longer present on re-scan."),
    "put_up": ("Guidance posted", "Messaging guidance appeared on this page that was not present on the prior scan."),
    "modified": ("Guidance changed", "Previously-detected guidance on this page changed between scans."),
}


def _render_changes(v: CandidateView) -> str:
    if not v.changes:
        return ""
    items = []
    for ch in v.changes:
        title, desc = _CHANGE_LABEL.get(ch["event_type"], (ch["event_type"], ""))
        cls = "chg-down" if ch["event_type"] == "take_down" else (
            "chg-up" if ch["event_type"] == "put_up" else "chg-mod")
        items.append(
            f'<li class="{cls}"><span class="chg-when">{_h((ch.get("detected_at") or "")[:10])}</span>'
            f'<strong>{_h(title)}</strong> — {_h(desc)} '
            f'<span class="chg-url">{_h(ch.get("url"))}</span></li>')
    return f"""
    <section class="changes">
      <h2 class="section-head"><span class="redbox"></span>Change history</h2>
      <p class="srcline">Put-up / take-down events detected across re-scans. A box
         removed after it draws attention is itself a recorded signal — the archived
         evidence is preserved regardless.</p>
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
    human reviewer before any finding is published.</strong></p>
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
                   desc=("How Red Box Watch detects red-boxing: polite browser crawls "
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
                   desc=("How to request a correction or appeal a Red Box Watch "
                         "finding. Every published item links to archived evidence; "
                         "corrections are logged and dated."))


def _render_about() -> str:
    body = """
    <article class="article">
    <p class="kicker">About <span class="redbox"></span> Who runs this</p>
    <h1 class="headline">Built by a volunteer who found the box.</h1>
    <p class="rationale">Red Box Watch is an independent, one-person monitoring project. It crawls the
    public websites of federal candidates nationwide, detects red-boxing &#8212; message
    guidance posted in plain sight for the super PACs that are barred from coordinating
    with campaigns directly &#8212; and publishes the evidence: the archived page, the exact
    quoted spans, and the aligned outside spending that followed.</p>
    <h2 class="section-head"><span class="redbox"></span>Why it exists</h2>
    <p class="rationale">The site is built and maintained by <strong>Charlie Garfield</strong>, who spent 2026
    as a campaign fellow in New York&#8217;s 12th District &#8212; one of the most
    expensive House primaries ever run, absorbing more than $20&nbsp;million. Told to
    downplay the campaign&#8217;s connection to outside money, he went looking &#8212;
    and found the campaign&#8217;s own red box.</p>
    <p class="rationale">That page is what this site exists to surface. Voters deserve to know exactly how
    involved a candidate is with the outside money in their local race &#8212; not because
    red-boxing is unlawful (it is not), but because it is public, deliberate, and easy to
    miss unless someone points at it.</p>
    <h2 class="section-head"><span class="redbox"></span>Who&#8217;s behind it</h2>
    <p class="rationale">Charlie is 22 and a recent college graduate. He believes technology can make
    democracy more transparent. Red Box Watch is unaffiliated with any campaign, party,
    or PAC.</p>
    <h2 class="section-head"><span class="redbox"></span>Contact</h2>
    <p class="rationale">Press and media inquiries:
    <a class="px-mail" href="#">press&nbsp;[at]&nbsp;redboxwatch&nbsp;[dot]&nbsp;org</a>.
    Candidates or representatives disputing a finding:
    <a href="corrections.html">corrections &amp; appeals</a>.</p>
    </article>"""
    return _layout("About", body, page_class="page-finding", active="about",
                   path="about",
                   desc=("Who runs Red Box Watch and why: an independent project by "
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
    simply be over. The <a href="/index.html">Candidate Index</a> is current;
    our <a href="/methodology.html">methodology</a> explains what we track,
    and if you believe this page <em>should</em> exist,
    <a href="/corrections.html">corrections &amp; appeals</a> is that way.</p>
    </article>"""
    return _layout("Page not found", body, page_class="page-finding", root="/")


INDEX_JS = """
const q=document.getElementById('q'),fs=document.getElementById('f-status'),
  fst=document.getElementById('f-state'),tb=document.getElementById('rows'),
  pager=document.querySelector('.pager'),pageRows=[...tb.querySelectorAll('tr')];
let allRows=null,fetchStarted=false,shown=pageRows;
function loadAll(){
  // Full row set (every page) fetched once, on first filter use. If the fetch
  // fails (e.g. file:// preview), filtering quietly stays page-local.
  if(fetchStarted||!PAGED)return;fetchStarted=true;
  fetch('index-data.json').then(r=>r.ok?r.json():Promise.reject())
    .then(d=>{const t=document.createElement('template');t.innerHTML=d.html;
      allRows=[...t.content.querySelectorAll('tr')];apply();})
    .catch(()=>{});}
function apply(){const t=q.value.trim().toLowerCase(),s=fs.value,st=fst.value,
    active=!!(t||s||st);
  if(active)loadAll();
  const src=active&&allRows?allRows:pageRows;
  if(shown!==src){shown=src;tb.replaceChildren(...src);}
  shown.forEach(r=>{const name=r.children[0].textContent.toLowerCase();
    const ok=(!t||name.includes(t))&&(!s||r.dataset.status===s)&&(!st||r.dataset.state===st);
    r.style.display=ok?'':'none';});
  if(pager)pager.style.display=active&&allRows?'none':'';}
[q,fs,fst].forEach(e=>e.addEventListener('input',apply));
document.querySelectorAll('th[data-sort]').forEach(th=>th.addEventListener('click',()=>{
  const i=+th.dataset.sort,tb=document.getElementById('rows');
  const sorted=[...tb.querySelectorAll('tr')].sort((a,b)=>{
    const x=a.children[i].textContent.trim(),y=b.children[i].textContent.trim();
    const nx=parseFloat(x.replace(/[$,]/g,'')),ny=parseFloat(y.replace(/[$,]/g,''));
    if(!isNaN(nx)||!isNaN(ny)){return(isNaN(ny)?-Infinity:ny)-(isNaN(nx)?-Infinity:nx);}
    return x.localeCompare(y);});
  sorted.forEach(r=>tb.appendChild(r));}));
document.querySelectorAll('.st.has-pop').forEach(el=>el.addEventListener('click',e=>{
  if(e.target.closest('a'))return;
  fst.value=el.dataset.state;apply();
  document.querySelector('.agate').scrollIntoView({behavior:'smooth'});}));
"""

CSS = """
/* BROADSHEET — editorial investigations-desk aesthetic. Warm paper, ink, one
   decisive red; Fraunces / Source Serif 4 / Libre Franklin. The red box is the
   brand mark. Heatmap grafted from the Sunlight concept. */
:root{
  --paper:#faf8f2;--paper-bright:#fffdf8;--ink:#1c1712;--ink-soft:#57503f;
  --ink-faint:#8d8471;--red:#b93425;--red-deep:#8e2417;--amber:#9a6b1f;
  --hair:rgba(28,23,18,.16);--hair-mid:rgba(28,23,18,.34);--hair-strong:rgba(28,23,18,.75);
  --red-h1:#f4ded8;--red-h2:#dc9a8b;--red-h3:#c9604c;--red-h4:#b93425;
  --display:"Fraunces","Iowan Old Style","Times New Roman",serif;
  --text:"Source Serif 4",Georgia,serif;
  --grot:"Libre Franklin","Helvetica Neue",Arial,sans-serif;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--text);font-size:1.0625rem;line-height:1.65;font-optical-sizing:auto}
.wrap{max-width:68rem;margin:0 auto;padding:0 2rem}
a{color:inherit;text-decoration-color:var(--hair-mid);text-underline-offset:3px}
a:hover{color:var(--red-deep);text-decoration-color:var(--red)}
::selection{background:var(--red);color:var(--paper-bright)}
code{font-size:.85em;background:var(--paper-bright);border:1px solid var(--hair);padding:.05em .35em}

/* the brand mark: a literal red box */
.redbox{display:inline-block;width:.52em;height:.52em;background:var(--red);vertical-align:.06em}

/* masthead */
.masthead{padding-top:1.1rem}
.folio{display:flex;justify-content:space-between;align-items:baseline;gap:1.5rem;border-bottom:1px solid var(--hair);padding-bottom:.6rem;font-family:var(--grot);font-size:.6875rem;letter-spacing:.14em;text-transform:uppercase;color:var(--ink-soft)}
.folio nav{display:flex;gap:1.75rem}
.folio nav a{text-decoration:none;color:var(--ink-soft)}
.folio nav a:hover{color:var(--red-deep)}
.folio nav a[aria-current="page"]{color:var(--ink);font-weight:700;border-bottom:2px solid var(--red);padding-bottom:2px}
.nameplate{text-align:center;padding:2.1rem 2rem 1.6rem}
.brand{font-family:var(--display);font-size:clamp(2rem,5vw,3.1rem);font-weight:620;font-variation-settings:"opsz" 144,"WONK" 1;letter-spacing:-.01em;line-height:1;text-decoration:none;color:var(--ink);white-space:nowrap}
.brand:hover{color:var(--ink)}
.brand .redbox{width:.42em;height:.42em;margin-right:.34em;vertical-align:.05em}
.brand:hover .redbox{background:var(--red-deep)}
.tagline{margin:.75rem 0 0;font-family:var(--grot);font-size:.72rem;letter-spacing:.22em;text-transform:uppercase;color:var(--ink-soft)}
.double-rule{border-top:3px solid var(--ink)}
.double-rule::after{content:"";display:block;border-top:1px solid var(--ink);margin-top:2px}

/* editorial furniture */
.kicker{margin:0 0 1.1rem;font-family:var(--grot);font-size:.72rem;font-weight:600;letter-spacing:.2em;text-transform:uppercase;color:var(--ink-soft);display:flex;align-items:center;gap:.7em;flex-wrap:wrap}
.kicker .redbox{width:.45em;height:.45em}
.kicker .k-hot{color:var(--red-deep);font-weight:700}
.headline{margin:0;font-family:var(--display);font-weight:540;font-variation-settings:"opsz" 144,"WONK" 1;font-size:clamp(2.5rem,6.4vw,4.15rem);line-height:1.01;letter-spacing:-.018em;text-wrap:balance;max-width:20ch}
.headline-cand{font-size:clamp(2.1rem,5.4vw,3.4rem);max-width:none}
.deck{margin:1.35rem 0 0;font-family:var(--display);font-weight:400;font-style:italic;font-variation-settings:"opsz" 34;font-size:clamp(1.15rem,2.4vw,1.45rem);line-height:1.45;color:var(--ink-soft);max-width:34em;text-wrap:pretty}
.byline-row{margin:1.8rem 0 0;padding:.65rem 0;border-top:1px solid var(--hair);border-bottom:1px solid var(--hair);font-family:var(--grot);font-size:.72rem;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-soft);display:flex;gap:.7em 1.4em;flex-wrap:wrap;align-items:baseline}
.byline-row strong{color:var(--ink);font-weight:700}
.byline-row .sep{color:var(--red)}
.dropcap::first-letter{font-family:var(--display);font-weight:800;font-variation-settings:"opsz" 144;float:left;font-size:3.7em;line-height:.78;padding:.06em .12em 0 0;color:var(--ink)}
.section-head{margin:0 0 .4rem;font-family:var(--display);font-weight:590;font-variation-settings:"opsz" 72;font-size:1.6rem;line-height:1.18;letter-spacing:-.008em;text-wrap:balance}
.section-head .redbox{width:.42em;height:.42em;margin-right:.5em;vertical-align:.08em}
.section-head-quiet{color:var(--ink-soft)}
.article .section-head{margin-top:2.6rem}
.srcline{margin:.35rem 0 0;font-family:var(--grot);font-size:.72rem;letter-spacing:.07em;text-transform:uppercase;color:var(--ink-faint);line-height:1.9}
.srcline a{color:var(--ink-soft);text-transform:none;letter-spacing:.01em;overflow-wrap:anywhere}
.lbl{margin:0;font-family:var(--grot);font-size:.68rem;font-weight:700;letter-spacing:.18em;text-transform:uppercase;color:var(--ink)}

/* notices */
.review-banner{margin:1.6rem 0 0;padding:1rem 1.3rem;border-top:1px solid var(--amber);border-bottom:1px solid var(--amber);font-style:italic;font-size:.97rem;color:var(--ink-soft)}
.ended-banner{margin:1.6rem 0 0;padding:1rem 1.3rem;border-top:1px solid var(--line);border-bottom:1px solid var(--line);font-style:italic;font-size:.97rem;color:var(--ink-soft)}
.review-banner strong{font-style:normal;color:var(--amber)}
.coverage{margin:1.4rem 0 0;font-family:var(--grot);font-size:.8rem;line-height:1.75;color:var(--ink-soft)}
.coverage strong{color:var(--ink)}

/* index page */
.page-index{padding:3.2rem 2rem 0}
.lede-grid{display:grid;grid-template-columns:1.35fr 1fr;gap:0 3rem;margin-top:2.4rem}
.lede-copy{padding-right:3rem;border-right:1px solid var(--hair)}
.standfirst{margin:0;font-size:1.14rem;line-height:1.72;text-wrap:pretty}
.standfirst+p{margin:1.1rem 0 0;color:var(--ink-soft);font-size:.98rem}
.standfirst strong,.lede-copy strong{font-weight:640}
.stat-deck{display:flex;flex-direction:column;justify-content:center}
.stat{padding:.9rem 0;border-bottom:1px solid var(--hair);display:flex;align-items:baseline;gap:1rem}
.stat:first-child{padding-top:.2rem}
.stat:last-child{border-bottom:none}
.stat b{font-family:var(--display);font-weight:620;font-variation-settings:"opsz" 144;font-size:2.55rem;line-height:1;letter-spacing:-.02em;font-feature-settings:"tnum";min-width:4.6ch}
.stat.stat-hot b{color:var(--red)}
.stat.stat-warm b{color:var(--amber)}
.stat span{font-family:var(--grot);font-size:.7rem;font-weight:600;letter-spacing:.16em;text-transform:uppercase;color:var(--ink-soft);line-height:1.5}

/* findings-by-state heatmap (from the Sunlight concept, re-inked) */
.map-band{display:grid;grid-template-columns:1.4fr 1fr;gap:0 3rem;margin-top:3rem;padding-top:1.4rem;border-top:2px solid var(--ink)}
.map-cell{min-width:0}
.statemap{margin-top:1.1rem;display:grid;grid-template-columns:repeat(11,minmax(0,1fr));grid-auto-rows:1fr;gap:4px;max-width:44rem}
.st{aspect-ratio:1/1;position:relative;outline:1px solid var(--hair);outline-offset:-1px;font-family:var(--grot);font-size:clamp(8px,.95vw,11px);font-weight:600;letter-spacing:.04em;color:var(--ink-faint);padding:3px 0 0 4px}
.st .n{position:absolute;right:4px;bottom:3px;font-size:.9em;font-feature-settings:"tnum";opacity:.9}
.st.h1{background:var(--red-h1);outline-color:var(--red-h1);color:var(--red-deep)}
.st.h2{background:var(--red-h2);outline-color:var(--red-h2);color:#fff}
.st.h3{background:var(--red-h3);outline-color:var(--red-h3);color:#fff}
.st.h4{background:var(--red-h4);outline-color:var(--red-h4);color:#fff}
.statemap .st:hover,.statemap .st:focus-visible{outline:2px solid var(--ink);outline-offset:-2px;z-index:3}
.st.has-pop{cursor:pointer}
.st.has-pop:hover,.st.has-pop:focus-within{z-index:4}
/* hover popup: the state's finding candidates */
.pop{position:absolute;left:-1px;bottom:calc(100% + 2px);width:19rem;max-height:19rem;overflow-y:auto;
  padding:.8rem .95rem .7rem;background:var(--paper-bright);border:1px solid var(--hair-strong);
  box-shadow:0 2px 0 var(--hair),0 10px 26px rgba(28,23,18,.14);cursor:default;
  visibility:hidden;opacity:0;transform:translateY(4px);transition:opacity .12s ease,transform .12s ease,visibility .12s;
  font-family:var(--grot);text-transform:none;letter-spacing:normal;font-weight:400}
.pop::after{content:"";position:absolute;left:0;right:0;bottom:-8px;height:8px}
.pop-below{bottom:auto;top:calc(100% + 2px);transform:translateY(-4px)}
.pop-below::after{bottom:auto;top:-8px}
.pop-right{left:auto;right:-1px}
.st:hover .pop,.st:focus-within .pop{visibility:visible;opacity:1;transform:translateY(0)}
.pop-head{margin:0;font-size:.66rem;font-weight:700;letter-spacing:.16em;text-transform:uppercase;color:var(--ink);
  display:flex;align-items:center;gap:.55em;padding-bottom:.5rem;border-bottom:1px solid var(--hair-strong)}
.pop-head .redbox{width:.6em;height:.6em}
.pop-list{list-style:none;margin:0;padding:0}
.pop-list li{display:flex;align-items:baseline;gap:.7em;padding:.42rem 0;border-bottom:1px solid var(--hair);font-size:.78rem;line-height:1.4}
.pop-list li:last-child{border-bottom:none}
.pop-list a{font-weight:600;color:var(--ink);text-decoration:none;min-width:0}
.pop-list a:hover{color:var(--red-deep);text-decoration:underline;text-decoration-color:var(--red)}
.pop-seat{color:var(--ink-faint);font-size:.68rem;letter-spacing:.05em;white-space:nowrap}
.pop-ie{margin-left:auto;font-weight:600;font-size:.72rem;font-feature-settings:"tnum";color:var(--red-deep);white-space:nowrap}
.pop-foot{margin:.55rem 0 0;font-family:var(--text);font-style:italic;font-size:.72rem;color:var(--ink-faint)}
.map-aside{display:flex;flex-direction:column;gap:1rem;justify-content:center;font-family:var(--grot)}
.map-note{margin:.4rem 0 0;font-size:.8rem;line-height:1.7;color:var(--ink-soft)}
.map-legend{display:flex;align-items:center;gap:1rem;flex-wrap:wrap}
.map-legend .key{display:inline-flex;align-items:center;gap:.45em;font-size:.68rem;letter-spacing:.1em;color:var(--ink-soft)}
.map-legend .key i{width:.85em;height:.85em;outline:1px solid var(--hair-mid);outline-offset:-1px}
.map-legend .k1 i{background:var(--red-h1);outline:none}
.map-legend .k2 i{background:var(--red-h2);outline:none}
.map-legend .k3 i{background:var(--red-h3);outline:none}
.map-legend .k4 i{background:var(--red-h4);outline:none}
.map-offgrid{margin:0;font-size:.74rem;color:var(--ink-soft)}
.map-count{margin:0;font-size:.68rem;letter-spacing:.14em;text-transform:uppercase;color:var(--ink-faint);font-feature-settings:"tnum"}

/* filters — wire-desk control strip */
.controls{margin-top:3rem;padding:.9rem 0;border-top:2px solid var(--ink);border-bottom:1px solid var(--hair);display:flex;gap:.9rem;align-items:center;flex-wrap:wrap}
.controls label{font-family:var(--grot);font-size:.65rem;font-weight:700;letter-spacing:.18em;text-transform:uppercase;color:var(--ink-soft);margin-right:.2rem}
.controls input[type="search"],.controls select{appearance:none;-webkit-appearance:none;font-family:var(--grot);font-size:.8125rem;color:var(--ink);background:var(--paper-bright);border:1px solid var(--hair-mid);border-radius:0;padding:.5rem .8rem}
.controls input[type="search"]{flex:1 1 14rem}
.controls input[type="search"]::placeholder{color:var(--ink-faint);font-style:italic;font-family:var(--text)}
.controls select{padding-right:2rem;background-image:linear-gradient(45deg,transparent 49%,var(--ink) 51%),linear-gradient(135deg,var(--ink) 49%,transparent 51%);background-position:right 1.05rem top 55%,right .75rem top 55%;background-size:.3rem .3rem;background-repeat:no-repeat}
.controls input:focus,.controls select:focus{outline:none;border-color:var(--red);box-shadow:0 1px 0 var(--red)}
.controls .paged-note{flex-basis:100%;margin:.15rem 0 0;font-family:var(--text);font-style:italic;font-size:.85rem;color:var(--ink-faint)}

/* the agate table */
.agate{width:100%;border-collapse:collapse;font-family:var(--grot);font-size:.84rem;font-feature-settings:"tnum";font-variant-numeric:tabular-nums}
.agate thead th{font-size:.65rem;font-weight:700;letter-spacing:.13em;text-transform:uppercase;color:var(--ink);text-align:left;padding:.8rem .7rem .55rem;border-bottom:1px solid var(--hair-strong);white-space:nowrap;cursor:pointer;user-select:none}
.agate td{padding:.58rem .7rem;border-bottom:1px solid var(--hair);vertical-align:baseline}
.agate .num,.agate th.num{text-align:right}
.agate td.cand{font-weight:600;letter-spacing:.015em}
.agate td.cand a{text-decoration:none}
.agate td.cand a:hover{text-decoration:underline;text-decoration-color:var(--red);color:var(--red-deep)}
.agate td.seat,.agate td.party{color:var(--ink-soft);font-size:.78rem;letter-spacing:.04em}
.src-tag{font-size:.6rem;font-weight:700;letter-spacing:.12em;color:var(--ink-faint);vertical-align:.08em;margin-left:.15em}
.agate .ie{font-weight:600}
.agate .conf,.agate .pages{color:var(--ink-soft);font-size:.8rem}

/* status marks — small squares echoing the brand */
.status{font-size:.66rem;font-weight:700;letter-spacing:.11em;text-transform:uppercase;white-space:nowrap}
.status::before{content:"";display:inline-block;width:.75em;height:.75em;margin-right:.6em;vertical-align:-.02em}
.status.pill-pos{color:var(--red-deep)}
.status.pill-pos::before{background:var(--red)}
.status.pill-pending,.status.pill-amb{color:var(--amber)}
.status.pill-pending::before,.status.pill-amb::before{outline:1px solid var(--amber);outline-offset:-1px}
.status.pill-neg{color:var(--ink-faint)}
.status.pill-neg::before{outline:1px solid var(--hair-mid);outline-offset:-1px}
.status.pill-muted{color:var(--ink-faint)}
.status.pill-muted::before{outline:1px dashed var(--hair-mid);outline-offset:-1px}

/* pager */
.pager{display:flex;align-items:baseline;gap:1.1rem;flex-wrap:wrap;margin:1.6rem 0 0;font-family:var(--grot);font-size:.72rem;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:var(--ink-soft)}
.pager a{text-decoration:none;color:var(--ink-soft)}
.pager a:hover{color:var(--red-deep)}
.pager .pg-nums{display:flex;gap:.8em;flex-wrap:wrap}
.pager .pg-nums strong{color:var(--red-deep);border-bottom:2px solid var(--red)}
.pager .pg-off{color:var(--hair-mid)}
.pager .pg-of{color:var(--ink-faint);margin-left:auto;font-feature-settings:"tnum"}

/* candidate / article pages */
.page-finding{padding:2.6rem 2rem 0}
.crumb{margin:0 0 2.2rem;font-family:var(--grot);font-size:.72rem;font-weight:600;letter-spacing:.14em;text-transform:uppercase}
.crumb a{text-decoration:none;color:var(--ink-soft)}
.crumb a:hover{color:var(--red-deep)}
.article{max-width:46.5rem;margin:0 auto}
.finding-tag{display:inline-flex;align-items:center;gap:.55em;font-family:var(--grot);font-size:.68rem;font-weight:700;letter-spacing:.2em;text-transform:uppercase;padding:.4em .85em;margin-left:.35rem;vertical-align:.55em;white-space:nowrap}
.finding-tag.pill-pos{color:var(--red-deep);border:1px solid var(--red)}
.finding-tag.pill-pos::before{content:"";width:.62em;height:.62em;background:var(--red)}
.finding-tag.pill-pending,.finding-tag.pill-amb{color:var(--amber);border:1px solid var(--amber)}
.finding-tag.pill-neg,.finding-tag.pill-muted{color:var(--ink-faint);border:1px solid var(--hair-mid)}
.meta{margin:2.1rem 0 0;border-top:2px solid var(--ink);font-family:var(--grot);font-size:.85rem;display:grid;grid-template-columns:10.5rem 1fr}
.meta dt{padding:.55rem .5rem .55rem 0;font-size:.65rem;font-weight:700;letter-spacing:.15em;text-transform:uppercase;color:var(--ink-soft);border-bottom:1px solid var(--hair)}
.meta dd{margin:0;padding:.55rem 0;border-bottom:1px solid var(--hair);font-feature-settings:"tnum";overflow-wrap:anywhere}
.meta dd a{text-decoration-color:var(--hair-mid)}
.detection{margin-top:3.6rem}
.rationale{margin:1.7rem 0 0;font-size:1.1rem;line-height:1.72;text-wrap:pretty}
.evidence-head{margin:3rem 0 .4rem;font-family:var(--grot);font-size:.7rem;font-weight:700;letter-spacing:.2em;text-transform:uppercase;color:var(--ink);display:flex;align-items:center;gap:.7em}
.evidence-head::after{content:"";flex:1;border-top:1px solid var(--hair)}
.evidence-head .redbox{width:.55em;height:.55em}
.evidence{list-style:none;margin:0;padding:0}
.evidence li{margin:2.4rem 0 0;padding-left:1.7rem;border-left:3px solid var(--red)}
.evidence blockquote{margin:0;position:relative;font-family:var(--text);font-style:italic;font-weight:440;font-size:1.32rem;line-height:1.5;letter-spacing:-.004em;text-wrap:pretty}
.evidence blockquote::before{content:"\\201C";position:absolute;left:-.62em;top:-.28em;font-family:var(--display);font-weight:800;font-style:normal;font-variation-settings:"opsz" 144;font-size:2.2em;line-height:1;color:var(--red);background:var(--paper);padding-bottom:.05em}
.evidence .why{display:block;margin-top:.8rem;font-family:var(--grot);font-size:.68rem;font-weight:600;letter-spacing:.13em;text-transform:uppercase;line-height:1.8;color:var(--ink-soft)}
.evidence .why::before{content:"\\2014\\2002";color:var(--red)}
.exhibit{margin:3.4rem 0 0}
.exhibit-frame{display:block;background:var(--paper-bright);border:1px solid var(--hair-mid);padding:.65rem;box-shadow:0 1px 0 var(--hair)}
.exhibit-frame img{display:block;width:100%;height:auto}
.exhibit figcaption{margin-top:.75rem;font-family:var(--grot);font-size:.74rem;line-height:1.75;color:var(--ink-soft);overflow-wrap:anywhere}
.exhibit-label{font-weight:700;font-size:.65rem;letter-spacing:.18em;text-transform:uppercase;color:var(--red-deep);margin-right:.6em}
.exhibit-label::before{content:"";display:inline-block;width:.55em;height:.55em;background:var(--red);margin-right:.55em}
.pagetext{margin:1.8rem 0 0}
.pagetext summary{cursor:pointer;font-family:var(--grot);font-size:.7rem;font-weight:700;letter-spacing:.18em;text-transform:uppercase;color:var(--red-deep)}
.pagetext summary::marker{color:var(--red)}
.pagetext[open] summary{margin-bottom:.7rem}
.pagetext pre{margin:0;max-height:460px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;background:var(--paper-bright);border:2px solid var(--red);padding:1rem 1.2rem;font-size:.82rem;line-height:1.65}
.evlinks{margin:1.6rem 0 0;font-family:var(--grot);font-size:.78rem;color:var(--ink-soft)}
.corroboration{margin-top:4.2rem}
.sequence{margin:1.6rem 0 0;font-size:1.06rem;line-height:1.7;text-wrap:pretty}
.sequence strong{font-weight:700;font-feature-settings:"tnum"}
.sequence .money-hot{color:var(--red-deep)}
.ie-table{margin-top:1.8rem;font-size:.82rem}
.ie-table td.committee{font-weight:600;letter-spacing:.02em}
.ie-table td.range{color:var(--ink-soft);font-size:.76rem;white-space:nowrap}
.ind{display:inline-flex;align-items:center;justify-content:center;width:1.35em;height:1.35em;font-size:.66rem;font-weight:700;line-height:1;font-family:var(--grot)}
.ind-S{color:var(--paper-bright);background:var(--red)}
.ind-O{color:var(--ink-soft);background:transparent;box-shadow:inset 0 0 0 1px var(--hair-mid)}
.legend{margin:.7rem 0 0;font-family:var(--grot);font-size:.68rem;letter-spacing:.1em;text-transform:uppercase;color:var(--ink-faint)}
.caveat{margin:1.8rem 0 0;padding:1.1rem 1.4rem;border-top:1px solid var(--hair);border-bottom:1px solid var(--hair);font-style:italic;font-size:.94rem;line-height:1.7;color:var(--ink-soft)}
.caveat strong{font-style:normal;color:var(--ink);font-feature-settings:"tnum"}

/* change history */
.changes{margin-top:3.6rem}
.changelog{list-style:none;margin:1.4rem 0 0;padding:0;font-family:var(--grot);font-size:.85rem}
.changelog li{padding:.7rem 0;border-bottom:1px solid var(--hair);line-height:1.7}
.changelog li:first-child{border-top:2px solid var(--ink)}
.chg-when{display:inline-block;min-width:6.5em;color:var(--ink-faint);font-feature-settings:"tnum";font-size:.78rem;letter-spacing:.06em}
.changelog li.chg-down strong{color:var(--red-deep)}
.changelog li.chg-up strong{color:var(--amber)}
.chg-url{color:var(--ink-faint);font-size:.76rem;overflow-wrap:anywhere}

/* standards list (corrections page) */
.standards{margin:1.7rem 0 0;padding-left:1.2rem;font-size:1.02rem;line-height:1.72}
.standards li{margin:.8rem 0;padding-left:.4rem}
.standards li::marker{content:"\\25A0\\2002";color:var(--red);font-size:.6em}

/* 404 — the one red box with nothing in it */
.nf-box{display:flex;align-items:center;justify-content:center;max-width:26rem;height:9.5rem;margin:2.4rem 0;border:3px dashed var(--red);padding:1rem}
.nf-box span{font-family:var(--grot);font-size:.72rem;letter-spacing:.14em;text-transform:uppercase;color:var(--ink-faint)}

/* footer */
.site-foot{margin-top:5rem;padding-bottom:3rem}
.site-foot .double-rule{margin-bottom:1.4rem}
.site-foot p{margin:0;font-family:var(--grot);font-size:.76rem;line-height:1.8;color:var(--ink-soft);max-width:56rem}
.site-foot strong{color:var(--ink)}
.site-foot .foot-mark{margin-bottom:.9rem}
.site-foot .foot-mark .redbox{width:.6em;height:.6em}

/* responsive */
@media (max-width:860px){
  .lede-grid{grid-template-columns:1fr;gap:2.4rem 0}
  .lede-copy{padding-right:0;border-right:none}
  .stat-deck{border-top:1px solid var(--hair)}
  .map-band{grid-template-columns:1fr;gap:1.6rem 0}
  .agate .pages,.agate th:nth-child(7){display:none}
}
@media (max-width:640px){
  .wrap,.page-index,.page-finding{padding-left:1.1rem;padding-right:1.1rem}
  .folio{flex-direction:column;gap:.5rem;align-items:flex-start}
  .meta{grid-template-columns:8rem 1fr}
  .agate .conf,.agate th:nth-child(5){display:none}
  .evidence li{padding-left:1.1rem}
  .evidence blockquote::before{display:none}
  /* stat deck: 4-digit counts and $-figures at full display size crowd the
     labels on narrow screens — step the numerals down and loosen the row */
  .stat{padding:.7rem 0;gap:.85rem}
  .stat b{font-size:1.9rem;min-width:3.8ch}
  .stat span{font-size:.64rem;letter-spacing:.12em}
}
"""
