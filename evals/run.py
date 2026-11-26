"""Eval runner: measures intent accuracy, relevance agreement and dedup correctness per model.

    python -m evals.run --task dedup                                     # deterministic, no API keys
    python -m evals.run --task intent --models groq/openai/gpt-oss-20b,gemini/gemini-3.5-flash-lite
    python -m evals.run --task relevance --models groq/openai/gpt-oss-20b

Each LLM case goes through the real LLMGateway (same prompts, schema and LiteLLM path as production), so the
numbers are measured, not estimated. Results are printed and written to evals/results/<date>-<task>.md, which is
where docs/05-llm-choice.md takes its "measured" numbers from.
"""

import argparse
import asyncio
import json
import statistics
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import logfire

from app.config import settings
from app.schemas import CandidateProfile, Contact, QueryIntent, RelevanceResult
from app.services.deduplication_service import consolidate
from app.services.llm_service import LLMGateway, candidates_block, load_prompt, query_block
from app.utils.normalizers import fold

ROOT = Path(__file__).resolve().parent
PACE = 0.0  # seconds between LLM calls; free tiers (e.g. Groq: 8,000 tokens/minute) need spacing to be measured fairly
logfire.configure(send_to_logfire="if-token-present", console=False, service_name="ai-prospect-discovery-evals")


def load(name: str) -> list[dict]:
    return [json.loads(line) for line in (ROOT / "datasets" / f"{name}.jsonl").read_text().splitlines() if line]


def _same(actual: str | None, expected: str | None) -> bool:
    if expected is None:
        return actual is None
    return actual is not None and (fold(expected) in fold(actual) or fold(actual) in fold(expected))


async def eval_intent(model: str) -> dict:
    gateway = LLMGateway.from_settings(replace(settings, llm_model=model, llm_fallback_model=""))
    prompt = load_prompt("intent")
    fields_ok = fields_total = valid_json = 0
    latencies, costs, failures = [], [], []
    for case in load("intent"):
        await asyncio.sleep(PACE)
        expected = case["expected"]
        with logfire.span("eval intent {query}", query=case["query"], model=model):
            started = time.monotonic()
            try:
                result = await gateway.structured(prompt, query_block(case["query"]), QueryIntent, budget=30)
            except Exception as exc:
                failures.append(f"{case['query']}: {type(exc).__name__}")
                fields_total += len(expected)
                continue
            latencies.append((time.monotonic() - started) * 1000)
            costs.append(result.cost_usd or 0.0)
            valid_json += 1
            got = result.value
            actual = {"interest": got.interest, "city": got.location.city, "state": got.location.state,
                      "country": got.location.country, "target_type": got.target_type, "is_valid": got.is_valid}
            for key, want in expected.items():
                fields_total += 1
                ok = actual[key] == want if key in ("target_type", "is_valid") else _same(actual[key], want)
                fields_ok += ok
                if not ok:
                    failures.append(f"{case['query']}: {key}={actual[key]!r} (expected {want!r})")
    return _summary(model, "intent", fields_ok, fields_total, valid_json, len(load("intent")), latencies, costs,
                    failures)


async def eval_relevance(model: str) -> dict:
    """One batched call per target (as in production), graded on interest and location per candidate."""
    gateway = LLMGateway.from_settings(replace(settings, llm_model=model, llm_fallback_model=""))
    prompt = load_prompt("relevance")
    groups: dict[str, list[dict]] = {}
    for case in load("relevance"):
        groups.setdefault(json.dumps(case["target"], sort_keys=True), []).append(case)
    ok = total = calls = 0
    latencies, costs, failures = [], [], []
    for key, cases in groups.items():
        await asyncio.sleep(PACE)
        target = json.loads(key)
        kind = "businesses (or their owners)" if target.get("target_type") == "business" else "individual people"
        user = (f'Target: interest "{target["interest"]}", audience "{target["audience"]}" ({kind}), '
                f'location "{target["location"]}"\n' + candidates_block([(c["id"], c["text"]) for c in cases]))
        started = time.monotonic()
        try:
            result = await gateway.structured(prompt, user, RelevanceResult, budget=60)
        except Exception as exc:
            failures.append(f"{target['audience']}: call failed: {exc}")
            total += 2 * len(cases)
            continue
        calls += 1
        latencies.append((time.monotonic() - started) * 1000)
        costs.append(result.cost_usd or 0.0)
        verdicts = {v.candidate_id: v for v in result.value.verdicts}
        for case in cases:
            v = verdicts.get(case["id"])
            for dim in ("interest", "location"):
                total += 1
                got = getattr(v, dim) if v else "missing"
                ok += got == case["expected"][dim]
                if got != case["expected"][dim]:
                    failures.append(f"{case['id']} {dim}: {got} (expected {case['expected'][dim]})")
    return _summary(model, "relevance", ok, total, calls, len(groups), latencies, costs, failures)


