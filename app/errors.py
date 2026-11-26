"""Exceptions shared across layers. Messages are client-facing, so they never contain keys or headers."""

import re

import httpx


class SourceError(Exception):
    """An expected, reportable failure from a search provider or platform API.

    `status_code` / `retry_after` drive retries and circuit-breaking; `quota_exhausted` means "stop calling this
    provider for now and fail over", which a retry can't fix.
    """

    def __init__(self, message: str, status_code: int | None = None, retry_after: float | None = None,
                 quota_exhausted: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
        self.quota_exhausted = quota_exhausted


class GuardrailBlocked(Exception):
    def __init__(self, rule: str, message: str) -> None:
        super().__init__(message)
        self.rule = rule


class QueryNotUnderstood(Exception):
    """The LLM says the text is not a prospect search."""


class LLMUnavailable(Exception):
    """Primary and fallback models both failed."""


class InvalidLLMOutput(Exception):
    """The model's output failed schema validation even after a repair attempt."""


class IdempotencyConflict(Exception):
    """The key was already used with a different query."""


class IdempotencyInProgress(Exception):
    """A request with this key is still running."""


def parse_retry_after(value: str | None) -> float | None:
    try:
        return max(float(value), 0.0) if value else None
    except ValueError:
        return None  # HTTP-date form; fall back to our own backoff


def raise_for_status(source: str, response: httpx.Response, quota_statuses: frozenset[int] = frozenset()) -> None:
    """Turn an HTTP error into a SourceError with a short, readable message."""
    status = response.status_code
    if status < 400:
        return
    retry_after = parse_retry_after(response.headers.get("Retry-After"))
    detail = re.sub(r"\s+", " ", response.text)[:200].strip()
    if status == 429:
        wait = f"{retry_after:g}s" if retry_after is not None else "later"
        raise SourceError(f"Rate limited by {source}, retry after {wait}", 429, retry_after)
    if status in quota_statuses:
        raise SourceError(f"{source} quota or credits exhausted (HTTP {status}): {detail}", status, retry_after,
                          quota_exhausted=True)
    raise SourceError(f"{source} HTTP {status}: {detail}", status, retry_after)


class LocalSkip(Exception):
    """We chose not to call a provider (circuit open, local rate limit). No request was sent."""

    def __init__(self, error_type: str, message: str) -> None:
        super().__init__(message)
        self.error_type = error_type
