# Other boards: Remotive, Working Nomads, We Work Remotely

Three remote job boards with a free public feed (issue #233). Each brings only a few Python jobs a month (measured on 2026-10-28), so they share one **Other boards** step:

| Board | Feed | What it holds |
|---|---|---|
| [Remotive](https://remotive.com) | `GET https://remotive.com/api/remote-jobs?category=software-dev` | The free API shows about a dozen software jobs, delayed by 24 h (3 with Python on that day). |
| [Working Nomads](https://www.workingnomads.com) | `GET https://www.workingnomads.com/api/exposed_jobs/` | About 40 latest jobs of every kind (8 with Python). |
| [We Work Remotely](https://weworkremotely.com) | RSS: `/categories/remote-back-end-programming-jobs.rss` and `/categories/remote-full-stack-programming-jobs.rss` | The back-end and full-stack categories, about 90 jobs (around 30 with Python). |

No key, no login, no page scraping. Remotive's terms: link back to the job on Remotive and name Remotive as the source, and request the API at most 4 times a day. Every board's vacancy links to its job page on that board and carries a "via <board>" badge.

## How a sync works

- **How often:** each board is read **at most every 6 hours**, however often Sync runs (Remotive's limit). A board read sooner says `read less than 6 h ago, skipped`.
- **What is read:** the whole feed. Jobs **older than 30 days** are skipped. A job seen in both We Work Remotely categories counts once. A job already stored is counted as `known`.
- **Noise filter:** the same as Himalayas and Remote OK: the title names a developer-type role (and not analyst / manager / sales / support / scientist / security / …), and Python, Django, FastAPI or Flask appears in the title, the tags or the description. One more check here: a title that names another stack (PHP, .NET, Java, Ruby, Swift, Angular, Vue, React, Node, Go, …) and no Python is skipped, because agencies like Lemon.io list Python in the boilerplate of every post. The rest is counted as `skipped`.
- **Vacancy fields:**
  - **Title, company:** from the job. We Work Remotely titles look like `Company: Role` and are split there.
  - **Location:** every job on these boards is remote. `Worldwide (Remote)` for `Worldwide` / `Anywhere in the World`; otherwise `Remote, <place>` (for We Work Remotely the region, not its list of countries) (`Remote, USA Only`, `Remote, CET (+/- 3 hours)`), so the score asks to check whether your home country fits.
  - **Salary:** Remotive's salary text when it has one.
  - **Description:** the description text, so the Python, language and auto-ignore rules work right away.
- **Link:** sources `remotive`, `workingnomads` (`external_id` = the board's job id) and `weworkremotely` (`external_id` = the job page URL). A job is never imported twice.
- **Errors:** a board that cannot be read is reported and the next board still runs; it is read again on the next sync.

Runs as part of **Sync** / `./jr sync`, or alone: `uv run job-radar sync remotive` (reads all three). Off with `JOB_RADAR_BOARDS=false`.
