"""Offline tests for the deploy command's stale-page sweep."""
from __future__ import annotations

import pytest

from redbox.cli import _clean_stale_pages

SITE_URL = "https://redboxwatch.org"


def _site(tmp_path, listed, extra_files, base=SITE_URL):
    locs = "".join(f"<url><loc>{base}/{u}</loc></url>" for u in listed)
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

    stale = _clean_stale_pages(site, SITE_URL)

    assert stale == ["H9999.html"]
    assert not (site / "H9999.html").exists()
    # everything else survives
    for name in ["index.html", "about.html", "H1234.html", "404.html",
                 "index-data.json", "feed.xml", "feed.json"]:
        assert (site / name).exists(), name
    assert (site / "evidence" / "abc.pdf").exists()


def test_sweep_handles_http_and_path_prefixed_site_urls(tmp_path):
    # The old implementation hardcoded "https://<bare-host>/" in its regex: an
    # http:// or path-prefixed site_url matched nothing, the listed set came
    # back empty, and every freshly built page was deleted as "stale".
    base = "http://staging.example/tracker"
    site = _site(tmp_path, listed=["", "about", "H1234"],
                 extra_files=["index.html", "about.html", "H1234.html",
                              "H9999.html", "404.html"],
                 base=base)
    stale = _clean_stale_pages(site, base)
    assert stale == ["H9999.html"]
    assert (site / "index.html").exists()


def test_sweep_refuses_when_sitemap_matches_nothing(tmp_path):
    # site_url that doesn't match the sitemap origin: refuse rather than
    # treat the whole site as stale.
    site = _site(tmp_path, listed=["", "about"],
                 extra_files=["index.html", "about.html", "404.html"])
    with pytest.raises(ValueError):
        _clean_stale_pages(site, "https://other.example")
    assert (site / "index.html").exists()
    assert (site / "about.html").exists()


def test_sweep_refuses_missing_sitemap(tmp_path):
    (tmp_path / "index.html").write_text("x")
    with pytest.raises(ValueError):
        _clean_stale_pages(tmp_path, SITE_URL)
    assert (tmp_path / "index.html").exists()


def test_sweep_refuses_mass_deletion(tmp_path):
    # A sitemap that lists one real page while dozens exist means something
    # upstream broke; deleting most of the site must never be the outcome.
    files = [f"H{i:04d}.html" for i in range(40)] + ["index.html", "404.html"]
    site = _site(tmp_path, listed=[""], extra_files=files)
    with pytest.raises(ValueError):
        _clean_stale_pages(site, SITE_URL)
    for name in files:
        assert (site / name).exists()
