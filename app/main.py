"""FastAPI entry point: `uvicorn app.main:app --host 0.0.0.0 --port 4000`"""

import os
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

import httpx
import logfire
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, Security
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader
from opentelemetry import trace

from app.config import ALL_PLATFORMS, settings
from app.errors import (
    GuardrailBlocked,
    IdempotencyConflict,
    IdempotencyInProgress,
    InvalidLLMOutput,
    LLMUnavailable,
    QueryNotUnderstood,
)
from app.platforms import build_platforms
from app.schemas import DiscoveryRequest, PublicDiscoveryResponse
from app.services.cache import Caches
from app.services.llm_service import LLMGateway
from app.services.profile_extractor import ProfileExtractor
from app.services.prospect_service import ProspectService
from app.services.query_service import QueryService
from app.services.relevance_service import RelevanceService
from app.services.resilience import CircuitBreaker, ClientLimiter, Guard, ProviderLimiter, RetryPolicy
from app.services.search_service import Brave, FailoverSearch, SearchService, Serper
from app.services.storage import Storage

# Without LOGFIRE_TOKEN this only prints spans to the console; with it, traces also go to the Logfire UI.
# Logfire's default scrubbing redacts secret-looking values. Contact details are never put in span attributes
# (spans carry counts, statuses and URLs of search APIs, not prospect data), and API keys travel in headers,
# which HTTP tracing does not record.
logfire.configure(service_name="ai-prospect-discovery", service_version="1.0.0", send_to_logfire="if-token-present")

TRACE_ID_HEADER = "X-Trace-ID"
RUN_ID_HEADER = "X-Run-ID"
REPLAYED_HEADER = "Idempotent-Replayed"

# Free quotas: Serper credits, Brave 1 req/s on the free plan, YouTube search has its own 100 calls/day bucket.
PROVIDER_RATES = {"serper": 5.0, "brave": 1.0, "youtube": 2.0}
DAILY_CAPS = {"youtube_search": 100}


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if not settings.api_key:
        logfire.warn("API_KEY is not set: POST /prospect-discovery is open to anyone who can reach this server")
    store = Storage(settings.db_path, retention_days=settings.retention_days)  # purges on first use, then daily
    caches = Caches()
    guard = Guard(CircuitBreaker(), ProviderLimiter(PROVIDER_RATES, DAILY_CAPS), RetryPolicy())
    # One pooled HTTP client for the app's lifetime, shared by every adapter.
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True,
                                 headers={"User-Agent": "ai-prospect-discovery/1.0"}) as client:
        logfire.instrument_httpx(client)  # headers/bodies are not captured; every API key is sent as a header
        search = FailoverSearch([Serper(settings.serper_api_key), Brave(settings.brave_api_key)], guard)
        llm = LLMGateway.from_settings(settings)
        app.state.service = ProspectService(
            queries=QueryService(llm, caches.intent),
            search=SearchService(build_platforms(settings, search, guard), client, caches.search,
                                 settings.platform_timeout),
            extractor=ProfileExtractor(client, caches.page, check_deliverability=False),
            relevance=RelevanceService(llm),
            store=store, flight=caches.flight, request_deadline=settings.request_deadline,
        )
        app.state.client_limiter = ClientLimiter(settings.client_rate_limit_per_minute)
        app.state.search_configured = search.is_configured()
        yield
    store.close()


app = FastAPI(
    title="AI-Powered Multi-Platform Prospect Discovery API",
    version="1.0.0",
    description=(
        "Understands a natural-language query, discovers real public profiles on Google, LinkedIn, X, YouTube, "
        "Instagram, Facebook and Threads, extracts published Email/Phone/WhatsApp contacts, consolidates "
        "profiles of the same prospect and returns only prospects with public evidence of relevance.\n\n"
        "Nothing is generated: every name, link and contact comes from a real source response.\n\n"
        "The default response is the brief's shape plus `match` (confirmed / partial). "
        "`?debug=true` adds the parsed intent, per-platform status and the evidence for every link. "
        "Send an `Idempotency-Key` header to get the stored answer on retries instead of a second search."
    ),
    lifespan=lifespan,
)
logfire.instrument_fastapi(app)


@app.middleware("http")
async def add_trace_id_header(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """Return the trace ID so a client can quote it and we can open that exact trace."""
    response = await call_next(request)
    context = trace.get_current_span().get_span_context()
    if context.is_valid:
        response.headers[TRACE_ID_HEADER] = f"{context.trace_id:032x}"
    return response


_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="Required when API_KEY is set")


