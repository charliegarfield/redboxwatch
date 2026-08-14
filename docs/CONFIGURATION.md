# Configuration reference

All non-secret settings live in [`config.yaml`](../config.yaml); secrets come
from the environment (optionally via a `.env` file). Deployment-specific
values — the crawler contact address, per-domain robots overrides, production
model choices — belong in the untracked `config.local.yaml`, which is
deep-merged on top of `config.yaml` (local wins). This page is the full knob
list; the README links here from its walkthrough.

## How config is loaded (`redbox/config.py`)

- **Default (no `--config`)**: `config.yaml` is read (a missing file is
  tolerated — a fresh checkout runs on code defaults), then
  `config.local.yaml` is deep-merged on top when present.
- **`--config config.yaml`** (the same file as the default): treated exactly
  like the implicit case, so naming the default path doesn't silently drop the
  local layer.
- **`--config other.yaml`** (any other file): a sibling `other.local.yaml`
  (same directory, `<stem>.local.yaml`) is merged when present; otherwise the
  base file is used alone. An explicit path that does not exist raises an
  error instead of silently yielding all defaults.
- Whenever a local layer is merged, a one-line notice naming the file goes to
  **stderr**.
- Merge semantics: dicts merge recursively; **lists (and every other non-dict
  value) are replaced wholesale** — a local file overriding e.g.
  `common_paths` must restate the entire list.

## Secrets (environment / `.env`)

| Variable | Purpose |
|---|---|
| `FEC_API_KEY` | openFEC key (free from api.data.gov). Aliases accepted: `DATA_GOV_API_KEY`, `OPENFEC_API_KEY`. Without any of them the code falls back to `DEMO_KEY` (30 req/hr). |
| `ANTHROPIC_API_KEY` | Classifier (first pass + escalation), Serper judge, `web_search` fallback. Billable. |
| `SERPER_KEY` | Optional. Serper.dev (Google) key; when set, it is the search backend for URL resolution. |
| `OPENAI_API_KEY` / `FIREWORKS_API_KEY` | Only needed when a classifier model uses the `openai/` / `fireworks/` provider prefix. |
| `WAYBACK_S3_KEY` / `WAYBACK_S3_SECRET` | Optional. Authenticated Internet Archive Save-Page-Now (higher limits); auto-detected by the archiver. |

A real exported env var always wins over a `.env` line.

## Keys

