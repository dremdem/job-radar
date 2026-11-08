# Send a LinkedIn page to Job Radar (bookmarklet)

LinkedIn's personal search (the one behind "See all jobs" in an alert email) ranks jobs by meaning and by your profile. The guest search in [linkedin-public-search.md](linkedin-public-search.md) cannot reproduce it, so some jobs never reach the queue. The **→ Job Radar** bookmarklet closes that gap: it reads the page **you** opened and hands it to the local app.

What it does not do: open pages, click, scroll, log in, or send any request to LinkedIn. It only reads what is already on the screen, when you click it. See the LinkedIn rule in [CLAUDE.md](../CLAUDE.md).

## Install (once)

1. Start the app (`uv run job-radar serve`) and open **Capture** in the header (`/capture`).
2. Drag the **→ Job Radar** link to the bookmarks bar.

## Use

- **Search results** (from an alert's "See all jobs", or any LinkedIn job search): scroll the list to the end of the first page (~25 jobs; LinkedIn only fills cards you scrolled to), then click **→ Job Radar**. Every job card on the page is sent.
- **One vacancy** (`/jobs/view/<id>` or a search with a job open on the right): click **→ Job Radar**; its full description is sent too, so the vacancy loses the *verify manually* badge.

A new tab opens on `/capture` and shows what was received: jobs on the page, new ones, already known ones, descriptions saved. On top, a **big traffic light** (#219), also in the tab title: green **✓ Description saved** / **✓ Saved**, yellow when part of it could not be read or the page had no description, red **✗ Nothing saved**. When everything went fine it **closes itself** after 1.5 seconds (**Settings → Capture**, 0–60 s, #221); it stays open when something could not be read. Meanwhile an open queue tab **updates the affected rows by itself** (a saved description shows the Python check and the text right away), shows a notice that stays until closed when a vacancy left the tab (e.g. auto-ignored, see [queue.md](queue.md#current-item)), and says when new vacancies arrived so you can reload.

## How it works

1. The bookmarklet collects job ids and each card's visible text: on the semantic search page (`/jobs/search-results/`, where alert emails lead) cards are `componentkey="job-card-component-ref-<id>"`; on classic pages it uses `/jobs/view/<id>` links and `data-job-id`. For the open vacancy (`currentJobId` or `/jobs/view/<id>`) it also sends the tab title ("<title> \| <company> \| LinkedIn") and the description (`componentkey="JobDetails_AboutTheJob_<id>"` on the semantic page, including the part folded under "… more").
2. It opens `http://127.0.0.1:<port>/capture#<data>`. The data is in the URL **fragment**, which browsers never send over the network; the page address is kept without its query string (no tracking or personal tokens).
3. The capture page posts the data to the local server (same origin, so LinkedIn's security policy is not involved) and clears the fragment.
4. The server parses the card text (title, company, location, salary, Easy Apply; noise such as "Promoted", "Be an early applicant", "3 connections work here" is dropped), creates opportunities with `source_type = linkedin_capture` (dedup by LinkedIn job id across alert, search and capture), scores them like alert jobs and stores a description as a `JobSnapshot`.

On a LinkedIn vacancy that says it is not accepting applications, the vacancy also gets a red **not accepting** badge and goes to Auto-ignored ([details](linkedin-chrome-fill.md#problems-with-one-vacancy), #278). A bookmarklet you added before this change does not know the line: drag it from the Capture page again.

The reading itself lives in `job_radar/web/capture_read.js`; the bookmarklet wraps it, and the [Chrome fill](linkedin-chrome-fill.md) (#247) runs the very same function on the vacancy pages it opens, so the two never drift apart.

LinkedIn changes its page layout from time to time. If cards come back empty or wrong, the capture page says so; send a screenshot and the parser gets a new rule and test.

## hh.ru vacancies

The same bookmarklet works on an hh.ru vacancy page (`hh.ru/vacancy/<id>`): it sends the title, company, salary, work format, city and description of that one vacancy. See [hh.md](hh.md#descriptions-the-bookmarklet-136).

## When a page comes back unreadable

The capture page offers a second bookmarklet, **→ Job Radar (debug)**. Clicked on the LinkedIn page, it opens `/capture/debug` and hands over the page's HTML (scripts, styles and images removed) with `postMessage`; the app saves it to `data/capture_debug/<time>.html`. The last payload of the normal bookmarklet is kept as `data/capture_debug/last_payload.json`. Both stay on this machine (`data/` is git-ignored) and are used to write parser rules and sanitized test fixtures.
