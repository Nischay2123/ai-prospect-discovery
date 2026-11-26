# 8. Known limits

What I'd tell a reviewer before they find it.

| Limit | Why | What would fix it |
|---|---|---|
| Some queries return few prospects (London and Ahmedabad: about 4–6) | Serper often returns posts, news or unverifiable redirect links, which I drop. A second, differently worded search already runs when the first finds fewer than 3 profiles | A Brave key (direct URLs; already wired in) |
| Results change between runs | Search results and the LLM's judgments vary | Saving and replaying fixtures for demos |
| WhatsApp links are rare (none in the latest samples) | Only published `wa.me` links count. I read each prospect's homepage and its contact page, but few publish one | Reading more pages per site |
| Few profiles get merged | Search snippets rarely share an email, phone or link | More sources per prospect (e.g. reading linked bios) |
| Location uses the profile's own wording ("Dubai Marina – Dubai – UAE", "NYC") | I only trim it to start at the place name; I don't rewrite it | Normalising to a standard city/region format |
| On Vercel, storage, caches and limits are per instance | Serverless has no shared disk or memory | Postgres + Redis |
| Claude Haiku / GPT-6 Luna not measured | No keys | Run the same evals with those keys |
| A LinkedIn name can differ from its URL slug ("Andrea Shaw" on `/in/andreabertola`) | Nicknames, married names and old slugs make a reliable automatic check hard, so I don't flag it | Showing the slug in debug output for a human to judge |
| Eval sets are still small (20 intent, 31 relevance, 10 dedup) | Built by hand within the time limit | More labelled cases |
