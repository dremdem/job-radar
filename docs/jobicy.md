# Jobicy

Job Radar reads fresh Python vacancies from [Jobicy](https://jobicy.com), a board of remote jobs (issue #233). Of the free remote-job feeds measured in October 2026 it brings the most: about 25 jobs tagged `python` a day.

It uses the official free [Jobicy API](https://jobi.cy/apidocs) (no key, no login, no page scraping):

```
GET https://jobicy.com/api/v2/remote-jobs?count=50&tag=python[&cursor=<nextCursor>]
```

Each page has up to 50 jobs, newest first, with title, company, publication time, the region the candidate may live in (`jobGeo`), seniority, and the full description (HTML). The next page is asked with the `nextCursor` of the previous one. Jobicy's terms: credit Jobicy with a direct link to the job and send applications to the job URL from the feed. The vacancy links to its jobicy.com page and carries a "via Jobicy" badge.

## How a sync works

- **What is read:** the first run looks back **1 day**. Later runs read everything published since the last successful run, with a 1-day margin. Pages are read until a job older than that point shows up, at most **4 pages** (200 jobs), 2 s apart.
- **Noise filter:** the `python` tag also covers data scientists, security, sales and support engineers. A job becomes a vacancy only when both checks pass, the rest is counted as `skipped`:
  - **Title:** the same role filter as Himalayas (engineer / developer / architect / backend / …, and not analyst / manager / QA / sales / support / scientist / security / …).
  - **Python:** Python, Django, FastAPI or Flask appears in the title or the description.
- **Vacancy fields:**
  - **Title, company:** taken from the job.
  - **Location:** every Jobicy job is remote. `Worldwide (Remote)` when `jobGeo` is `Anywhere`; otherwise `Remote, <regions>` (`Remote, Canada, USA`), so the score asks to check whether your home country fits. Most Python jobs on Jobicy are for the USA, Canada or Europe.
  - **Description:** the description text, so the Python, language and auto-ignore rules work right away.
- **Link:** source `jobicy`, `external_id` = the Jobicy job id, URL = its jobicy.com page. A job is never imported twice.
- **Errors:** a failed request stops this step for this sync and keeps the last successful run, so the next sync reads the missed jobs. The rest of the sync goes on.

Runs as part of **Sync** / `./jr sync`, or alone: `uv run job-radar sync jobicy`. Off with `JOB_RADAR_JOBICY=false`.
