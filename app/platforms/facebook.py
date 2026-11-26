"""Facebook public profiles and pages via `site:facebook.com` search.

Page search needs App Review (Page Public Metadata Access) and people search was removed in 2018."""

from app.platforms.base import SiteSearchPlatform


class Facebook(SiteSearchPlatform):
    name = "Facebook"
    sites = ("facebook.com",)
