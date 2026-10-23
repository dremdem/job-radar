# Job Radar

Local-first assistant for reviewing job opportunities: collects vacancies (LinkedIn alert emails, Gmail history, hh.ru notifications, Telegram channels, Himalayas), deduplicates them, scores with explanations and keeps application history per company.

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

How the queue works (tabs, buttons, **Sync** button): [docs/queue.md](docs/queue.md). Deep analysis of chosen vacancies with Claude: [docs/claude.md](docs/claude.md). Which missing technologies would open the most vacancies: [docs/skills.md](docs/skills.md). Auto-ignore rules (hybrid, already applied, …): [docs/settings.md](docs/settings.md). Send LinkedIn pages you open to the queue with the bookmarklet: [docs/linkedin-capture.md](docs/linkedin-capture.md).

The queue is empty until a source is imported. To look around first: `uv run job-radar demo`.

## Everyday commands: `./jr`

A small wrapper so you do not have to remember the commands below:

| Command | What it does |
|---|---|
| `./jr start` | Start the web UI in the background (log `data/server.log`) and open http://127.0.0.1:8000 |
| `./jr stop` / `./jr restart` | Stop / restart it |
| `./jr status` | Is it running, on which address |
| `./jr logs` | Follow the server log |
| `./jr sync` | Sync every source: Gmail, LinkedIn, Telegram, Himalayas, AI analysis (same as the **Sync** button) |
| `./jr auth` | One-time Gmail consent (read-only) |
| `./jr update` | `git pull`, install dependencies, restart the server |
| `./jr check` | `ruff` + `pytest` |
| `./jr autostart on` / `off` | Start Job Radar with the machine and restart it if it crashes (a systemd **user** service, no root) / turn that off |
| `./jr unit` | Print that service file without installing it |
| `./jr help` | This list |

A second `./jr start` does not start a second server.

**Autostart.** `./jr autostart on` writes `~/.config/systemd/user/job-radar.service` and enables it. The service runs `uv run job-radar serve` in this folder on 127.0.0.1:8000, logs to `data/server.log` and uses the `PATH` of the shell you ran it from, so `claude` and `uv` are found. After that, `./jr start` / `stop` / `restart` / `status` go through `systemctl --user`. The service starts at boot without a login when *linger* is on for your user (`loginctl show-user $USER -p Linger`); otherwise it starts when you log in. Run `./jr autostart on` again after moving the folder or changing `PATH`.

## CLI

| Command | What it does |
|---|---|
| `job-radar db upgrade` | Apply Alembic migrations to the configured database |
| `job-radar serve [--host] [--port] [--reload]` | Run the web UI (review queue) |
| `job-radar demo [--remove]` | Add a few demo opportunities (companies marked `(demo)`) to try the queue; `--remove` deletes only them |
| `job-radar gmail auth` | One-time Gmail OAuth consent (read-only), see [docs/gmail.md](docs/gmail.md) |
| `job-radar sync gmail [--query NAME] [--since DATE]` | The whole sync: read-only Gmail import, then every other source and analysis (see [docs/queue.md](docs/queue.md#sync)) |
| `job-radar import linkedin` | Turn stored LinkedIn alert emails into opportunities, see [docs/linkedin-alerts.md](docs/linkedin-alerts.md) |
| `job-radar import applications` | Past applications from mail, see [docs/applications.md](docs/applications.md) |
| `job-radar import contacts` | Link mail to companies and label it, for company history in the queue |
| `job-radar import linkedin-search` | Opt-in: first 25 jobs of each new alert search from LinkedIn's public guest search, see [docs/linkedin-public-search.md](docs/linkedin-public-search.md) |
| `job-radar import descriptions` | Opt-in: descriptions for To review from public job pages, see [docs/linkedin-public-search.md](docs/linkedin-public-search.md#descriptions-public-job-page) |
| `job-radar sync telegram` | Vacancies from allowlisted Telegram channels via telegram-mcp, see [docs/telegram.md](docs/telegram.md) |
| `job-radar sync himalayas` | Fresh worldwide Python vacancies from the Himalayas public API, see [docs/himalayas.md](docs/himalayas.md) |
| `job-radar analyze` | Work mode and residence from descriptions with a local model (Ollama), see [docs/ai.md](docs/ai.md) |
| `job-radar sync <other source>` | Placeholder until the source lands |

## Configuration

Settings come from environment variables with the `JOB_RADAR_` prefix, or from `.env` (see [.env.example](.env.example)).

| Variable | Default | Meaning |
|---|---|---|
| `JOB_RADAR_DATABASE_URL` | `sqlite:///data/job_radar.db` | SQLAlchemy URL of the database |
| `JOB_RADAR_GMAIL_CLIENT_SECRET` | `secrets/gmail_client_secret.json` | OAuth Desktop client downloaded from Google Cloud |
| `JOB_RADAR_GMAIL_TOKEN` | `secrets/gmail_token.json` | Where the OAuth token is stored |
| `JOB_RADAR_TELEGRAM_MCP_COMMAND` | — | How to start the telegram-mcp server; Telegram is off without it ([docs/telegram.md](docs/telegram.md)) |
| `JOB_RADAR_HIMALAYAS` | `true` | Read Himalayas on every sync ([docs/himalayas.md](docs/himalayas.md)) |
| `JOB_RADAR_AI_ANALYSIS` | `false` | Analyze descriptions with the local model on every sync; off by default, Claude does the filtering ([docs/ai.md](docs/ai.md)) |
| `JOB_RADAR_PROFILE` | `data/profile.yaml` | Your stack from the CV; vacancies requiring other technologies are marked ([docs/ai.md](docs/ai.md#stack-vs-your-cv), example: `profile.example.yaml`) |
| `JOB_RADAR_HH_DIGESTS_SINCE` | — | Import hh.ru «Вакансии по подписке» digests received on or after this date ([docs/hh.md](docs/hh.md#digests-вакансии-по-подписке-102)) |
| `JOB_RADAR_CV_PATH` | — | Your CV (Markdown) for Claude's deep analysis ([docs/claude.md](docs/claude.md)) |
| `JOB_RADAR_CLAUDE_COMMAND` | `claude` | Claude Code CLI used in print mode, on your subscription ([docs/claude.md](docs/claude.md)) |
| `JOB_RADAR_LINKEDIN_PUBLIC_SEARCH` | `false` | Opt-in public LinkedIn search, with limits in [docs/linkedin-public-search.md](docs/linkedin-public-search.md) |

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

The same checks run in CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) on every PR. `main` is protected: the `checks` job must pass and the branch must be up to date with `main` before merging (`gh pr update-branch` if it is behind); there is no second run after the merge. Tests use fixtures only: no secrets, no Gmail, no real sleeps or network.

Tests run in parallel (`pytest-xdist`, `-n auto` in `pyproject.toml`); add `-n0` to debug a single test. Keep the CI job under a minute: GitHub bills every job rounded up to a whole minute (#201).

Schema changes: edit `job_radar/models.py`, then `uv run alembic revision --autogenerate -m "..."` and review the generated file.
