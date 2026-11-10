# Security policy

Job Radar runs on your own machine and keeps its data in a local SQLite file. It has no server of its own, and nothing is reachable from outside unless you expose it yourself. The web UI listens on `127.0.0.1` only.

## Reporting a vulnerability

Please report security problems privately, not in a public issue. Use GitHub's **Report a vulnerability** button on the [Security tab](https://github.com/dremdem/job-radar/security). You should get an answer within a week.

Useful details: what an attacker can do, how to reproduce it, and the commit you tested.

## Supported versions

Only the latest `main` is supported. There are no releases.

## What the repository checks

Every PR and `main` run secret scans over the full history (gitleaks, trufflehog), `pip-audit`, `bandit`, CodeQL, and `zizmor` and `actionlint` on the workflows. OpenSSF Scorecard runs on every change to `main`. Actions are pinned to commit SHAs, and Dependabot keeps them and the Python dependencies current.
