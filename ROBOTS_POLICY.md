# Crawler & robots.txt policy

The spec (§3.3) requires a documented, deliberate decision on robots posture.
This is that decision.

## Posture

**Default: respect `robots.txt`.** The crawler reads each origin's
`robots.txt`, honours `Disallow` for our User-Agent, and applies any
`Crawl-delay`. This is the default for every domain and the recommended
production setting.

**Per-domain override is possible but must be explicit and is logged.** The spec
notes that at least one known target site disallows automated fetchers via
robots, while the pages in question are *public, indexed* pages that the site
serves to any human browser. Because a positive finding here is a public
statement about a real person, we do **not** silently ignore robots. Instead:

- `config.yaml -> robots_policy.default: respect | override`
- `config.local.yaml -> robots_policy.per_domain: { "example.com": override }`
  (per-domain overrides are deployment-specific decisions and live in the
  untracked local config, not the public repo). Host matching is normalized:
  an override for `example.com` also covers `www.example.com`.
- Every fetch records its posture (`respect` | `override`) in the
  `scans.robots_posture` column, so the basis for collecting any given page
  is auditable after the fact.

## Why this shape

- These are public campaign pages, not private data; reading them is closer to
  what a journalist or any visitor does than to abusive scraping.
- But "we can" is not "we should by default." Defaulting to *respect* keeps us
  polite and defensible; making override a conscious, logged, per-domain choice
  keeps a human accountable for each exception.

## A common pattern: allowlist robots.txt

Some campaign sites use an **allowlist** `robots.txt` — explicitly permitting
Googlebot/Bingbot/archive.org and disallowing everyone else (including AI
crawlers like `ClaudeBot`/`GPTBot`) by a catch-all `Disallow`. Our crawler is
not on the allow list, so by default it scans **zero pages** on such a site. The
tool surfaces these honestly with a **"Site blocks automated access"** status
(rather than a misleading clean negative). Because the pages are public, you can
add the host to `robots_policy.per_domain` (in the untracked
`config.local.yaml`) as a reviewed, logged override.

## Politeness (always on, regardless of robots posture)

- **Honest User-Agent** identifying the project and a contact address
  (`config.yaml -> user_agent`). No spoofing of browser UAs to evade blocks.
- **Per-domain rate limiting** with a minimum inter-request delay
  (`config.yaml -> rate_limit`).
- **A real browser context** (Playwright/Chromium) is used for rendering, not to
  disguise the crawler — JS-rendered and hidden/unlinked content is part of the
  detection target (§3.3).
- **A `robots.txt` answering 5xx reads as temporarily-disallow** (the host is
  under stress), not as open season; a 404 means no policy and allows.
- Re-fetched pages whose stored `text_hash` is unchanged are not re-classified
  or re-archived. (The page itself is still fetched — the crawler does not
  currently send conditional requests such as `If-None-Match`.)

## What we never do

- No login/credential bypass, no paywalled content, no non-public endpoints.
- No high request rates; no ignoring 429s (we back off).
