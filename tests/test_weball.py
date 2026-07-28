"""Tests for the FEC bulk 'weball' candidate-summary reader (spec §3.1)."""
from __future__ import annotations

from redbox.weball import load_funded, parse_weball

# Minimal pipe-delimited rows in the real weball column layout (30 fields;
# only the ones we read need to be meaningful). cols: 0 id,1 name,2 ici,4 party,
# 5 receipts, 18 state, 19 district.
SAMPLE = "\n".join([
    # House, NY-12, two funded Dems (contested), one under floor
    "H6NY12001|DOE, JANE|C|1|DEM|2071992.26|" + "0|"*12 + "NY|12|" + "0|"*8 + "0",
    "H6NY12002|ROE, RICHARD|C|1|DEM|2873154.0|" + "0|"*12 + "NY|12|" + "0|"*8 + "0",
    "H6NY12999|PAPER, CANDIDATE|C|1|DEM|4000.0|" + "0|"*12 + "NY|12|" + "0|"*8 + "0",
    # Senate AK (district 00)
    "S6AK00001|GLACIER, GRACE|C|1|DEM|8661662.38|" + "0|"*12 + "AK|00|" + "0|"*8 + "0",
    # House TX-28 REP
    "H6TX28100|SMITH, JOE|I|2|REP|600000.0|" + "0|"*12 + "TX|28|" + "0|"*8 + "0",
    # House VT-01: sitting incumbent UNDER the receipts floor
    "H6VT01001|SAFE, SAM|I|1|DEM|30000.0|" + "0|"*12 + "VT|01|" + "0|"*8 + "0",
    "",  # blank line tolerated
])


def _write(tmp_path):
    p = tmp_path / "weball.txt"
    p.write_text(SAMPLE)
    return p


def test_parse_basic_fields(tmp_path):
    rows = {r.candidate_id: r for r in parse_weball(_write(tmp_path))}
    doe = rows["H6NY12001"]
    assert doe.name == "DOE, JANE"
    assert doe.office == "H"
    assert doe.party == "DEM"
    assert doe.state == "NY"
    assert doe.district == "12"
    assert doe.receipts == 2071992.26
    # Senate office + at-large district
    assert rows["S6AK00001"].office == "S"
    assert rows["S6AK00001"].district == "00"


def test_load_funded_applies_floor(tmp_path):
    p = _write(tmp_path)
    rows = load_funded(p, receipts_floor=50000)
    ids = {r.candidate_id for r in rows}
    assert "H6NY12999" not in ids          # under $50k -> dropped
    assert "H6NY12001" in ids and "H6NY12002" in ids


def test_load_funded_keeps_subfloor_incumbents_when_asked(tmp_path):
    # include_incumbents rule: the floor exemption applies to CAND_ICI == 'I'
    # only — a sub-floor challenger is still a paper candidate.
    p = _write(tmp_path)
    default = {r.candidate_id for r in load_funded(p, receipts_floor=50000)}
    assert "H6VT01001" not in default
    kept = {r.candidate_id for r in
            load_funded(p, receipts_floor=50000, keep_subfloor_incumbents=True)}
    assert "H6VT01001" in kept
    assert "H6NY12999" not in kept


def test_load_funded_filters_office_state_district(tmp_path):
    p = _write(tmp_path)
    ny12 = load_funded(p, offices={"H"}, states={"NY"}, district="12", receipts_floor=50000)
    assert {r.candidate_id for r in ny12} == {"H6NY12001", "H6NY12002"}
    senate = load_funded(p, offices={"S"}, receipts_floor=50000)
    assert {r.candidate_id for r in senate} == {"S6AK00001"}


def test_discovery_groups_from_weball(tmp_path):
    # End-to-end: bulk-file source -> contested-primary grouping.
    from redbox.config import Config
    from redbox.discovery import Discovery
    from redbox.website import WebsiteResolver

    cfg = Config(raw={"election_year": 2026, "receipts_floor": 50000})
    resolver = WebsiteResolver(overrides_path=tmp_path / "none.json", wikipedia=False)
    d = Discovery(cfg, fec=None, rating_adapter=None, resolver=resolver,
                  weball_path=_write(tmp_path))
    entries = d.build_universe(offices=["H"], states=["NY"], district="12")
    ids = {e.candidate.candidate_id for e in entries}
    # Two funded same-party NY-12 candidates -> contested primary.
    assert ids == {"H6NY12001", "H6NY12002"}
    assert all("contested_primary" in e.reasons for e in entries)


def test_fetch_weball_installs_and_backs_up(tmp_path):
    # Serve a zip via file:// and confirm: install, row-count guard, dated backup.
    import zipfile

    import pytest

    from redbox.weball import fetch_weball

    rows = "\n".join(
        f"H6NY12{i:03d}|DOE, JANE {i}|C|1|DEM|99999.0|" + "0|" * 12 + "NY|12|" + "0|" * 8 + "0"
        for i in range(1200))
    zpath = tmp_path / "webl26.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("webl26.txt", rows)
    dest = tmp_path / "data" / "webl26.txt"
    dest.parent.mkdir()
    dest.write_text("H0OLD00000|STALE, SUE|C|1|DEM|1.0|" + "0|" * 12 + "NY|12|" + "0|" * 8 + "0")

    info = fetch_weball(dest, cycle=2026, url=zpath.as_uri())
    assert info["rows"] == 1200
    assert "DOE, JANE 5" in dest.read_text()
    backups = list(dest.parent.glob("webl26.txt.old-*"))
    assert len(backups) == 1 and "STALE, SUE" in backups[0].read_text()

    # A truncated download must never replace a working file.
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("webl26.txt", "H6NY12001|DOE, JANE|C|1|DEM|9.0|" + "0|" * 12 + "NY|12|" + "0|" * 8 + "0")
    with pytest.raises(RuntimeError, match="refusing to replace"):
        fetch_weball(dest, cycle=2026, url=zpath.as_uri())
    assert "DOE, JANE 5" in dest.read_text()   # unchanged


def test_fetch_weball_sets_readable_perms_and_unique_backups(tmp_path, monkeypatch):
    # mkstemp files are 0600 — a fetch under cron/another uid then breaks
    # discover with PermissionError. And two fetches the same day must not
    # clobber the first backup.
    import io
    import zipfile

    from redbox import weball

    rows = "|".join(["H0XX00001", "DOE, JANE", "C"] + [""] * 15 + ["XX", "01"])
    lines = "\n".join(
        "|".join([f"H0XX{i:05d}", f"DOE, JANE {i}", "C", "", "REP", "120000"]
                 + [""] * 12 + ["XX", "01"]) for i in range(1200))

    def fake_urlopen(req, timeout=0):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("webl26.txt", lines)
        buf.seek(0)
        return io.BytesIO(buf.read())

    class _Ctx:
        def __init__(self, data): self._d = data
        def __enter__(self): return self._d
        def __exit__(self, *a): return False

    import urllib.request as _ur
    monkeypatch.setattr(_ur, "urlopen",
                        lambda req, timeout=0: _Ctx(fake_urlopen(req)))
    dest = tmp_path / "webl26.txt"
    r1 = weball.fetch_weball(dest, cycle=2026)
    assert r1["rows"] == 1200
    import stat
    assert stat.S_IMODE(dest.stat().st_mode) == 0o644

    r2 = weball.fetch_weball(dest, cycle=2026)
    r3 = weball.fetch_weball(dest, cycle=2026)
    backups = sorted(p.name for p in tmp_path.glob("webl26.txt.old-*"))
    assert len(backups) == 2                    # same-day backups both kept
    assert r2["backup"] != r3["backup"]
