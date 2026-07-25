"""Archiver — preserve evidence at detection time (spec §3.4).

For any page classified positive or ambiguous (and on first detection of
change), capture:
- full-page screenshot + raw HTML + extracted text, stored on disk with a
  content hash and timestamp. Playwright renders PNG; we transcode to WebP
  before writing (lossless WebP is typically ~70-90% smaller than PNG for
  text-heavy page captures, with no visible loss). If Pillow is unavailable we
  fall back to writing the original PNG, so evidence is never lost.
- a Wayback "Save Page Now" snapshot of the live URL, storing the snapshot URL.

Every published claim must link to evidence that survives a take-down — our own
screenshot/HTML is the primary evidence; the Wayback snapshot is corroborating.

Wayback auth note: the SPN2 JSON API now requires archive.org S3 keys, but the
anonymous ``GET /save/<url>`` path still returns the snapshot URL in the 302
``Location`` header. We default to that; if ``WAYBACK_S3_KEY``/``_SECRET`` are
set we use the authenticated JSON + polling flow instead.
"""
from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx

from .crawler import FetchResult
from .pdf import render_pdf_png
from .util import now_iso, sha256_bytes, sha256_text


def encode_screenshot(png: bytes, *, lossless: bool = True, quality: int = 80) -> tuple[bytes, str]:
    """Transcode a PNG screenshot to WebP. Returns ``(bytes, extension)``.

    WebP shrinks full-page page captures dramatically versus PNG. We default to
    *lossless* so the archived evidence is a bit-exact rendering of the page (a
    take-down-proof record shouldn't introduce compression artifacts); the file
    is still far smaller than PNG. If Pillow isn't installed we return the PNG
    unchanged — evidence preservation must never depend on an optional codec.
    """
    try:
        import io

        from PIL import Image
    except ImportError:
        return png, "png"
    try:
        im = Image.open(io.BytesIO(png))
        buf = io.BytesIO()
        # method=6 = slowest/best ratio; screenshots are archived once, so the
        # extra encode time is worth the smaller file.
        im.save(buf, format="WEBP", lossless=lossless, quality=quality, method=6)
        return buf.getvalue(), "webp"
    except Exception:
        # Any decode/encode failure: keep the original PNG rather than lose it.
        return png, "png"


@dataclass
class ArchiveRecord:
    url: str
    screenshot_path: str | None
    html_path: str | None
    text_path: str | None
    content_hash: str
    wayback_url: str | None
    wayback_job_id: str | None
    archived_at: str
    pdf_path: str | None = None      # raw document when the source was a PDF


