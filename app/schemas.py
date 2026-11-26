"""All models: API request/response, LLM input/output, and internal pipeline types."""

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PlatformName = Literal[
    "Google", "LinkedIn", "X", "YouTube", "Instagram", "WhatsApp", "Facebook", "Email", "Threads", "Phone"
]
ContactKind = Literal["Email", "Phone", "WhatsApp"]
Method = Literal["search_api", "official_api", "extraction"]

MIN_QUERY_LENGTH = 3
MAX_QUERY_LENGTH = 300
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


# ---------------------------------------------------------------- API ----

class DiscoveryRequest(BaseModel):
    query: str = Field(
        description="Natural-language prospect search, e.g. audience + location.",
        examples=["Football lovers in Washington DC in USA"],
    )

    @field_validator("query")
    @classmethod
    def clean_query(cls, value: str) -> str:
        if _CONTROL_CHARS.search(value):
            raise ValueError("Query must not contain control characters")
        value = " ".join(value.split())
        if len(value) < MIN_QUERY_LENGTH:
            raise ValueError(f"Query must be at least {MIN_QUERY_LENGTH} characters")
        if len(value) > MAX_QUERY_LENGTH:
            raise ValueError(f"Query must not exceed {MAX_QUERY_LENGTH} characters (got {len(value)})")
        return value


class SocialLink(BaseModel):
    platform: PlatformName
    link: str


class Evidence(BaseModel):
    platform: PlatformName
    link: str
    source_url: str
    text: str


class Prospect(BaseModel):
    name: str = Field(min_length=1)
    location: str | None = None
    social_link: list[SocialLink] = Field(default_factory=list)
    match: Literal["confirmed", "partial"] = Field(
        description="confirmed: public evidence for interest and location; partial: evidence for one of them"
    )
    evidence: list[Evidence] | None = Field(default=None, description="Only with ?debug=true")


class PlatformStatus(BaseModel):
    platform: PlatformName
    method: Method
    status: Literal["ok", "failed", "not_configured"]
    found: int = 0
    provider: str | None = None
    error_type: str | None = None
    error: str | None = None


# ---------------------------------------------------- LLM input/output ----
# Every field is required (nullable where needed) and extra keys are forbidden, so the JSON schema
# works as a strict structured-output schema on every provider. Range checks live in code, not in the schema.

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Location(_Strict):
    city: str | None
    state: str | None
    country: str | None

    def label(self) -> str | None:
        parts = [p for p in (self.city, self.state, self.country) if p]
        # "Washington DC, District of Columbia, USA" reads oddly; the brief shows "Washington DC, USA".
        if self.city and self.country:
            parts = [self.city, self.country]
        return ", ".join(parts) or None


class PlatformQueries(_Strict):
    """Search phrases per platform (brief §7 step 5). Keywords only: code adds the `site:` operator itself."""

    google: str
    linkedin: str
    x: str
    youtube: str
    instagram: str
    facebook: str
    threads: str


class QueryIntent(_Strict):
    interest: str
    audience: str
    location: Location
    target_type: Literal["individual", "business"]
    keywords: list[str] = Field(description="Interest synonyms for search and filtering, e.g. ['soccer', 'football']")
    search_queries: PlatformQueries
    is_valid: bool = Field(description="False when the text is not a prospect search")
    reason: str | None = Field(description="Why the query is invalid, else null")


class Verdict(_Strict):
    candidate_id: str
    interest: Literal["yes", "no", "unknown"]
    location: Literal["yes", "no", "unknown"]
    audience: Literal["yes", "no", "unknown"]
    evidence: str = Field(description="Exact quote from the candidate's text showing the interest/audience, else empty")
    location_text: str = Field(description="Exact quote from the candidate's text saying where they are, else empty")


class RelevanceResult(_Strict):
    verdicts: list[Verdict]


class PublicProspect(BaseModel):
    """The brief's §5 prospect shape, plus `match` so a client can tell verified matches from partial ones (§11)."""

    name: str = Field(min_length=1)
    location: str | None = Field(default=None, description="Where the profile's own public text says it is")
    social_link: list[SocialLink] = Field(default_factory=list)
    match: Literal["confirmed", "partial"]


class PublicDiscoveryResponse(BaseModel):
    """Default response of POST /prospect-discovery (brief §5). `?debug=true` returns DiscoveryResponse instead."""

    success: bool = Field(description="True when at least one discovery source answered")
    query: str
    total_prospects: int
    prospects: list[PublicProspect]


class DiscoveryResponse(BaseModel):
    """Full internal/debug response: adds the parsed intent, per-platform status and evidence for every link."""

    success: bool = Field(description="True when at least one discovery source answered")
    query: str
    total_prospects: int
    prospects: list[Prospect]
    intent: QueryIntent | None = None
    platforms: list[PlatformStatus] = Field(default_factory=list)


# ------------------------------------------------------------ internal ----

@dataclass(frozen=True)
class SearchHit:
    """One raw result from a search provider or platform API. Everything downstream is derived from these."""

    url: str
    title: str
    snippet: str
    platform: PlatformName
    provider: str
    query: str


@dataclass(frozen=True)
class Contact:
    kind: ContactKind
    value: str          # mailto:…, tel:+…, https://wa.me/…
    source_url: str


@dataclass
class CandidateProfile:
    id: str
    platform: PlatformName
    url: str                        # normalised profile URL
    name: str | None
    handle: str | None
    text: str                       # title + snippet: the only evidence the relevance step may quote
    source_url: str
    contacts: list[Contact] = field(default_factory=list)
    linked_urls: list[str] = field(default_factory=list)  # normalised profile URLs mentioned in its text


@dataclass
class ProspectCluster:
    id: str
    candidates: list[CandidateProfile]
    merge_reasons: list[str] = field(default_factory=list)
    prefilter_score: int = 0

    @property
    def text(self) -> str:
        return "\n".join(c.text for c in self.candidates)

    @property
    def contacts(self) -> list[Contact]:
        seen: dict[str, Contact] = {}
        for c in self.candidates:
            for contact in c.contacts:
                seen.setdefault(contact.value, contact)
        return list(seen.values())
