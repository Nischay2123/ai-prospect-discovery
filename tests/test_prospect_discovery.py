"""Offline tests for the whole pipeline and its parts. Grouped in the brief's processing-flow order."""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app.errors import GuardrailBlocked, IdempotencyConflict, LLMUnavailable, LocalSkip, QueryNotUnderstood, SourceError
from app.main import app
from app.platforms.base import llm_phrase, role_terms
from app.platforms.linkedin import LinkedIn
from app.schemas import CandidateProfile, Contact, DiscoveryRequest, Location, PlatformQueries, QueryIntent
from app.services.cache import SingleFlight, TTLCache
from app.services.deduplication_service import consolidate
from app.services.llm_service import strict_schema
from app.services.profile_extractor import ProfileExtractor, hit_to_candidate
from app.services.relevance_service import match_of, prefilter_score
from app.services.resilience import CircuitBreaker, ClientLimiter, Deadline, Guard, ProviderLimiter, RetryPolicy
from app.services.search_service import FailoverSearch
from app.utils.normalizers import name_from_title, normalize_url, profile_of
from app.utils.validators import check_query, find_contacts, quote_in_text, valid_email
from tests.conftest import FOOTBALL_DC, FakeLLM, FakePlatform, hit, make_service


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- step 2: input validation + guardrails ----

@pytest.mark.parametrize("query", ["", "  ", "ab", "x" * 301, "football\x00 fans"])
def test_invalid_queries_rejected(query):
    with pytest.raises(ValueError):
        DiscoveryRequest(query=query)


def test_query_whitespace_collapsed():
    assert DiscoveryRequest(query="  Football   lovers in DC ").query == "Football lovers in DC"


@pytest.mark.parametrize("query,rule", [
    ("Find John Smith's phone number", "targeted_individual"),
    ("email address of Jane Doe", "targeted_individual"),
    ("Diabetes patients in Ahmedabad", "sensitive_category"),
    ("People with depression in London", "sensitive_category"),
    ("Muslims in Dubai", "sensitive_category"),
    ("Teenagers in London", "sensitive_category"),
    ("fans of https://example.com in DC", "contact_in_query"),
    ("call +1 202 555 0143 football", "contact_in_query"),
])
def test_guardrails_block(query, rule):
    assert check_query(query) == rule


@pytest.mark.parametrize("query", [
    "Football lovers in Washington DC in USA", "Digital marketing agency owners in New York USA",
    "Startup founders in London UK", "Cricket lovers in Ahmedabad India", "Restaurant owners in Dubai UAE",
    "Hearing aids shop owners in Pune",
    "Startups founded 2020 - 2024 in London UK",
    "Dentists treating patients in Pune India",
    "Christian bookstore owners in Texas USA",
    "Cancer researchers in Boston USA",
    "Top 100 restaurant owners in Dubai",
])
def test_guardrails_allow_brief_queries(query):
    assert check_query(query) is None


# ------------------------------------------------------------ steps 3-5: intent + search queries ----

def test_intent_cached_and_invalid_query_raises():
    service, llm, _ = make_service()
    run(service._queries.understand("Football lovers in Washington DC in USA", 5))
    run(service._queries.understand("football lovers in washington dc in usa ", 5))
    assert llm.calls == ["intent"]  # second call served from cache

    bad = FOOTBALL_DC.model_copy(update={"is_valid": False, "reason": "Not a prospect search"})
    service, _, _ = make_service(FakeLLM(intent=bad))
    with pytest.raises(QueryNotUnderstood):
        run(service._queries.understand("what is the weather", 5))


def test_platform_query_uses_llm_phrase_but_code_owns_site_operator():
    intent = FOOTBALL_DC.model_copy(update={"search_queries": FOOTBALL_DC.search_queries.model_copy(
        update={"linkedin": 'site:evil.com "football coach" Washington DC'})})
    query = LinkedIn(search=None).build_query(intent)
    assert query.startswith("site:linkedin.com/in ")
    assert "evil.com" not in query and '"football coach"' in query


def test_template_fallback_when_llm_gives_no_phrase():
    empty = PlatformQueries(google="", linkedin="", x="", youtube="", instagram="", facebook="", threads="")
    intent = FOOTBALL_DC.model_copy(update={"search_queries": empty, "interest": "Digital Marketing",
                                            "audience": "Digital marketing agency owners"})
    assert llm_phrase(intent, "LinkedIn") == ""
    assert role_terms(intent) == "agency owners"
    assert LinkedIn(search=None).build_query(intent) == 'site:linkedin.com/in "Digital Marketing" agency owners ' \
                                                       '"Washington DC"'