def require_api_key(api_key: Annotated[str | None, Security(_api_key_header)]) -> None:
    """Every call spends search and LLM quota, so the endpoint can be locked with API_KEY."""
    if settings.api_key and not secrets.compare_digest(api_key or "", settings.api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


def client_ip(request: Request) -> str:
    """The caller's IP. Behind Vercel's proxy, x-real-ip / x-forwarded-for are set by the platform; locally the
    socket address is used. Never keyed by a header value the client can freely change (like X-API-Key)."""
    if os.getenv("VERCEL"):
        forwarded = request.headers.get("x-real-ip") or request.headers.get("x-forwarded-for", "").split(",")[0]
        if forwarded.strip():
            return forwarded.strip()
    return request.client.host if request.client else "unknown"


def client_rate_limit(request: Request) -> None:
    wait = request.app.state.client_limiter.acquire(client_ip(request))
    if wait > 0:
        raise HTTPException(status_code=429, detail=f"Too many requests, retry in {wait:.0f}s",
                            headers={"Retry-After": str(int(wait) + 1)})


def get_service(request: Request) -> ProspectService:
    return request.app.state.service


INDEX_HTML = Path(__file__).resolve().parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    """The web UI: one static page that calls POST /prospect-discovery?debug=true."""
    return FileResponse(INDEX_HTML, media_type="text/html")


@app.get("/health", summary="Liveness and configuration status (key names only, never values)")
def health(request: Request) -> dict:
    return {
        "status": "ok",
        "time": int(time.time()),
        "llm": {"model": settings.llm_model, "fallback": settings.llm_fallback_model,
                "keys_configured": sorted(settings.llm_keys)},
        "search_configured": request.app.state.search_configured,
        "youtube_configured": bool(settings.youtube_api_key),
        "platforms": [p for p in ALL_PLATFORMS if p in settings.enabled_platforms],
        "api_key_required": bool(settings.api_key),
    }


@app.post(
    "/prospect-discovery",
    response_model=PublicDiscoveryResponse,
    summary="Discover public prospects for a natural-language query",
    dependencies=[Depends(require_api_key), Depends(client_rate_limit)],
    responses={
        401: {"description": "API_KEY is set and X-API-Key is missing or wrong"},
        409: {"description": "A request with the same Idempotency-Key is still running"},
        422: {"description": "Invalid query, blocked by a guardrail, not a prospect search, or key reused"},
        429: {"description": "Too many requests from this client"},
        503: {"description": "The LLM providers are unavailable, so the query can't be understood"},
    },
)
async def prospect_discovery(
    payload: DiscoveryRequest,
    service: Annotated[ProspectService, Depends(get_service)],
    debug: Annotated[bool, Query(description="Add intent, platform status, match level and evidence")] = False,
    idempotency_key: Annotated[str | None, Header(min_length=1, max_length=255, pattern=r"^[\x21-\x7e]+$",
                                                  description="Optional. Same key + same query returns the "
                                                              "stored answer.")] = None,
) -> JSONResponse:
    try:
        outcome = await service.discover(payload.query, idempotency_key)
    except GuardrailBlocked as exc:
        raise HTTPException(status_code=422, detail={"rule": exc.rule, "message": str(exc)}) from exc
    except QueryNotUnderstood as exc:
        raise HTTPException(status_code=422, detail=f"Query is not a prospect search: {exc}") from exc
    except IdempotencyConflict as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except IdempotencyInProgress as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (LLMUnavailable, InvalidLLMOutput) as exc:
        raise HTTPException(status_code=503, detail=f"Could not understand the query right now: {exc}",
                            headers={"Retry-After": "30"}) from exc

    full = outcome.response
    if debug:
        body = full.model_dump(mode="json")
    else:  # brief §5 shape (+ match); Pydantic validates it on construction (brief §7 step 12)
        body = PublicDiscoveryResponse.model_validate(full.model_dump(include={
            "success": True, "query": True, "total_prospects": True,
            "prospects": {"__all__": {"name", "location", "social_link", "match"}}})).model_dump(mode="json")
    headers = {}
    if outcome.run_id:
        headers[RUN_ID_HEADER] = outcome.run_id
    if outcome.replayed:
        headers[REPLAYED_HEADER] = "true"
    return JSONResponse(body, headers=headers)
