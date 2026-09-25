# Application history

Past applications reconstructed from Gmail (#15), so the queue can say "you already applied here" (#14). Everything is read-only: nothing is sent, and the user confirms what the rules only guess.

## Sources

| Source | Recognized by | Company | Role | Review |
|---|---|---|---|---|
| LinkedIn | `jobs-noreply@linkedin.com`, subject `…your application was sent to <company>` | subject | body: first line after the heading; LinkedIn job id from the "View job" link | **confirmed** (LinkedIn states it as a fact) |
| ATS confirmation | sender domain of an applicant tracking system (Workable, Ashby, Lever, Greenhouse, Teamtailor, SmartRecruiters, Workday, JazzHR/applytojob, Recruitee, BambooHR, Personio, Breezy, Jobvite, iCIMS) **and** subject/snippet saying the application was received ("Thank you for applying", "we received your application", …) | sender display name, else subject ("applying to X", "… at X"), else ATS subdomain (`northwind.teamtailor-mail.com`) | subject/snippet when present ("applying to the <role> position", "<role> - <company>", "… — <role> at <company>") | **suggested** (heuristic; confirm or dismiss) |

Not covered yet: hh.ru (company only in the body), applications sent from your own mailbox, recruiter outreach. Those are part of company history (#14).

## Bodies

LinkedIn confirmations are generated mail. For the ones stored without a body (they came in through `label:job-proposal`, headers only), the body is fetched from Gmail by id, **role and job id are extracted, the body is not stored** — the same rule as for rejections ([gmail.md](gmail.md#privacy-decisions)). ATS confirmations use subject and snippet only.

## Dedup

- One email → at most one `Application` (`email_message_id` is unique); a processed email gets `EmailMessage.detected_event = "application_confirmation"`.
- The same application often produces a LinkedIn and an ATS email. A new record is not created when the same company already has an application within **3 days**; the email is linked to it for history.
- A dismissed suggestion stays in the table (`review = dismissed`), so re-running never brings it back.

## Status

`Application.status` is shown as **rejected** when a rejection for the same company arrived on or after the application date (see [data-model.md](data-model.md#rejection)); otherwise it stays **applied**. The stored value is not rewritten.

## Review

`/applications` in the web UI:

- **To review**: suggested applications with *Confirm* / *Dismiss*, and *Confirm all*.
- **History**: confirmed applications, newest first: date, company, role, status, link to the Gmail thread.

## Commands

```sh
uv run job-radar import applications            # from mail already in the database
uv run job-radar sync gmail                      # syncs, then imports alerts, rejections, applications
```
