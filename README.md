# Red-Boxing Tracker

Detects **red-boxing** — public-facing messaging/media-buy guidance that
campaigns post so aligned outside spenders (super PACs) can coordinate
*lawfully, by the letter of the rule*. The tool builds a candidate universe from
FEC data, scans campaign sites for the **functional content pattern** of
red-box guidance, archives evidence, corroborates against independent-expenditure
filings, **gates every positive finding behind human review**, and publishes
careful, evidence-linked per-candidate status. The public build runs at
**[redboxwatch.org](https://redboxwatch.org)**.

This README is a front-to-back usage guide. Where any document conflicts with
the code, **the code wins**; mechanical drift guards
(`tests/test_docs_drift.py`) keep the command tables, flags, and config keys
here and in `docs/` in sync with the code, while the narrative is maintained by
hand and verified best-effort. The original build spec,
[`redbox-tracker-spec.md`](redbox-tracker-spec.md), is kept as a historical
design document. Companion references:

- [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) — every config key and
  secret, `--config`/local-layer semantics.
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md) — scaling up (a state / the
  country), timeouts & watchdogs, deploy operations, maintenance, and the
  project's limitations.
- [`docs/DATA_MODEL.md`](docs/DATA_MODEL.md) — tables and the semantic enums
  (`inactive`, `scan_status`, `url_source`, change event types, publish
  statuses).
- [`ROBOTS_POLICY.md`](ROBOTS_POLICY.md) — the crawler posture.

> ### ⚠️ Read this first — ethics & legal safety
> Red-boxing is **not per se unlawful**; it exploits the rules openly. This tool
> is built to be careful about that:
> - **Attribution is confirmed before publication.** Scanning is a read-only
>   fetch of public pages, so a site isn't gate-checked before a scan; the human
>   review gate confirms a flagged site really belongs to the candidate before
>   anything is published. (Set `require_verified_url: true` to require
>   human-verified URLs *before* scanning instead.)
> - **No positive is ever published without a human approving it.** Detections sit
>   in a review queue; the site marks them "pending review — not published."
> - **Published language is disciplined** (spec §3.7a): *"posted public messaging
>   guidance consistent with red-boxing"*, never "does not red-box"; negatives are
>   dated; every claim links to archived evidence.
> - **Publishing is a separate, explicit act.** `publish` builds locally;
>   nothing reaches the public site until you run `deploy`, which only ever
>   ships the strict approved-only build (and sweeps stale pages so removed
>   candidates don't linger at old URLs). Syndication (RSS/JSON feeds, the
>   Bluesky poster) draws exclusively from published — i.e. human-approved —
>   findings.
> - **Withdrawn / wrong-race / defeated candidacies are pruned** (`mark-inactive`,
>   `mark-primary-losers`, and human calls in the review console) so the tracker
>   never scans or publishes a phantom record.
> - **Do not run live crawls against real candidate sites without authorization.**
>   `scan` / `scan-all` are dry runs unless you pass `--authorize`.
> - **One-sided by subject matter, not by intent.** Red-boxing is, in current
>   practice, overwhelmingly a Democratic-side tactic, so this tool's findings
>   structurally concern one party's candidates. The target is the *practice* —
>   public signaling to outside spenders — wherever it appears; the same
>   functional test applies to any campaign of any party that adopts it.
> - **The exact classifier prompt and production model choices are not
>   published.** A public detector spec is a test target for campaigns writing
>   red boxes to evade it. The repo ships a functional starter prompt and
>   sensible default models; the production deployment's tuned prompt and model
>   selection live in untracked local files (`data/prompts/classifier.txt`,
>   `config.local.yaml`).
> - **Forks are not RedBoxWatch.** This code is open source (AGPL-3.0), but
>   the human-review discipline above is a practice, not a technical guarantee —
>   nothing stops a fork from publishing unreviewed output. Findings published
>   anywhere other than **redboxwatch.org** are not findings of this project,
>   and forks may not present themselves as affiliated with it.

---

## 1. Install

Prerequisites: **Python 3.11+** and git. Then:

```bash
cd RedboxFinder
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium      # headless browser used by the crawler
```

### API keys

Two secrets are required with the default config (plus optional Serper and
Wayback keys). They live in a **`.env` file**:

```bash
# .env
FEC_API_KEY=your_api_data_gov_key          # https://api.data.gov/signup/  (1,000 req/hr)
ANTHROPIC_API_KEY=your_anthropic_key        # billable — classifier + Serper judge
# Optional — Serper.dev (Google) key for URL resolution. When set, it's the
# search backend (better recall, ~50x cheaper than Claude web_search): serper.dev
SERPER_KEY=your_serper_key
# Optional — authenticated Internet Archive Save-Page-Now (recommended at scale):
WAYBACK_S3_KEY=your_archive_org_access_key
WAYBACK_S3_SECRET=your_archive_org_secret
```

- The **FEC key** is free from api.data.gov (the env vars `DATA_GOV_API_KEY`
  and `OPENFEC_API_KEY` are accepted as aliases). Without one, the code falls
  back to `DEMO_KEY` (30 req/hr).
