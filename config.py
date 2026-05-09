"""
AI Technical Support Agent — Configuration
==========================================
Loads all environment variables from a `.env` file via pydantic-settings.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application-wide settings loaded from the environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Gmail ---------------------------------------------------------------
    gmail_credentials_path: str = "credentials.json"
    gmail_token_path: str = "token.json"
    gmail_support_address: str = "support@company.com"
    gmail_poll_interval_seconds: int = 60

    # --- External services (placeholders) ------------------------------------
    anthropic_api_key: str = ""
    github_token: str = ""

    # --- Persistence ---------------------------------------------------------
    database_path: str = "./support_agent.db"

    # --- Observability -------------------------------------------------------
    log_level: str = "INFO"


# Module-level singleton — import this everywhere instead of re-instantiating.
settings = Settings()
