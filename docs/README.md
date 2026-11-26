# Docs

Short notes on how I built this and why. Each file is one topic and should take a few minutes to read.

| # | File | What's in it |
|---|---|---|
| 0 | [REQUIREMENTS.md](REQUIREMENTS.md) | The original brief from Dxora, unchanged |
| 1 | [01-architecture.md](01-architecture.md) | The big picture in one diagram |
| 2 | [02-request-flow.md](02-request-flow.md) | What happens during one request, step by step |
| 3 | [03-platforms.md](03-platforms.md) | How each of the 9 platforms is reached, and the limits |
| 4 | [04-relevance-and-duplicate-rules.md](04-relevance-and-duplicate-rules.md) | How a candidate is judged relevant, and when two profiles are merged |
| 5 | [05-llm-choice.md](05-llm-choice.md) | LLM comparison, measured results, cost |
| 6 | [06-reliability-and-safety.md](06-reliability-and-safety.md) | Failures, retries, guardrails, security |
| 7 | [07-deploy.md](07-deploy.md) | Running locally and on Vercel |
| 8 | [08-known-limits.md](08-known-limits.md) | What doesn't work well yet, honestly |

Real outputs for the five test queries are in [samples/](samples/), and screenshots in [screenshots/](screenshots/).
