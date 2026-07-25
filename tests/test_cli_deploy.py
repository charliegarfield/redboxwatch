"""Offline tests for the deploy command's stale-page sweep."""
from __future__ import annotations

from redbox.cli import _clean_stale_pages


def _site(tmp_path, listed, extra_files):
    locs = "".join(f"<url><loc>https://redboxwatch.org/{u}</loc></url>" for u in listed)
    (tmp_path / "sitemap.xml").write_text(
        f'<?xml version="1.0"?><urlset>{locs}</urlset>')
    for name in extra_files:
        (tmp_path / name).write_text("x")
    return tmp_path


def test_sweep_deletes_only_unlisted_html(tmp_path):
    site = _site(
        tmp_path,
        listed=["", "about", "H1234"],           # "" = index.html
        extra_files=["index.html", "about.html", "H1234.html",
                     "H9999.html",               # stale candidate page
                     "404.html",                 # sitemap-absent by design
                     "index-data.json", "feed.xml", "feed.json"],  # non-.html
    )
    (site / "evidence").mkdir()
    (site / "evidence" / "abc.pdf").write_text("x")

    stale = _clean_stale_pages(site)

    assert stale == ["H9999.html"]
    assert not (site / "H9999.html").exists()
    # everything else survives
    for name in ["index.html", "about.html", "H1234.html", "404.html",
                 "index-data.json", "feed.xml", "feed.json"]:
        assert (site / name).exists(), name
    assert (site / "evidence" / "abc.pdf").exists()
