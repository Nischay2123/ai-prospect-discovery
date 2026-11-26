"""Brief §7 steps 2-5: validate the query, get the intent and platform search queries from the LLM.

Input guardrails run first (free). The intent is cached by normalised query + prompt version + models, so a
repeated query costs no LLM call.
"""

import logfire

from app.errors import GuardrailBlocked, QueryNotUnderstood
from app.schemas import Location, QueryIntent
from app.services.cache import TTLCache
from app.services.llm_service import LLMGateway, load_prompt, query_block
from app.utils.normalizers import normalize_query
from app.utils.validators import GUARDRAIL_MESSAGES, check_query, looks_like_injection

MAX_KEYWORDS = 8


def _canonical(location: Location) -> Location:
    """Models name DC's city "Washington" (its official name); users and the brief say "Washington DC"."""
    if (location.city or "").strip().lower() == "washington" and \
            (location.state or "").strip().lower() == "district of columbia":
        return location.model_copy(update={"city": "Washington DC"})
    return location


class QueryService:
    def __init__(self, llm: LLMGateway, cache: TTLCache) -> None:
        self._llm = llm
        self._cache = cache
        self._prompt = load_prompt("intent")

    def check(self, query: str) -> None:
        """Input guardrails (step 2). Raises GuardrailBlocked before any API is called."""
        with logfire.span("guardrails.input") as span:
            rule = check_query(query)
            injection = looks_like_injection(query)
            span.set_attributes({"rule_hit": rule, "injection_hint": injection})
            if rule:
                raise GuardrailBlocked(rule, GUARDRAIL_MESSAGES[rule])

    async def understand(self, query: str, budget: float) -> tuple[QueryIntent, bool]:
        """(intent, from_cache). Raises QueryNotUnderstood, LLMUnavailable or InvalidLLMOutput."""
        key = (normalize_query(query), self._prompt.version, self._llm.models_key)
        cached = self._cache.get(key)
        if cached is not None:
            return cached, True

        result = await self._llm.structured(self._prompt, query_block(query), QueryIntent, budget)
        intent = result.value
        if not intent.is_valid:
            raise QueryNotUnderstood(intent.reason or "The query is not a request to find people or businesses")
        if not intent.interest.strip() or not intent.audience.strip():
            raise QueryNotUnderstood("Could not identify an interest and audience in the query")
        keywords = list(dict.fromkeys(k.strip().lower() for k in intent.keywords if k.strip()))
        intent = intent.model_copy(update={"keywords": keywords[:MAX_KEYWORDS],
                                           "location": _canonical(intent.location)})
        self._cache.set(key, intent)
        return intent, False
