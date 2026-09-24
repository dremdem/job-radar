from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JOB_RADAR_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///data/job_radar.db"
    gmail_client_secret: Path = Path("secrets/gmail_client_secret.json")
    gmail_token: Path = Path("secrets/gmail_token.json")


@lru_cache
def get_settings() -> Settings:
    return Settings()
