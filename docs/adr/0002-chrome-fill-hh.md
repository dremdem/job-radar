# ADR 0002: hh.ru descriptions through the user's own Chrome

- Status: asked for by the owner, 2026-11-03; accepted when the CLAUDE.md rule below is merged
- Issue: #260
- Extends: [ADR 0001](0001-chrome-description-fill.md) (the Chrome fill for LinkedIn)
- Changes: the "LinkedIn access" rule in CLAUDE.md ("hh.ru is never fetched automatically").
  This is a changed requirement, not a fix.

## Context

hh.ru vacancies reach To review without a description, from the saved-search digests
(docs/hh.md). hh.ru's API is closed to job seekers (#101), so until now the only way to get a
description was the bookmarklet (#136): open each vacancy, click it, 20–50 times per batch.
The Chrome fill (ADR 0001) already does exactly this for LinkedIn, and the bookmarklet's
reader already reads hh.ru vacancy pages. The owner asked for the same button to fill hh.ru
too, automatically.

## Decision

The Chrome fill also opens hh.ru vacancies, under the same limits as LinkedIn:

- **Which pages:** only `https://hh.ru/vacancy/<id>`, for vacancies already in To review
  (`new`) with source `hh` and no description. There is nothing to expand on hh.ru: the
  page shows the whole description, so the run does not click anything on it.
- **Where, how it reads, pace:** as in ADR 0001: the same run, the same tab of its own, one
  page at a time in score order (LinkedIn and hh.ru together), the same pause between pages
  (Settings → Fill from Chrome), at most 50 vacancies per run, the bookmarklet's own reader
  into the same import as the bookmarklet.
- **Stops at once on:** a login page, a captcha ("не робот") or any other verification page;
  HTTP 403, 429 or 999; a different page in the tab; the Stop button. Stopping covers the
  whole run, LinkedIn included.
- **Opt-in:** the same `JOB_RADAR_LINKEDIN_CHROME=true` and the same button. Only the user's
  button starts a run.

Still forbidden on hh.ru: logging in, reading or setting cookies, requests to hh.ru outside
the browser (its API included), and any action on the user's behalf (respond to a vacancy,
message, save, subscribe, hide).

## Proposed text of the rule (CLAUDE.md, "LinkedIn access")

Replace

> The one browser exception (#247, [ADR 0001](docs/adr/0001-chrome-description-fill.md)): the
> **Chrome fill** may drive the user's own Chrome through Playwright MCP `--extension`, in a
> tab of its own, only to open the pages of LinkedIn vacancies already in To review without a
> description, expand "show more" in that description and read it with the bookmarklet's own
> reader — opt-in flag, off by default, started by the user's button, human pace with random
> pauses, stopping at once on authwall/login/checkpoint/any verification page, on 429/999, and
> when the tab is not the expected vacancy. It never logs in, reads or sets cookies, sends
> requests to LinkedIn outside the browser, uses Easy Apply, or acts for the user (apply,
> message, like, follow, save, connect). hh.ru is never fetched automatically (its API is
> closed to job seekers, #101).

with

> The one browser exception (#247, [ADR 0001](docs/adr/0001-chrome-description-fill.md);
> hh.ru: #260, [ADR 0002](docs/adr/0002-chrome-fill-hh.md)): the **Chrome fill** may drive the
> user's own Chrome through Playwright MCP `--extension`, in a tab of its own, only to open the
> pages of LinkedIn and hh.ru vacancies already in To review without a description, expand
> "show more" in a LinkedIn description and read the page with the bookmarklet's own reader —
> opt-in flag, off by default, started by the user's button, human pace with random pauses,
> stopping at once on authwall/login/checkpoint/captcha/any verification page, on 403/429/999,
> and when the tab is not the expected vacancy. It never logs in, reads or sets cookies, sends
> requests to LinkedIn or hh.ru outside the browser, uses Easy Apply, or acts for the user
> (apply, respond, message, like, follow, save, connect). Apart from the Chrome fill, hh.ru is
> never fetched automatically (its API is closed to job seekers, #101).

## Amendment (#272, 2026-11-08): an unavailable vacancy is not a block

Asked for by the owner; a changed requirement. hh.ru answers **HTTP 403** on the vacancy's own
URL with the text "Вам недоступна эта вакансия" (a vacancy closed to this account), and "stops
on any 403" made one such vacancy end every run at the same place. That page, and a 404/410 on
a vacancy page, is now a problem of that vacancy: it is marked in the vacancy's event log and
the run goes on. Later runs skip it until the mark is deleted. Captcha, login, 429, 999 and any
other 403 still stop the run at once; three failed vacancies in a row stop it too, so a site
that starts refusing everything is not walked through page by page.

## Consequences

- hh.ru sees the user's own session open vacancy pages, at the pace of a person; it may show
  a captcha sooner than LinkedIn, and the run then stops and leaves the tab open.
- If hh.ru changes its `data-qa` attributes, the bookmarklet and the fill break together, and
  are fixed together.
