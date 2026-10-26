from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://memory:memory_dev_password@db:5432/memory"
    git_repo_path: str = "/data/git-repo"

    auth_mode: Literal["dev", "oidc"] = "dev"
    dev_bearer_token: str = "dev-local-token"

    # Gate for the web UI, checked in the app itself rather than only in the proxy.
    # Defaults to on so that a deployment which forgets the variable is closed, not
    # open; local development without a proxy sets it to false.
    web_auth_required: bool = True

    oidc_introspection_url: str = ""
    oidc_client_id: str = "memory-service"
    oidc_client_secret: str = ""

    embedding_model_name: str = "intfloat/multilingual-e5-small"
    embedding_dim: int = 384

    # Recency boost in search: final score = rrf * (1 + weight * 0.5 ** (age_days / half_life)).
    # With 0.15, a today-entry can overtake a year-old one up to ~9 ranks above it within the
    # same signal (adjacent RRF ranks differ by ~1.6%), but never a hit that fulltext and vector
    # both agree on, which scores ~2x a single-signal hit.
    # Weight 0 disables it.
    search_recency_weight: float = 0.15
    search_recency_half_life_days: float = 90.0

    upload_max_bytes: int = 10 * 1024 * 1024  # 10 MiB


@lru_cache
def get_settings() -> Settings:
    return Settings()
