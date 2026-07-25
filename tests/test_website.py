"""Offline tests for website resolution, incl. the Wikipedia resolver (§3.1)."""
from __future__ import annotations

from redbox.website import (ResolvedURL, SearchResolver, SerperResolver,
                            WebsiteResolver, WikipediaResolver)


# --- name normalization ----------------------------------------------------
def test_fec_to_name_strips_honorifics():
    assert WikipediaResolver.fec_to_name("DOE, JANE ANN") == ("Jane", "Doe")
    assert WikipediaResolver.fec_to_name("DAVIS, DON") == ("Don", "Davis")
    assert WikipediaResolver.fec_to_name("DUNN, LAURA L. MS. ESQ.") == ("Laura", "Dunn")
    assert WikipediaResolver.fec_to_name("HARRIS, MARK E") == ("Mark", "Harris")


# --- infobox website extraction --------------------------------------------
def test_extract_prefers_campaign_over_gov():
    wt = "{{Infobox officeholder\n| website = {{URL|dondavis.house.gov|House website}}<br>{{URL|votedondavis.com|Campaign website}}\n}}"
    assert WikipediaResolver.website_from_wikitext(wt) == "https://votedondavis.com"


def test_extract_lowercase_url_template():
    wt = "| website = {{url|janedoeforny.example|Campaign website}}"
    assert WikipediaResolver.website_from_wikitext(wt) == "https://janedoeforny.example"


def test_extract_excludes_gov_when_no_campaign_label():
    wt = "| website = {{URL|jones.house.gov|Official}}<br>{{URL|jonesforcongress.com}}"
    # no 'campaign' label, but the .gov is an office site -> pick the non-gov one
    assert WikipediaResolver.website_from_wikitext(wt) == "https://jonesforcongress.com"


def test_extract_external_link_form():
    wt = "| website = [https://smithforsenate.com Official site]"
    assert WikipediaResolver.website_from_wikitext(wt) == "https://smithforsenate.com"


def test_extract_none_when_no_website_field():
    assert WikipediaResolver.website_from_wikitext("| office = U.S. Rep") is None
    assert WikipediaResolver.website_from_wikitext("| website = (none)") is None


def test_extract_none_when_only_gov_office_site():
    # A state-legislature bio (only a .gov link) is not a campaign site.
    wt = "| website = {{URL|ncleg.gov/Members/Biography/H/757|NC House}}"
    assert WikipediaResolver.website_from_wikitext(wt) is None
    wt2 = "| website = {{URL|jones.house.gov|House website}}"
    assert WikipediaResolver.website_from_wikitext(wt2) is None


# --- resolve() with a stubbed API ------------------------------------------
def _stub_api(title, wikitext):
    def get_json(params):
        if params.get("list") == "search":
            return {"query": {"search": [{"title": title}]}}
        return {"query": {"pages": {"1": {"revisions": [
            {"slots": {"main": {"*": wikitext}}}]}}}}
    return get_json


def test_resolve_matches_and_extracts():
    wp = WikipediaResolver(get_json=_stub_api(
        "Jane Doe", "| website = {{url|janedoeforny.example|Campaign website}}"))
    assert wp.resolve("DOE, JANE ANN", "NY") == "https://janedoeforny.example"


def test_resolve_guards_against_wrong_person():
    # Search returns a page whose title doesn't contain the candidate's name.
    wp = WikipediaResolver(get_json=_stub_api(
        "Some Unrelated Article", "| website = {{url|wrong.com}}"))
    assert wp.resolve("DOE, JANE ANN", "NY") is None


def test_resolve_none_when_page_has_no_website():
    wp = WikipediaResolver(get_json=_stub_api("Don Davis (North Carolina politician)",
                                              "| office = U.S. Representative"))
    assert wp.resolve("DAVIS, DON", "NC") is None


