"""Docs-drift guards: mechanically catch README/config falling out of sync.

The doc audit that motivated these found two shipped commands and four flags
absent from the README command table, two config keys that nothing read, and
two documented knobs missing from config.yaml — all from commits that touched
only the code. Each class of drift here is cheap to detect, so detect it.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def test_every_cli_command_is_documented_in_readme():
    cli = (ROOT / "redbox" / "cli.py").read_text()
    readme = (ROOT / "README.md").read_text()
    commands = set(re.findall(r'add_parser\(\s*"([a-z][a-z0-9-]*)"', cli))
    assert commands, "no subparsers found — regex out of date?"
    missing = sorted(c for c in commands if f"`{c}`" not in readme)
    assert not missing, (
        f"CLI command(s) {missing} are not documented in README.md — "
        f"add them to the §5 command table")


def test_every_config_yaml_key_is_read_somewhere():
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    corpus = "".join(p.read_text() for p in (ROOT / "redbox").glob("*.py"))
    # Top-level keys, plus children of structured blocks whose keys are code
    # identifiers (pricing's children are model names — skip those).
    keys = set(cfg)
    for parent in ("models", "publish", "rate_limit", "robots_policy",
                   "scan_cadence", "confidence_thresholds", "screenshot"):
        keys.update((cfg.get(parent) or {}).keys())
    keys.discard("pricing")
    # per_domain values are deployment data, not identifiers.
    keys.discard("per_domain")
    keys.discard("tokens_per_minute_by_model")
    unread = sorted(k for k in keys if k not in corpus)
    assert not unread, (
        f"config.yaml key(s) {unread} are never read by any module — "
        f"dead config misleads operators; wire or remove them")


def test_documented_config_keys_exist_in_config_yaml():
    """Every `x.y` config key the README documents must be discoverable in
    config.yaml itself (an operator editing the file can't find a knob the
    file doesn't show)."""
    readme = (ROOT / "README.md").read_text()
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
    documented = re.findall(r"\|\s*`([a-z_]+)\.([a-z_]+)`", readme)
    missing = sorted({f"{a}.{b}" for a, b in documented
                      if a in cfg and isinstance(cfg[a], dict) and b not in cfg[a]})
    # models.* starting defaults are deliberately code-side (production values
    # live in the untracked config.local.yaml) — everything else must show.
    missing = [m for m in missing if not m.startswith("models.")]
    assert not missing, (
        f"README documents config key(s) {missing} that config.yaml doesn't "
        f"contain — add them (commented is fine) so they're discoverable")
