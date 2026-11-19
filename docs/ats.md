# Original postings on employer ATS boards

Issue: [#2](https://github.com/dremdem/job-radar/issues/2). Code: `job_radar/sources/ats/`.

A LinkedIn alert gives a title, a company and a location, but no description, and for a
vacancy without Easy Apply the real application happens on the employer's own careers
site. Many employers publish that site through an ATS (applicant tracking system) whose
vendor offers an official public job board API. The **ATS originals** sync step looks
the vacancy up there and, when it is sure, adds the original posting to the vacancy:
its full description and its real link.

Nothing here touches LinkedIn, and nothing is sent to a third party: plain HTTPS requests
from your machine to the vendor's documented public endpoint, the same data the
employer's careers page shows.

| Vendor | Endpoint | Docs |
|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/<slug>/jobs?content=true`, `/v1/boards/<slug>` for the board's name | [Job Board API](https://developers.greenhouse.io/job-board.html) |
| Lever | `api.lever.co/v0/postings/<slug>?mode=json` | [Postings API](https://github.com/lever/postings-api) |
| Ashby | `api.ashbyhq.com/posting-api/job-board/<slug>` | [Public job posting API](https://developers.ashbyhq.com/docs/public-job-posting-api) |
| Workable | `apply.workable.com/api/v1/widget/accounts/<slug>?details=true` | the careers widget's endpoint |

## Which vacancies

To review vacancies that came from a LinkedIn alert, are not Easy Apply (or unknown), and
have no ATS source yet. A vacancy is looked up again a week after an unsuccessful lookup;
one that failed with an error is retried on the next run.

## Finding the employer's board

The board is identified by its **slug**, the employer's tenant name in the URL
(`jobs.lever.co/<slug>`). Boards are kept in `ats_boards`, apart from the company's
aliases, with how sure the link is:

- **verified**
  - `link`: a tenant link already in stored data about the company: the URL of one of its
    vacancies from another source, or a stored email of the company
    (`job-boards.greenhouse.io/<slug>/…`, `jobs.lever.co/<slug>/…`,
    `jobs.ashbyhq.com/<slug>`, `apply.workable.com/<slug>/…`);
  - `name`: a slug guessed from the company name that the API knows, and whose own company
    name (Greenhouse and Workable give one) is the company's;
  - `manual`: confirmed by you with `job-radar ats confirm`.
- **guessed**: a slug guessed from the name that the API knows but cannot tie to the
  company (Lever and Ashby give no company name). Its postings are only shown as
  candidates, never attached.
- **absent**: a guess the API did not know; not asked again for 30 days.

A mail sender domain like `greenhouse-mail.io` or `workablemail.com` only says which
vendor sent the mail, not which employer's board it is, so it is never used as a slug.

Guesses: the normalized company name joined (`acmerobotics`) and with hyphens
(`acme-robotics`), on Greenhouse, Lever, Ashby, Workable in this order, stopping at the
first board found. Turn guessing off with `JOB_RADAR_ATS_GUESS=false`.

## Matching

Each board is read **once per run**, and every waiting vacancy of the company is matched
against that one answer.

The title is only for finding candidates: titles are compared after lowercasing, dropping
punctuation and expanding `Sr.`/`Jr.`. Two teams can post the same title in the same city,
so a posting is attached only when:

- it is on a **verified** board;
- it is the **only** candidate whose location is compatible with the alert's, and no other
  candidate's location is unknown.

Locations are compatible when they name a common place (`Berlin, Germany` / `Berlin,
Berlin, Germany`; `US` counts as `United States`), or when both are remote and one names
no place (`Remote` / `European Union (Remote)`). A missing location is **unknown**, never
compatible.

| Result | When | What happens |
|---|---|---|
| `matched` | one certain posting | The ATS `SourceRecord` is added to the existing vacancy, the description stored as a snapshot, the auto-ignore rules run |
| `ambiguous` | several possible postings, a posting with an unknown location, or a match on a guessed board | Candidates kept; `job-radar ats candidates` lists them |
| `no_match` | no posting with that title in a compatible location | Postings with the title elsewhere are kept as candidates |
| `no_board` | no board found | |
| `taken` | the posting is already on another vacancy | Nothing is moved |
| `error` | a board could not be read | Retried on the next run |

On a match the vacancy keeps its alert source, its state and its history. The ATS source
is `greenhouse` / `lever` / `ashby` / `workable` with `external_id = "<slug>:<job id>"`:
job ids are unique only within one board. The description then goes through the same path
as any other: description checks in the queue, the local model and Claude analysis.
Running again adds no second source record and no unchanged snapshot. A posting that later
disappears from the board deletes nothing.

The text of a Lever posting joins `descriptionPlain`, every `lists[]` section (that is
where the requirements usually are) and `additionalPlain`.

## Errors and limits

- A 404 means the board does not exist. A 429, a 5xx, a timeout, a body that is not JSON,
  an unexpected shape or a body over 20 MB is a **source error**: shown in the sync status,
  stored on the board and on the vacancy's lookup, and never taken as "no match". Only a
  valid empty list is an empty board.
- 30 s timeout per request, a random 1–3 s pause between requests, at most
  `JOB_RADAR_ATS_MAX_REQUESTS` (60) requests per run; the rest waits for the next run.

## Settings

| Variable | Default | |
|---|---|---|
| `JOB_RADAR_ATS` | `true` | Run the step on every sync |
| `JOB_RADAR_ATS_GUESS` | `true` | Guess slugs from company names when no link is known |
| `JOB_RADAR_ATS_MAX_REQUESTS` | `60` | Requests per run |

## Commands

| Command | |
|---|---|
| `job-radar ats run` | Run the step on its own |
| `job-radar ats boards` | Known boards, their status and how each was found |
| `job-radar ats candidates` | Vacancies whose original is not certain, with the postings it could be |
| `job-radar ats confirm COMPANY PROVIDER SLUG` | Record a board by hand (e.g. `job-radar ats confirm "Acme" greenhouse acme`); the company's unmatched vacancies are looked up again on the next run |

Not yet: picking a candidate in the web UI.
