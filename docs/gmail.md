# Gmail

Read-only import of job-related mail into `EmailMessage` (#12). Nothing is ever sent, deleted or modified.

## Setup (once)

1. Google Cloud project with the Gmail API enabled:
   ```sh
   gcloud projects create <project-id> --name="Job Radar"
   gcloud services enable gmail.googleapis.com --project <project-id>
   ```
2. In the console, **Google Auth Platform** (`https://console.cloud.google.com/auth/overview?project=<project-id>`):
   - *Get started*: app name, support email, audience **External**;
   - *Audience → Test users*: add your Gmail address;
   - *Clients → Create client*: type **Desktop app**, download the JSON.
   - Optional: *Audience → Publish app*. In *Testing* mode refresh tokens expire after 7 days; a published personal app only shows an "unverified app" warning at login.
3. Save the JSON as `secrets/gmail_client_secret.json` (git-ignored).
4. Authorize: `uv run job-radar gmail auth`. A browser opens; after consent the token is written to `secrets/gmail_token.json` (mode 600).

Scope: `https://www.googleapis.com/auth/gmail.readonly` only.

## Sync

```sh
uv run job-radar sync gmail                  # all queries, incremental
uv run job-radar sync gmail --query job_proposal
uv run job-radar sync gmail --since 2026-01-01
```

Queries (`job_radar/gmail/queries.py`):

| Name | Gmail query | Stores body | First run looks back |
|---|---|---|---|
| `linkedin_alerts` | `from:(jobalerts-noreply@linkedin.com OR jobs-noreply@linkedin.com)` | yes (HTML + text) | 30 days |
| `job_proposal` | `label:job-proposal` | no | everything |

## How it works

- **Threads, not messages.** Each query is run with `threads.list`, and every message of a matching thread is stored. This is how replies you sent inside a job thread (`direction = out`) are picked up without scanning all of `in:sent`. A wider `in:sent` search for applications outside the label belongs to history backfill (#15).
- **Idempotent.** `EmailMessage.gmail_message_id` is unique; an already stored message only gets the query name added to `categories`.
- **Incremental.** `SyncCursor` keeps, per query, the date of the newest message seen. The next run adds `after:<cursor - 1 day>` to the query; the overlap is harmless because of the unique message id. `--since` overrides the cursor for one run.
- **Quota.** Gmail limits query cost per user per minute (6000 units for a new project). Requests are paced (0.5 s apart); on `429` / `rateLimitExceeded` the client waits 60 s for the next window and retries, up to 5 times. First import of ~1700 threads takes about 15–20 minutes and prints progress every 50 threads.
- **Resumable.** Messages are committed per thread; if a run is interrupted, the next one skips what is stored. The cursor moves only after a query finishes.
- **Direction** is `out` when the message has the `SENT` label, otherwise `in`.

## Privacy decisions

- Headers (date, from, to, subject), labels and Gmail's `snippet` are stored for every message.
- **Full bodies are stored only for LinkedIn alerts**: they are generated mailings, and the parser (#13) needs their HTML. Private correspondence (`job_proposal`) keeps headers + snippet; if a feature needs a body later, it fetches it by message id and extracts facts without persisting the raw text.
- Mail content is never sent to external AI APIs.
