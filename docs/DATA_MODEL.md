# Data model

The SQLite schema lives in [`redbox/db.py`](../redbox/db.py) (`SCHEMA` +
`_MIGRATIONS`; `init_db` creates/updates it). This page documents the tables
and — more importantly — the **semantic enums** that live in plain columns,
which the schema alone can't express.

## Tables

| Table | One row per | Written by | Notes |
|---|---|---|---|
| `candidates` | FEC candidate id | `discover` (upsert), `resolve` (URL fields), `calendar` (primary_date), pruning commands + review console (`inactive`), pipeline (`scan_status`, `last_attempt_at`, `last_scan_partial`) | The hub. `discover`'s upsert deliberately never touches URL fields — `resolve` and the `/urls` triage own them. |
| `elections` | (state, cycle, office-key) | `calendar` / `discover` | `office` `''` = statewide date; `'H'` = office-wide override; `'H:01'` = single-district override (postponed races). Backfill resolves most-specific-first. `runoff_date` (statewide rows) is the primary-runoff date where the state holds one — a runoff state's races aren't settled (and `mark-primary-losers` won't mark losers) until the **runoff** has passed. The calendar fixture covers the 50 states + DC; the delegate territories (AS, GU, MP, VI) are absent — a known gap, deliberately not filled with fabricated dates. |
| `scans` | fetched page per scan run | pipeline | Audit trail: `raw_text` is exactly what the classifier saw; `text_hash` drives change detection and alias dedup. Fresh DBs have no `raw_html` column — it is deprecated, never populated anymore (full HTML lives on disk via the archiver); legacy DBs reclaim it with `vacuum`. |
| `detections` | classifier verdict | pipeline | Hash-unchanged re-scans and template-alias pages carry **no detection row of their own** — verdicts resolve through `(candidate_id, text_hash)`. |
| `archives` | preserved evidence | archiver | Screenshot (WebP)/HTML/text paths, optional `pdf_path` for PDF-sourced evidence, optional Wayback URL. Linked to the detection it evidences. |
| `reviews` | human decision | `review` / review console | **Append-only; latest review wins** (`reviewed_at DESC, review_id DESC`). Every publish/corroboration surface uses this convention. |
| `ie_filings` | FEC Schedule E transaction | `corroborate` | Delete-then-insert per (candidate, cycle). |
| `corroboration` | candidate summary | `corroborate` | Aligned IE totals + `guidance_first_detected` (never dated from a rejected detection). |
| `change_events` | put-up / take-down / modified | pipeline, `backfill-changes` | See below. |
| `publications` | — (vestigial) | nobody | The spec §3.8 table was never used — publish state lives entirely in `reviews` (latest review wins) and the built site. Fresh DBs no longer create it; legacy DBs keep the empty table. |

**Untracked JSON sidecars** (under `data/`, human-owned, survive a DB rebuild):
`websites.json` — manual URL overrides (verified); `url_triage.json` — the
review console's durable triage record: every no-site ("human checked, no
campaign site exists") and wrong-race call is appended with outcome, reviewer,
and timestamp; `nominees/<cycle>.json` — manual nominee calls.

## Enums in plain columns

### `candidates.inactive` — why a row is out of the universe

| Value | Meaning | Set by | Cleared by |
|---|---|---|---|
| `NULL`/`0` | active | — | — |
| `1` | FEC `candidate_inactive` (withdrawn / superseded record) | `mark-inactive` | `mark-inactive`, when the FEC un-flags |
| `2` | human wrong-race / not-a-real-candidacy call | review console `/urls`, manual | manual only — never touched by machine syncs |
| `3` | lost their primary | `mark-primary-losers` | self-heals each run as feed data improves — but a run that would clear more than max(5, 20%) of a state's marks refuses without `--allow-mass-clear` (a feed going quiet is not evidence) |

Inactive rows are kept for history and excluded from resolve/scan/publish —
except candidates with an approved finding, which stay on the public ledger
(banner-labeled) so ending a candidacy can't unpublish a finding.

### `candidates.scan_status` / `last_attempt_at` / `last_scan_partial`

| `scan_status` | Meaning |
|---|---|
| `NULL` | never scanned |
| `scanned` | at least one **usable** page fetched (see "usable scan" below) |
| `robots_blocked` | robots.txt disallowed the crawler (public "Site blocks automated access") |
| `fetch_failed` | unreachable, empty, or nothing usable (DNS/timeout/all-4xx/bot challenges) — retry with `scan-all --rescan` |

`last_attempt_at` (ISO-8601 UTC) is stamped on **every** concluded attempt —
including zero-page outcomes (robots_blocked / fetch_failed) that write no
`scans` rows. The scheduler keys cadence off
MAX(`scans.fetched_at`, `last_attempt_at`), so a blocked/unreachable site
respects the cadence instead of being re-hit daily as "never scanned".

`last_scan_partial` — `1` when the attempt was truncated by the per-candidate
wall-clock ceiling (`candidate_wallclock_seconds`), `0` when it ran to
completion, `NULL` on rows predating the column. A truncated partial finishes
as a normal `scanned`, so this flag is the only way to tell a partial sweep
from a full one (and plain `scan-all` will skip it — use `--rescan`/`--due`;
see `docs/OPERATIONS.md`).

### "Usable" scans

A scan is **usable** — counts as coverage, anchors diff baselines, and is the
only kind that moves a URL's recorded state — iff `http_status == 200` AND
the extracted text is non-empty AND it is not a bot-challenge shell (a
sub-400-char body containing challenge markers like "just a moment" /
"checking your browser"). One predicate (`pipeline.usable_scan` /
`usable_scan_sql`) feeds both the classify gate and the diff logic so they
cannot disagree.

### `change_events.event_type`

| Value | Meaning |
|---|---|
| `put_up` | page content went from no-guidance to guidance (diffed between **usable** scans only) |
| `take_down` | guidance gone: either replaced by usable non-guidance content, or the page 404/410'd on **two consecutive scans** (one may be a blip; 403/5xx/challenges never count — being blocked is not a removal) |
| `modified` | guidance page's content changed while staying positive; the publisher refines this to "updated / revised / changed" by comparing quoted spans |

`backfill-changes` reconciles this table against a replay of the full scan
history under the current rules (dry-run by default; `--apply` inserts what
old logic dropped, deletes what it manufactured from error scans, and
corrects events whose recorded transition disagrees with the replay).

### `scans.robots_posture`

`respect` | `override` — how robots.txt was applied to that fetch
(ROBOTS_POLICY.md's audit trail). `NULL` on rows predating the column.

### `candidates.url_source` / `url_verified`

`url_source` — which resolver produced the URL (the tags
`redbox/website.py` emits, plus the review console's):
`none` | `wikipedia` | `committee` (FEC committee metadata) | `serper`
(Serper.dev Google search + LLM judge) | `search` (Claude `web_search`
fallback) | `manual` (human override — `data/websites.json` or the `/urls`
triage) | `human_none` (a human looked and found no site — stays a published
coverage gap). `url_verified=1` means a human confirmed the attribution;
verification is enforced at the review gate before anything publishes (or
pre-scan with `require_verified_url: true`).

### `candidates.universe_reason` / `nominee_source`

`universe_reason` — why discovery admitted the row. Base reasons:
`contested_primary`, `contested_general`, `competitive_general`, `nominee`,
`incumbent`, `funded_nominee`. Exactly {contested_primary,
competitive_general} keeps the legacy label `both`; every other combination
is `'+'`-joined in a fixed order with **`nominee` leading** (e.g.
`nominee+incumbent`, `contested_primary+contested_general`). Display-only
downstream.

`nominee_source` — how a general/full-mode nominee was confirmed:
`uncontested` (lone funded candidate of the party) | `feed:<name>` (results
feed, e.g. `feed:civicapi`) | `manual` (override file). `NULL` for
primary-mode rows.

## Derived: the published status (not a column)

The publisher/review console derive a nine-value per-candidate status from
detections + latest reviews + scan state (single source:
`STATUSES` in `redbox/render.py`; the `public` flag is the approved-only
build's allowlist):

| Status | Public build? | Meaning |
|---|---|---|
| `positive_published` | yes | approved finding (FINDING) |
| `positive_pending` | no | red-box detection awaiting review |
| `ambiguous_pending` | no | ambiguous detection awaiting review |
| `rejected` | no | reviewed — not a finding |
| `negative` | yes | scanned, nothing detected (dated) |
| `fetch_failed` | yes | coverage gap: site unreachable |
| `blocked_by_robots` | yes | coverage gap: robots.txt blocks the crawler |
| `no_url` | yes | coverage gap: no campaign site resolved |
| `not_scanned` | yes | in the universe, not yet scanned |
