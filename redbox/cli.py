"""Command-line interface for the Red-Boxing Tracker.

Usage:
    python -m redbox initdb
    python -m redbox discover --states NC,TX --offices H,S [--no-cache]
"""
from __future__ import annotations

import argparse
import sys

from .config import load_config
from .db import init_db
from .discovery import Discovery
from .fec import FECClient
from .ratings.fixture import FixtureRatingAdapter


def _make_rating_adapter(cfg):
    # Only the offline fixture adapter ships with the repo; add live adapters
    # (spec §3.1B) behind cfg.rating_source when you have licensed access.
    return FixtureRatingAdapter()


def _build_token_limiter(cfg):
    """Build a per-model token-rate limiter from config (or None if unconfigured).

    ``models.tokens_per_minute`` (scalar) is applied to BOTH the first-pass and
    escalation models as independent buckets — so neither is left unmetered and
    they no longer share one ceiling. ``models.tokens_per_minute_by_model`` (a
    {model: tpm} map) overrides per model, which is the accurate way to encode
    Anthropic's separate per-model limits.
    """
    models = cfg.models
    limits: dict[str, float] = {}
    scalar = models.get("tokens_per_minute", 0)
    if scalar:
        limits[models.get("first_pass", "claude-haiku-4-5")] = float(scalar)
        limits.setdefault(models.get("escalation", "claude-sonnet-4-6"), float(scalar))
    for model, tpm in (models.get("tokens_per_minute_by_model") or {}).items():
        if tpm:
            limits[model] = float(tpm)
    if not limits:
        return None
    from .ratelimit_tokens import PerModelTokenRateLimiter
    return PerModelTokenRateLimiter(limits)


def _estimate_scan_cost(conn, n_candidates: int, cfg) -> str:
    """Rough pre-flight cost estimate for scanning ``n_candidates`` candidates.

    Grounds per-candidate page/token/escalation rates in THIS DB's own scan
    history when it exists (a classified page == one ``detections`` row; empty and
    pre-filtered pages never reach the LLM, so they don't count), else falls back
    to documented defaults. Prices come from the config ``pricing`` block. This is
    an estimate — actual spend depends on real page counts and content — so it
    prints its assumptions for sanity-checking.
    """
    from .ratelimit_tokens import estimate_input_tokens

    models = cfg.models
    fp_model = models.get("first_pass", "claude-haiku-4-5")
    esc_model = models.get("escalation", "claude-sonnet-4-6")
    pricing = cfg.get("pricing", {}) or {}
    price_fp = float(pricing.get(fp_model, 1.0))
    price_esc = float(pricing.get(esc_model, 3.0))

    row = conn.execute(
        """SELECT COUNT(*) AS dets, COUNT(DISTINCT d.candidate_id) AS cands,
                  AVG(LENGTH(s.raw_text)) AS avg_len, AVG(d.escalated) AS esc_rate
           FROM detections d JOIN scans s USING(scan_id)""").fetchone()
    if row and row["dets"] and row["cands"]:
        pages_per_cand = row["dets"] / row["cands"]
        avg_tokens = estimate_input_tokens("x" * int(row["avg_len"] or 0))
        esc_rate = row["esc_rate"] or 0.0
        basis = (f"from {row['dets']:,} classified pages across "
                 f"{row['cands']} scanned candidate(s) in this DB")
    else:
        pages_per_cand, avg_tokens, esc_rate = 30.0, 2000, 0.15
        basis = "no scan history in this DB — using default assumptions"

    fp_pages = n_candidates * pages_per_cand
    fp_tokens = fp_pages * avg_tokens
    esc_tokens = fp_pages * esc_rate * avg_tokens
    fp_cost = fp_tokens / 1e6 * price_fp
    esc_cost = esc_tokens / 1e6 * price_esc
    return (
        "Cost preflight (ESTIMATE — verify pricing in config.yaml):\n"
        f"  basis: {basis}\n"
        f"  ~{pages_per_cand:.0f} classified pages/candidate x {n_candidates:,} "
        f"candidates = ~{fp_pages:,.0f} pages\n"
        f"  ~{avg_tokens:,} input tok/page; ~{esc_rate*100:.0f}% escalate to {esc_model}\n"
        f"  first-pass {fp_model}: ~{fp_tokens/1e6:,.1f}M tok @ ${price_fp:g}/M = ${fp_cost:,.2f}\n"
        f"  escalation {esc_model}: ~{esc_tokens/1e6:,.1f}M tok @ ${price_esc:g}/M = ${esc_cost:,.2f}\n"
        f"  estimated total: ~${fp_cost + esc_cost:,.2f}")


def _build_nominee_resolver(cfg, *, use_feed: bool = True):
    """NomineeResolver for general/full discovery modes (None feed = uncontested
    auto + manual override only). Returns (resolver, feed_name_or_None)."""
    from .nominees import CivicAPIFeed, NomineeResolver

    feed = None
    if use_feed and cfg.nominee_feed == "civicapi":
        feed = CivicAPIFeed(cycle=cfg.election_year, user_agent=cfg.user_agent)
    return NomineeResolver(cfg.election_year, feed=feed), (feed.name if feed else None)


def _primary_dates() -> dict[str, str]:
    """state -> ISO primary date, from the calendar fixture (for full mode)."""
    import json

    from .scheduler import CALENDAR_FIXTURE

    return {r["state"]: r["primary_date"] for r in json.loads(CALENDAR_FIXTURE.read_text())}


def _build_archiver(cfg, push_wayback: bool):
    """Construct an Archiver, applying the optional ``screenshot`` config block
    (``lossless`` / ``quality``) used to transcode page captures to WebP."""
    from .archiver import Archiver

    shot = cfg.get("screenshot", {}) or {}
    return Archiver(
        cfg.artifacts_dir,
        push_wayback=push_wayback,
        screenshot_lossless=shot.get("lossless", True),
        screenshot_quality=shot.get("quality", 80),
    )


def cmd_initdb(args, cfg) -> int:
    conn = init_db(cfg.database_path)
    conn.close()
    print(f"Initialised schema at {cfg.database_path}")
    return 0


