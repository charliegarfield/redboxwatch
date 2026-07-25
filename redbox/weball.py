"""FEC bulk 'weball' candidate-summary file reader (spec §3.1, scale).

The FEC publishes a per-cycle "All candidates" financial-summary file (weball26
for 2026): one pipe-delimited row per candidate with name, party, state,
district, AND total receipts inline. Using it for discovery replaces thousands
of per-candidate API calls (/candidates/ + /totals/) with a single local file —
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

# Default location alongside the cache. Override via config `weball_path`.
DEFAULT_WEBALL = Path("data/webl26.txt")

OFFICE_FROM_ID = {"H": "H", "S": "S", "P": "P"}


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
    return OFFICE_FROM_ID.get(candidate_id[:1].upper(), "")


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
