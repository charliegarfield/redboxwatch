# Operations guide — scale, timeouts, deploy, maintenance

The README walks one candidate end-to-end. This page is the field guide for
running the pipeline at scale (a state, or the country), the timeout/watchdog
machinery that keeps a big run from wedging, deploy operations, ongoing
maintenance, and the project's honest limitations.

## 1. Scaling up — a whole state (or the country)

### The flow

```bash
# 1. Discover from the FEC bulk file (no per-candidate API calls; seconds, free).
#    --fetch re-downloads data/webl26.txt from fec.gov first (backing up the
#    old file); drop it to reuse the file already on disk. Builds the
#    universe only; URL resolution is the separate next step.
python -m redbox discover --fetch --states NY --offices H,S

# 2. Prune before spending anything on the dead weight.
python -m redbox mark-inactive
python -m redbox mark-primary-losers

# 3. Resolve URLs. The search backup is billable but cheap with Serper
#    (~$0.002/candidate); runs concurrently (--workers). --no-search = free only.
python -m redbox resolve --state NY

# 4. Dry-run first — lists exactly which candidates/domains will be hit and
#    prints a cost preflight (pages x models, from the `pricing` config block).
python -m redbox scan-all --state NY --workers 2

# 5. The real run. Scans candidates concurrently across domains. Safe to Ctrl-C
#    and re-run: already-attempted candidates are skipped (use --rescan to redo).
python -m redbox scan-all --state NY --workers 2 --authorize --push-wayback

# 6. Corroborate every positive at once, review, then release.
python -m redbox corroborate
python -m redbox review-web
python -m redbox deploy --dry-run && python -m redbox deploy
```

### Concurrency & rate limits (read before a big run)

`scan-all --workers N` runs N candidates at once. Long pages are split into
chunks that classify concurrently through **one shared pool**
(`models.classify_pool_size`), so total in-flight LLM calls stay bounded
instead of growing as `workers × concurrency`. The real ceiling is **not**
request count — it's your **input-tokens-per-minute** limit, enforced **per
model** (provider limits are per-model): a token bucket each for the
first-pass and escalation models gates classification across all workers, so
sustained scanning doesn't trigger 429s. The per-domain politeness limiter is
likewise **one shared instance across all workers**, so two workers landing on
the same host still honor the crawl delay between them. Three rules of thumb:

- **Set `models.tokens_per_minute_by_model` to your actual provider tiers** (a
  bit under each model's ceiling for margin; keys must match the model
  strings, including any provider prefix). This is the single most important
  knob at scale — too high and you get 429s and dropped pages; too low and
  the run crawls. (`models.tokens_per_minute` is a scalar fallback applied to
  both models.)
- **`models.classify_pool_size`** caps concurrent chunk calls globally; raise
  it only if long pages dominate and you have token headroom.
- **Start with `--workers 2`**, confirm a clean run (no 429 churn in the
  output), then raise. More workers don't help once the token budget is the
  bottleneck.

### What to expect (measured on NY: 62 candidates, ~5,700 pages)

- **Cost**: with the Serper backend, URL resolution is ~$0.002 × the
  candidates the free sources miss — cheap. Scanning is also cheap (prefilter
  + a budget first-pass model). A statewide scan is a dollar or two;
  nationwide (~1,500-candidate universe) is tens of dollars. The `scan-all`
  dry run prints a per-run estimate before you commit. (The old Claude
  `web_search` resolution backend made that the dominant cost at ~$0.07 each —
  Serper is ~50× cheaper there.)
- **Time** is throttle-bound, not compute-bound: a token-limited statewide
  scan runs a few hours. Discovery is seconds (bulk file); resolution runs
  concurrently (`--workers`, default 8 — one search + judge per candidate the
  free sources miss).
- **Coverage gaps are surfaced, not hidden.** Candidates with no resolvable
  site, or whose `robots.txt` blocks our crawler, show distinct **"No site
  found"** / **"Site blocks automated access"** statuses on the published
  index with a coverage-gap banner — never a misleading clean negative. The
  `/urls` triage queue in `review-web` works those gaps down by hand,
  richest-first.

### Gotchas the live runs surfaced (all handled, worth knowing)

- **Sprawling sites.** One site crawled to 1,100+ pages (blog/calendar links).
  `max_pages_per_site` (default 150) is a **per-class fetch budget** —
  ordinary pages, media-kit-style pages, and PDFs each get their own 150
  (hard total 3×), with high-value pages fetched first — so a giant blog
  can't starve out a likely red box.
- **AI-crawler-blocking robots.txt.** Some campaigns allowlist Google/Bing and
  block everyone else; we respect that and mark them "blocked." If you want
  to scan a specific public site anyway, add it to `robots_policy.per_domain`
  as a logged per-site override (one such block was hiding a real red box).
- **Rate-limiting hosts.** The crawler backs off on 429/503 (honoring
  `Retry-After`, capped) and abandons a host for the rest of the crawl after
  3 consecutive rate-limit responses — see `ROBOTS_POLICY.md`. Such a
  candidate simply gets rescanned on a later run.
