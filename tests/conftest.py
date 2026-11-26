"""Fakes injected through constructors (dependency injection), so tests run offline and deterministic."""

import os

os.environ.setdefault("DB_PATH", ":memory:")
os.environ["API_KEY"] = ""

from typing import ClassVar  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from app.errors import LLMUnavailable  # noqa: E402
from app.platforms.base import Platform  # noqa: E402
from app.schemas import (  # noqa: E402
    Location,
    PlatformQueries,
    QueryIntent,
    RelevanceResult,
    SearchHit,
    Verdict,
)
from app.services.cache import Caches  # noqa: E402
from app.services.llm_service import LLMResult  # noqa: E402
from app.services.profile_extractor import ProfileExtractor  # noqa: E402
from app.services.prospect_service import ProspectService  # noqa: E402
from app.services.query_service import QueryService  # noqa: E402
from app.services.relevance_service import RelevanceService  # noqa: E402
from app.services.search_service import SearchService  # noqa: E402
from app.services.storage import Storage  # noqa: E402

FOOTBALL_DC = QueryIntent(
    interest="Football", audience="Football lovers",
    location=Location(city="Washington DC", state="District of Columbia", country="USA"),
    target_type="individual", keywords=["football", "nfl", "soccer"],
    search_queries=PlatformQueries(google="football fan Washington DC", linkedin="football Washington DC",
                                   x="football fan Washington DC", youtube="football Washington DC",
                                   instagram="football fan Washington DC", facebook="football fan Washington DC",
                                   threads="football fan Washington DC"),
    is_valid=True, reason=None,
)


def hit(url: str, title: str, snippet: str = "", platform: str = "Google") -> SearchHit:
    return SearchHit(url=url, title=title, snippet=snippet, platform=platform, provider="fake", query="q")


class FakeLLM:
    """Intent: returns `intent`. Relevance: 'yes' where the candidate text contains the word, quoting it."""

    models_key = "fake|fake"

    def __init__(self, intent: QueryIntent = FOOTBALL_DC, fail: set[str] | None = None,
                 extra_verdict_ids: list[str] | None = None, bad_quote: bool = False) -> None:
        self.intent = intent
        self.fail = fail or set()
        self.extra = extra_verdict_ids or []
        self.bad_quote = bad_quote
        self.calls: list[str] = []

    async def structured(self, prompt, user, schema, budget):
        self.calls.append(prompt.task)
        if prompt.task in self.fail:
            raise LLMUnavailable("fake outage")
        if schema is QueryIntent:
            value = self.intent
        else:
            verdicts = []
            for block in user.split('<candidate id="')[1:]:
                cid, text = block.split('">', 1)
                text = text.split("</candidate>")[0]
                low = text.lower()
                interest = "yes" if "football" in low else "unknown"
                location = "yes" if "washington" in low else "unknown"
                quote = "invented quote" if self.bad_quote else ("football" if interest == "yes" else "")
                where = "invented place" if self.bad_quote else ("Washington DC" if location == "yes" else "")
                verdicts.append(Verdict(candidate_id=cid, interest=interest, location=location, audience="unknown",
                                        evidence=quote, location_text=where))
            verdicts += [Verdict(candidate_id=i, interest="yes", location="yes", audience="yes", evidence="x",
                                 location_text="x") for i in self.extra]
            value = RelevanceResult(verdicts=verdicts)
        return LLMResult(value=value, model="fake", fallback_used=False, input_tokens=10, output_tokens=5,
                         cached_tokens=0, cost_usd=0.0001, latency_ms=1.0)


class FakePlatform(Platform):
    method = "search_api"
    name: ClassVar[str] = "LinkedIn"

    def __init__(self, name: str, hits: list[SearchHit] | None = None, error: Exception | None = None,
                 configured: bool = True) -> None:
        self.name = name  # type: ignore[misc]
        self._hits = hits or []
        self._error = error
        self._configured = configured
        self.calls = 0

    def is_configured(self) -> bool:
        return self._configured

    def build_query(self, intent):
        return f"{self.name} {intent.interest}"

    async def discover(self, client, intent, budget):
        self.calls += 1
        if self._error:
            raise self._error
        return list(self._hits), "fake"


DC_HITS = {
    "LinkedIn": [hit("https://uk.linkedin.com/in/jane-doe-123/", "Jane Doe - Sports Analyst | LinkedIn",
                     "Washington DC. Football analytics. jane@janedoe-sports.com", "LinkedIn")],
    "Instagram": [hit("https://www.instagram.com/janedoe_dc/?hl=en", "Jane Doe (@janedoe_dc) • Instagram photos "
                      "and videos", "Football every Sunday. Contact jane@janedoe-sports.com", "Instagram"),
                  hit("https://www.instagram.com/p/abc123/", "A post", "football", "Instagram")],
    "X": [hit("https://twitter.com/johnsmithdc", "John Smith (@johnsmithdc) / X", "DC football fan 🏈", "X"),
          hit("https://x.com/othername", "Other Name (@othername) / X", "Cooking recipes from Paris", "X")],
    "Facebook": [hit("https://www.facebook.com/groups/dcfootball", "DC Football group | Facebook", "", "Facebook")],
}


def make_service(llm: FakeLLM | None = None, platforms: list[Platform] | None = None,
                 store: Storage | None = None) -> tuple[ProspectService, FakeLLM, list[Platform]]:
    llm = llm or FakeLLM()
    if platforms is None:
        platforms = [FakePlatform(name, hits) for name, hits in DC_HITS.items()]
    caches = Caches()
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    service = ProspectService(
        queries=QueryService(llm, caches.intent),
        search=SearchService(platforms, client, caches.search, platform_timeout=5),
        extractor=ProfileExtractor(client, caches.page),
        relevance=RelevanceService(llm),
        store=store, flight=caches.flight, request_deadline=20,
    )
    return service, llm, platforms


@pytest.fixture
def store():
    s = Storage(":memory:")
    yield s
    s.close()
