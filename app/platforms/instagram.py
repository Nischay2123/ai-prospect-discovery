"""Instagram public profiles via `site:instagram.com` search.

Graph API Business Discovery needs an exact username of a Business/Creator account; it has no keyword search."""

from app.platforms.base import SiteSearchPlatform


class Instagram(SiteSearchPlatform):
    name = "Instagram"
    sites = ("instagram.com",)
