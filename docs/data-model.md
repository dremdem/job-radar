# Data model

Minimal schema for step 1 (#11). Tables grow only when a source or feature needs them.

```
Company 1───* Opportunity 1───* SourceRecord 1───* JobSnapshot
   │               │
   ├──* Application *┘ (opportunity optional)
   └──* Rejection   (opportunity + email optional)
EmailMessage *───1 Company (optional)
```

## Company

One real employer. `normalized_name` is unique and used for dedup.

| Column | Notes |
|---|---|
| `name` | Display name as first seen |
| `normalized_name` | Lowercase, punctuation and legal suffixes (`Inc`, `Ltd`, `LLC`, `GmbH`, …) removed. Unique |
| `aliases` | JSON list of other normalized names that resolve to this company |
| `domains` | JSON list of email / web domains |

## Opportunity

One vacancy, independent of where it was seen.

| Column | Notes |
|---|---|
| `title`, `company_id` | |
| `state` | `new` → `interested` / `ignore` / `applied`. Changed only by the user |
| `score` | Nullable float, 0–10; rule-based for now (#13). Intrinsic (title/location) score; the rejection penalty is applied at display time, see [Rejection](#rejection) |
| `score_reasons` | JSON list of `{"sign": "+" or "-", "text": ...}` explaining the score |
| `location`, `salary` | As shown by the source, free text |
| `easy_apply` | Nullable bool: LinkedIn Easy Apply; unknown (null) for public search |
| `created_at`, `updated_at` | UTC |

## SourceRecord

One sighting of an opportunity in a source. Unique on `(source_type, external_id)`, so re-running an import never creates duplicates.

| Column | Notes |
|---|---|
| `source_type` | `linkedin_alert`, `linkedin_search` (public guest search, [linkedin-public-search.md](linkedin-public-search.md)), `gmail`, `himalayas`, `telegram`. A LinkedIn job id is one opportunity whichever LinkedIn source saw it |
| `external_id` | Stable id inside the source (e.g. LinkedIn job id) |
| `url` | Canonical URL, tracking parameters stripped |
| `opportunity_id` | |

## JobSnapshot

Raw text of a vacancy at a point in time. Unique on `(source_record_id, content_hash)`: the same text is stored once, a changed text becomes a new snapshot.

| Column | Notes |
|---|---|
| `raw_text` | |
| `content_hash` | SHA-256 of `raw_text` |
| `retrieved_at` | UTC |

## Application

A fact that the user applied somewhere. Built from mail by [applications.md](applications.md) or entered by hand.

| Column | Notes |
|---|---|
| `company_id` | Required |
| `opportunity_id` | Optional: set when the LinkedIn job id matches a known opportunity |
| `applied_at` | Date of the confirmation email |
| `role` | Nullable: unknown when the email does not say |
| `status` | `applied` by default; shown as `rejected` when a later rejection exists |
| `source` | `linkedin`, `ats`, `manual` |
| `review` | `confirmed`, `suggested` (needs the user), `dismissed` (kept so it is not re-suggested) |
| `email_message_id` | Unique, nullable: the email it came from |
| `linkedin_job_id` | Nullable: from the "View job" link |
| `notes` | |

## Rejection

A company said no. A rejection is a fact about the **company**: one rejection flags
**all** of that company's opportunities in the review queue. Recorded manually (a
button in the queue) or auto-detected from mail (#28).

| Column | Notes |
|---|---|
| `company_id` | Required. The rejecting company |
| `opportunity_id` | Optional: the opportunity the rejection was recorded from |
| `email_message_id` | Optional: the source email (auto-detect). Unique, so re-runs don't duplicate |
| `source` | `manual` / `gmail` |
| `role` | Optional: role the rejection was for (from the email subject) |
| `rejected_at` | When rejected (click time for manual, email date for auto) |
| `created_at` | UTC |

Queue behaviour: a company with ≥1 rejection has every opportunity marked with a
`rejected · <date>` badge (the most recent `rejected_at`), a `− Rejected by …`
reason, an effective score of `score − REJECTION_PENALTY` (clamped at 0), and is
sorted to the very bottom — still visible, so the user can decide whether an old
rejection is worth a retry. The manual toggle only removes `manual` rejections;
auto-detected ones stay.

## EmailMessage

A Gmail message relevant to the search. Unique on `gmail_message_id`.

| Column | Notes |
|---|---|
| `gmail_message_id`, `gmail_thread_id` | |
| `direction` | `in` / `out` |
| `sent_at`, `from_addr`, `to_addr`, `subject` | |
| `snippet` | Gmail snippet |
| `label_ids` | JSON list of Gmail label ids |
| `categories` | JSON list of sync query names that returned it (`linkedin_alerts`, `job_proposal`) |
| `body_html`, `body_text` | Stored only for LinkedIn alerts, see [gmail.md](gmail.md#privacy-decisions) |
| `detected_event` | Nullable: `application_confirmation`, `application_update`, `rejection`, `interview`, `recruiter_outreach`, `other` |
| `company_id` | Optional link, see [applications.md](applications.md#linking-mail-to-a-company) |
| `company_match` | Nullable: `exact` or `probable` |

## SyncCursor

Per-query position of an incremental import.

| Column | Notes |
|---|---|
| `name` | Primary key, e.g. `gmail:linkedin_alerts` |
| `last_message_at` | Date of the newest message seen |
| `last_run_at` | UTC time of the last successful run |

## LinkedInFetch

Audit log of requests to LinkedIn's public guest search (`linkedin_fetches`).

| Column | Notes |
|---|---|
| `at` | UTC |
| `email_message_id` | The alert whose search was run |
| `keywords`, `geo_id`, `start` | What was requested |
| `http_status` | Nullable |
| `status` | `ok`, `blocked`, `error` |
| `jobs` | Cards on the page |

## OpportunityNote

A reason to ignore a vacancy, or a free note (`opportunity_notes`). See [applications.md](applications.md#reasons-and-notes).

| Column | Notes |
|---|---|
| `opportunity_id`, `company_id` | The vacancy and its company (company kept for history and score) |
| `kind` | `only_location`, `language`, `stack`, `seniority`, `salary`, `contract`, `other`, `note` |
| `detail` | For `only_location`: the country or region; for `language`: the language |
| `comment` | Optional free text |
| `created_at` | UTC |