- **Unverified URLs.** At scale most URLs come from web search and are
  *unverified* — attribution is confirmed by a human at the review gate, and
  nothing publishes without approval. Spot-check a few before trusting the
  set. The scanner otherwise trusts whatever URL it's given — a wrong
  resolution means crawling the wrong site (surfaced in review, not
  published).
- **Interrupting a scan.** Safe to Ctrl-C and re-run. The first Ctrl-C
  cancels every queued candidate and lets the in-flight scans finish cleanly
  (a second Ctrl-C abandons them mid-scan). Each page is persisted
  atomically — the scan row and its detection are written together *after*
  classification — so an interrupt never leaves a fetched-but-unclassified
  row for hash-dedup to silently skip. `scan-all` skips candidates already
  attempted (`scan_status` set) and resumes a half-finished candidate
  page-by-page (done pages are hash-skipped, the rest are scanned). Use
  `--rescan` to force a redo.
- **Disk growth.** Archived evidence adds up; screenshots are transcoded to
  WebP (typically 70–90% smaller than PNG), and `vacuum` reclaims space (see
  §4 below).

## 2. Timeouts, watchdogs & partial scans

A nationwide run must survive hostile or merely broken sites. The guards, from
outermost in:

- **Per-candidate wall-clock ceiling** — `candidate_wallclock_seconds`
  (default 1500 s = 25 min; see [`docs/CONFIGURATION.md`](CONFIGURATION.md)).
  Checked cooperatively **between pages**, so every page's writes stay
  complete: a site that is merely slow across many pages ends as a clean
  **partial** — the pages already scanned are committed, the candidate
  finishes with `scan_status='scanned'` and **`last_scan_partial=1`**, and it
  re-enters on its next cadence day.
- **PDF drip-feed guard** — `fetch_pdf` streams with a **total** 120-second
  wall-clock deadline and a **50 MB** body cap. httpx's read timeout resets
  on every chunk, so a server dripping one byte every few seconds could
  otherwise hold a worker forever; the total deadline closes that.
- **Sitemap-enumeration caps** (`redbox/crawler.py`) — enumeration runs on the
  crawl's first step, *before* the candidate ceiling is ever checked, so it
  bounds itself: at most **25** sitemap-index children fetched, **2,000**
  `<loc>` URLs collected, **120 s** for the whole sitemap pass, and each
  sitemap/robots document is streamed with a **30 s** total deadline and a
  **5 MB** cap. Truncation is silent by design — a partial sitemap is still a
  fine enumeration source; the link crawl covers the rest.
- **The watchdog thread** — `scan-all` runs a daemon thread that checks every
  60 s for any candidate running past the ceiling (+2 min grace) and prints
  who it is and how long it's been running. The ceiling itself is enforced
  cooperatively; a candidate still over it here is wedged *inside* a blocking
  call, and the watchdog makes the hang loud (who + which site) instead of a
  silent 0%-CPU stall.

**Recovery gotcha:** a partial or timed-out candidate ends as
`scan_status='scanned'`, so a plain `scan-all` **skips** it on the next run
(the restart filter only re-attempts `scan_status IS NULL`). To finish it, use
`scan-all --rescan` (or let `scan-all --due` pick it up — the due set ignores
the restart filter and re-scans on cadence). Find partials with:

```sql
SELECT candidate_id, name FROM candidates WHERE last_scan_partial = 1;
```

## 3. Deploy operations

`deploy` and `publish` build into the **same `site/` directory** — a review
build left on disk is simply overwritten by the deploy's approved-only
rebuild, and the evidence sweep (which runs on *every* `build_site`, review
builds included) re-syncs `site/evidence/` to exactly what the current build
references.

