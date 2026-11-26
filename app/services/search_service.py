"""Brief §7 step 6: discovery across platforms.

Two parts: the web search providers used for `site:` discovery (Serper = Google results, Brave as fallback),
and SearchService, which fans out over every platform adapter concurrently with per-platform isolation.
Only search results are read (title, snippet, URL). Profile pages on social platforms are never fetched.
"""

import asyncio
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

import httpx
import logfire

from app.errors import LocalSkip, SourceError, raise_for_status
from app.schemas import PlatformName, PlatformStatus, QueryIntent, SearchHit
from app.services.cache import TTLCache
from app.services.resilience import Deadline, Guard, classify
from app.utils.normalizers import country_code

if TYPE_CHECKING:
    from app.platforms.base import Platform

_TAGS = re.compile(r"<[^>]+>")
MIN_USEFUL_HITS = 4  # fewer usable results than this from one provider -> also ask the next one
_GOOGLE_REDIRECT = re.compile(r"^https?://(?:www\.)?google\.[a-z.]+/(?:goto|url)\?", re.IGNORECASE)


class SearchProvider(Protocol):
    name: str

    def is_configured(self) -> bool: ...

    async def search(self, client: httpx.AsyncClient, query: str, country: str | None,
                     platform: PlatformName) -> list[SearchHit]: ...


@dataclass
class Serper:
    """POST https://google.serper.dev/search. Google ignores `num` since Sept 2025, so expect ~10 results."""

    api_key: str
    name: str = "serper"

    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def search(self, client, query, country, platform):
        body = {"q": query}
        if country:
            body["gl"] = country.lower()
        response = await client.post("https://google.serper.dev/search", json=body,
                                     headers={"X-API-KEY": self.api_key})
        # Serper answers "Not enough credits" with a 4xx; treat it as quota so we fail over instead of retrying.
        if response.status_code in (400, 402, 403) and "credit" in response.text.lower():
            raise_for_status("Serper", response, quota_statuses=frozenset({response.status_code}))
        raise_for_status("Serper", response, quota_statuses=frozenset({402}))
        hits, opaque = [], 0
        for item in response.json().get("organic", []):
            link = item.get("link") or ""
            # Quirk: many results come back as opaque https://www.google.com/goto?url=... links. Resolving them is
            # unreliable (sometimes an interstitial page, once a DIFFERENT person's profile), so they are dropped:
            # a link we can't verify is never shown.
            if not link or _GOOGLE_REDIRECT.match(link):
                opaque += 1
                continue
            # `subtitle` often holds "London, England, United Kingdom · Founder · Acme": real location evidence.
            snippet = " · ".join(p for p in (item.get("subtitle", ""), item.get("snippet", "")) if p)
            hits.append(SearchHit(url=link, title=item.get("title", ""), snippet=snippet, platform=platform,
                                  provider=self.name, query=query))
        if opaque:
            logfire.info("serper returned {opaque} unverifiable redirect links for {platform}; dropped",
                         opaque=opaque, platform=platform)
        return hits


