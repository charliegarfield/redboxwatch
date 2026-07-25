# Nationwide-scale findings

Audit done 2026-06-02 (three static passes + a live run on 7 unseen New Jersey
House candidates). NY today is ~62 candidates / ~5,800 pages; "nationwide" means
~2,000–5,000 candidates and ~200,000+ pages, scanned concurrently.

Severity: **BLOCKER** (breaks or is unusable) · **SLOW** (works, costly/painful)
· **MINOR**. Each item cites `file:line` and, where we saw it, the live evidence.

---

## Fixed (2026-06-03)

1. **`discover` resolved every URL inline, sequentially, and the web-search call
   had no timeout.** `discovery.build_universe` now does NOT resolve — resolution
   is the separate, restart-safe `resolve` step; `SearchResolver` got a hard
   `timeout` (`website.py`). *Live before fix:* NJ discover hung indefinitely on a
   web-search call. *After:* discover runs in ~0.1s. Also fixed the `persist`
   UPSERT so re-running discover no longer wipes a URL `resolve` filled in.
2. **`scan-all` re-scanned everything on restart.** Now skips candidates whose
   `scan_status` is set; `--rescan` forces a redo. (`cli.py` cmd_scan_all.)
3. **Commit-per-row in the scan pipeline.** `scan_candidate` now does all
   network/LLM/disk work first, then one short write transaction per page
   (scan + detection + archive row + change), ~1 commit/page instead of 3–4 —
   without ever holding the SQLite write lock across a network call. (`pipeline.py`.)
4. **Silent 0-page coverage gap.** A reachable-but-empty/unreachable site now
   records `scan_status='fetch_failed'` (distinct from `robots_blocked` and from
   NULL "never scanned"); surfaced in the publisher. *Live:* Coleman returned 0
   pages with a NULL status before this. (`pipeline.py`, `publisher.py`.)

---

## Fixed (2026-06-04)

5. **Low-yield, expensive resolution → Serper + LLM judge.** Replaced the primary
   search backend: Serper.dev (Google) retrieval + a cheap Haiku judge that picks
   the candidate's own site (or NONE). Validated on NJ House: **~84% recall vs
   ~15% Wikipedia-only**, ~50× cheaper than Claude `web_search`, **zero
   misattributions** (judge biased toward NONE; output must match a presented
   result + pass the denylist). Used when `SERPER_KEY` is set; `web_search` is the
   fallback. `url_source` now records which backend resolved each URL. Note
   `website.py` still caches nothing and resolution is still **sequential** — a
   resolution cache + concurrency remain worthwhile follow-ups.

## Fixed (2026-06-24)

6. **Publisher was N+1 and emitted one unbounded page.** `_gather` now runs a
   constant set of aggregate queries (counts grouped by candidate; top detection
   / latest review / archive / corroboration via `ROW_NUMBER()` window functions)
   and stitches results in memory — **7 queries for 62 candidates, same 7 for
   thousands** (was ~6×N). `index.html` is paginated (`publish.page_size`, default
   500), ordered actionable-first so findings/pending lead page 1. (`publisher.py`,
   `cli.py`.) *Verified:* 62-candidate NY DB builds in 7 SELECTs across 3 pages.
7. **Resolution was sequential and uncached.** `cmd_resolve` now resolves
   candidates concurrently (`--workers`, default 8; network/LLM in threads, all DB
   writes batched on the main thread) and `WebsiteResolver` has a `fec.py`-style
   disk cache keyed by candidate + active-backend fingerprint, so reruns don't
   re-search. `--force` bypasses the cache read (and refreshes it); `--no-cache`
   disables it. (`website.py`, `cli.py`, `config.py`: `resolve_cache_dir`.)