# --- WebsiteResolver chain --------------------------------------------------
def test_manual_override_wins_and_is_verified(tmp_path):
    import json
    ov = tmp_path / "websites.json"
    ov.write_text(json.dumps({"H1": {"url": "https://example.org", "verified": True}}))
    # wikipedia=False disables the live lookup for this offline test.
    r = WebsiteResolver(overrides_path=ov, wikipedia=False)
    res = r.resolve({"candidate_id": "H1", "name": "X, Y", "state": "NY"})
    assert res == ResolvedURL(url="https://example.org", source="manual", verified=True)


def test_chain_falls_through_to_wikipedia(tmp_path):
    wp = WikipediaResolver(get_json=_stub_api(
        "Don Davis (North Carolina politician)",
        "| website = {{URL|votedondavis.com|Campaign website}}"))
    r = WebsiteResolver(overrides_path=tmp_path / "none.json", wikipedia=wp)
    res = r.resolve({"candidate_id": "H2", "name": "DAVIS, DON", "state": "NC"})
    assert res.url == "https://votedondavis.com"
    assert res.source == "wikipedia"
    assert res.verified is False


# --- SearchResolver (web search backup) ------------------------------------
def test_search_extract_url_valid_and_none():
    assert SearchResolver._extract_url("https://janedoeforny.example/") == "https://janedoeforny.example/"
    assert SearchResolver._extract_url("The site: https://janefor.us/home") == "https://janefor.us/home"
    assert SearchResolver._extract_url("NONE") is None
    assert SearchResolver._extract_url("") is None
    # trailing punctuation stripped
    assert SearchResolver._extract_url("(https://janeforcongress.com).") == "https://janeforcongress.com"
    # markdown backticks stripped (seen live: `https://www.jerone.vote/`)
    assert SearchResolver._extract_url("`https://www.jerone.vote/`") == "https://www.jerone.vote/"


def test_search_extract_url_blocks_non_campaign_domains():
    for bad in ["https://en.wikipedia.org/wiki/Jane", "https://ballotpedia.org/Jane",
                "https://www.facebook.com/jane", "https://secure.actblue.com/x",
                "https://jane.house.gov", "https://ncleg.gov/Members/X"]:
        assert SearchResolver._extract_url(bad) is None, bad


def test_search_resolve_uses_responder_and_validates():
    sr = SearchResolver("fake-key", responder=lambda prompt: "https://kaskyforcongress.com")
    assert sr.resolve("KASKY, CAMERON", "NY", "H", "12") == "https://kaskyforcongress.com"
    # a blocked answer -> None
    sr2 = SearchResolver("fake-key", responder=lambda p: "https://twitter.com/cameron")
    assert sr2.resolve("KASKY, CAMERON", "NY", "H", "12") is None


def test_search_resolve_no_key_returns_none():
    sr = SearchResolver(None, responder=lambda p: "https://x.com")
    assert sr.resolve("X, Y", "NY") is None


def test_search_query_includes_office_and_district():
    q = SearchResolver._query("Jane Doe", "NY", "H", "12")
    assert "U.S. House" in q and "New York" in q and "district 12" in q


def test_chain_falls_through_to_search(tmp_path):
    # Wikipedia misses -> search backup resolves.
    wp = WikipediaResolver(get_json=_stub_api("Unrelated", "| office = x"))  # guard -> None
    sr = SearchResolver("fake-key", responder=lambda p: "https://janedoeforny.example")
    r = WebsiteResolver(overrides_path=tmp_path / "none.json", wikipedia=wp, search=sr)
    res = r.resolve({"candidate_id": "H9", "name": "DOE, JANE", "state": "NY",
                     "office": "H", "district": "12"})
    assert res.url == "https://janedoeforny.example"
    assert res.source == "search"
    assert res.verified is False


def test_search_disabled_by_default_without_key(tmp_path):
    # No key + enable_search default False -> no search resolver built.
    wp = WikipediaResolver(get_json=_stub_api("Unrelated", "| office = x"))
    r = WebsiteResolver(overrides_path=tmp_path / "none.json", wikipedia=wp)
    assert r.search is None
    res = r.resolve({"candidate_id": "H9", "name": "DOE, JANE", "state": "NY"})
    assert res.url is None and res.source == "none"


