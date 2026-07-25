"""Configuration loading (spec §6).

Single source of truth: ``config.yaml`` for non-secret settings, environment
variables (optionally via a ``.env`` file) for secrets. Nothing secret is ever
written to ``config.yaml``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.yaml"
LOCAL_CONFIG_PATH = REPO_ROOT / "config.local.yaml"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no dependency on python-dotenv).

    Only sets keys that are not already present in the environment, so a real
    exported env var always wins.
    """
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass
class Config:
    """Typed view over config.yaml + secrets from the environment."""

    raw: dict[str, Any]
    repo_root: Path = REPO_ROOT

    # --- secrets (env only) ---
    @property
    def fec_api_key(self) -> str:
        return (
            os.environ.get("FEC_API_KEY")
            or os.environ.get("DATA_GOV_API_KEY")
            or os.environ.get("OPENFEC_API_KEY")
            or "DEMO_KEY"
        )

    @property
    def anthropic_api_key(self) -> str | None:
        return os.environ.get("ANTHROPIC_API_KEY")

    @property
    def openai_api_key(self) -> str | None:
        # Only needed when a classifier model uses the openai/ provider prefix.
        return os.environ.get("OPENAI_API_KEY")

    @property
    def fireworks_api_key(self) -> str | None:
        # Only needed when a classifier model uses the fireworks/ provider prefix.
        return os.environ.get("FIREWORKS_API_KEY")

    @property
    def serper_key(self) -> str | None:
        # Serper.dev (Google search) key for URL resolution. When present (and
        # search is enabled), it's the search backend instead of Claude web_search.
        return os.environ.get("SERPER_KEY")

    # --- convenience accessors ---
    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    @property
    def election_year(self) -> int:
        return int(self.raw.get("election_year", 2026))

    @property
    def receipts_floor(self) -> float:
        return float(self.raw.get("receipts_floor", 50000))

    @property
    def general_receipts_floor(self) -> float:
        """Receipts floor for the contested-GENERAL screen: a district with >=2
        candidates from >=2 parties each above this is a contested general.
        Higher than receipts_floor so a token challenger doesn't qualify a
        whole district. 0 disables the screen."""
        return float(self.raw.get("general_receipts_floor", 100000))

    @property
    def include_incumbents(self) -> bool:
        """Baseline-coverage rule D: every sitting incumbent (FEC I flag) enters
        the universe, even below receipts_floor and with no funded opposition."""
        return bool(self.raw.get("include_incumbents", False))

    @property
    def include_funded_nominees(self) -> bool:
        """Baseline-coverage rule E: a race's sole funded candidate of a party
        (uncontested primary -> presumptive nominee) enters the universe if they
        clear receipts_floor, even when the general has no funded contest."""
        return bool(self.raw.get("include_funded_nominees", False))

    @property
    def rating_threshold(self) -> list[str]:
        return list(self.raw.get("rating_threshold", ["Tilt", "Lean", "Toss-up"]))

    @property
    def rating_source(self) -> str:
        return str(self.raw.get("rating_source", "fixture"))

    @property
    def scan_mode(self) -> str:
        """Which universe to build: 'primary' (contested primaries, default),
        'general' (primary winners / nominees), or 'full' (per-race phase)."""
        return str(self.raw.get("scan_mode", "primary")).lower()

    @property
    def nominee_feed(self) -> str:
        """Results feed for nominee resolution in general/full modes:
        'civicapi' (default) or 'none' (uncontested-auto + manual override only)."""
        return str((self.raw.get("nominees", {}) or {}).get("feed", "civicapi")).lower()

    @property
    def models(self) -> dict[str, Any]:
        return dict(self.raw.get("models", {}))

    @property
    def common_paths(self) -> list[str]:
        return list(self.raw.get("common_paths", []))

    @property
    def crawl_depth(self) -> int:
        return int(self.raw.get("crawl_depth", 2))

    @property
    def require_verified_url(self) -> bool:
        # Off by default: URL verification is a review-time signal, not a pre-scan
        # blocker. Set true to restore the spec §3.1 pre-scan gate.
        return bool(self.raw.get("require_verified_url", False))

    @property
    def enable_search_backup(self) -> bool:
        # Billable web-search resolution (Claude web_search tool) as the last
        # resort when free sources (Wikipedia/committee) miss.
        return bool(self.raw.get("enable_search_backup", True))

    @property
    def search_model(self) -> str:
        return str(self.raw.get("models", {}).get("search", "claude-sonnet-4-6"))

    @property
    def judge_model(self) -> str:
        # Cheap model that judges which Serper result is the candidate's own site.
        return str(self.raw.get("models", {}).get("judge", "claude-haiku-4-5"))

    @property
    def user_agent(self) -> str:
        return str(self.raw.get("user_agent", "RedBoxTracker/0.1"))

    # --- paths (resolved relative to repo root) ---
    def _path(self, key: str, default: str) -> Path:
        p = Path(self.raw.get(key, default))
        return p if p.is_absolute() else self.repo_root / p

    @property
    def database_path(self) -> Path:
        return self._path("database_path", "data/redbox.sqlite")

    @property
    def artifacts_dir(self) -> Path:
        return self._path("artifacts_dir", "data/artifacts")

    @property
    def fec_cache_dir(self) -> Path:
        return self._path("fec_cache_dir", "data/.fec_cache")

    @property
    def resolve_cache_dir(self) -> Path:
        """Disk cache for URL-resolution results (Wikipedia/committee/search), so
        a rerun doesn't re-search candidates already resolved."""
        return self._path("resolve_cache_dir", "data/.resolve_cache")

    @property
    def weball_path(self) -> Path:
        """FEC bulk candidate-summary file (weball). Used for discovery if present."""
        return self._path("weball_path", "data/webl26.txt")


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge ``override`` into ``base`` (override wins)."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load .env + config.yaml into a :class:`Config`.

    If an untracked ``config.local.yaml`` exists next to ``config.yaml`` it is
    deep-merged on top (local wins). Deployment-specific values that don't
    belong in the public repo — the crawler contact address, per-domain robots
    overrides, production model choices — live there.
    """
    _load_dotenv(REPO_ROOT / ".env")
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    raw = yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
    raw = raw or {}
    if path is None and LOCAL_CONFIG_PATH.exists():
        local = yaml.safe_load(LOCAL_CONFIG_PATH.read_text()) or {}
        raw = _deep_merge(raw, local)
    return Config(raw=raw)