def test_strict_schema_has_no_refs_or_defaults():
    schema = strict_schema(QueryIntent)
    text = str(schema)
    assert "$ref" not in text and "$defs" not in text and "'default'" not in text
    assert schema["additionalProperties"] is False
    assert schema["properties"]["location"]["additionalProperties"] is False


# ---------------------------------------------------------------- step 6: discovery fan-out ----

def test_one_platform_failing_does_not_affect_others():
    platforms = [FakePlatform("LinkedIn", [hit("https://linkedin.com/in/a", "A B | LinkedIn", "", "LinkedIn")]),
                 FakePlatform("X", error=RuntimeError("boom")),
                 FakePlatform("Facebook", error=TimeoutError()),
                 FakePlatform("Threads", configured=False)]
    service, _, _ = make_service(platforms=platforms)
    hits, statuses = run(service._search.discover_all(FOOTBALL_DC, Deadline(10)))
    by = {s.platform: s for s in statuses}
    assert len(hits) == 1 and by["LinkedIn"].status == "ok"
    assert by["X"].error_type == "unexpected"
    assert by["Facebook"].error_type == "timeout"
    assert by["Threads"].status == "not_configured"


def test_search_results_cached():
    platform = FakePlatform("LinkedIn", [hit("https://linkedin.com/in/a", "A | LinkedIn", "", "LinkedIn")])
    service, _, _ = make_service(platforms=[platform])
    run(service._search.discover_all(FOOTBALL_DC, Deadline(10)))
    run(service._search.discover_all(FOOTBALL_DC, Deadline(10)))
    assert platform.calls == 1


# ---------------------------------------------------------------- steps 7-8: extraction + validation ----

@pytest.mark.parametrize("url,expected", [
    ("https://uk.linkedin.com/in/jane-doe/", ("LinkedIn", "jane-doe")),
    ("https://twitter.com/jdoe?lang=en", ("X", "jdoe")),
    ("https://www.instagram.com/jane.doe/", ("Instagram", "jane.doe")),
    ("https://www.threads.com/@jane", ("Threads", "jane")),
    ("https://www.youtube.com/@JaneDoe", ("YouTube", "janedoe")),
    ("https://www.facebook.com/profile.php?id=1000123&ref=x", ("Facebook", "1000123")),
    ("https://www.instagram.com/p/abc/", None),
    ("https://x.com/jdoe/status/1", None),
    ("https://www.facebook.com/groups/dc", None),
    ("https://linkedin.com/posts/abc", None),
    ("https://www.linkedin.com/company/hunter-digital/", ("LinkedIn", "hunter-digital")),
    ("https://example.com/in/jane", None),
])
def test_profile_urls(url, expected):
    assert profile_of(url) == expected


def test_normalize_url():
    assert normalize_url("HTTP://Mobile.Twitter.com/JDoe/?s=20#x") == "https://x.com/jdoe"
    assert normalize_url("ftp://x.com") is None


@pytest.mark.parametrize("platform,title,name", [
    ("LinkedIn", "Jane Doe - Founder at Acme - London | LinkedIn", "Jane Doe"),
    ("X", "John Smith (@johnsmithdc) / X", "John Smith"),
    ("Instagram", "Jane Doe (@jd) • Instagram photos and videos", "Jane Doe"),
    ("Facebook", "Log in | Facebook", None),
    ("LinkedIn", "Jane D... | LinkedIn", None),
    ("Google", "Contact us | Abo Ali Restaurant & Cafe", "Abo Ali Restaurant & Cafe"),
    ("Google", "Contact Abo Ali Restaurant & Cafe", "Abo Ali Restaurant & Cafe"),
    ("Google", "Contact", None),
    ("Google", "About Us - Acme Agency", "Acme Agency"),
    ("LinkedIn", "Christopher undefined - Sports Analyst | LinkedIn", "Christopher"),
    ("LinkedIn", "Aisha Shakoor\u200f - Owner | LinkedIn", "Aisha Shakoor"),
])
def test_name_from_title(platform, title, name):
    assert name_from_title(platform, title) == name


def test_contacts_validated_and_never_guessed():
    text = ("Mail jane@janedoe-sports.com or noreply@shop.com, logo@2x.png, test@example.com. "
            "Call (202) 555-0143. Chat https://wa.me/12025550143")
    contacts = {(c.kind, c.value) for c in find_contacts(text, "https://src", "US")}
    assert ("Email", "mailto:jane@janedoe-sports.com") in contacts
    assert ("Phone", "tel:+12025550143") in contacts
    assert ("WhatsApp", "https://wa.me/12025550143") in contacts
    assert not any("noreply" in v or "example.com" in v or "2x.png" in v for _, v in contacts)
    # A phone number alone never becomes a WhatsApp link.
    only_phone = find_contacts("Call (202) 555-0143", "https://src", "US")
    assert [c.kind for c in only_phone] == ["Phone"]
    assert valid_email("not-an-email") is None


