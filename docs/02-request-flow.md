# 2. What happens in one request

The brief suggests a 13-step flow (§7). I kept its order.

```mermaid
sequenceDiagram
    participant U as User
    participant API as FastAPI
    participant LLM as LLM (Gemini)
    participant S as Search (Serper / YouTube)
    participant W as Prospect websites

    U->>API: POST {"query": "Startup founders in London UK"}
    API->>API: 1-2. validate + guardrails
    API->>LLM: 3-5. understand query, write a search phrase per platform
    LLM-->>API: intent JSON (cached for 24h)
    par all 7 platforms at once
        API->>S: 6. site: searches + YouTube API
    end
    S-->>API: titles, snippets, URLs
    API->>API: 7. keep only real profile URLs, name from page title
    API->>W: 8. read prospects' own sites for email/phone (robots.txt respected)
    API->>LLM: 9. one batched call: is each candidate relevant? quote the proof
    LLM-->>API: verdicts + quotes (checked against the text)
    API->>API: 10. merge profiles of the same person
    API->>API: 11-12. build JSON, validate with Pydantic
    API-->>U: 13. {success, query, total_prospects, prospects}
```

## The steps in plain words

| Step | What it does | File |
|---|---|---|
| 1–2 | Rejects empty/long queries and queries targeting one named person or sensitive groups | `query_service.py`, `validators.py` |
| 3–5 | One LLM call returns interest, audience, location, and a search phrase for each platform | `query_service.py` |
| 6 | Runs all platforms concurrently; a failing platform only marks itself as failed | `search_service.py` |
| 7 | Keeps profile URLs only (no posts or articles); the name comes from the page title | `profile_extractor.py` |
| 8 | Reads up to 5 prospect websites for contacts and their own social links | `profile_extractor.py` |
| 9 | Keyword pre-filter, then one LLM call judges every candidate with quotes | `relevance_service.py` |
| 10 | Merges candidates that are clearly the same person or business | `deduplication_service.py` |
| 11–13 | Builds and validates the response | `prospect_service.py`, `main.py` |

## Time budget

The whole request has 30 seconds. Every step gets what's left, so a slow platform can't make the request hang. In my runs a request takes 8–17 seconds.

## When things fail

```mermaid
flowchart LR
    A[Platform fails] --> B[marked failed in debug output,<br/>others continue] --> C[200 with partial results]
    D[Primary LLM fails] --> E[Groq fallback] --> F{works?}
    F -->|yes| C
    F -->|no, at step 3| G[503 + Retry-After]
    F -->|no, at step 9| H[keyword-only verdicts,<br/>all marked partial]
```
