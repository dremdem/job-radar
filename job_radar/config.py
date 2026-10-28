from datetime import date
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
    linkedin_public_max_descriptions_per_day: int = 100
    # Telegram via the existing telegram-mcp server (docs/telegram.md); off without a command.
    telegram_mcp_command: str | None = None
    telegram_sources: Path = Path("data/telegram.yaml")
    telegram_first_run_days: int = 14
    # Himalayas public API (docs/himalayas.md): official, no key; the first run looks back a day.
    himalayas: bool = True
    himalayas_first_run_days: float = 1
    # "Ask HN: Who is hiring?" via the HN Search API (docs/hackernews.md): public, no key.
    hackernews: bool = True
    # Remote OK public JSON feed (docs/remoteok.md): no key, one request per sync.
    remoteok: bool = True
    # hh.ru digests from the remote saved search, received on or after this date (docs/hh.md).
    hh_digests_since: date | None = None
    # Description analysis with a local model through Ollama (docs/ai.md); opt-in since #223.
    ai_analysis: bool = False
    ollama_url: str = "http://localhost:11434"
    ai_model: str = "gemma3:4b"
    ai_max_per_sync: int = 150
    profile: Path = Path("data/profile.yaml")  # your stack from the CV
    # Deep analysis with Claude Code in print mode, on the subscription (docs/claude.md).
    cv_path: Path | None = None
    claude_command: str = "claude"
    claude_worker: bool = True  # false on test servers over a DB copy: never spend limits twice
    claude_workdir: Path = Path("data/claude")  # its sessions, apart from your own
    # Local-only diagnostics of bookmarklet captures (git-ignored), see docs/linkedin-capture.md.
    capture_debug_dir: Path = Path("data/capture_debug")


@lru_cache
def get_settings() -> Settings:
    return Settings()
