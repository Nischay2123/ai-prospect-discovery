"""Threads public profiles via search. Both domains are in use, so both are searched.

The Threads keyword-search and profile-lookup APIs need App Review / advanced access."""

from app.platforms.base import SiteSearchPlatform


class Threads(SiteSearchPlatform):
    name = "Threads"
    sites = ("threads.net", "threads.com")