def test_hit_to_candidate_rejects_non_profiles_and_maps_platform():
    assert hit_to_candidate(hit("https://www.instagram.com/p/x/", "Post", "", "Instagram"), "US") is None
    assert hit_to_candidate(hit("https://news.bbc.co.uk/sport", "BBC Sport", ""), "US") is None
    c = hit_to_candidate(hit("https://linkedin.com/in/jane", "Jane Doe - Analyst | LinkedIn", "", "Google"), "US")
    assert c.platform == "LinkedIn" and c.name == "Jane Doe"  # found via Google, credited to LinkedIn


def test_website_enrichment_reads_contacts_and_linked_profiles():
    html = ('<html><body><a href="mailto:hello@acme-agency.com">Mail</a><a href="tel:+12125550143">Call</a>'
            '<a href="https://instagram.com/acmeagency">IG</a><a href="https://wa.me/12125550143">WA</a></body></html>')

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    extractor = ProfileExtractor(client, TTLCache(60))
    site = hit_to_candidate(hit("https://acme-agency.com/", "Acme Agency | Digital Marketing NYC", ""), "US")
    linked = run(extractor.enrich_from_websites([site], "US", 5))
    kinds = {c.kind for c in site.contacts}
    assert kinds == {"Email", "Phone", "WhatsApp"}
    assert [(c.platform, c.url) for c in linked] == [("Instagram", "https://instagram.com/acmeagency")]


def test_robots_disallow_is_respected():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        raise AssertionError("page must not be fetched")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    site = hit_to_candidate(hit("https://acme-agency.com/", "Acme Agency | NYC", ""), "US")
    assert run(ProfileExtractor(client, TTLCache(60)).enrich_from_websites([site], "US", 5)) == []


# ---------------------------------------------------------------- step 9: relevance ----

def _cand(cid, platform, url, name, text, contacts=(), linked=(), handle=None):
    return CandidateProfile(id=cid, platform=platform, url=url, name=name, handle=handle, text=text,
                            source_url=url, contacts=list(contacts), linked_urls=list(linked))


def test_prefilter_and_match_levels():
    c = _cand("c1", "X", "https://x.com/a", "A", "DC football fan")
    assert prefilter_score(c, FOOTBALL_DC) == 2
    assert prefilter_score(_cand("c2", "X", "https://x.com/b", "B", "Join us for cooking"), FOOTBALL_DC) == 0
    from app.schemas import Verdict
    assert match_of(Verdict(candidate_id="c", interest="yes", location="yes", audience="unknown", evidence="",
                            location_text="")) == "confirmed"
    assert match_of(Verdict(candidate_id="c", interest="yes", location="unknown", audience="unknown",
                            evidence="", location_text="")) == "partial"
    assert match_of(Verdict(candidate_id="c", interest="yes", location="no", audience="yes", evidence="",
                            location_text="")) is None
    # Location alone is not a match: interest is the core of the query.
    assert match_of(Verdict(candidate_id="c", interest="unknown", location="yes", audience="unknown", evidence="",
                            location_text="DC")) is None


def test_fabricated_evidence_quote_is_downgraded_and_unknown_ids_dropped():
    service, _, _ = make_service(FakeLLM(bad_quote=True, extra_verdict_ids=["c_invented"]))
    c = _cand("c1", "X", "https://x.com/a", "A", "DC football fan")
    result = run(service._relevance.assess([c], FOOTBALL_DC, 5))
    assert result["c1"].match is None  # "invented quote" isn't in the text, so no "yes" survives
    assert "c_invented" not in result
    assert quote_in_text("Washington, DC", "Based in washington dc!")


def test_relevance_llm_outage_falls_back_to_partial_keyword_matches():
    service, _, _ = make_service(FakeLLM(fail={"relevance"}))
    c = _cand("c1", "X", "https://x.com/a", "A", "Washington football fan")
    assert run(service._relevance.assess([c], FOOTBALL_DC, 5))["c1"].match == "partial"


# ---------------------------------------------------------------- step 10: consolidation ----

