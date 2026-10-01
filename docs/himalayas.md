# Himalayas

Job Radar reads fresh Python vacancies open **worldwide** from [Himalayas](https://himalayas.app), the same list as <https://himalayas.app/jobs/worldwide/python?sort=recent> (issue #89).

It uses the official free public [Remote Jobs API](https://himalayas.app/docs/remote-jobs-api) (no key, no login, no page scraping):

```
GET https://himalayas.app/jobs/api/search?q=python&worldwide=true&sort=recent&page=N
```

Each page has 20 jobs with title, company, publication time, salary, location and timezone restrictions, the full description (HTML) and the job page on himalayas.app. Himalayas refreshes this data once a day, so polling more often brings nothing. The API is rate limited: a `429` stops the step for this sync, and the rest of the sync goes on.

## How a sync works

- **What is read:** the first run looks back **1 day** (`JOB_RADAR_HIMALAYAS_FIRST_RUN_DAYS`). Later runs read everything published since the last successful run, with a 1-day margin, because a job can show up in the daily cache later than its publication time.
- **Paging:** pages are sorted by date only roughly and overlap, so reading stops when **every job on a page** is older than that point. At most 10 pages per run, 2 s apart.
- **Noise filter:** the search returns any job that mentions Python, including analysts, security roles, physicists and mathematicians. A job becomes a vacancy only when both checks pass:
  - **Title:** it passes the same role filter as Telegram (engineer / developer / architect / backend / …, and not analyst / manager / QA / …). Himalayas also excludes security, scientist / researcher, mathematician / physicist, and civil / structural / aerodynamics / control-system engineering.
  - **Python:** Python, Django, FastAPI or Flask appears in the title, the description or Himalayas' categories.
  - The other jobs are only counted as `skipped`.
- **Vacancy fields:**
  - **Title, company, salary:** taken from the job. Salary looks like `100,000-175,000 USD / year`.
  - **Location:**
    - `Worldwide (Remote)` when the job does not restrict the countries the candidate may live in;
    - otherwise `Remote, <countries>`, so the score asks to check whether Thailand fits.
    - Timezone restrictions are ignored: work hours in any timezone are fine (#91).
  - **Description:** the description text, so the Python check, the language check and the auto-ignore rules work right away.
- **Link:** the vacancy links to its himalayas.app page (attribution). Source `himalayas`, `external_id` = that page URL (the API's `guid`). A job is never imported twice.

Runs as part of **Sync mail** / `./jr sync`, or alone: `uv run job-radar sync himalayas`.