def eval_dedup() -> dict:
    ok, failures = 0, []
    cases = load("dedup")
    for case in cases:
        candidates = [CandidateProfile(
            id=c["id"], platform=c["platform"], url=c["url"], name=c["name"], handle=c.get("handle"), text="",
            source_url=c["url"], contacts=[Contact("Phone" if v.startswith("tel:") else "Email", v, c["url"])
                                           for v in c.get("contacts", [])],
            linked_urls=c.get("linked", [])) for c in case["candidates"]]
        got = sorted(sorted(m.id for m in cl.candidates) for cl in consolidate(candidates))
        if got == sorted(sorted(g) for g in case["expected"]):
            ok += 1
        else:
            failures.append(f"{case['name']}: got {got}")
    return _summary("deterministic", "dedup", ok, len(cases), len(cases), len(cases), [], [], failures)


def _summary(model, task, ok, total, valid, cases, latencies, costs, failures) -> dict:
    return {
        "model": model, "task": task,
        "accuracy": round(ok / total, 3) if total else 0.0,
        "valid_json": f"{valid}/{cases}",
        "latency_p50_ms": round(statistics.median(latencies)) if latencies else None,
        "latency_p95_ms": round(sorted(latencies)[int(0.95 * (len(latencies) - 1))]) if latencies else None,
        "cost_per_call_usd": round(statistics.mean(costs), 6) if costs else None,
        "failures": failures,
    }


def write_report(task: str, rows: list[dict]) -> Path:
    models = "-".join(r["model"].split("/")[-1] for r in rows)
    out = ROOT / "results" / f"{datetime.now(UTC):%Y-%m-%d}-{task}-{models}.md"
    lines = [f"# Eval: {task} ({datetime.now(UTC):%Y-%m-%d %H:%M} UTC)", "",
             "| Model | Accuracy | Valid JSON | p50 ms | p95 ms | Cost/call USD |", "|---|---|---|---|---|---|"]
    lines += [f"| `{r['model']}` | {r['accuracy']:.1%} | {r['valid_json']} | {r['latency_p50_ms']} | "
              f"{r['latency_p95_ms']} | {r['cost_per_call_usd']} |" for r in rows]
    for r in rows:
        if r["failures"]:
            lines += ["", f"## Misses: `{r['model']}`", *[f"- {f}" for f in r["failures"]]]
    out.write_text("\n".join(lines) + "\n")
    return out


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", choices=["intent", "relevance", "dedup"], required=True)
    parser.add_argument("--models", default=settings.llm_model, help="comma-separated LiteLLM model strings")
    parser.add_argument("--pace", type=float, default=0.0, help="seconds between LLM calls (for free-tier limits)")
    args = parser.parse_args()
    global PACE
    PACE = args.pace
    if args.task == "dedup":
        rows = [eval_dedup()]
    else:
        run = eval_intent if args.task == "intent" else eval_relevance
        rows = [await run(m.strip()) for m in args.models.split(",") if m.strip()]
    for r in rows:
        print(json.dumps({k: v for k, v in r.items() if k != "failures"}), f"misses={len(r['failures'])}")
    print("report:", write_report(args.task, rows))


if __name__ == "__main__":
    asyncio.run(main())
