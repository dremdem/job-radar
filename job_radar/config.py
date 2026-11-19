"""Application settings read from the environment and ``.env``.

``Settings`` is a pydantic-settings model: every field can be set with a ``JOB_RADAR_``
variable (``JOB_RADAR_DATABASE_URL``, ``JOB_RADAR_FDETIMES`` and so on). Opt-in sources
are off by default. ``get_settings`` returns one cached instance for the web app; the CLI
creates its own. See .env.example and docs/usage.md.

Usage:
    import job_radar.config as config
"""

import datetime as dt
import functools
import pathlib

import pydantic
import pydantic_settings


class Settings(pydantic_settings.BaseSettings):
    """All settings of the app; each field comes from a ``JOB_RADAR_<FIELD>`` variable."""

    model_config = pydantic_settings.SettingsConfigDict(
        env_prefix="JOB_RADAR_", env_file=".env", extra="ignore"
    )

    database_url: str = "sqlite:///data/job_radar.db"
    gmail_client_secret: pathlib.Path = pathlib.Path("secrets/gmail_client_secret.json")
    gmail_token: pathlib.Path = pathlib.Path("secrets/gmail_token.json")
    # Opt-in public LinkedIn guest search, see docs/linkedin-public-search.md.
    linkedin_public_search: bool = False
    linkedin_public_max_searches_per_day: int = 5
    linkedin_public_max_jobs: int = 25
    linkedin_public_max_descriptions_per_day: int = 100
    # Chrome fill (#247): descriptions read in your own Chrome through Playwright MCP's
    # extension; opt-in, see docs/linkedin-chrome-fill.md and docs/adr/0001.
    linkedin_chrome: bool = False
    linkedin_chrome_command: str = "npx -y @playwright/mcp@0.0.84 --extension"
    linkedin_chrome_max_per_run: int = 50
    # Shown by the Playwright extension; without it every run asks you to allow the connection.
    playwright_mcp_extension_token: str | None = pydantic.Field(
        default=None, validation_alias="PLAYWRIGHT_MCP_EXTENSION_TOKEN"
    )
    # Telegram via the existing telegram-mcp server (docs/telegram.md); off without a command.
    telegram_mcp_command: str | None = None
    telegram_sources: pathlib.Path = pathlib.Path("data/telegram.yaml")
    telegram_first_run_days: int = 14
    # Himalayas public API (docs/himalayas.md): official, no key; the first run looks back a day.
    himalayas: bool = True
    himalayas_first_run_days: float = 1
    # "Ask HN: Who is hiring?" via the HN Search API (docs/hackernews.md): public, no key.
    hackernews: bool = True
    # Remote OK public JSON feed (docs/remoteok.md): no key, one request per sync.
    remoteok: bool = True
    # Jobicy public API (docs/jobicy.md): no key; the first run looks back a day.
    jobicy: bool = True
    # FDE Times public job list (docs/fdetimes.md): plain HTML, one request per sync; the
    # descriptions come from the role pages in the Descriptions job.
    fdetimes: bool = True
    fdetimes_first_run_days: float = 7
    # also the related AI roles, not only FDE ones
    fdetimes_related: bool = False
    # e.g. "remote,apac"; empty: every region
    fdetimes_regions: str = ""
    # role pages per Descriptions run
    fdetimes_max_descriptions: int = 50
    # Original postings of LinkedIn alert vacancies on the employer's ATS board through the
    # official public APIs of Greenhouse, Lever, Ashby, Workable (#2, docs/ats.md).
    ats: bool = True
    # try slugs derived from the company name when no board link is known
    ats_guess: bool = True
    # requests per run; the rest waits for the next run
    ats_max_requests: int = 60
    # Remotive, Working Nomads, We Work Remotely public feeds (docs/boards.md).
    boards: bool = True
    # hh.ru digests from the remote saved search, received on or after this date (docs/hh.md).
    hh_digests_since: dt.date | None = None
    # Description analysis with a local model through Ollama (docs/ai.md); opt-in since #223.
    ai_analysis: bool = False
    ollama_url: str = "http://localhost:11434"
    ai_model: str = "gemma3:4b"
    ai_max_per_sync: int = 150
    # your stack from the CV
    profile: pathlib.Path = pathlib.Path("data/profile.yaml")
    # Deep analysis with Claude Code in print mode, on the subscription (docs/claude.md).
    cv_path: pathlib.Path | None = None
    claude_command: str = "claude"
    # false on test servers over a DB copy: never spend limits twice
    claude_worker: bool = True
    # its sessions, apart from your own
    claude_workdir: pathlib.Path = pathlib.Path("data/claude")
    # Local-only diagnostics of bookmarklet captures (git-ignored), see docs/linkedin-capture.md.
    capture_debug_dir: pathlib.Path = pathlib.Path("data/capture_debug")


@functools.lru_cache
def get_settings() -> Settings:
    return Settings()
