"""Offline tests for the deploy command: stale-page sweep + pre-upload purge."""
from __future__ import annotations

import argparse

import pytest

import redbox.publisher
from redbox.cli import _clean_stale_pages, cmd_deploy
from redbox.config import Config

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


# ---------------------------------------------------------------------------
# cmd_deploy pre-upload purge: wrangler uploads the whole site/ directory, and
# Cloudflare Pages' always-ignore list does NOT cover .wrangler/, so its cache
# (account id + name) must be gone before the upload command ever runs.

class _Result:
    returncode = 0


# Per-build footer fingerprint (publisher._layout): the smoke check must see
# THIS build's fingerprint on the live site, not just a 200 + nameplate.
FINGERPRINT = "Generated 2026-08-05 12:34 UTC"
STALE_FINGERPRINT = "Generated 2026-08-01 00:00 UTC"


class _Resp:
    status_code = 200
    text = f"RedBoxWatch {FINGERPRINT}"


class _StaleResp:
    # What the PREVIOUS deploy serves: 200 + nameplate, older fingerprint.
    status_code = 200
    text = f"RedBoxWatch {STALE_FINGERPRINT}"


def _deploy_env(tmp_path, monkeypatch):
    """Wire cmd_deploy to a tmp repo: fake build_site, record subprocess.run
    (asserting the purge already happened), stub the live check."""

    def fake_build_site(conn, out, *, approved_only, page_size, site_url):
        out.mkdir(exist_ok=True)
        (out / "index.html").write_text(f"RedBoxWatch {FINGERPRINT}")
        (out / "sitemap.xml").write_text(
            f'<?xml version="1.0"?><urlset><url><loc>{site_url}/</loc></url>'
            "</urlset>")

    calls = []

    def fake_run(cmd, **kwargs):
        site = tmp_path / "site"
        # The purge must precede the upload: nothing dot-named may remain.
        assert not (site / ".wrangler").exists()
        assert not list(site.rglob(".DS_Store"))
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr(redbox.publisher, "build_site", fake_build_site)
    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr("httpx.get",
                        lambda url, **kw: _Resp())
    cfg = Config(
        raw={"database_path": str(tmp_path / "db.sqlite"),
             "publish": {"site_url": SITE_URL, "pages_project": "redbox"}},
        repo_root=tmp_path)
    args = argparse.Namespace(skip_tests=True, dry_run=False)
    return cfg, args, calls


def test_deploy_purges_wrangler_cache_and_ds_store(tmp_path, monkeypatch):
    cfg, args, calls = _deploy_env(tmp_path, monkeypatch)
    site = tmp_path / "site"
    (site / ".wrangler" / "cache").mkdir(parents=True)
    (site / ".wrangler" / "cache" / "wrangler-account.json").write_text(
        '{"account_id": "secret"}')
    (site / ".DS_Store").write_bytes(b"\x00")

    rc = cmd_deploy(args, cfg)

    assert rc == 0
    assert not (site / ".wrangler").exists()
    assert not (site / ".DS_Store").exists()
    # Deploy proceeded: exactly one subprocess call, and it's the upload.
    assert len(calls) == 1
    assert "wrangler" in calls[0]


def test_deploy_aborts_on_unexpected_dotfile(tmp_path, monkeypatch):
    cfg, args, calls = _deploy_env(tmp_path, monkeypatch)
    site = tmp_path / "site"
    site.mkdir()
    (site / ".env").write_text("SECRET=1")

    rc = cmd_deploy(args, cfg)

    assert rc == 1
    assert calls == []          # wrangler never invoked
    assert (site / ".env").exists()  # refused, not silently deleted


def test_deploy_clean_site_deploys(tmp_path, monkeypatch):
    cfg, args, calls = _deploy_env(tmp_path, monkeypatch)
    rc = cmd_deploy(args, cfg)
    assert rc == 0
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Smoke check: HTTP 200 + "RedBoxWatch" is also true of the PREVIOUS deploy,
# so the live check must additionally see this build's footer fingerprint.

