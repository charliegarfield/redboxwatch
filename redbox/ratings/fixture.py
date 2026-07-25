"""Offline fixture-backed rating adapter (spec §3.1B).

Reads normalised ratings from ``fixtures/ratings/<cycle>.json`` so the pipeline
is testable without depending on a live scrape. Shape::

    [{"office": "H", "state": "TX", "district": "28",
      "rating": "Lean", "party_favored": "D", "source": "sabato-fixture"}]
"""
from __future__ import annotations

import json
from pathlib import Path

from .base import RaceRating, RatingAdapter

FIXTURE_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures" / "ratings"


class FixtureRatingAdapter(RatingAdapter):
    name = "fixture"

    def __init__(self, fixture_dir: Path | None = None) -> None:
        self.fixture_dir = fixture_dir or FIXTURE_DIR

    def fetch(self, *, cycle: int) -> list[RaceRating]:
        path = self.fixture_dir / f"{cycle}.json"
        if not path.exists():
            return []
        rows = json.loads(path.read_text())
        return [
            RaceRating(
                office=row["office"],
                state=row["state"],
                district=row.get("district"),
                rating=row["rating"],
                party_favored=row.get("party_favored"),
                source=row.get("source", "fixture"),
            )
            for row in rows
        ]