def cmd_discover(args, cfg) -> int:
    states = [s.strip().upper() for s in args.states.split(",")] if args.states else None
    offices = [o.strip().upper() for o in args.offices.split(",")] if args.offices else ["H", "S", "P"]

    fec = FECClient(
        api_key=cfg.fec_api_key,
        cache_dir=cfg.fec_cache_dir,
        user_agent=cfg.user_agent,
    )
    # Prefer the FEC bulk file (no per-candidate API calls) unless --no-bulk.
    weball = None if getattr(args, "no_bulk", False) else cfg.weball_path
    # Discovery no longer resolves URLs (that's the separate `resolve` step), so
    # it needs no web-search resolver — keeping discovery fast, free, and never
    # blocked on a slow Wikipedia/web-search call.
    discovery = Discovery(
        config=cfg,
        fec=fec,
        rating_adapter=_make_rating_adapter(cfg),
        weball_path=weball,
    )
    district = getattr(args, "district", None)
    mode = (getattr(args, "mode", None) or cfg.scan_mode).lower()
    if mode not in ("primary", "general", "full"):
        print(f"--mode must be primary | general | full (got {mode!r})")
        return 2

    # general/full modes scan primary WINNERS (nominees), resolved via
    # uncontested-auto + manual override + an optional results feed (civicAPI).
    nominee_resolver = None
    today = primary_dates = None
    if mode in ("general", "full"):
        nominee_resolver, feed_name = _build_nominee_resolver(
            cfg, use_feed=not getattr(args, "no_feed", False))
        fb = feed_name or "none (uncontested-auto + manual override only)"
        print(f"(nominee resolution: feed={fb}; manual overrides: "
              f"data/nominees/{cfg.election_year}.json)")
    if mode == "full":
        from datetime import date
        today, primary_dates = date.today(), _primary_dates()

    src = ("FEC bulk file " + str(cfg.weball_path.name)
           if weball and cfg.weball_path.exists() else "openFEC API")
    print(f"Discovering universe [{src}] mode={mode}: offices={offices} "
          f"states={states or 'ALL'} district={district or 'ALL'} "
          f"year={cfg.election_year} receipts_floor=${cfg.receipts_floor:,.0f}")
    entries = discovery.build_universe(
        offices=offices, states=states, district=district, mode=mode,
        nominee_resolver=nominee_resolver, today=today, primary_dates=primary_dates)

    conn = init_db(cfg.database_path)
    n = discovery.persist(conn, entries)
    conn.close()

    csv_path = cfg.repo_root / "data" / f"candidate_universe_{cfg.election_year}.csv"
    discovery.to_csv(entries, csv_path)
    fec.close()

    # Summary
    from collections import Counter
    labels = Counter(discovery.reason_label(e.reasons) for e in entries)
    nominees = sum(1 for e in entries if "nominee" in e.reasons)
    by_src = Counter(e.nominee_source.split(":")[0] for e in entries if e.nominee_source)
    print(f"\nUniverse ({mode}): {len(entries)} candidates {dict(labels)}")
    if nominees:
        print(f"  nominees: {nominees} (by source: {dict(by_src)})")
    print(f"  persisted {n} rows to {cfg.database_path}")
    print(f"  wrote {csv_path}")
    print("  next: run `resolve` to backfill campaign URLs "
          "(Wikipedia + optional web search), then `scan-all`.")
    return 0


def cmd_mark_inactive(args, cfg) -> int:
    """Refresh the FEC candidate_inactive flag onto existing candidates.

    Flags candidacies the FEC marks withdrawn/superseded — typically a House
    member now running for Senate whose old H record still shows cycle money.
    Inactive rows are kept for history but excluded from resolve/scan/publish.
    FEC-set flags (inactive=1) are cleared if the FEC un-flags them; human
    calls from the review console (inactive=2) are never touched here.
    """
    from .util import now_iso

    conn = init_db(cfg.database_path)
    ids = [r[0] for r in conn.execute("SELECT candidate_id FROM candidates")]
    fec = FECClient(api_key=cfg.fec_api_key, cache_dir=cfg.fec_cache_dir,
                    user_agent=cfg.user_agent)
    inactive = fec.inactive_ids(ids, use_cache=not args.no_cache)
    fec.close()
    ts = now_iso()
    ids_sorted = sorted(inactive)
    marks = ",".join("?" * len(ids_sorted))
    if ids_sorted:
        flagged = conn.execute(
            f"""UPDATE candidates SET inactive=1, updated_at=?
                WHERE candidate_id IN ({marks}) AND COALESCE(inactive,0) != 1""",
            (ts, *ids_sorted)).rowcount
        cleared = conn.execute(
            f"""UPDATE candidates SET inactive=NULL, updated_at=?
                WHERE inactive=1 AND candidate_id NOT IN ({marks})""",
            (ts, *ids_sorted)).rowcount
    else:
        flagged = 0
        cleared = conn.execute(
            "UPDATE candidates SET inactive=NULL, updated_at=? WHERE inactive=1",
            (ts,)).rowcount
    conn.commit()
    for r in conn.execute(
        """SELECT candidate_id, name, office, state, district FROM candidates
           WHERE COALESCE(inactive,0)=1 ORDER BY state, name"""):
        print(f"  inactive: {r['candidate_id']} {r['name']} "
              f"({r['office']}-{r['state']}-{r['district']})")
    n_total = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE COALESCE(inactive,0)!=0").fetchone()[0]
    conn.close()
    print(f"\n{len(inactive)} FEC-inactive candidacy(ies) ({flagged} newly flagged, "
          f"{cleared} cleared); {n_total} total excluded incl. human calls.")
    print("Inactive rows are excluded from resolve/scan-all/publish; re-run "
          "`publish` to drop them from the site.")
    return 0


def cmd_mark_primary_losers(args, cfg) -> int:
    """Flag primary LOSERS in states whose primary has passed (inactive=3).

    The nominee machinery (manual override -> uncontested-auto -> results feed)
    normally runs only in general/full discover modes; this points it at the
    EXISTING universe so a primary-mode DB sheds defeated candidates too. For
    each race bucket (state, office, district, party) in a past-primary state
    where a nominee is affirmatively resolved, every OTHER candidate in the
    bucket lost and is marked inactive=3 — excluded from resolve/scan/publish
    and the review console's URL triage queue. Unresolved buckets are left
    alone (a coverage gap must not invent losers), and uncontested buckets
    have no losers by construction. Reversible: inactive=3 rows are re-checked
    every run, and `UPDATE candidates SET inactive=NULL WHERE candidate_id=?`
    undoes one by hand.
    """
    from datetime import date

    from .nominees import flag_primary_losers
    from .util import now_iso

    today = date.fromisoformat(args.today) if args.today else date.today()
    past = {st for st, d in _primary_dates().items()
            if d and date.fromisoformat(d[:10]) < today}
    if not past:
        print("No state's primary is in the past — nothing to do.")
        return 0

    conn = init_db(cfg.database_path)
    resolver, feed_name = _build_nominee_resolver(
        cfg, use_feed=not getattr(args, "no_feed", False))
    from .nominees import TOP_TWO_STATES
    skipped = sorted(past & TOP_TWO_STATES)
    if skipped:
        print(f"(skipping top-two state(s) {', '.join(skipped)} — the per-party "
              f"nominee model can't express two same-party advancers; resolve "
              f"those races via overrides or the review console)")
    print(f"{len(past)} state(s) past their primary as of {today} "
          f"(feed: {feed_name or 'none — override + uncontested only'})")
    lost, cleared, unresolved = flag_primary_losers(
        conn, resolver, states=past, ts=now_iso())
    for c in lost:
        print(f"  lost primary: {c['candidate_id']} {c['name']} "
              f"({c.get('office')}-{c.get('state')}-{c.get('district')})")
    n_total = conn.execute(
        "SELECT COUNT(*) FROM candidates WHERE inactive=3").fetchone()[0]
    conn.close()
    print(f"\n{len(lost)} newly marked lost-primary, {cleared} cleared; "
          f"{n_total} total inactive=3.")
    if unresolved:
        print(f"{unresolved} contested race(s) unresolved by the feed — their "
              f"candidates were LEFT IN (coverage gap, not evidence of loss). "
              f"Add calls to data/nominees/{cfg.election_year}.json to close them.")
    print("Re-run `publish` to update the site.")
    return 0


