# Chrome fill: LinkedIn and hh.ru descriptions from your own Chrome (#247, #260)

Vacancies from alert emails and from search lists reach To review without a description.
The **Fill from Chrome** button (in the header) opens them in your Chrome one at a time,
unfolds the description and saves it, as if you clicked the bookmarklet on each one. Why
this is allowed and where the line is: [ADR 0001](adr/0001-chrome-description-fill.md).

## Setup

1. Install the **Playwright Extension** in Chrome (Playwright Team). You need Node.js for
   `npx`.
2. Open the extension's page and copy its token. In `.env` add:

   ```
   JOB_RADAR_LINKEDIN_CHROME=true
   PLAYWRIGHT_MCP_EXTENSION_TOKEN=<the token>
   ```

   Without the token, every run asks you in Chrome to allow the connection.
3. Restart the server (`./jr restart`). It needs your desktop session (`DISPLAY`), which
   the systemd user service has.

Optional: `JOB_RADAR_LINKEDIN_CHROME_MAX_PER_RUN` (default 50), and
`JOB_RADAR_LINKEDIN_CHROME_COMMAND` (default `npx -y @playwright/mcp@0.0.84 --extension`,
pinned to the version that was measured). The run adds `--snapshot-mode none
--timeout-settle 100` unless the command sets them itself (#254): without them every page
open sends back a full snapshot of LinkedIn's big page, which the fill never reads, and
every step waits 1–6 s for LinkedIn's background requests to finish. Together that made
a vacancy take 11–20 s instead of a few.

## What a run does

1. It takes the To review vacancies from LinkedIn and hh.ru that have no description, best
   score first (both sites together), up to 50. hh.ru: since #260,
   [ADR 0002](adr/0002-chrome-fill-hh.md); the page is `https://hh.ru/vacancy/<id>`.
2. It works in **a tab of its own** and never touches your tabs. With the token, the extension connects its own connect page (`chrome-extension://…/connect.html`), and the run uses that page (#250). If the extension was given one of your tabs instead (the connect dialog), the run opens a new tab and leaves yours alone.
3. For each vacancy it:
   - waits for the page to load and for the description to appear on it (checked every
     0.4–0.8 s, at most 10 s; #254);
   - checks the page: an hh.ru page saying "Вам недоступна эта вакансия" or a 404/410 on the
     vacancy is **marked and skipped**, and the run goes on (#272, see [Problems](#problems-with-one-vacancy));
   - on an authwall, login, checkpoint, captcha ("не робот") or verification page, or on any
     other HTTP 403 / 429 / 999, it stops at once and leaves the tab open for you to see;
   - checks that the tab shows **this** vacancy. If it shows anything else (you clicked
     around), it stops and saves nothing;
   - on LinkedIn, presses the "… more" button **inside the description**, if there is
     one (hh.ru shows the whole description, nothing is clicked there). Folded text
     is not in what the page shows, so without this the end of the description is lost;
   - reads the page with the bookmarklet's own reader (`web/capture_read.js`) and saves it
     as the bookmarklet would, but only the vacancy itself: the "similar jobs" on its page
     are not added to To review;
   - waits its own random pause, **Settings → Fill from Chrome** (2–4 s by default, #252; the guest pause of the Descriptions button stays 10–40 s), then goes on
     to the next vacancy in the same tab. Before leaving the page, it checks again that the
     tab is still its own.
4. At the end it closes its tab, so nothing is left open.

Three vacancies in a row that failed in any way stop the run: the browser has gone away, or
the layout has changed and the bookmarklet needs the same fix.

## Problems with one vacancy

Each vacancy has its own limit (60 s from opening it to saving it) and its own error handler,
so a page that hangs or breaks costs that vacancy only (#272). What went wrong is written in
the vacancy's **Company history**, once per distinct problem, and in the LinkedIn log. These
marks are not contacts: they do not count in the "contacted" badge or as your last action.

| Kind | When | Next run |
| --- | --- | --- |
| `unavailable` | hh.ru says the vacancy is not available to you, or a 404/410 | skipped, until you delete the mark (× in the history) |
| `fill_problem` | timeout, browser error, no description, or the page that stopped the run | tried again |

The status line lists the vacancies of this run that had a problem (#274): the title opens it
in Job Radar, the site name opens the posting, and the reason follows. The list lasts until the
next run or a server restart; the marks stay.

To drop a vacancy for good, ignore it as usual; to try it again, delete its `unavailable` mark.

## Status, Stop and reruns

Under the header: `Chrome fill: 7 of 30 opened · 6 saved · 23 left · next in 18 s`, the
last vacancy it handled, **Stop**, a progress bar, and the counts per site (`LinkedIn: 5 of
24 · 4 saved · 1 with a problem`, `hh.ru: 2 of 6 · 2 saved`; #272). Stop takes effect during the pause, at the latest
after the current page. The finished line says why it ended (`done`, `stopped by you`,
`stopped: HTTP 429`, `stopped: another page in the tab: stopped`), and links to the
**LinkedIn log**, where every page is listed with kind `chrome`. A saved page shows how
long each step took (`ok · open 0.3 s · load 0.9 s · expand 0.8 s · read 0.2 s`): opening it, waiting
for the description, unfolding it and reading it. The "more" click comes 0.3 s after the
step that finds the button has answered: LinkedIn sends requests on that click, and
Playwright MCP would otherwise wait up to 5 s for them (#258).

A vacancy that already has a description is never opened again, so a rerun continues where
the last one stopped, and nothing is duplicated or overwritten. Only one LinkedIn
description job runs at a time: the guest **Descriptions** job or the Chrome fill.

## What it never does

It never logs in, reads or sets cookies, or sends requests to LinkedIn outside the browser.
It never uses Easy Apply, and never acts for you: no apply, message, like, follow, save or
connect. It calls only three Playwright MCP tools: `browser_tabs` (to open and close its
tab), `browser_navigate` and `browser_evaluate`. The only click is the description's own
"more" button.
