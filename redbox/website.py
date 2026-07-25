"""Campaign website resolution (spec §3.1 "Website resolution").

FEC committee records are unreliable for the official campaign URL, so we try a
chain and record *where* the URL came from plus whether it is verified. The
verified flag is a **signal surfaced at the human review/publish gate** (so
attribution is confirmed before anything is published), not a pre-scan blocker by
default. Set ``require_verified_url`` to restore the spec §3.1 behavior of
refusing to scan an unverified URL.

Resolution chain (first hit wins):
  1. manual override  -> verified     (data/websites.json, human-curated, untracked)
  2. Wikipedia        -> unverified   (open MediaWiki API; campaign site from infobox)
  3. committee metadata (FEC)         -> unverified  (free; FEC's `website` field is ~always empty)
  4. search backup    -> unverified   (Serper.dev Google search + a cheap LLM judge
                                        when SERPER_KEY is set; else Claude web_search)

Free sources (Wikipedia, committee) run first; the search backup only fires for
candidates they miss. Serper+judge measured ~84% recall on a held-out state (vs
~15% for Wikipedia alone) at ~50x lower cost than the web_search tool, with the
judge biased hard toward NONE so a coverage gap never becomes a misattribution.

Adapters NOT used:
- **Ballotpedia** (a spec-suggested source) blocks automated access: every
  request — honest UA, browser UA, and headless Chromium alike — returns an
  empty HTTP 202 bot-challenge. A working scrape would require evading their bot
  protection, which ``ROBOTS_POLICY.md`` says we won't do. A Ballotpedia
  *official API* adapter could be added if a licensed key is available.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from .util import sha256_text

OVERRIDES_PATH = (
    Path(__file__).resolve().parent.parent / "data" / "websites.json"
)

# Two-letter code -> full state name, for Wikipedia search disambiguation.
STATE_NAMES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi",
    "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina",
    "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah",
    "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
}


class WikipediaResolver:
    """Resolve a candidate's campaign website from Wikipedia's open API.

    Wikipedia's MediaWiki API is open and scraping-friendly (with an honest
    User-Agent), unlike Ballotpedia. We search for the candidate, guard against
    matching the wrong person, and pull the campaign site from the infobox
    ``website`` field — preferring a "Campaign website"-labeled link and skipping
    official ``.gov`` office sites. Returns None on any miss (conservative: a
    None beats a misattributed URL, since results are unverified and reviewed).

    Coverage is realistic: incumbents and notable candidates resolve well;
    lesser-known primary challengers often have no Wikipedia page (or no campaign
    site in the infobox) and won't resolve.
    """

    API = "https://en.wikipedia.org/w/api.php"
    OFFICE_TLDS = (".house.gov", ".senate.gov", ".gov")
    HONORIFICS = {"DR", "JR", "SR", "II", "III", "IV", "MR", "MRS", "MS", "ESQ",
                  "COL", "HON", "REV"}

    def __init__(self, user_agent: str = "RedBoxTracker/0.1", timeout: float = 15.0,
                 get_json: Callable[[dict], dict] | None = None):
        self.user_agent = user_agent
        self.timeout = timeout
        self._get_json = get_json or self._http_get_json

    def _http_get_json(self, params: dict) -> dict:
        r = httpx.get(self.API, params=params,
                      headers={"User-Agent": self.user_agent}, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    @classmethod
    def fec_to_name(cls, fec_name: str) -> tuple[str, str]:
        """'DOE, JANE ANN' -> ('Jane', 'Doe'); drops honorifics."""
        last, _, rest = fec_name.partition(",")
        toks = [t for t in rest.strip().split()
                if t.strip(".").upper() not in cls.HONORIFICS]
        first = toks[0].title() if toks else ""
        return first, last.strip().title()

    def _search_titles(self, first: str, last: str, state: str) -> list[str]:
        q = f"{first} {last} {STATE_NAMES.get(state, state)} politician"
        try:
            data = self._get_json({"action": "query", "list": "search",
                                   "srsearch": q, "srlimit": 3, "format": "json"})
        except (httpx.HTTPError, ValueError):
            return []
        return [h["title"] for h in data.get("query", {}).get("search", [])]

    def _page_wikitext(self, title: str) -> str:
        try:
            data = self._get_json({"action": "query", "prop": "revisions",
                                   "rvprop": "content", "rvslots": "main",
                                   "titles": title, "redirects": 1, "format": "json"})
        except (httpx.HTTPError, ValueError):
            return ""
        for _, p in data.get("query", {}).get("pages", {}).items():
            try:
                return p["revisions"][0]["slots"]["main"]["*"]
            except (KeyError, IndexError):
                continue
        return ""

    @classmethod
    def website_from_wikitext(cls, wikitext: str) -> str | None:
        """Extract the campaign site from an infobox ``| website =`` field."""
        m = re.search(r"\|\s*website\s*=\s*(.+)", wikitext, re.I)
        if not m:
            return None
        field = m.group(1)
        cands: list[tuple[str, str]] = []
        for um in re.finditer(r"\{\{\s*url\s*\|\s*([^}|]+?)\s*(?:\|\s*([^}]*))?\}\}", field, re.I):
            cands.append((um.group(1).strip(), (um.group(2) or "").strip()))
        for um in re.finditer(r"\[(https?://[^\s\]]+)\s*([^\]]*)\]", field):
            cands.append((um.group(1).strip(), um.group(2).strip()))
        if not cands:
            return None

        def is_office(u: str) -> bool:
            host = u.split("//")[-1].split("/")[0].lower()
            return any(host.endswith(t) for t in cls.OFFICE_TLDS)

        campaign = [c for c in cands if "campaign" in c[1].lower() and not is_office(c[0])]
        non_office = [c for c in cands if not is_office(c[0])]
        picks = campaign or non_office
        if not picks:
            # Only official .gov office sites in the infobox -> not a campaign
            # site; return None rather than a misattributed office page.
            return None
        url = picks[0][0]
        return url if url.startswith("http") else "https://" + url

    def resolve(self, fec_name: str, state: str) -> str | None:
        first, last = self.fec_to_name(fec_name)
        if not last:
            return None
        for title in self._search_titles(first, last, state):
            tl = title.lower()
            # Guard against the wrong person: require last name and (first name
            # or a very short first) to appear in the page title.
            if last.lower() in tl and (first.lower() in tl or len(first) < 3):
                site = self.website_from_wikitext(self._page_wikitext(title))
                if site:
                    return site
        return None


@dataclass
class ResolvedURL:
    url: str | None
    source: str          # manual | wikipedia | committee | search | none
    verified: bool


class WebsiteResolver:
    def __init__(self, fec_client: Any | None = None, overrides_path: Path | None = None,
                 wikipedia: WikipediaResolver | None = None, user_agent: str = "RedBoxTracker/0.1",
                 search: "SearchResolver | SerperResolver | None | bool" = None,
                 anthropic_api_key: str | None = None, enable_search: bool = False,
                 search_model: str = "claude-sonnet-4-6",
                 serper_key: str | None = None, judge_model: str = "claude-haiku-4-5",
                 cache_dir: str | Path | None = None, cache_ttl_seconds: float | None = None):
        self.fec = fec_client
        self.overrides_path = overrides_path or OVERRIDES_PATH
        self._overrides = self._load_overrides()
        # Optional disk cache for resolution results (network/LLM lookups are the
        # expensive part). Keyed by candidate identity + which backends are active,
        # so a rerun re-uses prior results instead of re-searching. ``None`` = off.
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.cache_ttl_seconds = cache_ttl_seconds
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        # Pass wikipedia=False to disable the Wikipedia lookup (e.g. fast/offline).
        self.wikipedia = (WikipediaResolver(user_agent=user_agent)
                          if wikipedia is None else (wikipedia or None))
        # Search backup (the last resort, after free Wikipedia/committee). Priority:
        #   1. an explicit instance passed in (tests); ``search=False`` disables.
        #   2. Serper.dev + LLM judge, if a SERPER_KEY is configured (better recall,
        #      ~50x cheaper than Claude web_search). Needs the Anthropic key too.
        #   3. Claude's web_search tool as the fallback when no SERPER_KEY.
        if search is not None:
            self.search = search or None
        elif enable_search and serper_key and anthropic_api_key:
            self.search = SerperResolver(serper_key, anthropic_api_key=anthropic_api_key,
                                         model=judge_model)
        elif enable_search and anthropic_api_key:
            self.search = SearchResolver(anthropic_api_key, model=search_model)
        else:
            self.search = None

    def _load_overrides(self) -> dict[str, dict[str, Any]]:
        if self.overrides_path.exists():
            return json.loads(self.overrides_path.read_text())
        return {}

    def resolve(self, candidate: dict[str, Any], *, use_cache: bool = True) -> ResolvedURL:
        cid = candidate.get("candidate_id", "")

        # 1. Human-curated override — trusted/verified. Checked before the cache so
        #    a newly-added override always wins over a stale auto-resolution.
        if cid in self._overrides:
            o = self._overrides[cid]
            return ResolvedURL(url=o.get("url"), source="manual", verified=bool(o.get("verified", True)))

        # 2. Disk cache (skip the network/LLM chain on a hit).
        if use_cache:
            hit = self._cache_get(candidate)
            if hit is not None:
                return hit

        result = self._resolve_chain(candidate)
        self._cache_put(candidate, result)
        return result

    def _resolve_chain(self, candidate: dict[str, Any]) -> ResolvedURL:
        """The actual resolution chain (Wikipedia -> committee -> search backup)."""
        cid = candidate.get("candidate_id", "")

        # Wikipedia infobox campaign site (unverified).
        if self.wikipedia:
            url = self.wikipedia.resolve(candidate.get("name", ""), candidate.get("state", ""))
            if url:
                return ResolvedURL(url=url, source="wikipedia", verified=False)

        # Committee metadata via FEC (free; FEC's website field is ~always empty).
        url = self._from_committee(cid)
        if url:
            return ResolvedURL(url=url, source="committee", verified=False)

        # Search backup — Serper+judge or Claude web_search (unverified). The
        # source records which backend resolved it for the audit trail.
        if self.search:
            url = self.search.resolve(
                candidate.get("name", ""), candidate.get("state", ""),
                candidate.get("office"), str(candidate.get("district") or "") or None)
            if url:
                return ResolvedURL(url=url, source=getattr(self.search, "name", "search"),
                                   verified=False)

        # Nothing resolved.
        return ResolvedURL(url=None, source="none", verified=False)

    # --- disk cache ---------------------------------------------------------
    def _backend_tag(self) -> str:
        """Fingerprint the active backends so enabling/disabling search (or
        Wikipedia) invalidates cached results that those backends would change."""
        return ("wiki" if self.wikipedia else "-") + ":" + getattr(self.search, "name", "none")

    def _cache_path(self, candidate: dict[str, Any]) -> Path | None:
        if not self.cache_dir:
            return None
        key = json.dumps({
            "cid": candidate.get("candidate_id", ""),
            "name": candidate.get("name", ""),
            "state": candidate.get("state", ""),
            "office": candidate.get("office", ""),
            "district": str(candidate.get("district") or ""),
            "backend": self._backend_tag(),
        }, sort_keys=True)
        return self.cache_dir / f"{sha256_text(key)}.json"

    def _cache_get(self, candidate: dict[str, Any]) -> ResolvedURL | None:
        path = self._cache_path(candidate)
        if not path or not path.exists():
            return None
        try:
            blob = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None
        if self.cache_ttl_seconds is not None:
            age = time.time() - float(blob.get("cached_at", 0))
            if age > self.cache_ttl_seconds:
                return None
        return ResolvedURL(url=blob.get("url"), source=blob.get("source", "none"),
                           verified=bool(blob.get("verified", False)))

    def _cache_put(self, candidate: dict[str, Any], result: ResolvedURL) -> None:
        path = self._cache_path(candidate)
        if not path:
            return
        blob = asdict(result)
        blob["cached_at"] = time.time()
        try:
            path.write_text(json.dumps(blob))
        except OSError:
            pass

    def _from_committee(self, candidate_id: str) -> str | None:
        """Look for a committee 'website' field on the candidate's committees."""
        if not self.fec or not candidate_id:
            return None
        try:
            data = self.fec.get(f"/candidate/{candidate_id}/committees/", {"per_page": 20})
        except Exception:
            return None
        for row in data.get("results", []) or []:
            site = row.get("website")
            if site and site.startswith("http"):
                return site
        return None


