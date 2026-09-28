from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="JOB_RADAR_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///data/job_radar.db"
    gmail_client_secret: Path = Path("secrets/gmail_client_secret.json")
    gmail_token: Path = Path("secrets/gmail_token.json")
    # Opt-in public LinkedIn guest search, see docs/linkedin-public-search.md.
    linkedin_public_search: bool = False
    linkedin_public_max_searches_per_day: int = 5
    linkedin_public_max_jobs: int = 25
    linkedin_public_delay_seconds: float = 3.0
    linkedin_public_max_descriptions_per_day: int = 100
    # Local-only diagnostics of bookmarklet captures (git-ignored), see docs/linkedin-capture.md.
    capture_debug_dir: Path = Path("data/capture_debug")


@lru_cache
def get_settings() -> Settings:
    return Settings()
