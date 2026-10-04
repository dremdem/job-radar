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

The full text opens with the row's **Description** button in a large reading window (bigger font, paragraphs kept, title/company/location on top, every *Python* highlighted); close it with Esc, the × button or a click outside. No need to open LinkedIn again just to read. The quote shows the context: Python in "nice to have" counts the same as in the requirements, so read it. Also available as an auto-ignore rule in [settings.md](settings.md).

## Buttons

- `interested` / `ignore` / `applied`, *ignore with reason* and `rejected` act at once. In a filtered tab a vacancy that no longer belongs there **disappears immediately** and the counts update, no reload needed. In **All** the row stays in place and only its state changes.
- There is no "back to new": find the vacancy in **Ignored** or **All** and pick another state.

Company history, reasons and notes: [applications.md](applications.md).

## Sync

**Sync** in the header runs the same pipeline as `./jr sync` (`uv run job-radar sync gmail`) in the background of the server. Only one sync runs at a time; clicking again shows the one in progress.

The status next to the button lists the steps, one line each, updated while it works:

| Step | What it does |
|---|---|
| Gmail | New mail for the queries ([gmail.md](gmail.md)) |
| LinkedIn alerts | Vacancies from the stored alert emails |
| LinkedIn search | Public guest search, if enabled ([linkedin-public-search.md](linkedin-public-search.md)) |
| Telegram | Allowlisted channels, if configured ([telegram.md](telegram.md)) |
| Himalayas | Fresh worldwide Python jobs ([himalayas.md](himalayas.md)) |
| Mail analysis | Rejections, applications, contacts from stored mail ([applications.md](applications.md)) |
| LinkedIn descriptions | Public job pages for To review, if enabled |
| AI analysis | Work mode, residence and stack with the local model ([ai.md](ai.md)) |

Each line shows the state:
- `·` waiting;
- `…` running;
- `✓` done, with the summary (e.g. "42 new");
- `off` disabled in the settings;
- `✗` failed, with the reason.

The full log is under **details**, and a **reload** link appears at the end.

**Steps are independent.** When one fails, the others still run. Without Gmail access the mail-based steps work on the mail already stored and do not fetch message bodies. The Gmail line then says what to do: *Gmail access expired or revoked: run `./jr auth`*.

`./jr sync` prints the same lines and exits with code 1 when a step failed.

## Phone

The UI adapts to phone width (≤ 720 px, #111), for example over Tailscale (`tailscale serve`).
- Each vacancy is a card:
  - the title with the score and the state;
  - country · company, then badges and the Claude verdict;
  - the action buttons in a row, large enough to tap, with **Select** (pick for Claude) and **✨ Claude** (analyze this one) among them, so the tick is never next to the title link.
- Long parts start folded on the phone: tap **Why 7.5** for the score reasons and **contacted · N** for the company history.
- The filters stack full width, and the Description window takes the whole screen.
- The vacancy page (**page →**) has the state buttons too, so you can open a vacancy, read the Claude analysis and decide there.

Desktop keeps the table layout. Since #119 the UI uses Bootstrap 5 (from the jsDelivr CDN), light or dark like the system. Buttons are coloured by meaning: interested is green, ignore red, applied blue, rejected grey, and the main actions are filled.
