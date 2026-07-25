# Red-Boxing Tracker — Build Spec

A system that detects "red-boxing" — public-facing messaging guidance that campaigns post to coordinate (lawfully, by the letter of the rule) with outside spenders such as super PACs. It builds a candidate universe, scans candidate websites for the *functional content pattern* of red-box guidance, archives evidence, corroborates against independent-expenditure filings, gates positive findings behind human review, and publishes per-candidate status.

This document is the authoritative spec. Build it in the phases listed at the end. Prefer small, testable modules with clear interfaces over a monolith.

---

## 1. Background: what we are detecting

Federal rules bar campaigns from *privately* coordinating ad strategy with super PACs, but campaigns may post information *publicly*, and a PAC may read it. "Red-boxing" is the practice of posting prescriptive messaging/media-buy guidance on a public page — historically in a red-bordered box, now often just plain prose on a `/media` or `/media-kit` page — so an aligned outside group can execute it without a private conversation.

**The signal is functional, not visual.** Do not key on CSS, borders, or the word "media." Key on the *content pattern*:

- **Segmented audiences with directives**: "Younger primary voters should see…", "Women likely to vote in the primary should read…", "Voters in [district/market] need to hear…"
- **Channel / timing / geography cues** that only make sense as media-buy instructions: "on the go" (digital), "in their mailboxes" (direct mail), "during high-visibility events and on the news" (TV), specific media markets, specific councilmanic districts.
- **Prescribed themes / contrast framing**: what message to run, which opponent contrast to draw, which endorsements to feature "in clear and bold language and imagery."
- Sometimes: suggested b-roll, photo/video availability framed for ad use, polling crosstabs, "updated [date], more to follow" cadence implying ongoing instruction.

**Not red-boxing** (avoid false positives): standard press kits (logos, headshots, bios, brand assets, press-release archives, press-contact info), donation/volunteer CTAs, and ordinary issue/platform pages written to persuade voters directly.

The detection target right now is largely unconcealed — the guidance is plain text on indexed pages. The system should still be robust to PDFs, unlinked subpages, and JS-rendered content, and to an eventual arms race toward subtler phrasing.

---

## 2. Architecture overview

```
discovery ──► crawler ──► classifier ──► review queue ──► publisher
    │             │             │              ▲
    │             └─► archiver ─┘              │
    └─► scheduler (per-state primary calendar) │
              corroboration (Schedule E) ──────┘
```

Pipeline is candidate-centric and scheduled, **not** triggered off filings. (A filed independent expenditure is *late* evidence — the red box is posted *before* the money to solicit it, and a Schedule E can oppose a candidate to benefit their opponent, so the filer's named candidate is not necessarily the one whose site has the box.) Schedule E data is used for **corroboration**, not as the trigger.

---

## 3. Modules

### 3.1 `discovery` — build the candidate universe

Two populations, unioned and deduped to a `candidates` table:

**A. Contested primaries (primary target — derived from FEC).** This is where the blatant boxes are now: super PACs picking nominees in safe seats that the general-election rating shops mark "Solid" and therefore ignore.
- Pull `GET /v1/candidates/` from the openFEC API (`https://api.open.fec.gov/v1/`, key from api.data.gov) filtered by `election_year`, `office` (H/S/P), and `candidate_status`/active flags.
- Group candidates by `(office, state, district, party)`.
- For each candidate, pull financial totals (`/v1/candidate/{candidate_id}/totals/`); keep only candidates above a configurable receipts floor (default **$50,000**) to drop paper candidates.
- Any group with **≥2** funded candidates is a contested primary. Add all funded candidates in that group to the universe.

**B. Competitive general elections (secondary overlay).**
- Ingest race ratings and keep anything rated **Tilt / Lean / Toss-up** (drop Likely/Solid).
- Free/structured source: Sabato's Crystal Ball (republished in machine-readable form via 270toWin's House/Senate/Gov tables). Cook is the gold standard but paywalled (they license API access). Make the rating source a pluggable adapter.
- Map each competitive district to its filed candidates via FEC.

**Cross-check (optional, human-curated backstop):** Ballotpedia tracks "battleground primaries" with structured per-race candidate pages and filing/primary dates — useful for catching races before the money shows up. Treat as a supplementary adapter, not the source of truth.

**Website resolution.** For each candidate, resolve the official campaign site URL. FEC committee records are unreliable for this, so: try committee metadata, then Ballotpedia, then a search step; store the resolved URL with a `url_source` and a `url_verified` boolean. Unverified URLs go to the review queue before first scan — never scan/publish against a guessed domain.

`candidates` fields (minimum): `candidate_id` (FEC), `name`, `office`, `state`, `district`, `party`, `cycle`, `universe_reason` (`contested_primary` | `competitive_general` | `both`), `primary_date`, `website_url`, `url_verified`, `receipts`.