def test_merge_on_strong_signals_only():
    email = Contact("Email", "mailto:jane@x-sports.com", "s")
    a = _cand("a", "LinkedIn", "https://linkedin.com/in/jane", "Jane Doe", "", [email])
    b = _cand("b", "Instagram", "https://instagram.com/janed", "Jane Doe", "", [email])
    c = _cand("c", "X", "https://x.com/jd", "Jane Doe", "", linked=["https://instagram.com/janed"])
    d = _cand("d", "Facebook", "https://facebook.com/jane.doe.77", "Jane Doe", "Washington DC")  # same name only
    clusters = consolidate([a, b, c, d])
    sizes = sorted(len(cl.candidates) for cl in clusters)
    assert sizes == [1, 3]
    merged = next(cl for cl in clusters if len(cl.candidates) == 3)
    assert any(r.startswith("same_email") for r in merged.merge_reasons)
    assert any(r.startswith("cross_link") for r in merged.merge_reasons)


def test_same_handle_needs_a_shared_name_word():
    a = _cand("a", "X", "https://x.com/janedoe", "Jane Doe", "", handle="janedoe")
    b = _cand("b", "Instagram", "https://instagram.com/janedoe", "Jane Doe", "", handle="janedoe")
    c = _cand("c", "Threads", "https://threads.net/@janedoe", "Marco Polo", "", handle="janedoe")
    assert sorted(len(cl.candidates) for cl in consolidate([a, b, c])) == [1, 2]


def test_brand_handles_and_website_merge_but_strangers_do_not():
    # Hunter Digital: same brand handle on Instagram, X and its website domain; Facebook extends it.
    ig = _cand("a", "Instagram", "https://instagram.com/hunterdigital", "Hunter Digital", "", handle="hunterdigital")
    x = _cand("b", "X", "https://x.com/hunterdigital_", "HUNTER Digital Marketing Agency", "", handle="HunterDigital_")
    site = _cand("c", "Google", "https://hunterdigital.com", "Hunter Digital", "", handle="hunterdigital")
    fb = _cand("d", "Facebook", "https://facebook.com/hunterdigitalagency", "Hunter Digital", "",
               handle="hunterdigitalagency")
    # Copify: same handle, name differs but the handle contains a name word.
    cx = _cand("e", "X", "https://x.com/copifycom", "copifycom", "", handle="copifycom")
    cy = _cand("f", "YouTube", "https://youtube.com/@copifycom", "Copify Digital Marketing Agency", "",
               handle="copifycom")
    clusters = consolidate([ig, x, site, fb, cx, cy])
    assert sorted(len(cl.candidates) for cl in clusters) == [2, 4]


def test_urls_compare_case_insensitively_for_handles():
    assert normalize_url("https://x.com/GCAMotera") == normalize_url("https://x.com/gcamotera")
    assert normalize_url("https://youtube.com/channel/UCAbC") != normalize_url("https://youtube.com/channel/UCabc")


def test_listing_pages_are_not_prospects():
    assert hit_to_candidate(hit("https://cofounders.example.org/", "Find or become a startup Co-Founder", ""),
                            "GB") is None
    assert hit_to_candidate(hit("https://facebook.com/cofounderfinder", "Find or become a startup Co-Founder | "
                                "Facebook", "", "Facebook"), "GB") is None
    assert hit_to_candidate(hit("https://topstartups.io/", "Top 43 London Startups (2026)", ""), "GB") is None
    assert hit_to_candidate(hit("https://uk.foundersbase.com/", "Foundersbase: Find a Co-Founder in United Kingdom",
                                ""), "GB") is None


# ---------------------------------------------------------------- steps 11-13: full pipeline + API ----

def test_full_pipeline_builds_consolidated_honest_prospects(store):
    service, _, _ = make_service(store=store)
    outcome = run(service.discover("Football lovers in Washington DC in USA"))
    response = outcome.response
    names = [p.name for p in response.prospects]
    assert names == ["Jane Doe", "John Smith"]  # "Other Name" (cooking, Paris) dropped; posts/groups dropped
    jane = response.prospects[0]
    assert jane.match == "confirmed" and jane.location == "Washington DC"  # quoted from its own text
    assert [(link.platform, link.link) for link in jane.social_link] == [
        ("LinkedIn", "https://linkedin.com/in/jane-doe-123"),
        ("Instagram", "https://instagram.com/janedoe_dc"),
        ("Email", "mailto:jane@janedoe-sports.com"),
    ]
    assert response.total_prospects == 2
    stored = store.get(outcome.run_id)
    assert stored["total_prospects"] == 2 and len(stored["sources"]) == 4  # write-as-you-go per platform


