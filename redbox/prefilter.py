"""Cheap pre-filter to skip LLM classification on obviously-empty pages (cost/speed).

The classifier (LLM) is the per-page bottleneck and cost driver. Most campaign
pages (donate, volunteer, privacy, careers, 404s) contain zero red-box language
and never need an LLM call. This module decides, with a conservative rule, which
pages can skip the classifier.

**Recall is paramount — the LLM stays the source of truth.** The rule is
deliberately biased to over-scan:

  - ALWAYS scan media-kit-style pages (/media, /press, /messaging, ...) and any
    PDF — red boxes live there, so they bypass the filter unconditionally
    (even if their text looks empty on a given crawl).
  - SKIP a page only when it is BOTH a boilerplate URL (donate/privacy/...) AND
    has zero red-box lexical signal.
  - Otherwise SCAN (default). An ambiguous page always reaches the LLM.

Validated against every page we have actually scanned: 0 real red boxes skipped.
A gate false-positive (scanning an empty page anyway) is harmless — just a
wasted call. A gate false-negative (skipping a real box) is unacceptable, so the
rule never decides a positive; it only decides what is obviously nothing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Media-kit-style paths: red boxes live here -> always classify.
MEDIA_PATHS = (
    "/media", "/media-kit", "/mediakit", "/press", "/messaging", "/newsroom",
    "/news", "/resources", "/toolkit", "/kit", "/comms", "/communications",
)

# The crawler's high-priority paths: everything that always classifies, plus
# variants worth *fetching* eagerly even though they usually alias into one of
# the above. Kept HERE, next to MEDIA_PATHS, so the two lists can't silently
# drift apart again (they had: /supporter was crawled eagerly but not
# always-classified; /communications the reverse).
HIGH_VALUE_PATHS = MEDIA_PATHS + ("/supporter",)

# URL substrings that are essentially never a red box (and have a clear purpose).
BOILERPLATE_PATHS = (
    "/donate", "/contribute", "/chip-in", "/volunteer", "/privacy", "/terms",
    "/contact", "/events", "/event/", "/shop", "/store", "/merch", "/jobs",
    "/careers", "/unsubscribe", "/login", "/account", "/cart", "/checkout",
    "/thank", "/endorse", "/sign", "/petition", "/rsvp", "/host", "/refund",
)

# Functional red-box language. Recall-oriented: union of segmented-audience
# directives, channel/timing/geo cues, ad-asset and instruction phrasing.
_SIGNAL_PATTERNS = [
    r"\bvoters?\s+(?:need|should|must)\b",
    r"\bneed to (?:see|hear|read|know)\b",
    r"\bshould (?:see|hear|read|tell|emphasize|mention|feature|highlight|know)\b",
    r"\bon the go\b",
    r"\bin (?:their|the) mailbox",
    r"\bb-?roll\b",
    r"\bmedia market\b",
    r"\bpaid (?:communication|media|advertising|comms)\b",
    r"\bdirect mail\b",
    r"\b(younger|older|suburban|rural|likely|latino|latina) (?:primary )?voters?\b",
    r"\bcontrast (?:with|against|messaging)\b",
    r"\bmessaging guidance\b",
    r"\bclear and bold\b",
    r"\bmore to follow\b",
    r"\bfor (?:national|local) press release\b",
    r"\bvoters (?:age|under|over|likely)\b",
    r"\bon (?:the )?(?:tv|broadcast|cable|ctv|youtube|meta|streaming)\b",
    r"\bcouncilmanic\b",
    r"\bcrosstab",
]
_PATS = [re.compile(p, re.I) for p in _SIGNAL_PATTERNS]


@dataclass
class FilterDecision:
    scan: bool          # True -> send to the LLM classifier
    reason: str         # media_or_pdf | signal | default | boilerplate_empty
    score: int          # number of red-box signal patterns matched


def signal_score(text: str | None) -> int:
    """Count distinct red-box signal patterns present in the page text."""
    t = text or ""
    return sum(1 for p in _PATS if p.search(t))


def _is_pdf(url: str, content_type: str | None) -> bool:
    return url.lower().endswith(".pdf") or "pdf" in (content_type or "").lower()


def _is_media(url: str) -> bool:
    u = url.lower()
    return any(m in u for m in MEDIA_PATHS)


def _is_boilerplate(url: str) -> bool:
    u = url.lower()
    return any(b in u for b in BOILERPLATE_PATHS)


def decide(url: str, text: str | None, content_type: str | None = None) -> FilterDecision:
    """Conservative pre-filter decision (see module docstring).

    Skip ONLY when the URL is boilerplate AND the text carries no signal. Media
    paths and PDFs always scan; everything ambiguous scans by default.
    """
    if _is_media(url) or _is_pdf(url, content_type):
        return FilterDecision(scan=True, reason="media_or_pdf", score=signal_score(text))
    score = signal_score(text)
    if score > 0:
        return FilterDecision(scan=True, reason="signal", score=score)
    if _is_boilerplate(url):
        return FilterDecision(scan=False, reason="boilerplate_empty", score=0)
    return FilterDecision(scan=True, reason="default", score=0)
