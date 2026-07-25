"""Small shared helpers: UTC timestamps and content hashing."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone


def now_iso() -> str:
    """Current time as an ISO-8601 UTC string (second precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_text(text: str) -> str:
    """Stable sha256 of text content (used for change detection / dedup)."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
