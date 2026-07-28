"""Small shared helpers: UTC timestamps, content hashing, party codes."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone


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


def norm_party(p: str | None) -> str:
    """THE canonical party code for race bucketing and comparison.

    Used by discovery grouping, nominee resolution, and the results-feed
    crosswalk — all three must agree or same-primary candidates split into
    parallel buckets (three divergent normalizers once put Minnesota's DFL
    filers in a different "primary" than their DEM opponents, crowning
    contested candidates as sole presumptive nominees).

    Folds FEC state-affiliate codes into the national party (MN
    Democratic-Farmer-Labor, ND Democratic-NPL — those candidates run in the
    same primary as DEM filers) and long/one-letter forms ('Democratic', 'D',
    'GOP', 'R') into 3-letter codes; anything else truncates to 3 letters.
    """
    s = (p or "").strip().upper()
    if s in ("DFL", "DNL"):
        return "DEM"
    if s.startswith("DEM") or s == "D":
        return "DEM"
    if s.startswith("REP") or s.startswith("GOP") or s == "R":
        return "REP"
    return s[:3]


def now_iso() -> str:
    """Current time as an ISO-8601 UTC string (second precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_text(text: str) -> str:
    """Stable sha256 of text content (used for change detection / dedup)."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
