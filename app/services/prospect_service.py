"""Orchestrates the brief's suggested processing flow (§7), in its order:

 1 receive query            (main.py)        8 validate profile URLs + contacts   (profile_extractor)
 2 validate input           (query_service)  9 calculate relevance                (relevance_service)
 3 LLM intent understanding (query_service) 10 consolidate duplicates             (deduplication_service)
 4 extract parameters       (query_service) 11 generate the structured response   (here)
 5 platform search queries  (query_service) 12 validate it with Pydantic          (here, DiscoveryResponse)
 6 multi-platform discovery (search_service)13 return through FastAPI             (main.py)
 7 extract profile info     (profile_extractor)

Cross-cutting: one time budget for the whole request, write-as-you-go storage that can never cost the caller
their results, Idempotency-Key replay/resume, single-flight for identical concurrent queries, and spans per step.
"""

import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

import logfire
from opentelemetry import trace

from app.errors import IdempotencyConflict, IdempotencyInProgress
from app.schemas import (
    CandidateProfile,
    DiscoveryResponse,
    Evidence,
    PlatformName,
    PlatformStatus,
    Prospect,
    ProspectCluster,
    QueryIntent,
    SearchHit,
    SocialLink,
)
from app.services.cache import SingleFlight
from app.services.deduplication_service import consolidate
from app.services.llm_service import request_costs
from app.services.profile_extractor import ProfileExtractor
from app.services.query_service import QueryService
from app.services.relevance_service import Assessment, RelevanceService, generic_name
from app.services.resilience import Deadline
from app.services.search_service import SearchService
from app.services.storage import Storage
from app.utils.normalizers import country_code, is_prospect_website, normalize_query, profile_of
from app.utils.validators import valid_email

INTENT_BUDGET = 6.0
WEBSITE_BUDGET = 5.0
RELEVANCE_BUDGET = 8.0
MAX_CONTACTS_PER_KIND = 3
_ORDER: dict[str, int] = {n: i for i, n in enumerate(
    ["LinkedIn", "Facebook", "Instagram", "X", "YouTube", "Threads", "Google", "Email", "Phone", "WhatsApp"])}


@dataclass(frozen=True)
class Outcome:
    response: DiscoveryResponse
    run_id: str | None
    replayed: bool = False
    shared: bool = False


def current_trace_id() -> str | None:
    context = trace.get_current_span().get_span_context()
    return f"{context.trace_id:032x}" if context.is_valid else None


