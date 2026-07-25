"""Offline tests for the archiver (spec §3.4). Wayback push is stubbed."""
from __future__ import annotations

from redbox.archiver import Archiver, WaybackClient
from redbox.crawler import FetchResult
from redbox.db import init_db


class StubWayback(WaybackClient):
    def save(self, url):
        return ("https://web.archive.org/web/20260529/" + url, None)


def _result():
    return FetchResult(
        url="https://example.com/media", final_url="https://example.com/media",
        status=200, content_type="text/html", render_mode="browser",
        html="<html><body>Younger voters should see ads on the go.</body></html>",
        visible_text="Younger voters should see ads on the go.",
        dom_text="Younger voters should see ads on the go.",
        screenshot_png=b"\x89PNG fake",
    )


def test_archive_writes_files_and_row(tmp_path):
    arc = Archiver(tmp_path / "artifacts", wayback=StubWayback(), push_wayback=True)
    rec = arc.archive(_result(), candidate_id="H6NC01165")

    # files exist on disk
    assert rec.screenshot_path and open(rec.screenshot_path, "rb").read() == b"\x89PNG fake"
    assert rec.html_path and "Younger voters" in open(rec.html_path).read()
    assert rec.text_path and "on the go" in open(rec.text_path).read()
    assert rec.content_hash
    assert rec.wayback_url.startswith("https://web.archive.org/web/")

    conn = init_db(tmp_path / "db.sqlite")
    # candidate row required by FK
    conn.execute(
        "INSERT INTO candidates (candidate_id, name, url_verified, created_at, updated_at)"
        " VALUES ('H6NC01165','BUCK',1,'t','t')")
    aid = Archiver.persist(conn, rec, candidate_id="H6NC01165", scan_id=None,
                           detection_id=None)
    assert aid > 0
    row = conn.execute("SELECT wayback_url, screenshot_path FROM archives").fetchone()
    assert row["wayback_url"].startswith("https://web.archive.org/web/")
    conn.close()


def test_archive_can_skip_wayback(tmp_path):
    arc = Archiver(tmp_path / "a", push_wayback=False)
    rec = arc.archive(_result(), candidate_id="C1")
    assert rec.wayback_url is None
    assert rec.text_path  # local evidence still written


def test_invalid_png_falls_back_to_png(tmp_path):
    # _result()'s screenshot isn't a decodable image: rather than lose evidence,
    # the archiver writes the original bytes verbatim as .png.
    arc = Archiver(tmp_path / "a", push_wayback=False)
    rec = arc.archive(_result(), candidate_id="C1")
    assert rec.screenshot_path.endswith(".png")
    assert open(rec.screenshot_path, "rb").read() == b"\x89PNG fake"


def _pdf_bytes(pages: int = 2) -> bytes:
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument.new()
    for _ in range(pages):
        doc.new_page(200, 300)
    import io

    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _pdf_result():
    return FetchResult(
        url="https://example.com/memo.pdf", final_url="https://example.com/memo.pdf",
        status=200, content_type="application/pdf", render_mode="pdf",
        visible_text="Younger voters should see ads on the go.",
        dom_text="Younger voters should see ads on the go.",
        pdf_bytes=_pdf_bytes(),
    )


def test_pdf_source_archives_raw_pdf_and_rendered_pages(tmp_path):
    import io

    __import__("pytest").importorskip("pypdfium2")
    from PIL import Image

    arc = Archiver(tmp_path / "a", push_wayback=False)
    rec = arc.archive(_pdf_result(), candidate_id="C1")

    # raw document preserved verbatim
    assert rec.pdf_path and rec.pdf_path.endswith(".pdf")
    assert open(rec.pdf_path, "rb").read()[:5] == b"%PDF-"
    # pages rasterized into the screenshot slot (2 stacked pages + gap)
    assert rec.screenshot_path
    img = Image.open(io.BytesIO(open(rec.screenshot_path, "rb").read()))
    assert img.height > img.width  # two 200x300 pages stacked vertically

    conn = init_db(tmp_path / "db.sqlite")
    conn.execute(
        "INSERT INTO candidates (candidate_id, name, url_verified, created_at, updated_at)"
        " VALUES ('C1','X',1,'t','t')")
    Archiver.persist(conn, rec, candidate_id="C1", scan_id=None, detection_id=None)
    row = conn.execute("SELECT pdf_path, screenshot_path FROM archives").fetchone()
    assert row["pdf_path"] == rec.pdf_path
    conn.close()


def test_corrupt_pdf_still_archives_raw_bytes(tmp_path):
    r = _pdf_result()
    r.pdf_bytes = b"%PDF-1.4 not really a pdf"
    arc = Archiver(tmp_path / "a", push_wayback=False)
    rec = arc.archive(r, candidate_id="C1")
    # render fails silently; the raw document and text are still preserved
    assert rec.pdf_path and open(rec.pdf_path, "rb").read() == r.pdf_bytes
    assert rec.screenshot_path is None
    assert rec.text_path


def test_real_screenshot_is_transcoded_to_webp(tmp_path):
    import io

    PIL = __import__("pytest").importorskip("PIL")
    from PIL import Image

    # A real PNG with some structure so WebP can actually beat it on size.
    img = Image.new("RGB", (400, 1200), "white")
    for y in range(0, 1200, 3):
        for x in range(0, 400, 40):
            img.putpixel((x, y), (0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png = buf.getvalue()

    r = _result()
    r.screenshot_png = png
    arc = Archiver(tmp_path / "a", push_wayback=False)
    rec = arc.archive(r, candidate_id="C1")

    assert rec.screenshot_path.endswith(".webp")
    on_disk = open(rec.screenshot_path, "rb").read()
    assert on_disk[:4] == b"RIFF" and on_disk[8:12] == b"WEBP"  # valid WebP
    assert len(on_disk) < len(png)  # actually smaller
    # round-trips back to the same pixels (lossless default)
    assert Image.open(io.BytesIO(on_disk)).size == (400, 1200)