8. **Classifier fan-out multiplied and the token limiter lumped models.** Two
   fixes: (a) one **shared** chunk-classification pool across all scan-all workers
   (`models.classify_pool_size`) caps global in-flight LLM calls instead of letting
   them grow as `workers × concurrency`; (b) the token limiter is now **per model**
   (`PerModelTokenRateLimiter`) so first-pass Haiku and escalation Sonnet meter
   against independent ceilings (`tokens_per_minute_by_model`) rather than one
   shared budget. (`classifier.py`, `ratelimit_tokens.py`, `cli.py`.) The meter is
   still input-tokens only — that's by design (output limits are a separate,
   generally non-binding ceiling), no longer a noted gap.

---

## Added (2026-06-28)

9. **Crawl held every page's screenshot in memory at once.** `crawl_site` returned
   a `list` of all `FetchResult`s, each carrying a full-page PNG — a 150-page site
   pinned ~150 screenshots × workers (the "memory under concurrency" risk). It is
   now a **generator** (`yield` per page); `scan_candidate` already iterates it, so
   per-worker peak drops from O(pages) to ~1 page. (`crawler.py`.)
10. **No pre-flight cost estimate.** `scan-all` now prints an estimated spend
    before the dry-run/launch, grounded in this DB's own scan history (pages/
    candidate, tokens/page, escalation rate) × config `pricing`. On NY: ~$0.10/
    candidate → ~$110–165 nationally. (`cli.py` `_estimate_scan_cost`, config
    `pricing`.)
11. **Scan modes + nominee resolution (general elections).** `discover --mode`
    (config `scan_mode`): `primary` (contested primaries; unchanged default),
    `general` (primary winners / nominees), `full` (per-race phase off the primary
    calendar). Nominees resolve via manual override → uncontested-auto → results
    feed, mapped to FEC IDs by `(state,office,district,party)` + fuzzy name; stored
    `nominee_source`. (`nominees.py`, `discovery.py`, `db.py` migration, `cli.py`.)
    - **civicAPI is a viable free feed but coverage varies by state.** Validated
      with `scripts/validate_civicapi.py`: **TX** auto-resolved ~89% of funded
      contested races (31 feed + 26 uncontested, 4 left to override); **NC** was
      sparse (only ~4 called federal races — uncontested-auto carried 21/23, 7
      contested unresolved). Treat as an input, never an authority; the probe
      caught two real bugs pre-flight (runoff duplicate races → keep latest date;
      same-surname crosswalk ambiguity → rank by first-name strength). Residual
      misses (middle-name nicknames, compound surnames) fall to the override file.

---

## Fixed (2026-07-03)

12. **`max_pages` cap was bypassable — crawl (and LLM spend) unbounded on
    blog-heavy sites.** "High-value" is a substring match (`/news` matches every
    post under a `/news/` blog) and those pages skipped the cap entirely AND the
    prefilter (`MEDIA_PATHS` matches the same paths), so one deep news archive
    meant an unbounded crawl at 2s/page plus an LLM call per post. *Live:* Mejia
    scanned 211 pages against a 150 cap. Now each class — ordinary pages,
    high-value pages, PDFs — draws on its OWN budget of `max_pages` fetches
    (hard total 3x per site); high-value pages still fetch first, and a bloated
    page crawl can't starve PDF fetches. (`crawler.py` crawl_site.) No
    wall-clock ceiling yet, but worst case is now bounded by 3x max_pages x
    (rate delay + fetch timeout).
13. **`resolve --force` could overwrite a human-verified URL.** The verified
    guard was `and not args.force`, so a forced rerun re-attempted verified
    candidates and a chain hit downgraded `url_verified` to 0. Verified URLs are
    now skipped unconditionally — `--force` re-resolves auto-resolved rows only.
    (`cli.py` cmd_resolve.)

## Added (2026-07-05)

