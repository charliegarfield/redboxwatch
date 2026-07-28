# bsky-poster

Cloudflare Worker that posts each **newly published** Red Box Watch finding to
[@redboxwatch.bsky.social](https://bsky.app/profile/redboxwatch.bsky.social).
It reads the public `feed.json` (generated only from the approved-only build,
so nothing a human didn't approve can reach social media), dedupes via KV, and
posts over the AT Protocol.

## Behavior

- **Cron**: hourly (`7 * * * *`, see `wrangler.jsonc`).
- **First run ever** (no KV key): records the current feed **without posting**
  — a fresh deploy never floods the account with backfill.
- **Flood absorb**: if more than `FLOOD_LIMIT` (default 15) items look new at
  once, that's a feed guid-scheme migration, not news — the run records them
  without posting.
- Posts **oldest-first**, capped at `MAX_POSTS_PER_RUN` (5) per run; KV state
  is written after **each** post, so a mid-run failure can't double-post. A
  single rejected post (e.g. an over-long title, though text is clipped) is
  logged and skipped without aborting the batch.

## Deploy

```bash
cd workers/bsky-poster
npx wrangler deploy
npx wrangler secret put BSKY_APP_PASSWORD   # an app password, not the account password
npx wrangler secret put RUN_TOKEN           # any random string; guards the manual trigger
```

The KV namespace binding `STATE` is declared in `wrangler.jsonc` (create your
own with `npx wrangler kv namespace create STATE` and swap the id if you fork
this). Plain vars (`FEED_URL`, `BSKY_HANDLE`, `MAX_POSTS_PER_RUN`,
`FLOOD_LIMIT`) also live in `wrangler.jsonc`.

## Manual trigger

```bash
curl -H "Authorization: Bearer $RUN_TOKEN" https://<worker-url>/
```

Returns the same JSON summary a cron tick produces
(`{posted, failed, pending}` / `{bootstrapped}` / `{absorbed}`).
