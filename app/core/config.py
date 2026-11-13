"""Typed application settings (Chapter 6: secret management).

WHY: configuration scattered over os.getenv() calls fails late and silently.
pydantic-settings reads env vars / .env once, validates types at startup and
wraps secrets in SecretStr so they never show up in logs or repr().

SECRETS: in Docker/Kubernetes, secrets are mounted as files. For any secret
field FOO, if the env var FOO_FILE points to a file, its content wins over FOO.
That keeps secrets out of `docker inspect` and process environments.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

SECRET_FIELDS = (
    "jwt_secret",
    "llm_api_key",
    "payment_client_secret",
    "crm_api_key",
    "shipping_api_key",
    "external_mcp_api_key",
    "reporting_database_url",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- app ---
    app_name: str = "Order Management Agent"
    environment: str = "local"  # local | test | production
    log_level: str = "INFO"
    cors_origins: list[str] = ["http://localhost:3000"]

    # --- database (Chapter 3). Postgres in Docker, SQLite for quick local runs ---
    database_url: str = "sqlite+aiosqlite:///./dev.db"
    db_pool_size: int = 5  # steady-state connections per process
    db_max_overflow: int = 10  # burst connections above pool_size
    db_pool_timeout: float = 10.0  # seconds to wait for a free connection
    reporting_database_url: SecretStr | None = None  # read-only role (connector)

    # --- auth ---
    jwt_secret: SecretStr = SecretStr("dev-only-change-me-dev-only-change-me")
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 30

    # --- LLM (any OpenAI-compatible endpoint: Groq, OpenAI, Ollama, vLLM...) ---
    llm_base_url: str = "https://api.groq.com/openai/v1"
    # GROQ_API_KEY accepted too, so the v0 .env keeps working.
    llm_api_key: SecretStr = Field(SecretStr(""), validation_alias=AliasChoices("LLM_API_KEY", "GROQ_API_KEY"))
    llm_model: str = "openai/gpt-oss-120b"
    agent_max_steps: int = 6

    # --- MCP ---
    mcp_server_url: str = "http://localhost:8001/mcp"  # our own order MCP server
    external_mcp_url: str = "http://localhost:8002/mcp"  # third-party logistics server
    external_mcp_api_key: SecretStr = SecretStr("logistics-dev-key")

    # --- external partners (served by mock_services in dev) ---
    payment_base_url: str = "http://localhost:9000/payment"
    payment_client_id: str = "order-service"
    payment_client_secret: SecretStr = SecretStr("payment-dev-secret")
    crm_base_url: str = "http://localhost:9000/crm"
    crm_api_key: SecretStr = SecretStr("crm-dev-key")
    shipping_base_url: str = "http://localhost:9000/shipping"
    shipping_api_key: SecretStr = SecretStr("shipping-dev-key")

    # --- connector resilience defaults ---
    http_timeout_seconds: float = 5.0
    retry_max_attempts: int = 3
    retry_base_delay: float = 0.2
    breaker_failure_threshold: int = 5
    breaker_reset_seconds: float = 30.0


def _apply_file_secrets() -> None:
    """Translate FOO_FILE=/path into FOO=<file content> before Settings loads."""
    for name in SECRET_FIELDS:
        path = os.environ.get(f"{name.upper()}_FILE")
        if path and Path(path).is_file():
            os.environ[name.upper()] = Path(path).read_text().strip()


@lru_cache
def get_settings() -> Settings:
    # lru_cache => parsed once per process; tests call get_settings.cache_clear().
    _apply_file_secrets()
    return Settings()