def test_deploy_smoke_check_passes_on_fresh_fingerprint(tmp_path, monkeypatch):
    cfg, args, calls = _deploy_env(tmp_path, monkeypatch)
    fetched = []
    monkeypatch.setattr("httpx.get",
                        lambda url, **kw: fetched.append(url) or _Resp())
    rc = cmd_deploy(args, cfg)
    assert rc == 0
    assert len(calls) == 1
    assert fetched == [SITE_URL]        # first poll succeeded, no retries


def test_deploy_smoke_check_fails_on_stale_content(tmp_path, monkeypatch, capsys):
    # Live site keeps serving the previous build (200 + nameplate, but the
    # old fingerprint): the deploy must FAIL loudly, naming old vs expected.
    cfg, args, calls = _deploy_env(tmp_path, monkeypatch)
    monkeypatch.setattr("httpx.get", lambda url, **kw: _StaleResp())
    monkeypatch.setattr("time.sleep", lambda s: None)   # don't wait ~30s
    rc = cmd_deploy(args, cfg)
    assert rc == 1
    assert len(calls) == 1              # wrangler ran; the smoke check failed
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert FINGERPRINT in out           # expected build
    assert STALE_FINGERPRINT in out     # what the live site actually serves


def test_deploy_smoke_check_polls_until_fresh(tmp_path, monkeypatch):
    # Pages propagation can lag: stale on the first two polls, fresh on the
    # third — the deploy succeeds without exhausting all attempts.
    cfg, args, calls = _deploy_env(tmp_path, monkeypatch)
    responses = [_StaleResp(), _StaleResp(), _Resp()]
    monkeypatch.setattr("httpx.get", lambda url, **kw: responses.pop(0))
    monkeypatch.setattr("time.sleep", lambda s: None)
    rc = cmd_deploy(args, cfg)
    assert rc == 0
    assert responses == []              # exactly three polls


def test_deploy_dry_run_builds_but_never_uploads_or_polls(tmp_path, monkeypatch):
    cfg, _, calls = _deploy_env(tmp_path, monkeypatch)
    fetched = []
    monkeypatch.setattr("httpx.get",
                        lambda url, **kw: fetched.append(url) or _Resp())
    args = argparse.Namespace(skip_tests=True, dry_run=True)
    rc = cmd_deploy(args, cfg)
    assert rc == 0
    assert calls == [] and fetched == []


# ---------------------------------------------------------------------------
# scan-all stuck-scan watchdog: workers insert/pop the in-flight dict while
# the watchdog iterates it. The loop body is extracted as _watchdog_overdue
# (snapshot under a shared lock) so it is unit-testable here.

def test_watchdog_overdue_flags_only_past_ceiling():
    import threading

    from redbox.cli import _watchdog_overdue

    lock = threading.Lock()
    # ceiling 1500 + 120s grace; at now=1700 only the t=0 start is overdue.
    in_flight = {"H2": ("Beta", 900.0), "H1": ("Alpha", 0.0)}
    out = _watchdog_overdue(in_flight, lock, 1500, 1700.0)
    assert out == [("H1", "Alpha", 1700.0 / 60)]
    # nothing overdue -> empty
    assert _watchdog_overdue(in_flight, lock, 1500, 100.0) == []


def test_watchdog_overdue_safe_under_concurrent_mutation():
    # The original bug: dict resized by a worker mid-iteration raised
    # RuntimeError and silently killed the watchdog. The snapshot-under-lock
    # version must never raise while another thread churns the dict.
    import threading

    from redbox.cli import _watchdog_overdue

    lock = threading.Lock()
    in_flight = {f"H{i:03d}": (f"cand{i}", 0.0) for i in range(64)}
    stop = threading.Event()

    def churn():
        i = 0
        while not stop.is_set():
            with lock:
                in_flight[f"X{i}"] = ("new", 0.0)
                in_flight.pop(f"X{i - 32}", None)
            i += 1

    t = threading.Thread(target=churn)
    t.start()
    try:
        for _ in range(300):
            _watchdog_overdue(in_flight, lock, 0, 1000.0)
    finally:
        stop.set()
        t.join()
