"""LLM gateway: every model call goes through here, and only here.

LiteLLM's Router gives one API for any provider plus fallbacks, retries, cooldowns and cost tracking. It stays
behind this small interface, so the rest of the app never imports LiteLLM and could swap it for direct SDKs.

Retries for LLM calls live ONLY in the Router, so retry layers never multiply. By default the fallback model IS
the retry (num_retries=0): worst case per task = primary timeout + fallback timeout + one repair attempt if time
remains, all inside the task's budget because every call gets only the time that is left.
"""

import copy
import json
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar

import logfire
from pydantic import BaseModel, ValidationError

from app.config import Settings
from app.errors import InvalidLLMOutput, LLMUnavailable

# Use LiteLLM's bundled price list instead of downloading it at import: cold start 6.5 s -> 1.2 s (measured).
# The bundled list already has the defaults' prices (gemini-3.5-flash-lite, gpt-oss-20b, claude-haiku-5-5).
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

T = TypeVar("T", bound=BaseModel)
Task = Literal["intent", "relevance"]
PROMPTS = Path(__file__).resolve().parent.parent / "prompts"


# Per-request LLM cost ledger: the orchestrator sets a fresh list, every call appends its cost.
request_costs: ContextVar[list[float] | None] = ContextVar("request_costs", default=None)


@dataclass(frozen=True)
class Prompt:
    task: Task
    version: str
    text: str


def load_prompt(task: Task) -> Prompt:
    """Prompt files start with `<!-- version: x -->`; the version goes on every span and into cache keys."""
    raw = (PROMPTS / f"{task}.md").read_text()
    first, _, rest = raw.partition("\n")
    version = first.removeprefix("<!-- version:").removesuffix("-->").strip()
    return Prompt(task=task, version=version, text=rest.strip())


@dataclass(frozen=True)
class LLMResult(Generic[T]):
    value: T
    model: str
    fallback_used: bool
    input_tokens: int
    output_tokens: int
    cached_tokens: int
    cost_usd: float | None
    latency_ms: float


def strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Pydantic schema made portable for strict structured output: $refs inlined, titles and defaults removed."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(copy.deepcopy(defs[node["$ref"].split("/")[-1]]))
            return {k: walk(v) for k, v in node.items() if k not in ("title", "default")}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


def build_router(settings: Settings):
    """Primary + fallback deployments. Anthropic needs explicit cache breakpoints; other providers cache
    repeated prefixes automatically, so the injection point is only added for anthropic/ models."""
    from litellm import Router  # imported here so tests and tools that don't call LLMs stay light

    def params(model: str) -> dict[str, Any]:
        p: dict[str, Any] = {"model": model}
        if model.startswith("anthropic/"):
            p["cache_control_injection_points"] = [{"location": "message", "role": "system"}]
        return p

    model_list = [{"model_name": "primary", "litellm_params": params(settings.llm_model)}]
    fallbacks = []
    if settings.llm_fallback_model and settings.llm_fallback_model != settings.llm_model:
        model_list.append({"model_name": "fallback", "litellm_params": params(settings.llm_fallback_model)})
        fallbacks = [{"primary": ["fallback"]}]
    return Router(model_list=model_list, fallbacks=fallbacks, num_retries=settings.llm_num_retries,
                  timeout=settings.llm_timeout)


