# Telegram channels

Job Radar reads vacancies from Telegram channels chosen in the audit (#84). It does **not** have its own Telegram client: it asks the existing [telegram-mcp](https://github.com/jgalea/telegram-mcp) server, which owns the login session (one shared daemon), and calls **only `read_messages`**. Nothing is ever sent, marked read, joined or changed.

## Setup

1. telegram-mcp is installed and logged in (`telegram-mcp login`, once, in a terminal).
2. Tell Job Radar how to start its server, in `.env`:
   ```sh
   JOB_RADAR_TELEGRAM_MCP_COMMAND="uv run --directory /home/you/work/pet/tg-mcp/src telegram-mcp serve"
   ```
   Without it the Telegram step is skipped.
3. Copy `telegram.example.yaml` to `data/telegram.yaml` (git-ignored) and adjust the channels.

## Channel list

| Key | Meaning |
|---|---|
| `name` | Short name, shown on vacancies and used in their ids |
| `chat` | `@username` / `username`, or the numeric id for channels without a username |
| `topics` | Forum supergroups: read only these topic ids |
| `require_tags` | The post must contain one of these tags (e.g. `#vacancy`) |
| `keywords` | Regex the post text must match. Default: Python, Django, FastAPI, Flask |
| `title_keywords` | Regex the vacancy title must match (the role). Default: engineer, developer, разработчик, programmer, architect, DevOps/SRE, ML/AI, (tech/team) lead, CTO, backend, Python |
| `exclude_title` | Regex that rejects a title. Default: analyst, manager, QA/test, recruiter, designer, sales, marketing, support, intern, digests ("дайджест", "подборка") |
| `parse` | `post` (default): one post = one vacancy. `graph_org_digest`: Digital nomads digests (#86, not read yet) |

## How a sync works

- For every channel (and topic) the newest posts are read page by page until the last post already stored; the first run looks back **14 days**.
- Every post is stored in `telegram_posts` once (`chat + message id`), whether it became a vacancy or not, so nothing is read twice.
- A post becomes a vacancy when it passes `require_tags`, `keywords`, and its title passes `title_keywords` and not `exclude_title`. The rest is kept as `skipped` (ads, other professions, digests, CVs). Measured on 14 days of the real channels: without the title filters 94 of 587 posts became vacancies, mostly analysts, QA and Go/.NET backends.
- From the post: **title** (first line, or `Looking for:`), **company** ("Title **в** Company", "Title **at** Company" — only on a short title line with a name-like company, "Company:"; otherwise a per-post placeholder `Unknown (<channel> <id>)`, so unrelated posts never share a company history), **location** (the `📍` line or `Location:`), links. The whole post text is the vacancy's **description**, so the Python check, language check and auto-ignore rules work right away.
- The vacancy links to the post (`https://t.me/<channel>/<id>`, or `t.me/c/<id>/<post>` for private channels) and has the source `telegram`.

Runs as part of **Sync mail** / `./jr sync` when configured, or alone: `uv run job-radar sync telegram`.
