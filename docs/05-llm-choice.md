# 5. LLM choice, cost and speed (brief §8)

## What the LLM does here

Only two calls per request. Everything code can decide (URL rules, email/phone checks, merging) is done by code.

| Call | In | Out |
|---|---|---|
| 1. Understand the query | the query + prompt with examples | interest, audience, location, type, keywords, one search phrase per platform |
| 2. Judge relevance (one batch) | ~25 candidates' title + snippet | per candidate: interest / location / audience + quotes |

## A. Comparison (prices from official pages, checked 27 Nov 2026)

USD per 1M tokens. The last column is a **list-price estimate** for about 4,500 input and 1,050 output tokens. The real measured cost (section D) is lower for Gemini because actual token counts per call vary and the stable prompt prefix is partly cached.

| Provider | Model | Input | Output | Context | Strict JSON | Free key | Est. cost / request |
|---|---|---:|---:|---|---|---|---:|
| Google | `gemini-3.5-flash-lite` | $0.30 | $2.50 | 1M | yes | yes | ~$0.0040 |
| Groq | `openai/gpt-oss-20b` | $0.075 | $0.30 | 131K | yes | yes | ~$0.0007 |
| Anthropic | `claude-haiku-5-5` | $0.10 | $0.50 | 1M | yes | starter credit | ~$0.0010 |
| OpenAI | `gpt-6-luna` | $0.10 | $0.50 | 1.05M | yes | no | ~$0.0010 |

Sources: platform.claude.com/docs/en/about-claude/pricing, developers.openai.com/api/docs/pricing, aistudio.google.com/docs/pricing, console.groq.com/docs/models.

## B. What I measured (my own evals, `evals/`)

I ran the same prompts through the real code path. **Measured**, not estimated:

| Model | Query understanding | Relevance | Speed |
|---|---|---|---|
| Gemini 3.5 Flash-Lite | 19/20 answered and every answered query fully correct, with one transient API failure (saved report). An earlier run of the same set scored 20/20 | **91.9%** on 31 labelled snippets (3 targets: people, businesses, founders) | ~1.5 s intent, ~2 s relevance |
| Groq gpt-oss-20b | **99%** field accuracy, 20/20 valid (calls spaced with `--pace`) | **83.9%** on the same 31 snippets | ~0.75 s per call |

Groq's free tier allows **8,000 tokens per minute**. Running 20 queries back to back hits that limit, and the failures are rate limits, not wrong answers. `evals/run.py --pace` spaces the calls for a fair run. Reports are in [evals/results/](../evals/results/).

Claude Haiku and GPT-6 Luna were **not measured**, since I didn't have keys. Their rows above are from the providers' docs only.

The eval also changed my decisions. My first prompt missed "Dubai" as a city, left "NYC" unexpanded, and called founders "businesses". I fixed the prompt (v2 → v3), and Gemini went from 97.5% to 100%. I had also planned Groq as the main model. The evals showed it's nearly as good at reading the query (99%) but clearly weaker at relevance (83.9% vs 91.9%), and relevance decides what the user sees. So Gemini is primary and Groq is the fallback.

## C. How I keep cost down

1. Code first: no LLM for anything a rule can decide.
2. Two calls max; relevance is **one batched call**, not one per candidate.
3. A keyword pre-filter drops obvious misses before the LLM.
4. Intent and search results are cached, so a repeated query is almost free.
5. Only title + snippet are sent, never whole pages.
6. Stable system prompt first, so providers can cache it.
7. Low reasoning effort, strict JSON output, no prose.

## D. Final recommendation

| Item | Value |
|---|---|
| Recommended now | **Gemini 3.5 Flash-Lite** (measured best accuracy, free key) |
| Fallback | Groq gpt-oss-20b (different provider, fastest, cheapest) |
| Tokens per request | ~4,500 in / ~1,050 out (measured) |
| Cost per request | ~$0.0025 measured (intent ~$0.001 + relevance ~$0.0015) |
| Expected LLM time | ~1.5 s intent + ~2 s relevance |
| Next to evaluate | Claude Haiku 5.5: a third of Gemini's per-token price and strict JSON. I'd switch only if it scores at least as well on the same evals |

Switching is one line in `.env` (`LLM_MODEL=anthropic/claude-haiku-5-5`), because every call goes through LiteLLM.