def cmd_resolve(args, cfg) -> int:
    """Backfill campaign URLs onto candidates already in the DB (phase 1).

    Tries each unresolved candidate against the resolution chain (manual override
    -> Wikipedia -> FEC committee -> web search). Resolved URLs are UNVERIFIED
    unless from a manual override; attribution is confirmed at the review gate.
    """
    from collections import Counter
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from .fec import FECClient
    from .util import now_iso
    from .website import WebsiteResolver

    conn = init_db(cfg.database_path)
    where = "WHERE state=?" if args.state else ""
    params = (args.state.upper(),) if args.state else ()
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM candidates {where} ORDER BY state, district, name", params).fetchall()]

    fec = FECClient(api_key=cfg.fec_api_key, cache_dir=cfg.fec_cache_dir, user_agent=cfg.user_agent)
    use_search = cfg.enable_search_backup and not args.no_search
    # --no-cache disables the resolution cache entirely (no read, no write).
    cache_dir = None if args.no_cache else cfg.resolve_cache_dir
    resolver = WebsiteResolver(
        fec_client=fec, user_agent=cfg.user_agent,
        anthropic_api_key=cfg.anthropic_api_key, enable_search=use_search,
        search_model=cfg.search_model, serper_key=cfg.serper_key,
        judge_model=cfg.judge_model, cache_dir=cache_dir)
    if use_search:
        backend = getattr(resolver.search, "name", None)
        if backend == "serper":
            print("(search backup: Serper + LLM judge — billable; use --no-search to disable)")
        elif backend == "search":
            print("(search backup: Claude web_search — billable; "
                  "set SERPER_KEY for cheaper Serper; use --no-search to disable)")
    if cache_dir:
        print(f"(resolution cache: {cache_dir}; --force to re-resolve, --no-cache to bypass)")

    # Decide who to attempt up front; skipped (kept) candidates need no lookup.
    by_source: Counter = Counter()
    to_attempt: list[dict] = []
    for c in rows:
        # NEVER overwrite a human-verified URL — not even with --force. Verified
        # attribution is human-owned; a forced rerun re-resolves auto-resolved
        # rows only (a --force used to downgrade verified URLs to whatever the
        # auto chain returned).
        if c.get("url_verified"):
            by_source["manual (kept)"] += 1
            continue
        # A human checked and found no site (review-console URL triage) — don't
        # re-attempt, and never replace that call with an unverified auto-guess.
        if c.get("url_source") == "human_none":
            by_source["human_none (kept)"] += 1
            continue
        # Not actually running for this seat (FEC candidate_inactive or a human
        # wrong-race call) — nothing to resolve for a phantom candidacy.
        if c.get("inactive"):
            by_source["inactive (skipped)"] += 1
            continue
        # Skip already-resolved (auto) candidates unless --force.
        if c.get("website_url") and not args.force:
            by_source[f"{c.get('url_source')} (kept)"] += 1
            continue
        to_attempt.append(c)

    # Resolve concurrently (each lookup is independent network/LLM I/O), but apply
    # all DB writes on this thread — the SQLite connection isn't thread-shareable.
    # --force bypasses the cache read so a forced run actually re-resolves.
    resolved = attempted = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(resolver.resolve, c, use_cache=not args.force): c
                for c in to_attempt}
        for fut in as_completed(futs):
            c = futs[fut]
            attempted += 1
            try:
                rr = fut.result()
            except Exception as e:                  # defensive: chain steps already swallow
                by_source["error"] += 1
                print(f"  ! {c['name']:38s} resolve failed: {e}")
                continue
            if rr.url:
                conn.execute(
                    """UPDATE candidates SET website_url=?, url_source=?, url_verified=?,
                           updated_at=? WHERE candidate_id=?""",
                    (rr.url, rr.source, int(rr.verified), now_iso(), c["candidate_id"]))
                resolved += 1
                by_source[rr.source] += 1
                print(f"  {c['name']:38s} {c.get('state')}-{c.get('district')} -> {rr.url}  [{rr.source}]")
            else:
                by_source["none"] += 1
                # On a forced re-resolve that now yields nothing, clear a previously
                # auto-resolved URL (never a human-verified one) so the DB reflects
                # the current resolution rather than a stale guess.
                if args.force and c.get("website_url") and not c.get("url_verified"):
                    conn.execute(
                        """UPDATE candidates SET website_url=NULL, url_source='none',
                               updated_at=? WHERE candidate_id=?""",
                        (now_iso(), c["candidate_id"]))
            # Commit incrementally so a long run isn't lost if it crashes partway
            # (each candidate's result is independent).
            if attempted % 10 == 0:
                conn.commit()
    conn.commit()
    conn.close()
    fec.close()
    print(f"\nResolved {resolved}/{attempted} attempted "
          f"({len(rows)} candidates total). By source: {dict(by_source)}")
    print("Resolved URLs are UNVERIFIED — attribution is confirmed at the review gate.")
    return 0


def _scan_one_candidate(candidate: dict, cfg, *, push_wayback: bool, rate_limiter=None,
                        chunk_executor=None):
    """Scan a single candidate in an isolated context (own browser + DB conn).

    Designed to run in a worker thread: Playwright's sync API is not thread-safe
    across a shared browser, and SQLite connections are not thread-shareable, so
    each call creates its own. ``rate_limiter`` (if given) is the SHARED token
    limiter and ``chunk_executor`` the SHARED chunk-classification pool — every
    worker passes the same instances so the org tokens/min ceiling and the global
    in-flight LLM-call count are both enforced across the whole run. Returns the
    ScanOutcome (or raises).
    """
    from .archiver import Archiver
    from .classifier import Classifier, build_llm
    from .crawler import Crawler, PlaywrightFetcher
    from .db import connect
    from .pipeline import scan_candidate
    from .ratelimit import DomainRateLimiter
    from .robots import RobotsPolicy

    robots_cfg = cfg.get("robots_policy", {}) or {}
    rl = cfg.get("rate_limit", {}) or {}
    models = cfg.models
    conn = connect(cfg.database_path)          # own connection for this thread
    try:
        with PlaywrightFetcher(user_agent=cfg.user_agent) as fetcher:
            crawler = Crawler(
                fetcher,
                robots=RobotsPolicy(
                    default=robots_cfg.get("default", "respect"),
                    per_domain=robots_cfg.get("per_domain", {}),
                    user_agent=cfg.user_agent),
                rate_limiter=DomainRateLimiter(rl.get("default_min_delay_seconds", 2.0)),
                common_paths=cfg.common_paths, crawl_depth=cfg.crawl_depth,
                user_agent=cfg.user_agent,
                max_pages=cfg.get("max_pages_per_site", 150))
            llm = build_llm(
                first_pass=models.get("first_pass", "claude-haiku-4-5"),
                escalation=models.get("escalation", "claude-sonnet-4-6"),
                anthropic_api_key=cfg.anthropic_api_key,
                openai_api_key=cfg.openai_api_key,
                fireworks_api_key=cfg.fireworks_api_key,
                max_tokens=models.get("max_tokens", 1024),
                rate_limiter=rate_limiter)
            classifier = Classifier(
                llm,
                first_pass_model=models.get("first_pass", "claude-haiku-4-5"),
                escalation_model=models.get("escalation", "claude-sonnet-4-6"),
                chunk_chars=models.get("chunk_chars", 40000),
                escalate_below=(cfg.get("confidence_thresholds", {}) or {}).get("escalate_below", 0.75),
                concurrency=models.get("concurrency", 8),
                executor=chunk_executor)
            archiver = _build_archiver(cfg, push_wayback)
            return scan_candidate(conn, candidate, crawler=crawler,
                                  classifier=classifier, archiver=archiver,
                                  require_verified=cfg.require_verified_url)
    finally:
        conn.close()


