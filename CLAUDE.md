# CLAUDE.md

Job Radar: local-first assistant for reviewing job opportunities. Plan and scope live in [Epic #9](https://github.com/dremdem/job-radar/issues/9).

## Workflow (every change)

1. **Issue first.** Every change starts from a GitHub issue. If none exists, create one with goal, scope and "done" criteria.
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
- **LinkedIn access.** Never automate LinkedIn with the user's account: no Playwright/browser automation, no login, no cookies, no clicks, no Easy Apply. LinkedIn data comes from alert emails. Original postings are looked up via official public ATS APIs (#22). Reading LinkedIn's public (guest) job search without login — plain HTTP, no cookies, no browser profile, first 25 results per search taken from the user's alert emails — is allowed only behind an opt-in flag, off by default, rate-limited, stopping on any authwall/429/999 (#23).
- **Gmail is read-only.** Never send, delete or modify mail.
- **Don't invent facts.** Scores, summaries and company history must be based on stored data; say "unknown" instead of guessing.
- **Secrets** live only in `.env` or `secrets/` (both git-ignored). Never commit tokens, credentials or personal data (`*.db`, exported mail).