# --- SerperResolver (Google search + LLM judge) ----------------------------
_NJ12 = [
    {"link": "https://ballotpedia.org/Adam_Hamawy", "title": "Adam Hamawy - Ballotpedia", "snippet": ""},
    {"link": "https://hamawyfornj.com/", "title": "Dr. Adam Hamawy for Congress", "snippet": "Doctor. Veteran."},
    {"link": "https://www.instagram.com/hamawyfornj/", "title": "Instagram", "snippet": ""},
]


def _serper(results):
    return lambda query: results


def test_serper_search_plus_judge_picks_campaign_site():
    sr = SerperResolver("serp-key", anthropic_api_key="anth-key",
                        search=_serper(_NJ12), judge=lambda p: "https://hamawyfornj.com/")
    assert sr.resolve("HAMAWY, ADAM", "NJ", "H", "12") == "https://hamawyfornj.com/"


def test_serper_judge_none_yields_no_url():
    # The misattribution guard: judge can't confirm this person's site -> NONE.
    sr = SerperResolver("serp-key", anthropic_api_key="anth-key",
                        search=_serper(_NJ12), judge=lambda p: "NONE")
    assert sr.resolve("SOOY, SARA", "NJ", "H", "07") is None


def test_serper_rejects_hallucinated_url_not_in_results():
    # Judge returns a URL that was never in the search results -> rejected.
    sr = SerperResolver("serp-key", anthropic_api_key="anth-key",
                        search=_serper(_NJ12), judge=lambda p: "https://madeup-notinresults.com")
    assert sr.resolve("HAMAWY, ADAM", "NJ", "H", "12") is None


def test_serper_blocklisted_results_are_hidden_from_judge():
    # Only the campaign site survives pre-filtering; the judge never sees socials/
    # Ballotpedia, and a judge that echoes one back still can't pass _select.
    seen = {}
    def judge(prompt):
        seen["prompt"] = prompt
        return "https://ballotpedia.org/Adam_Hamawy"
    sr = SerperResolver("serp-key", anthropic_api_key="anth-key",
                        search=_serper(_NJ12), judge=judge)
    assert sr.resolve("HAMAWY, ADAM", "NJ", "H", "12") is None
    assert "ballotpedia.org" not in seen["prompt"]      # denylisted before the judge
    assert "hamawyfornj.com" in seen["prompt"]


def test_serper_requires_both_keys():
    sr = SerperResolver(None, anthropic_api_key="anth-key", search=_serper(_NJ12),
                        judge=lambda p: "https://hamawyfornj.com/")
    assert sr.resolve("HAMAWY, ADAM", "NJ", "H", "12") is None
    sr2 = SerperResolver("serp-key", anthropic_api_key=None, search=_serper(_NJ12),
                         judge=lambda p: "https://hamawyfornj.com/")
    assert sr2.resolve("HAMAWY, ADAM", "NJ", "H", "12") is None


def test_serper_query_includes_disambiguation():
    q = SerperResolver._query("Adam", "Hamawy", "New Jersey district 12", "U.S. House")
    assert "Adam Hamawy" in q and "New Jersey district 12" in q and "U.S. House" in q


def test_chain_prefers_serper_when_key_present(tmp_path):
    # With a SERPER_KEY, the search backend is Serper (source tag 'serper').
    wp = WikipediaResolver(get_json=_stub_api("Unrelated", "| office = x"))
    r = WebsiteResolver(overrides_path=tmp_path / "none.json", wikipedia=wp,
                        enable_search=True, anthropic_api_key="anth", serper_key="serp")
    assert isinstance(r.search, SerperResolver)
    # inject the network seam so resolve() is offline
    r.search._search = _serper(_NJ12)
    r.search._judge = lambda p: "https://hamawyfornj.com/"
    res = r.resolve({"candidate_id": "H9", "name": "HAMAWY, ADAM", "state": "NJ",
                     "office": "H", "district": "12"})
    assert res.url == "https://hamawyfornj.com/"
    assert res.source == "serper"


