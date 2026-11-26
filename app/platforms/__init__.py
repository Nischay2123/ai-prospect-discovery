"""Platform registry: discovery runs on every enabled platform listed here (selection is internal)."""

from app.config import Settings
from app.platforms.base import Platform
from app.platforms.facebook import Facebook
from app.platforms.google import Google
from app.platforms.instagram import Instagram
from app.platforms.linkedin import LinkedIn
from app.platforms.threads import Threads
from app.platforms.x_platform import X
from app.platforms.youtube import YouTube
from app.services.resilience import Guard
from app.services.search_service import FailoverSearch


def build_platforms(settings: Settings, search: FailoverSearch, guard: Guard) -> list[Platform]:
    """Create the adapters. Add a new platform here (plus its module and env vars)."""
    every: list[Platform] = [
        Google(search), LinkedIn(search), X(search), Instagram(search), Facebook(search), Threads(search),
        YouTube(api_key=settings.youtube_api_key, guard=guard),
    ]
    return [p for p in every if p.name in settings.enabled_platforms]


__all__ = ["Platform", "build_platforms"]