OFFICE_FULL = {"H": "U.S. House", "S": "U.S. Senate", "P": "President"}

# Domains that are never a candidate's official campaign site.
_SEARCH_BLOCK = (
    "wikipedia.org", "ballotpedia.org", "facebook.com", "twitter.com", "x.com",
    "instagram.com", "youtube.com", "linkedin.com", "tiktok.com", "actblue.com",
    "winred.com", "fec.gov", "opensecrets.org", "vote411.org", "wikidata.org",
    "linktr.ee", "votesmart.org", "govtrack.us", "legistorm.com", "ballotready.org",
    "vote.org", "emilyslist.org", "votevets.org",
)


def _host(url: str) -> str:
    """Bare hostname of a URL, with any leading ``www.`` stripped."""
    h = url.split("//", 1)[-1].split("/", 1)[0].lower()
    return h[4:] if h.startswith("www.") else h


def _is_blocked_host(url: str) -> bool:
    """True if the URL's host is a .gov office or a known non-campaign domain."""
    h = _host(url)
    return h.endswith(".gov") or any(h == b or h.endswith("." + b) for b in _SEARCH_BLOCK)


def _pick_campaign_url(text: str | None) -> str | None:
    """Extract the first URL from free text and reject non-campaign hosts."""
    if not text:
        return None
    m = re.search(r"https?://[^\s)>\]\"'`]+", text)
    if not m:
        return None
    url = m.group(0).rstrip(".,);'\"`")
    return None if _is_blocked_host(url) else url


