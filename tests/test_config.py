"""Tests for config layering in redbox.config.load_config.

All configs are built fresh under tmp_path; the module's default-path
constants are monkeypatched so no real config.yaml / config.local.yaml /
.env from the repo is ever read.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from redbox import config as config_mod
from redbox.config import _deep_merge, load_config


def _write(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Point the module's default paths into tmp_path (nothing exists yet)."""
    monkeypatch.setattr(config_mod, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(config_mod, "DEFAULT_CONFIG_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(config_mod, "LOCAL_CONFIG_PATH", tmp_path / "config.local.yaml")
    return tmp_path


# --- Bug A: explicit path naming the default file must keep the local layer ---

def test_explicit_default_path_merges_local(sandbox, capsys):
    _write(sandbox / "config.yaml", {"a": 1, "models": {"first_pass": "base"}})
    _write(sandbox / "config.local.yaml", {"models": {"first_pass": "prod"}, "b": 2})

    cfg = load_config(sandbox / "config.yaml")

    assert cfg.raw == {"a": 1, "b": 2, "models": {"first_pass": "prod"}}
    err = capsys.readouterr().err
    assert "config.local.yaml" in err
    assert "2 top-level keys" in err


def test_explicit_default_path_via_indirect_route_merges_local(sandbox):
    """A path that resolves to the default file (e.g. sub/../config.yaml)
    is recognized as the default."""
    (sandbox / "sub").mkdir()
    _write(sandbox / "config.yaml", {"a": 1})
    _write(sandbox / "config.local.yaml", {"a": 2})

    cfg = load_config(sandbox / "sub" / ".." / "config.yaml")

    assert cfg.raw == {"a": 2}


def test_implicit_default_still_merges_local(sandbox, capsys):
    _write(sandbox / "config.yaml", {"a": 1})
    _write(sandbox / "config.local.yaml", {"b": 2})

    cfg = load_config()

    assert cfg.raw == {"a": 1, "b": 2}
    assert "config.local.yaml" in capsys.readouterr().err


# --- Bug A part 2: sibling local file for a genuinely different explicit path ---

def test_explicit_other_path_merges_sibling_local(sandbox, capsys):
    # A default-side local file with a sentinel proves the wrong layer isn't used.
    _write(sandbox / "config.yaml", {"unused": True})
    _write(sandbox / "config.local.yaml", {"sentinel": "wrong-layer"})
    other = _write(sandbox / "other.yaml", {"a": 1, "nested": {"x": 1}})
    _write(sandbox / "other.local.yaml", {"nested": {"y": 2}})

    cfg = load_config(other)

    assert cfg.raw == {"a": 1, "nested": {"x": 1, "y": 2}}
    assert "sentinel" not in cfg.raw
    err = capsys.readouterr().err
    assert "other.local.yaml" in err
    assert "1 top-level key" in err


def test_explicit_other_path_without_sibling_is_base_only_and_silent(sandbox, capsys):
    other = _write(sandbox / "other.yaml", {"a": 1})

    cfg = load_config(other)

    assert cfg.raw == {"a": 1}
    assert capsys.readouterr().err == ""


# --- Bug B: missing paths ---

def test_missing_explicit_path_raises(sandbox):
    with pytest.raises(FileNotFoundError, match="confg.yaml"):
        load_config(sandbox / "confg.yaml")


def test_missing_implicit_default_yields_empty_config(sandbox):
    # Preserved current behavior: fresh checkout without config.yaml still works.
    cfg = load_config()
    assert cfg.raw == {}


def test_missing_implicit_default_with_local_still_merges(sandbox):
    # Also current behavior: local layer applies even over an empty base.
    _write(sandbox / "config.local.yaml", {"b": 2})
    cfg = load_config()
    assert cfg.raw == {"b": 2}


# --- merge semantics ---

def test_deep_merge_recurses_dicts_and_replaces_lists_wholesale():
    base = {
        "models": {"first_pass": "base", "judge": "j"},
        "common_paths": ["/donate", "/about"],
        "keep": 1,
    }
    override = {
        "models": {"first_pass": "prod"},
        "common_paths": ["/only-this"],
    }

    merged = _deep_merge(base, override)

    assert merged == {
        "models": {"first_pass": "prod", "judge": "j"},
        "common_paths": ["/only-this"],
        "keep": 1,
    }
    # Inputs are not mutated.
    assert base["models"]["first_pass"] == "base"
    assert base["common_paths"] == ["/donate", "/about"]


def test_list_replacement_applies_through_load_config(sandbox):
    _write(sandbox / "config.yaml", {"common_paths": ["/donate", "/about"]})
    _write(sandbox / "config.local.yaml", {"common_paths": ["/contribute"]})

    cfg = load_config()

    assert cfg.common_paths == ["/contribute"]
