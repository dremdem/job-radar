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
| `parse` | `post` (default): one post = one vacancy. `graph_org_digest`: a daily digest whose categories link to job lists (Digital nomads) |
| `categories` | For `graph_org_digest`: which digest categories to open, by name (`Back-end, Python`, …) |

## How a sync works

- For every channel (and topic) the newest posts are read page by page until the last post already stored; the first run looks back **14 days**.
- Every post is stored in `telegram_posts` once (`chat + message id`), whether it became a vacancy or not, so nothing is read twice.
- A post becomes a vacancy when it passes `require_tags`, `keywords`, and its title passes `title_keywords` and not `exclude_title`. The rest is kept as `skipped` (ads, other professions, digests, CVs). Measured on 14 days of the real channels: without the title filters 94 of 587 posts became vacancies, mostly analysts, QA and Go/.NET backends.
- From the post: **title** (first line, or `Looking for:`), **company** ("Title **в** Company", "Title **at** Company" — only on a short title line with a name-like company, "Company:"; otherwise a per-post placeholder `Unknown (<channel> <id>)`, so unrelated posts never share a company history), **location** (the `📍` line or `Location:`), links. The whole post text is the vacancy's **description**, so the Python check, language check and auto-ignore rules work right away.
- The vacancy links to the post (`https://t.me/<channel>/<id>`, or `t.me/c/<id>/<post>` for private channels) and has the source `telegram`.

Runs as part of **Sync** / `./jr sync` when configured, or alone: `uv run job-radar sync telegram`.

## Digests (Digital nomads)

The channel posts one digest a day, *"100% remote jobs from the last 24 hours"*: a table of contents whose categories (`Back-end, Python - 4`, …) link to `graph.org` pages. Only the `categories` from the channel list are opened.

1. A **category page** (read through the public Telegraph API, `api.telegra.ph/getPage/<path>`, no login) lists vacancies grouped by where they were found (HeadHunter, LinkedIn, …): *"Title at Company"*, each linking to its own page.
2. A **vacancy page** has the channel's note on why it is 100 % remote, the description and the link to the original posting (hh.ru, LinkedIn, a company ATS).
3. The vacancy gets title and company from the list, location `Remote` (the channel only publishes fully remote jobs; its remote note is kept in the description), and the page text as its description. The same `keywords` and title filters apply, checked against title + description.

**Overlap with LinkedIn**: when the original link is a LinkedIn job (`linkedin.com/jobs/view/<id>`) already known from alerts, search or the bookmarklet, no second vacancy is created; the known one gets the digest's description if it had none. A new LinkedIn-linked vacancy also records the LinkedIn job id, so a later alert does not duplicate it.

Each vacancy page is read once (dedup by its page); the digest post itself is stored with status `digest`. Telegraph requests are paced (0.3 s) and capped at 300 vacancy pages per sync.