- The **Anthropic key** powers the classifier (cheap first pass, stronger
  escalation), the Serper judge, and the `web_search` fallback. You are billed
  per classification (pages are short and cheap; a full site scan is a few
  cents). Model strings are `provider/model` — an unprefixed name means
  Anthropic; `fireworks/` (needs `FIREWORKS_API_KEY`) and `openai/` (needs
  `OPENAI_API_KEY`) models are also supported. The shipped defaults are sensible
  starting points, **not** the production deployment's model choices — validate
  your own with `scripts/eval_classifier_models.py` and set them in the
  untracked `config.local.yaml`.
- The **Wayback S3 keys** are optional. Without them, `--push-wayback` uses the
  anonymous Save-Page-Now endpoint, which is heavily rate-limited and a
  bottleneck when archiving many positives. With them, the archiver uses the
  authenticated JSON+polling flow (higher limits). Get a free key pair at
  **https://archive.org/account/s3.php** (log in → "Generate"). No code change —
  the archiver auto-detects them.

Everything non-secret is configured in [`config.yaml`](config.yaml) — the full
knob reference (and the `--config` / `config.local.yaml` layering rules) is
[`docs/CONFIGURATION.md`](docs/CONFIGURATION.md). The defaults are sensible;
you can run the whole walkthrough below without touching it.

### Verify the install

```bash
python -m pytest tests/ -q        # the offline test suite: no API calls, no network
python -m redbox --help           # list all commands
```

---

## 2. How it works (the pipeline)

The system is **candidate-centric and scheduled** — it is *not* triggered by
filings. Each candidate flows through these stages:

```
discover ─► resolve ─► scan ──────► corroborate ─► review ─► publish ─► deploy
   │       (URLs)       │              (FEC          (human    (static   (public
 (FEC)                  │             Schedule E)    gate)      site)     site)
   │              crawl→classify→archive
 prune                  │
 (mark-inactive,   scheduler decides
  mark-primary-    who is due, when
  losers)
```

| Stage | Command | What it does |
|---|---|---|
| 1. Discover | `discover` | Build the candidate universe from FEC (fast, free — no URL resolution). |
| 1b. Resolve | `resolve` | Backfill each campaign URL (Wikipedia → FEC committee → Serper search + LLM judge); marked verified or auto-resolved. |
| 1c. Prune | `mark-inactive`, `mark-primary-losers` | Flag FEC-withdrawn/superseded candidacies and defeated primary candidates; excluded from everything downstream. |
| 2. Scan | `scan` / `scan-all` | Crawl the candidate's site, classify each page, archive positives/ambiguous. |
| 3. Corroborate | `corroborate` | Pull FEC Schedule E; align independent-expenditure dollars + committees. |
| 4. Review | `review` / `review-web` | Human approves/rejects each positive — and confirms the site's attribution — before publication. The web console also triages missing site URLs. |
| 5. Publish | `publish` | Build the local review-console / results site. |
| 5b. Deploy | `deploy` | One-shot public release: approved-only build → stale-page sweep → Cloudflare Pages → live smoke check. |
| — Schedule | `calendar`, `schedule` | Per-state primary calendar + scan cadence for ongoing operation. |
| — Maintain | `vacuum`, `backfill-pdf-evidence`, `backfill-changes` | Reclaim DB space (incl. the phantom-404 sweep); re-archive PDFs behind older PDF-sourced detections; reconstruct put-up/take-down history from stored scans (dry-run by default). |

> Optionally pre-verify a URL by hand in `data/websites.json` (untracked)
> (it then shows as "verified" in review). This is not required to scan unless
> `require_verified_url: true` is set in `config.yaml`.

For bulk runs — a whole state or the country — follow this same pipeline via
the field guide in [`docs/OPERATIONS.md`](docs/OPERATIONS.md), which also
covers the concurrency/rate-limit knobs and the gotchas that only show up at
scale.

---

## 3. Walkthrough — a complete run

This follows a real candidate end-to-end: **Haley Stevens (MI-Sen)**. Substitute
any FEC candidate ID to run your own.

### Step 0 — Initialize the database

```bash
python -m redbox initdb
```

Creates `data/redbox.sqlite` with all tables. Safe to re-run (idempotent; also
applies any schema migrations).

### Step 1 — Discover the candidate universe

