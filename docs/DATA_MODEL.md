# Data model

The SQLite schema lives in [`redbox/db.py`](../redbox/db.py) (`SCHEMA` +
`_MIGRATIONS`; `init_db` creates/updates it). This page documents the tables
and — more importantly — the **semantic enums** that live in plain columns,
which the schema alone can't express.

## Tables

| Table | One row per | Written by | Notes |
|---|---|---|---|
| `candidates` | FEC candidate id | `discover` (upsert), `resolve` (URL fields), `calendar` (primary_date), pruning commands + review console (`inactive`), pipeline (`scan_status`) | The hub. `discover`'s upsert deliberately never touches URL fields — `resolve` and the `/urls` triage own them. |
| `elections` | (state, cycle, office-key) | `calendar` / `discover` | `office` `''` = statewide date; `'H'` = office-wide override; `'H:01'` = single-district override (postponed races). Backfill resolves most-specific-first. |
| `scans` | fetched page per scan run | pipeline | Audit trail: `raw_text` is exactly what the classifier saw; `text_hash` drives change detection and alias dedup. Fresh DBs have no `raw_html` column (legacy DBs: see `vacuum`). |
| `detections` | classifier verdict | pipeline | Hash-unchanged re-scans and template-alias pages carry **no detection row of their own** — verdicts resolve through `(candidate_id, text_hash)`. |
| `archives` | preserved evidence | archiver | Screenshot (WebP)/HTML/text paths, optional `pdf_path` for PDF-sourced evidence, optional Wayback URL. Linked to the detection it evidences. |
| `reviews` | human decision | `review` / review console | **Append-only; latest review wins** (`reviewed_at DESC, review_id DESC`). Every publish/corroboration surface uses this convention. |
| `ie_filings` | FEC Schedule E transaction | `corroborate` | Delete-then-insert per (candidate, cycle). |
| `corroboration` | candidate summary | `corroborate` | Aligned IE totals + `guidance_first_detected` (never dated from a rejected detection). |
| `change_events` | put-up / take-down / modified | pipeline, `backfill-changes` | See below. |

## Enums in plain columns

### `candidates.inactive` — why a row is out of the universe

| Value | Meaning | Set by | Cleared by |
|---|---|---|---|
| `NULL`/`0` | active | — | — |
| `1` | FEC `candidate_inactive` (withdrawn / superseded record) | `mark-inactive` | `mark-inactive`, when the FEC un-flags |
| `2` | human wrong-race / not-a-real-candidacy call | review console `/urls`, manual | manual only — never touched by machine syncs |
| `3` | lost their primary | `mark-primary-losers` | self-heals each run as feed data improves |

Inactive rows are kept for history and excluded from resolve/scan/publish —
except candidates with an approved finding, which stay on the public ledger
(banner-labeled) so ending a candidacy can't unpublish a finding.

### `candidates.scan_status` — last scan disposition

| Value | Meaning |
|---|---|
| `NULL` | never scanned |
| `scanned` | at least one **usable** page fetched (HTTP 200, real content) |
| `robots_blocked` | robots.txt disallowed the crawler (public "Site blocks automated access") |
| `fetch_failed` | unreachable, empty, or nothing usable (DNS/timeout/all-4xx/bot challenges) — retry with `scan-all --rescan` |

### `change_events.event_type`

| Value | Meaning |
|---|---|
| `put_up` | page content went from no-guidance to guidance (diffed between **usable** scans only) |
| `take_down` | guidance gone: either replaced by usable non-guidance content, or the page 404/410'd on **two consecutive scans** (one may be a blip; 403/5xx/challenges never count — being blocked is not a removal) |
| `modified` | guidance page's content changed while staying positive; the publisher refines this to "updated / revised / changed" by comparing quoted spans |

`backfill-changes` reconciles this table against a replay of the full scan
history under the current rules (dry-run by default; `--apply` inserts what
old logic dropped and deletes what it manufactured from error scans).

### `scans.robots_posture`

`respect` | `override` — how robots.txt was applied to that fetch
(ROBOTS_POLICY.md's audit trail). `NULL` on rows predating the column.

### `candidates.url_source` / `url_verified`

`url_source`: `none` | `wikipedia` | `fec_committee` | `search` | `manual` |
`human_none` (a human looked and found no site — stays a published coverage
gap). `url_verified=1` means a human confirmed the attribution; verification
is enforced at the review gate before anything publishes (or pre-scan with
`require_verified_url: true`).
