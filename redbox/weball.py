"""FEC bulk candidate-summary file reader (spec §3.1, scale).

This reads the FEC "Current campaigns — House and Senate" financial-summary
file (webl{yy}.zip; webl26 for 2026 — NOT the all-candidates 'weball' product,
despite this module's historical name): one pipe-delimited row per candidate
with name, party, state, district, AND total receipts inline. Despite the
product's House-and-Senate name, presidential (P-prefixed) rows do appear in
the file and are expected — the parser keeps them and discovery gates them by
cycle (``_office_on_ballot``). Using it for discovery replaces thousands of
per-candidate API calls (/candidates/ + /totals/) with a single local file —
the difference between a ~10-hour rate-limited discovery and a ~1-second one.

File description:
https://www.fec.gov/campaign-finance-data/current-campaigns-house-and-senate-file-description/

Columns we use (1-indexed in the spec; 0-indexed below):
  0  CAND_ID            candidate id (office = first char: H/S/P)
  1  CAND_NAME          "LAST, FIRST ..."
  2  CAND_ICI           incumbent/challenger/open (I/C/O)
  4  PTY_CD / party     three-letter party (DEM/REP/...) — field 5 in the spec
  5  TTL_RECEIPTS       total receipts (the money figure)
  18 CAND_OFFICE_ST     two-letter state
  19 CAND_OFFICE_DISTRICT  two-digit district ("00" for Senate/at-large)
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass
class WeballRow:
    candidate_id: str
    name: str
    office: str            # H | S | P (from candidate_id[0])
    party: str             # DEM | REP | ...
    state: str
    district: str
    receipts: float
    incumbent_challenge: str

    def as_candidate(self) -> dict:
        """Shape matching the openFEC /candidates/ record fields Discovery uses."""
        return {
            "candidate_id": self.candidate_id,
            "name": self.name,
            "office": self.office,
            "state": self.state,
            "district": self.district,
            "party": self.party,
            "incumbent_challenge": self.incumbent_challenge,
        }


def _office(candidate_id: str) -> str:
    c = candidate_id[:1].upper()
    return c if c in "HSP" else ""


def parse_weball(path: str | Path) -> Iterator[WeballRow]:
    """Yield one WeballRow per data line. Skips malformed/short rows."""
    path = Path(path)
    with path.open(encoding="latin-1") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("|")
            if len(parts) < 20 or not parts[0]:
                continue
            cid = parts[0].strip()
            try:
                receipts = float(parts[5] or 0)
            except ValueError:
                receipts = 0.0
            yield WeballRow(
                candidate_id=cid,
                name=parts[1].strip(),
                office=_office(cid),
                party=(parts[4] or "").strip(),
                state=(parts[18] or "").strip().upper(),
                district=(parts[19] or "").strip() or "00",
                receipts=receipts,
                incumbent_challenge=(parts[2] or "").strip(),
            )


def load_funded(
    path: str | Path, *, offices: set[str] | None = None,
    states: set[str] | None = None, district: str | None = None,
    receipts_floor: float = 50000.0, keep_subfloor_incumbents: bool = False,
) -> list[WeballRow]:
    """All candidates at/above the receipts floor, filtered by office/state/district.

    ``keep_subfloor_incumbents`` exempts sitting incumbents (CAND_ICI == 'I')
    from the floor: incumbency, not money, is their notability signal (the
    include_incumbents universe rule)."""
    out: list[WeballRow] = []
    for row in parse_weball(path):
        if row.receipts < receipts_floor and not (
                keep_subfloor_incumbents and row.incumbent_challenge == "I"):
            continue
        if offices and row.office not in offices:
            continue
        if states and row.state not in states:
            continue
        if district and row.district != district:
            continue
        out.append(row)
    return out


# ---------------------------------------------------------------------------
BULK_URL_TEMPLATE = "https://www.fec.gov/files/bulk-downloads/{cycle}/webl{yy}.zip"


def fetch_weball(dest: str | Path, *, cycle: int = 2026,
                 url: str | None = None, timeout: int = 120) -> dict:
    """Download the FEC bulk candidate-summary zip and install it at ``dest``.

    The zip holds a single ``webl{yy}.txt``. The current file (if any) is kept
    as ``<dest>.old-YYYYMMDD`` (dated by ITS download day, so the name says how
    stale it was) before the new one is moved into place atomically. The new
    file must parse and hold at least 1000 candidate rows — a truncated or
    reshaped download never replaces a working file.

    Returns {"rows": n, "backup": path-or-None, "url": url}.
    """
    import io
    import os
    import tempfile
    import urllib.request
    import zipfile
    from datetime import date, datetime

    dest = Path(dest)
    yy = str(cycle)[-2:]
    url = url or BULK_URL_TEMPLATE.format(cycle=cycle, yy=yy)
    member = f"webl{yy}.txt"

    req = urllib.request.Request(url, headers={"User-Agent": "redboxfinder/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        blob = resp.read()
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = zf.namelist()
        pick = member if member in names else next(
            (n for n in names if n.lower().endswith(".txt")), None)
        if pick is None:
            raise RuntimeError(f"no .txt member in {url} (members: {names})")
        data = zf.read(pick)

    tmp_fd, tmp_path = tempfile.mkstemp(dir=dest.parent, prefix=".webl-incoming-")
    try:
        with os.fdopen(tmp_fd, "wb") as fh:
            fh.write(data)
        rows = sum(1 for _ in parse_weball(tmp_path))
        if rows < 1000:
            raise RuntimeError(
                f"downloaded file parses to only {rows} candidate rows — "
                f"refusing to replace {dest}")
        # mkstemp files are 0600; the installed file must be readable like a
        # normal data file (a fetch under cron/another uid otherwise leaves
        # discover with a PermissionError).
        os.chmod(tmp_path, 0o644)
        backup = None
        if dest.exists():
            stamp = date.fromtimestamp(dest.stat().st_mtime).strftime("%Y%m%d")
            backup = dest.with_name(f"{dest.name}.old-{stamp}")
            # Never clobber an earlier same-day backup — "keeps the previous
            # file" must hold per run, not per day.
            n = 1
            while backup.exists():
                n += 1
                backup = dest.with_name(f"{dest.name}.old-{stamp}.{n}")
            os.replace(dest, backup)
        os.replace(tmp_path, dest)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise
    return {"rows": rows, "backup": str(backup) if backup else None, "url": url}
