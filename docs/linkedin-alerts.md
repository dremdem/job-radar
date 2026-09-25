# LinkedIn job alerts

LinkedIn job alert emails become opportunities in the review queue (#13). Nothing ever talks to linkedin.com; see the LinkedIn rule in [CLAUDE.md](../CLAUDE.md).

## Input

Alert emails from `jobalerts-noreply@linkedin.com`, stored with their HTML by `job-radar sync gmail` (query `linkedin_alerts`, see [gmail.md](gmail.md)). Other `jobs-noreply@` mail (application confirmations) is ignored here; it feeds application history (#15).

Tip: alerts are usually deleted after review. Run `job-radar sync gmail` first, or add a Gmail filter that labels them.

## What is extracted

Each alert has up to ~6 job cards. The format has been stable since at least 2022:

```
<title>
<company> · <location>
[<salary>]                 e.g. "€70K-€85K / year"
[Actively recruiting] [Easy Apply] [N connection(s)]
```

| Field | Stored in |
|---|---|
| LinkedIn job id | `SourceRecord.external_id` (`source_type = linkedin_alert`) |
| Canonical URL `https://www.linkedin.com/jobs/view/<id>/` | `SourceRecord.url` |
| Title | `Opportunity.title` |
| Company | `Company` via name normalization |
| Location, salary, Easy Apply | `Opportunity.location`, `.salary`, `.easy_apply` |

Links in the email carry personal tokens (`otpToken`, `midToken`, `midSig`, `trk`, …). Only the job id is kept; the rest of the URL is discarded.

## Dedup

- One LinkedIn job id → one `SourceRecord` → one `Opportunity`, however many alerts mention it.
- Re-running the import creates nothing new.

## Score

Rule-based, from title and location only (`job_radar/scoring.py`). Starts at 5, each rule adds or subtracts, result is clamped to 0–10. Every rule that fired is kept as a reason and shown in the queue:

- **positives**: Senior/Staff/Lead, Python, Backend, FastAPI/Django/Flask, Platform/Data;
- **risks**: Frontend, Java/.NET/PHP/… without Python, Junior/Intern, Full-stack, on-site/hybrid, remote limited to one country ("remote in Germany" is not "remote from Thailand").

Only the ~6 jobs in the email are seen; the rest of the search's first page can be read with the opt-in [public search](linkedin-public-search.md).

An alert has no full description, so every such opportunity is marked **verify manually** until a full description is found (#22, #23).

## Commands

```sh
uv run job-radar sync gmail        # syncs mail, then imports alerts
uv run job-radar import linkedin   # import only, from mail already in the database
```
