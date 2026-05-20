"""Application configuration loaded from environment variables / .env file."""
from __future__ import annotations
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "sqlite:///dynasty.db"

    # Paid sources — fill in if/when you have access
    fantasypros_api_key: str | None = None
    pff_api_key: str | None = None

    # HTTP defaults
    request_timeout_seconds: int = 30
    user_agent: str = "DynastyFootballModel/0.3 (open-source dynasty FF aggregator; https://github.com/pstiehl/Dynasty-Football-Model)"


settings = Settings()
