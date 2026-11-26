"""LinkedIn public profiles via `site:linkedin.com/in` search.

LinkedIn has no people-search API for third parties and its terms forbid scraping, so the app never fetches
linkedin.com: name and headline come only from the search result."""

from app.platforms.base import SiteSearchPlatform
from app.schemas import QueryIntent


class LinkedIn(SiteSearchPlatform):
    name = "LinkedIn"
    sites = ("linkedin.com/in",)

    def sites_for(self, intent: QueryIntent | None) -> tuple[str, ...]:
        """Businesses have company pages; their owners have personal profiles. Search both for business queries."""
        if intent is not None and intent.target_type == "business":
            return ("linkedin.com/company", "linkedin.com/in")
        return self.sites
