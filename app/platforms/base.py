"""Common contract for every discovery source, plus the shared base for `site:` search adapters."""

import re
import time
from abc import ABC, abstractmethod
from typing import ClassVar

import httpx

from app.schemas import Method, PlatformName, QueryIntent, SearchHit
from app.utils.normalizers import country_code, profile_of

MIN_PROFILES = 3            # fewer usable profiles than this -> try a second wording
SECOND_SEARCH_MIN_TIME = 3  # seconds that must be left to bother

# Words that describe *liking* something; they don't help a search engine find people.
_AUDIENCE_FILLER = {"lovers", "lover", "fans", "fan", "enthusiasts", "enthusiast", "people", "persons", "in", "of",
                    "the", "and", "who", "like", "love"}


def location_term(intent: QueryIntent) -> str | None:
    loc = intent.location
    return loc.city or loc.state or loc.country


def role_terms(intent: QueryIntent) -> str:
    """Audience words that aren't the interest or filler: 'Digital marketing agency owners' -> 'agency owners'."""
    interest_words = {w.lower() for w in intent.interest.split()}
    words = [w for w in intent.audience.split()
             if w.lower() not in interest_words and w.lower().rstrip("s") not in interest_words
             and w.lower() not in _AUDIENCE_FILLER]
    return " ".join(words)


_SITE_OPERATOR = re.compile(r"\b(?:site|inurl|intitle|filetype):\S*", re.IGNORECASE)


def llm_phrase(intent: QueryIntent, platform: str) -> str:
    """The LLM's search phrase for this platform, cleaned: operators removed (code owns `site:`), length capped.
    Empty when the LLM gave nothing usable, so the caller falls back to the deterministic template."""
    phrase = getattr(intent.search_queries, platform.lower(), "") or ""
    phrase = " ".join(_SITE_OPERATOR.sub(" ", phrase).split())[:150]
    return phrase if phrase.count('"') % 2 == 0 else phrase.replace('"', "")


def quoted(term: str | None) -> str:
    return f'"{term}"' if term else ""


class Platform(ABC):
    """One discovery source. Adding a platform = one subclass + one entry in `build_platforms()`."""

    name: ClassVar[PlatformName]
    method: ClassVar[Method]

    @abstractmethod
    def is_configured(self) -> bool: ...

    @abstractmethod
    def build_query(self, intent: QueryIntent) -> str:
        """Deterministic search string from the parsed intent (no LLM, nothing hardcoded per test query)."""

    @abstractmethod
    async def discover(self, client: httpx.AsyncClient, intent: QueryIntent,
                       budget: float) -> tuple[list[SearchHit], str]:
        """Run the search; return (hits, provider name). Raise on failure; the caller isolates it."""


class SiteSearchPlatform(Platform):
    """A platform reached through web search with `site:` (no third-party people-search API exists)."""

    method = "search_api"
    sites: ClassVar[tuple[str, ...]]

    def __init__(self, search) -> None:  # search: FailoverSearch (typed loosely to avoid an import cycle)
        self._search = search

    def is_configured(self) -> bool:
        return self._search.is_configured()

    def sites_for(self, intent: QueryIntent | None) -> tuple[str, ...]:
        return self.sites

    def site_expr(self, intent: QueryIntent | None = None) -> str:
        sites = self.sites_for(intent)
        if len(sites) == 1:
            return f"site:{sites[0]}"
        return "(" + " OR ".join(f"site:{s}" for s in sites) + ")"

    def build_query(self, intent: QueryIntent) -> str:
        """`site:` operator from code + the LLM's platform-specific phrase (template if the LLM gave none)."""
        phrase = llm_phrase(intent, self.name) or self.template(intent)
        return f"{self.site_expr(intent)} {phrase}".strip()

    def template(self, intent: QueryIntent) -> str:
        parts = [quoted(intent.interest), role_terms(intent), quoted(location_term(intent))]
        return " ".join(p for p in parts if p)

    def usable(self, hits: list[SearchHit]) -> int:
        """How many hits are profiles on this platform (posts, news and articles don't count)."""
        return sum(1 for h in hits if (p := profile_of(h.url)) and p[0] == self.name)

    async def discover(self, client, intent, budget):
        """One search; if it yields fewer than MIN_PROFILES usable profiles, a second, differently worded search
        (the template from the parsed intent instead of the LLM's phrase), when time allows. Results merged."""
        started = time.monotonic()
        country = country_code(intent.location.country)
        first = self.build_query(intent)
        hits, provider = await self._search.search(client, first, country, self.name, budget)
        second = f"{self.site_expr(intent)} {self.template(intent)}".strip()
        remaining = budget - (time.monotonic() - started)
        if self.usable(hits) < MIN_PROFILES and second != first and remaining > SECOND_SEARCH_MIN_TIME:
            try:
                more, provider2 = await self._search.search(client, second, country, self.name, remaining)
            except Exception:  # noqa: BLE001 - the first search already succeeded; keep its results
                return hits, provider
            seen = {h.url for h in hits}
            hits = hits + [h for h in more if h.url not in seen]
            provider = provider if provider == provider2 else f"{provider}+{provider2}"
        return hits, provider
