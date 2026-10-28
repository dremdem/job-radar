# Hacker News: Who is hiring?

Every month Hacker News has an **"Ask HN: Who is hiring?"** thread (posted by the `whoishiring` account on the first weekday). Each top-level comment is one company's job post, written by the company itself, often remote (issue #233). Job Radar reads the current month's thread and keeps the remote Python posts.

It uses the public [HN Search API by Algolia](https://hn.algolia.com/api), the search linked from the bottom of every Hacker News page (no key, no login, no page scraping). One sync makes **two requests**:

```
GET https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring   # find the thread
GET https://hn.algolia.com/api/v1/items/<thread id>                                # the thread with all comments
```

The official [Firebase HN API](https://github.com/HackerNews/API) has the same data, but it needs one request per comment (300–400 a month).

## How a sync works

- **Which thread:** the newest story by `whoishiring` whose title starts with `Ask HN: Who is hiring?`. The "Who wants to be hired?" and "Freelancer?" threads are skipped.
- **What is read:** every top-level comment of that thread, on every sync. Replies to a post are not job posts and are skipped, and so are deleted comments. Posts already stored are counted as `known`, so the first sync of a month brings the whole thread so far and later syncs only what was added.
- **The header line:** posts start with a line like `Company | Role | Location | Full-time | Salary` (sometimes `—` instead of `|`). From it:
  - **Company:** the first part, without links.
  - **Title:** the first part that names a developer-type role (the same role filter as Telegram and Himalayas). When the header names no role (`Curai | Remote (US) | Full-time`), the title is `<Company>: roles in the post`.
  - **Location:** the parts about the work format. A part like `San Francisco HYBRID (3 days/wk) or REMOTE (US)` is cut at `or` and ` / `, and only the remote option is kept (`REMOTE (US)`), so the hybrid auto-ignore rule does not drop a post that can be done remotely. `Remote` in brackets after the company (`PrairieLearn (Remote US)`) counts too. Without a remote option the location is the on-site / hybrid part.
- **Kept only when** all of these hold, the rest is counted as `skipped`:
  - **Remote:** the location has a remote option. If the header says nothing about the work format, the post must mention remote somewhere.
  - **Python:** Python, Django, FastAPI or Flask appears anywhere in the post.
  - **Role:** the title passes the role filter (engineer / developer / architect / backend / …, and not analyst / manager / QA / sales / …). Posts whose header names no role pass.
  - **Open:** the header does not say `CLOSED` or `FILLED`. Companies edit old posts this way.
- **Vacancy fields:** the title, company and location above; the whole post text is the description, so the language, Python and auto-ignore rules work right away. Residence limits such as `REMOTE (US)` are not filtered here: the score asks to check whether Thailand fits, and Claude's analysis reads the full post.
- **Link:** the vacancy links to the comment on news.ycombinator.com. Source `hackernews`, `external_id` = the comment id. A post is never imported twice.

Runs as part of **Sync** / `./jr sync`, or alone: `uv run job-radar sync hackernews`. Off with `JOB_RADAR_HACKERNEWS=false`.