def cmd_scan_all(args, cfg) -> int:
    """Scan many candidates concurrently across domains (phase 1 scale).

    Selects scannable candidates (resolved URL; verified-only if configured) and
    runs N at a time, each in its own browser + DB connection. Live crawling
    requires --authorize. Burst API load ~= --workers x models.concurrency.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    conn = init_db(cfg.database_path)
    # inactive = withdrawn/superseded candidacy (FEC flag or human wrong-race
    # call) — never scan a phantom record.
    where = ["website_url IS NOT NULL", "COALESCE(inactive,0) = 0"]
    params: list = []
    if args.state:
        where.append("state = ?"); params.append(args.state.upper())
    if cfg.require_verified_url:
        where.append("url_verified = 1")
    # Restart safety: by default skip candidates already attempted (scan_status
    # set to 'scanned' | 'robots_blocked' | 'fetch_failed'), so an interrupted
    # nationwide run resumes where it left off instead of re-crawling everything.
    # --rescan re-attempts all matching candidates (incl. prior failures).
    if not args.rescan:
        where.append("scan_status IS NULL")
    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM candidates WHERE {' AND '.join(where)} ORDER BY state, district, name",
        params).fetchall()]
    # How many already-done candidates we're skipping (for an honest summary).
    skip_where = ["website_url IS NOT NULL", "scan_status IS NOT NULL"]
    skip_params: list = []
    if args.state:
        skip_where.append("state = ?"); skip_params.append(args.state.upper())
    already = conn.execute(
        f"SELECT COUNT(*) FROM candidates WHERE {' AND '.join(skip_where)}",
        skip_params).fetchone()[0]
    cost_summary = _estimate_scan_cost(conn, len(rows), cfg)
    conn.close()

    if not rows:
        if already and not args.rescan:
            print(f"Nothing to scan — all {already} candidate(s) with a URL are "
                  f"already attempted. Use --rescan to re-scan them.")
            return 0
        print("No scannable candidates (need a resolved website_url"
              + (" and url_verified" if cfg.require_verified_url else "") + ").")
        return 0
    if already and not args.rescan:
        print(f"(skipping {already} already-attempted candidate(s); --rescan to redo them)")

    scope = f"state={args.state.upper()}" if args.state else "ALL states"
    print(f"{len(rows)} candidate(s) to scan [{scope}], {args.workers} concurrent.")
    print(cost_summary)
    if not args.authorize:
        print("Dry run — re-run with --authorize to crawl these live domains "
              "(add --push-wayback to archive positives).")
        for c in rows[:20]:
            print(f"  would scan {c['name']} ({c['candidate_id']}) {c.get('website_url')}")
        if len(rows) > 20:
            print(f"  … and {len(rows) - 20} more")
        return 0

    # Per-model token-rate limiter, shared across all workers (org tokens/min
    # ceilings — first-pass and escalation metered independently).
    limiter = _build_token_limiter(cfg)
    if limiter is not None and limiter.limits:
        print("Token-rate limits (per model, shared across workers): "
              + ", ".join(f"{m} {int(v):,}/min" for m, v in sorted(limiter.limits.items())))

    # One shared chunk-classification pool caps the GLOBAL number of concurrent
    # LLM calls from multi-chunk pages at classify_pool_size, instead of letting
    # it grow as workers x concurrency (which it did when each page made its own).
    pool_size = max(1, cfg.models.get("classify_pool_size", cfg.models.get("concurrency", 8)))
    print(f"Classify pool: up to {pool_size} concurrent chunk calls "
          f"(shared across {args.workers} workers).")

    done = fails = pos = amb = 0
    interrupted = False
    with ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix="chunk") as chunk_pool, \
         ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(_scan_one_candidate, c, cfg,
                            push_wayback=args.push_wayback, rate_limiter=limiter,
                            chunk_executor=chunk_pool): c
                for c in rows}
        try:
            for fut in as_completed(futs):
                c = futs[fut]
                try:
                    o = fut.result()
                except Exception as e:
                    fails += 1
                    print(f"  ✗ {c['candidate_id']} {c['name'][:30]}: {e}")
                    continue
                done += 1
                pos += o.positives; amb += o.ambiguous
                flag = " ⚑ POSITIVE" if o.positives else (" ? ambiguous" if o.ambiguous else "")
                dd = f", {o.deduped} deduped" if o.deduped else ""
                print(f"  ✓ {c['candidate_id']} {c['name'][:30]:30s} "
                      f"{o.pages_scanned}p {o.positives}+/{o.ambiguous}? "
                      f"({o.prefiltered} prefiltered{dd}){flag}")
        except KeyboardInterrupt:
            # Without this, the executors' context exit is shutdown(wait=True)
            # WITHOUT cancel_futures — every queued candidate would still run
            # and Ctrl-C would not actually stop the run. Cancel the queue, let
            # the in-flight scans finish cleanly (each commits per page and sets
            # scan_status, so a resumed run skips them). The chunk pool is NOT
            # cancelled: in-flight candidates still need it to classify.
            interrupted = True
            n_cancelled = sum(1 for f in futs if f.cancel())
            print(f"\nInterrupted — cancelled {n_cancelled} queued candidate(s); "
                  f"letting up to {args.workers} in-flight scan(s) finish "
                  f"(may take a few minutes; Ctrl-C again to abandon them mid-scan)...")
    print(f"\nDone: {done} scanned, {fails} failed | "
          f"{pos} positive, {amb} ambiguous detections (all held for review).")
    if interrupted:
        print("Run interrupted. Re-run the same scan-all command to resume — "
              "already-attempted candidates are skipped automatically.")
        return 130
    return 0


def cmd_scan(args, cfg) -> int:
    """Walk one candidate end-to-end: crawl -> classify -> archive -> persist.

    Requires a resolved website_url and explicit --authorize for live crawling.
    URL verification is a review-time signal, not a pre-scan blocker by default
    (set require_verified_url in config.yaml to restore the spec §3.1 gate).
    Wayback pushes require --push-wayback.
    """
    from .archiver import Archiver
    from .classifier import Classifier, build_llm
    from .crawler import Crawler, PlaywrightFetcher
    from .pipeline import scan_candidate
    from .ratelimit import DomainRateLimiter
    from .robots import RobotsPolicy

    conn = init_db(cfg.database_path)
    row = conn.execute(
        "SELECT * FROM candidates WHERE candidate_id=?", (args.candidate,)
    ).fetchone()
    if not row:
        print(f"No candidate {args.candidate} in DB. Run `discover` first.")
        return 1
    candidate = dict(row)

    if not candidate.get("website_url"):
        print(f"No website_url resolved for {candidate['candidate_id']}. Resolve "
              f"one (re-run discover, or add it to data/websites.json) first.")
        return 2
    if cfg.require_verified_url and not candidate.get("url_verified"):
        print(f"Refusing to scan {candidate['candidate_id']}: require_verified_url "
              f"is set and website_url is unverified "
              f"({candidate.get('website_url')!r}). Add a verified override in "
              f"data/websites.json and re-run discover.")
        return 2
    if not candidate.get("url_verified"):
        print(f"Note: {candidate.get('website_url')!r} is auto-resolved "
              f"(unverified). Confirm it is really this candidate's site before "
              f"approving any finding for publication.")
    if not args.authorize:
        print(f"Would scan {candidate['name']} at {candidate['website_url']}.\n"
              f"Live crawling of a real domain is gated — re-run with --authorize "
              f"to proceed (add --push-wayback to also archive to the Wayback Machine).")
        return 0

    robots_cfg = cfg.get("robots_policy", {}) or {}
    rl = cfg.get("rate_limit", {}) or {}
    fetcher_cm = PlaywrightFetcher(user_agent=cfg.user_agent)
    models = cfg.models
    with fetcher_cm as fetcher:
        crawler = Crawler(
            fetcher,
            robots=RobotsPolicy(
                default=robots_cfg.get("default", "respect"),
                per_domain=robots_cfg.get("per_domain", {}),
                user_agent=cfg.user_agent,
            ),
            rate_limiter=DomainRateLimiter(rl.get("default_min_delay_seconds", 2.0)),
            common_paths=cfg.common_paths,
            crawl_depth=cfg.crawl_depth,
            user_agent=cfg.user_agent,
            max_pages=cfg.get("max_pages_per_site", 150),
        )
        classifier = Classifier(
            build_llm(
                first_pass=models.get("first_pass", "claude-haiku-4-5"),
                escalation=models.get("escalation", "claude-sonnet-4-6"),
                anthropic_api_key=cfg.anthropic_api_key,
                openai_api_key=cfg.openai_api_key,
                fireworks_api_key=cfg.fireworks_api_key,
                max_tokens=models.get("max_tokens", 1024)),
            first_pass_model=models.get("first_pass", "claude-haiku-4-5"),
            escalation_model=models.get("escalation", "claude-sonnet-4-6"),
            chunk_chars=models.get("chunk_chars", 40000),
            escalate_below=(cfg.get("confidence_thresholds", {}) or {}).get("escalate_below", 0.75),
            concurrency=models.get("concurrency", 8),
        )
        archiver = _build_archiver(cfg, args.push_wayback)
        outcome = scan_candidate(conn, candidate, crawler=crawler,
                                 classifier=classifier, archiver=archiver,
                                 require_verified=cfg.require_verified_url)
    conn.close()
    print(f"Scanned {outcome.candidate_id}: {outcome.pages_scanned} pages, "
          f"{outcome.detections} classified "
          f"({outcome.positives} positive, {outcome.ambiguous} ambiguous), "
          f"{outcome.prefiltered} skipped by pre-filter, "
          f"{outcome.deduped} template-alias duplicates deduped, "
          f"{outcome.archived} archived.")
    if outcome.positives or outcome.ambiguous:
        print("  Positives/ambiguous are archived and await human review before "
              "any publication (spec §3.7).")
    return 0


def cmd_calendar(args, cfg) -> int:
    """Load the per-state primary calendar and backfill candidate primary dates (phase 7)."""
    from .scheduler import backfill_primary_dates, load_calendar

    conn = init_db(cfg.database_path)
    n = load_calendar(conn, cycle=cfg.election_year)
    m = backfill_primary_dates(conn, cycle=cfg.election_year)
    conn.close()
    print(f"Loaded {n} state primary dates into `elections`; "
          f"backfilled primary_date onto {m} candidates.")
    return 0


def cmd_schedule(args, cfg) -> int:
    """List candidates due for a scan today under the cadence rules (phase 7)."""
    from datetime import date

    from .scheduler import due_candidates

    conn = init_db(cfg.database_path)
    cadence = cfg.get("scan_cadence", {}) or {}
    today = date.fromisoformat(args.today) if args.today else None
    due = due_candidates(
        conn, today=today,
        daily_window_days=cadence.get("daily_window_days", 21),
        default_interval_days=cadence.get("default_interval_days", 7),
        require_verified=cfg.require_verified_url)
    conn.close()
    if not due:
        print("No candidates with a resolved URL are due for a scan today.")
        return 0
    print(f"{len(due)} candidate(s) due for scan"
          f"{' as of '+args.today if args.today else ''}:\n")
    for d in due:
        c = d.candidate
        dtp = f"{d.days_to_primary}d to primary" if d.days_to_primary is not None else "no primary date"
        print(f"  [{d.cadence:6}] {c['name']} ({c['candidate_id']}) "
              f"{c.get('state')}-{c.get('district')} · {dtp} · {d.reason}")
    print("\nScan one:  python -m redbox scan --candidate <ID> --authorize")
    return 0


def cmd_corroborate(args, cfg) -> int:
    """Schedule E corroboration for candidates with a positive detection (phase 4)."""
    from .corroboration import candidates_with_positive, run
    from .fec import FECClient

    conn = init_db(cfg.database_path)
    if args.candidate:
        targets = [args.candidate]
    else:
        targets = candidates_with_positive(conn)
    if not targets:
        print("No candidates with a positive detection to corroborate.")
        conn.close()
        return 0

    fec = FECClient(api_key=cfg.fec_api_key, cache_dir=cfg.fec_cache_dir,
                    user_agent=cfg.user_agent)
    for cid in targets:
        corr = run(conn, fec, candidate_id=cid, cycle=cfg.election_year,
                   use_cache=not args.no_cache)
        name = conn.execute("SELECT name FROM candidates WHERE candidate_id=?", (cid,)).fetchone()
        print(f"\n{name['name'] if name else cid} ({cid})")
        print(f"  {corr.headline}")
        if corr.ie_filing_count:
            print(f"  supporting: ${corr.supporting_total:,.0f} | opposing: "
                  f"${corr.opposing_total:,.0f} | {corr.ie_filing_count} filings")
            if corr.guidance_first_detected:
                print(f"  guidance first detected (by us): {corr.guidance_first_detected[:10]} "
                      f"| supporting IE dated on/after: ${corr.supporting_after_detection:,.0f}")
            for s in corr.committees[:6]:
                print(f"    [{s.indicator}] {s.committee_name}: ${s.amount:,.0f} "
                      f"({s.count} filings, {s.first_date}..{s.last_date})")
    fec.close()
    conn.close()
    return 0


def cmd_review(args, cfg) -> int:
    """Human gate on positives (spec §3.7). List pending, or approve/reject one."""
    from .util import now_iso

    conn = init_db(cfg.database_path)
    if args.list or not args.detection:
        pend = conn.execute(
            """SELECT d.detection_id, d.candidate_id, c.name, d.classification,
                      d.confidence, s.url, s.text_hash
               FROM detections d JOIN candidates c USING(candidate_id)
               JOIN scans s USING(scan_id)
               LEFT JOIN reviews r ON r.detection_id=d.detection_id
               WHERE d.classification IN ('red_box_guidance','ambiguous')
                 AND (r.action IS NULL OR r.action='needs_more')
               GROUP BY d.detection_id ORDER BY d.confidence DESC""").fetchall()
        if not pend:
            print("No detections pending review.")
        else:
            # Collapse template aliases: the same page body under several URLs
            # (catch-all /media, /media-kit, /press routes) is ONE reviewable
            # finding — list it once with its alias count. Review it with
            # --group to act on all of its detections at once.
            groups: dict = {}
            for r in pend:
                groups.setdefault((r["candidate_id"], r["text_hash"]), []).append(r)
            print(f"{len(groups)} finding(s) pending review "
                  f"({len(pend)} detection(s) incl. template aliases):\n")
            for rows in groups.values():
                r = rows[0]
                alias = (f"   [+{len(rows)-1} identical alias page(s) — use --group]"
                         if len(rows) > 1 else "")
                print(f"  #{r['detection_id']}  {r['name']} ({r['candidate_id']})  "
                      f"{r['classification']} conf={r['confidence']:.2f}{alias}\n      {r['url']}")
            print("\nApprove/reject:  python -m redbox review --detection <ID> "
                  "--action approve|reject|needs_more [--group]")
        conn.close()
        return 0

    if args.action not in ("approve", "reject", "needs_more"):
        print("--action must be approve | reject | needs_more")
        return 2
    targets = [args.detection]
    if args.group:
        # Apply to every detection of the same candidate whose page body is
        # identical (template aliases) — one human judgment, one review record
        # per alias so none linger as "pending".
        siblings = conn.execute(
            """SELECT d2.detection_id FROM detections d
               JOIN scans s USING(scan_id)
               JOIN scans s2 ON s2.candidate_id = s.candidate_id
                            AND s2.text_hash = s.text_hash
               JOIN detections d2 ON d2.scan_id = s2.scan_id
               WHERE d.detection_id = ?
                 AND d2.classification IN ('red_box_guidance','ambiguous')""",
            (args.detection,)).fetchall()
        targets = sorted({r["detection_id"] for r in siblings} | {args.detection})
    ts = now_iso()
    conn.executemany(
        """INSERT INTO reviews (detection_id, reviewer, action, notes, reviewed_at)
           VALUES (?,?,?,?,?)""",
        [(d, args.reviewer, args.action, args.notes, ts) for d in targets])
    conn.commit()
    conn.close()
    verb = {"approve": "approved (will publish)", "reject": "rejected (recorded, not published)",
            "needs_more": "marked needs-more (re-scan/expand)"}[args.action]
    extra = f" (+{len(targets)-1} identical alias detection(s))" if len(targets) > 1 else ""
    print(f"Detection #{args.detection} {verb}{extra}.")
    return 0


def cmd_review_web(args, cfg) -> int:
    """Serve the local web review console (phase 5). Binds 127.0.0.1 only —
    pending detections are unpublished allegations and stay on this machine."""
    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import WSGIServer, make_server

    from .reviewweb import ReviewApp

    class _Server(ThreadingMixIn, WSGIServer):
        daemon_threads = True

    app = ReviewApp(cfg.database_path)
    with make_server("127.0.0.1", args.port, app, server_class=_Server) as httpd:
        print(f"Review console at http://127.0.0.1:{args.port}/  (Ctrl-C to stop)")
        print("Approvals only record reviews — publish with `python -m redbox publish`.")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")
    return 0


def cmd_publish(args, cfg) -> int:
    """Generate the static review/results site (spec §3.8)."""
    from .publisher import build_site

    conn = init_db(cfg.database_path)
    out = cfg.repo_root / "site"
    pub_cfg = cfg.get("publish", {}) or {}
    # Canonical URL (and thus sitemap/robots/canonical tags) only for the public
    # build — review-console pages must never carry public URLs.
    site_url = pub_cfg.get("site_url") if args.approved_only else None
    build_site(conn, out, approved_only=args.approved_only,
               page_size=pub_cfg.get("page_size", 500), site_url=site_url)
    conn.close()
    index = out / "index.html"
    mode = "public (approved findings + dated negatives)" if args.approved_only else "review console (includes pending)"
    print(f"Built {mode} site → {index}")
    print(f"Open it:  open {index}")
    if args.serve:
        import http.server, socketserver, functools, os
        os.chdir(out)
        handler = functools.partial(http.server.SimpleHTTPRequestHandler)
        with socketserver.TCPServer(("127.0.0.1", args.port), handler) as httpd:
            print(f"Serving at http://127.0.0.1:{args.port}/  (Ctrl-C to stop)")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nstopped.")
    return 0


def cmd_backfill_pdf_evidence(args, cfg) -> int:
    """Refetch the PDFs behind existing PDF-sourced detections and archive them.

    Historical PDF scans kept only extracted text — no raw document, no visual
    exhibit. This refetches each qualifying detection's PDF (live fetch, gated
    behind --authorize), verifies the extracted text still hashes to what the
    classifier saw, and writes an archive (raw .pdf + rendered page image)
    linked to the detection so the publisher can show it like a screenshot.
    """
    import time

    from .archiver import Archiver
    from .crawler import fetch_pdf

    conn = init_db(cfg.database_path)
    rows = conn.execute("""
        SELECT d.detection_id, d.candidate_id, d.scan_id, s.url, s.text_hash,
               c.name, COALESCE(r.action, 'pending') AS action
        FROM detections d
        JOIN scans s USING(scan_id)
        JOIN candidates c USING(candidate_id)
        LEFT JOIN (SELECT detection_id, action, ROW_NUMBER() OVER (
                       PARTITION BY detection_id
                       ORDER BY reviewed_at DESC, review_id DESC) rn
                   FROM reviews) r
               ON r.detection_id = d.detection_id AND r.rn = 1
        WHERE s.render_mode = 'pdf'
          AND d.classification != 'no_guidance_detected'
          AND COALESCE(r.action, 'pending') IN ('approve', 'pending')
          AND NOT EXISTS (SELECT 1 FROM archives a
                          WHERE a.detection_id = d.detection_id
                            AND a.pdf_path IS NOT NULL)
        ORDER BY d.candidate_id""").fetchall()
    if not rows:
        print("Nothing to backfill — every PDF-sourced detection has an archived PDF.")
        conn.close()
        return 0
    if not args.authorize:
        print(f"Would refetch {len(rows)} PDF(s) for archiving:")
        for r in rows:
            print(f"  {r['candidate_id']}  {r['name']}  [{r['action']}]  {r['url']}")
        print("Live fetching is gated — re-run with --authorize to proceed "
              "(add --push-wayback to also archive to the Wayback Machine).")
        conn.close()
        return 0

    archiver = _build_archiver(cfg, args.push_wayback)
    done = mismatched = failed = 0
    for r in rows:
        try:
            res = fetch_pdf(r["url"], user_agent=cfg.user_agent)
        except Exception as e:
            print(f"  FAIL {r['candidate_id']}  {r['url']}  ({e})")
            failed += 1
            continue
        if not res.pdf_bytes:
            print(f"  FAIL {r['candidate_id']}  {r['url']}  (HTTP {res.status})")
            failed += 1
            continue
        if res.text_hash != r["text_hash"] and not args.force:
            # The document no longer matches what the classifier saw — an
            # archive made now would misrepresent the evidence. Skip unless
            # the operator explicitly accepts the current version (--force).
            print(f"  SKIP {r['candidate_id']}  {r['url']}  "
                  f"(content changed since detection; use --force to archive anyway)")
            mismatched += 1
            continue
        rec = archiver.archive(res, candidate_id=r["candidate_id"])
        Archiver.persist(conn, rec, candidate_id=r["candidate_id"],
                         scan_id=r["scan_id"], detection_id=r["detection_id"])
        note = "" if res.text_hash == r["text_hash"] else "  (content changed — forced)"
        print(f"  OK   {r['candidate_id']}  {r['url']}"
              f"{'' if rec.screenshot_path else '  (render failed; raw PDF kept)'}{note}")
        done += 1
        time.sleep(2.0)  # polite pacing across campaign domains
    conn.close()
    print(f"Backfilled {done}, skipped {mismatched} changed, {failed} failed. "
          f"Re-run `publish --approved-only` to surface the new exhibits.")
    return 0


def _clean_stale_pages(site: "Path") -> list[str]:
    """Delete site *.html files absent from the freshly built sitemap.

    The sitemap lists extensionless clean URLs; map each back to its .html
    file. Only .html is ever deleted: evidence/, feeds, index-data.json are
    untouched, and 404.html is deliberately sitemap-absent but must survive
    (its presence is what disables the Pages soft-200 SPA fallback).
    """
    import re

    listed = set(re.findall(r"<loc>https://[^/<]+/([^<]*)</loc>",
                            (site / "sitemap.xml").read_text()))
    listed = {("index.html" if u == "" else u + ".html") for u in listed}
    stale = sorted(p.name for p in site.glob("*.html")
                   if p.name not in listed and p.name != "404.html")
    for name in stale:
        (site / name).unlink()
    return stale


def cmd_deploy(args, cfg) -> int:
    """One-shot public release: run the offline test suite, publish
    --approved-only, sweep stale pages, deploy to Cloudflare Pages (wrangler),
    and smoke-check the live site."""
    import subprocess
    import sys

    from .publisher import build_site

    pub_cfg = cfg.get("publish", {}) or {}
    site_url = pub_cfg.get("site_url")
    project = pub_cfg.get("pages_project")
    if not (site_url and project):
        print("publish.site_url and publish.pages_project must be set in config.yaml")
        return 2

    if not args.skip_tests:
        # A deploy can happen between commits (approvals don't touch git), so
        # CI alone can't guarantee the publisher code being deployed is green.
        r = subprocess.run([sys.executable, "-m", "pytest", "tests/", "-q"],
                           cwd=cfg.repo_root)
        if r.returncode != 0:
            print("Test suite failed — nothing deployed. "
                  "Fix the failures or re-run with --skip-tests.")
            return r.returncode

    conn = init_db(cfg.database_path)
    out = cfg.repo_root / "site"
    build_site(conn, out, approved_only=True,
               page_size=pub_cfg.get("page_size", 500), site_url=site_url)
    conn.close()
    stale = _clean_stale_pages(out)
    print(f"Built public site ({len(list(out.glob('*.html')))} pages); "
          f"removed {len(stale)} stale: {', '.join(stale) or 'none'}")
    if args.dry_run:
        print("Dry run — site built and swept, nothing deployed.")
        return 0

    r = subprocess.run(
        ["npx", "wrangler", "pages", "deploy", str(out), "--project-name", project,
         "--branch", "main", "--commit-dirty=true"], cwd=cfg.repo_root)
    if r.returncode != 0:
        print("wrangler deploy failed — site/ is built; re-run `redbox deploy` to retry.")
        return r.returncode

    # Smoke-check: the deployed index must answer 200 and carry the nameplate.
    import httpx

    try:
        resp = httpx.get(site_url, follow_redirects=True, timeout=30)
        ok = resp.status_code == 200 and "Red Box Watch" in resp.text
        print(f"Live check {site_url}: HTTP {resp.status_code} "
              f"{'OK' if ok else 'UNEXPECTED — verify manually'}")
        return 0 if ok else 1
    except Exception as e:
        print(f"Live check failed ({e}) — deploy reported success; verify manually.")
        return 1


def cmd_vacuum(args, cfg) -> int:
    """Reclaim disk space: null out the deprecated scans.raw_html and VACUUM.

    New scans no longer store raw_html, but legacy rows still hold it (~98% of an
    older DB). This strips that column and compacts the file. The full HTML of any
    archived page is preserved on disk by the archiver, so nothing of evidentiary
    value is lost. Make a backup first; VACUUM rewrites the whole file.
    """
    from pathlib import Path

    db_path = Path(cfg.database_path)
    before = db_path.stat().st_size if db_path.exists() else 0
    conn = init_db(db_path)
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(LENGTH(raw_html)),0) FROM scans WHERE raw_html IS NOT NULL"
    ).fetchone()
    n_rows, html_bytes = row[0], row[1]
    print(f"DB file: {before/1e6:.1f} MB")
    print(f"Legacy rows with raw_html: {n_rows:,}  (~{html_bytes/1e6:.1f} MB of inline HTML)")
    if args.dry_run:
        print("Dry run — nothing changed. Re-run without --dry-run to reclaim.")
        conn.close()
        return 0
    if n_rows:
        conn.execute("UPDATE scans SET raw_html = NULL WHERE raw_html IS NOT NULL")
        conn.commit()
    # VACUUM can't run inside a transaction and needs no WAL; isolation_level=None.
    conn.isolation_level = None
    conn.execute("VACUUM")
    conn.close()
    after = db_path.stat().st_size
    print(f"Done. DB file: {before/1e6:.1f} MB → {after/1e6:.1f} MB "
          f"({100 - 100*after/before:.0f}% smaller)" if before else "Done.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="redbox", description="Red-Boxing Tracker")
    parser.add_argument("--config", default=None, help="path to config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("initdb", help="create the database schema")

    d = sub.add_parser("discover", help="build the candidate universe (phase 1)")
    d.add_argument("--states", default=None, help="comma list, e.g. NC,TX (default: all)")
    d.add_argument("--offices", default="H,S,P", help="comma list of H,S,P")
    d.add_argument("--district", default=None, help="two-digit district, e.g. 12 (requires a single state)")
    d.add_argument("--no-cache", action="store_true", help="bypass FEC response cache")
    d.add_argument("--no-bulk", action="store_true",
                   help="use the openFEC API instead of the bulk weball file")
    d.add_argument("--mode", default=None, choices=["primary", "general", "full"],
                   help="universe to build: primary (contested primaries; default), "
                        "general (primary winners/nominees), or full (per-race phase). "
                        "Overrides config scan_mode.")
    d.add_argument("--no-feed", action="store_true",
                   help="general/full: skip the results feed (civicAPI); use only "
                        "uncontested-auto + the manual nominees override file")

    mi = sub.add_parser("mark-inactive",
                        help="flag FEC-inactive (withdrawn/superseded) candidacies")
    mi.add_argument("--no-cache", action="store_true", help="bypass FEC response cache")

    ml = sub.add_parser("mark-primary-losers",
                        help="flag primary losers in states whose primary has passed")
    ml.add_argument("--today", default=None, help="override today's date (YYYY-MM-DD)")
    ml.add_argument("--no-feed", action="store_true",
                    help="skip the results feed; use only manual overrides + "
                         "uncontested-auto (which alone marks no one)")

    rs = sub.add_parser("resolve", help="backfill campaign URLs onto existing candidates (phase 1)")
    rs.add_argument("--state", default=None, help="limit to one state, e.g. NC")
    rs.add_argument("--force", action="store_true", help="re-resolve even candidates that already have a URL (bypasses the cache)")
    rs.add_argument("--no-search", action="store_true", help="disable the billable web-search backup")
    rs.add_argument("--workers", type=int, default=8, help="candidates resolved concurrently")
    rs.add_argument("--no-cache", action="store_true", help="bypass the resolution cache (no read, no write)")

    s = sub.add_parser("scan", help="walk one candidate end-to-end (phases 2-3)")
    s.add_argument("--candidate", required=True, help="FEC candidate_id from the universe")
    s.add_argument("--authorize", action="store_true",
                   help="confirm live crawling of this real domain")
    s.add_argument("--push-wayback", action="store_true",
                   help="also push positives to the Internet Archive")

    sa = sub.add_parser("scan-all", help="scan many candidates concurrently (phase 1 scale)")
    sa.add_argument("--state", default=None, help="limit to one state, e.g. NY")
    sa.add_argument("--workers", type=int, default=4, help="candidates scanned concurrently")
    sa.add_argument("--authorize", action="store_true", help="confirm live crawling")
    sa.add_argument("--push-wayback", action="store_true",
                    help="also push positives to the Internet Archive")
    sa.add_argument("--rescan", action="store_true",
                    help="re-scan candidates already attempted (default: skip them)")

    sub.add_parser("calendar", help="load per-state primary calendar (phase 7)")

    sch = sub.add_parser("schedule", help="list candidates due for a scan (phase 7)")
    sch.add_argument("--today", default=None, help="override today's date (YYYY-MM-DD)")

    cor = sub.add_parser("corroborate", help="Schedule E corroboration (phase 4)")
    cor.add_argument("--candidate", default=None, help="candidate_id (default: all with a positive)")
    cor.add_argument("--no-cache", action="store_true", help="bypass FEC response cache")

    r = sub.add_parser("review", help="human gate on positives (phase 5)")
    r.add_argument("--list", action="store_true", help="list detections pending review")
    r.add_argument("--detection", type=int, default=None, help="detection_id to act on")
    r.add_argument("--action", default=None, help="approve | reject | needs_more")
    r.add_argument("--group", action="store_true",
                   help="apply the action to all of this candidate's detections "
                        "with an identical page body (template aliases)")
    r.add_argument("--reviewer", default=None, help="reviewer name/id")
    r.add_argument("--notes", default=None, help="review notes")

    rw = sub.add_parser("review-web", help="serve the local web review console (phase 5)")
    rw.add_argument("--port", type=int, default=8001,
                    help="port for the console (always binds 127.0.0.1)")

    vac = sub.add_parser("vacuum", help="reclaim DB space: strip deprecated raw_html + compact")
    vac.add_argument("--dry-run", action="store_true", help="report what would be reclaimed, change nothing")

    bf = sub.add_parser("backfill-pdf-evidence",
                        help="refetch + archive PDFs behind existing PDF-sourced detections")
    bf.add_argument("--authorize", action="store_true",
                    help="actually fetch (without this, just lists what would be fetched)")
    bf.add_argument("--push-wayback", action="store_true",
                    help="also push each PDF URL to the Wayback Machine")
    bf.add_argument("--force", action="store_true",
                    help="archive even if the PDF's text no longer matches the detection")

    dp = sub.add_parser("deploy",
                        help="test + publish --approved-only + stale-page sweep + Cloudflare Pages deploy")
    dp.add_argument("--dry-run", action="store_true",
                    help="build and sweep only; skip the wrangler deploy")
    dp.add_argument("--skip-tests", action="store_true",
                    help="deploy without running the offline test suite first")

    p = sub.add_parser("publish", help="build the static review/results site (phase 6)")
    p.add_argument("--approved-only", action="store_true",
                   help="strict public build: approved findings + dated negatives only")
    p.add_argument("--serve", action="store_true", help="serve the site locally after building")
    p.add_argument("--port", type=int, default=8000, help="port for --serve")

    args = parser.parse_args(argv)
    cfg = load_config(args.config)

    if args.command == "initdb":
        return cmd_initdb(args, cfg)
    if args.command == "discover":
        return cmd_discover(args, cfg)
    if args.command == "mark-inactive":
        return cmd_mark_inactive(args, cfg)
    if args.command == "mark-primary-losers":
        return cmd_mark_primary_losers(args, cfg)
    if args.command == "resolve":
        return cmd_resolve(args, cfg)
    if args.command == "scan":
        return cmd_scan(args, cfg)
    if args.command == "scan-all":
        return cmd_scan_all(args, cfg)
    if args.command == "calendar":
        return cmd_calendar(args, cfg)
    if args.command == "schedule":
        return cmd_schedule(args, cfg)
    if args.command == "corroborate":
        return cmd_corroborate(args, cfg)
    if args.command == "review":
        return cmd_review(args, cfg)
    if args.command == "review-web":
        return cmd_review_web(args, cfg)
    if args.command == "vacuum":
        return cmd_vacuum(args, cfg)
    if args.command == "backfill-pdf-evidence":
        return cmd_backfill_pdf_evidence(args, cfg)
    if args.command == "publish":
        return cmd_publish(args, cfg)
    if args.command == "deploy":
        return cmd_deploy(args, cfg)
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