class ProspectService:
    def __init__(self, queries: QueryService, search: SearchService, extractor: ProfileExtractor,
                 relevance: RelevanceService, store: Storage | None, flight: SingleFlight,
                 request_deadline: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._queries = queries
        self._search = search
        self._extractor = extractor
        self._relevance = relevance
        self._store = store
        self._flight = flight
        self._deadline_seconds = request_deadline
        self._clock = clock
        self._in_flight_keys: set[str] = set()

    async def discover(self, query: str, idempotency_key: str | None = None) -> Outcome:
        if idempotency_key and idempotency_key in self._in_flight_keys:
            raise IdempotencyInProgress("A request with this Idempotency-Key is still in progress")
        flight_key = ("key", idempotency_key) if idempotency_key else ("query", normalize_query(query))
        if idempotency_key:
            self._in_flight_keys.add(idempotency_key)
        try:
            outcome, shared = await self._flight.run(flight_key, lambda: self._run(query, idempotency_key))
        finally:
            if idempotency_key:
                self._in_flight_keys.discard(idempotency_key)
        return Outcome(outcome.response, outcome.run_id, outcome.replayed, shared=shared)

    async def _run(self, query: str, idempotency_key: str | None) -> Outcome:
        costs: list[float] = []
        request_costs.set(costs)
        with logfire.span("prospect discovery", query=query, idempotency_key=idempotency_key) as span:
            # Step 2: input guardrails before anything is stored or spent.
            self._queries.check(query)
            deadline = Deadline(self._deadline_seconds, self._clock)

            run = self._start_run(query, idempotency_key)
            if run and run.replay is not None:
                span.set_attributes({"run_id": run.id, "replayed": True})
                return Outcome(DiscoveryResponse.model_validate(run.replay), run.id, replayed=True)
            run_id = run.id if run else None
            span.set_attribute("run_id", run_id)

            # Steps 3-5: intent + platform-specific search queries (one LLM call, cached).
            intent, intent_cached = await self._queries.understand(query, deadline.budget(INTENT_BUDGET))
            span.set_attributes({"intent": intent.model_dump(exclude={"search_queries"}),
                                 "intent_cache_hit": intent_cached})
            region = country_code(intent.location.country)

            # Step 6: discovery on every platform, concurrently; each result stored the moment it lands.
            def on_done(platform: PlatformName, hits: list[SearchHit], status: PlatformStatus) -> None:
                if run_id:
                    self._store_safely("record_source", run_id, platform, hits, status)

            hits, statuses = await self._search.discover_all(
                intent, deadline, on_done=on_done, skip=run.done_sources if run else None)

            # Steps 7-8: profiles from hits; contacts and linked profiles from prospects' own websites.
            with logfire.span("extract") as extract_span:
                candidates = [c for c in self._extractor.extract(hits, region) if not generic_name(c.name, intent)]
                candidates += await self._extractor.enrich_from_websites(
                    candidates, region, deadline.budget(WEBSITE_BUDGET))
                extract_span.set_attributes({"hits": len(hits), "candidates": len(candidates),
                                             "contacts": sum(len(c.contacts) for c in candidates)})

            # Step 9: relevance per candidate. Step 10: consolidate. Step 11: build the response.
            assessments = await self._relevance.assess(candidates, intent,
                                                       max(0.0, deadline.budget(RELEVANCE_BUDGET) - 0.5))
            clusters = consolidate(candidates)
            prospects = build_prospects(clusters, assessments, intent)

            # Step 12: Pydantic validates the whole response on construction.
            response = DiscoveryResponse(success=any(s.status == "ok" for s in statuses), query=query,
                                         total_prospects=len(prospects),
                                         prospects=prospects, intent=intent, platforms=statuses)
            total_cost = round(sum(costs), 6) if costs else None
            span.set_attributes({"total_prospects": len(prospects), "llm_cost_total_usd": total_cost,
                                 "confirmed": sum(p.match == "confirmed" for p in prospects),
                                 "partial": sum(p.match == "partial" for p in prospects)})
            if run_id:
                self._store_safely("finish", run_id, intent.model_dump_json(), response.model_dump(mode="json"),
                                   total_cost)
        return Outcome(response, run_id)

    def _start_run(self, query: str, idempotency_key: str | None):
        if self._store is None:
            return None
        try:
            return self._store.start(query, idempotency_key, current_trace_id())
        except (IdempotencyConflict, IdempotencyInProgress):
            raise
        except Exception:
            logfire.exception("could not start run in storage; continuing without it")
            return None

    def _store_safely(self, method: str, *args: object) -> None:
        """Discovery already happened, so a storage failure must not cost the caller their results."""
        try:
            getattr(self._store, method)(*args)
        except Exception:
            logfire.exception("could not store {method}", method=method)


# ------------------------------------------------------------- step 11 ----

def _link_ok(platform: str, link: str) -> bool:
    """Output guardrail: every returned link must still pass its platform's validation."""
    if platform == "Email":
        return link.startswith("mailto:") and valid_email(link[7:]) is not None
    if platform == "Phone":
        return link.startswith("tel:+") and link[5:].isdigit()
    if platform == "WhatsApp":
        return link.startswith("https://wa.me/") and link[14:].isdigit()
    if platform == "Google":
        return is_prospect_website(link)
    profile = profile_of(link)
    return profile is not None and profile[0] == platform


def build_prospects(clusters: list[ProspectCluster], assessments: dict[str, Assessment],
                    intent: QueryIntent) -> list[Prospect]:
    """Keep a cluster when at least one of its profiles is relevant; its other profiles come along because a strong
    identity signal tied them together (that's the point of consolidation)."""
    with logfire.span("respond") as span:
        prospects: list[Prospect] = []
        dropped_links = 0
        for cluster in clusters:
            members = [(c, assessments.get(c.id)) for c in cluster.candidates]
            matches = [a.match for _, a in members if a and a.match]
            if not matches:
                continue
            match = "confirmed" if "confirmed" in matches else "partial"
            relevant = [c for c, a in members if a and a.match]
            name = Counter(c.name for c in relevant if c.name).most_common(1)[0][0]
            # Location shown = where the profile's OWN text says it is (verified quote), never the query's place.
            places = Counter(a.location_text for _, a in members if a and a.location_text)
            location = places.most_common(1)[0][0] if places else None

            links: list[SocialLink] = []
            evidence: list[Evidence] = []
            seen: set[str] = set()
            for candidate, assessment in sorted(members, key=lambda m: _ORDER.get(m[0].platform, 99)):
                if candidate.url in seen:
                    continue
                seen.add(candidate.url)
                if not _link_ok(candidate.platform, candidate.url):
                    dropped_links += 1
                    continue
                links.append(SocialLink(platform=candidate.platform, link=candidate.url))
                evidence.append(_evidence(candidate, assessment))
            per_kind: Counter[str] = Counter()
            for contact in sorted(cluster.contacts, key=lambda c: _ORDER[c.kind]):
                if contact.value in seen or not _link_ok(contact.kind, contact.value):
                    dropped_links += contact.value not in seen
                    continue
                if per_kind[contact.kind] >= MAX_CONTACTS_PER_KIND:  # a page listing many numbers isn't one person's
                    continue
                per_kind[contact.kind] += 1
                seen.add(contact.value)
                links.append(SocialLink(platform=contact.kind, link=contact.value))
                # The evidence is the value itself, exactly as found on the source page.
                shown = contact.value.removeprefix("mailto:").removeprefix("tel:")
                evidence.append(Evidence(platform=contact.kind, link=contact.value, source_url=contact.source_url,
                                         text=shown))
            if not links:
                continue
            prospects.append(Prospect(name=name, location=location,
                                      social_link=links, match=match, evidence=evidence))
        prospects.sort(key=lambda p: (p.match != "confirmed", -len(p.social_link), p.name.lower()))
        span.set_attributes({"prospects": len(prospects), "guardrail.dropped_links": dropped_links})
    return prospects


def _evidence(candidate: CandidateProfile, assessment: Assessment | None) -> Evidence:
    quote = assessment.verdict.evidence if assessment and assessment.verdict.evidence else ""
    return Evidence(platform=candidate.platform, link=candidate.url, source_url=candidate.source_url,
                    text=quote or candidate.text[:200])