Pull funded candidates from FEC and group them into contested primaries,
competitive/contested generals, and the baseline cohort (all incumbents +
sole funded nominees — see [§6](#6-how-discovery-decides-spec-31)). Scope it to
keep the FEC calls (and rate limit) modest:

```bash
# One race (fewest API calls — best for a targeted run):
python -m redbox discover --states MI --offices S

# A single House district, or a whole state / multiple states:
python -m redbox discover --states NY --district 12 --offices H
python -m redbox discover --states NC --offices H,S
```

(`--district` is zero-padded for you — `--district 1` matches the FEC's
two-digit `01` form, which used to silently match nothing unpadded.)

Output:
- the `candidates` table in the DB, and
- `data/candidate_universe_2026.csv` (sortable summary).

Re-runs also print an **orphan report**: active candidates in the DB that this
run's universe did not produce (scoped to the run's states/offices). It is
report-only — a row may be a hand-inserted nominee or a filer who dipped below
the floor; the human decides.

FEC responses are cached under `data/.fec_cache/`, so re-runs are fast and don't
re-spend your rate limit. (See [§6 How discovery decides](#6-how-discovery-decides-spec-31)
for the grouping logic.)

**Bulk file (recommended at scale).** If a FEC "weball" candidate-summary file
is present at `data/webl26.txt` (config `weball_path`), discovery reads
candidates + receipts from it — **no per-candidate API calls**, turning a
~10-hour nationwide discovery into ~1 second. Refresh it any time with
`python -m redbox fetch-fec` (or `discover --fetch` to do both in one go):
it downloads the current file from fec.gov, keeps the old one as
`webl26.txt.old-YYYYMMDD`, and refuses to install a truncated download.
Manual alternative: the [FEC bulk data page](https://www.fec.gov/data/browse-data/?tab=bulk-data)
("All candidates"). Pass `--no-bulk` to force the API path instead.

Find a candidate's FEC ID by name if you don't know it:

```bash
curl -s "https://api.open.fec.gov/v1/candidates/?api_key=$FEC_API_KEY&q=stevens&office=S&election_year=2026" \
  | python3 -m json.tool | grep -E 'candidate_id|name'
# -> S6MI00426  STEVENS, HALEY
```

### Step 1c — Prune the universe (ongoing hygiene)

Two commands keep the universe honest as the cycle moves; run them
occasionally (and always before a big scan or publish):

```bash
python -m redbox mark-inactive          # FEC-flagged withdrawn/superseded candidacies
python -m redbox mark-primary-losers    # candidates defeated in a state whose primary has passed
```

- `mark-inactive` refreshes the FEC candidate-inactive flag — typically a House
  member now running for Senate whose old H record still shows cycle money
  (`inactive=1`). FEC-set flags are cleared if the FEC un-flags them.
- `mark-primary-losers` uses the nominee machinery (manual override →
  uncontested-auto → results feed) against past-primary states: in each race
  where a nominee is affirmatively resolved, everyone else lost (`inactive=3`).
  Unresolved races are left alone — a coverage gap must not invent losers.
  A **runoff state** doesn't count as past-primary until its *runoff* date
  (`runoff_date` in `fixtures/primary_calendar_2026.json`) has passed —
  between the rounds, first-round results can't say who lost a race that
  advanced, and the command prints which states it is holding. Clearing marks
  is likewise guarded: a run that would clear more than max(5, 20%) of a
  state's marks refuses unless you pass `--allow-mass-clear` (a feed going
  quiet is not evidence a race un-resolved). `--no-feed` skips the civicAPI
  feed; `--today YYYY-MM-DD` overrides the date.
- Human wrong-race calls from the review console set `inactive=2` and are never
  touched by either command.

Inactive rows are kept for history but excluded from resolve, scan, publish,
and the review console's URL triage queue. All reversible
(`UPDATE candidates SET inactive=NULL WHERE candidate_id=?`).

### Step 2 — Resolve campaign URLs

`discover` does **not** resolve URLs — it just builds the universe (fast and free,
never blocked on a slow web-search call). Resolution is a separate, restart-safe
step (manual override → Wikipedia → FEC committee → web search). Run it after
discover (and any time to fill in candidates that are still unresolved):

```bash
python -m redbox resolve                 # fill in unresolved candidates
python -m redbox resolve --state MI      # one state
python -m redbox resolve --workers 12    # more concurrent lookups (default 8)
python -m redbox resolve --force         # re-resolve everyone (won't touch manual/verified)
python -m redbox resolve --no-search     # skip the billable web-search backup
python -m redbox resolve --no-cache     # bypass the resolution cache
```

`resolve` runs candidates concurrently (`--workers`) and **caches every result to
disk** (`resolve_cache_dir`, keyed by candidate + which backends are active), so a
rerun re-uses prior lookups instead of re-searching. `--force` re-resolves and
refreshes the cache; `--no-cache` skips it entirely. Re-running `discover` later
never overwrites a URL `resolve` already filled in.

Wikipedia covers incumbents and notable candidates (campaign sites only; `.gov`
office pages excluded) — it's what resolves our walkthrough candidate to
`haleyformi.com`. The **search backup** catches most of the rest. Two backends:

- **Serper.dev (Google) + a cheap LLM judge** — used when `SERPER_KEY` is set.
  Serper returns Google results (far better recall than Wikipedia for obscure
  primary challengers); a small Haiku call picks *this* candidate's own campaign
  site, or NONE. Measured **~84% recall on a held-out state (vs ~15% Wikipedia
  alone)** at ~$0.002/candidate, biased hard toward NONE so a coverage gap never
  becomes a misattribution. **This is the preferred backend.**
- **Claude's `web_search` tool** — the fallback when no `SERPER_KEY` (~$0.07
  each).

The backup only fires for candidates the free sources miss; disable per-run with
`--no-search` or globally with `enable_search_backup: false`. Each resolved URL
records which backend found it (`url_source`: `wikipedia` / `committee` /
`serper` / `search` — the full enum is in
[`docs/DATA_MODEL.md`](docs/DATA_MODEL.md)). Resolved URLs are **unverified** —
scanning works anyway, and attribution is confirmed at the human review/publish
gate.

Candidates the whole chain comes up empty for land in the review console's
**website triage queue** (`review-web` → `/urls`, richest-first) where a human
can paste a URL, mark "no site exists," or flag a wrong-race record — see
Step 5.

**Optional — pre-verify a URL by hand.** Add an entry to
`data/websites.json` (untracked) (it then shows as *verified*
in review) and re-run `discover`/`resolve`:

```json
{
  "S6MI00426": {"url": "https://haleyformi.com", "verified": true,
                "note": "verified by hand"}
}
```

To make verification mandatory before any scan (the spec §3.1 behavior), set
`require_verified_url: true` in `config.yaml`.

### Step 3 — Scan the site

`scan` requires a resolved `website_url` and an explicit `--authorize` for live
crawling. (An unverified URL just prints a one-line attribution reminder; it does
not block.) Start with a dry run:

```bash
python -m redbox scan --candidate S6MI00426                 # dry run: shows what it would do
python -m redbox scan --candidate S6MI00426 --authorize     # live crawl + classify
python -m redbox scan --candidate S6MI00426 --authorize --push-wayback   # also archive to the Wayback Machine
```

What a live scan does, per page:
1. **Crawl** — enumerate pages (sitemap, common paths like `/media`, depth-2 link
   crawl, linked PDFs), render each with headless Chromium and extract visible
   *and* hidden DOM text (text nodes only — `<script>`/`<style>` config is
   stripped, so a red box in a hidden div is caught but framework/theme JSON
   isn't). Respects robots + per-domain rate limits (with 429/503 backoff — see
   [`ROBOTS_POLICY.md`](ROBOTS_POLICY.md)); each fetch class (ordinary
   pages, media-kit-style pages, PDFs) is budgeted at `max_pages_per_site`, so a
   sprawling blog can't crowd out the high-value pages. A merely-slow site ends
   as a clean partial at the per-candidate wall-clock ceiling
   (`candidate_wallclock_seconds`; see
   [`docs/OPERATIONS.md`](docs/OPERATIONS.md)).
2. **Pre-filter** — a cheap regex/URL gate skips the LLM on obviously-empty
   boilerplate pages (donate, privacy, careers, 404s with no red-box terms),
   cutting LLM calls and cost. It is conservative by design: media-kit pages
   (`/media`, `/press`, …) and **all PDFs always classify**, and any page with
   red-box signal scans — a page is skipped *only* when its URL is boilerplate
   **and** its text has zero signal. Validated against every page scanned so far:
   no real red box skipped. The LLM remains the source of truth.
3. **Classify** — a cheap first-pass model classifies every page;
   ambiguous/low-confidence results escalate to a stronger model. Strict JSON,
   temperature 0. Model choices are configurable per provider
   (Anthropic/Fireworks/OpenAI) and validated against ground truth with the
   eval harness before adoption.
4. **Archive** (positives/ambiguous only) — full-page screenshot (stored as
   WebP, lossless by default) + raw HTML + extracted text to
   `data/artifacts/<candidate_id>/`, plus a Wayback snapshot with
   `--push-wayback`.
5. **Diff** — on re-scans, compare each page to its prior scan and record
   **put-up / take-down / modified** events.

Positives and ambiguous results are **held for review** — never auto-published.

### Step 4 — Corroborate with FEC Schedule E

For a scanned candidate, pull independent expenditures and align the spend:

```bash
python -m redbox corroborate --candidate S6MI00426
```

Example output (real data, smaller spenders trimmed):

```
STEVENS, HALEY (S6MI00426)
  $22,489,376 in supporting independent expenditures aligned with this candidate from A STRONGER MICHIGAN, CENTER FORWARD COMMITTEE, UNITED DEMOCRACY PROJECT ('UDP'), …
  supporting: $22,489,376 | opposing: $0 | 76 filings
  guidance first detected (by us): 2026-07-09 | supporting IE dated on/after: $3,885
    [S] A STRONGER MICHIGAN: $12,108,976 (24 filings, 2026-06-02..2026-07-09)
    [S] UNITED DEMOCRACY PROJECT ('UDP'): $9,232,573 (14 filings, 2026-06-09..2026-07-08)
    [S] CENTER FORWARD COMMITTEE: $999,999 (5 filings, 2026-05-11..2026-05-27)
```

This is **corroboration, not a trigger or an accusation of illegality**. It reports
the full aligned spend (the newsworthy signal) *and*, transparently, the strict
"supporting IE dated on/after our detection" figure — which is often near $0
because we usually crawl *after* the box (and the money) went up. Run with no
`--candidate` to corroborate every candidate that has a positive detection.

### Step 5 — Review (the human gate)

The comfortable way is the **web review console**:

```bash
python -m redbox review-web                 # http://127.0.0.1:8001/
```

It shows the pending queue (template aliases — the same page body under several
URLs — collapsed to one reviewable finding), and per detection everything a
reviewer needs on one page: the quoted evidence spans, the classifier's
rationale and confidence, the archived screenshot / raw HTML / extracted text,
the Wayback link, the IE corroboration, and a prominent warning when the site
URL was auto-resolved (confirm attribution before approving!). Approve, reject,
or mark needs-more with a reviewer name and notes; after each decision it jumps
to the next pending finding.

Group actions cover all alias pages at once — with a safety rail: the
"apply to all aliases" box is pre-checked only while **every** sibling is
undecided. Once any alias already has a decision on record, the console shows
each sibling's current state and unchecks the box by default, so a group
fan-out never silently overrides an earlier judgment. Decisions are
**append-only history** — re-review a detection to change its outcome; the
latest decision wins.

**Multi-committee duplicates.** Occasionally the *same page body* is detected
under two different committees (e.g. one person's House and Senate records, or
a shared vendor template) — only one race's attribution can be right. Both the
console and `review --list` flag these with a MULTI-COMMITTEE warning naming
the twin detection(s) and their review state; the workflow is
**approve the committee whose race the guidance targets, reject the other**.

The console has a second queue at **`/urls` — website triage**: every candidate
the resolution chain came up empty for, richest first. Paste a hand-found URL
(recorded as human-verified, appended to `data/websites.json`, and the
candidate's `scan_status` is cleared so the next `scan-all` re-crawls the
corrected address), mark "no campaign site exists" (a dated coverage note, not
a clean negative), or flag a wrong-race/phantom record (sets `inactive=2`).
If the candidate already *has* a URL, the form shows it and requires an
explicit "replace the existing URL" confirmation — a human correction is never
silent. No-site and wrong-race calls are appended to `data/url_triage.json`
(outcome, reviewer, timestamp) as the durable human record.

Built for fast triage from the keyboard: on the queue <kbd>j</kbd>/<kbd>k</kbd>
select and <kbd>Enter</kbd> opens; on a detection <kbd>a</kbd>/<kbd>r</kbd>/<kbd>n</kbd>
pick approve/reject/needs-more, <kbd>g</kbd> toggles the alias-group checkbox,
<kbd>Enter</kbd> submits, and <kbd>q</kbd> returns to the queue (keys are inert
while typing in a form field). The console binds to **127.0.0.1 only**
(pending detections are unpublished allegations), and POSTs are additionally
guarded by Host/Origin loopback checks — a malicious page in the reviewer's
browser can't fire a cross-origin form POST at the console, and a DNS-rebound
hostname is refused. It never publishes anything itself — the site is still
built explicitly in Step 6.

The same gate is scriptable from the CLI:

```bash
python -m redbox review --list                              # detections awaiting review (incl. multi-committee warnings)
python -m redbox review --detection 2 --action approve      # -> becomes a published finding
python -m redbox review --detection 2 --action reject       # -> recorded, never published
python -m redbox review --detection 2 --action needs_more   # -> re-scan / expand
python -m redbox review --detection 2 --action approve --group   # all template-alias pages at once
```

Until you approve a detection, the published site labels it **pending review** and
treats it as *not* a finding.

### Step 6 — Publish locally & view results

```bash
python -m redbox publish --serve            # build + serve at http://127.0.0.1:8000
# or:
python -m redbox publish                    # build only
open site/index.html                        # open the static site directly
```

The site is self-contained (no server needed). The index is a
sortable/filterable table with five columns — **Candidate, Seat, Party,
Status, Aligned IE** — and a page per candidate carrying the rest: classifier
confidence, pages scanned, the quoted evidence, the archived screenshot, the
Wayback link, the IE corroboration, and a put-up/take-down timeline. A
candidate with boxes on several pages shows one **exhibit** per distinct page
body (template-alias URLs collapsed), and an exhibit whose guidance was
*revised* links its **earlier versions** — each with its own archived
evidence, lifespan, and a quote-level diff. It also generates **about**,
**methodology**, and **corrections/appeals** pages.

- Default build is the **review console** (includes pending detections, clearly
  marked).
- `python -m redbox publish --approved-only` is the **strict public build** —
  approved findings + dated negatives only, applied per detection: a pending
  exhibit (or an unapproved earlier version) never renders even when the
  candidate has a separate approved finding. With `publish.site_url` set it
  also emits `sitemap.xml`, `robots.txt`, canonical/OpenGraph tags, and the
  new-findings feeds (`feed.xml` RSS 2.0 + `feed.json` JSON Feed 1.1).
- **Internal links** are extensionless (`/S6MI00426`) on public builds — the
  form Cloudflare Pages actually serves — and plain `.html` on local builds so
  the output still browses from `file://`.

Every build (review builds included) re-syncs `site/evidence/` to exactly the
evidence the current build references, so a rejected detection's screenshot
doesn't stay on disk (or deployed) after its page is gone.

**Feeds & syndication.** The public feeds carry the 50 most recently approved
findings (the index stays the full ledger). Each item's guid is the candidate
id plus a fingerprint of the *approved exhibit bodies* (their text hashes), so
approving a new or revised box **re-announces** the candidate, while
re-detections of a known box and primary/alias URL flips stay silent. The
`feed.json` drives a small Cloudflare Worker
([`workers/bsky-poster`](workers/bsky-poster)) that posts each newly published
finding to **@redboxwatch.bsky.social** — it dedupes via KV and absorbs
guid-scheme migrations without posting (see its README). Because the feeds are
generated only from the approved-only build, nothing can reach social media
that a human didn't approve.

### Step 7 — Deploy the public site

```bash
python -m redbox deploy --dry-run           # build + sweep only, nothing ships
python -m redbox deploy                     # the real release
```

`deploy` is the one-shot public release: it first runs the **full test suite**
(a deploy can happen between commits, so CI alone can't guarantee the code
being deployed is green — a failure aborts the release; `--skip-tests`
bypasses), rebuilds the **approved-only** site into the same `site/` directory
`publish` uses, sweeps **stale HTML pages** out of it (any page absent from
the freshly built sitemap — so candidates removed since the last build don't
linger at old URLs), deploys via `npx wrangler pages deploy` to the
Cloudflare Pages project in `publish.pages_project`, then **smoke-checks** the
live URL until it serves *this build's* footer fingerprint (a bare 200 would
also pass for the previous deploy). It requires `publish.site_url` and
`publish.pages_project` in `config.yaml` and a wrangler login
(`npx wrangler login`).

