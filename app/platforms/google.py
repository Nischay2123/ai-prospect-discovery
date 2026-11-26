"""Google as a discovery source: a general web search (no `site:`) for the audience and location.

Hits that are social profiles are attributed to their platform; hits that are a prospect's own website
(home/about/contact page of a non-directory domain) become a "Google" link. Generic result URLs are dropped.
"""

from app.platforms.base import SiteSearchPlatform, location_term, quoted, role_terms
from app.schemas import QueryIntent


class Google(SiteSearchPlatform):
    name = "Google"
    sites = ()

    def site_expr(self, intent=None) -> str:
        return ""

    def usable(self, hits) -> int:
        """For the general search, a prospect's own website or any social profile counts."""
        from app.utils.normalizers import is_prospect_website, profile_of
        return sum(1 for h in hits if profile_of(h.url) or is_prospect_website(h.url))

    def template(self, intent: QueryIntent) -> str:
        parts = [quoted(intent.interest), role_terms(intent), quoted(location_term(intent))]
        if intent.target_type == "business":
            parts.append("contact")
        return " ".join(p for p in parts if p)