def test_chain_falls_back_to_websearch_without_serper_key(tmp_path):
    wp = WikipediaResolver(get_json=_stub_api("Unrelated", "| office = x"))
    r = WebsiteResolver(overrides_path=tmp_path / "none.json", wikipedia=wp,
                        enable_search=True, anthropic_api_key="anth")  # no serper_key
    assert isinstance(r.search, SearchResolver)


# --- resolution disk cache --------------------------------------------------
class _CountingSearch:
    """Stand-in search backend that records how many live lookups it ran."""
    name = "fake"

    def __init__(self, url):
        self.url = url
        self.calls = 0

    def resolve(self, name, state, office=None, district=None):
        self.calls += 1
        return self.url


_CAND = {"candidate_id": "H1", "name": "DOE, JANE", "state": "NY",
         "office": "H", "district": "1"}


def _cached_resolver(tmp_path, search, **kw):
    return WebsiteResolver(overrides_path=tmp_path / "none.json", wikipedia=False,
                           search=search, cache_dir=tmp_path / "cache", **kw)


def test_cache_hit_skips_the_chain(tmp_path):
    s = _CountingSearch("https://janefor.us")
    r = _cached_resolver(tmp_path, s)
    a = r.resolve(_CAND)
    b = r.resolve(_CAND)
    assert a.url == b.url == "https://janefor.us"
    assert a.source == "fake"
    assert s.calls == 1                 # second lookup served from disk cache


def test_force_bypasses_cache_read_but_refreshes(tmp_path):
    s = _CountingSearch("https://janefor.us")
    r = _cached_resolver(tmp_path, s)
    r.resolve(_CAND)
    r.resolve(_CAND, use_cache=False)   # --force path: re-runs the chain
    assert s.calls == 2


def test_cache_respects_ttl(tmp_path):
    import json
    s = _CountingSearch("https://janefor.us")
    r = _cached_resolver(tmp_path, s, cache_ttl_seconds=100)
    r.resolve(_CAND)
    # Age the cached entry past the TTL; next resolve must re-run.
    f = next((tmp_path / "cache").glob("*.json"))
    blob = json.loads(f.read_text())
    blob["cached_at"] -= 1000
    f.write_text(json.dumps(blob))
    r.resolve(_CAND)
    assert s.calls == 2


def test_cache_key_includes_active_backends(tmp_path):
    # A result found via search must NOT be reused by a resolver with search off:
    # the backend fingerprint is part of the cache key.
    s = _CountingSearch("https://janefor.us")
    _cached_resolver(tmp_path, s).resolve(_CAND)
    no_search = WebsiteResolver(overrides_path=tmp_path / "none.json", wikipedia=False,
                                search=False, cache_dir=tmp_path / "cache")
    res = no_search.resolve(_CAND)
    assert res.url is None and res.source == "none"   # cache miss -> empty chain


def test_no_cache_dir_means_no_caching(tmp_path):
    s = _CountingSearch("https://janefor.us")
    r = WebsiteResolver(overrides_path=tmp_path / "none.json", wikipedia=False, search=s)
    r.resolve(_CAND)
    r.resolve(_CAND)
    assert s.calls == 2                 # no cache_dir -> every call hits the chain


def test_override_wins_over_cache(tmp_path):
    import json
    ov = tmp_path / "websites.json"
    ov.write_text(json.dumps({"H1": {"url": "https://manual.example", "verified": True}}))
    s = _CountingSearch("https://janefor.us")
    r = WebsiteResolver(overrides_path=ov, wikipedia=False, search=s,
                        cache_dir=tmp_path / "cache")
    res = r.resolve(_CAND)
    assert res.url == "https://manual.example" and res.source == "manual"
    assert s.calls == 0                 # override short-circuits before any lookup