If the stale sweep **refuses** — sitemap missing/empty, or the sweep would
delete most of the site — deploy exits 1 and **nothing is deployed**; that
almost always means `publish.site_url` doesn't match the built sitemap. If the
wrangler step fails, `site/` is already built — re-run `deploy` to retry.
Deploy-operations details (dot-entry purge, fingerprint check) are in
[`docs/OPERATIONS.md`](docs/OPERATIONS.md).

### Ongoing operation — scheduling

For continuous monitoring rather than a one-off walk:

```bash
python -m redbox calendar                   # load the 2026 per-state primary calendar
python -m redbox schedule                   # which candidates are due to scan today
python -m redbox schedule --today 2026-06-10 # cadence as of a given date
```

The cadence: scan **daily** in the final ~3 weeks before a candidate's primary
*and again before the general*, **weekly** otherwise, from the filing deadline
through election day. Candidates still active past their primary are presumed
advancers (losers get marked inactive) and stay on cadence keyed to the
general; a candidate with an unknown primary date falls back to
general-election cadence rather than dropping to the bottom of the queue.
Cadence is measured from the last **attempt** (`last_attempt_at`), so a
robots-blocked or unreachable site waits out its interval like everyone else
instead of being re-hit daily. Never schedules a candidate with no resolved
URL (add `require_verified_url: true` to also require human-verified URLs).
`schedule` prints the due list; `scan-all --due --authorize` scans exactly
that set in priority order — including previously-scanned candidates (unlike
plain `scan-all`, which skips anything already attempted; that matters for
partial scans — see [`docs/OPERATIONS.md`](docs/OPERATIONS.md)). As primaries
pass, re-run `mark-primary-losers` so defeated candidates drop out of the
rotation. States that moved individual races off their statewide date (AL's
rescheduled CDs, LA's postponed House primary) are handled per-race via
`overrides` in `fixtures/primary_calendar_2026.json`; runoff states carry a
`runoff_date` and aren't treated as settled until it passes.

---

## 4. Command reference

| Command | Key flags | Purpose |
|---|---|---|
| `initdb` | — | Create the SQLite schema. |
| `discover` | `--fetch` `--states NC,TX` `--district 12` `--offices H,S,P` `--mode primary\|general\|full` `--no-feed` `--no-cache` `--no-bulk` | Build the candidate universe (from the FEC bulk file if present, else the API). `--mode` picks contested-primary / nominee / per-race-phase selection; `--no-feed` skips the civicAPI results feed for nominees. Prints an orphan report for rows the run no longer produces. |
| `mark-inactive` | `--no-cache` | Flag FEC-inactive (withdrawn/superseded) candidacies (`inactive=1`); cleared if the FEC un-flags them. |
| `mark-primary-losers` | `--today YYYY-MM-DD` `--no-feed` `--allow-mass-clear` | Flag primary losers in past-primary states (`inactive=3`) via the nominee machinery; never guesses unresolved races, holds runoff states until the runoff passes, and refuses mass-clears of existing marks without `--allow-mass-clear`. |
| `resolve` | `--state NC` `--workers 8` `--force` `--no-search` `--no-cache` | Backfill campaign URLs onto existing candidates (manual → Wikipedia → FEC committee → Serper+judge search); concurrent + disk-cached. |
| `scan` | `--candidate ID` `--authorize` `--push-wayback` | Crawl → classify → archive one candidate's site. |
| `scan-all` | `--state NY` `--workers 4` `--authorize` `--push-wayback` `--rescan` `--due` | Scan many candidates concurrently across domains (dry-run without `--authorize` lists targets + cost preflight). `--due` scans exactly the scheduler's cadence-due set, priority-ordered, re-scanning already-attempted candidates; plain runs skip them (`--rescan` to redo). |
| `corroborate` | `--candidate ID` `--no-cache` | FEC Schedule E alignment (omit `--candidate` for all positives). |
| `review` | `--list` `--detection N` `--action approve\|reject\|needs_more` `--group` `--reviewer` `--notes` | Human gate on positives (CLI); `--group` applies to all template-alias pages; `--list` flags multi-committee duplicate detections. |
| `review-web` | `--port 8001` | Serve the local web review console (detections queue + `/urls` website triage; binds 127.0.0.1 only, loopback-checked POSTs). |
| `publish` | `--approved-only` `--serve` `--port 8000` | Build (and optionally serve) the static site locally. |
| `deploy` | `--dry-run` `--skip-tests` | Public release: full test suite (aborts the release on failure; `--skip-tests` bypasses) → approved-only build → stale-page sweep → Cloudflare Pages deploy → live build-fingerprint smoke check. |
| `calendar` | — | Load per-state primary dates (incl. per-race `overrides` and `runoff_date`) into the DB and backfill them onto candidates. `discover` also runs this automatically. |
| `fetch-fec` | `--cycle 2026` | Download the FEC bulk candidate file to `data/` (validated, previous file kept as a dated backup). |
| `schedule` | `--today YYYY-MM-DD` | List candidates due for a scan. |
| `vacuum` | `--dry-run` | Reclaim DB space: strip deprecated raw_html, sweep phantom 404/410 scans of never-usable URLs, compact. |
| `backfill-pdf-evidence` | `--authorize` `--push-wayback` `--force` | Refetch + archive PDFs behind existing PDF-sourced detections. Skips any whose text hash no longer matches (the archive would misrepresent the evidence); `--force` overrides that guard. |
| `backfill-changes` | `--apply` | Reconcile `change_events` against a replay of the whole scan history under the current diff rules: inserts events old logic dropped, deletes ones manufactured from error/blocked fetches, corrects mismatched ones. Dry-run by default. |

Global: `python -m redbox --config <file> <command>` — see
[`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) for how an explicit `--config`
interacts with the local overlay layer.

---

## 5. What gets written where

| Path | Contents |
|---|---|
| `data/redbox.sqlite` | All structured data (candidates, scans, detections, archives, IE filings, corroboration, reviews, change events, elections). |
| `data/candidate_universe_<year>.csv` | Human-readable universe summary from `discover`. |
| `data/artifacts/<candidate_id>/` | Archived evidence: `<hash>.webp` / `.html` / `.txt`, plus `<hash>.pdf` for PDF-sourced evidence (screenshot falls back to `.png` without Pillow). |
| `data/.fec_cache/` | Cached FEC API responses. |
| `data/.resolve_cache/` | Cached URL-resolution results (`resolve`). |
| `data/websites.json`, `data/url_triage.json` | Untracked human-owned sidecars: manual URL overrides; the `/urls` triage decisions (no-site / wrong-race calls). |
| `site/` | Generated static site (`publish`/`deploy`): per-candidate pages, `index.html` (+`index-2.html`… when paginated), `index-data.json`, `about`/`methodology`/`corrections` pages, `404.html` (its presence disables the Pages soft-200 SPA fallback), `styles.css`, the favicons (`favicon.svg`/`favicon.png`/`favicon.ico`/`apple-touch-icon.png`), `evidence/` (re-synced on every build to exactly what the build references), plus `sitemap.xml`, `robots.txt`, `feed.xml`, `feed.json` on public builds. |

`data/` and `site/` are gitignored (they hold scraped evidence and build output);
regenerate them from the commands above.

---

## 6. How discovery decides (spec §3.1)

1. Pull active, fund-raising candidates per office from openFEC. (Presidential
   (`P`) records are only considered in presidential cycles.)
2. Keep only those at/above `receipts_floor` (default **$50k**) — drops paper
   candidates.
3. **Person-dedupe:** collapse one person's multiple FEC IDs (same name + race +
   identical receipts → shared committee) so duplicate IDs don't fake a contested
   primary.
4. **Contested primary:** any (office, state, district, party) group with **≥2**
   funded candidates.
5. **Contested-general money screen:** a district where ≥2 candidates from ≥2
   parties each clear `general_receipts_floor` (default **$100k**) is a contested
   general — those candidates enter even with uncontested primaries. Pure FEC
   data: red-boxing follows money, not ratings.
6. **Competitive-general ratings overlay:** candidates in districts a pluggable
   ratings adapter marks Tilt/Lean/Toss-up. Only the offline fixture adapter
   ships with the repo, and its data (`fixtures/ratings/2026.json`) is a
   **5-row demo file**, not real coverage — replace it, or add a live adapter
   when you have licensed ratings access (`rating_source: none` disables the
   overlay).
7. **Baseline coverage** (people look up safe-seat members too):
   `include_incumbents` puts every sitting incumbent in the universe even with
   no funded opposition — "scanned, clean" is itself the answer visitors want —
   and `include_funded_nominees` adds a race's sole funded candidate of a party
   (uncontested primary → presumptive nominee) if they clear `receipts_floor`.

   **Scan modes (`scan_mode` / `discover --mode`).** The above is `primary` mode.
   `general` mode instead builds the universe of **primary winners (nominees)** —
   who is actually on the November ballot — and `full` mode mixes the two per
   race, scanning the contested field while a state is pre-primary and the nominee
   once it has voted (states primary on different dates; the per-state calendar is
   `fixtures/primary_calendar_2026.json`). A nominee is mapped to its FEC candidate
   by `(state, office, district, party)` via, first hit wins: **manual override**
   (`data/nominees/<cycle>.json`, *verified*) → **uncontested-auto** (the lone
   funded candidate of that party, *high*) → **results feed** (a called primary
   winner crosswalked by name, *medium*; default backend **civicAPI** — free and
   key-less). Each candidate's `nominee_source` is stored for the review gate.
   Nominee selection only decides *who is scanned*, never what is published, so an
   unverified feed is a safe input; contested races a feed can't resolve are
   surfaced (not guessed) and fall to the override file. civicAPI's federal-primary
   coverage **varies by state** (rich for some, sparse for others) — validate
   before relying on it with `python scripts/validate_civicapi.py --state XX`.
   The same machinery powers `mark-primary-losers` in primary-mode DBs.
8. **Website resolution** (first hit wins):
   1. **manual override** (`data/websites.json`) → *verified*
   2. **Wikipedia** — campaign site from the candidate's infobox, via the open
      MediaWiki API → *unverified*
   3. **FEC committee metadata** → *unverified* (free; FEC's `website` field is
      almost always empty, so this rarely fires; source tag `committee`)
   4. **search backup** → *unverified* (**billable**; last resort, only for
      candidates the free sources missed). Two backends: **Serper.dev (Google) +
      a cheap Haiku judge** when `SERPER_KEY` is set (preferred — ~84% recall,
      ~$0.002/candidate, source tag `serper`), else **Claude's `web_search`**
      tool (source tag `search`, ~$0.07 each). Disable with
      `enable_search_backup: false` or `--no-search`.

   Verification is a review-time signal; scanning doesn't require it unless
   `require_verified_url` is set. Whatever the chain misses lands in the
   `review-web` `/urls` triage queue for a human.

   **Coverage is realistic.** Wikipedia resolves incumbents and notable
   candidates well (campaign sites only — `.gov` office pages are excluded) and
   misses obscure primary challengers who have no Wikipedia page. It is
   conservative: when unsure of the right person it resolves nothing rather than
   risk a misattribution.

---

## 7. Testing

```bash
python -m pytest tests/ -q                  # the offline suite (no network, no API spend)
```

The same suite runs in GitHub Actions on every push/PR
(`.github/workflows/tests.yml`) — it needs no browsers or API keys. CI cannot
deploy: the DB and archived evidence are local-only/gitignored by design. The
suite includes drift guards (`tests/test_docs_drift.py`) that fail when a CLI
command/flag or config key falls out of sync with this documentation.

The classifier's labeled-fixture acceptance test hits the live model APIs and
is therefore opt-in:

```bash
REDBOX_LIVE_LLM=1 python -m pytest tests/test_classifier_fixtures.py -v
```

There is also a model-eval harness (`scripts/eval_classifier_models.py`) that
replays every ground truth — labeled fixtures, known red boxes, and sampled
negatives — against a candidate classifier model before it's adopted; validate
any model or prompt change with it before switching production over.

---

## 8. Project layout

```
redbox/
  cli.py            # command-line entry point (python -m redbox)
  config.py         # config.yaml + .env loading (see docs/CONFIGURATION.md)
  db.py             # SQLite schema + migrations (see docs/DATA_MODEL.md)
  fec.py            # openFEC client (cached): candidates, totals, Schedule E
  weball.py         # FEC bulk candidate-summary file reader (fast discovery)
  discovery.py      # candidate universe (phase 1)
  nominees.py       # nominee resolution + primary-loser flagging (general/full modes)
  ratings/          # pluggable race-rating adapters
  website.py        # campaign-URL resolution + verified/unverified gating
  crawler.py        # enumeration + Playwright render + fetch budgets + backoff (phase 2)
  pdf.py            # linked-PDF download + text extraction
  robots.py         # robots.txt policy (respect/override, per-domain)
  archiver.py       # WebP screenshot/HTML/text + Wayback Save-Page-Now (phase 2)
  prefilter.py      # cheap regex/URL gate to skip the LLM on empty pages
  classifier.py     # multi-provider classifier (Fireworks/Anthropic/OpenAI), escalation, strict JSON (phase 3)
  ratelimit.py      # per-domain crawl politeness (one shared limiter per run)
  ratelimit_tokens.py  # per-model tokens/min buckets for concurrent scanning
  pipeline.py       # scan orchestration + change-diffing + scan dispositions
  util.py           # shared helpers: timestamps, hashing, party codes, state names
  corroboration.py  # FEC Schedule E alignment (phase 4)
  reviewweb.py      # local web console: review gate + /urls website triage (phase 5)
  render.py         # shared presentation: labels, STATUSES table, static assets
  assets/           # site stylesheet, index script, favicons
  publisher.py      # static review-console / results site + feeds + sitemap (phase 6)
  scheduler.py      # primary calendar + scan cadence (phase 7)
workers/
  bsky-poster/      # Cloudflare Worker: feed.json -> @redboxwatch.bsky.social (own README)
scripts/            # model-eval harness + civicAPI coverage validator
docs/               # configuration + operations + data-model references, frozen audit logs
fixtures/           # offline test fixtures + ratings demo + primary calendar
tests/              # offline test suite (incl. docs-drift guards) + opt-in live classifier test
config.yaml         # all non-secret settings (config.local.yaml, untracked, overrides it)
```

---

## License

[AGPL-3.0](LICENSE). Copyleft is deliberate: if you run a modified version of
this tool as a network service, the AGPL requires you to publish your changes.
You may not present a fork as affiliated with RedBoxWatch (also styled
"Red Box Watch") / redboxwatch.org.
