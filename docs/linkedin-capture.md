# Send a LinkedIn page to Job Radar (bookmarklet)

LinkedIn's personal search (the one behind "See all jobs" in an alert email) ranks jobs by meaning and by your profile. The guest search in [linkedin-public-search.md](linkedin-public-search.md) cannot reproduce it, so some jobs never reach the queue. The **→ Job Radar** bookmarklet closes that gap: it reads the page **you** opened and hands it to the local app.

What it does not do: open pages, click, scroll, log in, or send any request to LinkedIn. It only reads what is already on the screen, when you click it. See the LinkedIn rule in [CLAUDE.md](../CLAUDE.md).

## Install (once)

1. Start the app (`uv run job-radar serve`) and open **Capture** in the header (`/capture`).
2. Drag the **→ Job Radar** link to the bookmarks bar.

## Use

- **Search results** (from an alert's "See all jobs", or any LinkedIn job search): scroll the list to the end of the first page (~25 jobs; LinkedIn only fills cards you scrolled to), then click **→ Job Radar**. Every job card on the page is sent.
- **One vacancy** (`/jobs/view/<id>` or a search with a job open on the right): click **→ Job Radar**; its full description is sent too, so the vacancy loses the *verify manually* badge.

A new tab opens on `/capture` and shows what was received: jobs on the page, new ones, already known ones, descriptions saved. Nothing else happens; the queue has them under **To review**.

## How it works

1. The bookmarklet collects job ids from `/jobs/view/<id>` links and `data-job-id` attributes, and each card's visible text; on a vacancy page also the title, company and description.
2. It opens `http://127.0.0.1:<port>/capture#<data>`. The data is in the URL **fragment**, which browsers never send over the network; the page address is kept without its query string (no tracking or personal tokens).
3. The capture page posts the data to the local server (same origin, so LinkedIn's security policy is not involved) and clears the fragment.
4. The server parses the card text (title, company, location, salary, Easy Apply; noise such as "Promoted", "Be an early applicant", "3 connections work here" is dropped), creates opportunities with `source_type = linkedin_capture` (dedup by LinkedIn job id across alert, search and capture), scores them like alert jobs and stores a description as a `JobSnapshot`.

LinkedIn changes its page layout from time to time. If cards come back empty or wrong, the capture page says so; send a screenshot and the parser gets a new rule and test.
