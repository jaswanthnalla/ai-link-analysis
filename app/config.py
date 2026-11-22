"""Application configuration read from environment variables (and an optional .env file)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime settings. Values come from environment variables or a local ``.env`` file."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM provider credentials. Never logged, never committed.
    google_api_key: str = ""

    # Primary model and the model used only when the primary call fails.
    llm_primary_model: str = "gemini-3-flash-preview"
    llm_fallback_model: str = "gemini-3.5-flash"
    llm_reasoning_effort: str = "medium"
    llm_timeout_seconds: float = 120.0
    llm_max_output_tokens: int = 16000

    # Website fetching limits.
    fetch_timeout_seconds: float = 15.0
    fetch_max_bytes: int = 2_000_000
    fetch_max_redirects: int = 5
    user_agent: str = "Mozilla/5.0 (compatible; ai-link-analysis/1.0; +https://github.com/)"

    # Cleaned website text is truncated to this many characters before the LLM call.
    content_max_chars: int = 20_000


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()
