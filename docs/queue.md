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

- **Header:** the tabs (each with its count), a search box, **Analyze ≥ 6.5**, **Sync** and the Claude status.
- **Left: the list.** One compact item per vacancy: the score, colour-coded (green from 8, amber from 6, red below), the title, company · location, and one line that says the most useful thing. That is Claude's verdict ("Claude: apply · 82%"), a suggested reason to ignore ("ignore? only India"), "no description" or "not analyzed".
- **Right: the panel** of the selected vacancy. It has the same content as its own page (**page →**):
  - the state buttons, the one-tap reasons to ignore and the reason chips ([applications.md](applications.md#reasons-and-notes));
  - the links and the reasons behind the score;
  - Claude's analysis with the chat and quick prompts ([claude.md](claude.md));
  - the description with *Python* highlighted, and the company history.

**The order inside a tab:** score after penalties, highest first; vacancies of companies that rejected you go last.

**The country** is the last part of the location ("Lucknow, Uttar Pradesh, India" → India, "Germany (Remote)" → Germany).

**Analyze ≥ 6.5** queues every vacancy in the list with a score of 6.5 or more, a description and no Claude analysis yet.

## Triage and keyboard

Pick a vacancy in the list, read the panel, decide. After **interested**, **ignore**, **applied** or an ignore reason, the list and the counts update. When the vacancy leaves the tab, the panel opens the **next** one, so you can go through the tab one by one.

| Key | Action |
|---|---|
| `J` / `K` | Next / previous vacancy |
| `I` / `X` / `A` | Interested / ignore / applied |
| `Enter` | Open the vacancy page |
| `/` | Search |

Keys are ignored while you type in a field.

## Filters

The search box matches the title, the company and the location, ignoring case and anywhere in the text: `ind` finds India, `EPAM` finds "EPAM Anywhere", `fastapi` finds "Senior Python Developer (FastAPI)". The list updates while you type.

Filters are kept in the URL (`/?tab=review&title=python`), survive switching tabs, and the tab counts show how many vacancies match.

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
| Mail analysis | Rejections, applications, contacts from stored mail ([applications.md](applications.md)) |
| LinkedIn descriptions | Public job pages for To review, if enabled |
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
