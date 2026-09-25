# Job Radar

Local-first assistant for reviewing job opportunities: collects vacancies (LinkedIn alert emails, Gmail history, later Himalayas / Telegram), deduplicates them, scores with explanations and keeps application history per company.

Human-in-the-loop only: no auto-apply, no LinkedIn scraping.

Plan and scope: [Epic #9](https://github.com/dremdem/job-radar/issues/9). Rules for contributors and agents: [CLAUDE.md](CLAUDE.md).

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12+.

```sh
uv sync                      # install deps into .venv
cp .env.example .env         # optional: override settings
uv run job-radar db upgrade  # create / migrate the SQLite database
uv run job-radar serve       # http://127.0.0.1:8000 — review queue
```

The queue is empty until a source is imported. To look around first: `uv run job-radar demo`.

## CLI

| Command | What it does |
|---|---|
| `job-radar db upgrade` | Apply Alembic migrations to the configured database |
| `job-radar serve [--host] [--port] [--reload]` | Run the web UI (review queue) |
| `job-radar demo [--remove]` | Add a few demo opportunities (companies marked `(demo)`) to try the queue; `--remove` deletes only them |
| `job-radar gmail auth` | One-time Gmail OAuth consent (read-only), see [docs/gmail.md](docs/gmail.md) |
| `job-radar sync gmail [--query NAME] [--since DATE]` | Incremental read-only import of job mail into the database |
| `job-radar import linkedin` | Turn stored LinkedIn alert emails into opportunities, see [docs/linkedin-alerts.md](docs/linkedin-alerts.md) |
| `job-radar import applications` | Past applications from mail, see [docs/applications.md](docs/applications.md) |
| `job-radar import contacts` | Link mail to companies and label it, for company history in the queue |
| `job-radar sync <other source>` | Placeholder until the source lands |

## Configuration

Settings come from environment variables with the `JOB_RADAR_` prefix, or from `.env` (see [.env.example](.env.example)).

| Variable | Default | Meaning |
|---|---|---|
| `JOB_RADAR_DATABASE_URL` | `sqlite:///data/job_radar.db` | SQLAlchemy URL of the database |
| `JOB_RADAR_GMAIL_CLIENT_SECRET` | `secrets/gmail_client_secret.json` | OAuth Desktop client downloaded from Google Cloud |
| `JOB_RADAR_GMAIL_TOKEN` | `secrets/gmail_token.json` | Where the OAuth token is stored |

## Layout

```
job_radar/
  config.py       settings (pydantic-settings)
  db.py           engine and session factory
  models.py       SQLAlchemy models, see docs/data-model.md
  dedup.py        idempotent upserts: companies, source records, snapshots
  demo.py         demo data for `job-radar demo`
  gmail/          read-only Gmail client, OAuth, sync (docs/gmail.md)
  linkedin/       LinkedIn alert email parser and import (docs/linkedin-alerts.md)
  scoring.py      rule-based score with reasons
  web/            FastAPI app, Jinja2 + HTMX templates
  cli.py          `job-radar` entry point
  migrations/     Alembic migrations
tests/            pytest
```

## Development

```sh
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

The same checks run in CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) on every PR and on `main`. Tests use fixtures only: no secrets, no Gmail.

Schema changes: edit `job_radar/models.py`, then `uv run alembic revision --autogenerate -m "..."` and review the generated file.
