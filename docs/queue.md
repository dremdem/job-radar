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

**Layout (#158): an inbox.**

- **Header:** the tabs (each with its count), a search box, **Analyze…**, **Sync**, **Descriptions** and the Claude status.
- **Left: the list.** One compact item per vacancy: the score, colour-coded (green from 8, amber from 6, red below), the title, company · location, and one line that says the most useful thing. That is Claude's verdict ("Claude: apply · 82%"), a suggested reason to ignore ("ignore? only India"), "no description" or "not analyzed".
- **Right: the panel** of the selected vacancy. It has the same content as its own page (**page →**):
  - the state buttons, the one-tap reasons to ignore and the reason chips ([applications.md](applications.md#reasons-and-notes));
  - the links and the reasons behind the score;
  - Claude's analysis with the chat and quick prompts ([claude.md](claude.md));
  - the description with *Python* highlighted, and the company history.

**The order inside a tab:** score after penalties, highest first; vacancies of companies that rejected you go last.

**The country** is the last part of the location ("Lucknow, Uttar Pradesh, India" → India, "Germany (Remote)" → Germany).

**Analyze…** opens a dialog with **Score from**, a list from 1.0 to 10.0 in steps of 0.5 (6.5 by default; the value you set is remembered in this browser). It shows how many vacancies of the list qualify — a description, that score or more, no Claude analysis yet — and lists them, or, when none does, why: how many lack a description, score lower or are already analysed. A **Skipped** line always counts what stays out: already analysed (every vacancy with a Claude chat has an analysis, so it is never re-run; use **Analyze again** in its panel for that), without a description, below the score (#190). **Start** queues them, and a notice lists what went to the Claude queue; it hides by itself after 10 seconds or with **Close** (#174).

## Triage and keyboard

Pick a vacancy in the list, read the panel, decide. After **interested**, **ignore**, **applied** or an ignore reason, the list and the counts update. When the vacancy leaves the tab, the panel opens the **next** one, so you can go through the tab one by one. After the last one the panel and the list are empty: "Nothing left in this tab" (#166). The vacancy in the panel is always the current one, even if its list item was redrawn meanwhile.

| Key | Action |
|---|---|
| `J` / `K` | Next / previous vacancy |
| `I` / `X` / `A` | Interested / ignore / applied |
| `O` | Open the posting on its site (LinkedIn, hh.ru, …) in a new tab |
| `Enter` | Open the vacancy page |
| `/` | Search |

Keys are ignored while you type in a field.

**Links.** Under the title the panel has a row of big buttons: **Open on linkedin.com ↗** (one per site the vacancy came from, in a new tab) and **Full page →** (#164).

## Filters

The search box matches the title, the company and the location, ignoring case and anywhere in the text: `ind` finds India, `EPAM` finds "EPAM Anywhere", `fastapi` finds "Senior Python Developer (FastAPI)". The list updates while you type.

Filters are kept in the URL (`/?tab=review&title=python`), survive switching tabs, and the tab counts show how many vacancies match.

**Description (#176).** Above the list: **All · No description · With description**. A two-step routine: first **No description**, open each vacancy you do not ignore outright and click the bookmarklet (the item leaves this view once its description is saved); then **With description** and **Analyze…** for the rest. The switch is kept with the tab, the search and your decisions. The tab counts ignore it, so To review never looks empty just because every vacancy there has a description; the switch shows its own counts for the current tab: **All (12) · No description (0) · With description (12)** (#186).

**Pages (#180).** The list shows 50 vacancies at a time; at its bottom are **‹ Prev**, "51–100 of 726", **Next ›** and a choice of 25 / 50 / 100 / 200 per page (kept with the tab, the search and the description switch; a new tab or search starts at page 1). The whole tab is sorted first, by the score alone, and only the shown page gets the full details (company history, Claude, hints), so long tabs such as All or Ignored load in about a second. **J / K** and **Analyze…** work on the page you see.

**Sort (#199, #207).** The list head has **Sort: Score · Claude % · Last action**. *Score* is the default. *Claude %* puts the highest match from Claude's latest finished analysis first (90% above 30%); vacancies Claude has not analysed go to the bottom, and equal ones keep the score order. A re-analysis waiting in the queue keeps the old %. *Last action* puts first the vacancy you did something with most recently: the latest of your state change (interested, ignore, applied), an application, a rejection, a note or ignore reason, an event you added, or your message in its Claude chat. Vacancies you have not touched come after them, newest first. In this mode each item says what and when, e.g. "12.10 · applied". The choice is kept with the tab, the search, the description switch and the pages.

## Current item

The vacancy you open stays selected and is remembered in this browser (localStorage). After a reload, or coming back from the vacancy page, it is opened again and scrolled into view.

**Back from a vacancy page.** **← Queue** on the vacancy page returns to the queue you came from, with the same tab and filters (remembered for this browser tab), and selects the vacancy you opened (#150).

**After the bookmarklet (#162).** When the [bookmarklet](linkedin-capture.md) saves a description, the open queue tab redraws that vacancy. If the vacancy left the tab, for example the auto-ignore rules moved it to **Auto-ignored**, a notice appears at the top of the page: title, company, the tab it moved to, the reason, and a link to the vacancy. It stays until you press **Close**. The panel keeps showing that vacancy, and **J / K** continue from its old place in the list.

## Description checks

When a vacancy has its description (sent by the [bookmarklet](linkedin-capture.md) from the vacancy page), the queue checks it for **Python**:

- `+ Python in description (x7)` and a quote around the first mention, e.g. *"…strong Python 3.10+ with production experience in asyncio…"*; related tools (Django, FastAPI, Flask, asyncio, Pydantic, pandas, …) are listed too;
- `− No Python in description` and **−3** to the score when the word never appears — unless the title already says Python;
- nothing without a description (the row keeps *verify manually*).

The full text is in the panel and on the vacancy page, every *Python* highlighted. The quote shows the context: Python in "nice to have" counts the same as in the requirements, so read it. Also available as an auto-ignore rule in [settings.md](settings.md).

## Buttons

- `interested` / `ignore` / `applied`, *ignore with reason* and `rejected` act at once. Only the list items they change are redrawn: that vacancy and the same company's other vacancies (#138). A vacancy that no longer belongs to the tab leaves the list, and the panel moves to the next one. In **All** it stays, with its new state.
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
| Hacker News | Remote Python posts from "Ask HN: Who is hiring?" ([hackernews.md](hackernews.md)) |
| Remote OK | Fresh Python jobs from a remote-only board ([remoteok.md](remoteok.md)) |
| Jobicy | Fresh Python jobs from a remote job board ([jobicy.md](jobicy.md)) |
| Other boards | Remotive, Working Nomads, We Work Remotely ([boards.md](boards.md)) |
| Mail analysis | Rejections, applications, contacts from stored mail ([applications.md](applications.md)) |
| LinkedIn descriptions | Starts the background **Descriptions** job, if enabled ([linkedin-public-search.md](linkedin-public-search.md#the-descriptions-button-205)) |
| AI analysis | Work mode, residence and stack with the local model ([ai.md](ai.md)) |

Each line shows the state:
- `·` waiting;
- `…` running;
- `✓` done, with the summary (e.g. "42 new");
- `off` disabled in the settings;
- `✗` failed, with the reason.

The full log is under **details**, and a **reload** link appears at the end. When the sync is over, the status folds into one line ("Sync done 14:57 · details"); open **details** for the steps and the log. **✕** hides it until the next sync, and the browser remembers that, so it stays hidden after a reload (#147).

**Steps are independent.** When one fails, the others still run. Without Gmail access the mail-based steps work on the mail already stored and do not fetch message bodies. The Gmail line then says what to do: *Gmail access expired or revoked: run `./jr auth`*.

`./jr sync` prints the same lines and exits with code 1 when a step failed.

## Phone

At phone width the list fills the screen, and tapping a vacancy opens its page. The page has the same panel content with large buttons; **← Queue** brings you back to the same item. The page works over Tailscale too (`tailscale serve`).

## Look

Since #158 the UI is dark: ground #101318, accent #3D7BFF, IBM Plex Sans with Plex Mono for scores. It is built on Bootstrap 5 from the jsDelivr CDN (#119). Buttons are coloured by meaning:
- interested is green;
- ignore and reasons to ignore are red;
- applied is blue;
- the main actions are filled.

The other pages (Applications, Skills, Settings, Profile, Capture) use the same parts since #168: a page header with a short explanation, content in dark cards, tables with small uppercase headers and Plex Mono for dates and numbers, and status pills (applied blue, rejected red, interview or offer green). On a phone, table rows become cards.

## Add by hand

Some things never reach the mail or the sources Job Radar reads (#182).

**An event.** Under **Company history** (in the panel and on the vacancy page): the kind, the date (today by default) and a short comment, then **Add event**. Kinds: recruiter outreach, call, interview, test task, offer, applied, rejection, other.
- **Applied** records an application (source `manual`) and moves the vacancy to Applied.
- **Rejection** records a rejection by the company, so its other vacancies get the usual penalty.
- The rest are stored as company events. They show in the history; like every history record they have a **×** to delete them ([applications.md](applications.md#deleting-a-record-225)).

**A vacancy.** **+ Add** next to **Analyze…** opens a dialog: title and company (required), link, location, state (To review, Interested or Applied) and a comment. The link decides the source: a LinkedIn job (`/jobs/view/…-<id>`) or an hh.ru vacancy is stored by its id, so a later alert or notice about it joins the same vacancy; any other link is stored as is. The score comes from the same title and location rules as the imports. A link Job Radar already has is refused with the vacancy it belongs to ("Already here: #1567 …"). The new vacancy's page opens.