14. **First-pass classifier switched to a cheaper model — ~85% classifier cost
    cut, accuracy-validated.** The classifier accepts `provider/model` strings
    (`fireworks/`, `openai/`, unprefixed = Anthropic; `classifier.py`
    build_llm/RouterLLM/OpenAICompatLLM). Benchmarked 6 candidate models
    against ALL ground truths (7 labeled fixtures + 12 known red boxes + 150
    sampled negatives; harness: `scripts/eval_classifier_models.py`). The
    chosen first pass scored 7/7 fixtures, 12/12 red boxes routed to review,
    0/150 false positives; the cheapest rejected candidate missed 4/12 real
    red boxes (its audience-test reasoning under-fires on segment+channel
    directives), and others fell in between on accuracy, price, or latency.
    Escalation stays on the strongest proven model (rare + accuracy-critical).
    The production model selection and per-model results are deliberately kept
    in the untracked `config.local.yaml` (see README ethics notes) — re-run the
    eval harness to reproduce the comparison for any candidate set.
    Nationwide preflight: **~$28 vs ~$116** (input-only, 942 candidates); live
    acceptance re-run green through the production pipeline (7/7).

---

## TODO — not yet fixed

### Resolution (remaining)
- **MINOR — a bad resolution still crawls the wrong domain.** *Live (pre-Serper):*
  one candidate resolved to a 2008 state-elections-archive PDF and the crawler
  crawled 141 pages of the wrong domain. Serper+judge makes this far rarer (it rejected that case), but the
  scanner still trusts whatever URL it's given — a host sanity check before
  crawling (title/name match) would close it fully.

### Crawl / classify hot path
- **MINOR — per-page screenshot still captured for every page** (`crawler.py`):
  the streaming generator (Added #9) means they no longer accumulate — peak is ~1
  page/worker, not the whole site — but each ordinary page is still screenshotted
  before we know it won't route to review. Capturing only for review-bound pages
  would cut the remaining waste. (OOM risk itself is resolved.)
- **MINOR — leaked Chromium if `PlaywrightFetcher.__exit__` cleanup throws**
  (`crawler.py:124-130`): the three teardown calls are existence-guarded
  (`if self._context:` …) but **not exception-guarded** — if `_context.close()`
  raises, `_browser.close()`/`_pw.stop()` are skipped. Wrap each in its own
  try/finally.
- **MINOR — per-domain rate limiter isn't shared across workers** (`ratelimit.py`;
  fresh instance per worker in `cli.py`), so two workers on the same host don't
  pace each other. Rare (most workers hit distinct domains).
- **MINOR — no WAL checkpoint tuning**; `-wal` can grow over a long run.

### Publisher (only matters once publishing nationally)
- **SLOW — per-build screenshot re-copy.** `shutil.copy2` of every archived image
  on every build with no mtime/hash check (`publisher.py:~143`); gigabytes of
  redundant I/O at thousands of detections. Also keys on `src.name`, so identical
  basenames could collide.

### Discovery / data
- **SLOW — discovery has no incremental persistence** (`cli.py` cmd_discover
  persists only after the full loop). A late failure loses the whole run.
  (`resolve` commits every 10; discover doesn't.)
- **MINOR — FEC cache is unbounded, no TTL** (`fec.py`): tens of thousands of
  small files nationwide; `candidate_totals` is cached, so receipts can go stale
  mid-cycle.
- **MINOR — `discover` writes a year-named CSV** (`cli.py:~84`,
  `data/candidate_universe_<year>.csv`): the name varies by `election_year` but
  **not** by state or DB path, so two configs in the same cycle (e.g. NY vs NJ)
  still clobber one file. (Hit twice during this audit.) Derive the CSV path from
  the DB path stem, or include the state set.
- **MINOR — weball district default** `"00"` collapses Senate / at-large
  (`weball.py:~80`); office is also in the key so collisions are unlikely, but
  worth a second look.

---

## Confirmed solid (don't touch)
Classification quality holds on unseen sites — the live NJ run found **1 true
positive (Hamawy `/media`, correct audience-test rationale) and 0 false
positives** across 433 pages. Token-bucket thread-safety (lock + sleep-outside-
lock). Per-page classification-failure isolation. Unparseable output → ambiguous.
robots caching incl. negative cache. `weball` streaming. FEC retries/backoff/
pagination.
