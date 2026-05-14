"""Padrino application settings loaded from environment variables."""

from __future__ import annotations

import functools

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed configuration for Padrino, loaded from .env and environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM provider credentials (optional so the engine works without real keys)
    cerebras_api_key: str | None = None
    deepinfra_api_key: str | None = None

    # Database
    padrino_db_url: str = "sqlite+aiosqlite:///./padrino.db"

    # Logging
    padrino_log_level: str = "INFO"

    # LLM inference
    padrino_llm_timeout_seconds: int = 45
    padrino_temperature: float = 0.7
    padrino_top_p: float = 1.0

    # Model routing
    padrino_primary_model: str = "cerebras/zai-glm-4.7"
    padrino_fallback_model: str = "deepinfra/deepseek-ai/DeepSeek-V4-Flash"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton."""
    return Settings()