class SearchResolver:
    """Web-search resolution via Claude's built-in ``web_search`` server tool.

    We reuse the Anthropic key the project already has (no separate search-API
    credential): Claude runs the search AND judges which result is the official
    campaign site for *this specific* candidate, returning a single URL. Output
    is defensively validated against a blocklist (socials, news, donation
    portals, Wikipedia/Ballotpedia, .gov office pages).

    Billable (~$10 / 1,000 searches; a few searches per lookup). It is the last
    link in the chain, so it only fires for candidates the free sources missed.
    Inject ``responder`` (prompt -> text) to test without hitting the API.

    Alternatives if you ever want to decouple search from the LLM: Tavily
    (free tier, LLM-friendly) or Serper.dev (real Google, cheap). Bing's Search
    API was retired in 2025 and Google Programmable Search is closed to new
    customers — don't reach for those.
    """

    name = "search"      # url_source tag for audit ("where did this URL come from")

    def __init__(self, api_key: str | None, *, model: str = "claude-sonnet-4-6",
                 max_uses: int = 4, timeout: float = 60.0,
                 responder: Callable[[str], str] | None = None):
        self.api_key = api_key
        self.model = model
        self.max_uses = max_uses
        self.timeout = timeout      # hard ceiling on the billable web-search call
        self._respond = responder or self._anthropic_respond

    def _anthropic_respond(self, prompt: str) -> str:
        import anthropic

        # Hard timeout so one hung web-search call can't stall a whole (sequential)
        # resolution run. The SDK also retries transient errors a few times within
        # this ceiling; on exhaustion it raises and the caller skips this candidate.
        client = anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout,
                                     max_retries=2)
        resp = client.messages.create(
            model=self.model, max_tokens=1024,
            tools=[{"type": "web_search_20260209", "name": "web_search",
                    "max_uses": self.max_uses}],
            messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in resp.content if b.type == "text").strip()

    @staticmethod
    def _query(name: str, state: str, office: str | None, district: str | None) -> str:
        who = f"{name}, a 2026 candidate for {OFFICE_FULL.get(office or '', 'U.S. Congress')}"
        where = STATE_NAMES.get(state, state)
        if district and office == "H":
            where += f" district {district}"
        return (
            f"Find the official CAMPAIGN website for {who} in {where}. I want the "
            f"candidate's own campaign site (e.g. nameforcongress.com), NOT Wikipedia, "
            f"Ballotpedia, news articles, social media, donation portals "
            f"(ActBlue/WinRed), or a government .gov office page. Reply with ONLY the "
            f"URL on a single line, or exactly NONE if you cannot confidently find the "
            f"official campaign site for this specific person.")

    @classmethod
    def _extract_url(cls, text: str) -> str | None:
        return _pick_campaign_url(text)

    def resolve(self, name: str, state: str, office: str | None = None,
                district: str | None = None) -> str | None:
        if not self.api_key:
            return None
        try:
            text = self._respond(self._query(name, state, office, district))
        except Exception:
            return None
        return self._extract_url(text)


