"""Resilience toolkit: time budget, safe retries, rate limits, circuit breaker, and the Guard that combines them.

Every outbound provider call (search APIs, YouTube) goes through `Guard.call`:
daily cap -> circuit breaker -> rate limit -> retries inside the time budget.
LLM calls are protected by LiteLLM's router instead (its own retries, fallbacks and cooldowns).
"""

import asyncio
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TypeVar

import httpx
import logfire

from app.errors import LocalSkip, SourceError

T = TypeVar("T")


# ------------------------------------------------------------ deadline ----
@dataclass
class Deadline:
    seconds: float
    clock: Callable[[], float] = time.monotonic
    expires_at: float = field(init=False)

    def __post_init__(self) -> None:
        self.expires_at = self.clock() + self.seconds

    def remaining(self) -> float:
        return max(0.0, self.expires_at - self.clock())

    def budget(self, cap: float) -> float:
        """Seconds a stage may spend: its own cap, but never past the request deadline."""
        return min(cap, self.remaining())


# ------------------------------------------------------------ retry ----

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 2          # total tries, including the first
    base_delay: float = 0.5    # seconds; doubles each retry
    max_delay: float = 4.0

    def is_retryable(self, exc: BaseException) -> bool:
        if isinstance(exc, SourceError):
            return not exc.quota_exhausted and exc.status_code in RETRYABLE_STATUS
        return isinstance(exc, (httpx.TransportError, TimeoutError))

    def delay(self, retry_number: int, exc: BaseException) -> float:
        """Honour Retry-After when given; otherwise exponential backoff with full jitter."""
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None:
            return retry_after
        return random.uniform(0, min(self.max_delay, self.base_delay * 2**retry_number))

    async def run(self, call: Callable[[float], Awaitable[T]], budget: float,
                  on_retry: Callable[[int, float, BaseException], None] | None = None,
                  clock: Callable[[], float] = time.monotonic) -> T:
        """Run `call(remaining_seconds)` with retries, never past `budget` seconds in total."""
        deadline = clock() + budget
        attempt = 1
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                raise TimeoutError
            try:
                return await asyncio.wait_for(call(remaining), remaining)
            except Exception as exc:
                if attempt >= self.attempts or not self.is_retryable(exc):
                    raise
                delay = self.delay(attempt - 1, exc)
                if clock() + delay >= deadline:
                    raise
                if on_retry:
                    on_retry(attempt, delay, exc)
                await asyncio.sleep(delay)
                attempt += 1


# ------------------------------------------------------------ rate limiter ----
@dataclass
class _Bucket:
    tokens: float
    updated: float


class ProviderLimiter:
    """Token bucket per provider (`rates` = calls/second) plus optional daily caps (e.g. YouTube's 100 searches).

    `acquire` returns 0 when the call may go ahead, else seconds to wait. Nothing is consumed when refused.
    No await between check and consume, so concurrent tasks on one event loop can't both pass.
    """

    def __init__(self, rates: dict[str, float], daily_caps: dict[str, int] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 today: Callable[[], str] = lambda: datetime.now(UTC).strftime("%Y-%m-%d")) -> None:
        self._rates = rates
        self._caps = daily_caps or {}
        self._clock = clock
        self._today = today
        self._buckets: dict[str, _Bucket] = {}
        self._daily: dict[str, tuple[str, int]] = {}

    def daily_exhausted(self, key: str) -> bool:
        cap = self._caps.get(key)
        if cap is None:
            return False
        day, used = self._daily.get(key, (self._today(), 0))
        return day == self._today() and used >= cap

    def acquire(self, key: str) -> float:
        rate = self._rates.get(key)
        if rate is None or rate <= 0:
            return 0.0
        now = self._clock()
        bucket = self._buckets.setdefault(key, _Bucket(tokens=1.0, updated=now))
        bucket.tokens = min(1.0, bucket.tokens + (now - bucket.updated) * rate)
        bucket.updated = now
        if bucket.tokens < 1.0:
            return (1.0 - bucket.tokens) / rate
        bucket.tokens -= 1.0
        return 0.0

    def count_daily(self, key: str) -> None:
        """Record one call against the daily cap (only for calls that actually went out)."""
        if key not in self._caps:
            return
        today = self._today()
        day, used = self._daily.get(key, (today, 0))
        self._daily[key] = (today, used + 1 if day == today else 1)


class ClientLimiter:
    """Sliding window: at most `per_minute` requests per client IP. Memory is bounded: idle clients are pruned
    and at most `max_clients` are tracked (oldest evicted)."""

    def __init__(self, per_minute: float, clock: Callable[[], float] = time.monotonic,
                 max_clients: int = 10_000) -> None:
        self._limit = int(per_minute)
        self._clock = clock
        self._max = max_clients
        self._hits: dict[str, deque[float]] = {}

    def acquire(self, client: str) -> float:
        if self._limit <= 0:
            return 0.0
        now = self._clock()
        if client not in self._hits and len(self._hits) >= self._max:
            self._prune(now)
            if len(self._hits) >= self._max:
                self._hits.pop(next(iter(self._hits)))
        hits = self._hits.setdefault(client, deque())
        while hits and now - hits[0] >= 60:
            hits.popleft()
        if len(hits) >= self._limit:
            return 60 - (now - hits[0])
        hits.append(now)
        return 0.0

    def _prune(self, now: float) -> None:
        for key in [k for k, h in self._hits.items() if not h or now - h[-1] >= 60]:
            del self._hits[key]