@dataclass
class Brave:
    """GET https://api.search.brave.com/res/v1/web/search. Independent index; up to 20 results per call."""

    api_key: str
    name: str = "brave"

    def is_configured(self) -> bool:
        return bool(self.api_key)

    async def search(self, client, query, country, platform):
        params = {"q": query, "count": 20}
        if country:
            params["country"] = country.upper()
        response = await client.get("https://api.search.brave.com/res/v1/web/search", params=params,
                                    headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"})
        raise_for_status("Brave", response, quota_statuses=frozenset({402}))
        results = response.json().get("web", {}).get("results", [])
        return [
            SearchHit(url=item["url"], title=_TAGS.sub("", item.get("title", "")),
                      snippet=_TAGS.sub("", item.get("description", "")),
                      platform=platform, provider=self.name, query=query)
            for item in results
            if item.get("url")
        ]


class FailoverSearch:
    """Tries each configured provider in order, each behind the shared Guard (breaker, limiter, retries).

    Moves to the next provider on failure, and also when a provider returns fewer than MIN_USEFUL_HITS usable
    results (results are merged, duplicates removed). Raises only when every provider failed, with every reason.
    """

    def __init__(self, providers: list[SearchProvider], guard: Guard) -> None:
        self._providers = providers
        self._guard = guard

    def is_configured(self) -> bool:
        return any(p.is_configured() for p in self._providers)

    async def search(self, client: httpx.AsyncClient, query: str, country: str | None, platform: PlatformName,
                     budget: float) -> tuple[list[SearchHit], str]:
        errors: list[tuple[str, BaseException]] = []
        collected: list[SearchHit] = []
        used: list[str] = []
        configured = [p for p in self._providers if p.is_configured()]
        deadline = time.monotonic() + budget
        for index, provider in enumerate(configured):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                errors.append((provider.name, TimeoutError()))
                break
            # Leave the fallback some time: a provider that isn't last gets at most 2/3 of what's left.
            share = remaining if index == len(configured) - 1 else remaining * 2 / 3
            try:
                hits = await self._guard.call(
                    provider.name, lambda _r, p=provider: p.search(client, query, country, platform), share
                )
            except Exception as exc:
                errors.append((provider.name, exc))
                continue
            collected.extend(h for h in hits if h.url not in {c.url for c in collected})
            used.append(provider.name)
            # Low yield (e.g. Serper returning mostly unverifiable redirect links): try the next provider too.
            if len(collected) >= MIN_USEFUL_HITS:
                break
        if collected or used:
            return collected, "+".join(used)
        if not errors:
            raise LocalSkip("not_configured", "No search provider configured (set SERPER_API_KEY or BRAVE_API_KEY)")
        if len(errors) == 1:
            raise errors[0][1]
        reasons = "; ".join(f"{name}: {classify(exc)[1]}" for name, exc in errors)
        last = errors[-1][1]
        raise SourceError(f"All search providers failed ({reasons})", getattr(last, "status_code", None),
                          quota_exhausted=all(classify(e)[0] == "quota_exhausted" for _, e in errors))


# ------------------------------------------------------- multi-platform discovery (brief §7 step 6) ----

OnPlatformDone = Callable[[PlatformName, list[SearchHit], PlatformStatus], None]


class SearchService:
    """Runs every platform concurrently; each one is a bulkhead: whatever it raises becomes its own status."""

    def __init__(self, platforms: Sequence["Platform"], client: httpx.AsyncClient, cache: TTLCache,
                 platform_timeout: float) -> None:
        self._platforms = platforms
        self._client = client
        self._cache = cache
        self._timeout = platform_timeout

    async def discover_all(self, intent: QueryIntent, deadline: Deadline,
                           on_done: OnPlatformDone | None = None,
                           skip: dict[PlatformName, list[SearchHit]] | None = None,
                           ) -> tuple[list[SearchHit], list[PlatformStatus]]:
        """`skip` = platforms already answered in an earlier, interrupted run of the same request (reused as is)."""
        skip = skip or {}
        to_run = [p for p in self._platforms if p.name not in skip]
        with logfire.span("discover {count} platforms", count=len(to_run)) as span:
            fresh = await asyncio.gather(*(self._discover_one(p, intent, deadline, on_done) for p in to_run))
            hits = [h for found in skip.values() for h in found]
            statuses = [PlatformStatus(platform=p.name, method=p.method, status="ok", found=len(skip[p.name]),
                                       provider="replayed") for p in self._platforms if p.name in skip]
            for found, status in fresh:
                hits.extend(found)
                statuses.append(status)
            order = {p.name: i for i, p in enumerate(self._platforms)}
            statuses.sort(key=lambda s: order[s.platform])
            span.set_attributes({"platforms_ok": [s.platform for s in statuses if s.status == "ok"],
                                 "platforms_failed": [s.platform for s in statuses if s.status != "ok"],
                                 "hits": len(hits)})
        return hits, statuses

    async def _discover_one(self, platform: "Platform", intent: QueryIntent, deadline: Deadline,
                            on_done: OnPlatformDone | None) -> tuple[list[SearchHit], PlatformStatus]:
        """Never raises. Order: configured? -> cache -> provider call (guard: breaker, limiter, retries)."""
        hits: list[SearchHit] = []
        with logfire.span("discover {platform}", platform=platform.name, method=platform.method) as span:
            try:
                if not platform.is_configured():
                    status = PlatformStatus(platform=platform.name, method=platform.method, status="not_configured",
                                            error_type="not_configured", error=f"{platform.name} source not configured")
                else:
                    query = platform.build_query(intent)
                    span.set_attribute("query", query)
                    key = (platform.name, query, country_code(intent.location.country))
                    cached = self._cache.get(key)
                    if cached is not None:
                        hits, provider = cached
                        span.set_attribute("cache_hit", True)
                    else:
                        hits, provider = await platform.discover(self._client, intent,
                                                                 deadline.budget(self._timeout))
                        self._cache.set(key, (hits, provider))
                    status = PlatformStatus(platform=platform.name, method=platform.method, status="ok",
                                            found=len(hits), provider=provider)
            except Exception as exc:  # bulkhead: one platform's failure never reaches the others
                error_type, message, _ = classify(exc)
                if error_type == "unexpected":
                    span.record_exception(exc)
                status = PlatformStatus(platform=platform.name, method=platform.method, status="failed",
                                        error_type=error_type, error=message)
            span.set_attributes({"status": status.status, "found": status.found, "provider": status.provider,
                                 "error_type": status.error_type})
            if status.status == "failed":
                span.set_level("warning")
            if on_done:
                on_done(platform.name, hits, status)  # write-as-you-go; the callback must not raise
        return hits, status