| Key | Default | Meaning |
|---|---|---|
| `election_year` | `2026` | Cycle to operate on. |
| `weball_path` | `data/webl26.txt` | FEC bulk candidate-summary file; used by `discover` when present (refresh with `fetch-fec`), else the openFEC API. |
| `receipts_floor` | `50000` | Minimum FEC receipts to keep a candidate. |
| `general_receipts_floor` | `100000` | Contested-general money screen: ≥2 parties over this ⇒ the district's candidates enter. `0` disables. |
| `include_incumbents` / `include_funded_nominees` | `true` | Baseline coverage: every incumbent; every sole funded nominee over `receipts_floor`. |
| `rating_threshold` | `[Tilt, Lean, Toss-up]` | Ratings kept for the competitive overlay. |
| `rating_source` | `fixture` | Rating adapter. Only the offline `fixture` adapter ships with the repo (its data is a 5-row demo file, `fixtures/ratings/2026.json` — replace it or add a live adapter for real coverage); `none` disables the ratings overlay; any other value fails loudly. |
| `scan_mode` | `primary` | Universe to build: `primary` (contested primaries), `general` (primary winners / nominees), or `full` (per-race phase). Override per run with `discover --mode`. |
| `nominees.feed` | `civicapi` | Results feed for nominee resolution (general/full modes + `mark-primary-losers`): `civicapi` (free, key-less) or `none` (uncontested-auto + manual override only). |
| `require_verified_url` | `false` | If `true`, refuse to scan a candidate whose URL isn't human-verified (spec §3.1 pre-scan gate). |
| `enable_search_backup` | `true` | Use the billable search resolver when free sources miss a URL (Serper if `SERPER_KEY` set, else Claude `web_search`). |
| `crawl_depth` | `2` | Link-crawl depth. |
| `max_pages_per_site` | `150` | **Per-class** fetch budget: ordinary pages, media-kit-style pages, and PDFs each get this many (hard total 3×); high-value pages fetch first. |
| `common_paths` | `/media`, `/press`, … | Paths probed on every site. |
| `robots_policy.default` | `respect` | `respect` \| `override` — see [`ROBOTS_POLICY.md`](../ROBOTS_POLICY.md). |
| `robots_policy.per_domain` | `{}` | Per-host overrides; deployment-specific, set them in `config.local.yaml`. |
| `scan_cadence` | `daily_window_days: 21`, `default_interval_days: 7` | Daily-scan window before each candidate's primary/general; weekly interval otherwise. |
| `candidate_wallclock_seconds` | `1500` (code default; commented in the file) | Per-candidate scan wall-clock ceiling. Checked between pages, so hitting it ends the candidate as a clean partial (`last_scan_partial=1`, `scan_status='scanned'`). See [`docs/OPERATIONS.md`](OPERATIONS.md) for the whole watchdog story and the recovery gotcha. |
| `user_agent` | identifiable UA string | Honest crawler identity. Set a REAL contact address (in `config.local.yaml`) before live crawls. |
| `rate_limit.default_min_delay_seconds` | `2.0` | Minimum delay between requests to one domain; the limiter instance is shared across all `scan-all` workers. |
| `models.first_pass` / `models.escalation` | `claude-haiku-4-5` → `claude-sonnet-4-6` | Classifier models. Strings are `provider/model`; unprefixed = Anthropic; `fireworks/` and `openai/` supported. Starting defaults — set production choices in `config.local.yaml`. |
| `models.search` | `claude-sonnet-4-6` | Model for the Claude `web_search` resolution fallback (when no `SERPER_KEY`). |
| `models.judge` | `claude-haiku-4-5` | Cheap model that picks the candidate's site from Serper results. |
| `models.max_tokens` | `1024` | Max output tokens per classification call. |
| `models.chunk_chars` | `40000` | Chars per classification chunk (most pages → one call). |
| `models.concurrency` | `8` | Chunks classified concurrently within one page. |
| `models.classify_pool_size` | `12` | Global cap on concurrent chunk-classify calls across all `scan-all` workers (one shared pool). Defaults to `concurrency`. |
| `models.tokens_per_minute` | `300000` | Scalar **input-tokens/min ceiling**, applied to first-pass and escalation as independent buckets. `0` disables. |
| `models.tokens_per_minute_by_model` | per-model map | **Per-model** input-tokens/min ceilings (keys match model strings incl. provider prefix). Overrides the scalar; the accurate way to set them. |
| `confidence_thresholds` | `escalate_below: 0.75` | First-pass results below this confidence escalate to the stronger model. (This is the only key in the block.) |
| `pricing` | per-model $/M input tokens | Drives the `scan-all` dry-run **cost preflight** only — not billing-accurate; verify current prices and override. |
| `database_path` | `data/redbox.sqlite` | SQLite database location (relative paths resolve against the repo root). |
| `artifacts_dir` | `data/artifacts` | Archived evidence (screenshots / HTML / text / PDFs). |
| `fec_cache_dir` | `data/.fec_cache` | Cached FEC API responses. |
| `resolve_cache_dir` | `data/.resolve_cache` | Disk cache for `resolve` results so reruns don't re-search. |
| `screenshot` | `lossless: true`, `quality: 80` | Evidence screenshots transcode PNG→WebP (~70–90% smaller); set `lossless: false` + a quality for lossy. |
| `publish.site_url` | `https://redboxwatch.org` | Canonical public origin; enables sitemap/robots/canonical/OG tags + feeds on approved-only builds. Never applied to review builds. |
| `publish.pages_project` | `redboxwatch` | Cloudflare Pages project for `redbox deploy`. |
| `publish.page_size` | `500` | Rows per generated index page; the index paginates (findings-first) past this. |

The `models.*` starting defaults are deliberately code-side too — the
production deployment's model selection (and its tuned classifier prompt)
lives in untracked local files and is not published (see the README's ethics
notes).
