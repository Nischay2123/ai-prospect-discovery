"""Settings from environment variables (and .env if present). Invalid values fail fast at startup."""

import os
import tempfile
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

LLM_KEY_NAMES = ("GROQ_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
ALL_PLATFORMS = ("Google", "LinkedIn", "X", "YouTube", "Instagram", "Facebook", "Threads")


def env(name: str, default: str = "") -> str:
    """Blank values count as unset, so `KEY=` in .env falls back to the default."""
    return os.getenv(name, "").strip() or default


def env_float(name: str, default: float) -> float:
    raw = env(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None
    if value < 0:
        raise ValueError(f"{name} must not be negative, got {raw!r}")
    return value


def env_list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = env(name)
    return tuple(part.strip() for part in raw.split(",") if part.strip()) if raw else default


@dataclass(frozen=True)
class Settings:
    # LLM gateway: any LiteLLM model string. Defaults use providers with free API keys.
    llm_model: str = "gemini/gemini-3.5-flash-lite"
    llm_fallback_model: str = "groq/openai/gpt-oss-20b"
    llm_timeout: float = 6.0
    llm_num_retries: int = 0  # the fallback model is the retry

    serper_api_key: str = ""
    brave_api_key: str = ""
    youtube_api_key: str = ""

    enabled_platforms: tuple[str, ...] = ALL_PLATFORMS
    request_deadline: float = 30.0
    platform_timeout: float = 12.0
    client_rate_limit_per_minute: float = 10.0
    api_key: str = ""  # empty = endpoint open (local dev); set it on any public deployment

    db_path: str = "data/prospects.db"
    retention_days: float = 7.0
    llm_keys: dict[str, str] = field(default_factory=dict)  # names of configured provider keys (values not kept)


def db_path() -> str:
    """Serverless (Vercel) has a read-only filesystem except the temp dir, so there the DB always goes to /tmp
    (per instance), even if a DB_PATH copied from a local .env points somewhere else."""
    configured = env("DB_PATH")
    if env("VERCEL"):
        tmp = tempfile.gettempdir()
        return configured if configured and os.path.abspath(configured).startswith(tmp) else \
            os.path.join(tmp, "prospects.db")
    return configured or "data/prospects.db"


def load_settings() -> Settings:
    unknown = set(env_list("ENABLED_PLATFORMS", ALL_PLATFORMS)) - set(ALL_PLATFORMS)
    if unknown:
        raise ValueError(f"ENABLED_PLATFORMS has unknown platforms: {sorted(unknown)}; allowed: {ALL_PLATFORMS}")
    return Settings(
        llm_model=env("LLM_MODEL", Settings.llm_model),
        llm_fallback_model=env("LLM_FALLBACK_MODEL", Settings.llm_fallback_model),
        llm_timeout=env_float("LLM_TIMEOUT_SECONDS", 6.0) or 6.0,
        llm_num_retries=int(env_float("LLM_NUM_RETRIES", 0)),
        serper_api_key=env("SERPER_API_KEY"),
        brave_api_key=env("BRAVE_API_KEY"),
        youtube_api_key=env("YOUTUBE_API_KEY"),
        enabled_platforms=env_list("ENABLED_PLATFORMS", ALL_PLATFORMS),
        request_deadline=env_float("REQUEST_DEADLINE_SECONDS", 30.0) or 30.0,
        platform_timeout=env_float("PLATFORM_TIMEOUT_SECONDS", 12.0) or 12.0,
        client_rate_limit_per_minute=env_float("CLIENT_RATE_LIMIT_PER_MINUTE", 10.0),
        api_key=env("API_KEY"),
        db_path=db_path(),
        retention_days=env_float("RETENTION_DAYS", 7.0),
        llm_keys={k: "set" for k in LLM_KEY_NAMES if env(k)},
    )


settings = load_settings()
