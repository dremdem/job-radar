# Remote OK

Job Radar reads fresh Python vacancies from [Remote OK](https://remoteok.com), a board of remote-only jobs (issue #233).

It uses Remote OK's public JSON feed (no key, no login, no page scraping), **one request per sync**:

```
GET https://remoteok.com/api?tag=python
```

The feed holds the latest 100 jobs tagged `python`, with title, company, publication time, tags, salary, location and the full description (HTML). The first element is Remote OK's terms of use, not a job: link back to the job on Remote OK and name Remote OK as the source. Job Radar does both: the vacancy links to its remoteok.com page and carries a "via Remote OK" badge.

## How a sync works

- **What is read:** the whole feed on every sync. Jobs **older than 30 days** are skipped: the feed reaches back several months, and old posts are usually filled. A job already stored is counted as `known`.
- **Noise filter:** a job becomes a vacancy only when both checks pass, the rest is counted as `skipped`:
  - **Title:** the same role filter as Himalayas (engineer / developer / architect / backend / …, and not analyst / manager / QA / scientist / security / …).
  - **Python:** Python, Django, FastAPI or Flask appears in the title, the tags or the description. The feed is already filtered by the `python` tag, so this is a safety net.
- **Vacancy fields:**
  - **Title, company:** taken from the job.
  - **Salary:** `100,000-140,000 USD / year` when Remote OK has it; Remote OK salaries are yearly USD. `0` means "not given".
  - **Location:** every Remote OK job is remote. `Remote` when the job names no place; otherwise `Remote, <place>`, so the score asks to check whether Thailand fits.
  - **Description:** the description text, so the Python, language and auto-ignore rules work right away.
  - Remote OK sometimes sends accented letters double-encoded (`EspaÃ±a`); they are repaired (`España`).
- **Link:** source `remoteok`, `external_id` = the Remote OK job id, URL = its remoteok.com page. A job is never imported twice.

Runs as part of **Sync** / `./jr sync`, or alone: `uv run job-radar sync remoteok`. Off with `JOB_RADAR_REMOTEOK=false`.
