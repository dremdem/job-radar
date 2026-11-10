# FDE Times

Job Radar reads Forward Deployed Engineer roles from [FDE Times](https://fdetimes.net/en/jobs/), a board of FDE roles at AI companies (issue #13). About 12 new FDE roles a day, most of them on-site in North America; the board also lists related AI roles (applied AI, solutions engineering).

FDE Times has no API or feed, so Job Radar reads its public pages with plain HTTP (no browser, no login, no cookies). robots.txt allows them. Its terms forbid copying the board to build another site: this is personal use, one list request per sync and a pause of tens of seconds between role pages.

## How a sync works

- **One request:** `GET https://fdetimes.net/en/jobs/`. The page lists every open role, newest first, with title, company, location, region (`north-america`, `europe`, `apac`, `remote`, `other`), kind (`fde` or `related`) and publication time.
- **What is read:** the first run looks back **7 days** (`JOB_RADAR_FDETIMES_FIRST_RUN_DAYS`). Later runs take what was published since the last successful run, with a 7-day margin: the time shown is when the company posted the role (some carry only a date), and a role can reach the board days later. It is one request either way, and known roles are not imported again.
- **Filters** (the rest is counted as `skipped`):
  - **Kind:** FDE roles only; `JOB_RADAR_FDETIMES_RELATED=true` adds the related AI roles.
  - **Region:** all by default; `JOB_RADAR_FDETIMES_REGIONS=remote,apac` keeps only those.
  - **Title:** the same role filter as Himalayas (engineer / developer / architect / …, and not manager / sales / analyst / scientist / security / …). "Forward Deployed Strategist" or "Manager, Forward Deployed Engineering" are skipped.
- **Vacancy fields:** title, company and location from the list; the score and auto-ignore rules work as for other sources, and a copy of a vacancy you applied to joins it (applied duplicates). There is no description yet.
- **Link:** source `fdetimes`, `external_id` = the role's slug (`celonis-8198943003`), URL = its page `https://fdetimes.net/en/jobs/<slug>/`. A role is never imported twice. Applications go through the form on that page; FDE Times shows no link to the company's own posting.
- **Errors:** a failed request stops this step for this sync and keeps the last successful run, so the next sync reads the missed roles.

Runs as part of **Sync** / `./jr sync`, or alone: `uv run job-radar sync fdetimes`. Off with `JOB_RADAR_FDETIMES=false`.

## Descriptions

The description is on the role page, in its schema.org `JobPosting` data. It is fetched by the **Descriptions** job (the button in the header, and the *Descriptions* step of Sync), after the LinkedIn descriptions:

- For FDE Times vacancies in **To review** without a description, best score first, at most **50 per run** (`JOB_RADAR_FDETIMES_MAX_DESCRIPTIONS`).
- Each request waits a random pause from the guest range on **Settings** (10–40 s by default); **Stop** stops it.
- A page that is gone (HTTP 404 or 410) marks the vacancy as **not available**, so the auto-ignore rule moves it out of To review. HTTP 403 or 429 stops this part of the run; the next run tries again. Other failures are counted as errors and retried next time.
- The Descriptions job runs when either the LinkedIn guest search or FDE Times is on.