class LLMGateway:
    def __init__(self, router: Any, primary_model: str, fallback_model: str | None = None) -> None:
        self._router = router
        self.primary_model = primary_model
        self.fallback_model = fallback_model

    @classmethod
    def from_settings(cls, settings: Settings) -> "LLMGateway":
        import litellm

        litellm.drop_params = True  # e.g. reasoning_effort on a provider that doesn't take it
        return cls(build_router(settings), settings.llm_model, settings.llm_fallback_model)

    @property
    def models_key(self) -> str:
        return f"{self.primary_model}|{self.fallback_model}"

    async def structured(self, prompt: Prompt, user: str, schema: type[T], budget: float) -> LLMResult[T]:
        """Strict-JSON call validated by Pydantic. One repair attempt if the output fails validation."""
        if budget <= 0:
            raise LLMUnavailable("No time left for the LLM call")
        messages = [{"role": "system", "content": prompt.text}, {"role": "user", "content": user}]
        response_format = {"type": "json_schema",
                           "json_schema": {"name": schema.__name__, "schema": strict_schema(schema), "strict": True}}
        deadline = time.monotonic() + budget
        for attempt in (1, 2):
            with logfire.span("llm {task}", task=prompt.task, prompt_version=prompt.version) as span:
                started = time.monotonic()
                try:
                    response = await self._router.acompletion(
                        model="primary", messages=messages, response_format=response_format,
                        temperature=0, reasoning_effort="low", timeout=max(0.1, deadline - time.monotonic()),
                    )
                except Exception as exc:
                    span.record_exception(exc)
                    raise LLMUnavailable(f"LLM call failed on primary and fallback: {type(exc).__name__}: {exc}") \
                        from exc
                result_meta = self._meta(response, started)
                span.set_attributes({
                    "gen_ai.request.model": self.primary_model,
                    "gen_ai.response.model": result_meta["model"],
                    "gen_ai.usage.input_tokens": result_meta["input_tokens"],
                    "gen_ai.usage.output_tokens": result_meta["output_tokens"],
                    "gen_ai.usage.cached_tokens": result_meta["cached_tokens"],
                    "llm.cost_usd": result_meta["cost_usd"],
                    "llm.fallback_used": result_meta["fallback_used"],
                    "llm.latency_ms": result_meta["latency_ms"],
                    "llm.attempt": attempt,
                })
                ledger = request_costs.get()
                if ledger is not None and result_meta["cost_usd"] is not None:
                    ledger.append(float(result_meta["cost_usd"]))
                content = response.choices[0].message.content or ""
                try:
                    return LLMResult(value=schema.model_validate_json(_strip_fences(content)), **result_meta)
                except ValidationError as exc:
                    span.set_attribute("llm.validation_error", str(exc)[:500])
                    if attempt == 2 or time.monotonic() >= deadline:
                        raise InvalidLLMOutput(f"{prompt.task} output failed validation: {exc.error_count()} errors") \
                            from exc
                    messages = [*messages, {"role": "assistant", "content": content},
                                {"role": "user", "content": f"Your output failed validation: {exc}. "
                                                            "Return only valid JSON matching the schema."}]
        raise InvalidLLMOutput("unreachable")  # pragma: no cover

    def _meta(self, response: Any, started: float) -> dict[str, Any]:
        usage = getattr(response, "usage", None)
        details = getattr(usage, "prompt_tokens_details", None)
        hidden = getattr(response, "_hidden_params", {}) or {}
        model = str(hidden.get("litellm_model_name") or getattr(response, "model", "") or self.primary_model)
        primary_tail = self.primary_model.split("/")[-1]
        return {
            "model": model,
            "fallback_used": bool(self.fallback_model) and primary_tail not in model,
            "input_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "cached_tokens": int(getattr(details, "cached_tokens", 0) or 0),
            "cost_usd": hidden.get("response_cost"),
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
        }


def _strip_fences(content: str) -> str:
    """Some models wrap JSON in ```json fences even in JSON mode."""
    text = content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


def candidates_block(items: list[tuple[str, str]]) -> str:
    """Retrieved web text is data: each candidate wrapped in tags, truncated, our delimiters stripped from it."""
    def clean(text: str) -> str:
        return text.replace("<candidate", "‹candidate").replace("</candidate", "‹/candidate")[:500]
    return "<candidates>\n" + "\n".join(
        f'<candidate id="{cid}">\n{clean(text)}\n</candidate>' for cid, text in items) + "\n</candidates>"


def query_block(query: str) -> str:
    return "<query>" + json.dumps(query)[1:-1].replace("</query", "‹/query") + "</query>"
