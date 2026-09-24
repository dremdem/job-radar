# Data model

Minimal schema for step 1 (#11). Tables grow only when a source or feature needs them.

```
Company 1───* Opportunity 1───* SourceRecord 1───* JobSnapshot
   │               │
   └──* Application *┘ (opportunity optional)
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
| `score` | Nullable float; filled by scoring (#13) |
| `created_at`, `updated_at` | UTC |

## SourceRecord

One sighting of an opportunity in a source. Unique on `(source_type, external_id)`, so re-running an import never creates duplicates.

| Column | Notes |
|---|---|
| `source_type` | `linkedin_alert`, `gmail`, `himalayas`, `telegram` |
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

A fact that the user applied somewhere, with its current status.

| Column | Notes |
|---|---|
| `company_id` | Required |
| `opportunity_id` | Optional: historic applications may not map to a known vacancy |
| `applied_at`, `status`, `notes` | `status` is free text for now (`applied`, `rejected`, `interview`, …) |

## EmailMessage

A Gmail message relevant to the search. Unique on `gmail_message_id`.

| Column | Notes |
|---|---|
| `gmail_message_id`, `gmail_thread_id` | |
| `direction` | `in` / `out` |
| `sent_at`, `from_addr`, `to_addr`, `subject` | |
| `detected_event` | Nullable: `alert`, `application_confirmation`, `rejection`, `interview`, … |
| `company_id` | Optional link |