# Judge prompt for SerperResolver. Conservative by design: it must return NONE
# rather than guess, because a misattributed URL is worse than a coverage gap.
_JUDGE_PROMPT = (
    "Candidate: {first} {last}, a 2026 candidate for {office} in {where}.\n"
    "(The candidate may appear under a nickname or shortened first name — e.g. "
    "'Sandy' for 'Chandiha', 'Bob' for 'Robert'.)\n\n"
    "Google search results:\n{listing}\n\n"
    "Return the URL of THIS candidate's OWN official campaign website. Accept a "
    "result whose domain or title clearly identifies it as this person's campaign "
    "(matching the surname and the state/office), nickname forms included. REJECT: "
    "news articles, endorsement orgs, voter guides, Wikipedia/Ballotpedia/govtrack/"
    "legistorm, social media, donation portals (ActBlue/WinRed), government .gov "
    "pages, and any site that belongs to a DIFFERENT candidate. If no result is "
    "clearly this specific person's own campaign site, answer NONE — a wrong or "
    "other-person guess is worse than NONE.\n"
    "Reply with ONLY the URL on a single line, or exactly NONE."
)


class SerperResolver:
    """Google-search resolution via Serper.dev + a cheap LLM judge.

    Serper returns Google organic results — far better recall than Wikipedia for
    obscure primary challengers, and ~50x cheaper than Claude's ``web_search``
    tool. A small Haiku call then judges which result is *this* candidate's own
    campaign site, returning a single URL or NONE. Validated NJ-House recall was
    ~84% (vs ~15% for Wikipedia alone) with zero misattributions.

    The judge is deliberately conservative: it returns NONE rather than guess, and
    its answer must match one of the presented results (no hallucinated domains)
    and pass the shared denylist. A NONE is surfaced as a reviewable coverage gap;
    a misattributed URL never is — so we bias hard toward NONE.

    Inject ``search`` (query -> [{title, link, snippet}]) and ``judge``
    (prompt -> text) to test without hitting the network.
    """

    name = "serper"      # url_source tag for audit
    ENDPOINT = "https://google.serper.dev/search"

    def __init__(self, serper_key: str | None, *, anthropic_api_key: str | None = None,
                 model: str = "claude-haiku-4-5", num: int = 10, timeout: float = 20.0,
                 search: Callable[[str], list[dict]] | None = None,
                 judge: Callable[[str], str] | None = None):
        self.serper_key = serper_key
        self.api_key = anthropic_api_key
        self.model = model
        self.num = num
        self.timeout = timeout
        self._search = search or self._serper_search
        self._judge = judge or self._anthropic_judge

    def _serper_search(self, query: str) -> list[dict]:
        resp = httpx.post(
            self.ENDPOINT,
            headers={"X-API-KEY": self.serper_key or "", "Content-Type": "application/json"},
            json={"q": query, "num": self.num}, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json().get("organic", []) or []

    def _anthropic_judge(self, prompt: str) -> str:
        import anthropic

        client = anthropic.Anthropic(api_key=self.api_key, timeout=self.timeout, max_retries=2)
        msg = client.messages.create(
            model=self.model, max_tokens=60, temperature=0,
            messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in msg.content if b.type == "text").strip()

    @staticmethod
    def _query(first: str, last: str, where: str, office_full: str) -> str:
        return f"{first} {last} {where} {office_full} campaign website".strip()

    @staticmethod
    def _judge_prompt(first: str, last: str, where: str, office_full: str,
                      results: list[dict]) -> str:
        listing = "\n".join(
            f"{i + 1}. {o['link']}\n   title: {o.get('title', '')}\n"
            f"   snippet: {o.get('snippet', '')[:160]}"
            for i, o in enumerate(results))
        return _JUDGE_PROMPT.format(first=first, last=last, office=office_full,
                                    where=where, listing=listing)

    def _select(self, judge_text: str, results: list[dict]) -> str | None:
        """Validate the judge's answer: must pass the denylist AND match one of the
        results it was shown (guards against a hallucinated domain)."""
        url = _pick_campaign_url(judge_text)
        if not url:
            return None
        h = _host(url)
        for o in results:
            if _host(o["link"]) == h:
                return o["link"]      # return the canonical result link
        return None

    def resolve(self, name: str, state: str, office: str | None = None,
                district: str | None = None) -> str | None:
        if not self.serper_key or not self.api_key:
            return None
        first, last = WikipediaResolver.fec_to_name(name)
        if not last:
            return None
        where = STATE_NAMES.get(state, state)
        if district and office == "H":
            where += f" district {district}"
        office_full = OFFICE_FULL.get(office or "", "U.S. Congress")
        try:
            raw = self._search(self._query(first, last, where, office_full))
        except Exception:
            return None
        # Drop obvious non-campaign hosts before the judge sees them.
        results = [o for o in raw if o.get("link") and not _is_blocked_host(o["link"])][:8]
        if not results:
            return None
        try:
            answer = self._judge(self._judge_prompt(first, last, where, office_full, results))
        except Exception:
            return None
        return self._select(answer, results)
