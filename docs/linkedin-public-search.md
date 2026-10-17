# LinkedIn public search (opt-in)

> The guest search is keyword-based and cannot reproduce LinkedIn's personal ranking; for everything you see in your own browser use the [bookmarklet](linkedin-capture.md).

An alert email lists about 6 jobs; the same search on LinkedIn shows ~25 on its first page. With this **opt-in** feature (#23) Job Radar reads that first page from LinkedIn's **public guest search**, without logging in. Off by default.

What it never does: log in, send cookies, use a browser or a browser profile, click, apply. It is still automated reading of linkedin.com and against LinkedIn's terms; the account is not involved, the IP may get throttled. See the LinkedIn rule in [CLAUDE.md](../CLAUDE.md).

## Flow

1. A new alert email arrives (`job-radar sync gmail`).
2. Its "Your job alert for …" link carries the search: `keywords`, `geoId`, `f_TPR=a<timestamp>-` (jobs since the previous alert). Only these are taken; the personal tokens in the link are ignored.
3. The guest endpoint `https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search` is requested with those parameters (`f_TPR` becomes `r<seconds since then>`, between 1 hour and 7 days), 10 cards per request, `start=0,10,20`, **first 25 jobs** only.
4. Cards become opportunities like alert jobs: dedup by LinkedIn job id across alert and search, same score. Guest cards show neither the work format nor Easy Apply, so the "No remote marker" penalty is replaced by a small "work format not shown" risk, and Easy Apply is unknown.

Each alert email is searched once. Only alerts from the last 3 days are considered, so turning the feature on does not dig through old mail.

## Safety limits

| Setting | Default | Meaning |
|---|---|---|
| `JOB_RADAR_LINKEDIN_PUBLIC_SEARCH` | `false` | Master switch |
| `JOB_RADAR_LINKEDIN_PUBLIC_MAX_SEARCHES_PER_DAY` | `5` | Alert searches per UTC day |
| `JOB_RADAR_LINKEDIN_PUBLIC_MAX_JOBS` | `25` | Jobs taken per search |

**Pause between requests (#205):** a random number of seconds between **From** and **To**, 10–40 by default, set on **Settings → LinkedIn** (stored in the database, used by searches and descriptions alike). A fixed 3 s pause got HTTP 429 after a few requests from 13 October on; a longer, uneven pause looks less like a script.

**Stop on the first sign of blocking**: HTTP 429 or 999, a redirect to login/authwall/checkpoint, or a page without job cards that mentions a challenge/captcha. The fetch is recorded as `blocked` and nothing more is requested until the next UTC day. No retries, no workarounds.

Every request is written to `linkedin_fetches` (time, search or vacancy, `start`, HTTP status, result, the reason when it is not `ok`, number of cards) and shown on the [LinkedIn log](#log) page.

## Commands

```sh
JOB_RADAR_LINKEDIN_PUBLIC_SEARCH=true uv run job-radar sync gmail   # or put it in .env
uv run job-radar import linkedin-search                               # only this step
```

## Descriptions (public job page)

With the same switch on, vacancies in **To review** that have no description yet get it from their public job page, `https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/<id>` — one guest request per vacancy, highest score first. The text (`.show-more-less-html__markup`) is stored like a description sent by the bookmarklet, so the Python check and the auto-ignore rules apply right away, and the row shows the Description block.

| Setting | Default | Meaning |
|---|---|---|
| `JOB_RADAR_LINKEDIN_PUBLIC_MAX_DESCRIPTIONS_PER_DAY` | `100` | Job pages per UTC day |

Same pause between requests and the same stop: any sign of blocking stops **both** searches and descriptions until the next day. Each request is logged in `linkedin_fetches` with `kind = description` and the vacancy.

### The Descriptions button (#205)

With pauses of 10–40 s, 30 descriptions take 5–20 minutes, so in the web UI they are fetched by a **background job**:

- **Descriptions** in the header starts it; **Sync** starts it too, at its *LinkedIn descriptions* step, and finishes without waiting for it.
- The status under the header shows the progress: *Descriptions: 7 of 29 · 6 saved · next in 23 s*, and **Stop**. One job at a time; clicking again shows the running one.
- When it ends, one line says how: *done*, *stopped by you*, *LinkedIn blocked (HTTP 429), stopped until tomorrow*, or *daily limit reached*, with a link to the log. Reload the queue to see the new descriptions.
- After the job, the [local AI analysis](ai.md) (if on) runs on the new descriptions.

From the terminal `./jr sync` and `uv run job-radar import descriptions` fetch them inline, with the same pauses.

## Log

**LinkedIn log** (`/linkedin/log`, linked from the Descriptions status and Settings) lists the requests of the last 7 days, newest first: local time, search or description, the vacancy (link), the result and why it was not `ok`: `HTTP 429`, `HTTP 999`, `redirected to login`, `challenge page`, `no description on page`, `HTTP 404`. Above it, one line per day: requests, saved, errors, and whether and when LinkedIn blocked.
