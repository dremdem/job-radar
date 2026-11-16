# Application history

Past applications reconstructed from Gmail (#15), so the queue can say "you already applied here" (#14). Everything is read-only: nothing is sent, and the user confirms what the rules only guess.

## Sources

| Source | Recognized by | Company | Role | Review |
|---|---|---|---|---|
| LinkedIn | `jobs-noreply@linkedin.com`, subject `…your application was sent to <company>` | subject | body: first line after the heading; LinkedIn job id from the "View job" link | **confirmed** (LinkedIn states it as a fact) |
| ATS confirmation | sender domain of an applicant tracking system (Workable, Ashby, Lever, Greenhouse, Teamtailor, SmartRecruiters, Workday, JazzHR/applytojob, Recruitee, BambooHR, Personio, Breezy, Jobvite, iCIMS, HiBob) **and** subject/snippet saying the application was received ("Thank you for applying", "we received your application", …) | sender display name, else subject ("applying to X", "… at X"), else ATS subdomain (`northwind.teamtailor-mail.com`) | subject/snippet when present ("applying to the <role> position", "<role> - <company>", "… — <role> at <company>") | **suggested** (heuristic; confirm or dismiss) |

| FDE Times | sender domain `fdetimes.net`, subject `Application received: <role> at <company>` (sent when you apply through the FDE Times form) | subject, after the last " at " | subject, before it | **confirmed**; linked to the vacancy: the FDE Times one of the company with that title, else another one with that title, else a new one. It is marked **Applied**, and the other copies with that title join it. A confirmation the contact step already labelled (`interview`, `other`, `recruiter_outreach`) is taken again (#15) |
| hh.ru | a rejection or "position closed" notice from `noreply@hh.ru` (hh.ru sends nothing when you apply) | body: `Компания: …` | body: `Вакансия: …` | **confirmed**; date = the notice's date, see [hh.md](hh.md) |

Not covered yet: applications sent from your own mailbox, recruiter outreach. Those are part of company history (#14).

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
- **Company** filter above both lists: any part of the name, ignoring case, while you type; kept in the URL (`/applications?company=value`). Counts follow the filter, and *Confirm all* then confirms only the suggestions you see.

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

**ATS domains name no company (#178).** HiBob (`hibob.com`, "… (via Bob)") sends mail for many employers. Before it was on the ATS list it was learned as eCarsTrade's domain, and every HiBob mail went to eCarsTrade. Now an ATS domain found among a company's learned domains is dropped, and its mail moves to the employer named in the subject ("Your application at Pure App was received", "Your Zepz Application for …", "… Role at Zepz"); mail whose subject names no employer stays where it was.
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

## Deleting a record (#225)

Every line of **Company history** (in the panel and on the vacancy page) has a **×**. It asks first, naming the one record ("Delete from the history: 2026-10-20 · ignored · salary · too low?"), and nothing changes if you cancel. Typical case: you clicked Ignore with a reason, then moved the vacancy to Interested; the old `ignored` line can now go.

What deleting does, by kind:

| Record | Effect |
|---|---|
| `ignored` / `note` | the reason or note is deleted; a location reason stops lowering the company's other vacancies |
| an event you added (call, interview, …) | deleted |
| `rejection` | deleted, so the company no longer counts as rejected; when it came from mail, the mail is hidden too |
| `applied` / `applied?` | the application is dismissed, as **Dismiss** on Applications does; the "applied here" badge goes |
| other mail (interview, recruiter outreach, …) | hidden |

Mail is never deleted, in Gmail or in the database: it gets `hidden_at`, and the next sync does not make it an application or a rejection again. The vacancy's own state does not change; set it with the state buttons.
- **Other vacancies here (#209)**: the company's other vacancies you acted on in Job Radar (applied, interested, ignore) with the date and a link, so the same role posted in several cities is not taken for a new one. A vacancy you marked **applied** counts for the **already applied** badge too, not only an application found in the mail; the list item then says "applied here DD.MM".

## Commands

```sh
uv run job-radar import contacts   # link and label mail already in the database
uv run job-radar sync gmail        # runs it last
```

# Reasons and notes

`ignore` is about the vacancy, `rejected` is about the company (it said no). When a vacancy does not fit, say why:

- **Suggested reason, one tap** (#117). When the checks already found why to skip a vacancy, the row (and the vacancy page) shows up to three buttons **Ignore: <reason>**. They come from:
  - the language checks (`language: German`);
  - the local model (`only EU`, `hybrid / on-site`);
  - the profile's blockers (`stack: TensorFlow`);
  - Claude's `skip` verdict, by its main blocker, or `other` with its summary.

  One tap ignores the vacancy with that reason.
- **Reason chips:** **Location** (with the country from the location), **Language**, **Stack**, **Seniority**, **Salary**, **Contract**, **Other**, and an optional comment field next to them. One tap ignores with that reason. **more…** opens the full form, to type a different country or language, or a detail.
- The reasons:
  - `only <country>`;
  - `language: <language>`;
  - `stack: <technology>`;
  - `hybrid / on-site` (kind `work_format`);
  - `seniority`, `salary`, `contract`, `other`.

  The vacancy becomes `ignore`. A plain `ignore` button still works without a reason.
- **Note**: in **more…**, the same form without ignoring, for any vacancy ("ask about EOR").

Both are stored in `opportunity_notes` and appear in the company history as `ignored` / `note` events (date, reason, comment), with a delete button ([Deleting a record](#deleting-a-record-225)).

## Effect on the score

When a company had a vacancy ignored as `only <country>`, its **other** vacancies get a risk:

- `− Last time <company> hired only in <country> (<date>)`: **−1.5**;
- **−3** when the new vacancy is in the same country again.

Only the latest such reason counts. Like the rejection penalty it is applied when the queue is shown, so it also affects vacancies already in the queue, and deleting the reason removes it. The company is never blocked. Other reasons (`stack`, `salary`, …) are shown in the history but do not change the score yet.

## Vacancy language

A title written in another language usually means the job needs that language. The title is checked without any external service (`job_radar/language.py`):

- non-Latin scripts: Chinese, Japanese, Korean, Arabic, Hebrew, Thai, Greek; **Cyrillic is treated as Russian and accepted**;
- Latin-script languages (Spanish, Portuguese, French, German, Italian, Polish, Dutch) by typical words ("Desarrollador", "Entwickler", "Développeur", "Schwerpunkt", "remoto", "h/f", …) and letters (`ñ`, `ç`, `ß`, `ã`, `ł`, …); a single weak hint is not enough;
- English and Russian are fine. Emoji, dashes and gender markers such as `(m/w/d)` in an otherwise English title do not count.

A detected language adds `− Title in <Language>: the job likely needs <Language>` (**−8**, a hard blocker, #115) when the queue is shown, like the other penalties. It is a hint: the title may be localized while the team works in English.

**The description** is checked too (#103), because an English title often hides a description written entirely in German. Long text is judged by its everyday words rather than by role names:
- **Latin script:** the share of each language's common small words is counted ("und", "für", "wir" in German; "de", "para", "con" in Spanish; "the", "and", "you" in English). A language wins when its share is clearly above English: at least 5 % of the words and more than twice the English share.
- **Other scripts:** a script that makes up more than 30 % of the letters (Chinese, Japanese, …; Vietnamese by its diacritics).
- **Cyrillic:** treated as Russian and accepted, shown as info only.

A description in another language adds `− Description in <Language>: the job likely needs <Language>` (**−8**). When the title already gave the language, it is not counted twice. On the real queue (2026-10-03), 8 of 274 descriptions were caught: German 3, Portuguese 2, Spanish, Italian, Polish, Vietnamese.

**A required language in an English text** counts the same (#115). Job Radar looks for a sentence that:
- names a language other than English or Russian (German / Deutsch, Dutch, French, Spanish, Italian, Polish, Chinese / Mandarin, …);
- asks for it at a working level ("fluent", "fluency", "proficiency", "native", "required", "mandatory", "must", "C1", "B2", "fließend", "Deutschkenntnisse"…);
- does not make it optional ("a plus", "nice to have", "advantage", "preferred", "not required", "not a must").

Examples, from real vacancies:
- caught: "Fluent German (C1/C2) is required", "Speak fluent Dutch and/or French", "Italian language proficiency is mandatory", "Sehr gute Deutsch- und Englischkenntnisse";
- not caught: "German is a big plus", "Korean or Mandarin is a strong plus", "German language not required".

It adds `− Requires German — “<quote>”` (**−8**). On the real data (2026-10-03), 13 such sentences were found, with no false alarms among the optional ones.

The optional auto-ignore rule **"Description not in English"** ([settings.md](settings.md)) moves them, and the vacancies that require such a language, out of To review with the reason `language: <Language>`.

Each reason has an icon, shown on its button, in the one-tap suggestions, in the list line and in the company history (#195): location ✈️ (you would have to move), language 🗣️, stack 🧰, hybrid / on-site 🏢, seniority 🎓, salary 💰, contract 📝, other 🤷, note 🗒️.
