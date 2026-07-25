"""Pluggable race-rating adapters (spec §3.1B)."""
from .base import COMPETITIVE, RaceRating, RatingAdapter
from .fixture import FixtureRatingAdapter

__all__ = ["COMPETITIVE", "RaceRating", "RatingAdapter", "FixtureRatingAdapter"]
