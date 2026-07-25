"""Race-rating adapter interface (spec §3.1B).

Ratings drive the competitive-general overlay. Sources differ (Sabato via
270toWin tables, Cook behind a flag, Ballotpedia backstop), so each is a
pluggable adapter returning a normalised list of :class:`RaceRating`.
"""
from __future__ import annotations

from dataclasses import dataclass

# Canonical rating buckets, most→least competitive. We keep Toss-up/Tilt/Lean
# and drop Likely/Solid per the default rating_threshold.
COMPETITIVE = {"Toss-up", "Tilt", "Lean"}


@dataclass(frozen=True)
class RaceRating:
    office: str          # H / S / P (and "G" for governor if a source provides it)
    state: str           # two-letter
    district: str | None # e.g. "07" for House, None for statewide
    rating: str          # normalised: Toss-up | Tilt | Lean | Likely | Solid
    party_favored: str | None = None   # which party the rating leans toward, if known
    source: str = "unknown"


class RatingAdapter:
    """Base class. Implementations override :meth:`fetch`."""

    name = "base"

    def fetch(self, *, cycle: int) -> list[RaceRating]:  # pragma: no cover
        raise NotImplementedError

    def competitive(self, *, cycle: int, keep: set[str] | None = None) -> list[RaceRating]:
        keep = keep or COMPETITIVE
        return [r for r in self.fetch(cycle=cycle) if _normalize(r.rating) in keep]


def _normalize(rating: str) -> str:
    """Map source-specific wording onto canonical buckets."""
    r = rating.strip().lower()
    if "toss" in r:
        return "Toss-up"
    if "tilt" in r:
        return "Tilt"
    if "lean" in r:
        return "Lean"
    if "likely" in r:
        return "Likely"
    if "solid" in r or "safe" in r:
        return "Solid"
    return rating.strip().title()