def test_idempotency_replay_conflict_and_resume(store):
    service, llm, platforms = make_service(store=store)
    first = run(service.discover("Football lovers in Washington DC in USA", "key-1"))
    again = run(service.discover("Football lovers in Washington DC in USA", "key-1"))
    assert again.replayed and again.response == first.response
    assert sum(p.calls for p in platforms) == len(platforms)  # replay made no external calls
    with pytest.raises(IdempotencyConflict):
        run(service.discover("Cricket lovers in Ahmedabad India", "key-1"))

    # Crash mid-run: the run row exists with one platform stored, no final response.
    crashed = store.start("Football lovers in Washington DC in USA", "key-2", None)
    from app.schemas import PlatformStatus
    store.record_source(crashed.id, "X", [hit("https://x.com/johnsmithdc", "John Smith (@johnsmithdc) / X",
                                              "DC football fan", "X")],
                        PlatformStatus(platform="X", method="search_api", status="ok", found=1))
    fresh_service, _, fresh_platforms = make_service(store=store)
    resumed = run(fresh_service.discover("Football lovers in Washington DC in USA", "key-2"))
    assert next(p for p in fresh_platforms if p.name == "X").calls == 0  # reused, not searched again
    assert resumed.response.total_prospects == 2


def test_storage_failure_never_costs_the_results(store):
    store.record_source = lambda *a: (_ for _ in ()).throw(RuntimeError("disk full"))
    store.finish = lambda *a: (_ for _ in ()).throw(RuntimeError("disk full"))
    service, _, _ = make_service(store=store)
    assert run(service.discover("Football lovers in Washington DC in USA")).response.total_prospects == 2


def test_single_flight_shares_identical_concurrent_requests():
    service, llm, platforms = make_service()

    async def both():
        return await asyncio.gather(service.discover("Football lovers in DC"),
                                    service.discover("football lovers in dc"))

    a, b = run(both())
    assert a.response == b.response and (a.shared or b.shared)
    assert llm.calls.count("intent") == 1


def test_intent_llm_outage_raises_unavailable():
    service, _, _ = make_service(FakeLLM(fail={"intent"}))
    with pytest.raises(LLMUnavailable):
        run(service.discover("Football lovers in Washington DC in USA"))


def test_guardrail_runs_before_any_spend():
    service, llm, platforms = make_service()
    with pytest.raises(GuardrailBlocked):
        run(service.discover("Find John Smith's phone number"))
    assert llm.calls == [] and all(p.calls == 0 for p in platforms)


@pytest.fixture
def client():
    with TestClient(app) as c:
        app.state.service, _, _ = make_service()
        app.state.client_limiter = ClientLimiter(100)
        yield c


def test_api_response_is_the_briefs_exact_shape(client):
    response = client.post("/prospect-discovery", json={"query": "Football lovers in Washington DC in USA"})
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"success", "query", "total_prospects", "prospects"}
    # Brief §5 shape, plus `match` so verified and partial matches can be told apart (§11).
    assert all(set(p) == {"name", "location", "social_link", "match"} for p in body["prospects"])
    assert client.app.openapi()["components"]["schemas"]["PublicProspect"]["required"] == ["name", "match"]
    assert all(set(link) == {"platform", "link"} for p in body["prospects"] for link in p["social_link"])


def test_api_debug_adds_intent_platforms_and_evidence(client):
    body = client.post("/prospect-discovery?debug=true", json={"query": "Football lovers in Washington DC"}).json()
    assert body["intent"]["location"]["city"] == "Washington DC"
    assert {p["platform"] for p in body["platforms"]} == set(["LinkedIn", "Instagram", "X", "Facebook"])
    assert all(p["match"] in ("confirmed", "partial") and p["evidence"] for p in body["prospects"])


def test_api_status_codes(client):
    assert client.post("/prospect-discovery", json={}).status_code == 422
    blocked = client.post("/prospect-discovery", json={"query": "Diabetes patients in Ahmedabad"})
    assert blocked.status_code == 422 and blocked.json()["detail"]["rule"] == "sensitive_category"
    app.state.service, _, _ = make_service(FakeLLM(fail={"intent"}))
    down = client.post("/prospect-discovery", json={"query": "Football lovers in DC"})
    assert down.status_code == 503 and down.headers["Retry-After"]


def test_api_key_and_client_rate_limit(client):
    from app import main
    object.__setattr__(main.settings, "api_key", "s3cret")
    try:
        assert client.post("/prospect-discovery", json={"query": "Football lovers in DC"}).status_code == 401
        ok = client.post("/prospect-discovery", json={"query": "Football lovers in DC"},
                         headers={"X-API-Key": "s3cret"})
        assert ok.status_code == 200
    finally:
        object.__setattr__(main.settings, "api_key", "")
    app.state.client_limiter = ClientLimiter(1)
    client.post("/prospect-discovery", json={"query": "Football lovers in DC"})
    limited = client.post("/prospect-discovery", json={"query": "Football lovers in DC"})
    assert limited.status_code == 429 and limited.headers["Retry-After"]