### 3.2 `scheduler` — when to scan

- Primaries are spread roughly March–September; there is no single national date. Maintain a **per-state primary calendar** (`elections` table: `state`, `primary_date`, `filing_deadline`).
- Scan cadence per candidate: begin at the filing deadline, scan **daily** in the final ~3 weeks before that candidate's primary (red boxes go up and get pulled fast), weekly otherwise.
- The red box is often **removed the moment it draws attention**, so detection-time archiving (3.4) is mandatory and re-scans should diff against prior snapshots to capture put-up/take-down events (itself a publishable signal).

### 3.3 `crawler` — fetch and extract candidate-page content

- For each candidate site, enumerate candidate pages: parse `sitemap.xml`; probe common paths (`/media`, `/media-kit`, `/press`, `/messaging`, `/newsroom`, `/resources`, trailing-slash variants); shallow link-crawl (depth ≤2) collecting same-domain pages and **linked PDFs**.
- Render with a **headless browser (Playwright)** — many sites are JS-rendered, and content can sit in hidden/unlinked divs. Extract visible text *and* DOM text. Extract text from discovered PDFs.
- **Be a compliant crawler:** identify with a real, honest User-Agent; rate-limit per domain; respect `robots.txt` by default with a documented, configurable policy decision (note: at least one known target site disallows automated fetchers via robots, so per-site handling and a real browser context are required — decide and document your robots posture). Keep these are public pages, but crawl politely.
- Emit a `scan` record per page: `candidate_id`, `url`, `fetched_at`, `content_type`, `raw_html`/`raw_text`, `text_hash` (to skip re-classification when unchanged).

### 3.4 `archiver` — preserve evidence at detection time

Triggered for any page classified positive or ambiguous (3.5), and on first detection of change:
- Capture full-page **screenshot** (PNG) + **raw HTML** + extracted text, stored with content hash and timestamp.
- Push the live URL to the Internet Archive **Save Page Now** endpoint and store the returned snapshot URL.
- Every published claim must link to archived evidence that survives a take-down.

### 3.5 `classifier` — detect the functional pattern

LLM classification over extracted page text. Use a cheap model for the first pass and **escalate ambiguous results to a stronger model**; check current model IDs at docs.claude.com (at time of writing, e.g. `claude-haiku-4-5` for first pass, `claude-sonnet-4-6` for escalation). Call via the Anthropic Messages API. Temperature 0. Require strict JSON output.

**System/classifier prompt (contract):**

The prompt instructs the model to classify by *function*, not styling: red-box
guidance pairs segmented audiences with media-buy directives (which voters
should see/read what, on which channels, in which geographies, with what message
themes or ad assets), addressed to an outside spender. Press kits, donation and
volunteer appeals, direct-to-voter issue pages, and internal press-shop notes
are explicitly negative. The model must return strict JSON:

```
{
  "classification": "red_box_guidance" | "ambiguous" | "no_guidance_detected",
  "confidence": <float 0-1>,
  "evidence": [ { "quote": "<verbatim span from the page>", "why": "<which signal>" } ],
  "rationale": "<2-3 sentence explanation>"
}
```

