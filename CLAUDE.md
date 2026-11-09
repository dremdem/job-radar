# CLAUDE.md

Job Radar: local-first assistant for reviewing job opportunities. Plan and scope live in this repository's issues.

## Workflow (every change)

1. **Issue first.** Every change starts from a GitHub issue. If none exists, create one with goal, scope and "done" criteria.
   - **This repository is public.** Its issues, PRs and commit messages describe the product only. The owner's job search (companies and vacancies applied to, CV, rejections, interview prep, numbers of applications) goes to the private `dremdem/job-radar-private`, never here. Code changes are made only here; the private repository has no code.
2. **Branch.** Branch off fresh `main`, named `<type>/<short-slug>` (`feat/`, `fix/`, `docs/`, `chore/`, `refactor/`, `test/`). Never commit directly to `main`.
3. **PR.** Open a PR that references the issue (`Closes #N`). Merge only when CI is green; squash-merge, delete the branch.
4. Keep PRs small and focused on one issue. Unrelated findings go into new issues, not into the current PR.

## Documentation first, test first

- **Docs first:** before writing code, update the docs that describe the behaviour (README, docstrings, issue description, ADR-style notes if a decision is non-obvious).
- **Test first:** write a failing test that captures the expected behaviour, then implement until it passes. Bug fixes start with a test that reproduces the bug.
- A PR is done when docs, tests and code agree, and `ruff` + `pytest` are green.

## Engineering practices

- Python 3.12+, `uv` for env and deps, `ruff` for lint/format, `pytest` for tests.
- Type hints everywhere; prefer simple functions and explicit data models over clever abstractions.
- No new infrastructure (PostgreSQL, Redis, Celery, Docker) until an issue explicitly calls for it.
- Commit messages: imperative mood, short subject, body explains *why*.

## Project rules

- **Human in the loop.** No auto-apply, no automatic messages to recruiters or companies.
- **LinkedIn access.** Never automate LinkedIn with the user's account: no login, no cookies, no Easy Apply, no browser automation beyond the one case below. LinkedIn data comes from alert emails. Original postings are looked up via official public ATS APIs (#22). Reading LinkedIn's public (guest) job search without login — plain HTTP, no cookies, no browser profile, first 25 results per search taken from the user's alert emails, and the public page of vacancies already in To review to read their description (#79) — is allowed only behind an opt-in flag, off by default, rate-limited, stopping on any authwall/429/999 (#23). A bookmarklet may read a LinkedIn or hh.ru page the user opened and scrolled themselves, only when the user clicks it, and hand it to the local app (#53, #136); it never navigates, clicks, scrolls or sends requests to LinkedIn or hh.ru. The one browser exception (#247, [ADR 0001](docs/adr/0001-chrome-description-fill.md); hh.ru: #260, [ADR 0002](docs/adr/0002-chrome-fill-hh.md)): the **Chrome fill** may drive the user's own Chrome through Playwright MCP `--extension`, in a tab of its own, only to open the pages of LinkedIn and hh.ru vacancies already in To review without a description, expand "show more" in a LinkedIn description and read the page with the bookmarklet's own reader — opt-in flag, off by default, started by the user's button, human pace with random pauses, stopping at once on authwall/login/checkpoint/captcha/any verification page, on 403/429/999, and when the tab is not the expected vacancy. A page saying that this one vacancy is not available (hh.ru's "Вам недоступна эта вакансия", a 404/410 on a vacancy page) is not a block: the vacancy is marked and the run goes on (#272). It never logs in, reads or sets cookies, sends requests to LinkedIn or hh.ru outside the browser, uses Easy Apply, or acts for the user (apply, respond, message, like, follow, save, connect). Apart from the Chrome fill, hh.ru is never fetched automatically (its API is closed to job seekers, #101).
- **Gmail is read-only.** Never send, delete or modify mail.
- **Telegram is read-only.** Read only through the existing telegram-mcp server (no own client or login), only its `read_messages` tool; never send, mark read, join or change anything (#85).
- **Don't invent facts.** Scores, summaries and company history must be based on stored data; say "unknown" instead of guessing.
- **Secrets** live only in `.env` or `secrets/` (both git-ignored). Never commit tokens, credentials or personal data (`*.db`, exported mail), and never put them in issues, PRs or commit messages. Test fixtures from real mail go through `tests/fixtures/sanitize.py`.
