"""X public profiles via `site:x.com` search.

The X API v2 has no free tier (pay-per-use, $0.010 per user read), so search is the default route."""

from app.platforms.base import SiteSearchPlatform


class X(SiteSearchPlatform):
    name = "X"
    sites = ("x.com",)