# ------------------------------------------------------------ circuit breaker ----
@dataclass
class _Circuit:
    failures: int = 0
    opened_at: float | None = None
    trial_started_at: float | None = None


class CircuitBreaker:
    """CLOSED -> (N consecutive health failures) -> OPEN -> (cool-down) -> HALF-OPEN (one trial call).

    Trial succeeds -> CLOSED; trial fails -> OPEN again. Only health failures count (network errors,
    timeouts, 5xx, 429); a 401 means bad credentials, not a sick platform.

    ponytail: in-process state, like the rate limiter; share it via Redis with several workers.
    """

    def __init__(
        self, failure_threshold: int = 5, reset_after: float = 60.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._threshold = failure_threshold
        self._reset_after = reset_after
        self._clock = clock
        self._circuits: dict[str, _Circuit] = {}

    def state(self, key: str) -> str:
        circuit = self._circuits.get(key)
        if circuit is None or circuit.opened_at is None:
            return "closed"
        return "open" if self._clock() - circuit.opened_at < self._reset_after else "half_open"

    def allow(self, key: str) -> float:
        """0 if a call may go ahead, else seconds until the circuit lets a trial call through."""
        circuit = self._circuits.setdefault(key, _Circuit())
        if circuit.opened_at is None:
            return 0.0
        now = self._clock()
        remaining = circuit.opened_at + self._reset_after - now
        if remaining > 0:
            return remaining
        # Half-open: allow one trial. A trial that never reported back (e.g. cancelled) expires.
        if circuit.trial_started_at is not None and now - circuit.trial_started_at < self._reset_after:
            return self._reset_after - (now - circuit.trial_started_at)
        circuit.trial_started_at = now
        return 0.0

    def record_success(self, key: str) -> None:
        self._circuits[key] = _Circuit()

    def record_failure(self, key: str) -> None:
        circuit = self._circuits.setdefault(key, _Circuit())
        circuit.failures += 1
        if circuit.trial_started_at is not None or circuit.failures >= self._threshold:
            circuit.opened_at = self._clock()
            circuit.trial_started_at = None


# ------------------------------------------------------------ resilience ----

MAX_LIMITER_WAIT = 1.0  # wait this long for a rate-limit slot before giving up locally


def classify(exc: BaseException) -> tuple[str, str, bool]:
    """(error_type, client-facing message, counts against provider health)."""
    if isinstance(exc, LocalSkip):
        return exc.error_type, str(exc), False
    if isinstance(exc, SourceError):
        if exc.quota_exhausted:
            return "quota_exhausted", str(exc), False
        unhealthy = exc.status_code is None or exc.status_code >= 500 or exc.status_code == 429
        return "platform_error", str(exc), unhealthy
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "timeout", "Timed out", True
    if isinstance(exc, httpx.TransportError):
        return "network_error", f"Network error ({type(exc).__name__})", True
    if isinstance(exc, (KeyError, TypeError, ValueError)):  # e.g. HTML error page instead of JSON
        return "invalid_response", f"Unexpected response ({type(exc).__name__}: {exc})", True
    return "unexpected", f"Unexpected error: {type(exc).__name__}: {exc}", False


class Guard:
    def __init__(self, breaker: CircuitBreaker, limiter: ProviderLimiter, retry: RetryPolicy) -> None:
        self.breaker = breaker
        self.limiter = limiter
        self.retry = retry

    async def call(self, key: str, fn: Callable[[float], Awaitable[T]], budget: float,
                   daily_key: str | None = None) -> T:
        if daily_key and self.limiter.daily_exhausted(daily_key):
            raise SourceError(f"{key} daily quota used up; resets tomorrow (UTC)", quota_exhausted=True)
        wait = self.breaker.allow(key)
        if wait > 0:
            raise LocalSkip("circuit_open", f"{key} is failing repeatedly; skipped for {wait:.0f}s (circuit open)")
        wait = self.limiter.acquire(key)
        if wait > 0:
            if wait > min(MAX_LIMITER_WAIT, budget):
                raise LocalSkip("rate_limited_local", f"{key} rate limited locally, retry in {wait:.1f}s")
            await asyncio.sleep(wait)
            budget -= wait

        async def counted(remaining: float) -> T:
            if daily_key:
                self.limiter.count_daily(daily_key)
            return await fn(remaining)

        def on_retry(attempt: int, delay: float, exc: BaseException) -> None:
            logfire.info("retrying {provider} in {delay}s after: {reason}", provider=key, delay=round(delay, 2),
                         reason=str(exc) or type(exc).__name__)

        try:
            result = await self.retry.run(counted, budget, on_retry)
        except Exception as exc:
            _, _, unhealthy = classify(exc)
            if unhealthy:
                self.breaker.record_failure(key)
            else:
                self.breaker.record_success(key)  # it answered; the problem is the request or our quota
            raise
        self.breaker.record_success(key)
        return result
