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

    upload_max_bytes: int = 10 * 1024 * 1024  # 10 MiB


@lru_cache
def get_settings() -> Settings:
    return Settings()
