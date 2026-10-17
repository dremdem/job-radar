# Settings and auto-ignore

**Settings** in the header (`/settings`) holds the auto-ignore rules. They cut noise from **To review**: a matching vacancy gets the state **`auto_ignore`** — separate from your own `ignore` — with the reason, and lands in the **Auto-ignored** tab instead of To review.

## Rules

| Rule | A vacancy is auto-ignored when… | Reason shown |
|---|---|---|
| Company rejected you | the company rejected you within the window | `Capitole rejected you on 2026-07-01` |
| Already applied | you have a confirmed application to the company within the window | `applied to Capitole on 2026-06-28` |
| Already ignored | you ignored another vacancy of the company (your own `ignore`, not auto) within the window | `ignored another Capitole vacancy on 2026-09-25` |
| Hybrid / on-site | "hybrid" or "on-site" appears in the title or location | `hybrid / on-site` |
| No Python in the description | the vacancy has a description, Python is not in it, and not in the title either | `no Python in the description` |
| Description not in English | the description (or the title) is in a language other than English or Russian ([applications.md](applications.md#vacancy-language)) | `language: German` |
| AI: hybrid / country-only | the local model found, with a quote checked against the description, that the job is hybrid / on-site or limited to some countries ([ai.md](ai.md)) | `AI: only for UK` |

Checked in this order; the first match gives the reason. **Window**: 90 days by default (setting), so a company comes back to To review three months later. All rules are **off** until switched on.

## When they run

- On every new vacancy entering the queue: alert emails, public search, bookmarklet; the Python rule also when the bookmarklet brings the description of a vacancy already in To review.
- **Apply to To review now** on the Settings page runs them over the vacancies currently `new`.
- Your own states are never changed: only `new` vacancies are auto-ignored.

## Undo

Open the **Auto-ignored** tab, check the reason, and press any state button (`interested`, `ignore`, `applied`): the vacancy is yours again and the auto reason is cleared.

Settings are stored in the database (`app_settings`), not in `.env`.

**Claude → Parallel analyses** (1–8, default 4): how many Claude analyses run at once; see [claude.md](claude.md#how-it-runs) (#197).

**LinkedIn → Pause between requests, From / To** (seconds, 0–600, default 10–40): each guest request to LinkedIn waits a random time in this range; see [linkedin-public-search.md](linkedin-public-search.md) (#205). The **LinkedIn log** link next to it shows every request and why fetching stopped.
