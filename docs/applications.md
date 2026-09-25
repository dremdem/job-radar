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

# Company history

Before applying, the queue shows what already happened with the company (#14): applications, rejections and other mail. It is a warning, never a block.

## Linking mail to a company

`EmailMessage.company_id` + `company_match`:

| How | Match |
|---|---|
| The email is an application confirmation, a rejection or LinkedIn's "application was viewed by <company>" (company from its subject / sender) | `exact` |
| Another message in the same Gmail thread is linked (your replies, the recruiter's follow-ups) | `exact` |
| Sender domain is a known domain of the company, learned from exactly linked mail (free-mail and ATS domains are never learned) | `exact` |
| The company name appears as whole words in the subject; only names of at least two words or 6+ characters, to avoid "Rain" or "Known" matching everywhere | `probable`; `exact` when the sender's own domain carries the name (`anna@northwind.com`), and then the domain is learned |

## What an email is

Linked mail without an event gets a rule-based label in `detected_event` (a hint for the history list, never a status change):

- `viewed`: LinkedIn "Your application was viewed by <company>";
- `interview`: interview / собеседование / meeting invite / technical task / next steps / scheduling links;
- `rejection`: the rejection wording from [gmail.md](gmail.md#rejections) in subject or snippet (ATS and direct mail too); also recorded as a `Rejection` when the company is known;
- `recruiter_outreach`: the thread was started by someone else and is not an ATS or LinkedIn notification;
- `other`.

## In the queue

- **contacted · N** with the latest events, **expanded by default**: date, type, role or subject, link to the Gmail thread; an extra warning when you already applied. **Expand all** / **Collapse all** above the queue toggle every list at once;
- or **no contacts found**, stated explicitly.

## Commands

```sh
uv run job-radar import contacts   # link and label mail already in the database
uv run job-radar sync gmail        # runs it last
```

# Reasons and notes

`ignore` is about the vacancy, `rejected` is about the company (it said no). When a vacancy does not fit, say why:

- **Ignore with a reason** (the row's *reason / note* form): `only <country>` (the country is prefilled from the location and can be edited), `stack`, `seniority`, `salary`, `contract`, `other`, plus an optional comment. The vacancy becomes `ignore`. A plain `ignore` button still works without a reason.
- **Note**: the same form without ignoring, for any vacancy ("ask about EOR").

Both are stored in `opportunity_notes` and appear in the company history as `ignored` / `note` events (date, reason, comment), with a delete button.

## Effect on the score

When a company had a vacancy ignored as `only <country>`, its **other** vacancies get a risk:

- `− Last time <company> hired only in <country> (<date>)`: **−1.5**;
- **−3** when the new vacancy is in the same country again.

Only the latest such reason counts. Like the rejection penalty it is applied when the queue is shown, so it also affects vacancies already in the queue, and deleting the reason removes it. The company is never blocked. Other reasons (`stack`, `salary`, …) are shown in the history but do not change the score yet.