The deploy-specific stale sweep is **HTML-only**: after the approved-only
rebuild, any `site/*.html` page absent from the freshly built `sitemap.xml`
is deleted (so removed candidates don't linger at old URLs). `404.html` is
deliberately sitemap-absent and always survives. The sweep **refuses and
aborts the deploy (exit 1, nothing deployed)** when the sitemap is missing or
empty, or when it would delete more than max(10, half) of the pages — both
mean something is wrong upstream (usually a `publish.site_url` /
built-sitemap mismatch). Fix the config mismatch and re-run; mass-deleting a
fresh build is never the failure mode.

Before uploading, deploy purges `.wrangler/` and `.DS_Store` from `site/` and
refuses to upload if any other dot-entry remains. The post-deploy smoke check
polls the live site (~30 s) until it serves **this build's** "Generated …
UTC" footer fingerprint — a plain 200 with the nameplate would also pass for
the *previous* deploy, so a no-op or mis-targeted deploy fails the check
instead of passing silently. If the wrangler step fails, `site/` is already
built — re-run `deploy` to retry.

## 4. Maintenance

- **`vacuum`** — three reclaims in one: strips the deprecated `raw_html` from
  legacy scan rows (fresh DBs never create the column), deletes **phantom
  404/410 scans** — page-absent fetches of URLs that never once served usable
  content (probe noise from guessed common paths; a 404 on a URL that ever
  had content is take-down evidence and is kept, as is any row that
  detections/archives/change-events still reference) — and compacts the file
  with `VACUUM`. `--dry-run` reports first; back up before the real run.
- **`mark-primary-losers`** — run as primaries pass. Two safety rails worth
  knowing at operation time:
  - **Runoff states**: a state with a `runoff_date` in
    `fixtures/primary_calendar_2026.json` is not treated as past-primary
    until the **runoff** has passed — between the rounds, first-round results
    can't say who lost a race that advanced, and the command prints which
    states it is holding.
  - **Mass-clear guard**: clearing an `inactive=3` mark is driven by
    *absence* of feed evidence, so a feed that merely goes quiet for a state
    must not un-mark its settled races en masse. A run that would clear more
    than max(5, 20%) of a state's currently marked rows **refuses those
    clears** (printing the would-be-cleared names); pass
    `--allow-mass-clear` to apply them deliberately. Small corrections — the
    normal self-heal — always apply.
- **`backfill-pdf-evidence`** / **`backfill-changes`** — see the README
  command table. Both are dry-run-by-default reconstruction tools.
- **`discover` re-runs** print an **orphan report**: active candidates in the
  DB that this run's universe did not produce (scoped to the run's
  offices/states/district). Report-only — a row may be a hand-inserted
  nominee, a filer who dipped below the floor, or genuinely gone; the human
  decides.

## 5. Limitations & honesty notes

- **Person-dedup** uses identical receipts as the shared-committee signal; a
  genuinely distinct same-name candidate with coincidentally identical
  receipts (vanishingly unlikely) would be merged.
- **Website resolution** covers manual overrides → Wikipedia → FEC committee
  → search backup. Wikipedia covers candidates with a Wikipedia page
  (incumbents + notable names); the **search backup** (Serper + LLM judge, or
  Claude `web_search`) catches most of the rest but is **billable** and only
  as good as the search results. The judge is deliberately conservative — it
  returns NONE rather than risk a misattribution, so some resolvable sites
  are left as coverage gaps for the `/urls` triage queue. A candidate no
  source resolves isn't scanned (no target). Whether a resolved URL is
  actually the candidate's is confirmed by a human at the review/publish gate
  (or up front via `require_verified_url`).
- **Rating adapters** other than the fixture are best-effort HTML scrapes and
  may break when source markup changes — and the shipped fixture is a 5-row
  demo file, not real coverage.
- **Absence of a finding is not proof** — a box may have been removed, a page
  uncrawled, or a PDF unparsed. Negatives are always dated.
- **The classifier is not perfectly deterministic.** Even at temperature 0,
  borderline pages (long fundraising/gaming pages, ambiguous press kits) can
  flip between `red_box_guidance` and `no_guidance_detected` across runs, so
  a single scan's positive set isn't perfectly stable. This is exactly what
  the human review gate absorbs — treat low-confidence detections as "needs a
  look," not as settled. Clear red boxes (the `/media` kits) reproduce
  reliably. Any first-pass model should be validated against all ground
  truths before adoption (`scripts/eval_classifier_models.py`); keep
  escalation on the strongest model you trust — escalations are rare and are
  the accuracy-critical judgment.
- **Nominee/primary-loser resolution is only as good as its feed.** civicAPI
  coverage varies by state; unresolved races are surfaced, never guessed, and
  fall to the manual override file. Top-two/top-four states (CA/WA/AK) are
  excluded from automatic loser-marking outright.
- **The primary calendar covers the 50 states + DC.** The delegate-electing
  territories (AS, GU, MP, VI) are absent from
  `fixtures/primary_calendar_2026.json` — a known gap, deliberately left
  open rather than filled with fabricated dates; their candidates fall back
  to general-election cadence.
- **Some sites sit behind a WAF** that blocks non-browser fetchers; the
  crawler uses a real Chromium context (per the spec) but does not spoof
  identity to evade blocks.
- **robots.txt-blocked sites are not scanned by default.** Some campaign
  sites allowlist Google/Bing but disallow other crawlers — we respect that,
  so they scan zero pages and are surfaced with a distinct **"Site blocks
  automated access"** status (not a clean negative). The pages are public;
  you can opt in per-site via `robots_policy.per_domain: { host: override }`
  after review. (Seen live: one such block was hiding a real red box.)
- **Scale & rate limits.** `scan-all` runs candidates concurrently; per-model
  token buckets (`models.tokens_per_minute_by_model`) keep classification
  under each provider's per-minute ceiling. Crawls are budgeted per fetch
  class at `max_pages_per_site` so one sprawling site can't dominate — but
  media-kit pages and PDFs have their own budgets and high-value pages fetch
  first. Set the token budgets to *your* provider tiers; provider limits are
  sliding windows, so leave real headroom.
