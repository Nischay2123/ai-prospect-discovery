"""Brief §7 step 10: identify and consolidate profiles that belong to the same prospect.

Union-find over candidates. Two candidates merge only on a STRONG identity signal:
  1. same normalised profile URL
  2. same email address, or same phone number (E.164) AND a shared distinctive name word
  3. one profile's published text/website links to the other profile (websites only keep links that look like
     their own accounts; see profile_extractor)
  4. same handle (4+ chars, case-insensitive) on two platforms (a website's handle is its domain brand:
     hunterdigital.com -> "hunterdigital"), AND a shared distinctive name word, or one profile's name is the
     handle itself
  5. one handle extends the other (hunterdigital / hunterdigitalagency, 6+ chars) AND a shared distinctive name word
Never on name alone, or name + city: a wrong merge would mix two people's contact details, which is worse than
showing the same person twice.
"""

from collections import Counter
from itertools import combinations

import logfire

from app.schemas import CandidateProfile, ProspectCluster
from app.utils.normalizers import handle_belongs, name_tokens, squash


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        self.parent[rb] = ra
        return True


def consolidate(candidates: list[CandidateProfile]) -> list[ProspectCluster]:
    with logfire.span("dedup") as span:
        uf = _UnionFind(len(candidates))
        reasons: dict[int, list[str]] = {}
        merges: Counter[str] = Counter()

        def link(a: int, b: int, reason: str) -> None:
            if uf.union(a, b):
                merges[reason] += 1
                reasons.setdefault(a, []).append(f"{reason}: {candidates[a].url} = {candidates[b].url}")

        tokens = [name_tokens(c.name) for c in candidates]
        by_key: dict[str, list[int]] = {}
        for i, c in enumerate(candidates):
            keys = [("url", c.url)] + [(contact.kind.lower(), contact.value.lower()) for contact in c.contacts
                                       if contact.kind in ("Email", "Phone")]
            for kind, value in keys:
                for j in by_key.setdefault(f"{kind}:{value}", []):
                    if kind == "phone" and not tokens[i] & tokens[j]:
                        # A shared number alone isn't enough: one switchboard can serve a whole mall or a group
                        # of businesses. Same phone AND a shared name word does merge.
                        continue
                    link(j, i, {"url": "same_url", "email": "same_email", "phone": "same_phone_and_name"}[kind])
                by_key[f"{kind}:{value}"].append(i)

        url_index = {c.url: i for i, c in enumerate(candidates)}
        for i, c in enumerate(candidates):
            for url in c.linked_urls:
                if url in url_index:
                    link(i, url_index[url], "cross_link")

        handles = [squash(c.handle) for c in candidates]
        for i, j in combinations(range(len(candidates)), 2):
            a, b = candidates[i], candidates[j]
            hi, hj = handles[i], handles[j]
            if a.platform == b.platform or len(hi) < 4 or len(hj) < 4:
                continue
            shared_word = bool(tokens[i] & tokens[j])
            # Names differ but one profile's "name" IS the handle (e.g. "copifycom") and the handle carries a word of
            # the other name ("Copify Digital Marketing Agency"): same brand.
            name_is_handle = (squash(a.name) == hi and handle_belongs(hi, None, b.name)) or \
                (squash(b.name) == hj and handle_belongs(hj, None, a.name))
            if hi == hj and (shared_word or name_is_handle):
                link(i, j, "same_handle")
            elif min(len(hi), len(hj)) >= 6 and (hi.startswith(hj) or hj.startswith(hi)) and shared_word:
                link(i, j, "similar_handle_and_name")

        groups: dict[int, list[int]] = {}
        for i in range(len(candidates)):
            groups.setdefault(uf.find(i), []).append(i)
        clusters = [
            ProspectCluster(id=f"p{n}", candidates=[candidates[i] for i in members],
                            merge_reasons=[r for i in members for r in reasons.get(i, [])])
            for n, members in enumerate(groups.values())
        ]
        span.set_attributes({"candidates": len(candidates), "clusters": len(clusters),
                             "merges_by_reason": dict(merges)})
    return clusters