# ---------------------------------------------------------------- resilience ----

class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_retry_policy_read_only_rules():
    policy = RetryPolicy()
    assert policy.is_retryable(SourceError("x", 500)) and policy.is_retryable(SourceError("x", 429))
    assert policy.is_retryable(httpx.ReadTimeout("t"))
    assert not policy.is_retryable(SourceError("x", 401))
    assert not policy.is_retryable(SourceError("x", 402, quota_exhausted=True))
    assert policy.delay(0, SourceError("x", 429, retry_after=3)) == 3
    assert all(0 <= policy.delay(n, ValueError()) <= 4 for n in range(6))


def test_retry_runs_then_succeeds():
    calls = []

    async def flaky(_remaining):
        calls.append(1)
        if len(calls) == 1:
            raise SourceError("busy", 503)
        return "ok"

    assert run(RetryPolicy(base_delay=0.001).run(flaky, 5)) == "ok" and len(calls) == 2


def test_circuit_breaker_opens_and_half_opens():
    clock = _Clock()
    breaker = CircuitBreaker(failure_threshold=2, reset_after=10, clock=clock)
    breaker.record_failure("serper")
    breaker.record_failure("serper")
    assert breaker.allow("serper") > 0
    clock.now = 11
    assert breaker.allow("serper") == 0 and breaker.allow("serper") > 0  # one trial only
    breaker.record_success("serper")
    assert breaker.state("serper") == "closed"


def test_provider_limiter_and_daily_cap():
    clock = _Clock()
    limiter = ProviderLimiter({"brave": 1.0}, {"youtube_search": 2}, clock=clock, today=lambda: "d1")
    assert limiter.acquire("brave") == 0 and limiter.acquire("brave") > 0
    clock.now = 1.0
    assert limiter.acquire("brave") == 0
    limiter.count_daily("youtube_search")
    limiter.count_daily("youtube_search")
    assert limiter.daily_exhausted("youtube_search")


def test_failover_search_switches_provider_on_quota():
    class Provider:
        def __init__(self, name, result):
            self.name, self.result = name, result

        def is_configured(self):
            return True

        async def search(self, client, query, country, platform):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    guard = Guard(CircuitBreaker(), ProviderLimiter({}), RetryPolicy())
    search = FailoverSearch([Provider("serper", SourceError("no credits", 400, quota_exhausted=True)),
                             Provider("brave", [hit("https://x.com/a", "A (@a) / X", "", "X")])], guard)
    hits, provider = run(search.search(None, "q", "US", "X", 5))
    assert provider == "brave" and len(hits) == 1

    # Low yield from the first provider: the second one is asked too and results are merged.
    one = [hit("https://x.com/a", "A (@a) / X", "", "X")]
    more = [hit("https://x.com/a", "A (@a) / X", "", "X"), hit("https://x.com/b", "B (@b) / X", "", "X")]
    merged = FailoverSearch([Provider("serper", one), Provider("brave", more)], guard)
    hits, provider = run(merged.search(None, "q", "US", "X", 5))
    assert provider == "serper+brave" and [h.url for h in hits] == ["https://x.com/a", "https://x.com/b"]

    none = FailoverSearch([], guard)
    with pytest.raises(LocalSkip):
        run(none.search(None, "q", "US", "X", 5))


def test_ttl_cache_and_single_flight():
    clock = _Clock()
    cache = TTLCache(ttl=10, clock=clock)
    cache.set("k", 1)
    assert cache.get("k") == 1
    clock.now = 11
    assert cache.get("k") is None

    calls = []

    async def work():
        calls.append(1)
        await asyncio.sleep(0.01)
        return 42

    async def both():
        flight = SingleFlight()
        return await asyncio.gather(flight.run("k", work), flight.run("k", work))

    assert [r for r, _ in run(both())] == [42, 42] and len(calls) == 1


def test_location_label_matches_brief_style():
    assert Location(city="Washington DC", state="District of Columbia", country="USA").label() == "Washington DC, USA"
    assert Location(city=None, state=None, country="UK").label() == "UK"


# ---------------------------------------------------------------- evals in CI (deterministic part) ----

def test_dedup_eval_dataset_passes():
    from evals.run import eval_dedup
    result = eval_dedup()
    assert result["accuracy"] == 1.0, result["failures"]


# ---------------------------------------------------------------- live-run fixes ----