The exact operational prompt wording is deliberately **not published** in this
repo — a public prompt is a test target for campaigns writing red boxes to
evade detection. The production prompt lives in the untracked
`data/prompts/classifier.txt`; `redbox/classifier.py` falls back to a
functional generic starter prompt when that file is absent. Tune any prompt
change against the labeled fixtures (`tests/test_classifier_fixtures.py`,
acceptance #3).

- Input: extracted page text (chunk long pages; classify each chunk, take the max-severity result and union evidence).
- Output written to a `detections` table: `scan_id`, `candidate_id`, `classification`, `confidence`, `evidence` (JSON), `model`, `classified_at`.
- Routing: `red_box_guidance` and `ambiguous` → archive + review queue. `no_guidance_detected` → record only.

### 3.6 `corroboration` — Schedule E correlation

For candidates with a positive detection, pull independent expenditures from `GET /v1/schedules/schedule_e/` filtered by `candidate_id` and cycle. Key fields: `support_oppose_indicator` (S/O), `expenditure_amount`, `expenditure_date`, `payee_name`, committee. Compute, per candidate: total **supporting** IE dollars that posted **after** the guidance was first detected, and the spender list. The headline, most-newsworthy output is the sequence: *guidance posted on [date] → $X in aligned independent expenditures appeared from [committees]*. Store as `corroboration` records linked to the detection.

### 3.7 `review` — human gate on positives

**The human-in-the-loop sits on the POSITIVE path, not the negatives.** A "this candidate uses red boxes" label is a public accusation of a borderline practice and is the claim that carries reputational/defamation risk; "no guidance found" is low-stakes. So:
- `red_box_guidance` and `ambiguous` detections enter a review queue and are **not published until a human approves**, viewing the archived screenshot/HTML and the quoted evidence.
- Reviewer actions: `approve` (→ publish), `reject` (→ record, no publish), `needs_more` (→ re-scan/expand crawl).
- `no_guidance_detected` may auto-record but is published only as a dated negative (see labels).

### 3.7a Labels (legal-safety — use this exact language discipline)

- Positive, approved: **"Posted public messaging guidance consistent with red-boxing"** with a link to the archived snapshot and quoted evidence. Avoid bald badges and avoid asserting illegality — red-boxing exploits the rules openly; it is not per se unlawful.
- Negative: **"No public messaging guidance detected as of [date]"** — never "does not red-box." Absence of a finding is not proof (unparsed PDF, uncrawled page, box already removed). Always date-stamp.
- Maintain a public corrections/appeals process and a methodology page. Keep all published text neutral, factual, and evidence-linked.

### 3.8 `publisher` — output

- Generate a static site (per-candidate pages + a sortable index) from **approved** records only. Each positive entry shows: the quoted evidence, the archived snapshot link, detection date, take-down date if applicable, and the Schedule E corroboration.
- Provide a JSON/CSV export of the dataset.
- Index filters: state, office, party, status, primary date.

---

## 4. Data model (tables)

`candidates`, `elections`, `scans`, `detections`, `archives`, `ie_filings`, `corroboration`, `reviews`, `publications`. SQLite for dev; design so it can move to Postgres. Keep raw artifacts (screenshots/HTML) on disk or object storage, referenced by path/URL in `archives`.

---

## 5. Tech stack

- **Language:** Python 3.11+.
- **HTTP:** httpx. **Rendering:** Playwright (Chromium). **PDF text:** pdfminstack of choice.
- **Data sources:** openFEC API (api.data.gov key; mind rate limits — request a key, cache responses), rating-source adapter (Sabato via 270toWin tables to start; Cook adapter behind a flag), optional Ballotpedia adapter.
- **Classifier:** Anthropic Messages API; model IDs configurable (verify current IDs at docs.claude.com).
- **Archive:** Internet Archive Save Page Now.
- **Storage:** SQLite/Postgres + local/object storage for artifacts.
- **Publishing:** any static-site generator.

---

## 6. Config (single config file / env)

`election_year`, `receipts_floor` (default 50000), `rating_threshold` (Tilt/Lean/Toss-up), `crawl_depth` (2), `common_paths` list, `scan_cadence` rules, `robots_policy`, `models` (first-pass, escalation), `confidence_thresholds`, API keys (FEC, Anthropic), per-domain rate limits, User-Agent string.

---

## 7. Acceptance criteria

1. `discovery` produces a deduped candidate universe with contested-primary detection from FEC plus a competitive-general overlay, each candidate tagged with `universe_reason` and a resolved, verified website URL.
2. `crawler` renders JS pages, finds `/media`-style subpages and linked PDFs, respects the configured robots policy, and rate-limits per domain.
3. `classifier` returns the strict JSON schema, flags the functional pattern, and does **not** flag plain press kits / donation CTAs / issue pages. (Build a labeled fixture set, including the known plain-prose example, and require it to pass.)
4. Every positive/ambiguous detection has a stored screenshot + HTML + Wayback snapshot before it can be published.
5. Positives and ambiguous results are **blocked from publishing without human approval**; negatives publish only as dated "no guidance detected as of [date]."
6. Positives display Schedule E corroboration (supporting IE dollars/committees appearing after detection) where available.
7. Re-scans detect put-up/take-down changes via content hashing and preserve prior snapshots.
8. Published language matches §3.7a; a methodology page and corrections process exist.

---

## 8. Build phases

1. **Discovery + schema.** FEC candidate pull, contested-primary grouping, receipts filter, website resolution, DB schema. Output: candidate universe CSV.
2. **Crawler + archiver.** Page enumeration, headless render, PDF extraction, content hashing, screenshot/HTML capture, Wayback push.
3. **Classifier.** Prompt + JSON parsing + chunking + escalation; labeled fixture test set (must include true plain-prose positives and press-kit negatives).
4. **Corroboration.** Schedule E pull + timing/amount correlation.
5. **Review gate.** Queue + minimal reviewer UI (can be a simple local web app or CLI showing screenshot + evidence).
6. **Publisher.** Static site + JSON/CSV export + methodology/corrections pages.
7. **Scheduler.** Per-state calendar + cadence + change-diffing.

Start with phase 1, get a clean candidate universe for one or two states, then walk a handful of known candidates end-to-end before scaling.
