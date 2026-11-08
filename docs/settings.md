# Settings and auto-ignore

**Settings** in the header (`/settings`) holds the auto-ignore rules. They cut noise from **To review**: a matching vacancy gets the state **`auto_ignore`** — separate from your own `ignore` — with the reason, and lands in the **Auto-ignored** tab instead of To review.

## Rules

| Rule | A vacancy is auto-ignored when… | Reason shown |
|---|---|---|
| Countries | the vacancy's location is in a listed country (#229): its name as a whole word ("Indiana" is not India), and for India and Pakistan also their big cities and states, because LinkedIn often gives only the city. The list is a text field, comma-separated; default **India, Pakistan**; empty turns the rule off | `country: India (Bengaluru (Remote))` |
| Company rejected you | the company rejected you within the window | `Capitole rejected you on 2026-07-01` |
| Already applied | you have a confirmed application to the company, or marked another of its vacancies **applied** in Job Radar (#211), within the window | `applied to Capitole on 2026-06-28` |
| Already ignored | you ignored another vacancy of the company (your own `ignore`, not auto) within the window | `ignored another Capitole vacancy on 2026-09-25` |
| Hybrid / on-site | "hybrid" or "on-site" appears in the title or location | `hybrid / on-site` |
| No Python in the description | the vacancy has a description, Python is not in it, and not in the title either | `no Python in the description` |
| Description not in English | the description (or the title) is in a language other than English or Russian ([applications.md](applications.md#vacancy-language)) | `language: German` |
| Vacancy not available or not accepting | the Chrome fill found that the site says the vacancy is not available to you, or a 404/410 ([linkedin-chrome-fill.md](linkedin-chrome-fill.md#problems-with-one-vacancy), #276); or a LinkedIn page read by the Chrome fill or the bookmarklet says it is not accepting applications (#278). **On by default**; the only rule besides Countries that is | `vacancy not available`, `not accepting applications` |
| Claude said skip | Claude's latest finished analysis says `skip`, and its main reason (`ignore_reason.kind`) is one you picked — or any reason at all ([below](#claude-said-skip), #245) | `Claude: location: EU only` |
| AI: hybrid / country-only | the local model found, with a quote checked against the description, that the job is hybrid / on-site or limited to some countries ([ai.md](ai.md)); works only with `JOB_RADAR_AI_ANALYSIS=true`, off by default | `AI: only for UK` |

**The same vacancy you applied to (#269)** is not a rule and needs no switch: a vacancy with the same company and the same title (case, spaces and punctuation ignored) as **exactly one** vacancy you marked applied never reaches To review, from any source. It joins the applied vacancy instead: its link, description and the rest move there, and the history gets a `seen again` event with the source. With two applied vacancies of that title (another city or team) nothing is joined. Copies stored before this: `uv run job-radar db merge-applied-duplicates` lists them (applied vacancy ← its copies), `--apply` merges them (`--skip 221,1628` leaves those applied vacancies' groups apart); only To review, auto-ignored and ignored copies, never interested ones. Back up `data/job_radar.db` first.

Checked in this order; the first match gives the reason. **Window**: 90 days by default (setting), so a company comes back to To review three months later. The countries rule is **on** with India and Pakistan; all other rules are **off** until switched on.

## Claude said skip

Claude's analysis names the one main reason for a `skip` ([claude.md](claude.md#what-comes-back)). Some reasons are clear-cut, others need your eyes: a stack Claude calls a blocker may be close to something in your CV. So the rule lets you choose:

- **Any reason**: every `skip` verdict auto-ignores the vacancy.
- Or tick the reasons: **location** (only some country or region, e.g. "EU only" — any country, not a list), **hybrid / on-site** (`work_format`), **language**, **stack**, **seniority**, **salary**, **contract**, **other**.

Nothing ticked: the rule is off (the default). `apply` and `maybe` never trigger it, and an old analysis without a named reason triggers only **Any reason**. It runs when an analysis finishes — the vacancy leaves To review by itself if it is still `new` — and with **Apply to To review now**.

## When they run

- On every new vacancy entering the queue: alert emails, public search, bookmarklet; the Python rule also when the bookmarklet brings the description of a vacancy already in To review.
- When a Claude analysis finishes, for the **Claude said skip** rule.
- **Apply to To review now** on the Settings page runs them over the vacancies currently `new`.
- Your own states are never changed: only `new` vacancies are auto-ignored.

## Undo

Open the **Auto-ignored** tab, check the reason, and press any state button (`interested`, `ignore`, `applied`): the vacancy is yours again and the auto reason is cleared.

Settings are stored in the database (`app_settings`), not in `.env`.

**Claude → Parallel analyses** (1–8, default 4): how many Claude analyses run at once; see [claude.md](claude.md#how-it-runs) (#197).

**LinkedIn → Pause between requests, From / To** (seconds, 0–600, default 10–40): each guest request to LinkedIn waits a random time in this range; see [linkedin-public-search.md](linkedin-public-search.md) (#205). The **LinkedIn log** link next to it shows every request and why fetching stopped.

## Capture

**Close the capture tab after, s**: how long the bookmarklet's tab stays open after a good result, 1.5 by default, 0–60 ([linkedin-capture.md](linkedin-capture.md), #221).