def test_serper_drops_google_redirect_links_and_uses_subtitle():
    from app.services.search_service import Serper

    payload = {"organic": [
        {"title": "Adam B. - Founder | LinkedIn", "link": "https://uk.linkedin.com/in/adamcbradley",
         "subtitle": "London, England, United Kingdom · Founder · Launchology", "snippet": "Tech startup founder"},
        {"title": "Hidden Person - Founder", "link": "https://www.google.com/goto?url=CAESZgHEIk", "snippet": "x"},
    ]}
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)))
    hits = run(Serper("k").search(client, "q", "GB", "LinkedIn"))
    assert [h.url for h in hits] == ["https://uk.linkedin.com/in/adamcbradley"]
    assert hits[0].snippet.startswith("London, England, United Kingdom")


def test_washington_dc_city_normalised():
    from app.services.query_service import _canonical

    assert _canonical(Location(city="Washington", state="District of Columbia", country="USA")).city == \
        "Washington DC"
    assert _canonical(Location(city="Washington", state=None, country="USA")).city == "Washington"


# ---------------------------------------------------------------- deployment ----

def test_requirements_and_pyproject_pins_match():
    """Vercel may install from either file; they must never drift apart."""
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    pins = {line.strip() for line in (root / "requirements.txt").read_text().splitlines()
            if line.strip() and not line.startswith("#")}
    assert set(tomllib.loads((root / "pyproject.toml").read_text())["project"]["dependencies"]) == pins


def test_phones_must_belong_to_the_searched_country():
    from app.utils.validators import find_phones

    text = "Call +971 4 289 9788 or ref 0066631212240 or +66 63 121 2240"
    assert [c.value for c in find_phones(text, "s", "AE")] == ["tel:+97142899788"]


def test_name_only_evidence_and_query_location_are_not_trusted():
    from app.services.relevance_service import supported

    c = _cand("c1", "Instagram", "https://instagram.com/reddevilsdc", "Red Devils DC", "Red Devils DC\nRed Devils DC")
    assert not supported("Red Devils DC", c)  # quoting the account's own name proves nothing
    d = _cand("c2", "LinkedIn", "https://linkedin.com/in/rich", "Rich Daniel", "Rich Daniel\nSilver Spring, Maryland")
    assert supported("Silver Spring, Maryland", d)


@pytest.mark.parametrize("name,generic", [
    ("DC", True), ("Digital Marketing Agency", True), ("🏏 Football Washington DC 🏏", True),
    ("Restaurant in Washington DC Mall", True), ("Jane Doe", False), ("Audi Field", False),
    ("Hunter Digital", False), ("Old Tbilisi Georgian Restaurant in Washington DC", False),
])
def test_generic_names_are_not_names(name, generic):
    from app.services.relevance_service import generic_name

    intent = FOOTBALL_DC.model_copy(update={"keywords": ["football", "nfl", "soccer", "digital marketing",
                                                         "restaurant"], "audience": "Football lovers"})
    assert generic_name(name, intent) is generic


def test_success_false_when_no_source_answered():
    service, _, _ = make_service(platforms=[FakePlatform("X", error=RuntimeError("down")),
                                            FakePlatform("LinkedIn", configured=False)])
    response = run(service.discover("Football lovers in Washington DC in USA")).response
    assert response.success is False and response.total_prospects == 0


def test_business_name_counts_only_with_independent_location_and_junk_places_rejected():
    from app.schemas import Verdict
    from app.services.relevance_service import place_like

    assert not place_like("@addressskyview") and not place_like("380008") and place_like("Dubai - UAE")
    service, _, _ = make_service()
    biz = _cand("c1", "Google", "https://ragstoriches.ae", "Rags To Riches Restaurant",
                "Rags To Riches Restaurant\nLevel 1, Studio One Hotel, Washington DC. For reservations call us")
    named_only = _cand("c2", "Instagram", "https://instagram.com/reddevilsdc", "Red Devils DC",
                       "Red Devils DC\nRed Devils DC")

    class Fixed:
        models_key = "f"

        async def structured(self, prompt, user, schema, budget):
            from app.schemas import RelevanceResult
            from app.services.llm_service import LLMResult
            v = [Verdict(candidate_id="c1", interest="yes", location="yes", audience="yes",
                         evidence="Rags To Riches Restaurant", location_text="Washington DC"),
                 Verdict(candidate_id="c2", interest="yes", location="yes", audience="yes",
                         evidence="Red Devils DC", location_text="DC")]
            return LLMResult(RelevanceResult(verdicts=v), "f", False, 0, 0, 0, 0.0, 0.0)

    service._relevance._llm = Fixed()
    result = run(service._relevance.assess([biz, named_only], FOOTBALL_DC, 5))
    assert result["c1"].match == "confirmed"   # name shows the type, the address proves the location
    assert result["c2"].match is None          # everything rests on the name