# ---------------------------------------------------------------------------
class WaybackClient:
    SAVE = "https://web.archive.org/save/"
    AVAILABLE = "https://archive.org/wayback/available"

    def __init__(self, user_agent: str = "RedBoxTracker/0.1", timeout: float = 60.0):
        self.user_agent = user_agent
        self.timeout = timeout
        self.s3_key = os.environ.get("WAYBACK_S3_KEY")
        self.s3_secret = os.environ.get("WAYBACK_S3_SECRET")

    def _headers(self, json_api: bool = False) -> dict[str, str]:
        h = {"User-Agent": self.user_agent}
        if json_api:
            h["Accept"] = "application/json"
        if self.s3_key and self.s3_secret:
            h["Authorization"] = f"LOW {self.s3_key}:{self.s3_secret}"
        return h

    def save(self, url: str) -> tuple[str | None, str | None]:
        """Return (snapshot_url, job_id). job_id only for the authenticated flow."""
        if self.s3_key and self.s3_secret:
            return self._save_authenticated(url)
        return self._save_anonymous(url), None

    def _save_anonymous(self, url: str, *, attempts: int = 3) -> str | None:
        """GET /save/<url>, read the 302 Location for the snapshot URL.

        Save-Page-Now is slow and occasionally returns a "please wait" 200 or a
        429 instead of the 302 redirect, so we retry with backoff before falling
        back to the availability API (any existing snapshot). The local
        screenshot/HTML remains the primary, take-down-proof evidence either way.
        """
        for attempt in range(attempts):
            try:
                resp = httpx.get(self.SAVE + url, headers=self._headers(),
                                 follow_redirects=False, timeout=self.timeout)
            except httpx.HTTPError:
                resp = None
            if resp is not None:
                loc = resp.headers.get("location") or resp.headers.get("content-location")
                if loc and "/web/" in loc:
                    return loc if loc.startswith("http") else "https://web.archive.org" + loc
                # A successful body without a redirect can still embed the URL.
                if resp.status_code < 400 and "/web/" in resp.text:
                    import re
                    m = re.search(r"https?://web\.archive\.org/web/\d+/\S+", resp.text)
                    if m:
                        return m.group(0).rstrip('"\'<>')
            if attempt < attempts - 1:
                time.sleep(3.0 * (attempt + 1))
        return self._latest_snapshot(url)

    def _save_authenticated(self, url: str) -> tuple[str | None, str | None]:
        try:
            resp = httpx.post(
                "https://web.archive.org/save",
                headers=self._headers(json_api=True),
                data={"url": url, "skip_first_archive": 1},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            job_id = resp.json().get("job_id")
        except (httpx.HTTPError, ValueError):
            return self._save_anonymous(url), None
        if not job_id:
            return self._save_anonymous(url), None
        # poll status
        for _ in range(20):
            time.sleep(5)
            try:
                st = httpx.get(f"https://web.archive.org/save/status/{job_id}",
                               headers=self._headers(json_api=True),
                               timeout=self.timeout).json()
            except (httpx.HTTPError, ValueError):
                continue
            if st.get("status") == "success":
                ts, orig = st.get("timestamp"), st.get("original_url", url)
                return f"https://web.archive.org/web/{ts}/{orig}", job_id
            if st.get("status") == "error":
                break
        return None, job_id

    def _latest_snapshot(self, url: str) -> str | None:
        """Fallback: ask the availability API for any existing snapshot."""
        try:
            data = httpx.get(self.AVAILABLE, params={"url": url},
                             headers=self._headers(), timeout=self.timeout).json()
            return (data.get("archived_snapshots", {})
                        .get("closest", {}).get("url"))
        except (httpx.HTTPError, ValueError):
            return None


# ---------------------------------------------------------------------------
class Archiver:
    def __init__(
        self,
        artifacts_dir: str | Path,
        *,
        wayback: WaybackClient | None = None,
        push_wayback: bool = True,
        screenshot_lossless: bool = True,
        screenshot_quality: int = 80,
    ) -> None:
        self.dir = Path(artifacts_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.wayback = wayback or WaybackClient()
        self.push_wayback = push_wayback
        self.screenshot_lossless = screenshot_lossless
        self.screenshot_quality = screenshot_quality

    def archive(self, result: FetchResult, *, candidate_id: str) -> ArchiveRecord:
        """Persist evidence for one fetched page. Files live under
        ``artifacts/<candidate_id>/<hash>.{webp,html,txt}`` (the screenshot
        falls back to ``.png`` if Pillow isn't installed)."""
        text = result.classifier_text
        content_hash = sha256_text(text)
        base = self.dir / candidate_id
        base.mkdir(parents=True, exist_ok=True)
        stem = content_hash[:16]

        screenshot_path = html_path = text_path = pdf_path = None
        shot_png = result.screenshot_png
        if result.pdf_bytes:
            # PDF source: preserve the original document, and rasterize its
            # pages into the screenshot slot so it gets the same visual exhibit
            # as an HTML page. The raw PDF is the primary evidence; a failed
            # render only costs the image.
            p = base / f"{stem}.pdf"
            p.write_bytes(result.pdf_bytes)
            pdf_path = str(p)
            if shot_png is None:
                shot_png = render_pdf_png(result.pdf_bytes)
        if shot_png:
            data, ext = encode_screenshot(
                shot_png,
                lossless=self.screenshot_lossless,
                quality=self.screenshot_quality,
            )
            p = base / f"{stem}.{ext}"
            p.write_bytes(data)
            screenshot_path = str(p)
        if result.html:
            p = base / f"{stem}.html"
            p.write_text(result.html)
            html_path = str(p)
        p = base / f"{stem}.txt"
        p.write_text(text)
        text_path = str(p)

        wayback_url = job_id = None
        if self.push_wayback:
            wayback_url, job_id = self.wayback.save(result.final_url or result.url)

        return ArchiveRecord(
            url=result.url, screenshot_path=screenshot_path, html_path=html_path,
            text_path=text_path, content_hash=content_hash, wayback_url=wayback_url,
            wayback_job_id=job_id, archived_at=now_iso(), pdf_path=pdf_path,
        )

    @staticmethod
    def persist(
        conn: sqlite3.Connection, rec: ArchiveRecord, *,
        candidate_id: str, scan_id: int | None, detection_id: int | None,
        commit: bool = True,
    ) -> int:
        cur = conn.execute(
            """INSERT INTO archives (candidate_id, scan_id, detection_id, url,
                   screenshot_path, html_path, text_path, content_hash,
                   wayback_url, wayback_job_id, archived_at, pdf_path)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (candidate_id, scan_id, detection_id, rec.url, rec.screenshot_path,
             rec.html_path, rec.text_path, rec.content_hash, rec.wayback_url,
             rec.wayback_job_id, rec.archived_at, rec.pdf_path),
        )
        # commit=False lets scan_candidate fold this into its per-page batch commit.
        if commit:
            conn.commit()
        return cur.lastrowid
