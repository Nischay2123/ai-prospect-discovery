"""Brief §7 steps 7-8: extract public profile information and validate URLs and contact details.

- Search hits become candidate profiles only when the URL is a real profile (posts, groups, articles and
  directories are dropped). A hit is credited to the platform its URL belongs to.
- Names come only from page titles. Contacts come only from text that was really published.
- A prospect's OWN website (found by the Google search) is read once, politely (robots.txt, short timeout), with
  BeautifulSoup for mailto:/tel:/wa.me links and the social profiles it links to. Social platforms are never fetched.
"""

import asyncio
import hashlib
import ipaddress
import re
import socket
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import logfire
from bs4 import BeautifulSoup

from app.schemas import CandidateProfile, Contact, SearchHit
from app.services.cache import TTLCache
from app.utils.normalizers import (
    handle_belongs,
    is_prospect_website,
    name_from_title,
    normalize_url,
    profile_of,
    site_label,
)
from app.utils.validators import find_contacts, find_phones

USER_AGENT = "ai-prospect-discovery/1.0"
MAX_REDIRECTS = 3
MAX_SITES_PER_REQUEST = 5
MAX_PAGE_BYTES = 1_000_000
# Pages that list many businesses or people are sources, not prospects: "Top 43 London Startups", "Find a
# Co-Founder", "10 best restaurants", "... directory".
_LISTING_TITLE = re.compile(r"\b(?:top|best)\s+\d+|\d+\s+(?:best|top|great)\b|\bbest\s+\w+\s+in\b|\bdirectory\b|"
                            r"\bfind\s+an?\b|\blist\s+of\b|\bnear\s+me\b|\breviews?\b|\branking|"
                            # Titles that start with an instruction are marketing pages, not someone's profile:
                            # "Find or become a startup Co-Founder", "Discover…", "Hire…"
                            r"^\W*(?:find|discover|meet|join|become|hire|get|explore|browse|search|connect)\b",
                            re.IGNORECASE)
_URL_IN_TEXT = re.compile(r"https?://[^\s\"'<>)]+|(?:www\.)?(?:linkedin|instagram|facebook|x|twitter|threads|youtube)"
                          r"\.(?:com|net)/[^\s\"'<>)]+", re.IGNORECASE)


def _contact_page(site_url: str, hrefs: list[str]) -> str | None:
    """First same-site link that looks like a contact page."""
    host = urlsplit(site_url).hostname
    for href in hrefs:
        url = urljoin(site_url + "/", href)
        parts = urlsplit(url)
        if parts.scheme in ("http", "https") and parts.hostname and parts.hostname.removeprefix("www.") == \
                (host or "").removeprefix("www.") and re.search(r"contact|reach-us|get-in-touch", parts.path, re.I):
            return url.split("#")[0]
    return None


async def is_public_host(url: str) -> bool:
    """True only when every address the host resolves to is a public unicast IP (no loopback, private, link-local,
    multicast or reserved ranges, so cloud metadata endpoints and internal services are unreachable)."""
    host = urlsplit(url).hostname
    if not host or urlsplit(url).scheme not in ("http", "https"):
        return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    addresses = {ipaddress.ip_address(info[4][0]) for info in infos}
    return bool(addresses) and all(a.is_global for a in addresses)


def candidate_id(url: str) -> str:
    return "c" + hashlib.sha1(url.encode(), usedforsecurity=False).hexdigest()[:8]


def hit_to_candidate(hit: SearchHit, region: str | None) -> CandidateProfile | None:
    """Hit -> profile, or None when the URL isn't a profile or the title holds no usable name."""
    url = normalize_url(hit.url)
    if url is None or _LISTING_TITLE.search(hit.title):  # lists and marketing pages, on any platform
        return None
    profile = profile_of(url)
    if profile:
        platform, handle = profile
    elif hit.platform == "Google" and is_prospect_website(url):
        platform, handle = "Google", site_label(url)
    else:
        return None
    name = name_from_title(platform, hit.title)
    if name is None:
        return None
    text = f"{hit.title}\n{hit.snippet}".strip()
    linked = sorted({u for u in (normalize_url(m if "://" in m else f"https://{m}") for m in _URL_IN_TEXT.findall(text))
                     if u and u != url and profile_of(u)})
    return CandidateProfile(
        id=candidate_id(url), platform=platform, url=url, name=name, handle=handle, text=text, source_url=hit.url,
        contacts=find_contacts(hit.snippet, hit.url, region), linked_urls=linked,
    )


