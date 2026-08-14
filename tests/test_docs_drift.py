"""Docs-drift guards: mechanically catch README/docs/config falling out of sync.

The doc audits that motivated these found shipped commands and flags absent
from the README command table, config keys that nothing read, documented knobs
missing from config.yaml, a documented threshold key that existed nowhere in
the code (`negative_floor`), and a code-read config key absent from
config.yaml (`candidate_wallclock_seconds`) — all from commits that touched
only the code or only the docs. Each class of drift here is cheap to detect,
so detect it.

Guards:
1. every CLI command documented in README;
2. every config.yaml key read by some module;
3. every `x.y` config key documented in README/docs exists in config.yaml;
4. every config key the code reads exists (commented is fine) in config.yaml;
5. every CLI `--flag` documented in README or docs/;
6. the config table's Default column names only keys config.yaml contains;
7. DATA_MODEL's url_source enum covers every tag website.py emits;
8. every repo path referenced in README/docs exists.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# The one consolidated skip-list, shared by every guard below. Interpretation
# by entry shape:
#   - ends with "/"                      -> path PREFIX to skip in the
#     referenced-paths guard (generated or untracked trees: nothing under it
#     is expected to exist in a fresh checkout);
#   - contains "/" or "."                -> exact path to skip (untracked /
#     placeholder files legitimately mentioned by the docs);
#   - bare [a-z_]+ identifier            -> config key exempt from the
#     "every config.yaml key is read" guard (structured data whose children
#     are values, not identifiers — pricing's children are model names,
#     per_domain's are hostnames, tokens_per_minute_by_model's are models).
SKIPS = frozenset({
    # config keys whose children are data, not code identifiers
    "pricing",
    "per_domain",
    "tokens_per_minute_by_model",
    # untracked / generated trees referenced throughout the docs
    "data/",
    "site/",
    ".wrangler/",
    # untracked-by-design files the docs must still be able to name
    "config.local.yaml",
    ".env",
})

_PATH_PREFIX_SKIPS = tuple(s for s in SKIPS if s.endswith("/"))
_PATH_EXACT_SKIPS = {s for s in SKIPS if ("/" in s or "." in s) and not s.endswith("/")}
_CONFIG_KEY_SKIPS = {s for s in SKIPS if "/" not in s and "." not in s}


def _docs_texts() -> dict[Path, str]:
    """README + every markdown page under docs/ (audits included)."""
    files = [ROOT / "README.md"] + sorted((ROOT / "docs").rglob("*.md"))
    return {p: p.read_text() for p in files}


def _config_yaml_text() -> str:
    return (ROOT / "config.yaml").read_text()


def _key_in_config_yaml(key: str, text: str) -> bool:
    """A key 'exists' in config.yaml if a (possibly commented) `key:` line
    shows it — discoverability for an operator reading the file is the bar."""
    return re.search(rf"^\s*#?\s*{re.escape(key)}\s*:", text, re.M) is not None


# ---------------------------------------------------------------------------
def test_every_cli_command_is_documented_in_readme():
    cli = (ROOT / "redbox" / "cli.py").read_text()
    readme = (ROOT / "README.md").read_text()
    commands = set(re.findall(r'add_parser\(\s*"([a-z][a-z0-9-]*)"', cli))
    assert commands, "no subparsers found — regex out of date?"
    missing = sorted(c for c in commands if f"`{c}`" not in readme)
    assert not missing, (
        f"CLI command(s) {missing} are not documented in README.md — "
        f"add them to the §4 command table")


def test_every_cli_flag_is_documented():
    """Every --flag any subcommand accepts must appear in README or docs/
    (parent-parser flags shared by several commands count once anywhere)."""
    cli = (ROOT / "redbox" / "cli.py").read_text()
    corpus = "\n".join(_docs_texts().values())
    flags = set(re.findall(r'add_argument\(\s*"(--[a-z][a-z-]*)"', cli))
    assert flags, "no flags found — regex out of date?"
    missing = sorted(f for f in flags if f not in corpus)
    assert not missing, (
        f"CLI flag(s) {missing} appear nowhere in README.md or docs/*.md — "
        f"document them (the §4 command table is the usual home)")


def test_every_config_yaml_key_is_read_somewhere():
    cfg = yaml.safe_load(_config_yaml_text())
    corpus = "".join(p.read_text() for p in (ROOT / "redbox").glob("*.py"))
    # Top-level keys, plus children of structured blocks whose keys are code
    # identifiers (see SKIPS for the blocks whose children are data).
    keys = set(cfg)
    for parent in ("models", "publish", "rate_limit", "robots_policy",
                   "scan_cadence", "confidence_thresholds", "screenshot",
                   "nominees"):
        keys.update((cfg.get(parent) or {}).keys())
    keys -= _CONFIG_KEY_SKIPS
    unread = sorted(k for k in keys if k not in corpus)
    assert not unread, (
        f"config.yaml key(s) {unread} are never read by any module — "
        f"dead config misleads operators; wire or remove them")


def test_code_config_reads_appear_in_config_yaml():
    """Reverse direction: every string key the code reads through a config
    accessor must be discoverable (commented is fine) in config.yaml. This is
    the guard that would have caught `candidate_wallclock_seconds` — read
    with a default in cli.py for weeks while config.yaml never mentioned it.
    """
    yaml_text = _config_yaml_text()
    keys: set[str] = set()
    for p in (ROOT / "redbox").glob("*.py"):
        text = p.read_text()
        # cfg.get("key") — also matches pub_cfg.get / robots_cfg.get.
        keys.update(re.findall(r'\b[a-z_]*cfg\.get\(\s*"([a-z_]+)"', text))
        # Sub-block reads through the conventional local names for config
        # blocks (models = cfg.models, shot = screenshot block, rl =
        # rate_limit, cad/cadence = scan_cadence).
        keys.update(re.findall(
            r'\b(?:models|shot|rl|cad|cadence)\.get\(\s*"([a-z_]+)"', text))
    # config.py: every string-literal .get() is a config key (env vars are
    # UPPER_CASE and don't match), and _path()'s first argument is one too.
    cfg_py = (ROOT / "redbox" / "config.py").read_text()
    keys.update(re.findall(r'\.get\(\s*"([a-z_]+)"', cfg_py))
    keys.update(re.findall(r'_path\(\s*"([a-z_]+)"', cfg_py))
    assert keys, "no config reads found — regexes out of date?"
    missing = sorted(k for k in keys
                     if not _key_in_config_yaml(k, yaml_text))
    assert not missing, (
        f"code reads config key(s) {missing} that config.yaml never shows — "
        f"add each (a commented default is fine) so operators can find it")


def test_documented_config_keys_exist_in_config_yaml():
    """Every `x.y` config key the docs document must be discoverable in
    config.yaml itself (an operator editing the file can't find a knob the
    file doesn't show)."""
    cfg = yaml.safe_load(_config_yaml_text())
    documented: set[tuple[str, str]] = set()
    for text in _docs_texts().values():
        documented.update(re.findall(r"\|\s*`([a-z_]+)\.([a-z_]+)`", text))
    missing = sorted({f"{a}.{b}" for a, b in documented
                      if a in cfg and isinstance(cfg[a], dict) and b not in cfg[a]})
    assert not missing, (
        f"docs document config key(s) {missing} that config.yaml doesn't "
        f"contain — add them (commented is fine) so they're discoverable")


def test_config_table_defaults_name_real_keys():
    """The Default column of the config table (docs/CONFIGURATION.md) may only
    name keys that exist in config.yaml. This is the `negative_floor` blind
    spot: the README once documented `negative_floor: 0.6` under
    confidence_thresholds, a key that existed nowhere."""
    table = (ROOT / "docs" / "CONFIGURATION.md").read_text()
    yaml_text = _config_yaml_text()
    phantom: list[str] = []
    for line in table.splitlines():
        cells = line.split("|")
        if len(cells) < 4 or not cells[1].strip().startswith("`"):
            continue                        # not a key row of the table
        # `key: value` inside backticks; ": " (not "://") so URLs don't match.
        for key in re.findall(r"`([a-z_]+):\s", cells[2]):
            if not _key_in_config_yaml(key, yaml_text):
                phantom.append(key)
    assert not phantom, (
        f"docs/CONFIGURATION.md's Default column names key(s) {sorted(set(phantom))} "
        f"that config.yaml does not contain — fiction in the Default column "
        f"is exactly the drift this guard exists for")


def test_data_model_url_source_enum_complete():
    """docs/DATA_MODEL.md's url_source enum must contain every source tag
    website.py can emit (ResolvedURL literals + resolver ``name`` attrs)."""
    website = (ROOT / "redbox" / "website.py").read_text()
    data_model = (ROOT / "docs" / "DATA_MODEL.md").read_text()
    tags = set(re.findall(r'source="([a-z_]+)"', website))
    tags.update(re.findall(r'^\s*name = "([a-z_]+)"', website, re.M))
    assert {"serper", "wikipedia"} <= tags, "extraction regex out of date?"
    missing = sorted(t for t in tags if f"`{t}`" not in data_model)
    assert not missing, (
        f"url_source tag(s) {missing} are emitted by website.py but absent "
        f"from docs/DATA_MODEL.md's url_source enum")


def test_referenced_repo_paths_exist():
    """Every backticked repo-path-looking reference (contains a slash, ends in
    a file extension) in README/docs must point at a real file — dangling
    references rot silently. Untracked/generated trees are skipped via SKIPS."""
    dangling: list[str] = []
    for doc, text in _docs_texts().items():
        for span in re.findall(r"`([A-Za-z0-9_./-]+)`", text):
            if "/" not in span or not re.search(r"\.[a-z]{2,6}$", span):
                continue                    # not a repo file path
            if span in _PATH_EXACT_SKIPS or span.startswith(_PATH_PREFIX_SKIPS):
                continue
            if not (ROOT / span).exists():
                dangling.append(f"{doc.relative_to(ROOT)}: {span}")
    assert not dangling, (
        "doc(s) reference repo path(s) that don't exist:\n  "
        + "\n  ".join(sorted(set(dangling))))
