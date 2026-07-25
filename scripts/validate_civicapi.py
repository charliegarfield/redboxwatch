#!/usr/bin/env python
"""Validation probe: how well does civicAPI cover federal primaries, and does it
crosswalk to FEC candidate IDs?

Pulls civicAPI's CALLED primary winners for a state, crosswalks each to the
FEC-funded universe (from the bulk weball file) by (state, office, district,
party) + fuzzy name, and reports the match rate plus an end-to-end
NomineeResolver summary. Read-only: hits the public civicAPI and reads the local
weball file. Nothing is written or scanned.

Usage:
    python scripts/validate_civicapi.py --state TX [--offices H,S] [--year 2026]
    python scripts/validate_civicapi.py --state TX --show-unmatched
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from redbox.config import load_config                       # noqa: E402
from redbox.nominees import (CivicAPIFeed, NomineeResolver,  # noqa: E402
                             crosswalk, norm_district, norm_party)
from redbox.weball import load_funded                       # noqa: E402


def _funded(cfg, state: str, offices: set[str]) -> list[dict]:
    path = cfg.weball_path
    if not path.exists():
        sys.exit(f"weball file not found: {path} (set weball_path / download it first)")
    rows = load_funded(path, offices=offices, states={state.upper()},
                       receipts_floor=cfg.receipts_floor)
    return [dict(r.as_candidate(), receipts=r.receipts) for r in rows]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", required=True, help="two-letter state code, e.g. TX")
    ap.add_argument("--offices", default="H,S", help="comma list of H,S,P (default H,S)")
    ap.add_argument("--year", type=int, default=None, help="cycle (default: config election_year)")
    ap.add_argument("--config", default=None)
    ap.add_argument("--show-unmatched", action="store_true",
                    help="print each civicAPI winner that did NOT crosswalk, with the FEC bucket")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    year = args.year or cfg.election_year
    state = args.state.upper()
    offices = {o.strip().upper() for o in args.offices.split(",") if o.strip()}

    cands = _funded(cfg, state, offices)
    print(f"FEC funded universe ({state}, offices={sorted(offices)}, "
          f">=${cfg.receipts_floor:,.0f}): {len(cands)} candidates")

    feed = CivicAPIFeed(cycle=year, user_agent=cfg.user_agent)
    try:
        calls = [c for c in feed.calls(state=state) if c.office in offices]
    except Exception as e:
        sys.exit(f"civicAPI query failed: {e}")
    print(f"civicAPI CALLED primary winners ({state}, {year}): {len(calls)} "
          f"({dict(Counter(c.office for c in calls))})")
    if not calls:
        print("  (no called federal primary winners returned — coverage gap or "
              "primaries not yet held/called for this state)")

    # --- raw crosswalk match rate ---------------------------------------------
    matched = unmatched = 0
    rows_unmatched = []
    for call in calls:
        cid = crosswalk(call, cands)
        if cid:
            matched += 1
        else:
            unmatched += 1
            key = (call.office, state, call.district, call.party)
            bucket = [c for c in cands
                      if (c.get("office"), state, norm_district(c.get("office"), c.get("district")),
                          norm_party(c.get("party"))) == key]
            rows_unmatched.append((call, bucket))
    if calls:
        print(f"\nCrosswalk to an FEC candidate_id: {matched}/{len(calls)} "
              f"({100*matched/len(calls):.0f}%) matched, {unmatched} unmatched")

    if args.show_unmatched and rows_unmatched:
        print("\nUnmatched civicAPI winners (winner -> FEC candidates in that bucket):")
        for call, bucket in rows_unmatched:
            names = ", ".join(c["name"] for c in bucket) or "(no funded FEC candidate in bucket)"
            print(f"  {call.office}-{state}-{call.district}-{call.party}: "
                  f"'{call.winner_name}'  ->  [{names}]")

    # --- end-to-end resolver summary ------------------------------------------
    res = NomineeResolver(year, feed=feed).resolve(cands, states=[state])
    by_source = Counter(n.source.split(":")[0] for n in res.nominees.values())
    print(f"\nNomineeResolver end-to-end: {len(res.nominees)} nominees resolved "
          f"(by source: {dict(by_source)}); {len(res.unresolved)} contested races unresolved")
    if res.unresolved:
        shown = ", ".join("-".join(k) for k in res.unresolved[:12])
        print(f"  unresolved buckets: {shown}{' …' if len(res.unresolved) > 12 else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