class ProfileExtractor:
    def __init__(self, client: httpx.AsyncClient, page_cache: TTLCache, check_deliverability: bool = False) -> None:
        self._client = client
        self._pages = page_cache
        self._robots: dict[str, RobotFileParser | None] = {}
        self._check_deliverability = check_deliverability

    def extract(self, hits: list[SearchHit], region: str | None) -> list[CandidateProfile]:
        """Step 7. Identical URLs from different searches are merged here (same page, same text)."""
        by_url: dict[str, CandidateProfile] = {}
        for hit in hits:
            candidate = hit_to_candidate(hit, region)
            if candidate is None:
                continue
            existing = by_url.get(candidate.url)
            if existing is None:
                by_url[candidate.url] = candidate
            elif candidate.text not in existing.text:
                existing.text += "\n" + candidate.text
                existing.contacts += [c for c in candidate.contacts if c not in existing.contacts]
                existing.linked_urls = sorted(set(existing.linked_urls) | set(candidate.linked_urls))
        return list(by_url.values())

    async def enrich_from_websites(self, candidates: list[CandidateProfile], region: str | None,
                                   budget: float) -> list[CandidateProfile]:
        """Step 8 (contacts): read each prospect's own website once; returns NEW candidates for the social
        profiles that site links to (published by the prospect, so a verified link and a strong merge signal)."""
        sites = [c for c in candidates if c.platform == "Google"][:MAX_SITES_PER_REQUEST]
        if not sites or budget <= 0.5:
            return []
        with logfire.span("read {count} prospect websites", count=len(sites)) as span:
            # Each site gets the budget on its own, so one slow site can't throw away the others' results.
            results = await asyncio.gather(*(asyncio.wait_for(self._read_site(c, region), budget) for c in sites),
                                           return_exceptions=True)
            span.set_attribute("timed_out", sum(isinstance(r, TimeoutError) for r in results))
            linked: list[CandidateProfile] = []
            for result in results:
                if isinstance(result, list):
                    linked.extend(result)
            span.set_attributes({"linked_profiles": len(linked),
                                 "contacts_found": sum(len(c.contacts) for c in sites)})
        return linked

    def _page_contacts(self, html: str, page_url: str, region: str | None) -> tuple[list[Contact], list[str]]:
        soup = BeautifulSoup(html, "html.parser")
        hrefs = [a.get("href", "") for a in soup.find_all("a")]
        text = soup.get_text(" ", strip=True)[:20000]
        contacts: list[Contact] = find_contacts(text + " " + " ".join(hrefs), page_url, region,
                                                self._check_deliverability)
        for href in hrefs:
            if href.lower().startswith("tel:"):
                contacts += find_phones(href[4:], page_url, region)
        return contacts, hrefs

    async def _read_site(self, site: CandidateProfile, region: str | None) -> list[CandidateProfile]:
        html = await self._fetch(site.url)
        if not html:
            return []
        contacts, hrefs = self._page_contacts(html, site.url, region)
        if not contacts:
            # Homepages often hide details behind a "Contact" page: read that one page too (same site only).
            contact_url = _contact_page(site.url, hrefs)
            if contact_url and (more := await self._fetch(contact_url)):
                contacts, extra = self._page_contacts(more, contact_url, region)
                hrefs += extra
        for contact in contacts:
            if contact not in site.contacts:
                site.contacts.append(contact)

        linked: list[CandidateProfile] = []
        taken: set[str] = set()
        for href in hrefs:
            url = normalize_url(urljoin(site.url, href))
            profile = profile_of(url) if url else None
            if not url or not profile or url in site.linked_urls:
                continue
            platform, handle = profile
            # Only the site's OWN accounts: a link to someone else (a partner, a player, a listed business) is not
            # this prospect. One per platform, so a directory page can't pull in dozens of profiles.
            if platform in taken or not handle_belongs(handle, site.handle, site.name):
                continue
            taken.add(platform)
            site.linked_urls.append(url)
            linked.append(CandidateProfile(
                id=candidate_id(url), platform=platform, url=url, name=site.name, handle=handle,
                text=f"Linked from {site.name}'s website {site.url}", source_url=site.url, linked_urls=[site.url],
            ))
        return linked

    async def _fetch(self, url: str) -> str | None:
        cached = self._pages.get(url)
        if cached is not None:
            return cached
        if not await self._allowed(url):
            return None
        status, html = await self._safe_get(url, accept_html_only=True)
        if status != 200 or html is None:
            return None
        self._pages.set(url, html)
        return html

    async def _safe_get(self, url: str, accept_html_only: bool = False, read_timeout: float = 5.0
                        ) -> tuple[int | None, str | None]:
        """GET with SSRF protection: redirects followed by hand (max 3), every hop must resolve to public IPs
        only, and the body is streamed with a size cap so a huge page can't exhaust memory.

        ponytail: DNS is checked before connecting, so a host that re-resolves between check and connect
        (DNS rebinding) isn't covered; pin the resolved IP in a custom transport if this ever fetches from a
        trusted network.
        """
        for _ in range(MAX_REDIRECTS + 1):
            if not await is_public_host(url):
                return None, None
            try:
                async with self._client.stream("GET", url, headers={"User-Agent": USER_AGENT}, timeout=read_timeout,
                                               follow_redirects=False) as response:
                    if response.is_redirect and response.headers.get("location"):
                        url = urljoin(url, response.headers["location"])
                        continue
                    if accept_html_only and "html" not in response.headers.get("content-type", ""):
                        return response.status_code, None
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body += chunk
                        if len(body) >= MAX_PAGE_BYTES:
                            break
                    return response.status_code, body[:MAX_PAGE_BYTES].decode(response.encoding or "utf-8",
                                                                                 errors="replace")
            except httpx.HTTPError:
                return None, None
        return None, None

    async def _allowed(self, url: str) -> bool:
        """robots.txt check. No robots.txt (404) means allowed; an unreachable one means don't fetch."""
        origin = "{0.scheme}://{0.netloc}".format(urlsplit(url))
        if origin not in self._robots:
            parser: RobotFileParser | None = RobotFileParser()
            status, text = await self._safe_get(f"{origin}/robots.txt", read_timeout=3.0)
            if status == 200:
                parser.parse((text or "").splitlines())
            elif status is None or status in (401, 403):
                parser = None  # unreachable, blocked by our SSRF check, or forbidden: don't fetch the site
            else:
                parser.parse([])
            self._robots[origin] = parser
        parser = self._robots[origin]
        return parser is not None and parser.can_fetch(USER_AGENT, url)
