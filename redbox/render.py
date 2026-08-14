"""Shared presentation pieces for the static site and the review console.

Holds what both surfaces (publisher.py's public/review build and reviewweb.py's
live console) need: the §3.7a legal-safety labels, HTML escaping, the font
stack, the static assets (stylesheet / index script / favicons, loaded once
from ``redbox/assets/``), and the single STATUSES table every status-keyed
consumer derives from.

publisher.py re-exports these names, so existing importers
(``from redbox.publisher import CSS, _h, ...``) keep working unchanged.
"""
from __future__ import annotations

import html
from dataclasses import dataclass
from importlib.resources import files

from .util import sha256_text

# ---------------------------------------------------------------------------
# §3.7a labels — the exact published wording; never assert illegality.
POSITIVE_LABEL = "Posted public messaging guidance consistent with red-boxing"
AMBIGUOUS_LABEL = "Possible messaging guidance — under review"
# An ambiguous detection a human reviewer then approved: publishable, with the
# classifier's initial hesitation disclosed rather than hidden.
AMBIGUOUS_CONFIRMED_LABEL = ("Posted public messaging guidance consistent with "
                             "red-boxing — initially classified ambiguous by the "
                             "automated screen; confirmed on human review")


def _neg_label(date: str) -> str:
    return f"No public messaging guidance detected as of {date[:10]}"


def _h(s) -> str:
    return html.escape(str(s if s is not None else ""))


_FONTS = """<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,300..900;1,9..144,300..900&family=Libre+Franklin:ital,wght@0,300..700;1,300..700&family=Source+Serif+4:ital,opsz,wght@0,8..60,300..700;1,8..60,300..700&display=swap" rel="stylesheet">"""

# ---------------------------------------------------------------------------
# Static assets, read once at import time from redbox/assets/. The project
# runs from source (no wheel/package-data step), so importlib.resources
# resolves straight to the files on disk.
_ASSETS = files("redbox") / "assets"

CSS = (_ASSETS / "site.css").read_text()
INDEX_JS = (_ASSETS / "index.js").read_text()
# Favicons: the brand's red box as the tab icon (see the design note in
# publisher.py's rendering section). SVG for modern browsers; PNG fallbacks
# (48px for Google search results, 180px apple-touch) because Safari ignores
# SVG favicons; a 16/32/48 favicon.ico for crawlers that request /favicon.ico
# directly (Cloudflare Pages otherwise soft-404s it with the HTML fallback).
FAVICON_SVG = (_ASSETS / "favicon.svg").read_text()
FAVICON_PNG48 = (_ASSETS / "favicon.png").read_bytes()
FAVICON_ICO = (_ASSETS / "favicon.ico").read_bytes()
FAVICON_PNG180 = (_ASSETS / "apple-touch-icon.png").read_bytes()

# Content hash for cache-busting the stylesheet URL. Pages serves styles.css
# with max-age=14400, so without this a deploy leaves visitors on 4-hour-stale
# CSS while the HTML is already new. Constant per process: the stylesheet is
# fixed at import time, so hashing it per-layout-call was pure waste.
CSS_VERSION = sha256_text(CSS)[:8]


# ---------------------------------------------------------------------------
# The one status table. Every status-keyed consumer — index sort rank, status
# pill, the index filter's <option> list, the public-build allowlist, the
# not-scanned row labels, and the coverage-gap page sections — derives from
# this dict, so a status can no longer exist in one surface and drift or go
# missing in another (the filter once offered options no public build could
# ever match).
@dataclass(frozen=True)
class StatusSpec:
    rank: int              # index sort order within a band (0 = leads)
    pill: str              # index/status pill text
    pill_class: str        # pill CSS class
    filter_label: str      # index status-filter <option> text
    public: bool           # rendered in --approved-only (public) builds
    # For the never-scanned buckets: the row label (also the candidate-page
    # section heading) and the page's rationale paragraph. ``{site}`` in the
    # body is replaced with a link to the resolved site.
    row_label: str | None = None
    gap_body: str | None = None


# Insertion order is the filter dropdown's display order.
STATUSES: dict[str, StatusSpec] = {
    "positive_published": StatusSpec(0, "FINDING", "pill-pos", "Findings", True),
    "positive_pending": StatusSpec(1, "PENDING REVIEW", "pill-pending",
                                   "Pending (red-box)", False),
    "ambiguous_pending": StatusSpec(2, "PENDING REVIEW", "pill-amb",
                                    "Pending (ambiguous)", False),
    "negative": StatusSpec(4, "NONE DETECTED", "pill-neg", "None detected", True),
    "rejected": StatusSpec(3, "NOT A FINDING", "pill-neg", "Not a finding", False),
    "not_scanned": StatusSpec(8, "NOT SCANNED", "pill-muted", "Not scanned", True,
                              row_label="Not yet scanned"),
    "no_url": StatusSpec(
        7, "NO SITE FOUND", "pill-muted", "No site found", True,
        row_label="No campaign site found — not scanned",
        gap_body=("No official campaign website could be resolved for "
                  "this candidate (manual override, Wikipedia, FEC committee, and web search "
                  "all returned nothing), so no pages were scanned. This is a coverage gap, "
                  "not a finding of any kind — it does not indicate the presence or absence "
                  "of red-boxing.")),
    "blocked_by_robots": StatusSpec(
        6, "BLOCKED (ROBOTS)", "pill-muted", "Blocked by robots", True,
        row_label="Site blocks automated access — not scanned",
        gap_body=("The candidate's site ({site}) "
                  "disallows our crawler via robots.txt, so no pages were scanned. The pages "
                  "are public (a browser or major search engine can read them), but we respect "
                  "robots by default. This is a coverage gap, not a finding — and a candidate "
                  "site that blocks automated access can be added to the per-site override list "
                  "after review.")),
    "fetch_failed": StatusSpec(
        5, "FETCH FAILED", "pill-muted", "Fetch failed", True,
        row_label="Site unreachable — fetch failed",
        gap_body=("We could not fetch any page from the resolved "
                  "site ({site}) "
                  "— it may be down, parked, moved, or the resolved URL may be wrong. "
                  "No pages were scanned. This is a coverage gap, not a finding; the "
                  "candidate can be re-scanned (the resolved URL is worth re-checking).")),
}
