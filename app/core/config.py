from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KASU_", env_file=".env", extra="ignore")

    env: str = "development"
    database_url: str = "postgresql+psycopg://kasu:kasu@127.0.0.1:5432/kasu"
    test_database_url: str = "postgresql+psycopg://kasu:kasu@127.0.0.1:5432/kasu_test"

    jwt_secret: str = "dev-insecure-secret-change-me-before-deploying"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 30
    refresh_token_days: int = 30

    upload_dir: Path = Path("./var/uploads")
    max_upload_mb: int = 20

    default_currency: str = "NGN"

    @property
    def is_production(self) -> bool:
        return self.env == "production"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.is_production and settings.jwt_secret.startswith("dev-"):
        raise RuntimeError("KASU_JWT_SECRET must be set in production")
    return settings