def test_location_must_name_the_target_city_and_is_trimmed():
    from app.services.relevance_service import names_target_place, place_quote

    assert names_target_place("Washington, District of Columbia", FOOTBALL_DC)
    assert not names_target_place("Silver Spring, Maryland", FOOTBALL_DC)
    assert not names_target_place("USA", FOOTBALL_DC)  # a city was asked for; the country is too broad
    assert place_quote("I'm in Washington DC", FOOTBALL_DC) == "Washington DC"
    assert place_quote("Washington DC-Baltimore Area", FOOTBALL_DC) == "Washington DC-Baltimore Area"


def test_role_headlines_are_not_names():
    assert name_from_title("LinkedIn", "Owner of Shamla Mandi Restaurant - Dubai | LinkedIn") is None
    assert name_from_title("LinkedIn", "Owner Smith - Chef | LinkedIn") == "Owner Smith"


def test_shared_phone_needs_a_shared_name_word():
    phone = Contact("Phone", "tel:+97141234567", "s")
    a = _cand("a", "Facebook", "https://facebook.com/burgerhouse", "Burger House", "", [phone])
    b = _cand("b", "Google", "https://burgerhouse.ae", "Burger House Dubai", "", [phone])
    c = _cand("c", "Instagram", "https://instagram.com/sushibar", "Sushi Bar", "", [phone])  # same mall switchboard
    assert sorted(len(cl.candidates) for cl in consolidate([a, b, c])) == [1, 2]


def test_low_yield_triggers_a_second_wording():
    calls = []

    class Search:
        def is_configured(self):
            return True

        async def search(self, client, query, country, platform, budget):
            calls.append(query)
            if len(calls) == 1:
                return [hit("https://linkedin.com/posts/x", "post", "", "LinkedIn")], "serper"
            return [hit("https://linkedin.com/in/jane", "Jane Doe | LinkedIn", "", "LinkedIn")], "serper"

    hits, provider = run(LinkedIn(Search()).discover(None, FOOTBALL_DC, 10))
    assert len(calls) == 2 and calls[0] != calls[1]
    assert [h.url for h in hits] == ["https://linkedin.com/posts/x", "https://linkedin.com/in/jane"]


def test_contact_page_is_read_when_homepage_has_no_contacts():
    pages = {"/": '<a href="/contact-us">Contact</a>', "/contact-us": '<a href="https://wa.me/12125550143">WA</a> '
             'hello@acme-agency.com'}

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=pages.get(request.url.path, ""), headers={"content-type": "text/html"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    site = hit_to_candidate(hit("https://acme-agency.com/", "Acme Agency | NYC", ""), "US")
    run(ProfileExtractor(client, TTLCache(60)).enrich_from_websites([site], "US", 5))
    assert {c.kind for c in site.contacts} == {"Email", "Phone", "WhatsApp"}  # the wa.me number is published too


def test_possessive_place_is_not_a_location_and_not_extra_evidence():
    from app.schemas import Location
    from app.services.relevance_service import names_target_place, supported

    dubai = FOOTBALL_DC.model_copy(update={"location": Location(city="Dubai", state="Dubai", country="UAE")})
    assert not names_target_place("the best of Dubai's New Middle Eastern places", dubai)
    assert names_target_place("Dubai Marina, Dubai, UAE", dubai)
    c = _cand("c1", "Instagram", "https://instagram.com/dubaieats", "Dubai Eats", "Dubai Eats\nDubai's")
    assert not supported("Dubai's", c)  # "Dubai's" is just the name "Dubai" plus an apostrophe


def test_organisations_are_not_individual_prospects():
    from app.services.relevance_service import looks_like_organisation

    assert looks_like_organisation("Ahmedabad Cricket Morning Group")
    assert looks_like_organisation("Sports at The cricket Update")
    assert not looks_like_organisation("Ashish Jha")


def test_business_queries_also_search_linkedin_company_pages():
    business = FOOTBALL_DC.model_copy(update={"target_type": "business"})
    assert LinkedIn(search=None).build_query(business).startswith("(site:linkedin.com/company OR site:linkedin.com/in)")
    assert LinkedIn(search=None).build_query(FOOTBALL_DC).startswith("site:linkedin.com/in ")


def test_db_path_is_always_writable_on_vercel(monkeypatch):
    import tempfile

    from app.config import db_path

    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("DB_PATH", "data/prospects.db")  # copied from a local .env
    assert db_path().startswith(tempfile.gettempdir())
    monkeypatch.delenv("VERCEL")
    assert db_path() == "data/prospects.db"
