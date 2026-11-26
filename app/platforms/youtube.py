"""YouTube via the official YouTube Data API v3: the one platform with a real keyword search for profiles.

search.list (type=channel) has its own bucket of 100 calls/day, so it's capped locally and cached upstream.
Channels expose no link or email fields; contacts can only come from the description text.
"""

from dataclasses import dataclass

import httpx

from app.errors import SourceError, raise_for_status
from app.platforms.base import Platform, llm_phrase, location_term, role_terms
from app.schemas import QueryIntent, SearchHit
from app.services.resilience import Guard
from app.utils.normalizers import country_code

API = "https://www.googleapis.com/youtube/v3"
DAILY_SEARCH_KEY = "youtube_search"


def _raise(response: httpx.Response) -> None:
    if response.status_code == 403 and "quota" in response.text.lower():  # reason: quotaExceeded
        raise SourceError("YouTube daily quota exceeded; resets at midnight Pacific time", 403, quota_exhausted=True)
    raise_for_status("YouTube", response)


@dataclass
class YouTube(Platform):
    api_key: str
    guard: Guard
    max_results: int = 10
    name = "YouTube"
    method = "official_api"

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def build_query(self, intent: QueryIntent) -> str:
        return llm_phrase(intent, self.name).replace('"', "") or \
            " ".join(p for p in (intent.interest, role_terms(intent), location_term(intent)) if p)

    async def discover(self, client, intent, budget):
        query = self.build_query(intent)
        region = country_code(intent.location.country)

        headers = {"X-Goog-Api-Key": self.api_key}

        async def call(_remaining: float) -> list[SearchHit]:
            # The key goes in a header, never the URL: HTTP tracing records full URLs.
            params = {"part": "snippet", "type": "channel", "q": query, "maxResults": self.max_results}
            if region:
                params["regionCode"] = region
            found = await client.get(f"{API}/search", params=params, headers=headers)
            _raise(found)
            ids = [item["id"]["channelId"] for item in found.json().get("items", [])]
            if not ids:
                return []
            details = await client.get(f"{API}/channels", headers=headers,
                                       params={"part": "snippet,brandingSettings", "id": ",".join(ids)})
            _raise(details)
            return [self._hit(item, query) for item in details.json().get("items", [])]

        hits = await self.guard.call("youtube", call, budget, daily_key=DAILY_SEARCH_KEY)
        return hits, "youtube_data_api"

    def _hit(self, item: dict, query: str) -> SearchHit:
        snippet = item.get("snippet", {})
        custom = snippet.get("customUrl") or ""
        url = f"https://www.youtube.com/{custom}" if custom.startswith("@") else \
            f"https://www.youtube.com/channel/{item['id']}"
        country = snippet.get("country") or item.get("brandingSettings", {}).get("channel", {}).get("country")
        text = snippet.get("description", "")
        if country:
            text += f"\nChannel country: {country}"
        return SearchHit(url=url, title=snippet.get("title", ""), snippet=text, platform="YouTube",
                         provider="youtube_data_api", query=query)
