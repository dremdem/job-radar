# Chrome fill: LinkedIn descriptions from your own Chrome (#247)

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

1. It takes the To review vacancies from LinkedIn that have no description, best score
   first, up to 50.
2. It works in **a tab of its own** and never touches your tabs. With the token, the extension connects its own connect page (`chrome-extension://…/connect.html`), and the run uses that page (#250). If the extension was given one of your tabs instead (the connect dialog), the run opens a new tab and leaves yours alone.
3. For each vacancy it:
   - waits for the page to load and for the description to appear on it (checked every
     0.4–0.8 s, at most 10 s; #254);
   - checks the page: on an authwall, login, checkpoint, captcha or verification page, or
     on HTTP 429 / 999, it stops at once and leaves the tab open for you to see;
   - checks that the tab shows **this** vacancy. If it shows anything else (you clicked
     around), it stops and saves nothing;
   - presses the "… more" button **inside the description**, if there is one. Folded text
     is not in what the page shows, so without this the end of the description is lost;
   - reads the page with the bookmarklet's own reader (`web/capture_read.js`) and saves it
     as the bookmarklet would, but only the vacancy itself: the "similar jobs" on its page
     are not added to To review;
   - waits its own random pause, **Settings → Fill from Chrome** (2–4 s by default, #252; the guest pause of the Descriptions button stays 10–40 s), then goes on
     to the next vacancy in the same tab. Before leaving the page, it checks again that the
     tab is still its own.
4. At the end it closes its tab, so nothing is left open.

Three pages in a row without a description stop the run: LinkedIn's layout has probably
changed, and the bookmarklet needs the same fix.

## Status, Stop and reruns

Under the header: `Chrome fill: 7 of 30 opened · 6 saved · 23 left · next in 18 s`, the
last vacancy it handled, and **Stop**. Stop takes effect during the pause, at the latest
after the current page. The finished line says why it ended (`done`, `stopped by you`,
`stopped: HTTP 429`, `stopped: another page in the tab: stopped`), and links to the
**LinkedIn log**, where every page is listed with kind `chrome`. A saved page shows how
long each step took (`ok · open 0.3 s · load 0.9 s · read 1.1 s`): opening it, waiting
for the description, unfolding and reading it.

A vacancy that already has a description is never opened again, so a rerun continues where
the last one stopped, and nothing is duplicated or overwritten. Only one LinkedIn
description job runs at a time: the guest **Descriptions** job or the Chrome fill.

## What it never does

It never logs in, reads or sets cookies, or sends requests to LinkedIn outside the browser.
It never uses Easy Apply, and never acts for you: no apply, message, like, follow, save or
connect. It calls only three Playwright MCP tools: `browser_tabs` (to open and close its
tab), `browser_navigate` and `browser_evaluate`. The only click is the description's own
"more" button.
