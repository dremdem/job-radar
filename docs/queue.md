# Review queue

The main page (`/`) lists opportunities in tabs:

| Tab | Shows | URL |
|---|---|---|
| **To review** (default) | `new`: not processed yet | `/` or `/?tab=review` |
| **Interested** | `interested`: the ones you plan to apply to | `/?tab=interested` |
| **Applied** | `applied` | `/?tab=applied` |
| **Ignored** | `ignore` | `/?tab=ignored` |
| **Auto-ignored** | `auto_ignore`: set by the rules in [settings.md](settings.md), with the reason | `/?tab=auto` |
| **All** | everything | `/?tab=all` |

Columns: title (with the full location, salary and badges below it), **Country**, company, score, state. The country is the last part of the location ("Lucknow, Uttar Pradesh, India" → India, "Germany (Remote)" → Germany); when the source gives only a city ("Lucknow (Remote)"), that is what is shown, and `—` when there is no location.

Each tab label carries its count. Inside a tab the order is: score (after penalties), highest first; vacancies of companies that rejected you at the bottom.

## Filters

In the table header, right under the column titles (like a spreadsheet's column filters): **Title**, **Country** and **Company**. Both match any part of the text, ignoring case (`ind` finds India, `EPAM` finds "EPAM Anywhere", `fastapi` finds "Senior Python Developer (FastAPI)"); with several filled, a vacancy must match all of them. Country is searched in the whole location (city, region, country), so `london` works too.

The rows update while you type; the field keeps focus, and the header with the filters stays even when nothing matches. Filters are kept in the URL (`/?tab=review&title=python&country=india&company=tri`), survive switching tabs and clicking the row buttons, and the tab counts show how many vacancies match the filters.

## Current row

Clicking a row — the vacancy link (also with the middle mouse button) or anywhere in the row — highlights it until you click another one. Open a vacancy in a new tab, study it, come back: the row you left from is marked. The choice is remembered in this browser (localStorage), so it survives a reload and the queue re-rendering after buttons and filters. After a reload the page **scrolls to the highlighted row**: a saved description changes the score, so the row may have moved in the list.

## Description checks

When a vacancy has its description (sent by the [bookmarklet](linkedin-capture.md) from the vacancy page), the queue checks it for **Python**:

- `+ Python in description (x7)` and a quote around the first mention, e.g. *"…strong Python 3.10+ with production experience in asyncio…"*; related tools (Django, FastAPI, Flask, asyncio, Pydantic, pandas, …) are listed too;
- `− No Python in description` and **−3** to the score when the word never appears — unless the title already says Python;
- nothing without a description (the row keeps *verify manually*).

The full text is in the row's **Description** block (collapsed), so you can read it without opening LinkedIn again. The quote shows the context: Python in "nice to have" counts the same as in the requirements, so read it. Also available as an auto-ignore rule in [settings.md](settings.md).

## Buttons

- `interested` / `ignore` / `applied`, *ignore with reason* and `rejected` act at once. In a filtered tab a vacancy that no longer belongs there **disappears immediately** and the counts update, no reload needed. In **All** the row stays in place and only its state changes.
- There is no "back to new": find the vacancy in **Ignored** or **All** and pick another state.

Company history, reasons and notes: [applications.md](applications.md).

## Sync mail

**Sync mail** in the header runs the same pipeline as `uv run job-radar sync gmail`: Gmail → LinkedIn alerts → public search (if enabled) → rejections → applications → contacts. It runs in the background of the server; the status next to the button shows the latest lines while it works and the summary when it is done, with a link to reload the page. Only one sync runs at a time; clicking again shows the one in progress. If Gmail access is missing or expired, the status says so: run `uv run job-radar gmail auth` once in a terminal.
