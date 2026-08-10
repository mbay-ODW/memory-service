from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://memory:memory_dev_password@db:5432/memory"
    git_repo_path: str = "/data/git-repo"

    auth_mode: Literal["dev", "oidc"] = "dev"
    dev_bearer_token: str = "dev-local-token"

    oidc_introspection_url: str = ""
    oidc_client_id: str = "memory-service"
    oidc_client_secret: str = ""

    embedding_model_name: str = "intfloat/multilingual-e5-small"
    embedding_dim: int = 384


@lru_cache
def get_settings() -> Settings:
    return Settings()
