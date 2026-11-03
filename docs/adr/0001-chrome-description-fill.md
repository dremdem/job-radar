# ADR 0001: LinkedIn descriptions through the user's own Chrome

- Status: accepted by the owner, 2026-11-02
- Issue: #247
- Changes: the "LinkedIn access" rule in CLAUDE.md. This is a changed requirement, not a fix.

## Context

LinkedIn vacancies reach To review without a description, from alert emails and from the
bookmarklet on search lists. To get a description, the user opened each vacancy and clicked
the bookmarklet: 20–50 identical clicks per batch, before Claude can analyse anything. The
guest page (#79) is opt-in and often empty or blocked. Until now the rule forbade any browser
automation with the user's account.

## Decision

Allow exactly one kind of automation, the **Chrome fill**:

- **What:** Playwright MCP (`@playwright/mcp --extension`) drives the user's own Chrome,
  which is already open, through the "Playwright Extension". There is no separate profile,
  no login and no cookie handling: the browser is the user's own, as when they click by hand.
- **Which pages:** only `https://www.linkedin.com/jobs/view/<id>/`, for vacancies already in
  To review (`new`) that have no description. Inside such a page the only interaction is
  expanding the folded description ("show more"). No other link, button or page is used.
- **Where:** in a tab of its own, opened by the run, never the tab the user works in. Before
  reading, the run checks that the tab shows the expected vacancy. If it does not, the run
  stops and saves nothing: the extension follows the active tab, so the user may have
  clicked somewhere else.
- **How it reads:** with the bookmarklet's own reader (`web/capture_read.js`, one source for
  both paths), into the same `import_capture`. Only the vacancy itself is saved, never the
  "similar jobs" on its page.
- **Pace:** one page at a time, with a random pause of its own between pages (Settings →
  Fill from Chrome, 2–4 s by default; #252: by hand the owner opens a vacancy and clicks the
  bookmarklet within seconds), on top of the time each page takes to load and show its
  description (polled, at most 10 s; #254 replaced a fixed 2.5–5 s wait), and at
  most 50 vacancies per run. The guest pause (10–40 s) is separate and unchanged: requests
  without a login get 429 much sooner. There is no daily cap: a person
  opens as many by hand.
- **Stops at once on:** an authwall, login, checkpoint, captcha or any other verification
  page; HTTP 429 or 999; a different vacancy in the tab; the Stop button.
- **Opt-in:** `JOB_RADAR_LINKEDIN_CHROME=true`, off by default. Only the user's button
  starts a run.

Still forbidden: logging in, reading or setting cookies, requests to LinkedIn outside the
browser, Easy Apply, and any action on the user's behalf (apply, message, like, follow, save,
connect). hh.ru stays manual only.

## Proposed text of the rule (CLAUDE.md, "LinkedIn access")

Replace the first sentence with:

> Never automate LinkedIn with the user's account: no login, no cookies, no Easy Apply, no
> browser automation beyond the one case below.

After "…it never navigates, clicks, scrolls or sends requests to LinkedIn or hh.ru." add:

> The one browser exception (#247, [ADR 0001](docs/adr/0001-chrome-description-fill.md)): the
> **Chrome fill** may drive the user's own Chrome through Playwright MCP `--extension`, in a
> tab of its own. It may only open the pages of LinkedIn vacancies already in To review
> without a description, expand "show more" in that description, and read it with the
> bookmarklet's own reader. It is behind an opt-in flag, off by default, and started only by
> the user's button. It runs at human pace with random pauses, and stops at once on an
> authwall, login, checkpoint or any verification page, on 429/999, and when the tab is not
> the expected vacancy. It never logs in, reads or sets cookies, sends requests to LinkedIn
> outside the browser, uses Easy Apply, or acts for the user (apply, message, like, follow,
> save, connect).

## Consequences

- The run happens in the user's real session, so LinkedIn sees the user. The pace and the
  stop rules keep it looking like a person clicking, and as gentle as one.
- If LinkedIn changes its markup, the bookmarklet and the fill break together, and are fixed
  together.
- The extension shows a connect dialog for every new connection unless
  `PLAYWRIGHT_MCP_EXTENSION_TOKEN` (shown by the extension) is set in `.env`.
